"""What the four command verbs print and return. See DESIGN.md §10, §10a.

`xams-ctl` is what somebody reaches for when the web UI is unreachable, which
is exactly when it must not have a NameError in an error branch — and it was
the least covered module in the system at 15 %.

These are CHARACTERISATION tests: they pin what the commands already do,
written before the duplicated ack protocol underneath them was replaced, so
that the replacement is demonstrably a refactor and not a rewrite. Where they
look pedantic about exact wording, that is the point. An operator reads
"REFUSED: ..." during an intervention and the reason matters more than the
formatting, but the formatting is what tells them which line is which.

The broker is a stand-in and the clock is fake, so a fifteen-second timeout
costs nothing to assert.
"""

from __future__ import annotations

import json

import pytest

from doubles import RecordingBus
from xams_sc import bus as bus_module
from xams_sc.bus import (ACK_HV_OUTPUT, ACK_HV_VSET, ACK_NOTIFY,
                         TOPIC_FLOW_RESET, TOPIC_HV_OUTPUT, TOPIC_HV_VSET,
                         TOPIC_NOTIFY)
from xams_sc.cli import xams_ctl

ACK_FLOW = "xams/ack/derived/flow_reset"


class Clock:
    """A clock that only moves when something sleeps.

    The commands poll with `time.sleep` against a `time.monotonic` deadline,
    so a fake clock makes a timeout instant and — more importantly —
    deterministic. With the real one these tests would take a minute and
    still be timing-dependent.
    """

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def cli(monkeypatch):
    """The CLI wired to a recording bus and a fake clock.

    Returns the bus. `xams_ctl` imports Bus from `..bus` inside each command,
    so the name is looked up on the module at call time and patching it there
    reaches every verb.
    """
    bus = RecordingBus()
    monkeypatch.setattr(bus_module, "Bus", lambda **kw: bus)
    monkeypatch.setattr(xams_ctl, "time", Clock())
    monkeypatch.setenv("USERNAME", "whoever")
    return bus


def answers(bus, ack_topic, reply):
    """Play the service: answer every command with `reply`.

    A callable gets the published payload and returns the ack, so a test can
    answer differently per channel.

    The channel of the command being answered is filled in unless the reply
    names one, because the real service always carries it — every ack in
    `caen.py`, refusals included, is `{"ok": ..., "channel": target, ...}`.
    A double that left it out would be answering in a way the service never
    does, and the discriminator that keeps two operators apart would have
    nothing to match on.
    """
    def on_publish(topic, payload):
        command = json.loads(payload)
        body = reply(command) if callable(reply) else reply
        if body is None:
            return
        body = dict(body)
        if "channel" in command:
            body.setdefault("channel", command["channel"])
        bus.deliver(ack_topic, json.dumps(body))
    bus.on_publish = on_publish


def args(**kw):
    kw.setdefault("broker", "127.0.0.1")
    kw.setdefault("port", 1883)
    kw.setdefault("by", "apc")
    return type("Args", (), kw)()


# =================================================================  hv set

