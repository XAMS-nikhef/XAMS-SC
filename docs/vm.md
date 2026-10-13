# The Nikhef VM (plotit-xams)

<https://plotit-xams.nikhef.nl> — the group's view of the slow control. The same
Grafana dashboards as on the lab PC, over a copy of the data, readable from
anywhere inside Nikhef or on the VPN without sitting at the lab PC. In place
since 23 September 2026 (DESIGN.md §12).

```
  lab PC (Windows)                              plotit-xams (Ubuntu 24.04)
  ────────────────                              ──────────────────────────
  MQTT ─┬─► pg_writer          → local PostgreSQL
        ├─► jsonl_writer       → data/raw/        the truth
        └─► pg_writer nikhef-vm ─ TLS, 5432 ─►  PostgreSQL 18
                                                     │ 127.0.0.1
                                                  Grafana (127.0.0.1:3000)
                                                     │
  browsers, Nikhef network / VPN ── HTTPS 443 ──► nginx
```

Three rules make it safe to have:

1. **Every connection goes out from the lab PC.** Nothing on the VM ever
   connects to the lab PC, and the MQTT broker — which carries the commands that
   set a high voltage — stays bound to `127.0.0.1`.
2. **The lab PC does not depend on the VM.** If the VM is down, only the remote
   writer notices: it logs a warning and buffers. The local database, the
   archive, the alarms and the web UI carry on untouched, and the `sinks`
   service does not turn amber.
