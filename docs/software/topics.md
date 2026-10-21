# Topic contract

This is the interface every driver and every sink agrees on. A driver
publishes here and knows nothing else; a subscriber reads here and knows no
driver. Changing anything on this page changes the contract between all of
them, which is why it has a page rather than a paragraph.

Source: [the design specification](../DESIGN.md) §3 and `src/xams_sc/bus.py`.

---

## The topics

| Topic | Payload | Retained |
|---|---|---|
| `xams/meas/<channel>` | a measurement, JSON | **yes** |
| `xams/status/<service>/heartbeat` | ISO-8601 UTC timestamp | **yes** |
| `xams/status/<service>/state` | `starting` · `running` · `degraded` · `stopped` | **yes** |
| `xams/alarm/<channel>` | alarm state change, JSON | **yes** |
| `xams/cmd/<device>/<action>` | a control request, JSON | no |
| `xams/ack/<device>/<action>` | what the service did about it, JSON | no |
| `xams/audit` | one record per write, JSON | no |
| `xams/flow/period` | the closed flow period, JSON | no |
| `xams/backup/status` | the nightly backup's last result | **yes** |
| `xams/rga/info` | the RGA's identity, controlling client and acquisition, JSON; for the RGA page, never archived ([RGA](../drivers/rga.md)) | **yes** |
| `xams/rga/scan` | the latest scan of a running RGA acquisition, JSON; for the live plot, never archived | **yes** |
| `xams/rga/saved` | the summary of one RGA Save, for `rga_summary` | no |
| `xams/status/limits` | the alarm thresholds in force, JSON ([alarms](alarms.md)) | **yes** |
| `xams/status/notify` | the master alarm switch: on/off, by, at | **yes** |
| `xams/status/armed` | every switchable alarm's own switch (alarms.yaml `switchable`) | **yes** |

`<channel>` is a name from [`channels.yaml`](config.md). `<service>` is a
service name — `cdaq`, `caen`, `lakeshore`, `ups`, `turbo`, `rga`, `scale`,
`derived`, `sinks`, `alarms`, `webui`.

**Retained is a deliberate choice, not a default.** It is what lets a page, a
report or a newly started service know the current state of the plant the
moment it connects, instead of waiting up to ten seconds for the next
reading. The web UI and the daily report are both built on this and neither
touches the database.