class TestHvSet:
    def test_a_successful_write_reports_the_readback(self, cli, capsys):
        answers(cli, ACK_HV_VSET, lambda cmd: {
            "ok": True, "channel": cmd["channel"], "old": -4000.0,
            "new": cmd["value"]})

        rc = xams_ctl.cmd_hv_set(args(channel="hv_cathode_vset", value=-4200.0))

        out = capsys.readouterr().out
        assert rc == 0
        assert "hv_cathode_vset" in out
        assert "-4000.0 -> -4200.0 V" in out

    def test_the_command_carries_the_operator(self, cli):
        answers(cli, ACK_HV_VSET, {"ok": True, "old": 0.0, "new": -4200.0})

        xams_ctl.cmd_hv_set(args(channel="hv_cathode_vset", value=-4200.0))

        (sent,) = cli.json_on(TOPIC_HV_VSET)
        assert sent == {"channel": "hv_cathode_vset", "value": -4200.0,
                        "by": "apc"}

    def test_the_operator_falls_back_to_the_environment(self, cli):
        answers(cli, ACK_HV_VSET, {"ok": True, "old": 0.0, "new": -4200.0})

        xams_ctl.cmd_hv_set(args(channel="hv_cathode_vset", value=-4200.0,
                                 by=None))

        assert cli.json_on(TOPIC_HV_VSET)[0]["by"] == "whoever"

    def test_a_refusal_is_reported_with_its_reason_and_fails(self, cli, capsys):
        answers(cli, ACK_HV_VSET, {
            "ok": False, "reason": "outside the permitted range"})

        rc = xams_ctl.cmd_hv_set(args(channel="hv_cathode_vset", value=-9000.0))

        out = capsys.readouterr().out
        assert rc == 1
        assert "REFUSED: outside the permitted range" in out

    def test_silence_is_reported_as_no_answer_and_fails(self, cli, capsys):
        answers(cli, ACK_HV_VSET, None)          # the service says nothing

        rc = xams_ctl.cmd_hv_set(args(channel="hv_cathode_vset", value=-4200.0))

        out = capsys.readouterr().out
        assert rc == 1
        assert "NO ANSWER" in out
        assert "caen service" in out

    def test_an_unreachable_broker_sends_nothing(self, cli, capsys):
        cli.connected = False

        rc = xams_ctl.cmd_hv_set(args(channel="hv_cathode_vset", value=-4200.0))

        out = capsys.readouterr().out
        assert rc == 1
        assert "broker not reachable" in out
        assert cli.published == [], "a command went out with no broker"

    def test_a_readback_without_an_old_value_still_prints(self, cli, capsys):
        """The board does not always have a previous value to report."""
        answers(cli, ACK_HV_VSET, {"ok": True, "old": None, "new": -4200.0})

        rc = xams_ctl.cmd_hv_set(args(channel="hv_cathode_vset", value=-4200.0))

        assert rc == 0
        assert "now -4200.0 V" in capsys.readouterr().out


# ============================================================  hv on / off

class TestHvOutput:
    def test_it_energises_every_enabled_channel_by_default(self, cli, capsys):
        answers(cli, ACK_HV_OUTPUT, lambda cmd: {
            "ok": True, "channel": cmd["channel"], "on": cmd["on"]})

        rc = xams_ctl.cmd_hv_on(args(channel=None))

        assert rc == 0
        sent = [c["channel"] for c in cli.json_on(TOPIC_HV_OUTPUT)]
        assert "hv_cathode_vset" in sent and "hv_anode_vset" in sent
        assert all(c["on"] is True for c in cli.json_on(TOPIC_HV_OUTPUT))

    def test_one_named_channel_sends_one_command(self, cli):
        answers(cli, ACK_HV_OUTPUT, {"ok": True, "on": False})

        xams_ctl.cmd_hv_off(args(channel="hv_anode_vset"))

        assert cli.json_on(TOPIC_HV_OUTPUT) == [
            {"channel": "hv_anode_vset", "on": False, "by": "apc"}]

    def test_a_refusal_among_several_is_counted_and_named(self, cli, capsys):
        """A batch that reported "3 of 4 succeeded" without saying which would
        be worse than useless on a rack of electrodes."""
        def reply(cmd):
            if cmd["channel"] == "hv_anode_vset":
                return {"ok": False, "reason": "the front-panel switch is off"}
            return {"ok": True, "channel": cmd["channel"], "on": True}
        answers(cli, ACK_HV_OUTPUT, reply)

        rc = xams_ctl.cmd_hv_on(args(channel=None))

        out = capsys.readouterr().out
        assert rc == 1
        assert "hv_anode_vset" in out
        assert "the front-panel switch is off" in out
        assert "1 of 7 refused or unanswered" in out  # 8 until the NaI was disabled

    def test_the_detail_is_shown_when_the_service_gives_one(self, cli, capsys):
        answers(cli, ACK_HV_OUTPUT, {
            "ok": True, "on": True, "detail": "ramping to +200.0 V"})

        xams_ctl.cmd_hv_on(args(channel="hv_anode_vset"))

        assert "ramping to +200.0 V" in capsys.readouterr().out