3. **The VM holds no unique data.** Its database is a copy, rebuilt from the
   JSONL archive at any time with [a replay](operating/tasks.md#replaying-the-archive-into-a-database).

**It is view-only.** There is no control path on the VM, and the slow-control
web UI is not there — only Grafana. The lab PC's web UI, its "open in Grafana"
links and the P&ID popups keep using the lab PC's own Grafana.

---

## Who can reach what

| From | To the VM | How |
|---|---|---|
| a browser at Nikhef or on the VPN | 443 | <https://plotit-xams.nikhef.nl>, anonymous viewer |
| the lab PC (`192.16.192.82`) | 5432 | the remote writer, TLS, `xams_writer` |
| your own machine | 22 | `ssh plotit-xams` (user `ubuntu`, sudo) |
| the internet | nothing | blocked by the Nikhef firewall |

**The lab PC has no ssh route to the VM** — only 5432 (and 443 like anyone).
Anything that needs a shell on the VM is done from your own machine, and
anything the lab PC needs from the VM (the certificate, the password) is
carried over by hand.

The VM's own firewall (ufw) admits ssh, 80 and 443 from anywhere and 5432 from
the lab PC's address alone; `pg_hba.conf` admits the writer from that address
alone, and only over TLS.

---

## Installing and updating: one script

Everything on the VM is set up by **`tools/vm/install_vm.sh`**. It is
idempotent — running it again changes nothing that is already right — so the
same command installs, updates and repairs:

```bash
ssh plotit-xams
sudo /opt/xams-sc/tools/vm/install_vm.sh
```

It first does `git pull` in `/opt/xams-sc` and restarts itself on the fresh
copy, so **updating the VM is: push to GitHub, run the script.**

On a brand-new VM, clone first:

```bash
sudo git clone https://github.com/acolijn/XAMS-SC.git /opt/xams-sc
sudo SERVER_NAME=plotit-xams.nikhef.nl LABPC_IP=192.16.192.82 \
     /opt/xams-sc/tools/vm/install_vm.sh
```

**What it does:**

| | |
|---|---|
| PostgreSQL 18 | from the PGDG repository; TLS with a self-signed certificate that the lab PC pins; database `xams`, schema from `sql/schema.sql` |
| Roles | `xams_admin` owns the schema · `xams_writer` may INSERT into `meas`, `alarm_events`, `rga_saves` and `rga_summary` and read only their key columns · `xams_reader` may SELECT |
| Grafana | on `127.0.0.1:3000` only; datasource from `grafana/provisioning-vm/`; anonymous Viewer; dashboards loaded from `grafana/dashboards-archive/` on the first run, and on later runs only those **not yet there** (`save_dashboard.py --load --missing`) |
| nginx | HTTPS in front of Grafana; port 80 redirects |
| ufw | as in the table above |
| Watchdog | the "lab PC silent" alert, only when `WATCHDOG_EMAIL` is set |

### Settings

Given as environment variables in front of the command, and **remembered** in
`/etc/xams-vm/vm.env`, so later runs need none of them:

| Setting | | Now |
|---|---|---|
| `LABPC_IP` | the one address allowed to write | `192.16.192.82` |
| `SERVER_NAME` | the public name | `plotit-xams.nikhef.nl` |
| `ANON_VIEW` | `yes` = view without login; `no` = login required | `yes` |
| `SMTP_HOST` | mail relay, `host:port`, for the watchdog | *not set* |
| `WATCHDOG_EMAIL` | who hears when the lab PC goes silent | *not set* |
| `SMTP_FROM` | sender | `xams-watchdog@<SERVER_NAME>` |
| `USE_UFW` | manage the host firewall | `yes` |
| `ACME` | get the web certificate by ACME (certbot), renewed automatically | `yes` |
| `ACME_SERVER` | the CA's ACME directory | HARICA, Nikhef's account |
| `ACME_EMAIL` | account contact | *not set* |

Changing one is running the script with the new value, e.g.

```bash
sudo LABPC_IP=<new address> /opt/xams-sc/tools/vm/install_vm.sh
```

### Where things are on the VM

| | |
|---|---|
| `/opt/xams-sc` | the repository, read-only clone (no push rights) |
| `/etc/xams-vm/vm.env` | the settings above |
| `/etc/xams-vm/credentials` | the four generated passwords, root only |
| `/etc/xams-vm/plotit-xams-ca.crt` | the database certificate, public — the lab PC's copy |
| `/etc/letsencrypt/live/plotit-xams.nikhef.nl/` | the web certificate (certbot's directory, whichever CA) |
| `/etc/xams-vm/web/` | the self-signed fallback, used only when `ACME=no` |
| `/etc/postgresql/18/main/conf.d/xams.conf`, `pg_hba.conf` | written by the script; edits are overwritten |
| `/etc/systemd/system/grafana-server.service.d/xams.conf` | Grafana's settings, as environment |
| `/var/lib/grafana/grafana.db` | Grafana's own database: the live dashboards |

The passwords, from your own machine:

```bash
ssh plotit-xams sudo cat /etc/xams-vm/credentials
```

---

## Connecting the lab PC

The writer is in the ordinary `sinks` service and does nothing until
`config/secrets.yaml` has a `postgres_remote:` block. Removing the block (or
starting the sinks with `--no-remote`) switches it off again.

1. **The certificate.** Its text, from your own machine:
   `ssh plotit-xams cat /etc/xams-vm/plotit-xams-ca.crt`. Save it on the lab PC
   as `config\plotit-xams-ca.crt` — in PowerShell, paste it between `@'` and
   `'@ | Set-Content -Encoding ascii config\plotit-xams-ca.crt`. `config/*.crt`
   is in `.gitignore`.
2. **The block**, with the writer password from `/etc/xams-vm/credentials`:

    ```yaml
    postgres_remote:
      host: "plotit-xams.nikhef.nl"
      port: 5432
      database: "xams"
      user: "xams_writer"
      password: "<PG_WRITER_PW>"
      sslmode: "verify-full"
      sslrootcert: "config/plotit-xams-ca.crt"
    ```

    `verify-full` checks the certificate *and* the name, so the lab PC knows it
    is talking to this VM.

3. **Test, then restart:**

    ```powershell
    Test-NetConnection plotit-xams.nikhef.nl -Port 5432
    .\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); from xams_sc.sinks.__main__ import dsn_from_secrets; import psycopg; print(psycopg.connect(dsn_from_secrets('postgres_remote')).pgconn.ssl_in_use)"
    xams-ctl restart
    Get-Content logs\sinks.log -Tail 20      # "[nikhef-vm] connected"
    ```

4. **The history**, once: [replay the archive](operating/tasks.md#replaying-the-archive-into-a-database)
   into the VM, and `--data-dir data\imported` for the LabVIEW years.

Only measurements, alarm transitions and the **RGA summaries** (`rga_saves`,
`rga_summary`, since 13 October 2026) go to the VM. Flow-integrator periods,
the audit trail and the RGA spectra (the files in `data/rga/`) stay on the lab
PC. An RGA summary the VM missed — it was down at the Save — is sent again
with `tools\rga_rebuild_summary.py --remote`; rows already there are left
alone (the writer may only INSERT, so `--replace` is refused with `--remote`).

---

## When the VM is down

Nothing on the lab PC changes except one line in `logs\sinks.log`. The remote
writer keeps up to **300 000 rows** — about sixteen hours at the normal rate —
and writes them when the VM is back. Past that it discards new rows as they
arrive and says so, naming the replay that puts them back:

```
[nikhef-vm] 1200 row(s) not sent (1200 since start): backlog full. They are in
the archive; once the VM is back, fill the gap with
`python tools/replay_jsonl.py --since 2026-09-24`
```

That is a WARNING, not DATA LOST: the rows are in the archive. Alarm
transitions during an outage are not buffered, so the VM's alarm history has a
gap; the lab PC's does not.

---

## Dashboards on the VM

**The VM's Grafana owns its dashboards.** An edit saved in the UI goes into
`/var/lib/grafana/grafana.db` on the VM and nowhere else — not into git, not to
the lab PC. The installer loads the whole archive only on its first run; later
runs load only dashboards the VM does not have yet (how the RGA dashboard got
there), so rerunning it never undoes an edit.

To log in: **Sign in**, top right, as `admin` with `GRAFANA_ADMIN_PW` from the
credentials file. Better, make yourself a personal Admin or Editor account under
*Administration → Users and access → Users* and keep `admin` for emergencies.

**Edit on the VM only**, then save to git from your own machine through a
tunnel:

```bash
ssh -L 3001:127.0.0.1:3000 plotit-xams                     # terminal 1
python3 tools/save_dashboard.py --check --url http://127.0.0.1:3001 --password <pw>
python3 tools/save_dashboard.py --save  --url http://127.0.0.1:3001 --password <pw>
git add grafana/dashboards-archive && git commit -m "grafana: ..." && git push
```

and bring the lab PC's Grafana into line with `git pull` and
`tools\save_dashboard.py --load` there. Editing both Grafanas makes them drift,
and a later `--load` then overwrites one side without a word ([Drift](grafana/drift.md)).
Nothing yet checks the VM's dashboards for unsaved edits.

---

## The web certificate

A browser-trusted certificate from **HARICA** (GÉANT TLS), through Nikhef's
ACME account, issued 24 September 2026 and valid to April 2027.
**`certbot.timer` renews it by itself**, twice daily checks, and a deploy
hook reloads nginx when it does. Nothing to do by hand.

The installer runs certbot with `ACME=yes` and the HARICA directory URL from
Nikhef CT in `ACME_SERVER`. HARICA needs *External Account Binding*: a Key ID
and HMAC key from CT, needed only once, to register the account. They went
into a root-only file written by hand, never into git, vm.env or this chat:

```bash
ssh -t plotit-xams 'sudo install -m 600 /dev/null /etc/xams-vm/acme-eab && sudo nano /etc/xams-vm/acme-eab'
#   ACME_EAB_KID=<Key ID>
#   ACME_EAB_HMAC=<HMAC key>
```

The installer used them to register and then **deleted the file**; renewals
use the registered account in `/etc/letsencrypt/accounts/`. So the keys are
needed again only on a new VM, or if that directory is lost — ask CT.

Nikhef has evidently validated nikhef.nl with HARICA in advance: issuance
worked although nothing on the internet can reach the VM. Let's Encrypt could not:
it must fetch a file over port 80 from outside, and the Nikhef firewall does
not let it.

Check it from anywhere at Nikhef:

```bash
echo | openssl s_client -connect plotit-xams.nikhef.nl:443 2>/dev/null | openssl x509 -noout -issuer -enddate
ssh plotit-xams 'sudo certbot certificates'
```

**Do not test renewal with `certbot renew --dry-run`.** A dry run is sent to
Let's Encrypt's staging server, not to HARICA, and that server cannot reach the
VM — so it always fails here, and says nothing about the real renewal. What
matters is that `/etc/letsencrypt/renewal/plotit-xams.nikhef.nl.conf` names the
HARICA `server` and the registered `account`, and that `systemctl list-timers
certbot.timer` shows the timer. Renewal starts about a month before expiry.

**If renewal ever fails**, the site keeps serving the old certificate until it
expires. `journalctl -u certbot` and `/var/log/letsencrypt/letsencrypt.log`
say why; the usual cause would be the ACME account having been withdrawn by
CT.

---

## The watchdog

The reason the VM is worth having at all: an alert on a machine that is not the
lab PC, for the one failure the lab PC cannot report — itself being off.

```
no measurement for 15 minutes  →  email       (no data and errors fire too)
```

The rule is `grafana/provisioning-vm/alerting/watchdog.yaml`. It is installed
only when both are known:

```bash
sudo WATCHDOG_EMAIL=<address> SMTP_HOST=<relay:port> /opt/xams-sc/tools/vm/install_vm.sh
```

**Not yet active**: the SMTP relay is still to be had from CT. Test it once it
is, by stopping the sinks outside a run and waiting sixteen minutes.

---

## Troubleshooting

**`permission denied for table meas`** in the lab PC's `sinks.log`, right after
`[nikhef-vm] connected`. The writer lacks SELECT on the key columns, which
`ON CONFLICT (t, channel, src)` needs to find an existing row. Rerun the
installer; it grants exactly those three columns.

**`root certificate file "C:Users...crt" does not exist`** — the backslashes
have gone. That was a bug, fixed on 23 September 2026 (`git pull`): inside a
quoted connection-string value a backslash is an escape, so the path is now
passed with forward slashes.

**`ssh: connect to host plotit-xams port 22: Connection timed out`** on the lab
PC. Expected — see [who can reach what](#who-can-reach-what). Run it from your
own machine.

**Nothing arrives, and the log says nothing.** Check on the VM:

```bash
ssh plotit-xams 'sudo -u postgres psql -d xams -c "select now()-max(t) from meas"'
ssh plotit-xams 'sudo -u postgres psql -d xams -c "select client_addr, ssl from pg_stat_activity join pg_stat_ssl using (pid) where usename = \$\$xams_writer\$\$"'
```

The second should show the lab PC's address and `t`.

**The disk.** The VM has 48 GB, about 440 days at roughly 100 MB a day; the
installer warns below 100 GB free. Ask CT to enlarge it before it matters.

---

## Starting again from nothing

The VM holds nothing that is not somewhere else, so the recovery for anything
beyond repair is a new VM:

1. ask CT for the VM and the 5432 rule from the lab PC;
2. clone and run the installer (above);
3. carry the **new** certificate and writer password to the lab PC — both are
   regenerated;
4. replay the archive;
5. reload the dashboards: they come from git, so any edit not saved there is
   gone — which is the reason to save them.
