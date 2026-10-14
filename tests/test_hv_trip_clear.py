"""Recovering a tripped CAEN channel. See DESIGN.md §10a.

A trip latches: the board switches the channel off, keeps STAT bit 7 set and
raises its board alarm, and neither ON nor the enable switch clears it. On
23 September 2026 the only way back was a power cycle.

The clear is `BDCLR`, and the rule these tests hold it to:

> **No latched channel comes out of a clear holding a setpoint.**

`BDCLR` is board-wide, so that means every latched channel on the supply,
not just the one somebody clicked - and if any of them cannot be zeroed, the
alarm is not cleared at all.
"""

import contextlib
import json
import threading

import pytest

from doubles import RecordingBus

from xams_sc.bus import ACK_HV_CLEAR, TOPIC_AUDIT, TOPIC_HV_CLEAR
from xams_sc.config import load
from xams_sc.devices.caen import CaenService
from xams_sc.hv_status import BIT_ON, BIT_TRIP, is_tripped

TRIP = 1 << BIT_TRIP
ON = 1 << BIT_ON
UNDER_VOLTAGE = 1 << 5


class TrippableSupply:
    """One board, per channel. `vset` holds unsigned magnitudes, as on the wire.

    `clear_drops_trip` decides whether BDCLR also drops STAT bit 7. The manual
    does not say, so both are tested.
    """

    def __init__(self, stat=None, vset=None, alarm=0, clear_drops_trip=True,
                 accepts_vset=True, accepts_clear=True):
        self.stat = dict(stat or {})
        self.vset = dict(vset or {})
        self.alarm = alarm
        self.clear_drops_trip = clear_drops_trip
        self.accepts_vset = accepts_vset
        self.accepts_clear = accepts_clear
        self.log = []                          # every write, in order
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            yield self

    def status(self, channel):
        return self.stat.get(channel, 0)

    def alarm_word(self):
        return self.alarm

    def monitor(self, channel, par):
        return self.vset.get(channel, 0.0) if par == "VSET" else 0.0

    def set_voltage(self, channel, magnitude):
        self.log.append(("VSET", channel, magnitude))
        if not self.accepts_vset:
            return False, "the supply refused it"
        self.vset[channel] = magnitude
        return True, ""

    def clear_alarm(self):
        self.log.append(("BDCLR",))
        if not self.accepts_clear:
            return False, "the supply refused it"
        self.alarm = 0
        if self.clear_drops_trip:
            self.stat = {i: w & ~TRIP for i, w in self.stat.items()}
        return True, ""


@pytest.fixture
def service(tmp_path):
    svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
    svc._readers = {"hv_1": TrippableSupply(), "hv_2": TrippableSupply()}
    return svc


def clear(service, channel="hv_cathode_vset", by="ap"):
    service._handle_clear(TOPIC_HV_CLEAR, json.dumps(
        {"channel": channel, "by": by}))
    return service.bus.last_json(ACK_HV_CLEAR)


def trip_cathode(service, **kw):
    """hv_2 channel 0 tripped at -2250 V, as it would be after a discharge."""
    supply = TrippableSupply(stat={0: TRIP | UNDER_VOLTAGE}, vset={0: 2250.0},
                             alarm=0b0001, **kw)
    service._readers["hv_2"] = supply
    return supply


class TestTheSetpointGoesFirst:
    def test_a_tripped_channel_is_zeroed_and_cleared(self, service):
        supply = trip_cathode(service)

        ack = clear(service)

        assert ack["ok"] is True
        assert supply.vset[0] == 0.0
        assert not is_tripped(supply.stat[0])

    def test_the_zero_is_written_BEFORE_the_alarm_is_cleared(self, service):
        """The other order leaves a window with the latch gone and 2250 V
        still loaded."""
        supply = trip_cathode(service)

        clear(service)

        assert supply.log == [("VSET", 0, 0.0), ("BDCLR",)]

    def test_the_ack_says_what_the_setpoint_was(self, service):
        trip_cathode(service)

        ack = clear(service)

        assert "-2250 V" in ack["detail"]

    def test_a_channel_already_at_zero_is_not_written(self, service):
        supply = trip_cathode(service)
        supply.vset[0] = 0.0

        clear(service)

        assert supply.log == [("BDCLR",)]

    def test_the_channel_is_not_switched_on(self, service):
        """Clearing is recovery, not energising. Turning on stays a
        separate, deliberate act."""
        supply = trip_cathode(service)

        clear(service)

        assert not supply.stat[0] & ON


