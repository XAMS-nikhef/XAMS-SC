"""cDAQ driver tests. No hardware required.

These cover the two things about this driver that are easy to get wrong and
expensive to get wrong: the excitation current, which DAQmx will not choose
correctly on its own, and the difference between a cold sensor and no sensor.
"""

import pytest

from doubles import RecordingBus

from xams_sc.config import load
from xams_sc.devices.cdaq import (CURRENT_RANGE_A, CURRENT_VALID_A, EXCITATION_A,
                                  RTD_VALID_C, CdaqService)
from xams_sc.model import Quality


@pytest.fixture
def config():
    return load()


@pytest.fixture
def service(config, tmp_path):
    return CdaqService(config, RecordingBus(), simulate=True, lock_dir=tmp_path)


class TestExcitation:
    """Measured from the hardware, 17 September 2026.

    Each module accepts exactly one value, and neither is the DAQmx default of
    2.5 mA. Getting this wrong does not produce a wrong reading — the task
    refuses to configure — but the values must not drift from what the modules
    actually accept.
    """

    def test_pt1000_module_runs_at_100_microamp(self):
        assert EXCITATION_A["NI9226"] == pytest.approx(100e-6)

    def test_pt100_module_runs_at_1_milliamp(self):
        assert EXCITATION_A["NI9216"] == pytest.approx(1e-3)

    def test_neither_is_the_daqmx_default(self):
        # DAQmx defaults to 2.5 mA, which BOTH modules refuse. This is why the
        # argument cannot be omitted, contrary to §7.1's example code.
        for value in EXCITATION_A.values():
            assert value != pytest.approx(2.5e-3)

    def test_pt1000_draws_less_current_than_pt100(self):
        # 1000 ohm at 1 mA would put a milliwatt into the sensor and self-heat
        # it. The tenfold lower current on the PT1000 module is the reason.
        assert EXCITATION_A["NI9226"] < EXCITATION_A["NI9216"]

    def test_every_rtd_module_in_the_config_has_an_excitation(self, config):
        models = {m["model"] for m in config.devices["cdaq"]["modules"]}
        rtd_models = {m for m in models if m in ("NI9216", "NI9226")}
        assert rtd_models, "no RTD modules in devices.yaml?"
        for model in rtd_models:
            assert model in EXCITATION_A, f"{model} has no excitation current"


class TestTaskBuilding:
    def test_unconnected_module_gets_no_task(self, service):
        """9216_2 is entirely unconnected — it must cost nothing."""
        aliases = {m.alias for m in service._modules}
        assert "9216_2" not in aliases

    def test_connected_modules_all_present(self, service):
        aliases = {m.alias for m in service._modules}
        # 9226 is absent: its readout unit failed on 28 September 2026 and
        # all its channels are disabled.
        assert aliases == {"9207", "9216_1"}

    def test_channels_are_ordered_by_physical_index(self, service):
        """Read order must match the order channels were added to the task,
        or every value lands on the wrong channel name."""
        for mod in service._modules:
            idx = [int(c.phys.split("/ai")[1]) for c in mod.channels]
            assert idx == sorted(idx), f"{mod.alias} channels out of order"

    def test_channel_count_matches_what_is_actually_wired(self, service):
        """15 live channels: 6 voltage + 2 current + 7 RTD.

        Milestone 2 read 20 (6 + 14). tt202 was then found to be a failed
        sensor — it reads open-circuit — and disabled on 17 September 2026,
        so the 9226 now contributes 6 RTDs rather than 7. On 22 September 2026
        the strain gauges sg101/sg102 were wired to the 9207's current half,
        which had until then been read by nothing. On 28 September 2026 the
        NI 9226 readout unit failed and tt201, tt203..tt207 were disabled
        too, taking the 9226 to none.

        This asserts an exact count on purpose. A channel silently appearing
        or disappearing is worth a failing test: it means either the wiring
        or channels.yaml changed, and both deserve a look.
        On 13 October 2026 the PKR gauge signal was wired to ai7 (p105_v),
        the flow meter having moved to ai6. p105 itself is derived from it
        and is not a task line.
        """
        total = sum(len(m.channels) for m in service._modules)
        assert total == 16
        kinds = [c.kind for m in service._modules for c in m.channels]
        assert kinds.count("voltage") == 7
        assert kinds.count("current") == 2
        assert kinds.count("rtd") == 7

    def test_failed_sensor_is_not_read(self, config, service):
        """tt202 is disabled, so no task should reference 9226/ai1."""
        assert config.channels["tt202"].enabled is False
        phys = [c.phys for m in service._modules for c in m.channels]
        assert "9226/ai1" not in phys

    def test_the_strain_gauges_are_read_and_the_spare_loops_are_not(self, service):
        """ai8/ai9 carry sg101/sg102; ai10:15 are unwired (§7.1).

        The older form of this test asserted that NOTHING on ai8:15 was read,
        which was true until the gauges were fitted. The half that still
        matters is the spare loops: an unwired current input reads a dead loop,
        and a task line for it would publish that as a measurement.
        """
        by_phys = {c.phys: c.name for m in service._modules for c in m.channels}
        assert by_phys.get("9207/ai8") == "sg101"
        assert by_phys.get("9207/ai9") == "sg102"
        for n in range(10, 16):
            assert f"9207/ai{n}" not in by_phys

    def test_the_9207_carries_both_kinds_in_one_task(self, service):
        """Voltage and current channels share the module's single task, in
        physical order — the read comes back as one list and is zipped against
        that order, so a mixed task must stay sorted by index."""
        mod = next(m for m in service._modules if m.alias == "9207")
        assert [c.kind for c in mod.channels] == ["voltage"] * 7 + ["current"] * 2


