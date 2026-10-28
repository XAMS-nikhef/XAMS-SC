"""Detecting a CAEN trip, making it safe, and what clearing it then means.

See DESIGN.md §10a and caen.py `_watch_trips`.

On 23 September 2026 the anode (hv_2 ch2) broke down twice - 20 uA at
+2500 V, then 5 uA at +2400 V. Both times the board cut the output and VMON
decayed to zero while STAT stayed ON + UNDER_VOLTAGE (33). **Bit 7 was never
set and BDALARM stayed 0.** Everything that waited for those flags waited
forever: no clear was offered, the channel sat "on" at its full setpoint with a
dead output, and the only way back was a power cycle.

The rules pinned here:

* a collapsed output under a channel that still says ON is a trip;
* a normal ramp, and current limiting just below the setpoint, are not;
* one corrupted status word with bit 7 in it is not a trip;
* a trip is made safe at once (VSET 0, then OFF), latched, and the channel
  cannot be turned on until somebody clears it;
* a trip is NOT an alarm - nobody is woken for it;
* a channel that trips again after a clear without ever working in between
  says the supply needs a power cycle.
"""

import contextlib
import json
import threading

import pytest

from doubles import RecordingBus

from xams_sc.bus import (ACK_HV_CLEAR, ACK_HV_OUTPUT, TOPIC_ALARM, TOPIC_AUDIT,
                         TOPIC_HV_CLEAR, TOPIC_HV_OUTPUT, TOPIC_HV_TRIP)
from xams_sc.config import load
from xams_sc.devices.caen import CaenService
from xams_sc.hv_status import (COLLAPSE_CONFIRM_READS, FLAG_CONFIRM_READS,
                               output_collapsed, output_healthy)

ON = 1 << 0
RAMP_UP = 1 << 1
UNDER_VOLTAGE = 1 << 5
TRIP = 1 << 7
ANODE = 2                                  # hv_2 channel 2, positive
TRIP_TOPIC = f"{TOPIC_HV_TRIP}/hv_anode_vset"


class Supply:
    """One DT1470ET: per-channel STAT, VSET, VMON and IMON, as on the wire."""

    def __init__(self):
        self.stat = {i: 0 for i in range(4)}
        self.vset = {i: 0.0 for i in range(4)}
        self.vmon = {i: 0.0 for i in range(4)}
        self.imon = {i: 0.0 for i in range(4)}
        self.alarm = 0
        self.accepts = True
        self.log = []
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            yield self

    def status(self, channel):
        return self.stat[channel]

    def alarm_word(self):
        return self.alarm

    def monitor(self, channel, par):
        return {"VSET": self.vset, "VMON": self.vmon,
                "IMON": self.imon}[par][channel]

    def set_voltage(self, channel, magnitude):
        self.log.append(("VSET", channel, magnitude))
        if not self.accepts:
            return False, "the supply is in LOCAL mode"
        self.vset[channel] = magnitude
        return True, ""

    def set_output(self, channel, on):
        self.log.append(("ON" if on else "OFF", channel))
        if not self.accepts:
            return False, "the supply is in LOCAL mode"
        self.stat[channel] = (self.stat[channel] | ON) if on else (
            self.stat[channel] & ~ON & ~UNDER_VOLTAGE)
        return True, ""

    def clear_alarm(self):
        self.log.append(("BDCLR",))
        self.alarm = 0
        return True, ""

    # ---- what the anode did on 23 September 2026

    def working(self, volts=2400.0, current=0.1):
        self.stat[ANODE] = ON
        self.vset[ANODE] = self.vmon[ANODE] = volts
        self.imon[ANODE] = current

    def breakdown(self, vmon=2044.7, imon=5.04):
        """Current-limiting: VMON sags, UNDER_VOLTAGE, the board still ON."""
        self.stat[ANODE] = ON | UNDER_VOLTAGE
        self.vmon[ANODE], self.imon[ANODE] = vmon, imon

    def collapsed(self, vmon=3.5):
        """The board has cut the output. Still ON + UNDER_VOLTAGE (33)."""
        self.stat[ANODE] = ON | UNDER_VOLTAGE
        self.vmon[ANODE], self.imon[ANODE] = vmon, 0.15


class Idle:
    """The other supply: every channel off and quiet."""

    def status(self, channel):
        return 0

    def alarm_word(self):
        return 0

    def monitor(self, channel, par):
        return 0.0



