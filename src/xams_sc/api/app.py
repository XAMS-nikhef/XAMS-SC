"""Web UI. See DESIGN.md §8.1.

    python -m xams_sc.api

**Binds to 127.0.0.1, never 0.0.0.0.** `0.0.0.0` is the default in most
tutorials and in much example code; on this system it would expose the control
surface to the building network. Every bind address is explicit, and loopback
is the only accepted value without a recorded decision to the contrary (§8).

That bind keeps the NETWORK out and nothing else. The browser on this PC can
reach this port whatever it is bound to, so every request is also checked for
where it came from - see `same_origin_only` in `create_app`, and "What
loopback does not protect against" in DESIGN.md §8.

**Almost everything here is read-only.** Three things are not:

  * the flow-integrator reset, which changes a record rather than hardware
    (§7.5)
  * the Lake Shore setpoint, on output 1
  * the Lake Shore heater range, on output 1

The last two write to an instrument (§10). This module validates none of it
beyond "is that a number": the range, the connection, the read-back and the
audit record all belong to the service that owns the port, so a command from
this page is treated exactly like one from the CLI. Duplicating the limits
here would mean two numbers to keep in step, and the copy in channels.yaml is
the one that counts.

Deliberately plain: server-rendered HTML, a meta refresh, no JavaScript
framework and no build step. In three years a student must be able to change
it without installing a toolchain (§8.1).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bus import (ACK_RGA_CONTROL, ACK_RGA_EM, ACK_RGA_EMISSION, ACK_RGA_SAVE, ACK_RGA_SCAN,
                   TOPIC_RGA_CONTROL, TOPIC_RGA_EM, TOPIC_RGA_EMISSION, TOPIC_RGA_SAVE, TOPIC_RGA_SCAN,
                   ACK_HV_CLEAR, ACK_HV_OUTPUT, ACK_HV_VSET, ACK_LS_RANGE,
                   ACK_LS_SETPOINT, ACK_NOTIFY, TOPIC_AUDIT, TOPIC_HV_CLEAR,
                   TOPIC_HV_OUTPUT, TOPIC_HV_VSET, TOPIC_LS_RANGE, TOPIC_LS_SETPOINT,
                   TOPIC_NOTIFY, TOPIC_RELOAD, Bus)
from ..config import (ConfigError, load, read_hv_defaults,
                      read_recipients, recipient_warnings, recipients_path,
                      validate_recipients, write_hv_defaults,
                      write_recipients)
from ..hv_status import (ARMED_ABOVE_V, describe_status, is_disabled,
                         is_energised, is_tripped, ramping, status_faults)
from ..grafana import DriftWatcher, base_url as grafana_base_url
from .logview import (ROTATIONS, colourise, log_files, newest_first,
                      rotated)
from .mimic import check_mimic_tags
from .recipients_form import people_from_form, recipient_changes
from .state import SystemState

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


OPERATOR_COOKIE = "xams_operator"


def safe_next(target: str) -> str:
    """Where to return after setting the operator.

    Only a path on this site. A redirect target taken from a form is an open
    redirect if it is used as given - "//evil.example" is a protocol-relative
    URL, not a path - so anything that is not a single-slash path is thrown
    away rather than repaired.
    """
    target = (target or "/").strip()
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target
# ------------------------------------------------- cross-site protection
#
# The loopback bind (see the module docstring) keeps the BUILDING network
# out. It does NOT keep out the browser already running on this PC: any
# page an operator opens can POST a form to 127.0.0.1:8000, and a plain
# form POST needs no CORS preflight, so the browser sends it and the
# command is carried out. `/alarms/notify` off, `/hv/output` off, a
# setpoint written, all with a chosen name in the `by` field. Loopback is
# a network boundary, not a browser one, and §8 leaned on it for both.

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host_name(value: str) -> str:
    """The name out of a `Host` header or an authority, WITHOUT the port.

    `localhost:8080` -> `localhost`, `[::1]:8000` -> `[::1]`. The brackets
    stay: they are how an IPv6 address is written in an authority, and
    stripping them would make two spellings of the same host look different.
    """
    value = (value or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[:end + 1] if end != -1 else value
    return value.split(":", 1)[0]


def _origin_host(url: str) -> str | None:
    """The host an `Origin` or `Referer` names, or None if it names none."""
    if not url:
        return None
    parts = urlsplit(url)
    # `Origin: null` is what a sandboxed frame sends. It parses to no scheme
    # and no authority, and it is not this site.
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return _host_name(parts.netloc)


def allowed_host_names(host: str) -> frozenset[str]:
    """The host NAMES this server answers to. The port is not one of them.

    Checked because nothing else checks it: without this, a domain the
    attacker controls can be re-pointed at 127.0.0.1 (DNS rebinding) and
    the browser then treats this site as SAME ORIGIN - which hands out
    /api/state and the recipient list, names and mobile numbers included,
    to a page on the internet. The Origin check below cannot see that,
    because to the browser it genuinely is same-origin by then.

    **THE PORT IS DELIBERATELY IGNORED**, and it costs nothing. Rebinding
    works by putting the ATTACKER'S OWN NAME in the Host header, so
    `evil.example` is refused whatever port follows it; matching the port as
    well added precision, not protection.

    What it does buy is the SSH tunnel, which §8 names as the way to reach
    this machine from outside. The forwarded port is chosen on the far end:
    `ssh -L 8080:127.0.0.1:8000` makes the browser send `localhost:8080`,
    and a colleague whose own 8000 is already taken by something else has no
    other option. Refusing that meant a 421 and a dead page on the one route
    that exists for an emergency - the route you least want to discover is
    broken at the moment you need it.

    The bind address is included as well as loopback: §8 allows another one
    with a recorded decision, and a UI that refuses every request the moment
    somebody takes that decision is a trap.
    """
    return frozenset({"127.0.0.1", "localhost", "[::1]", "::1",
                      _host_name(host)})


def operator_of(request: Request, submitted: str = "") -> str:
    """Who is performing this action (section 10 rule 5).

    Taken from a cookie set once, rather than typed before every command.
    Retyping a name for each setpoint is the kind of friction that gets
    worked around - by leaving it blank, which costs the audit trail the
    thing it exists for.

    Still taken on trust: there is no login on this UI, so this identifies
    a browser, not a person. That is weak and is recorded honestly rather
    than dressed up. A name given on the request itself wins, so the CLI and
    the API are unaffected by any of this.
    """
    name = (submitted or "").strip()
    if not name:
        name = (request.cookies.get(OPERATOR_COOKIE) or "").strip()
    return name or "webui (unnamed)"


@lru_cache(maxsize=8)
def _operator_names(config_dir: str, stamp: int) -> tuple[str, ...]:
    """The parse behind `operator_names`, kept off the render path.

    Keyed on the directory as well as the timestamp: the tests point
    CONFIG_DIR at a directory of their own, and a cache keyed on the
    timestamp alone would hand one test another's names.
    """
    return tuple(
        name for name in
        (str(person.get("name", "")).strip()
         for person in read_recipients(config_dir))
        if name)


def operator_names() -> list[str]:
    """The names the "acting as" box SUGGESTS: everyone on the recipient list.

    Suggestions, not a closed list, and the box stays a text field. The
    recipient list answers "who is told when an alarm fires", which is not
    the same question as "who is at the keyboard": a student on a measurement
    shift or a technician swapping a pump belongs in the audit trail without
    belonging in the SMS list. Offering only these five would push such a
    person into picking somebody else's name, and a plausible wrong name in
    the audit trail is worse than the `webui (unnamed)` they get for typing
    nothing. What this does buy is one spelling per person: `apc`,
    `AP Colijn` and `Auke-Pieter Colijn` are three different people to
    anything that reads the audit topic later.

    Everyone is offered, `enabled` or not. Disabled means "do not notify me",
    which is usually somebody away for a week - exactly when a colleague is
    the one operating.

    Read from the file, not from `state.config`: it is edited on /alarms and
    the names must follow without a restart, the same way the alarm engine
    picks the list up at send time. Cached on the file's timestamp because
    this renders in the header of every page and most pages reload every
    10 s.
    """
    path = recipients_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        # No file, or one that cannot be read: nothing to suggest. The box
        # is a text field and keeps working with no list behind it.
        return []
    return list(_operator_names(str(path.parent), stamp))


def _remember(response, name: str):
    """Keep the operator name for a year. Harmless if never set."""
    if name and name != "webui (unnamed)":
        response.set_cookie(OPERATOR_COOKIE, name, max_age=365 * 24 * 3600,
                            samesite="lax", path="/")
    return response


async def _form(request: Request):
    """The submitted form as a plain dict."""
    return dict(await request.form())


def _short(channel: str) -> str:
    """`hv_cathode_vset` reads as `cathode` in a message to a person."""
    return channel.replace("hv_", "").replace("_vset", "")


def _limits_view(state) -> dict:
    """The thresholds in force, arranged for display (§11).

    Returns `{"known": bool, "rows": [...]}`. **`known` is false when the
    engine has published nothing**, and the page then says so rather than
    showing an empty table - "no thresholds are configured" and "I cannot see
    the alarm engine" are different facts, and only one of them is about the
    plant.
    """
    published = state.limits()
    if published is None:
        return {"known": False, "rows": []}

    order = ("lolo", "low", "high", "hihi")
    rows = []
    for channel in sorted(published):
        thresholds = published[channel] or {}
        cfg = state.config.channels.get(channel)
        rows.append({
            "channel": channel,
            "description": cfg.description if cfg else "",
            "unit": cfg.unit if cfg else "",
            "levels": [(name, thresholds.get(name)) for name in order],
        })
    return {"known": True, "rows": rows}


def _audit(state, actor: str, action: str, target: str, old, new,
           result: str = "ok", detail: str = "") -> None:
    """Record a change on the audit topic (§10 rule 5).

    The web UI audits this one directly, where every write to an INSTRUMENT is
    audited by the service that owns the port. The rule is the same in both
    cases - whoever performs the change records it - and here the thing being
    changed is a file this process owns, not a port.
    """
    import json as _json
    from ..model import utcnow
    state.bus.publish_raw(TOPIC_AUDIT, _json.dumps(
        {"t": utcnow().isoformat().replace("+00:00", "Z"),
         "actor": actor or "unknown", "action": action, "target": target,
         "old": None if old is None else str(old),
         "new": None if new is None else str(new),
         "result": result, "detail": detail}, separators=(",", ":")))


def _json_age(age_s: float) -> float | None:
    """An age as JSON can carry it: a number, or `null` for "never".

    A channel that has never reported has an age of infinity, which strict
    JSON cannot express. `json.dumps` raises on it rather than writing
    `Infinity`, so one silent channel used to take the entire `/api/state`
    response down with it - the machine-readable view of the system failing
    precisely when part of the system had stopped talking. `null` is how the
    same "no reading yet" is already reported for a service heartbeat.
    """
    return None if age_s is None or not math.isfinite(age_s) else round(age_s, 1)


def _redirect_hv(error: str | None, ok: str | None = None):
    """Back to /hv, carrying what happened. POST then redirect: that page
    reloads itself, and a refresh must not repeat a write to an instrument."""
    from urllib.parse import quote
    if error:
        return RedirectResponse("/hv?hv_error=" + quote(error), status_code=303)
    return RedirectResponse("/hv?hv_ok=" + quote(ok or "done"), status_code=303)


def _redirect_rga(error: str | None, ok: str | None = None):
    """Back to the RGA page, carrying what happened (POST then redirect)."""
    from urllib.parse import quote
    if error:
        return RedirectResponse("/rga?error=" + quote(error), status_code=303)
    return RedirectResponse("/rga?ok=" + quote(ok or "done"), status_code=303)


def _redirect_ls(error: str | None, ok: str | None = None):
    """Back to the overview, carrying what happened. POST then redirect: a
    refresh must not repeat a write to an instrument."""
    from urllib.parse import quote
    if error:
        return RedirectResponse("/hv?ls_error=" + quote(error) + "#cryostat",
                                status_code=303)
    return RedirectResponse("/hv?ls_ok=" + quote(ok or "done") + "#cryostat",
                            status_code=303)


def create_app(broker: str = "127.0.0.1", port: int = 1883, *,
               http_host: str = "127.0.0.1",
               http_port: int = 8000) -> FastAPI:
    """The application. `http_host`/`http_port` are where THIS server
    will be reached, which the cross-site check below needs to know: a
    UI started on another port must not lock itself out."""
    # The INITIAL configuration only. Handlers read `state.config`, which is
    # replaced on `xams-ctl reload` — a description or a unit edited in
    # channels.yaml must appear on the page without restarting anything, or
    # the reload looks as though it did nothing.
    config = load()
    bus = Bus(client_id="webui", host=broker, port=port)
    state = SystemState(config, bus)
    state.start()

    for problem in check_mimic_tags(config):
        log.warning("mimic drift: %s", problem)

    app = FastAPI(title="XAMS Slow Control", docs_url=None, redoc_url=None)
    app.state.system = state
    app.state.config = config
    # Cached: this page reloads every 10 s and must not make an HTTP request
    # to Grafana per view, nor wait on one.
    drift = DriftWatcher()
    app.state.drift = drift

    host_names = allowed_host_names(http_host)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Refuse what did not come from this site's own pages.

        Two checks, against two different attacks, and neither replaces the
        other:

          * the `Host` header, on EVERY request, against DNS rebinding
          * the `Origin` (or failing that the `Referer`), on the methods that
            change something, against an ordinary cross-site form POST

        No token, no session, nothing to keep in step with ten templates -
        which is the §8.1 trade: a student has to be able to read this.

        `curl` posting to this port is refused too, and that is intended: the
        CLI talks MQTT, not HTTP, and nothing in the repository posts here.
        """
        if _host_name(request.headers.get("host", "")) not in host_names:
            # 421, not 403: the request reached the wrong server for that
            # name, which is exactly what this status is for.
            log.warning("refused a request for host %r",
                        request.headers.get("host", ""))
            return PlainTextResponse("wrong host for this server",
                                     status_code=421)
        if request.method in UNSAFE_METHODS:
            # The Referer is consulted ONLY when there is no Origin. An
            # Origin that is present and wrong is an answer, not a gap to
            # fall through.
            origin = request.headers.get("origin")
            came_from = (_origin_host(origin) if origin
                         else _origin_host(request.headers.get("referer", "")))
            if came_from not in host_names:
                # Logged, and loudly: if a browser ever withholds both
                # headers on a same-origin form, the operator sees a control
                # that does nothing and this line is the only explanation
                # anywhere. It is on the Logs page under `webui`.
                log.warning("refused a cross-site %s %s (origin %r)",
                            request.method, request.url.path, origin)
                return PlainTextResponse(
                    "cross-site request refused - open this page from "
                    "http://%s:%d, or through an SSH tunnel to it, and try "
                    "again" % (http_host, http_port), status_code=403)
        return await call_next(request)

    def page(request: Request, name: str, **context):
        status, css = state.overall()
        # state.config, not the copy captured at startup: it is replaced on
        # reload and the page must show the current one.
        return templates.TemplateResponse(request, name, {
            "state": state, "config": state.config, "overall": status,
            "overall_class": css, "alarms": state.active_alarms(),
            # Site-wide (§4.4a): a page must never be able to show a healthy
            # system without showing that its alarms are switched off.
            "notify": state.notifications(),
            # Site-wide: the operator identifies the session, not one
            # instrument, and the HV control surface will want the same name
            # without setting it again (section 10 rule 5).
            "operator": request.cookies.get(OPERATOR_COOKIE, ""),
            # The names that box offers. Suggestions only - see
            # `operator_names`.
            "operators": operator_names(),
            # Read once here rather than hardcoded per template (§8.2a): the
            # navigation link and the mimic popup must never be able to disagree.
            "grafana_url": grafana_base_url(),
            **context})

    # ------------------------------------------------------------- pages

    @app.get("/system", response_class=HTMLResponse)
    def overview(request: Request):
        """Is the SOFTWARE all right? Services, faults, channels not reading.

        Was `/` and was called Overview, and it carried the Lake Shore and
        flow-reset controls because they had nowhere else to be. Two jobs on
        one page: "is everything all right" and "change something". They are
        asked at different moments and they are now on different pages — the
        controls moved to /hv, which is the Control page.
        """
        return page(request, "overview.html",
                    services=state.services(),
                    unhealthy=state.unhealthy_channels(),
                    faults=state.known_faults(),
                    flow=state.flow_total(),
                    drift=drift.get(),
                    backup=state.backup_status(),
                    lakeshore=lakeshore_view(),
                    operator=request.cookies.get(OPERATOR_COOKIE, ""),
                    ls_ok=request.query_params.get("ls_ok"),
                    ls_error=request.query_params.get("ls_error"),
                    reset=request.query_params.get("reset"),
                    reset_total=request.query_params.get("total"),
                    ups=state.channels("ups"))

    def lakeshore_view():
        """The Lake Shore as the landing page shows it (§8.1).

        Sensors and outputs are listed SEPARATELY rather than paired up. Which
        input drives which output is set by OUTMODE on the instrument and is
        not read here, so pairing them on the page would be a guess presented
        as a fact — and a convincing one, since output 1's setpoint and tt401
        currently agree to a millikelvin.
        """
        # OUTPUT 1 ONLY. Output 2 is not used on this cryostat, and a row
        # that always reads 0 % against a setpoint nobody acts on is noise on
        # the page people are meant to scan in one glance (section 8.1).
        #
        # It is still READ, still archived, and still on /status - an unused
        # heater that starts doing something remains worth catching. What
        # changes here is only that it has no place on the landing page.
        return {
            "sensors": [state.channel(n) for n in ("tt401", "tt402")],
            "outputs": [
                {"n": n,
                 "setpoint": state.channel("ls_setpoint_%d" % n),
                 "percent": state.channel("ls_heater_%d" % n),
                 "watts": state.channel("ls_heater_%d_w" % n)}
                for n in (1,)],
        }

    @app.post("/operator")
    def set_operator(request: Request, operator: str = Form(""),
                     next: str = Form("/")):
        """Remember who is at the keyboard, so commands need no name.

        Set from the header, so it can be submitted from any page and has to
        return to the one it was submitted from.
        """
        name = (operator or "").strip()[:40]
        response = RedirectResponse(safe_next(next), status_code=303)
        if name:
            return _remember(response, name)
        response.delete_cookie(OPERATOR_COOKIE, path="/")
        return response

    @app.post("/hv/apply")
    def hv_apply(request: Request):
        """Write the setpoints the operator has filled in (§10a).

        **Every non-empty box is applied, as one action.** Empty boxes are
        left alone, so a page full of channels can be moved one at a time or
        all together without a different button for each case.

        The service does the validating - range, polarity, whether the channel
        is enabled, and the read-back. Duplicating those rules here would mean
        two copies, and the copy that matters is the one next to the hardware.
        """
        import asyncio
        form = asyncio.run(_form(request))
        who = operator_of(request, form.get("by", ""))

        results, failures = [], 0
        for name, raw in form.items():
            if not name.startswith("hv_") or not name.endswith("_vset"):
                continue
            text = (raw or "").strip()
            if not text:
                continue
            try:
                value = float(text)
            except ValueError:
                results.append(f"{name}: {text!r} is not a number")
                failures += 1
                continue
            answer = state.command(TOPIC_HV_VSET, ACK_HV_VSET,
                                   {"channel": name, "value": value,
                                    "by": who},
                                   match=("channel",))
            if answer.get("ok"):
                results.append(f"{_short(name)} now {answer['new']:+.1f} V")
                log.warning("HV setpoint %s -> %+.1f by %s", name, value, who)
            else:
                results.append(f"{_short(name)}: {answer.get('reason')}")
                failures += 1

        if not results:
            return _redirect_hv(None, "nothing to apply - every box was empty")
        joined = "; ".join(results)
        return _redirect_hv(joined if failures else None,
                            None if failures else joined)

    # ------------------------------------------------- HV default setpoints
    #
    # The one place this UI edits a file in git, and it is allowed for the
    # reason §8.1 gave in advance: the need is demonstrated. This is an R&D
    # setup, the operating point changes often, and the alternative is an
    # editor and a reload for a number that reaches no instrument.
    #
    # It stays inside the line §8.1 draws. `limits` are NOT editable here:
    # they are the range the write path validates against, and a page that
    # could widen its own limit and then write to it is not a guardrail.

    @app.get("/hv/defaults", response_class=HTMLResponse)
    def hv_defaults(request: Request):
        """Edit the values `load defaults` offers (§4.6)."""
        raw = read_hv_defaults()
        rows = []
        for ch in state.config.channels.values():
            if ch.kind != "hv_vset":
                continue
            rows.append({
                "name": ch.name,
                "label": _short(ch.name),
                "description": ch.description,
                "default": ch.default_setpoint,
                "limits": ch.limits or {},
                "unit": ch.unit,
            })
        rows.sort(key=lambda r: r["name"])
        return page(request, "hv_defaults.html", rows=rows,
                    updated=raw.get("updated"), updated_by=raw.get("by"),
                    saved=request.query_params.get("saved"),
                    error=request.query_params.get("error"))

    @app.post("/hv/defaults")
    def hv_defaults_save(request: Request):
        """Write hv_defaults.yaml, then reload so /hv offers the new values.

        **Validated here against `channels.yaml`, and written all or nothing.**
        A partial save would leave the file describing a set of defaults
        nobody chose, which is the failure this page exists to prevent.
        """
        import asyncio
        from urllib.parse import quote
        form = asyncio.run(_form(request))
        who = operator_of(request, form.get("by", ""))

        values: dict[str, float] = {}
        problems: list[str] = []
        for ch in state.config.channels.values():
            if ch.kind != "hv_vset":
                continue
            if ch.name not in form:
                # ABSENT is not the same as EMPTY, and conflating them loses
                # data. A box left blank on this page is submitted as an empty
                # string and means "no default". A field that is not in the
                # form at all did not come from this page - a partial POST, or
                # some future caller sending one channel - and must leave the
                # other seven alone rather than clear them.
                keep = state.config.channels[ch.name].default_setpoint
                if keep is not None:
                    values[ch.name] = keep
                continue
            text = (form.get(ch.name) or "").strip()
            if not text:
                # An empty box means "no default for this channel" - the box
                # on /hv is then simply not filled by the button, which is
                # what hv_pmt_top looked like before anyone set one.
                continue
            try:
                volts = float(text)
            except ValueError:
                problems.append(f"{_short(ch.name)}: {text!r} is not a number")
                continue
            if not ch.limits:
                problems.append(
                    f"{_short(ch.name)}: no limits in channels.yaml, so no "
                    f"default can be applied to it")
                continue
            if not ch.in_limits(volts):
                problems.append(
                    f"{_short(ch.name)}: {volts:+.1f} V is outside "
                    f"{ch.limits['min']:+.0f}..{ch.limits['max']:+.0f} V. "
                    f"Limits live in channels.yaml and are not changed here.")
                continue
            values[ch.name] = volts

        if problems:
            return RedirectResponse(
                "/hv/defaults?error=" + quote("; ".join(problems)),
                status_code=303)

        # What changed, for the audit trail - computed before the write, while
        # the old values are still loaded.
        before = {ch.name: ch.default_setpoint
                  for ch in state.config.channels.values()
                  if ch.kind == "hv_vset"}

        try:
            write_hv_defaults(values, who)
        except OSError as exc:
            log.exception("could not write hv_defaults.yaml")
            return RedirectResponse(
                "/hv/defaults?error=" + quote(f"could not write the file: {exc}"),
                status_code=303)

        # Reload before reporting success. If the file we just wrote does not
        # load, saying "saved" would be a lie of exactly the kind §7.5 calls
        # out: reporting work that did not take effect.
        try:
            state.reload_config()
        except ConfigError as exc:
            log.error("hv_defaults.yaml written but will not load: %s", exc)
            return RedirectResponse(
                "/hv/defaults?error=" + quote(f"written, but it will not "
                                              f"load: {exc}"),
                status_code=303)

        changed = 0
        for name, old in sorted(before.items()):
            new = values.get(name)
            if (old is None and new is None) or (
                    old is not None and new is not None and old == new):
                continue
            changed += 1
            _audit(state, who, "hv_default", name, old, new)
            log.warning("HV default %s: %s -> %s by %s", name, old, new, who)

        # Every other service re-reads too, so `xams-ctl check` and a second
        # browser do not sit on the old file.
        state.bus.publish_raw(TOPIC_RELOAD, "{}")

        if not changed:
            return RedirectResponse("/hv/defaults?saved=" +
                                    quote("saved - nothing was different"),
                                    status_code=303)
        return RedirectResponse(
            "/hv/defaults?saved=" + quote(
                f"saved {changed} default{'s' if changed != 1 else ''}; "
                f"/hv now offers them"),
            status_code=303)

    @app.post("/hv/output")
    def hv_output(request: Request, channel: str = Form(""),
                  on: str = Form(""), by: str = Form("")):
        """Energise or de-energise one channel (§10a step 4).

        NOT the enable switch: that is a hand operation at the supply and
        nothing here can change it. This energises a channel that is already
        enabled, and it ramps to whatever setpoint is loaded - which is why
        the answer says which voltage that is.
        """
        who = operator_of(request, by)
        wanted = on.strip().lower() in ("1", "true", "on", "yes")
        answer = state.command(TOPIC_HV_OUTPUT, ACK_HV_OUTPUT,
                               {"channel": channel.strip(), "on": wanted,
                                "by": who},
                               match=("channel",))
        if answer.get("ok"):
            detail = answer.get("detail") or ("on" if wanted else "off")
            log.warning("HV output %s -> %s by %s", channel,
                        "on" if wanted else "off", who)
            return _redirect_hv(None, f"{_short(channel)}: {detail}")
        return _redirect_hv(f"{_short(channel)}: {answer.get('reason')}")

    @app.post("/hv/clear")
    def hv_clear(request: Request, channel: str = Form(""),
                 by: str = Form("")):
        """Acknowledge and clear a tripped channel (§10a *Recovering from a trip*).

        The service zeroes and switches off every tripped channel on that
        supply, then clears the board alarm. The channel is left OFF at 0 V;
        turning it back on and restoring its voltage are separate steps.
        """
        who = operator_of(request, by)
        answer = state.command(TOPIC_HV_CLEAR, ACK_HV_CLEAR,
                               {"channel": channel.strip(), "by": who},
                               match=("channel",))
        if answer.get("ok"):
            log.warning("HV trip cleared on %s by %s", channel, who)
            return _redirect_hv(None, f"{_short(channel)}: {answer.get('detail')}")
        return _redirect_hv(f"{_short(channel)}: {answer.get('reason')}")

    @app.post("/lakeshore/setpoint")
    def lakeshore_setpoint(request: Request, value: str = Form(""),
                           by: str = Form("")):
        """Change the Lake Shore setpoint (§10).

        This page validates NOTHING beyond "is it a number". The range, the
        instrument being connected, the read-back and the audit record all
        live in the service that owns the port, so a command from the CLI
        gets exactly the same treatment as one from this form. Duplicating
        the range here would mean two numbers to keep in step, and the copy
        in channels.yaml is the one that counts.
        """
        who = operator_of(request, by)
        try:
            wanted = float((value or "").strip())
        except ValueError:
            return _redirect_ls("that is not a number")
        result = state.command(TOPIC_LS_SETPOINT, ACK_LS_SETPOINT,
                               {"output": 1, "value": wanted, "by": who},
                               match=("output",))
        if result.get("ok"):
            log.warning("setpoint changed to %.3f C by %s", wanted, who)
            return _redirect_ls(None, "setpoint now %.3f C" % result["new"])
        return _redirect_ls(result.get("reason", "refused"))

    @app.post("/lakeshore/range")
    def lakeshore_range(request: Request, range: str = Form(""),
                        by: str = Form("")):
        """Switch the heater on (high) or off (§10)."""
        who = operator_of(request, by)
        result = state.command(TOPIC_LS_RANGE, ACK_LS_RANGE,
                               {"output": 1, "range": (range or "").strip(),
                                "by": who},
                               match=("output",))
        if result.get("ok"):
            log.warning("heater range set to %s by %s", result["new"], who)
            return _redirect_ls(None, "heater %s" % result["new"])
        return _redirect_ls(result.get("reason", "refused"))

    @app.post("/flow/reset")
    def flow_reset(request: Request, by: str = Form("")):
        """Close the flow period and open a new one (§7.5).

        The one thing this UI can change, and it changes a RECORD, not an
        instrument: the closed period keeps its total and its gaps in
        `flow_periods`. That is the whole difference between this and zeroing
        a counter, and it is why it can exist before milestone 8.

        Audited like any other control action (§10), which means it needs a
        name. There is no login on this UI, so the name is typed and taken on
        trust — weak, but a weak attribution recorded honestly beats an
        anonymous one, and it matches what `xams-ctl flow-reset --by` does.

        POST, then redirect: a GET that mutates would fire on a refresh or a
        prefetch, and this page refreshes itself every ten seconds.
        """
        who = operator_of(request, by)
        closed = state.reset_flow(who)
        if closed is None:
            log.warning("flow reset by %s was not acknowledged", who)
            return RedirectResponse("/hv?reset=failed#flow", status_code=303)
        log.info("flow period closed by %s: %.3f g over %.0f s of gaps",
                 who, closed.get("total_g", 0.0), closed.get("gaps_s", 0.0))
        return RedirectResponse(
            "/hv?reset=ok&total=%.3f#flow" % closed.get("total_g", 0.0),
            status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request):
        by_device: dict[str, list] = {}
        for view in state.channels():
            by_device.setdefault(view.device, []).append(view)
        return page(request, "status.html", by_device=by_device)

    @app.get("/rga", response_class=HTMLResponse)
    def rga(request: Request):
        """The residual gas analyser: what it is, who holds it, what it is doing.

        STATUS ONLY (step 1 of docs/drivers/rga.md): nothing on this page
        reaches the analyser. Scans, Save and the emission button arrive in
        later steps.
        """
        from ..devices.prismapro import SYSTEM_STATUS_BITS, decode_system_status

        channels = {c.name: c for c in state.channels("rga")}
        status = channels.get("rga_status")
        bits = None
        if status is not None and status.healthy and status.value is not None:
            word = int(status.value)
            bits = {"word": f"0x{word:08X}",
                    "on": [name for name, on in decode_system_status(word).items() if on],
                    "named": len(SYSTEM_STATUS_BITS)}
        # The page judges on/off from the service's own record, published
        # every read (every second while something switches), not from the
        # rga_* channels, which are 10-s means: deciding the buttons from
        # those brought "Switch emission on" back while emission was already
        # coming on (13 October 2026). The channels remain the fallback when
        # the record is old or missing.
        info = state.rga_info()
        live = False
        try:
            from ..model import parse_iso
            age = (datetime.now(timezone.utc) - parse_iso(info["t"])).total_seconds()
            live = age < 30
        except (KeyError, TypeError, ValueError):
            pass
        return page(request, "rga.html", info=info, live=live,
                    channels=channels, bits=bits,
                    ok=request.query_params.get("ok"),
                    error=request.query_params.get("error"))

    # The RGA's writes live on its own page, not on Control: they are one
    # workflow - take it, scan, look, save, give it back - around a plot, and
    # splitting them from the spectrum they act on would serve nobody. The
    # page is still a thin form: validation, the read-back and the audit
    # record are all in the RGA service, as for every other instrument.

    @app.post("/rga/control")
    def rga_control(request: Request, action: str = Form(""),
                    force: str = Form(""), by: str = Form("")):
        who = operator_of(request, by)
        result = state.command(TOPIC_RGA_CONTROL, ACK_RGA_CONTROL,
                               {"action": action, "force": force == "yes", "by": who},
                               timeout_s=15.0)
        if result.get("ok"):
            return _redirect_rga(None, "control " + result.get("detail", action))
        return _redirect_rga(result.get("reason", "refused"))

    @app.post("/rga/emission")
    def rga_emission(request: Request, action: str = Form(""), by: str = Form("")):
        """Emission On or Off. The p105 guard and every other check are in the
        RGA service; this form only asks."""
        who = operator_of(request, by)
        result = state.command(TOPIC_RGA_EMISSION, ACK_RGA_EMISSION,
                               {"action": action, "by": who}, timeout_s=15.0)
        if result.get("ok"):
            return _redirect_rga(None, result.get("detail", action))
        return _redirect_rga(result.get("reason", "refused"))

    @app.post("/rga/em")
    def rga_em(request: Request, action: str = Form(""), by: str = Form("")):
        """Electron multiplier On or Off. Its p105 limit and every other check
        are in the RGA service; this form only asks."""
        who = operator_of(request, by)
        result = state.command(TOPIC_RGA_EM, ACK_RGA_EM,
                               {"action": action, "by": who}, timeout_s=15.0)
        if result.get("ok"):
            return _redirect_rga(None, result.get("detail", action))
        return _redirect_rga(result.get("reason", "refused"))

    @app.post("/rga/scan")
    def rga_scan_command(request: Request, action: str = Form(""),
                         recipe: str = Form(""), by: str = Form("")):
        who = operator_of(request, by)
        result = state.command(TOPIC_RGA_SCAN, ACK_RGA_SCAN,
                               {"action": action, "recipe": recipe, "by": who},
                               timeout_s=20.0)
        if result.get("ok"):
            return _redirect_rga(None, result.get("detail", action))
        return _redirect_rga(result.get("reason", "refused"))

    @app.post("/rga/save")
    def rga_save(request: Request, comment: str = Form(""), tags: str = Form(""),
                 background: str = Form(""), by: str = Form("")):
        who = operator_of(request, by)
        tag_list = [t.strip() for t in tags.replace(";", ",").split(",") if t.strip()]
        result = state.command(TOPIC_RGA_SAVE, ACK_RGA_SAVE,
                               {"comment": comment.strip(), "tags": tag_list,
                                "background": background.strip(), "by": who},
                               timeout_s=15.0)
        if result.get("ok"):
            return _redirect_rga(None, result.get("detail", "saved"))
        return _redirect_rga(result.get("reason", "refused"))

    @app.get("/rga/file/{name}")
    def rga_file_download(name: str):
        """One saved RGA measurement, as a download. Only a name of the
        saved-file pattern is served, from the RGA data directory and nowhere
        else."""
        from fastapi.responses import FileResponse
        from .. import rga_file
        from ..config import ROOT
        if not rga_file.FILE_NAME.fullmatch(name):
            return PlainTextResponse("not an RGA file name", status_code=404)
        spec = state.config.devices.get("rga", {}) or {}
        path = ROOT / str(spec.get("data_dir", "data/rga")) / name[:4] / name
        if not path.is_file():
            return PlainTextResponse("no such file", status_code=404)
        return FileResponse(path, media_type="application/json", filename=name)

    @app.get("/api/rga/scan")
    def api_rga_scan():
        """The latest scan of the running acquisition, for the live plot.
        Empty object when there is none."""
        import json as _json
        text = state.rga_scan()
        return JSONResponse(_json.loads(text) if text else {})

    @app.get("/hv", response_class=HTMLResponse)
    def high_voltage(request: Request):
        """The CAEN supplies: what each channel reads, and what the board is
        configured to allow. The limits are DISPLAYED, never writable (§8.3)."""
        supplies = []
        for spec in state.config.devices.get("caen", []) or []:
            channels = []
            for ch in state.config.channels.values():
                if ch.device != spec["id"] or ch.kind != "hv_vmon":
                    continue
                index = int(ch.phys)
                imon_name = ch.name.replace("_vmon", "_imon")
                expect = (spec.get("expect") or {}).get(index, {})
                stat_view = state.channel(ch.name.replace("_vmon", "_stat"))
                vset_name = ch.name.replace("_vmon", "_vset")
                vset_view = state.channel(vset_name)
                default_channel = state.config.channels.get(vset_name)

                # The board's own STAT word decides on/off, NEVER whether
                # VMON is above zero: a channel can be enabled and sitting at
                # zero volts (§7.2). None means the word could not be read,
                # and the page says so rather than guessing from the voltage.
                word = None
                if (stat_view is not None and stat_view.healthy
                        and stat_view.value is not None):
                    word = int(stat_view.value)

                # THE SECTION 10a INVARIANT: a channel that is not enabled
                # must have VSET 0, because the enable is a hand operation and
                # the board ramps to whatever VSET holds the moment it is
                # flipped. Where that is not true, flipping the switch is a
                # step into an unannounced voltage - so the page says so
                # rather than leaving it to be discovered at the supply.
                # is_disabled - THE SWITCH - not is_energised, the output.
                #
                # This asked whether the channel was putting out volts, so a
                # channel whose switch was on but which was not energised
                # counted as "disabled with a setpoint" and produced the
                # warning "flipping the enable would ramp straight to it"
                # about a channel already enabled. The fourth bug of this
                # shape in one afternoon, which is what prompted the rename.
                armed = None
                if (vset_view is not None and vset_view.healthy
                        and vset_view.value is not None and word is not None):
                    armed = (is_disabled(word)
                             and abs(vset_view.value) > ARMED_ABOVE_V)

                # FOUR STATES, and the distinction between the middle two
                # is the whole of section 10a:
                #
                #   bit 10 set              the front-panel switch is off
                #   neither bit            switch on, output NOT energised
                #   bit 0 set, VMON ~ 0    energised, sitting at zero volts
                #   bit 0 set, VMON != 0   energised with volts out
                #
                # `enabled` used to mean bit 0, on the mistaken belief that
                # bit 0 was the enable. It is not: bit 0 is the OUTPUT. A
                # channel whose switch had been flipped on but which had not
                # been energised therefore matched nothing and displayed as
                # "off" - which is what it looked like on 18 September 2026
                # after the nai channel was re-enabled by hand.
                channels.append({
                    "index": index,
                    "label": ch.name.replace("hv_", "").replace("_vmon", ""),
                    "description": ch.description,
                    "vmon": state.channel(ch.name),
                    "imon": state.channel(imon_name),
                    "vset": vset_view,
                    "vset_channel": vset_name,
                    "default": default_channel.default_setpoint
                               if default_channel else None,
                    "armed": armed,
                    "stat": stat_view,
                    "energised": None if word is None else is_energised(word),
                    "switched_off": None if word is None else is_disabled(word),
                    "faults": [] if word is None else status_faults(word),
                    "flags": "" if word is None else describe_status(word),
                    "expect": expect,
                    "limits": ch.limits or {},
                    # The limits of the VSET channel, which is what the write
                    # path validates against (caen.py). `limits` above belongs
                    # to the VMON channel and happens to carry the same pair
                    # today - relying on that is how `channels_for(self.name)`
                    # came to mark nothing. The confirmation quotes the range
                    # the supply will actually refuse against.
                    "vset_limits": (default_channel.limits or {}
                                    if default_channel else {}),
                    "sign": ch.sign,
                    # The trip the CAEN service latched, or None. The board's
                    # own flag is not required: on 23 September 2026 two
                    # real trips set none, and a page that waited for it
                    # offered nothing (caen.py, `_watch_trips`).
                    "trip": state.hv_trip(vset_name),
                    "hw_tripped": word is not None and is_tripped(word),
                    # Moving: the board's ramp bits, and a turn-off the CAEN
                    # service is still waiting to see complete (bit 0 stays
                    # set through the ramp down and some seconds after).
                    "ramping": None if word is None else ramping(word),
                    "switching": state.hv_switching(vset_name),
                })
            supplies.append({
                "trips": [c for c in channels if c.get("trip")],
                "armed": [c for c in channels if c.get("armed")],
                "id": spec["id"], "serial": spec.get("board_serial"),
                "model": spec.get("board_name"),
                "firmware": spec.get("firmware"),
                "channels": sorted(channels, key=lambda c: c["index"]),
            })
        # The cryostat and the flow integrator come along because this is
        # the Control page, not the HV page: one place answers "I want to
        # change something", which is the question asked before navigating.
        # They were on the overview only because they had nowhere else.
        q = request.query_params
        moving = any(c["ramping"] or c["switching"]
                     for s in supplies for c in s["channels"])
        return page(request, "hv.html", supplies=supplies, moving=moving,
                    hv_ok=q.get("hv_ok"), hv_error=q.get("hv_error"),
                    lakeshore=lakeshore_view(), flow=state.flow_total(),
                    ls_ok=q.get("ls_ok"), ls_error=q.get("ls_error"),
                    reset=q.get("reset"), reset_total=q.get("total"))

    # ------------------------------------------------------------- alarms
    #
    # The alarm chain, end to end, on one page: what has fired, what WOULD
    # fire, and who is told. It exists because the chain was the least visible
    # part of the system and the most consequential - the thresholds in force
    # were published on `xams/status/limits` (§11) and displayed nowhere, and
    # the recipient list could only be changed by editing a file.
    #
    # Everything here is read from retained MQTT or from a file. No database
    # (§8.1): the page that says whether anyone will be told must work at
    # precisely the moment PostgreSQL is down.

    @app.get("/alarms", response_class=HTMLResponse)
    def alarms_page(request: Request):
        people = read_recipients()
        return page(request, "alarms.html",
                    active=state.active_alarms(),
                    limits=_limits_view(state),
                    people=people,
                    alarm_count=sum(1 for p in people if p.get("alarms")),
                    daily_count=sum(1 for p in people if p.get("daily")),
                    saved=request.query_params.get("saved"),
                    error=request.query_params.get("error"))

    @app.post("/alarms/recipients")
    def alarms_recipients(request: Request):
        """Save the recipient list (§4.4).

        **All or nothing, and validated before anything is written.** A
        half-saved list is a list nobody chose, and this one decides who finds
        out that something is wrong.
        """
        import asyncio
        from urllib.parse import quote
        form = asyncio.run(_form(request))
        who = operator_of(request, form.get("by", ""))

        before = read_recipients()
        people, removed = people_from_form(form)

        problems = validate_recipients(people)
        if problems:
            return RedirectResponse(
                "/alarms?error=" + quote("; ".join(problems)),
                status_code=303)

        try:
            write_recipients(people, who)
        except OSError as exc:
            log.exception("could not write recipients.yaml")
            return RedirectResponse(
                "/alarms?error=" + quote(f"could not write the file: {exc}"),
                status_code=303)

        for line in recipient_changes(before, people, removed):
            _audit(state, who, "recipients", line["target"],
                   line["old"], line["new"])
            log.warning("recipients: %s %s -> %s by %s", line["target"],
                        line["old"], line["new"], who)

        on_alarms = sum(1 for p in people if p.get("alarms"))
        note = f"saved {len(people)} recipient{'' if len(people) == 1 else 's'}"
        # Said after the save, not instead of it: a blank contact field is
        # somebody mid-edit far more often than it is a mistake, and taking
        # the whole list hostage over one is the wrong trade (§4.4).
        for warning in recipient_warnings(people):
            note += f" - WARNING: {warning}"
        if not on_alarms:
            # A warning, not a refusal: turning everyone off may be exactly
            # what somebody means to do during an intervention. The engine
            # raises its own alarm about it too (§4.4). Said about the alarm
            # column alone: an empty daily report list is a quiet morning,
            # not a system nobody is watching.
            note += (" - WARNING: nobody is on the alarm list, "
                     "so alarms reach nobody")
        return RedirectResponse("/alarms?saved=" + quote(note),
                                status_code=303)

    @app.post("/alarms/notify")
    def alarms_notify(request: Request, enabled: str = Form(""),
                      by: str = Form("")):
        """The master switch: turn alarm DELIVERY off or on (§4.4a).

        **This is the most dangerous control on the site**, and it is here
        because the alternative is worse. The slow control runs when the plant
        does not, and a fortnight of "the cryostat is warm" at three in the
        morning is how an operator learns to ignore the message that matters.
        Stopping that properly is a switch somebody threw on purpose, with
        their name on it - not a recipient list quietly emptied, not a service
        stopped, and not a threshold widened until it never fires again.

        What it does NOT do is make the system look well. The engine still
        evaluates, the pages still show every active alarm, the history is
        unbroken, and the badge at the top of every page says the alarms are
        off for as long as they are.
        """
        from urllib.parse import quote
        who = operator_of(request, by)
        wanted = enabled.strip().lower() in ("1", "true", "on", "yes")
        answer = state.command(TOPIC_NOTIFY, ACK_NOTIFY,
                               {"enabled": wanted, "by": who})
        if not answer.get("ok"):
            return RedirectResponse(
                "/alarms?error=" + quote(str(answer.get("reason", "refused"))),
                status_code=303)

        _audit(state, who, "alarm_notifications", "all",
               "on" if answer.get("was") else "off", "on" if wanted else "off")
        log.critical("ALARM NOTIFICATIONS %s by %s",
                     "ON" if wanted else "OFF", who)
        if wanted:
            still = answer.get("active") or []
            note = "alarm notifications are ON"
            if still:
                note += (" - %d channel(s) still in alarm will notify on "
                         "their next reading: %s"
                         % (len(still), ", ".join(still)))
        else:
            note = ("WARNING: alarm notifications are OFF - alarms are still "
                    "evaluated and shown here, but nobody is told")
        return RedirectResponse("/alarms?saved=" + quote(note),
                                status_code=303)

    @app.get("/mimic", response_class=HTMLResponse)
    def mimic_alias(request: Request):
        """Where the P&ID used to live. Bookmarks and the manual still work."""
        return RedirectResponse("/", status_code=301)

    @app.get("/", response_class=HTMLResponse)
    def mimic(request: Request):
        """The P&ID with live values on it, and the page this site opens on.

        It is where people actually go: the plant drawn as it is, with every
        reading in the place it physically belongs. It answers the question
        the status table cannot — *where* is tt203, and what is it next to.

        READ-ONLY, deliberately (§8.2). This is the page left open on a
        screen all day, so nothing on it writes to an instrument; the cards
        beside the drawing link to the Control page instead. A setpoint box
        on an unattended display is the wrong thing to reach for by accident.
        """
        svg_path = HERE / "static" / "xams_pid.svg"
        if not svg_path.exists():
            return HTMLResponse(
                "<p>The mimic has not been built. Run "
                "<code>python tools/build_mimic.py</code>.</p>", status_code=503)
        return page(request, "mimic.html",
                    svg=svg_path.read_text(encoding="utf-8"),
                    side=mimic_sidebar())

    def mimic_sidebar() -> dict:
        """The rows of the column beside the drawing (§8.2).

        Strictly the COMPLEMENT of the P&ID. Every pressure, temperature,
        flow rate and heater wattage is already on the drawing, in the place
        it physically belongs, and repeating one here would give the same
        reading two homes on one page - the sort of thing that is fine until
        the day the two disagree and somebody has to work out which is lying.

        So what is left is the state that has no place on a pipework diagram:
        the supplies, the setpoint the cryostat is being held to, the mains,
        and the integrated mass that answers "how much have we moved". Each
        of those otherwise costs a navigation away from the page you are
        watching, which on a mimic is the one thing you do not want to do.

        Only the LABELS are built here. The values are filled by the same
        five-second fetch that drives the drawing, because this page has no
        meta refresh (§8.2) and a server-rendered number on it would be
        frozen at whatever it was when the tab was opened.
        """
        hv = []
        for spec in state.config.devices.get("caen", []) or []:
            for ch in state.config.channels.values():
                if ch.device != spec["id"] or ch.kind != "hv_vmon":
                    continue
                hv.append({
                    "vmon": ch.name,
                    "stat": ch.name.replace("_vmon", "_stat"),
                    "label": ch.description or ch.name,
                })
        return {"hv": hv}

    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request, service: str = "alarms", lines: int = 80,
             older: int = 0):
        """The last lines of each service log — saves logging in and hunting
        for files (§8.1).

        Newest first, because the reason anybody opens this page is the most
        recent thing a service said.

        `older=N` reads the Nth rotated file instead, `<service>.log.N`. The
        handler writes 10 MB before rotating and keeps 5 of them, so a busy
        service's last hour can already be in `.log.1` — without this the page
        showed almost nothing and looked like silence.
        """
        # Chosen from the whitelist, never pasted together out of what the URL
        # said: `logs/{service}.log` reads any .log file on the disk, loopback
        # binding or not.
        choices = log_files()
        current = choices.get(service)
        rotations = [n for n in ROTATIONS
                     if current is not None and rotated(current, n).exists()]
        path = current if not older else (
            rotated(current, older) if current is not None
            and older in ROTATIONS else None)

        if path is None:
            text = "(no such log)"
        elif not path.exists():
            text = f"({path.name} does not exist — it has not rotated that far)"
        else:
            try:
                # utf-8-SIG: PowerShell 5.1 writes a BOM at the head of
                # backup.log, and read as plain utf-8 it lands in front of
                # the first timestamp - which then matches nothing and comes
                # out grey.
                content = path.read_text(encoding="utf-8-sig",
                                         errors="replace")
                text = "\n".join(newest_first(content.splitlines()[-lines:]))
            except Exception as exc:
                text = f"could not read {path}: {exc}"

        return page(request, "logs.html", available=sorted(choices),
                    selected=service, text=colourise(text), rotations=rotations,
                    older=older, lines=lines,
                    read_at=datetime.now().strftime("%H:%M:%S"))

    # --------------------------------------------------------------- api

    @app.get("/api/state")
    def api_state():
        """Everything the pages show, as JSON. For the Python client and for
        anything else that wants it without scraping HTML."""
        status, _ = state.overall()
        return JSONResponse({
            "overall": status,
            "config": state.config.config_hash,
            "services": state.services(),
            "alarms": state.active_alarms(),
            # Whether anybody would be told (§4.4a). A caller polling this to
            # decide "is the plant being watched" needs the switch as much as
            # it needs the alarms; `enabled: null` is the engine not saying.
            "notifications": state.notifications(),
            "faults": state.known_faults(),
            "channels": [_channel_json(c) for c in state.channels()],
        })

    def _channel_json(c) -> dict:
        out = {
            "name": c.name, "value": c.value, "unit": c.unit,
            "quality": c.quality,
            # `null`, not the infinity a never-reported channel carries
            # internally: strict JSON has no way to write it, and
            # emitting it raw made the WHOLE endpoint 500 the moment any
            # one channel went silent - the API falling over exactly
            # when something is wrong with the plant. `null` reads the
            # same as it does for a service with no heartbeat.
            "age_s": _json_age(c.age_s),
            "alarm": c.alarm, "healthy": c.healthy,
        }
        # A STATUS word is a number that means nothing until it is decoded,
        # and the decoding belongs to hv_status.py alone (§7.2). Decoding it
        # here rather than in the browser is what keeps the mimic's HV panel
        # from growing a second, drifting copy of STAT_BITS in JavaScript -
        # and bit 0 is the one bit nobody may get wrong twice.
        spec = state.config.channels.get(c.name)
        if (spec is not None and spec.kind == "hv_stat"
                and c.healthy and c.value is not None):
            word = int(c.value)
            out["status"] = {
                "text": describe_status(word),
                "faults": status_faults(word),
                "energised": is_energised(word),
                "disabled": is_disabled(word),
            }
        return out

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        status, _ = state.overall()
        return status

    # ------------------------------------------------------------- manual

    # The documentation, mounted rather than served from a second process.
    # The point is that it is present whenever the UI is: the lab PC cannot be
    # assumed to reach the internet, and a manual you can only read when the
    # network is healthy is missing exactly when it is wanted.
    #
    # HERE, not the working directory: this runs as a service, whose working
    # directory is not the repository.
    #
    # /manual, not /docs, although docs_url=None leaves /docs free — "docs" in
    # a FastAPI application means Swagger to anyone who has seen one before.
    manual = HERE / "site"
    if manual.is_dir():
        app.mount("/manual", StaticFiles(directory=str(manual), html=True),
                  name="manual")
    else:
        # Not fatal. A missing manual is a nuisance; refusing to start the
        # monitoring because of it would be a fault.
        log.warning("the manual has not been built — run "
                    "`python tools/build_docs.py`; /manual will return 404")

    return app
