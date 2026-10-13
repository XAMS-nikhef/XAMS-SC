# CAEN DT1470ET

Two units, four HV channels each. Neither carries a USB serial number, so
identity rests entirely on the `BDSNUM` query.

The driver **writes exactly four things: `VSET`, `ON`, `OFF` and `BDCLR`**
(§10a), each checked, read back from the board and audited. `BDCLR` clears a
latched trip — see [Recovering from a trip](#recovering-from-a-trip).
How to drive it is in [The web interface](../operating/webui.md#control-high-voltage).

It **reads** the protection settings at startup and alarms on a mismatch, and
never writes them: ramp rate, trip current and over-voltage limit are configured
on the instrument and stay there. Nor can it enable a channel or take a board
out of `LOCAL` — those are hand operations, and they are the two gates between
this software and an electrode.

---

## Three things decide whether there are volts on an electrode

Confusing them is the single most productive source of bugs in this driver —
four in one afternoon on 18 September 2026, all the same shape.

| | What it is | Who can change it |
|---|---|---|
| the **enable switch** | clears the `DISABLED` bit | **a hand at the front panel.** No command here can touch it |
| **`ON` / `OFF`** | energises a channel that is already enabled | this driver |
| **`VSET`** | where it ramps to | this driver |

Clearing `DISABLED` alone does nothing visible: the channel is *permitted*
and *inert*, sitting at `STAT=0`. That is exactly what was observed when the
`nai` enable was flipped and nothing happened.

And the reverse trap, which is worse: a channel can be **enabled and
energised at zero volts** — `STAT=1, VSET=0.0, VMON=0.0`. Inferring "off"
from the voltage, as the web UI first did, calls that off. It is not off. It
is on, at zero, and one turn of a setpoint away from putting volts on a
photomultiplier.

> **A display that understates what is live is the kind of wrong that gets
> somebody hurt.**

So the code asks two separate questions, and never one in place of the other:

```python
is_disabled(word)   # the switch is OFF        — bit 10
is_energised(word)  # the output is energised  — bit 0
```

---

## The protocol

ASCII over the virtual COM port, 9600 baud, no vendor library.

```
$BD:00,CMD:MON,PAR:VMON,CH:2          →   #BD:00,CMD:OK,VAL:2249.8
$BD:00,CMD:SET,PAR:VSET,CH:2,VAL:2300 →   #BD:00,CMD:OK
```

**Both units answer at board address 0.** They are two independent USB
connections, not a daisy chain, and they are told apart by *which port their
`BDSNUM` came back on* — never by address and never by COM number.

Note the reply shapes, because they differ in a way that matters: a
successful `SET` answers `CMD:OK` with **no `VAL:` field**. Success is
therefore the presence of `CMD:OK`, not the presence of a value.

A malformed, truncated or error reply is a **read failure**, never a silently
substituted value.

| Monitored | |
|---|---|
| `VMON`, `IMON` | what the channel is actually doing |
| `VSET` | where it is asked to go |
| `STAT` | the status word, below |
| `POL` | polarity, per channel |
| `MAXV`, `RUP`, `RDW`, `TRIP`, `ISET` | protection settings — read, compared, never written |
| `BDNAME`, `BDSNUM`, `BDCTR` | identity and LOCAL/REMOTE |

---

## The status word

`STAT` is a bit field. Fourteen bits are decoded, and they fall into two
groups — which is the whole point of the split.

**States. Not faults.**

| Bit | | |
|---|---|---|
| 0 | `ON` | the output is energised |
| 1 | `RAMP_UP` | going up |
| 2 | `RAMP_DOWN` | coming down |
| 10 | `DISABLED` | the front-panel switch is off |

**Faults. Something is wrong.**

| Bit | | What it means, and what to do |
|---|---|---|
| 3 | `OVER_CURRENT` | drawing more than `ISET` — the channel is current-limiting. Look for a discharge or a leaky divider |
| 4 | `OVER_VOLTAGE` | above the board's own `MAXV` |
| 5 | `UNDER_VOLTAGE` | below where it should be while energised |
| 6 | `MAX_V` | the hardware voltage limit was reached |
| 7 | `TRIP` | the board tripped the channel: over-current persisted past its trip time. **Investigate before re-energising** — the supply did this to protect something |
| 8 | `OVER_POWER` | the board's power budget |
| 9 | `OVER_TEMP` | the supply is too hot. Check ventilation |
| 11 | `KILL` | killed by the external kill input |
| 12 | `INTERLOCK` | the interlock is open. This is hardware protection working |
| 13 | `UNCALIBRATED` | the board does not trust its own calibration. Do not rely on readings |

Ramping and being disabled are ordinary behaviour and are **not** dressed up
as faults, because a display that cries wolf at a supply doing exactly what
it was asked is one nobody reads.

`hv_status.py` is deliberately separate from the driver: it is pure logic with
no instrument behind it, and the web UI needs it. `caen.py` imports
`pyserial`, and importing that into the UI would make a machine that only
serves pages need the hardware extra to start.

---

## Signs

The supplies report `VMON` and `VSET` as **unsigned magnitudes**, with
polarity as a separate `POL` parameter. A cathode at −2250 V answers
`2250.0`.

Values are **stored signed**. The sign is applied once, in `scaling.py`, and
the inverse conversion — signed value to the magnitude the board expects —
**refuses the wrong polarity rather than taking its absolute value**:

> Asking for +2250 on the cathode and getting −2250 on the electrode would be
> a request honoured, a read-back that agreed, an audit record that looked
> clean, and the wrong voltage on the detector. A wrong sign is a mistake
> about which electrode is being addressed, so it is refused, not corrected.

Zero has no polarity and is always allowed, which matters because zero is how
§10a's resting invariant is established on a channel whose stored setpoint is
wrong.

---

## What a write actually does

1. **Resolve** the channel name to a supply and an index.
2. **Validate** against `channels.yaml` — a channel with no `limits` accepts
   nothing.
3. **Refuse** outright if a non-zero setpoint is aimed at a channel that is
   not enabled (§10a).
4. **Convert** the signed value to a magnitude, refusing the wrong polarity.
5. **Send** `CMD:SET`.
6. **Read back** from the board and compare within 0.5 V. Only then is the
   command successful.
7. **Acknowledge** on the bus, and **audit** — including refusals.

> A refused command is acknowledged with a reason and recorded. What somebody
> *tried* to put on an electrode is worth as much afterwards as what they
> managed to.

Energising works the same way, and its acknowledgement **says what will
happen**: turning a channel on ramps it to whatever `VSET` currently holds,
so the ack names that voltage. Turning on at `VSET 0` — the resting state the
invariant guarantees — energises at zero and moves nothing, which is a
perfectly reasonable thing to do.

---

## LOCAL is invisible until you write

`BDCTR` says `LOCAL` or `REMOTE`. In `LOCAL` the front panel has control:
every remote `SET` is refused with `LOC:ERR` while `MON` keeps answering
normally.

That asymmetry is why a board in `LOCAL` looks exactly like a working one
until somebody tries to change something. The driver reads `BDCTR` at startup
and logs it, and the [HV page](../operating/webui.md#control-high-voltage) shows it.

---

## Protection settings are read, compared, and never written

`devices.yaml` carries an `expect` block per channel — `pol`, `maxv`, `rup`,
`rdw`, `trip`, `iset` — recording what each channel was found set to on
17 September 2026. At startup the driver reads the board's actual values and
**alarms on any mismatch**.

There is deliberately no code path that could change them. A mismatch means
somebody changed a limit on the front panel, which is worth knowing about and
is not this system's business to correct. That is what keeps the software out
of the protection path.

!!! note "One open question (§16)"
    On `hv_1`, channel 0 ramps at 1 V/s and trips at 2.0 µA where every other
    channel is 20–50 V/s and 10.0 µA. Confirm that this is intent rather than
    history.

---

## Identity and reconnection

Neither unit exposes a USB serial number and both present the same VID/PID —
Windows enumerates them as generic "USB Serial Device". The USB instance path
is a hub socket and changes when a cable is moved. **So `BDSNUM` is not a
cross-check; it is the only identification there is.**

Resolution is two-pass: probe every candidate port, then bind. Two ports
reporting the same serial is fatal, because there is no safe way to guess
which unit is which.

**A reconnect is a full re-resolution, never a bare reopen.** A replugged unit
can come back on a different COM number, and if the two cables were swapped
while the link was down, a reopen would read the wrong supply under the right
name. Unlike startup, reconnection is forgiving: it keeps whatever still
works and retries the rest, because startup's job is to refuse to run while a
running service's job is to recover.

---

## Recovering from a trip

### What a trip actually looks like

The manual says a trip switches the channel off, sets `STAT` bit 7 and raises
the board alarm (`BDALARM`, one bit per channel). **On `hv_2` (firmware 1.04)
it did none of that.** The anode broke down twice on 23 September 2026 —
20 µA at +2500 V, then 5 µA at +2400 V — and both times:

| | STAT | VMON | IMON |
|---|---|---|---|
| before | `1` ON | +2400.8 V | 0.1 µA |
| breakdown | `33` ON, UNDER_VOLTAGE | +2044.7 V | **5.04 µA** |
| 10 s later | `33` | +198 V | 0.15 µA |
| a minute later | `33` | 0.0 V | 0.0 µA |

The board cut the output and VMON decayed to zero, but the channel went on
reporting **ON**. Bit 7 never appeared and `BDALARM` stayed 0. Turning it
off and on, and cycling the enable switch, left the output dead: it accepted
ON, ramped, and VMON stayed at 0 V. Only a **power cycle** brought it back.

### Detection

So the driver recognises a trip two ways, on every 1 Hz read (`_watch_trips`):

- **the output collapsed:** the channel is ON, not ramping, `UNDER_VOLTAGE`
  is set and VMON is below half of VSET (VSET at least 20 V), on 3 reads in
  a row. A normal ramp has `RAMP_UP` set and cannot match. A channel
  current-limiting just below its setpoint stays above half of it until the
  board cuts it;
- **the board flagged it:** `STAT` bit 7 or the channel's `BDALARM` bit, on 2
  reads in a row. One corrupted status word is not enough: 683 and 819 were
  each read once on 17–18 September, and both contain bit 7.

### What happens then

1. **It is made safe at once:** `VSET` 0, then `OFF`, audited with actor
   `automatic (trip)`. In that order, so if `OFF` is refused (a board in
   `LOCAL`) the setpoint is still gone. A failed attempt is retried every
   30 s.
2. **It is latched.** A retained record goes out on `xams/hv/trip/<channel>`
   with the cause and the highest IMON in the minute before (the spike is
   gone by the time the collapse is confirmed). `/hv` shows a banner and
   `TRIPPED` on the row. The latch survives a restart of either service.
3. **The channel cannot be turned on** until somebody clears it.
4. **It is not an alarm.** Nobody is emailed or woken: an HV trip happens
   with people in the lab (A.P. Colijn, 23 September 2026).

### Clearing

**clear trip** on `/hv`, or `xams-ctl hv-clear-trip <channel>`, is the
acknowledgement. Inside one serial transaction:

1. **Sets `VSET` to 0 and switches OFF every tripped channel on that
   supply** — latched here, or flagged by the board. Every one, not just the
   one clicked: `BDCLR` has no per-channel form. If any of this fails,
   nothing is cleared.
2. **Sends `BDCLR`**, then reads `STAT` and `BDALARM` back.
3. **Drops the latch** and empties the retained record.

A channel that is ON and has not tripped is refused. One that is off may be
cleared even with nothing latched: that harms nothing, and it is what is left
to try on a trip nothing saw.

The channel is left **off at 0 V**. Turning it on energises at zero; the
working voltage is one deliberate step away (*Load defaults*). Find out why it
tripped before putting it back, and raise it in small steps.

**Whether `BDCLR` brings a dead DT1470ET output back is not yet known.** The
driver finds out the next time it happens:

- if a cleared channel then holds its setpoint, the log says so, and whether
  the supply was relinked (power-cycled) in between;
- if it collapses again first, the new trip is marked **needs power cycle**
  and `/hv` says so. Ramp the other channels on that supply down first: a
  power cycle takes all of them.

Whether `BDCLR` drops `STAT` bit 7 itself, or only the next `ON` does, is not
in the manual either. If the bit is still set afterwards the acknowledgement
says so and suggests turning on — safe, because the setpoint is already zero.

`BDCLR` changes no limit: `MAXV`, `ISET`, `TRIP`, `RUP` and `RDW` stay what the
front panel set (§10 rule 2).

## What this driver cannot do

- enable or disable a channel — the front-panel switch
- take a board out of `LOCAL`
- change `MAXV`, `RUP`, `RDW`, `TRIP` or `ISET`
- write anything at all to a channel whose `limits` are absent from
  `channels.yaml`

Each of those is a deliberate absence of code, not a check that could be
loosened.
