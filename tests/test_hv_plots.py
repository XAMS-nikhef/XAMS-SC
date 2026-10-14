"""The HV plots on the Control page (14 October 2026).

What is pinned: the web UI keeps the HV readings of the last hours in memory
(from the bus, never the database, §8.1), seeds them from the JSONL archive at
start without counting anything twice, serves them per supply for the plots,
and leaves out the current of a channel listed in `plot_without_imon`.
"""

import json
import time
from datetime import datetime, timezone

import pytest

from doubles import RecordingBus, StubDrift, ui_client
from xams_sc.api import app as app_module
from xams_sc.api.hv_history import read_archive
from xams_sc.api.state import HISTORY_S, keeps_history
from xams_sc.model import iso


def payload(ch, value, t, q="ok"):
    return json.dumps({"t": iso(datetime.fromtimestamp(t, timezone.utc)), "ch": ch,
                       "v": value, "u": "V", "q": q})


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "Bus", lambda **kw: RecordingBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    return app_module.create_app(history_dir=tmp_path)


def test_only_hv_monitor_and_setpoint_channels_are_kept():
    assert keeps_history("hv_cathode_vmon")
    assert keeps_history("hv_cathode_imon")
    assert keeps_history("hv_cathode_vset")
    assert not keeps_history("hv_cathode_stat")
    assert not keeps_history("tt401")


def test_bus_readings_are_remembered_and_bad_ones_are_not(app):
    state = app.state.system
    now = time.time()
    state._on_measurement("m", payload("hv_cathode_vmon", -1700.0, now - 20))
    state._on_measurement("m", payload("hv_cathode_vmon", -1701.0, now - 10))
    state._on_measurement("m", payload("hv_cathode_vmon", None, now - 5))
    state._on_measurement("m", payload("hv_cathode_vmon", -5.0, now - 4, q="error"))
    state._on_measurement("m", payload("tt401", 20.0, now))
    h = state.history(["hv_cathode_vmon", "tt401"], 60)
    assert [v for _, v in h["hv_cathode_vmon"]] == [-1700.0, -1701.0]
    assert h["tt401"] == []


def test_old_readings_fall_out_of_the_window(app):
    state = app.state.system
    now = time.time()
    with state._lock:
        state._remember("hv_gate_vmon", now - HISTORY_S - 100, 1.0)
        state._remember("hv_gate_vmon", now, 2.0)
    assert [v for _, v in state.history(["hv_gate_vmon"], HISTORY_S)["hv_gate_vmon"]] == [2.0]


def test_seed_goes_in_front_and_never_duplicates(app):
    state = app.state.system
    now = time.time()
    state._on_measurement("m", payload("hv_anode_vmon", 2200.0, now - 30))
    n = state.seed_history({"hv_anode_vmon": [(now - 50, 2100.0), (now - 40, 2150.0),
                                              (now - 30, 9999.0), (now - 20, 9999.0)]})
    assert n == 2
    pts = state.history(["hv_anode_vmon"], 120)["hv_anode_vmon"]
    assert [v for _, v in pts] == [2100.0, 2150.0, 2200.0]


def test_archive_reader_takes_hv_lines_only(tmp_path):
    now = time.time()
    day = datetime.now(timezone.utc).date().isoformat()
    lines = [json.dumps({"t": "x", "meta": {"version": "test"}}),
             payload("hv_gate_vmon", -1200.0, now - 100),
             payload("hv_gate_stat", 1.0, now - 100),
             payload("tt401", 21.0, now - 100),
             payload("hv_gate_imon", 0.5, now - 100, q="stale"),
             '{"t": "half a line',
             payload("hv_gate_vmon", -1190.0, now - 99999)]
    (tmp_path / f"{day}.jsonl").write_text("\n".join(lines) + "\n")
    points = read_archive(tmp_path, now - 3600)
    assert list(points) == ["hv_gate_vmon"]
    assert [v for _, v in points["hv_gate_vmon"]] == [-1200.0]


def test_history_endpoint_per_supply_without_the_nai_current(app):
    state = app.state.system
    now = time.time()
    for ch, v in (("hv_nai_vmon", 800.0), ("hv_nai_imon", 150.0),
                  ("hv_cathode_vmon", -1700.0), ("hv_cathode_imon", 0.3)):
        state._on_measurement("m", payload(ch, v, now - 5))
    r = ui_client(app).get("/api/hv/history?window=900")
    assert r.status_code == 200
    d = r.json()
    hv2 = next(s for s in d["supplies"] if s["id"] == "hv_2")
    by = {c["label"]: c for c in hv2["channels"]}
    assert [p[1] for p in by["cathode"]["v"]] == [-1700.0]
    assert [p[1] for p in by["cathode"]["i"]] == [0.3]
    assert [p[1] for p in by["nai"]["v"]] == [800.0]
    assert by["nai"]["i"] is None
    assert [c["index"] for c in hv2["channels"]] == sorted(c["index"] for c in hv2["channels"])


def test_window_is_clamped(app):
    d = ui_client(app).get("/api/hv/history?window=999999").json()
    assert d["window"] == HISTORY_S
    d = ui_client(app).get("/api/hv/history?window=1").json()
    assert d["window"] == 60


def test_page_has_a_plot_per_supply_and_window_buttons_that_do_not_submit(app):
    html = ui_client(app).get("/hv").text
    assert html.count('class="hv-plot"') == 2
    assert 'data-supply="hv_1"' in html and 'data-supply="hv_2"' in html
    for w in ("900", "3600", "21600"):
        assert f'<button type="button" class="hv-btn" data-window="{w}">' in html
    assert "/api/hv/history" in html
    assert 'class="swatch"' in html
