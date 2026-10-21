# Hardware, as found on the lab PC

Verified 17 September 2026.

| Device | Identity |
|---|---|
| cDAQ-9174 | serial `20C5E1C`, modules 9207 / 9216 / 9216 / 9226 |
| | 19 live channels: 6 voltage, 13 RTD (`tt202` failed, awaiting replacement) |
| CAEN DT1470ET | serial **19198**, firmware 1.08 — PMT bottom, PMT top, top screen, bottom screen |
| CAEN DT1470ET | serial **79**, firmware 1.04 — cathode, (ch 1 disconnected), anode, gate (ch 3, **positive**, moved from ch 1 on 14 Oct 2026 for the SiPM-neutral point CM-B; NaI no longer powered from here) |
| Lake Shore 335 | `335A12T` on COM6 |
| UPS | APC, `3S2005X18782`, USB HID |
| Turbo tap | EXSYS EX-13009, FTDI `0403:6001` serial `DIBIURQ3`, listen-only on the HiPace 80 RS-485 |
| LN₂ scale | All Scales Europe ISCI (Dini Argeo DFW06, fw 3.11), S/N 0100164095; RJ45 RS-232 via FTDI `0403:6001` serial `AU05TI3P` |

Neither CAEN unit exposes a USB serial number, so they are told apart by asking
each board for its own `BDSNUM` — never by COM port number (§6.2).

Of the cDAQ's 40 available channels, **20 are connected**: 6 voltage inputs on
the 9207, 7 PT1000 on the 9226, and 7 PT100 on the first 9216. The second 9216
is entirely free — eight spare PT100 inputs already wired into the chassis.
