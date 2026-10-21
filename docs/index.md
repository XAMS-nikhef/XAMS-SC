# XAMS Slow Control

Reads, stores, plots and alarms on the XAMS slow-control hardware, and is the
one place where the instruments that may be changed are changed. Runs on the
slow-control PC in the lab; the web UI is at `http://127.0.0.1:8000` there.

## The instruments

| Instrument | Service | What it does here |
|---|---|---|
| NI cDAQ-9174 | `cdaq` | pressures, temperatures (RTDs), flow, bottle masses — read |
| 2 × CAEN DT1470ET | `caen` | high voltage — read, **set and switch** |
| Lake Shore 335 | `lakeshore` | cryostat temperatures — read, **setpoint and heater range** (output 1) |
| APC UPS | `ups` | mains and battery — read |
| HiPace 80 turbo | `turbo` | status and speed — **listened to, never written** |
| PrismaPro RGA | `rga` | status — read; **control, scans, emission, EM** behind the p105 guard |
| LN₂ dewar scale | `scale` | LN₂ mass — read, **queries only** |

Plus `derived` (the flow integrator), `sinks` (archive and database), `alarms`
(email / SMS) and `webui` (pages and this manual).

## Where to start

| | |
|---|---|
| Using the web pages | [Web interface](operating/webui.md) |
| Running it, or it misbehaves | [Operating](operating/index.md) |
| Alarms: who is told, switching them off | [Alarm engine](software/alarms.md) |
| Adding a sensor | [Configuration files](software/config.md) |
| Writing a driver | [How a driver works](drivers/index.md) |
| Reinstalling | [Installation](install.md) |
| How and why it is built this way | [Architecture](software/architecture.md), [Design specification](DESIGN.md) |

## How it fits together

```
  cdaq      ─┐
  caen      ─┤
  lakeshore ─┤                         ┌─► sinks ──► data/raw/*.jsonl   the archive
  ups       ─┤                         │           PostgreSQL ──► Grafana
  turbo     ─┼─► MQTT (mosquitto) ─────┼─► alarms ─► SMS / email
  rga       ─┤                         │
  scale     ─┤                         └─► webui (FastAPI, 127.0.0.1:8000)
  derived   ─┘   (flow integrator,          pages, P&I, this manual
                  computed from fm101)
```

Each service reads one instrument and publishes on the bus; storage, alarms and
the UI only subscribe. Commands go the other way: the web UI or `xams-ctl`
publishes on `xams/cmd/…`, and the service that owns the instrument answers on
`xams/ack/…` ([Topic contract](software/topics.md)).

**The archive files are the truth; the database is an index over them.** Lose
PostgreSQL and the archive is replayed into a new one.

## The rules for changing an instrument

- **Every write** is checked against the range in `channels.yaml`, **read back**
  from the instrument before it counts, acknowledged, and audited with the old
  value, the new value and who did it. No range written down means no write.
- **Two HV gates are hands-only:** a board must be in `REMOTE` and a channel
  enabled at the front panel before software can do anything. A channel that
  is not enabled holds `VSET` 0, so enabling it never brings up a voltage.
- **Protection settings are never written** (`MAXV`, `RUP`, `RDW`, `TRIP`,
  `ISET`): displayed and alarmed on, not changeable from here.
- **Nothing happens by itself** on a start or restart. The only automatic
  actions switch things **off**: RGA emission and EM, by the p105 guard or
  after an hour without a scan.
- **This is not a protection system.** Interlocks belong in hardware (§10).