@pytest.fixture(autouse=True)
def pt1000_readout_off(monkeypatch):
    """These tests are about switching, not about the PT1000 lock (28 October
    2026), which refuses every ON while the readout is on or unknown. The
    readout is taken as OFF here; the lock has its own tests
    (test_pt1000_interlock.py)."""
    monkeypatch.setattr(CaenService, "_readout_lock", lambda self: None)

@pytest.fixture
def supply():
    return Supply()


@pytest.fixture
def service(tmp_path, supply):
    svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
    svc._readers = {"hv_1": Idle(), "hv_2": supply}
    return svc


def poll(service, times=1):
    for _ in range(times):
        service.read()


def trip_the_anode(service, supply):
    supply.working()
    poll(service, 3)
    supply.breakdown()
    poll(service)
    supply.collapsed()
    poll(service, COLLAPSE_CONFIRM_READS)


def turn_on(service, channel="hv_anode_vset"):
    service._handle_output(TOPIC_HV_OUTPUT, json.dumps(
        {"channel": channel, "on": True, "by": "ap"}))
    return service.bus.last_json(ACK_HV_OUTPUT)


def clear(service, channel="hv_anode_vset"):
    service._handle_clear(TOPIC_HV_CLEAR, json.dumps(
        {"channel": channel, "by": "ap"}))
    return service.bus.last_json(ACK_HV_CLEAR)


class TestTheRule:
    def test_the_23_september_signature_is_a_collapse(self):
        assert output_collapsed(33, 2400.0, 3.5)

    def test_a_ramp_is_not(self):
        assert not output_collapsed(ON | RAMP_UP | UNDER_VOLTAGE, 2000.0, 0.0)

    def test_current_limiting_above_half_the_setpoint_is_not(self):
        assert not output_collapsed(33, 2400.0, 2044.7)

    def test_a_channel_that_is_off_is_not(self):
        assert not output_collapsed(UNDER_VOLTAGE, 2400.0, 0.0)

    def test_a_tiny_setpoint_is_not(self):
        assert not output_collapsed(33, 10.0, 0.0)

    def test_a_missing_reading_is_not_evidence(self):
        assert not output_collapsed(33, 2400.0, None)

    def test_holding_the_setpoint_is_healthy(self):
        assert output_healthy(ON, 2400.0, 2400.8)
        assert not output_healthy(ON, 2400.0, 0.0)


class TestDetection:
    def test_the_anode_trip_of_23_september_is_detected(self, service, supply):
        trip_the_anode(service, supply)

        assert "hv_anode_vset" in service._tripped
        record = service.bus.last_json(TRIP_TOPIC)
        assert "collapsed" in record["cause"]

    def test_it_needs_several_reads_in_a_row(self, service, supply):
        supply.working()
        poll(service)
        supply.collapsed()
        poll(service, COLLAPSE_CONFIRM_READS - 1)

        assert service._tripped == {}

    def test_a_ramp_from_zero_is_not_a_trip(self, service, supply):
        supply.stat[ANODE] = ON | RAMP_UP
        supply.vset[ANODE] = 2000.0
        poll(service, 20)

        assert service._tripped == {}

    def test_current_limiting_alone_is_not_a_trip(self, service, supply):
        """The board decides when to give up; this only reports that it has."""
        supply.working()
        supply.breakdown()
        poll(service, 10)

        assert service._tripped == {}

    def test_one_corrupted_word_with_bit_7_is_not_a_trip(self, service, supply):
        """683 and 819 were each read once on 17-18 September; both have
        bit 7 in them."""
        supply.working()
        supply.stat[ANODE] = 683
        poll(service)
        supply.working()
        poll(service, 5)

        assert service._tripped == {}

    def test_the_board_flag_seen_twice_is_a_trip(self, service, supply):
        supply.stat[ANODE] = TRIP
        poll(service, FLAG_CONFIRM_READS)

        assert "flagged" in service._tripped["hv_anode_vset"]["cause"]

    def test_a_BDALARM_bit_seen_twice_is_a_trip(self, service, supply):
        supply.alarm = 1 << ANODE
        poll(service, FLAG_CONFIRM_READS)

        assert "BDALARM" in service._tripped["hv_anode_vset"]["cause"]

    def test_the_current_spike_before_it_is_reported(self, service, supply):
        trip_the_anode(service, supply)

        record = service.bus.last_json(TRIP_TOPIC)
        assert record["imon_peak"] == pytest.approx(5.04)
        assert record["vmon_at_imon_peak"] == pytest.approx(2044.7)

    def test_a_trip_is_not_an_alarm(self, service, supply):
        """Nobody is woken for an HV trip (A.P. Colijn, 23 September 2026)."""
        trip_the_anode(service, supply)

        assert not [t for t, _ in service.bus.published
                    if t.startswith(TOPIC_ALARM)]


