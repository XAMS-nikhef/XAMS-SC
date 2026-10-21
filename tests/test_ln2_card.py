"""The LN2 dewar tile on the Control page and its alarm switch."""

import json

from doubles import stub_commands
from xams_sc.model import iso, utcnow


def measure(bus, channel, value, unit="kg"):
    bus.deliver(f"xams/meas/{channel}",
                json.dumps({"t": iso(utcnow()), "ch": channel, "v": value,
                            "u": unit, "q": "ok"}))


def armed(bus, enabled, by="apc"):
    bus.deliver("xams/status/armed", json.dumps(
        {"ln2_mass": {"enabled": enabled, "by": by,
                      "at": "2026-10-21T14:00:00Z"}}))


def test_three_tiles_in_the_top_row(client):
    html = client.get("/hv").text
    row = html[html.index('class="control-pair"'):html.index('<div id="hv"')]
    for tile in ('id="cryostat"', 'id="flow"', 'id="ln2"'):
        assert tile in row
    assert 'data-plot="ln2"' in html


def test_the_mass_and_the_scale_reading(webui):
    http, _, bus = webui
    measure(bus, "ln2_mass", 141.45)
    measure(bus, "ln2_gross", 278.20)
    text = http.get("/hv").text
    assert "141.45" in text and "278.20" in text and "136.75" in text


def test_the_switch_is_unknown_until_the_engine_says(webui):
    http, _, _ = webui
    text = http.get("/hv").text
    assert "alarm switch unknown" in text
    assert 'action="/alarms/arm"' not in text


def test_on_offers_off_and_off_says_who(webui):
    http, _, bus = webui
    armed(bus, True)
    text = http.get("/hv").text
    assert "Switch alarm off" in text and 'name="enabled" value="0"' in text

    armed(bus, False, by="Auke")
    text = http.get("/hv").text
    assert "alarm OFF" in text and "Auke" in text
    assert "Switch alarm on" in text and 'name="enabled" value="1"' in text


def test_the_button_sends_the_arm_command(webui):
    http, app, _ = webui
    stub_commands(app)
    r = http.post("/alarms/arm", data={"channel": "ln2_mass", "enabled": "0",
                                       "by": "apc"}, follow_redirects=False)
    assert r.status_code == 303 and "ln2_error" not in r.headers["location"]
    topic, payload = app.state.commands[-1]
    assert topic == "xams/cmd/alarms/arm"
    assert payload["channel"] == "ln2_mass" and payload["enabled"] is False


def test_a_refusal_is_shown(webui):
    http, app, _ = webui
    app.state.system.command = lambda *a, **k: {
        "ok": False, "reason": "pmain is not a switchable alarm"}
    r = http.post("/alarms/arm", data={"channel": "pmain", "enabled": "0"},
                  follow_redirects=False)
    assert "ln2_error=" in r.headers["location"]
