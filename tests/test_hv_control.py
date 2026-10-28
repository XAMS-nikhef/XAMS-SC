"""The CAEN setpoint write path. See DESIGN.md §10a.

**This is the code that puts kilovolts on electrodes.** The tests are
overwhelmingly about refusal, because the failure modes are not symmetric: a
command wrongly refused is an annoyance, and a command wrongly accepted is
4.2 kV on an anode nobody was standing next to.

The invariant the whole design rests on:

> A channel that is not enabled has `VSET` = 0.

The enable is a hand operation and the board ramps to `VSET` the instant it is
flipped. If the invariant holds, flipping the switch is always safe. If it does
not, flipping it is a step into an unannounced voltage.
"""

import contextlib
import json

import pytest

from doubles import RecordingBus

from xams_sc.bus import (ACK_HV_OUTPUT, ACK_HV_VSET, TOPIC_AUDIT,
                         TOPIC_HV_OUTPUT, TOPIC_HV_VSET)
from xams_sc.config import load
from xams_sc.devices.caen import CaenService
from xams_sc.hv_status import BIT_DISABLED, BIT_ON
from xams_sc.model import Measurement, Quality, utcnow

ENABLED = 1 << BIT_ON
DISABLED = 1 << BIT_DISABLED


class FakeReader:
    """One supply. `vset` is the unsigned magnitude, as on the wire."""

    def __init__(self, stat=ENABLED, vset=0.0, obey=True, accepts=True,
                 answers=True, refusal="the supply refused it"):
        import threading
        self.stat = stat
        self.vset = vset
        self.obey = obey            # False: acknowledges, keeps the old value
        self.accepts = accepts      # False: the board refuses the command
        self.answers = answers      # False: reads come back as None
        self.refusal = refusal      # why the board refused, as it reports it
        self.written = []
        self.outputs = []
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def transaction(self):
        """As the real reader does: hold the port for one conversation.

        Reentrant, because the calls the service makes inside it take the
        lock themselves. This used to be a bare `_lock` attribute, mirrored
        here only because the service reached in and took it.
        """
        with self._lock:
            yield self

    def status(self, channel):
        return self.stat if self.answers else None

    def alarm_word(self):
        return 0 if self.answers else None

    def monitor(self, channel, par):
        if not self.answers:
            return None
        return self.vset if par == "VSET" else 0.0

    def set_voltage(self, channel, magnitude):
        # (ok, reason) like the real reader: the board says WHY it refused,
        # and the reasons are not interchangeable - LOCAL mode needs a person
        # at the front panel, a rejected value needs a different number.
        self.written.append(magnitude)
        if not self.accepts:
            return False, self.refusal
        if self.obey:
            self.vset = magnitude
        return True, ""

    def set_output(self, channel, on):
        self.outputs.append(on)
        if not self.accepts:
            return False, self.refusal
        if self.obey:
            self.stat = (self.stat | (1 << BIT_ON)) if on else (
                self.stat & ~(1 << BIT_ON))
        return True, ""

    def control_mode(self):
        return "REMOTE" if self.accepts else "LOCAL"



@pytest.fixture(autouse=True)
def pt1000_readout_off(monkeypatch):
    """These tests are about switching, not about the PT1000 lock (28 October
    2026), which refuses every ON while the readout is on or unknown. The
    readout is taken as OFF here; the lock has its own tests
    (test_pt1000_interlock.py)."""
    monkeypatch.setattr(CaenService, "_readout_lock", lambda self: None)

@pytest.fixture
def service(tmp_path):
    svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
    svc._readers = {"hv_1": FakeReader(), "hv_2": FakeReader()}
    return svc


def send(service, channel="hv_cathode_vset", value=-1000.0, by="ap"):
    service._handle_vset(TOPIC_HV_VSET, json.dumps(
        {"channel": channel, "value": value, "by": by}))
    return service.bus.last_json(ACK_HV_VSET)


