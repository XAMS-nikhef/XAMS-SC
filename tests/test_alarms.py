"""Alarm engine, flight recorder and flow integrator. See DESIGN.md §11, §9.1, §7.5.

These cover milestone 6's acceptance criteria: thresholds from alarms.yaml,
staleness alarms firing, a flight-recorder dump on a test alarm, and the
integrator surviving a restart without losing its total.
"""

import json
import pathlib
import tempfile
import time

from doubles import RecordingBus
from xams_sc.alarms.engine import AlarmEngine
from xams_sc.alarms.flight_recorder import FlightRecorder
from xams_sc.alarms.notify import Notifier, _mask
from xams_sc.config import load
from xams_sc.model import Measurement, Quality, utcnow


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text, channels, rich=None):
        # `rich` is (subject, html, text) for the email, built by the engine so
        # an alarm mail carries the plant around it. SMS keeps the short text.
        # Accepted here because the real Notifier accepts it: a double that
        # lags the interface passes tests the real object would fail.
        self.sent.append((text, tuple(channels)))
        self.rich = rich
        return {"email": 1}


def engine_with(thresholds, notify_state=None, **defaults):
    cfg = load()
    cfg.alarms = {
        "defaults": {"hysteresis": 0.02, "min_repeat_minutes": 15,
                     "stale_after_seconds": 60, **defaults},
        "channels": thresholds,
        "staleness": {"severity": "major", "notify": ["email"]},
    }
    bus, notifier = RecordingBus(), RecordingNotifier()
    # ALWAYS an isolated path for the master switch (§4.4a). The default is
    # `data/alarm_notify.json` in the working directory, and an engine built
    # in a checkout where somebody has alarms switched off would start every
    # test with notifications disabled - silently turning the notification
    # assertions below into assertions about nothing.
    engine = AlarmEngine(
        bus, cfg, notifier=notifier,
        notify_state_path=notify_state or (_ISOLATED / "alarm_notify.json"),
        armed_state_path=_ISOLATED / "alarm_armed.json")
    return engine, bus, notifier


# A directory no test writes to, so the switch always starts in its default
# position (on) unless a test says otherwise.
_ISOLATED = pathlib.Path(tempfile.gettempdir()) / "xams-tests-no-such-dir"


def reading(channel, value, quality=Quality.OK):
    return Measurement(t=utcnow(), channel=channel, value=value,
                       unit="bar", quality=quality)


class TestThresholds:
    THRESHOLDS = {"pmain": {
        "low": {"value": 0.9, "severity": "minor"},
        "high": {"value": 1.8, "severity": "minor"},
        "hihi": {"value": 2.0, "severity": "major", "notify": ["sms", "email"]},
    }}

    def test_a_normal_value_raises_nothing(self):
        engine, _, notifier = engine_with(self.THRESHOLDS)
        engine.on_measurement(reading("pmain", 1.5))
        assert notifier.sent == []
        assert engine.active() == []

    def test_crossing_high_raises_minor(self):
        engine, _, notifier = engine_with(self.THRESHOLDS)
        engine.on_measurement(reading("pmain", 1.9))
        active = engine.active()
        assert len(active) == 1
        assert active[0].state == "minor" and active[0].threshold == "high"
        assert len(notifier.sent) == 1

    def test_most_severe_threshold_wins(self):
        """A value past hihi must report hihi, not high."""
        engine, _, notifier = engine_with(self.THRESHOLDS)
        engine.on_measurement(reading("pmain", 2.5))
        assert engine.active()[0].threshold == "hihi"
        assert engine.active()[0].state == "major"

    def test_low_threshold_compares_downwards(self):
        engine, _, _ = engine_with(self.THRESHOLDS)
        engine.on_measurement(reading("pmain", 0.5))
        assert engine.active()[0].threshold == "low"

    def test_severity_routing_comes_from_the_threshold(self):
        engine, _, notifier = engine_with(self.THRESHOLDS)
        engine.on_measurement(reading("pmain", 2.5))
        _, channels = notifier.sent[0]
        assert channels == ("sms", "email")

    def test_recovery_clears_the_alarm(self):
        engine, _, _ = engine_with(self.THRESHOLDS)
        engine.on_measurement(reading("pmain", 2.5))
        engine.on_measurement(reading("pmain", 1.0))
        assert engine.active() == []

    def test_a_channel_with_no_thresholds_never_trips_on_value(self):
        engine, _, notifier = engine_with({})
        engine.on_measurement(reading("tt206", -90.0))
        assert notifier.sent == []


