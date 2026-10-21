"""LN2 dewar scale: All Scales Europe ISCI indicator. See docs/drivers/scale.md.

The indicator is a Dini Argeo DFW under another label (`VER` answers
"VER,311,DFW06"). It is read on its RS-232 PC port - the RJ45 socket on the
back, which is not Ethernet - through the supplier's RJ45-USB cable (FTDI),
at the factory settings: 9600 baud, 8-N-1, every command and answer ending
in CR LF, PC port in `ondE` mode so it speaks only when asked.

**QUERIES ONLY.** The protocol can also tare, zero, press keys and write on
the display. None of that is wanted from here: a tare sent by accident would
make every following reading a net weight. The port wrapper therefore takes
only the commands in `QUERIES` and refuses anything else before a byte is
sent, so a typo cannot turn a question into an instruction.

**One reading per cycle, two weights from it.** `RALL` returns net, gross
and tare at once. The gross is used, never the net: somebody pressing TARE
on the front panel changes the net and leaves the gross alone, so the
archive does not jump. `ln2_gross` is that number; `ln2_mass` is the same
number minus the empty dewar, set as the channel's offset in channels.yaml.

**Identity before any write.** The turbo's RS-485 tap is an FTDI adapter
with the same USB vid:pid. Probing it with `VER` would put bytes on the
pump's bus, which that service exists never to do. So the port is chosen by
its USB serial number first, and only that one port is asked who it is.

A reading the indicator marks overload, underload or tilted, or one in a
unit other than kg, is published as quality=error with no value - never as
a plausible number.
"""

from __future__ import annotations

import logging
import math
import random
import re
import threading
import time

import serial

from ..config import Config
from ..model import Measurement, Quality, utcnow
from ..scaling import apply
from ..service import BaseService
from .serial_id import candidates

log = logging.getLogger(__name__)

# THE WHITELIST. Every command here only asks; see ISC-Series serial protocol
# v-ISCs-3-21. TARE, T, TMAN, ZERO, Z, C, CLEAR, KEYP, KEYR, KEYEe, DISP, DINT,
# PCOK, SPMU, STPT, TATO, TLCKe, CMDSAVE, NTGS, PRNT, PID, PIDD and ALDL are
# left out on purpose.
QUERIES = frozenset({"VER", "READ", "REXT", "RALL", "GR10", "ALIM", "ALIMN",
                     "STAT", "ECHO"})

# Weight status on the indicator. Only these two carry a valid weight; the
# others mean the number is out of range or the platform is not level.
VALID_STATUS = {"ST": "stable", "US": "unstable"}
INVALID_STATUS = {"OL": "overload", "UL": "underload", "TL": "tilted"}

_WEIGHT = re.compile(r"^\s*(PT)?\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Z]{1,2})\s*$")


class ScaleError(Exception):
    """The indicator answered something that is not a valid reading."""


def parse_weight(field: str) -> float:
    """One weight field of RALL, e.g. ' 278.20kg' or 'PT  1.500kg', in kg.

    Anything but kg is refused rather than converted: the unit is set on the
    indicator, and a scale switched to lb would otherwise read 2.2 times too
    heavy without anything looking wrong.
    """
    m = _WEIGHT.match(field)
    if not m:
        raise ScaleError(f"not a weight: {field!r}")
    unit = m.group(3).lower()
    if unit != "kg":
        raise ScaleError(f"weight in {unit!r}, expected kg: {field!r}")
    return float(m.group(2))


def parse_rall(text: str) -> dict:
    """The RALL answer: status, net, gross and tare.

        ST,1, 278.20kg, 278.20kg,     0.00kg,   0.00kg,000,001,0000,000,00000-000000
        ss c  net       gross     [PT]tare    last total ...

    Returns {"status", "net", "gross", "tare"}. Raises ScaleError on anything
    else, including a status that does not carry a valid weight.
    """
    parts = [p for p in (text or "").split(",")]
    if len(parts) < 5:
        raise ScaleError(f"RALL answer too short: {text!r}")
    status = parts[0].strip()
    if status in INVALID_STATUS:
        raise ScaleError(f"scale reports {INVALID_STATUS[status]} ({status})")
    if status not in VALID_STATUS:
        raise ScaleError(f"unknown scale status {status!r} in {text!r}")
    return {"status": status,
            "net": parse_weight(parts[2]),
            "gross": parse_weight(parts[3]),
            "tare": parse_weight(parts[4])}


def parse_alim(text: str) -> tuple[bool, int]:
    """ALIM: 'PW: 1 BT: 9' -> (on mains, battery level 0-9)."""
    m = re.match(r"^\s*PW:\s*([01])\s+BT:\s*(\d)\s*$", text or "")
    if not m:
        raise ScaleError(f"not an ALIM answer: {text!r}")
    return m.group(1) == "1", int(m.group(2))