class TestTheInvariant:
    """Section 10a: a channel that is not enabled keeps VSET at zero."""

    def test_a_non_zero_setpoint_on_a_disabled_channel_is_refused(self, service):
        """The heart of it. Allowing this would let somebody stage 4.2 kV,
        walk to the supply, flip the switch, and get the unannounced ramp the
        whole design exists to prevent."""
        service._readers["hv_2"].stat = DISABLED

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "disabled at the supply" in ack["reason"]
        assert service._readers["hv_2"].written == [], (
            "a setpoint reached a disabled channel")

    def test_a_switched_on_but_UNENERGISED_channel_accepts_a_setpoint(self, service):
        """The rule is about the SWITCH (bit 10), not the output (bit 0).

        This is the state an operator is in between flipping the enable and
        pressing turn-on, and it is exactly when they want to load a setpoint:
        load defaults, read them, adjust, apply, and only then energise.

        The first version of the rule read bit 0 and refused here - which made
        the intended sequence impossible, while looking like a safety feature.
        """
        service._readers["hv_2"].stat = 0        # neither bit: switch on, off

        ack = send(service, value=-1000.0)

        assert ack["ok"] is True
        assert service._readers["hv_2"].written == [1000.0]

    def test_zero_IS_allowed_on_a_disabled_channel(self, service):
        """This is how the invariant gets established on a channel whose
        stored setpoint is currently wrong - which is true of seven of the
        eight channels today."""
        service._readers["hv_2"].stat = DISABLED
        service._readers["hv_2"].vset = 2250.0

        ack = send(service, value=0.0)

        assert ack["ok"] is True
        assert service._readers["hv_2"].written == [0.0]

    def test_an_enabled_channel_accepts_a_voltage(self, service):
        service._readers["hv_2"].stat = ENABLED

        ack = send(service, value=-1000.0)

        assert ack["ok"] is True
        assert ack["new"] == pytest.approx(-1000.0)

    def test_an_unreadable_status_word_refuses_rather_than_assumes(self, service):
        """Not knowing whether a channel is enabled is not permission to
        assume it is."""
        service._readers["hv_2"].answers = False

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "unknown" in ack["reason"]
        assert service._readers["hv_2"].written == []


class TestPolarity:
    """The supplies take an unsigned magnitude and apply their own POL."""

    def test_a_negative_channel_is_written_as_a_magnitude(self, service):
        send(service, channel="hv_cathode_vset", value=-2250.0)

        assert service._readers["hv_2"].written == [2250.0]

    def test_a_positive_value_on_a_negative_channel_is_refused(self, service):
        """`abs()` in the conversion would ask for +2250 and deliver -2250:
        the board applies its own POL, so the sign is discarded, the read-back
        agrees and the audit looks clean.

        Today the RANGE check refuses this first, because every HV channel's
        limits are one-sided (the cathode permits -3500..0). The reason
        reported is therefore the range, not the polarity - which is fine, and
        is asserted here as it actually behaves rather than as one might
        expect. The polarity check is the backstop for a channel whose limits
        are ever made symmetric; `test_scaling.py` covers it directly.
        """
        ack = send(service, channel="hv_cathode_vset", value=2250.0)

        assert ack["ok"] is False
        assert "outside the permitted range" in ack["reason"]
        assert service._readers["hv_2"].written == [], (
            "a positive setpoint reached a negative electrode")

    def test_a_negative_value_on_the_anode_is_refused(self, service):
        ack = send(service, channel="hv_anode_vset", value=-1000.0)

        assert ack["ok"] is False
        assert service._readers["hv_2"].written == []

    def test_the_polarity_backstop_refuses_independently_of_the_range(self):
        """With symmetric limits the range check would let a sign error
        through, and `magnitude_for` is what stops it."""
        from xams_sc.scaling import magnitude_for

        with pytest.raises(ValueError, match="polarity"):
            magnitude_for(2250.0, -1)
        with pytest.raises(ValueError, match="polarity"):
            magnitude_for(-4200.0, 1)


