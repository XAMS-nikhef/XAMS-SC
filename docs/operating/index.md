# Operating

**How to run the system, and what to do when it misbehaves.** Not architecture
— that is [the design specification](../DESIGN.md). Not installation from
scratch — that is [Installation](../install.md).

This document **grows with each milestone** (§14). It is deliberately written
during construction rather than afterwards: written at the end it would record
what the author remembers rather than what a reader does not understand.

**The acceptance test for this document** (§14): a colleague, using these pages
alone, can stop the system, add a channel and start it again — without asking
anyone. If that fails, the document is unfinished however thorough it looks.

| | |
|---|---|
| The interface you use every day, tab by tab | [The web interface](webui.md) |
| Starting, stopping, handing the hardware back | [Starting and stopping](running.md) |
| Adding a channel, importing history, saving a dashboard | [Routine tasks](tasks.md) |
| Something is broken | [Troubleshooting](troubleshooting.md) |
| Backups | [Backup and restore](backup.md) |
| Alarm mail and the daily report | [Email and reports](email.md) |
| Taped to the rack | [Emergency card](emergency.md) |

---

## Every day

Open <http://127.0.0.1:8000>. It lands on **P&I** — the plant drawn, with every
reading in the place it physically belongs. The badge in the header is the
whole system in one word, on every page; the **System health** tab is the
detail behind it. What each tab shows is in [The web interface](webui.md).

The same picture from a terminal, in
`C:\Users\localadmin\Documents\XAMS-SC`:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status
```

This shows each service, whether it is running, and how long ago it last sent a
heartbeat. Heartbeat ages come from the **retained MQTT topics, not the
database** — the status view works even when PostgreSQL does not, which is
precisely when it is needed.

**Every service publishes a heartbeat**, `sinks` and `alarms` included, so
silence means the same thing for any of them. Those two used to be exempt,
which left the service that writes the archive and the service that telephones
people as the two the status view could never show as broken.

For history and plots: Grafana on <http://127.0.0.1:3000>, the **XAMS Overview**
dashboard.

### What healthy looks like

- every service running, every heartbeat a few seconds old — and **none of
  them `degraded`**, which for the sinks means they have had to discard data
  ([when the database is down](../software/storage.md#when-the-database-is-down));
- the **P&I** updating: if it has stopped it says **NOT UPDATING** across the
  top of its column and greys the drawing out, so a live-looking screen really
  is live;
- the **Channels not reading OK** card absent, and Grafana's equivalent panel
  **empty** — that emptiness is the point, because a frozen plausible value is
  worse than a gap;
- no active alarms, or only ones somebody is already dealing with;
- backup `ok` and dashboards `saved to git` on the System health page.

---

## What this system will and will not do to the hardware

**It writes to three instruments.** The Lake Shore heater — setpoint and range, on
output 1 — both CAEN supplies: `VSET` per channel, and energising a channel
on or off — and the residual gas analyser: control, scan setup, start and stop,
and emission On/Off behind a p105 guard, from the [RGA page](../drivers/rga.md).
It never touches the analyser's electron multiplier or ion source. How to drive either is in
[The web interface](webui.md#control); the same commands exist as
`xams-ctl hv-set`, `hv-standby`, `hv-on`, `hv-off` and `flow-reset`.

Every write is range-checked against `channels.yaml`, **read back from the
instrument** before it counts as successful, acknowledged on the bus and
recorded in the audit log with the old value, the new value and who did it —
including the writes that were refused.

**Two hand gates stay out of software's reach.** A board must be in `REMOTE` at
its front panel, and a channel must be enabled at its front panel. No command
exists for either. Hence §10a's invariant — **a disabled channel holds `VSET`
0** — which is what makes flipping an enable switch safe: it always brings a
channel up at zero volts.

**Nothing else actuates.** The cDAQ has no output module; the UPS is read, not
commanded; the board's protection settings (`MAXV`, `RUP`, `RDW`, `TRIP`,
`ISET`) are displayed and alarmed on, never written. Nothing actuates on startup
or restart. There is no automatic actuation anywhere — the decided heater cut on
a `pmain` hihi (§10) is specified but not built, `KILL VOLTAGE` is open, and
named procedures are milestone 9.

**This is not a protection system.** Interlocks belong in hardware, wired from a
real gauge trip (§10 rule 1).

---

## What each alarm means

The thresholds live in `config\alarms.yaml` and what fires is described in
[the alarm engine](../software/alarms.md); who gets told, and how, is in
[Email and reports](email.md).

**Still missing: the prescribed response to each threshold.** The numbers of the
old LabVIEW system exist in the VIs, but the response to each exists only in
people's heads, and a threshold without a prescribed action is half the
information (§14). Filling that in, channel by channel, is outstanding work —
see [Project status](../status.md).

---

## Reference

| Thing | Where |
|---|---|
| Configuration | `config\*.yaml` — in git, except `secrets.yaml` |
| Credentials | `config\secrets.yaml` — **never committed** |
| Archive | `data\raw\*.jsonl` |
| Logs | `logs\<service>.log`, rotating |
| Broker | `127.0.0.1:1883` |
| PostgreSQL | `127.0.0.1:5432`, database `xams` |
| Grafana | <http://127.0.0.1:3000> |
| Web UI | <http://127.0.0.1:8000> |

**Everything binds to loopback, deliberately.** Commands travel over MQTT, so
anything that can reach the broker can change a setpoint. Remote viewing is
[the Nikhef VM](../vm.md), a read-only mirror — never opening the
control port (§8).
