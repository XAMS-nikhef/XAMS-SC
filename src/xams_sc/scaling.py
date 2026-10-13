"""Raw values to engineering units. See DESIGN.md §4.2.

The whole module is one formula, and that is the point: scaling lives here and
nowhere else, so there is exactly one place to look when a number is wrong.
"""

from __future__ import annotations


def apply(raw: float, offset: float = 0.0, multiplier: float = 1.0) -> float:
    """value = (raw - offset) * multiplier

    This matches the LabVIEW implementation exactly — its front panel labels
    the two columns "Offsets subtracted" and "Multipliers", in that order.

    DO NOT change the convention or the order of operations. Every historical
    CSV value was produced this way, and `tools/compare_to_labview.py` checks
    the new system against them.
    """
    return (raw - offset) * multiplier


def apply_sign(magnitude: float, sign: int) -> float:
    """Apply a polarity to an unsigned magnitude from a CAEN supply.

    The DT1470ET reports VMON and VSET as unsigned magnitudes with polarity as
    a separate POL parameter: a cathode at minus 2250 volts answers "2250.0".
    Values are stored SIGNED (§7.2), so the sign is applied once, here.

    This is almost certainly what the LabVIEW `DAISY_polarity_signs.vi` does.
    """
    if sign not in (-1, 1):
        raise ValueError(f"sign must be -1 or +1, got {sign!r}")
    return abs(magnitude) * sign


def magnitude_for(value: float, sign: int) -> float:
    """Signed value -> the unsigned magnitude the supply expects. The inverse
    of `apply_sign`, and the only place a setpoint crosses that boundary.

    **This refuses a value of the wrong polarity rather than taking `abs()`.**

    The temptation is to write `abs(value)` and be done, because the wire
    format is unsigned anyway. That would mean asking for +2250 on the cathode
    and getting -2250 on the electrode: the board applies its own POL, so the
    magnitude is obeyed and the operator's sign is silently discarded. The
    request would be honoured, the read-back would agree, the audit record
    would look clean, and the wrong voltage would be on the detector.

    A wrong sign is a mistake about which electrode is being addressed. It is
    refused, not corrected.

    Zero has no polarity and is always allowed - which matters, because zero
    is how section 10a's invariant gets established on a channel whose stored
    setpoint is wrong.
    """
    if sign not in (-1, 1):
        raise ValueError(f"sign must be -1 or +1, got {sign!r}")
    if value == 0:
        return 0.0
    if (value > 0) != (sign > 0):
        wanted = "positive" if sign > 0 else "negative"
        raise ValueError(
            f"{value:+g} has the wrong polarity for this channel, which is "
            f"{wanted}. Refusing rather than taking the magnitude: the supply "
            f"applies its own POL, so the sign would be discarded and the "
            f"opposite voltage delivered without anything reporting an error.")
    return abs(value)


def strip_sign(value: float) -> float:
    """Inverse of apply_sign, for values going back out to a supply.

    A setpoint is held signed in this system and must be sent as a magnitude.
    Polarity is a hardware setting and is never changed by a write (§8.3).
    """
    return abs(value)


def heater_power(percent: float, full_scale_v: float,
                 resistance_ohm: float) -> float:
    """Lake Shore heater output, from percent of full scale to watts.

        volts = percent / 100 * full_scale_v
        watts = volts^2 / resistance_ohm

    **This is quadratic, so it cannot be expressed as an offset and a
    multiplier.** That is why it is a named transform rather than another row
    of the ordinary scaling table: `(raw - offset) * multiplier` is linear by
    definition and §4.2 says not to bend that convention.

    NOTE ON HEATER RANGE. The 335 switches heater range in roughly tenfold
    power steps, and `full_scale_v` describes one particular range — 3 (high)
    at the time these constants were supplied. If the range is changed on the
    instrument, the watts computed here become wrong by about that factor
    while the percentage stays perfectly plausible.

    A range guard was offered and deliberately declined on 17 September 2026.
    It is recorded here so that a future reader finding a surprising wattage
    knows where to look first, not as a reason to add one.
    """
    volts = percent / 100.0 * full_scale_v
    return volts * volts / resistance_ohm


def pkr_pressure(volts: float, u_min: float = 0.5, u_max: float = 9.5) -> float | None:
    """Pfeiffer PKR 25x full-range gauge: measuring signal in volts to mbar.

        p [mbar] = 10 ** (1.667 * U - 11.33)

    PKR 251 operating instructions, appendix A; 0.6 V per decade, valid for
    5e-9 < p < 1000 mbar, which is 1.82 V .. 8.6 V.

    **Returns None for a signal that is not a measurement**, rather than a
    pressure: below `u_min` (0.5 V) the gauge has no supply, above `u_max`
    (9.5 V) its Pirani element is defective. The formula would happily turn
    either into a number - 0 V is 4.7e-12 mbar, an excellent vacuum - and a
    dead gauge would then read as the best vacuum the system has ever had.
    A None from a transform is published as quality=error.

    Between those limits and outside 1.82..8.6 V the gauge is under- or over-
    range; the value is still the formula's, as on the DCU display.
    """
    if not (u_min <= volts <= u_max):
        return None
    return 10.0 ** (1.667 * volts - 11.33)


# Named transforms available to channels.yaml via `derive.transform`.
#
# A NAME, never an expression. Putting an eval-able formula string in the
# configuration would make config into code, and channels.yaml is edited by
# people who should not have to think about what they are executing.
TRANSFORMS = {
    "heater_power": heater_power,
    "pkr_pressure": pkr_pressure,
}


def integrate_step(flow_per_minute: float, dt_seconds: float) -> float:
    """Mass passed during one interval, in grams.

    `fm101` is a MASS FLOW IN GRAMS PER MINUTE — per *minute*. The interval
    arrives in seconds, so it is converted here, once.

    Two traps, both covered in tests/test_scaling.py:

      * Using dt in seconds without converting gives a factor-60 error that
        looks entirely plausible in a plot and can go unnoticed for months.
      * The LabVIEW front panel labels this channel "Flow (SLPM)". That label
        is WRONG. It is not a volumetric standard-litre flow, and nobody
        should "correct" this back by consulting the old front panel (§4.2).
    """
    return flow_per_minute * (dt_seconds / 60.0)
