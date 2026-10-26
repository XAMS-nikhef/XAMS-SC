"""The PT1000 readout switch (26 October 2026).

With the HV on, the PT1000s on the NI 9226 are disconnected and grounded.
Read, they would only give open-circuit errors and major alarms. What is
pinned: the 9226 is a switchable readout; off stops its thread, closes its
task and survives a restart; on builds it again (and a failure leaves it off);
the alarm engine withdraws the channels' alarms and ignores them while off;
the pages and the daily report show them as off, not as broken; the command
works over the bus and from xams-ctl.
"""

import json
import pathlib
import tempfile
import time

import pytest

from doubles import RecordingBus
from xams_sc.alarms.engine import AlarmEngine
from xams_sc.bus import ACK_READOUT, STATUS_READOUT, TOPIC_READOUT
from xams_sc.config import load
from xams_sc.devices.cdaq import CdaqService
from xams_sc.model import Measurement, Quality, iso, utcnow
from xams_sc.readout import (channels_of, module_for_switch, off_channels,
                             parse_status, switchable_modules)

PT1000 = ["tt201", "tt203", "tt204", "tt205", "tt206", "tt207"]


class FakeTask:
    def __init__(self, n):
        self.n, self.reads, self.closed = n, 0, False

    def read(self):
        self.reads += 1
        return [20.0] * self.n

    def close(self):
        self.closed = True


def off_status(enabled=False, by="apc"):
    return json.dumps({"9226": {"enabled": enabled, "switch": "pt1000",
                                "label": "PT1000 readout", "by": by,
                                "at": "2026-10-26T09:00:00Z"}})


# ------------------------------------------------------------------ config

def test_the_9226_is_the_switchable_pt1000_readout():
    cfg = load()
    assert switchable_modules(cfg) == {"9226": {"switch": "pt1000",
                                                "label": "PT1000 readout"}}
    assert module_for_switch(cfg, "pt1000") == "9226"
    assert channels_of(cfg, "9226") == PT1000          # tt202 is disabled
    assert off_channels(cfg, parse_status(off_status())) == set(PT1000)
    assert off_channels(cfg, parse_status(off_status(enabled=True))) == set()


# ------------------------------------------------------------ the service

@pytest.fixture
def service(tmp_path):
    bus = RecordingBus()
    svc = CdaqService(load(), bus, simulate=False, lock_dir=tmp_path,
                      readout_state_path=tmp_path / "cdaq_readout.json")
    for m in svc._modules:
        m.task = FakeTask(len(m.channels))
        m.interval_s = 0.05
    yield svc, bus, tmp_path
    svc._close_tasks()


def mod(svc, alias):
    return next(m for m in svc._modules if m.alias == alias)


def test_the_state_is_published_at_start(service):
    _, bus, _ = service
    status = bus.last_json(STATUS_READOUT)
    assert status["9226"]["enabled"] is True and status["9226"]["switch"] == "pt1000"


def test_off_stops_the_thread_closes_the_task_and_keeps_it(service):
    svc, bus, tmp = service
    svc._start_background()
    m = mod(svc, "9226")
    task = m.task
    time.sleep(0.2)

    ack = svc.set_readout("9226", False, "apc")

    assert ack["ok"] and ack["was"] is True and ack["enabled"] is False
    assert m.task is None and task.closed and m.thread is None
    assert bus.last_json(STATUS_READOUT)["9226"]["enabled"] is False
    saved = json.loads((tmp / "cdaq_readout.json").read_text())
    assert saved["9226"]["enabled"] is False and saved["9226"]["by"] == "apc"
    # Nothing of the 9226 is published any more; the rest carries on.
    names = {r.channel for r in svc.read()}
    assert not names & set(PT1000)
    time.sleep(0.2)
    names = {r.channel for r in svc.read()}
    assert not names & set(PT1000) and "tt301" in names   # the 9216 carries on


def test_off_survives_a_restart(service):
    svc, _, tmp = service
    svc.set_readout("9226", False, "apc")
    again = CdaqService(load(), RecordingBus(), simulate=False, lock_dir=tmp / "b",
                        readout_state_path=tmp / "cdaq_readout.json")
    assert again.readout_on("9226") is False
    assert again.readout_on("9207") is True          # not switchable: always on


