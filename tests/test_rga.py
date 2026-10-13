"""PrismaPro RGA, step 1: status only. No hardware required.

Replies recorded from the analyser on 13 October 2026, emission off, nobody
in control (`recorded/prismapro_2026-10-13.json`), through the same client.
"""

import inspect
import json
import pathlib
import tempfile

import pytest

from doubles import RecordingBus
from xams_sc.bus import TOPIC_MEAS, TOPIC_RGA_INFO
from xams_sc.config import load
from xams_sc import rga_file
from xams_sc.devices import prismapro, rga
from xams_sc.sinks import rga_writer
from xams_sc.devices.prismapro import (WRITABLE, ControlRefused, MMSPClient,
                                       MMSPControl, MMSPError, decode_system_status,
                                       emission_on, read_identity)
from xams_sc.devices.rga import CONTROL_NONE, CONTROL_OTHER, READERS, RgaService
from xams_sc.model import Measurement, Quality, utcnow

RECORDED = json.loads(
    (pathlib.Path(__file__).parent / "recorded" / "prismapro_2026-10-13.json")
    .read_text(encoding="utf-8"))


class RecordedClient(MMSPControl):
    """The real client, with the network replaced by the recording."""

    def __init__(self, *args, replies=None, **kw):
        super().__init__(*args, **kw)
        self.replies = dict(RECORDED if replies is None else replies)
        self.asked: list[str] = []

    def _request(self, path):
        self.asked.append(path)
        if path not in self.replies:
            raise OSError(f"nothing recorded for {path}")
        reply = self.replies[path]
        return reply if isinstance(reply, bytes) else json.dumps(reply).encode()


def with_reply(target, data):
    return dict(RECORDED) | {f"/mmsp/{target}/get":
                             {"name": "got", "origin": f"/mmsp/{target}", "data": data}}


# ------------------------------------------------------------------- client

class TestClient:
    def test_reads_the_data_of_a_recorded_reply(self):
        assert RecordedClient("x").get("status/systemStatus") == 0x01103B90

    def test_an_error_event_with_http_200_is_an_error(self):
        """The device says no with a 200; the status code alone means nothing."""
        with pytest.raises(MMSPError, match="msgIdInvalid"):
            RecordedClient("x").get("sensorInfo/serialNumberString")

    def test_an_empty_reply_is_an_error_not_a_none(self):
        """What a dropped request looks like on this device."""
        client = RecordedClient("x", replies={"/mmsp/status/systemStatus/get": b"  "})
        with pytest.raises(MMSPError, match="empty"):
            client.get("status/systemStatus")

    def test_a_broken_connection_is_retried_once_on_a_fresh_one(self):
        client = RecordedClient("x")
        calls = []
        real = client._request

        def flaky(path):
            calls.append(path)
            if len(calls) == 1:
                raise ConnectionResetError("closed by peer")
            return real(path)

        client._request = flaky
        assert client.get("status/hardwareErrors") == 0
        assert len(calls) == 2

    def test_a_second_failure_is_not_retried_again(self):
        client = RecordedClient("x")
        client._request = lambda path: (_ for _ in ()).throw(OSError("down"))
        with pytest.raises(OSError):
            client.get("status/hardwareErrors")

    def test_the_reader_has_no_way_to_write(self):
        """MMSPClient reads. Writing is MMSPControl's, through a whitelist."""
        assert not [n for n in dir(MMSPClient) if n.startswith(("set", "_set", "put", "post"))]


class TestDecoding:
    def test_the_recorded_word_has_emission_off(self):
        assert not emission_on(0x01103B90)

    @pytest.mark.parametrize("bit", [31, 30])
    def test_either_regulator_mode_means_emission_on(self, bit):
        assert emission_on(1 << bit)

    def test_named_bits_of_the_recorded_word(self):
        on = {k for k, v in decode_system_status(0x01103B90).items() if v}
        assert on == {"rod_polarity_reversed", "peak_position_measurement_scheduled",
                      "peak_position_correction", "baseline_continuous_scheduled",
                      "baseline_correction"}

    def test_identity_from_the_recording(self):
        ident = read_identity(RecordedClient("x"))
        assert ident.name == "RGA PrismaPro A 200 47504651"
        assert ident.serial == "47504651"
        assert ident.mass_range == 200
        assert ident.as_dict()["filaments"] == ["yttria-iridium"] * 3
        assert ident.as_dict()["tp_units"] == "Torr"


# ------------------------------------------------------------------ service

def build(monkeypatch, replies=None, devices=None):
    config = load()
    if devices is not None:
        config.devices["rga"] = devices
    monkeypatch.setattr(rga, "MMSPControl",
                        lambda *a, **kw: RecordedClient(*a, replies=replies, **kw))
    return RgaService(config, RecordingBus(),
                      lock_dir=pathlib.Path(tempfile.mkdtemp()))


