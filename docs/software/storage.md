# Storage and sinks

Five subscribers write, and not one of them is a driver. Everything here
listens to [the bus](topics.md) and stores what it hears, which is why adding
a sink has never required touching a driver.

| Sink | Subscribes to | Writes |
|---|---|---|
| JSONL archive | `xams/meas/#` | `data/raw/<UTC date>.jsonl` |
| PostgreSQL | `xams/meas/#` | `meas` |
| alarm state | `xams/alarm/#` | `alarm_events` |
| audit | `xams/audit` | `audit` |
| flow periods | `xams/flow/period` | `flow_periods` |

All of them run in one process — `xams-ctl` starts it as `sinks`, first at
startup and last to stop, so the sinks outlive the producers and the final
measurements are archived rather than dropped. The four database writers
start only when a DSN is configured in
[`secrets.yaml`](config.md#secretsyaml); the archive writer always runs.
The process holds a single-instance lock, for the reason below.

---

## The files are the truth

```
data/raw/2026-09-18.jsonl        one file per UTC day
```

One JSON object per line, exactly as published, written **independently of
the database**. If PostgreSQL falls over — or was never configured — nothing
is lost. The archive keeps running.

The first line of every file says what produced it:

```json
{"t":"2026-09-18T00:00:00Z","meta":{"config":"89f5d1d","version":"1.4.0","host":"xams-sc"}}
```

That header is what makes a file self-describing, and it exists because of a
specific failure: the LabVIEW system kept column meanings in a separate,
overwritable header file, and those old CSVs consequently cannot be trusted
without counting columns to guess the layout. A file that carries its own
config hash cannot develop that problem.

**Flushed and `fsync`ed at least every 10 s**, so a power loss costs seconds
rather than hours.

### `raw` is kept as well as the scaled value

For the first year at minimum (§9.4). If a multiplier turns out to be wrong —
the `×25` on `p101` is the obvious candidate — the entire history can be
rescaled from `raw`. Without it, it cannot be. The cost is about 120 MB a day
uncompressed, roughly 10 MB gzipped.

!!! info "Parquet is designed but not built"
    §9.4 calls for `tools/jsonl_to_parquet.py` to convert the closed day
    nightly and gzip the JSONL behind it. That tool does not exist yet, so
    today the archive is JSONL only and nothing is compressed or pruned.
    Until it is written, `data/raw/` grows without bound — worth watching on
    a machine that also runs LabVIEW.

---

## The database is an index over them

```
PostgreSQL     meas · alarm_events · flow_periods · audit
```

Created by `sql/schema.sql`, which is run on every install — on the lab PC
and on the Nikhef VM alike — so the install is a repeatable procedure rather
than something that was once assembled by hand.

| Table | |
|---|---|
| `meas` | one row per channel per log interval: `t`, `channel`, `value`, `vmin`, `vmax`, `raw`, `unit`, `quality`, `src` |
| `alarm_events` | **transitions only**: `t`, `channel`, `state`, `threshold`, `value`, `message` |
| `flow_periods` | closed and open integrator periods: `start`, `stop`, `total_g`, `gaps_s`, `reset_by` |
| `audit` | every write and every recipient change: `actor`, `action`, `target`, `old_value`, `new_value`, `result`, `detail` |

Grafana reads from here, and only from here. **Nothing in the alarm path
does** — the engine decides and notifies without the database being involved
at all, which is the whole reason Grafana's own alerting is not used.

**A write failure never blocks the bus or a service.** The writer logs,
alarms, and keeps going. Two writers can run side by side against different
addresses — one local with short retention, one on a Nikhef VM with the full
history — and if the VM is unreachable only that writer falls behind, buffers
and catches up. Nothing local notices.

### Duplicates are normal

`meas` carries a unique index on `(t, channel, src)` and the writer uses
`ON CONFLICT DO NOTHING`. This is not defensive coding against a bug.
**MQTT re-delivers retained messages to every new subscriber**, so each time
the sinks reconnect they receive the last value of every channel again, with
its original timestamp, and would insert it a second time.

Deleting such rows does not help, because the next reconnect writes them back;
only clearing the broker does, which is what `tools/clear_retained.py` is for.
On 17 September 2026 a stray second sinks process turned this into 46,418
duplicate rows — which is also why the sinks hold a single-instance lock.
Two drivers fighting over an instrument fail loudly; two writers succeed
quietly, and the only symptom is a row count.

The same constraint is what makes a replay safe.

---

## Why the database is disposable

Because the files are complete and the database is derived, PostgreSQL can be
lost, moved, re-schemaed or replaced without touching a driver or losing a
measurement. That is a property worth protecting, and it is what the replay
procedure exercises.

**To rebuild from the archive:**

1. `psql -U postgres -d xams -f sql/schema.sql` — the schema is idempotent.
2. Replay the JSONL into it:
   `tools\replay_jsonl.py --target postgres --since <first day>` (see
   [Replaying the archive](../operating/tasks.md#replaying-the-archive-into-a-database)).
   Every record carries its own timestamp, channel and `src`, so the unique
   index makes the replay repeatable: run it twice and the row count does not
   change.
3. Nothing else. No driver, no configuration and no dashboard is aware that
   it happened.

Restoring from the nightly backup instead is in [Backup and
restore](../operating/backup.md).

---

## When the database is down

The archive keeps running; the files are the truth and nothing about them
depends on PostgreSQL. What happens to the database writer is worth knowing,
because it is visible and it is meant to be.

**Readings queue in memory and are written when the database returns.** The
queue holds `max_pending` rows — 100 000, about four hours at the normal rate
— and the sinks log a warning once the backlog passes 10 000:

```
the write backlog is 12480 rows and growing; nothing is lost yet, but it will be at 100000
```

*Nothing is lost yet* is the important half. A full backlog is a delay; the
rows are in memory and will be written.

**Past the cap, rows are discarded, and that is loss.** It is reported as an
error and the service publishes `degraded`, so it shows on the System health
page rather than only to somebody reading the log at the time:

```
DATA LOST: 143 measurement row(s) discarded because the write backlog was full
```

Reported when the number changes, with a reminder every five minutes — a
service that logs the same error every five seconds for a week teaches people
to filter it out. The last line of a run names any loss from the whole run,
because an error at 03:00 has scrolled off by the time anybody asks how last
night went.

**The rows discarded are not lost from the system**, only from the database:
they are in the JSONL archive, which is what makes the database disposable in
the first place. [Replay](../operating/tasks.md#replaying-the-archive-into-a-database)
recovers them.

**When the database comes back the backlog drains in one pass**, in seconds,
not at one batch per flush interval. That matters because the slow version was
still discarding new readings the whole time it was catching up — a recovery
that loses data of its own is the wrong way round.

---

## Provenance, again

Every row carries `src`, and the archive does too: `xams` for a reading this
system took, `sim` for a synthetic value, `labview` for imported history.
It is on the measurement rather than on the writer because **one writer
serves every service** and therefore cannot tell them apart — which is not a
hypothetical. Running the simulator once put 62,000 synthetic rows into
`meas` that were indistinguishable from real ones, and undoing it meant
deleting rows by timestamp and channel name.

`tools/import_labview_csv.py` brings the old CSV history in under
`src="labview"`, so Grafana shows one continuous timeline across the
migration instead of starting from zero — and a query can still exclude it.

---

## RGA measurement files

Not from the bus either. The RGA service writes one JSON file per **Save**,
`data/rga/YYYY/<start>_<recipe>.json`, with every scan, their mean and the
analyser's settings; nothing else of a spectrum is stored anywhere. The
`rga_saves` and `rga_summary` tables are an index over those files, filled
from `xams/rga/saved` by `sinks/rga_writer.py` and rebuilt from the files with
`tools/rga_rebuild_summary.py`. See [RGA](../drivers/rga.md#data).

## Flight recorder files

Not a sink, but it writes: the alarm service keeps the last ten minutes of
every channel at **full rate** in memory and dumps it to
`data/events/<timestamp>_<channel>_<severity>_<threshold>.jsonl` when
something goes wrong. See [Alarm engine](alarms.md#flight-recorder).
