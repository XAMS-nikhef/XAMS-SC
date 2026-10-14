"""CAEN DT1470ET high-voltage supplies. See DESIGN.md §7.2.

Two units, both on USB, ASCII protocol over the virtual COM port. No vendor
library.

**This driver writes exactly four things: `VSET`, `ON`, `OFF` and `BDCLR`**
(§10a). Everything else is read. `BDCLR` clears a latched trip, and only after
the tripped channels' setpoints are zeroed - see `_handle_clear`. A trip the
driver detects itself is answered with `VSET` 0 and `OFF` at once, with nobody
behind the command - see `_watch_trips`. There is
deliberately no code here that can change `MAXV`, `RUP`, `RDW`, `TRIP` or
`ISET` - protection stays configured on the instrument (§10 rule 2).

**`ON` is not the enable switch, and the difference matters.** Three separate
things decide whether there are volts on an electrode:

  * the **enable switch** on the front panel clears the `DISABLED` bit. It is
    a hand operation and no command here can change it.
  * **`ON`/`OFF`** energises a channel that is already enabled. That is the
    "turn on the HV" step, and it is this driver's business.
  * **`VSET`** decides where it ramps to.

Clearing `DISABLED` alone does nothing visible, which is exactly what was
observed on 18 September 2026 when the `nai` enable was flipped and the
channel sat at `STAT=0`: permitted, but not energised.

A write is validated against `channels.yaml`, refused outright if it would put
a non-zero setpoint on a channel that is not enabled (§10a), **read back from
the board before it is called successful**, acknowledged on the bus and
recorded in the audit trail.

The same invariant is also ENFORCED, once per read cycle: a channel whose
enable switch is off and whose `VSET` is not zero has it written to zero. That
state is not reachable through this driver - it arrives when somebody flips a
switch on a channel that was de-energised with its setpoint still loaded - and
until it is cleared, the next flip of that switch is an unannounced ramp. See
`_enforce_disabled_zero`.

Both units answer at **board address 0**: they are two independent USB
connections, not a daisy chain. They are told apart by which port their
`BDSNUM` came back on, never by address and never by COM number (§6.2).

SIGN CONVENTION. The supplies report `VMON` and `VSET` as **unsigned
magnitudes**, with polarity as a separate `POL` parameter. A cathode at minus
2250 volts answers "2250.0". The sign is applied here, once, and values are
stored signed (§7.2). This is almost certainly what the LabVIEW
`DAISY_polarity_signs.vi` exists to do.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import threading
import time

import serial

from ..bus import (ACK_HV_CLEAR, ACK_HV_OUTPUT, ACK_HV_VSET, TOPIC_AUDIT,
                   TOPIC_HV_CLEAR, TOPIC_HV_OUTPUT, TOPIC_HV_SWITCHING,
                   TOPIC_HV_TRIP, TOPIC_HV_VSET)
from ..config import Config
from ..model import Measurement, Quality, iso, utcnow
from ..scaling import apply_sign, magnitude_for
from ..service import BaseService
from .serial_id import IdentityError, resolve

log = logging.getLogger(__name__)

# The status-word decoding lives in `hv_status` so the web UI can use it
# without importing a serial driver, and is re-exported here because this is
# where anything working on the CAEN looks for it. Declared rather than left
# to look accidental: without this they read as six unused imports, and the
# next person to tidy up would delete them and break test_caen_protocol.
__all__ = [
    "CaenChannelReader", "CaenService",
    "BIT_DISABLED", "BIT_ON", "BIT_TRIP", "FAULT_BITS", "STAT_BITS",
    "describe_status", "status_faults",
]

# The status word and its decoding live in hv_status.py: they are pure logic
# and the web UI needs them, and this module imports `serial`. Re-exported
# here because this is where anyone would look for them.
from ..hv_status import (  # noqa: F401
    ARMED_ABOVE_V, BIT_DISABLED, BIT_ON, BIT_TRIP, COLLAPSE_CONFIRM_READS,
    FAULT_BITS, FLAG_CONFIRM_READS, STAT_BITS, describe_status, is_disabled,
    is_energised, is_tripped, output_collapsed, output_healthy, status_faults)

# How long a channel switched off is shown as "switching off" at most, beyond
# its ramp-down time at the configured rate (`expect.rdw`, V/s): STAT bit 0
# stayed set some seconds after the ramp on 29 September 2026. Past it the
# page stops saying so and the log says the board did not follow.
SWITCH_OFF_MARGIN_S = 60.0
SWITCH_OFF_DEFAULT_S = 300.0      # no ramp rate known

# A setpoint that reads back further than this from what was asked means the
# write did not take. 0.5 V is far below anything that matters on a kilovolt
# electrode and well above the board's own rounding.
VSET_TOLERANCE_V = 0.5

# How long to leave a disabled channel alone after a failed attempt to zero
# its setpoint. The attempt fails for reasons a person has to fix - a board in
# LOCAL mode refuses every remote write until somebody walks to the front
# panel - and retrying three serial exchanges a second against a supply that
# is going to say no crowds out the readings that still work.
ZERO_RETRY_S = 30.0

# The actor recorded for a setpoint zeroed by the rule below. Not a person,
# and deliberately not shaped like one: this is the one VSET write in the
# system with nobody behind it, and the audit trail has to say so.
AUTOMATIC = "automatic (section 10a)"

# The actor for the writes that make a tripped channel safe. Its own name, so
# the audit trail tells "the switch was off" from "the output collapsed".
AUTOMATIC_TRIP = "automatic (trip)"

# How many reads of each channel are kept, so a trip can be reported with
# what led up to it - the current spike that preceded the collapse. At 1 Hz
# this is the last minute.
TRIP_HISTORY_READS = 60


def _decode(response: str) -> str | None:
    """Extract VAL from '#BD:00,CMD:OK,VAL:19198'.

    Returns None for anything malformed, absent or reporting an error. A
    malformed reply is a read failure, never a silently substituted value
    (§7.2).
    """
    if not response:
        return None
    response = response.strip()
    if not response.startswith("#BD:") or "CMD:OK" not in response:
        return None
    _, _, tail = response.partition("VAL:")
    return tail.strip() or None


class CaenChannelReader:
    """One supply: a port, a board address, and the monitor commands."""

    def __init__(self, device_id: str, port: str, address: int, baud: int,
                 board_name: str, board_serial: str):
        self.device_id = device_id
        self.port = port
        self.address = address
        self.baud = baud
        self.board_name = board_name
        self.board_serial = board_serial
        self._serial: serial.Serial | None = None
        # ONE CONVERSATION AT A TIME, for the same reason as the Lake Shore.
        # A serial instrument matches a reply to a query only by arrival
        # order, so the 1 Hz poll and a command arriving on the MQTT thread
        # hand each other their answers - and the answers are plausible
        # numbers, so nothing looks wrong. On the Lake Shore, on 18 September
        # 2026, a setpoint read back as the heater percentage. Reentrant, so a
        # command can hold it across write-then-read-back.
        self._lock = threading.RLock()
        # Evidence that the reply stream has slipped out of step. See _talk.
        self._stale_events = 0
        self._stale_last_log = 0.0

    # ----------------------------------------------------------------- serial

    def open(self) -> None:
        self._serial = serial.Serial(self.port, self.baud, bytesize=8,
                                     parity=serial.PARITY_NONE, stopbits=1,
                                     timeout=1.5, write_timeout=1.5)

    @contextlib.contextmanager
    def transaction(self):
        """Hold the port for a sequence that must not be interleaved.

        A serial instrument matches a reply to a query only by arrival order,
        so a read-old / write / read-back has to be one conversation or the
        1 Hz poll lands in the middle and hands its reply to the command.
        The lock is reentrant, so the calls inside still take it themselves.

        Public because the service needs it: it used to reach in and take
        `reader._lock` directly, which worked and would have kept working
        right up until somebody changed how this class locks.
        """
        with self._lock:
            yield self

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def _talk(self, cmd: str) -> str | None:
        """Send one command, return the RAW reply, or None if it did not answer.

        Shared by MON and SET: the framing, the lock and the retry are
        identical, and two copies of them would drift.

        **A reply carries no echo of PAR or CH.** `#BD:00,CMD:OK,VAL:135.0`
        could be the answer to any query on any channel, so question and
        answer are matched by arrival order and nothing else. That holds as
        long as every command gets its reply before the next one is sent. It
        stops holding the moment one answer arrives late: the flush below
        cannot discard a reply still on the wire, so it lands in the next
        read, and from then on every value is attributed to the wrong
        channel - plausibly, and silently. That is why `stale` is measured
        and reported rather than quietly thrown away.
        """
        if self._serial is None:
            return None
        with self._lock:
            # Bytes already waiting BEFORE we write are a reply nobody read.
            # On a healthy link this is always zero.
            #
            # In its OWN try, outside the one below, and deliberately so: this
            # is a diagnostic, and a diagnostic that can turn a working read
            # into "the supply did not answer" is worse than no diagnostic.
            # `in_waiting` is a pyserial property that talks to the driver and
            # can raise on a port that is going away - which is exactly when
            # the reading underneath it still matters.
            try:
                stale = self._serial.in_waiting
            except Exception:
                stale = 0
            try:
                started = time.monotonic()
                self._serial.reset_input_buffer()
                self._serial.write((cmd + "\r\n").encode("ascii"))
                time.sleep(0.05)
                raw = self._serial.read_until(b"\r\n", 200).decode(
                    "ascii", errors="replace")
                retried = False
                if not raw.strip():
                    # Some firmware answers slowly; one short retry.
                    retried = True
                    time.sleep(0.15)
                    raw = self._serial.read(200).decode("ascii", errors="replace")
            except Exception as exc:
                log.debug("%s: %s raised %s", self.device_id, cmd, exc)
                return None

        if stale:
            self._note_stale(stale, cmd)
        # Every exchange, with its timing. Off unless the service is started
        # with --log-level DEBUG, and the formatting is not done until then.
        log.debug("%s: %s -> %r [%.0f ms%s%s]", self.device_id, cmd,
                  raw.strip(), (time.monotonic() - started) * 1000.0,
                  ", retried" if retried else "",
                  ", %d stale bytes discarded" % stale if stale else "")
        return raw

    def _note_stale(self, count: int, cmd: str) -> None:
        """Report discarded bytes, loudly the first time and then rarely.

        WARNING rather than DEBUG because this is not housekeeping: bytes in
        the buffer before a command is sent mean an earlier reply was never
        collected, and with no PAR or CH to match on, every subsequent value
        on this supply may belong to a different channel than the one it is
        filed under. A reading that is wrong looks exactly like a reading
        that is right.

        Rate-limited because a link that has slipped does it many times a
        second, and a warning repeated thirty times a second is a warning
        nobody reads.
        """
        self._stale_events += 1
        now = time.monotonic()
        if now - self._stale_last_log < 60.0:
            return
        self._stale_last_log = now
        log.warning(
            "%s: %d unread byte(s) in the buffer before %s - an earlier reply "
            "was never collected. Replies carry no channel or parameter, so "
            "readings on this supply may be attributed to the wrong channel. "
            "%d occurrence(s) so far.",
            self.device_id, count, cmd.split(",PAR:")[-1], self._stale_events)

    def _command(self, par: str, channel: int | None = None) -> str | None:
        """Read one parameter with CMD:MON, returning its VAL."""
        cmd = f"$BD:{self.address},CMD:MON,PAR:{par}"
        if channel is not None:
            cmd += f",CH:{channel}"
        raw = self._talk(cmd)
        value = _decode(raw) if raw is not None else None
        if value is None:
            # The raw instrument response, not just the parsed failure - that
            # is what makes protocol bugs findable (section 12).
            log.debug("%s: %s -> unparseable %r", self.device_id, cmd, raw)
        return value

    def set_voltage(self, channel: int, magnitude: float) -> bool:
        """Write VSET for one channel. THE ONLY WRITE IN THIS DRIVER.

        `magnitude` is unsigned, as the supply expects. The caller converts
        from the signed value with `scaling.magnitude_for`, which refuses the
        wrong polarity rather than silently taking its absolute value.

        Returns whether the board ACKNOWLEDGED the command - not whether it is
        now holding that value. Only a read-back establishes that, and every
        caller here does one. A value above the board's own MAXV is refused by
        the instrument, which is the protection working as intended.

        Note the reply shape: a successful SET answers `#BD:00,CMD:OK` with no
        `VAL:` field, so `_decode` returns None for it. Success is therefore
        the presence of CMD:OK, not the presence of a value - which is why
        this does not go through `_command`.
        """
        if magnitude < 0:
            raise ValueError("set_voltage takes an unsigned magnitude")
        raw = self._talk(f"$BD:{self.address},CMD:SET,PAR:VSET,"
                         f"CH:{channel},VAL:{magnitude:.1f}")
        if raw is None:
            log.warning("%s ch%d: no reply to SET VSET", self.device_id, channel)
            return False, "the supply did not answer"

        reply = raw.strip()
        if reply.startswith("#BD:") and "CMD:OK" in reply:
            return True, ""

        log.warning("%s ch%d: the board refused SET VSET: %r",
                    self.device_id, channel, reply)

        # The board says WHY, and the reasons are not interchangeable.
        # LOC:ERR means it is in LOCAL mode: the front panel has control and
        # every remote SET is refused, while MON keeps working perfectly -
        # which is why every reading looked fine and only writing failed.
        if "LOC:ERR" in reply:
            return False, ("the supply is in LOCAL mode, so it refuses every "
                           "remote setpoint. Put the board in REMOTE at its "
                           "front panel; nothing in this software can do it, "
                           "by design")
        if "CMD:ERR" in reply:
            return False, "the supply rejected the command itself (%s)" % reply
        if "VAL:ERR" in reply:
            return False, ("the supply rejected the value, most likely above "
                           "its own MAXV (%s)" % reply)
        return False, "the supply refused it: %s" % reply

    # --------------------------------------------------------------- identity

    def identity(self) -> tuple[str, str] | None:
        """(BDNAME, BDSNUM), or None if the board did not answer properly."""
        name = self._command("BDNAME")
        serial_num = self._command("BDSNUM")
        if name is None or serial_num is None:
            return None
        return (name, serial_num)

    # ------------------------------------------------------------------ reads

    def monitor(self, channel: int, par: str) -> float | None:
        value = self._command(par, channel)
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            log.debug("%s ch%d %s: non-numeric %r", self.device_id, channel, par, value)
            return None

    def set_output(self, channel: int, on: bool) -> tuple[bool, str]:
        """Energise or de-energise one channel. NOT the enable switch.

        A channel whose `DISABLED` bit is set cannot be energised by this -
        the enable is a hand operation, and the board refuses. The caller
        checks first so the refusal names the remedy rather than the symptom.
        """
        par = "ON" if on else "OFF"
        raw = self._talk(f"$BD:{self.address},CMD:SET,PAR:{par},CH:{channel}")
        if raw is None:
            return False, "the supply did not answer"
        reply = raw.strip()
        if reply.startswith("#BD:") and "CMD:OK" in reply:
            return True, ""
        log.warning("%s ch%d: the board refused %s: %r",
                    self.device_id, channel, par, reply)
        if "LOC:ERR" in reply:
            return False, ("the supply is in LOCAL mode, so it refuses remote "
                           "commands. Switch the board to REMOTE at its front "
                           "panel; nothing here can do it, by design")
        return False, "the supply refused it: %s" % reply

    def clear_alarm(self) -> tuple[bool, str]:
        """Clear the board's latched alarm: `BDCLR`, the recovery from a trip.

        A trip switches the channel OFF and LATCHES - `STAT` bit 7 and the
        board alarm stay set, and neither ON nor the enable switch clears
        them. Before this existed the only way back was a power cycle
        (23 September 2026).

        **Board-wide.** There is no per-channel form of `BDCLR`, so it clears
        the alarm on every channel of this supply at once. The caller zeroes
        the setpoint of every latched channel first (§10a *Recovering from a
        trip*); this method only sends the command.

        It changes no limit. `MAXV`, `ISET`, `TRIP`, `RUP`, `RDW` stay what
        the front panel set (§10 rule 2).
        """
        raw = self._talk(f"$BD:{self.address},CMD:SET,PAR:BDCLR")
        if raw is None:
            return False, "the supply did not answer"
        reply = raw.strip()
        if reply.startswith("#BD:") and "CMD:OK" in reply:
            return True, ""
        log.warning("%s: the board refused BDCLR: %r", self.device_id, reply)
        if "LOC:ERR" in reply:
            return False, ("the supply is in LOCAL mode, so it refuses remote "
                           "commands. Switch the board to REMOTE at its front "
                           "panel; nothing here can do it, by design")
        return False, "the supply refused it: %s" % reply

    def alarm_word(self) -> int | None:
        """`BDALARM`: one bit per channel in alarm, bit n for channel n."""
        value = self._command("BDALARM")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def control_mode(self) -> str:
        """LOCAL or REMOTE, from the board's own BDCTR.

        In LOCAL the front panel has control and every remote SET is refused
        with LOC:ERR, while MON keeps answering normally. That asymmetry is
        why a board in LOCAL is indistinguishable from a working one until
        somebody tries to write.
        """
        return (self._command("BDCTR") or "unknown").strip().upper()

    def status(self, channel: int) -> int | None:
        value = self._command("STAT", channel)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None


class CaenService(BaseService):
    """Reads both supplies. One service, two instruments — they share a
    protocol and a resolution pass, and splitting them would mean two
    processes racing to enumerate the same ports."""

    name = "caen"

    def __init__(self, config: Config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._readers: dict[str, CaenChannelReader] = {}
        self._specs = config.devices.get("caen", []) or []
        # phys is the 0-based channel index on its board.
        self._channels = [c for c in config.enabled_channels()
                          if c.device in {s["id"] for s in self._specs}]
        # Consecutive read cycles in which a given supply answered nothing.
        # Tracked PER DEVICE: two supplies share this service, and one can
        # lose its USB while the other keeps answering perfectly.
        self._link_down: dict[str, int] = {}
        self._next_relink = 0.0
        # When a channel whose setpoint could not be zeroed may be tried
        # again, by hv_vset channel name. See `_enforce_disabled_zero`.
        self._zero_retry_at: dict[str, float] = {}
        # TRIPS, by hv_vset channel name. See `_watch_trips`.
        #   _tripped   the latched record, as published on TOPIC_HV_TRIP.
        #              Present until somebody clears it; while present the
        #              channel cannot be turned on.
        #   _cleared   channels cleared since their trip whose output has not
        #              yet been seen working. A second trip before it has is
        #              the evidence that the clear did not recover it.
        #   _history   the last minute of (time, vset, vmon, imon, word).
        self._tripped: dict[str, dict] = {}
        self._cleared: dict[str, dict] = {}
        self._history: dict[str, collections.deque] = {}
        self._flag_hits: dict[str, int] = {}
        self._collapse_hits: dict[str, int] = {}
        self._safe_retry_at: dict[str, float] = {}
        # When each supply was last relinked (time.time()). A power cycle
        # drops the USB link, so a relink after a trip is how a clear can
        # tell whether the supply was power-cycled in between.
        self._relinked_at: dict[str, float] = {}

        # SWITCHING (14 October 2026, docs/operating/webui.md "Control"):
        #   _commanded_at  when a command last published a channel's state out
        #                  of cadence (STAT or VSET). A poll sample taken
        #                  before that is older than what was published and is
        #                  dropped, so the next 10-s publish cannot put the
        #                  pre-command state back on the page.
        #   _stat_last     the STAT value last published, so a change is
        #                  published at once rather than at the next 10 s.
        #   _switching     channels turned off and not yet off, by hv_vset
        #                  name: {"wanted", "since", "_until"}.
        self._commanded_at: dict[str, object] = {}
        self._stat_last: dict[str, float] = {}
        self._switching: dict[str, dict] = {}
        self._vset_for_stat: dict[str, object] = {}
        for stat in (c for c in self._channels if c.kind == "hv_stat"):
            for ch in self._channels:
                if (ch.kind == "hv_vset" and ch.device == stat.device
                        and int(ch.phys) == int(stat.phys)):
                    self._vset_for_stat[stat.name] = ch

    def owned_channels(self):
        """Both supplies' channels, by device id rather than service name.

        THE ONE SERVICE WHERE THE TWO DIFFER (§7.2). It is called `caen` and
        reads `hv_1` and `hv_2`, so the inherited lookup found nothing and a
        link that died marked none of these 32 channels as unreadable.
        """
        return list(self._channels)

    # --------------------------------------------------------------- identity

    def _probe(self, port: str) -> tuple[str, ...] | None:
        """Open a port, ask whoever is there who they are, and let go."""
        address = int(self._specs[0].get("board_address", 0))
        baud = int(self._specs[0].get("baud", 9600))
        reader = CaenChannelReader("probe", port, address, baud, "", "")
        try:
            reader.open()
            return reader.identity()
        except Exception as exc:
            log.warning("could not probe %s: %s", port, exc)
            return None
        finally:
            reader.close()

    def verify_identity(self) -> bool:
        if self.simulate:
            log.info("simulate=True: skipping hardware identity check")
            return True

        by_vidpid: dict[tuple[str, str], dict] = {}
        for spec in self._specs:
            key = (spec["match"]["vid"], spec["match"]["pid"])
            by_vidpid.setdefault(key, {})[spec["id"]] = (
                spec["board_name"], str(spec["board_serial"]))

        try:
            for (vid, pid), expected in by_vidpid.items():
                mapping = resolve(vid, pid, self._probe, expected)

                for spec in self._specs:
                    if spec["id"] not in mapping:
                        continue
                    self._readers[spec["id"]] = CaenChannelReader(
                        device_id=spec["id"], port=mapping[spec["id"]],
                        address=int(spec.get("board_address", 0)),
                        baud=int(spec.get("baud", 9600)),
                        board_name=spec["board_name"],
                        board_serial=str(spec["board_serial"]))
        except IdentityError as exc:
            log.critical("FATAL: %s", exc)
            return False

        for device_id, reader in self._readers.items():
            try:
                reader.open()
            except Exception as exc:
                log.critical("FATAL: could not open %s on %s: %s",
                             device_id, reader.port, exc)
                return False
            log.info("%s: %s serial %s on %s, verified",
                     device_id, reader.board_name, reader.board_serial, reader.port)
            # LOCAL or REMOTE. Logged at startup because it decides whether
            # any setpoint can be written at all, and a board quietly in
            # LOCAL looks identical to a working one until the first write.
            mode = reader.control_mode()
            log.info("%s: control mode %s%s", device_id, mode,
                     "" if mode == "REMOTE" else
                     " - setpoints cannot be written until the front panel "
                     "is switched to REMOTE")
            self._check_expectations(device_id, reader)

        return True

    def _check_expectations(self, device_id: str, reader: CaenChannelReader) -> None:
        """Compare the board's protection settings against devices.yaml.

        The software DISPLAYS and ALARMS on these; it never writes them (§8.3).
        A mismatch means somebody changed a limit on the front panel, which is
        worth knowing about and is not this system's business to correct.
        """
        spec = next((s for s in self._specs if s["id"] == device_id), None)
        expect = (spec or {}).get("expect") or {}
        for channel, wanted in expect.items():
            channel = int(channel)
            for par, want in wanted.items():
                if par == "pol":
                    got = reader._command("POL", channel)
                    ok = got is not None and got.strip().startswith(str(want))
                else:
                    got = reader.monitor(channel, par.upper())
                    ok = got is not None and abs(got - float(want)) <= 0.51
                if not ok:
                    log.warning(
                        "%s ch%d: %s is %s, devices.yaml expects %s. The "
                        "software does not change instrument limits (§8.3) — "
                        "check the front panel.",
                        device_id, channel, par.upper(), got, want)

    # ------------------------------------------------------------------- read

    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()

        now = utcnow()
        out: list[Measurement] = []
        # False until a channel on that supply answers. A supply whose reader
        # is missing entirely - a relink that has not found it yet - stays
        # False, and its channels report error rather than going quietly
        # stale.
        alive = {c.device: False for c in self._channels}
        # What the switch and the setpoint read this cycle, by (device, phys),
        # so the invariant below can pair them up. Collected here rather than
        # read again afterwards: the pair has to come from one sweep, or the
        # rule acts on a switch position and a setpoint that were never true
        # at the same moment.
        words: dict[tuple[str, int], int] = {}
        setpoints: dict[tuple[str, int], tuple] = {}
        # Signed volts and microamps from the same sweep, for the trip watch.
        vmons: dict[tuple[str, int], float] = {}
        imons: dict[tuple[str, int], float] = {}

        for ch in self._channels:
            reader = self._readers.get(ch.device)
            if reader is None:
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=Quality.ERROR))
                continue
            index = int(ch.phys)

            if ch.kind == "hv_stat":
                # The status word is a bitmask, not a measurement: stored as
                # it comes off the wire so every flag survives, and decoded
                # where it is displayed.
                word = reader.status(index)
                if word is None:
                    out.append(Measurement(t=now, channel=ch.name, value=None,
                                           unit=ch.unit, quality=Quality.ERROR))
                    continue
                alive[ch.device] = True
                words[(ch.device, index)] = word
                out.append(Measurement(t=now, channel=ch.name,
                                       value=float(word), unit=ch.unit,
                                       raw=float(word), quality=Quality.OK))
                continue

            # VSET is a set parameter, but it is READ here with the ordinary
            # monitor command - CMD:MON,PAR:VSET - which is what makes stage 1
            # of section 10a a read-only change. There is still no SET path in
            # this driver.
            par = {"hv_vmon": "VMON", "hv_vset": "VSET"}.get(ch.kind, "IMON")
            magnitude = reader.monitor(index, par)

            if magnitude is None:
                # The device is there but did not answer intelligibly. Publish
                # the absence, never a substituted value.
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=Quality.ERROR))
                continue

            alive[ch.device] = True
            # Both VMON and VSET come back as unsigned magnitudes with POL
            # separate, and both are stored signed (section 7.2). IMON is a
            # current and has no polarity to apply.
            value = (apply_sign(magnitude, ch.sign)
                     if ch.kind in ("hv_vmon", "hv_vset") else magnitude)
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=magnitude, quality=Quality.OK))
            if ch.kind == "hv_vmon":
                vmons[(ch.device, index)] = value
            elif ch.kind == "hv_imon":
                imons[(ch.device, index)] = value
            if ch.kind == "hv_vset":
                # The position, so a setpoint this cycle zeroes can be
                # replaced with what the board now holds rather than published
                # as the value it held a moment ago.
                setpoints[(ch.device, index)] = (ch, value, len(out) - 1)

        if out and not any(alive.values()):
            # Nothing anywhere answered: the link is gone, not the readings.
            # Raise so BaseService backs off and retries (§6.1).
            raise RuntimeError("no CAEN channel answered; link lost")

        # ONE supply can lose its USB while the other keeps answering, and it
        # must still be recovered. This used to require EVERY channel in the
        # service to fail, so unplugging a single unit left it publishing
        # errors forever with its healthy neighbour holding the service up -
        # and nothing in the log, because a failed command is logged at debug.
        # Liveness is tracked per device because the link is per device.
        for device_id, ok in alive.items():
            if ok:
                if self._link_down.get(device_id):
                    log.info("%s: answering again", device_id)
                self._link_down[device_id] = 0
                continue
            self._link_down[device_id] = self._link_down.get(device_id, 0) + 1
            if self._link_down[device_id] == 2:
                log.warning("%s stopped answering; its channels now read "
                            "error and a relink will be attempted", device_id)

        if any(n >= 2 for n in self._link_down.values()):
            self._relink()

        # One BDALARM per answering supply per cycle: the trip flag the
        # manual describes, read alongside the one the board actually shows.
        alarms: dict[str, int | None] = {}
        for device_id, ok in alive.items():
            reader = self._readers.get(device_id)
            if ok and reader is not None:
                alarms[device_id] = reader.alarm_word()

        self._watch_trips(now, out, words, setpoints, vmons, imons, alarms)
        self._enforce_disabled_zero(now, out, words, setpoints)
        return out

    def _read_simulated(self) -> list[Measurement]:
        import random
        now = utcnow()
        out = []
        for ch in self._channels:
            if ch.kind == "hv_stat":
                # 1 = ON. A simulated supply is on, so the page shows the
                # interesting case rather than a wall of DISABLED.
                out.append(Measurement(t=now, channel=ch.name, value=1.0,
                                       unit=ch.unit, raw=1.0,
                                       quality=Quality.OK, src="sim"))
                continue
            if ch.kind in ("hv_vmon", "hv_vset") and ch.limits:
                span = ch.limits["max"] if ch.sign > 0 else ch.limits["min"]
                magnitude = abs(span) * 0.8 + random.gauss(0, 1.0)
                value = apply_sign(magnitude, ch.sign)
            else:
                magnitude = value = abs(random.gauss(0.5, 0.05))
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=magnitude,
                                   quality=Quality.OK, src="sim"))
        return out

    # ------------------------------------------------------------- control
    #
    # THE ONLY WRITE PATH IN THIS DRIVER (section 10a). It writes VSET and
    # nothing else. There is no command here that enables or disables a
    # channel: that is a hand operation at the supply, and a hardware gate
    # the software cannot reach is the last thing standing between a bug and
    # an electrode.

    def _audit(self, target, old, new, result, detail="", actor="",
               action="caen_vset"):
        self.bus.publish_raw(TOPIC_AUDIT, json.dumps(
            {"t": iso(utcnow()), "actor": actor or "unknown",
             "action": action, "target": target,
             "old": None if old is None else str(old),
             "new": None if new is None else str(new),
             "result": result, "detail": detail}, separators=(",", ":")))

    def _refuse(self, target, reason, actor, old=None, new=None,
                ack_topic=None, action="caen_vset"):
        """Say no, out loud, and record it.

        A refused command is acknowledged with a reason and audited, never
        dropped (section 10). What somebody TRIED to put on an electrode is
        worth as much afterwards as what they managed to.
        """
        log.warning("refused %s on %s by %s: %s", action, target,
                    actor or "unknown", reason)
        self.bus.publish_raw(ack_topic or ACK_HV_VSET, json.dumps(
            {"ok": False, "channel": target, "reason": reason, "by": actor},
            separators=(",", ":")))
        self._audit(target, old, new, "rejected", reason, actor, action=action)

    def _handle_vset(self, topic: str, payload: str) -> None:
        try:
            command = json.loads(payload) or {}
        except ValueError:
            self._refuse(None, "unparseable command", "")
            return

        actor = str(command.get("by") or "").strip() or "unknown"
        name = str(command.get("channel") or "").strip()

        channel = self.config.channels.get(name)
        if channel is None or channel.kind != "hv_vset" or not channel.enabled:
            self._refuse(name or None,
                         "%r is not an enabled hv_vset channel" % name, actor)
            return

        try:
            wanted = float(command["value"])
        except (KeyError, TypeError, ValueError):
            self._refuse(name, "no usable 'value' in the command", actor)
            return

        if self.simulate:
            self._refuse(name, "this service is simulating and holds no "
                               "instrument to write to", actor, new=wanted)
            return

        reader = self._readers.get(channel.device)
        if reader is None:
            self._refuse(name, "not connected to %s" % channel.device, actor,
                         new=wanted)
            return

        # The software write range from channels.yaml. A channel with no
        # limits refuses everything, which is the intended default: a range
        # nobody wrote down is not permission to put volts on an electrode.
        if not channel.in_limits(wanted):
            limits = channel.limits or {}
            self._refuse(name, "%+.1f V is outside the permitted range %s to "
                               "%s set in channels.yaml"
                               % (wanted, limits.get("min", "unset"),
                                  limits.get("max", "unset")), actor,
                         new=wanted)
            return

        index = int(channel.phys)

        # THE SECTION 10a RULE. A channel that is not enabled must keep VSET 0,
        # because the enable is a hand operation and the board ramps to VSET
        # the instant it is flipped. Allowing a non-zero setpoint on a
        # disabled channel would let somebody stage 4.2 kV, walk to the
        # supply, flip the switch and get exactly the unannounced ramp this
        # whole design exists to prevent - with the software's blessing.
        #
        # Zero is always allowed. That is how the invariant gets established
        # on a channel whose stored setpoint is currently wrong.
        word = reader.status(index)
        if word is None:
            self._refuse(name, "could not read the channel's status word, so "
                               "whether it is enabled is unknown", actor,
                         new=wanted)
            return
        # is_disabled (bit 10, the front-panel switch), NOT is_energised
        # (bit 0, the output being energised). This first read bit 0 and so
        # refused a setpoint to any channel that was switched on but not yet
        # energised - which is precisely the state the operator is in between
        # flipping the switch and pressing turn-on, and precisely when they
        # want to load a setpoint.
        #
        # The invariant is about the SWITCH: a channel whose switch is off
        # keeps VSET 0, because flipping it would ramp straight there. Once
        # the switch is on, a setpoint may be loaded; energising is a separate,
        # explicit act that reports the voltage it will ramp to.
        if wanted != 0 and is_disabled(word):
            self._refuse(
                name,
                "this channel is disabled at the supply, so its setpoint must "
                "stay at 0 (section 10a). Flip its enable switch first - the "
                "channel stays at zero volts until it is energised - then set "
                "the voltage.", actor, new=wanted)
            return

        try:
            magnitude = magnitude_for(wanted, channel.sign)
        except ValueError as exc:
            self._refuse(name, str(exc), actor, new=wanted)
            return

        # Held across read-old / write / read-back, so the 1 Hz poll cannot
        # land in the middle and hand us its reply instead of ours.
        with reader.transaction():
            before_mag = reader.monitor(index, "VSET")
            sent, why = reader.set_voltage(index, magnitude)
            after_mag = reader.monitor(index, "VSET") if sent else None

        before = (apply_sign(before_mag, channel.sign)
                  if before_mag is not None else None)
        if not sent:
            self._refuse(name, why, actor, old=before, new=wanted)
            return

        if after_mag is None:
            self._refuse(name, "the setpoint could not be read back, so "
                               "whether the write took is unknown", actor,
                         old=before, new=wanted)
            return

        after = apply_sign(after_mag, channel.sign)
        if abs(after - wanted) > VSET_TOLERANCE_V:
            self._refuse(
                name, "read back %+.1f V after asking for %+.1f V; the write "
                      "did not take. A value above the board's own MAXV is "
                      "refused by the instrument, which is the protection "
                      "working." % (after, wanted), actor, old=before,
                new=wanted)
            return

        log.warning("VSET %s: %s -> %+.1f V, by %s", name,
                    ("%+.1f" % before) if before is not None else "unknown",
                    after, actor)
        # BEFORE the ack, as the energise path does: the web UI waits on the
        # ack and renders the moment it arrives, so a reading published after
        # it arrives after the page it was meant to correct.
        self._publish_vset_now(channel, after, after_mag)
        self.bus.publish_raw(ACK_HV_VSET, json.dumps(
            {"ok": True, "channel": name, "old": before, "new": after,
             "by": actor}, separators=(",", ":")))
        self._audit(name, before, after, "ok", "", actor)

    def _resolve(self, name, ack_topic, actor, action="caen_output"):
        """(channel, reader, index) for an hv_vset name, or None after refusing."""
        channel = self.config.channels.get(name)
        if channel is None or channel.kind != "hv_vset" or not channel.enabled:
            self._refuse(name or None,
                         "%r is not an enabled hv_vset channel" % name, actor,
                         ack_topic=ack_topic, action=action)
            return None
        if self.simulate:
            self._refuse(name, "this service is simulating and holds no "
                               "instrument to write to", actor,
                         ack_topic=ack_topic, action=action)
            return None
        reader = self._readers.get(channel.device)
        if reader is None:
            self._refuse(name, "not connected to %s" % channel.device, actor,
                         ack_topic=ack_topic, action=action)
            return None
        return channel, reader, int(channel.phys)

    def _stat_channel_for(self, channel):
        """The hv_stat channel watching the same physical output, or None.

        Matched on device AND phys rather than by rewriting the name: the
        naming convention is a convention, and config.py already guarantees
        that one (device, phys) pair is used once per kind (§6.1).
        """
        for ch in self._channels:
            if (ch.kind == "hv_stat" and ch.device == channel.device
                    and int(ch.phys) == int(channel.phys)):
                return ch
        return None

    def _publish_status_now(self, channel, word: int) -> None:
        """Publish a status word read back from a write, out of cadence.

        WHY THIS EXISTS. The poll reads every second and publishes every ten,
        so a channel energised just after a publish stayed "not energised" on
        the web page for most of the following ten seconds - the operator saw
        nothing happen, and clicked the button again. By then the row may
        have refreshed into "turn off", so the second click de-energised the
        channel the first had just started. The read-back is already in hand
        here and was already trusted enough to verify the write against; the
        only thing missing was saying so.

        The window is dropped for this channel deliberately. It holds samples
        from BOTH sides of the switch, and _emit_window means them - a mean of
        a bitmask is not a bitmask, and `int(0.4)` is 0, so that aggregate
        would have published the channel as off again a few seconds after this
        said it was on. Ten seconds of samples are lost at the instant of a
        command; the ack and the audit record the transition exactly, and a
        meaningless average is not worth keeping over that.
        """
        stat = self._stat_channel_for(channel)
        if stat is None:
            return
        self._commanded_at[stat.name] = utcnow()
        self._window.pop(stat.name, None)
        self._stat_last[stat.name] = float(word)
        self.bus.publish_measurement(
            Measurement(t=utcnow(), channel=stat.name, value=float(word),
                        unit=stat.unit, raw=float(word), quality=Quality.OK))

    def _publish_vset_now(self, channel, value: float,
                          magnitude: float) -> None:
        """Publish a setpoint read back from a write, out of cadence.

        The twin of `_publish_status_now`, and it exists for the same reason:
        the poll reads every second and publishes every ten, so a setpoint
        written just after a publish went on reading as the OLD voltage on
        the Control page for most of the following ten seconds. Energising
        was given this and changing a setpoint was not, which made the two
        controls on one page behave differently for no reason anybody could
        see.

        The window is dropped for this channel deliberately. It holds samples
        from both sides of the write, and `_emit_window` means them - so the
        page would have shown an average of the old setpoint and the new one.
        That is worse here than a wrong bitmask is: it is a plausible voltage
        that was never set, on the page somebody reads to find out where the
        supply is going. Ten seconds of samples are lost at the instant of a
        command; the ack and the audit record the transition exactly.

        The value is already trusted - it was read back inside the
        transaction and checked against what was asked (VSET_TOLERANCE_V), or
        this is not reached.
        """
        self._commanded_at[channel.name] = utcnow()
        self._window.pop(channel.name, None)
        self.bus.publish_measurement(
            Measurement(t=utcnow(), channel=channel.name, value=value,
                        unit=channel.unit, raw=magnitude, quality=Quality.OK))

    # ------------------------------------------------------------ switching

    def _accumulate(self, batch: list[Measurement]) -> None:
        """The poll's samples, with three things added for switching.

        1. A sample taken before a command published this channel's state is
           dropped. The poll runs on its own thread: a sweep that started
           before the command can finish after it, and its old STAT would be
           the last word in the window the command had just cleared.
        2. A STAT that changed is published at once, not at the next 10-s
           window. It is a bitmask, published as its last value anyway, so
           this adds rows only where something happened.
        3. A channel switched off stops being "switching" when its output is
           off, or when it has had its ramp time plus a margin.
        """
        kept = []
        for m in batch:
            since = self._commanded_at.get(m.channel)
            if since is not None and m.t < since:
                continue
            kept.append(m)
            if m.channel not in self._vset_for_stat:
                continue
            if m.quality == Quality.OK and m.value is not None:
                if m.value != self._stat_last.get(m.channel):
                    self._stat_last[m.channel] = m.value
                    self.bus.publish_measurement(m)
                self._check_switching(self._vset_for_stat[m.channel], int(m.value))
        super()._accumulate(kept)

    def _check_switching(self, vset_channel, word: int) -> None:
        record = self._switching.get(vset_channel.name)
        if record is None:
            return
        if not is_energised(word):
            self._set_switching(vset_channel.name, None)
        elif time.monotonic() > record["_until"]:
            log.warning("%s: turned off at %s but still reports ON (status %d) "
                        "after its ramp time and %.0f s; no longer shown as "
                        "switching", vset_channel.name, record["since"], word,
                        SWITCH_OFF_MARGIN_S)
            self._set_switching(vset_channel.name, None)

    def _set_switching(self, name: str, record: dict | None) -> None:
        """Remember (or forget) a channel on its way off, and tell the page."""
        if record is None:
            if self._switching.pop(name, None) is None:
                return
            payload = ""
        else:
            self._switching[name] = record
            payload = json.dumps({k: v for k, v in record.items()
                                  if not k.startswith("_")}, separators=(",", ":"))
        self.bus.publish_raw("%s/%s" % (TOPIC_HV_SWITCHING, name), payload, retain=True)

    def _off_wait_s(self, channel, volts: float | None) -> float:
        """Ramp-down time at the configured rate, plus the margin."""
        spec = next((s for s in self._specs if s["id"] == channel.device), {})
        rate = ((spec.get("expect") or {}).get(int(channel.phys)) or {}).get("rdw")
        if not rate or volts is None:
            return SWITCH_OFF_DEFAULT_S
        return abs(volts) / float(rate) + SWITCH_OFF_MARGIN_S

    # ------------------------------------------- the invariant, enforced
    #
    # Section 10a states that a channel which is not enabled has VSET 0. The
    # write path refuses to BREAK it (see `_handle_vset`), which is not the
    # same as keeping it: the enable switch is a hand operation nothing here
    # can see coming, so the state arrives from outside the software.
    #
    # It arrives by the ordinary route. A channel is energised at its working
    # voltage, de-energised - which ramps down but leaves VSET where it was -
    # and then disabled at the front panel hours later by somebody who has
    # every reason to think the channel is off, because it is. The board now
    # holds a disabled channel with 2250 V in its setpoint, and the next
    # flip of that switch is an unannounced ramp.
    #
    # Refusal alone also left no way out: with the switch off the web UI
    # disables the setpoint box, so the one value that WOULD be accepted -
    # zero - could not be sent from the page that was warning about it.
    #
    # So the rule is enforced on every poll rather than only at the moment of
    # a command. It keys on bit 10, THE SWITCH, and nothing else.

    def _enforce_disabled_zero(self, now, out: list[Measurement],
                               words: dict, setpoints: dict) -> None:
        """Zero the setpoint of every channel whose enable switch is off.

        Runs once per read cycle over what that cycle already read, so a
        channel in the ordinary state - disabled and at zero, or enabled -
        costs nothing at all: no extra serial traffic, no extra commands.

        **On bit 10 and never bit 0.** A channel that is switched on but not
        energised is exactly where an operator stands between flipping the
        enable and pressing turn-on, and it is when they load a setpoint.
        Zeroing there would erase what they typed a second after they typed
        it, every second, and the fifth bug of that shape would be this one.
        """
        for key, (channel, value, position) in setpoints.items():
            word = words.get(key)
            if word is None or not is_disabled(word):
                continue
            if abs(value) <= ARMED_ABOVE_V:
                continue
            if time.monotonic() < self._zero_retry_at.get(channel.name, 0.0):
                continue
            fresh = self._zero_setpoint(channel, value)
            if fresh is None:
                self._zero_retry_at[channel.name] = (time.monotonic()
                                                     + ZERO_RETRY_S)
                continue
            self._zero_retry_at.pop(channel.name, None)
            # What the board holds NOW, in place of what it held before the
            # write. Without this the old value goes into the ten-second
            # window after the fact and is published as an average of a
            # setpoint that no longer exists and the one that replaced it.
            after, magnitude = fresh
            out[position] = Measurement(
                t=now, channel=channel.name, value=after, unit=channel.unit,
                raw=magnitude, quality=Quality.OK)

    def _zero_setpoint(self, channel, before: float):
        """Write VSET 0 to a disabled channel. (volts, magnitude), or None.

        Audited like any other write, with `AUTOMATIC` as the actor: this is
        the only setpoint in the system with nobody behind it, and an entry in
        the trail that cannot be accounted for afterwards is worse than the
        state it was fixing.

        **It publishes no acknowledgement**, unlike every other path here. The
        ack topics are matched by channel name (`state.command`), so an ack
        from a write nobody asked for can be handed to an operator's command
        as the answer to the question they actually asked.
        The audit record and the log carry it instead.
        """
        reader = self._readers.get(channel.device)
        if reader is None:
            return None
        index = int(channel.phys)

        with reader.transaction():
            # Read the switch again INSIDE the transaction. Up to a second has
            # passed since the poll read it, and the thing being guarded
            # against is somebody at the front panel: if they have flipped it
            # back on in that second the channel may legitimately hold a
            # setpoint, and zeroing it would be this rule causing the surprise
            # it exists to prevent.
            word = reader.status(index)
            if word is None or not is_disabled(word):
                return None
            sent, why = reader.set_voltage(index, 0.0)
            after_mag = reader.monitor(index, "VSET") if sent else None

        if not sent:
            log.warning(
                "%s: disabled with VSET %+.1f V, and it could not be zeroed: "
                "%s. Until it is, flipping this channel's enable switch ramps "
                "straight to that voltage (section 10a). Retrying in %.0f s.",
                channel.name, before, why, ZERO_RETRY_S)
            self._audit(channel.name, before, 0.0, "rejected", why, AUTOMATIC)
            return None

        if after_mag is None or abs(after_mag) > VSET_TOLERANCE_V:
            detail = ("read back %s after writing 0"
                      % ("nothing" if after_mag is None
                         else "%+.1f V" % apply_sign(after_mag, channel.sign)))
            log.warning(
                "%s: disabled with VSET %+.1f V, and the zeroing did not "
                "take - %s. Flipping this channel's enable switch ramps "
                "straight to that voltage (section 10a). Retrying in %.0f s.",
                channel.name, before, detail, ZERO_RETRY_S)
            self._audit(channel.name, before, 0.0, "rejected", detail,
                        AUTOMATIC)
            return None

        after = apply_sign(after_mag, channel.sign)
        # WARNING, not INFO. Nobody asked for this write, and the setpoint an
        # operator left behind is gone - they will come back to a channel that
        # no longer remembers where it was running. `load defaults` on /hv
        # offers it again from channels.yaml or hv_defaults.yaml, which is
        # where the operating point is meant to be recorded anyway.
        log.warning(
            "%s: the enable switch is off and VSET was %+.1f V, so it has "
            "been zeroed (section 10a). The enable is now safe to flip; "
            "`load defaults` on /hv offers the working voltage again.",
            channel.name, before)
        self._publish_vset_now(channel, after, after_mag)
        self._audit(channel.name, before, after, "ok",
                    "the channel is disabled at the supply, so its setpoint "
                    "must be zero (section 10a)", AUTOMATIC)
        return (after, after_mag)

    # ------------------------------------------------------------ trips
    #
    # A trip is DETECTED here, made safe here, and latched until a person
    # clears it (`_handle_clear`). It raises no alarm: an HV trip happens with
    # people in the lab and is shown on /hv, logged and audited instead.
    #
    # Two ways in, because the board does not do what its manual says:
    #
    #   * the manual's: STAT bit 7 or the channel's BDALARM bit, seen on
    #     FLAG_CONFIRM_READS reads in a row;
    #   * the one observed on 23 September 2026: the output collapses under a
    #     channel that still reports ON + UNDER_VOLTAGE (hv_status,
    #     `output_collapsed`), on COLLAPSE_CONFIRM_READS reads in a row.

    def _watch_trips(self, now, out: list[Measurement], words: dict,
                     setpoints: dict, vmons: dict, imons: dict,
                     alarms: dict) -> None:
        """Look for trips in what this read cycle already read.

        Costs no serial traffic on a healthy supply: everything it looks at
        was read anyway, except one BDALARM per supply, read by the caller.
        """
        for key, (channel, vset, position) in setpoints.items():
            word = words.get(key)
            if word is None:
                continue
            name, index = channel.name, key[1]
            vmon, imon = vmons.get(key), imons.get(key)
            history = self._history.setdefault(
                name, collections.deque(maxlen=TRIP_HISTORY_READS))
            history.append((time.time(), vset, vmon, imon, word))

            alarm = alarms.get(channel.device)
            flagged = is_tripped(word) or (
                alarm is not None and bool(alarm & (1 << index)))
            collapsed = output_collapsed(word, vset, vmon)
            self._flag_hits[name] = (self._flag_hits.get(name, 0) + 1
                                     if flagged else 0)
            self._collapse_hits[name] = (self._collapse_hits.get(name, 0) + 1
                                         if collapsed else 0)

            if name in self._cleared and output_healthy(word, vset, vmon):
                self._note_recovered(channel, vset)

            if name in self._tripped:
                # Already latched. If making it safe failed - a board in
                # LOCAL refuses every write - keep trying, at the same pace
                # as the disabled-channel rule.
                record = self._tripped[name]
                if (not record.get("made_safe")
                        and time.monotonic() >= self._safe_retry_at.get(name, 0.0)):
                    fresh = self._make_safe(channel, record)
                    if fresh is not None:
                        out[position] = Measurement(
                            t=now, channel=name, value=fresh[0],
                            unit=channel.unit, raw=fresh[1], quality=Quality.OK)
                continue

            if self._flag_hits[name] >= FLAG_CONFIRM_READS:
                cause = ("the supply flagged a trip (%s%s)" % (
                    describe_status(word),
                    ", BDALARM set" if alarm and alarm & (1 << index) else ""))
            elif self._collapse_hits[name] >= COLLAPSE_CONFIRM_READS:
                cause = ("the output collapsed: VMON %+.1f V against a setpoint "
                         "of %+.1f V, with the channel still reporting %s"
                         % (vmon, vset, describe_status(word)))
            else:
                continue

            record = self._latch_trip(channel, cause, word, vset, vmon, history)
            fresh = self._make_safe(channel, record)
            if fresh is not None:
                out[position] = Measurement(
                    t=now, channel=name, value=fresh[0], unit=channel.unit,
                    raw=fresh[1], quality=Quality.OK)

    def _latch_trip(self, channel, cause: str, word: int, vset: float,
                    vmon, history) -> dict:
        """Build the record of a trip, latch it, and say so."""
        name = channel.name
        # What led up to it: the worst current in the last minute, and the
        # voltage it was reached at. The spike is gone by the time the
        # collapse is confirmed, and it is the most useful number here.
        peak = max((h for h in history if h[3] is not None),
                   key=lambda h: h[3], default=None)
        highest = max((abs(h[2]) for h in history if h[2] is not None),
                      default=None)
        cleared = self._cleared.pop(name, None)
        record = {
            "channel": name,
            "device": channel.device,
            "t": iso(utcnow()),
            "cause": cause,
            "status": describe_status(word),
            "vset": vset,
            "vmon": vmon,
            "vmon_highest": highest,
            "imon_peak": peak[3] if peak else None,
            "vmon_at_imon_peak": peak[2] if peak else None,
            "made_safe": False,
            "safe_detail": "",
            # Tripped again after a clear, without the output having worked
            # in between: the clear did not recover it.
            "needs_power_cycle": cleared is not None,
            "_since": time.time(),
        }
        self._tripped[name] = record
        log.warning(
            "TRIP %s: %s. IMON peaked at %s uA in the last minute (at %s V).%s",
            name, cause,
            "%.2f" % record["imon_peak"] if record["imon_peak"] is not None
            else "unknown",
            "%+.1f" % record["vmon_at_imon_peak"]
            if record["vmon_at_imon_peak"] is not None else "unknown",
            " It was cleared at %s and had not worked since, so the clear did "
            "NOT recover it: the supply needs a power cycle." % cleared["t"]
            if cleared else "")
        self._audit(name, "on" if is_energised(word) else "off", "tripped",
                    "ok", cause, AUTOMATIC_TRIP, action="caen_trip")
        return record

    def _make_safe(self, channel, record: dict):
        """VSET 0, then OFF, on a tripped channel. (volts, magnitude) or None.

        In that order: if OFF is refused the setpoint is at least gone, and a
        channel whose output comes back - after a clear, or a power cycle -
        comes back at 0 V rather than ramping into whatever tripped it.

        No acknowledgement, for the reason `_zero_setpoint` gives none. The
        record on TOPIC_HV_TRIP, the log and the audit trail carry it.
        """
        name = channel.name
        reader = self._readers.get(channel.device)
        if reader is None:
            self._safe_retry_at[name] = time.monotonic() + ZERO_RETRY_S
            self._publish_trip(record)
            return None
        index = int(channel.phys)

        with reader.transaction():
            before_mag = reader.monitor(index, "VSET")
            sent_v, why_v = reader.set_voltage(index, 0.0)
            after_mag = reader.monitor(index, "VSET") if sent_v else None
            sent_o, why_o = reader.set_output(index, False)
            after_word = reader.status(index)

        zeroed = (sent_v and after_mag is not None
                  and abs(after_mag) <= VSET_TOLERANCE_V)
        problems = []
        if not zeroed:
            problems.append("the setpoint could not be zeroed (%s)" % (
                why_v or "read back %s" % (
                    "nothing" if after_mag is None else "%.1f V" % after_mag)))
        if not sent_o:
            problems.append("it could not be switched off (%s)" % why_o)
        record["made_safe"] = not problems
        record["safe_detail"] = ("setpoint zeroed and output switched off"
                                 if not problems else "; ".join(problems))
        if problems:
            self._safe_retry_at[name] = time.monotonic() + ZERO_RETRY_S
            log.warning("TRIP %s: could not make it safe - %s. Retrying in "
                        "%.0f s.", name, record["safe_detail"], ZERO_RETRY_S)
        else:
            self._safe_retry_at.pop(name, None)
            log.warning("TRIP %s: %s. It stays latched until somebody clears "
                        "it on /hv.", name, record["safe_detail"])

        before = (apply_sign(before_mag, channel.sign)
                  if before_mag is not None else None)
        if zeroed:
            self._audit(name, before, 0.0, "ok",
                        "zeroed because the channel tripped", AUTOMATIC_TRIP)
        else:
            self._audit(name, before, 0.0, "rejected",
                        record["safe_detail"], AUTOMATIC_TRIP)
        if sent_o:
            self._audit(name, None, "off", "ok",
                        "switched off because the channel tripped",
                        AUTOMATIC_TRIP, action="caen_output")
        if after_word is not None:
            self._publish_status_now(channel, after_word)
        self._publish_trip(record)
        if not zeroed:
            return None
        after = apply_sign(after_mag, channel.sign)
        self._publish_vset_now(channel, after, after_mag)
        return (after, after_mag)

    def _publish_trip(self, record: dict) -> None:
        public = {k: v for k, v in record.items() if not k.startswith("_")}
        self.bus.publish_raw("%s/%s" % (TOPIC_HV_TRIP, record["channel"]),
                             json.dumps(public, separators=(",", ":")),
                             retain=True)

    def _note_recovered(self, channel, vset: float) -> None:
        """A cleared channel is holding its setpoint again. Say how it got there.

        This is the experiment the 23 September trips left open: does BDCLR
        bring a tripped DT1470ET back, or does only a power cycle? The answer
        is in this log line the first time it happens either way.
        """
        cleared = self._cleared.pop(channel.name)
        log.warning(
            "%s is holding %+.1f V again after the trip cleared at %s - "
            "%s.", channel.name, vset, cleared["t"],
            "the supply was power-cycled (relinked) in between"
            if cleared.get("relinked") else
            "WITHOUT a power cycle: the clear recovered it")

    def _on_trip_record(self, topic: str, payload: str) -> None:
        """Restore latches from the retained records, after a restart.

        The broker hands back every record on subscribe, including the ones
        this service published itself; an empty payload is a clear.
        """
        name = topic.rsplit("/", 1)[-1]
        if not payload.strip():
            self._tripped.pop(name, None)
            return
        if name in self._tripped:
            return
        if not any(c.name == name and c.kind == "hv_vset"
                   for c in self._channels):
            return
        try:
            record = json.loads(payload)
        except ValueError:
            return
        if isinstance(record, dict):
            record.setdefault("_since", 0.0)
            self._tripped[name] = record
            log.warning("%s: tripped at %s and not yet cleared (restored "
                        "from the bus)", name, record.get("t", "unknown"))

    def _handle_output(self, topic: str, payload: str) -> None:
        """Energise or de-energise a channel - the "turn ON HV" of section 10a.

        The safety of this rests on one thing: **it says what will happen.**
        Turning a channel on ramps it to whatever VSET holds, so the
        acknowledgement carries that voltage. Turning on at VSET 0 - which is
        the resting state the invariant guarantees - energises at zero and
        moves nothing, and that is a perfectly reasonable thing to do.
        """
        try:
            command = json.loads(payload) or {}
        except ValueError:
            self._refuse(None, "unparseable command", "",
                         ack_topic=ACK_HV_OUTPUT, action="caen_output")
            return

        actor = str(command.get("by") or "").strip() or "unknown"
        name = str(command.get("channel") or "").strip()
        if "on" not in command:
            self._refuse(name or None, "the command must say on: true or false",
                         actor, ack_topic=ACK_HV_OUTPUT, action="caen_output")
            return
        wanted_on = bool(command["on"])

        resolved = self._resolve(name, ACK_HV_OUTPUT, actor)
        if resolved is None:
            return
        channel, reader, index = resolved

        # A tripped channel stays off until somebody has acknowledged it.
        # Turning it back on without looking is how a discharge becomes two.
        if wanted_on and name in self._tripped:
            record = self._tripped[name]
            self._refuse(
                name, "this channel tripped at %s (%s) and has not been "
                      "cleared. Find out why, then use 'clear trip' on /hv or "
                      "xams-ctl hv-clear-trip" % (
                          record.get("t", "an unknown time"),
                          record.get("cause", "cause unknown")),
                actor, old="off", new="on", ack_topic=ACK_HV_OUTPUT,
                action="caen_output")
            return

        with reader.transaction():
            word = reader.status(index)
            vset_mag = reader.monitor(index, "VSET")

            if word is None:
                self._refuse(name, "could not read the channel's status word",
                             actor, ack_topic=ACK_HV_OUTPUT,
                             action="caen_output")
                return

            # A channel whose enable is off cannot be energised, and the board
            # would refuse anyway - but saying so here names the remedy rather
            # than reporting the symptom.
            if wanted_on and is_disabled(word):
                self._refuse(
                    name, "this channel is disabled at the supply. Flip its "
                          "enable switch on the front panel first - nothing "
                          "here can do that, by design (section 10a)", actor,
                    ack_topic=ACK_HV_OUTPUT, action="caen_output")
                return

            was_on = is_energised(word)
            sent, why = reader.set_output(index, wanted_on)
            after = reader.status(index) if sent else None

        vset = (apply_sign(vset_mag, channel.sign)
                if vset_mag is not None else None)

        if not sent:
            if wanted_on and is_tripped(word):
                why += (". The channel has TRIPPED, which latches until it is "
                        "cleared: use 'clear trip' on /hv or xams-ctl "
                        "hv-clear-trip, which zeroes the setpoint and clears it")
            self._refuse(name, why, actor, old="on" if was_on else "off",
                         new="on" if wanted_on else "off",
                         ack_topic=ACK_HV_OUTPUT, action="caen_output")
            return

        # THE READ-BACK IS ASYMMETRIC, and deliberately so.
        #
        # Turning ON sets bit 0 immediately, even when the channel then spends
        # a minute ramping, so it can be verified at once.
        #
        # Turning OFF starts a ramp DOWN, and bit 0 stays set until the
        # channel actually reaches zero. Demanding it clear immediately would
        # report a failure for a command that worked perfectly - and, worse,
        # would train somebody to re-send OFF to a channel already on its way
        # down. So OFF is verified as "the board accepted it", and the state
        # is reported as it is.
        now_on = is_energised(after) if after is not None else None
        if wanted_on and now_on is not True:
            self._refuse(
                name, "asked the channel to turn on, but it did not report ON "
                      "(status %s). The write did not take." % after, actor,
                old="on" if was_on else "off", new="on",
                ack_topic=ACK_HV_OUTPUT, action="caen_output")
            return

        ramping = vset is not None and abs(vset) > 1.0
        detail = ""
        if wanted_on and ramping:
            detail = "ramping to %+.1f V" % vset
        elif wanted_on:
            detail = "energised at 0 V; nothing will move until a setpoint is set"
        elif now_on:
            detail = "ramping down at the board's own rate"

        log.warning("output %s: %s -> %s%s, by %s", name,
                    "on" if was_on else "off", "on" if wanted_on else "off",
                    (" (%s)" % detail) if detail else "", actor)
        # Before the ack, not after: the ack is what releases the web request,
        # and the page it then renders must not be built from the state this
        # command has just made obsolete.
        if after is not None:
            self._publish_status_now(channel, after)
        # On its way off: shown as switching until STAT bit 0 clears.
        if not wanted_on and now_on:
            self._set_switching(name, {
                "wanted": "off", "since": iso(utcnow()),
                "_until": time.monotonic() + self._off_wait_s(channel, vset)})
        elif wanted_on:
            self._set_switching(name, None)
        self.bus.publish_raw(ACK_HV_OUTPUT, json.dumps(
            {"ok": True, "channel": name, "on": wanted_on, "vset": vset,
             "detail": detail, "by": actor}, separators=(",", ":")))
        self._audit(name, "on" if was_on else "off",
                    "on" if wanted_on else "off", "ok", detail, actor,
                    action="caen_output")

    # --------------------------------------------------- recovering from a trip

    def _handle_clear(self, topic: str, payload: str) -> None:
        """Acknowledge and clear a trip, zeroing the setpoint first (§10a).

        **What counts as tripped.** The latch this service keeps
        (`_tripped`, from `_watch_trips`), OR the board's own STAT bit 7 or
        BDALARM bit. Until 23 September 2026 only the board's flags counted,
        and on the day two real trips happened the board set neither - so
        the clear refused, the page offered nothing, and the only way back was
        a power cycle.

        A channel that is none of those may still be cleared as long as it is
        OFF: zeroing an idle channel and sending BDCLR harms nothing, and it
        is the one thing left to try on a supply whose trip went unseen. One
        that is ON and not tripped is refused - clearing is not how to turn a
        working channel off.

        **VSET goes to zero before the alarm is cleared, and the channel is
        switched OFF.** The setpoint the channel tripped at is still loaded,
        and a recovery that turns it back on ramps straight into whatever made
        it trip. A cleared channel is exactly as safe as a freshly enabled
        one: turning it on energises at 0 V.

        **Every tripped channel on the supply, not just the one asked about.**
        `BDCLR` is board-wide. If ANY of those zeroings fails, nothing is
        cleared.

        **Whether BDCLR actually brings a DT1470ET output back is not known.**
        The channel is remembered as cleared (`_cleared`); if its output then
        holds a setpoint the log says so and whether a power cycle came in
        between, and if it collapses again first, the new trip says the clear
        did not recover it and the supply needs a power cycle.
        """
        try:
            command = json.loads(payload) or {}
        except ValueError:
            self._refuse(None, "unparseable command", "",
                         ack_topic=ACK_HV_CLEAR, action="caen_clear")
            return

        actor = str(command.get("by") or "").strip() or "unknown"
        name = str(command.get("channel") or "").strip()
        resolved = self._resolve(name, ACK_HV_CLEAR, actor, action="caen_clear")
        if resolved is None:
            return
        channel, reader, index = resolved

        siblings = [c for c in self._channels
                    if c.kind == "hv_vset" and c.device == channel.device]
        zeroed = []                       # (channel, before, after, magnitude)
        switched_off = []

        with reader.transaction():
            alarm = reader.alarm_word()
            words = {int(c.phys): reader.status(int(c.phys)) for c in siblings}
            word = words.get(index)
            if word is None:
                self._refuse(name, "could not read the channel's status word",
                             actor, ack_topic=ACK_HV_CLEAR, action="caen_clear")
                return

            def latched(sib):
                i = int(sib.phys)
                w = words.get(i)
                return (sib.name in self._tripped
                        or (w is not None and is_tripped(w))
                        or (alarm is not None and bool(alarm & (1 << i))))

            if not latched(channel) and is_energised(word):
                self._refuse(
                    name, "it is on and has not tripped (the supply reports "
                          "%s), so there is nothing to clear. Turn it off "
                          "first if it needs resetting" % describe_status(word),
                    actor, ack_topic=ACK_HV_CLEAR, action="caen_clear")
                return

            targets = [s for s in siblings if s is channel or latched(s)]
            for sib in targets:
                i = int(sib.phys)
                before_mag = reader.monitor(i, "VSET")
                if before_mag is None or abs(before_mag) > VSET_TOLERANCE_V:
                    sent, why = reader.set_voltage(i, 0.0)
                    after_mag = reader.monitor(i, "VSET") if sent else None
                    before = (apply_sign(before_mag, sib.sign)
                              if before_mag is not None else None)
                    if (not sent or after_mag is None
                            or abs(after_mag) > VSET_TOLERANCE_V):
                        why = why or ("read back %s after writing 0" % (
                            "nothing" if after_mag is None
                            else "%.1f V" % after_mag))
                        self._refuse(
                            name, "the setpoint of %s could not be zeroed (%s), "
                                  "so the trip was NOT cleared - clearing it "
                                  "would leave that channel holding the voltage "
                                  "it tripped at" % (sib.name, why), actor,
                            old=before, new=0.0, ack_topic=ACK_HV_CLEAR,
                            action="caen_clear")
                        return
                    zeroed.append((sib, before, apply_sign(after_mag, sib.sign),
                                   after_mag))
                w = words.get(i)
                if w is not None and is_energised(w):
                    sent, why = reader.set_output(i, False)
                    if not sent:
                        self._refuse(
                            name, "%s could not be switched off (%s), so the "
                                  "trip was NOT cleared" % (sib.name, why),
                            actor, ack_topic=ACK_HV_CLEAR, action="caen_clear")
                        return
                    switched_off.append(sib)

            sent, why = reader.clear_alarm()
            after_word = reader.status(index) if sent else None
            after_alarm = reader.alarm_word() if sent else None

        # The zeroings happened whether or not the clear then did: publish
        # and audit them either way, so the page and the trail match the board.
        for sib, before, after, magnitude in zeroed:
            self._publish_vset_now(sib, after, magnitude)
            self._audit(sib.name, before, after, "ok",
                        "zeroed before clearing a trip (section 10a)", actor)
        for sib in switched_off:
            self._audit(sib.name, "on", "off", "ok",
                        "switched off before clearing a trip", actor,
                        action="caen_output")

        if not sent:
            self._refuse(name, why, actor, ack_topic=ACK_HV_CLEAR,
                         action="caen_clear")
            return

        if after_word is not None:
            self._publish_status_now(channel, after_word)

        # The acknowledgement: the latch goes, and each channel is watched
        # for whether its output comes back.
        acknowledged = []
        for sib in targets:
            record = self._tripped.pop(sib.name, None)
            self._flag_hits.pop(sib.name, None)
            self._collapse_hits.pop(sib.name, None)
            self._safe_retry_at.pop(sib.name, None)
            if record is None and sib is not channel:
                continue
            since = (record or {}).get("_since", 0.0)
            self._cleared[sib.name] = {
                "t": iso(utcnow()),
                "relinked": (record is not None and
                             self._relinked_at.get(sib.device, 0.0) > since),
            }
            if record is not None:
                acknowledged.append(sib.name)
            self.bus.publish_raw("%s/%s" % (TOPIC_HV_TRIP, sib.name), "",
                                 retain=True)

        parts = []
        if acknowledged:
            parts.append("trip acknowledged on %s" % ", ".join(acknowledged))
        if zeroed:
            parts.append("setpoint%s zeroed: %s" % (
                "s" if len(zeroed) > 1 else "",
                ", ".join("%s (was %s)" % (
                    s.name, "%+.0f V" % b if b is not None else "unknown")
                    for s, b, _, _ in zeroed)))
        still = []
        if after_word is not None and is_tripped(after_word):
            still.append("STAT still has TRIP")
        if after_alarm is not None and after_alarm & (1 << index):
            still.append("BDALARM still set")
        if still:
            # Not called a failure: whether the board drops bit 7 on BDCLR or
            # only on the next ON is not documented, and the setpoint is zero
            # either way, so turning it on is now safe to try.
            parts.append("the board accepted the clear but %s; turning the "
                         "channel on now energises at 0 V and may clear it"
                         % " and ".join(still))
        else:
            parts.append("board alarm cleared; the channel is off at 0 V")
        parts.append("whether the output works again is only known once it "
                     "is on - raise it in small steps; if it does not follow, "
                     "it is flagged again and the supply needs a power cycle")
        detail = "; ".join(parts)

        log.warning("trip clear %s: %s, by %s", name, detail, actor)
        self.bus.publish_raw(ACK_HV_CLEAR, json.dumps(
            {"ok": True, "channel": name, "detail": detail, "by": actor},
            separators=(",", ":")))
        self._audit(name, describe_status(word),
                    describe_status(after_word) if after_word is not None
                    else None, "ok", detail, actor, action="caen_clear")

    def run(self) -> int:
        self.bus.subscribe(TOPIC_HV_VSET, self._handle_vset)
        self.bus.subscribe(TOPIC_HV_OUTPUT, self._handle_output)
        self.bus.subscribe(TOPIC_HV_CLEAR, self._handle_clear)
        self.bus.subscribe("%s/+" % TOPIC_HV_TRIP, self._on_trip_record)
        # A "switching off" left retained by a previous run is not known to
        # be true any more: clear it. A channel still ramping shows that from
        # its own STAT bits.
        for ch in self._channels:
            if ch.kind == "hv_vset":
                self.bus.publish_raw("%s/%s" % (TOPIC_HV_SWITCHING, ch.name), "",
                                     retain=True)
        return super().run()

    def _relink(self, force: bool = False) -> bool:
        """Re-resolve the supplies after a link loss, keeping what still works.

        Deliberately a FULL re-resolution, never a bare reopen. A replugged
        unit can come back on a different COM number, and the handle held
        before the unplug is dead regardless; the ports are enumerated again
        and every board is asked for its BDSNUM, so if the two cables were
        swapped while the link was down this finds each unit where it now is
        rather than reading the wrong supply under the right name (§6.2
        rule 6).

        Unlike startup this is FORGIVING, and that is the point. Startup is
        strict by design - refuse to run rather than guess (§6.1) - and
        verify_identity() clears every reader before it raises, so reusing it
        here would drop a healthy supply on the floor the moment its
        neighbour went missing. Each supply is resolved on its own: one that
        is still unplugged costs the other nothing.

        Returns True if at least one supply is connected afterwards.
        """
        if self.simulate:
            return True

        now = time.monotonic()
        if not force and now < self._next_relink:
            return bool(self._readers)
        # Enumerating and probing costs a second or two, during which the
        # healthy supply is not read. Once every 10 s keeps a dead cable from
        # starving a live one.
        self._next_relink = now + 10.0

        for reader in self._readers.values():
            reader.close()
        self._readers.clear()

        for spec in self._specs:
            vid, pid = spec["match"]["vid"], spec["match"]["pid"]
            expected = {spec["id"]: (spec["board_name"], str(spec["board_serial"]))}
            try:
                mapping = resolve(vid, pid, self._probe, expected)
            except IdentityError as exc:
                log.warning("%s: not found; leaving it disconnected (%s)",
                            spec["id"], str(exc).split(".")[0])
                continue
            reader = CaenChannelReader(
                device_id=spec["id"], port=mapping[spec["id"]],
                address=int(spec.get("board_address", 0)),
                baud=int(spec.get("baud", 9600)),
                board_name=spec["board_name"],
                board_serial=str(spec["board_serial"]))
            try:
                reader.open()
            except Exception as exc:
                log.warning("%s: could not open %s: %s",
                            spec["id"], reader.port, exc)
                continue
            self._readers[spec["id"]] = reader
            self._link_down[spec["id"]] = 0
            self._relinked_at[spec["id"]] = time.time()
            log.info("%s: relinked on %s, serial %s re-verified",
                     spec["id"], reader.port, reader.board_serial)
            self._check_expectations(spec["id"], reader)

        return bool(self._readers)

    def reconnect(self) -> bool:
        """Called by BaseService when every channel has failed (§6.1)."""
        return self._relink(force=True)

    def close(self) -> None:
        for reader in self._readers.values():
            reader.close()
        log.info("CAEN ports released")
