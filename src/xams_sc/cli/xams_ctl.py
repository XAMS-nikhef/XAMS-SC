"""xams-ctl — start, stop, check and reload the services. See DESIGN.md §12.

One command, correct order, every time.

    xams-ctl start | stop | restart | status | reload | check

`stop` releases all hardware for LabVIEW, which matters while LabVIEW is still
the fallback: every device admits only one process.

Milestone 1 runs the services as plain processes. From the trial phase they
run under NSSM as Windows services (§12), and this command drives those
instead — which is why the verbs are chosen to match.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ..config import CONFIG_DIR, LOG_DIR, ConfigError, load, wants_alarms

# Started in this order; stopped in reverse, so sinks outlive the producers
# and the last measurements are archived rather than dropped.
#
# `sim` is deliberately NOT here. It publishes every enabled channel, including
# the ones the cdaq service owns, so running both means two producers writing
# the same channel names and the archive recording whichever arrived last. Run
# it by hand for development on a machine with no hardware:
#
#     python -m xams_sc.devices sim
#
SERVICES = ["sinks", "cdaq", "caen", "lakeshore", "ups", "turbo", "rga", "derived",
            "alarms", "webui"]

PID_DIR = LOG_DIR


SERVICE_PREFIX = "XAMS-"


def _service_name(name: str) -> str:
    return f"{SERVICE_PREFIX}{name}"


def _installed_as_service(name: str) -> bool:
    """True when this service is registered with Windows (via NSSM).

    Everything below branches on this, because the two modes need opposite
    handling: a plain process is stopped by killing it, whereas killing a
    NSSM-managed one just makes NSSM start it again ten seconds later.
    """
    try:
        out = subprocess.run(["sc", "query", _service_name(name)],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except Exception:
        return False


def _service_mode() -> bool:
    """True when the stack is installed as Windows services."""
    return any(_installed_as_service(n) for n in SERVICES)


def _sc(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["sc", *args], capture_output=True, text=True, timeout=30)


def _service_state(name: str) -> str:
    out = _sc("query", _service_name(name)).stdout
    for token in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING", "PAUSED"):
        if token in out:
            return token.lower()
    return "unknown"


def _pid_file(name: str) -> Path:
    return PID_DIR / f"{name}.pid"


def _running(name: str) -> int | None:
    """Return the pid if the service looks alive, else None."""
    f = _pid_file(name)
    if not f.exists():
        return None
    try:
        pid = int(f.read_text().strip())
    except (ValueError, OSError):
        return None

    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return pid if str(pid) in out else None
    except Exception:
        return None


def _command_for(name: str) -> list[str]:
    if name in ("sinks", "alarms"):
        return [sys.executable, "-m", f"xams_sc.{name}"]
    if name == "webui":
        return [sys.executable, "-m", "xams_sc.api"]
    return [sys.executable, "-m", "xams_sc.devices", name]


def cmd_start(args) -> int:
    if _service_mode():
        return _start_services()
    PID_DIR.mkdir(parents=True, exist_ok=True)
    for name in SERVICES:
        if _running(name):
            print(f"  {name:12s} already running")
            continue
        proc = subprocess.Popen(
            _command_for(name),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _pid_file(name).write_text(str(proc.pid))
        print(f"  {name:12s} started (pid {proc.pid})")
        time.sleep(0.5)
    return 0


def _lock_held(name: str) -> bool:
    """True while a service still holds its lock file.

    On Windows an open file cannot be deleted, so being able to remove it is
    proof that nothing holds it. The lock is recreated by the service itself
    on the next start, so removing a released one here is harmless.
    """
    path = PID_DIR / f"{name}.lock"
    if not path.exists():
        return False
    try:
        path.unlink()
        return False
    except OSError:
        return True


def _start_services() -> int:
    """Start the Windows services, and restore auto-start if it was suspended."""
    failed = []
    for name in SERVICES:
        if not _installed_as_service(name):
            continue
        # Undo a previous `stop --for-labview`, which set these to demand so a
        # reboot could not quietly take the instruments back.
        _sc("config", _service_name(name), "start=", "auto")
        if _service_state(name) == "running":
            print(f"  {name:12s} already running")
            continue
        result = _sc("start", _service_name(name))
        if result.returncode == 0:
            print(f"  {name:12s} started")
        else:
            failed.append(name)
            print(f"  {name:12s} FAILED to start")
        time.sleep(0.8)
    if failed:
        print()
        print(f"  {len(failed)} service(s) did not start. Look in logs/<name>.log")
        return 1
    print()
    print("Auto-start is on: the stack returns by itself after a reboot.")
    return 0


def _stop_services(for_labview: bool) -> int:
    failed = []
    for name in reversed(SERVICES):
        if not _installed_as_service(name):
            continue
        if _service_state(name) != "stopped":
            result = _sc("stop", _service_name(name))
            if result.returncode != 0 and "1062" not in result.stdout:
                failed.append(name)
        if for_labview:
            # Stopping alone is not enough: a service left on automatic comes
            # back at the next reboot and takes the hardware from LabVIEW,
            # with nobody present to notice.
            _sc("config", _service_name(name), "start=", "demand")
        print(f"  {name:12s} stopped" + ("  (auto-start suspended)" if for_labview else ""))
        time.sleep(0.4)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if all(_service_state(n) == "stopped"
               for n in SERVICES if _installed_as_service(n)):
            break
        time.sleep(0.5)

    if failed:
        print()
        print(f"  WARNING: {', '.join(failed)} did not stop cleanly.")
        return 1

    print()
    print("All hardware released. LabVIEW can be started.")
    if for_labview:
        print("Auto-start is suspended, so a reboot will NOT take it back.")
        print("Run `xams-ctl start` when you want the slow control again.")
    else:
        print("NOTE: auto-start is still on — a reboot will bring these back")
        print("and reclaim the instruments. Use `stop --for-labview` to")
        print("suspend that as well.")
    return 0


def cmd_stop(args) -> int:
    if _service_mode():
        return _stop_services(getattr(args, "for_labview", False))
    stopped = []
    for name in reversed(SERVICES):
        pid = _running(name)
        if not pid:
            print(f"  {name:12s} not running")
            _pid_file(name).unlink(missing_ok=True)
            continue
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        _pid_file(name).unlink(missing_ok=True)
        stopped.append(name)
        print(f"  {name:12s} stopped")

    # WAIT FOR THE PROCESSES TO ACTUALLY GO.
    #
    # taskkill returns as soon as it has asked. The process may take a moment
    # to die, and until it does it still holds its single-instance lock and
    # its hardware. `restart` then failed with "another cdaq service is
    # already running" — the lock working exactly as intended against a stop
    # that had not finished. Waiting for the locks to clear is what makes
    # stop mean stopped.
    held = []
    if stopped:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            held = [n for n in stopped if _lock_held(n)]
            if not held:
                break
            time.sleep(0.3)
        if held:
            print(f"\n  WARNING: still holding a lock after 15s: {', '.join(held)}")
            print("  Their hardware may not be released yet.")
            return 1

    print("\nAll hardware released. LabVIEW can be started.")
    return 0


def cmd_restart(args) -> int:
    if cmd_stop(args) != 0:
        print("\nNot restarting: something did not stop cleanly.")
        return 1
    print()
    return cmd_start(args)


def cmd_status(args) -> int:
    try:
        config = load()
    except ConfigError as exc:
        print(f"config: INVALID — {exc}")
        return 2

    enabled = config.enabled_channels()
    print(f"config    {config.config_hash}  "
          f"{len(enabled)} enabled of {len(config.channels)} channels")
    print()

    if _service_mode():
        print("  (running as Windows services)")
        for name in SERVICES:
            if not _installed_as_service(name):
                print(f"  {name:12s} not installed as a service")
                continue
            state = _service_state(name)
            boot = "auto" if "AUTO_START" in _sc(
                "qc", _service_name(name)).stdout else "manual"
            print(f"  {name:12s} {state:8s} (start: {boot})")
    else:
        for name in SERVICES:
            pid = _running(name)
            print(f"  {name:12s} {'running (pid %d)' % pid if pid else 'stopped'}")

    # Heartbeat ages come from the retained MQTT topics, never from the
    # database: the status view must work when the database does not (§8.1).
    print()
    try:
        _print_bus_status(args)
    except Exception as exc:
        print(f"  broker    unreachable — {exc}")

    _print_backup_status(args)
    _print_dashboard_drift()
    return 0


def _print_dashboard_drift() -> None:
    """Say so when Grafana and git have diverged (§12).

    Drift is otherwise silent until somebody thinks to check, and a dashboard
    that exists only in Grafana's database is lost when that database is.
    Printed here because this is the command that gets run daily.

    Never fatal and never noisy: Grafana being down or unconfigured means the
    question cannot be answered, not that something is wrong.
    """
    from ..grafana import check

    result = check()
    if result["state"] == "ok":
        # Said out loud rather than passed over in silence: "nothing printed"
        # is also what a check that never ran looks like.
        print(f"\n  grafana   dashboards saved to git ({len(result['dashboards'])})")
        return
    if result["state"] == "unknown":
        # Only worth a line when it looks like it was meant to work.
        if "no Grafana token" not in result["detail"]:
            print(f"\n  grafana   drift unknown — {result['detail']}")
        return
    states = {d["state"] for d in result["dashboards"]}
    print(f"\n  grafana   NOT SAVED TO GIT — {result['detail']}")
    if states - {"ok", "uncommitted"}:
        print("            a dashboard only in Grafana is lost with Grafana:")
        # Full command, with the venv interpreter: .\\tools\\save_dashboard.py
        # goes through the Windows py launcher, which swallows the script
        # errors. No --password: --save reads, via the read-only token.
        # One backslash each in the printed line: it is meant to be pasted.
        print("            .\\.venv\\Scripts\\python.exe "
              "tools\\save_dashboard.py --save")
    if "uncommitted" in states:
        # Exported but never committed: the file already matches Grafana, so
        # --save has nothing left to do and the advice above is the wrong
        # advice. What is missing is the commit.
        print("            exported but NOT COMMITTED — on this disk only:")
        print("            git add grafana\\dashboards-archive && git commit")

def _print_backup_status(args) -> None:
    """The nightly backup's last result, from its retained topic.

    Read from the broker rather than from `data/.backup-stamp`, so this
    reports what the backup actually published rather than what a file on
    this disk claims. Never fatal.
    """
    from ..bus import TOPIC_BACKUP, Bus
    from ..model import parse_iso, utcnow

    seen: dict[str, str] = {}
    try:
        bus = Bus(client_id="xams-ctl-backup", host=args.broker, port=args.port)
        bus.subscribe(TOPIC_BACKUP, lambda t, p: seen.__setitem__(t, p))
        bus.connect()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not bus.connected:
            time.sleep(0.1)
        time.sleep(1.0)
        bus.disconnect()
    except Exception:
        return

    if not seen:
        # Silence here would read as "fine". It is not: it means the backup
        # has never reported, and the archive is on one disk.
        print("\n  backup    NEVER REPORTED - the archive is on this PC only")
        print("            .\\tools\\backup.ps1")
        return

    try:
        status = json.loads(next(iter(seen.values())))
        age_h = (utcnow() - parse_iso(status["t"])).total_seconds() / 3600.0
    except Exception:
        print("\n  backup    unparseable status")
        return

    detail = status.get("detail", "")
    if not status.get("ok"):
        print(f"\n  backup    FAILED - {detail}")
        print("            .\\tools\\backup.ps1")
    elif age_h > 30:
        print(f"\n  backup    OVERDUE - last success {age_h:.0f} hours ago")
        print("            .\\tools\\backup.ps1")
    else:
        print(f"\n  backup    ok, {age_h:.1f} h ago ({detail})")


def _print_bus_status(args) -> None:
    from ..bus import TOPIC_STATUS, Bus
    from ..model import parse_iso, utcnow

    seen: dict[str, str] = {}
    bus = Bus(client_id="xams-ctl-status", host=args.broker, port=args.port)
    bus.subscribe(f"{TOPIC_STATUS}/#", lambda t, p: seen.__setitem__(t, p))
    bus.connect()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not bus.connected:
        time.sleep(0.1)
    if not bus.connected:
        bus.disconnect()
        print("  broker    not reachable")
        return
    time.sleep(1.5)
    bus.disconnect()

    now = utcnow()
    for topic in sorted(seen):
        if topic.endswith("/heartbeat"):
            service = topic.split("/")[2]
            try:
                age = (now - parse_iso(seen[topic])).total_seconds()
                print(f"  {service:12s} heartbeat {age:.0f}s ago")
            except Exception:
                print(f"  {service:12s} heartbeat unparseable: {seen[topic]!r}")


def cmd_reload(args) -> int:
    """Re-read the YAML without restarting, so a change costs no data gap."""
    try:
        config = load()
    except ConfigError as exc:
        print(f"REFUSED: configuration is invalid, nothing reloaded.\n  {exc}")
        return 2

    from ..bus import Bus
    bus = Bus(client_id="xams-ctl-reload", host=args.broker, port=args.port)
    bus.connect()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not bus.connected:
        time.sleep(0.1)
    if not bus.connected:
        print("broker not reachable; nothing reloaded")
        bus.disconnect()
        return 1

    # Collect acknowledgements and report what ACTUALLY reloaded.
    #
    # An earlier version published the command and printed "reload requested",
    # which was true and useless: nothing subscribed to the topic, so the
    # command reported success while changing nothing. A threshold edit
    # followed by `reload` left the old limit in force, silently. Reporting
    # only what came back is the fix.
    acks: dict[str, dict] = {}
    bus.subscribe("xams/ack/+/reload",
                  lambda t, p: acks.__setitem__(t.split("/")[2], json.loads(p)))
    time.sleep(0.3)
    bus.publish_raw("xams/cmd/all/reload", json.dumps({"config": config.config_hash}))
    time.sleep(2.5)
    bus.disconnect()

    if not acks:
        print("No service acknowledged the reload.")
        print("Nothing has changed. Are the services running?")
        return 1

    applied = sorted(k for k, v in acks.items() if v.get("applied"))
    refused = sorted(k for k, v in acks.items() if not v.get("applied"))

    print(f"config {config.config_hash}")
    for name in applied:
        print(f"  {name:12s} reloaded")
    for name in refused:
        why = acks[name].get("error")
        print(f"  {name:12s} {'REFUSED: ' + why if why else 'needs a restart to apply this'}")

    if refused and not any(acks[n].get("error") for n in refused):
        print()
        print("Services that hold hardware rebuild their tasks from the channel")
        print("map at startup, so a channel change needs:")
        print("  xams-ctl restart")
    return 0 if not any(acks[n].get("error") for n in refused) else 2


def _session(client_id: str, ack_topic: str, args):
    """A connected CommandSession for this invocation. See DESIGN.md §10.

    The connect-wait, the settle, the publish and the poll used to be written
    out by hand in each of the four command verbs below, each with its own
    timeout — 8 s in flow-reset, 15 s in the others, against the web UI's
    10 s — for no reason anybody had decided. They live in `bus.py` now, so
    both halves of §10 share one implementation and one timeout.

    Raises BrokerUnreachable, which every caller reports as "nothing was
    sent". That is the honest reading: if the command never reached the
    broker, the instrument never heard it.
    """
    from ..bus import CommandSession

    return CommandSession(client_id, ack_topic,
                          host=args.broker, port=args.port,
                          sleep=time.sleep, monotonic=time.monotonic)


def cmd_flow_reset(args) -> int:
    """Close the running flow-integrator period and open a new one (§7.5).

    Nothing is erased. The closed period keeps its total and its gaps, so the
    history of how much passed through during each period survives — unlike a
    counter somebody zeroed, which is gone.
    """
    from ..bus import TOPIC_FLOW_RESET, BrokerUnreachable

    who = args.by or os.environ.get("USERNAME") or "unknown"
    ack_topic = "xams/ack/derived/flow_reset"

    try:
        with _session("xams-ctl-flow-reset", ack_topic, args) as session:
            result = session.send(TOPIC_FLOW_RESET, ack_topic, {"by": who})
    except BrokerUnreachable:
        print("broker not reachable; nothing was reset")
        return 1

    if not result:
        print("The derived service did not acknowledge. Is it running?")
        print("Nothing has been reset.")
        return 1

    print(f"Flow period closed by {who}:")
    print(f"  started   {result.get('start')}")
    print(f"  stopped   {result.get('stop')}")
    print(f"  total     {result.get('total_g', 0):.3f} g")
    print(f"  gaps      {result.get('gaps_s', 0):.0f} s")
    if result.get("gaps_s", 0) > 0:
        print("            (the total is an underestimate by whatever flowed")
        print("             during those gaps — that is why they are recorded)")
    print()
    print("A new period is now open. The closed one is kept in flow_periods.")
    return 0


def _hv_write(args, targets) -> int:
    """Send VSET commands and report each acknowledgement. See DESIGN.md 10a.

    `targets` is a list of (channel, signed value). Sent one at a time and
    waited for individually: a batch that reported "3 of 4 succeeded" without
    saying which would be worse than useless on a rack of electrodes.
    """
    from ..bus import ACK_HV_VSET, TOPIC_HV_VSET, BrokerUnreachable

    who = args.by or os.environ.get("USERNAME") or "unknown"
    failures = 0

    try:
        with _session("xams-ctl-hv", ACK_HV_VSET, args) as session:
            for channel, value in targets:
                # Every one of these is answered on the same topic, so "the
                # next ack" is not the same question as "the answer to this
                # command". The channel is already in both payloads.
                ack = session.send(TOPIC_HV_VSET, ACK_HV_VSET,
                                   {"channel": channel, "value": value,
                                    "by": who},
                                   match=("channel",))
                if ack is None:
                    print(f"  {channel:22s} NO ANSWER - is the caen service "
                          f"running?")
                    failures += 1
                elif ack.get("ok"):
                    old = ack.get("old")
                    print(f"  {channel:22s} {old:+.1f} -> {ack['new']:+.1f} V"
                          if isinstance(old, (int, float))
                          else f"  {channel:22s} now {ack['new']:+.1f} V")
                else:
                    print(f"  {channel:22s} REFUSED: {ack.get('reason')}")
                    failures += 1
    except BrokerUnreachable:
        print("broker not reachable; nothing was sent")
        return 1

    if failures:
        print()
        print(f"{failures} of {len(targets)} refused or unanswered. "
              f"Nothing partial was left behind: each write is verified "
              f"against the board before it is called successful.")
    return 1 if failures else 0


def cmd_hv_set(args) -> int:
    """Set one HV channel's setpoint (10a).

    Refused unless the channel is enabled at the supply, unless the value is
    inside the range in channels.yaml, and unless the polarity matches the
    electrode. The service does that checking, not this command: a command
    that validated locally would be a second copy of the rules, and the copy
    that matters is the one next to the hardware.
    """
    return _hv_write(args, [(args.channel, args.value)])


def cmd_hv_standby(args) -> int:
    """Set HV setpoints to zero - the way down, and the way to make the
    enable switch safe to touch (10a).

    With no channel named, every hv_vset channel is zeroed. This is the
    operation that establishes the invariant on a system where the stored
    setpoints are whatever somebody last left in the boards.
    """
    config = load()
    names = [args.channel] if args.channel else sorted(
        c.name for c in config.enabled_channels() if c.kind == "hv_vset")
    if not names:
        print("no hv_vset channels are configured")
        return 1

    print(f"Setting {len(names)} setpoint(s) to zero.")
    print("This does NOT switch anything off: a channel that is on will ramp")
    print("down at the board's own RDW rate, and the enable switch is")
    print("untouched either way.")
    print()
    return _hv_write(args, [(name, 0.0) for name in names])


def _hv_output(args, on: bool) -> int:
    """Energise or de-energise channels and report each answer (10a step 4).

    NOT the front-panel enable, which stays a hand operation. A channel must
    already be enabled at the supply before this can do anything.
    """
    from ..bus import ACK_HV_OUTPUT, TOPIC_HV_OUTPUT, BrokerUnreachable

    config = load()
    names = [args.channel] if args.channel else sorted(
        c.name for c in config.enabled_channels() if c.kind == "hv_vset")
    if not names:
        print("no hv_vset channels are configured")
        return 1

    who = args.by or os.environ.get("USERNAME") or "unknown"
    failures = 0

    try:
        with _session("xams-ctl-hv-output", ACK_HV_OUTPUT, args) as session:
            for name in names:
                ack = session.send(TOPIC_HV_OUTPUT, ACK_HV_OUTPUT,
                                   {"channel": name, "on": on, "by": who},
                                   match=("channel",))
                if ack is None:
                    print(f"  {name:22s} NO ANSWER - is the caen service "
                          f"running?")
                    failures += 1
                elif ack.get("ok"):
                    state = "ON" if ack.get("on") else "off"
                    detail = ack.get("detail") or ""
                    print(f"  {name:22s} {state}"
                          + (f"  ({detail})" if detail else ""))
                else:
                    print(f"  {name:22s} REFUSED: {ack.get('reason')}")
                    failures += 1
    except BrokerUnreachable:
        print("broker not reachable; nothing was sent")
        return 1

    if failures:
        print()
        print(f"{failures} of {len(names)} refused or unanswered.")
    return 1 if failures else 0


def cmd_hv_on(args) -> int:
    """Energise HV channels (10a step 4).

    A channel ramps to whatever VSET holds, so the acknowledgement says which
    voltage that is. With the setpoints at zero - the resting state 10a's
    invariant guarantees - this energises at zero and moves nothing, which is
    a perfectly reasonable thing to do first.
    """
    return _hv_output(args, True)


def cmd_hv_off(args) -> int:
    """De-energise HV channels. The channel ramps down at the board's own RDW
    rate, so it does not reach zero instantly and its status keeps reporting
    ON until it does."""
    return _hv_output(args, False)


def cmd_hv_clear_trip(args) -> int:
    """Clear a tripped HV channel (10a, *Recovering from a trip*).

    A trip latches until it is cleared, and a tripped channel cannot be
    turned on until then. The service zeroes and switches off every tripped
    channel on that supply FIRST - the clear is board-wide - and only then
    clears the board alarm. The channel is left off at 0 V. Works on a
    channel whose trip the board never flagged, and on any channel that is
    off; refused on one that is on and has not tripped.
    """
    from ..bus import ACK_HV_CLEAR, TOPIC_HV_CLEAR, BrokerUnreachable

    who = args.by or os.environ.get("USERNAME") or "unknown"
    try:
        with _session("xams-ctl-hv-clear", ACK_HV_CLEAR, args) as session:
            ack = session.send(TOPIC_HV_CLEAR, ACK_HV_CLEAR,
                               {"channel": args.channel, "by": who},
                               match=("channel",))
    except BrokerUnreachable:
        print("broker not reachable; nothing was sent")
        return 1

    if ack is None:
        print(f"  {args.channel:22s} NO ANSWER - is the caen service running?")
        return 1
    if not ack.get("ok"):
        print(f"  {args.channel:22s} REFUSED: {ack.get('reason')}")
        return 1
    print(f"  {args.channel:22s} {ack.get('detail')}")
    return 0


def _set_notifications(args, enabled: bool) -> int:
    """Throw the master alarm switch from a terminal (§4.4a).

    Here as well as on the page for one reason: the way to get the alarms
    back is not allowed to depend on the web UI being up. If the switch could
    only be reached from a page, a web service that would not start would
    leave the alarms off with no way to re-arm them but editing a file the
    engine has already read.
    """
    from ..bus import ACK_NOTIFY, TOPIC_NOTIFY, BrokerUnreachable

    who = args.by or os.environ.get("USERNAME") or "unknown"

    try:
        with _session("xams-ctl-notify", ACK_NOTIFY, args) as session:
            # No `match`: the switch has one target for the whole system, so
            # there is nothing to tell one ack from another (§4.4a).
            ack = session.send(TOPIC_NOTIFY, ACK_NOTIFY,
                               {"enabled": enabled, "by": who})
    except BrokerUnreachable:
        print("broker not reachable; nothing was sent")
        return 1

    if not ack:
        print("NO ANSWER - is the alarms service running?")
        print("Nothing was changed.")
        return 1
    if not ack.get("ok"):
        print(f"refused: {ack.get('reason', 'no reason given')}")
        return 1

    if enabled:
        print(f"alarm notifications are ON (by {who})")
        still = ack.get("active") or []
        if still:
            print(f"  {len(still)} channel(s) still in alarm will notify on "
                  f"their next reading:")
            for name in still:
                print(f"    {name}")
    else:
        print(f"alarm notifications are OFF (by {who})")
        print("  Alarms are still evaluated, published and recorded.")
        print("  NOBODY WILL BE TOLD about them until you run:")
        print("    xams-ctl alarms-on --by <you>")
    return 0


def cmd_alarms_on(args) -> int:
    """Turn alarm delivery back on for the whole system."""
    return _set_notifications(args, True)


def cmd_alarms_off(args) -> int:
    """Stop delivering alarms to anybody. Evaluation carries on."""
    return _set_notifications(args, False)


def cmd_check(args) -> int:
    """Validate the configuration and print what it defines. No side effects."""
    try:
        config = load()
    except ConfigError as exc:
        print(f"INVALID: {exc}")
        return 2

    by_device: dict[str, int] = {}
    for c in config.enabled_channels():
        by_device[c.device] = by_device.get(c.device, 0) + 1

    print(f"config {config.config_hash} from {CONFIG_DIR}")
    print(f"{len(config.channels)} channels defined, "
          f"{len(config.enabled_channels())} enabled\n")
    for device in sorted(by_device):
        print(f"  {device:12s} {by_device[device]:3d} enabled")

    tbd = [c.name for c in config.enabled_channels() if c.unit == "TBD"]
    if tbd:
        print(f"\n  {len(tbd)} enabled channels have unit TBD (§16): "
              f"{', '.join(tbd)}")

    alarm_recipients = [
        r for r in (config.recipients.get("recipients") or []) if wants_alarms(r)
    ]
    if not alarm_recipients:
        print("\n  WARNING: nobody is on the alarm list — "
              "alarms would reach nobody (§4.4)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The whole command line, in one place.

    Separate from `main` so that `tools/gen_doc_pages.py` can walk it: the
    Command line page in the manual is generated from this parser rather than
    typed out next to it, on the same argument as the channel and alarm
    tables. A list of options maintained by hand is wrong within a month, and
    wrong in the way that costs the most — it reads as though it were right.

    Which means the `help=` strings below are documentation, not hints.
    """
    p = argparse.ArgumentParser(prog="xams-ctl", description=__doc__.split("\n")[0])
    p.add_argument("--broker", default="127.0.0.1",
                   help="MQTT broker to talk to")
    p.add_argument("--port", type=int, default=1883, help="broker port")
    sub = p.add_subparsers(dest="command", required=True)
    for verb, fn, helptext in [
        ("start", cmd_start,
         "start every service, in dependency order"),
        ("restart", cmd_restart,
         "stop, then start — what a channel change needs"),
        ("status", cmd_status,
         "what is running, what the bus says, what has drifted"),
        ("reload", cmd_reload,
         "re-read the YAML without restarting, so a change costs no data gap"),
        ("check", cmd_check,
         "validate the configuration and print what it defines; no side effects"),
    ]:
        sub.add_parser(verb, help=helptext).set_defaults(func=fn)

    stop = sub.add_parser("stop", help="stop the services and release the hardware")
    stop.add_argument("--for-labview", action="store_true",
                      help="also suspend auto-start, so a reboot does not take "
                           "the instruments back")
    stop.set_defaults(func=cmd_stop)

    flow = sub.add_parser("flow-reset",
                          help="close the flow-integrator period and open a new one")
    flow.add_argument("--by", help="who is doing this (recorded in the audit log)")
    flow.set_defaults(func=cmd_flow_reset)

    hv_set = sub.add_parser(
        "hv-set", help="set one HV channel's setpoint (DESIGN.md 10a)")
    hv_set.add_argument("channel", help="e.g. hv_cathode_vset")
    hv_set.add_argument("value", type=float, help="signed volts, e.g. -2250")
    hv_set.add_argument("--by", help="who is doing this (recorded in audit)")
    hv_set.set_defaults(func=cmd_hv_set)

    hv_standby = sub.add_parser(
        "hv-standby",
        help="set HV setpoints to zero (all of them, unless one is named)")
    hv_standby.add_argument("channel", nargs="?",
                            help="one channel; default is every hv_vset channel")
    hv_standby.add_argument("--by", help="who is doing this (recorded in audit)")
    hv_standby.set_defaults(func=cmd_hv_standby)

    hv_on = sub.add_parser(
        "hv-on", help="energise HV channels (they must be enabled by hand first)")
    hv_on.add_argument("channel", nargs="?",
                       help="one channel; default is every hv_vset channel")
    hv_on.add_argument("--by", help="who is doing this (recorded in audit)")
    hv_on.set_defaults(func=cmd_hv_on)

    hv_off = sub.add_parser("hv-off", help="de-energise HV channels")
    hv_off.add_argument("channel", nargs="?",
                        help="one channel; default is every hv_vset channel")
    hv_off.add_argument("--by", help="who is doing this (recorded in audit)")
    hv_off.set_defaults(func=cmd_hv_off)

    hv_clear = sub.add_parser(
        "hv-clear-trip",
        help="acknowledge a tripped HV channel: zero its setpoint, switch it "
             "off, then clear the board alarm (the channel stays off)")
    hv_clear.add_argument("channel", help="the hv_vset channel that tripped")
    hv_clear.add_argument("--by", help="who is doing this (recorded in audit)")
    hv_clear.set_defaults(func=cmd_hv_clear_trip)

    alarms_off = sub.add_parser(
        "alarms-off",
        help="stop delivering alarms to anybody (they are still evaluated)")
    alarms_off.add_argument("--by", help="who is doing this (recorded in audit)")
    alarms_off.set_defaults(func=cmd_alarms_off)

    alarms_on = sub.add_parser("alarms-on",
                               help="deliver alarms again")
    alarms_on.add_argument("--by", help="who is doing this (recorded in audit)")
    alarms_on.set_defaults(func=cmd_alarms_on)
    return p


def main(argv=None) -> int:
    # The Windows console defaults to a codepage that cannot render the §
    # section references this project uses throughout. Ask for UTF-8 rather
    # than avoiding the character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
