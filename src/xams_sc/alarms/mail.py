"""HTML for the alarm and daily-digest emails. See DESIGN.md §11.

**Email HTML is not web HTML, and the difference is not stylistic.** Outlook
renders with Word's engine; Gmail strips `<style>` blocks it dislikes; several
clients ignore `float`, `flex` and `grid` entirely. So everything here is
tables with inline styles, which is ugly to write and the only thing that
renders the same in all of them.

Rules this file keeps to, each for a reason:

* **Tables for layout**, never flex or grid. Word's engine supports neither.
* **Inline styles**, never a stylesheet. Gmail discards `<head><style>`.
* **No images, no JavaScript, no web fonts.** Images are blocked by default in
  most clients, so anything that matters must survive without them - which
  rules out drawing status as coloured dots alone.
* **Colour is never the only signal.** A red border is decoration; the word
  ALARM is information. Roughly one reader in twelve cannot reliably
  distinguish the two colours this file leans on.
* **A plain-text alternative always.** Some people read mail as text, and a
  text part is also what stops the whole message being scored as spam.

The palette is light, not the dark theme of the web UI: a dark email looks
broken in clients that force a light background, and about half of them do.
"""

from __future__ import annotations

from datetime import datetime, timezone

# The zone these emails are READ in. `None` means the machine's own, which on
# the lab PC is Europe/Amsterdam and so follows CET/CEST without anybody
# having to remember the changeover. Set it to a ZoneInfo to pin it.
#
# Storage is untouched and stays UTC: the archive, the database, MQTT and
# every timestamp inside them (§3, §9.3). This is a rendering decision about
# one output, not a change of clock. Mixing the two is how an archive ends up
# with an hour that happens twice a year and no way to tell which is which.
DISPLAY_TZ = None


def _local(when: datetime) -> datetime:
    """An aware timestamp in the zone the reader lives in.

    The logs and the HV page already show local time, so a UTC email was the
    one place in the system that answered "when" in a different clock — and
    it is the place read at three in the morning by somebody deciding whether
    to drive in.

    A naive datetime is assumed to be UTC rather than rejected. `model.iso`
    raises on one, which is right for something being stored; refusing to
    render an ALARM email over a missing tzinfo is not.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(DISPLAY_TZ)

# Kept close to the web UI's accents so the two do not feel like different
# systems, but darkened for legibility on white.
GOOD = "#1a7f37"
WARN = "#9a6700"
BAD = "#b42318"
INK = "#1f2328"
DIM = "#656d76"
LINE = "#d8dee4"
PANEL = "#f6f8fa"

UI_URL = "http://127.0.0.1:8000"

_STATE_COLOUR = {"ok": GOOD, "minor": WARN, "major": BAD, "critical": BAD}


def colour_for(state: str) -> str:
    return _STATE_COLOUR.get(str(state).lower(), DIM)


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def shell(title: str, subtitle: str, accent: str, body: str,
          preheader: str = "") -> str:
    """Wrap rendered blocks in the outer email document.

    `preheader` is the grey line a client shows beside the subject in the
    inbox list. Left empty, clients scrape whatever text comes first, which is
    usually a heading and tells the reader nothing they did not get from the
    subject. It is hidden in the body itself.
    """
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title></head>
<body style="margin:0;padding:0;background:#eef1f4;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_esc(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#eef1f4;padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0"
         style="width:600px;max-width:100%;background:#ffffff;border:1px solid {LINE};
                border-radius:8px;overflow:hidden;
                font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <tr><td style="background:{accent};padding:18px 24px;">
      <div style="color:#ffffff;font-size:18px;font-weight:700;">{_esc(title)}</div>
      <div style="color:#ffffff;opacity:.85;font-size:13px;padding-top:3px;">{_esc(subtitle)}</div>
    </td></tr>
    <tr><td style="padding:4px 24px 24px 24px;color:{INK};font-size:14px;line-height:1.5;">
      {body}
    </td></tr>
    <tr><td style="background:{PANEL};border-top:1px solid {LINE};padding:14px 24px;
                   color:{DIM};font-size:12px;line-height:1.5;">
      XAMS slow control &middot; <a href="{UI_URL}" style="color:#0969da;">{UI_URL}</a><br>
      Sent from the lab PC. Reply to this address and nobody will read it -
      the sender is a machine.
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def heading(text: str) -> str:
    return (f'<div style="font-size:13px;font-weight:700;color:{DIM};'
            f'text-transform:uppercase;letter-spacing:.04em;'
            f'padding:22px 0 8px 0;">{_esc(text)}</div>')


def banner(word: str, detail: str, colour: str) -> str:
    """The one thing the reader must see. Word first, colour second."""
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="margin-top:18px;border-left:4px solid {colour};background:{PANEL};">
<tr><td style="padding:14px 16px;">
  <div style="font-size:17px;font-weight:700;color:{colour};">{_esc(word)}</div>
  <div style="font-size:14px;color:{INK};padding-top:4px;">{detail}</div>
</td></tr></table>"""