class TestHysteresis:
    """A channel sitting on a limit must not produce a stream of alarms."""

    THRESHOLDS = {"pmain": {"high": {"value": 1.0, "severity": "minor"}}}

    def test_value_just_below_the_limit_stays_in_alarm(self):
        engine, _, _ = engine_with(self.THRESHOLDS, hysteresis=0.10)
        engine.on_measurement(reading("pmain", 1.05))    # trips
        assert engine.active()
        # 0.95 is below the limit but inside the 10% hysteresis band, so the
        # alarm must NOT clear yet.
        engine.on_measurement(reading("pmain", 0.95))
        assert engine.active(), "cleared inside the hysteresis band"

    def test_clear_recovery_does_clear(self):
        engine, _, _ = engine_with(self.THRESHOLDS, hysteresis=0.10)
        engine.on_measurement(reading("pmain", 1.05))
        engine.on_measurement(reading("pmain", 0.5))
        assert engine.active() == []

    def test_chattering_produces_one_notification_not_many(self):
        engine, _, notifier = engine_with(self.THRESHOLDS, hysteresis=0.10)
        for value in (1.01, 0.99, 1.01, 0.99, 1.01):
            engine.on_measurement(reading("pmain", value))
        assert len(notifier.sent) == 1, (
            f"chatter produced {len(notifier.sent)} notifications")


class TestQualityIsAnAlarm:
    def test_error_quality_raises(self):
        """A sensor that is not reporting is not a sensor that is fine."""
        engine, _, notifier = engine_with({})
        engine.on_measurement(reading("tt103", None, Quality.ERROR))
        assert engine.active()[0].state == "major"
        assert len(notifier.sent) == 1

    def test_recovery_from_error_clears(self):
        engine, _, _ = engine_with({})
        engine.on_measurement(reading("tt103", None, Quality.ERROR))
        engine.on_measurement(reading("tt103", -60.0))
        assert engine.active() == []


class TestStaleness:
    def test_a_channel_that_stops_arriving_alarms(self):
        engine, _, notifier = engine_with({}, stale_after_seconds=0.05)
        engine.on_measurement(reading("pmain", 1.5))
        assert engine.active() == []
        time.sleep(0.1)
        engine.check_staleness()
        active = engine.active()
        assert len(active) == 1
        assert active[0].threshold == "staleness"
        assert len(notifier.sent) == 1

    def test_staleness_clears_when_data_returns(self):
        engine, _, _ = engine_with({}, stale_after_seconds=0.05)
        engine.on_measurement(reading("pmain", 1.5))
        time.sleep(0.1)
        engine.check_staleness()
        assert engine.active()
        engine.on_measurement(reading("pmain", 1.5))
        assert engine.active() == []

    def test_a_fresh_channel_is_not_stale(self):
        engine, _, _ = engine_with({}, stale_after_seconds=60)
        engine.on_measurement(reading("pmain", 1.5))
        engine.check_staleness()
        assert engine.active() == []