class TestOpenCircuitDetection:
    """A disconnected RTD input does not read a temperature. It reads the rail.

    On this hardware an unconnected 9226 input swings between roughly -245 and
    +1327 C. Publishing that as quality=ok is the frozen-plausible-value
    failure principle 4 forbids, and it is how tt202 was found.
    """

    def test_valid_range_is_the_iec_60751_span(self):
        assert RTD_VALID_C == (-200.0, 850.0)

    @pytest.mark.parametrize("reading", [1326.57, -245.0, 9999.0, -1000.0])
    def test_out_of_range_is_rejected(self, reading):
        lo, hi = RTD_VALID_C
        assert not (lo <= reading <= hi)

    @pytest.mark.parametrize("reading", [-90.3, -56.1, 24.2, 48.7, 0.0, -199.9, 849.9])
    def test_real_readings_are_accepted(self, reading):
        """Including the genuinely cold cryostat sensors, which must NOT be
        mistaken for a fault: -90 C is a real measurement."""
        lo, hi = RTD_VALID_C
        assert lo <= reading <= hi

    def test_a_cold_sensor_is_not_a_fault(self):
        # The cryostat runs near -90 C. If the valid range were ever narrowed
        # to something "reasonable" like -100..100, real data would be thrown
        # away. The bound is the sensor standard, not an expectation.
        assert RTD_VALID_C[0] < -90.0


class TestBrokenLoopDetection:
    """A 4-20 mA transmitter cannot read below 4 mA. Zero load is 4 mA.

    Below the loop's floor there is no measurement at all — the loop is open,
    or its 24 V supply is off — and scaling that gives a confident negative
    weight. Same argument as the RTD range above, on the other half of the
    9207.
    """

    def test_the_valid_span_brackets_the_loop(self):
        lo, hi = CURRENT_VALID_A
        assert lo < 0.004 and hi > 0.020

    def test_the_task_can_read_over_the_top_of_the_loop(self):
        """The requested range must exceed the validity ceiling.

        If the two were equal, a transmitter driven over range would clip to
        the ceiling and read as a valid 20.5 mA — the frozen-plausible-value
        failure, arrived at from the other direction.
        """
        assert CURRENT_RANGE_A[1] > CURRENT_VALID_A[1] > 0.020

    def test_the_requested_range_fits_the_module(self):
        """Published 9207 specifications give ±21.5 mA in some revisions and
        ±22 mA in others, and DAQmx refuses a max_val outside the module's
        range. Staying under the smaller figure means the task configures
        whichever is right."""
        assert CURRENT_RANGE_A[1] <= 0.0215

    @pytest.mark.parametrize("milliamps", [0.0, 1.0, 3.0, 25.0, -5.0])
    def test_a_dead_or_over_range_loop_is_rejected(self, milliamps):
        lo, hi = CURRENT_VALID_A
        assert not (lo <= milliamps * 1e-3 <= hi)

    @pytest.mark.parametrize("milliamps", [4.0, 4.1, 12.0, 19.9, 20.0])
    def test_real_loop_readings_are_accepted(self, milliamps):
        lo, hi = CURRENT_VALID_A
        assert lo <= milliamps * 1e-3 <= hi

    def test_an_empty_scale_is_not_a_fault(self):
        # Zero load sends exactly 4 mA. If the floor were ever raised to 4 mA
        # or above, an empty load cell would report as a broken loop forever.
        assert CURRENT_VALID_A[0] < 0.004