def table(rows: list[tuple], right_align_last: bool = True) -> str:
    """A borderless table of (label, value) or (label, value, colour)."""
    out = ['<table role="presentation" width="100%" cellpadding="0" '
           'cellspacing="0" style="border-collapse:collapse;">']
    for row in rows:
        label, value = row[0], row[1]
        colour = row[2] if len(row) > 2 else INK
        align = "right" if right_align_last else "left"
        out.append(
            f'<tr>'
            f'<td style="padding:6px 0;border-bottom:1px solid {LINE};'
            f'color:{DIM};font-size:13px;">{_esc(label)}</td>'
            f'<td align="{align}" style="padding:6px 0;border-bottom:1px solid {LINE};'
            f'color:{colour};font-size:14px;font-weight:600;'
            f'font-family:ui-monospace,Consolas,monospace;">{value}</td>'
            f'</tr>')
    out.append("</table>")
    return "".join(out)


def _reading(view) -> str:
    """One channel as a value, or an em dash when it is not trustworthy.

    A stale channel shows a dash and never its last number - the same rule as
    the web UI and the mimic (§8.2). A frozen value in a daily summary is
    worse than a gap, because it is read hours after it stopped being true.
    """
    if view is None:
        return f'<span style="color:{DIM};">not read</span>'
    if not view.healthy:
        return f'<span style="color:{WARN};">&mdash; ({_esc(view.quality)})</span>'
    unit = f' <span style="color:{DIM};font-weight:400;">{_esc(view.unit)}</span>'
    return _esc(view.formatted()) + unit


def _alarm_rows(alarms: list) -> list[tuple]:
    rows = []
    for a in alarms:
        state = str(a.get("state", "?"))
        value = a.get("value")
        shown = f"{value:.3f}" if isinstance(value, (int, float)) else "&mdash;"
        label = a.get("channel", "?")
        if a.get("description"):
            label = f"{label} - {a['description']}"
        detail = f"{state.upper()} at {a.get('threshold') or '?'}, now {shown}"
        if a.get("acknowledged"):
            detail += " (acknowledged)"
        rows.append((label, detail, colour_for(state)))
    return rows


# --------------------------------------------------------------- daily digest

