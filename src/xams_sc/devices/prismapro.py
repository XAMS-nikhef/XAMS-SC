"""Pfeiffer PrismaPro QMG 250 (RGA): the MMSP HTTP/JSON client. See docs/drivers/rga.md.

This module knows HTTP and the PrismaPro, and nothing about MQTT, so it can be
used from a notebook as well as from the service in `rga.py`.

**THIS CLIENT ONLY READS.** There is no `set` here yet: step 1 of the RGA plan
is status only. Writes (control, scan setup, emission) arrive in step 2 and 3,
behind a whitelist of targets - not as a general-purpose `set(path, value)`.

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
"""

from __future__ import annotations

import http.client
import json
import logging
import time
from dataclasses import dataclass

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
