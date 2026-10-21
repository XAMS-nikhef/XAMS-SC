"""LN2 dewar scale (ISCI / Dini Argeo DFW06). No hardware required.

The answers below were read from the real indicator on the lab PC on
21 October 2026 (COM8, 9600 8-N-1), dewar on the platform, nothing tared.
"""

import pathlib
import tempfile

import pytest

from doubles import RecordingBus
from xams_sc.config import load
from xams_sc.devices import scale
from xams_sc.devices.scale import (DiniScale, ScaleError, ScaleService,
                                   parse_alim, parse_rall, parse_weight)
from xams_sc.devices.serial_id import Candidate
from xams_sc.model import Quality

RECORDED = {
    "VER": "VER,311,DFW06   ",
    "RALL": "ST,1, 278.20kg, 278.20kg,     0.00kg,   0.00kg,000,001,0000,000,00000-000000",
    "ALIM": "PW: 1 BT: 9",
}


class FakeSerial:
    """The port as DiniScale uses it: write a line, read one back."""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.writes = []
        self._next = b""

    def reset_input_buffer(self):
        self._next = b""

    def write(self, data):
        self.writes.append(data)
        command = data.decode("ascii").strip()
        answer = self.answers.get(command)
        self._next = b"" if answer is None else (answer + "\r\n").encode("ascii")

    def read_until(self, terminator, size):
        out, self._next = self._next, b""
        return out

    def close(self):
        pass


def device_with(answers):
    d = DiniScale("COM8")
    d._serial = FakeSerial(answers)
    return d


def service(answers=RECORDED, simulate=False):
    s = ScaleService(load(), RecordingBus(), simulate=simulate,
                     lock_dir=pathlib.Path(tempfile.mkdtemp()))
    if not simulate:
        s._device = device_with(answers)
    return s


def by_name(measurements):
    return {m.channel: m for m in measurements}


class TestParsing:
    def test_the_recorded_rall_answer(self):
        r = parse_rall(RECORDED["RALL"])
        assert r == {"status": "ST", "net": 278.20, "gross": 278.20, "tare": 0.0}

    def test_a_preset_tare_field(self):
        r = parse_rall("ST,1,   3.500kg,   5.000kg,PT 1.500kg,   3.500kg,000")
        assert (r["net"], r["gross"], r["tare"]) == (3.5, 5.0, 1.5)

    def test_unstable_still_carries_a_weight(self):
        """LN2 boils: the platform is rarely perfectly still."""
        assert parse_rall(RECORDED["RALL"].replace("ST,", "US,", 1))["gross"] == 278.20

    @pytest.mark.parametrize("status", ["OL", "UL", "TL"])
    def test_overload_underload_tilt_are_not_weights(self, status):
        with pytest.raises(ScaleError):
            parse_rall(RECORDED["RALL"].replace("ST,", status + ",", 1))

    def test_pounds_are_refused_not_converted(self):
        """A scale switched to lb would read 2.2 times heavy and look fine."""
        with pytest.raises(ScaleError):
            parse_weight(" 613.33lb")

    def test_garbage_is_not_a_reading(self):
        for text in ("", "ERR04", "ST,1", "XX,1, 1.0kg, 1.0kg, 0.0kg"):
            with pytest.raises(ScaleError):
                parse_rall(text)

    def test_alim(self):
        assert parse_alim("PW: 1 BT: 9") == (True, 9)
        assert parse_alim("PW: 0 BT: 4") == (False, 4)
        with pytest.raises(ScaleError):
            parse_alim("PW: 11895 BT: 7836")       # that is ALIMN


class TestOnlyQueries:
    @pytest.mark.parametrize("command", ["TARE", "T", "ZERO", "Z", "C", "CLEAR",
                                         "KEYP04", "DISP", "TMAN", "CMDSAVE",
                                         "PID", "ALDL", "PRNT"])
    def test_a_command_that_acts_never_reaches_the_port(self, command):
        d = device_with(RECORDED)
        with pytest.raises(ValueError):
            d.ask(command)
        assert d._serial.writes == []

    def test_a_query_goes_out_with_cr_lf(self):
        d = device_with(RECORDED)
        assert d.version() == "VER,311,DFW06"
        assert d._serial.writes == [b"VER\r\n"]

    def test_an_error_answer_is_no_answer(self):
        assert device_with({"RALL": "ERR04"}).ask("RALL") is None