class TestTheSoftwareRange:
    def test_a_value_beyond_the_configured_limit_is_refused(self, service):
        """channels.yaml permits the cathode -3500..0."""
        ack = send(service, channel="hv_cathode_vset", value=-4000.0)

        assert ack["ok"] is False
        assert "outside the permitted range" in ack["reason"]
        assert service._readers["hv_2"].written == []


class TestTheReadBack:
    """`set_voltage` reports that the board acknowledged, not that it obeyed."""

    def test_a_write_that_did_not_take_is_reported_as_failure(self, service):
        service._readers["hv_2"] = FakeReader(stat=ENABLED, obey=False)

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "did not take" in ack["reason"]

    def test_a_board_refusal_is_reported(self, service):
        """A value above the board's own MAXV is refused by the instrument -
        the protection working as intended (§10 rule 2)."""
        service._readers["hv_2"] = FakeReader(
            stat=ENABLED, accepts=False,
            refusal="the supply rejected the value, most likely above MAXV")

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "MAXV" in ack["reason"]

    def test_a_board_in_local_mode_says_so(self, service):
        """The real finding, 18 September 2026: both boards were in LOCAL, so
        every SET was refused with LOC:ERR while every MON kept working.

        The message must name the cause and the remedy. "the supply did not
        accept the command" sent somebody to read a log file; "it is in LOCAL,
        switch the front panel to REMOTE" does not.
        """
        service._readers["hv_2"] = FakeReader(
            stat=ENABLED, accepts=False,
            refusal=("the supply is in LOCAL mode, so it refuses every remote "
                     "setpoint. Put the board in REMOTE at its front panel"))

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "LOCAL" in ack["reason"] and "REMOTE" in ack["reason"]


class TestRefusalsAreRecorded:
    def test_a_rejected_command_is_audited(self, service):
        service._readers["hv_2"].stat = DISABLED
        send(service, value=-1000.0)

        record = service.bus.last_json(TOPIC_AUDIT)
        assert record["result"] == "rejected"
        assert record["action"] == "caen_vset"
        assert record["actor"] == "ap"
        assert record["new"] == "-1000.0"

    def test_a_successful_write_records_both_values(self, service):
        service._readers["hv_2"].vset = 500.0
        send(service, value=-1000.0)

        record = service.bus.last_json(TOPIC_AUDIT)
        assert record["result"] == "ok"
        assert record["old"] == "-500.0"
        assert record["new"] == "-1000.0"

    def test_an_unnamed_command_is_recorded_as_unknown(self, service):
        service._handle_vset(TOPIC_HV_VSET, json.dumps(
            {"channel": "hv_cathode_vset", "value": 0.0}))

        assert service.bus.last_json(TOPIC_AUDIT)["actor"] == "unknown"


class TestBadInput:
    @pytest.mark.parametrize("payload", [
        "{not json",
        '{"value": -100}',                        # no channel
        '{"channel": "hv_cathode_vset"}',         # no value
        '{"channel": "tt401", "value": -100}',    # not an HV setpoint
        '{"channel": "hv_cathode_vmon", "value": -100}',   # a monitor, not a setpoint
    ])
    def test_it_is_refused_without_raising(self, service, payload):
        service._handle_vset(TOPIC_HV_VSET, payload)

        assert service.bus.last_json(ACK_HV_VSET)["ok"] is False
        assert service._readers["hv_2"].written == []

    def test_a_simulating_service_refuses_to_write(self, service):
        """Simulation must never look like a successful write to hardware."""
        service.simulate = True

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "simulating" in ack["reason"]


class TestWhatTheDriverCannotDo:
    """Section 10a: these are absences, and each is deliberate."""

    def test_no_protection_setting_is_ever_written(self):
        """§10 rule 2: ramp rate, trip current and the over-voltage limit stay
        configured on the instrument. That is what keeps this software out of
        the protection path, and it is the reason a bug here cannot raise a
        limit to make room for a mistake."""
        source = (__import__("pathlib").Path(
            __import__("xams_sc.devices.caen", fromlist=["x"]).__file__)
            .read_text(encoding="utf-8"))

        for line in source.splitlines():
            if "CMD:SET" not in line:
                continue
            for forbidden in ("MAXV", "RUP", "RDW", "TRIP", "ISET", "BDCTR"):
                assert forbidden not in line, (
                    "a protection setting is written: %s" % line.strip())