class TestAcknowledgeAndSilence:
    THRESHOLDS = {"pmain": {"high": {"value": 1.0, "severity": "minor"}}}

    def test_acknowledge_stops_repeats_but_keeps_the_alarm_active(self):
        """Acknowledging is not fixing, and the two must not look alike."""
        engine, _, notifier = engine_with(self.THRESHOLDS, min_repeat_minutes=0)
        engine.on_measurement(reading("pmain", 2.0))
        assert engine.acknowledge("pmain")
        before = len(notifier.sent)
        for _ in range(5):
            engine.on_measurement(reading("pmain", 2.0))
        assert len(notifier.sent) == before, "acknowledged alarm kept notifying"
        assert engine.active(), "acknowledging must not clear the condition"

    def test_escalation_clears_an_acknowledgement(self):
        """Acknowledging 'high' must not silence the 'hihi' that follows."""
        thresholds = {"pmain": {
            "high": {"value": 1.0, "severity": "minor"},
            "hihi": {"value": 2.0, "severity": "major"}}}
        engine, _, notifier = engine_with(thresholds)
        engine.on_measurement(reading("pmain", 1.5))
        engine.acknowledge("pmain")
        before = len(notifier.sent)
        engine.on_measurement(reading("pmain", 2.5))
        assert len(notifier.sent) > before, "escalation was silently swallowed"
        assert engine.active()[0].acknowledged is False

    def test_silence_suppresses_for_a_while(self):
        engine, _, notifier = engine_with(self.THRESHOLDS, min_repeat_minutes=0)
        engine.on_measurement(reading("pmain", 2.0))
        engine.silence("pmain", minutes=10)
        before = len(notifier.sent)
        for _ in range(3):
            engine.on_measurement(reading("pmain", 2.0))
        assert len(notifier.sent) == before


class TestPublishedState:
    def test_alarm_state_is_published(self):
        engine, bus, _ = engine_with(
            {"pmain": {"high": {"value": 1.0, "severity": "minor"}}})
        engine.on_measurement(reading("pmain", 2.0))
        topics = [t for t, _ in bus.published]
        assert "xams/alarm/pmain" in topics
        payload = json.loads(dict(bus.published)["xams/alarm/pmain"])
        assert payload["state"] == "minor"
        assert payload["threshold"] == "high"

    def test_limits_are_published_on_start(self):
        """The real risk is believing a threshold is 2.0 when it is 20."""
        engine, bus, _ = engine_with(
            {"pmain": {"hihi": {"value": 2.0, "severity": "major"}}})
        engine._publish_limits()
        payload = json.loads(dict(bus.published)["xams/status/limits"])
        assert payload["pmain"]["hihi"] == 2.0


class TestFlightRecorder:
    def test_dump_writes_the_buffer(self, tmp_path):
        rec = FlightRecorder(RecordingBus(), directory=tmp_path)
        for i in range(50):
            rec.record(reading("pmain", 1.0 + i * 0.01))
        path = rec.dump("hihi", "pmain")
        assert path is not None and path.exists()

        lines = path.read_text(encoding="utf-8").strip().split("\n")
        header = json.loads(lines[0])
        assert header["meta"]["reason"] == "hihi"
        assert header["meta"]["channel"] == "pmain"
        assert header["meta"]["measurements"] == 50
        assert len(lines) == 51

    def test_the_buffer_is_bounded(self):
        """The guard against an incident must not create a memory leak."""
        rec = FlightRecorder(RecordingBus(), window_s=10, expected_rate_hz=1, channels=2)
        for _ in range(1000):
            rec.record(reading("pmain", 1.0))
        assert rec.depth <= 20

    def test_dump_on_alarm_captures_the_approach(self, tmp_path):
        """The point of the recorder: the run-up to the alarm, not just the
        alarm. The present system has only averaged values afterwards."""
        rec = FlightRecorder(RecordingBus(), directory=tmp_path)
        for value in (1.0, 1.2, 1.5, 1.8, 2.1):
            rec.record(reading("pmain", value))

        cfg = load()
        cfg.alarms = {"defaults": {}, "channels": {
            "pmain": {"hihi": {"value": 2.0, "severity": "major"}}}}
        engine = AlarmEngine(RecordingBus(), cfg, notifier=RecordingNotifier(),
                             on_alarm=lambda ch, sev, thr: rec.dump(thr or sev, ch))
        engine.on_measurement(reading("pmain", 2.1))

        dumps = list(tmp_path.glob("*.jsonl"))
        assert len(dumps) == 1
        values = [json.loads(line)["v"]
                  for line in dumps[0].read_text(encoding="utf-8").strip().split("\n")[1:]]
        assert values == [1.0, 1.2, 1.5, 1.8, 2.1]

    def test_empty_buffer_dumps_nothing(self, tmp_path):
        rec = FlightRecorder(RecordingBus(), directory=tmp_path)
        assert rec.dump("test") is None


