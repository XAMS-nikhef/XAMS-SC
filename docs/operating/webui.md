# The web interface

<http://127.0.0.1:8000> — the page to bookmark, and where the day-to-day work
happens. It reads the **retained MQTT topics, never the database**, so it keeps
working when PostgreSQL does not, which is exactly when it is needed (§8.1).
Every page refreshes itself; nothing has to be reloaded by hand — see
[When a page reloads](#when-a-page-reloads) for the one rule that matters,
which is that it will not do it while you are in the middle of something.

It binds to loopback only. There is no login, and there is deliberately no
remote access — see [Who is acting](#who-is-acting-and-why-it-is-asked) for
what that means for attribution, and §8 for why a tunnel is the answer if
remote viewing is ever wanted.

| Tab | The question it answers |
|---|---|
| **P&I** | What is the plant doing — the landing page, and read-only |
| **Channels** | What is every channel reading, right now |
| **Control** | I want to change something |
| **System health** | Is the *software* all right |
| **Alarms** | What fired, what *would* fire, and who gets told |
| **Logs** | What did a service say when it went wrong |

One tab per question. The Overview used to answer two — "is everything all
right?" and "change something" — because the cryostat setpoint and the flow
reset had nowhere else to be. Those are asked at different moments, so they
are on different pages now, and the page that answers neither is the one the
site opens on.

**The P&I writes to nothing.** It is the page left open on a screen all day,
and a setpoint box on an unattended display is the wrong thing to reach for by
accident. Its cards link to Control instead, which costs a deliberate
navigation — the same reasoning that keeps the CAEN front-panel enable a hand
operation (§10a).

Two links lead off the site: **Grafana ↗** for history and plots, **Manual ↗**
for this documentation, served by the same process so it is present on the lab
PC whether or not the building network is.

---

## When a page reloads

**P&I** does not reload at all: it fetches `/api/state` every **five seconds**
and rewrites the numbers in place, so the drawing never blinks and never loses
your scroll position. If those fetches stop — the web service down, the network
gone — it says **NOT UPDATING** and greys the whole drawing out after twenty
seconds. A frozen plausible drawing is worse than an obviously dead one.

Every other page except **Logs** reloads itself **every ten seconds**, so a
screen left open is never showing yesterday.

**It holds off while the page contains anything you have typed or loaded and
not yet sent.** The question it asks is *does this page differ from what the
server sent?*, not *is the cursor in a box?* — so a value filled in by hand and
a column filled in by **Load defaults** are treated alike. Both are unsent
intent, and a reload would throw either away.

That distinction was a bug, on the HV page and in the worst possible place.
*Load defaults* fills boxes nobody is touching, so nothing took focus, the
timer ran out, and the operator watched the setpoints they had just loaded turn
back into empty boxes a few seconds later.

The hold is not permanent. Clear the boxes or apply them, and the very next
tick reloads — a box filled and forgotten must not freeze the numbers on the
page for the rest of the afternoon.

With JavaScript off, a plain ten-second meta refresh takes over and none of the
above applies: it cannot be cancelled once the page is parsed.

---

## The header, on every page

- **config hash and uptime** — the hash identifies which version of
  `config/*.yaml` is running, and is the same hash written into the first line
  of every archive file.
- **acting as** — who you are. See below.
- **the badge on the right** — the whole system in one word. It is the first
  thing to look at and the only thing worth looking at from across the room.

### Who is acting, and why it is asked

Every command is recorded with a name (§10 rule 5). Set it once in the header;
it is kept in a cookie in this browser and attached to everything you send, so
it never has to be retyped before an action.

**The box suggests the alarm recipients**, so the regulars are one click and
one spelling — `apc`, `AP Colijn` and `Auke-Pieter Colijn` are three different
people to anything that reads the audit trail later. Everybody on the list is
offered, whether their **Notify** is on or off: off usually means somebody is
away, which is exactly when a colleague is the one operating. **The suggestions
are not a closed list** — type any name over them. Somebody can be at the
keyboard without being an alarm recipient, and the one thing that must not
happen is a student on shift picking a colleague's name from a menu because
their own was not in it.

**It is taken on trust.** There is no login on this interface, so the name is
whatever was typed. Left empty, a command records `webui (unnamed)` rather than
a blank. Weak attribution recorded honestly beats an anonymous change, and it
matches what `xams-ctl ... --by <you>` does from a terminal. Real attribution
needs authentication, which this interface does not have.

---

## P&I

The plant drawing with a live value in each instrument bubble — the page that
answers *where is that sensor*, which a table of tag names cannot. It is the
landing page, and it is **read-only**: nothing on it writes to an instrument.

Built from the original drawing by `tools/build_mimic.py`: rotated to
landscape, frame and title block removed, recoloured for the dark interface,
and cropped to the ink so the drawing gets the whole box rather than the blank
paper the original sheet carried around it.

**A stale channel greys out and shows a dash**, exactly as on the Channels page
and for the same reason.

**Click a value to see its history.** A stale, greyed-out reading is still
clickable — that is exactly when someone wants to know when it stopped, and
what it was doing before. Valves, pipes and unmeasured tags such as `SG101`
do nothing: only a value opens a plot, so the drawing never invites a click
it can't answer. The popup shows a 1 h / 24 h / 7 d range and an *open in
Grafana* link for the full toolbar; if Grafana is unreachable or embedding is
not enabled on this host, the plot area stays blank but the link still works.

The tags in the SVG are checked against `channels.yaml` when the service starts,
and drift is reported **in both directions** — a bubble with no channel behind
it, and a channel that appears nowhere on the drawing.

### The column beside the drawing

Strictly the **complement** of the drawing: what has no place on a pipework
diagram. Every pressure, temperature and flow rate is already drawn in the
place it physically belongs, and a reading with two homes on one page is fine
until the day the two disagree.

Top to bottom, and the order is deliberate:

| | |
|---|---|
| *status line* | whether anything is wrong. Fixed height, so it never moves |
| **Cryostat setpoint** | what the cryostat is being held to, and what holding it costs |
| **Xenon moved** | the integrated total since the period started |
| **High voltage** | `VMON` and the decoded state of every channel |
| **Mains** | line power or battery, charge, UPS runtime |
| **Turbo pump** | state in words, speed, over-temperature; says so when the speed is not on the bus |
| **Alarms** | what is firing, and whether anybody is being told |

**Alarms is last because it is the only card that changes height.** It grows
as alarms come and go, and anything below a growing card moves. The status
line at the top says *whether* something is wrong and never moves; the card at
the bottom says *which*, and is free to grow.

The cryostat card shows the heater in **watts**, with the percentage beside
it. Percent is of full scale, so it answers "how hard is it working" only once
you know what full scale is; watts is the number to compare against the heat
load.

### The turbo on the drawing

The turbo pump's **outline** is coloured by its state, and the speed is written
underneath the pump. The card in the column says the same state in
words, so nothing depends on colour alone.

| Outline | Card says | Meaning |
|---|---|---|
| the drawing's own line | `off` | pumping station off, rotor stopped |
| amber | `accelerating`, `below set speed`, `running down` | turning, but not at set speed (running down: switched off and still spinning — do not vent) |
| green | `at speed` | at set speed |
| red | `Err 006`, `over temperature` | the pump reports an error or warning, or an over-temperature |
| dashed grey | `no data` | the status is not being read: service, DCU or cable |

Clicking the pump opens the speed history. If the speed shows a dash while the
rest reads, the DCU is no longer polling it: set the DCU's service line
(parameter 795) back to 309. See [the driver page](../drivers/turbo.md).

**The cards that correspond to something changeable carry a link** — *change
→*, *reset →*, *set →* — which takes you to the matching section of
[Control](#control). The forms are not on this page, and that is deliberate:
this is the page left open on a screen all day, and a setpoint box on an
unattended display is the wrong thing to reach for by accident.

### When the page stops updating

Every reading here comes from one fetch every five seconds. If that fetch
fails once, nothing changes — making the whole drawing flicker because a packet
went missing would be worse than the packet.

If it has not succeeded for **twenty seconds**, the status line reads **NOT
UPDATING** with the age of the last answer, and **the drawing is greyed out**.
Withdrawn rather than annotated: a banner over a drawing that still looks live
is half a warning.

A browser tab that has been throttled in the background never reaches the
failure at all, so elapsed time is checked on its own timer as well.

---

## Channels

Every channel, grouped by the service that reads it: value, unit, age, quality,
alarm state and description. This is the page to open when a number somewhere
else looks wrong, because it shows *when* the number was read as well as what it
was.

**A stale channel shows a dash, never its last number.** A frozen value
displayed as though it were live invites a decision based on a reading that
stopped being true an hour ago — that is the failure this kind of page exists to
avoid.

Quality is the driver's own verdict on the reading:

| quality | means |
|---|---|
| `ok` | a real reading from a verified instrument |
| `error` | the instrument answered, but not with a number — open circuit, out of range, link down. **No value is published** |
| `unverified` | read from a device that has not proved its identity; never written to |
| `stale` | nothing new has arrived within the staleness window |

---

## Control

**One page acts on hardware: Control** — with one exception, the
[RGA](#rga), which has its own page. The CAEN supplies, the cryostat
setpoint and the flow-integrator reset are all on Control, because "I want to
change something" is one question and it should have one answer. The P&I
links here rather than carrying the forms itself. Everything below is confirmed
before it is sent, audited with your name, and answered by the service that owns
the instrument — never by the web process, which holds no permitted range and no
instrument handle. A command from this UI and one from `xams-ctl` get identical
treatment.

### Lake Shore setpoint

Type the temperature in °C, press **Set**, confirm. Output 1 only.

The page validates nothing beyond *is it a number*. The permitted range, the
instrument being connected, the read-back and the audit record all live in the
service that owns the serial port, so a command sent from here gets exactly the
same treatment as one sent any other way. What that treatment is:

1. the value is checked against the range in `channels.yaml` — a channel with
   no range refuses everything, because a range nobody wrote down is not
   permission to write anything;
2. the setpoint is **read back from the instrument** and must match to 0.01 °C
   before the write counts as successful;
3. the result is published on `xams/ack/lakeshore/setpoint` and appended to the
   audit log with the old value, the new value and your name.

A refusal is shown in red as **"Refused — nothing was changed"**, with the
reason. A rejected command is never silently dropped.

### Heater range

**high** or **off**, on output 1, confirmed the same way and audited the same
way. The 335 has four ranges; the two the procedure actually uses are offered.

### Reset the flow integrator

Closes the running period and opens a new one. **Nothing is erased** — the
closed period keeps its total, its gaps and your name in the `flow_periods`
table. That is the whole difference between this and zeroing a counter, and it
is why this button could exist before any instrument was writable.

If the **derived** service is not running, the reset fails and says so; nothing
is closed in that case.

> **`N s of gaps`** on the card means the integrator was not running for part of
> the period, so the total is an underestimate by whatever flowed during them.
> That is recorded rather than papered over: extrapolating the last known rate
> would be inventing data.

Equivalently, from a terminal:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl flow-reset --by <you>
```

The cryostat and the flow integrator sit side by side at the top of the page;
the high voltage, which needs far more room, is below them and has
[a section of its own](#control-high-voltage).

### What cannot be changed here, by design

- **A channel's enable switch.** No such command exists. A hardware gate that
  software cannot reach is the last thing standing between a bug and an
  electrode.
- **`BDCTR`, the board's LOCAL/REMOTE mode.** Front panel only.
- **`MAXV`, `RUP`, `RDW`, `TRIP`, `ISET`** — the board's own protection. The
  software displays them and alarms when a board disagrees with `devices.yaml`;
  it cannot write them (§10 rule 2).
- **The cDAQ**, which has no output module, and **the UPS**, which is read
  through its HID and not commanded.
- **Anything at all on startup or restart.** A restart is invisible to the
  hardware (§6.1 rule 4).

Named procedures — a reviewed sequence run as one action — are milestone 9 and
do not exist yet.

---

## Control: high voltage

The lower half of the **Control** page. Both CAEN supplies, eight channels
each, stacked one above the other: `VSET`,
`VMON`, `IMON`, the state, and the board's own protection settings. **This page
also operates them** — setpoints and energising.

### The three things that decide whether there are volts out

They are separate on purpose, and only the third is a command:

| | changed by | what it does |
|---|---|---|
| the board's `BDCTR` mode | **front panel only** | in `LOCAL` the board refuses every remote setpoint |
| the channel **enable switch** | **front panel only** | clears `DISABLED`. Permits; energises nothing |
| **turn ON / turn off** | this page, or `xams-ctl hv-on` / `hv-off` | energises a channel that is already enabled; it then ramps to `VSET` |
| **clear trip** | this page, or `xams-ctl hv-clear-trip` | shown on a tripped channel instead of *turn ON*: the acknowledgement, without which the channel cannot be turned on. Zeroes and switches off every tripped channel on that supply, then clears the latched board alarm. The channel stays off ([details](../drivers/caen.md#recovering-from-a-trip)) |

Nothing in the software can touch the first two, by design. Two hand gates sit
between code and an electrode, and neither is reachable from here.

### The Status column

From the board's own `STAT` word, never from the voltage. **Four** states:

| shown | meaning |
|---|---|
| `disabled` | the front-panel enable switch is off |
| `enabled` | switch on, output **not energised**. Permitted and inert |
| `ON 0 V` | energised, sitting at zero. Live, with nothing on it yet |
| `ON` | energised with volts out |
| `TRIP`, `INTERLOCK`, … | a fault flag, in red, in place of all four |

**`enabled` is not `off`.** The page showed both as "off" until 17 September
2026, because it inferred the state from `VMON > 1` instead of asking the board.

The full bitmask is stored as `hv_*_stat`, so the history carries `TRIP`,
`INTERLOCK`, `OVER_CURRENT` and `OVER_TEMP` too.

### A VSET in red

A **disabled** channel whose setpoint is not zero, and each supply says so in a
red banner at the top listing them.

The enable switch is a hand operation and the board ramps to `VSET` the moment
it is flipped — so a red value is the voltage you would get by touching that
switch, with no confirmation and no warning. On 18 September 2026 seven of eight
channels sat like that, the anode at **+4200 V**.

§10a turns *a disabled channel has `VSET` 0* into an invariant precisely so that
the switch is safe to flip. **Until the red values are zeroed, it is not.**
Zeroing everything:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl hv-standby --by <you>
```

### Setting voltages

The boxes in the `set V` column are a **plan**: filling them changes nothing.

1. **Load defaults** fills every box from `hv_defaults.yaml`, falling back to
   `channels.yaml` — in git, reviewable, rather than remembered — and **writes
   nothing**. Read them, change what you want. **Clear** empties the boxes.
   Filled boxes [hold off the ten-second reload](#when-a-page-reloads), so they
   stay filled for as long as you need them.
2. **Apply setpoints** writes every box you filled in, as one action, across
   both supplies. Empty boxes are left alone, so one channel or eight is the
   same gesture.
3. Each channel is answered individually: `cathode now -2250.0 V`, or the reason
   it was refused. A refusal is shown in red as **"Refused — nothing was changed
   for these"**.

A box is greyed out and cannot be filled when the channel's switch is off: such
a channel must keep `VSET` 0, so offering the box and then refusing it would be
theatre. Flip the switch and its default appears.

### Changing what "Load defaults" offers

**Edit defaults…**, beside *Load defaults*, opens `/hv/defaults`. Type the new
values, press **Save defaults**, and the next *Load defaults* offers them.

This is an R&D setup and the operating point moves, so the defaults are meant
to be changed. Saving **writes a file and touches no instrument** — nothing
ramps and no voltage changes. Every change is audited, and
`config/hv_defaults.yaml` is tracked by git, so commit it along with everything
else.

The **allowed range** shown beside each channel is *not* editable there. It
lives in `channels.yaml` because it is what every write is checked against. A
default outside it is refused and nothing is written; if the range itself is
wrong, edit `channels.yaml`, commit, and `xams-ctl reload`.

What the service checks before writing, in order — the page itself checks
nothing but *is it a number*:

1. it is an enabled `hv_vset` channel, not a monitor;
2. the value is inside the range in `channels.yaml`. That range carries the
   polarity: −500 V on the anode is refused as firmly as −3000 V on the cathode;
3. the channel's **enable switch is on**, unless the value is zero. Zero is
   always allowed — that is how the invariant gets re-established;
4. the setpoint is **read back from the board** and must match to within the
   tolerance. A value above the board's own `MAXV` is refused by the instrument
   itself, which is the protection working;
5. the result is published on `xams/ack/caen/vset` and written to the audit log
   with the old value, the new value and your name.

From a terminal, one channel at a time:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl hv-set hv_cathode_vset -2250 --by <you>
```

### Energising a channel

**turn ON** / **turn off**, per channel, each its own button and its own
confirmation — a separate act from applying a voltage, because it is one.

**Turning on ramps the channel to whatever `VSET` currently holds**, at the
board's own `RUP`, so the confirmation and the answer both name that voltage:
*ramping to −2250.0 V*, or *energised at 0 V; nothing will move until a setpoint
is set*. Turning on at zero is a perfectly reasonable thing to do and moves
nothing.

**Turning off starts a ramp down** at the board's `RDW`, and the channel reads
`ON` until it actually reaches zero. That is reported as *ramping down at the
board's own rate* rather than as a failure — demanding the bit clear at once
would report a failure for a command that worked, and train somebody to re-send
*off* to a channel already on its way down.

A channel whose switch is off cannot be energised. The refusal says so and names
the remedy: flip the enable at the front panel.

**The button says `switching…` and stops accepting clicks** until the answer
comes back. Energising is a round trip — command, read-back, ack, redirect —
and while it was in flight the row still read *not energised* and the button
still said **turn ON**, so an operator who saw nothing happen clicked again. If
a refresh landed in between, the button under the cursor had become **turn
off**, and the second click de-energised the channel the first had just
started. Only the energise buttons do this; *Apply setpoints* is idempotent and
re-sending it costs nothing.

**The Status column updates the instant the command is answered**, rather than
at the next ten-second publish. Two things used to delay it: the status was
published on the ordinary cadence, and a status word that changed mid-window
was *averaged* — and a mean of a bitmask is not a bitmask. A channel caught
part-way through a ramp averaged to a word with `RAMP_UP` set and `ON` clear,
so the page reported a live channel as off until the ramp finished. Flags are
now taken as they last came off the wire, and the read-back the write is
verified against is published straight away.

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl hv-on  hv_cathode_vset --by <you>
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl hv-off hv_cathode_vset --by <you>
```

With no channel named, `hv-on`, `hv-off` and `hv-standby` act on every
`hv_vset` channel.

### The safe order

```
   load defaults, edit          no effect on anything
   flip the enable by hand      safe: the channel is at 0 V
   apply setpoints              VSET written, read back, audited
   turn ON                      the board ramps, at its own rate
```

### What this page shows but cannot change

`MAXV`, `RUP`, `RDW`, `TRIP` and `POL` are the board's own protection settings,
displayed from `devices.yaml`. The software alarms when a board disagrees with
what is recorded and **cannot write any of them** (§8.3, §10 rule 2). Ramp rate,
trip current and over-voltage limit live on the instrument, and that is what
keeps this software out of the protection path.

---

## RGA

The residual gas analyser (Pfeiffer PrismaPro, on the pre-volume). Its own
page, not part of Control: taking it, scanning, looking at the spectrum,
saving and giving it back are one piece of work around one plot. Control links
here. Background and design: [RGA](../drivers/rga.md).

**This page cannot switch emission**, nor the electron multiplier or the ion
source. With emission off a scan measures the detector's noise, about
10⁻¹⁴ A — which is what a background scan is. Emission is still switched with
PV MassSpec.

### What it shows

The instrument (name, firmware, filaments, the unit the device uses), then its
state: **Emission** on or off, **Control** — nobody, *this system*, or *another
client* with its name and address (PV MassSpec, or someone in the analyser's
own web page) — hardware errors and warnings, the analyser's own lifetime count
of **over-pressure trips** (a rise means it switched emission off itself), the
electronics temperature, and the named `systemStatus` bits.

### Taking it and giving it back

**Take control** before anything else. If another client holds the analyser
this is refused, saying who; tick *take it anyway* only when you know their
measurement can be cut off — it asks once more. **Release control** gives it
back (and stops a running scan).

You do not have to remember to release: the service gives control back by
itself after five minutes without a scan or a command, and when it stops. PV
MassSpec is never locked out by a page left open.

### Scanning

Pick a recipe and press **Start**:

| Recipe | What | Time |
|---|---|---|
| `survey` | 1–200 amu, 10 points/amu | 3 scans of ~32 s |
| `air_leak` | 2, 4, 14, 18, 28, 32, 40, 44 amu | 10 scans of ~2 s |
| `xenon` | 124–136 amu (Xe⁺ isotopes) | 5 scans of ~4 s |
| `quick` | 1–50 amu, 4 points/amu | 2 scans of ~6 s |

Recipes are in `config/rga_recipes.yaml`; a new one is a few lines there and a
restart of `XAMS-rga`. The plot fills in as scans arrive: green the latest
scan, grey the mean so far, log or linear, in ampere. **Stop** ends a scan
early; what was taken can still be saved.

### Saving

Nothing is stored unless you press **Save**. The form appears when an
acquisition has finished or was stopped. Write what the measurement is — valves
open or closed, before or after a fill — and tags (`background`,
`before-fill`, `after-bake`, `leak-check`). For a measurement, name the
background file it should be compared with.

A Save writes one file, `data/rga/YYYY/<start>_<recipe>.json`, with every scan,
their mean, the analyser's settings and the slow-control values at that
moment, and adds a summary of up to nine masses (those the recipe covers) to
the database for Grafana. One
acquisition is saved once. Reading a file back for analysis:
`xams_sc.rga_file.load_rga(path)`.

## System health

The question it answers is *is the software all right*, and it is built so that
a healthy system is a boring page: no alarm card, no unhealthy channels, green
dots.

It used to be the landing page and carried the cryostat and flow controls as
well, because they had nowhere else to be. They are on [Control](#control) now,
and this page answers one question.

**Active alarms** appear at the top, and only when there are any. Each row
gives the channel, the state, the threshold it crossed and the value that
crossed it. *Acknowledged* means the repeating notification has been stopped —
**it does not mean the condition is gone**, and the two must never be read as
the same thing.

**Services** — one row per service, with its state and how long ago it last sent
a heartbeat. A few seconds is normal. Every one publishes one, so **silent**
means silent for any of them, including `sinks` and `alarms`. Green is beating
and reporting itself running; amber is beating but degraded — the sinks go
amber when they have had to discard data; red is silent, stopped, or in a
state it should not be in.

Note what this card cannot do: the alarm engine cannot report that the alarm
engine has stopped. That needs the outside watchdog of §12.

**UPS & dashboards** — line power or `ON BATTERY`, battery charge and runtime,
plus two housekeeping facts that ride along rather than taking a card of their
own: whether the **nightly backup** ran, and whether the **Grafana dashboards**
are saved to git. Both say `unknown` rather than `ok` when they cannot see;
a check that reports green because it could not reach anything is worse than
no check.

**Channels not reading OK** — appears only when something is stale or erroring.
This panel being empty is the point of the page.

**Known faults** — channels disabled in `channels.yaml`, listed from the
configuration. `tt202` is here. A disabled channel produces no data at all, so
it cannot show up anywhere driven by measurements; listing it here keeps a known
fault visible without leaving a permanently active alarm that people learn to
ignore.

---

## Alarms

The alarm chain on one page: the thresholds in force, who is told, and — at the
bottom, where it is read rather than scrolled past on the way to the settings
above it — what has actually fired.

### Thresholds in force

**What the engine actually loaded**, read from the bus rather than from
`alarms.yaml`. The two should agree; this is how you find out when they do not.
The risk it guards against is believing a threshold is 2.0 when it is 20.

**`Unknown` is not `none`.** If the alarm engine is not running, the page says
so rather than showing an empty table. "I cannot see the engine" and "no
thresholds are configured" are different problems.

To change one: edit `alarms.yaml`, commit, `xams-ctl reload`. A threshold is an
engineering decision and keeps its review — it is deliberately not editable
here.

### Who is notified

Add, remove, or tick **Alarms** and **Notify**, then **Save recipients**. It
applies to the next alarm — no restart, no reload.

**Two lists in one table.** *Alarms* is the alarm email and SMS, at whatever
hour it fires; *Notify* is the daily report email, once a morning. Neither
implies the other: somebody can read the morning report without being woken by
an SMS, and somebody who wants to be woken need not want an email every day.

| Column | |
|---|---|
| **Name** | what the person is called. Two rows with the same name are refused |
| **Email** | where the alarm mail and the daily report go |
| **Phone** | where the SMS goes. **Blank means do not SMS them** — they get email alone, which is normal and not an omission. A number that *is* filled in needs its country code, `+31…` |
| **Alarms** | the alarm email and SMS. Off keeps somebody on the list without alarming them — a holiday, without losing the number |
| **Notify** | the daily report email. It is email only, so a row ticked here with no address is warned about |
| **Remove** | takes effect **on save**, not on click |

To add somebody, type into the blank row at the bottom. To remove somebody,
tick **Remove** and save.

**The whole list is saved as one action**, and a bad row refuses all of it —
nothing is written, nothing is half-applied, and the red banner names the row.
Four things are refused:

| | why |
|---|---|
| no name | the audit trail is kept by name; an unnamed row cannot be traced back |
| an address without a plausible `@` | it is a typo, and it fails silently at 3am |
| a phone without `+` and a country code | the gateway wants international form. Better refused here than by the gateway |
| the same name twice | two rows, one person, and no way to tell which is current |

**A recipient with neither an email nor a phone is not refused.** The row is
saved, both boxes are outlined in amber on the page, and the green banner
carries `WARNING: <name> has no email and no phone, so they are on the list and
hear nothing`. They would sit there looking notified and hear nothing, which is
worth being told — but refusing over it meant a name typed while you go and
look up a number took the whole list hostage, and the change nobody could make
was the one to the list of people who get told things go wrong.

**Emptying the alarm column is allowed, with a warning**, because it may be
exactly what you mean during an intervention. The page says so in red and the
alarm engine raises its own low-severity alarm, so it is not a state you can
drift into unnoticed. An empty *Notify* column is a quiet morning rather than
an unwatched system, so it is said in grey and nothing else happens.

Every change is audited — added, removed, which of the two lists they joined
or left, and the address or number as it was. Removals especially: once
somebody is out of the file, the audit trail is the only record that they were
ever in it.

`config/recipients.yaml` holds colleagues' addresses and mobile numbers and is
**gitignored**; `config/recipients.example.yaml` is the template that ships
with the repository.

### Active alarms

At the **bottom of the page**, and the same list as the overview, with a link
to Grafana for history — alarm state is stored like any other record, so the
plots have it and this page does not have to query a database.

---

## Logs

The last lines of any service log, without going to the filesystem, **newest
first**. The same files are in `logs\<service>.log`.

This is the second place to look when a service is unhappy; the first is
`xams-ctl status`. What the messages mean is in
[Troubleshooting](troubleshooting.md).

**The lines are coloured by level**, the way an editor colours a `.log` file:
`INFO` green, `WARNING` amber and `ERROR` red, in the same colours the overview
uses for the same idea. An error colours its message as well as the level word,
because a five-character word is easy to miss in eighty lines. `backup.log` is
written by PowerShell rather than by Python, in a shorter format with `WARN`
for `WARNING` and no service or logger column, and it is coloured on the same
scale. Everything each line
repeats — the timestamp, the service, the logger — is dimmed instead: it is
there to be checked, not read. Traceback bodies are dimmed as one block, so
the eye falls to the next timestamp rather than into the stack frames.

**This page does not refresh itself**, and it is the only one that does not: a
log that reloads while you are reading it takes the line away mid-sentence. It
says when it was read, above the text — that stamp is the page's age, and
reloading is a keystroke.

**Older lines are behind the `.1` … `.5` links**, which appear next to
`current` when a service has rotated. Logs rotate at 10 MB and five are kept,
so a talkative service can have its last hour in `.1` while the tab shows a
nearly empty `caen.log`. Anything past `.5` is gone.

`?lines=N` shows more or fewer than the default 80.

The tabs across the top are every `*.log` file in the log folder, which is why
`backup` and `sim` appear there next to the services. That list is also the
whole of what the page will open: a name that is not on it reads nothing,
whatever the URL says.

The folder is `<repo>\logs` — set `XAMS_LOG_DIR` to move it, and the services,
their pid files and this page all follow.

---

## When the interface itself is the problem

If the page does not load at all, the web service is not running:
`xams-ctl status`, then `xams-ctl start`.

If the page loads but everything is dashed, the **broker** is the likely
culprit, not the web service — it renders from retained MQTT topics. See
[Troubleshooting](troubleshooting.md#services-and-the-bus).
