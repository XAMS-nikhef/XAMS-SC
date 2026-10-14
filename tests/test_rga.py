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
            "rga_emission": 0.0, "rga_em": 0.0, "rga_control": float(CONTROL_NONE),
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

    def test_the_page_never_reloads_itself_whole(self, client):
        """A reload emptied the Save form (13 October 2026): the page swaps
        its changing parts in place instead, and opts out of base.html's."""
        body = client.get("/rga").text
        assert "location.reload" not in body
        assert "rga-controls" in body and "DOMParser" in body

    def test_a_pressed_button_greys_the_controls_until_the_answer(self, client):
        """13 October 2026: actions take seconds, and a button still live in
        that time invited a second click."""
        body = client.get("/rga").text
        assert "addEventListener('submit'" in body and "working…" in body
        assert "if (busy) return;" in body

    def test_the_page_switches_the_em_but_sets_nothing_of_it(self, client):
        """Step 4: EM On/Off yes; its voltage and gain are not on the page."""
        body = client.get("/rga").text.lower()
        assert 'action="/rga/em"' in body
        assert "emvoltage" not in body and "emgain" not in body


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
        self.em = False
        self.em_interlock = False
        self.em_equiv = 0

    def status(self):
        word = 0x01103B90 | (2 if self.scanning else 0)
        if self.emission:
            word |= 1 << 31
        if self.interlock:
            word |= 1 << 27
        if self.em:
            word |= 1 << 23
        if self.em_interlock:
            word |= 1 << 21
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
                if not self.emission:
                    self.em = False        # the device stops the EM with emission
            elif target == "generalControl/setEM":
                self.em = query == "On"
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
        if target == "generalControl/setEM":
            return self._event(path, "On" if self.em else "Off")
        extra = {"sensorIonSource/emissionCurrent": 2000,
                 "sensorDetector/emVoltage": 800,
                 "sensorDetector/emGain": 1234.57,
                 "sensorDetector/emGainMass": 2800,
                 "generalControl/emEquivIonSet": self.em_equiv,
                 "sensorIonSource/electronEnergy": 70,
                 "sensorIonSource/ppSensitivityFactor": 0.000793007,
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
    def test_only_emission_and_em_are_writable_among_the_instrument_settings(self):
        assert sorted(t for t in WRITABLE if t.startswith(
            ("generalControl", "sensorIonSource", "sensorDetector", "digital",
             "analog", "userDB", "system"))) == ["generalControl/setEM",
                                                 "generalControl/setEmission"]

    @pytest.mark.parametrize("target", ["sensorDetector/emVoltage",
                                        "sensorDetector/emGain",
                                        "generalControl/emEquivIonSet",
                                        "generalControl/degas",
                                        "sensorIonSource/emissionCurrent",
                                        "sensorIonSource/filamentSelected"])
    def test_a_target_outside_it_is_refused_before_anything_is_sent(self, target):
        device = FakeAnalyser("x")
        with pytest.raises(PermissionError):
            device._set(target, {"v": "On"})
        assert device.sets == []

    @pytest.mark.parametrize("target", ["generalControl/setEmission",
                                        "generalControl/setEM"])
    @pytest.mark.parametrize("value", ["Standby", "on", "1", "Degas"])
    def test_emission_and_em_take_on_or_off_and_nothing_else(self, target, value):
        device = FakeAnalyser("x")
        with pytest.raises(PermissionError):
            device._set(target, {"v": value})
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
        assert save_row[-1] == "Faraday"           # the detector column

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
        (4e-4, "ok", "mbar", "at or above the limit"),
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
    def page_with(self, webui, emission=0.0, guard_ok=True, wanted=None):
        client, app, bus = webui
        bus.deliver(TOPIC_RGA_INFO, json.dumps({
            "identity": {"name": "RGA PrismaPro A 200 47504651", "detector": "CDEM"},
            "control": {"controlled": True, "us": True, "client": "xams-sc"},
            "recipes": {"quick": {"description": "", "scans": 2, "seconds": 6.3}},
            "emission": {"on": bool(emission), "interlock": False, "note": None,
                         "pending": wanted, "wanted": wanted},
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

    def test_on_off_follows_the_live_record_not_the_10_s_channel(self, webui):
        """13 October 2026: the channel (a 10-s mean) still said off after the
        service's record had stopped saying "switching", so "Switch emission
        on" came back for a few seconds while emission was on."""
        client, app, bus = webui
        from datetime import datetime, timezone
        bus.deliver(TOPIC_RGA_INFO, json.dumps({
            "t": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "identity": {"name": "RGA PrismaPro A 200 47504651"},
            "control": {"controlled": True, "us": True, "client": "xams-sc"},
            "recipes": {}, "emission": {"on": True, "pending": None},
            "em": {"on": False, "pending": None},
            "guard": {"ok": True, "em_ok": True, "reason": "", "em_reason": ""}}))
        deliver(bus, "rga_control", 1.0, "code")
        deliver(bus, "rga_emission", 0.0, "bool")          # not caught up yet
        body = client.get("/rga").text
        assert "Switch emission off" in body and "Switch emission on" not in body

    @pytest.mark.parametrize("emission, wanted, label", [
        (0.0, "On", "Emission starting…"), (1.0, "Off", "Emission stopping…")])
    def test_while_switching_no_emission_button_can_be_pressed(
            self, webui, emission, wanted, label):
        body = self.page_with(webui, emission=emission, wanted=wanted)
        assert label in body
        assert 'action="/rga/emission"' not in body
        assert 'data-pending="yes"' in body


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


class TestPeakWindow:
    """13 October 2026: a large C3F5+ peak at 131.2 (the analyser reads ~0.2
    amu high) and no xenon. Its tail at 131.5 must not count as Xe-132."""
    RECIPE = {"channels": [{"mode": "sweep", "start": 129, "stop": 133, "ppamu": 10, "dwell": 32}]}

    def values(self, peaks):
        ms = rga_file.masses(self.RECIPE)
        return [sum(h * max(0.0, 1 - abs(m - c) / 0.45) for c, h in peaks) + 1e-14 for m in ms]

    def test_the_tail_of_131_is_not_132(self):
        v = self.values([(131.2, 6e-13)])
        assert rga_file.peak(self.RECIPE, v, 131) == pytest.approx(6e-13 + 1e-14)
        assert rga_file.peak(self.RECIPE, v, 132) < 1e-13

    def test_a_peak_read_high_is_still_found(self):
        v = self.values([(132.3, 5e-13)])
        assert rga_file.peak(self.RECIPE, v, 132) == pytest.approx(5e-13 + 1e-14)

    def test_69_is_a_summary_mass(self):
        from xams_sc.devices.rga import SUMMARY_MASSES
        assert 69 in SUMMARY_MASSES


# ================================================================ step 4
#
# The electron multiplier: its own, lower p105 limit (em_guard_limit_mbar,
# 1e-5; datasheet maximum 5e-5), only with emission on, never mid-scan, and
# always off before emission.

def em(service, action):
    return command(service, "_handle_em", "xams/ack/rga/em", action=action)


def emission_held(monkeypatch, tmp_path, analyser, p=5e-6):
    service = holding(monkeypatch, tmp_path, analyser, p=p)
    assert emission(service, "on")["ok"]
    service.read()
    return service


def em_running(monkeypatch, tmp_path, analyser):
    service = emission_held(monkeypatch, tmp_path, analyser)
    assert em(service, "on")["ok"]
    service.read()
    assert service.em_is_on()
    return service


class TestEmLimitConfiguration:
    @pytest.mark.parametrize("limit", [6e-5, 1e-3, 0, -1e-6])
    def test_a_limit_above_the_datasheet_or_not_positive_is_refused(
            self, monkeypatch, tmp_path, limit):
        config = load()
        config.devices["rga"] = dict(config.devices["rga"], em_guard_limit_mbar=limit,
                                     data_dir=str(tmp_path / "rga"))
        monkeypatch.setattr(rga, "ROOT", pathlib.Path("/"))
        with pytest.raises(ValueError, match="em_guard_limit_mbar"):
            RgaService(config, RecordingBus(), lock_dir=pathlib.Path(tempfile.mkdtemp()))

    def test_a_limit_above_the_emission_limit_is_refused(self, monkeypatch, tmp_path):
        config = load()
        config.devices["rga"] = dict(config.devices["rga"], guard_limit_mbar=2e-5,
                                     em_guard_limit_mbar=3e-5,
                                     data_dir=str(tmp_path / "rga"))
        monkeypatch.setattr(rga, "ROOT", pathlib.Path("/"))
        with pytest.raises(ValueError, match="emission limit"):
            RgaService(config, RecordingBus(), lock_dir=pathlib.Path(tempfile.mkdtemp()))

    def test_the_configured_limits_are_a_little_under_the_datasheet(self, config):
        """4e-4 and 4e-5 mbar (A.P. Colijn, 13 October 2026); datasheet 5e-4
        (Faraday) and 5e-5 (C-SEM)."""
        assert config.devices["rga"]["guard_limit_mbar"] == pytest.approx(4e-4)
        assert config.devices["rga"]["em_guard_limit_mbar"] == pytest.approx(4e-5)

    @pytest.mark.parametrize("limit", [6e-4, 1e-3, 0])
    def test_an_emission_limit_above_the_datasheet_is_refused(
            self, monkeypatch, tmp_path, limit):
        config = load()
        config.devices["rga"] = dict(config.devices["rga"], guard_limit_mbar=limit,
                                     data_dir=str(tmp_path / "rga"))
        monkeypatch.setattr(rga, "ROOT", pathlib.Path("/"))
        with pytest.raises(ValueError, match="guard_limit_mbar"):
            RgaService(config, RecordingBus(), lock_dir=pathlib.Path(tempfile.mkdtemp()))


class TestEmOn:
    def test_with_emission_on_and_p105_below_its_limit_it_goes_on(
            self, monkeypatch, tmp_path, analyser):
        service = emission_held(monkeypatch, tmp_path, analyser)
        ack = em(service, "on")
        assert ack["ok"] and "5.00e-06" in ack["detail"], ack
        assert analyser["device"].em
        service.read()
        info = service.bus.last_json("xams/rga/info")
        assert info["em"]["on"] and info["em"]["gain"] == 1234.57
        assert service.bus.last_json("xams/audit")["action"] == "rga_em_on"

    def test_between_the_two_limits_emission_yes_em_no(self, monkeypatch, tmp_path, analyser):
        """p105 = 1e-4 mbar: fine for emission, too high for the EM."""
        service = emission_held(monkeypatch, tmp_path, analyser, p=1e-4)
        ack = em(service, "on")
        assert not ack["ok"] and "EM limit" in ack["reason"] and "4.0e-05" in ack["reason"]
        assert not analyser["device"].em
        guard = service.bus.last_json("xams/rga/info")["guard"]
        assert guard["ok"] and not guard["em_ok"]

    def test_at_exactly_the_limit_it_is_refused(self, monkeypatch, tmp_path, analyser):
        service = emission_held(monkeypatch, tmp_path, analyser, p=4e-5)
        assert not em(service, "on")["ok"]

    def test_just_under_the_limit_it_goes_on(self, monkeypatch, tmp_path, analyser):
        """2e-5 mbar, the p105 of 13 October 2026: allowed with 4e-5."""
        service = emission_held(monkeypatch, tmp_path, analyser, p=2e-5)
        assert em(service, "on")["ok"]

    def test_not_with_emission_off(self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser, p=5e-6)
        ack = em(service, "on")
        assert not ack["ok"] and "emission is off" in ack["reason"]

    def test_not_without_control(self, monkeypatch, tmp_path, analyser):
        service = running(monkeypatch, tmp_path, analyser)
        p105(service, 5e-6)
        ack = em(service, "on")
        assert not ack["ok"] and "take control first" in ack["reason"]

    def test_not_with_a_stale_p105(self, monkeypatch, tmp_path, analyser):
        service = emission_held(monkeypatch, tmp_path, analyser)
        service._guard_seen -= 31
        ack = em(service, "on")
        assert not ack["ok"] and "old" in ack["reason"]

    def test_not_during_a_scan(self, monkeypatch, tmp_path, analyser):
        service = emission_held(monkeypatch, tmp_path, analyser)
        assert command(service, "_handle_scan", "xams/ack/rga/scan",
                       action="start", recipe="survey")["ok"]
        ack = em(service, "on")
        assert not ack["ok"] and "scan is running" in ack["reason"]

    def test_not_with_the_em_interlock(self, monkeypatch, tmp_path, analyser):
        service = emission_held(monkeypatch, tmp_path, analyser)
        analyser["device"].em_interlock = True
        service.read()
        ack = em(service, "on")
        assert not ack["ok"] and "EM interlock" in ack["reason"]


class TestEmOff:
    def test_p105_reaching_the_em_limit_switches_only_the_em_off(
            self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        p105(service, 4e-5)
        service.read()
        device = analyser["device"]
        assert not device.em and device.emission
        assert "guard" in service.bus.last_json("xams/rga/info")["em"]["note"]

    def test_a_stale_p105_is_given_the_same_grace(self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        service._guard_seen -= 31 + 30
        service.read()
        assert analyser["device"].em
        service._guard_seen -= 31
        service.read()
        assert not analyser["device"].em

    def test_emission_off_from_the_page_takes_the_em_off_first(
            self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        assert emission(service, "off")["ok"]
        sets = analyser["device"].sets
        assert sets.index("/mmsp/generalControl/setEM/set?Off") < \
            sets.index("/mmsp/generalControl/setEmission/set?Off")

    def test_the_emission_guard_takes_the_em_off_first(self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        p105(service, 7e-4)
        service.read()
        sets = analyser["device"].sets
        assert sets.index("/mmsp/generalControl/setEM/set?Off") < \
            sets.index("/mmsp/generalControl/setEmission/set?Off")

    def test_release_takes_the_em_off(self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        assert command(service, "_handle_control", "xams/ack/rga/control",
                       action="release")["ok"]
        assert not analyser["device"].em and not analyser["device"].emission

    def test_a_stopping_service_takes_the_em_off(self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        device = analyser["device"]
        service.close()
        assert not device.em and not device.emission

    def test_off_from_the_page(self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        assert em(service, "off")["ok"]
        assert not analyser["device"].em and analyser["device"].emission


class TestEmAcquisition:
    def test_a_scan_with_the_em_is_marked_and_saved_as_such(
            self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        assert command(service, "_handle_scan", "xams/ack/rga/scan",
                       action="start", recipe="quick")["ok"]
        for _ in range(5):
            service.read()
        assert service.bus.last_json("xams/rga/scan")["gain"] == 1234.57
        assert command(service, "_handle_save", "xams/ack/rga/save", comment="em")["ok"]
        (path,) = rga_file.files(tmp_path / "rga")
        record = rga_file.load_rga(path)
        assert record["detector"] == "EM" and record["device"]["em_gain"] == 1234.57

    def test_the_em_going_off_mid_scan_ends_the_acquisition(
            self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        assert command(service, "_handle_scan", "xams/ack/rga/scan",
                       action="start", recipe="quick")["ok"]
        analyser["device"].queue = []          # running, no scan finished yet
        p105(service, 1e-4)
        service.read()
        acq = service.bus.last_json("xams/rga/info")["acquisition"]
        assert acq["state"] == "stopped" and "EM switched off" in acq["error"]

    def test_the_summary_is_on_the_ion_current_scale(self):
        """EM values come negative and times the gain (13 October 2026)."""
        recipe = {"name": "x", "channels": [{"mode": "single", "mass": 28, "dwell": 32}]}
        base = {"t_saved": "2026-10-13T20:00:00Z", "file": "f.json", "recipe": recipe,
                "device": {"em_gain": 1234.57, "em_equiv_ion": 0}}
        em_rec = rga_file.summary(base | {"detector": "EM", "mean": [-1234.57e-12]}, [28])
        assert em_rec["peaks"][0]["current_a"] == pytest.approx(1e-12)
        fc = rga_file.summary(base | {"detector": "Faraday", "mean": [2e-12]}, [28])
        assert fc["peaks"][0]["current_a"] == pytest.approx(2e-12)
        converted = rga_file.summary(
            base | {"detector": "EM", "mean": [3e-12],
                    "device": {"em_gain": 1234.57, "em_equiv_ion": 1}}, [28])
        assert converted["peaks"][0]["current_a"] == pytest.approx(3e-12)

    def test_a_negative_em_peak_is_found_as_a_peak(self):
        """The bug of 13 October 2026: the largest value near a mass, taken
        from raw negative EM values, was the point nearest zero."""
        recipe = {"name": "x", "channels": [{"mode": "sweep", "start": 27, "stop": 29,
                                             "ppamu": 10, "dwell": 32}]}
        ms = rga_file.masses(recipe)
        raw = [-6e-8 * max(0.0, 1 - abs(m - 28.1) / 0.4) - 1e-14 for m in ms]
        record = {"t_saved": "t", "file": "f.json", "recipe": recipe, "mean": raw,
                  "detector": "EM", "device": {"em_gain": 30.0, "em_equiv_ion": 0}}
        (row,) = rga_file.summary(record, [28])["peaks"]
        assert row["current_a"] == pytest.approx((6e-8 + 1e-14) / 30)

    def test_the_live_scan_carries_the_scale_and_says_uncalibrated(
            self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        assert command(service, "_handle_scan", "xams/ack/rga/scan",
                       action="start", recipe="quick")["ok"]
        for _ in range(5):
            service.read()
        scan = service.bus.last_json("xams/rga/scan")
        assert scan["scale"] == pytest.approx(-1 / 1234.57)
        assert scan["gain_calibrated"] is False
        assert service.bus.last_json("xams/rga/info")["em"]["gain_calibrated"] is False


class TestGrafanaDashboard:
    """grafana/dashboards-archive/rga.json, loaded with
    tools/save_dashboard.py --load xams-rga."""
    PATH = pathlib.Path(__file__).resolve().parents[1] / "grafana" / "dashboards-archive" / "rga.json"

    def dashboard(self):
        return json.loads(self.PATH.read_text(encoding="utf-8"))

    def test_it_is_the_xams_rga_dashboard_on_the_xams_datasource(self):
        d = self.dashboard()
        assert d["uid"] == "xams-rga" and "xams" in d["tags"]
        for panel in d["panels"]:
            assert panel["datasource"]["uid"] == "xams-postgres"
            (t,) = panel["targets"]
            assert t["rawQuery"] and t["editorMode"] == "code"

    def test_the_rga_panels_read_only_the_summary_tables(self):
        for panel in self.dashboard()["panels"]:
            sql = panel["targets"][0]["rawSql"]
            assert "rga_" in sql or "FROM meas WHERE channel = 'p105'" in sql
            assert not any(w in sql.upper() for w in ("INSERT", "UPDATE ", "DELETE", "DROP"))

    def test_all_is_a_subquery_never_the_loaded_list(self):
        """13 October 2026: Grafana 13 expanded All to "IN ()" before the
        options were fetched; every panel failed with a syntax error."""
        for v in self.dashboard()["templating"]["list"]:
            if v.get("includeAll"):
                assert v["allValue"].upper().startswith("SELECT DISTINCT"), v["name"]
                assert v["refresh"] == 1 and len(v["options"]) > 1

    def test_the_detector_defaults_to_faraday(self):
        """EM values are relative while the gain is uncalibrated."""
        (det,) = [v for v in self.dashboard()["templating"]["list"] if v["name"] == "detector"]
        assert det["current"]["value"] == ["Faraday"]

    def test_131_is_not_a_summary_mass_on_it(self):
        """131 is mostly C3F5+ from PTFE: xenon is read from 129 and 132."""
        assert "WHEN 131" not in json.dumps(self.dashboard())


class TestOnTheVm:
    """The RGA summaries also go to the Nikhef VM, INSERT only (docs/vm.md)."""

    class Cursor:
        def __init__(self, log):
            self.log = log
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params=None):
            self.log.append(sql)

    class Conn:
        def __init__(self):
            self.log = []
        def cursor(self):
            return TestOnTheVm.Cursor(self.log)

    SUMMARY = {"t": "2026-10-13T19:19:56Z", "file": "2026-10-13T191448_survey.json",
               "recipe": "survey", "detector": "EM",
               "peaks": [{"mass": 28.0, "current_a": 5e-11, "pp_mbar": None}]}

    def test_the_vm_writer_never_updates_an_existing_row(self):
        conn = self.Conn()
        rga_writer.store(conn, self.SUMMARY, update=False)
        assert all("DO NOTHING" in sql and "DO UPDATE" not in sql for sql in conn.log)

    def test_the_local_writer_brings_the_detector_up_to_date(self):
        conn = self.Conn()
        rga_writer.store(conn, self.SUMMARY)
        assert "DO UPDATE SET detector" in conn.log[0]

    def test_the_rebuild_tool_will_not_replace_on_the_vm(self, capsys):
        import importlib.util
        path = pathlib.Path(__file__).resolve().parents[1] / "tools" / "rga_rebuild_summary.py"
        spec = importlib.util.spec_from_file_location("rga_rebuild_summary", path)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        assert tool.main(["--remote", "--replace"]) == 2
        assert "may only" in capsys.readouterr().err

    def test_the_vm_installer_grants_insert_and_key_columns_only(self):
        script = (pathlib.Path(__file__).resolve().parents[1] / "tools" / "vm"
                  / "install_vm.sh").read_text(encoding="utf-8")
        assert "GRANT INSERT ON rga_saves, rga_summary TO xams_writer;" in script
        grants = [l for l in script.splitlines() if "xams_writer" in l and "rga_" in l]
        assert not [l for l in grants if "UPDATE" in l or "DELETE" in l]
        assert "--load --missing" in script


class TestSwitchingIsShownUntilTheStateIsReached:
    """13 October 2026: 'working...' went away before emission was on - the
    analyser's own pending bits did not cover the seconds in between."""

    def test_emission_on_stays_starting_until_the_analyser_reports_it(
            self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        device = analyser["device"]
        assert emission(service, "on")["ok"]
        # Published before the acknowledgement, for the page that follows it.
        assert service.bus.last_json("xams/rga/info")["emission"]["wanted"] == "On"
        device.emission = False                # the filament has not lit yet
        service.read()
        info = service.bus.last_json("xams/rga/info")["emission"]
        assert info["pending"] == "On" and not info["on"]
        assert service.interval_s == service._scan_interval   # reading fast
        device.emission = True
        service.read()
        info = service.bus.last_json("xams/rga/info")["emission"]
        assert info["on"] and info["pending"] is None
        assert service.interval_s == service._idle_interval

    def test_a_state_never_reached_is_given_up_with_a_note(
            self, monkeypatch, tmp_path, analyser):
        service = holding(monkeypatch, tmp_path, analyser)
        assert emission(service, "on")["ok"]
        analyser["device"].emission = False
        monkeypatch.setattr(rga, "SWITCH_WAIT_S", 0.0)
        service.read()
        info = service.bus.last_json("xams/rga/info")["emission"]
        assert info["pending"] is None and "did not follow" in info["note"]

    def test_em_off_shows_stopping_until_it_is_off(self, monkeypatch, tmp_path, analyser):
        service = em_running(monkeypatch, tmp_path, analyser)
        device = analyser["device"]
        assert em(service, "off")["ok"]
        device.em = True                       # still on, for now
        service.read()
        assert service.bus.last_json("xams/rga/info")["em"]["wanted"] == "Off"
        device.em = False
        service.read()
        assert service.bus.last_json("xams/rga/info")["em"]["pending"] is None