class TestNotifier:
    def test_no_enabled_recipients_is_reported_not_silent(self, caplog):
        """An alarm reaching nobody must be loud about it (§4.4)."""
        notifier = Notifier({}, lambda: [{"name": "x", "enabled": False}])
        with caplog.at_level("ERROR"):
            result = notifier.send("test", ["email"])
        assert result == {}
        assert any("NOT DELIVERED" in r.message for r in caplog.records)

    def test_recipients_are_read_at_send_time(self):
        """recipients.yaml is edited from the web UI and applied without a
        restart, so the list cannot be captured at construction."""
        people = []
        notifier = Notifier({}, lambda: people)
        notifier.send("first", ["email"])          # nobody yet
        people.append({"name": "a", "email": "a@b.c", "enabled": True})
        result = notifier.send("second", ["email"])
        assert result != {}, "a recipient added later was not picked up"

    def test_phone_numbers_are_masked_in_logs(self):
        assert _mask("+31612345678") == "********5678"
        assert "612345" not in _mask("+31612345678")

    def test_missing_credentials_disable_rather_than_crash(self):
        notifier = Notifier({}, lambda: [
            {"name": "a", "phone": "+31600000000", "email": "a@b.c",
             "enabled": True}])
        # No secrets configured at all: must return cleanly, not raise.
        result = notifier.send("test", ["sms", "email"])
        assert result["sms"] == 0 and result["email"] == 0


