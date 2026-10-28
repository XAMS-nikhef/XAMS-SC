# NI cDAQ-9174

One chassis, four modules: NI 9207 (voltage), two NI 9216 (PT100), NI 9226
(PT1000).

RTD excitation current is not the DAQmx default and each module accepts
exactly one value — 100 µA for the 9226, 1 mA for the 9216 — so both must be
set explicitly or the task refuses to configure.

**Read-only.** The chassis has no output module, so this service has no
control path and no code that could write to hardware.

It carries 20 of the roughly 30 connected channels — the pressures, the flow
meter and every cryostat RTD — which makes it the service whose silence is
most noticeable.

---

## Slow modules read in the background (21 October 2026)

The modules are read on demand, one task each. The RTD modules (9216, 9226)
take seconds per read. Polled in the same 1-s loop as the 9207 they left fewer
pressure readings in each 10-s mean: when the repaired 9226 was enabled again,
p102 / p103 / p104 went from 1.5 / 3.8 / 3.1 mV to 5.1 / 12.4 / 11.2 mV of
noise, and back to normal with the module in the chassis but not read. Neither
the RTD cabling nor the module's presence was the cause.

A module with `background: true` in `devices.yaml` is therefore read in its own
thread every `interval_s` seconds (5 s for the RTD modules); its readings are
handed to the next 1-s read and published with the rest. The 9207 stays in the
loop. A background module that fails (pulled, broken) publishes `quality=error`
for its own channels only; the pressures carry on.

Every ten minutes the log says how long each module's read takes
(`read time per module: ...`), so a slow module shows itself there rather than
as noise.

**Later the same day: the 9226 at 10 s and high speed.** In the background
the noise only came halfway down (2.6 / 6.3 / 6.0 mV), although the 9207 now
got *more* readings per mean (~9, against ~5 in the morning with the 9216 in
the loop). So the 9226 converting disturbs the 9207 itself, beyond the loop
time; the 9216 does not. The 9226 is therefore read every 10 s with
`adc_timing: high_speed` (the DAQmx default is high resolution: 1.2 s per read
of its 6 channels), so it spends far less of the time converting. The
resolution lost does not matter for the PT1000s. `adc_timing` takes
`high_speed`, `high_resolution` or `automatic`; an unknown value stops the
service at start.

