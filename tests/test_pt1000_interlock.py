"""The PT1000 lock (28 October 2026, A.P. Colijn).

"I cannot switch ON HV ... when the PT1000 readout is ON. This prevents me to
forget to disconnect them." What is pinned, both ways round:

* the caen service refuses to switch ANY HV channel on while the PT1000
  readout is ON, or while its state is unknown - and never refuses OFF;
* the cdaq service refuses to switch the readout back ON while any HV output
  is energised, or while the HV state is unknown or old - and never refuses
  switching it off.
"""

import json
from datetime import timedelta

import pytest

from doubles import RecordingBus
from xams_sc.bus import ACK_HV_OUTPUT, STATUS_READOUT, TOPIC_HV_OUTPUT
from xams_sc.config import load
from xams_sc.devices.caen import CaenService
from xams_sc.devices.cdaq import CdaqService
from xams_sc.model import Measurement, Quality, utcnow


def readout(enabled):
    return json.dumps({"9226": {"enabled": enabled, "switch": "pt1000",
                                "label": "PT1000 readout", "by": "apc", "at": ""}})


class Supply:
    """Enough of a reader for an ON/OFF: every channel enabled, off until
    switched, and reporting what it was switched to."""
    def __init__(self):
        self.outputs = []
        self.on = {}

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()

    def status(self, channel):
        return 1 if self.on.get(channel) else 0

    def monitor(self, channel, par):
        return 0.0

    def set_output(self, channel, on):
        self.outputs.append((channel, on))
        self.on[channel] = on
        return True, ""


@pytest.fixture
def caen(tmp_path):
    svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
    svc._readers = {"hv_1": Supply(), "hv_2": Supply()}
    return svc


def switch(svc, channel, on):
    svc._handle_output(TOPIC_HV_OUTPUT, json.dumps(
        {"channel": channel, "on": on, "by": "apc"}))
    return svc.bus.last_json(ACK_HV_OUTPUT)


@pytest.mark.parametrize("channel", ["hv_cathode_vset", "hv_gate_vset", "hv_anode_vset",
                                     "hv_sipm_eq_vset", "hv_ts_vset", "hv_bs_vset",
                                     "hv_pmt_top_vset", "hv_pmt_bot_vset"])
def test_no_channel_goes_on_while_the_readout_is_on(caen, channel):
    caen._on_readout_status(STATUS_READOUT, readout(True))
    ack = switch(caen, channel, True)
    assert not ack["ok"] and "PT1000 readout is ON" in ack["reason"]
    assert "xams-ctl readout pt1000 off" in ack["reason"]
    assert caen._readers["hv_1"].outputs == caen._readers["hv_2"].outputs == []


def test_unknown_is_refused_too(caen):
    ack = switch(caen, "hv_anode_vset", True)
    assert not ack["ok"] and "unknown" in ack["reason"]


def test_with_the_readout_off_it_goes_on(caen):
    caen._on_readout_status(STATUS_READOUT, readout(False))
    ack = switch(caen, "hv_anode_vset", True)
    assert ack["ok"]
    assert caen._readers["hv_2"].outputs == [(2, True)]


def test_off_is_never_refused(caen):
    caen._on_readout_status(STATUS_READOUT, readout(True))
    ack = switch(caen, "hv_cathode_vset", False)
    assert ack["ok"]
    assert caen._readers["hv_2"].outputs == [(0, False)]


def test_the_refusal_is_audited(caen):
    caen._on_readout_status(STATUS_READOUT, readout(True))
    switch(caen, "hv_gate_vset", True)
    audit = caen.bus.last_json("xams/audit")
    assert audit["result"] == "rejected" and "PT1000" in audit["detail"]


# --------------------------------------------------------- the cdaq side

@pytest.fixture
def cdaq(tmp_path):
    svc = CdaqService(load(), RecordingBus(), simulate=True, lock_dir=tmp_path,
                      readout_state_path=tmp_path / "cdaq_readout.json")
    svc.set_readout("9226", False, "apc")
    return svc


def hv(svc, energised=(), age_s=0.0, skip=()):
    for name in svc._hv_stat_names:
        if name in skip:
            svc._hv_stat.pop(name, None)
            continue
        word = 1 if name in energised else 1024
        svc._hv_stat[name] = Measurement(t=utcnow() - timedelta(seconds=age_s),
                                         channel=name, value=float(word),
                                         unit="bits", quality=Quality.OK)


def test_the_readout_stays_off_while_hv_is_on(cdaq):
    hv(cdaq, energised=("hv_sipm_eq_stat",))
    ack = cdaq.set_readout("9226", True, "apc")
    assert not ack["ok"] and "HV is ON (sipm_eq)" in ack["reason"]
    assert cdaq.readout_on("9226") is False


def test_unknown_or_old_hv_state_keeps_it_off(cdaq):
    hv(cdaq, skip=("hv_gate_stat",))
    ack = cdaq.set_readout("9226", True, "apc")
    assert not ack["ok"] and "unknown for gate" in ack["reason"]
    hv(cdaq, age_s=300)
    assert not cdaq.set_readout("9226", True, "apc")["ok"]


def test_with_all_hv_off_it_comes_on(cdaq):
    hv(cdaq)
    ack = cdaq.set_readout("9226", True, "apc")
    assert ack["ok"] and cdaq.readout_on("9226")


def test_switching_off_is_never_refused(cdaq):
    hv(cdaq)
    cdaq.set_readout("9226", True, "apc")
    hv(cdaq, energised=("hv_anode_stat",))
    assert cdaq.set_readout("9226", False, "apc")["ok"]


def test_the_status_words_arrive_from_the_bus(cdaq):
    from xams_sc.model import iso
    cdaq.bus.deliver("xams/meas/hv_anode_stat", json.dumps(
        {"t": iso(utcnow()), "ch": "hv_anode_stat", "v": 1.0, "u": "bits", "q": "ok"}))
    assert cdaq._hv_stat["hv_anode_stat"].value == 1.0
