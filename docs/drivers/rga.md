# Residual gas analyser (PrismaPro)

!!! warning "Partly built: Faraday cup only"
    This page is the implementation plan and the record of what is built.
    Steps 0-2 are deployed (13 October 2026): status, control, scans and
    Save. Step 3, emission On/Off behind the p105 guard, is built; its first
    run with emission on is to be done together in the lab. The electron
    multiplier is a later stage. See [Steps](#steps).

Pfeiffer PrismaPro QMG 250 on the pre-volume, on the lab Ethernet. Used
occasionally, until now with Pfeiffer's PV MassSpec. The plan is an RGA
service in this system and an RGA page in the web UI, for **occasional,
on-demand** measurements: few scans, saved by hand, no continuous RGA data in
the archive or the database.

It is the third instrument this system writes to, after the Lake Shore and
the CAEN supplies. That is a design change and goes into
[DESIGN.md](../DESIGN.md) when the first write is built.

---

## The device

Read on 13 October 2026 from the slow-control PC, with GETs only: no control
taken, nothing set.

| | |
|---|---|
| Address | `192.16.192.86` = `prismapro-xams-rga.nikhef.nl`, port 80. Reachable from the slow-control PC, not from the Nikhef office network |
| Name | `RGA PrismaPro A 200 47504651`, sensor serial 47504864, mass range **200 amu** |
| Firmware | control `1.9.5.202306010915` (≥ 1.9.0: the control/lock procedure below applies), monitor `1.05.00` |
| Filaments | three entries, all type 1 = **yttria-coated iridium** (more tolerant of air than tungsten); filament 1 selected |
| Filament hours | `emisOnTime` 2 330 444 s ≈ 650 h on filaments 0 and 1; 20 919 s on filament 2 |
| Detector | CDEM; `emOnTime` 409 |
| Ion source | emission 2000 µA (level `Lo`), electron energy 70 V, ion energy 8000, optimisation `Linearity` |
| Sensitivity | `ppSensitivityFactor` 7.93 × 10⁻⁴ A/Torr |
| Pressure units | `tPunits` = 0 = **Torr** |
| Own over-pressure trip | `tpThresholdCurrent` 5 × 10⁻⁷ A, `tpTresholdCount` 1; `tpTripCount` **32** (emission switched off by the device itself 32 times in its life) |
| State | emission, RF and EM off; no interlock; hardware errors and warnings 0; filaments not open; nobody in control |
| `systemStatus` | `0x01103B90`: bits 4, 7, 8, 9 (baseline correction on), 11, 12 (peak position correction on), 13, 20, 24 |
| `ionSourceAnomaly` | `0x40000000` (bit 30) — meaning to look up |
| Error log | 1163 entries in its life, none current |
| Session timeout | `sessionTimeout` 60 s |

**Every group can be read without control.** The full state of all groups
was read with GETs on `/mmsp/<group>/get.api` without taking control,
including targets the API reference marks `readmode: locked`
(`measurement/totalPressure` returns 0 with emission off, not −1 as the
reference says). Writes are what need control. `/mmsp/diagnostics` returns
nothing.

**There is no machine-readable schema on this firmware.** `/mmsp/get.api`
returns current *values*, group by group (`@isStub` entries expand one level
at a time), not the JSON schema the quick-start guide describes. The API
reference PDF stays the description of types, ranges and write modes.

**It drops rapid back-to-back connections.** A burst of separate `curl` calls
mostly came back empty; one request a second, or one reused HTTP connection,
worked every time. The client keeps one connection open.

The vendor package, the full state snapshot of 13 October and the API
reference live in the workspace, `reference/PrismaPro/`, not in this
repository.

---

## What is built (step 1, 13 October 2026)

**`devices/prismapro.py`** — the MMSP client. One kept-alive HTTP connection,
a small gap between requests, one retry on a fresh connection, every reply
checked by its `name` (an error event is an error even with HTTP 200, an
empty reply is an error, not a `None`). **It has no write method.**
Also `decode_system_status`, `emission_on`, and `read_identity`. No MQTT, so
it works from a notebook:

```python
from xams_sc.devices.prismapro import MMSPClient, read_identity
c = MMSPClient("192.16.192.86")          # reachable from the slow-control PC
read_identity(c).as_dict()
c.get("status/systemStatus")
```

**`devices/rga.py`** — `RgaService`, `python -m xams_sc.devices rga`. Refuses
to start unless the analyser reports the `match.name` and `match.serial` from
`devices.yaml`. Reads every `read_interval_s` (5 s): one GET per target, about
0.7 s a cycle, so 6 requests every 5 s. Takes no control. Publishes:

| Channel | From | Unit | |
|---|---|---|---|
| `rga_emission` | `status/systemStatus` bit 31 or 30 | bool | the real state, not the last request |
| `rga_control` | `communication/controlInfo` | code | 0 nobody, 1 this system, 2 another client |
| `rga_status` | `status/systemStatus` | bits | named on the page |
| `rga_hw_errors` | `status/hardwareErrors` | bits | 0 = none |
| `rga_hw_warnings` | `status/hardwareWarnings` | bits | 0 = none |
| `rga_tp_trips` | `sensorIonSource/tpTripCount` | code | the device's own lifetime trip count; a rise means it switched emission off itself |
| `rga_box_temp` | `diagnosticData/internalBoxTemperature` | C | electronics box |

These are ordinary channels: archived, in PostgreSQL, available to alarms and
Grafana - seven numbers every 10 s, never spectra (decided 13 October 2026).
No alarm thresholds are set on them yet. The identity and **who** holds
control (client name, address) go on the retained topic `xams/rga/info`,
outside `xams/meas`, so they are not archived.

**The RGA page** (`/rga`, in the navigation after System health): identity,
emission, control and by whom, hardware errors and warnings, trips,
electronics temperature, and the named `systemStatus` bits. No form on it.

**Tests:** `tests/test_rga.py`, on replies recorded from the analyser on
13 October (`tests/recorded/prismapro_2026-10-13.json`). Checked against the real
analyser through the slow-control PC the same day: identity accepted, all
seven channels read.

**Deploying it** (not done; needs an OK): pull on the slow-control PC, then
`.\tools\install_services.ps1 -Only rga` **in the same session**. Once the
configuration has the `rga_*` channels, the badge says "CHANNEL(S) NOT OK" and
System health "SERVICE NOT REPORTING" until the service runs.

---

## What is built (step 2, 13 October 2026)

**Control, scans and Save**, from the RGA page. Still no emission: the
client's whitelist (`prismapro.WRITABLE`) holds `communication/clientName`,
`control`, `controlLock`, `scanSetup` and `scanSetup/channels/<n>`, with a
fixed list of fields for the last two, and `MMSPControl._set` refuses
anything else before a byte is sent.

Found in a live test with control taken and emission off (13 October 2026):
writes answer `"name": "wasSet"`; a sweep channel reports
`reportUnits: "Current"`, so **scan values are ion currents in A** (noise
about ±1.6 × 10⁻¹⁴ A with emission off); `systemStatus` bit 1 is set while a
scan runs; `nextScan` hands over each scan once. 1–50 amu at 4/amu gave 197
points per scan, 2 scans in 15 s.

**Control.** *Take control* refuses if another client holds the analyser
unless "take it anyway" is ticked (with a confirmation); *Release* gives it
back and stops a running scan. The service releases by itself after
`release_idle_s` (300 s) without a scan or a command, and on shutdown.

**Recipes** in `config/rga_recipes.yaml`: `survey` (1–200 amu, 10/amu,
16 ms, 3 scans of ~32 s), `air_leak` (2, 4, 14, 18, 28, 32, 40, 44 at 256 ms,
10 scans of ~2 s), `xenon` (124–136 amu, 5 scans of ~4 s), `quick` (the
tested 1–50 amu set-up, 2 scans of ~6 s). Validated at startup
(`rga_file.validate_recipe`): out-of-bounds is refused, never clipped.

**Acquisition.** *Start* loads the recipe into the analyser's channels 1..n
and starts `scans` scans; while it runs the service reads every second and
drains `nextScan`. A scan whose length differs from the recipe's is not
recorded (the acquisition fails, saying why). The latest scan and the running
mean go on retained `xams/rga/scan` for the live plot (canvas on the page,
log or linear, polled every 2 s).

**Save** writes the finished (or stopped) acquisition to
`data/rga/YYYY/<start>_<recipe>.json` (format `xams-rga/1`, see
`rga_file.py`): all scans and their mean in A, the recipe, the analyser's
identity and settings at Save (emission current and state, electron energy,
EM state, sensitivity, units, total pressure), the latest values of
`snapshot_channels` (`p105`, `pmain`), comment, tags, background file and
operator. One acquisition is saved once; a file is never overwritten.
`load_rga(path)` reads it back with the mass axis and numpy arrays.

**Summary** → `rga_saves` (one row per Save) and `rga_summary` (one row per
summary mass: the largest mean current within ±0.5 amu, and a partial
pressure in mbar from the device's own sensitivity and unit). Summary masses
in `devices.yaml` `rga.summary_masses` (2, 4, 18, 28, 32, 40, 44, 129, 132).
Written by `sinks/rga_writer.py` from `xams/rga/saved`; rebuilt from the
files with `python tools/rga_rebuild_summary.py`. Tables in `sql/schema.sql`.
Local PostgreSQL only for now; the Grafana dashboard (step 4) decides whether
they also go to the Nikhef VM.

**The page keeps its own writes.** Every other instrument write is on the
Control page; the RGA's are on `/rga`, beside the spectrum they act on (one
workflow: take, scan, look, save, give back). Control links to it; tests pin
both.

**Checked end-to-end on the analyser** (13 October 2026, from a test machine
through the slow-control PC, emission off): take → `quick` → 2 scans in
14.5 s → Save (12.6 kB file) → release; nobody in control afterwards.
Full test suite 1029 passed.

**Deployed** 13 October 2026 (`0a51a16`, with the lock fix of #14):
`sql/schema.sql` applied (`rga_saves`, `rga_summary`, owned by `xams`),
`XAMS-sinks`, `XAMS-rga` and `XAMS-webui` restarted. First run from the page:
take → `quick` → 2 scans → Save → release in 21 s; file
`data/rga/2026/2026-10-13T150521_quick.json` (13.4 kB), one `rga_saves` row
and seven `rga_summary` rows (background, emission off: 0.6-1.4 × 10⁻¹⁴ A),
four audit records.

---

## What is built (step 3, 13 October 2026)

**Emission On/Off** from the RGA page. The whitelist gains exactly one target,
`generalControl/setEmission`, with exactly two values, `On` and `Off`
(`prismapro.EMISSION_VALUES`). The electron multiplier (`setEM`), filament
selection, degas and the ion-source settings stay outside it.

**On** is refused unless all of these hold, each refusal saying why:

- this system holds the analyser;
- the **p105 guard** allows it: `p105` received, quality `ok`, in mbar, at
  most `guard_max_age_s` (30 s) old, and below `guard_limit_mbar`
  (5 × 10⁻⁴ mbar);
- the analyser reports no hardware errors;
- its emission interlock (systemStatus bit 27) is not active.

The reply is read back (`setEmission` must say `On`); the real state follows in
seconds and is the `rga_emission` channel (systemStatus bit 31/30), not the
request. **Off** needs only control.

**The watchdog** runs every read cycle while emission is on and this system
holds the analyser, and only ever switches emission **off** (audited as actor
`rga service`, the reason shown on the page):

- p105 at or above the limit → off at once, no grace;
- p105 missing or stale → off after `guard_grace_s` (60 s) on top of the 30 s;
- no scan or command for `emission_idle_off_s` (1 h) → off.

Emission switched on by another client (PV MassSpec) is left alone: without
control nothing can be written, and that is the other client's to look after.

**Never left on behind the service:** *Release control* switches emission off
first; control is not given up automatically while emission is on (so the
watchdog keeps the means to act); a stopping service switches it off before
releasing.

**Commands run on a worker thread.** The MQTT thread only queues them, so an
analyser call taking seconds can no longer stall what the service publishes
(the hang of 13 October 2026).

**The page** now has two rows: the instrument and its state side by side, and
below them a *Controls* panel (control, emission, scan, Save) to the left of
the spectrum. The p105 value, its age and the limit are shown in the state
card; the reason the guard gives is shown under the emission button, which
is disabled while the guard says no.

Settings, all in `devices.yaml` `rga:` with these defaults: `guard_channel`
`p105`, `guard_limit_mbar` 5e-4, `guard_max_age_s` 30, `guard_grace_s` 60,
`emission_idle_off_s` 3600, `release_idle_s` 300.

**Not yet tested with emission on.** Tests (fake analyser): 78 for the RGA,
full suite 1065. The first emission-on run is for the lab, together: take
control → Emission ON → `quick` → Save → Emission OFF → Release.

---

## The API, in short

Plain HTTP GET on port 80, JSON back. `/mmsp/<group>/<target>/get`, or
`/set?<value>`; several fields at once with `/mmsp/<group>/set?a=1&b=2`.

**Errors arrive as HTTP 200** with a JSON event whose `name` is `error` and a
`conditions` list (`id` e.g. `msgIdInvalid`). The client checks `name`, never
only the HTTP status.

**One controller at a time** (firmware ≥ 1.9.0):

1. `communication/controlInfo/get` — check nobody (PV MassSpec, the device's
   own web page) is in control.
2. `communication/clientName/set?<name>`, `communication/control/set?force`,
   `communication/controlLock/set?Session`.
3. Release: `controlLock/set?Unlocked`, then `control/set?release`.

Control falls back to "everybody" after a few minutes without requests, so
the service keeps it alive while it holds it. `force` takes control away from
PV MassSpec, so it is used only when an operator chooses it.

**A scan is a list of channels.** Each channel has a `channelMode`: `Sweep`
(start and stop mass, points per amu, dwell in ms), `Single` (one mass), or a
housekeeping value (`Timestamp`, `TotalPressure`, `SystemStatus`,
`EmissionCurrent`, …). `scanSetup/set?startChannel=..&stopChannel=..&scanCount=..`
then `scanStart=1`. Points per sweep = 1 + (stop − start) × ppamu; scan time ≈
points × dwell.

**Reading:** `measurement/nextScan/get` gives the next unread scan with
`scannum`, `currentScan` and `systemStatus` — no scan is lost or read twice.
`measurement/scans/-1/get` is the last complete scan.

**Emission:** `generalControl/setEmission/set?On|Off`. On also starts the RF;
it takes seconds. The sensor must be under vacuum, or the filament is
destroyed.

---

## Where it goes in this system

The same pattern as every other driver: its own process, publishing to MQTT;
the UI and storage are subscribers.

```
PrismaPro ◄─HTTP─► rga service (own process) ──MQTT──► web UI: RGA page
                        │   ▲                    ├──► audit (emission, control)
                        │   └── p105 ◄───────────┘
                        └──► data/rga/YYYY/*.json  (on Save) ──► rga_summary ──► Grafana
```

| File | What | Knows MQTT? |
|---|---|---|
| `devices/prismapro.py` | HTTP client (one connection, error events, timeouts; built, read-only), later: whitelist of writable targets, control take/keep/release, recipe → `scanSetup` | no — usable from a notebook |
| `devices/rga.py` | the service: identity check, state on the bus, commands from the bus (worker thread), live scans to the page, Save, emission watchdog, p105 guard | yes |
| `rga_file.py` | JSON writer, `load_rga(path)`, summary extraction — shared by the service, the rebuild tool and analysis | no |
| `sinks/` | `rga_summary` rows on "saved" events; rebuild from `data/rga/` | — |
| `api/templates/rga.html` | the RGA page | — |
| `config/devices.yaml` `rga:` | address, client name, summary masses, p105 limit | — |
| `config/rga_recipes.yaml` | survey 0–200 amu; air leak 2, 4, 14, 18, 28, 32, 40, 44; xenon 124–136 | — |

**Identity:** `sensorInfo/name` must be `RGA PrismaPro A 200 47504651` and
`electronicsInfo/serialNumber` `47504651`, or the service refuses to start —
the HTTP equivalent of `*IDN?`.

---

## Data

| What | Where | When |
|---|---|---|
| Live spectra | memory and the browser only | while scanning |
| Full measurement | `data/rga/YYYY/YYYY-MM-DDTHHMMSS_<recipe>.json` | only on **Save** |
| Summary: total pressure and ~10 masses | PostgreSQL `rga_summary` → its own Grafana dashboard | only on Save |
| Emission on/off, control taken/released | audit, as for HV | on the action |
| State: emission, control, status words, trips, box temperature | seven ordinary channels (`rga_*`), archived like any channel | every 10 s |
| Identity, who holds control | retained `xams/rga/info`, not archived | on change |

- The RGA files are a separate set from `data/raw/*.jsonl`. No spectra in the
  jsonl stream, none in PostgreSQL.
- The files are the truth: `rga_summary` is rebuilt from `data/rga/` the same
  way the readings tables are rebuilt from the archive.
- **One file per Save**, plain JSON: UTC time; recipe name and full
  definition; device serial, firmware, filament, emission current, electron
  energy, EM state and voltage, units, sensitivity factor; a snapshot of the
  slow-control values at that moment (p105, pmain, temperatures); comment and
  tags (`background`, `before-fill`, `after-bake`, `leak-check`); the file name
  of the background scan it refers to. Data: N raw scans (default 5–10, at most
  20) and their mean, total pressure and `systemStatus` per scan. Values as
  the device gives them, not rounded; no mass axis (start, stop and ppamu
  define it).
- **Size:** about 14 bytes a point plus 2–3 kB of metadata. 0–200 amu at 10
  points/amu is 2001 points, about 30 kB a scan; a Save of 10 scans about
  300 kB.
- **Background:** an emission-off (or baseline) scan saved as its own file at
  the start of a session.
- **Trend mode** (selected masses and total pressure about once a minute, into
  `rga_summary`) only later, only started by hand, with an automatic stop.

---

## Safety

- **Emission only from the page's emission button**, with a confirmation.
  Never from a recipe, never from an alarm. The only automatic action is *off*.
- **p105 guard: the RGA may be operated only with p105 below
  5 × 10⁻⁴ mbar** (= 3.75 × 10⁻⁴ Torr). A software guard on top of the
  analyser's own trip, not an interlock (DESIGN.md §10 rule 1). The service
  subscribes to p105:
    - before emission on: refuse if p105 is missing, stale or at or above the limit;
    - during operation: p105 at or above the limit → emission off at once;
    - p105 stale: a grace period (60 s), then emission off;
    - every guard action audited. Details: [step 3](#what-is-built-step-3-13-october-2026).
- **Caveat:** p105 is measured at the turbo; the RGA is on the pre-volume with
  valves in between. With a valve closed, p105 can be low while the RGA sees
  more. The device's own over-pressure trip stays the last line of defence;
  this system reads its settings and never changes them.
- **Writable targets are a whitelist** in `prismapro.py`: control and lock,
  `clientName`, `scanSetup`, `setEmission` (On/Off only). Never
  `sensorIonSource`, `setEM`, `shutdown`, firmware, network, IO, `userDB` or
  degas. Every write
  is checked, read back, acknowledged on the bus and audited, as for `VSET`.
- **Never `force` without an operator choosing it**, and control is released
  when the service is idle, so PV MassSpec can still be used.
- **Own process.** The HTTP calls are slow (seconds for emission); nothing in
  them can block another service.

---

## Steps

0. **Discovery, read-only** — done 13 October 2026 (this page).
1. **Client and status** — built and deployed 13 October 2026 (see
   [What is built](#what-is-built-step-1-13-october-2026)).
2. **Control, scans, Save** — built and deployed 13 October 2026 (see
   [What is built](#what-is-built-step-2-13-october-2026)).
3. **Emission On/Off with the p105 guard** (steps 3 and 5 of the original
   plan, done together) — built 13 October 2026, not yet deployed, first
   emission-on run in the lab (see
   [What is built](#what-is-built-step-3-13-october-2026)).
4. **Grafana:** its own dashboard on `rga_summary`; a file browser on the page.
5. **Electron multiplier** (CDEM, ~100-1000× the Faraday cup's signal): later.
   On only with emission on and a low pressure, off before emission off.
6. **Later:** trend mode; analysis helpers.

**Decided in step 2:** values stored as the device gives them (A); pressures in
mbar converted in the service, the device left in Torr; summary masses 2, 4,
18, 28, 32, 40, 44, 129, 132 (`devices.yaml`, changeable, history rebuilt from
the files).