class TestIdentity:
    def test_the_configured_analyser_is_accepted(self, monkeypatch):
        service = build(monkeypatch)
        assert service.verify_identity()

    def test_identity_is_published_retained_for_the_page(self, monkeypatch):
        service = build(monkeypatch)
        service.verify_identity()
        info = service.bus.last_json(TOPIC_RGA_INFO)
        assert info["identity"]["name"] == "RGA PrismaPro A 200 47504651"
        assert info["identity"]["host"] == "192.16.192.86"

    def test_another_analyser_on_the_address_is_refused(self, monkeypatch):
        replies = with_reply("electronicsInfo/serialNumber", "12345678")
        assert not build(monkeypatch, replies=replies).verify_identity()

    def test_an_address_without_a_name_and_serial_is_refused(self, monkeypatch):
        assert not build(monkeypatch, devices={"host": "192.16.192.86"}).verify_identity()

    def test_no_answer_is_refused_cleanly(self, monkeypatch):
        assert not build(monkeypatch, replies={}).verify_identity()

    def test_every_configured_channel_names_something_readable(self, config):
        assert config.channels_for("rga")
        assert {c.phys for c in config.channels_for("rga")} <= set(READERS)


class TestRead:
    def test_one_cycle_publishes_every_channel_from_the_recording(self, monkeypatch):
        service = build(monkeypatch)
        service.verify_identity()
        values = {m.channel: m.value for m in service.read()}

        assert values == {
            "rga_emission": 0.0, "rga_control": float(CONTROL_NONE),
            "rga_status": float(0x01103B90), "rga_hw_errors": 0.0,
            "rga_hw_warnings": 0.0, "rga_tp_trips": 32.0, "rga_box_temp": 35.749,
        }

    def test_each_target_is_asked_once_per_cycle(self, monkeypatch):
        """emission and rga_status share systemStatus: one request, not two."""
        service = build(monkeypatch)
        service.verify_identity()
        service._client.asked.clear()
        service.read()
        assert len(service._client.asked) == len(set(service._client.asked))

    def test_another_client_in_control_is_named_on_the_page(self, monkeypatch):
        replies = with_reply("communication/controlInfo", {
            "controlled": True, "amInControl": False, "locked": True,
            "clientName": "PVMassSpec", "ipAddress": "192.16.192.20"})
        service = build(monkeypatch, replies=replies)
        service.verify_identity()
        values = {m.channel: m.value for m in service.read()}

        assert values["rga_control"] == CONTROL_OTHER
        control = service.bus.last_json(TOPIC_RGA_INFO)["control"]
        assert control == {"controlled": True, "client": "PVMassSpec",
                           "ip": "192.16.192.20", "locked": True, "us": False}

    def test_one_unreadable_target_fails_the_whole_read(self, monkeypatch):
        """Raised, so the loop marks every channel bad - never a partial cycle
        that leaves the missing ones at their last value."""
        replies = dict(RECORDED)
        del replies["/mmsp/status/hardwareErrors/get"]
        service = build(monkeypatch, replies=replies)
        service.verify_identity()
        with pytest.raises(OSError):
            service.read()

    def test_status_is_asked_every_few_seconds_not_every_second(self, monkeypatch):
        assert build(monkeypatch).interval_s >= 5


# --------------------------------------------------------------------- page

def deliver(bus, channel, value, unit):
    m = Measurement(t=utcnow(), channel=channel, value=value, unit=unit,
                    quality=Quality.OK)
    bus.deliver(f"{TOPIC_MEAS}/{channel}", m.to_json())


class TestPage:
    def test_it_says_when_the_service_has_not_reported(self, client):
        body = client.get("/rga").text
        assert "No word from the RGA service yet" in body

    def test_it_shows_identity_state_and_who_holds_it(self, webui):
        client, app, bus = webui
        bus.deliver(TOPIC_RGA_INFO, json.dumps({
            "identity": {"name": "RGA PrismaPro A 200 47504651", "firmware": "1.09.05",
                         "mass_range_amu": 200, "filaments": ["yttria-iridium"]},
            "control": {"controlled": True, "client": "PVMassSpec", "ip": "", "locked": False}}))
        deliver(bus, "rga_emission", 1.0, "bool")
        deliver(bus, "rga_control", 2.0, "code")
        deliver(bus, "rga_status", float(0x01103B90), "bits")

        body = client.get("/rga").text
        assert "RGA PrismaPro A 200 47504651" in body
        assert ">ON<" in body
        assert "another client" in body and "PVMassSpec" in body
        assert "0x01103B90" in body and "baseline_correction" in body

    def test_the_page_has_no_electron_multiplier_control(self, client):
        """Emission yes (step 3); the electron multiplier is a later stage."""
        body = client.get("/rga").text.lower()
        assert "/rga/em\"" not in body and "setem" not in body


