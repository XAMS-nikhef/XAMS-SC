"""Residual gas analyser (Pfeiffer PrismaPro), over Ethernet. See docs/drivers/rga.md.

Step 1 of the RGA plan: STATUS ONLY. This service reads the analyser's state
and publishes a handful of channels; it takes no control and writes nothing.
The HTTP client it uses (`prismapro.MMSPClient`) has no write method at all.

What is published, every `read_interval_s`:

  * a few channels from `channels.yaml` (device `rga`), whose `phys` names
    what is read - see `READERS` below. Spectra are never channels: they are
    saved by hand, one file per measurement (step 2).
  * a RETAINED `xams/rga/info` record for the RGA page: identity, firmware,
    filaments, units, and who is in control. Not archived.

Identity (section 6.2): the analyser must report the `name` and `serial` set
in `devices.yaml`, or the service refuses to start. An IP address alone says
nothing about what answers on it.
"""

from __future__ import annotations

import json
import logging
import math
import time

from ..bus import TOPIC_RGA_INFO
from ..config import Config
from ..model import Measurement, Quality, iso, utcnow
from ..service import BaseService
from .prismapro import MMSPClient, MMSPError, emission_on, read_identity

log = logging.getLogger(__name__)

# Control as one code: who holds the analyser.
CONTROL_NONE, CONTROL_US, CONTROL_OTHER = 0, 1, 2


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


class RgaService(BaseService):
    name = "rga"

    def __init__(self, config: Config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._spec = config.devices.get("rga", {}) or {}
        self._channels = config.channels_for("rga")
        # The analyser is asked over HTTP, one request per target. Once every
        # few seconds is plenty for status, and kind to a device that drops
        # requests that come too fast.
        self.interval_s = max(self.interval_s,
                              float(self._spec.get("read_interval_s", 5.0)))
        self._client: MMSPClient | None = None
        self._identity: dict | None = None
        self._control: dict | None = None
        self._t0 = time.monotonic()

    # ------------------------------------------------------------ identity

    def _check_channels(self) -> bool:
        unknown = [ch.name for ch in self._channels if ch.phys not in READERS]
        for name in unknown:
            log.critical("FATAL: channel %s names %r, which this driver does not "
                         "know how to read. Refusing to start rather than guess.",
                         name, next(c.phys for c in self._channels if c.name == name))
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

        client = MMSPClient(host, int(self._spec.get("port", 80)),
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
        self._client = client
        self._identity = ident.as_dict() | {"host": host}
        self._publish_info()
        return True

    def reconnect(self) -> bool:
        if self.simulate:
            return True
        self.close()
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

        now = utcnow()
        out = []
        for ch in self._channels:
            target, convert = READERS[ch.phys]
            value = convert(data[target])
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=value, quality=Quality.OK))
        return out

    def _note_control(self, info: dict) -> None:
        """Publish the controlling client when it changes, and say so in the log.

        The page needs to show WHO holds the analyser - PV MassSpec, somebody's
        browser - and a number cannot carry a name.
        """
        summary = {"controlled": bool(info.get("controlled")),
                   "client": info.get("clientName") or "",
                   "ip": info.get("ipAddress") if info.get("ipAddress") not in (None, "null") else "",
                   "locked": bool(info.get("locked"))}
        if summary != self._control:
            if summary["controlled"]:
                log.info("RGA now controlled by %r from %s", summary["client"],
                         summary["ip"] or "?")
            elif self._control is not None:
                log.info("RGA control released")
            self._control = summary
            self._publish_info()

    def _publish_info(self) -> None:
        record = {"t": iso(utcnow()), "identity": self._identity,
                  "control": self._control}
        self.bus.publish_raw(TOPIC_RGA_INFO, json.dumps(record), retain=True)

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

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            log.info("RGA connection closed")
