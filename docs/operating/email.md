# Email: alarms and the daily report

Two kinds of message leave this system by email. Both are HTML with a plain-text
alternative, and both carry more than the one number that prompted them.

## The relay

```yaml
# config/secrets.yaml
email:
  smtp_host: smtp.nikhef.nl
  smtp_port: 25
  username: ''          # none needed
  password: ''
  from_address: xams-sc@nikhef.nl
```

Probed from the lab PC on 18 September 2026: `smtp.nikhef.nl:25` accepts mail
from this host **unauthenticated**, to internal *and* external recipients — all
three addresses in `recipients.yaml` were accepted, including the
`@student.hhs.nl` one. Port 587 is closed, so authenticated submission is not
available even if it were wanted.

There is therefore nothing secret in that block. It lives in `secrets.yaml`
because that is where the other delivery settings are, not because it needs
protecting.

!!! note "It had never worked before"

    `smtp_host` was empty until this was set up, so every email alarm since the
    system was built had been silently discarded — `send_email` logged *"no
    smtp_host in secrets.yaml; email disabled"* once and returned `False`
    thereafter. SMS was unaffected.

## The daily report

```powershell
.\.venv\Scripts\python.exe -m xams_sc.alarms.daily                   # send it
.\.venv\Scripts\python.exe -m xams_sc.alarms.daily --preview r.html  # render only
.\.venv\Scripts\python.exe -m xams_sc.alarms.daily --to you@nikhef.nl
```

Scheduled at **07:30** — after the night's backup at 03:30, so it can report
whether the backup ran:

```powershell
# elevated, once
.\tools\install_report_task.ps1 -RunNow
```

It contains, in this order: a banner saying whether anything is wrong, any
active alarms, key readings (pressure, cryostat and gas temperatures, flow,
integrated flow, the LN2 left in the dewar), the Lake Shore control state, live HV channels,
and housekeeping (services, mains, backup age, config hash, uptime).

**Ordered so a good day can be read and dismissed at the banner.** Somebody
skimming it on a phone over breakfast should not have to scroll to learn that
nothing is wrong.

`--preview` writes the HTML to a file and sends nothing, which is how to
iterate on the wording or the layout without mailing three people each time.

## Alarm emails

Sent by the alarm engine, to everyone whose route includes `email`. The message
carries the plant around the alarm, not just the channel that tripped:

- the alarm itself — channel, description, severity, threshold, reading
- **every other channel currently in alarm**
- key readings at that moment (`pmain`, `tt401`, `tt402`, `tt104`, `tt302`, `fm101`)
- which services are down, if any
- the configuration hash and the time it was raised

The reasoning is in [the design](../DESIGN.md) §11: an alarm that says only
*"tt302 is high"* sends the reader to the web UI to find out whether anything
else is wrong — and at three in the morning, from a phone, they will not go.

**SMS stays terse.** It is charged per message and read on a lock screen, so it
keeps the one-line form. Only the email is enriched.

## Why the mail is written the way it is

Email HTML is not web HTML, and the constraints are not stylistic:

| Rule | Because |
|---|---|
| Tables for layout, never flex or grid | Outlook renders with Word's engine, which supports neither |
| Inline styles, never a stylesheet | Gmail discards `<head><style>` |
| No images, no JavaScript, no web fonts | Images are blocked by default in most clients |
| Colour is never the only signal | Roughly one reader in twelve cannot reliably tell the two accents apart, and many clients strip styling |
| Always a plain-text part | Some people read mail as text, and a message without one scores worse with spam filters — a poor way to lose an alarm |
| Light palette, not the dark web UI | A dark email looks broken in clients that force a light background, and about half of them do |

**A stale reading shows a dash, never its last number** — the same rule as the
web UI and the mimic. It matters more here, because an email is read hours
after it was written by somebody who cannot glance at the instrument to check.

`tests/test_mail.py` holds these down. They are not tests of how the mail
looks; they are tests of what would make it misleading.


## Times in these emails

Local time, named: `2026-07-15 14:30:45 CEST`. It follows the machine's own
zone, so the CET/CEST changeover needs no thought and nothing to remember.

**Only the display changed.** The archive, the database and every MQTT
timestamp remain UTC (§9.3). An email is read by a person deciding whether to
drive in at three in the morning; an archive is read by software correlating
it with everything else, and those want different clocks. The zone is printed
next to the time so a message read on a phone abroad is not ambiguous.

To pin the zone rather than follow the machine — a server in another country,
say — set `mail.DISPLAY_TZ` to a `ZoneInfo`.

## When it does not arrive

1. **Check `logs/alarms.log`** for `email to ... failed:`. Delivery failures
   are logged and never raised — a gateway being down must not stop the engine
   evaluating the next reading.
2. **Check the recipient list** on the [Alarms page](webui.md#who-is-notified),
   or `recipients.yaml` directly. The two columns are two lists: an alarm goes
   to the entries with **Alarms** ticked, the daily report to those with
   **Notify** ticked, and both need an email address. An empty alarm list logs
   `ALARM NOT DELIVERED: no recipients want alarms`, and the page says so in
   red; a missing daily report logs `nobody is on the daily report with an
   email address`.
3. **Test the relay** without sending anything:

    ```powershell
    .\.venv\Scripts\python.exe -c "import smtplib; s=smtplib.SMTP('smtp.nikhef.nl',25,timeout=15); s.ehlo(); s.mail('xams-sc@nikhef.nl'); print(s.rcpt('you@nikhef.nl')); s.quit()"
    ```

    `(250, ...)` means the relay will accept it. This stops at `RCPT TO` and
    sends no message.
