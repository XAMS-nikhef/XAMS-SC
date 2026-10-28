# Alarm engine

Threshold evaluation, hysteresis, staleness, severity routing and the flight
recorder. Source: `src/xams_sc/alarms/`, [the design
specification](../DESIGN.md) §4.3 and §11. The thresholds currently in force
are in [the alarm reference](../reference/alarms.md).

The engine subscribes to `xams/meas/#`, evaluates every reading against
[`alarms.yaml`](config.md), publishes state to `xams/alarm/<channel>` and
notifies. **It decides; Grafana only displays.**

---

## Why Grafana's own alerting is not used

Grafana alerts by querying the database on a schedule, which puts PostgreSQL
*and* Grafana inside the alarm path: if either is down, slow, or mid-upgrade,
notifications do not go out — and the moment the database is struggling is
not the moment to lose alarms.

Alarms are the part of this system that must be most reliable, so the path is
kept as short as it can be:

```
measurement ──▶ bus ──▶ engine ──▶ SMS / email
```

No database, no web server, no scheduler. The alarm service runs **locally**
on the lab PC and never depends on the Nikhef VM — when the network is down
is exactly when somebody needs to see what is happening. Grafana still shows
alarm state and history, from the `alarm_events` table, because a *display*
of alarms may safely depend on a database that a *notification* may not.

---

## What raises an alarm

**Four thresholds per channel, EPICS-style**, each with its own severity and
its own notification routes:

| | |
|---|---|
| `lolo` | far below |
| `low` | below |
| `high` | above |
| `hihi` | far above |

Severity is one of `ok` · `minor` · `major` · `critical`, and it is per
threshold rather than per channel — `high` can be minor while `hihi` is
major.

Two further conditions raise alarms without any threshold being crossed:

- **Staleness.** A channel that has stopped arriving within
  `stale_after_seconds` (60 by default) is raised at the severity in the
  `staleness` block — `major`, the same as a threshold breach. A dead sensor
  must not read as healthy.
- **Bad quality.** A reading published with `quality=error` is a failed read,
  and is treated as a fault rather than ignored.

Neither applies to a channel that is not being read on purpose: one disabled in
`channels.yaml`, or one whose cDAQ readout is **switched off** at run time
(`xams/status/readout`, owned by the cdaq service; the PT1000s while the HV is
on). Its alarm is withdrawn — published back to `ok`, so the history records
the end — and it is neither evaluated nor checked for staleness until the
readout is switched on again.

### Unreadable groups: one alarm, later, quieter

The default — any unreadable channel is a major alarm at once — is right for
a sensor the plant depends on and wrong for an instrument used on demand. An
`unreadable:` group in `alarms.yaml` changes that for a set of channels:

```yaml
unreadable:
  rga:
    channels: [rga_status, rga_emission, rga_em, rga_control, rga_hw_errors,
               rga_hw_warnings, rga_tp_trips, rga_box_temp]
    alarm_on: rga_status       # the one channel that speaks for the group
    after_seconds: 180         # unreadable (bad quality or silent) this long first
    severity: minor
    notify: [email]
```

Only `alarm_on` alarms, and only after the group has been unreadable for
`after_seconds` (silence: at least `stale_after_seconds` too); a good reading
restarts the clock. The other channels never alarm on being unreadable; any
thresholds they have still apply to their values. A channel name that is not
a channel stops the load.

Added on 28 October 2026 for the RGA: its HTTP server missed a few requests a
dozen times that day, and each 20-s outage gave eight major alarms and eight
flight-recorder dumps. Now a short outage gives nothing, and one lasting three
minutes gives one minor alarm by email.

!!! warning "Physics thresholds are still largely TBD"
    `alarms.yaml` carries the structure, the staleness rules and the
    pressures; the rest await the LabVIEW `Error and Alarm` tab (§16). The
    engine logs a loud warning at startup when no physics thresholds are
    configured at all, so an empty file cannot pass for a quiet plant.

### Staleness needs a clock, not an event

This is why the engine cannot be purely event-driven: **the absence of a
message is the signal**, and nothing will deliver it. A background pass walks
every known channel and raises the ones that have gone quiet.

---

## Hysteresis: why it does not chatter

A channel already in alarm must come back past its threshold by `hysteresis`
of the limit's own magnitude — 2% by default — before it clears. Without
that, a value sitting exactly on a limit produces a stream of alternating
alarm and clear notifications, and the reader learns to ignore the channel.

A condition that persists is re-notified every `min_repeat_minutes`
(15 by default), not on every reading.

---

## Acknowledging is not fixing

Two ways to stop a repeating notification, and neither of them changes the
condition:

| | |
|---|---|
| **acknowledge** | stop repeating. The alarm stays active and stays visible |
| **silence** | suppress notifications for a set number of minutes — for a deliberate intervention |

**A new, worse condition clears an acknowledgement.** Acknowledging `high`
must not silence the `hihi` that follows it, so any transition to a higher
severity un-acknowledges the channel and notifies again.

The acknowledged flag is published with the alarm state, so the UI can show
"still in alarm, somebody has seen it" — which is a different thing from
"resolved", and the two must never look alike.

### Switching one alarm off