# ================================================================ step 2
#
# A fake analyser that behaves as the real one did in the live test of
# 13 October 2026: writes answer "wasSet", control is a session, a scan is
# handed over once by nextScan, and systemStatus bit 1 is set while scanning.

QUICK_POINTS = 197      # 1-50 amu at 4 per amu, as the device reported


class FakeAnalyser(MMSPControl):
    def __init__(self, *a, points=QUICK_POINTS, other=None, **kw):
        super().__init__(*a, **kw)
        self.min_gap_s = 0
        self.points = points
        self.control = {"controlled": bool(other), "amInControl": False,
                        "locked": bool(other), "clientName": other,
                        "ipAddress": "192.16.192.20" if other else "null"}
        self.count = 0
        self.queue: list[dict] = []
        self.scanning = False
        self.sets: list[str] = []
        self.emission = False
        self.interlock = False
        # Reads of setEmission that still give the old value after a write,
        # as the device did on 13 October 2026.
        self.emission_lag = 0
        self._lagging = 0

    def status(self):
        word = 0x01103B90 | (2 if self.scanning else 0)
        if self.emission:
            word |= 1 << 31
        if self.interlock:
            word |= 1 << 27
        return word

    def _event(self, path, data, name="got"):
        return json.dumps({"name": name, "origin": path, "data": data}).encode()

    def _request(self, path):
        if "/set?" in path:
            self.sets.append(path)
            target, query = path[len("/mmsp/"):].split("/set?")
            if target == "communication/control":
                mine = query == "force"
                self.control.update(controlled=mine, amInControl=mine,
                                    locked=mine and self.control["locked"],
                                    clientName="xams-sc" if mine else None)
            elif target == "communication/controlLock":
                self.control["locked"] = query == "Session"
            elif target == "scanSetup" and query.startswith("scanCount="):
                self.count = int(query.split("=")[1])
            elif target == "scanSetup" and query.startswith("scanStart"):
                self.scanning = True
                self.queue = [{"scannum": n, "scansize": self.points,
                               "systemStatus": 0x01103B92,
                               "values": [1e-14 * ((i % 7) - 3) for i in range(self.points)]}
                              for n in range(1, self.count + 1)]
            elif target == "scanSetup" and query.startswith("scanStop"):
                self.scanning = False
            elif target == "generalControl/setEmission":
                self.emission = query == "On"
                self._lagging = self.emission_lag
            return self._event(path, query, "wasSet")
        target = path[len("/mmsp/"):-len("/get")]
        if target == "communication/controlInfo":
            return self._event(path, dict(self.control))
        if target == "measurement/nextScan":
            if self.queue:
                d = self.queue.pop(0)
                if not self.queue:
                    self.scanning = False
                return self._event(path, d)
            return self._event(path, {"values": None, "systemStatus": self.status()})
        if target == "status/systemStatus":
            return self._event(path, self.status())
        if target == "generalControl/setEmission":
            if self._lagging:
                self._lagging -= 1
                return self._event(path, "Off" if self.emission else "On")
            return self._event(path, "On" if self.emission else "Off")
        extra = {"sensorIonSource/emissionCurrent": 2000,
                 "sensorIonSource/electronEnergy": 70,
                 "sensorIonSource/ppSensitivityFactor": 0.000793007,
                 "generalControl/setEM": "Off",
                 "measurement/totalPressure": 0}
        if target in extra:
            return self._event(path, extra[target])
        reply = RECORDED.get(path)
        if reply is None:
            raise OSError(f"nothing recorded for {path}")
        return json.dumps(reply).encode()


@pytest.fixture
def analyser():
    return {}


def running(monkeypatch, tmp_path, analyser, **fake):
    """A service connected to a FakeAnalyser, writing files under tmp_path."""
    config = load()
    config.devices["rga"] = dict(config.devices["rga"], data_dir=str(tmp_path / "rga"),
                                 release_idle_s=300)

    def make(*a, **kw):
        analyser["device"] = FakeAnalyser(*a, **fake, **kw)
        return analyser["device"]

    monkeypatch.setattr(rga, "MMSPControl", make)
    monkeypatch.setattr(rga, "ROOT", pathlib.Path("/"))
    service = RgaService(config, RecordingBus(), lock_dir=pathlib.Path(tempfile.mkdtemp()))
    assert service.verify_identity()
    service.read()
    return service