def energise(service, channel="hv_anode_vset", on=True, by="ap"):
    service._handle_output(TOPIC_HV_OUTPUT, json.dumps(
        {"channel": channel, "on": on, "by": by}))
    return service.bus.last_json(ACK_HV_OUTPUT)


class TestEnergisingSaysSoAtOnce:
    """The read-back is published immediately, not left to the next poll.

    The poll reads every second and publishes every ten, so before this the
    web page went on showing a freshly energised channel as off for most of
    the following ten seconds. The operator saw nothing happen and clicked
    again - and by then the button had become "turn off".
    """

    @pytest.fixture
    def service(self, tmp_path):
        svc = CaenService(load(), RecordingBus(), simulate=False,
                          lock_dir=tmp_path)
        # stat 0: the front-panel switch is on, the output is NOT energised -
        # which is exactly the state the turn ON button is offered in.
        svc._readers = {"hv_1": FakeReader(stat=0),
                        "hv_2": FakeReader(stat=0, vset=600.0)}
        return svc

    def test_the_status_word_is_published_without_waiting_for_the_poll(self, service):
        assert energise(service)["ok"] is True
        m = service.bus.measured("hv_anode_stat")
        assert m is not None, "the page still had to wait for the next poll"
        assert int(m.value) & (1 << BIT_ON), "published as not energised"

    def test_it_is_published_as_a_good_reading_of_the_right_channel(self, service):
        energise(service)
        m = service.bus.measured("hv_anode_stat")
        assert m.quality == Quality.OK
        assert m.unit == "bits"
        assert m.raw == m.value

    def test_it_matches_what_the_board_now_reports(self, service):
        energise(service)
        assert service.bus.measured("hv_anode_stat").value == float(
            service._readers["hv_2"].stat)

    def test_the_straddling_window_is_dropped(self, service):
        """A mean of a bitmask is not a bitmask.

        _emit_window averages the window, so samples from both sides of the
        switch average to a fraction, and int(0.4) is 0 - the page would have
        gone back to "not energised" seconds after this said otherwise.
        """
        service._accumulate([Measurement(t=utcnow(), channel="hv_anode_stat",
                                         value=0.0, unit="bits", raw=0.0,
                                         quality=Quality.OK)] * 9)
        energise(service)
        assert "hv_anode_stat" not in service._window

    def test_only_the_channel_commanded_is_republished(self, service):
        energise(service)
        assert service.bus.measured("hv_cathode_stat") is None

    def test_turning_off_publishes_the_read_back_too(self, service):
        service._readers["hv_2"].stat = 1 << BIT_ON
        assert energise(service, on=False)["ok"] is True
        m = service.bus.measured("hv_anode_stat")
        assert m is not None and not int(m.value) & (1 << BIT_ON)

    def test_a_board_refusal_publishes_no_status_at_all(self, service):
        """Nothing changed, so nothing is claimed. A published word here would
        be a fresh timestamp on an unchanged state, which reads as news."""
        service._readers["hv_2"].accepts = False
        assert energise(service)["ok"] is not True
        assert service.bus.measured("hv_anode_stat") is None

    def test_a_disabled_channel_is_refused_and_says_where_the_switch_is(self, service):
        service._readers["hv_2"].stat = 1 << BIT_DISABLED
        answer = energise(service)
        assert answer["ok"] is not True
        assert "front panel" in answer["reason"]
        assert service.bus.measured("hv_anode_stat") is None