class TestRead:
    def test_both_weights_from_one_gross_reading(self):
        m = by_name(service().read())
        assert m["ln2_gross"].value == pytest.approx(278.20)
        # ln2_mass = (gross - empty dewar) * 1, offset in channels.yaml
        assert m["ln2_mass"].value == pytest.approx(278.20 - 136.75)
        assert m["ln2_mass"].raw == pytest.approx(278.20)
        assert m["ln2_scale_mains"].value == 1.0
        assert all(x.quality is Quality.OK for x in m.values())

    def test_a_front_panel_tare_does_not_move_the_mass(self):
        """The net changes with a tare; the gross, which is used, does not."""
        tared = dict(RECORDED, RALL="ST,1,   0.00kg, 278.20kg,   278.20kg,"
                                    "   0.00kg,000,001,0000,000,00000-000000")
        m = by_name(service(tared).read())
        assert m["ln2_mass"].value == pytest.approx(278.20 - 136.75)

    def test_overload_is_published_as_error_not_a_number(self):
        bad = dict(RECORDED, RALL=RECORDED["RALL"].replace("ST,", "OL,", 1))
        m = by_name(service(bad).read())
        for name in ("ln2_gross", "ln2_mass"):
            assert m[name].quality is Quality.ERROR and m[name].value is None
        assert m["ln2_scale_mains"].quality is Quality.OK

    def test_no_answer_at_all_raises(self):
        """The loop needs the exception to mark everything and reconnect."""
        with pytest.raises(RuntimeError):
            service({"VER": RECORDED["VER"]}).read()

    def test_on_battery(self):
        m = by_name(service(dict(RECORDED, ALIM="PW: 0 BT: 7")).read())
        assert m["ln2_scale_mains"].value == 0.0

    def test_every_configured_channel_is_read(self):
        s = service()
        assert {m.channel for m in s.read()} == {c.name for c in s._channels}

    def test_simulated_readings_say_so(self):
        out = service(simulate=True).read()
        assert out and all(m.src == "sim" for m in out)


class TestIdentity:
    TURBO = Candidate("COM7", "0403", "6001", "DIBIURQ3A", "USB Serial Port")
    SCALE = Candidate("COM8", "0403", "6001", "AU05TI3PA", "USB Serial Port")

    def opened(self, monkeypatch, ports, ver=RECORDED["VER"]):
        touched = []

        class Probe(DiniScale):
            def open(self):
                touched.append(self.port)
                self._serial = FakeSerial({"VER": ver})

        monkeypatch.setattr(scale, "candidates", lambda vid, pid: list(ports))
        monkeypatch.setattr(scale, "DiniScale", Probe)
        s = ScaleService(load(), RecordingBus(),
                         lock_dir=pathlib.Path(tempfile.mkdtemp()))
        return s, touched

    def test_the_turbo_tap_is_never_opened(self, monkeypatch):
        """Same vid:pid; a VER on that port would go onto the pump's bus."""
        s, touched = self.opened(monkeypatch, [self.TURBO, self.SCALE])
        assert s.verify_identity()
        assert touched == ["COM8"]

    def test_scale_unplugged_refuses_without_probing_anything(self, monkeypatch):
        s, touched = self.opened(monkeypatch, [self.TURBO])
        assert not s.verify_identity()
        assert touched == []

    def test_another_instrument_on_the_port_refuses(self, monkeypatch):
        s, _ = self.opened(monkeypatch, [self.SCALE], ver="VER,100,DGT4")
        assert not s.verify_identity()

    def test_silence_refuses(self, monkeypatch):
        s, _ = self.opened(monkeypatch, [self.SCALE], ver=None)
        assert not s.verify_identity()