class TestTheMasterSwitch:
    """Turning alarm delivery off for the whole system. See DESIGN.md §4.4a.

    The dangerous control, and the reason it is allowed to exist: the slow
    control runs when the plant does not, and a fortnight of 3am messages
    about a cryostat nobody is cooling is how an operator learns to ignore the
    one that matters. The alternative ways of getting that quiet — stopping
    the service, emptying the recipient list, widening a threshold — all leave
    the system looking armed when it is not.

    So the rule this class exists to hold: **it stops delivery and nothing
    else.** Evaluation, publication, the recorded history and the flight
    recorder all carry on, and the system says loudly that it is off.
    """

    THRESHOLDS = {"pmain": {
        "hihi": {"value": 2.0, "severity": "major", "notify": ["sms", "email"]},
    }}

    def test_nobody_is_notified_while_it_is_off(self, tmp_path):
        engine, _, notifier = engine_with(
            self.THRESHOLDS, notify_state=tmp_path / "notify.json")
        engine.set_notifications(False, "apc")
        engine.on_measurement(reading("pmain", 2.5))
        assert notifier.sent == []

    def test_the_alarm_still_happens(self, tmp_path):
        """Off is not blind. The page, the history and the plots must not
        lose an alarm because nobody was woken up for it."""
        engine, bus, _ = engine_with(
            self.THRESHOLDS, notify_state=tmp_path / "notify.json")
        engine.set_notifications(False, "apc")
        engine.on_measurement(reading("pmain", 2.5))

        active = engine.active()
        assert len(active) == 1
        assert active[0].state == "major" and active[0].threshold == "hihi"
        published = [json.loads(p) for t, p in bus.published
                     if t == "xams/alarm/pmain"]
        assert published and published[-1]["state"] == "major"

    def test_the_flight_recorder_still_dumps(self, tmp_path):
        """The data around an alarm is worth most when nobody was told."""
        dumps = []
        cfg = load()
        cfg.alarms = {"defaults": {}, "channels": self.THRESHOLDS,
                      "staleness": {}}
        engine = AlarmEngine(
            RecordingBus(), cfg, notifier=RecordingNotifier(),
            on_alarm=lambda c, s, t: dumps.append((c, s, t)),
            notify_state_path=tmp_path / "notify.json")
        engine.set_notifications(False, "apc")
        engine.on_measurement(reading("pmain", 2.5))
        assert dumps == [("pmain", "major", "hihi")]

    def test_turning_it_back_on_does_not_stay_quiet(self, tmp_path):
        """Whatever is still wrong announces itself on its next reading.

        `last_notified` is not stamped while the switch is off, so the repeat
        interval is not silently waited out in the dark.
        """
        engine, _, notifier = engine_with(
            self.THRESHOLDS, notify_state=tmp_path / "notify.json")
        engine.set_notifications(False, "apc")
        engine.on_measurement(reading("pmain", 2.5))
        assert notifier.sent == []

        engine.set_notifications(True, "apc")
        engine.on_measurement(reading("pmain", 2.6))
        assert len(notifier.sent) == 1

    def test_it_says_what_is_still_wrong_when_switched_on(self, tmp_path):
        engine, _, _ = engine_with(
            self.THRESHOLDS, notify_state=tmp_path / "notify.json")
        engine.set_notifications(False, "apc")
        engine.on_measurement(reading("pmain", 2.5))
        assert engine.set_notifications(True, "apc")["active"] == ["pmain"]

    def test_the_switch_is_published_retained(self, tmp_path):
        """A page connecting later must see it without waiting for a change."""
        engine, bus, _ = engine_with(
            self.THRESHOLDS, notify_state=tmp_path / "notify.json")
        engine.set_notifications(False, "apc")
        payloads = [json.loads(p) for t, p in bus.published
                    if t == "xams/status/notify"]
        assert payloads[-1]["enabled"] is False
        assert payloads[-1]["by"] == "apc"
        assert payloads[-1]["at"]

    def test_a_restart_does_not_re_arm_the_alarms(self, tmp_path):
        """The more dangerous default, and the right one.

        Nobody asked for them back. A service that quietly re-armed on a
        restart would send the 3am message the switch was thrown to stop.
        """
        path = tmp_path / "notify.json"
        engine, _, _ = engine_with(self.THRESHOLDS, notify_state=path)
        engine.set_notifications(False, "apc")

        restarted, _, notifier = engine_with(self.THRESHOLDS, notify_state=path)
        assert restarted.notifications_enabled is False
        assert restarted.notify_changed_by == "apc"
        restarted.on_measurement(reading("pmain", 2.5))
        assert notifier.sent == []

    def test_a_missing_file_means_alarms_are_on(self, tmp_path):
        engine, _, _ = engine_with(self.THRESHOLDS,
                                   notify_state=tmp_path / "never-written.json")
        assert engine.notifications_enabled is True

    def test_an_unreadable_file_means_alarms_are_on(self, tmp_path):
        """Garbage on disk must not be able to disarm the alarms."""
        path = tmp_path / "notify.json"
        path.write_text("{not json", encoding="utf-8")
        engine, _, _ = engine_with(self.THRESHOLDS, notify_state=path)
        assert engine.notifications_enabled is True

    def test_switching_to_the_state_it_is_in_is_harmless(self, tmp_path):
        engine, _, _ = engine_with(self.THRESHOLDS,
                                   notify_state=tmp_path / "notify.json")
        first = engine.set_notifications(False, "apc")
        second = engine.set_notifications(False, "apc")
        assert first["was"] is True and second["was"] is False
        assert second["ok"] and engine.notifications_enabled is False


