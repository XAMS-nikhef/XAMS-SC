"""Decoding the CAEN channel STATUS word. See DESIGN.md §7.2.

Separate from `devices/caen.py` because it is pure logic with no instrument
behind it, and because the web UI needs it. `caen.py` imports `serial`, which
belongs to the optional `hardware` extra — importing it into the UI would make
a machine that only serves pages need pyserial to start.

**Bit 0 is the answer to "is this channel on", not VMON.** The supplies have a
physical enable per channel, and a channel can be enabled while sitting at
zero volts:

    17 Sep 2026, hv_1 ch1 — STAT=1 (ON), VSET=0.0, VMON=0.0

Inferring on/off from the voltage, as the web UI first did, calls that "off".
It is not off: it is on, at zero, and one turn of a setpoint away from putting
volts on a photomultiplier. A display that understates what is live is the
kind of wrong that gets somebody hurt.
"""

from __future__ import annotations

# DT1470ET status word. Confirmed against both units on 17 September 2026:
# bit 10 on every channel that was disabled, bit 0 on the one that was not.
STAT_BITS = {
    0: "ON", 1: "RAMP_UP", 2: "RAMP_DOWN", 3: "OVER_CURRENT",
    4: "OVER_VOLTAGE", 5: "UNDER_VOLTAGE", 6: "MAX_V", 7: "TRIP",
    8: "OVER_POWER", 9: "OVER_TEMP", 10: "DISABLED", 11: "KILL",
    12: "INTERLOCK", 13: "UNCALIBRATED",
}

BIT_ON = 0
BIT_RAMP_UP = 1
BIT_RAMP_DOWN = 2
BIT_UNDER_VOLTAGE = 5
BIT_TRIP = 7
BIT_DISABLED = 10

# WHAT A TRIP LOOKS LIKE ON THESE UNITS, which is not what the manual says.
#
# The anode (hv_2 ch2) broke down twice on 23 September 2026 - 20 uA at
# +2500 V, then 5 uA at +2400 V. Both times the board cut the output within
# seconds and VMON decayed to zero over the next minute, while STAT stayed
# ON + UNDER_VOLTAGE (33). Bit 7 was never set and BDALARM stayed 0, so
# nothing keyed on them noticed: no clear was offered, and the channel sat
# "on" at its full setpoint with a dead output until somebody power-cycled
# the supply.
#
# So a trip is recognised from what the output DOES, as well as from the flag:
# energised, not ramping, UNDER_VOLTAGE set, and VMON below half the setpoint,
# for `COLLAPSE_CONFIRM_READS` reads in a row. A normal ramp carries RAMP_UP,
# so it cannot match; a channel current-limiting just below its setpoint
# stays above half of it until the board gives up and cuts it.
COLLAPSE_FRACTION = 0.5
COLLAPSE_MIN_VSET_V = 20.0
COLLAPSE_CONFIRM_READS = 3
# Bit 7 or BDALARM has to be seen on this many consecutive reads. One corrupted
# status word is not a trip: 683 and 819 were each read once on 17-18 September
# and both have bit 7 in them.
FLAG_CONFIRM_READS = 2

# How far a setpoint must sit from zero before a DISABLED channel counts as
# ARMED - far enough that flipping the enable by hand would put real volts on
# an electrode, and comfortably above the board's own rounding on a value that
# was written as zero.
#
# Shared by the page that warns about it and the driver that zeroes it, so the
# two cannot drift apart. A page calling a channel armed that the driver will
# not act on - or the reverse - is worse than either behaviour alone.
ARMED_ABOVE_V = 1.0

# Bits meaning something is WRONG, as against describing what the channel is
# doing. ON, RAMP_UP, RAMP_DOWN and DISABLED are ordinary states and must not
# be dressed up as faults, or the display cries wolf at a supply behaving
# exactly as intended.
FAULT_BITS = {
    3: "OVER_CURRENT", 4: "OVER_VOLTAGE", 5: "UNDER_VOLTAGE", 6: "MAX_V",
    7: "TRIP", 8: "OVER_POWER", 9: "OVER_TEMP", 11: "KILL",
    12: "INTERLOCK", 13: "UNCALIBRATED",
}