Some conditions are expected for days: the LN₂ dewar empty or taken away for
a refill. For those, `alarms.yaml` lists channels as **`switchable`**, and each
gets its own switch (for now only `ln2_mass`, on the LN₂ tile of the
[Control page](../operating/webui.md#ln2-dewar)).

It follows the rules of the master switch on the Alarms page:

- **delivery only.** The alarm is still evaluated, published on
  `xams/alarm/<channel>`, shown in colour, listed on Alarms and recorded; only
  the email and SMS are skipped, with a warning in `alarms.log`;
- **switching it back on is not quiet.** `last_notified` is not stamped while
  off, so a condition still present announces itself on its next reading;
- **a restart does not re-arm it.** The state is kept in
  `data/alarm_armed.json` and logged at CRITICAL on every start while off;
- **only listed channels.** `set_armed` refuses any channel not in
  `switchable`, so it cannot be used to silence `pmain` one request at a time,
  and a switch file left from an earlier configuration silences nothing;
- **audited**, including a refusal (`action: alarm_switch`).

Command `xams/cmd/alarms/arm` `{"channel", "enabled", "by"}`, answered on
`xams/ack/alarms/arm`; the state of every switch is retained on
`xams/status/armed`. `xams-ctl check` refuses a `switchable` name that is not
a channel with thresholds.

---

## What it publishes

```
xams/alarm/tt302   {"state":"minor","threshold":"high","value":-88.2,
                    "since":"2026-09-18T09:56:16.004Z","acknowledged":false}
```

Retained, so a UI connecting later sees the true current state rather than
waiting for the next transition. The [alarm sink](storage.md) stores
**transitions only** into `alarm_events` — a retained republish on every
reconnect would otherwise fill the table with rows saying the same thing.

It also publishes `xams/status/limits`, retained: the thresholds actually
loaded, per channel. The real risk is not a missing line on a plot, it is
believing a threshold is 2.0 when it is 20, and this makes what is in force
inspectable.

---

## Notification

A thin interface — `send_sms`, `send_email` — and the engine knows nothing
else, so what sits underneath is replaceable without touching alarm logic.
Routes come from the threshold's own `notify` list, falling back to email.

**SMS reuses the path the LabVIEW system already used**: the same MessageBird
library and sender id, because the numbers, the gateway and the message
format are all known to work and rebuilding that means rediscovering it.

!!! danger "The old script had its API key in plaintext"
    In three copies, on the Desktop. Here it is read from
    [`secrets.yaml`](config.md#secretsyaml) and must never enter the
    repository. That key has sat on a desktop for years and is a good
    candidate for rotation.

Every send is best effort and failures are logged rather than raised: a
gateway being down must not stop the engine evaluating the next reading. The
notifier reports how many deliveries succeeded per channel, so "nobody was
told" is distinguishable from "everybody was told". Phone numbers are masked
in the logs — logs get pasted into issues and emails.

The recipients list is read **at send time**, so a change made on the
[Alarms page](../operating/webui.md#who-is-notified) applies to the next alarm
with no restart.

### The emails

`alarms/mail.py` renders both the alarm mail and the daily digest, and it is
written under constraints that look archaic until you have seen the result:
tables for layout, inline styles only, no images, no JavaScript, no web
fonts, and a plain-text alternative always. Outlook renders with Word's
engine, Gmail discards `<style>` blocks, and about half of all clients force
a light background — hence a light palette rather than the web UI's dark one.

**Colour is never the only signal.** A red border is decoration; the word
ALARM is information. Roughly one reader in twelve cannot reliably tell the
two colours apart.

**An alarm mail carries the plant around it** — the other channels, the
service states — because an alarm that says only "tt302 is high" makes the
reader open the web UI at three in the morning to find out whether anything
else is wrong.

The [daily report](../operating/email.md) runs from Task Scheduler as a
separate process, deliberately **not** inside the alarm service: a report is
a convenience and the alarm engine is not, and a scheduler bug that wedged a
thread would take the alarms down with it. Its state comes from retained
MQTT, so it can be sent while PostgreSQL is down — which is one of the things
worth being told about.

---

## Flight recorder

The last **ten minutes of every channel at full rate**, in memory, written
out the instant an alarm fires:

```
data/events/2026-09-17T13-02-11_pmain_hihi.jsonl
```

This is what the present system lacks. After an incident there are only
averaged values and no record of the approach to it — which is precisely the
part you want when working out what happened. The buffer is a fixed-length
deque costing a few megabytes, never written unless something goes wrong: the
guard against one failure must not create another.

It lives in the same process as the engine on purpose. The dump must happen
at the moment the alarm is raised, and routing that through the bus would add
a delay to the one moment where the data matters most.

---

## Reload

This is the service where `xams-ctl reload` matters most: a threshold change
is the commonest configuration edit, and restarting the engine to apply one
would lose every channel's alarm state — including which alarms are
acknowledged, which would start the notifications up again.

So the engine applies new thresholds in place, keeping state, and reports
that it did. A device service, which holds serial ports and DAQmx tasks built
from the channel map, answers the same reload by saying it needs a restart —
and that is how `xams-ctl reload` knows which services to name.
