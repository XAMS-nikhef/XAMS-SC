# How a driver works

Every driver does the same four things: find its instrument, prove it is the
right one, read it on a schedule, and publish. None of them write.

Device resolution **never uses a COM number**. Candidates are narrowed by USB
hardware ID, then the instrument is asked who it is.

Three exceptions to "none of them write", all deliberate and all narrow: the
[Lake Shore](lakeshore.md) writes a setpoint and a heater range on output 1,
the [CAEN](caen.md) driver writes `VSET`, `ON` and `OFF`, and the
[RGA](rga.md) driver takes and releases control, sets up, starts and stops
scans and switches emission On or Off - through a whitelist, behind a p105
guard. Everything else
in this directory only reads.

---

## What a driver actually contains

`BaseService` holds the loop, the backoff, the heartbeat, the single-instance
lock and the averaging window — once, for all of them. A driver implements
two methods:

```python
def verify_identity(self) -> bool:
    """Ask the device who it is. Return False to refuse to start."""

def read(self) -> list[Measurement]:
    """One acquisition cycle. Raise on failure; the loop handles backoff."""
```

and may implement three more: `reconnect()` to rebuild a lost link,
`close()` to release the hardware, and `on_reload()` to take a new
configuration without restarting.

That is the whole contract. A driver is short because everything that is the
same in every driver is not in it.

---

## Five rules the base class exists to enforce

These come from §6 of [the design specification](../DESIGN.md), and each one
is there because the opposite behaviour has cost somebody a day.

**1. Startup is read-only.** No setpoint written, no channel enabled, no
state restored. A restart must be invisible to the hardware — which is what
makes restarting a service a safe thing to do at three in the morning.

**2. A busy device is a clean failure, not a stack trace.** Every instrument
here admits exactly one process, and while LabVIEW is still the fallback that
happens routinely.

**3. One instance only.** A lock file per service. Two copies of a driver is
not a tidiness problem, it is two processes fighting over an instrument.

**4. The service never exits because a device disappeared.** It publishes the
failure, backs off, and keeps trying. It exits only when it cannot be
identified **at startup** — an unplugged instrument is a fault to report, not
a reason to stop reporting.

**5. A stale value is never left standing as if fresh.** This is the one that
shapes the most code.

---

## Why a frozen plausible value is worse than a gap

A gap says "nobody knows what happened here". A frozen value says "this was
2.1 bar for six hours", and it says it in the same typeface as the truth.
Everything downstream believes it: the plot looks flat and healthy, the
alarm engine sees a value inside its limits and stays quiet, and the daily
report shows a number. Nothing anywhere indicates that the last real reading
was at breakfast.

That failure is silent, and it is discovered — if at all — long after the
decisions made on the strength of it. So:

- a read that fails publishes **every channel of that service** with
  `quality=error` and `value=null`, not the last good number;
- an averaging window containing a bad sample is published as `stale`,
  because quality is never averaged;
- a channel that stops arriving raises a [staleness
  alarm](../software/alarms.md) at the same severity as a threshold breach;
- the web UI and the emails show a dash for anything untrustworthy, never a
  last-known value.

---

## Identity: asking, not assuming

A device is identified by **asking it**, never by which COM port it happens
to occupy. A port renumbered by Windows, an instrument moved to another hub
socket, or two cables swapped are therefore all harmless.

`devices/serial_id.py` implements the six rules of §6.2 once, for every
serial driver:

1. **Never probe a port that did not pass the VID/PID filter.** Writing bytes
   at an unknown serial device is not a neutral act — this machine also
   exposes COM3 as Intel AMT Serial-over-LAN.
2. **Match on the identity pair** — model *and* serial — never the serial
   alone.
3. **A malformed, truncated or absent reply means unidentified.** Never
   "probably the right one".
4. **Probe every candidate before binding any.** Resolution is two-pass.
5. **Two ports reporting the same identity is fatal.** There is no safe way
   to guess which unit is which.
6. **Re-verify on every reconnect**, not only at startup.

Rule 6 is the subtle one. A reconnect after a USB glitch is exactly where a
swapped cable slips through: the port reopens, readings resume, and they are
the wrong instrument's — under the right channel names, into the permanent
archive.

For the [CAEN supplies](caen.md) this is not a cross-check on the hardware
ID, it is the *only* identification: neither unit exposes a USB serial number
and both present the same VID/PID, so step 1 cannot tell them apart at all.

---

## The loop

```
read every interval_s (1 s)  ──▶  accumulate
publish every log_interval_s (10 s)  ──▶  mean of the window + heartbeat
```

**Sampling and logging are separate decisions.** The mean of ten readings is
less noisy than any one of them, so what is stored is better than what is
discarded — the averaging is not merely data reduction.

When `read()` raises:

1. every channel of the service is published as `error`;
2. the service state goes to `degraded`;
3. the retry backs off — 1 s, 2 s, 4 s … to a 30 s ceiling;
4. after two consecutive failures `reconnect()` is called, because retrying a
   read on a link that is gone will never succeed;
5. on the first good read the state returns to `running` and the backoff
   resets.

Each cycle also publishes a heartbeat, and logs a warning every 30 seconds
while the broker is unreachable — acquisition continues regardless, but an
outage that produces no log line is indistinguishable from a healthy system
with nothing to say.

---

## Simulation

Every driver can run without its instrument, and `devices/sim.py` publishes
synthetic values for the **whole** channel map — enough to develop the stack
on a laptop while the lab PC is still running LabVIEW.

Simulated readings carry `src="sim"` and are given a visible wobble rather
than a flat line, so nobody reads physics out of them. The provenance field
exists because synthetic values once reached the production database and
could not be told apart from measurements afterwards.

---

## The drivers

| | |
|---|---|
| [NI cDAQ-9174](cdaq.md) | pressures, RTDs, levels — 20 channels |
| [CAEN DT1470ET](caen.md) | two supplies, eight HV channels |
| [Lake Shore 335](lakeshore.md) | cryostat temperature control |
| [UPS](ups.md) | mains and battery |
| [Turbo pump](turbo.md) | HiPace 80 status and speed, listen-only on RS-485 |
| [Derived channels](derived.md) | computed, not read |
| [Writing a new driver](writing.md) | start to finish |