def test_on_builds_the_task_again(service, monkeypatch):
    svc, bus, _ = service
    svc.set_readout("9226", False, "apc")
    m = mod(svc, "9226")

    def fake_open(module):
        module.task = FakeTask(len(module.channels))
        svc._start_module_thread(module)
        return None
    monkeypatch.setattr(svc, "_open_module", fake_open)

    ack = svc.set_readout("9226", True, "apc")
    assert ack["ok"] and m.task is not None and m.thread is not None
    assert bus.last_json(STATUS_READOUT)["9226"]["enabled"] is True
    time.sleep(0.2)
    assert set(PT1000) <= {r.channel for r in svc.read()}


def test_a_task_that_will_not_start_leaves_it_off(service, monkeypatch):
    svc, _, _ = service
    svc.set_readout("9226", False, "apc")
    monkeypatch.setattr(svc, "_open_module", lambda module: "device not present")
    ack = svc.set_readout("9226", True, "apc")
    assert not ack["ok"] and "device not present" in ack["reason"]
    assert svc.readout_on("9226") is False


def test_a_module_without_a_switch_is_refused(service):
    svc, _, _ = service
    ack = svc.set_readout("9207", False, "apc")
    assert not ack["ok"] and "not a switchable readout" in ack["reason"]
    assert mod(svc, "9207").task is not None


def test_the_command_over_the_bus_is_acked_and_audited(service):
    svc, bus, _ = service
    bus.deliver(TOPIC_READOUT, json.dumps({"module": "9226", "enabled": False,
                                           "by": "apc"}))
    deadline = time.monotonic() + 5
    while not bus.json_on(ACK_READOUT) and time.monotonic() < deadline:
        time.sleep(0.02)
    ack = bus.last_json(ACK_READOUT)
    assert ack["ok"] and ack["module"] == "9226"
    audit = bus.last_json("xams/audit")
    assert audit["action"] == "readout_switch" and audit["new"] == "off"


def test_a_switch_needs_a_background_module():
    cfg = load()
    for spec in cfg.devices["cdaq"]["modules"]:
        if spec["alias"] == "9207":
            spec["switch"] = "pressures"
    with pytest.raises(ValueError, match="background"):
        CdaqService(cfg, RecordingBus(), simulate=False,
                    readout_state_path=pathlib.Path(tempfile.gettempdir())
                    / "xams-tests-no-such-dir" / "r.json")


# ---------------------------------------------------------- alarm engine

def engine():
    iso_dir = pathlib.Path(tempfile.gettempdir()) / "xams-tests-no-such-dir"
    bus = RecordingBus()
    eng = AlarmEngine(bus, load(), notify_state_path=iso_dir / "n.json",
                      armed_state_path=iso_dir / "a.json")
    return eng, bus


def open_circuit(channel):
    return Measurement(t=utcnow(), channel=channel, value=None, unit="C",
                       quality=Quality.ERROR)


def test_off_withdraws_the_alarms_and_ignores_the_channels():
    eng, bus = engine()
    eng.on_measurement(open_circuit("tt201"))
    assert bus.last_json("xams/alarm/tt201")["state"] == "major"

    eng.on_readout_status(STATUS_READOUT, off_status())

    assert bus.last_json("xams/alarm/tt201")["state"] == "ok"
    eng.on_measurement(open_circuit("tt201"))        # one still in flight
    assert bus.last_json("xams/alarm/tt201")["state"] == "ok"
    assert "tt201" not in eng._states                # so never stale either
    assert not [s for s in eng.active() if s.channel in PT1000]


def test_on_watches_them_again():
    eng, bus = engine()
    eng.on_readout_status(STATUS_READOUT, off_status())
    eng.on_readout_status(STATUS_READOUT, off_status(enabled=True))
    eng.on_measurement(open_circuit("tt203"))
    assert bus.last_json("xams/alarm/tt203")["state"] == "major"


def test_a_reload_keeps_them_off():
    eng, bus = engine()
    eng.on_readout_status(STATUS_READOUT, off_status())
    eng.reload(load())
    eng.on_measurement(open_circuit("tt204"))
    assert bus.last_json("xams/alarm/tt204") is None


# ---------------------------------------------------------------- web UI

def measure(bus, channel, value, unit="C", q="ok"):
    bus.deliver(f"xams/meas/{channel}",
                json.dumps({"t": iso(utcnow()), "ch": channel, "v": value,
                            "u": unit, "q": q}))