class TestASetpointSaysSoAtOnce:
    """The twin of `TestEnergisingSaysSoAtOnce`, for the other control.

    Energising published its read-back immediately and writing a setpoint did
    not, so two buttons on one page behaved differently: the supply switched
    on in front of you, and a setpoint you had just applied went on reading
    as the old voltage until the next publish ten seconds later. Nothing in
    the UI could fix that - the number it renders is the last one published.
    """

    def test_the_setpoint_is_published_without_waiting_for_the_poll(self, service):
        assert send(service, value=-1000.0)["ok"] is True
        m = service.bus.measured("hv_cathode_vset")
        assert m is not None, "the page still had to wait for the next poll"
        assert m.value == -1000.0

    def test_it_is_published_as_a_good_reading_with_the_right_units(self, service):
        send(service, value=-1000.0)
        m = service.bus.measured("hv_cathode_vset")
        assert m.quality == Quality.OK
        assert m.unit == "V"

    def test_the_published_value_is_signed_and_the_raw_is_not(self, service):
        """§7.2: the board reports magnitudes with POL separate, and this
        system stores signed. The poll publishes both that way, and a
        reading published out of cadence must not be the exception."""
        send(service, value=-1000.0)
        m = service.bus.measured("hv_cathode_vset")
        assert m.value == -1000.0
        assert m.raw == 1000.0

    def test_it_matches_what_the_board_now_reports(self, service):
        send(service, value=-1000.0)
        assert abs(service.bus.measured("hv_cathode_vset").value) == \
            service._readers["hv_2"].vset

    def test_the_straddling_window_is_dropped(self, service):
        """A mean of the old setpoint and the new one is a voltage nobody set.

        Worse than the bitmask case it mirrors: it is PLAUSIBLE. The page
        would have shown a number between the two, on the page somebody reads
        to find out where the supply is going.
        """
        service._accumulate([Measurement(t=utcnow(), channel="hv_cathode_vset",
                                         value=-500.0, unit="V", raw=500.0,
                                         quality=Quality.OK)] * 9)
        send(service, value=-1000.0)
        assert "hv_cathode_vset" not in service._window

    def test_only_the_channel_commanded_is_republished(self, service):
        send(service, value=-1000.0)
        assert service.bus.measured("hv_anode_vset") is None

    def test_a_refused_write_publishes_nothing(self, service):
        """Nothing was written, so there is nothing to say. Publishing the
        value that is still there would be harmless; publishing the value
        that was ASKED for would be a lie the page could not detect."""
        service._readers["hv_2"].stat = DISABLED
        assert send(service, value=-1000.0)["ok"] is False
        assert service.bus.measured("hv_cathode_vset") is None

    def test_a_write_that_did_not_take_publishes_nothing(self, service):
        # `obey=False`: the board acknowledges and keeps the old value,
        # which the read-back catches.
        service._readers["hv_2"].obey = False
        assert send(service, value=-1000.0)["ok"] is False
        assert service.bus.measured("hv_cathode_vset") is None


# --------------------------------------------------------------------------
# The invariant is not only REFUSED against, it is ENFORCED. See
# `CaenService._enforce_disabled_zero`.
#
# The state these tests are about cannot be created through the driver. It
# arrives from the front panel: a channel is energised at its working voltage,
# de-energised - which ramps down and leaves VSET alone - and then disabled by
# hand, by somebody with every reason to believe it is off, because it is. The
# board is now holding a disabled channel with kilovolts in its setpoint, and
# the next flip of that switch ramps straight to them.


def audits(service):
    return [json.loads(p) for t, p in service.bus.published if t == TOPIC_AUDIT]


