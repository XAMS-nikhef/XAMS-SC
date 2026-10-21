"""The 9226 read faster and less often (21 October 2026, later).

Read in the background, p102-p104 were still about twice the morning noise:
the 9226 converting disturbs the 9207 itself. It is now read every 10 s with
the high-speed ADC timing mode. What is pinned: the config asks for that, an
unknown mode is refused at start, the mode is set on every channel, and a
module that refuses the mode keeps its temperatures with the default.
"""

import enum

import pytest

from doubles import RecordingBus
from xams_sc.config import load
from xams_sc.devices.cdaq import ADC_TIMING, CdaqService, ModuleTask


class Mode(enum.Enum):
    AUTOMATIC = 0
    HIGH_RESOLUTION = 1
    HIGH_SPEED = 2


class Chan:
    def __init__(self, refuse=False):
        self.refuse, self._mode = refuse, None

    @property
    def ai_adc_timing_mode(self):
        return self._mode

    @ai_adc_timing_mode.setter
    def ai_adc_timing_mode(self, v):
        if self.refuse:
            raise RuntimeError("-200077 requested value is not supported")
        self._mode = v


class Task:
    def __init__(self, n, refuse=False):
        self.ai_channels = [Chan(refuse) for _ in range(n)]


def test_the_9226_is_read_every_10_s_at_high_speed():
    m = {m.alias: m for m in CdaqService(load(), RecordingBus(), simulate=False)._modules}
    assert m["9226"].interval_s == 10 and m["9226"].adc_timing == "high_speed"
    assert m["9207"].adc_timing is None          # the pressures keep the default
    assert m["9216_1"].adc_timing is None


def test_an_unknown_mode_is_refused_at_start(monkeypatch):
    cfg = load()
    for spec in cfg.devices["cdaq"]["modules"]:
        if spec["alias"] == "9226":
            monkeypatch.setitem(spec, "adc_timing", "fastest")
    with pytest.raises(ValueError, match="adc_timing"):
        CdaqService(cfg, RecordingBus(), simulate=False)


def test_the_mode_is_set_on_every_channel():
    task = Task(6)
    CdaqService._set_adc_timing(ModuleTask("9226", "NI9226", [], adc_timing="high_speed"),
                                task, Mode)
    assert all(c.ai_adc_timing_mode is Mode.HIGH_SPEED for c in task.ai_channels)


def test_a_refused_mode_keeps_the_default(caplog):
    task = Task(2, refuse=True)
    CdaqService._set_adc_timing(ModuleTask("9226", "NI9226", [], adc_timing="high_speed"),
                                task, Mode)                 # does not raise
    assert "refused" in caplog.text


def test_every_mode_name_exists_in_nidaqmx():
    nidaqmx = pytest.importorskip("nidaqmx.constants")
    for member in ADC_TIMING.values():
        assert hasattr(nidaqmx.ADCTimingMode, member)
