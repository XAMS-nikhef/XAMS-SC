"""Residual gas analyser (Pfeiffer PrismaPro), over Ethernet. See docs/drivers/rga.md.

Steps 1 and 2 of the RGA plan: status, control, scans and Save. **Emission is
not here.** Nothing in this service can switch it on or off: the client's
whitelist (`prismapro.WRITABLE`) does not contain it. A scan with emission off
measures the detector's noise, which is what a background scan is; a scan
with emission on (switched on with PV MassSpec, say) measures gas, and this
service records whichever it gets.

What is published, every `read_interval_s` (1 s while a scan runs):

  * a few channels from `channels.yaml` (device `rga`), whose `phys` names
    what is read - see `READERS` below. Spectra are never channels.
  * a RETAINED `xams/rga/info` record for the RGA page: identity, who is in
    control, and the acquisition (recipe, scans done). Not archived.
  * the latest scan of a running acquisition on RETAINED `xams/rga/scan`, for
    the live plot. Not archived.

Commands (each acknowledged and audited, like every write in this system):

  * `xams/cmd/rga/control` {action: take|release, force, by}
  * `xams/cmd/rga/scan`    {action: start|stop, recipe, by}
  * `xams/cmd/rga/save`    {comment, tags, background, by} - writes the last
    acquisition to one JSON file and publishes its summary on xams/rga/saved.

Control is released by itself after `release_idle_s` without a scan or a
command, and when the service stops, so PV MassSpec is never locked out by a
page somebody forgot.

Identity (section 6.2): the analyser must report the `name` and `serial` set
in `devices.yaml`, or the service refuses to start.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from datetime import datetime

from .. import rga_file
from ..bus import (ACK_RGA_CONTROL, ACK_RGA_EM, ACK_RGA_EMISSION, ACK_RGA_SAVE,
                   ACK_RGA_SCAN, TOPIC_AUDIT, TOPIC_MEAS, TOPIC_RGA_CONTROL,
                   TOPIC_RGA_EM, TOPIC_RGA_EMISSION,
                   TOPIC_RGA_INFO, TOPIC_RGA_SAVE, TOPIC_RGA_SAVED, TOPIC_RGA_SCAN,
                   TOPIC_RGA_SCANDATA)
from ..config import CONFIG_DIR, ROOT, Config
from ..model import Measurement, Quality, iso, utcnow
from ..service import BaseService
from .prismapro import (EM_INTERLOCK_BIT, EM_PENDING_BIT, TO_MBAR, ControlRefused,
                        MMSPControl, MMSPError, em_on, emission_on, read_identity)

log = logging.getLogger(__name__)

# Control as one code: who holds the analyser.
CONTROL_NONE, CONTROL_US, CONTROL_OTHER = 0, 1, 2

SCANNING_BIT = 1 << 1
INTERLOCK_BIT = 1 << 27
EMISSION_PENDING_BIT = 1 << 28      # asked for, not yet on (API: "System Status")
# How long a requested emission or EM state is waited for before the page
# stops saying "starting..." / "stopping..." and says it did not follow.
SWITCH_WAIT_S = 30.0
# The datasheet's maximum operating pressures (QMG 250 M, "Technical data"),
# hPa = mbar: 5e-4 with the Faraday cup, 5e-5 with the C-SEM (electron
# multiplier). The configured limits may be lower, never higher.
DATASHEET_MAX_MBAR = 5e-4
EM_DATASHEET_MAX_MBAR = 5e-5
# 69 is CF3+, the largest of the fluorocarbon (PTFE) series seen on
# 13 October 2026; it tells whether a peak at 131 is C3F5+ rather than Xe.
SUMMARY_MASSES = [2, 4, 18, 28, 32, 40, 44, 69, 129, 132]


def _control_code(info: dict) -> int:
    if info.get("amInControl"):
        return CONTROL_US
    if info.get("controlled"):
        return CONTROL_OTHER
    return CONTROL_NONE


# phys -> (target to GET, how to turn its data into a number). Several
# channels may share one target; it is read once per cycle.
READERS = {
    "emission": ("status/systemStatus", lambda d: float(emission_on(int(d)))),
    "em": ("status/systemStatus", lambda d: float(em_on(int(d)))),
    "status/systemStatus": ("status/systemStatus", lambda d: float(int(d))),
    "status/hardwareErrors": ("status/hardwareErrors", lambda d: float(int(d))),
    "status/hardwareWarnings": ("status/hardwareWarnings", lambda d: float(int(d))),
    "sensorIonSource/tpTripCount": ("sensorIonSource/tpTripCount",
                                    lambda d: float(int(d))),
    "diagnosticData/internalBoxTemperature": (
        "diagnosticData/internalBoxTemperature", lambda d: float(d)),
    "control": ("communication/controlInfo", lambda d: float(_control_code(d))),
}

# Read once at Save, into the file: what the analyser was set to.
SETTINGS = {
    "emission_current_ua": "sensorIonSource/emissionCurrent",
    "electron_energy_v": "sensorIonSource/electronEnergy",
    "pp_sensitivity_a_per_unit": "sensorIonSource/ppSensitivityFactor",
    "tp_units_code": "sensorIonSource/tPunits",
    "em_state": "generalControl/setEM",
    "em_voltage_v": "sensorDetector/emVoltage",
    "em_gain": "sensorDetector/emGain",
    "em_gain_mass_camu": "sensorDetector/emGainMass",
    "em_equiv_ion": "generalControl/emEquivIonSet",
    "emission_state": "generalControl/setEmission",
    "total_pressure": "measurement/totalPressure",
}


class RgaService(BaseService):
    name = "rga"

    def __init__(self, config: Config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._spec = config.devices.get("rga", {}) or {}
        self._channels = config.channels_for("rga")
        # Status once every few seconds is plenty, and kind to a device that
        # drops requests that come too fast. While a scan runs the loop turns
        # faster, so the live plot follows and no scan waits long.
        self._idle_interval = max(self.interval_s,
                                  float(self._spec.get("read_interval_s", 5.0)))
        self._scan_interval = float(self._spec.get("scan_interval_s", 1.0))
        self.interval_s = self._idle_interval
        self._release_idle_s = float(self._spec.get("release_idle_s", 300))
        self._client_name = str(self._spec.get("client_name", "xams-sc"))
        self._data_dir = ROOT / str(self._spec.get("data_dir", "data/rga"))
        self._summary_masses = list(self._spec.get("summary_masses", SUMMARY_MASSES))
        self._snapshot_channels = list(self._spec.get("snapshot_channels",
                                                      ["p105", "pmain"]))
        self._client: MMSPControl | None = None
        self._identity: dict | None = None
        self._control: dict | None = None
        self._recipes: dict[str, dict] = {}
        self._recipe_error: str | None = None
        self._acq: dict | None = None          # the current or last acquisition
        # NOT `self._lock`: BaseService uses that name for the single-instance
        # lock file. Shadowing it once made run() take THIS lock in the main
        # thread for good, and every command then hung the MQTT thread - the
        # page timed out and the RGA went stale (13 October 2026).
        self._acq_lock = threading.RLock()     # guards _acq and _last_activity
        self._last_activity = time.monotonic()
        self._snapshot: dict[str, dict] = {}
        self._t0 = time.monotonic()

        # The p105 GUARD (docs/drivers/rga.md, "Safety"). A software guard on
        # top of the analyser's own over-pressure trip, NOT an interlock in the
        # sense of DESIGN.md section 10 rule 1. It refuses emission on, and
        # switches emission off, on the slow-control pressure p105.
        self._guard_channel = str(self._spec.get("guard_channel", "p105"))
        self._guard_limit = float(self._spec.get("guard_limit_mbar", 4e-4))
        if not 0 < self._guard_limit <= DATASHEET_MAX_MBAR:
            raise ValueError(
                f"devices.yaml rga.guard_limit_mbar = {self._guard_limit:g}: must be "
                f"above 0 and at most {DATASHEET_MAX_MBAR:g} mbar (datasheet)")
        self._guard_max_age = float(self._spec.get("guard_max_age_s", 30))
        self._guard_grace = float(self._spec.get("guard_grace_s", 60))
        self._guard_seen: float | None = None     # monotonic receipt of p105
        self._emission_idle_off_s = float(self._spec.get("emission_idle_off_s", 3600))
        self._status_word: int | None = None      # systemStatus, last read
        self._hw_errors: int | None = None
        self._emission_note: str | None = None    # why emission last went off by itself

        # The electron multiplier's own, lower, limit on the same p105 (step
        # 4). Same freshness rules and grace as the emission guard. A limit
        # above the datasheet's or above the emission limit is a
        # configuration error, refused at start rather than clipped.
        self._em_limit = float(self._spec.get("em_guard_limit_mbar", 4e-5))
        if not 0 < self._em_limit <= min(EM_DATASHEET_MAX_MBAR, self._guard_limit):
            raise ValueError(
                f"devices.yaml rga.em_guard_limit_mbar = {self._em_limit:g}: must be "
                f"above 0 and at most {min(EM_DATASHEET_MAX_MBAR, self._guard_limit):g} "
                f"mbar (datasheet {EM_DATASHEET_MAX_MBAR:g}, emission limit "
                f"{self._guard_limit:g})")
        self._em_note: str | None = None          # why the EM last went off by itself
        # What emission and the EM were last asked to be, and when: until the
        # analyser reports that state (or SWITCH_WAIT_S passes) the page shows
        # them as switching and keeps the button greyed. The analyser's own
        # "request pending" bits did not cover the seconds in between
        # (13 October 2026).
        self._wanted: dict[str, tuple[bool, float]] = {}
        self._em_settings: dict = {}              # voltage, gain, as last read
        # Whether the analyser's stored EM gain has been checked against the
        # Faraday cup. It had not on 13 October 2026 (stored 1234.57, measured
        # ~30 at 800 V): EM values are then relative, and the page says so.
        self._em_gain_calibrated = bool(self._spec.get("em_gain_calibrated", False))

        # Commands run on a worker thread, never on the MQTT thread: an
        # analyser call takes up to seconds, and the MQTT thread is the one
        # that carries everything this service publishes (13 October 2026).
        self._commands: queue.Queue = queue.Queue()
        self._load_recipes(int(self._spec.get("mass_range", 200)))

        # The last acquisition, until it is saved, also lives on disk: the
        # service is restarted for every deployment, and an unsaved survey was
        # lost that way once (13 October 2026). Reading it back is a file
        # read, nothing touches the analyser.
        self._pending_path = self._data_dir / "pending.json"
        self._restore_pending()
        self._saved_files = self._list_saved()

    def _load_recipes(self, mass_range: int) -> None:
        path = CONFIG_DIR / "rga_recipes.yaml"
        try:
            self._recipes = rga_file.load_recipes(path, mass_range)
            self._recipe_error = None
        except (OSError, rga_file.RecipeError, KeyError, TypeError, ValueError) as exc:
            # Status still works without recipes; scans are refused, saying why.
            self._recipes = {}
            self._recipe_error = f"{path.name}: {exc}"
            log.error("no RGA recipes: %s", self._recipe_error)

    # ------------------------------------------------------------ identity

    def _check_channels(self) -> bool:
        unknown = [ch for ch in self._channels if ch.phys not in READERS]
        for ch in unknown:
            log.critical("FATAL: channel %s names %r, which this driver does not "
                         "know how to read. Refusing to start rather than guess.",
                         ch.name, ch.phys)
        return not unknown

    def verify_identity(self) -> bool:
        if not self._check_channels():
            return False
        if self.simulate:
            log.info("simulate=True: skipping hardware identity check")
            self._identity = {"name": "simulated PrismaPro", "simulated": True}
            self._publish_info()
            return True

        host = str(self._spec.get("host", "")).strip()
        match = self._spec.get("match", {}) or {}
        want_name = str(match.get("name", "")).strip()
        want_serial = str(match.get("serial", "")).strip()
        if not (host and want_name and want_serial):
            log.critical("FATAL: devices.yaml rga needs host, match.name AND "
                         "match.serial. An address alone identifies nothing.")
            return False

        client = MMSPControl(host, int(self._spec.get("port", 80)),
                             timeout_s=float(self._spec.get("timeout_s", 5.0)))
        try:
            ident = read_identity(client)
        except (OSError, MMSPError) as exc:
            log.critical("FATAL: no answer from the RGA at %s: %s", host, exc)
            client.close()
            return False

        if ident.name != want_name or ident.serial != want_serial:
            log.critical("FATAL: %s is %r (serial %s), not the configured %r "
                         "(serial %s). Refusing to start.", host, ident.name,
                         ident.serial, want_name, want_serial)
            client.close()
            return False

        log.info("RGA at %s: %s, firmware %s, %d amu, filaments %s, units %s",
                 host, ident.name, ident.firmware, ident.mass_range,
                 ident.as_dict()["filaments"], ident.as_dict()["tp_units"])
        if ident.mass_range != int(self._spec.get("mass_range", 200)):
            self._load_recipes(ident.mass_range)
        self._client = client
        self._identity = ident.as_dict() | {"host": host}
        self._read_em_settings()
        self._publish_info()
        if self._acq is not None and self._acq.get("scans"):
            self._publish_scan(self._acq)       # the restored one, for the plot
        return True

    def _read_em_settings(self) -> None:
        """The EM's voltage and gain, for the page. Read, never written."""
        try:
            self._em_settings = {
                "voltage_v": self._client.get("sensorDetector/emVoltage"),
                "gain": self._client.get("sensorDetector/emGain"),
                "gain_mass_camu": self._client.get("sensorDetector/emGainMass"),
                "equiv_ion": self._client.get("generalControl/emEquivIonSet")}
        except (OSError, MMSPError) as exc:
            log.warning("could not read the EM settings: %s", exc)

    def reconnect(self) -> bool:
        if self.simulate:
            return True
        self.close_client()
        return self.verify_identity()

    # ---------------------------------------------------------------- read

    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()
        if self._client is None:
            raise RuntimeError("RGA client not open")

        # One GET per target, not per channel. A failure of any one is a
        # failed read: the loop marks every channel bad and backs off.
        data = {}
        targets = {READERS[ch.phys][0] for ch in self._channels}
        # The guard needs these whatever channels.yaml asks for.
        targets |= {"status/systemStatus", "status/hardwareErrors",
                    "communication/controlInfo"}
        for target in targets:
            data[target] = self._client.get(target)
        self._status_word = int(data["status/systemStatus"])
        self._hw_errors = int(data["status/hardwareErrors"])

        control = data["communication/controlInfo"] or {}
        self._note_control(control)

        self._collect_scans()
        self._watch_em(control)
        self._watch_emission(control)
        self._release_if_idle(control)
        # Every cycle, not only on a change: the guard's p105 and its age are
        # what the page shows beside the emission button.
        self._publish_info()
        # Read fast while something is switching, so the page follows within
        # a second; back to the idle pace after (a scan keeps its own).
        with self._acq_lock:
            scanning = self._acq is not None and self._acq["state"] == "scanning"
        if not scanning:
            self.interval_s = (self._scan_interval if self._wanted
                               else self._idle_interval)

        now = utcnow()
        out = []
        for ch in self._channels:
            target, convert = READERS[ch.phys]
            value = convert(data[target])
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=value, quality=Quality.OK))
        return out

    def _note_control(self, info: dict) -> None:
        """Publish the controlling client when it changes, and say so in the log."""
        summary = {"controlled": bool(info.get("controlled")),
                   "client": info.get("clientName") or "",
                   "ip": info.get("ipAddress") if info.get("ipAddress") not in (None, "null") else "",
                   "locked": bool(info.get("locked")),
                   "us": bool(info.get("amInControl"))}
        if summary != self._control:
            if summary["controlled"]:
                log.info("RGA now controlled by %r from %s%s", summary["client"],
                         summary["ip"] or "?", " (this service)" if summary["us"] else "")
            elif self._control is not None:
                log.info("RGA control released")
            self._control = summary
            self._publish_info()

    def _publish_info(self) -> None:
        with self._acq_lock:
            acq = self._acq_view()
        emission_to, em_to = self.switching("emission"), self.switching("em")
        record = {"t": iso(utcnow()), "identity": self._identity,
                  "control": self._control, "acquisition": acq,
                  "recipes": {n: {"description": r["description"], "scans": r["scans"],
                                  "seconds": round(rga_file.scan_seconds(r), 1)}
                              for n, r in self._recipes.items()},
                  "recipe_error": self._recipe_error,
                  "saved_files": self._saved_files,
                  "emission": {"on": self.emission_is_on(),
                               "pending": emission_to,
                               "wanted": emission_to,
                               "interlock": bool((self._status_word or 0) & INTERLOCK_BIT),
                               "note": self._emission_note},
                  "em": {"on": self.em_is_on(),
                         "gain_calibrated": self._em_gain_calibrated,
                         "pending": em_to,
                         "wanted": em_to,
                         "interlock": bool((self._status_word or 0) & EM_INTERLOCK_BIT),
                         "note": self._em_note, **self._em_settings},
                  "guard": self.guard_view()}
        self.bus.publish_raw(TOPIC_RGA_INFO, json.dumps(record), retain=True)

    def _acq_view(self) -> dict | None:
        a = self._acq
        if a is None:
            return None
        return {"recipe": a["recipe"]["name"], "state": a["state"],
                "done": len(a["scans"]), "of": a["count"], "t_start": a["t_start"],
                "by": a["by"], "saved": a.get("saved"), "error": a.get("error"),
                "detector": a.get("detector", "Faraday")}

    # ------------------------------------------------------------- scans

    def _collect_scans(self) -> None:
        """Take every finished scan off the analyser while an acquisition runs.

        `nextScan` hands each scan over once, in order; it is drained here
        until it has nothing new, so a fast recipe does not fall behind.
        """
        with self._acq_lock:
            acq = self._acq
            if acq is None or acq["state"] != "scanning":
                return
        expected = acq["points"]
        for _ in range(50):
            d = self._client.get("measurement/nextScan") or {}
            values = d.get("values")
            scanning = bool(int(d.get("systemStatus", 0)) & SCANNING_BIT)
            if values is None:
                if not scanning and len(acq["scans"]) < acq["count"] \
                        and time.monotonic() - acq["mono_start"] > 5:
                    self._finish(acq, "stopped",
                                 "the analyser stopped scanning before all "
                                 "scans were taken")
                return
            if len(values) != expected or not rga_file.finite(values):
                self._finish(acq, "failed",
                             f"scan {d.get('scannum')} has {len(values)} points, "
                             f"the recipe {expected}; not recorded")
                return
            with self._acq_lock:
                acq["scans"].append({"scannum": d.get("scannum"), "t": iso(utcnow()),
                                     "system_status": d.get("systemStatus"),
                                     "values": values})
                done = len(acq["scans"]) >= acq["count"]
            self._publish_scan(acq)
            if done:
                self._finish(acq, "done")
                return

    def _restore_pending(self) -> None:
        acq = rga_file.read_pending(self._pending_path)
        if not acq or acq.get("saved") or not acq.get("scans"):
            return
        if acq.get("state") == "scanning":
            acq["state"] = "stopped"
            acq["error"] = "the service restarted during the scan"
        acq["mono_start"] = time.monotonic()
        self._acq = acq
        log.info("unsaved RGA acquisition restored: %d scans of %s from %s",
                 len(acq["scans"]), acq["recipe"]["name"], acq["t_start"])

    def _list_saved(self) -> list[dict]:
        try:
            return rga_file.recent(self._data_dir)
        except OSError:
            return []

    def _persist(self, acq: dict) -> None:
        try:
            with self._acq_lock:
                rga_file.write_pending(self._pending_path, acq)
        except (OSError, ValueError) as exc:
            log.warning("could not keep the RGA acquisition on disk: %s", exc)

    def _finish(self, acq: dict, state: str, error: str | None = None) -> None:
        with self._acq_lock:
            acq["state"] = state
            if error:
                acq["error"] = error
                log.warning("RGA acquisition %s: %s", state, error)
            else:
                log.info("RGA acquisition %s: %d scans of %s", state,
                         len(acq["scans"]), acq["recipe"]["name"])
            self._last_activity = time.monotonic()
        self.interval_s = self._idle_interval
        self._persist(acq)
        self._publish_info()

    def _publish_scan(self, acq: dict) -> None:
        with self._acq_lock:
            last = acq["scans"][-1]
            payload = {"t": last["t"], "recipe": acq["recipe"]["name"],
                       "scannum": last["scannum"], "done": len(acq["scans"]),
                       "of": acq["count"], "masses": acq["masses"],
                       "values": last["values"],
                       "mean": rga_file.mean([s["values"] for s in acq["scans"]]),
                       "detector": acq.get("detector", "Faraday")}
        # For the plot: the gain, and what to multiply the values by to get
        # ion current - the same rule as for a saved file (rga_file.ion_scale).
        as_saved = {"detector": payload["detector"],
                    "device": {"em_gain": self._em_settings.get("gain"),
                               "em_equiv_ion": self._em_settings.get("equiv_ion")}}
        payload["gain"] = rga_file.em_gain(as_saved)
        payload["scale"] = rga_file.ion_scale(as_saved)
        payload["gain_calibrated"] = self._em_gain_calibrated
        self.bus.publish_raw(TOPIC_RGA_SCANDATA, json.dumps(payload), retain=True)
        if acq["state"] == "scanning":
            self._persist(acq)
        self._publish_info()

    def _release_if_idle(self, control: dict) -> None:
        if not control.get("amInControl"):
            return
        if self.emission_is_on():
            # Keep control while emission is on: without it the guard could
            # not switch emission off. Emission has its own idle limit.
            return
        with self._acq_lock:
            busy = self._acq is not None and self._acq["state"] == "scanning"
            idle = time.monotonic() - self._last_activity
        if busy or idle < self._release_idle_s:
            return
        try:
            self._client.release_control()
            log.info("RGA control released after %.0f s idle", idle)
            self._audit("rga_control", "rga", "held", "released", "ok",
                        "released automatically after %.0f s idle" % idle, "rga service")
        except (OSError, MMSPError) as exc:
            log.warning("could not release RGA control: %s", exc)

    # ---------------------------------------------------------- the guard

    def emission_is_on(self) -> bool:
        """The REAL emission state: systemStatus bit 31 or 30, as last read.
        Not what was last asked for - the regulator takes seconds to follow."""
        return self._status_word is not None and emission_on(self._status_word)

    def em_is_on(self) -> bool:
        """The REAL EM state: systemStatus bit 23, as last read."""
        return self._status_word is not None and em_on(self._status_word)

    def _expect(self, kind: str, on: bool) -> None:
        """Note that `kind` ("emission" or "em") was asked to go on or off, and
        tell the page now, before the command is acknowledged: the page that
        loads after the answer must already show it as switching."""
        self._wanted[kind] = (on, time.monotonic())
        self.interval_s = self._scan_interval
        self._publish_info()

    def switching(self, kind: str) -> str | None:
        """"On" or "Off" while `kind` is on its way there, else None. Clears
        the request once the analyser reports it, or after SWITCH_WAIT_S."""
        bit = EMISSION_PENDING_BIT if kind == "emission" else EM_PENDING_BIT
        actual = self.emission_is_on() if kind == "emission" else self.em_is_on()
        wanted = self._wanted.get(kind)
        if wanted is not None:
            on, since = wanted
            if on == actual:
                del self._wanted[kind]
            elif time.monotonic() - since > SWITCH_WAIT_S:
                del self._wanted[kind]
                note = (f"asked for {'On' if on else 'Off'} at {iso(utcnow())} but the "
                        f"analyser did not follow within {SWITCH_WAIT_S:.0f} s")
                log.warning("RGA %s: %s", kind, note)
                if kind == "emission":
                    self._emission_note = note
                else:
                    self._em_note = note
            else:
                return "On" if on else "Off"
        if (self._status_word or 0) & bit:
            return "On"
        return None

    def guard_view(self) -> dict:
        """p105 as the guard sees it: value, age, and whether emission and
        the EM may go on."""
        snap = self._snapshot.get(self._guard_channel) or {}
        age = (time.monotonic() - self._guard_seen) if self._guard_seen else None
        ok, reason = self.guard_allows()
        em_ok, em_reason = self.em_guard_allows()
        return {"channel": self._guard_channel, "value": snap.get("v"),
                "unit": snap.get("u"), "quality": snap.get("q"),
                "age_s": None if age is None else round(age, 1),
                "limit_mbar": self._guard_limit, "ok": ok, "reason": reason,
                "em_limit_mbar": self._em_limit, "em_ok": em_ok, "em_reason": em_reason}

    def _pressure_allows(self, limit: float) -> tuple[bool, str]:
        """Is p105 fresh, good, in mbar and below `limit`? One check for both
        limits, so emission and the EM cannot drift apart in how they judge."""
        snap = self._snapshot.get(self._guard_channel)
        name = self._guard_channel
        if not snap or self._guard_seen is None:
            return False, f"{name} has not been received; the guard cannot judge"
        age = time.monotonic() - self._guard_seen
        if age > self._guard_max_age:
            return False, f"{name} is {age:.0f} s old (more than {self._guard_max_age:.0f} s)"
        if snap.get("q") != "ok" or not isinstance(snap.get("v"), (int, float)):
            return False, f"{name} quality is {snap.get('q')}"
        if snap.get("u") not in (None, "mbar"):
            return False, f"{name} is in {snap.get('u')}, the guard expects mbar"
        if snap["v"] >= limit:
            return False, (f"{name} = {snap['v']:.2e} mbar, at or above the limit "
                           f"{limit:.1e} mbar")
        return True, f"{name} = {snap['v']:.2e} mbar < {limit:.1e} mbar"

    def guard_allows(self) -> tuple[bool, str]:
        """May emission be on, as far as p105 is concerned?"""
        return self._pressure_allows(self._guard_limit)

    def em_guard_allows(self) -> tuple[bool, str]:
        """May the EM be on, as far as p105 is concerned? Its own, lower limit."""
        return self._pressure_allows(self._em_limit)

    def _off_reason(self, limit: float) -> str | None:
        """Why something guarded by `limit` must go off now, or None: at once
        at the limit; when p105 is missing, old or bad, only after the grace."""
        ok, why = self._pressure_allows(limit)
        if ok:
            return None
        snap = self._snapshot.get(self._guard_channel) or {}
        if isinstance(snap.get("v"), (int, float)) and snap["v"] >= limit:
            return f"guard: {why}"
        age = (time.monotonic() - self._guard_seen) if self._guard_seen else None
        if age is None or age > self._guard_max_age + self._guard_grace:
            return f"guard: {why}, past the grace period"
        return None

    def _em_off(self, reason: str, actor: str = "rga service") -> bool:
        """Switch the EM off and say why. True if the analyser took it."""
        try:
            self._client.set_em(False)
        except (OSError, MMSPError) as exc:
            log.error("COULD NOT SWITCH THE RGA EM OFF (%s): %s", reason, exc)
            return False
        self._expect("em", False)
        if actor == "rga service":
            self._em_note = f"switched off by the service at {iso(utcnow())}: {reason}"
            log.warning("RGA EM switched OFF by the service: %s", reason)
        self._audit("rga_em_off", "rga", "On", "Off", "ok", reason, actor)
        with self._acq_lock:
            acq = self._acq
            scanning = acq is not None and acq["state"] == "scanning"
        if scanning and acq.get("detector") == "EM":
            # One detector per acquisition: the scans so far are EM scans, and
            # stay saveable; the rest would not be.
            try:
                self._client.stop_scan()
            except (OSError, MMSPError) as exc:
                log.warning("could not stop the scan after the EM went off: %s", exc)
            self._finish(acq, "stopped", f"stopped: EM switched off ({reason})")
        return True

    def _watch_em(self, control: dict) -> None:
        """Switch the EM off when it must not stay on. Only ever OFF, and only
        while this system holds the analyser (as for emission)."""
        if not self.em_is_on() or not control.get("amInControl"):
            return
        reason = self._off_reason(self._em_limit)
        if reason is None and not self.emission_is_on():
            reason = "emission is off"
        if reason is not None:
            self._em_off(reason)

    def _watch_emission(self, control: dict) -> None:
        """Switch emission off when it must not stay on. Only ever OFF.

        Acts only while this system holds the analyser: without control it
        cannot write, and emission switched on by PV MassSpec is PV MassSpec's
        to look after (and the analyser's own trip's).
        """
        if not self.emission_is_on() or not control.get("amInControl"):
            return
        reason = self._off_reason(self._guard_limit)
        if reason is None:
            with self._acq_lock:
                busy = self._acq is not None and self._acq["state"] == "scanning"
                idle = time.monotonic() - self._last_activity
            if not busy and idle > self._emission_idle_off_s:
                reason = (f"no scan or command for {idle / 60:.0f} min "
                          f"(limit {self._emission_idle_off_s / 60:.0f} min)")
        if reason is None:
            return
        if self.em_is_on():
            self._em_off(f"before emission off: {reason}")
        try:
            self._client.set_emission(False)
        except (OSError, MMSPError) as exc:
            log.error("COULD NOT SWITCH RGA EMISSION OFF (%s): %s", reason, exc)
            return
        self._expect("emission", False)
        self._emission_note = f"switched off by the service at {iso(utcnow())}: {reason}"
        log.warning("RGA emission switched OFF by the service: %s", reason)
        self._audit("rga_emission_off", "rga", "On", "Off", "ok", reason, "rga service")

    # ---------------------------------------------------------- commands

    def _audit(self, action, target, old, new, result, detail="", actor=""):
        self.bus.publish_raw(TOPIC_AUDIT, json.dumps(
            {"t": iso(utcnow()), "actor": actor or "unknown", "action": action,
             "target": target, "old": None if old is None else str(old),
             "new": None if new is None else str(new),
             "result": result, "detail": detail}, separators=(",", ":")))

    def _ack(self, topic, ok, action, actor, detail="", **extra):
        """Acknowledge and audit one command. A refusal carries `reason`, as
        every other service's does; a success carries `detail`."""
        key = "detail" if ok else "reason"
        self.bus.publish_raw(topic, json.dumps(
            {"ok": ok, key: detail, "by": actor, **extra}, separators=(",", ":")))
        self._audit(action, "rga", extra.get("old"), extra.get("new"),
                    "ok" if ok else "rejected", detail, actor)
        if not ok:
            log.warning("refused %s by %s: %s", action, actor, detail)

    def _parse(self, payload: str):
        try:
            d = json.loads(payload) or {}
        except ValueError:
            d = {}
        return d, str(d.get("by") or "").strip() or "unknown"

    def _usable(self, ack, action, actor) -> bool:
        if self.simulate:
            self._ack(ack, False, action, actor,
                      "this service is simulating and holds no analyser")
            return False
        if self._client is None:
            self._ack(ack, False, action, actor, "not connected to the analyser")
            return False
        return True

    def _handle_control(self, topic: str, payload: str) -> None:
        d, actor = self._parse(payload)
        what = str(d.get("action", "")).lower()
        action = "rga_control_" + (what or "unknown")
        if what not in ("take", "release"):
            self._ack(ACK_RGA_CONTROL, False, action, actor,
                      "action must be take or release")
            return
        if not self._usable(ACK_RGA_CONTROL, action, actor):
            return
        with self._acq_lock:
            self._last_activity = time.monotonic()
            scanning = self._acq is not None and self._acq["state"] == "scanning"
        try:
            if what == "take":
                info = self._client.take_control(self._client_name,
                                                 force=bool(d.get("force")))
                detail = "taken" + (" by force" if d.get("force") else "")
            else:
                if scanning:
                    self._client.stop_scan()
                    self._finish(self._acq, "stopped", "stopped: control released")
                detail = "released"
                if self.em_is_on():
                    self._em_off("before releasing control", actor)
                if self.emission_is_on():
                    # Never leave emission on behind a released analyser:
                    # nothing could switch it off then.
                    self._client.set_emission(False)
                    self._expect("emission", False)
                    self._audit("rga_emission_off", "rga", "On", "Off", "ok",
                                "switched off before releasing control", actor)
                    detail = "released, emission switched off first"
                info = self._client.release_control()
        except ControlRefused as exc:
            self._ack(ACK_RGA_CONTROL, False, action, actor,
                      f"{exc}; tick 'take it anyway' to take it from them")
            return
        except (OSError, MMSPError) as exc:
            self._ack(ACK_RGA_CONTROL, False, action, actor, f"the analyser said: {exc}")
            return
        self._note_control(info)
        self._ack(ACK_RGA_CONTROL, True, action, actor, detail,
                  new=("held" if info.get("amInControl") else "released"))

    def _handle_scan(self, topic: str, payload: str) -> None:
        d, actor = self._parse(payload)
        what = str(d.get("action", "")).lower()
        action = "rga_scan_" + (what or "unknown")
        if what not in ("start", "stop"):
            self._ack(ACK_RGA_SCAN, False, action, actor, "action must be start or stop")
            return
        if not self._usable(ACK_RGA_SCAN, action, actor):
            return
        with self._acq_lock:
            self._last_activity = time.monotonic()
            acq = self._acq

        if what == "stop":
            if acq is None or acq["state"] != "scanning":
                self._ack(ACK_RGA_SCAN, False, action, actor, "no scan is running")
                return
            try:
                self._client.stop_scan()
            except (OSError, MMSPError) as exc:
                self._ack(ACK_RGA_SCAN, False, action, actor, f"the analyser said: {exc}")
                return
            self._finish(acq, "stopped")
            self._ack(ACK_RGA_SCAN, True, action, actor,
                      f"stopped after {len(acq['scans'])} of {acq['count']} scans")
            return

        name = str(d.get("recipe", ""))
        recipe = self._recipes.get(name)
        if recipe is None:
            self._ack(ACK_RGA_SCAN, False, action, actor,
                      self._recipe_error or f"no recipe called {name!r}")
            return
        if acq is not None and acq["state"] == "scanning":
            self._ack(ACK_RGA_SCAN, False, action, actor,
                      "a scan is already running; stop it first")
            return
        if not (self._control or {}).get("us"):
            self._ack(ACK_RGA_SCAN, False, action, actor,
                      "this system does not hold the analyser; take control first")
            return
        try:
            self._client.setup_scan(recipe["channels"], recipe["scans"])
            self._client.start_scan()
        except (OSError, MMSPError, PermissionError) as exc:
            self._ack(ACK_RGA_SCAN, False, action, actor, f"the analyser said: {exc}")
            return
        new = {"recipe": recipe, "count": recipe["scans"], "scans": [],
               "points": sum(rga_file.points(c) for c in recipe["channels"]),
               "masses": rga_file.masses(recipe), "t_start": iso(utcnow()),
               "mono_start": time.monotonic(), "state": "scanning", "by": actor,
               "detector": "EM" if self.em_is_on() else "Faraday"}
        with self._acq_lock:
            self._acq = new
        self.interval_s = self._scan_interval
        self._publish_info()
        self._ack(ACK_RGA_SCAN, True, action, actor,
                  f"{name}: {recipe['scans']} scans of ~{rga_file.scan_seconds(recipe):.0f} s",
                  new=name)

    def _handle_save(self, topic: str, payload: str) -> None:
        d, actor = self._parse(payload)
        action = "rga_save"
        with self._acq_lock:
            self._last_activity = time.monotonic()
            acq = self._acq
        if acq is None or not acq["scans"]:
            self._ack(ACK_RGA_SAVE, False, action, actor, "there is no acquisition to save")
            return
        if acq["state"] == "scanning":
            self._ack(ACK_RGA_SAVE, False, action, actor,
                      "the scan is still running; wait for it, or stop it")
            return
        if acq.get("saved"):
            self._ack(ACK_RGA_SAVE, False, action, actor,
                      f"already saved as {acq['saved']}")
            return
        try:
            device = self._read_settings()
        except (OSError, MMSPError) as exc:
            self._ack(ACK_RGA_SAVE, False, action, actor,
                      f"could not read the analyser's settings: {exc}")
            return
        tags = [t for t in (str(x).strip() for x in (d.get("tags") or [])) if t]
        with self._acq_lock:
            scans = [dict(s) for s in acq["scans"]]
        record = {
            "format": rga_file.FORMAT,
            "file": rga_file.file_name(
                datetime.fromisoformat(acq["t_start"].replace("Z", "+00:00")),
                acq["recipe"]["name"]),
            "t_start": acq["t_start"], "t_saved": iso(utcnow()),
            "recipe": acq["recipe"], "state": acq["state"],
            "device": device, "sc_snapshot": dict(self._snapshot),
            "comment": str(d.get("comment") or ""), "tags": tags,
            "background": (str(d.get("background")).strip() or None)
                          if d.get("background") else None,
            "operator": actor, "units": "A",
            "detector": acq.get("detector", "Faraday"),
            "scans": scans, "mean": rga_file.mean([s["values"] for s in scans]),
        }
        try:
            path = rga_file.write(self._data_dir, record)
        except (OSError, ValueError) as exc:
            self._ack(ACK_RGA_SAVE, False, action, actor, f"could not write the file: {exc}")
            return
        with self._acq_lock:
            acq["saved"] = record["file"]
        try:
            self._pending_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._saved_files = self._list_saved()
        self.bus.publish_raw(TOPIC_RGA_SAVED, json.dumps(
            rga_file.summary(record, self._summary_masses)))
        self._publish_info()
        log.info("RGA acquisition saved to %s by %s", path, actor)
        self._ack(ACK_RGA_SAVE, True, action, actor, f"saved as {record['file']}",
                  file=record["file"], new=record["file"])

    def _handle_emission(self, topic: str, payload: str) -> None:
        """Emission On or Off, from the page. On only past every check."""
        d, actor = self._parse(payload)
        what = str(d.get("action", "")).lower()
        action = "rga_emission_" + (what or "unknown")
        if what not in ("on", "off"):
            self._ack(ACK_RGA_EMISSION, False, action, actor, "action must be on or off")
            return
        if not self._usable(ACK_RGA_EMISSION, action, actor):
            return
        if not (self._control or {}).get("us"):
            self._ack(ACK_RGA_EMISSION, False, action, actor,
                      "this system does not hold the analyser; take control first")
            return
        with self._acq_lock:
            self._last_activity = time.monotonic()
        if what == "on":
            ok, why = self.guard_allows()
            if not ok:
                self._ack(ACK_RGA_EMISSION, False, action, actor, why)
                return
            if self._hw_errors:
                self._ack(ACK_RGA_EMISSION, False, action, actor,
                          f"the analyser reports hardware errors 0x{self._hw_errors:08X}")
                return
            if (self._status_word or 0) & INTERLOCK_BIT:
                self._ack(ACK_RGA_EMISSION, False, action, actor,
                          "the analyser's emission interlock is active")
                return
        old = "On" if self.emission_is_on() else "Off"
        if what == "off" and self.em_is_on():
            self._em_off("before emission off", actor)
        try:
            asked = self._client.set_emission(what == "on")
        except (OSError, MMSPError, PermissionError) as exc:
            self._ack(ACK_RGA_EMISSION, False, action, actor, f"the analyser said: {exc}")
            return
        want = "On" if what == "on" else "Off"
        self._expect("emission", what == "on")
        if asked != want:
            # The analyser answered "wasSet", so the request was taken; only
            # its read-back has not caught up. Saying "refused, nothing was
            # changed" here would be wrong (13 October 2026: it was, and the
            # emission came on).
            log.warning("RGA emission %s requested by %s; the analyser accepted it "
                        "but still reports %s", want, actor, asked)
            self._ack(ACK_RGA_EMISSION, True, action, actor,
                      f"emission {want} requested and accepted, but the analyser's "
                      f"read-back still says {asked}; check the emission state "
                      "in a few seconds", old=old, new=want)
            return
        self._emission_note = None
        log.warning("RGA emission %s requested by %s%s", want, actor,
                    f" ({self.guard_allows()[1]})" if want == "On" else "")
        self._ack(ACK_RGA_EMISSION, True, action, actor,
                  f"emission {want} requested; the analyser takes a few seconds to follow"
                  + (f" ({self.guard_allows()[1]})" if want == "On" else ""),
                  old=old, new=want)

    def _handle_em(self, topic: str, payload: str) -> None:
        """Electron multiplier On or Off, from the page (step 4). On only past
        every check; the pressure limit is the EM's own, lower one."""
        d, actor = self._parse(payload)
        what = str(d.get("action", "")).lower()
        action = "rga_em_" + (what or "unknown")
        if what not in ("on", "off"):
            self._ack(ACK_RGA_EM, False, action, actor, "action must be on or off")
            return
        if not self._usable(ACK_RGA_EM, action, actor):
            return
        if not (self._control or {}).get("us"):
            self._ack(ACK_RGA_EM, False, action, actor,
                      "this system does not hold the analyser; take control first")
            return
        with self._acq_lock:
            self._last_activity = time.monotonic()
            scanning = self._acq is not None and self._acq["state"] == "scanning"
        if what == "on":
            word = self._status_word or 0
            checks = [
                (scanning, "a scan is running; one detector per acquisition - "
                           "switch the EM between scans"),
                (not self.emission_is_on(), "emission is off; switch emission on first"),
                (bool(self._hw_errors),
                 f"the analyser reports hardware errors 0x{self._hw_errors or 0:08X}"),
                (bool(word & EM_INTERLOCK_BIT), "the analyser's EM interlock is active"),
            ]
            for failed, why in checks:
                if failed:
                    self._ack(ACK_RGA_EM, False, action, actor, why)
                    return
            ok, why = self.em_guard_allows()
            if not ok:
                self._ack(ACK_RGA_EM, False, action, actor, f"EM limit: {why}")
                return
        old = "On" if self.em_is_on() else "Off"
        want = "On" if what == "on" else "Off"
        if what == "off":
            if not self._em_off("switched off from the page", actor):
                self._ack(ACK_RGA_EM, False, action, actor,
                          "the analyser did not take the request; see the log")
                return
            self._ack(ACK_RGA_EM, True, action, actor,
                      "EM off requested; the analyser takes a few seconds to follow",
                      old=old, new=want)
            return
        try:
            asked = self._client.set_em(True)
        except (OSError, MMSPError, PermissionError) as exc:
            self._ack(ACK_RGA_EM, False, action, actor, f"the analyser said: {exc}")
            return
        self._em_note = None
        self._expect("em", True)
        why = self.em_guard_allows()[1]
        log.warning("RGA EM On requested by %s (%s)", actor, why)
        detail = (f"EM on requested ({why}); the analyser takes a few seconds to follow"
                  if asked == want else
                  f"EM on requested and accepted, but the analyser's read-back still "
                  f"says {asked}; check the EM state in a few seconds ({why})")
        self._ack(ACK_RGA_EM, True, action, actor, detail, old=old, new=want)

    def _read_settings(self) -> dict:
        out = dict(self._identity or {})
        for key, target in SETTINGS.items():
            out[key] = self._client.get(target)
        code = int(out.get("tp_units_code", 0))
        out["unit_to_mbar"] = TO_MBAR.get(code)
        tp = out.get("total_pressure")
        out["total_pressure_mbar"] = (float(tp) * TO_MBAR[code]
                                      if isinstance(tp, (int, float)) and tp > 0
                                      and code in TO_MBAR else None)
        return out

    def _on_snapshot(self, topic: str, payload: str) -> None:
        try:
            m = json.loads(payload)
        except ValueError:
            return
        name = topic.rsplit("/", 1)[-1]
        self._snapshot[name] = {
            "v": m.get("v"), "u": m.get("u"), "q": m.get("q"), "t": m.get("t")}
        if name == self._guard_channel:
            self._guard_seen = time.monotonic()

    # -------------------------------------------------------------- run

    def _enqueue(self, handler):
        """A bus callback that only queues: the MQTT thread never waits on
        the analyser."""
        return lambda topic, payload: self._commands.put((handler, topic, payload))

    def _work(self) -> None:
        while True:
            item = self._commands.get()
            if item is None:
                return
            handler, topic, payload = item
            try:
                handler(topic, payload)
            except Exception:
                log.exception("RGA command on %s raised", topic)

    def run(self) -> int:
        self.bus.subscribe(TOPIC_RGA_CONTROL, self._enqueue(self._handle_control))
        self.bus.subscribe(TOPIC_RGA_SCAN, self._enqueue(self._handle_scan))
        self.bus.subscribe(TOPIC_RGA_SAVE, self._enqueue(self._handle_save))
        self.bus.subscribe(TOPIC_RGA_EMISSION, self._enqueue(self._handle_emission))
        self.bus.subscribe(TOPIC_RGA_EM, self._enqueue(self._handle_em))
        for ch in dict.fromkeys(self._snapshot_channels + [self._guard_channel]):
            self.bus.subscribe(f"{TOPIC_MEAS}/{ch}", self._on_snapshot)
        worker = threading.Thread(target=self._work, name="rga-commands", daemon=True)
        worker.start()
        try:
            return super().run()
        finally:
            self._commands.put(None)

    def _read_simulated(self) -> list[Measurement]:
        now = utcnow()
        elapsed = time.monotonic() - self._t0
        out = []
        for ch in self._channels:
            if ch.phys == "diagnosticData/internalBoxTemperature":
                value = 36.0 + 0.5 * math.sin(elapsed / 60.0)
            elif ch.phys == "status/systemStatus":
                value = float(0x01103B90)
            else:
                value = 0.0
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=value, quality=Quality.OK,
                                   src="sim"))
        return out

    def close_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def close(self) -> None:
        """On shutdown: stop a running scan, switch the EM and emission off,
        give control back, then close.

        A service that stops while holding the analyser would lock PV MassSpec
        out for the device's own timeout - minutes, for nothing. Emission left
        on with nobody watching p105 is worse, so it goes off first.
        """
        if self._client is not None:
            try:
                with self._acq_lock:
                    scanning = self._acq is not None and self._acq["state"] == "scanning"
                if scanning:
                    self._client.stop_scan()
                info = self._client.get("communication/controlInfo") or {}
                if info.get("amInControl"):
                    status = int(self._client.get("status/systemStatus"))
                    if em_on(status):
                        self._client.set_em(False)
                        log.warning("RGA EM switched off on shutdown")
                        self._audit("rga_em_off", "rga", "On", "Off", "ok",
                                    "switched off: service stopping", "rga service")
                    if emission_on(status):
                        self._client.set_emission(False)
                        log.warning("RGA emission switched off on shutdown")
                        self._audit("rga_emission_off", "rga", "On", "Off", "ok",
                                    "switched off: service stopping", "rga service")
                    self._client.release_control()
                    log.info("RGA control released on shutdown")
            except (OSError, MMSPError) as exc:
                log.warning("could not release the RGA on shutdown: %s", exc)
            self.close_client()
            log.info("RGA connection closed")
