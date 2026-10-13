# Project status

Where the work has got to, and what is outstanding. Each milestone has an
acceptance criterion in [the design specification §15](DESIGN.md); do not start
the next before the current one passes.

## Milestones

| # | Milestone | State |
|---|---|---|
| 1 | Skeleton: config, bus, sinks, simulation, install | **done** |
| 2 | cDAQ read-only | **done** |
| 3 | Channel verification against physical sensors | **partial** — the empirical per-sensor check is still outstanding, see below |
| 4 | Scaling and history import | **done** |
| 5 | Lake Shore + CAEN monitoring | **done** |
| 6 | UPS, alarms, flow integrator | **done** — email delivery finally works (18 Sep) |
| 7 | Web UI and P&ID mimic | **mostly done** — no Python client |

| 8 | Control path | **in progress** — Lake Shore and HV both writable from the UI and the CLI; the shared staged plan of §10a is not built |
| 9 | Procedures | not started |
| 10 | Production | **partly** — running as services, auto-start, nightly backup, daily report; the restore rehearsal is outstanding |

### Where milestone 8 actually stands, 18 September 2026

**Lake Shore: complete.** Setpoint and heater range, output 1 only. Every write
is validated against `channels.yaml`, **read back from the instrument before it
is called successful**, acknowledged on the bus and recorded in `audit` —
including the writes that were refused. Output 2 is read but not writable: it
is not used on this cryostat.

**HV: operable, 18 September 2026.** Setpoints and energising, from `/hv` or
from `xams-ctl hv-set`, `hv-standby`, `hv-on` and `hv-off`.

| Stage | | State |
|---|---|---|
| 1 | Read `VSET` into `hv_*_vset` channels | **done, 18 Sep** |
| 2 | The write path (`VSET`, and `ON`/`OFF`) | **done, 18 Sep** |
| 3 | The plan (staged setpoints, applied as one action) | **partly** — staged in the page, not on the bus |
| 4 | The UI for it | **done, 18 Sep** |
| 5 | Editing the default setpoints from `/hv/defaults` (§4.6) | **done, 20 Sep** |

**The alarms page, 20 September 2026.** `/alarms` shows the alarm chain end to
end: what has fired, the thresholds the engine actually loaded (§11 — published
since the engine was written and displayed nowhere until now), and the
recipient list, which is editable there. Add, remove, and *Notify* on or off;
it applies to the next alarm with no restart.

**Defaults are editable from the web UI since 20 September 2026.** This is an
R&D setup and the operating point moves, so the values *Load defaults* offers
live in `config/hv_defaults.yaml` and are edited from `/hv/defaults` — a file
write, audited, reaching no instrument. The `limits` each default is checked
against stayed in `channels.yaml`, hand-edited and in git, because they are
what the write path validates against (§4.6).

**Stage 3 is the one that is not what §10a asks for.** The plan is held in the
boxes on `/hv`: *Load defaults* fills them from `hv_defaults.yaml`, falling
back to `channels.yaml`, and writes nothing, *Apply setpoints* writes every filled box as one action, and energising
is a separate button per channel. So the plan-then-apply order is real, and so
are the refusals. What is missing is the plan being **server-side and retained
on the bus**: a second browser sees nothing pending, and a reload throws away
what was typed. It matters when two people operate the supplies; it has not yet,
and rebuilding it is the remaining work on this milestone.

**The write path (stage 2) is built and verified against the real supplies.**
It writes exactly four things — `VSET`, `ON`, `OFF` and, since 23 September,
`BDCLR` to clear a latched trip — and nothing else: no
command exists that can change `MAXV`, `RUP`, `RDW`, `TRIP` or `ISET`, that can
enable or disable a channel, or that can take a board out of `LOCAL`. Every
write is read back from the board before it is called successful. Each of these
was refused by the live boards on 18 September 2026, with nothing written:

| asked for | refused because |
|---|---|
| −1000 V on the disabled cathode | not enabled, so its setpoint must stay at 0 (§10a) |
| −3000 V on the cathode | outside the −2500..0 range in `channels.yaml` |
| −500 V on the anode | outside the 0..4500 range — the wrong polarity for that electrode |
| a value on `hv_cathode_vmon` | that is a monitor, not a setpoint |

**The zeroing is done, 18 September 2026.** Both boards were put in `REMOTE`
at the front panel, and all eight setpoints were zeroed — each verified by
read-back, each in the audit trail. **§10a’s invariant now holds**, so enabling
a channel brings it up at zero volts and `/hv` shows nothing armed.