class TestDisabledChannels:
    """A channel set `enabled: false` in channels.yaml raises nothing.

    28 September 2026: the NI 9226 readout failed and tt201..tt207 held six
    permanent major alarms. Disabling them in channels.yaml has to end those,
    and must not turn them into staleness alarms once the driver goes quiet.
    """

    @staticmethod
    def disable(engine, name):
        import dataclasses
        cfg = engine.config
        cfg.channels = dict(cfg.channels)
        cfg.channels[name] = dataclasses.replace(cfg.channels[name],
                                                 enabled=False)
        return cfg

    def test_a_disabled_channel_raises_nothing(self):
        engine, _, notifier = engine_with({})
        self.disable(engine, "tt103")

        engine.on_measurement(reading("tt103", None, Quality.ERROR))

        assert engine.active() == []
        assert notifier.sent == []

    def test_disabling_withdraws_the_alarm_on_reload(self):
        engine, bus, _ = engine_with({})
        engine.on_measurement(reading("tt103", None, Quality.ERROR))
        assert [s.channel for s in engine.active()] == ["tt103"]

        engine.reload(self.disable(engine, "tt103"))

        assert engine.active() == []
        assert bus.last_json("xams/alarm/tt103")["state"] == "ok"

    def test_it_does_not_come_back_as_stale(self):
        engine, _, _ = engine_with({})
        engine.on_measurement(reading("tt103", None, Quality.ERROR))
        engine.reload(self.disable(engine, "tt103"))

        engine._states.get("tt103") and setattr(
            engine._states["tt103"], "last_seen", 0.0)
        engine.check_staleness()

        assert engine.active() == []

    def test_a_retained_alarm_is_withdrawn_after_a_restart(self):
        """A fresh engine never sees the disabled channel, so without this
        the alarm left on the broker would show on every page forever."""
        engine, bus, _ = engine_with({})
        self.disable(engine, "tt103")

        engine._on_retained_alarm("xams/alarm/tt103", json.dumps(
            {"state": "major", "threshold": "quality=error"}))

        assert bus.last_json("xams/alarm/tt103")["state"] == "ok"

    def test_its_own_ok_does_not_loop(self):
        engine, bus, _ = engine_with({})
        self.disable(engine, "tt103")

        engine._on_retained_alarm("xams/alarm/tt103", json.dumps({"state": "ok"}))

        assert bus.published_on("xams/alarm/tt103") == []

    def test_an_enabled_channel_is_left_alone(self):
        engine, bus, _ = engine_with({})

        engine._on_retained_alarm("xams/alarm/tt103", json.dumps(
            {"state": "major"}))

        assert bus.published_on("xams/alarm/tt103") == []


