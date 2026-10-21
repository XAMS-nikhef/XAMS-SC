"""Slow cDAQ modules read in the background (21 October 2026).

An RTD read takes seconds. Polled in the same 1-s loop as the 9207 it left
fewer pressure readings in each 10-s mean: p102-p104 were 3.5x noisier as soon
as the 9226 was read again, and back to normal with the module in but unread.
What is pinned here: the RTD modules are background modules, a slow background
read never holds up read(), its readings still arrive, and a failing background
module costs only its own channels.
"""

import time

import pytest

from doubles import RecordingBus
from xams_sc.config import load
from xams_sc.devices.cdaq import CdaqService
from xams_sc.model import Quality


class FakeTask:
    def __init__(self, values, delay=0.0, fail=False):
        self.values, self.delay, self.fail, self.reads = values, delay, fail, 0

    def read(self):
        time.sleep(self.delay)
        self.reads += 1
        if self.fail:
            raise RuntimeError("device not present")
        return list(self.values)

    def close(self):
        pass


@pytest.fixture
def service(tmp_path):
    svc = CdaqService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
    yield svc
    svc._close_tasks()


def mods(svc):
    return {m.alias: m for m in svc._modules}


def wire(svc, slow_delay=0.0, fail=None, interval=0.05):
    """Fake tasks: 9207 voltages, RTD modules at a plausible 20 C."""
    for m in svc._modules:
        if m.alias == "9207":
            vals = [0.012 if c.kind == "current" else 2.0 for c in m.channels]
            m.task = FakeTask(vals)
        else:
            m.task = FakeTask([20.0] * len(m.channels), delay=slow_delay,
                              fail=(m.alias == fail))
            m.interval_s = interval


def test_rtd_modules_are_background_and_the_9207_is_not():
    svc = CdaqService(load(), RecordingBus(), simulate=False)
    m = mods(svc)
    assert not m["9207"].background
    assert m["9226"].background and m["9216_1"].background


def test_a_slow_rtd_read_does_not_hold_up_the_loop(service):
    wire(service, slow_delay=0.5)
    service._start_background()
    t0 = time.monotonic()
    out = service.read()
    assert time.monotonic() - t0 < 0.2          # the 0.5-s RTD reads are not waited for
    assert {m.channel for m in out} >= {c.name for c in mods(service)["9207"].channels}


def test_background_readings_arrive_with_the_next_read(service):
    wire(service)
    service._start_background()
    time.sleep(0.3)
    names = {m.channel: m for m in service.read()}
    for alias in ("9226", "9216_1"):
        for ch in mods(service)[alias].channels:
            assert names[ch.name].quality is Quality.OK
            assert names[ch.name].value == pytest.approx(20.0)


def test_each_background_reading_is_handed_over_once(service):
    wire(service, interval=10.0)                # one read, then a long wait
    service._start_background()
    time.sleep(0.2)
    first = [m for m in service.read() if m.channel == "tt201"]
    second = [m for m in service.read() if m.channel == "tt201"]
    assert len(first) == 1 and second == []


def test_a_failing_background_module_costs_only_its_own_channels(service):
    wire(service, fail="9226")
    service._start_background()
    time.sleep(0.2)
    out = {m.channel: m for m in service.read()}
    assert out["tt201"].quality is Quality.ERROR and out["tt201"].value is None
    assert out["p102"].quality is Quality.OK
    assert out["pmain"].quality is Quality.OK


def test_threads_stop_before_the_tasks_close(service):
    wire(service, slow_delay=0.1)
    service._start_background()
    assert service._bg_threads
    service._close_tasks()
    assert service._bg_threads == []
    assert all(m.task is None for m in service._modules)


def test_read_times_are_counted_per_module(service):
    wire(service)
    service._start_background()
    time.sleep(0.2)
    service.read()
    m = mods(service)
    assert m["9207"].timing[0] >= 1 and m["9226"].timing[0] >= 1
