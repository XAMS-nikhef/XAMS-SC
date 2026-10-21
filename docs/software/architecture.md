# Architecture

Every device service reads its instrument and publishes to a local MQTT
broker. Nothing writes to a database directly. Storage, alarming and the UI
are all subscribers, so any of them can be added or replaced without touching
a driver.

**The files are the truth; the database is an index over them.** If PostgreSQL
is lost, replay the archive into a new one. Nothing in any driver changes.

This page is the short version of [the design specification](../DESIGN.md)
§2–§4 — enough to find your way around the code. The specification has the
full argument and the decisions that were rejected.

---

## The shape of it

```
  instruments            services              the bus            subscribers

  cDAQ-9174   ──▶  cdaq      ─┐
  CAEN hv_1   ──▶  caen      ─┤                              ┌─▶  sinks ──▶ JSONL archive
  CAEN hv_2   ──▶            ─┤                              │             PostgreSQL
  Lake Shore  ──▶  lakeshore ─┼─▶  xams/meas/<channel>  ─────┼─▶  alarms ──▶ SMS / email
  APC UPS     ──▶  ups       ─┤    (retained)                │
  HiPace 80   ──▶  turbo     ─┤    (listen-only tap)         │
  PrismaPro   ──▶  rga       ─┤    (Ethernet, HTTP)          │
  LN₂ scale   ──▶  scale     ─┤    (RS-232, queries only)    │
                   derived   ─┘                              └─▶  webui ──▶ browser
                                                                   Grafana ◀── PostgreSQL
```

Eleven processes, each started and stopped by
[`xams-ctl`](../reference/cli.md). Every one of them is an ordinary Python
process with no state worth preserving — except `derived`, which is the one
exception and says so loudly.

| Service | What it is |
|---|---|
| `sinks` | writes the archive and the database. Started first, stopped last |
| `cdaq` | NI chassis: pressures, RTDs, levels |
| `caen` | both HV supplies, in one process |
| `lakeshore` | the 335 temperature controller |
| `ups` | mains and battery state |
| `turbo` | HiPace 80 turbo status and speed, listening on its RS-485 link — never transmits |
| `rga` | PrismaPro residual gas analyser: status channels, control, scans, emission ([RGA](../drivers/rga.md)) |
| `scale` | LN₂ dewar scale: gross weight, LN₂ mass, mains — queries only ([LN₂ scale](../drivers/scale.md)) |
| `derived` | the flow integrator — **the only stateful service** |
| `alarms` | evaluation, notification, flight recorder |
| `webui` | the pages, and this manual |

---

## Five principles, in priority order

They are in the specification (§1) and they decide the arguments, so they are
worth knowing by number.

1. **Readable beats clever.** This replaces a system that failed on
   maintainability, not on capability. A new student must be able to read a
   driver in one sitting.
2. **Configuration is data, not code.** Channels, calibrations and limits are
   in [`channels.yaml`](config.md), under version control. Adding a sensor
   touches no Python.
3. **Monitoring never blocks on control.** A hung write must not stall a read
   loop.
4. **Fail loudly, never silently.** An unidentified device, a stale value, a
   service that has stopped producing — all raise alarms. **A frozen
   plausible value is worse than a gap.**
5. **Restart is a no-op on hardware.** Starting a service never changes an
   instrument's state.

Principle 4 is the one that shows up most often in the code, and usually as a
refusal: a reading that cannot be trusted is published as `quality=error`
rather than omitted, and a device that will not say who it is stops the
service rather than being read anyway.

---

## Why a bus in the middle

A local MQTT broker sits between everything, and the cost is one more moving
part. What it buys:

- **Acquisition and storage cannot entangle.** A driver publishes and is
  done. It holds no database handle, no file handle and no knowledge of who
  is listening, so a database outage cannot reach back into a read loop.
- **A subscriber can be added without touching a driver.** The audit writer,
  the alarm-state writer and the flow-period writer were all added this way —
  no driver changed for any of them.
- **The last value of every channel is always available.** Measurements are
  published **retained**, so a page, a report or a newly started service gets
  the current state from the broker immediately instead of waiting for the
  next reading or querying the database.

The topic layout and the payload schema are the interface every driver and
every sink agrees on, and they have [a page of their own](topics.md).

**The broker is loopback-only, and that is a safety property, not a
convenience.** Commands travel over this bus, so anything that can reach the
broker can set a high voltage. A broker on `0.0.0.0` would be an
unauthenticated control interface for the building network.

---

## What happens to a reading

1. A driver reads its instrument at `interval_s` (1 s) and timestamps the
   value **at the moment of the read**, never at publish time.
2. Raw counts become engineering units through the one formula in
   `scaling.py`: `value = (raw - offset) * multiplier`.
3. Samples accumulate for `log_interval_s` (10 s) and the **mean** is
   published. That is not merely data reduction — a mean of ten readings is
   less noisy than any one of them, so what is stored is better than what is
   discarded.
4. If any sample in the window was bad, the aggregate says so. Quality is
   never averaged.
5. The sinks write it to today's JSONL file and to PostgreSQL; the alarm
   engine evaluates it; the web UI renders it. None of them know about each
   other.

---

## What is stateful, and what is not

Nearly nothing. Kill any service, start it again, and it picks up where the
hardware is — there is no state to restore, and startup is read-only by
design (§6): no setpoint is written, no channel enabled, nothing restored.

**The flow integrator is the exception.** A running total cannot be
rediscovered from the instrument, so it is written to disk after every
publish and read back on startup, gaps are recorded rather than
interpolated, and a reset closes a period instead of zeroing a counter. The
reasoning is on [Derived channels](../drivers/derived.md).

---

## Files, then database

```
data/raw/<UTC date>.jsonl      every measurement, as published — the archive
PostgreSQL                     the same data, queryable — a cache
```

The JSONL writer runs whether or not PostgreSQL is reachable, and the two are
independent: losing the database costs queries and Grafana, not data. This is
what makes the database **disposable** — a property worth keeping, because it
means the schema can be changed, the server moved, or the whole thing
replayed into something else without a driver noticing. [Storage and
sinks](storage.md) has the layout and the replay procedure.

---

## Where to read next

| | |
|---|---|
| the wire format between everything | [Topic contract](topics.md) |
| what the YAML files mean | [Configuration files](config.md) |
| how a reading becomes a row | [Storage and sinks](storage.md) |
| how a reading becomes an SMS | [Alarm engine](alarms.md) |
| how an instrument becomes a reading | [How a driver works](../drivers/index.md) |
| the full argument, with the rejected alternatives | [Design specification](../DESIGN.md) |