def command(service, handler, ack_topic, **payload):
    getattr(service, handler)("", json.dumps({"by": "tester"} | payload))
    return service.bus.last_json(ack_topic)


def acquire(service, recipe="quick"):
    assert command(service, "_handle_control", "xams/ack/rga/control", action="take")["ok"]
    service.read()
    ack = command(service, "_handle_scan", "xams/ack/rga/scan", action="start", recipe=recipe)
    assert ack["ok"], ack
    for _ in range(5):
        service.read()
    return ack


class TestTheWhitelist:
    def test_only_emission_is_writable_among_the_instrument_settings(self):
        assert [t for t in WRITABLE if t.startswith(
            ("generalControl", "sensorIonSource", "sensorDetector", "digital",
             "analog", "userDB", "system"))] == ["generalControl/setEmission"]

    @pytest.mark.parametrize("target", ["generalControl/setEM",
                                        "generalControl/degas",
                                        "sensorIonSource/emissionCurrent",
                                        "sensorIonSource/filamentSelected"])
    def test_a_target_outside_it_is_refused_before_anything_is_sent(self, target):
        device = FakeAnalyser("x")
        with pytest.raises(PermissionError):
            device._set(target, {"v": "On"})
        assert device.sets == []

    @pytest.mark.parametrize("value", ["Standby", "on", "1", "Degas"])
    def test_emission_takes_on_or_off_and_nothing_else(self, value):
        device = FakeAnalyser("x")
        with pytest.raises(PermissionError):
            device._set("generalControl/setEmission", {"v": value})
        assert device.sets == []

    def test_an_unlisted_field_of_a_listed_target_is_refused(self):
        device = FakeAnalyser("x")
        with pytest.raises(PermissionError):
            device._set("scanSetup", {"dwellGlobal": 1})
        assert device.sets == []


class TestControl:
    def test_taking_a_free_analyser(self):
        device = FakeAnalyser("x")
        assert device.take_control("xams-sc")["amInControl"]
        assert device.sets[-1].endswith("controlLock/set?Session")

    def test_one_held_by_another_client_is_not_taken_unasked(self):
        device = FakeAnalyser("x", other="PVMassSpec")
        with pytest.raises(ControlRefused, match="PVMassSpec"):
            device.take_control("xams-sc")
        assert device.sets == []

    def test_force_takes_it(self):
        device = FakeAnalyser("x", other="PVMassSpec")
        assert device.take_control("xams-sc", force=True)["amInControl"]

    def test_the_service_refuses_politely_and_audits_it(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser, other="PVMassSpec")
        ack = command(service, "_handle_control", "xams/ack/rga/control", action="take")
        assert not ack["ok"] and "PVMassSpec" in ack["reason"]
        audit = service.bus.last_json("xams/audit")
        assert audit["action"] == "rga_control_take" and audit["result"] == "rejected"


