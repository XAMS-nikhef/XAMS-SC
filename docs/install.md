# Installation

**This page is for someone reinstalling the system from scratch.** To run a
system that is already installed, see [Operating](operating/index.md). If the
old PC is lost rather than merely being reinstalled, read
[Backup and restore](operating/backup.md) first: the measurement archive comes
back from the Nikhef cluster, and that is the one thing that cannot be redone.

## Order of work

The sections below are numbered by topic; on a new PC do them in this order.

| Step | What | Section |
|---|---|---|
| 0 | Windows, network name, OpenSSH Server (with Nikhef CT); fetch the archive and the NI driver from the cluster | [Backup and restore](operating/backup.md#restoring) |
| 1 | Git, Python ≥ 3.11, clone, `.venv`, `pip install -e ".[hardware,api,notify,docs]"`, `xams_ctl check` | [§1](#1-python) |
| 2 | NI-DAQmx 24.5.0 from the archived cache, the NI-MAX aliases | [§2](#2-ni-daqmx-and-the-module-aliases) |
| 3 | Mosquitto, PostgreSQL, Grafana; `config/secrets.yaml` | [§3](#3-mosquitto-postgresql-and-grafana) |
| 4 | Put the archive back and replay it into PostgreSQL | [§7](#7-putting-the-data-back) |
| 5 | Grafana first login, load the dashboards | [§4](#4-grafana-first-login) |
| 6 | Check the cDAQ on its own | [§2.3](#23-check-it) |
| 7 | Install the XAMS services as Windows services | [§5](#5-windows-services) |
| 8 | Scheduled tasks: nightly backup, daily report | [§8](#8-scheduled-tasks-backup-and-daily-report) |
| 9 | Read-only access for the DAQ (SSH) | [§9](#9-read-only-access-for-the-daq) |
| 10 | Check the whole system | [§10](#10-checking-the-installation) |

[§11](#11-versions-this-system-runs-with) lists the versions of everything as
read from the lab PC; install those, not "the latest".

## 1. Python

Python 3.11 or newer. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Add `.[hardware]` for the device drivers (`nidaqmx`, `pyserial`, `lakeshore`).
`nidaqmx` is only a wrapper — it needs the NI-DAQmx driver from §2, and
**installing the Python package does not install the driver**. The serial
instruments need no vendor driver: the CAEN supplies and the Lake Shore
enumerate through Windows' generic `usbser.sys`, and the UPS through HID.

Everything except the cDAQ works without §2. The simulated service publishes
every enabled channel, and the sinks, alarms, web UI and Grafana run on it
unchanged — which is exactly why a missing NI-DAQmx is not noticed until
`xams-ctl start` reaches the cDAQ service.

Check that the configuration is valid — this starts nothing:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl check
```

## 2. NI-DAQmx, and the module aliases

**This is the one step that is not scripted, and the one most likely to be
missed.** The lab PC has had NI-DAQmx installed since before this project
existed, because LabVIEW ran on it — so on that machine this section has never
been performed by anyone. On a clean machine it must be.

Two separate things, and only the second comes from pip:

| | |
|---|---|
| **NI-DAQmx** | the driver: `nicaiu.dll`, NI-MAX, Windows services. A download from NI |
| **`nidaqmx`** | the Python wrapper, installed by `.[hardware]` in §1 |

### 2.1 The driver

Free of charge but **not freely available**: closed source, and the download is
behind an ni.com account. It cannot be scripted the way §3 scripts Mosquitto
and Grafana, because there is a login in the way. Installed through NI Package
Manager.

!!! danger "NI-DAQmx **24.5.0** — install this version, not “the latest”"

    Read from the lab PC on 18 September 2026. This is the version that was
    demonstrably driving the cDAQ-9174, including
    `ni-daqmx-cdaq-firmware 24.5.0`.

    Record the number rather than "the latest", for the ordinary reason: it is
    the version this system has been run and verified against, and a driver
    upgrade is a change worth making deliberately rather than by accident.

    On 18 September 2026 NI’s download page offered **26.5.0**, not 24.5.0.
    26.5.0 has not been tried against this hardware, so upgrading is a thing to
    do on purpose, with the cDAQ service watched afterwards — not a thing to do
    while rebuilding a dead machine.

    !!! note "Correction, 18 September 2026"

        An earlier version of this page called the cDAQ-9174 a **discontinued
        chassis**. **That was wrong.** NI lists it as *Active* and sells it
        (about €1,680). The claim went unchecked into this page and then into
        [Backup and restore](operating/backup.md), where it was used to argue
        that the driver might become unobtainable. It might not. The archived
        copy is still worth having — see there for the honest reason.

**The driver is archived on the cluster**, at
`/data/xenon/xams_slow_control/software/` (see
[Backup and restore](operating/backup.md)):

| | |
|---|---|
| `nipm-package-cache-2026-09-18.tar` | the whole NI Package Manager cache from the lab PC, ~6.8 GB, **containing NI-DAQmx 24.5.0** |
| `ni-daqmx_26.5.0_offline.iso` | NI’s current offline installer, 3.2 GB, version **not** verified against the 9174 |

A rebuild therefore does not depend on someone remembering an ni.com password,
on the account still existing, on NI still publishing 24.5.0, or on the lab PC
reaching the internet at all.

Two honest caveats, both recorded in the README beside those files: the cache
needs **NI Package Manager itself**, which is not archived because it was not
present on the lab PC as a standalone installer; and **nobody has yet installed
24.5.0 from that cache onto a clean machine.** It is very likely to work, and
that is not the same as knowing.

**Do not run the hardware services under WSL2.** USB passthrough to WSL2 does
not work with NI-DAQmx. Anything that touches an instrument runs natively on
Windows; see [the decision document](OPTIONS.md).

### 2.2 The aliases — do not skip this

This system addresses cDAQ modules by **alias**, never by slot, so that a
module moved between slots is harmless. Aliases are set in NI-MAX and stored in
the **host's** configuration database — *not* on the chassis. A fresh Windows
install therefore has none of them, however untouched the hardware is, and the
cDAQ service refuses to start with:

```
FATAL: module alias '9207' not found in NI-MAX. Aliases are configured
there and this service addresses modules by alias, never by slot.
```

Correct behaviour, and baffling if you do not know why. In NI-MAX, rename each
module to the alias below. The serials are authoritative — they are what
`config/devices.yaml` checks at every startup, and a mismatch is fatal by
design:

| Alias | Model | Slot | Serial |
|---|---|---|---|
| `9207` | NI 9207 | 1 | `020DFD57` |
| `9216_1` | NI 9216 | 2 | `020F64D1` |
| `9216_2` | NI 9216 | 3 | `020F64D0` |
| `9226` | NI 9226 | 4 | `02159CBB` |

The chassis itself must appear as `cDAQ1`, serial `020C5E1C`.

If a module has genuinely been replaced, the new serial goes into
`config/devices.yaml` as a deliberate, committed change. Never edit it to make
an error go away.

### 2.3 Check it

The identity check runs at **service startup**, so the way to verify this
section is to start the cDAQ service and read the first few log lines. There is
no one-shot mode.

That needs the broker, so it has to wait until §3 is done:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices cdaq
```

A good start logs the chassis serial and then the serial of every module that
has enabled channels as confirmed, before any reading is published. Ctrl-C once
you have seen them. Any `FATAL:` line names exactly what disagrees — a missing
alias, or a serial that does not match `config/devices.yaml`.

A module with **no enabled channels gets no task**, and its serial is then not
checked either (`no task for 9216_2 - no enabled channels`). That is expected,
not a fault.

A known-good start, recorded on the lab PC on 28 September 2026
(`logs\cdaq.log`; timestamps and the config hash will differ):

```text
INFO [cdaq] xams_sc.devices.cdaq: module 9207 (NI9207): 8 enabled channels
INFO [cdaq] xams_sc.devices.cdaq: module 9216_1 (NI9216): 7 enabled channels
INFO [cdaq] xams_sc.devices.cdaq: module 9226 (NI9226): 6 enabled channels
INFO [cdaq] xams_sc.devices.cdaq: no task for 9216_2 - no enabled channels
INFO [cdaq] xams_sc.bus: connected to broker 127.0.0.1:1883
INFO [cdaq] xams_sc.devices.cdaq: chassis cDAQ1 serial 20C5E1C confirmed
INFO [cdaq] xams_sc.devices.cdaq: module 9207 serial 20DFD57 confirmed
INFO [cdaq] xams_sc.devices.cdaq: module 9216_1 serial 20F64D1 confirmed
INFO [cdaq] xams_sc.devices.cdaq: module 9226 serial 2159CBB confirmed
INFO [cdaq] xams_sc.devices.cdaq: task for 9207 ready (8 channels)
INFO [cdaq] xams_sc.devices.cdaq: task for 9216_1 ready (7 channels)
INFO [cdaq] xams_sc.devices.cdaq: task for 9226 ready (6 channels)
INFO [cdaq] xams_sc.service: cdaq started (simulate=False, read 1.0s, log 10.0s, config 6652d1c)
INFO [cdaq] xams_sc.service: state: starting -> running
```

The log prints serials without the leading zero (`20C5E1C` for `020C5E1C`).

## 3. Mosquitto, PostgreSQL and Grafana

One script does all three, in an **elevated** PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\tools\setup_services.ps1
```

It asks for two passwords (the `postgres` superuser, and one for the `xams`
role it creates), then downloads and installs the three services, binds all of
them to loopback, creates the database, applies `sql/schema.sql`, writes
`config/secrets.yaml`, and verifies that every port is listening on `127.0.0.1`
**and nowhere else**.

It is idempotent — if a step fails, fix the cause and run it again. Roughly
870 MB of downloads, staged in `tools/installers/` (gitignored) so a re-run
does not fetch them twice.

**Why the loopback binding matters.** Control commands travel over MQTT
(`xams/cmd/#`), so anything that can reach the broker can set a high voltage. A
broker listening on `0.0.0.0` — the default in most tutorials — is an
unauthenticated control interface on the building network. Remote viewing is
done by a read-only mirror, [the Nikhef VM](vm.md), never by opening
the port (§8).

**What the script does not do:** install the XAMS services themselves as
Windows services. That is [§5](#5-windows-services), deliberately a separate
step: a service that starts at boot claims the instruments, and every device
admits only one process.

## 4. Grafana first login

<http://127.0.0.1:3000>, `admin` / `admin`, and change the password when asked.

The **XAMS** datasource is provisioned from this repository. **Dashboards are
not** — provisioning refuses every UI save, so Grafana owns the live dashboard
and `grafana/dashboards-archive/` is the copy git tracks. On a fresh install,
load them:

```powershell
.\.venv\Scripts\python.exe tools\save_dashboard.py --load --password <admin pw>
```

Afterwards the direction reverses: edit in the UI, then
`tools\save_dashboard.py --save` and commit. A dashboard that exists only in
Grafana's own database is lost when that database is — see
[Grafana](grafana/index.md).

## 5. Windows services

The steps above leave the XAMS services as plain processes, which would not
survive a reboot. Install them as Windows services, in an **elevated**
PowerShell from the repository root:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\tools\install_services.ps1
```

It downloads NSSM 2.24 into `tools\installers\` and installs the services,
each started at boot and restarted on a crash, running as `LocalSystem`:
`XAMS-sinks`, `XAMS-cdaq`, `XAMS-caen`, `XAMS-lakeshore`, `XAMS-ups`,
`XAMS-turbo`, `XAMS-rga`, `XAMS-scale`, `XAMS-derived`, `XAMS-alarms`, `XAMS-webui`. It also builds this manual (§6).
Idempotent: re-running updates the services in place.

| Option | |
|---|---|
| `-Manual` | restart on crash, but not at boot |
| `-Uninstall` | remove the services again |
| `-Only <name>` | add or update only the named service(s); nothing else is stopped. How a new driver joins a running system, e.g. `-Only turbo` |

To hand the instruments to another program, `xams-ctl stop --for-labview` stops
the services **and** suspends their auto-start, so a reboot does not quietly
take the hardware back; `xams-ctl start` restores both.

Check: `xams-ctl status` lists every service running, and
`Get-Service XAMS-*` shows them `Running` / `Automatic`.

## 6. The manual

This documentation is served by the web UI itself, at
<http://127.0.0.1:8000/manual>, so it is present on the lab PC whether or not
the building network is. `install_services.ps1` builds it. By hand:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[docs]"
.\.venv\Scripts\python.exe tools\build_docs.py
```

## 7. Putting the data back

Only on a replacement PC; a reinstall on the same disk keeps `data\`.

1. Copy the archive back from `/data/xenon/xams_slow_control/archive/` on the
   cluster into the repository: `data\raw\`, `data\events\`,
   `data\quarantine\` and `data\fm101_total.json` (the flow integrator's
   running total). If the old disk is still readable, take its `data\` folder
   as well — it may hold the last day, which the nightly copy had not sent yet.
2. Fill the new database from it. `setup_services.ps1` (§3) has applied
   `sql\schema.sql`; then, dry run first:

```powershell
.\.venv\Scripts\python.exe tools\replay_jsonl.py --target postgres --since <first day> --dry-run
.\.venv\Scripts\python.exe tools\replay_jsonl.py --target postgres --since <first day>
```

Details and options: [Routine tasks](operating/tasks.md#replaying-the-archive-into-a-database).
The alarm history, flow periods and audit trail are only in PostgreSQL and do
not come back ([why](operating/backup.md#the-database-is-not-entirely-an-index)).
The Nikhef VM needs nothing: it keeps its own copy.

## 8. Scheduled tasks: backup and daily report

Two Windows Scheduled Tasks, registered once from an **elevated** prompt. Both
run as `localadmin` (not as a service: the backup's SSH key lives in that
user's profile, see [Backup and restore](operating/backup.md#how-it-runs)):

```powershell
.\tools\install_backup_task.ps1 -RunNow     # "XAMS nightly backup", daily 03:30
.\tools\install_report_task.ps1             # "XAMS daily report",   daily 07:30
```

The backup needs, before it can work:

- a **dedicated** SSH key without passphrase in
  `C:\Users\localadmin\.ssh\` (on the lab PC `id_ed25519_xams_backup`),
  used for nothing else;
- two entries in `C:\Users\localadmin\.ssh\config`: `nikhef-jump`
  (`login.nikhef.nl`) and `nikhef-backup` (`stbc-i2.nikhef.nl`, with
  `ProxyJump nikhef-jump`), both with that key;
- the public key authorized on a Nikhef account that can write
  `/data/xenon/xams_slow_control/archive/`.

The report needs `email.smtp_host` in `config/secrets.yaml`; without it the
task exits with an error and nothing is sent.

Check: `-RunNow` ends with a verified copy, and the overview page of the web UI
shows `backup ok`. `Get-ScheduledTaskInfo "XAMS nightly backup"` shows
`LastTaskResult 0`.

## 9. Read-only access for the DAQ

The XAMS DAQ stores the slow-control values at the start and end of every run
in its run database. It reads them over SSH, with a key that can do exactly one
thing: fetch `/api/state` from the web UI on this PC. Nothing on the lab PC
connects to the DAQ.

1. **OpenSSH Server**: install the Windows optional feature, start `sshd`,
   start type Automatic.
2. **`C:\ProgramData\ssh\sshd_config`**: comment out the Windows default
   block at the end,

    ```text
    #Match Group administrators
    #       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
    ```

    so that `localadmin` (an administrator) uses its own
    `C:\Users\localadmin\.ssh\authorized_keys`. Keep
    `PubkeyAuthentication yes`. Restart `sshd`.
3. **`authorized_keys`**: the maintainers' personal public keys, one per line,
   and the DAQ's key restricted to the one command:

    ```text
    command="curl.exe -s -m 10 http://127.0.0.1:8000/api/state",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 <DAQ public key> xams sc_snapshot read-only
    ```

    Write it as plain ASCII, one key per line. In PowerShell, build the lines as
    an array (`@(...)`) and use `Set-Content -Encoding ascii`; appending to a
    string with `+=` glues the lines together and every key on it stops working.
4. The DAQ side (its key, its `known_hosts` after a reinstall, the test) is
   described in the XAMS handbook, "Rebuild the slow-control PC".

Check from the DAQ host: `ssh <alias>` returns the JSON state within a few
seconds. Whatever command the client asks for, the forced command runs instead,
so the key cannot do anything else.

## 10. Checking the installation

- `xams-ctl status`: every service running. The web UI
  (<http://127.0.0.1:8000>) and Grafana (<http://127.0.0.1:3000>) show current
  values for every enabled channel.
- Ports: 1883, 3000, 5432 and 8000 listen on `127.0.0.1` only
  (`Get-NetTCPConnection -State Listen`); only `sshd` (22) faces the network.
- The cDAQ log shows the chassis and every module with enabled channels
  confirmed (§2.3).
- The backup ran once successfully (§8) and the overview shows it.
- The Nikhef VM ([plotit-xams](vm.md)) shows new data.
- The DAQ's read-only access works (§9).

## 11. Versions this system runs with

Read from the lab PC on 3 October 2026. Install these, not "the latest"; an
upgrade is a separate, deliberate change.

| Component | Version |
|---|---|
| Windows | 10 Enterprise LTSC, build 19044, 64-bit |
| Python (`.venv`) | 3.12.0 (3.11 also works) |
| NI-DAQmx | 24.5.0, cDAQ firmware 24.5.0 |
| NI Measurement & Automation Explorer | 24.3.0 |
| NI Package Manager | 25.3 |
| Mosquitto | 2.1.2 |
| PostgreSQL | 18.6-1 |
| Grafana | 13.2.2 |
| NSSM | 2.24 |
| Git | 2.42.0 |

Python packages in `.venv` (there is no lock file; these are the versions that
run):

| Package | Version | Package | Version |
|---|---|---|---|
| nidaqmx | 1.6.0 | fastapi | 0.141.1 |
| pyserial | 3.5 | uvicorn | 0.53.0 |
| lakeshore | 1.10.0 | Jinja2 | 3.1.6 |
| pywinusb | 0.4.2 | messagebird | 2.2.0 |
| paho-mqtt | 2.1.0 | PyYAML | 6.0.3 |
| psycopg / psycopg-binary | 3.3.5 | mkdocs-material | 9.7.7 |