class TestOneAlarmSwitchedOff:
    """alarms.yaml `switchable`: one alarm off on its own, for an expected
    condition (the LN2 dewar empty or away). The same rule as the master
    switch - delivery only - and only for the channels listed."""

    THRESHOLDS = {
        "ln2_mass": {"low": {"value": 100.0, "severity": "minor"},
                     "lolo": {"value": 20.0, "severity": "major",
                              "notify": ["sms", "email"]}},
        "pmain": {"hihi": {"value": 2.0, "severity": "major"}},
    }

    def engine(self, tmp_path, armed_path=None):
        cfg = load()
        cfg.alarms = {"defaults": {}, "channels": self.THRESHOLDS,
                      "staleness": {}, "switchable": ["ln2_mass"]}
        bus, notifier = RecordingBus(), RecordingNotifier()
        engine = AlarmEngine(bus, cfg, notifier=notifier,
                             notify_state_path=tmp_path / "notify.json",
                             armed_state_path=armed_path or tmp_path / "armed.json")
        return engine, bus, notifier

    def kg(self, value):
        return Measurement(t=utcnow(), channel="ln2_mass", value=value,
                           unit="kg", quality=Quality.OK)

    def test_on_by_default(self, tmp_path):
        engine, _, notifier = self.engine(tmp_path)
        assert engine.is_armed("ln2_mass")
        engine.on_measurement(self.kg(10.0))
        assert len(notifier.sent) == 1

    def test_off_tells_nobody_but_the_alarm_still_happens(self, tmp_path):
        engine, bus, notifier = self.engine(tmp_path)
        assert engine.set_armed("ln2_mass", False, "apc")["ok"]
        engine.on_measurement(self.kg(10.0))
        assert notifier.sent == []
        assert [s.channel for s in engine.active()] == ["ln2_mass"]
        published = [json.loads(p) for t, p in bus.published
                     if t == "xams/alarm/ln2_mass"]
        assert published[-1]["state"] == "major"

    def test_other_alarms_are_untouched(self, tmp_path):
        engine, _, notifier = self.engine(tmp_path)
        engine.set_armed("ln2_mass", False, "apc")
        engine.on_measurement(reading("pmain", 2.5))
        assert len(notifier.sent) == 1

    def test_a_channel_not_listed_cannot_be_switched(self, tmp_path):
        """Not a way to silence pmain one request at a time."""
        engine, _, notifier = self.engine(tmp_path)
        answer = engine.set_armed("pmain", False, "apc")
        assert not answer["ok"] and "not a switchable" in answer["reason"]
        engine.on_measurement(reading("pmain", 2.5))
        assert len(notifier.sent) == 1

    def test_switching_back_on_is_not_quiet(self, tmp_path):
        engine, _, notifier = self.engine(tmp_path)
        engine.set_armed("ln2_mass", False, "apc")
        engine.on_measurement(self.kg(10.0))
        answer = engine.set_armed("ln2_mass", True, "apc")
        assert answer["state"] == "major"
        engine.on_measurement(self.kg(9.0))
        assert len(notifier.sent) == 1

    def test_a_restart_does_not_re_arm_it(self, tmp_path):
        engine, _, _ = self.engine(tmp_path)
        engine.set_armed("ln2_mass", False, "apc")
        again, _, notifier = self.engine(tmp_path)
        assert not again.is_armed("ln2_mass")
        again.on_measurement(self.kg(10.0))
        assert notifier.sent == []

    def test_a_stale_switch_file_cannot_silence_an_unlisted_channel(self, tmp_path):
        path = tmp_path / "armed.json"
        path.write_text(json.dumps({"pmain": {"enabled": False}}))
        engine, _, notifier = self.engine(tmp_path, armed_path=path)
        engine.on_measurement(reading("pmain", 2.5))
        assert len(notifier.sent) == 1

    def test_published_retained_for_the_pages(self, tmp_path):
        engine, bus, _ = self.engine(tmp_path)
        engine.set_armed("ln2_mass", False, "apc")
        last = [json.loads(p) for t, p in bus.published
                if t == "xams/status/armed"][-1]
        assert last["ln2_mass"]["enabled"] is False
        assert last["ln2_mass"]["by"] == "apc"


# ================================================================ unreadable
#
# 28 October 2026: the RGA missed a few HTTP requests a dozen times; each time
# all eight rga_* channels went major (eight dumps) and back 20 s later.

RGA = ["rga_status", "rga_emission", "rga_em", "rga_control", "rga_hw_errors",
       "rga_hw_warnings", "rga_tp_trips", "rga_box_temp"]


def rga_engine(after=180.0, **defaults):
    engine, bus, notifier = engine_with({}, **defaults)
    alarms = dict(engine.config.alarms)
    alarms["unreadable"] = {"rga": {"channels": RGA, "alarm_on": "rga_status",
                                    "after_seconds": after, "severity": "minor",
                                    "notify": ["email"]}}
    engine.config.alarms = alarms
    engine.reload(engine.config)
    return engine, bus, notifier