class TestMadeSafe:
    def test_the_setpoint_is_zeroed_then_it_is_switched_off(self, service, supply):
        trip_the_anode(service, supply)

        assert supply.log == [("VSET", ANODE, 0.0), ("OFF", ANODE)]
        assert service._tripped["hv_anode_vset"]["made_safe"] is True

    def test_it_is_audited_as_automatic(self, service, supply):
        trip_the_anode(service, supply)

        actors = {(r["action"], r["actor"])
                  for r in service.bus.json_on(TOPIC_AUDIT)}
        assert ("caen_trip", "automatic (trip)") in actors
        assert ("caen_vset", "automatic (trip)") in actors

    def test_a_board_in_local_is_retried_not_forgotten(self, service, supply):
        supply.accepts = False
        trip_the_anode(service, supply)

        assert service._tripped["hv_anode_vset"]["made_safe"] is False
        writes = len(supply.log)
        poll(service, 3)
        assert len(supply.log) == writes, "retried on every poll"

    def test_nothing_else_on_the_supply_is_touched(self, service, supply):
        supply.stat[1], supply.vset[1], supply.vmon[1] = ON, 2000.0, 2000.5
        trip_the_anode(service, supply)

        assert all(entry[1] == ANODE for entry in supply.log)


class TestTheLatch:
    def test_a_tripped_channel_cannot_be_turned_on(self, service, supply):
        trip_the_anode(service, supply)

        ack = turn_on(service)

        assert ack["ok"] is False
        assert "clear trip" in ack["reason"]
        assert ("ON", ANODE) not in supply.log

    def test_it_can_be_cleared_although_the_board_flags_nothing(
            self, service, supply):
        """The case that had no way out on 23 September."""
        trip_the_anode(service, supply)
        assert supply.alarm == 0 and not supply.stat[ANODE] & TRIP

        ack = clear(service)

        assert ack["ok"] is True
        assert "hv_anode_vset" not in service._tripped
        assert service.bus.published_on(TRIP_TOPIC)[-1] == ""
        assert turn_on(service)["ok"] is True

    def test_it_survives_a_restart(self, service, supply):
        """Latches are restored from the retained records on the bus."""
        trip_the_anode(service, supply)
        record = service.bus.published_on(TRIP_TOPIC)[-1]
        service._tripped.clear()

        service._on_trip_record(TRIP_TOPIC, record)

        assert turn_on(service)["ok"] is False


class TestAfterAClear:
    def test_tripping_again_without_working_says_power_cycle(
            self, service, supply):
        """The clear did not bring the output back: that is the answer to
        the question the manual leaves open, and the operator needs it."""
        trip_the_anode(service, supply)
        clear(service)
        turn_on(service)
        supply.vset[ANODE] = 1000.0
        supply.collapsed(vmon=0.0)
        poll(service, COLLAPSE_CONFIRM_READS)

        assert service._tripped["hv_anode_vset"]["needs_power_cycle"] is True

    def test_working_in_between_means_it_is_a_new_trip(self, service, supply,
                                                       caplog):
        trip_the_anode(service, supply)
        clear(service)
        turn_on(service)
        supply.working(volts=1000.0)
        poll(service)
        assert "WITHOUT a power cycle" in caplog.text

        supply.collapsed()
        poll(service, COLLAPSE_CONFIRM_READS)

        assert service._tripped["hv_anode_vset"]["needs_power_cycle"] is False


class TestClearingRefusals:
    def test_a_working_channel_is_not_cleared(self, service, supply):
        supply.working()

        ack = clear(service)

        assert ack["ok"] is False
        assert "has not tripped" in ack["reason"]
        assert supply.log == []

    def test_an_idle_channel_may_be_cleared(self, service, supply):
        """Zeroing an idle channel and sending BDCLR harms nothing, and it is
        what is left to try on a trip nothing saw."""
        ack = clear(service)

        assert ack["ok"] is True
        assert ("BDCLR",) in supply.log

    def test_a_latched_channel_still_on_is_switched_off_first(
            self, service, supply):
        supply.accepts = False
        trip_the_anode(service, supply)
        supply.accepts = True
        supply.log.clear()

        clear(service)

        assert supply.log == [("VSET", ANODE, 0.0), ("OFF", ANODE), ("BDCLR",)]