class TestTheInvariantIsEnforcedOnEveryRead:

    def test_a_disabled_channel_with_a_setpoint_is_zeroed(self, service):
        """The case that prompted all of this: off, disabled, VSET -2250."""
        service._readers["hv_2"].stat = DISABLED
        service._readers["hv_2"].vset = 2250.0

        service.read()

        assert service._readers["hv_2"].vset == 0.0
        assert set(service._readers["hv_2"].written) == {0.0}

    def test_a_channel_whose_SWITCH_IS_ON_keeps_its_setpoint(self, service):
        """Bit 10, never bit 0. Switch on and not energised is where an
        operator stands between flipping the enable and pressing turn-on, and
        it is exactly when they load a setpoint. Zeroing here would erase what
        they typed a second after they typed it, once a second, forever."""
        service._readers["hv_1"].stat = 0          # switch on, not energised
        service._readers["hv_1"].vset = 700.0

        service.read()

        assert service._readers["hv_1"].written == []
        assert service._readers["hv_1"].vset == 700.0

    def test_an_energised_channel_is_left_alone(self, service):
        service._readers["hv_1"].stat = ENABLED
        service._readers["hv_1"].vset = 700.0

        service.read()

        assert service._readers["hv_1"].written == []

    def test_a_disabled_channel_already_at_zero_is_not_written_to(self, service):
        """The ordinary resting state of seven channels out of eight. It must
        cost nothing: no write, and no serial traffic beyond the poll."""
        service._readers["hv_2"].stat = DISABLED
        service._readers["hv_2"].vset = 0.0

        service.read()

        assert service._readers["hv_2"].written == []

    def test_one_supply_being_disabled_does_not_touch_the_other(self, service):
        service._readers["hv_1"].stat = ENABLED
        service._readers["hv_1"].vset = 700.0
        service._readers["hv_2"].stat = DISABLED
        service._readers["hv_2"].vset = 2250.0

        service.read()

        assert service._readers["hv_1"].written == []
        assert set(service._readers["hv_2"].written) == {0.0}

    def test_an_unreadable_status_word_changes_nothing(self, service):
        """The same rule as everywhere else in this driver: not knowing
        whether the switch is off is not permission to act as though it is."""
        service._readers["hv_2"].stat = DISABLED
        service._readers["hv_2"].vset = 2250.0
        service._readers["hv_2"].answers = False

        service.read()

        assert service._readers["hv_2"].written == []


class TestWhatTheZeroingPublishes:

    @pytest.fixture
    def service(self, tmp_path):
        svc = CaenService(load(), RecordingBus(), simulate=False,
                          lock_dir=tmp_path)
        svc._readers = {"hv_1": FakeReader(),
                        "hv_2": FakeReader(stat=DISABLED, vset=2250.0)}
        return svc

    def test_the_reading_is_the_new_setpoint_and_not_the_old(self, service):
        """The cycle read -2250 V before the write. Publishing that is
        publishing a setpoint the board no longer holds - and it is the
        reading that goes into the ten-second window, so it would come out as
        an average of a voltage that exists and one that does not."""
        out = service.read()

        cathode = next(m for m in out if m.channel == "hv_cathode_vset")
        assert cathode.value == pytest.approx(0.0)
        assert cathode.quality is Quality.OK

    def test_it_is_published_at_once_rather_than_at_the_next_window(self, service):
        service.read()

        published = [m for m in service.bus.measurements
                     if m.channel == "hv_cathode_vset"]
        assert published and published[-1].value == pytest.approx(0.0)

    def test_it_is_audited_with_nobody_as_the_actor(self, service):
        """The one VSET write in the system with no person behind it. An
        audit entry that cannot be accounted for afterwards is worse than the
        state it was correcting."""
        service.read()

        entry = next(a for a in audits(service)
                     if a["target"] == "hv_cathode_vset")
        assert entry["result"] == "ok"
        assert entry["action"] == "caen_vset"
        assert entry["actor"] == "automatic (section 10a)"
        assert entry["old"].startswith("-2250")
        assert float(entry["new"]) == pytest.approx(0.0)

    def test_no_acknowledgement_is_published(self, service):
        """Acks are matched by channel name, so an ack from a write nobody
        asked for can be handed to an operator's command as the answer to the
        question they actually asked."""
        service.read()

        assert not [t for t, _ in service.bus.published if t == ACK_HV_VSET]


