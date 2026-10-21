# LN2 dewar scale (ISCI)

The platform scale under the LN₂ dewar, with an All Scales Europe **ISCI**
weight indicator: 600 kg max, 0.05 kg division, S/N 0100164095. Inside it is a
**Dini Argeo DFW**: asked for its version it answers `VER,311,DFW06`
(firmware 3.11). Connected to the SC PC and checked on 21 October 2026.

**This driver only asks.** The protocol can also tare, zero, press keys and
write on the display; `devices/scale.py` sends only the queries in its
`QUERIES` whitelist and refuses anything else before a byte leaves the port.
A tare sent by accident would turn every following reading into a net weight.

---

## The hardware

| | |
|---|---|
| Port | the **RJ45 socket on the back** of the indicator — RS-232 on the PC serial port, **not Ethernet**. Never plug it into a network switch |
| RJ45 pins | 6 = TX (indicator out), 3 = RX, 5 = GND (technical manual §6.1.1, §12.3) |
| Cable | the supplier's RJ45–USB cable (Henk Maas), FTDI `0403:6001`, USB serial `AU05TI3P` (pyserial reports `AU05TI3PA`); COM8 when installed |
| Serial | 9600, 8-N-1, every command and answer ends in CR LF |
| Indicator settings | factory: `SEtuP → SEriAL → CoM.PC`: `PCModE = ondE` (answers on request), `bAud = 9600`, `bit = n-8-1`. Nothing was changed |
| Power | 12 V DC from the mains adapter (the 2-pin round socket), plus an internal 6 V battery |

The manuals are in the workspace, `reference/weegschaal/`: the ISC technical
manual (connections, setup menu, serial commands), the user manual (DFW
series) and the ISC serial protocol (v-ISCs-3-21, the full command list).

---

## The channels

| Channel | `phys` | | |
|---|---|---|---|
| `ln2_gross` | `gross` | the scale's reading: dewar + LN₂ | kg |
| `ln2_mass` | `ln2` | the same reading minus the empty dewar | kg, `offset: 136.75` |
| `ln2_scale_mains` | `mains` | indicator on mains (1) or its own battery (0) | `bool` |

**Both weights come from one `RALL` answer**, so they cannot disagree. The
**gross** weight is used, never the net: a TARE pressed on the front panel
changes the net and leaves the gross alone, so the archive does not jump.

The empty dewar, **136.75 kg**, is the channel's offset in `channels.yaml`
(value = (raw − offset) × multiplier, §4.2). It was given on 21 October 2026
as "I believe"; confirm it the next time the dewar is empty. `ln2_gross` is
the instrument's own number and is archived as well, so a better tare later
is a config change and the history can be recomputed.

---

## One cycle

Every second (the service default), two queries:

| Query | Answer (recorded 21 Oct 2026) | Used for |
|---|---|---|
| `RALL` | `ST,1, 278.20kg, 278.20kg,     0.00kg,   0.00kg,000,001,…` | status, net, **gross**, tare |
| `ALIM` | `PW: 1 BT: 9` | mains (1) / battery (0), battery level 0–9 |

The 10-s mean is published, as for every service. At 9600 baud the indicator
answers up to 14 requests a second.

Identity at startup and on every reconnect: the port is chosen by its **USB
serial first**, and only then asked `VER`, which must contain `DFW06`. The
turbo's RS-485 tap is an FTDI adapter with the same vid:pid, and a `VER`
written to it would go onto the pump's bus — the one thing that service
exists never to do. A scale that is not plugged in is therefore a refusal
without anything being probed.

---

## What a fault looks like from the outside

| What you see | What it means |
|---|---|
| `ln2_gross` and `ln2_mass` quality=error, `ln2_scale_mains` fine | the indicator says **OL / UL / TL** (overload, underload, not level), or reports a unit other than kg; the reason is in `scale.log` |
| all three error, service DEGRADED | no answer to `RALL`: cable out, indicator off, or the PC port mode is not `ondE`; the service reconnects and asks `VER` again |
| refuses to start: "no serial port with … serial AU05TI3PA" | the USB cable is not plugged in, or a different cable (other serial) is used — update `devices.yaml` |
| refuses to start: "answered VER with …" | something else answers on that port, or the indicator's PC port settings changed |
| `ln2_scale_mains` = 0 | the indicator runs on its own battery (~60 h); the Control page says so |

A unit other than kg is refused, not converted: a scale switched to lb would
otherwise read 2.2 times too heavy with nothing looking wrong.

---

## Alarms

On `ln2_mass` (`alarms.yaml`): **warning** (minor, email) below **100 kg**,
**alarm** (major, SMS + email + sound) below **20 kg**; 2 % hysteresis, so they
clear above 102 and 20.4 kg.

`ln2_mass` is the one entry in `switchable`: its notifications can be switched
off on their own from the LN₂ tile on the Control page, for when the dewar is
deliberately empty or away. Like the master switch on Alarms it stops only the
email and SMS — the value still turns amber and red and is listed on Alarms.
The switch survives a restart (`data/alarm_armed.json`) and is audited. See
[Alarm engine](../software/alarms.md#switching-one-alarm-off).

---

## What it deliberately cannot do

- tare, zero, press a key or write on the display — not in the whitelist;
- change the indicator's setup (port mode, baud rate): that is done on the
  indicator's keypad, by hand;
- know the dewar's empty weight by itself: that is configuration.

The protocol commands that do these things (`TARE`, `T`, `TMAN`, `ZERO`, `Z`,
`C`, `CLEAR`, `KEYP`, `DISP`, `CMDSAVE`, `PID`, `ALDL`, …) are left out of
`QUERIES` on purpose. Adding one is a write path, and §10 of the design
applies.
