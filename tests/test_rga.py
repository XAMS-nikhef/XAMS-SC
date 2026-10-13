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
from xams_sc.devices import prismapro, rga
from xams_sc.devices.prismapro import (MMSPClient, MMSPError, decode_system_status,
                                       emission_on, read_identity)
from xams_sc.devices.rga import CONTROL_NONE, CONTROL_OTHER, READERS, RgaService
from xams_sc.model import Measurement, Quality, utcnow

RECORDED = json.loads(
    (pathlib.Path(__file__).parent / "recorded" / "prismapro_2026-10-13.json")
    .read_text(encoding="utf-8"))


class RecordedClient(MMSPClient):
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

    def test_the_client_has_no_way_to_write(self):
        """Step 1 is status only. A write path is added deliberately, later,
        behind a whitelist - it must not already be lying here."""
        source = inspect.getsource(prismapro)
        assert not [n for n in dir(MMSPClient) if n.startswith(("set", "put", "post"))]
        assert "/set" not in source and '"POST"' not in source


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
    monkeypatch.setattr(rga, "MMSPClient",
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
                           "ip": "192.16.192.20", "locked": True}

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

    def test_the_page_writes_to_nothing(self, client):
        """The site-wide operator box is on every page; nothing of the RGA's."""
        body = client.get("/rga").text
        assert 'action="/rga' not in body and 'method="post"' not in body.split("<main")[-1]
