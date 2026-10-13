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
import threading
import time
from datetime import datetime

from .. import rga_file
from ..bus import (ACK_RGA_CONTROL, ACK_RGA_SAVE, ACK_RGA_SCAN, TOPIC_AUDIT,
                   TOPIC_MEAS, TOPIC_RGA_CONTROL, TOPIC_RGA_INFO, TOPIC_RGA_SAVE,
                   TOPIC_RGA_SAVED, TOPIC_RGA_SCAN, TOPIC_RGA_SCANDATA)
from ..config import CONFIG_DIR, ROOT, Config
from ..model import Measurement, Quality, iso, utcnow
from ..service import BaseService
from .prismapro import (TO_MBAR, ControlRefused, MMSPControl, MMSPError,
                        emission_on, read_identity)

log = logging.getLogger(__name__)

# Control as one code: who holds the analyser.
CONTROL_NONE, CONTROL_US, CONTROL_OTHER = 0, 1, 2

SCANNING_BIT = 1 << 1
SUMMARY_MASSES = [2, 4, 18, 28, 32, 40, 44, 129, 132]


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
        self._load_recipes(int(self._spec.get("mass_range", 200)))

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
        self._publish_info()
        return True

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
        for target in {READERS[ch.phys][0] for ch in self._channels}:
            data[target] = self._client.get(target)

        control = data.get("communication/controlInfo")
        if control is not None:
            self._note_control(control)

        self._collect_scans()
        self._release_if_idle(control or {})

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
        record = {"t": iso(utcnow()), "identity": self._identity,
                  "control": self._control, "acquisition": acq,
                  "recipes": {n: {"description": r["description"], "scans": r["scans"],
                                  "seconds": round(rga_file.scan_seconds(r), 1)}
                              for n, r in self._recipes.items()},
                  "recipe_error": self._recipe_error}
        self.bus.publish_raw(TOPIC_RGA_INFO, json.dumps(record), retain=True)

    def _acq_view(self) -> dict | None:
        a = self._acq
        if a is None:
            return None
        return {"recipe": a["recipe"]["name"], "state": a["state"],
                "done": len(a["scans"]), "of": a["count"], "t_start": a["t_start"],
                "by": a["by"], "saved": a.get("saved"), "error": a.get("error")}

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
        self._publish_info()

    def _publish_scan(self, acq: dict) -> None:
        with self._acq_lock:
            last = acq["scans"][-1]
            payload = {"t": last["t"], "recipe": acq["recipe"]["name"],
                       "scannum": last["scannum"], "done": len(acq["scans"]),
                       "of": acq["count"], "masses": acq["masses"],
                       "values": last["values"],
                       "mean": rga_file.mean([s["values"] for s in acq["scans"]])}
        self.bus.publish_raw(TOPIC_RGA_SCANDATA, json.dumps(payload), retain=True)
        self._publish_info()

    def _release_if_idle(self, control: dict) -> None:
        if not control.get("amInControl"):
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
                info = self._client.release_control()
                detail = "released"
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
               "mono_start": time.monotonic(), "state": "scanning", "by": actor}
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
            "scans": scans, "mean": rga_file.mean([s["values"] for s in scans]),
        }
        try:
            path = rga_file.write(self._data_dir, record)
        except (OSError, ValueError) as exc:
            self._ack(ACK_RGA_SAVE, False, action, actor, f"could not write the file: {exc}")
            return
        with self._acq_lock:
            acq["saved"] = record["file"]
        self.bus.publish_raw(TOPIC_RGA_SAVED, json.dumps(
            rga_file.summary(record, self._summary_masses)))
        self._publish_info()
        log.info("RGA acquisition saved to %s by %s", path, actor)
        self._ack(ACK_RGA_SAVE, True, action, actor, f"saved as {record['file']}",
                  file=record["file"], new=record["file"])

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
        self._snapshot[topic.rsplit("/", 1)[-1]] = {
            "v": m.get("v"), "u": m.get("u"), "q": m.get("q"), "t": m.get("t")}

    # -------------------------------------------------------------- run

    def run(self) -> int:
        self.bus.subscribe(TOPIC_RGA_CONTROL, self._handle_control)
        self.bus.subscribe(TOPIC_RGA_SCAN, self._handle_scan)
        self.bus.subscribe(TOPIC_RGA_SAVE, self._handle_save)
        for ch in self._snapshot_channels:
            self.bus.subscribe(f"{TOPIC_MEAS}/{ch}", self._on_snapshot)
        return super().run()

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
        """On shutdown: stop a running scan and give control back, then close.

        A service that stops while holding the analyser would lock PV MassSpec
        out for the device's own timeout - minutes, for nothing.
        """
        if self._client is not None:
            try:
                with self._acq_lock:
                    scanning = self._acq is not None and self._acq["state"] == "scanning"
                if scanning:
                    self._client.stop_scan()
                info = self._client.get("communication/controlInfo") or {}
                if info.get("amInControl"):
                    self._client.release_control()
                    log.info("RGA control released on shutdown")
            except (OSError, MMSPError) as exc:
                log.warning("could not release the RGA on shutdown: %s", exc)
            self.close_client()
            log.info("RGA connection closed")
