# XAMS Slow Control

Monitoring and convenience control of the XAMS slow-control hardware: one NI
cDAQ-9174 chassis, two CAEN DT1470ET high-voltage supplies, a Lake Shore 335
and a UPS. Storage, plotting, alarming and a small web UI.

Every device service reads its instrument and publishes to a local MQTT
broker. Nothing writes to a database directly. Storage, alarming and the UI
are all subscribers, so any of them can be added or replaced without touching
a driver.

```
  cdaq ─┐
  caen ─┤                    ┌─► jsonl_writer ──► data/raw/*.jsonl   the archive
  ls335 ┼─► MQTT (mosquitto) ┼─► pg_writer ─────► PostgreSQL ──► Grafana
  ups  ─┘                    ├─► alarm engine ──► SMS / email
                             └─► web UI (FastAPI, 127.0.0.1:8000)
```

**The files are the truth; the database is an index over them.** If PostgreSQL
is lost, replay the archive into a new one. Nothing in any driver changes.

**It reads everything and writes three instruments:** the Lake Shore heater
(setpoint and range, output 1), both CAEN supplies (`VSET` per channel, and
energising a channel), and the residual gas analyser (taking and releasing
control, scan setup, start and stop; never emission). Every write is range-checked against `channels.yaml`,
read back from the instrument, acknowledged on the bus and audited. Enabling a
channel and taking a board out of `LOCAL` stay hand operations at the front
panel — no command exists for either.

---

## The manual

**All documentation lives in [`docs/`](docs/), and is served by the web UI
itself at <http://127.0.0.1:8000/manual>** — so it is on the lab PC whether or
not the building network is.

| | |
|---|---|
| Run it, or fix it when it breaks | [docs/operating/](docs/operating/index.md) |
| Reinstall from scratch | [docs/install.md](docs/install.md) |
| How it works, and how the drivers work | [docs/software/architecture.md](docs/software/architecture.md) |
| Add a sensor | [docs/software/config.md](docs/software/config.md) |
| Where the work has got to | [docs/status.md](docs/status.md) |
| Why it is built this way | [docs/DESIGN.md](docs/DESIGN.md) |

To read it as a site, with search and the generated channel and alarm tables:

```bash
python -m pip install -e ".[docs]"
python -m mkdocs serve -a 127.0.0.1:8001
```

Port 8001, not 8000 — 8000 is the web UI itself.