class TestWhenTheZeroingCannotBeDone:

    @pytest.fixture
    def service(self, tmp_path):
        svc = CaenService(load(), RecordingBus(), simulate=False,
                          lock_dir=tmp_path)
        svc._readers = {"hv_1": FakeReader(),
                        "hv_2": FakeReader(stat=DISABLED, vset=2250.0)}
        return svc

    def test_a_board_in_local_mode_is_audited_as_rejected(self, service):
        service._readers["hv_2"].accepts = False
        service._readers["hv_2"].refusal = "LOC:ERR"

        service.read()

        entry = next(a for a in audits(service)
                     if a["target"] == "hv_cathode_vset")
        assert entry["result"] == "rejected"
        assert "LOC:ERR" in entry["detail"]

    def test_it_is_not_retried_on_every_single_poll(self, service):
        """A supply in LOCAL refuses until somebody walks to the front panel.
        Three serial exchanges a second against a board that is going to say
        no crowds out the readings that still work."""
        service._readers["hv_2"].accepts = False

        service.read()
        attempts = len(service._readers["hv_2"].written)
        service.read()
        service.read()

        assert len(service._readers["hv_2"].written) == attempts

    def test_a_write_that_did_not_take_is_not_called_success(self, service):
        """The board acknowledged and kept the old value."""
        service._readers["hv_2"].obey = False

        out = service.read()

        entry = next(a for a in audits(service)
                     if a["target"] == "hv_cathode_vset")
        assert entry["result"] == "rejected"
        cathode = next(m for m in out if m.channel == "hv_cathode_vset")
        assert cathode.value == pytest.approx(-2250.0), (
            "a setpoint that is still on the board was published as zero")

    def test_the_switch_flipped_back_ON_between_the_poll_and_the_write(self, service):
        """A second passes between the poll reading the switch and the write
        going out, and the hand at the front panel is the whole reason this
        rule exists. If the channel has been enabled in that second it may
        legitimately hold a setpoint, and zeroing it would be this rule
        causing exactly the surprise it exists to prevent."""
        reader = service._readers["hv_2"]
        reads = {"n": 0}
        original = reader.status

        def status(channel):
            reads["n"] += 1
            # The poll's reads come first - one per channel in use on hv_2,
            # three since the NaI's channel became the gate's - and the
            # write's re-read finds the switch back on.
            return DISABLED if reads["n"] <= 3 else ENABLED

        reader.status = status

        service.read()

        assert reader.written == []
        assert reader.vset == 2250.0


# ================================================================ switching
#
# 14 October 2026: the row and its button came back before the board had
# got there - OFF keeps STAT bit 0 through the ramp down (and some seconds
# after), ON is "on" at once and ramps up after - and a poll sample taken
# before a command could be published after it.

from datetime import timedelta  # noqa: E402

from xams_sc.bus import TOPIC_HV_SWITCHING  # noqa: E402
from xams_sc.hv_status import BIT_RAMP_DOWN, ramping  # noqa: E402

ON_RAMPING_DOWN = (1 << BIT_ON) | (1 << BIT_RAMP_DOWN)


def stat_sample(word, t=None, channel="hv_anode_stat"):
    return Measurement(t=t or utcnow(), channel=channel, value=float(word),
                       unit="bits", raw=float(word), quality=Quality.OK)