def rga_outage(engine):
    for ch in RGA:
        engine.on_measurement(reading(ch, None, Quality.ERROR))


class TestAnUnreadableGroup:
    def test_a_short_outage_raises_nothing(self):
        engine, _, notifier = rga_engine(after=180)
        rga_outage(engine)
        assert engine.active() == [] and notifier.sent == []
        for ch in RGA:
            engine.on_measurement(reading(ch, 0.0))
        assert engine.active() == []

    def test_a_long_one_is_one_minor_alarm_by_email(self):
        engine, _, notifier = rga_engine(after=0.05)
        rga_outage(engine)
        time.sleep(0.1)
        rga_outage(engine)
        (alarm,) = engine.active()
        assert alarm.channel == "rga_status" and alarm.state == "minor"
        assert notifier.sent and notifier.sent[0][1] == ("email",)

    def test_it_clears_when_the_rga_answers_again(self):
        engine, _, _ = rga_engine(after=0.05)
        rga_outage(engine)
        time.sleep(0.1)
        rga_outage(engine)
        engine.on_measurement(reading("rga_status", 17828752.0))
        assert engine.active() == []

    def test_the_delay_restarts_after_a_good_reading(self):
        engine, _, _ = rga_engine(after=0.2)
        rga_outage(engine)
        time.sleep(0.15)
        engine.on_measurement(reading("rga_status", 1.0))
        rga_outage(engine)
        time.sleep(0.1)
        rga_outage(engine)
        assert engine.active() == []

    def test_silence_is_one_alarm_too_and_only_after_the_delay(self):
        engine, _, _ = rga_engine(after=0.2, stale_after_seconds=0.05)
        for ch in RGA:
            engine.on_measurement(reading(ch, 0.0))
        time.sleep(0.1)
        engine.check_staleness()
        assert engine.active() == []           # stale, but not yet 0.2 s
        time.sleep(0.15)
        engine.check_staleness()
        (alarm,) = engine.active()
        assert alarm.channel == "rga_status" and alarm.threshold == "staleness"

    def test_other_channels_keep_the_default(self):
        engine, _, _ = rga_engine()
        engine.on_measurement(reading("tt103", None, Quality.ERROR))
        assert engine.active()[0].state == "major"

    def test_an_old_unreadable_alarm_on_a_member_is_withdrawn(self):
        engine, _, _ = engine_with({})
        engine.on_measurement(reading("rga_box_temp", None, Quality.ERROR))
        assert engine.active()[0].channel == "rga_box_temp"
        alarms = dict(engine.config.alarms)
        alarms["unreadable"] = {"rga": {"channels": RGA, "alarm_on": "rga_status",
                                        "after_seconds": 180, "severity": "minor"}}
        engine.config.alarms = alarms
        engine.reload(engine.config)
        engine.on_measurement(reading("rga_box_temp", None, Quality.ERROR))
        assert engine.active() == []


class TestTheConfiguredRgaRule:
    def test_alarms_yaml_has_it(self):
        rule = load().alarms["unreadable"]["rga"]
        assert rule["alarm_on"] == "rga_status" and rule["severity"] == "minor"
        assert rule["after_seconds"] >= 120 and rule["notify"] == ["email"]
        assert set(rule["channels"]) == set(RGA)

    def test_a_typo_in_a_group_stops_the_load(self, tmp_path):
        import shutil
        import pytest
        from xams_sc.config import CONFIG_DIR, ConfigError
        for f in CONFIG_DIR.glob("*.yaml"):
            shutil.copy(f, tmp_path / f.name)
        text = (tmp_path / "alarms.yaml").read_text(encoding="utf-8")
        (tmp_path / "alarms.yaml").write_text(
            text.replace("rga_box_temp]", "rga_box_tmp]"), encoding="utf-8")
        with pytest.raises(ConfigError, match="rga_box_tmp"):
            load(tmp_path)