# ===========================================================  alarms on/off

class TestNotifications:
    def test_turning_them_off_says_what_that_means(self, cli, capsys):
        answers(cli, ACK_NOTIFY, {"ok": True, "enabled": False})

        rc = xams_ctl.cmd_alarms_off(args())

        out = capsys.readouterr().out
        assert rc == 0
        assert "alarm notifications are OFF (by apc)" in out
        assert "NOBODY WILL BE TOLD" in out
        assert "still evaluated, published and recorded" in out

    def test_turning_them_on_lists_what_is_still_in_alarm(self, cli, capsys):
        answers(cli, ACK_NOTIFY, {
            "ok": True, "enabled": True, "active": ["pmain", "t_cryostat"]})

        rc = xams_ctl.cmd_alarms_on(args())

        out = capsys.readouterr().out
        assert rc == 0
        assert "alarm notifications are ON (by apc)" in out
        assert "2 channel(s) still in alarm" in out
        assert "pmain" in out and "t_cryostat" in out

    def test_the_command_carries_the_wanted_state(self, cli):
        answers(cli, ACK_NOTIFY, {"ok": True, "enabled": True})

        xams_ctl.cmd_alarms_on(args())

        assert cli.json_on(TOPIC_NOTIFY) == [{"enabled": True, "by": "apc"}]

    def test_silence_changes_nothing_and_fails(self, cli, capsys):
        answers(cli, ACK_NOTIFY, None)

        rc = xams_ctl.cmd_alarms_off(args())

        out = capsys.readouterr().out
        assert rc == 1
        assert "NO ANSWER" in out and "Nothing was changed" in out


# =============================================================  flow reset

class TestFlowReset:
    def test_a_closed_period_is_reported_in_full(self, cli, capsys):
        answers(cli, ACK_FLOW, {
            "ok": True, "start": "2026-08-01T09:14:00Z",
            "stop": "2026-09-16T11:02:00Z", "total_g": 4213.8, "gaps_s": 0.0})

        rc = xams_ctl.cmd_flow_reset(args())

        out = capsys.readouterr().out
        assert rc == 0
        assert "2026-08-01T09:14:00Z" in out and "2026-09-16T11:02:00Z" in out
        assert "4213.800 g" in out
        assert "A new period is now open" in out

    def test_gaps_are_called_out_as_an_underestimate(self, cli, capsys):
        """The total must carry that evidence rather than requiring somebody
        to reconstruct it from service logs months later."""
        answers(cli, ACK_FLOW, {
            "ok": True, "start": "x", "stop": "y",
            "total_g": 118.4, "gaps_s": 7200.0})

        xams_ctl.cmd_flow_reset(args())

        out = capsys.readouterr().out
        assert "7200 s" in out
        assert "underestimate" in out

    def test_no_gaps_means_no_caveat(self, cli, capsys):
        answers(cli, ACK_FLOW, {"ok": True, "start": "x", "stop": "y",
                                "total_g": 10.0, "gaps_s": 0.0})

        xams_ctl.cmd_flow_reset(args())

        assert "underestimate" not in capsys.readouterr().out

    def test_the_command_carries_the_operator(self, cli):
        answers(cli, ACK_FLOW, {"ok": True, "start": "x", "stop": "y",
                                "total_g": 0.0, "gaps_s": 0.0})

        xams_ctl.cmd_flow_reset(args())

        assert cli.json_on(TOPIC_FLOW_RESET) == [{"by": "apc"}]

    def test_silence_resets_nothing_and_fails(self, cli, capsys):
        answers(cli, ACK_FLOW, None)

        rc = xams_ctl.cmd_flow_reset(args())

        out = capsys.readouterr().out
        assert rc == 1
        assert "did not acknowledge" in out
        assert "Nothing has been reset" in out