def describe_status(word: int) -> str:
    flags = [name for bit, name in STAT_BITS.items() if word & (1 << bit)]
    return ",".join(flags) if flags else "OFF"


def status_faults(word: int) -> list[str]:
    """The fault flags set in a status word, if any."""
    return [name for bit, name in sorted(FAULT_BITS.items())
            if word & (1 << bit)]


def is_energised(word: int) -> bool:
    """Is the OUTPUT energised? Bit 0.

    **This is not "is the channel enabled".** It was called `is_enabled` until
    18 September 2026, and that name caused four separate bugs in one
    afternoon, because in this system's own vocabulary "enabled" means the
    front-panel switch - which is bit 10, and a different question:

      * `is_disabled(word)`  - the switch is OFF (bit 10)
      * `is_energised(word)` - the output is ON (bit 0)
      * neither              - switch on, output off. Permitted and inert.

    Every one of the four bugs was the same shape: code that meant "has the
    operator flipped the switch" and asked "is it putting out volts". It
    displayed a live channel as off, refused a setpoint to a channel that was
    ready for one, and warned that an enabled channel was dangerous to enable.

    The name is the fix. Anything that wants the switch asks `is_disabled`.
    """
    return bool(word & (1 << BIT_ON))


def ramping(word: int) -> str | None:
    """"up" or "down" while the board ramps the output (bits 1 and 2), else
    None. Turning on sets bit 0 at once and the ramp up follows; turning off
    starts a ramp down with bit 0 still set (§10a)."""
    if word >> BIT_RAMP_UP & 1:
        return "up"
    if word >> BIT_RAMP_DOWN & 1:
        return "down"
    return None


def is_disabled(word: int) -> bool:
    """Is it explicitly DISABLED? Bit 10.

    Not simply `not is_energised(...)`: a word could have neither bit set, and
    the two questions are reported separately by the board.
    """
    return bool(word & (1 << BIT_DISABLED))


def is_tripped(word: int) -> bool:
    """Did the board trip this channel? Bit 7.

    **It latches.** The channel is switched OFF and the bit stays set until
    the board alarm is cleared (`BDCLR`); neither ON nor the enable switch
    clears it. That is what made a trip need a power cycle before
    23 September 2026 (§10a *Recovering from a trip*).
    """
    return bool(word & (1 << BIT_TRIP))


def output_collapsed(word: int, vset: float | None, vmon: float | None) -> bool:
    """Is this channel energised with an output that has gone? See above.

    One read's worth; the caller requires `COLLAPSE_CONFIRM_READS` in a row.
    Signs do not matter - magnitudes are compared - and a missing reading
    is never evidence of a trip.
    """
    if vset is None or vmon is None:
        return False
    if not word & (1 << BIT_ON) or not word & (1 << BIT_UNDER_VOLTAGE):
        return False
    if word & ((1 << BIT_RAMP_UP) | (1 << BIT_RAMP_DOWN)):
        return False
    if abs(vset) < COLLAPSE_MIN_VSET_V:
        return False
    return abs(vmon) < COLLAPSE_FRACTION * abs(vset)


def output_healthy(word: int, vset: float | None, vmon: float | None) -> bool:
    """Energised, settled and holding its setpoint: evidence an output works.

    Used after a clear, to tell a channel that came back from one that did
    not - which is how this system will find out whether BDCLR recovers a
    tripped DT1470ET at all, or only a power cycle does.
    """
    if vset is None or vmon is None or abs(vset) < COLLAPSE_MIN_VSET_V:
        return False
    if not word & (1 << BIT_ON):
        return False
    if word & ((1 << BIT_RAMP_UP) | (1 << BIT_RAMP_DOWN)):
        return False
    return abs(abs(vmon) - abs(vset)) <= 0.1 * abs(vset)