A gate nobody knew about turned up in the process: the supplies were in
`LOCAL` mode, in which the front panel has control and every remote setpoint
is refused with `LOC:ERR` while `MON` keeps working normally. `BDCTR` is
front-panel only, so there are **two** hand gates before software can put
volts anywhere — the board in `REMOTE`, and the channel enabled — and neither
is reachable from code.

**Stage 1 was worth doing on its own, because it made a live hazard visible.**
Reading `VSET` for the first time showed every channel `DISABLED` with these
setpoints sitting in the boards:

| | pmt_bot | pmt_top | ts | bs | cathode | gate | anode | nai |
|---|---|---|---|---|---|---|---|---|
| `VSET` | −700 | 0 | −500 | −600 | −2250 | −1750 | **+4200** | +600 |

The enable switch is a hand operation and the board ramps to `VSET` the instant
it is flipped, so **seven of the eight channels would have gone straight to
those voltages if somebody had touched the switch that morning** — the anode to
4.2 kV. They have since been zeroed, `/hv` marks any recurrence in red, and the
write path exists largely to make that state impossible to reach again.

### Also done since the milestone table was last accurate

- **Nightly backup** of the irreplaceable data to `/data/xenon/xams_slow_control/`
  at Nikhef, verified, registered as a Scheduled Task. The restore has **never
  been rehearsed**, so that path is still a claim.
- **Email works.** `smtp_host` had been empty since the system was built, so
  every email alarm ever raised was silently discarded. Alarm mail now carries
  the plant around the alarm, and there is a daily report at 07:30.
- **The manual** (`/manual`) was never built; it is now, and the three tests
  that had been silently skipping now run.
- **The NI driver** is archived on the cluster — for the ordinary reason that a
  rebuild should not need an ni.com account, not the dramatic one first given
  here. The cDAQ-9174 is **not** discontinued; that claim was wrong.
- **An undefined-name check** over the whole package, after a `NameError` in
  `xams-ctl status` was committed and shipped.

Each has an acceptance criterion in [the design specification §15](DESIGN.md). Do
not start the next before the current one passes.

**Milestone 1 acceptance, met 17 September 2026:** the simulated service
publishes, both sinks store independently, Grafana plots from the provisioned
dashboard, and the install is scripted and reproducible.