def digest(state, when: datetime) -> tuple[str, str, str]:
    """The daily overview. Returns (subject, html, text).

    Ordered by what the reader needs first: is anything wrong, then the
    numbers, then the housekeeping. Somebody skimming this on a phone over
    breakfast should be able to stop reading after the banner on a good day.
    """
    overall, _ = state.overall()
    alarms = state.active_alarms()
    services = state.services()
    unhealthy = state.unhealthy_channels()
    backup = state.backup_status()

    down = [s for s in services if not s.get("healthy")]
    accent = BAD if (alarms or down) else (WARN if unhealthy else GOOD)

    if alarms:
        word, detail = ("%d ACTIVE ALARM%s" % (len(alarms), "" if len(alarms) == 1 else "S"),
                        ", ".join(_esc(a["channel"]) for a in alarms[:6]))
    elif down:
        word, detail = ("%d SERVICE DOWN" % len(down),
                        ", ".join(_esc(s["name"]) for s in down))
    elif unhealthy:
        word, detail = ("RUNNING, %d channel(s) not reading" % len(unhealthy),
                        ", ".join(_esc(c.name) for c in unhealthy[:6]))
    else:
        word, detail = "ALL OK", "Every service running, every channel reading."

    body = [banner(word, detail, accent)]

    if alarms:
        body.append(heading("Active alarms"))
        body.append(table(_alarm_rows(alarms)))

    body.append(heading("Key readings"))
    body.append(table([
        ("Main pressure", _reading(state.channel("pmain"))),
        # The insulation vacuum (PKR gauge, P105 on the P&ID), in mbar with an
        # exponent like everywhere else (24 October 2026, user).
        ("Vacuum (P105)", _reading(state.channel("p105"))),
        ("Cryostat A (tt401)", _reading(state.channel("tt401"))),
        ("Cryostat B (tt402)", _reading(state.channel("tt402"))),
        ("Gas system (tt104)", _reading(state.channel("tt104"))),
        ("Flow", _reading(state.channel("fm101"))),
        ("Integrated flow", _reading(state.channel("fm101_total"))),
        # The LN2 left in the dewar (SG401 on the P&ID): gross minus the empty
        # dewar, from the scale under it (24 October 2026, user).
        ("LN2 in the dewar", _reading(state.channel("ln2_mass"))),
    ]))

    body.append(heading("Temperature control"))
    body.append(table([
        ("Setpoint, output 1", _reading(state.channel("ls_setpoint_1"))),
        ("Heater output 1", _reading(state.channel("ls_heater_1"))),
        ("Heater power", _reading(state.channel("ls_heater_1_w"))),
    ]))

    hv = [c for c in state.channels() if c.name.endswith("_vmon")]
    live = [c for c in hv if c.healthy and c.value is not None and abs(c.value) > 1]
    body.append(heading("High voltage"))
    if live:
        body.append(table([(c.name.replace("hv_", "").replace("_vmon", ""),
                            _reading(c)) for c in live]))
    else:
        body.append(f'<div style="color:{DIM};">All channels at zero or off.</div>')

    body.append(heading("Housekeeping"))
    ups = state.channel("ups_on_battery")
    on_battery = bool(ups and ups.healthy and ups.value and ups.value > 0.5)
    backup_colour = {"ok": GOOD, "overdue": WARN}.get(backup["state"], BAD)
    backup_text = ("%.0f h ago" % backup["age_h"]) if (
        backup["state"] == "ok" and backup["age_h"] is not None) else backup["state"]
    body.append(table([
        # All are counted now that every service publishes a heartbeat. This
        # used to count only the five that did, because including the two
        # that did not made a healthy system report "5 of 7" and look as
        # though something had died.
        ("Services running",
         "%d of %d" % (len([s for s in services if s.get("healthy")]),
                       len(services))),
        ("Channels not reading", str(len(unhealthy)),
         WARN if unhealthy else GOOD),
        ("Mains", "ON BATTERY" if on_battery else "line power",
         BAD if on_battery else GOOD),
        ("Backup", _esc(backup_text), backup_colour),
        *_readout_rows(state),
        ("Configuration", _esc(state.config.config_hash)),
        # No "Uptime": it was the age of this report's own process, started a
        # few seconds earlier, so it always read 0m (removed 24 October 2026).
    ]))

    subject = "XAMS daily report - %s" % (
        "ALL OK" if accent == GOOD else word.replace("&", "and"))
    html = shell("XAMS slow control",
                 _local(when).strftime("Daily report, %A %d %B %Y, %H:%M %Z"),
                 accent, "".join(body),
                 preheader="%s. %s" % (word, detail))
    return subject, html, _digest_text(state, when, word, detail, alarms)


def _readout_rows(state) -> list[tuple]:
    """A switchable cDAQ readout that is OFF, one row each (26 October 2026).

    Off is a deliberate state - the PT1000s while the HV is on - so it is not
    counted as "not reading". It is still said every morning, in amber, so a
    readout left off after the HV came down is noticed the next day.
    """
    rows = []
    for r in state.readouts():
        if r["known"] and r["enabled"] is False:
            since = (r["at"][:16].replace("T", " ") + " UTC") if r["at"] else "?"
            rows.append((r["label"],
                         _esc(f"OFF since {since}, by {r['by'] or 'somebody'}"), WARN))
    return rows


