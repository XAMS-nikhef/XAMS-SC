# Configuration files

Everything the system knows about the hardware is in `config/`, in git.
**Adding a sensor touches no Python.**

| File | What it defines | How it is changed |
|---|---|---|
| `channels.yaml` | every channel: address, scaling, unit, write range | edit, commit, `xams-ctl reload` |
| `devices.yaml` | how each instrument is found and identified | edit, commit, `xams-ctl reload` |
| `alarms.yaml` | thresholds and severity routing | edit, commit, `xams-ctl reload` |
| `recipients.yaml` | who gets notified | the web UI at `/alarms`, or by hand — no restart |
| `hv_defaults.yaml` | the HV setpoints `load defaults` offers | the web UI at `/hv/defaults`, or by hand — no restart |
| `secrets.yaml` | credentials | by hand; **never committed** |

Three conventions that must not be changed:

- **Scaling is `value = (raw - offset) * multiplier`**, in that order. It
  matches LabVIEW exactly. Changing it breaks every historical comparison.
- **HV values are stored signed.** The supplies report unsigned magnitudes with
  polarity separate; the sign is applied in software. Changing this later
  silently inverts history.
- **Channel names are permanent.** They are the identity of a measurement in
  the MQTT topic, the archive, the database and the UI.

Every file is validated at load. A typo stops a service at startup with a
message naming the problem, rather than becoming a wrong number six months
later — run [`xams-ctl check`](../reference/cli.md#check) to validate without
starting anything.

---

## `channels.yaml`

One entry per channel. Only `name`, `device`, `phys` and `kind` are required.

```yaml
  - name: p101
    device: cdaq
    phys: 9207/ai0
    kind: voltage
    unit: bar
    offset: 1.0
    multiplier: 25.0
    legacy: P101
    description: gas rack high pressure side
```

| Field | |
|---|---|
| `name` | **the identity of the measurement.** MQTT topic, archive record, database row, UI label. Renaming one breaks history |
| `device` | which service reads it: `cdaq`, `hv_1`, `hv_2`, `lakeshore`, `ups`, `turbo`, `rga`, `scale`, `derived` |
| `phys` | where it is, in that device's own terms — a module alias and line, an HV channel index, a HID usage, a Pfeiffer parameter number |
| `kind` | `voltage` · `rtd` · `temperature` · `hv_vmon` · `hv_imon` · `hv_stat` · `hv_vset` · `status` · `power` · `setpoint` · `current` · `weight` |
| `unit` | engineering unit as published. Declared, never inferred |
| `offset`, `multiplier` | the scaling, in that order. Default 0 and 1 |
| `sign` | `+1` or `-1`. Applied to the unsigned magnitudes the CAEN supplies report |
| `rtd` | RTD type and wiring for a cDAQ RTD channel |
| `limits` | `{min, max}` — the **software write range** |
| `derive` | compute this channel from another: `{from, transform, …}`, for relationships that are not linear |
| `default_setpoint` | what the HV page offers as "load defaults". Offered, never applied. Overridden by [`hv_defaults.yaml`](#hv_defaultsyaml-the-operating-point) |
| `legacy` | the LabVIEW name, for `tools/compare_to_labview.py` |
| `enabled` | `false` leaves the channel defined but unread. Listed, not deleted |
| `log_minmax` | also store the window's min and max, not only the mean |
| `on_pid` | whether the channel belongs on the [P&ID mimic](webui.md). `false` for a reading with no place on a piping drawing |
| `description` | one line, shown in the UI and in the generated table |

**`limits` is convenience, not protection.** The instrument's own limit
applies underneath and always wins. A channel with **no** `limits` accepts
nothing: a write range that was never specified is not permission to write
anything.

**A disabled channel is listed, not removed.** The map stays complete and a
gap is never left ambiguous — `v4` and `v6` are in the file and say *not
connected*.

---

## `devices.yaml`

How each instrument is found and how it proves it is the right one.
**Resolution never uses a COM number.** Candidates are narrowed by USB
hardware ID, then the instrument is asked who it is — which for the CAEN
supplies is the *only* identification, since neither carries a USB serial
number and both present the same VID/PID.

```yaml
  - id: hv_1
    match: {vid: "21E1", pid: "0003"}
    board_name: DT1470ET
    board_serial: "19198"
    baud: 9600
    expect:
      0: {pol: "-", maxv: 1100, rup: 1, rdw: 20, trip: 2.0, iset: 20}
```

| Field | |
|---|---|
| `match` | the USB hardware id: `vid`, `pid`, and `serial` where the device has one. Ports that do not match are **never opened** |
| `board_name` + `board_serial`, `idn_contains` | the identity the instrument must report. Matched as a **pair**, never on the serial alone |
| `baud` and friends | serial settings. The Lake Shore's 7-O-1 is not a typo — see [its page](../drivers/lakeshore.md) |
| `expect` | the protection settings this system **reads and alarms on, and never writes** |
| `chassis`, `modules` | the cDAQ chassis and its modules, by alias, model, slot and serial |

`expect` is the shape of §10 rule 2: ramp rate, trip current and over-voltage
limit are configured on the instrument and stay there. The software compares
and complains; it has no code that could change them.

---

## `alarms.yaml`

Thresholds, four per channel, EPICS-style — `lolo`, `low`, `high`, `hihi` —
each with a severity and who to notify. Plus `defaults` (hysteresis, repeat
interval, `stale_after_seconds`), a `staleness` block, `switchable` alarms and
`unreadable` groups ([one alarm for a group of channels that may be
unreadable for a while](alarms.md#unreadable-groups-one-alarm-later-quieter),
the RGA's).

Editing a threshold silently changes what the system protects against, which
is why this file is in git and applied with an explicit `xams-ctl reload`.
The current contents are published as [the alarm
table](../reference/alarms.md); the engine's behaviour is on [Alarm
engine](alarms.md).

---

## `recipients.yaml`, and why it is the odd one out

Everything else here is edited in a text editor, committed, and reloaded.
This one is edited **from the web UI — the [Alarms](../operating/webui.md)
page** — and takes effect without restarting anything.

The difference is what the file is. A threshold is an engineering decision:
it should be reviewable, attributable and revertible, so it belongs in git.
A phone number is not — it is a fact about who is on shift this month, it
changes when somebody goes on leave, and it has to be changeable at the
moment somebody notices that alarms are going to a person who left. Putting
that behind an edit-commit-reload cycle means the list is wrong on exactly
the weekend it matters.

So the recipients list is read at **send time**, not at startup: a change
applies to the next alarm, with no restart and no reload. Changes are
[audited](storage.md) like any other write — including removals, so it stays
recoverable who would have been notified when a given alarm fired.

What the page refuses, and why each one is a way to be notified by nothing:

| | |
|---|---|
| neither an email nor a phone | **allowed**, with a warning naming the row: they sit on the list looking notified and hear nothing |
| a phone without `+` and a country code | the gateway wants international form; better refused here than at 3am |
| a name listed twice | two rows, one person, and no way to tell which one is current |
| nobody enabled | **allowed**, with a warning. It may be deliberate during an intervention |

An **empty phone is not a mistake** — it means *do not SMS this person*, and
they are notified by email alone.

---

## `hv_defaults.yaml`, the operating point

The values **Load defaults** fills into the boxes on `/hv`. Edited from
**`/hv/defaults`** — the *Edit defaults…* button beside *Load defaults* — or by
hand.

```yaml
updated: 2026-09-20T14:02:11+00:00
by: apc
defaults:
  hv_cathode_vset: -2250.0
  hv_anode_vset:   4200.0
```

The second file edited from the web UI, and for the same kind of reason as
`recipients.yaml`: this is an R&D setup, the operating point moves, and putting
a number that changes weekly behind an edit-commit-reload cycle means the
defaults on the page are the ones from a month ago.

A channel that is not named keeps its `default_setpoint` from `channels.yaml`,
so this is an override and a fresh install needs no such file.

**Signed volts**, as everywhere: the cathode, gate and screens negative, the
anode and NaI positive.

!!! warning "The write range is *not* edited here"
    `limits` stays in `channels.yaml`, hand-edited and committed, because it is
    the range **every write to an electrode is checked against**. A page that
    could widen its own limit and then write to it is not a limit.

    A default outside its channel's `limits` is refused — by the page before it
    writes anything, and again by every service that loads the file. The
    refusal names the channel and tells you to change the limit in
    `channels.yaml` if that is really what you want.

**Nothing here reaches an instrument.** Saving changes which number appears in
a box. A person still presses *Apply setpoints*, and that write is validated,
read back and audited exactly as before — which is why this is safe to edit
from a web page when an alarm threshold is not.

Every change is [audited](storage.md): who, when, from, to. The file is tracked
by git, so `git status` shows it as modified and you commit it with everything
else; the web UI does not run `git` itself.

---

## `secrets.yaml`

Database DSN, the SMS gateway key, the Grafana token, SMTP credentials.
**Never committed** — `secrets.example.yaml` is the template in git, with
empty values.

A key committed to git stays in the history after it is deleted, and private
repositories are still cloned, shared and backed up. Absent secrets is a
normal state rather than an error: without a DSN the archive still runs, and
without a Grafana token the [drift check](../grafana/drift.md) reports
`unknown` instead of complaining.

---

## The config hash

Every load computes a hash over the YAML. It is stamped into the header of
each JSONL file, logged at service start, and shown on the System health page — so
an archive file says what configuration produced it, and two services running
different configurations are visible rather than mysterious.

## Generated references

The current contents of two of these files are published as part of this
manual, straight from the YAML: [the channel table](../reference/channels.md)
and [the alarm table](../reference/alarms.md). They are regenerated at every
build, so they cannot drift.
