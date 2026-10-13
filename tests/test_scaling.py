"""Scaling tests. See DESIGN.md §13.

These exist to pin down three things that have already gone wrong once, in the
system this one replaces:

  * the order of operations in (raw - offset) * multiplier
  * the sign convention on HV channels
  * the minutes-vs-seconds trap in the flow integrator
"""

import math

import pytest

from xams_sc.scaling import apply, apply_sign, integrate_step, strip_sign


class TestApply:
    def test_offset_is_subtracted_before_multiplying(self):
        # The LabVIEW front panel labels the columns "Offsets subtracted" and
        # "Multipliers", in that order. The other order gives 2*25 - 1 = 49.
        assert apply(2.0, offset=1.0, multiplier=25.0) == 25.0

    def test_defaults_are_identity(self):
        assert apply(3.14) == 3.14

    # Values recovered from the LabVIEW front panel (§4.2, §7.1).
    @pytest.mark.parametrize("raw,offset,mult,expected", [
        (2.0, 1.0, 25.0, 25.0),      # p101
        (1.0, 1.0, 25.0, 0.0),       # p101 at the offset
        (2.143, 0.0, 0.714, 1.530),  # pmain -> the 1.53 the front panel shows
        (1.0, 0.0, 6.0, 6.0),        # fm101
    ])
    def test_recovered_labview_values(self, raw, offset, mult, expected):
        assert apply(raw, offset, mult) == pytest.approx(expected, abs=1e-3)

    def test_negative_raw(self):
        assert apply(-1.0, offset=1.0, multiplier=2.0) == -4.0


class TestSign:
    """The supplies report unsigned magnitudes with POL separate (§7.2)."""

    def test_negative_channel_is_stored_signed(self):
        # The cathode answers "2250.0" and is stored as -2250.0.
        assert apply_sign(2250.0, -1) == -2250.0

    def test_positive_channel_unchanged(self):
        assert apply_sign(4200.0, 1) == 4200.0

    def test_magnitude_already_signed_is_not_double_negated(self):
        # Defensive: apply_sign takes a magnitude, and must be idempotent in
        # the sense that feeding it a signed value still yields the right sign.
        assert apply_sign(-2250.0, -1) == -2250.0

    def test_strip_sign_for_the_wire(self):
        assert strip_sign(-2250.0) == 2250.0
        assert strip_sign(4200.0) == 4200.0

    def test_round_trip(self):
        for magnitude, sign in [(2250.0, -1), (4200.0, 1), (0.0, -1)]:
            assert strip_sign(apply_sign(magnitude, sign)) == magnitude

    def test_invalid_sign_refused(self):
        with pytest.raises(ValueError):
            apply_sign(100.0, 0)


class TestIntegrator:
    """fm101 is grams per MINUTE; dt arrives in seconds (§7.5)."""

    def test_one_minute_of_flow_is_one_minute_of_mass(self):
        assert integrate_step(6.0, 60.0) == pytest.approx(6.0)

    def test_ten_seconds_at_six_grams_per_minute(self):
        assert integrate_step(6.0, 10.0) == pytest.approx(1.0)

    def test_the_factor_sixty_trap(self):
        # Forgetting the conversion gives 60x the real mass, and the result
        # looks entirely plausible on a plot. This is the whole reason the
        # conversion lives in one tested function.
        wrong = 6.0 * 10.0
        right = integrate_step(6.0, 10.0)
        assert wrong == pytest.approx(right * 60.0)

    def test_zero_flow_adds_nothing(self):
        assert integrate_step(0.0, 10.0) == 0.0

    def test_accumulation_over_an_hour(self):
        total = sum(integrate_step(6.0, 10.0) for _ in range(360))
        assert total == pytest.approx(360.0)  # 6 g/min for 60 min
        assert not math.isnan(total)


class TestPkrPressure:
    """PKR 251 manual, appendix A: p = 10^(1.667 U - 11.33) mbar, 0.6 V/decade."""

    @pytest.mark.parametrize("volts,mbar", [(8.6, 1000.0), (6.8, 1.0), (5.0, 1e-3),
                                            (3.2, 1e-6)])
    def test_the_manual_table(self, volts, mbar):
        from xams_sc.scaling import pkr_pressure
        assert pkr_pressure(volts) == pytest.approx(mbar, rel=0.05)

    @pytest.mark.parametrize("volts", [-1.0, 0.0, 0.49, 9.51, 10.0])
    def test_no_supply_or_a_broken_pirani_is_not_a_pressure(self, volts):
        from xams_sc.scaling import pkr_pressure
        assert pkr_pressure(volts) is None
