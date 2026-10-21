# XAMS Slow Control — Software Design Specification

**Version 0.2 — 16 September 2026, revised 20 September 2026**

Companion to `XAMS-slow-control-options.pdf` (the decision document). That document explains *why*; this one specifies *what to build*.

Status of this document: authoritative for implementation. It describes both
what is built and what is specified but not yet built, and says which is which
at each point; **what is actually done is tracked in [`status.md`](status.md)**,
and where the two disagree that file is right. Anything marked **TBD** must be resolved before the affected component is written; nothing else should be invented at implementation time. If a TBD blocks progress, record the assumption in the code comment and list it in §16.

---

## 1. Scope and principles

**In scope.** Monitoring and convenience control of the XAMS slow-control hardware: one NI cDAQ-9174 chassis (20 connected channels of 40 available), two CAEN DT1470ET HV supplies (8 channels), one Lake Shore 335, one UPS. Storage, plotting, alarming, and a small web UI.

**Platform decision, 16 September 2026.** A Python service stack on Windows, on the existing control PC. EPICS was specified in full as an alternative ([`notes/EPICS.md`](https://github.com/XAMS-nikhef/XAMS-SC/tree/main/notes/EPICS.md)) and set aside: the CompactDAQ carries 20 of the 30 connected channels, has no maintained EPICS device support, and so needs a Python soft IOC either way — EPICS would mean maintaining two paradigms instead of one.

**Out of scope.** Safety interlocks. Anything that protects hardware or people belongs in hardware or in the instrument's own limits. See §10.

**Principles, in priority order.**

1. **Readable beats clever.** The system is replacing a project that failed on maintainability, not on capability. A new student must be able to read a driver in one sitting.
2. **Configuration is data, not code.** Channels, calibrations and limits live in `channels.yaml`, under version control. Adding a sensor touches no Python.
3. **Monitoring never blocks on control.** A hung write must not stall a read loop.
4. **Fail loudly, never silently.** A device that cannot be identified, a value that is stale, a service that has stopped producing — all raise alarms. A frozen plausible value is worse than a gap.
5. **Restart is a no-op on hardware.** Starting a service never changes an instrument's state.

---

## 2. Repository layout

```
xams-sc/
├── README.md                   short front page; the manual is docs/
├── mkdocs.yml                  the manual, served by the web UI at /manual
├── docs/
│   ├── index.md                what it is, and where to start
│   ├── install.md              reinstalling from scratch
│   ├── status.md               milestones, and what still needs doing
│   ├── operating/              running it, and what to do when it breaks
│   ├── software/  drivers/  grafana/  reference/
│   └── DESIGN.md               this document
├── notes/                      working material: EPICS.md, PDF toolchain, P&ID, LabVIEW export
├── pyproject.toml
├── config/
│   ├── channels.yaml           channel definitions — single source of truth
│   ├── devices.yaml            device connection + identity
│   ├── alarms.yaml             thresholds and severity routing
│   ├── recipients.yaml         who gets notified
│   ├── secrets.yaml            credentials — NOT in git (§11)
│   └── secrets.example.yaml    template with empty values, in git
├── src/xams_sc/
│   ├── __init__.py
│   ├── bus.py                  MQTT publish/subscribe wrapper
│   ├── config.py               load + validate YAML, compute config hash
│   ├── model.py                Measurement, Status, Quality dataclasses
│   ├── service.py              BaseService: lifecycle, reconnect, heartbeat
│   ├── scaling.py              raw -> engineering units, and the HV sign (§7.2)
│   ├── hv_status.py            decode the CAEN STATUS word (§7.2) — no serial import
│   ├── grafana.py              dashboard drift check against git (§12)
│   ├── devices/
│   │   ├── __main__.py          entry point: python -m xams_sc.devices <name>
│   │   ├── serial_id.py         resolve serial instruments by identity (§6.2)
│   │   ├── sim.py               synthetic data, no hardware (see note below)
│   │   ├── cdaq.py
│   │   ├── derived.py           flow integrator (§7.5)
│   │   ├── caen.py
│   │   ├── lakeshore.py
│   │   ├── ups.py
│   │   └── turbo.py             HiPace 80, listen-only RS-485 tap (§7.4a)
│   ├── sinks/
│   │   ├── __main__.py          one process runs them all (§9.4b: one lock)
│   │   ├── pg_writer.py         PostgreSQL — measurements
│   │   ├── jsonl_writer.py      the archive (§9.3)
│   │   ├── alarm_writer.py      alarm state transitions (§11)
│   │   ├── audit_writer.py      every write, append-only (§10 rule 5)
│   │   └── flow_writer.py       closed integrator periods (§7.5)
│   ├── alarms/
│   │   ├── __main__.py          entry point
│   │   ├── engine.py           threshold evaluation, hysteresis, dedup
│   │   ├── notify.py           SMS, email, sound
│   │   ├── mail.py             HTML for the alarm and digest emails
│   │   ├── daily.py            the daily report (§11), run from Task Scheduler
│   │   └── flight_recorder.py  the last 10 minutes at full rate (§9.1)
│   ├── api/
│   │   ├── app.py              FastAPI: setpoints, status, web UI (127.0.0.1 only)
│   │   ├── state.py            live state from retained MQTT, never the database
│   │   ├── templates/          server-rendered pages, no build step (§8.1)
│   │   ├── site/               the built manual, mounted at /manual
│   │   └── static/
│   │       └── xams_pid.svg    the P&ID, tag bubbles carry channel ids (§8.2)
│   └── cli/
│       └── xams_ctl.py         start/stop/status/reload/check, and the control verbs (§12)
├── sql/
│   └── schema.sql              tables + indexes — run on every install
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/        points Grafana at PostgreSQL
│   │   └── dashboards/         tells Grafana where the JSON lives
│   └── dashboards-archive/*.json   the dashboards as git holds them (§12)
├── tests/                      see §13
└── tools/
    ├── setup_services.ps1      install broker, database, Grafana
    ├── install_services.ps1    install the XAMS services under NSSM (§12)
    ├── backup.ps1              nightly backup, from Task Scheduler
    ├── build_docs.py           build the manual into api/site/
    ├── build_mimic.py          the P&ID SVG (§8.2)
    ├── save_dashboard.py       --save / --check / --load (§12)
    ├── clear_retained.py       clear the broker's retained messages (§9.4b)
    ├── import_labview_csv.py   convert CSV history into JSONL/Parquet
    └── compare_to_labview.py   validation against historical CSV
```

**Designed but not built**, so that their absence is not mistaken for an
oversight: `sinks/mongo_writer.py` (§9.5), `api/client.py` — the Python client
for notebooks, milestone 7 — `tools/jsonl_to_parquet.py` (§9.4) and
`procedures/` (§10). Each is described where it belongs and tracked in
`docs/status.md`.

Each service is a thin `__main__` that constructs a device class and runs `BaseService`. No business logic in entry points.

### 2.1 Why a message bus

Every service publishes to MQTT; nothing writes to a database directly. The cost is one extra moving part. What it buys:

**Consumers can be added without touching a driver.** Each of these is a subscriber, and none of them requires a change anywhere else: the PostgreSQL writer, the JSONL archive, the MongoDB writer during the transition (§9.5), the alarm engine, the flight recorder, the web UI, and a forwarder to a remote server if remote viewing is ever wanted.

**Storage decisions stay reversible.** This design moved from InfluxDB to PostgreSQL during its own drafting. Without a bus that would have meant a different database client in every driver; with one it is a single new file in `sinks/`.

**Debugging is direct.** `mosquitto_sub -t 'xams/#'` shows every measurement live, with no UI and no database involved.

**It enforces a boundary.** This is the real argument. Without a bus, "just write this one thing here as well" gradually accumulates inside the drivers, and acquisition, scaling, storage and alarming end up entangled — which is precisely what happened to the LabVIEW project. The bus makes the wrong thing awkward to do.

**On MQTT specifically.** It is an OASIS standard, also ISO/IEC 20922, with a frozen specification. It was built at IBM in 1999 for SCADA telemetry over low-bandwidth satellite links, so its design target is exactly this: instrument readings sent to a central point, tolerant of interruptions. Mosquitto (the broker) and paho-mqtt (the Python client) are both Eclipse Foundation projects, packaged in every major distribution, stable for over a decade. Neither is a one-person project that could disappear.

Note for context: MQTT is mainstream in industry and IoT, but it is *not* the convention in accelerator and large-detector controls, where EPICS and Tango dominate. For XAMS that is immaterial; it would matter only if the system ever had to integrate with a large controls group.

**The cost, stated honestly.** The broker becomes a single point through which all data flows. If it stops, services keep reading hardware but nothing is stored. Mitigations, all required (§6.4): the broker runs on loopback with automatic restart, and each service buffers its measurements in memory for several minutes and republishes them when the broker returns. A short broker outage then costs nothing; a long one costs a gap that is visible in the data and raised as an alarm, rather than passing unnoticed.

---

## 3. Naming conventions

**Channel names** are the identity of a measurement, used in MQTT topics, JSONL records, the database and the UI. They must be stable forever — renaming breaks history.

**Use the instrument tag names the lab already uses**, lowercased. `TT201` becomes `tt201`. The authoritative list is the P&ID, `notes/xams_piping_and_instrumentation.pdf` (Sarfemijn and Sluitman, 17 May 2024) — the same drawing the web UI renders as a live mimic (§8.2). These are ISA-style tags (`TT` = temperature transmitter, `P` = pressure, `FM` = flow meter) that match the plant numbering, so they carry meaning that an invented scheme would discard. An earlier draft of this document proposed names like `t_cathode_top`; that is superseded.

Rules: lowercase, ASCII, no separator inside a tag, `snake_case` only where a tag has no established form.

```
p101, p102, p103, p104          pressures (9207 voltage)
pmain                           detector pressure
fm101                           flow meter
tt103, tt104                    temperatures, 1xx series
tt201 … tt207                   temperatures, 2xx series (PT1000)
tt301 … tt304                   temperatures, 3xx series
ttamb                           ambient temperature
hv_anode, hv_cathode, hv_gate   HV channels
hv_pmt_top, hv_pmt_bot, hv_ts, hv_bs, hv_nai
ls_sensor_a, ls_sensor_b        Lake Shore inputs
ls_heater_1, ls_heater_2        Lake Shore outputs
ups_*                           UPS status
```

The `legacy:` key in `channels.yaml` records the original casing (`TT201`, `Pmain`) so historical CSV data can still be matched.

**The numbering groups by subsystem**, established 17 September 2026 from the physical locations recorded in `channels.yaml`:

| Series | Subsystem | Channels |
|---|---|---|
| 1xx | **xenon circulation** — the gas loop through the pump | `p101`–`p104`, `fm101`, `tt103` (pump inlet xenon), `tt104` (pump outlet xenon) |
| 2xx | **detector vessel and bucket** — the cold volume | `tt201` (suction tube), `tt202` (bottom of the detector vessel), `tt203`–`tt205` (top of the bucket), `tt206`–`tt207` (bottom of the bucket) |
| 3xx | **cooling and heat exchange** | `tt301`/`tt302` (pump inlet/outlet water), `tt303`/`tt304` (heat exchanger inlet/outlet) |

`pmain` and `ttamb` sit outside the numbering: the detector pressure and the ambient temperature belong to no single subsystem.

Every channel now carries its physical location in `description`. A tag without a location is only half an identity.

**MQTT topics.**

```
xams/meas/<channel>             measurement, JSON payload, retained
xams/status/<service>/heartbeat ISO-8601 UTC timestamp, retained
xams/status/<service>/state     starting | running | degraded | stopped
xams/alarm/<channel>            alarm state changes
xams/cmd/<device>/<action>      control requests (see §10)
xams/ack/<device>/<action>      command acknowledgements
```

Retained messages on `meas` and `status` mean a newly started subscriber immediately sees the current state instead of waiting for the next cycle.

---

## 4. Configuration

### 4.1 `devices.yaml`

```yaml
cdaq:
  chassis: cDAQ1
  serial: "020C5E1C"
  modules:
    - alias: "9207"     model: NI9207   slot: 1   serial: "020DFD57"
    - alias: "9216_1"   model: NI9216   slot: 2   serial: "020F64D1"
    - alias: "9216_2"   model: NI9216   slot: 3   serial: "020F64D0"
    - alias: "9226"     model: NI9226   slot: 4   serial: "02159CBB"

caen:
  # Verified on the lab PC, 17 September 2026. CAEN's own VID/PID, not FTDI;
  # Windows enumerates both as a generic "USB Serial Device" (usbser.sys).
  # Neither unit carries a USB serial number - the instance path
  # (5&35CD27EE&0&5) is a hub socket and changes if the cable is moved.
  # Identity therefore rests entirely on the BDSNUM query (§6.2).
  - id: hv_1
    match: {vid: "21E1", pid: "0003"}
    board_address: 0
    board_name: DT1470ET         # both checked together with the serial
    board_serial: "19198"        # verified via BDSNUM on every connect
    firmware: "1.08"             # informational; logged, alarmed on change
    baud: 9600
    # PMT bottom / PMT top / top screen / bottom screen - all negative
    expect:                      # read and alarmed on; NEVER written (§8.3)
      0: {pol: "-", maxv: 1100, rup: 1,  rdw: 20,  trip: 2.0,  iset: 20}
      1: {pol: "-", maxv: 1100, rup: 20, rdw: 20,  trip: 10.0, iset: 20}
      2: {pol: "-", maxv: 1710, rup: 50, rdw: 50,  trip: 10.0, iset: 5}
      3: {pol: "-", maxv: 2000, rup: 50, rdw: 50,  trip: 10.0, iset: 5}
  - id: hv_2
    match: {vid: "21E1", pid: "0003"}
    board_address: 0             # BOTH units are address 0: two separate USB
    board_name: DT1470ET         # connections, not a daisy chain
    board_serial: "79"
    firmware: "1.04"
    baud: 9600
    # cathode / gate / anode / NaI - anode and NaI positive
    expect:
      0: {pol: "-", maxv: 2500, rup: 25, rdw: 50,  trip: 10.0, iset: 310}
      1: {pol: "-", maxv: 3750, rup: 50, rdw: 100, trip: 10.0, iset: 10}
      2: {pol: "+", maxv: 4500, rup: 50, rdw: 50,  trip: 10.0, iset: 10}
      3: {pol: "+", maxv: 1000, rup: 50, rdw: 50,  trip: 10.0, iset: 150}

lakeshore:
  match: {vid: "1FB9", pid: "0300", serial: "335A12T"}
  idn_contains: "MODEL335"
  baud: 57600                    # with 7 data bits, ODD parity, 1 stop (§7.3)

ups:
  model: "APC Legacy Communication Card"
  match: {vid: "051D", pid: "0002", serial: "3S2005X18782"}
  connection: usb-hid            # read from the HID, alongside PowerChute (§7.4)
```

**The `expect:` blocks are the mechanism behind §8.3 and §10 rule 2.** They record
what each channel was *found* configured to, so the software can read the board's
own `POL`, `MAXV`, `RUP`, `RDW`, `TRIP` and `ISET` at startup and raise an alarm
when they differ. It never writes them: protection stays configured on the
instrument, and the software's job is to notice when it changes, not to restore
it. A value here is therefore an observation with an alarm attached, not a
setting — and `hv_1` channel 0 is flagged in the file itself, because its `RUP`
of 1 V/s and `TRIP` of 2.0 µA look more like history than intent (§16).

The Lake Shore is identified twice over: the USB descriptor carries the serial
`335A12T`, and `*IDN?` answers `LSCI,MODEL335,335A12T/#######,1.2`. The third
field is `<instrument serial>/<option card serial>`; only the part before the
slash identifies the unit, and it matches the descriptor. Both are checked.

Device resolution never uses a COM number. It narrows candidates on USB hardware ID via `serial.tools.list_ports`, then confirms identity by querying the instrument (§6.2). For the CAEN units the second step is not a confirmation of the first — it is the *only* identification, because the hardware ID cannot distinguish the two.

`hv_1` is serial 19198 and `hv_2` is serial 79, confirmed 17 September 2026 against the channel assignment in §7.2: board 79 carries the anode on index 2, which is the supply LabVIEW calls "CAEN 2", channel 3.

### 4.2 `channels.yaml`

One entry per channel. Schema:

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | channel identity (§3) |
| `device` | yes | `cdaq` / `hv_1` / `hv_2` / `lakeshore` / `ups` / `turbo` |
| `phys` | yes | physical address, e.g. `9207/ai5`, or HV channel index |
| `kind` | yes | `voltage` / `current` / `rtd` / `hv_vmon` / `hv_imon` / `temperature` / `status` |
| `unit` | yes | engineering unit string |
| `offset` | no | default 0.0 |
| `multiplier` | no | default 1.0 |
| `rtd` | for `kind: rtd` | `{type, r0, wiring}` |
| `legacy` | no | name in the LabVIEW system |
| `enabled` | no | default true |
| `description` | no | free text: what and where |
| `log_minmax` | no | default false; store interval min/max as well (§9.1) |

Example, using values recovered from the LabVIEW front panel:

```yaml
- name: pmain
  device: cdaq
  phys: 9207/ai5
  kind: voltage
  offset: 0.0
  multiplier: 0.714
  unit: TBD            # front panel shows "Detector Pressure 1.53" without a unit
  legacy: Pmain
  description: detector pressure

- name: p101
  device: cdaq
  phys: 9207/ai0
  kind: voltage
  offset: 1.0
  multiplier: 25.0
  unit: TBD
  legacy: P101

- name: fm101
  device: cdaq
  phys: 9207/ai7
  kind: voltage
  offset: 0.0
  multiplier: 6.0
  unit: g/min          # mass flow. NOT SLPM — see §4.2 note below
  legacy: FM101

- name: tt301
  device: cdaq
  phys: 9216_1/ai0
  kind: rtd
  rtd: {type: PT3851, r0: 100.0, wiring: 3}
  unit: C
  legacy: TT301
  description: TBD — physical location

- name: tt201
  device: cdaq
  phys: 9226/ai0
  kind: rtd
  rtd: {type: PT3750, r0: 1000.0, wiring: 3}
  unit: C
  legacy: TT201
  description: TBD — physical location
```

Unconnected channels are listed explicitly with `enabled: false` rather than omitted, so the channel map is complete and nobody has to wonder later whether a gap is an oversight:

```yaml
- name: spare_9216_2_ai0
  device: cdaq
  phys: 9216_2/ai0
  kind: rtd
  rtd: {type: PT3851, r0: 100.0, wiring: 3}
  unit: C
  enabled: false
  description: not connected
```

**Scaling is defined as `value = (raw - offset) * multiplier`.** This matches the LabVIEW implementation exactly — the front panel labels the two columns "Offsets subtracted" and "Multipliers", in that order. Do not change the convention, or historical comparisons break.

**Units are not recovered, and one of them in the old system is wrong.** The LabVIEW front panel shows the scaling factors but not the engineering units — except for the flow, which it labels "Flow (SLPM)". **That label is incorrect: `fm101` is a mass flow in g/min.** It is recorded here explicitly so that nobody later "corrects" it back by consulting the old front panel.

This is a concrete instance of the risk that milestone 3 exists to catch: the number was right, the name was wrong, and nothing in the system noticed for years.

The pressure units are therefore **TBD** (§16) and must not be guessed. A value of 1.53 is equally plausible in bar or in another unit, and a wrong label propagates into every plot, every alarm threshold and eventually into a paper.

### 4.3 `alarms.yaml`

```yaml
defaults:
  hysteresis: 0.02          # fraction of range, prevents chatter
  min_repeat_minutes: 15    # re-notification interval
  stale_after_seconds: 60

channels:
  pmain:
    high:  {value: 1.8, severity: minor}
    hihi:  {value: 2.0, severity: major, notify: [sms, email, sound]}
    low:   {value: 0.9, severity: minor}

routing:
  sms:   {recipients: enabled}    # everyone enabled in recipients.yaml
  email: {recipients: enabled}
```

Thresholds are **TBD** pending the `Error and Alarm` tab of the LabVIEW front panel.

### 4.4 `recipients.yaml`

Who receives alarm notifications. Separate from `alarms.yaml` because it changes often and for different reasons: people join the group, go on holiday, change numbers.

```yaml
recipients:
  - name: Alice Example
    phone: "+31..."
    email: alice.example@example.org
    alarms: true          # the alarm email and SMS, at whatever hour
    daily: true           # the daily report email, once a morning
  - name: ...
    phone: "+31..."
    alarms: false         # off the alarm list, without losing the number
    daily: true           # still reading the morning report
```

The real file is **gitignored**: it holds colleagues' names, addresses and
mobile numbers. `config/recipients.example.yaml` is the template to copy, and
the test suite uses that template rather than the real list.

**Two lists in one file, split 22 September 2026.** `alarms` is the alarm email and SMS; `daily` is the daily report email (§11). Neither implies the other, because they interrupt people differently: wanting the morning summary is not wanting to be woken at 3am, and the one flag that used to mean both made the quiet one unavailable without the loud one. No shift roster, no escalation chain: each list is the list.

`enabled:` is what the pair used to be. A row carrying only `enabled:` is read as **both** — that is what it meant when it was written, and reading it as neither would take somebody off a list silently, which is the failure this subsystem exists to avoid. The web UI keeps writing it alongside the two, so that an alarm service which has not been restarted since the split goes on notifying exactly who it did before.

**Applied without restarting anything**: the alarm engine re-reads the file at
send time, not at startup, so a change takes effect on the next notification.

**Edited from `/alarms`** — add, remove, or tick *Alarms* and *Notify* — and
saved as one action. Editing the file by hand works equally well and has the same effect.
Built 20 September 2026; it lives on the alarms page rather than a page of its
own, for the reason given in §8.1.

Four rules the page follows, each of which is a way the list can fail quietly:

- **A recipient with neither an email nor a phone is warned about, not
  refused.** They would sit on the list looking notified, and hear nothing —
  so the save says which row, by name. It is not blocked: a blank field is
  somebody mid-edit far more often than it is a mistake, and refusing the
  whole list over one of them means the change nobody could make was the one
  to the list of people who get told things go wrong.
- **An empty phone is not a mistake.** It means *do not SMS this person*; they
  are notified by email alone. Three of the four entries are like this, and a
  page that treated a blank as an omission would nag about a deliberate choice.
- **Nobody on the alarm list is a warning, not a refusal.** It may be exactly
  what somebody means during an intervention. It is said loudly, on the page
  and by the engine, and allowed. An empty *daily* list is a quiet morning
  rather than an unwatched system, and is said once, in grey.
- **Removal is a checkbox applied on save, not a button that deletes on
  click.** The page sits open beside a UI that reloads itself, and a one-click
  irreversible delete next to that is the wrong affordance.

The whole list is saved as one action and validated first: a half-saved list is
a list nobody chose, and this one decides who finds out that something is
wrong.

This is deliberately unlike the alarm thresholds (§4.3), which stay in git and are applied with `xams-ctl reload`. Changing a threshold silently alters what the system protects against and deserves review; changing a recipient does not.

Every change is appended to the same audit log as control actions (§10), so it remains recoverable who was on the list when a given alarm fired.

**An empty alarm list is a warning.** If nobody has `alarms: true`, alarms reach nobody. The alarm engine raises a low-severity alarm on startup and on reload when that is the case, so this is found on a quiet afternoon rather than during an incident.

### 4.5 Config hash

`config.py` computes a SHA-256 over the parsed, normalised contents of `channels.yaml`, `devices.yaml` and `alarms.yaml` (not `recipients.yaml`, which does not affect the data) and exposes the first 7 hex characters as `config_hash`. It is written into every JSONL daily file header (§9.3) and published on `xams/status/config`.

### 4.6 `hv_defaults.yaml` — the HV operating point

**Built 20 September 2026.** The values `load defaults` offers on `/hv`, and
the one file in git this web UI writes.

```yaml
# Written by the web UI. Safe to edit by hand.
updated: 2026-09-20T14:02:11+00:00
by: apc
defaults:
  hv_cathode_vset: -2250.0
  hv_anode_vset:   4200.0
  ...
```

**Why it is a separate file, and not `channels.yaml`.** §10a left this open —
"`channels.yaml` if there is one right answer per channel; a separate file per
run configuration if there are several". This is an R&D setup, so there is not
one right answer: the operating point moves while the things around it do not.
Splitting them puts the number that changes weekly in a file a machine can
rewrite, and leaves the channel's identity, its sign and its `limits` in a file
that is hand-edited and reviewed. It also keeps the thirty lines of commentary
above the `hv_vset` entries, which a YAML round-trip would silently discard.

A channel not named here keeps the `default_setpoint` from `channels.yaml`, so
the file is an override and a fresh clone needs none.

**What it may not do, and this is the whole of it: `limits` are not editable
from the web UI.** They are the range the write path of §10a validates against.
A page that could widen its own limit and then write to it is not a guardrail —
it is a guardrail-shaped thing that moves when pushed. A default outside its
limits is refused, by the page before writing and by `load()` on every read,
and the refusal says to go and edit `channels.yaml` if that is really intended.

**Nothing here reaches an instrument.** A default is a number that appears in a
box; a person still presses Apply, and that write is validated, read back and
audited exactly as before. That is what makes this safe to edit from a web page
when §4.3's alarm thresholds are not: a wrong threshold silently removes
protection, while a wrong default is visible in a box before anything happens
to it, and refused outright if it is out of range.

**Excluded from the config hash** (§4.5), like `recipients.yaml`: it does not
affect how a reading is taken or what a stored value means.

**Every change is audited** (§10 rule 5) — who, when, old value, new value — on
the same trail as every write to an instrument. `git status` shows the file as
modified, and it is committed with everything else; the UI does not run `git`
itself, because a lab PC committing unattended turns a setpoint edit into a git
error on a dirty tree.

Saving reloads the configuration in this process before reporting success, and
publishes `xams/cmd/all/reload` so the other services follow. Reporting "saved"
for something not yet in force is the failure §7.5 names.

---

## 5. Data model

```python
@dataclass(frozen=True)
class Measurement:
    t: datetime          # timezone-aware, UTC
    channel: str
    value: float         # engineering units
    unit: str
    raw: float | None = None
    quality: Quality = Quality.OK

class Quality(Enum):
    OK = "ok"
    STALE = "stale"          # not refreshed within stale_after_seconds
    ERROR = "error"          # read failed
    UNVERIFIED = "unverified"  # device identity not confirmed
```

MQTT payload on `xams/meas/<channel>`:

```json
{"t":"2026-09-16T13:02:00.000Z","ch":"pmain","v":1.530,"raw":2.143,"u":"bar","q":"ok"}
```

Timestamps are UTC, ISO-8601, millisecond precision, taken **at the moment of the hardware read**, not at publish time.

`raw` is written for the first year of operation at minimum (§9.4).

---

## 6. Service contract

Every device service inherits `BaseService` and must satisfy all of the following. This is the specification; deviations need a comment explaining why.

### 6.1 Lifecycle

```
resolve device  →  verify identity  →  configure  →  loop { read, publish, heartbeat }
                          ↓ fail
                   log FATAL, publish state=stopped, exit non-zero
```

- **Startup is read-only.** No setpoint is written, no channel enabled, no state restored. A restart must be invisible to the hardware.
- **A busy device is a clean failure**, not a stack trace:
  `FATAL: cDAQ1 in use by another process (LabVIEW running?). Refusing to start.`
- **One instance only.** A named mutex (Windows) or lock file prevents a second copy.
- **Reconnect loop.** On a read failure: publish `quality=error`, back off (1 s, 2 s, 4 s … capped at 30 s), retry indefinitely. The service never exits because a device disappeared; it exits only when it cannot be identified at startup.

**Liveness is judged per device, never per service.** One service may hold more than one instrument — `caen.py` holds both supplies — and they fail independently. A check of the form "every channel in this service failed" cannot see one of two instruments die, because the healthy one keeps the condition false.

This was not hypothetical. On 17 September 2026 the USB was pulled from one CAEN supply and replugged; it never came back, and sat publishing `quality=error` until the service was restarted by hand. Its neighbour was answering perfectly, so the service-wide check never fired, the reconnect path was never reached, and **nothing appeared in the log** — a failed command is logged at debug.

Three rules follow, and `test_caen_link.py` holds each of them down:

1. **Count failures per device.** Two consecutive cycles in which a given instrument answers nothing is a lost link, whatever its neighbours are doing.
2. **A relink must be forgiving where startup is strict.** Startup refuses to run rather than guess (above), and clears every reader before it raises. Reusing that logic for recovery would drop a healthy instrument the moment its neighbour went missing. Each device is re-resolved on its own.
3. **Never discard good readings to signal a bad link.** The reconnect path is reached by raising, and a raise throws away everything read in that cycle. If one instrument is unplugged for a week, raising would stop the other being published for a week. A partial failure is handled inside the driver and the good data still goes out.

A relink is a **full re-resolution**, never a bare reopen: the handle held before the unplug is dead, a replugged unit can return on a different COM number, and the cables may have been swapped while the link was down (§6.2 rule 6). It is rate-limited to one attempt per 10 s, because enumerating and probing takes a second or two during which the healthy instrument is not being read.

### 6.2 Identity verification

Applies to every serial device, and is mandatory before any write.

A device is identified by **asking it who it is**, never by which COM port it happens to occupy.

1. Enumerate ports with `serial.tools.list_ports`, narrowing by VID/PID and — where available — USB serial number.
2. Open each candidate and query the instrument for its own identity:
   - CAEN: `$BD:<addr>,CMD:MON,PAR:BDSNUM` and `PAR:BDNAME`
   - Lake Shore: `*IDN?`
3. Compare against `devices.yaml`.

**Six rules govern step 2.** They matter because for the CAEN units this query is not a cross-check on the hardware ID — it is the entire identification (§4.1).

1. **Never probe a port that did not pass step 1.** Only ports already matching the configured VID/PID are opened. Writing bytes at an unknown serial device is not a neutral act: this machine also exposes `COM3` as Intel AMT Serial-over-LAN, and a stray probe there is at best meaningless and at worst confusing to something else.
2. **Match on `BDNAME` *and* `BDSNUM` together.** One of the two units has serial `79`, low enough that a collision with some other CAEN model is not fanciful. The pair `(DT1470ET, 79)` is unambiguous; `79` alone is merely probably unambiguous, and "probably" is not an identity.
3. **A malformed, truncated or absent reply means unidentified.** Never "probably the right one", never a retry that silently accepts the second answer. The reply is parsed strictly, and a parse failure is the same outcome as a wrong serial: refuse.
4. **Probe every candidate before binding any.** Resolution is a two-pass operation — read all identities first, then assign. This is what makes a cable swap harmless.
5. **Two ports reporting the same identity is a fatal error.** It means either a duplicate serial or a bug, and there is no safe way to guess which unit is which. Refuse to start and say so.
6. **Re-verify on every reconnect, not only at startup.** A service that has been running for months and reconnects after a USB glitch must re-confirm it is still talking to the same board. A reconnect is where a swap would otherwise slip through unnoticed.

**On/off comes from the board's STAT word, never from VMON.** Each channel has a physical enable, and a channel can be enabled while sitting at zero volts. Read from the hardware on 17 September 2026:

```
hv_1 ch1 — STAT=1 (bit 0, ON), VSET=0.0, VMON=0.0
every other channel — STAT=1024 (bit 10, DISABLED)
```

The web UI first inferred the state from `VMON > 1` and so showed that channel as **off**. It is not off: it is switched on, at zero, and one setpoint away from putting volts on a photomultiplier. A display that understates what is live is the kind of wrong that gets somebody hurt, so the word is read per channel and stored as `hv_*_stat`.

The page distinguishes three states: **ON** (putting volts out), **enabled** (switched on, at zero) and **off** (output disabled). A trip, interlock or over-current replaces all three. Where the word cannot be read the page says **no reading** rather than falling back to the voltage — the fallback *is* the bug.

The whole bitmask is stored rather than a decoded flag, so `TRIP`, `INTERLOCK`, `OVER_CURRENT` and `OVER_TEMP` come along on the same wire at no extra cost. Alarming on them later (§16) then needs no new channel, and so leaves no gap in their history.

**Why `BDSNUM` is trustworthy as an identity.** It is set at the factory in the board's non-volatile configuration, and the ASCII protocol has no `CMD:SET,PAR:BDSNUM` — the board cannot be talked into changing its own name, by this software or any other. The query is `CMD:MON`, so it is read-only and safe to issue on every connect, including before identity is established and therefore while all writes are still refused. The thing that makes it safe is that it is the board's own answer about itself: every failure mode that breaks COM-number matching leaves it untouched.

**What it does not survive, by design: a replaced board.** A unit returned under warranty comes back with a different serial, and the service will then refuse to start. That is the intended behaviour, not a gap — replacing an instrument should require a human to edit `devices.yaml` and notice that the history before and after that date came from different hardware.

A COM port renumbered by Windows, a device moved to another hub socket, or the two supplies' cables swapped are therefore all harmless: the service finds each unit wherever it is. This is not a hypothetical robustness: the CAEN units expose **no** USB serial number, so step 1 cannot tell them apart at all, and only step 2 — asking the instrument itself — distinguishes them.

This replaces a real weakness of the present system, where COM8 and COM5 are fixed choices on the LabVIEW front panel. If Windows renumbers them, someone must notice and correct it by hand — and if the two supplies exchange numbers, LabVIEW will talk to the wrong one without any error, since both are valid CAEN units giving well-formed replies.

**Behaviour on the three outcomes:**

| `board_serial` in `devices.yaml` | Behaviour |
|---|---|
| `TBD` (not yet known) | starts **read-only**; publishes with `quality=unverified`; **every write refused** |
| set, and it matches | normal operation; writes permitted |
| set, and it does not match | refuse to start, raise an alarm, do not guess |
| set, but the reply is malformed or absent | treated exactly as a mismatch — refuse |
| set, and two ports return it | fatal; refuse to start (rule 5 above) |

**Both CAEN serials are now known** (`19198`, `79`, recorded in §4.1), so the supplies start in the second row rather than the first: verified, and writes permitted once the control path exists (§10). The Lake Shore reports `335A12T` in its USB descriptor *and* answers `*IDN?`, so it is verified on both counts.

The first row exists so the configuration can bootstrap itself. The serial numbers are not needed in order to communicate — they are needed in order to *write safely*. Connect the service, let it report what it found, record that in `devices.yaml`, and the channel moves from `unverified` to `ok`. Monitoring (milestone 5) can therefore run before the serials are known, with the data visibly marked as unverified rather than silently trusted.

### 6.3 Heartbeat and staleness

- Publish `xams/status/<service>/heartbeat` every cycle.
- A separate monitor raises an alarm when a heartbeat exceeds `stale_after_seconds`.
- A channel not refreshed within the same window is republished with `quality=stale`. **Never leave a stale value standing as if fresh.**

---

### 6.4 Broker outage

The MQTT broker is a shared dependency (§2.1). Every service therefore:

- buffers measurements in a bounded in-memory queue (default 5 minutes' worth) while the broker is unreachable;
- republishes the buffer, in order, once the broker returns;
- raises an alarm if the buffer overflows, and records the gap explicitly rather than letting it pass silently;
- **keeps reading the hardware throughout.** A broker outage must never stop acquisition.

The broker itself runs on loopback under NSSM with automatic restart, so the common case is an outage of a few seconds.

---

## 7. Device specifications

### 7.1 cDAQ service (`devices/cdaq.py`)

Read-only. The chassis has no output module, so this service has no control path.

**Tasks.** All four modules are low-rate delta-sigma with differing aggregate rates and cannot share one hardware-timed task. Slow control reads at 1 Hz (§9.1), so use **on-demand (software-timed) reads**, one task per module, polled in sequence. The CompactDAQ timing-engine constraint then does not apply.

| Task | Module alias | Channels | DAQmx call |
|---|---|---|---|
| 1 | `9207` | `ai0:7` voltage | `add_ai_voltage_chan` |
| 1b | `9207` | `ai8:9` current | `add_ai_current_chan` — strain gauges, added 22 September 2026 |
| 1c | `9207` | `ai10:15` current | **not used — no task line created** |
| 2 | `9216_1` | `ai0:6` | `add_ai_rtd_chan` |
| 3 | `9216_2` | — | **entirely unconnected; task not created** |
| 4 | `9226` | `ai0:6` | `add_ai_rtd_chan` |

**NI 9207 voltage channels** (recovered from the LabVIEW front panel). Applied value is `(raw - offset) * multiplier`:

| Channel | Tag | Offset | Multiplier | Role |
|---|---|---|---|---|
| `9207/ai0` | `p101` | 1 | 25 | |
| `9207/ai1` | `p102` | 0 | 1 | |
| `9207/ai2` | `p103` | 0 | 1 | |
| `9207/ai3` | `p104` | 0 | 1 | |
| `9207/ai4` | `v4` | 0 | 1 | generic name — likely unused |
| `9207/ai5` | `pmain` | 0 | 0.714 | **detector pressure** |
| `9207/ai6` | `fm101` | 0 | 6 | **flow meter** (moved from ai7, 13 October 2026) |
| `9207/ai7` | `p105_v` | 0 | 1 | **PKR vacuum gauge signal**, floating (off the AI-/COM jumper chain); `p105` [mbar] derived via `pkr_pressure` |

**NI 9207 current channels.** The current half was unused until 22 September 2026, when two 4-20 mA strain gauge load cells were wired to `ai8` and `ai9`. Their LabVIEW names `i0`–`i7` with offset 0 and multiplier 1 throughout were what settled that the half was idle; it is idle no longer.

| Channel | Tag | Offset | Multiplier | Role |
|---|---|---|---|---|
| `9207/ai8` | `sg101` | 0.004 | 1250 | **strain gauge, 0-20 kg** |
| `9207/ai9` | `sg102` | 0.004 | 1250 | **strain gauge, 0-20 kg** |
| `9207/ai10:15` | — | — | — | not connected; `enabled: false` |

**DAQmx returns amps, not milliamps**, so the offset is `0.004` and the multiplier is `20 kg / 0.016 A = 1250 kg/A`. A multiplier written for milliamps is out by a factor of a thousand and still looks like a plausible number, which is why `tests/test_cdaq.py` pins both endpoints.

**The scaling is not yet confirmed against the sensors.** The span and the full scale reached this software as "2x 4-20 mA strain gauge, 0-20 kg"; no datasheet has been read and no loaded reading taken. What that does not settle is the direction — a reverse-acting transmitter sends 20 mA at zero and 4 mA at full scale, over the same wires, and every weight would read as its complement. Confirm with the loop powered: an empty cell reads about 4 mA, and a known mass moves it **up**. If it moves down, the transmitter is reverse-acting and the constants become offset `0.020`, multiplier `-1250`.

**A reading below 4 mA is not a light load.** A 4-20 mA transmitter sends 4 mA at zero, so anything under it means an open loop, an unpowered transmitter, or no sensor. The driver publishes `quality=error` for it rather than a negative weight — the same distinction as `RTD_VALID_C` (`CURRENT_VALID_A`, 3.5-21 mA, deliberately a little wider than the loop so an endpoint reading is not called a fault). **The loops need external 24 V**: the 9207 measures current but does not source loop power, so a channel pinned at 0 mA with the sensor connected is the supply, not the sensor.

The unconnected inputs `ai10:15` are listed in `channels.yaml` with `enabled: false`, as with the unconnected RTD inputs, so the channel map stays complete. No task line is created for them.

That leaves the 9207 carrying **8 connected channels of 16** — 6 voltage and 2 current — and the chassis as a whole 13 RTDs, 6 voltages and 2 currents.

**RTD channel map** (from the lab, September 2026):

| Module | Type | Channel | Tag | Notes |
|---|---|---|---|---|
| `9226` | PT1000 | `ai0` … `ai6` | `tt201` … `tt207` | seven consecutive |
| `9226` | PT1000 | `ai7` | — | not connected |
| `9216_1` | PT100 | `ai0` | `tt301` | |
| `9216_1` | PT100 | `ai1` | `tt302` | |
| `9216_1` | PT100 | `ai2` | `tt103` | |
| `9216_1` | PT100 | `ai3` | `tt104` | |
| `9216_1` | PT100 | `ai4` | `ttamb` | ambient |
| `9216_1` | PT100 | `ai5` | `tt303` | |
| `9216_1` | PT100 | `ai6` | `tt304` | |
| `9216_1` | PT100 | `ai7` | — | not connected |
| `9216_2` | PT100 | `ai0` … `ai7` | — | **module entirely unconnected** |

**An out-of-range RTD reading is not a measurement.** Platinum RTDs to IEC 60751 are defined from −200 to +850 °C. Outside that the module is reporting an open circuit, a short or a missing sensor, and the driver publishes `quality=error` with no value rather than a number.

This is deliberately **not** an alarm threshold and does not belong in `alarms.yaml`: it is the difference between a measurement and the absence of one, and it must not be configurable per installation. Note also that the bound is the *sensor standard*, not an expectation about the experiment — the cryostat sensors legitimately read −90 °C, and narrowing the range to something "reasonable" would discard real data.

**`tt202` (`9226/ai1`) — the sensor has failed.** Found by the open-circuit check on 17 September 2026 and confirmed in the lab the same day. It reads identically to the known-unconnected `9226/ai7`: both swing between roughly −245 and +1327 °C across consecutive reads, where every working sensor is stable to better than 0.5 °C. Its location is the bottom of the detector vessel.

It is marked `enabled: false` rather than left running. The reasoning is worth stating, because the opposite choice is defensible: left enabled it would publish `quality=error` every cycle and, from milestone 6, hold a permanently active alarm — and **an alarm that is always on is one nobody reads.** The failure is tracked as an open item in §16 instead, which is where it stays visible without training anyone to ignore a red indicator. When the sensor is replaced, `enabled: true` is the only change needed.

So **thirteen RTDs are live, not fourteen**, and the cDAQ carries **19 channels, not 20**.

**Fourteen RTDs are in use, not twenty-four.** The whole of `9216_2` is free, which means eight spare PT100 inputs are already wired into the chassis — room for expansion without buying hardware. Do not create a DAQmx task for it; list its channels in `channels.yaml` with `enabled: false` so the map stays complete.

**RTD configuration** (confirmed from the LabVIEW front panel):

| Module | `r_0` | `rtd_type` | `resistance_config` |
|---|---|---|---|
| NI 9216 (both) | 100.0 | `RTDType.PT_3851` | `THREE_WIRE` |
| NI 9226 | 1000.0 | `RTDType.PT_3750` | `THREE_WIRE` |

No custom Callendar–Van Dusen coefficients are in use. Let DAQmx return °C directly; do not hand-roll the conversion.

**Excitation current must be set explicitly — correction, 17 September 2026.** Each module accepts exactly one value, and neither is the DAQmx default of 2.5 mA, so a task that omits `current_excit_val` **fails to configure**:

| Module | Sensor | Accepted | Refused |
|---|---|---|---|
| NI 9226 | PT1000 | **100 µA** | 1 mA, 2.5 mA |
| NI 9216 | PT100 | **1 mA** | 100 µA, 2.5 mA |

The physics agrees: 1000 Ω at 1 mA would dissipate a milliwatt in the sensor and self-heat it, which is why the PT1000 module runs at a tenth of the current. The example below is corrected accordingly; an earlier draft omitted the argument and would not have run.

```python
task.ai_channels.add_ai_rtd_chan(
    "9216_1/ai0:6",
    rtd_type=RTDType.PT_3851,
    resistance_config=ResistanceConfiguration.THREE_WIRE,
    current_excit_source=ExcitationSource.INTERNAL,
    current_excit_val=1e-3,          # REQUIRED: 100 uA for the 9226 (PT1000)
    r_0=100.0,
    units=TemperatureUnits.DEG_C,
)
```

Modules are addressed by **alias** (`9207/ai0`), not `cDAQ1Mod1/ai0`. Aliases are configured in NI-MAX and survive re-slotting.

Timing mode: High Speed, as currently configured. 50 Hz rejection comes from the module's ADC mode, not from software averaging.

For reference, the settings the LabVIEW system uses: a 2 s cycle, a moving average over 2 samples on the flow and over 20 samples on the detector pressure, and an integrated-flow calculation. The new system reads at 1 Hz and logs 10 s means (§9.1), so these are not carried over directly, but they indicate the smoothing the operators are used to seeing.

### 7.2 CAEN service (`devices/caen.py`)

Two instances, one per supply. ASCII protocol over the USB virtual COM port; no vendor library.

Both units answer at **board address 0**: they are two independent USB connections, not a daisy chain, and neither responds at address 1 or 2. `<addr>` is therefore 0 in every command to either supply, and the supplies are told apart by which port their `BDSNUM` came back on (§6.2), never by address.

The two run **different firmware** — 1.08 on serial 19198, 1.04 on serial 79. No protocol difference has been observed between them, but `test_caen_protocol.py` carries recorded responses from **both** units, so a divergence shows up in the tests rather than in the lab.

```
$BD:<addr>,CMD:MON,PAR:VMON,CH:<n>        read measured voltage
$BD:<addr>,CMD:MON,PAR:IMON,CH:<n>        read measured current
$BD:<addr>,CMD:MON,PAR:STAT,CH:<n>        channel status word
$BD:<addr>,CMD:SET,PAR:VSET,CH:<n>,VAL:<v>   set voltage   (control path, §10)
```

Terminate with `\r\n`. Parse the response; a malformed or absent reply is a read error, never a silently substituted value.

**Channel map**, from the lab, 17 September 2026. Channel indices are 0-based as the protocol uses them; the LabVIEW front panel numbers the same channels 1–4.

| Board | `id` | CH | Channel name | Polarity | `VSET` | `ISET` µA | `MAXV` | `RUP` | `RDW` | `TRIP` |
|---|---|---|---|---|---|---|---|---|---|---|
| 19198 | `hv_1` | 0 | `hv_pmt_bot` | − | 700.0 | 20 | 1100 | 1 | 20 | 2.0 |
| 19198 | `hv_1` | 1 | `hv_pmt_top` | − | 1000.0 | 20 | 1100 | 20 | 20 | 10.0 |
| 19198 | `hv_1` | 2 | `hv_ts` (top screen) | − | 500.0 | 5 | 1710 | 50 | 50 | 10.0 |
| 19198 | `hv_1` | 3 | `hv_bs` (bottom screen) | − | 600.0 | 5 | 2000 | 50 | 50 | 10.0 |
| 79 | `hv_2` | 0 | `hv_cathode` | − | 2250.0 | 310 | 2500 | 25 | 50 | 10.0 |
| 79 | `hv_2` | 1 | `hv_gate` | − | 1750.0 | 10 | 3750 | 50 | 100 | 10.0 |
| 79 | `hv_2` | 2 | `hv_anode` | **+** | 4200.0 | 10 | 4500 | 50 | 50 | 10.0 |
| 79 | `hv_2` | 3 | `hv_nai` (external NaI) | **+** | 600.0 | 150 | 1000 | 50 | 50 | 10.0 |

The names come from the lab; the electrical values were read from the boards themselves. **The two agree independently**: all four channels named as negative on `hv_1` report `POL:-`, and exactly the two named as positive on `hv_2` report `POL:+`. That cross-check is why this table is recorded as confirmed rather than as a transcription.

The `VSET` column is the value each channel was left at, not a commissioning setpoint — all eight channels read `VMON` 0.0 and `STAT` 1024 (disabled) when this was taken. Treat it as the expected operating point to be confirmed, not as a target the software may drive to.

Two asymmetries worth a question before milestone 8, since both look more like history than intent: `hv_pmt_bot` ramps at 1 V/s where every other channel is 20–50, and trips at 2.0 µA where every other channel is 10.0.

### Sign convention — decide before any HV value is stored

The supplies report `VMON` and `VSET` as **unsigned magnitudes**, with polarity as a separate `POL` parameter. A cathode at minus 2250 volts answers `2250.0`. Software must therefore apply the sign itself, and this is almost certainly what `DAISY_polarity_signs.vi` exists to do — the name and the hardware behaviour fit exactly.

**Recommendation: store signed values.** `hv_cathode` is written as `-2250.0`, `hv_anode` as `+4200.0`. The alternative — storing magnitudes and carrying polarity as metadata — makes a plot of the cathode climb upwards as the voltage becomes more negative, and puts two channels of opposite sign on the same axis with no visible difference. This system already carries one instance of a value that was numerically right and semantically wrong for years (§4.2, the flow in "SLPM"), and an unsigned cathode voltage is the same failure waiting to happen.

The cost is that `VSET` writes must be signed consistently too, and the sign stripped before the value goes back on the wire. That belongs in one place in `caen.py`, tested both directions.

**TBD** — confirm the convention, then state it in `channels.yaml` and never revisit it: changing it later silently inverts history. The polarity read from each channel is also compared against the expected polarity in `devices.yaml` at startup, and a mismatch is an alarm, not a correction (§8.3).

`anode_timing.vi` remains **TBD** — determine whether its behaviour must be reproduced before the control path is built (§16).

### 7.3 Lake Shore service (`devices/lakeshore.py`)

Official `lakeshore` package. COM port resolved by hardware ID, identity confirmed with `*IDN?` (§4.1).

**Serial settings are 57600 baud, 7 data bits, ODD parity, 1 stop bit.** 7-O-1 is
the 335's factory setting and is not a typo. At 8-N-1 the port opens and the
instrument returns nothing intelligible, which presents as a dead instrument
rather than as a wrong setting — so it is recorded here, not rediscovered.

**Sensors are read in Celsius, with `CRDG?`.** An earlier draft of
`channels.yaml` said Kelvin; the imported history then showed these channels
ranging to −90, and there is no negative Kelvin. Confirmed against the
instrument: `CRDG? A` = −89.998 and `KRDG? A` = +183.15 describe the same
temperature.

Read: two sensor inputs, two heater outputs, setpoint, heater range, PID. The
two outputs are configured differently, and both are read — settings as found on
17 September 2026:

| Output | Setpoint | Range | PID |
|---|---|---|---|
| 1 | −90.000 | 3 | 100, 20, 0 |
| 2 | −100.00 | 0 | 50, 20, 120 |

These are displayed and alarmed on, never written (§8.3) — with the single
exception of the setpoint and heater range, which are the control path below.

Control path (§10): **built, 18 September 2026.** Setpoint and heater range are
writable from `/` and from `xams-ctl`, validated, read back, acknowledged and
audited. The instrument's own setpoint limit and heater range remain the
authority.

### 7.4 UPS service (`devices/ups.py`)

An APC, serial `3S2005X18782`, reporting itself as *Legacy Communication Card
FW:LCC 03.1 / ID=5004*. Read-only status: line power present, battery level,
time on battery, timestamp. Publishes `ups_*` channels.

A power event is one of the few things that can end a run, so this is worth
having even though it is the smallest service.

**Read directly from the USB HID, alongside PowerChute.** HID input is shareable
on Windows, so PowerChute Personal Edition keeps doing its safe-shutdown job and
this service reads the same feature reports. Nothing is taken away from the
thing that already protects the machine.

**Two routes that do not work here, recorded so nobody retries them:**

- `Win32_Battery` and the other WMI battery classes report **no instances at
  all** on this machine.
- `GetSystemPowerStatus` answers, but says `BatteryFlag=128` — "no system
  battery". Its `ACLineStatus` describes the **wall socket, not the UPS**, so it
  would cheerfully read "on line power" while the UPS ran on battery. That is
  the worst kind of wrong: a plausible answer to a question it is not being
  asked, and precisely the failure this service exists to catch.

---

### 7.4a Turbo pump service (`devices/turbo.py`)

Pfeiffer HiPace 80 (TC 110) in a HiCube 80 Eco, added 8 October 2026. Read by
**listening** to the RS-485 link between the pump and the station's DCU, via a
Y-piece and an EXSYS EX-13009 (FTDI) adapter. **It never transmits**: the DCU
is the bus master, and a telegram from this PC would collide with its polls or
be obeyed as a command. The port wrapper has no write method.

Consequences: only parameters the DCU polls can be read (the status set
always; the speed while the DCU display or service line, parameter 795, shows
309), and identity is the adapter's USB serial plus valid replies from the
pump address rather than an `*IDN?` (§6.2). A parameter the DCU stops polling
is published as `quality=error`. The vacuum pressure is not on this bus. See
[the driver page](drivers/turbo.md).

### 7.5 Derived channels — the flow integrator

The present system integrates the flow and offers a reset button (`Average_flow_x_time.vi`). The feature is carried over, as `devices/derived.py`: a bus consumer that subscribes to `fm101` and publishes `fm101_total`, stored and plotted like any other channel.

**This is the only stateful component in the system.** Everything else is restartable without consequence; an integrator is not, which raises three requirements that must not be skipped.

**It survives a restart.** After every publish the accumulator is written to disk together with the timestamp of the last sample processed, and read back on startup. A Windows update that reset the total to zero would make the feature useless.

**Gaps are recorded, never invented.** If the service was down for two hours, what flowed during those two hours is unknown. Extrapolating the last value is tempting and wrong. Such intervals are excluded from the total and counted separately:

```json
{"ch":"fm101_total","v":4213.8,"u":"g","q":"ok","gaps_s":7200}
```

The total then carries the evidence that it is an underestimate, instead of that having to be reconstructed months later. Samples whose `quality` is not `ok` are likewise not integrated.

**Reset closes a period; it does not erase.** Instead of zeroing a counter, the running period is closed and a new one opened, in a `flow_periods` table:

| start | stop | total_g | gaps_s | reset_by |
|---|---|---|---|---|
| 2026-08-01 09:14 | 2026-09-16 11:02 | 4213.8 | 0 | apc |
| 2026-09-16 11:02 | — | 118.4 | 0 | — |

This keeps the history of how much passed through during each period, rather than a number somebody once discarded. A reset is a control action and goes through the audit log (§10).

**Two ways to ask, one thing that does it.** Either `xams-ctl flow-reset --by <you>` or the button on `/`. Both publish to `xams/cmd/derived/flow_reset` and wait for the derived service to acknowledge; neither touches the accumulator itself. The integrator owns its own state, and something that reached around it could not be audited and would race the service still accumulating into it (§2.1).

**A reset nobody acknowledged is reported as a failure.** If the derived service is down, the period was *not* closed, and both callers say so rather than returning quietly. Reporting success for work that did not happen is the failure mode that `xams-ctl reload` shipped with and somebody had to catch in the lab.

**The attribution is taken on trust.** There is no login on the web UI, so `reset_by` is whatever was typed into the box — "apc", or `webui (unnamed)` if it was left empty. That is weak, and it is recorded honestly rather than dressed up: a name taken on trust is still better than an anonymous change, and it matches what `--by` already does. Real attribution needs authentication, which arrives with §10 if it arrives at all.

**Units.** `fm101` is a **mass flow in g/min** — *per minute*. The integral is `Σ (flow × dt)` with `dt` in **minutes**, giving a mass in **grams**; `fm101_total` therefore has unit `g`. Grafana may display kilograms, but the stored value is grams.

Two traps, both covered explicitly in `test_scaling.py`: a `dt` in seconds produces a factor-60 error that looks entirely plausible, and the LabVIEW front panel labels this channel "Flow (SLPM)", which is wrong — it is not a volumetric standard-litre flow.

---

## 8. Interfaces and access

**Hard requirement: the system is reachable from the lab PC only.** Every listening service binds to the loopback address. This matches how the LabVIEW system works today and is a deliberate constraint, not an oversight to be relaxed later without discussion.

```python
uvicorn.run(app, host="127.0.0.1", port=8000)   # NOT 0.0.0.0
```

`0.0.0.0` is the default in most tutorials and in much example code. On this system it would expose HV control to the building network. Every bind address is therefore explicit in configuration, and `127.0.0.1` is the only accepted value without a recorded decision to the contrary.

The same applies to everything else that opens a port:

| Service | Bind | Note |
|---|---|---|
| FastAPI web UI | `127.0.0.1:8000` | control surface |
| Mosquitto | `127.0.0.1:1883` | **see below** |
| PostgreSQL | `127.0.0.1:5432` | |
| Grafana | `127.0.0.1:3000` | |

**Mosquitto deserves particular care.** Commands travel over MQTT (`xams/cmd/#`), so anything that can reach the broker can set a high voltage. A broker listening on all interfaces is equivalent to an unauthenticated control interface on the network. Bind to loopback, and disable anonymous access on any listener that is ever added.

If remote viewing is wanted later, the answer is an SSH tunnel or a read-only mirror of the data — never opening the control port. Adding remote access is a decision with a security consequence, and should be recorded as such.

**Recorded, 23 September 2026:** remote viewing is the read-only mirror on the Nikhef VM `plotit-xams` (§12). The lab PC opens nothing; it connects out, to one port, over TLS, as a role that can only insert. Operating it: `docs/vm.md`.

### What loopback does not protect against

**The bind address is a network boundary, not a browser one.** Everything above keeps the building network out. It does nothing about the browser already running on the lab PC, and for a while this section read as though it did — which is the more dangerous kind of mistake, because it stops anyone from looking further.

The attack takes no skill and no access to this machine. An operator opens any page in any tab; that page contains a hidden form pointed at `http://127.0.0.1:8000/hv/output`, and submits it. A plain form POST needs no CORS preflight, so the browser sends it, cookies and all, and the request arrives indistinguishable from one made on the page itself. There is no login to fail. The command runs. The attacker never sees the response — and does not need to, because the damage is the write, not the reply:

* `/alarms/notify` with `enabled=0` — the plant is no longer watched by anyone, while every page still says the alarms are simply off
* `/hv/output` with `on=0` — a channel de-energised in the middle of a measurement
* `/hv/apply`, `/lakeshore/setpoint`, `/lakeshore/range` — a setpoint moved within the limits the service enforces, which is a legal value and therefore not refused anywhere
* `/alarms/recipients` — the list of who is told rewritten
* and the `by` field travels with the request, so the audit trail records a name the attacker chose

The same gap reads as well as writes. Nothing checked the `Host` header, so a domain the attacker controls could be re-pointed at `127.0.0.1` (DNS rebinding); the browser then treats this site as same-origin and hands over `/api/state` and the Alarms page — colleagues' names, e-mail addresses and mobile numbers, the data §11 keeps out of git.

**The fix, as of 21 September 2026:** one middleware in `api/app.py`, two checks, against the two attacks:

* the `Host` header, on **every** request, against rebinding — matched on the NAME, never the port, so an SSH tunnel forwarded to any local port still works. Rebinding puts the attacker's own name in that header, so the port never contributed to the protection; refusing it only broke the one route that exists for reaching this machine from outside.
* the `Origin` — falling back to the `Referer` where a browser sends none — on `POST`, `PUT`, `PATCH` and `DELETE`, against the cross-site form

No token and no session. A per-form CSRF token is the stricter answer, and it was not taken: it means a hidden field in ten templates and a secret to manage, for a UI with no login on a machine one person at a time uses. Two headers a browser sets by itself, checked in one place a student can read, is the §8.1 trade.

Three consequences worth knowing before something looks broken:

1. **The allowed hosts come from the bind arguments**, not a hardcoded `8000`. `--port 8080` would otherwise start a UI that refused every request to itself, with nothing on screen explaining why.
2. **`curl` posting to this port is refused**, and that is intended rather than tolerated: the CLI talks MQTT, and nothing in this repository posts HTTP here. A script that needs to change something uses `xams-ctl`.
3. **A refusal is logged at `WARNING`** and therefore appears on the Logs page under `webui`. If a browser ever withholds both headers on a same-origin form, the operator sees a control that does nothing, and that line is the only explanation anywhere.

The rule this leaves behind: **loopback answers "who can reach the port", never "who asked".** Any future surface on this machine has to answer the second question for itself.

### 8.1 The four surfaces

Separated by how often they are touched and how much a mistake costs.

**Grafana — monitoring.** Plots, history, dashboards, trends. Read-only by construction, and where most of the day is spent. It **displays** alarm state and alarm history, but it does not evaluate alarms or send notifications — that is `alarms/engine.py` (§11). No control, ever: that separation is the point.

**Web UI — the landing page.** `http://localhost:8000/` is the page to bookmark and open every morning. It answers "is everything all right?" without a click, and only then offers links.

```
┌────────────────────────────────────────────────┐
│  XAMS Slow Control              ● ALL OK       │
│  config a3f91c2 · v1.4.0 · up 6d 4h            │
├────────────────────────────────────────────────┤
│  Services          cdaq ● 2s   caen ● 3s       │
│                    lakeshore ● 2s   ups ● 11s  │
│  Sinks             postgres ● 1s   jsonl ● 1s  │
│                    nikhef-vm ⚠ 4m behind       │
│  Alarms            none active                 │
│  Disk              C: 812 GB free              │
├────────────────────────────────────────────────┤
│  Grafana · Mimic · Status · Recipients · Logs  │
└────────────────────────────────────────────────┘
```

| Page | Contents |
|---|---|
| `/` | the P&ID with live values on it (§8.2), read-only; clicking a value opens its history (§8.2a) |
| `/system` | the health overview above, self-refreshing |
| `/hv` | Control: the CAEN setpoints and outputs, the cryostat setpoint, and the flow-integrator reset (§7.5, §10, §10a) |
| `/mimic` | a 301 to `/`, kept so bookmarks and the manual still work |
| `/status` | per channel: value, unit, age, `quality` |
| `/logs` | the last lines of each service log — saves logging in and hunting for files |
| `/hv` | the high-voltage page: per channel `VSET`, `VMON`, `IMON`, state, and the control actions (§10a) |
| `/hv/defaults` | edit the default setpoints `load defaults` offers (§4.6) — writes a file, touches no instrument |
| `/alarms` | the alarm chain end to end: what has fired, the thresholds in force (§11), and who is notified (§4.4) |

**`/alarms` rather than a top-level `/recipients`.** An earlier draft reserved
`/recipients` in this table. It moved onto the alarms page instead, and the
reasoning is worth keeping: this bar is five things an operator *looks at*, and
the recipient list is a setting touched a few times a year. Giving it the same
weight as *High voltage* would start the bar down the road of becoming a
settings drawer — and `/hv/defaults` had already set the opposite precedent,
that a setting lives next to the thing it configures. Recipients configure
alarms.

It also gave the thresholds in force (§11) a home. They were published and
displayed nowhere, and "what am I protected against, and who gets told?" is one
question, not two.

Plus links to Grafana on `:3000` and to the manual, which is **mounted by this
same application at `/manual`** rather than served from a second process. The
lab PC is not assumed to reach the internet, and a manual you can only read when
the network is up is not a manual. `/manual` and not `/docs`, because "docs" on
a FastAPI application means the OpenAPI page and would be read as that.

The Lake Shore setpoint and heater range live on `/`, beside the reading they
change. There is no separate `/control` page: the control actions went to the
pages that already show the thing being controlled, which is one fewer place to
look and keeps the value and the box that changes it in the same eyeful.

**The status page reads from MQTT retained topics, never from the database.** If PostgreSQL is down the page must still work — that is precisely when it is needed. A status page that fails together with the component it reports on is worthless.

Kept deliberately plain: server-rendered HTML from FastAPI, a meta refresh or a few lines of `fetch`, no JavaScript framework and no build step. In three years a student must be able to change it without installing a toolchain.

**The one thing this UI can change is the flow-integrator reset**, and it is allowed to exist before §10 because it changes a *record* rather than an instrument — nothing is erased, the period is kept (§7.5). It is a `POST` followed by a redirect, never a `GET`: the overview reloads itself every ten seconds, so a mutating `GET` would fire on its own, repeatedly, with nobody at the machine.

That self-reload is suspended while anything on the page has focus. A ten-second refresh that clears a half-typed name, and the click that was about to follow it, makes the page actively hostile to the one action it offers.

**Web UI — control.** Current value of every channel, alarm state, service health, and the control actions. Per HV channel it shows `VSET`, `VMON`, `IMON`, on/off state and ramping status.

Setting a value requires a confirmation step. For HV the dialog **names every channel it is about to move, what that channel holds now, and what it is being moved to** — a number to confirm rather than an intention. It also says which of them are energised and will therefore ramp immediately, and which are merely being stored until somebody turns them on (§10a). Validation before any write: identity confirmed (§6.2), value within the range in `channels.yaml`, device not in an error state. A rejected command is acknowledged with a reason, never silently dropped.

**Retyping the value was the original plan and is deliberately not what was built.** The friction was justified here by "the failure mode is an extra zero on an electrode", and that reasoning does not survive contact with the rest of the design: an extra zero is out of range by construction and is refused twice before it reaches an electrode, by the software range from `channels.yaml` and by the board's own `MAXV`. What neither can catch is the wrong-but-legal value — the gate's voltage typed into the cathode's box — and against that, retyping is no help at all: it asks for the same wrong number a second time. Naming the channel beside both numbers is what catches it. Retyping would also have to be done eight times on a page whose whole point is applying a column of setpoints in one gesture, which is the kind of friction that gets worked around rather than followed.

This paragraph described the retyping as though it existed, for as long as the page had a single "Apply the setpoints you have entered?" and nothing more. A design document promising a safeguard the code does not have is worse than one that admits the gap, because it is trusted.

The page also carries alarm **acknowledge** (stop the repeating notification; the condition is still displayed as active) and **silence for N minutes** (for a deliberate intervention). Acknowledging is not fixing, and the UI must never let the two look alike.

**Config files in git — the system's definition.** Calibrations, channel map, alarm thresholds. Edited in a text editor, committed, then applied without a restart and without a gap in the data:

```
xams-ctl reload
```

These are not editable from the web UI — with two exceptions, both of which
change often and neither of which carries a safety consequence:
`recipients.yaml` (§4.4), and the HV default setpoints in `hv_defaults.yaml`
(§4.6). The second is the case this section anticipated below: the need was
demonstrated by an R&D setup whose operating point moves weekly. It went to a
**separate file** so that the reviewed parts of `channels.yaml` — identity,
sign, and above all `limits` — stay hand-edited and in git. A wrong threshold silently disables protection and is discovered months later; in git it has review, history, and the config hash in every data file records exactly which version produced which data (§4.4). A clickable threshold has none of that.

The honest cost: changing a limit means editing a file rather than dragging a slider. For a handful of changes a year that is the right trade. If thresholds turn out to need weekly adjustment, the fix is a UI that edits the YAML **and** commits it — the same audit trail with less friction. Build that only once the need is demonstrated.

**Python client — scripts and notebooks.**

```python
from xams_sc import Client
c = Client()
c.get("pmain")                   # current value
c.history("tt201", hours=24)     # pandas DataFrame
c.set("hv_anode", 3500)          # same validation, same audit log as the UI
```

This is what makes the system useful beyond monitoring: calibration runs, scripted ramps, correlation studies. It is also how the `anode_timing.vi` behaviour would be reimplemented if it turns out to be needed (§7.2).

For debugging, `mosquitto_sub -t 'xams/#'` shows everything live with no UI involved.

### 8.2 The P&ID mimic

`/` renders `notes/xams_piping_and_instrumentation.pdf` as a live diagram: the plant as drawn, with the current value written next to every instrument bubble. It answers the question the status table cannot — *where* is `tt203`, and what is it next to. This is the one place where the tag names of §3 stop being labels and become a map.

It earns its own page rather than a place on `/`. The landing page must answer "is everything all right?" in one glance with no scrolling; a full P&ID needs zoom and attention. Both are wanted, and they are wanted at different moments.

**How it is built.** The PDF is converted once to `api/static/xams_pid.svg` (`pdf2svg`, or Inkscape) and committed. Each instrument bubble is given `id="<channel>"` — `tt201`, `pt101`, `fm101` — matching `name` in `channels.yaml` exactly, and holds an empty `<text>` node for the value. The page then needs perhaps thirty lines of `fetch` and `getElementById`: no framework, no build step, the same constraint as the rest of the UI. The SVG is a static file; an SVG editor is the only tool needed to touch it.

**It reads MQTT retained topics, not the database** — the same source as `/status`, for the same reason (§8.1). Alarm state comes from `xams/alarm/<channel>` and colours the bubble.

**A stale channel goes grey, never keeps its last number.** A mimic diagram showing a frozen value as though it were live is the classic failure of this kind of display, and it is worse than showing nothing: it invites a decision based on a reading that stopped being true an hour ago. Staleness is already defined for alarms (§11) — the same threshold applies here.

**Read-only. Valves are drawn, never clickable.** Control stays on `/control`, where every write is validated, confirmed and audited (§10). A diagram is an invitation to click, and the valves on this drawing are manual hardware in any case.

**The drift risk, and the check that catches it.** The SVG is a copy of a drawing that will eventually change, and a mimic quietly out of date with the plant is a liability. At service start, every `id` in the SVG is compared against `channels.yaml` in both directions, and any tag present in one and missing from the other is logged as an error. Roughly ten lines; it is what makes the page survivable three years from now. The SVG is re-exported when the P&ID is revised — a step for `OPERATIONS.md` (§14).

**Tags on the drawing that are not instrumented** — `SG101`, `SG102`, the RGA, `EVM116`, the valves `V1`–`V28`, the compressor and the pulse tube — are drawn without a value and greyed. That is informative in itself: it shows at a glance how much of the plant the slow control actually sees, and what a later phase could add.

### 8.2a Clicking a value on the mimic opens its history

The mimic answers *where* a tag is. The question that follows it, every time, is *what has it been doing* — and today that means leaving the page, opening Grafana, finding the right dashboard and hunting for the channel among a dozen others. Clicking the number closes that gap: `tt203` on the drawing becomes `tt203` on a plot, in one click, with no searching.

This is the natural companion to §8.2 and not a new surface. The mimic stays what it was; it gains one affordance.

**One dashboard, not one per channel.** Every reading lands in a single `meas` table keyed by `channel` (§9.2), so one dashboard with a `$channel` template variable plots any tag in the system:

```sql
SELECT $__timeGroupAlias(t, $__interval), avg(value) AS value
FROM meas WHERE channel = '$channel' AND $__timeFilter(t)
GROUP BY 1 ORDER BY 1
```

A dashboard per channel would mean forty dashboards to build, forty to keep in step with the schema, and a Grafana sidebar nobody can read. The variable costs one dashboard and one line of SQL. The existing dashboards (`XAMS Heaters`, `Overview`) stay as they are — they are curated views of related channels, which is a different job from "show me this one tag".

**The link is computed, never configured.** Each value node on the SVG already carries `id="v-<channel>"`, matching `name` in `channels.yaml` exactly — the invariant §8.2 already enforces in both directions. The channel name is therefore already in the DOM, and the URL is built from it:

```
{grafana}/d/xams-channel/channel?var-channel={name}&from=now-24h&to=now
```

No new field in `channels.yaml`, no tag-to-dashboard mapping table, no per-channel entry anywhere. A channel added to `channels.yaml` and drawn on the P&ID gets its plot the same day, with nothing else edited. This is the property that keeps the feature from becoming a maintenance chore: **there is nothing to keep in step, because there is no second list.**

**Only values are clickable. Valves are not, and never become so.** §8.2 draws the line at control: the drawing is read-only and a valve is manual hardware. That line does not move here — looking at history is reading. But the *habit* the drawing teaches does matter: once one thing on a diagram responds to a click, everything on it invites one. So the clickable region is exactly the value text and its bubble, it carries `cursor: pointer` and a tooltip, and nothing else on the SVG reacts to the pointer at all. Tags drawn without a value — `SG101`, the RGA, `V1`–`V28` — have no channel and so have no link, which is correct rather than a gap: there is no history to show.

**A stale channel still links.** Grey and a dash means the reading stopped (§8.2), and that is precisely the moment someone wants the plot — *when* did it stop, and what was it doing before. Suppressing the link on stale channels would remove it exactly when it is most useful.

**Built in two steps, and the second is not rework.**

*Step 1 — the link.* Clicking opens the dashboard in a new tab. Nothing changes in Grafana, nothing changes on the host, and the operator gets the full toolbar: zoom, range picker, export, add-to-dashboard. Roughly ten lines in `mimic.html` plus a URL builder.

*Step 2 — the panel in place.* The same URL builder, pointed at `/d-solo/...&panelId=1&theme=dark`, loaded into a small modal over the drawing: channel name, one time-series, a 1 h / 24 h / 7 d selector, an *open in Grafana* link, and Esc or click-outside to dismiss. The drawing stays behind it, so the reading keeps its context — which is the whole reason the mimic exists.

Step 1 is worth shipping on its own, and step 2 is a strict addition to it: the modal calls the same function, and the *open in Grafana* link inside the modal **is** step 1. If embedding turns out to be more trouble than it is worth on this host, step 1 remains and nothing is thrown away.

**One source for the Grafana URL.** It is hardcoded in `base.html` today. With a second and third use it becomes a thing that can disagree with itself, so it moves to `grafana.url` in `secrets.yaml` — the key `grafana.py` already reads for the drift check (§12) — and is passed into the page context alongside `state` and `config`. One value, one place, already exists. (`secrets.example.yaml` does not currently document the `grafana` block at all; it should.)

**What step 2 costs, stated plainly.** An iframe is the browser talking to Grafana directly, so Grafana has to permit it:

```ini
[security]
allow_embedding = true
[auth.anonymous]
enabled = true
org_role = Viewer
```

Without the second block the panel renders a login form instead of a plot for anyone not already signed in — which looks like a broken page, not a permissions message.

Anonymous viewer access is acceptable **only because of §8**: Grafana binds to `127.0.0.1` and is reachable from the lab PC alone. It grants read access to every dashboard to anyone who can already reach the machine, and anyone who can reach the machine can already open Grafana. **If Grafana is ever exposed beyond loopback, this setting must be reconsidered in the same breath** — it is one of the assumptions that quietly stops holding when a bind address changes.

`grafana.ini` lives outside this repository, so this is a host change that git does not record and `grafana.py` does not check. It therefore belongs in the install script and in `OPERATIONS.md` (§14), or a rebuilt lab PC gets a mimic whose popups are all login forms, with nothing anywhere explaining why.

**When Grafana is down.** The modal shows a blank frame and no error — an iframe fails silently. It therefore renders its *open in Grafana* link and the channel name immediately, before the frame loads, so a failed embed degrades to step 1 rather than to an empty box. The overview already reports Grafana's reachability (§12); the mimic does not need to duplicate that check.

**The checks that keep it honest.** The dashboard `uid` becomes an interface — the mimic hardcodes it — so it is fixed at `xams-channel` and the dashboard is committed to `grafana/dashboards-archive/`, where the existing drift check (§12) compares it against Grafana and reports it as `unsaved` or `missing` if the two part company. One test asserts the URL builder produces the expected string for a known channel; the SVG-versus-`channels.yaml` check of §8.2 already guarantees the channel name in it is real, so there is nothing further to verify.

**Steps, in order.**

| # | Step | Done when |
|---|---|---|
| 1 | Add the `grafana` block to `secrets.example.yaml`; read `grafana.url` in `app.py` and pass it to the page context; use it in `base.html` in place of the hardcoded address | the Grafana link in the footer still works, and the address appears once in the source |
| 2 | Build the `xams-channel` dashboard in Grafana: one time-series panel, `$channel` query variable over `SELECT DISTINCT channel FROM meas`, uid `xams-channel` | the dashboard plots any channel picked from the variable dropdown |
| 3 | Export it to `grafana/dashboards-archive/channel.json` and commit | `xams-ctl` reports Grafana drift `ok`, not `unsaved` |
| 4 | Add the URL builder and click handler to `mimic.html`; `cursor: pointer` and a `<title>` tooltip on value nodes only | clicking `tt203` opens its 24 h plot in a new tab; clicking a valve, a pipe or `SG101` does nothing |
| 5 | Test: the builder returns the expected URL for a known channel | `pytest` green |
| 6 | Add the modal: overlay, `d-solo` iframe, range buttons, *open in Grafana* link rendered before the frame loads, Esc and click-outside to close | the plot appears over the drawing, and closing it leaves zoom and scroll position untouched |
| 7 | Set `allow_embedding` and anonymous viewer in `grafana.ini`; record both in the install script and in `OPERATIONS.md`, with the loopback caveat above | a browser with no Grafana session sees the plot, not a login form |

Steps 1–5 are independent of any host change and can land alone. Steps 6–7 go together: without 7, step 6 shows a login form.

### 8.3 What is deliberately not in any interface

Ramp rates, trip currents, over-voltage limits, heater range and setpoint limits live on the instruments themselves. The software **displays** them and raises an alarm if they differ from the expected values in `devices.yaml`, but it cannot change them. This is what keeps the software out of the protection path (§10).

---

## 9. Storage

**What exists today.** The CSV files on the lab PC are the authoritative record; the MongoDB instance on the Nikhef server is temporary storage only, and is retired when LabVIEW is. So there is currently **no permanent archive** — the entire history sits on one disk, in a format whose column meanings live in a separate, overwritable header file.

Creating a durable archive is therefore not a reorganisation of something existing. It is the single largest improvement in this design.

Three layers, each with one job.

### 9.1 Sampling and logging rates

Sampling rate and logging rate are separate decisions. Conflating them is why the LabVIEW system writes a record every couple of seconds for channels whose physics moves in minutes.

```yaml
sampling:
  interval_s: 1          # read the hardware
  log_interval_s: 10     # write the mean of those samples
  flight_recorder:
    window_s: 600        # last 10 minutes at 1 Hz, held in memory
    dump_on: [alarm, trip, service_restart]
```

**Read at 1 Hz, log every 10 s as the mean of the ten samples.** The averaging is not merely data reduction — a mean of ten readings is less noisy than any single one, so the stored value is better than what is discarded.

Disk is not the constraint. At 30 channels and 1 Hz the cost would be roughly 10 MB gzipped per day, which is nothing on this machine. The reasons to log less often are query speed, plot legibility and ease of analysis. At 10 s the system produces about 260 000 records per day, ~1 MB gzipped, ~400 MB per year: ten years of history in 4 GB.

**Why 10 s and not slower.** The physics differs per channel:

| Channel | Time constant | 1 Hz is |
|---|---|---|
| Cryostat RTDs | minutes | heavily oversampled |
| Ambient temperature | tens of minutes | absurdly oversampled |
| Pressure, steady state | minutes | oversampled |
| Pressure, while pumping or filling | seconds | about right |
| HV `VMON`/`IMON`, normal operation | steady | oversampled |
| HV during a trip | **milliseconds** | **far too slow** |

A fixed 1 Hz is therefore simultaneously too fast for the temperatures and too slow for the only moment that resolution matters. Temperatures alone would be well served by 60 s, but **the rate is deliberately uniform across all channels**: identical timestamps let channels be compared without interpolation, and that simplicity is worth more than the few megabytes differentiation would save.

**Flight recorder.** The last 10 minutes at the full 1 Hz are held in a fixed-length `collections.deque`. When an alarm fires, a channel trips, or a service restarts unexpectedly, the buffer is written out:

```
data/events/2026-09-16T13-02-11_pmain_hihi.jsonl
```

This gives full resolution exactly around the moments that matter, without storing it permanently. It is what the present system lacks: after an incident there are only averaged values, and no record of the approach to it.

Optionally, for pressure and HV channels, store `min` and `max` of each interval alongside the mean, so short excursions are not lost between log points. Two extra columns; decide per channel in `channels.yaml`.

### 9.2 PostgreSQL — working store

**PostgreSQL.** Not InfluxDB, not "Timescale or Postgres" — TimescaleDB is an *extension* to PostgreSQL, not a separate product, and at this data volume it is not needed. It can be enabled later on the same database, without touching the data, if compression and automatic retention become worth having.

Rationale: a native Windows installer, a built-in Grafana datasource needing no plugin, `psycopg` and `pandas.read_sql` in Python, and SQL that anyone in the group can read today and in ten years. InfluxDB was the obvious default next to Grafana, but its Windows support is uneven and its data model and query language have been reworked twice between major versions — the wrong kind of churn for a system meant to be installed and left alone.

One table:

```sql
CREATE TABLE meas (
    t        timestamptz  NOT NULL,
    channel  text         NOT NULL,
    value    double precision,          -- mean over the log interval
    vmin     double precision,          -- optional, per channel
    vmax     double precision,          -- optional, per channel
    raw      double precision,
    unit     text,
    quality  text         NOT NULL DEFAULT 'ok',
    src      text         NOT NULL DEFAULT 'xams'   -- 'labview' for imported history
);
CREATE INDEX ON meas (channel, t DESC);
```

Written by `sinks/pg_writer.py`, subscribed to `xams/meas/#`. Batched, flushed at least every 10 s. **A failure to write must never block the bus or the services** — the writer logs, alarms, and keeps going.

This database is a **cache, not storage.** The archive is the files (§9.3, §9.4). If the database is lost or a different one is wanted later, write a new sink and replay the Parquet history into it. Nothing in any driver changes.

Grafana reads from here.

### 9.3 JSONL — raw archive

One file per UTC day: `data/raw/2026-09-16.jsonl`. Written by `sinks/jsonl_writer.py`, **independently of the database**. If the database falls over, no data is lost. The files are the truth; the database is an index over them.

First line of every file:

```json
{"t":"2026-09-16T00:00:00Z","meta":{"config":"a3f91c2","version":"1.4.0","host":"xams-sc"}}
```

Flush and `fsync` at least every 10 s so a power loss costs seconds, not hours.

### 9.4 Parquet — long term

**Not built** — the JSONL archive (§9.3) is complete and is the truth, so this
is a compaction step rather than a gap in the record. `tools/jsonl_to_parquet.py`
runs nightly on the closed day, then gzips the JSONL. Both are kept until the conversion has been verified; after that, keeping the gzipped JSONL as well is cheap and worth it.

**Log `raw` alongside the scaled value for at least the first year.** If a multiplier turns out to be wrong — the `×25` on `p101` is a candidate — the entire history can be rescaled. Without `raw`, it cannot. At ~30 channels every 2 s this costs roughly 120 MB/day uncompressed, around 10 MB gzipped.

---

### 9.4a Provenance: every reading says where it came from

Added 17 September 2026, after simulated values reached the production database
and could not be told apart from measurements.

`Measurement.src` travels **on the reading**, not on the writer:

| `src` | Meaning |
|---|---|
| `xams` | read from an instrument by this system |
| `sim` | **synthetic**, from simulation mode. Not a measurement. |
| `labview` | imported from the LabVIEW history (§9.6) |

It must be a field on the measurement rather than a property of the sink,
because one writer serves every service and therefore cannot distinguish them.
With provenance on the writer, running `sim` once put 62,000 synthetic rows
into `meas` indistinguishable from real ones, and undoing it meant deleting
rows by timestamp and channel name.

**A value that is not a measurement must never be able to pass as one.**

### 9.4b Storage is idempotent

`meas` carries a unique index on `(t, channel, src)` and the writer uses
`ON CONFLICT DO NOTHING`.

This is not defensive coding against a bug; duplicates arrive by a route that
is working as designed. **MQTT re-delivers retained messages to every new
subscriber**, so each time a sink reconnects it receives the last value of
every channel again, with its original timestamp, and would store it a second
time. Deleting those rows does not help — the next reconnect writes them back.
Only clearing the broker does, which is what `tools/clear_retained.py` is for.

With the constraint in place a replay is harmless, and reprocessing an archive
can never inflate the history.

The same reasoning gives the sinks a single-instance lock (§6.1), which they
originally lacked: on 17 September 2026 two sink processes ran at once and
duplicated 46,418 rows. Two drivers fighting over an instrument fail loudly;
two writers succeed quietly, and the only symptom is a row count.

### 9.5 MongoDB — temporary, and in the end not needed

> **Not built, and now unlikely to be.** This was insurance for a transition
> that turned out not to need it: Grafana was running before anyone missed the
> old Python viewer, so the compatibility layer never had a window in which it
> was the only way to see the data. It is kept here because the *reasoning*
> below is what made it safe to skip — a sink is a subscriber, so adding one
> later costs one file and touches no driver. Delete this section once the
> Nikhef server no longer expects data at all.

The existing MongoDB writer can be reused as a third sink, so the Nikhef server keeps receiving data and the existing Python viewer keeps working while Grafana is being set up. Adding it touches no driver: it is another MQTT subscriber.

**Deliberately a compatibility layer with an expiry.** It initially emits the current wide document shape, so the existing viewer needs no changes. Once Grafana is running it either moves to the long form or is removed. Record that expiry here and in the code — an undated temporary layer becomes permanent, which is how the present system came about.

Same rules as the other sinks: a Mongo failure never blocks the bus, and a TTL index on the timestamp keeps the collection from growing without bound. MongoDB is a convenience copy, never a second source of truth.

### 9.6 Importing the LabVIEW history

`tools/import_labview_csv.py` converts the existing CSV history into the same JSONL/Parquet form, so Grafana shows one continuous timeline across the migration rather than starting from zero. This is cheap and worth doing.

**One hazard, which must be checked before the import is trusted.** The LabVIEW front panel has a "Re-save headers" button, so the header file is mutable and there may be only one — describing only the most recent channel layout. If a channel was ever added, removed or reordered, older CSV files silently do not match it. Columns shift by one position and the values still look entirely plausible.

Check first:

```bash
for f in *.csv; do printf "%s  %s\n" "$(head -1 "$f" | awk -F';' '{print NF}')" "$f"; done | sort | uniq -c -w4
```

One group of column counts means one layout throughout, and the import is straightforward. Several groups mean several layouts, and each era needs its own column mapping — recovered by hand, from file dates and from what the values physically are. The importer must refuse to guess: a file whose column count does not match its declared mapping is rejected, not imported with a warning.

Imported records are tagged `"src":"labview"` so they are distinguishable from data taken by the new system.

---

## 10. Control path and safety

**The slow control is not a protection system.** It monitors, logs, alarms, and offers convenience setpoint changes.

### Rules

1. **Interlocks live in hardware.** HV trip on a pressure or vacuum excursion is wired to the CAEN interlock input from a real gauge trip. Not a Python service reading MQTT and deciding.
2. **Limits live in the instrument.** Ramp rate, trip current, over-voltage limit, heater range, setpoint limits are configured on the device. The software **verifies** them at startup and alarms on mismatch; it does not write them.
3. **No write without verified identity** (§6.2).
4. **Restart never actuates** (§6.1).
5. **Every write is logged** — who, what, when, previous value, new value — to an append-only audit file and to `xams/ack/#`.

**The RGA (added 13 October 2026, [drivers/rga.md](drivers/rga.md)).** The third instrument written to: control, scan setup, start and stop, and emission On/Off, through a whitelist. Rule 1 holds for it too: its protection against pressure is the analyser's own emission over-pressure trip, in the instrument. The p105 check is a **software guard** on top of that — it refuses to switch emission on and switches it off on a rising or missing p105 — and must not be described as an interlock.

### Command flow

```
UI / API  →  xams/cmd/<device>/<action>  →  service validates  →  instrument
                                                    ↓
                                        xams/ack/<device>/<action>
```

Validation before any write: identity confirmed, value within the configured range, device not in an error state. A rejected command is acknowledged with a reason; it is never silently dropped.

### Procedures

Operating the Lake Shore and the CAEN supplies by hand is tedious: menu navigation on the front panel for a setpoint, per-channel navigation across two supplies for high voltage. Bringing the TPC up means eight manual steps in the right order, which is exactly the kind of work that goes wrong at two in the morning.

Control therefore does more than save effort — a written, reviewed sequence that always proceeds in the same order is **more reliable than a person with a printed procedure.** This is an argument for building the control path, not merely a convenience.

Named sequences live in git as data, like everything else:

```yaml
# procedures/hv_rampup.yaml
name: HV ramp up
steps:
  - set: {channel: hv_cathode, value: -10000, ramp: 50}
  - wait_until: {channel: hv_cathode, stable_within: 10, timeout_s: 600}
  - set: {channel: hv_gate, value: -5000, ramp: 50}
abort_on: [any_alarm, trip, operator_stop]
```

Requirements:

- **Dry run.** Every procedure can be executed in a mode that reports each step without writing anything. This is also how a procedure is reviewed before it is run for the first time.
- **Abort is always available**, and aborting leaves the hardware in a defined state — it does not simply stop mid-sequence without saying where it stopped.
- **Every step passes the ordinary write validation** (§6.2, and the rules above). A procedure is not a way around the guardrails.
- **The whole run is audited**, step by step, not merely as "procedure executed".
- A procedure that is edited is a change to a file in git, reviewable like code.

### Decided: the heaters are cut on a `pmain` hihi, and on nothing else

The existing LabVIEW system performs protective actions in software: a "Shut off heater if Alarm is active" switch and a `KILL VOLTAGE` button.

**Decided 18 September 2026: the heaters are switched OFF when `pmain` reaches its `hihi` threshold (2.5 bar). That is the only condition, and it is the only automatic actuation anywhere in this system.**

> **Decided, not built.** The threshold is live in `alarms.yaml` and the write
> path it would use exists (`xams/cmd/lakeshore/range`), but nothing connects
> them: today a `pmain` hihi notifies a person, and a person cuts the heaters.
> That is the safe direction for the gap to be in — the system under-acts
> rather than over-acts — but it must not be described to an operator as
> something the software does, because it does not.

The reasoning is the physics, and it is worth writing down because the direction is not obvious from the alarm name. Cutting the heater lets the cryostat run colder; colder xenon recondenses; recondensing xenon drops the pressure. **The wanted direction in this emergency is more cooling, not less**, and cutting the heater is how the software can move that way. It is not a temperature action that happens to be triggered by a pressure alarm — it is a pressure action.

**Narrower than LabVIEW's, deliberately.** The original switch fired on *any* active alarm. A thermocouple going stale, a UPS on battery or a gate current drifting would all have cut the heaters, none of which is helped by doing so, and each of which teaches the operator that the system does surprising things. The trigger here is one channel at one threshold.

Three properties this must have, because each failure is worse than not having the feature:

- **It latches.** Once cut, the heaters stay off until a person puts them back. Restoring them because the pressure dipped back under 2.5 bar would drive exactly the oscillation that got us there.
- **A failed write is loud.** If the command cannot be delivered, that is an alarm in its own right, at the severity of the condition that prompted it. Silence must never be mistaken for "the heaters are off".
- **It is audited like every other write** (rule 5): who or what, when, previous value, new value.

**This is not an interlock, and must not be described as one.** It depends on the broker being up, the alarm service being alive, the reading being fresh and the threshold being right, and it fails silently if any of those is untrue — which is precisely what rule 1 says a protective system may not do. It is a **software backup that acts faster than a person can**, and where the pressure excursion is genuinely dangerous the protection belongs in hardware, wired from a gauge trip. Having this must not be allowed to postpone that.

`KILL VOLTAGE` is a separate question about the CAEN supplies and **remains TBD** (§16). It is not settled by the above.

---

## 10a. Bringing the high voltage up

**Status: built, 18 September 2026**, except where this section says otherwise.
Written from the operating problem below, then implemented: `VSET` and
energising are in the driver, on `/hv` and in `xams-ctl`; the invariant holds on
both boards. The one part still unbuilt is the **server-side retained plan** —
setpoints are staged in the page's own boxes and applied as one action, which
gives the plan-then-apply order but not the shared, reload-proof plan described
under *Plan, then apply* below.

### The problem

Today, switching a channel on means flipping the physical enable at the supply,
after which **the channel ramps immediately to whatever `VSET` happens to be
stored in the board.** That stored value is invisible from the lab, unchanged
since whenever somebody last set it, and nobody is asked to confirm it.

This is not hypothetical. On 18 September 2026 all eight channels were
`DISABLED`, and the setpoints sitting in the boards were:

| | pmt_bot | pmt_top | ts | bs | cathode | gate | anode | nai |
|---|---|---|---|---|---|---|---|---|
| `VSET` | 700 | 0 | 500 | 600 | 2250 | 1750 | **4200** | 600 |

Flipping the anode enable at that moment would have taken it to 4.2 kV, with
the only warning being that somebody remembered.

### Two hand gates, not one

Found on 18 September 2026, while the write path was first tried against the
real supplies: **every `SET` was refused with `#BD:00,LOC:ERR`.** Both boards
were in `LOCAL` mode, in which the front panel has control and remote
setpoints are rejected — while `MON` keeps answering perfectly. That asymmetry
is why a board in `LOCAL` is indistinguishable from a working one until
somebody tries to write, and why the first version of the error message
(*"the supply did not accept the command"*) was useless.

`BDCTR` is set from the front panel and **nothing in this software can change
it.** So there are two independent gates between code and an electrode, and
neither is reachable from here:

| Gate | Scope | Set by |
|---|---|---|
| `BDCTR` = `REMOTE` | the whole board | front panel menu |
| the channel enable | one channel | front panel |

This was not designed, it was discovered — and it is a better safety story
than the one this section was originally written around. Both are read and
both are shown: the control mode is logged at startup and named in the refusal
when a write is attempted in `LOCAL`.

### The invariant that fixes it

> **A channel that is not enabled has `VSET` = 0.**

Everything else follows. If that holds, the physical enable becomes a safe
action: it always brings a channel up at zero volts. **Raising voltage is then
always a deliberate, software, audited step**, never a side effect of touching
the hardware.

The enable switch stays a hand operation and the software never gets a command
to change it. That is not an omission — a hardware gate that software cannot
reach is the last thing standing between a bug and an electrode.

**Refusing to break it is not the same as keeping it.** Added 22 September
2026, after the invariant was found broken by an entirely ordinary sequence:

1. a channel is energised at its working voltage;
2. it is switched off from `/hv` — it ramps down, and `VSET` stays where it
   was, because nothing asked for it to change;
3. hours later somebody disables that channel at the front panel. They have
   every reason to think it is off, because it is.

The board now holds a disabled channel with kilovolts in its setpoint, and the
next flip of that switch is the unannounced ramp this whole section exists to
prevent. The write path could not have stopped it: the state was created at the
front panel, which the software cannot see coming. Worse, the page that warned
about it also disabled the setpoint box for a switched-off channel, so the one
value that *would* have been accepted — zero — could not be sent from the page
complaining about it.

So the invariant is **enforced on every read cycle**, not only at the moment of
a command: a channel whose enable is off and whose `VSET` is not zero has it
written to zero, audited with no person as the actor, and logged. It keys on
bit 10, the switch, and never on bit 0, the output — a channel that is switched
on but not energised is where an operator stands between flipping the enable
and pressing turn-on, and is exactly when they load a setpoint.

The stored setpoint is lost when this fires. That is the right trade: the
operating point belongs in `channels.yaml` or `hv_defaults.yaml` (§4.6), where
*load defaults* offers it again, and not in a board that nobody can read from
the lab. A channel disabled at the front panel is one whose setpoint nobody
should be relying on the hardware to remember.

Enforcement can fail — a board in `LOCAL` refuses every remote write until
somebody walks to the front panel. It is then retried on a backoff rather than
every second, and the warning on `/hv` remains the last line: if it is showing,
the enable switch is genuinely not safe to touch.

### States

Per channel, derived from `STAT` and the monitors (§7.2):

| State | Meaning |
|---|---|
| `DISABLED` | the front-panel switch is off (bit 10). **Invariant: `VSET` must be 0.** |
| `STANDBY` | switched on, output not energised (neither bit 10 nor bit 0). A setpoint may be loaded here; nothing comes out. |
| `RAMPING` | energised, `VSET` != `VMON`; the board is moving at its own `RUP`/`RDW` |
| `ON` | energised, `VMON` at `VSET` |
| `FAULT` | trip, interlock, over-current, over-temp |

**Three levels, not two, and only the middle one is software's.** The switch is
the hand gate; energising is the software act; the setpoint is the voltage it
will go to. `DISABLED` <-> `STANDBY` is **by hand, at the supply** — no software
path exists in either direction. `STANDBY` <-> `ON` is `PAR:ON`/`PAR:OFF`, which
this software does send, from `/hv` and from `xams-ctl hv-on` / `hv-off`.

Reading the switch is **bit 10**, never "not bit 0". They are different
questions and conflating them caused four bugs in one afternoon on
18 September 2026 — which is why `hv_status.py` names them `is_disabled` and
`is_energised` and no longer offers an `is_enabled` for either to hide behind.
The most consequential of the four refused a setpoint to a channel that was
switched on but not yet energised, which is exactly the state an operator is in
when they want to load one.

### Plan, then apply

Setpoints are staged before they are written. A **plan** is a set of proposed
values that has had no effect on any instrument.

1. **Load defaults** — fill the plan from configuration. No hardware effect.
2. **Edit** — the operator changes what they want, validated against the
   `limits` in `channels.yaml` as they type, and again at apply.
3. **Apply** — write `VSET` per channel, read back, verify, audit.
4. **Standby** — write `VSET` 0 to the named channels. The way down.

The plan lives **server-side and published retained on the bus**, not in a
browser tab: two people looking at the page must see the same pending change,
and a page reload must not silently discard one.

> **Not built as specified.** What `/hv` does today is steps 1–4 with the plan
> held in the page's own boxes: *Load defaults* fills them from `channels.yaml`
> and writes nothing, *Apply setpoints* writes every filled box as one action,
> and *turn ON* is separate from both. The order and the refusals are as
> designed; what is missing is the shared plan — a second browser sees nothing,
> and a reload discards what was typed. That is a smaller loss than it looks
> while one person operates the supplies, and it is the next thing to build if
> two ever do.

**A plan is discarded when the service restarts** (§6.1 rule 4: restart never
actuates). A plan that survived a restart and was applied later, by somebody
who had not staged it, is worse than losing it.

### The rule that makes the order safe

> **Apply refuses to write a non-zero `VSET` to a channel that is not enabled.**

This is what keeps the invariant true, and it forces the only safe order:

```
   stage the values          (no effect)
   enable by hand            (safe: the channel is at 0 V)
   apply                     (the board ramps, at its own rate)
```

Without this rule the whole design is decorative: an operator could stage
4.2 kV, apply it to a disabled anode, walk over and flip the switch — and get
exactly today's rough behaviour, now with the software's blessing.

It also settles what "turn on the HV with everything at 0 V" means: it is
allowed, and it does nothing. The UI shows *waiting for the enable switch* on
any planned channel that is still disabled.

### Changing a voltage while it is on

**The same plan-and-apply flow, always.** No text box that acts on Enter, and
no quick path that skips the audit — one mechanism is easier to reason about
than two, and the second one is always the one that bites.

What changes is the confirmation, which should state what the operator
actually needs to judge:

- the change, from and to
- **the ramp time**, computed from the board's own `RUP`/`RDW`: a 2 kV move at
  50 V/s is forty seconds during which the detector is neither where it was
  nor where it is going
- whether the new value crosses an alarm threshold

### Recovering from a trip

Added 23 September 2026, after a channel tripped and could only be recovered by
power-cycling the supply. A trip **latches**: the board switches the channel
off, sets `STAT` bit 7 and raises its board alarm, and neither `ON` nor the
enable switch clears it.

The recovery is `BDCLR`, the fourth and last parameter this driver writes. It
is board-wide, and it is sent only after **the setpoint of every latched
channel on that supply has been written to zero and read back**. If any of those
fails, the alarm is not cleared. The channel comes out of a clear exactly as
safe as a freshly enabled one: off, at 0 V, one deliberate step from its
working voltage. The same trade as the invariant above — the setpoint is lost,
and belongs in `hv_defaults.yaml` anyway.

`BDCLR` touches no limit, so it stays outside rule 2 below.

**Revised the same evening, after two real trips on the anode set neither
bit 7 nor `BDALARM`.** The board cut the output and went on reporting ON +
UNDER_VOLTAGE, so nothing that waited for its flags ever fired. The driver
now detects a trip from the output itself as well — ON, not ramping,
UNDER_VOLTAGE, VMON below half of VSET, on 3 reads in a row — makes it safe at
once (VSET 0, then OFF, audited as `automatic (trip)`), and latches it on the
retained topic `xams/hv/trip/<channel>` until a person clears it. A latched
channel cannot be turned on. A trip is **not an alarm**: nobody is notified,
because it happens with people in the lab. Whether `BDCLR` actually recovers a
dead output is still open; a channel that trips again after a clear without
having worked in between is marked as needing a power cycle. Details in
`docs/drivers/caen.md`.

### What the software must never do

1. **Never flip a channel's front-panel enable.** No such command exists, in
   either direction, and none is to be added. Energising an *already
   switched-on* channel is a different act and is allowed (§10a *States*):
   the switch is the gate software cannot reach, and it is what makes the
   invariant worth having.
2. **Never write `MAXV`, `RUP`, `RDW`, `TRIP` or `ISET`** (§10 rule 2).
   Protection stays configured on the instrument.
3. **Never actuate on startup or restart** (§6.1 rule 4).
4. **Never apply a plan on its own.** A plan is applied by a person, once.

### Before any of this can be true

~~**The invariant is violated today.**~~ **Done, 18 September 2026.** Both
boards were switched to `REMOTE` at the front panel, and
`xams-ctl hv-standby --by AP` zeroed all eight setpoints — each verified by
read-back and recorded in the audit trail:

```
hv_anode_vset    +4200.0 -> 0.0     hv_cathode_vset  -2250.0 -> -0.0
hv_gate_vset     -1750.0 -> -0.0    hv_pmt_bot_vset   -700.0 -> -0.0
hv_bs_vset        -600.0 -> -0.0    hv_nai_vset       +600.0 -> 0.0
hv_ts_vset        -500.0 -> -0.0    hv_pmt_top_vset     -0.0 -> -0.0
```

The invariant now holds, and `/hv` shows it: no channel is armed, and the
enable switch is a safe thing to flip.

~~**`VSET` is not currently read.**~~ **Done, 18 September 2026.** `VSET` is
published as `hv_*_vset`, stored signed, and shown on `/hv` beside `VMON`.
A disabled channel whose setpoint is not zero is marked in red — which is how
the seven violations above were found rather than guessed at.

### Open

- **Ordering between channels.** Does the gate require the cathode first? If so
  this grows into §10's `procedures/hv_rampup.yaml`, and the plan-and-apply
  model above is the step on the way.
- ~~**Where default setpoints live.**~~ **Resolved 20 September 2026: a
  separate file, `config/hv_defaults.yaml`, editable from `/hv/defaults`**
  (§4.6). There is not one right answer per channel — this is an R&D setup and
  the operating point moves — so the number that changes weekly was split from
  the channel definition that does not. `limits` stayed behind in
  `channels.yaml`, deliberately.
- **An orderly "everything to standby"** — distinct from `KILL VOLTAGE`, which
  remains TBD.


---

## 11. Alarm engine

`alarms/engine.py` subscribes to `xams/meas/#` and evaluates against `alarms.yaml`. It decides and notifies; Grafana only displays the result.

**Grafana's own alerting is deliberately not used.** It works by querying the database on a schedule, which places PostgreSQL and Grafana inside the alarm path: if either is down or slow, notifications do not go out. Alarms are the part of this system that must be most reliable, so the path is kept as short as possible — measurement → bus → engine → SMS, with no database and no web server involved. Grafana also has no native SMS contact point, so a script would have to be written regardless.

The engine publishes alarm state to `xams/alarm/<channel>`, which is stored like any other record, so Grafana can show current state and history without being part of the mechanism.

### Thresholds live in one place

Displaying alarm *state* needs no knowledge of the limits: the engine publishes a result (`{"state":"major"}`) and Grafana colours a panel from it. Drawing a threshold *line* on a graph is different — Grafana would need the number, and it would then exist both in `alarms.yaml` and in the dashboard JSON, where the two will drift apart.

**Therefore: no threshold lines on graphs.** The alarm panel colours, which is what is actually watched. Generating the dashboard JSON from `alarms.yaml` would solve it properly, but the cost is not the generator (a template substitution, some forty lines) — it is the recurring workflow: every dashboard edited in the Grafana UI must be re-exported and re-templated, or the generator overwrites the manual work. For a handful of panels that is more friction than the problem warrants. Revisit it if the lines turn out to be genuinely missed *and* have drifted once.

**Instead, make drift visible.** On startup and on every reload the engine publishes the limits it actually loaded:

```
xams/status/limits   {"pmain": {"high": 1.8, "hihi": 2.0}, ...}
```

written to the log and **shown on `/alarms`** (§8.1) — beside what has fired
and who would be told about it, which is the same question asked three ways.
The real risk is not a missing line on a plot; it is believing a threshold is
2.0 when it is 20. This exposes what is in force, with no generation machinery.

An earlier draft said "shown on the status page". It is not: `/status` is a
45-row channel table and a threshold list is not a channel reading. **Where
nothing has been published the page says `unknown`, never "no thresholds"** —
the engine being unreachable and the engine having nothing configured are
different facts, and only one of them is about the plant. Same rule as the
dashboard drift check (§12): a check that cannot see is not a check that
passed.

- Four thresholds per channel, EPICS-style: `lolo`, `low`, `high`, `hihi`, each with a severity.
- **Hysteresis** on every threshold, to stop a channel sitting on a limit from producing a stream of notifications.
- **Deduplication**: one notification per state transition, then repeat at `min_repeat_minutes` while the condition persists.
- **Staleness is an alarm**, at the same severity as a threshold breach. A dead sensor must not read as healthy.
- Alarm state is published on `xams/alarm/<channel>` and is itself retained, so the UI shows the true state on connect.

### Notification

`alarms/notify.py` exposes a thin interface — `send_sms(number, text)`, `send_email(...)` — and the alarm engine knows nothing else, so what sits underneath is replaceable without touching alarm logic. Recipients come from `recipients.yaml` (§4.4); severity routing from `alarms.yaml`.

**Reuse the existing SMS script.** LabVIEW already calls a Python script (`SC_software\send_sms_python\`); the new system can import it as a module instead of starting a subprocess. The argument is risk, not convenience: that script works. The numbers are right, the gateway accepts it, the message format arrives, the costs are known. Rebuilding it means rediscovering all of that. The same applies to the email script and `check_ups.vi`.

Three things to check before adopting it: whether it is Python 2 or 3; whether phone numbers are hardcoded (they belong in `recipients.yaml` now, passed in as arguments); and whether it writes a status file that only LabVIEW reads.

### The daily report

`alarms/daily.py` sends one email a day: what is alarming, what is stale, which
services are up, and the current value of every channel. It is rendered by
`alarms/mail.py`, which also renders the alarm notifications themselves.

```
python -m xams_sc.alarms.daily                       # render and send
python -m xams_sc.alarms.daily --preview out.html    # render only, send nothing
python -m xams_sc.alarms.daily --to me@nikhef.nl     # send to one address
```

**Run from Task Scheduler, not from the alarm service.** A report is a
convenience and the alarm engine is not, and a scheduler bug that wedged a
thread inside the engine would take the alarms down with it. Separate process,
separate failure.

**Its state comes from retained MQTT, never from the database** (§8.1), for the
same reason the status page does: the report must be sendable when PostgreSQL is
down, because that is one of the things worth being told about.

It is also a **liveness signal in its own right** — a report that stops arriving
says something even when it says nothing alarming. That is a weak signal, not a
substitute for the VM watchdog (§12), because it shares a machine and a code
path with everything it reports on.

**Email HTML is not web HTML.** `mail.py` exists as a separate module rather than
a template because mail clients accept a decade-old subset of it, and the rules
for that subset have nothing to do with how the web UI is built.

### Credentials

**Credentials never enter the repository.** An API key committed to git remains in the history after deletion, and private repositories are still cloned, shared and backed up. Credentials live in `config/secrets.yaml`, listed in `.gitignore`; `config/secrets.example.yaml` is committed with empty values so the required keys are documented. `notify.py` reads from there, never from code.

While examining the existing script, check whether its key is still valid and who else holds it. A gateway credential that has sat on a desktop for years is a good candidate for rotation.

---

## 12. Operations

### Service management

Each service runs under NSSM as a Windows service, with stdout/stderr to `logs/<service>.log`, and restart on crash with a throttle — a service that cannot start at all is not retried forever.

`tools/install_services.ps1` installs them; `-Manual` selects the trial phase, `-Uninstall` removes them again. Re-running it updates the existing services in place.

| Phase | Start type | Notes |
|---|---|---|
| Debug | not installed | run from a terminal; let it die on errors |
| Trial | `SERVICE_DEMAND_START` | restarts on crash, not at boot — `install_services.ps1 -Manual` |
| Production | `SERVICE_AUTO_START` | **in force since 18 September 2026** — see below |

**Automatic start was held off while LabVIEW was the fallback**, because every device admits only one process and a service auto-starting after an overnight reboot claims the hardware and locks LabVIEW out.

**That condition has passed, and auto-start is on.** LabVIEW is closed, this system holds all four instruments, and real alarm thresholds now depend on it running. The reasoning reversed with the dependency: a reboot that left the lab PC with a broker, a database and a Grafana — and nothing acquiring, storing or alarming — would say nothing about it, because the thing that would say so was also down.

**Going back to LabVIEW is therefore one deliberate command**, not an omission somebody has to remember:

```
xams-ctl stop --for-labview
```

It stops the services *and* suspends their auto-start, so a reboot does not quietly take the instruments back. `xams-ctl start` restores both. Handing the hardware over and handing it back are each a single action, which is what keeps the fallback real rather than theoretical.

### `xams-ctl`

```
xams-ctl start | stop [--for-labview] | restart | status | reload | check
xams-ctl flow-reset [--by WHO]
xams-ctl hv-set <channel> <signed volts> [--by WHO]
xams-ctl hv-standby [channel] [--by WHO]
xams-ctl hv-on | hv-off [channel] [--by WHO]
```

`reload` re-reads the YAML configuration without restarting the services, so a threshold or calibration change costs no gap in the data.

`stop` releases all hardware for LabVIEW; `--for-labview` also suspends auto-start (above). `status` shows each service's state, heartbeat age, and whether the Grafana dashboards still match git. One command, correct order, every time.

`check` validates the configuration and prints what it defines — channel counts per device, the config hash, and any enabled channel still carrying `unit: TBD`. It touches no hardware and no service, so it is safe to run at any time, including before a first install.

The control verbs carry `--by`, which is recorded in the audit log (§10 rule 5). They are the command-line half of the web UI's control actions and go through the same bus, the same validation and the same acknowledgement — never around them. `hv-set` takes a **signed** value (§7.2); `hv-standby` is the way down, and `hv-on`/`hv-off` energise a channel that a person has already enabled by hand at the supply (§10a).

### Alarm watchdog on the VM

The alarm engine cannot report that the alarm engine has stopped, and the heartbeat monitor (§6.3) runs in the same Python environment on the same machine — if that goes down thoroughly, both are silent.

If a Nikhef VM exists, configure **one** Grafana alert rule there:

```
no measurement received for 15 minutes  →  email
```

A dead-man's switch with a completely separate code path, on a different machine, on a different network. It catches what nothing local can: the lab PC off or crashed, Windows rebooting for updates, services stopped and not restarted, or the network between lab and Nikhef down.

This is a single rule about silence, not a physics threshold, so it duplicates nothing from `alarms.yaml`. A second copy of this rule on the local Grafana would be pointless: if the PC is off, that Grafana is off too.

It is also the strongest argument for setting up the VM — stronger than colleagues being able to look at plots. Without an outside observer, "the system has gone completely quiet" is the one failure that cannot be detected.

### Deployment and topology

The lab PC is self-contained: everything needed to acquire, store, plot and alarm runs there, and none of it depends on any other machine. A Nikhef VM, if one is added later, is a copy plus a window for the rest of the group.

**The VM exists since 23 September 2026:** `plotit-xams.nikhef.nl`, installed by `tools/vm/install_vm.sh`. How to run it is in `docs/vm.md`.

| Component | Lab PC | Nikhef VM (optional) |
|---|---|---|
| Device services | ✓ | — |
| Mosquitto | ✓ | — |
| **Alarm engine** | ✓ **always** | — |
| JSONL + Parquet archive | ✓ **the truth** | copy |
| PostgreSQL | ✓ short retention (~30 days) | ✓ full history |
| Grafana | ✓ a few operational dashboards | ✓ the full set, for the group |
| Web UI / status page | ✓ | — |

The alarm engine runs locally and never depends on the VM. When the network is down is exactly when the operator needs to see what is happening, so the local view must stand on its own.

Two writers, no added complexity — the same sink with a different address:

```
MQTT ──┬─► pg_writer (localhost)      short retention
       ├─► pg_writer (nikhef-vm)      full history, outbound only
       └─► jsonl_writer               archive, local
```

If the VM is unreachable only that one writer fails; it buffers and catches up, and nothing local notices.

### Reproducible installation

`sql/schema.sql` and the provisioned Grafana dashboards exist regardless of whether a VM is ever set up, because they make the **local** install reproducible. Grafana stores dashboards in its own internal database: without provisioning they are lost when that database is lost or Grafana is reinstalled, taking hours of work with them. With the schema and the dashboards in git, installation is a procedure rather than something that was once assembled by hand and can no longer be repeated. This is the same argument as for `channels.yaml`: configuration is data, not an action living in someone's memory.

**Dashboards are archived, not provisioned.** Provisioning refuses every save from the Grafana UI, and `allowUiUpdates` is read only when the service starts — which needs admin on this machine. Fighting that cost an afternoon, so Grafana owns the live dashboard and `grafana/dashboards-archive/` is the copy git tracks, with `tools/save_dashboard.py` moving one into the other (`--save`, `--check`, `--load`). The requirement above is that a dashboard survives losing Grafana’s database, and an archive plus a load command satisfies that as well as provisioning did.

What it does *not* satisfy on its own is that somebody remembers to run it. Drift is silent, and on 17 September 2026 both copies were edited at once — a panel setting into the archive file, a layout rearranged in the UI — so `--load` would have reverted the layout without a word. The system therefore reports it: `xams-ctl status` and the web UI overview both show whether the dashboards still match git, read through a **read-only (Viewer) service account** whose token sits in `secrets.yaml`. The check only ever looks; saving and loading still take the admin password, because those write. Grafana being down or unconfigured reports `unknown`, never `ok` and never drift — a check that cannot see is not a check that passed.

Installing anywhere — lab PC or VM — is then:

```bash
git clone <repo> /opt/xams-sc
psql -U postgres -d xams -f /opt/xams-sc/sql/schema.sql
# point Grafana provisioning at /opt/xams-sc/grafana/
```

and updating is `git pull`. A dashboard edited on the lab PC, exported to JSON and committed, appears on the VM at the next pull. No copying, no versions drifting apart, no deployment tooling.

`secrets.yaml` is in `.gitignore`, so it never travels with the repository; each machine keeps its own.

### Backups

`tools/backup.ps1` runs nightly from Task Scheduler and copies to the Nikhef
cluster. It copies **only what cannot be reconstructed**:

| Copied | Why |
|---|---|
| `data/raw/` | the measurement archive — the truth (§9.3) |
| `data/events/` | flight-recorder dumps: the ten minutes before an alarm (§9.1) |
| `data/quarantine/` | data deliberately set aside; small, and not reproducible |
| `data/fm101_total.json` | the integrator's running total (§7.5) — the one piece of state that cannot be recomputed |

Deliberately **not** copied: `data/imported/`, which is reconstructed from the
LabVIEW CSVs that still exist; **PostgreSQL**, accepted as expendable on
18 September 2026 because `meas` replays from the archive (§9.2); `logs/`,
useful for a week and worthless after; and `config/secrets.yaml`, because
credentials do not go on shared storage (§11).

Deciding what *not* to back up is the substance here. A backup that copies
everything is slow enough that it gets turned off, and it obscures which files
the system actually cannot lose.

### Logging

Python `logging` to rotating files, INFO by default, DEBUG selectable per service. Every log line carries the service name. Hardware errors log the raw instrument response, not just the parsed exception — that is what makes protocol bugs findable.

---

## 13. Testing

**Simulation mode.** Every device class accepts `simulate=True` and returns plausible synthetic data without hardware. The whole stack — bus, writers, alarms, UI — must be runnable on a laptop with no instruments attached. This is what makes development possible without occupying the lab PC.

**Deviation, milestone 1, 17 September 2026.** Simulation is *additionally*
implemented as a standalone service, `devices/sim.py`, which publishes
synthetic values for every enabled channel across all devices at once. The
reason is ordering: milestone 1 is built before any device class exists, so
there was nothing to pass `simulate=True` to, and the acceptance criterion
requires a service that publishes so the sinks, the dashboards and the install
can be exercised end to end.

`simulate=True` on each real device class still stands and is built with that
class, from milestone 2 onward. The two are complementary: `sim` exercises the
*whole channel map* without hardware, while `simulate=True` exercises *one
driver's* code path. Neither replaces the other, and `sim` is not a substitute
for testing a driver.

**Unit tests**, no hardware required. `pytest` runs the lot on a laptop:

| Test | Covers |
|---|---|
| `test_scaling.py` | `(raw - offset) * multiplier`, the recovered values for `p101`, `pmain`, `fm101`, and the HV sign convention (§7.2) |
| `test_config.py` | schema validation, duplicate channel names, unknown device references, config hash stability |
| `test_caen_protocol.py` | response parsing against recorded strings from **both** units, including malformed and truncated replies |
| `test_caen_link.py` | the three per-device liveness rules of §6.1 |
| `test_hv_status.py` | decoding the STATUS word: the switch is bit 10, the output is bit 0 (§10a) |
| `test_hv_control.py`, `test_hv_page.py` | the write path and its refusals, and what `/hv` shows |
| `test_cdaq.py`, `test_lakeshore.py`, `test_lakeshore_control.py` | driver logic against recorded or faked instrument responses |
| `test_integrator.py` | the flow total across restarts and gaps (§7.5) |
| `test_alarms.py`, `test_mail.py` | thresholds, hysteresis, dedup, staleness; and the rendered email |
| `test_pipeline.py` | a measurement from bus to sink, end to end |
| `test_grafana_drift.py` | the dashboard drift check reports `unknown` rather than `ok` when it cannot see (§12) |
| `test_manual.py` | the manual builds, and its internal links resolve |
| `test_cli_parser.py`, `test_webui_*.py` | the command grammar and the web UI's mutating actions |
| `test_no_undefined_names.py` | nothing references a name that does not exist |

**Hardware checklist**, run once per device on first connection:
1. Identity query returns the expected serial number
2. All configured channels read without error
3. Values are physically plausible
4. Disconnecting the device produces `quality=error` and a reconnect attempt, not a crash
5. Reconnecting recovers without a restart
6. Restarting the service changes nothing on the instrument

---

## 14. Documentation

Documentation is written **during** construction, one increment per milestone — not afterwards. Written at the end it records what the author remembers rather than what a reader does not understand, and by then everything has become obvious to the person who built it. That is how the LabVIEW project ended up without usable documentation, and it was not for lack of goodwill.

All of it lives in the repository, in git, beside the code.

| Document | Audience | Written |
|---|---|---|
| `docs/install.md` | someone reinstalling the system from scratch | milestone 1 |
| `docs/operating/` | the group, daily | grows with each milestone |
| `DESIGN.md` | someone changing the system | this document |
| Docstrings, and `description` in `channels.yaml` | someone reading the code or the config | as written |

The first two began life as `README.md` and `OPERATIONS.md` at the repository root and were moved into `docs/` when the manual was built, so that the whole of it is one searchable site served alongside the web UI. The names below are kept where they read naturally; *the operations document* is `docs/operating/`.

### The operations document

The most important of the four, and the one most often missing. Not architecture — actions:

- starting, stopping, checking status
- **what each alarm means and what to do about it**, per threshold
- replacing a sensor and adjusting its calibration
- adding a channel
- what to do when a service will not start, when a CAEN supply stops responding, when there is a gap in the data
- how to restore LabVIEW if the system has to be rolled back

The alarm point is not hypothetical. The thresholds of the present system exist in the VIs, but the *response* to each exists only in people's heads (§16). A threshold without a prescribed action is half the information, and reproducing that omission would waste the migration.

The troubleshooting section grows: every time something breaks and is fixed, it goes in. After a year it is the most valuable file in the repository.

### Acceptance

**Documentation is not accepted because its author considers it complete.** The test is that a colleague, using `docs/operating/` alone, can stop the system, add a channel and start it again — without asking the author anything.

If that fails, the document is unfinished however thorough it looks. This is also the only real mitigation for the bus-factor problem: a manual nobody has ever used proves nothing.

---

## 15. Milestones

Each milestone has an acceptance criterion. Do not start the next before the current one passes.

**This table is the criteria, not the state.** What is actually done is tracked in
[`docs/status.md`](status.md), which is updated as work lands; a criterion here is
edited only when the criterion itself changes. Where the two disagree, `status.md`
is right and this table has been missed.

| # | Milestone | Acceptance criterion |
|---|---|---|
| 1 | Skeleton | `BaseService`, config loading, MQTT bus, simulation mode. Fake service publishes, writers store, Grafana plots. No hardware. `README.md` lets someone else reproduce the install, and the install is scripted: `sql/schema.sql` plus provisioned Grafana dashboards. |
| 2 | cDAQ read-only | All connected channels (6 voltage + 14 RTD) read and logged, LabVIEW stopped. Values plausible. Restart changes nothing. |
| 3 | Channel verification | Each `tt*` tag confirmed empirically against its channel (warm a sensor, watch which value moves) and its physical location recorded. Tags are known; the mapping to hardware is what is being verified. |
| 4 | Scaling and history | Column-count check passed (§9.6), history imported, and scaled values agree with the LabVIEW record for the same sensors within expected tolerance. |
| 5 | Lake Shore + CAEN monitoring | Read-only. Identity verification working. Unplug test passes. |
| 6 | UPS, alarms, flow integrator | Thresholds from `alarms.yaml`, SMS and email delivered, staleness alarms fire, flight-recorder dump produced on a test alarm. Integrator survives a service restart without losing its total. |
| 7 | Web UI | Current values, alarm state, service health. Read-only, bound to `127.0.0.1`. Python client works from a notebook. The P&ID mimic (§8.2) shows live values on the drawing, greys stale channels, and the SVG-vs-`channels.yaml` tag check passes in both directions. |
| 7a | History from the mimic | Clicking a value on the P&ID opens that channel's plot (§8.2a). One `$channel` dashboard, committed to `grafana/dashboards-archive/` and reported `ok` by the drift check. Steps 1–5 of §8.2a; the embedded panel (steps 6–7) is optional and may lag. |
| 8 | Control path | **Lake Shore setpoint and heater range: DONE** (18 Sep 2026) — validated, read back, acknowledged, audited. **HV: DONE** (18 Sep 2026) — `VSET` and energising from `/hv` and `xams-ctl`, the invariant of §10a holding on both boards. What remains is the **server-side retained plan** of §10a: setpoints are staged in the page's own boxes, so a second browser sees nothing and a reload discards them. |
| 9 | Procedures | Named sequences (§10) run, abort cleanly, and are audited step by step. |
| 10 | Production | `SERVICE_AUTO_START`, LabVIEW retired but installed. **`docs/operating/` passes the acceptance test of §14** — a colleague operates the system from it unaided. |

Milestone 3 is the one that cannot be rushed. The tag names are now known, but a tag that was already attached to the wrong channel in the LabVIEW system would be copied across silently. Verifying each one physically is the only way to catch that.

---

## 16. Open items

Everything marked **TBD** above, consolidated:

| Item | Needed for | How to resolve |
|---|---|---|
| ~~Physical location of each `tt*` tag, and what the 1xx/2xx/3xx series denote~~ | — | **Resolved 17 September 2026.** Locations recorded in `channels.yaml`; series meanings in §3. |
| **Empirical per-sensor verification** — warm each sensor, confirm which channel moves | milestone 3 | Readings were checked against LabVIEW on 17 Sep 2026 and agree, but that cannot catch a mislabelling inherited *from* LabVIEW, which is the failure §15 names. Independent corroboration so far: three inlet/outlet pairs all read the correct sign (§7.1). |
| ~~`tt202` reads open-circuit — never installed, or failed?~~ | — | **Resolved 17 September 2026: the sensor has failed.** Now `enabled: false` — see the repair item below. |
| **Replace the `tt202` sensor** (bottom of the detector vessel) | — | hardware repair. Then set `enabled: true` in `channels.yaml`; nothing else changes. |
| **Replace the NI 9226 readout unit** (PT1000, `tt201`–`tt207`) | — | **Failed 28 September 2026**: every channel reads open-circuit (1326.8 °C). `tt201` and `tt203`–`tt207` are `enabled: false`, so the alarm engine ignores them and withdraws their alarms. Then remove those six `enabled: false` lines and restart `cdaq`; `tt202` stays off until its own sensor is replaced. |
| Alarm thresholds and responses | ongoing | **Partly done, 17 September 2026:** `pmain` (0.9 / 2.1 / 2.5 bar), `tt104` (60 / 65 °C), `tt302` (30 / 35 °C) and three UPS channels, all supplied by A.P. Colijn. Thresholds for the remaining channels, and the *response* to each, are still to be written into `OPERATIONS.md` — a threshold without a prescribed action is half the information (§14). |
| ~~HV channel → supply/index mapping, and which board is `hv_1` vs `hv_2`~~ | — | **Resolved 17 September 2026.** Full map in §7.2, cross-checked against the polarity reported by each channel. |
| ~~HV channel identity verification~~ | — | **Working, 17 September 2026.** Both boards resolved by `BDSNUM` over enumerated ports, unplug test passes, and a reconnect re-runs the full identity check (§6.2 rule 6). |
| HV setpoints, ramp rates, trip limits | milestone 8 | **Read from the boards, §7.2.** What remains is confirming they are intended rather than inherited — in particular the `RUP` of 1 V/s and `TRIP` of 2.0 µA on `hv_pmt_bot`. |
| ~~HV sign convention: signed values or magnitudes?~~ | — | **Settled: SIGNED**, implemented in `scaling.apply_sign` and enforced by tests. The cathode reads negative and the anode positive, so the two cannot plot as though they were alike. |
| ~~USB serial numbers of the two CAEN units~~ | — | **Resolved 17 September 2026.** The units carry no USB serial number at all; board serials `19198` and `79` read via `BDSNUM` and recorded in §4.1. |
| ~~Lake Shore baud rate~~ | — | **Resolved 17 September 2026: 57600 baud, 7 data bits, ODD parity, 1 stop bit.** 7-O-1 is the factory setting and is not a typo — at 8-N-1 the port opens and the instrument returns nothing intelligible, which looks like a dead instrument rather than a wrong setting. |
| ~~Lake Shore sensor units~~ | — | **Celsius, not Kelvin.** An earlier draft of `channels.yaml` said K; the imported history then showed these channels ranging to −90, and there is no negative Kelvin. Confirmed against the instrument: `CRDG? A` = −89.998 and `KRDG? A` = +183.15 describe the same temperature. The driver reads `CRDG?`. |
| ~~UPS model and connection~~ | — | **Resolved 17 September 2026.** APC, serial 3S2005X18782, read from its USB HID **alongside PowerChute** (§7.4). WMI reports no battery at all, and `GetSystemPowerStatus` describes the wall socket rather than the UPS — both were tried and rejected. |
| ~~Are `9207/ai8:15` current channels used?~~ | — | **Resolved 17 September 2026: not used.** Superseded 22 September 2026: `ai8`/`ai9` now carry the strain gauges `sg101`/`sg102` (§7.1); `ai10:15` remain unused. |
| What do `sg101` and `sg102` actually weigh? | A.P. Colijn | Specified to the software only as "2x 4-20 mA strain gauge, 0-20 kg". The channel descriptions say so rather than guess, and the names are permanent (§3) — the description is not. No alarm thresholds either, so only staleness applies to them. They ARE on the P&ID: both tags were already drawn, with bubbles (§8.2). |
| Are `sg101`/`sg102` forward-acting, and is the span really 4-20 mA? | A.P. Colijn | Taken from the request that added them, not from a datasheet or a reading. Direction is the part that matters: reverse-acting gives the complement of every weight (§7.1). |
| ~~Engineering unit for `pmain`~~ | — | **Resolved 17 September 2026: bar.** Established from the alarm limits supplied by A.P. Colijn, not guessed. |
| ~~Engineering units for `p101`–`p104`~~ | — | **Resolved 17 September 2026: bar**, supplied by A.P. Colijn along with their locations (gas rack high/low pressure side, pump inlet, pump outlet). No channel now carries `unit: TBD`. |
| Purpose of `anode_timing.vi` | milestone 8 | read the block diagram |
| ~~Purpose of `DAISY_polarity_signs.vi`~~ | — | **Explained 17 September 2026**, near-certainly: the supplies report unsigned magnitudes with `POL` separate, so the sign must be applied in software (§7.2). Confirm against the block diagram when convenient. |
| ~~Heater shut-off: hardware or software?~~ | — | **Decided 18 September 2026 (§10):** software, on `pmain` hihi alone, latching, loud on failure, audited. **Still to build** — see the note in §10. |
| **`KILL VOLTAGE`** — the CAEN equivalent | milestone 8 | decision. Not settled by the heater decision above, and distinct from an orderly "everything to standby" (§10a). |
| ~~The `/recipients` page~~ | — | **Built 20 September 2026**, as a section of `/alarms` rather than a page of its own (§8.1). |
| **The Python client** `api/client.py` — milestone 7's "works from a notebook" | milestone 7 | build. Reading from PostgreSQL with `pandas.read_sql` covers most of it today (§9.2). |
| **`tools/jsonl_to_parquet.py`** (§9.4) | — | build. The JSONL archive is complete, so this is compaction, not a gap in the record. |
| **The shared staged HV plan** (§10a) | — | build. Setpoints stage in the page's own boxes, so a second browser sees nothing and a reload discards them. Costs little while one person operates the supplies. |
| ~~How far back the CSV history goes, and whether the column count is constant throughout~~ | — | **Resolved 17 September 2026, and it is not constant.** 733 log files from 2023-02-21; **9 distinct column counts** whose date ranges interleave, and 304 header files containing **90 distinct layouts** (1 to 2269 columns). The current header describes 62 columns for data that has 47. `tools/import_labview_csv.py` therefore ignores the headers entirely and refuses any file whose layout it has not confirmed. |
| ~~Whether the P&ID of 17 May 2024 is still current~~ | — | **Confirmed current, 17 September 2026**, before the mimic was built on it. |
| **Anonymous viewer access in Grafana** — needed only for the embedded panel on the mimic (§8.2a step 7) | milestone 7a | decision. It grants read access to every dashboard to anyone who can reach the lab PC, which today is anyone who can already open Grafana (§8). It stops being harmless the moment Grafana leaves loopback. The link-in-a-new-tab form needs none of this and is the fallback if the answer is no. |
| Second maintainer | production | decision |
| **Nikhef VM watchdog** — one Grafana rule, "no measurement for 15 minutes" | production | §12. The only failure this system cannot report is its own machine being off, and auto-start makes that *less* likely to be noticed rather than more. |