class TestSwitchingIsShownUntilItIsDone:
    @pytest.fixture
    def service(self, tmp_path):
        svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
        nai = FakeReader(stat=1 << BIT_ON, vset=600.0)

        def ramp_down(channel, on):
            # As the board does: OFF starts a ramp down with bit 0 still set.
            nai.outputs.append(on)
            nai.stat = (1 << BIT_ON) | (0 if on else 1 << BIT_RAMP_DOWN)
            return True, ""
        nai.set_output = ramp_down
        svc._readers = {"hv_1": FakeReader(), "hv_2": nai}
        return svc

    def switching(self, service):
        payloads = service.bus.published_on(TOPIC_HV_SWITCHING + "/hv_anode_vset")
        return json.loads(payloads[-1]) if payloads and payloads[-1] else None

    def vmon(self, volts):
        return Measurement(t=utcnow(), channel="hv_anode_vmon", value=volts,
                           unit="V", raw=abs(volts), quality=Quality.OK)

    def test_turning_off_is_switching_until_off_and_at_zero(self, service):
        """14 October 2026, nai at +100 V: STAT went to 0 at 13:23:23.5 with
        VMON still 13.5 V two seconds later."""
        assert energise(service, on=False)["ok"]
        assert self.switching(service)["wanted"] == "off"
        service._accumulate([stat_sample(ON_RAMPING_DOWN), self.vmon(80.0)])
        service._accumulate([stat_sample(1 << BIT_ON), self.vmon(40.0)])  # ramp over, ON
        assert self.switching(service) is not None
        service._accumulate([stat_sample(0), self.vmon(13.5)])  # off, volts still there
        assert self.switching(service) is not None
        service._accumulate([stat_sample(0), self.vmon(0.4)])
        assert self.switching(service) is None

    def test_it_is_given_up_after_the_ramp_time_and_a_margin(self, service):
        energise(service, on=False)
        service._switching["hv_anode_vset"]["_until"] = 0.0
        service._accumulate([stat_sample(1 << BIT_ON)])
        assert self.switching(service) is None

    def test_the_wait_is_the_ramp_time_at_the_configured_rate(self, service):
        channel = next(c for c in service._channels if c.name == "hv_anode_vset")
        spec = next(s for s in service._specs if s["id"] == "hv_2")
        rdw = spec["expect"][int(channel.phys)]["rdw"]
        assert service._off_wait_s(channel, 600.0) == pytest.approx(600.0 / rdw + 60.0)

    def test_turning_on_clears_a_switching_record(self, service):
        energise(service, on=False)
        energise(service, on=True)
        assert self.switching(service) is None

    def test_the_ramp_bits_read_as_up_and_down(self):
        assert ramping(0b011) == "up" and ramping(0b101) == "down"
        assert ramping(0b001) is None


class TestAStalePollSampleCannotUndoACommand:
    @pytest.fixture
    def service(self, tmp_path):
        svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
        svc._readers = {"hv_1": FakeReader(stat=0), "hv_2": FakeReader(stat=0)}
        return svc

    def test_a_sample_taken_before_the_command_is_dropped(self, service):
        before = utcnow() - timedelta(seconds=1)
        energise(service)
        service._accumulate([stat_sample(0, t=before)])
        assert "hv_anode_stat" not in service._window
        assert int(service.bus.measured("hv_anode_stat").value) & (1 << BIT_ON)

    def test_a_sample_taken_after_it_is_kept(self, service):
        energise(service)
        service._accumulate([stat_sample(1 << BIT_ON)])
        assert len(service._window["hv_anode_stat"]) == 1


class TestAStatusChangeIsPublishedAtOnce:
    @pytest.fixture
    def service(self, tmp_path):
        svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
        svc._readers = {"hv_1": FakeReader(), "hv_2": FakeReader()}
        return svc

    def test_a_change_is_published_without_waiting_for_the_window(self, service):
        service._accumulate([stat_sample(0)])
        service._accumulate([stat_sample(1 << BIT_ON)])
        assert [int(m.value) for m in service.bus.measurements
                if m.channel == "hv_anode_stat"] == [0, 1]

    def test_an_unchanged_word_is_not_published_again(self, service):
        for _ in range(5):
            service._accumulate([stat_sample(1 << BIT_ON)])
        assert len([m for m in service.bus.measurements if m.channel == "hv_anode_stat"]) == 1

    def test_a_voltage_is_still_only_averaged(self, service):
        service._accumulate([Measurement(t=utcnow(), channel="hv_anode_vmon", value=600.0,
                                         unit="V", raw=600.0, quality=Quality.OK)])
        assert service.bus.measured("hv_anode_vmon") is None


def test_a_switch_that_worked_says_nothing_green(webui):
    """14 October 2026: the button shows the new state; the green banner only
    pushed the page down. A refusal still comes back in red."""
    from doubles import stub_commands
    http, app, bus = webui
    stub_commands(app)
    r = http.post("/hv/output", data={"channel": "hv_anode_vset", "on": "1"},
                  follow_redirects=False)
    assert r.status_code == 303
    assert "hv_ok" not in r.headers["location"]