def test_the_strip_is_unknown_until_the_service_says(webui):
    http, _, _ = webui
    text = http.get("/hv").text
    assert 'id="readout"' in text and "PT1000 readout" in text
    assert "state unknown" in text and 'action="/cdaq/readout"' not in text


def test_on_shows_the_temperatures_and_offers_off(webui):
    http, _, bus = webui
    bus.deliver(STATUS_READOUT, off_status(enabled=True))
    measure(bus, "tt201", 22.55)
    text = http.get("/hv").text
    strip = text[text.index('id="readout"'):text.index('<div id="hv"')]
    assert "22.55" in strip and "Switch readout off" in strip
    assert 'name="module" value="9226"' in strip and 'name="enabled" value="0"' in strip
    assert "HV is on and the" not in strip


def test_off_shows_who_and_the_channels_as_off(webui):
    http, app, bus = webui
    measure(bus, "tt201", 22.55)
    bus.deliver(STATUS_READOUT, off_status(by="Auke"))
    text = http.get("/hv").text
    strip = text[text.index('id="readout"'):text.index('<div id="hv"')]
    assert "OFF" in strip and "Auke" in strip and "Switch readout on" in strip
    assert "22.55" not in strip
    state = app.state.system
    view = state.channel("tt201")
    assert view.off and view.value is None and view.formatted() == "—"
    assert "tt201" not in {c.name for c in state.unhealthy_channels()}
    api = {c["name"]: c for c in http.get("/api/state").json()["channels"]}
    assert api["tt201"]["quality"] == "off"


def test_the_page_sends_the_switch_without_reloading(webui):
    text = webui[0].get("/hv").text
    assert "'/cdaq/readout':" in text and "'readout'" in text


def test_the_post_goes_to_the_cdaq_service(webui):
    http, _, bus = webui

    def answer(topic, payload):
        if topic == TOPIC_READOUT:
            req = json.loads(payload)
            bus.deliver(ACK_READOUT, json.dumps({"ok": True, "module": req["module"],
                                                 "enabled": req["enabled"]}))
    bus.on_publish = answer
    r = http.post("/cdaq/readout", data={"module": "9226", "enabled": "0", "by": "apc"},
                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/hv#readout"
    sent = bus.last_json(TOPIC_READOUT)
    assert sent == {"module": "9226", "enabled": False, "by": "apc"}


def test_the_daily_report_says_it_is_off(webui):
    from xams_sc.alarms import mail
    _, app, bus = webui
    bus.deliver(STATUS_READOUT, off_status(by="Auke"))
    _, html, text = mail.digest(app.state.system, utcnow())
    assert "PT1000 readout" in html and "OFF since 2026-10-26 09:00 UTC" in html
    assert "PT1000 readout OFF since" in text


def test_the_daily_report_says_nothing_while_it_is_on(webui):
    from xams_sc.alarms import mail
    _, app, bus = webui
    bus.deliver(STATUS_READOUT, off_status(enabled=True))
    _, html, _ = mail.digest(app.state.system, utcnow())
    assert "PT1000 readout" not in html


# ------------------------------------------------------------------- CLI

def test_xams_ctl_knows_the_switch_by_name(capsys):
    from xams_sc.cli import xams_ctl
    args = xams_ctl.build_parser().parse_args(["readout", "nonsense", "off"])
    assert args.func(args) == 1
    assert "pt1000" in capsys.readouterr().out


def test_the_strip_turns_amber_when_the_hv_is_on_and_it_is_not_off(webui):
    """A hint, never a lock: with an output energised and the PT1000s still
    read, the strip says to switch them off before they are grounded."""
    http, _, bus = webui
    bus.deliver(STATUS_READOUT, off_status(enabled=True))
    measure(bus, "hv_cathode_stat", 1.0, unit="code")      # bit 0: energised
    measure(bus, "hv_cathode_vmon", -200.0, unit="V")
    strip = (lambda t: t[t.index('id="readout"'):t.index('<div id="hv"')])(http.get("/hv").text)
    assert "HV is on and the PT1000 readout is still ON" in strip
    bus.deliver(STATUS_READOUT, off_status())
    strip = (lambda t: t[t.index('id="readout"'):t.index('<div id="hv"')])(http.get("/hv").text)
    assert "HV is on and the" not in strip