class TestSimulation:
    def test_simulate_produces_every_enabled_channel(self, service):
        batch = service.read()
        expected = ({c.name for m in service._modules for c in m.channels}
                    | {c.name for c in service._derived})
        assert {m.channel for m in batch} == expected

    def test_simulate_needs_no_hardware(self, service):
        assert service.verify_identity() is True

    def test_simulated_values_are_quality_ok(self, service):
        assert all(m.quality is Quality.OK for m in service.read())

    def test_simulated_rtds_are_inside_the_valid_range(self, service):
        by_name = {m.channel: m.raw for m in service.read()}
        for mod in service._modules:
            for ch in mod.channels:
                if ch.kind == "rtd":
                    lo, hi = RTD_VALID_C
                    assert lo <= by_name[ch.name] <= hi, ch.name


class TestScalingThroughTheDriver:
    def test_voltage_channels_carry_their_configured_scaling(self, config, service):
        """p101 is (raw - 1.0) * 25.0 — the LabVIEW convention (§4.2)."""
        ch = config.channels["p101"]
        assert ch.offset == 1.0 and ch.multiplier == 25.0

    def test_strain_gauges_map_the_loop_onto_the_full_scale(self, config):
        """4 mA is 0 kg and 20 mA is 20 kg, in AMPS — DAQmx returns amps.

        This is the whole calibration, and it is one line of arithmetic, so it
        is worth pinning: a multiplier entered for milliamps instead of amps
        is out by a factor of a thousand and still looks like a number.
        """
        from xams_sc.scaling import apply
        for name in ("sg101", "sg102"):
            ch = config.channels[name]
            assert ch.unit == "kg"
            assert apply(0.004, ch.offset, ch.multiplier) == pytest.approx(0.0)
            assert apply(0.020, ch.offset, ch.multiplier) == pytest.approx(20.0)
            assert apply(0.012, ch.offset, ch.multiplier) == pytest.approx(10.0)

    def test_rtd_scaling_is_a_no_op(self, config):
        """DAQmx returns degrees C directly; do not hand-roll the conversion."""
        for name in ("tt201", "tt301", "ttamb"):
            ch = config.channels[name]
            assert ch.offset == 0.0 and ch.multiplier == 1.0


class TestThePkrGauge:
    """P105: the PKR signal on ai7, floating, and the pressure derived from it."""

    def test_flow_meter_moved_to_ai6_and_gauge_is_on_ai7(self, config):
        assert config.channels["fm101"].phys == "9207/ai6"
        assert config.channels["p105_v"].phys == "9207/ai7"
        assert "v6" not in config.channels

    def test_the_pressure_is_not_a_task_line(self, service):
        """Derived from p105_v; reading ai7 twice would be two names for one terminal."""
        names = {c.name for m in service._modules for c in m.channels}
        assert "p105_v" in names and "p105" not in names
        assert [c.name for c in service._derived] == ["p105"]

    def test_the_pressure_follows_the_signal(self, service):
        from xams_sc.model import Measurement, utcnow
        now = utcnow()
        out = service._derive_from([Measurement(t=now, channel="p105_v", value=8.6,
                                                unit="V")], now)
        assert out[0].channel == "p105"
        assert out[0].value == pytest.approx(1000, rel=0.05)
        assert out[0].quality is Quality.OK

    @pytest.mark.parametrize("volts", [0.0, 0.3, 9.8])
    def test_a_dead_gauge_is_not_a_good_vacuum(self, service, volts):
        """0 V would be 4.7e-12 mbar by the formula: an unpowered gauge must
        not read as the best vacuum ever seen."""
        from xams_sc.model import Measurement, utcnow
        now = utcnow()
        out = service._derive_from([Measurement(t=now, channel="p105_v", value=volts,
                                                unit="V")], now)
        assert out[0].value is None
        assert out[0].quality is Quality.ERROR

    def test_an_unreadable_signal_gives_an_unknown_pressure(self, service):
        from xams_sc.model import Measurement, utcnow
        now = utcnow()
        out = service._derive_from([Measurement(t=now, channel="p105_v", value=None,
                                                unit="V", quality=Quality.ERROR)], now)
        assert out[0].value is None and out[0].quality is Quality.ERROR