class TestTheClearIsBoardWide:
    def test_every_latched_channel_on_the_supply_is_zeroed(self, service):
        """BDCLR clears the alarm on every channel. A second tripped channel
        left holding its setpoint would come out of this armed."""
        supply = TrippableSupply(
            stat={0: TRIP, 2: TRIP}, vset={0: 2250.0, 2: 4200.0},
            alarm=0b0101)
        service._readers["hv_2"] = supply

        clear(service)

        assert supply.vset[0] == 0.0
        assert supply.vset[2] == 0.0, "the anode kept 4200 V through a clear"

    def test_a_channel_latched_only_in_BDALARM_is_zeroed_too(self, service):
        # The gate, on hv_2 channel 3 since 14 October 2026.
        supply = TrippableSupply(stat={0: TRIP}, vset={0: 2250.0, 3: 1750.0},
                                 alarm=0b1001)
        service._readers["hv_2"] = supply

        clear(service)

        assert supply.vset[3] == 0.0

    def test_healthy_channels_keep_their_setpoints(self, service):
        supply = TrippableSupply(stat={0: TRIP, 3: ON},
                                 vset={0: 2250.0, 3: 600.0}, alarm=0b0001)
        service._readers["hv_2"] = supply

        clear(service)

        assert supply.vset[3] == 600.0

    def test_the_other_supply_is_untouched(self, service):
        trip_cathode(service)
        other = TrippableSupply(stat={0: TRIP}, vset={0: 700.0}, alarm=1)
        service._readers["hv_1"] = other

        clear(service)

        assert other.log == []

    def test_if_any_zeroing_fails_nothing_is_cleared(self, service):
        supply = trip_cathode(service, accepts_vset=False)

        ack = clear(service)

        assert ack["ok"] is False
        assert "NOT cleared" in ack["reason"]
        assert ("BDCLR",) not in supply.log


class TestRefusals:
    def test_nothing_latched_is_refused_and_says_what_the_board_reports(
            self, service):
        """UNDER_VOLTAGE on its own is a live condition, not a latch."""
        supply = TrippableSupply(stat={0: ON | UNDER_VOLTAGE}, vset={0: 2250.0})
        service._readers["hv_2"] = supply

        ack = clear(service)

        assert ack["ok"] is False
        assert "UNDER_VOLTAGE" in ack["reason"]
        assert supply.log == []

    def test_a_board_that_refuses_the_clear_is_reported(self, service):
        trip_cathode(service, accepts_clear=False)

        ack = clear(service)

        assert ack["ok"] is False

    def test_an_unknown_channel_is_refused(self, service):
        ack = clear(service, channel="hv_nonsense")

        assert ack["ok"] is False


class TestWhenTheTripBitStays:
    def test_it_is_not_called_a_failure_and_says_to_try_on(self, service):
        """Whether BDCLR drops STAT bit 7, or only the next ON does, is not
        documented. With the setpoint at zero, trying ON is safe."""
        trip_cathode(service, clear_drops_trip=False)

        ack = clear(service)

        assert ack["ok"] is True
        assert "still has TRIP" in ack["detail"]
        assert "0 V" in ack["detail"]


class TestAudit:
    def test_the_zeroing_and_the_clear_are_both_audited(self, service):
        trip_cathode(service)

        clear(service, by="ap")

        records = [json.loads(p) for t, p in service.bus.published
                   if t == TOPIC_AUDIT]
        actions = [(r["action"], r["target"], r["actor"]) for r in records]
        assert ("caen_vset", "hv_cathode_vset", "ap") in actions
        assert ("caen_clear", "hv_cathode_vset", "ap") in actions