The first deployment set the mode channel by channel. DAQmx wants it the same
on every channel of a module at once (-200106, "Property must have the same
value for all channels on this device"), so the 9226 task would not start and
the whole cDAQ service stopped (13:08, ~30 s without cDAQ readings, until the
old `devices.yaml` was put back). The mode is now set on all channels in one
go (`task.ai_channels.all`), and a task that still will not start with it is
rebuilt without the mode and started with the default, with a warning in the
log: a slower conversion never costs the readings.

**High speed reverted (same afternoon).** With high speed the 9226
temperatures jumped up and down, so the 9226 is back on the default (high
resolution) timing; it stays at a 10-s interval. The `adc_timing` option is
kept for later use.

## Switching a readout off (26 October 2026)

A module with a `switch` name in `devices.yaml` can have its readout switched
off and on while the service runs — for now the 9226 (`switch: pt1000`,
`label: "PT1000 readout"`). With the HV on, the PT1000s are disconnected and
grounded; read, they would give nothing but open-circuit errors and alarms.

- **Off** stops the module's thread (a read in progress finishes), closes its
  task and drops what it read but nobody published yet. Nothing is read or
  published for its channels; the excitation is off with the task.
- **On** builds and starts the task again. If that fails the readout stays
  off and the reason goes back to whoever asked.
- The state is the cdaq service's: published retained on
  `xams/status/readout`, kept in `data/cdaq_readout.json`, and read back at
  start, so a restart does not start reading grounded sensors again. A module
  switched off gets no task at start either.
- Commands on `xams/cmd/cdaq/readout` (`{module, enabled, by}`), answered on
  `xams/ack/cdaq/readout` and audited (`readout_switch`). From the Control
  page or `xams-ctl readout pt1000 off|on --by <you>`.
- Only a background module can have a switch (refused at start otherwise):
  its task is used by its own thread alone, so closing it cannot pull it from
  under a read.

**The lock (28 October 2026).** The readout is not switched back ON while any
CAEN output is energised, or while that cannot be told (a status word missing,
bad, or older than 60 s; the service follows every `hv_*_stat` on the bus).
The caen service holds the other half: no HV channel ON while a switchable
readout is ON or its state unknown. Off is never refused, on either side.

The alarm engine withdraws the channels of a module that is off, and the web
UI and the daily report show them as off rather than broken — see
[the alarm engine](../software/alarms.md) and
[the Control page](../operating/webui.md#pt1000-readout).

## The slot map

| Alias | Module | Slot | Reads | Channels |
|---|---|---|---|---|
| `9207` | NI 9207 | 1 | ±10 V | `ai0:7` — 4 pressures, detector pressure, flow meter (ai6), PKR vacuum gauge (ai7, floating, `p105`, on the P&I as P105), ai4 unconnected |
| `9207` | NI 9207 | 1 | 4-20 mA | `ai8:9` — strain gauges `sg101`, `sg102`; `ai10:15` unconnected |
| `9216_1` | NI 9216 | 2 | PT100 | `ai0:6` — water, xenon and heat-exchanger temperatures, ambient |
| `9216_2` | NI 9216 | 3 | PT100 | **none — entirely unconnected** |
| `9226` | NI 9226 | 4 | PT1000 | `ai0:6` — bucket and vessel temperatures |

**Modules are addressed by alias, never by `cDAQ1Mod1/ai0`.** Aliases are
configured in NI-MAX and follow the module, so re-slotting a module changes
nothing here. `phys` in [`channels.yaml`](../software/config.md) is
`<alias>/<line>` — `9207/ai0`.

A module with no enabled channels gets **no task at all**, which is how
`9216_2` costs nothing beyond a row in `devices.yaml`.

### The 9207's current inputs — two of eight

The module has eight voltage inputs (`ai0:7`) and eight current inputs
(`ai8:15`). The current half was wired to nothing until 22 September 2026,
when two 4-20 mA strain gauge load cells were fitted to `ai8` and `ai9` as
`sg101` and `sg102`. `ai10:15` are still connected to nothing and get no task
line: an unwired current input reads a dead loop, and a task line for it would
publish that as a measurement.

Both kinds share the module's **single task**, added in physical order. The
read returns one list, zipped against that order — so a mixed task must stay
sorted by channel index or every value lands on the wrong name.

**DAQmx returns amps.** `sg101`/`sg102` scale with offset `0.004` and
multiplier `1250` (= 20 kg / 0.016 A), giving 0 kg at 4 mA and 20 kg at 20 mA.

**Below 4 mA there is no measurement.** A 4-20 mA transmitter sends 4 mA at
zero load, so anything under that is an open loop, an unpowered transmitter or
a missing sensor — `CURRENT_VALID_A` (3.5-21 mA) catches it and the driver
publishes `quality=error` rather than a negative weight. This is the same
distinction as `RTD_VALID_C` below, on the other half of the module.

**The loops need external 24 V.** The 9207 measures current; it does not
source loop power. A channel sitting at a flat 0 mA with the sensor plugged in
is the supply, not the sensor.

**Both gauges are on the mimic.** The P&ID already carried `SG101` and `SG102`
as tagged instruments with their own bubbles, so
`tools/build_mimic.py` placed their value slots the
moment the channels existed — nothing was drawn by hand. Re-run it after any
change to the drawing.

---

## Why on-demand reads

All four modules are low-rate delta-sigma with differing aggregate rates, and
**they cannot share one hardware-timed task**. Slow control reads at 1 Hz, so
this driver uses **software-timed (on-demand) reads — one task per module,
polled in sequence**. The CompactDAQ timing-engine constraint then does not
apply at all.

This is the single most important thing to know before changing the
acquisition here: reaching for a hardware-timed, multi-module task is the
obvious optimisation, and it does not work on this chassis.

---

## Excitation current

```python
EXCITATION_A = {"NI9226": 100e-6,   # PT1000 — 1 mA and 2.5 mA both refused
                "NI9216": 1e-3}     # PT100  — 100 µA and 2.5 mA both refused
```

Measured from the hardware on 17 September 2026. Each module accepts exactly
one value, and neither is the DAQmx default of 2.5 mA, so both are passed
explicitly.

The physics agrees: 1000 Ω at 1 mA would dissipate a milliwatt in the sensor
and self-heat it, which is why the PT1000 module runs at a tenth of the
current.

!!! warning "Do not let DAQmx choose"
    Dropping the argument is exactly what §7.1's example code does, and the
    task then refuses to configure. It looks like a wiring fault.

RTD type, `r0` and wiring come from each channel's `rtd:` block —
`PT3750`/1000 Ω on the 9226, `PT3851`/100 Ω on the 9216, three-wire
throughout.

---

## Identity

At startup the driver asks NI-DAQmx for the chassis and every module, and
compares **serial numbers** against `devices.yaml`:

```
chassis cDAQ1 serial 020C5E1C confirmed
module 9207 serial 020DFD57 confirmed
```

A mismatch is fatal and the service refuses to start. **A module swapped
between slots is harmless** — the alias follows the module — but a module
*replaced* is a different instrument with a different calibration, and that
needs a deliberate config change rather than silent acceptance.

If NI-DAQmx itself cannot be reached, or the chassis is not present, the
service says so and stops rather than reading nothing in a loop.

---

## What a module fault looks like from the outside

**An open circuit, a short or a missing RTD does not fail the read.** The
module returns a number — the rail — and it is a plausible-looking
temperature. So the driver range-checks:

> Platinum RTDs to IEC 60751 are defined from **−200 to +850 °C**. A reading
> outside that is not a cold or hot sensor; it is an open circuit, a short,
> or no sensor at all.

Such a channel is published with `quality=error` and `value=null` — never
`+1326 °C`. The log line names the channel, its `phys`, the reading and the
likely cause, and it is printed **once** per channel rather than every
second, so a disconnected sensor does not bury everything else in the log.

This is deliberately *not* a threshold in `alarms.yaml`: it is the difference
between a measurement and the absence of one, not between a good value and a
bad one.

| Symptom | Likely cause |
|---|---|
| one RTD channel `error`, reading far outside −200…850 | sensor, wiring or terminal block on that line |
| every channel of one module `error` | the module, or its task |
| the service will not start, naming a serial | a module was replaced — update `devices.yaml` deliberately |
| the service will not start, "cannot reach NI-DAQmx" | driver not installed, or the chassis is off |
| the service will not start, alias not in NI-MAX | aliases were lost — reconfigure them in NI-MAX |

Anything worse than a single channel ends up in the same place: every channel
of the service published as `error`, the state `degraded`, and a backoff
retry — see [How a driver works](index.md#the-loop).

---

## Scaling

RTD channels come back from DAQmx **already in degrees Celsius**, and their
offset and multiplier are the defaults, so scaling is a no-op for them. The
voltage channels are where `value = (raw - offset) * multiplier` actually
does something — `p101` is `(raw − 1.0) × 25.0` bar, and `raw` is archived
alongside so the history can be rescaled if a multiplier turns out to be
wrong.

---

## Handing it back

`close()` releases every task, so LabVIEW can have the chassis back. Like
every device here, it admits one process at a time — see [Starting and
stopping](../operating/running.md#handing-the-hardware-back-to-labview).
