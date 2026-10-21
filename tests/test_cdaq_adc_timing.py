"""The 9226 read faster and less often (21 October 2026, later).

Read in the background, p102-p104 were still about twice the morning noise:
the 9226 converting disturbs the 9207 itself. It is now read every 10 s with
the high-speed ADC timing mode. What is pinned: the config asks for that, an
unknown mode is refused at start, the mode is set on all channels at once, and a
module that will not start with it is rebuilt without, so it still reads.
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


class All:
    """`task.ai_channels.all`: one property set for every channel at once."""
    def __init__(self, task):
        self.task = task

    @property
    def ai_adc_timing_mode(self):
        return self.task.mode

    @ai_adc_timing_mode.setter
    def ai_adc_timing_mode(self, v):
        self.task.mode = v


class Channels:
    def __init__(self, task):
        self.all = All(task)


class Task:
    """Refuses to START with a mode, as the 9226 did with high speed set
    channel by channel (-200106, 21 October 2026)."""
    def __init__(self, refuse_mode=False):
        self.mode, self.refuse_mode = None, refuse_mode
        self.started = self.closed = False
        self.ai_channels = Channels(self)

    def start(self):
        if self.refuse_mode and self.mode is not None:
            raise RuntimeError("-200106 Property must have the same value for all channels")
        self.started = True

    def close(self):
        self.closed = True


def service():
    return CdaqService(load(), RecordingBus(), simulate=False)


def mod9226():
    return ModuleTask("9226", "NI9226", [], adc_timing="high_speed")


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


def test_the_mode_is_set_on_all_channels_at_once_and_the_task_started():
    task = Task()
    out = service()._start_task(mod9226(), task, Mode)
    assert out is task and task.started and task.mode is Mode.HIGH_SPEED


def test_a_module_without_a_mode_is_started_untouched():
    task = Task()
    out = service()._start_task(ModuleTask("9207", "NI9207", []), task, Mode)
    assert out is task and task.started and task.mode is None


def test_a_task_that_will_not_start_with_the_mode_is_rebuilt_without(monkeypatch, caplog):
    """The failure of 21 October: the mode made the task refuse to start and
    every cDAQ reading went down. Now the temperatures come up regardless."""
    svc = service()
    first, second = Task(refuse_mode=True), Task(refuse_mode=True)
    monkeypatch.setattr(svc, "_new_task", lambda mod: second)
    out = svc._start_task(mod9226(), first, Mode)
    assert first.closed and not first.started
    assert out is second and second.started and second.mode is None
    assert "refused" in caplog.text


def test_every_mode_name_exists_in_nidaqmx():
    nidaqmx = pytest.importorskip("nidaqmx.constants")
    for member in ADC_TIMING.values():
        assert hasattr(nidaqmx.ADCTimingMode, member)