class DiniScale:
    """The indicator's ASCII protocol, queries only."""

    def __init__(self, port: str, baud: int = 9600):
        self.port = port
        self.baud = baud
        self._serial: serial.Serial | None = None
        # A reply is matched to its query only by arriving next, so one
        # conversation at a time (see the Lake Shore driver for what happens
        # otherwise).
        self._lock = threading.Lock()

    def open(self) -> None:
        self._serial = serial.Serial(
            self.port, self.baud, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=1.0, write_timeout=1.0)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def ask(self, command: str) -> str | None:
        """Send one query and return its answer, or None if none came.

        Refuses anything not in QUERIES before touching the port.
        """
        if command not in QUERIES:
            raise ValueError(f"{command!r} is not a query this driver sends")
        if self._serial is None:
            return None
        with self._lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write((command + "\r\n").encode("ascii"))
                raw = self._serial.read_until(b"\r\n", 200)
            except Exception as exc:
                log.debug("%s raised %s", command, exc)
                return None
        text = raw.decode("ascii", errors="replace").strip("\r\n")
        if not text.strip():
            return None
        if text.startswith("ERR"):
            log.warning("%s -> %s", command, text.strip())
            return None
        return text

    def version(self) -> str | None:
        """'VER,311,DFW06   ' -> 'VER,311,DFW06'."""
        text = self.ask("VER")
        return text.strip() if text else None


class ScaleService(BaseService):
    name = "scale"

    def __init__(self, config: Config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._spec = config.devices.get("scale", {}) or {}
        self._channels = config.channels_for("scale")
        self._device: DiniScale | None = None
        self._t0 = time.monotonic()

    # -------------------------------------------------------------- identity

    def verify_identity(self) -> bool:
        if self.simulate:
            log.info("simulate=True: skipping hardware identity check")
            return True

        match = self._spec.get("match", {}) or {}
        vid, pid = str(match.get("vid", "")), str(match.get("pid", ""))
        wanted_serial = str(match.get("serial", "")).strip().upper()
        wanted_model = str(self._spec.get("idn_contains", "DFW06"))
        baud = int(self._spec.get("baud", 9600))

        if not wanted_serial:
            # Not a bootstrap convenience as for the Lake Shore: without the
            # serial the turbo's tap is a candidate too, and it must never be
            # written to.
            log.critical("FATAL: devices.yaml pins no USB serial for the "
                         "scale. Other FTDI adapters (the turbo's RS-485 "
                         "tap) share its vid:pid, so it is not probed blind.")
            return False

        ports = [c for c in candidates(vid, pid)
                 if (c.usb_serial or "").upper() == wanted_serial]
        if not ports:
            log.critical("FATAL: no serial port with USB id %s:%s and serial "
                         "%s. Is the scale's USB cable plugged in?",
                         vid, pid, wanted_serial)
            return False
        if len(ports) > 1:
            log.critical("FATAL: %d ports report USB serial %s (%s); refusing "
                         "to guess.", len(ports), wanted_serial,
                         ", ".join(c.port for c in ports))
            return False

        port = ports[0].port
        device = DiniScale(port, baud)
        try:
            device.open()
        except Exception as exc:
            log.critical("FATAL: could not open %s: %s", port, exc)
            return False
        version = device.version()
        if not version or wanted_model not in version:
            log.critical("FATAL: %s answered VER with %r, expected a model "
                         "containing %r. Check the PC port settings on the "
                         "indicator (9600, n-8-1, mode ondE).",
                         port, version, wanted_model)
            device.close()
            return False

        log.info("%s reports %s", port, version)
        self._device = device
        return True

    def reconnect(self) -> bool:
        """Re-resolve the port and ask VER again (§6.2 rule 6)."""
        if self.simulate:
            return True
        if self._device is not None:
            self._device.close()
            self._device = None
        return self.verify_identity()

    def close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None
        log.info("scale port released")

    # ------------------------------------------------------------------ read

    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()

        d = self._device
        if d is None:
            raise RuntimeError("scale not connected")

        now = utcnow()
        answer = d.ask("RALL")
        if answer is None:
            raise RuntimeError("scale did not answer RALL; link lost")

        weight = None
        try:
            weight = parse_rall(answer)
        except ScaleError as exc:
            # The link works but the weight is not valid (overload, tilt, a
            # unit that is not kg). Not a reason to reconnect: publish the
            # weights as errors and carry on.
            log.warning("%s", exc)

        mains = None
        alim = d.ask("ALIM")
        if alim is not None:
            try:
                mains, _battery = parse_alim(alim)
            except ScaleError as exc:
                log.warning("%s", exc)

        return self._measurements(now, weight, mains, src="xams")

    def _measurements(self, now, weight: dict | None, mains: bool | None,
                      src: str) -> list[Measurement]:
        out = []
        for ch in self._channels:
            if ch.phys in ("gross", "ln2"):
                raw = None if weight is None else weight["gross"]
            elif ch.phys == "mains":
                raw = None if mains is None else (1.0 if mains else 0.0)
            else:
                log.error("%s: unknown phys %r", ch.name, ch.phys)
                raw = None
            if raw is None:
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=Quality.ERROR,
                                       src=src))
                continue
            out.append(Measurement(
                t=now, channel=ch.name,
                value=apply(raw, ch.offset, ch.multiplier),
                unit=ch.unit, raw=raw, quality=Quality.OK, src=src))
        return out

    def _read_simulated(self) -> list[Measurement]:
        # A dewar slowly emptying, with the jitter of boiling liquid on a
        # platform scale. Tagged src="sim" so nobody reads physics out of it.
        elapsed = time.monotonic() - self._t0
        gross = (280.0 - elapsed / 3600.0 * 2.0
                 + 0.1 * math.sin(elapsed / 30.0) + random.gauss(0, 0.03))
        weight = {"status": "ST", "gross": round(gross / 0.05) * 0.05,
                  "net": gross, "tare": 0.0}
        return self._measurements(utcnow(), weight, True, src="sim")