def _digest_text(state, when, word, detail, alarms) -> str:
    lines = ["XAMS SLOW CONTROL - daily report",
             _local(when).strftime("%A %d %B %Y, %H:%M %Z"), "",
             "%s - %s" % (word, detail), ""]
    if alarms:
        lines.append("ACTIVE ALARMS")
        for a in alarms:
            lines.append("  %-18s %s at %s" % (a.get("channel"),
                                               str(a.get("state")).upper(),
                                               a.get("threshold")))
        lines.append("")
    lines.append("KEY READINGS")
    for label, name in (("main pressure", "pmain"), ("vacuum (P105)", "p105"),
                        ("tt401", "tt401"),
                        ("tt402", "tt402"), ("flow", "fm101"),
                        ("integrated flow", "fm101_total"),
                        ("LN2 in the dewar", "ln2_mass")):
        view = state.channel(name)
        lines.append("  %-18s %s" % (
            label, (view.formatted() + " " + view.unit) if view and view.healthy else "--"))
    for r in state.readouts():
        if r["known"] and r["enabled"] is False:
            lines += ["", "%s OFF since %s, by %s" % (
                r["label"], r["at"][:16].replace("T", " ") + " UTC" if r["at"] else "?",
                r["by"] or "somebody")]
    lines += ["", "Web UI: " + UI_URL]
    return "\n".join(lines)


# ---------------------------------------------------------------- alarm email

def alarm(channel: str, state_name: str, threshold, value, description: str,
          context: dict, when: datetime) -> tuple[str, str, str]:
    """One alarm, with the surrounding state. Returns (subject, html, text).

    **The context is the point.** An alarm that says only "tt302 is high" makes
    the reader open the web UI to find out whether anything else is wrong,
    whether the services are alive, and what the plant was doing - and at three
    in the morning, from a phone, that is exactly when they will not. What
    matters is here.
    """
    colour = colour_for(state_name)
    shown = f"{value:.3f}" if isinstance(value, (int, float)) else "unknown"
    label = f"{channel} - {description}" if description else channel

    detail = (f'<b>{_esc(label)}</b><br>'
              f'{_esc(str(state_name).upper())} at threshold '
              f'{_esc(threshold or "?")}, reading <b>{_esc(shown)}</b>')
    body = [banner("ALARM: %s" % channel, detail, colour)]

    others = [a for a in context.get("alarms", []) if a.get("channel") != channel]
    if others:
        body.append(heading("Also in alarm"))
        body.append(table(_alarm_rows(others)))

    readings = context.get("readings") or []
    if readings:
        body.append(heading("The plant at this moment"))
        body.append(table([(name, _reading(view)) for name, view in readings]))

    services = context.get("services") or []
    down = [s for s in services if not s.get("healthy")]
    body.append(heading("System"))
    rows = [("Services", "ALL RUNNING" if not down else
             "DOWN: " + ", ".join(_esc(s["name"]) for s in down),
             GOOD if not down else BAD)]
    if context.get("config_hash"):
        rows.append(("Configuration", _esc(context["config_hash"])))
    rows.append(("Raised", _local(when).strftime("%Y-%m-%d %H:%M:%S %Z")))
    body.append(table(rows))

    body.append(
        f'<div style="padding-top:20px;font-size:13px;color:{DIM};">'
        f'Acknowledging an alarm in the web UI stops the repeating '
        f'notification. It does not fix the condition, and the two must never '
        f'look alike.</div>')

    subject = "XAMS %s: %s%s" % (str(state_name).upper(), channel,
                                 (" (%s)" % description) if description else "")
    html = shell("XAMS alarm",
                 _local(when).strftime("%A %d %B %Y, %H:%M:%S %Z"),
                 colour, "".join(body),
                 preheader="%s %s at %s, reading %s"
                           % (channel, str(state_name).upper(), threshold, shown))

    text = ["XAMS SLOW CONTROL - ALARM", "",
            "%s: %s" % (str(state_name).upper(), label),
            "threshold %s, reading %s" % (threshold, shown),
            _local(when).strftime("raised %Y-%m-%d %H:%M:%S %Z"), ""]
    if others:
        text.append("ALSO IN ALARM")
        for a in others:
            text.append("  %s %s" % (a.get("channel"), str(a.get("state")).upper()))
        text.append("")
    if readings:
        text.append("READINGS")
        for name, view in readings:
            text.append("  %-18s %s" % (
                name, (view.formatted() + " " + view.unit)
                if view and view.healthy else "--"))
        text.append("")
    text.append("Web UI: " + UI_URL)
    return subject, html, "\n".join(text)
