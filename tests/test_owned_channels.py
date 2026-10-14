"""Which channels a service is responsible for. See DESIGN.md §6.1, §7.2.

**A failed read marked nothing at all for the CAEN service, and nothing
noticed.** `_publish_error` asked `channels_for(self.name)`, which matches on
the channel's DEVICE. For most services the device id and the service name
are the same word, so it worked by coincidence: `cdaq` reads channels whose
device is `cdaq`. The CAEN service is called `caen` and its channels belong
to `hv_1` and `hv_2`, so the list came back empty.

What that cost: both supplies unreachable, `read()` raises, the ERROR
measurements it had built are discarded with the exception, and
`_publish_error` then marks none of the 32 channels. Nothing is published at
all. The retained topics keep the last good voltages, the channels stay
`healthy` because that is quality-and-age and the age has not run out yet,
and the badge at the top of every page goes on saying ALL OK for a minute
while the high voltage is not being read. That is the frozen plausible value
principle 4 exists to forbid, on the one readout where it matters most.

The old test for this asserted only that `read()` raises. What is published
afterwards - the part an operator actually sees - was never checked.

The first two tests here are the structural guard. They would have caught
this on the day it was written, and they catch the next service whose device
id stops matching its name.
"""

import pathlib
import tempfile

import pytest

from doubles import RecordingBus
from xams_sc.config import load
from xams_sc.devices.caen import CaenService
from xams_sc.devices.cdaq import CdaqService
from xams_sc.devices.derived import DerivedService
from xams_sc.devices.lakeshore import LakeShoreService
from xams_sc.devices.rga import RgaService
from xams_sc.devices.turbo import TurboService
from xams_sc.devices.ups import UpsService
from xams_sc.model import Quality

# Every service that reads channels. A new one belongs here; that is the
# point of the file.
SERVICES = [CdaqService, CaenService, LakeShoreService, UpsService,
            TurboService, RgaService, DerivedService]


def build(cls, config=None):
    return cls(config or load(), RecordingBus(), simulate=True,
               lock_dir=pathlib.Path(tempfile.mkdtemp()))


@pytest.mark.parametrize("cls", SERVICES, ids=lambda c: c.name)
def test_every_service_can_name_the_channels_it_reads(cls):
    """THE BUG, as a one-line assertion.

    A service that cannot name its channels cannot report them as unreadable,
    and a read failure then goes out as silence.
    """
    service = build(cls)
    assert service.owned_channels(), (
        f"{service.name} owns no channels, so a failed read would mark "
        f"nothing and the last good values would stand")


def test_every_enabled_channel_is_read_by_exactly_one_service():
    """The partition, in both directions.

    A channel nobody owns is one nobody can mark as failed - the CAEN case.
    A channel two services own would be published twice, from two different
    reads, which is the disagreement §3 warns about.
    """
    config = load()
    owners: dict[str, list[str]] = {}
    for cls in SERVICES:
        service = build(cls, config)
        for channel in service.owned_channels():
            owners.setdefault(channel.name, []).append(service.name)

    enabled = {c.name for c in config.enabled_channels()}
    assert set(owners) == enabled, (
        f"unowned: {sorted(enabled - set(owners))}; "
        f"owned but not enabled: {sorted(set(owners) - enabled)}")
    assert not [n for n, who in owners.items() if len(who) > 1], (
        f"claimed twice: {[(n, w) for n, w in owners.items() if len(w) > 1]}")


class TestTheCaenServiceInParticular:
    """The service the bug was on, and the only one where the id differs."""

    def test_it_owns_the_channels_of_both_supplies(self):
        service = build(CaenService)
        devices = {c.device for c in service.owned_channels()}
        assert devices == {"hv_1", "hv_2"}

    def test_the_service_name_alone_finds_nothing(self):
        """Kept as a statement of WHY the override exists.

        If this ever starts returning channels, somebody has renamed the
        devices to `caen` and the override in CaenService is dead weight -
        which is worth knowing, rather than leaving it there forever because
        nobody dared touch it.
        """
        assert load().channels_for("caen") == []

    def test_a_failed_read_marks_every_hv_channel_bad(self):
        """What an operator sees when both supplies go.

        The read raises, so nothing it had built is published; this is the
        publication the loop makes instead, and it has to cover all 32.
        """
        config = load()
        service = build(CaenService, config)
        # Derived from the CONFIGURATION, never from owned_channels(): taking
        # the expectation from the thing under test makes this pass by
        # agreeing with itself, which is exactly how the empty list went
        # unnoticed. Verified by deleting the override and watching this fail.
        expected = {c.name for c in config.enabled_channels()
                    if c.device in ("hv_1", "hv_2")}
        assert len(expected) == 28  # 32 until the NaI's four were disabled, 14 Oct 2026

        service._publish_error("no CAEN channel answered; link lost")

        published = {m.channel: m for m in service.bus.measurements}
        assert set(published) == expected
        assert all(m.quality == Quality.ERROR for m in published.values())
        assert all(m.value is None for m in published.values()), (
            "a value was published for a channel that could not be read")

    def test_the_hv_setpoints_and_status_words_are_included(self):
        """Not just the monitors. A stale STAT word is what the Control page
        reads to decide whether a channel is energised (§7.2), and a stale
        VSET is what it shows as the voltage the board will ramp to."""
        service = build(CaenService)
        service._publish_error("link lost")
        published = {m.channel for m in service.bus.measurements}
        assert "hv_cathode_stat" in published
        assert "hv_cathode_vset" in published
        assert "hv_cathode_vmon" in published


@pytest.mark.parametrize("cls", SERVICES, ids=lambda c: c.name)
def test_no_service_shadows_the_single_instance_lock(cls):
    """`BaseService._lock` is the lock FILE that keeps a second copy of a
    service from starting. The RGA service once assigned its own RLock to the
    same name: run() then "acquired the lock file" by taking that RLock in the
    main thread for good, and every command waited on it forever on the MQTT
    thread - the page timed out and the analyser's channels went stale
    (13 October 2026)."""
    from xams_sc.service import SingleInstance
    assert isinstance(build(cls)._lock, SingleInstance)
