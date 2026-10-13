# Lake Shore 335

Temperature controller, read over serial — and **the first write path in the
system** (§10). The setpoint and the heater range on output 1 can be
commanded; everything else here is read-only.

How to drive it is in [The web
interface](../operating/webui.md#control). This page is the instrument.

---

## Serial settings, which are not the usual ones

```
57600 baud · 7 data bits · ODD parity · 1 stop bit
```

**7-O-1 is the 335's factory setting and is not a typo.** At 8-N-1 the port
opens happily and the instrument returns nothing intelligible — which looks
like a dead instrument rather than a wrong setting, and costs an afternoon if
you do not know.

---

## What it reads

| Channel | | |
|---|---|---|
| `tt401`, `tt402` | inputs A and B | `CRDG?` |
| `ls_setpoint_1`, `ls_setpoint_2` | setpoint per output | `SETP?` |
| `ls_heater_1`, `ls_heater_2` | heater output, percent | `HTR?` |
| `ls_heater_1_w`, `ls_heater_2_w` | the same, in watts | derived |

Both outputs are **read**, although only output 1 can be written. An unused
heater that starts doing something is exactly the surprise worth catching,
and refusing to write it is not a reason to stop looking at it.

The PID settings and the heater range are read and logged at startup —
displayed, never written.

### Units are Celsius

`CRDG?`, not `KRDG?`. An earlier draft of `channels.yaml` declared these
channels Kelvin; the imported LabVIEW history then showed them ranging from
−90 to +21.8, and there is no negative Kelvin. They also track the cryostat
RTDs closely, which settled it.

### Heater watts are derived, not measured

`ls_heater_N_w` is computed from the percentage through the `heater_power`
transform — full scale 45.4 V into 31.2 Ω — because the relationship goes as
the **square** of the percentage and so cannot be expressed as an offset and
a multiplier.

Two properties matter and are easy to get wrong:

- **Same timestamp as its source.** A derived value and the reading it came
  from must line up exactly, or a plot of the two shows a phantom lag.
- **It inherits its source's quality.** If the heater percentage could not be
  read, its wattage is not zero — it is unknown.

### A disconnected sensor is not a temperature

`RDGST?` is read alongside every `CRDG?`. A sensor that is disconnected or
out of range makes the 335 return a reading *with a status flag* rather than
a number, and that is published as `quality=error` — never a plausible-looking
zero.

---

## Identification

```
*IDN?  →  LSCI,MODEL335,335A12T/#######,3.2
```

The third field is **not one serial number**. It is
`<instrument serial>/<option card serial>`, and when no option card is fitted
the second half is literally `#######`. Only the part before the slash
identifies the instrument, and that is what matches the serial in its USB
descriptor.

Getting this wrong is not dangerous — §6.2 rule 3 makes an unexpected
identity a refusal rather than a guess, so the service simply would not
start. It cost one confused startup on 17 September 2026.

A reconnect re-resolves the port **and** re-confirms `*IDN?`. Reopening alone
would be the moment a swapped cable slipped through.

---

## One conversation at a time

A serial instrument matches a reply to a query **only by order of arrival**.
The poll loop and the control path are different threads, so without a lock
they can interleave: the answer to `SETP? 1` gets read by whoever asked last.

They did, on 18 September 2026, and **a setpoint read back as the heater
percentage**. Every exchange now takes a re-entrant lock around the whole
send-and-receive, not around the send alone.

Related, and the same instinct: `query` asks, `send` instructs, and they are
**separate methods** so that a typo cannot turn a question into an
instruction. `send` returns whether the bytes went out — not whether the
instrument did what was asked. Only a read-back can tell you that, and every
caller does one.

---

## What a write does

Setpoint and heater range, **output 1 only**:

```
SETP 1,-90.000        RANGE 1,2
```

| Step | |
|---|---|
| validate | against `channels.yaml` — `ls_setpoint_1` permits −196.0 … 0.0 °C |
| refuse output 2 | it is not used on this cryostat, so a command naming it is a mistake, not an instruction |
| send | one command, under the lock |
| **read back** | `SETP? 1` within 0.01 °C — far below anything that matters, far above the instrument's rounding |
| acknowledge | on `xams/ack/lakeshore/…`, with the reason if refused |
| audit | including refusals |

Heater ranges are `off` · `low` · `medium` · `high` — 0 to 3 on the wire.

A rejected command is acknowledged with a reason and audited, never silently
dropped: an attempted write that was refused is exactly as interesting six
months later as one that succeeded.

**The permitted range lives in `channels.yaml`, not here and not in the web
UI.** A channel with no `limits` accepts nothing, and the instrument's own
limits apply underneath regardless.

---

## What this driver cannot do

- write anything to output 2
- change the PID settings
- write a setpoint outside the range in `channels.yaml`
- report a write as successful without reading it back
