"""Pfeiffer PrismaPro QMG 250 (RGA): the MMSP HTTP/JSON client. See docs/drivers/rga.md.

This module knows HTTP and the PrismaPro, and nothing about MQTT, so it can be
used from a notebook as well as from the service in `rga.py`.

**WRITES ARE A WHITELIST, NOT A FUNCTION.** There is no general-purpose
`set(path, value)`. `MMSPClient` reads; `MMSPControl` (below) adds the few
writes the RGA plan needs, each built from a fixed target in `WRITABLE`:
taking and releasing control, setting up, starting and stopping a scan, and
switching emission On or Off (step 3, with nothing but those two values).
The electron multiplier, the ion source, filament selection, degas, IO,
network and firmware are not in it and cannot be reached from here.

The protocol, as found on our unit (firmware 1.9.5, 13 October 2026):

  * `GET /mmsp/<group>/<target>/get` returns
    `{"name": "got", "origin": "/mmsp/...", "data": ...}`.
  * Errors come back as HTTP 200 too, with `"name": "error"` and a
    `conditions` list. The HTTP status alone says nothing, so every reply is
    checked by its `name`.
  * Every group can be read without taking control, including the targets
    the API reference marks `readmode: locked`. Only writes need control.
  * **It drops rapid back-to-back connections.** A burst of separate requests
    mostly came back empty; one reused connection, or one request a second,
    worked every time. So this client keeps ONE keep-alive connection open,
    leaves a small gap between requests, and on a failure closes it and tries
    exactly once more on a fresh one.
  * A write answers `"name": "wasSet"` with what the device now holds; a
    sweep channel reports `reportUnits: "Current"`, so scan values are ion
    currents in AMPERE. While a scan runs, systemStatus bit 1 is set.
    (Live test with control taken, emission off, 13 October 2026.)
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

log = logging.getLogger(__name__)


class MMSPError(RuntimeError):
    """The device answered, but with an error event, or not with JSON."""


class MMSPClient:
    """One keep-alive HTTP connection to a PrismaPro, read-only."""

    def __init__(self, host: str, port: int = 80, timeout_s: float = 5.0,
                 min_gap_s: float = 0.1):
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.min_gap_s = min_gap_s
        self._conn: http.client.HTTPConnection | None = None
        self._last = 0.0
        # One connection, two threads: the service loop reads, and commands
        # arrive on the MQTT thread. A request and its reply must not be
        # interleaved with another's, so every request holds this.
        self.lock = threading.RLock()

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(self.host, self.port,
                                                    timeout=self.timeout_s)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _request(self, path: str) -> bytes:
        with self.lock:
            return self._request_locked(path)

    def _request_locked(self, path: str) -> bytes:
        wait = self.min_gap_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            conn = self._connection()
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise MMSPError(f"{path}: HTTP {resp.status}")
            return body
        finally:
            self._last = time.monotonic()

    def raw(self, path: str) -> dict:
        """GET one path and return the decoded event, error events included.

        A broken connection is retried once on a fresh one; the device closing
        a kept-alive connection is normal, not a fault.
        """
        with self.lock:
            try:
                body = self._request(path)
            except (OSError, http.client.HTTPException):
                self.close()
                body = self._request(path)
        if not body.strip():
            # What a dropped request looks like: a 200 with nothing in it.
            raise MMSPError(f"{path}: empty reply")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise MMSPError(f"{path}: not JSON: {body[:80]!r}") from exc

    def get(self, target: str):
        """Read one target, e.g. `status/systemStatus`. Returns its `data`."""
        path = f"/mmsp/{target.strip('/')}/get"
        event = self.raw(path)
        if event.get("name") != "got":
            conditions = event.get("conditions") or []
            ids = ", ".join(str(c.get("id")) for c in conditions) or event.get("name")
            raise MMSPError(f"{target}: {ids}")
        return event.get("data")


# ------------------------------------------------------------------ writing

# Every target this code may write, and nothing else. A path not built from
# one of these cannot be sent: `MMSPControl._set` refuses it.
WRITABLE = frozenset({
    "communication/clientName",
    "communication/control",
    "communication/controlLock",
    "scanSetup",                 # startChannel, stopChannel, scanCount, scanStart, scanStop
    "scanSetup/channels/{n}",    # channelMode, startMass, stopMass, ppamu, dwell, enabled
    "generalControl/setEmission",  # On or Off, nothing else (EMISSION_VALUES)
})

# The only values setEmission may be given. On also starts the RF; Off stops
# both (API reference, "Set Emission State").
EMISSION_VALUES = frozenset({"On", "Off"})

# The fields of scanSetup and of a channel that may be set.
SCAN_FIELDS = frozenset({"startChannel", "stopChannel", "scanCount",
                         "scanStart", "scanStop"})
CHANNEL_FIELDS = frozenset({"channelMode", "startMass", "stopMass", "ppamu",
                            "dwell", "enabled"})


class ControlRefused(MMSPError):
    """Somebody else holds the analyser, and taking it was not asked for."""


class MMSPControl(MMSPClient):
    """The client plus the whitelisted writes of the RGA plan."""

    def _set(self, target: str, fields: dict) -> dict:
        """Write `fields` to one whitelisted target; return what it now holds."""
        template = ("scanSetup/channels/{n}"
                    if target.startswith("scanSetup/channels/") else target)
        if template not in WRITABLE:
            raise PermissionError(f"{target} is not writable from this code")
        allowed = (CHANNEL_FIELDS if template == "scanSetup/channels/{n}"
                   else SCAN_FIELDS if template == "scanSetup" else None)
        if allowed is not None and not set(fields) <= allowed:
            raise PermissionError(
                f"{target}: fields {sorted(set(fields) - allowed)} are not writable")
        if allowed is None:
            # A bare value: /communication/control/set?force
            (value,) = fields.values()
            if target == "generalControl/setEmission" and value not in EMISSION_VALUES:
                raise PermissionError(f"setEmission accepts only {sorted(EMISSION_VALUES)}")
            query = quote(str(value), safe="")
        else:
            query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in fields.items())
        event = self.raw(f"/mmsp/{target}/set?{query}")
        if event.get("name") != "wasSet":
            conditions = event.get("conditions") or []
            ids = ", ".join(str(c.get("id")) for c in conditions) or event.get("name")
            raise MMSPError(f"{target}: {ids}")
        return event.get("data")

    # ------------------------------------------------------------- control

    def take_control(self, client_name: str, force: bool = False) -> dict:
        """Take the analyser (flowchart, firmware >= 1.9.0).

        Refused if another client holds it, unless `force` - which takes it
        away from PV MassSpec or a browser mid-measurement, so it is only
        ever passed when an operator chose it.
        """
        with self.lock:
            info = self.get("communication/controlInfo") or {}
            if info.get("amInControl"):
                return info
            if info.get("controlled") and not force:
                raise ControlRefused(
                    "the RGA is held by %r from %s" % (
                        info.get("clientName") or "another client",
                        info.get("ipAddress") or "?"))
            self._set("communication/clientName", {"v": client_name})
            # "force" is the API's word for "take", whether or not anybody
            # held it; the refusal above is what keeps it polite.
            self._set("communication/control", {"v": "force"})
            self._set("communication/controlLock", {"v": "Session"})
            info = self.get("communication/controlInfo") or {}
            if not info.get("amInControl"):
                raise MMSPError("asked for control, but the RGA does not say "
                                "this session holds it")
            return info

    def release_control(self) -> dict:
        with self.lock:
            self._set("communication/controlLock", {"v": "Unlocked"})
            self._set("communication/control", {"v": "release"})
            return self.get("communication/controlInfo") or {}

    # ---------------------------------------------------------------- scans

    def setup_scan(self, channels: list[dict], count: int) -> None:
        """Load a recipe's channels into slots 1..n and set the scan count.

        `channels` are already validated (`rga_file.validate_recipe`); this
        only translates them.
        """
        with self.lock:
            self.stop_scan()
            for n, ch in enumerate(channels, start=1):
                target = f"scanSetup/channels/{n}"
                if ch["mode"] == "sweep":
                    self._set(target, {"channelMode": "Sweep",
                                       "startMass": ch["start"], "stopMass": ch["stop"]})
                    self._set(target, {"dwell": ch["dwell"], "ppamu": ch["ppamu"],
                                       "enabled": "True"})
                else:
                    self._set(target, {"channelMode": "Single", "startMass": ch["mass"]})
                    self._set(target, {"dwell": ch["dwell"], "ppamu": 1,
                                       "enabled": "True"})
            self._set("scanSetup", {"startChannel": 1, "stopChannel": len(channels)})
            self._set("scanSetup", {"scanCount": int(count)})

    def start_scan(self) -> None:
        self._set("scanSetup", {"scanStart": 1})

    def stop_scan(self) -> None:
        self._set("scanSetup", {"scanStop": "Immediately"})

    # ------------------------------------------------------------ emission

    # The read-back of setEmission lags the write: on 13 October 2026 a read
    # straight after a "wasSet" still gave the old value, while the write had
    # taken (the next read gave the new one). So the read-back is polled.
    EMISSION_CONFIRM_S = 3.0

    def set_emission(self, on: bool) -> str:
        """Ask for emission On or Off; return what the device now says it was
        asked for, polled for up to EMISSION_CONFIRM_S until it agrees. The
        emission itself follows within seconds - its real state is
        systemStatus bit 31/30 (`emission_on`), not this reply."""
        want = "On" if on else "Off"
        with self.lock:
            self._set("generalControl/setEmission", {"v": want})
            deadline = time.monotonic() + self.EMISSION_CONFIRM_S
            while True:
                asked = str(self.get("generalControl/setEmission"))
                if asked == want or time.monotonic() >= deadline:
                    return asked
                time.sleep(0.25)


# ------------------------------------------------------------------ decoding

# systemStatus, from the API reference (V1.9.10, "System Status"). Only the
# bits worth naming; the full word is published as it is.
SYSTEM_STATUS_BITS = {
    31: "emission_regulated",
    30: "emission_constant_power",
    29: "degas",
    28: "emission_request_pending",
    27: "emission_interlock",
    26: "filament_2_selected",
    25: "rf_on",
    24: "rod_polarity_reversed",
    23: "em_on",
    22: "em_request_pending",
    21: "em_interlock",
    13: "peak_position_measurement_scheduled",
    12: "peak_position_correction",
    11: "baseline_continuous_scheduled",
    10: "baseline_single_scheduled",
    9: "baseline_correction",
    1: "scanning",               # seen set during a scan, 13 October 2026
}


def decode_system_status(word: int) -> dict[str, bool]:
    """Name the bits of a systemStatus word that are worth naming."""
    return {name: bool(word >> bit & 1) for bit, name in SYSTEM_STATUS_BITS.items()}


def emission_on(word: int) -> bool:
    """Emission is on when the regulator runs in either of its modes."""
    return bool(word >> 31 & 1 or word >> 30 & 1)


# tPunits / gauge units: 0 Torr, 1 mbar, 2 Pa (API reference, "TP units").
TO_MBAR = {0: 1.333223684, 1: 1.0, 2: 0.01}
UNIT_NAMES = {0: "Torr", 1: "mbar", 2: "Pa"}


@dataclass(frozen=True)
class Identity:
    name: str            # sensorInfo/name, e.g. "RGA PrismaPro A 200 47504651"
    serial: str          # electronicsInfo/serialNumber
    firmware: str        # electronicsInfo/controlFWVersion
    mass_range: int      # electronicsInfo/massRange, amu
    filament_types: tuple[int, ...]   # 0 tungsten, 1 yttria-coated iridium
    detector: str        # sensorInfo/detectorType
    tp_units: int        # sensorIonSource/tPunits

    def as_dict(self) -> dict:
        return {
            "name": self.name, "serial": self.serial, "firmware": self.firmware,
            "mass_range_amu": self.mass_range,
            "filaments": ["yttria-iridium" if t == 1 else "tungsten"
                          for t in self.filament_types],
            "detector": self.detector,
            "tp_units": UNIT_NAMES.get(self.tp_units, str(self.tp_units)),
        }


def read_identity(client: MMSPClient) -> Identity:
    filaments = client.get("sensorInfo/filaments") or []
    return Identity(
        name=str(client.get("sensorInfo/name")),
        serial=str(client.get("electronicsInfo/serialNumber")),
        firmware=str(client.get("electronicsInfo/controlFWVersion")),
        mass_range=int(client.get("electronicsInfo/massRange")),
        filament_types=tuple(int(f.get("filamentType", -1)) for f in filaments),
        detector=str(client.get("sensorInfo/detectorType")),
        tp_units=int(client.get("sensorIonSource/tPunits")),
    )