class TestScanAndSave:
    def test_a_scan_needs_control_first(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        ack = command(service, "_handle_scan", "xams/ack/rga/scan",
                      action="start", recipe="quick")
        assert not ack["ok"] and "take control first" in ack["reason"]
        assert not [p for p in analyser["device"].sets if "scanSetup" in p]

    def test_an_unknown_recipe_is_refused(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        command(service, "_handle_control", "xams/ack/rga/control", action="take")
        service.read()
        ack = command(service, "_handle_scan", "xams/ack/rga/scan",
                      action="start", recipe="nonsense")
        assert not ack["ok"]

    def test_the_recipe_is_loaded_as_the_device_was_set_up_by_hand(
            self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        acquire(service)
        sets = analyser["device"].sets
        assert "/mmsp/scanSetup/channels/1/set?channelMode=Sweep&startMass=1&stopMass=50" in sets
        assert "/mmsp/scanSetup/channels/1/set?dwell=32&ppamu=4&enabled=True" in sets
        assert "/mmsp/scanSetup/set?scanCount=2" in sets
        assert sets[-1] == "/mmsp/scanSetup/set?scanStart=1"

    def test_every_scan_is_collected_and_the_acquisition_ends(
            self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        acquire(service)
        acq = service.bus.last_json("xams/rga/info")["acquisition"]
        assert (acq["state"], acq["done"], acq["of"]) == ("done", 2, 2)
        live = service.bus.last_json("xams/rga/scan")
        assert len(live["values"]) == len(live["masses"]) == QUICK_POINTS
        assert service.interval_s >= 5      # back to the slow status rate

    def test_a_scan_of_the_wrong_length_is_not_recorded(
            self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser, points=100)
        acquire(service)
        acq = service.bus.last_json("xams/rga/info")["acquisition"]
        assert acq["state"] == "failed" and acq["done"] == 0

    def test_save_writes_one_file_and_publishes_its_summary(
            self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        acquire(service)
        ack = command(service, "_handle_save", "xams/ack/rga/save",
                      comment="emission off", tags=["background"])
        assert ack["ok"], ack

        files = rga_file.files(tmp_path / "rga")
        assert [f.name for f in files] == [ack["file"]]
        record = rga_file.load_rga(files[0])
        assert record["units"] == "A" and record["tags"] == ["background"]
        assert len(record["scans"]) == 2 and len(record["mean"]) == QUICK_POINTS
        assert len(record["masses"]) == QUICK_POINTS
        assert record["masses"][0] == 1 and record["masses"][-1] == 50
        assert record["device"]["unit_to_mbar"] == pytest.approx(1.3332, rel=1e-3)
        assert record["device"]["emission_state"] == "Off"

        summary = service.bus.last_json("xams/rga/saved")
        assert summary["file"] == ack["file"]
        assert {p["mass"] for p in summary["peaks"]} == {2, 4, 18, 28, 32, 40, 44}
        save_row, peak_rows = rga_writer.rows(summary)
        assert save_row[1] == ack["file"] and len(peak_rows) == 7

    def test_the_same_acquisition_is_not_saved_twice(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        acquire(service)
        assert command(service, "_handle_save", "xams/ack/rga/save")["ok"]
        ack = command(service, "_handle_save", "xams/ack/rga/save")
        assert not ack["ok"] and "already saved" in ack["reason"]

    def test_nothing_to_save_is_said(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        ack = command(service, "_handle_save", "xams/ack/rga/save")
        assert not ack["ok"]


class TestGivingItBack:
    def test_control_is_released_after_the_idle_time(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        acquire(service)
        service._last_activity -= 301
        service.read()
        assert analyser["device"].sets[-1] == "/mmsp/communication/control/set?release"

    def test_not_while_a_scan_runs(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        command(service, "_handle_control", "xams/ack/rga/control", action="take")
        service.read()
        command(service, "_handle_scan", "xams/ack/rga/scan", action="start", recipe="quick")
        analyser["device"].queue.clear()            # scans not ready yet
        analyser["device"].scanning = True
        service._last_activity -= 1000
        service.read()
        assert not [p for p in analyser["device"].sets if p.endswith("control/set?release")]

    def test_a_stopping_service_stops_the_scan_and_releases(
            self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        command(service, "_handle_control", "xams/ack/rga/control", action="take")
        service.read()
        command(service, "_handle_scan", "xams/ack/rga/scan", action="start", recipe="quick")
        device = analyser["device"]
        service.close()
        assert "/mmsp/scanSetup/set?scanStop=Immediately" in device.sets
        assert device.sets[-1] == "/mmsp/communication/control/set?release"


class TestRecipes:
    def test_the_repository_recipes_are_valid(self):
        recipes = rga_file.load_recipes(rga_file_path())
        assert set(recipes) >= {"survey", "air_leak", "xenon", "quick"}
        assert sum(rga_file.points(c) for c in recipes["quick"]["channels"]) == QUICK_POINTS

    @pytest.mark.parametrize("channel, message", [
        ({"mode": "sweep", "start": 1, "stop": 250, "ppamu": 10}, "outside"),
        ({"mode": "sweep", "start": 10, "stop": 5, "ppamu": 10}, "stop must exceed"),
        ({"mode": "single", "mass": 18, "dwell": 50000}, "dwell"),
        ({"mode": "emission", "mass": 18}, "mode"),
    ])
    def test_a_bad_recipe_is_refused_not_clipped(self, channel, message):
        with pytest.raises(rga_file.RecipeError, match=message):
            rga_file.validate_recipe("bad", {"channels": [channel]})


def rga_file_path():
    from xams_sc.config import CONFIG_DIR
    return CONFIG_DIR / "rga_recipes.yaml"


class TestThePageWithControl:
    def test_it_offers_release_and_start_when_it_holds_the_analyser(self, webui):
        client, app, bus = webui
        bus.deliver(TOPIC_RGA_INFO, json.dumps({
            "identity": {"name": "RGA PrismaPro A 200 47504651"},
            "control": {"controlled": True, "us": True, "client": "xams-sc"},
            "recipes": {"quick": {"description": "", "scans": 2, "seconds": 6.3}},
            "acquisition": {"recipe": "quick", "state": "done", "done": 2, "of": 2,
                            "t_start": "2026-10-13T14:00:00Z", "by": "t", "saved": None}}))
        deliver(bus, "rga_control", 1.0, "code")
        body = client.get("/rga").text
        assert 'value="release"' in body and 'value="start"' in body
        assert 'action="/rga/save"' in body

    def test_the_live_scan_is_served_as_json(self, webui):
        client, app, bus = webui
        bus.deliver("xams/rga/scan", json.dumps({"masses": [1, 2], "values": [1e-14, 2e-14]}))
        assert client.get("/api/rga/scan").json()["values"] == [1e-14, 2e-14]


class TestCommandsFromTheBusThread:
    def test_a_command_completes_while_run_holds_the_instance_lock(
            self, monkeypatch, tmp_path, analyser):
        """The bug of 13 October 2026, as it happened: run() takes the
        instance lock in the main thread, and the command arrives on another
        (paho's network thread). It must finish, not wait forever."""
        import threading

        service = running(monkeypatch, tmp_path, analyser)
        assert service._lock.acquire()         # what BaseService.run() does
        try:
            worker = threading.Thread(
                target=service._handle_control,
                args=("", json.dumps({"action": "take", "by": "tester"})),
                daemon=True)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive(), "the command hung on another thread"
        finally:
            service._lock.release()
        assert service.bus.last_json("xams/ack/rga/control")["ok"]



# ================================================================ step 3
#
# Emission On/Off behind the p105 guard. The guard is a software guard on top
# of the analyser's own over-pressure trip (DESIGN.md section 10 rule 1).

def p105(service, value, quality="ok", unit="mbar"):
    service._on_snapshot("xams/meas/p105", json.dumps(
        {"t": "2026-10-13T15:00:00Z", "ch": "p105", "v": value, "u": unit, "q": quality}))


def holding(monkeypatch, tmp_path, analyser, p=1.9e-5):
    service = running(monkeypatch, tmp_path, analyser)
    if p is not None:
        p105(service, p)
    assert command(service, "_handle_control", "xams/ack/rga/control", action="take")["ok"]
    service.read()
    return service


def emission(service, action):
    return command(service, "_handle_emission", "xams/ack/rga/emission", action=action)


class TestEmissionOn:
    def test_with_control_and_a_good_p105_it_goes_on(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        ack = emission(service, "on")
        assert ack["ok"], ack
        assert analyser["device"].emission
        assert "1.90e-05" in ack["detail"]
        service.read()
        assert service.emission_is_on()
        assert service.bus.last_json("xams/rga/info")["emission"]["on"]
        assert service.bus.last_json("xams/audit")["action"] == "rga_emission_on"

    def test_a_lagging_read_back_is_waited_for(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        analyser["device"].emission_lag = 2
        ack = emission(service, "on")
        assert ack["ok"] and "read-back" not in ack["detail"], ack
        assert analyser["device"].emission

    def test_a_read_back_that_never_agrees_is_not_called_refused(
            self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        device = analyser["device"]
        device.emission_lag = 10 ** 6
        device.EMISSION_CONFIRM_S = 0.3
        ack = emission(service, "on")
        assert ack["ok"] and "read-back still says Off" in ack["detail"], ack
        assert device.emission

    def test_not_without_control(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        p105(service, 1.9e-5)
        ack = emission(service, "on")
        assert not ack["ok"] and "take control first" in ack["reason"]
        assert not analyser["device"].emission

    @pytest.mark.parametrize("value, quality, unit, words", [
        (6e-4, "ok", "mbar", "at or above the limit"),
        (5e-4, "ok", "mbar", "at or above the limit"),
        (None, "error", "mbar", "quality is error"),
        (1e-5, "ok", "bar", "expects mbar"),
    ])
    def test_not_past_the_guard(self, monkeypatch, tmp_path, analyser,
                                value, quality, unit, words):
        service = holding(monkeypatch, tmp_path, analyser, p=None)
        p105(service, value, quality, unit)
        ack = emission(service, "on")
        assert not ack["ok"] and words in ack["reason"]
        assert not [x for x in analyser["device"].sets if "setEmission" in x]

    def test_not_when_p105_was_never_received(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser, p=None)
        ack = emission(service, "on")
        assert not ack["ok"] and "has not been received" in ack["reason"]

    def test_not_when_p105_is_stale(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        service._guard_seen -= 31
        ack = emission(service, "on")
        assert not ack["ok"] and "s old" in ack["reason"]

    def test_not_with_hardware_errors(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        service._hw_errors = 0x10
        ack = emission(service, "on")
        assert not ack["ok"] and "hardware errors" in ack["reason"]

    def test_not_with_the_interlock_active(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        analyser["device"].interlock = True
        service.read()
        ack = emission(service, "on")
        assert not ack["ok"] and "interlock" in ack["reason"]

    def test_off_is_always_allowed_with_control(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        emission(service, "on")
        p105(service, 1e-3)                    # off must not need a good p105
        ack = emission(service, "off")
        assert ack["ok"] and not analyser["device"].emission


class TestTheWatchdog:
    def on(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        assert emission(service, "on")["ok"]
        service.read()
        return service

    def test_p105_above_the_limit_switches_it_off_at_once(self, monkeypatch, tmp_path, analyser):
        service = self.on(monkeypatch, tmp_path, analyser)
        p105(service, 7e-4)
        service.read()
        assert not analyser["device"].emission
        audit = service.bus.last_json("xams/audit")
        assert audit["action"] == "rga_emission_off" and audit["actor"] == "rga service"
        assert "guard" in service.bus.last_json("xams/rga/info")["emission"]["note"]

    def test_a_stale_p105_is_given_a_grace_period(self, monkeypatch, tmp_path, analyser):
        service = self.on(monkeypatch, tmp_path, analyser)
        service._guard_seen -= 31 + 30         # stale, but inside the grace
        service.read()
        assert analyser["device"].emission
        service._guard_seen -= 31              # now past it
        service.read()
        assert not analyser["device"].emission

    def test_a_good_p105_leaves_it_on(self, monkeypatch, tmp_path, analyser):
        service = self.on(monkeypatch, tmp_path, analyser)
        for _ in range(3):
            p105(service, 2e-5)
            service.read()
        assert analyser["device"].emission

    def test_an_hour_without_anything_switches_it_off(self, monkeypatch, tmp_path, analyser):
        service = self.on(monkeypatch, tmp_path, analyser)
        p105(service, 2e-5)
        service._last_activity -= 3601
        service.read()
        assert not analyser["device"].emission

    def test_control_is_not_given_up_while_emission_is_on(self, monkeypatch, tmp_path, analyser):
        service = self.on(monkeypatch, tmp_path, analyser)
        p105(service, 2e-5)
        service._last_activity -= 400          # past release_idle_s, inside the hour
        service.read()
        assert analyser["device"].emission
        assert not [x for x in analyser["device"].sets if x.endswith("control/set?release")]

    def test_emission_on_from_another_client_is_left_alone(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser, other="PVMassSpec")
        analyser["device"].emission = True
        p105(service, 7e-4)
        service.read()
        assert analyser["device"].emission     # cannot write without control


class TestEmissionIsNeverLeftBehind:
    def test_release_switches_it_off_first(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        emission(service, "on")
        service.read()
        ack = command(service, "_handle_control", "xams/ack/rga/control", action="release")
        assert ack["ok"] and "emission switched off first" in ack["detail"]
        sets = analyser["device"].sets
        assert sets.index("/mmsp/generalControl/setEmission/set?Off") < \
            sets.index("/mmsp/communication/control/set?release")

    def test_a_stopping_service_switches_it_off(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        emission(service, "on")
        device = analyser["device"]
        service.close()
        assert not device.emission
        assert device.sets[-1] == "/mmsp/communication/control/set?release"


class TestCommandsRunOffTheBusThread:
    def test_a_bus_callback_only_queues(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        callback = service._enqueue(service._handle_control)
        callback("xams/cmd/rga/control", json.dumps({"action": "take", "by": "t"}))
        assert service.bus.last_json("xams/ack/rga/control") is None   # not run yet
        service._commands.put(None)
        service._work()
        assert service.bus.last_json("xams/ack/rga/control")["ok"]


class TestThePageLayout:
    def page_with(self, webui, emission=0.0, guard_ok=True):
        client, app, bus = webui
        bus.deliver(TOPIC_RGA_INFO, json.dumps({
            "identity": {"name": "RGA PrismaPro A 200 47504651", "detector": "CDEM"},
            "control": {"controlled": True, "us": True, "client": "xams-sc"},
            "recipes": {"quick": {"description": "", "scans": 2, "seconds": 6.3}},
            "emission": {"on": bool(emission), "interlock": False, "note": None},
            "guard": {"channel": "p105", "value": 1.9e-5 if guard_ok else 7e-4,
                      "unit": "mbar", "quality": "ok", "age_s": 3.0,
                      "limit_mbar": 5e-4, "ok": guard_ok,
                      "reason": "p105 = 1.90e-05 mbar < 5.0e-04 mbar" if guard_ok
                      else "p105 = 7.00e-04 mbar, at or above the limit 5.0e-04 mbar"}}))
        deliver(bus, "rga_control", 1.0, "code")
        deliver(bus, "rga_emission", emission, "bool")
        return client.get("/rga").text

    def test_identity_and_state_share_a_row_and_controls_sit_left_of_the_spectrum(self, webui):
        body = self.page_with(webui)
        pair, main = body.index('class="rga-pair"'), body.index('class="rga-main"')
        assert pair < body.index("Residual gas analyser") < body.index(">State<") < main
        controls, spectrum = body.index(">Controls<"), body.index(">Spectrum<")
        assert main < controls < spectrum
        for action in ('action="/rga/control"', 'action="/rga/emission"', 'action="/rga/scan"'):
            assert controls < body.index(action) < spectrum

    def test_emission_on_is_offered_only_past_the_guard(self, webui):
        body = self.page_with(webui, guard_ok=False)
        button = body[body.index('value="on"'):]
        assert "disabled" in button[:button.index("</button>")]
        assert "at or above the limit" in body

    def test_with_emission_on_the_button_switches_it_off(self, webui):
        body = self.page_with(webui, emission=1.0)
        assert 'value="off"' in body and "Switch emission off" in body


# ============================================================ keeping data
#
# An unsaved survey was lost on 13 October 2026 when the service was restarted
# for a deployment: the acquisition lived only in memory.

class TestAnUnsavedAcquisitionSurvivesARestart:
    def test_it_is_restored_and_can_still_be_saved(self, monkeypatch, tmp_path, analyser):
        first = running(monkeypatch, tmp_path, analyser)
        acquire(first)
        assert (tmp_path / "rga" / "pending.json").exists()

        second = running(monkeypatch, tmp_path, analyser)      # the restart
        acq = second.bus.last_json("xams/rga/info")["acquisition"]
        assert (acq["state"], acq["done"]) == ("done", 2)
        assert len(second.bus.last_json("xams/rga/scan")["values"]) == QUICK_POINTS
        assert command(second, "_handle_save", "xams/ack/rga/save")["ok"]
        assert not (tmp_path / "rga" / "pending.json").exists()

    def test_one_cut_off_mid_scan_comes_back_as_stopped(self, monkeypatch, tmp_path, analyser):
        first = running(monkeypatch, tmp_path, analyser)
        command(first, "_handle_control", "xams/ack/rga/control", action="take")
        first.read()
        command(first, "_handle_scan", "xams/ack/rga/scan", action="start", recipe="quick")
        analyser["device"].queue = analyser["device"].queue[:1]
        analyser["device"].scanning = True
        first.read()                                           # one scan in
        second = running(monkeypatch, tmp_path, analyser)
        acq = second.bus.last_json("xams/rga/info")["acquisition"]
        assert acq["state"] == "stopped" and acq["done"] == 1
        assert "restarted" in acq["error"]

    def test_a_saved_one_is_not_restored(self, monkeypatch, tmp_path, analyser):
        first = running(monkeypatch, tmp_path, analyser)
        acquire(first)
        command(first, "_handle_save", "xams/ack/rga/save")
        second = running(monkeypatch, tmp_path, analyser)
        assert second.bus.last_json("xams/rga/info")["acquisition"] is None


class TestSavedFilesAreListed:
    def test_a_save_appears_in_the_list(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        acquire(service)
        ack = command(service, "_handle_save", "xams/ack/rga/save",
                      comment="pump-down", tags=["test"])
        files = service.bus.last_json("xams/rga/info")["saved_files"]
        assert files[0]["file"] == ack["file"]
        assert files[0]["comment"] == "pump-down" and files[0]["scans"] == 2

    def test_pending_is_not_listed_as_saved(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        acquire(service)
        assert rga_file.files(tmp_path / "rga") == []


class TestTheDownload:
    def setup_file(self, monkeypatch, tmp_path):
        from xams_sc import config as config_module
        monkeypatch.setattr(config_module, "ROOT", tmp_path)
        name = "2026-10-13T150521_quick.json"
        (tmp_path / "data" / "rga" / "2026").mkdir(parents=True)
        (tmp_path / "data" / "rga" / "2026" / name).write_text('{"format": "xams-rga/1"}')
        return name

    def test_a_saved_file_is_served(self, webui, monkeypatch, tmp_path):
        name = self.setup_file(monkeypatch, tmp_path)
        r = webui[0].get(f"/rga/file/{name}")
        assert r.status_code == 200 and r.json()["format"] == "xams-rga/1"

    @pytest.mark.parametrize("name", ["..%2F..%2Fconfig%2Fsecrets.yaml", "pending.json",
                                      "2026-10-13T150521_quick.yaml", "x.json"])
    def test_nothing_else_is(self, webui, monkeypatch, tmp_path, name):
        self.setup_file(monkeypatch, tmp_path)
        assert webui[0].get(f"/rga/file/{name}").status_code == 404


class TestTheSaveSectionIsAlwaysThere:
    def test_with_nothing_to_save_it_says_so(self, client):
        body = client.get("/rga").text
        assert ">Save<" in body and "nothing to save" in body
