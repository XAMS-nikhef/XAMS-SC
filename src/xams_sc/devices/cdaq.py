"""NI cDAQ-9174 service. See DESIGN.md §7.1.

Read-only. The chassis has no output module, so this service has no control
path and never writes anything to hardware.

All four modules are low-rate delta-sigma with differing aggregate rates and
cannot share one hardware-timed task. Slow control reads at 1 Hz, so this uses
**on-demand (software-timed) reads**, one task per module, polled in sequence.
The CompactDAQ timing-engine constraint then does not apply.

**Slow modules are read in the background** (`background: true` in
devices.yaml, 21 October 2026). An RTD read on the 9226 or 9216 takes seconds,
and polled in the same loop it held up the 9207: fewer pressure readings fell
into each 10-s mean, and p102-p104 were 3.5x noisier the moment the 9226 was
enabled again (module in but not read: back to normal). A background module
gets its own thread, reads at its own `interval_s` and hands its readings to
the next `read()`; the 9207 keeps its one read per second. How long each
module's read takes is logged every ten minutes, so the next slow module shows
itself in the log rather than as noise.

Tasks actually created:

    9207     ai0:7 voltage   and ai8:9 current (4-20 mA strain gauges)
    9226     ai0:6 PT1000    (ai7 not connected)
    9216_1   ai0:6 PT100     (ai7 not connected)
    9216_2   none — module entirely unconnected, no task created

Modules are addressed by **alias** (`9207/ai0`), not `cDAQ1Mod1/ai0`. Aliases
are configured in NI-MAX and survive re-slotting.

**A module with a `switch` can have its readout switched off and on while the
service runs** (26 October 2026; the 9226 with the PT1000s, which are
disconnected and grounded while the HV is on). Off stops the module's thread
and closes its task: nothing is read or published for its channels, and the
alarm engine and the pages show them as off. On builds the task again. The
state is published retained on STATUS_READOUT and kept in
data/cdaq_readout.json, so a restart does not quietly start reading grounded
sensors again. Only background modules can be switched: their task is used by
their own thread alone, so closing it cannot pull it from under a read.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..bus import ACK_READOUT, STATUS_READOUT, TOPIC_AUDIT, TOPIC_READOUT
from ..config import ROOT, Channel, Config
from ..model import Measurement, Quality, iso, utcnow
from ..readout import switchable_modules
from ..scaling import apply
from ..service import BaseService

log = logging.getLogger(__name__)

# Excitation current, per module model. MEASURED FROM THE HARDWARE, 17 Sep 2026.
#
# Each module accepts EXACTLY ONE value, and neither is the DAQmx default of
# 2.5 mA — so both must be set explicitly or the task fails to configure:
#
#     NI 9226 (PT1000): 100 uA only. 1 mA and 2.5 mA are both refused.
#     NI 9216 (PT100):  1 mA only.   100 uA and 2.5 mA are both refused.
#
# The physics agrees: 1000 ohm at 1 mA would dissipate a milliwatt in the
# sensor and self-heat it, which is why the PT1000 module runs at a tenth of
# the current. Do not "simplify" this by dropping the argument and letting
# DAQmx choose — §7.1's example code does exactly that, and it does not work.
EXCITATION_A = {
    "NI9226": 100e-6,
    "NI9216": 1e-3,
}

RTD_TYPES = {"PT3851": "PT_3851", "PT3750": "PT_3750"}

# Platinum RTDs to IEC 60751 are defined from -200 to +850 C. A reading outside
# that is not a cold or hot sensor — it is an open circuit, a short, or a
# missing sensor, and the module is reporting the rail rather than a
# temperature.
#
# This is not a threshold and does not belong in alarms.yaml: it is the
# difference between a measurement and the absence of one. Publishing +1326 C
# as quality=ok would be the frozen-plausible-value failure principle 4 exists
# to forbid — and it is how an unconnected input looks on this hardware, which
# is exactly how tt202 was found on 17 September 2026.
RTD_VALID_C = (-200.0, 850.0)

# Valid span of a 4-20 mA loop, in AMPS — DAQmx returns amps, not milliamps.
#
# The same argument as RTD_VALID_C, on the other half of the module. A 4-20 mA
# transmitter cannot read below 4 mA: at zero load it still sends 4. Below
# about 3.5 mA the loop is broken, the transmitter is unpowered, or nothing is
# connected — and scaling that to kilograms produces a confident negative
# weight, which is the frozen-plausible-value failure principle 4 forbids.
#
# The bounds are deliberately a little wider than 4-20 so that a transmitter
# sitting exactly at its endpoint, or a shunt a percent out, is not reported as
# a fault. They detect the ABSENCE of a measurement, not an out-of-range load:
# over-range is a threshold question and belongs in alarms.yaml.
#
# THE LOOPS NEED EXTERNAL 24 V — the 9207 measures current but does not source
# loop power. A channel pinned at 0 mA with the sensor plugged in is the supply.
CURRENT_VALID_A = (0.0035, 0.0205)

# What the task asks the module for, in AMPS. Kept BELOW the 9207's own range
# rather than at it: published specifications for the current inputs give
# +/-21.5 mA in some revisions and +/-22 mA in others, and DAQmx REFUSES a
# max_val outside the module's range — which is a startup failure, not a wrong
# reading, but a startup failure for no reason. 21 mA is inside either figure
# and still a milliamp above the top of a 4-20 mA loop.
#
# It sits above CURRENT_VALID_A's ceiling on purpose: a loop driven over range
# must be able to READ over range, or it clips to the ceiling and passes the
# validity check as though it were fine.
CURRENT_RANGE_A = (0.0, 0.021)

WIRING = {2: "TWO_WIRE", 3: "THREE_WIRE", 4: "FOUR_WIRE"}


@dataclass
class ModuleTask:
    """One DAQmx task: a module, and the channels read from it in order."""

    alias: str
    model: str
    channels: list[Channel]
    task: object | None = None
    # Read in its own thread every `interval_s` instead of in the 1-s loop
    # (devices.yaml `background`, `interval_s`). See the module docstring.
    background: bool = False
    interval_s: float = 5.0
    # DAQmx ADC timing mode for every channel of the module (devices.yaml
    # `adc_timing`), or None for the DAQmx default. See ADC_TIMING.
    adc_timing: str | None = None
    # Read durations since the last report: (count, total seconds, max).
    timing: list = field(default_factory=lambda: [0, 0.0, 0.0])
    # The module's own thread and its stop, so one module can be switched
    # off without stopping the others (devices.yaml `switch`).
    bg_stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

# devices.yaml `adc_timing` -> nidaqmx.constants.ADCTimingMode member.
# 21 October 2026: the 9226 in its default (high resolution) mode converts for
# 1.2 s per read of 6 channels, and while it converts p102-p104 on the 9207 are
# 2-3x noisier. High speed shortens the conversion; the resolution lost does
# not matter for the PT1000s.
ADC_TIMING = {
    "high_speed": "HIGH_SPEED",
    "high_resolution": "HIGH_RESOLUTION",
    "automatic": "AUTOMATIC",
}

# How often the read durations per module are logged.
TIMING_REPORT_S = 600.0


class CdaqService(BaseService):
    name = "cdaq"

    def __init__(self, config: Config, bus, simulate: bool = False,
                 readout_state_path: Path | str | None = None, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._modules: list[ModuleTask] = []
        self._derived: list[Channel] = []
        self._device_serials: dict[str, str] = {}
        # Log an out-of-range RTD once, not every second.
        self._reported_open: set[str] = set()
        # Background modules: their readings wait here for the next read().
        self._bg_lock = threading.Lock()
        self._bg_pending: list[Measurement] = []
        self._bg_stop = threading.Event()
        self._bg_threads: list[threading.Thread] = []
        self._bg_failed: set[str] = set()
        self._next_timing_report = time.monotonic() + TIMING_REPORT_S
        # Switchable readouts (devices.yaml `switch`): alias -> {"enabled",
        # "by", "at"}; a module not in it is on. Read back before the tasks
        # are opened, so a module switched off stays off across a restart.
        self._readout_path = Path(readout_state_path or ROOT / "data" / "cdaq_readout.json")
        self._switchable = switchable_modules(config)
        self._readout: dict[str, dict] = {}
        self._switch_lock = threading.Lock()
        self._restore_readout()
        self._build_tasks()
        self.bus.subscribe(TOPIC_READOUT, self._on_readout_command)
        self.publish_readout()

    # ------------------------------------------------------------------ setup

    def _build_tasks(self) -> None:
        """Group the enabled cdaq channels by module alias.

        A module with no enabled channels gets no task — which is how 9216_2,
        entirely unconnected, ends up costing nothing.
        """
        cfg = self.config.devices.get("cdaq", {})
        models = {m["alias"]: m["model"] for m in cfg.get("modules", [])}
        specs = {m["alias"]: m for m in cfg.get("modules", [])}
        self._device_serials = {m["alias"]: m.get("serial", "")
                                for m in cfg.get("modules", [])}

        by_alias: dict[str, list[Channel]] = {}
        # Derived channels are computed from a read one (`derive` in
        # channels.yaml) and never become a DAQmx task line: they share the
        # physical input of their source, and adding it twice would read the
        # same terminal into two names.
        self._derived = [c for c in self.config.channels_for("cdaq")
                         if c.derive is not None]
        for ch in self.config.channels_for("cdaq"):
            if ch.derive is not None:
                continue
            alias = ch.phys.split("/")[0]
            by_alias.setdefault(alias, []).append(ch)

        for alias, channels in sorted(by_alias.items()):
            channels.sort(key=lambda c: int(c.phys.split("/ai")[1]))
            spec = specs.get(alias, {})
            mod = ModuleTask(alias=alias, model=models.get(alias, ""), channels=channels,
                             background=bool(spec.get("background", False)),
                             interval_s=float(spec.get("interval_s", 5.0)),
                             adc_timing=spec.get("adc_timing"))
            if spec.get("switch") and not mod.background:
                raise ValueError(f"devices.yaml cdaq module {alias}: a `switch` needs "
                                 f"`background: true` (its task must be its own thread's)")
            if mod.adc_timing is not None and mod.adc_timing not in ADC_TIMING:
                raise ValueError(f"devices.yaml cdaq module {alias}: adc_timing "
                                 f"{mod.adc_timing!r} is not one of {sorted(ADC_TIMING)}")
            self._modules.append(mod)
            log.info("module %s (%s): %d enabled channels%s",
                     alias, models.get(alias, "?"), len(channels),
                     (f", read in the background every {mod.interval_s:g} s"
                      if mod.background else "")
                     + (f", ADC timing {mod.adc_timing}" if mod.adc_timing else "")
                     + (", readout SWITCHED OFF" if not self.readout_on(alias) else ""))

        skipped = [a for a in models if a not in by_alias]
        if skipped:
            log.info("no task for %s — no enabled channels", ", ".join(sorted(skipped)))

    # -------------------------------------------------------------- identity

    def verify_identity(self) -> bool:
        """Confirm the chassis and every module are the ones in devices.yaml.

        A module swapped between slots is harmless — aliases follow the module.
        A module *replaced* is not, and that is what this catches.
        """
        if self.simulate:
            log.info("simulate=True: skipping hardware identity check")
            return True

        from nidaqmx.system import System

        cfg = self.config.devices.get("cdaq", {})
        expected_chassis = str(cfg.get("serial", "")).upper().lstrip("0")

        try:
            devices = {d.name: d for d in System.local().devices}
        except Exception as exc:
            log.critical("FATAL: cannot reach NI-DAQmx: %s", exc)
            return False

        chassis_name = cfg.get("chassis", "cDAQ1")
        if chassis_name not in devices:
            log.critical("FATAL: chassis %s not present. Is it powered and connected?",
                         chassis_name)
            return False

        found = f"{devices[chassis_name].serial_num:08X}".upper().lstrip("0")
        if expected_chassis and found != expected_chassis:
            log.critical(
                "FATAL: chassis serial mismatch — devices.yaml says %s, hardware "
                "reports %s. Refusing to start; do not guess.",
                cfg.get("serial"), found)
            return False
        log.info("chassis %s serial %s confirmed", chassis_name, found)

        for mod in self._modules:
            expected = str(self._device_serials.get(mod.alias, "")).upper().lstrip("0")
            if mod.alias not in devices:
                log.critical("FATAL: module alias %r not found in NI-MAX. Aliases are "
                             "configured there and this service addresses modules by "
                             "alias, never by slot.", mod.alias)
                return False
            actual = f"{devices[mod.alias].serial_num:08X}".upper().lstrip("0")
            if expected and actual != expected:
                log.critical(
                    "FATAL: module %s serial mismatch — devices.yaml says %s, "
                    "hardware reports %s. A replaced module needs a deliberate "
                    "config change, not a silent acceptance.",
                    mod.alias, self._device_serials[mod.alias], actual)
                return False
            log.info("module %s serial %s confirmed", mod.alias, actual)

        return self._open_tasks()

    def _open_tasks(self) -> bool:
        """Create one on-demand task per module. Returns False on a clean failure."""
        from nidaqmx.constants import ADCTimingMode

        for mod in self._modules:
            if not self.readout_on(mod.alias):
                log.warning("%s: readout switched OFF (by %s) - no task, its "
                            "channels are not read", mod.alias,
                            self._readout.get(mod.alias, {}).get("by") or "somebody")
                continue
            try:
                task = self._new_task(mod)
                if task is None:
                    self._close_tasks()
                    return False
                # No timing configured: on-demand, software-timed reads (§7.1).
                task = self._start_task(mod, task, ADCTimingMode)
                mod.task = task
                log.info("task for %s ready (%d channels)", mod.alias, len(mod.channels))
            except Exception as exc:
                self._close_tasks()
                if "-200022" in str(exc) or "already been reserved" in str(exc):
                    log.critical(
                        "FATAL: %s in use by another process (LabVIEW running?). "
                        "Refusing to start. Stop the LabVIEW slow-control VI and "
                        "try again — every device admits only one process.",
                        self.config.devices.get("cdaq", {}).get("chassis", "cDAQ1"))
                else:
                    log.critical("FATAL: could not configure %s: %s", mod.alias, exc)
                return False
        self._start_background()
        return True

    def _new_task(self, mod: ModuleTask):
        """A DAQmx task with the module's channels added, not started. None
        on a configuration this service cannot read (logged as FATAL)."""
        import nidaqmx
        from nidaqmx.constants import (CurrentShuntResistorLocation, CurrentUnits,
                                       ExcitationSource, ResistanceConfiguration,
                                       RTDType, TemperatureUnits)

        task = nidaqmx.Task(new_task_name=f"xams_{mod.alias}")
        for ch in mod.channels:
            if ch.kind == "rtd":
                rtd = ch.rtd or {}
                excit = EXCITATION_A.get(mod.model)
                if excit is None:
                    log.critical("FATAL: no excitation current known for "
                                 "module model %r (%s)", mod.model, mod.alias)
                    task.close()
                    return None
                task.ai_channels.add_ai_rtd_chan(
                    ch.phys,
                    rtd_type=getattr(RTDType, RTD_TYPES[rtd["type"]]),
                    resistance_config=getattr(
                        ResistanceConfiguration, WIRING[int(rtd.get("wiring", 3))]),
                    current_excit_source=ExcitationSource.INTERNAL,
                    current_excit_val=excit,
                    r_0=float(rtd["r0"]),
                    units=TemperatureUnits.DEG_C,
                )
            elif ch.kind == "voltage":
                task.ai_channels.add_ai_voltage_chan(ch.phys)
            elif ch.kind == "current":
                # Range given explicitly rather than left to DAQmx.
                # See CURRENT_RANGE_A for why it is 21 mA and not the
                # module's full span. The shunt is internal to the
                # module; there is no external resistor to declare.
                task.ai_channels.add_ai_current_chan(
                    ch.phys,
                    min_val=CURRENT_RANGE_A[0],
                    max_val=CURRENT_RANGE_A[1],
                    units=CurrentUnits.AMPS,
                    shunt_resistor_loc=CurrentShuntResistorLocation.INTERNAL,
                )
            else:
                log.critical("FATAL: channel %s has kind %r, which this "
                             "service cannot read", ch.name, ch.kind)
                task.close()
                return None
        return task

    def _start_task(self, mod: ModuleTask, task, modes):
        """Start the task, with the module's ADC timing mode if it has one.

        DAQmx wants the mode the same on every channel of a module at once:
        set channel by channel, the 9226 refused at the third and then would
        not start at all (-200106, 21 October 2026), which took every cDAQ
        reading down. So the mode is set on all channels in one go, and if the
        task still will not start with it, it is rebuilt without: a slower
        conversion is no reason to lose the temperatures, let alone the
        pressures. Returns the started task.
        """
        if mod.adc_timing is None:
            task.start()
            return task
        try:
            task.ai_channels.all.ai_adc_timing_mode = getattr(modes, ADC_TIMING[mod.adc_timing])
            task.start()
            log.info("%s: ADC timing mode %s", mod.alias, mod.adc_timing)
            return task
        except Exception as exc:
            log.warning("%s: ADC timing mode %s refused (%s); started with the default",
                        mod.alias, mod.adc_timing, exc)
            try:
                task.close()
            except Exception:
                pass
        task = self._new_task(mod)
        if task is None:
            raise RuntimeError(f"{mod.alias}: could not rebuild the task")
        task.start()
        return task

    # ------------------------------------------------- switchable readouts

    def readout_on(self, alias: str) -> bool:
        """Is this module's readout on? Always for a module without a switch:
        a state file left from an earlier configuration cannot stop one."""
        if alias not in self._switchable:
            return True
        return bool(self._readout.get(alias, {}).get("enabled", True))

    def _restore_readout(self) -> None:
        try:
            data = json.loads(self._readout_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._readout = {str(k): dict(v) for k, v in data.items()
                             if isinstance(v, dict)}

    def _save_readout(self) -> None:
        try:
            self._readout_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._readout_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._readout), encoding="utf-8")
            os.replace(tmp, self._readout_path)
        except OSError:
            # In force for this process either way; what is lost is only that
            # it survives a restart - and that is said, not swallowed.
            log.exception("could not persist the readout switches to %s",
                          self._readout_path)

    def readout_view(self) -> dict:
        """Every switchable module and its switch, for STATUS_READOUT."""
        return {alias: {"enabled": self.readout_on(alias),
                        "switch": spec["switch"], "label": spec["label"],
                        "by": str(self._readout.get(alias, {}).get("by") or ""),
                        "at": str(self._readout.get(alias, {}).get("at") or "")}
                for alias, spec in sorted(self._switchable.items())}

    def publish_readout(self) -> None:
        """Retained, so every page and the alarm engine know at once."""
        self.bus.publish_raw(STATUS_READOUT, json.dumps(
            self.readout_view(), separators=(",", ":")), retain=True)

    def _on_readout_command(self, topic: str, payload: str) -> None:
        """On the bus thread: hand the work to a thread of its own. Switching
        off waits for a read in progress (seconds for an RTD module), which
        must not hold up every other message to this service."""
        threading.Thread(target=self._handle_readout, args=(payload,),
                         name="cdaq-readout-switch", daemon=True).start()

    def _handle_readout(self, payload: str) -> None:
        try:
            request = json.loads(payload)
            module = str(request["module"])
            wanted = bool(request["enabled"])
        except (ValueError, KeyError, TypeError):
            log.error("readout command ignored, malformed: %r", payload)
            self.bus.publish_raw(ACK_READOUT, json.dumps(
                {"ok": False, "reason": "malformed command"}))
            return
        by = str(request.get("by") or "") or "unknown"
        result = self.set_readout(module, wanted, by)
        self.bus.publish_raw(TOPIC_AUDIT, json.dumps({
            "t": result.get("at") or iso(utcnow()), "actor": by,
            "action": "readout_switch", "target": module,
            "old": None if "was" not in result else ("on" if result["was"] else "off"),
            "new": "on" if wanted else "off",
            "result": "ok" if result.get("ok") else "refused",
            "detail": result.get("reason", ""),
        }, separators=(",", ":")))
        self.bus.publish_raw(ACK_READOUT, json.dumps(result, separators=(",", ":")))

    def set_readout(self, alias: str, enabled: bool, by: str) -> dict:
        """Switch one module's readout off or on. Returns the acknowledgement.

        Off: its thread is stopped (a read in progress finishes), the task is
        closed, and what it read but nobody published yet is dropped. On: the
        task is built and started again; if that fails the readout stays off
        and the reason is returned. Switching to the state it is in changes
        nothing and is reported as ok, so a double click is harmless.
        """
        if alias not in self._switchable:
            return {"ok": False, "module": alias,
                    "reason": f"{alias} is not a switchable readout"}
        mod = next((m for m in self._modules if m.alias == alias), None)
        with self._switch_lock:
            was = self.readout_on(alias)
            if mod is not None and not self.simulate and enabled != was:
                if enabled:
                    reason = self._open_module(mod)
                    if reason:
                        log.error("%s: readout could NOT be switched on: %s",
                                  alias, reason)
                        return {"ok": False, "module": alias, "was": was,
                                "reason": f"the {alias} task could not be started: {reason}"}
                else:
                    self._close_module(mod)
            now = iso(utcnow())
            self._readout[alias] = {"enabled": bool(enabled), "by": by, "at": now}
            self._save_readout()
            self.publish_readout()
        label = self._switchable[alias]["label"]
        if enabled:
            log.warning("%s (%s) SWITCHED ON by %s", label, alias, by)
        else:
            log.warning("%s (%s) SWITCHED OFF by %s: its channels are not read, "
                        "published or alarmed on until it is switched back on",
                        label, alias, by)
        return {"ok": True, "module": alias, "enabled": bool(enabled),
                "was": was, "by": by, "at": now}

    def _close_module(self, mod: ModuleTask) -> None:
        self._stop_module_thread(mod)
        if mod.task is not None:
            try:
                mod.task.close()
            except Exception:
                log.exception("%s: closing the task raised", mod.alias)
            mod.task = None
        names = {c.name for c in mod.channels}
        with self._bg_lock:
            self._bg_pending = [m for m in self._bg_pending if m.channel not in names]
        self._bg_failed.discard(mod.alias)

    def _open_module(self, mod: ModuleTask) -> str | None:
        """Build, start and thread one module's task. None, or why not."""
        try:
            from nidaqmx.constants import ADCTimingMode
            task = self._new_task(mod)
            if task is None:
                return "its channels cannot be configured"
            mod.task = self._start_task(mod, task, ADCTimingMode)
        except Exception as exc:
            mod.task = None
            return str(exc)
        log.info("task for %s ready (%d channels)", mod.alias, len(mod.channels))
        if mod.background:
            self._start_module_thread(mod)
        return None

    # ------------------------------------------------------------ background

    def _start_background(self) -> None:
        """One thread per background module, reading at its own interval."""
        self._bg_stop.clear()
        for mod in self._modules:
            if mod.background and mod.task is not None:
                self._start_module_thread(mod)

    def _start_module_thread(self, mod: ModuleTask) -> None:
        mod.bg_stop = threading.Event()
        th = threading.Thread(target=self._background_loop, args=(mod,),
                              name=f"cdaq-{mod.alias}", daemon=True)
        mod.thread = th
        th.start()
        self._bg_threads.append(th)

    def _stop_module_thread(self, mod: ModuleTask) -> None:
        """Stop one module's thread and wait out a read in progress."""
        mod.bg_stop.set()
        if mod.thread is not None:
            mod.thread.join(timeout=30.0)
            if mod.thread in self._bg_threads:
                self._bg_threads.remove(mod.thread)
            mod.thread = None

    def _stop_background(self) -> None:
        """Stop the threads BEFORE their tasks are closed under them. The join
        waits out a read in progress (an RTD read takes seconds)."""
        self._bg_stop.set()
        for mod in self._modules:
            mod.bg_stop.set()
        for th in self._bg_threads:
            th.join(timeout=30.0)
        self._bg_threads = []
        for mod in self._modules:
            mod.thread = None

    def _background_loop(self, mod: ModuleTask) -> None:
        stop = mod.bg_stop
        while not self._bg_stop.is_set() and not stop.is_set():
            t0 = time.monotonic()
            try:
                values = self._timed_read(mod)
                readings = self._convert(mod, values, utcnow())
                if mod.alias in self._bg_failed:
                    log.info("%s reads again", mod.alias)
                    self._bg_failed.discard(mod.alias)
            except Exception as exc:
                # The module's own channels are unknown; the rest of the
                # chassis is unaffected, which is the point of the thread.
                if mod.alias not in self._bg_failed:
                    log.error("%s: background read failed: %s. Publishing "
                              "quality=error for its channels; retrying.", mod.alias, exc)
                    self._bg_failed.add(mod.alias)
                now = utcnow()
                readings = [Measurement(t=now, channel=ch.name, value=None, unit=ch.unit,
                                        quality=Quality.ERROR) for ch in mod.channels]
            with self._bg_lock:
                if not stop.is_set():       # switched off during the read
                    self._bg_pending.extend(readings)
            stop.wait(max(0.0, mod.interval_s - (time.monotonic() - t0)))

    def _take_background(self) -> list[Measurement]:
        with self._bg_lock:
            out, self._bg_pending = self._bg_pending, []
        return out

    def _timed_read(self, mod: ModuleTask):
        t0 = time.monotonic()
        values = mod.task.read()
        dt = time.monotonic() - t0
        mod.timing[0] += 1
        mod.timing[1] += dt
        mod.timing[2] = max(mod.timing[2], dt)
        return values

    def _report_timing(self) -> None:
        """Every TIMING_REPORT_S: how long each module's read takes. A slow
        module in the 1-s loop is what made the pressures noisy."""
        if time.monotonic() < self._next_timing_report:
            return
        self._next_timing_report = time.monotonic() + TIMING_REPORT_S
        parts = []
        for mod in self._modules:
            n, total, worst = mod.timing
            if n:
                parts.append(f"{mod.alias} {total / n:.3f} s (max {worst:.3f}, n={n}"
                             f"{', background' if mod.background else ''})")
            mod.timing[:] = [0, 0.0, 0.0]
        if parts:
            log.info("read time per module: %s", "; ".join(parts))

    def _close_tasks(self) -> None:
        self._stop_background()
        for mod in self._modules:
            if mod.task is not None:
                try:
                    mod.task.close()
                except Exception:
                    pass
                mod.task = None

    def close(self) -> None:
        """Release the chassis so LabVIEW can have it back."""
        self._close_tasks()
        log.info("cDAQ released")

    # ------------------------------------------------------------------- read

    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()

        now = utcnow()
        out: list[Measurement] = []
        for mod in self._modules:
            if mod.task is None or mod.background:
                continue
            out.extend(self._convert(mod, self._timed_read(mod), now))
        # What the background modules have read since the last call.
        out.extend(self._take_background())
        self._report_timing()
        out.extend(self._derive_from(out, now))
        return out

    def _convert(self, mod: ModuleTask, values, now) -> list[Measurement]:
        """One module's raw values -> measurements, with the validity checks."""
        # A single-channel task returns a scalar, not a list.
        if not isinstance(values, list):
            values = [values]
        if len(values) != len(mod.channels):
            raise RuntimeError(
                f"{mod.alias}: read returned {len(values)} values for "
                f"{len(mod.channels)} channels")
        out: list[Measurement] = []
        for ch, raw in zip(mod.channels, values):
            # RTD channels come back in degrees C from DAQmx; their offset
            # and multiplier default to 0 and 1, so this is a no-op for
            # them and the real scaling for the voltage channels (§4.2).
            quality = Quality.OK
            if ch.kind == "rtd" and not (RTD_VALID_C[0] <= raw <= RTD_VALID_C[1]):
                # Report the absence of a measurement, not a number.
                quality = Quality.ERROR
                if ch.name not in self._reported_open:
                    log.error(
                        "%s (%s) reads %.1f C, outside the RTD range %s..%s — "
                        "open circuit, short, or no sensor. Publishing "
                        "quality=error, not a temperature.",
                        ch.name, ch.phys, raw, *RTD_VALID_C)
                    self._reported_open.add(ch.name)
            elif ch.kind == "rtd":
                self._reported_open.discard(ch.name)
            elif ch.kind == "current" and not (
                    CURRENT_VALID_A[0] <= raw <= CURRENT_VALID_A[1]):
                # A dead 4-20 mA loop, not a light load. See CURRENT_VALID_A.
                quality = Quality.ERROR
                if ch.name not in self._reported_open:
                    log.error(
                        "%s (%s) reads %.2f mA, outside the 4-20 mA loop range "
                        "%.1f..%.1f mA — broken loop, unpowered transmitter, or "
                        "no sensor. Publishing quality=error, not a weight.",
                        ch.name, ch.phys, raw * 1e3,
                        CURRENT_VALID_A[0] * 1e3, CURRENT_VALID_A[1] * 1e3)
                    self._reported_open.add(ch.name)
            elif ch.kind == "current":
                self._reported_open.discard(ch.name)

            out.append(Measurement(
                t=now, channel=ch.name,
                value=apply(raw, ch.offset, ch.multiplier) if quality is Quality.OK
                      else None,
                unit=ch.unit, raw=raw, quality=quality,
            ))
        return out

    def _derive_from(self, readings: list[Measurement], now) -> list[Measurement]:
        """Compute the derived channels from the readings just taken.

        Same timestamp as the source, and the source's quality: if the
        voltage could not be read, the pressure is unknown, not zero. A
        transform that returns None is saying the input is not a measurement
        (a PKR signal below 0.5 V is a gauge without supply) and is published
        as quality=error - logged once, not every second.
        """
        from ..scaling import TRANSFORMS

        by_name = {m.channel: m for m in readings}
        out = []
        for ch in self._derived:
            source = by_name.get(ch.derive["from"])
            if source is None:
                continue
            if source.quality is not Quality.OK or source.value is None:
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=source.quality,
                                       src=source.src))
                continue
            params = {k: v for k, v in ch.derive.items()
                      if k not in ("from", "transform")}
            try:
                value = TRANSFORMS[ch.derive["transform"]](source.value, **params)
            except Exception:
                log.exception("could not derive %s from %s", ch.name, source.channel)
                value = None
            if value is None:
                if ch.name not in self._reported_open:
                    log.error("%s: %s = %.3f is not a valid input for %s; "
                              "publishing quality=error", ch.name, source.channel,
                              source.value, ch.derive["transform"])
                    self._reported_open.add(ch.name)
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, raw=source.value,
                                       quality=Quality.ERROR, src=source.src))
                continue
            self._reported_open.discard(ch.name)
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=source.value,
                                   quality=Quality.OK, src=source.src))
        return out

    def _read_simulated(self) -> list[Measurement]:
        """Synthetic values for this driver's own channels (§13).

        Complements devices/sim.py: that one covers the whole channel map,
        this one exercises this driver's code path without the chassis.
        """
        now = utcnow()
        out = []
        for mod in self._modules:
            if not self.readout_on(mod.alias):
                continue
            for ch in mod.channels:
                if ch.kind == "rtd":
                    raw = random.gauss(-60.0, 0.5)
                elif ch.kind == "current":
                    # Mid-loop, about 10 kg — inside CURRENT_VALID_A, so the
                    # simulated channels are quality=ok like the rest.
                    raw = random.gauss(0.012, 0.0002)
                elif any(d.derive.get("transform") == "pkr_pressure"
                         and d.derive.get("from") == ch.name for d in self._derived):
                    # A PKR signal at about 1e-6 mbar, inside its valid range.
                    raw = random.gauss(3.2, 0.01)
                else:
                    raw = random.gauss(1.5, 0.01)
                out.append(Measurement(
                    t=now, channel=ch.name,
                    value=apply(raw, ch.offset, ch.multiplier),
                    unit=ch.unit, raw=raw, quality=Quality.OK, src="sim",
                ))
        out.extend(self._derive_from(out, now))
        return out