It also has a consequence worth knowing: **every subscriber receives the last
value of every channel again each time it reconnects**, with the original
timestamp. That is why the `meas` table has a unique index — see
[Storage](storage.md#duplicates-are-normal).

---

## The measurement payload

Short keys, because this is written roughly 260,000 times a day.

```json
{"t":"2026-09-18T13:27:45.983Z","ch":"tt301","v":-92.4,"u":"C","q":"ok"}
```

| Key | |
|---|---|
| `t` | ISO-8601 **UTC**, milliseconds, `Z`-suffixed. Taken at the moment of the hardware read, never at publish time |
| `ch` | channel name |
| `v` | value in engineering units, or `null` when there is none |
| `u` | unit, as `channels.yaml` declares it |
| `q` | `ok` · `stale` · `error` · `unverified` |
| `raw` | the pre-scaling value, when the driver has one. Omitted otherwise |
| `src` | provenance. Omitted when it is `xams` |

**`t` is the read time on purpose.** A timestamp applied at publish silently
absorbs every delay in the path between, and that is exactly the error that
makes two channels look correlated when they are not.

### Quality

| | |
|---|---|
| `ok` | read, scaled, trustworthy |
| `stale` | not refreshed within `stale_after_seconds`, or an aggregate containing a bad sample |
| `error` | the read failed. `v` is `null` — never the last good number |
| `unverified` | the device has not confirmed its identity (§6.2) |

A channel that cannot be read is published with `quality=error` rather than
left at its last value. The whole of principle 4 lives in that sentence.

### Provenance

`src` says where a number came from, and it travels **on the measurement**,
not on the writer, because one writer serves every service.

| | |
|---|---|
| `xams` | read from an instrument by this system. The default |
| `sim` | **synthetic**, from simulation mode. Not a measurement |
| `labview` | imported from the LabVIEW history (§9.6) |

Without this, simulated values land in the permanent archive
indistinguishable from real ones — which happened on 17 September 2026 and
was undone by deleting rows from the database. A value that is not a
measurement must never be able to pass as one.

---

## Status and heartbeats

Every service publishes a heartbeat and a state, both retained, on the same
cadence as its measurements. Two things consume them: the Services card in
[the web UI](../operating/webui.md) and `xams-ctl status`.

```
xams/status/lakeshore/heartbeat   2026-09-18T13:27:45.983Z
xams/status/lakeshore/state       running
```

**`stopped` is published by the broker, not by the service.** Each connection
sets an MQTT *last will* on its state topic, so a service that is killed, or
whose machine loses power, is reported as stopped by the broker on its
behalf. A service that could only report its own death would never report the
deaths that matter.

Every service publishes a heartbeat, `sinks` and `alarms` included. They did
not until recently, on the reasoning that neither is a `BaseService` — which
left the archive and the notifier as the two things the Services card could
never show as broken, and meant a process that HUNG rather than exited kept a
retained `running` next to its name indefinitely. A frozen plausible value is
worse than a gap (§1, principle 4).

---

## Commands and acknowledgements

Every topic that makes an instrument *do* something has an acknowledgement
beside it, and the list is short on purpose (§10, §10a).

| Command | Acknowledgement | What it does |
|---|---|---|
| `xams/cmd/lakeshore/setpoint` | `xams/ack/lakeshore/setpoint` | temperature setpoint, output 1 |
| `xams/cmd/lakeshore/range` | `xams/ack/lakeshore/range` | heater range, output 1 |
| `xams/cmd/caen/vset` | `xams/ack/caen/vset` | one HV setpoint |
| `xams/cmd/caen/output` | `xams/ack/caen/output` | energise or de-energise one HV channel |
| `xams/cmd/caen/clear` | `xams/ack/caen/clear` | clear a latched trip on one supply |
| `xams/cmd/derived/flow_reset` | `xams/ack/derived/flow_reset` | close the flow period, open a new one |
| `xams/cmd/all/reload` | `xams/ack/<service>/reload` | re-read the YAML |
| `xams/cmd/rga/control` | `xams/ack/rga/control` | take or release the RGA (`force` only when an operator chose it) |
| `xams/cmd/rga/scan` | `xams/ack/rga/scan` | start a recipe, or stop the scan |
| `xams/cmd/rga/save` | `xams/ack/rga/save` | write the last acquisition to its file |
| `xams/cmd/rga/emission` | `xams/ack/rga/emission` | RGA emission On or Off; On only past the p105 guard |
| `xams/cmd/rga/em` | `xams/ack/rga/em` | RGA electron multiplier On or Off |
| `xams/cmd/alarms/notify` | `xams/ack/alarms/notify` | the master alarm switch (delivery only) |
| `xams/cmd/alarms/arm` | `xams/ack/alarms/arm` | one switchable alarm on or off (delivery only) |

**The rules that apply to all of them:**

- The service that owns the port validates, writes, **reads back**, and only
  then calls it successful. An acknowledgement reports what the instrument
  did, not what it was asked.
- A refusal is acknowledged with a reason and audited. A command is never
  silently dropped — what somebody *tried* to put on an electrode is worth as
  much afterwards as what they managed to.
- Every command carries `by`: who is doing this. It reaches the audit record.
- Nothing here can enable a CAEN channel or change a protection setting.
  Those stay hand operations at the instrument (§10 rule 2).

A caller publishes the command and waits for the matching ack — that is how
the web UI and the CLI both work, and it is why they get identical treatment.

---

## Audit

```
xams/audit    {"t":…,"actor":"AP","action":"hv_vset","target":"hv_cathode_vset",
               "old_value":"-2250.0","new_value":"-2300.0","result":"ok"}
```

Published rather than written, so the record survives the database being
down: it is in the JSONL archive either way. The
[audit sink](storage.md) stores it into a queryable table, in the same
relationship as `meas` to the archive.

---

## Outages

The bus is a shared dependency, so an outage must cost a **visible** gap and
never a silent one.

- Services keep reading their instruments while the broker is unreachable and
  buffer in memory — roughly five minutes' worth, then the oldest go.
- What is dropped is **counted and logged as an error**, not passed over.
- On reconnect the buffer is republished in order.
- While the broker is down each service logs a warning every 30 seconds
  saying that nothing is being stored, because an outage that produces no log
  line looks exactly like a healthy system with nothing to say.

The buffer is bounded deliberately: an unbounded queue turns a broker outage
into a memory leak, which is a worse failure than the one it was guarding
against.