**Milestone 2 acceptance, met 17 September 2026:** all 20 connected channels
(6 voltage + 14 RTD) read and logged with LabVIEW stopped, values plausible
(`ttamb` 24.2 °C, cryostat −90 °C, `pmain` 1.495 against LabVIEW's 1.53), and
readings continuous across a service restart to better than 0.7%.

**Milestone 3, partially met 17 September 2026.** Every channel's physical
location is recorded in `channels.yaml`, and the 1xx / 2xx / 3xx series are now
known to mean xenon circulation, detector vessel and bucket, and cooling and
heat exchange respectively (DESIGN.md §3). Readings were checked against the
LabVIEW project and agree.

**The empirical per-sensor check is still outstanding.** Agreement with LabVIEW
confirms we read the same hardware the same way — but it cannot catch a tag
that was already on the wrong channel *in LabVIEW*, because we would inherit
the error and the comparison would agree perfectly. That is the specific
failure §15 says this milestone exists to catch, so it is not yet closed.

**Milestone 4 acceptance, met 17 September 2026.** The column-count census of
§9.6 was run over all 733 log files, one week of history was imported
(6.3 million readings, tagged `src="labview"`), and every channel present in
both systems agrees across the changeover:

```
19 of 19 channels agree, within 0.4%
  pmain    LabVIEW 1.495   XAMS 1.495   -0.03%
  fm101    LabVIEW 7.136   XAMS 7.127   -0.13%
  tt206    LabVIEW -90.317 XAMS -90.460  0.16%
```

Reproduce with `python tools/compare_to_labview.py`.

The largest differences are the cryostat sensors, all drifting the same way by
about 0.2 °C across the 4.8-minute changeover gap — consistent with slow
cooling, not with a scaling error.

**Milestone 5 acceptance, met 17 September 2026.** Both CAEN supplies and the
Lake Shore are read, identity-verified and logged:

```
  2 candidate ports for 21E1:0003: COM4, COM5
  COM4 reports DT1470ET / 19198    -> hv_1
  COM5 reports DT1470ET / 79       -> hv_2
  COM6 reports MODEL335 / 335A12T  -> lakeshore
```

**Unplug test passes.** With the link cut under a running service: readings are
published `quality=error`, the service goes `degraded` and stays alive, and
after two failures it re-enumerates the ports and re-asks every board for its
serial before resuming. A reconnect re-verifies identity rather than merely
reopening the port — a swapped cable during an outage would otherwise resume
under the wrong name (§6.2 rule 6).

Two things the instruments confirmed that the design had recorded from the old
system: the Lake Shore PID is **100, 20, 0**, exactly as §7.3 says, and all
eight HV channels are `DISABLED` with the protection settings in `devices.yaml`
matching the boards.

**Milestone 6 acceptance, met 17 September 2026.**

- **Thresholds** from `alarms.yaml` on `pmain`, `tt104`, `tt302` and three UPS
  channels. Editing one and running `xams-ctl reload` applies it live.
- **Staleness alarms fire.** Proven twice on unplanned faults, not just tests:
  a service that died from a bad config deploy was reported 60 s later, and a
  channel rename produced a staleness alarm for the old name.
- **Flight-recorder dumps** are written on every alarm, holding the ten
  minutes of full-rate data leading up to it.
- **SMS delivered** — one real message through the existing MessageBird
  gateway. Email is not yet configured (no `smtp_host`).
- **The integrator survives a restart** and records gaps rather than
  inventing flow across them. A reset closes a period into `flow_periods`
  with its total, its gaps and who did it, and is written to the audit log.

The UPS is read from its HID **alongside PowerChute**, which keeps doing its
safe-shutdown job. Two other routes were tried and rejected: WMI reports no
battery at all, and `GetSystemPowerStatus` describes the wall socket rather
than the UPS — it would have read "on line power" while running on battery.

**Milestone 7, 17 September 2026.** Web UI on `127.0.0.1:8000` with the five
pages above, and the P&I mimic built from the drawing by
`tools/build_mimic.py`: rotated to landscape, frame and title block removed,
recoloured for the dark interface, with a live value in each instrument bubble.
A **stale channel greys out and shows a dash, never its last number** — a
frozen value displayed as though it were live is the failure this kind of page
exists to avoid. The SVG-vs-`channels.yaml` tag check runs at startup and
reports drift in both directions.

Still outstanding for this milestone: editing `recipients.yaml` from the UI,
and the Python client for notebooks.

**The pressure units are now known: bar**, supplied by the group along with
each transmitter's location. **No channel carries `unit: TBD` any more.** They
were never guessed — the milestone 4 comparison validated the *numbers* to
0.03% while leaving the *labels* open, which is the distinction the `fm101`
"SLPM" story exists to make (§4.2).

The verification found one fault: **the `tt202` sensor has failed** (bottom of
the detector vessel). It is `enabled: false` pending replacement, so the cDAQ
carries **19 live channels, not 20**.

## What still needs doing

**Done:** the services run as Windows services that start at boot and restart
on a crash (`tools\install_services.ps1`, NSSM; verified running on
3 October 2026). How to (re)install them: [Installation §5](install.md#5-windows-services).

Then, roughly in order of how much it matters:

| | Why it matters |
|---|---|
| **The heater cut on a `pmain` hihi** (§10) — decided 18 Sep, not built | The decision is recorded and the threshold is live in `alarms.yaml`, but nothing joins them: a `pmain` hihi notifies a person, and a person cuts the heaters. The safe direction for the gap to be in, but it must not be described to an operator as something the software does. |
| **The Nikhef VM watchdog** — one Grafana rule, "no measurement for 15 minutes" (§12) | The one failure this system cannot report is its own machine being off. Auto-start makes that *less* likely to be noticed, not more, because the system now looks after itself well enough that nobody checks. |
| **Replace the `tt202` sensor** | Then `enabled: true` in `channels.yaml` and `xams-ctl reload`. Nothing else changes. |
| **Rotate the MessageBird key** | It sat in plaintext in three copies on the Desktop for years. Update `secrets.yaml` and the LabVIEW scripts together while LabVIEW is still the fallback. |
| **Alarm thresholds for the remaining channels, and the response to each** | A threshold without a prescribed action is half the information (§14). [Operating](operating/index.md) has the section waiting. |
| **RGA (PrismaPro)** — steps 0–1 deployed 13 Oct (status + `/rga`); step 2 (control, scans, Save, `rga_summary`) built, **not yet deployed** | On-demand gas analysis from the web UI instead of PV MassSpec, with a p105 interlock (< 5 × 10⁻⁴ mbar) to come with emission. Plan and status: [RGA](drivers/rga.md). |
| **Milestone 3's empirical check** | Readings agree with LabVIEW, which cannot catch a tag that was already on the wrong channel *in* LabVIEW. Warming one sensor at a time and watching which value moves is the only thing that can. |
