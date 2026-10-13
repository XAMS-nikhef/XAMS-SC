# Grafana

<http://127.0.0.1:3000> → **XAMS Overview**. History and plots; everything live
is on [the web interface](../operating/webui.md) instead.

Grafana reads PostgreSQL and nothing else. **It is a view, never a source:** a
dashboard can be deleted and rebuilt without touching any data, and PostgreSQL
itself is only an index over the JSONL archive. Nothing is lost by breaking a
panel, which is worth remembering before editing one.

| | |
|---|---|
| I want to add a channel to a graph | [Editing dashboards](dashboards.md) |
| It says my dashboards are not saved to git | [Drift](drift.md) |
| Every panel says "No data" | [Troubleshooting](../operating/troubleshooting.md#grafana-and-the-database) |

---

## What is on the Overview dashboard

Ten panels, seven of them time series:

| Panel | Selects | Unit |
|---|---|---|
| Temperatures — detector | `tt2%` | °C |
| Temperatures — gas system & ambient | `tt1%`, `tt3%`, `tt4%`, `ttamb` | °C |
| Pressures | `pmain`, `p101`–`p104` | bar |
| Vacuum — P105 (PKR) and turbo speed | `p105` (left, **log** axis), `turbo_speed` (right, dashed) | mbar, Hz |
| Flow (fm101) — mass flow | `fm101` | g/min |
| High voltage — VMON (signed) | `hv_%_vmon` | V |
| High voltage — IMON | `hv_%_imon` | µA |
| Active alarms | latest state per channel, anything not `ok` | |
| Alarm history | `alarm_events` over the range | |
| Channels not reading OK | latest row per channel with `quality <> 'ok'` | |

**Clicking P105 on the P&ID** opens the Channel dashboard, which draws `p105` on a log axis (a field override for that one channel); every other channel keeps its linear axis.

**The 1xx / 2xx / 3xx series mean something**: xenon circulation, detector vessel
and bucket, cooling and heat exchange (§3). That is why most panels select with
`LIKE` — a new sensor named to the convention appears on the right graph with no
edit at all.

Default range is the last 15 minutes, refreshing every 10 s, in
`Europe/Amsterdam`. Timestamps are stored UTC and converted for display.

**Voltages are signed.** The supplies report unsigned magnitudes with polarity
as a separate parameter; the sign is applied once, in software, before storage
(§7.2). So the cathode plots negative and the anode positive, which is what you
want on one graph.

---

## The datasource

Provisioned from `grafana/provisioning/datasources/xams.yaml`, name **XAMS**,
uid `xams-postgres`, connecting to `127.0.0.1:5432` as the `xams` role. The
password comes from an environment variable and never enters the repository.

Datasources *are* provisioned; dashboards are not. Nothing edits a datasource by
hand, so it has none of the friction described below.

!!! warning "`database` is set in two places, and both are needed"
    Recent PostgreSQL datasource versions read the database name from
    `jsonData.database`; the legacy top-level `database:` is still honoured by
    the **backend**. With only the top-level field set, the health check reports
    *"Database Connection OK"* and server-side queries return data — while the
    query editor reports *"You do not currently have a default database
    configured"* and issues nothing at all. Every panel then shows **No data**
    with a red marker and **nothing appears in the Grafana log**, because no
    request is ever made. Do not remove either field.

Provisioned files are re-read only when the service starts:

```powershell
Restart-Service Grafana
```

---

## Why dashboards are not provisioned

Provisioning refuses every save from the Grafana UI — *"This dashboard cannot be
saved from the Grafana UI because it has been provisioned from another
source"* — and `allowUiUpdates`, the setting that would permit it, is read only
when the service starts, which needs administrator rights on this machine.
Editing a dashboard is a daily action and must not depend on a service restart.

So the arrangement is:

```
  Grafana owns the live dashboard, and the UI edits and saves it freely.
  grafana/dashboards-archive/   is the copy git tracks.
  tools/save_dashboard.py       moves one into the other.
```

§12 asks that a dashboard survive losing Grafana's own database. An archive in
git plus a load command satisfies that exactly as well as provisioning did, and
does not make every edit a fight. What it does not satisfy on its own is that
somebody remembers to run the save — so the system checks, and says so: see
[Drift](drift.md).

---

## First install

Grafana is installed, bound to loopback and given its datasource by
`tools\setup_services.ps1`; see [Installation](../install.md). First login is
`admin` / `admin`, changed when asked.

On a machine whose Grafana database is empty — a reinstall, or a fresh clone —
the dashboards come back from git:

```powershell
.\.venv\Scripts\python.exe tools\save_dashboard.py --load --password <admin pw>
```

That is the only direction that needs the admin password, and the only time this
command should be run.

**Grafana binds to `127.0.0.1` only**, like everything else here. If remote
viewing is wanted, the answer is a read-only mirror of the data — never opening
a port (§8). That mirror exists: [the Nikhef VM](../vm.md), with its own Grafana
and the same dashboards.
