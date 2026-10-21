"""Alarm service: python -m xams_sc.alarms

Runs the alarm engine, the notifier and the flight recorder in one process.
They belong together — the recorder must dump at the instant the engine raises
an alarm, and routing that through the bus would add a delay to the one moment
where the data matters most.

Runs LOCALLY and never depends on the Nikhef VM (§12). When the network is down
is exactly when the operator needs to see what is happening.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

import yaml

from ..bus import ACK_ARM, ACK_NOTIFY, TOPIC_ARM, TOPIC_NOTIFY, Bus
from ..config import CONFIG_DIR, LOG_DIR, ConfigError, load, wants_alarms
from ..model import ServiceState
from ..service import SingleInstance, setup_logging
from .engine import AlarmEngine
from .flight_recorder import FlightRecorder
from .notify import Notifier

log = logging.getLogger(__name__)


def read_secrets() -> dict:
    path = CONFIG_DIR / "secrets.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("could not read secrets.yaml")
        return {}


def recipients_provider():
    """Read recipients.yaml at send time, not at startup.

    It is edited from the web UI and applied without restarting anything
    (§4.4), so capturing the list once would mean a change never took effect
    until the next restart — which is the opposite of what that design says.
    """
    path = CONFIG_DIR / "recipients.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data.get("recipients") or []
    except Exception:
        log.exception("could not read recipients.yaml")
        return []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m xams_sc.alarms")
    p.add_argument("--broker", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--no-notify", action="store_true",
                   help="evaluate and publish, but send nothing")
    args = p.parse_args(argv)

    setup_logging("alarms", args.log_level)

    lock = SingleInstance("alarms", LOG_DIR)
    if not lock.acquire():
        log.critical("FATAL: another alarms process is already running "
                     "(lock: %s). Refusing to start — two engines would "
                     "double every notification.", lock.path)
        return 1

    try:
        config = load()
    except ConfigError as exc:
        print(f"FATAL: configuration is invalid: {exc}", file=sys.stderr)
        lock.release()
        return 2

    bus = Bus(client_id="alarms", host=args.broker, port=args.port)

    notifier = None
    if args.no_notify:
        log.warning("--no-notify: alarms will be evaluated and published but "
                    "NOT delivered to anyone")
    else:
        notifier = Notifier(read_secrets(), recipients_provider)
        wanted = [r for r in recipients_provider() if wants_alarms(r)]
        if not wanted:
            log.error("NOBODY WANTS ALARMS — they would reach nobody. "
                      "Tick Alarms for somebody in recipients.yaml (§4.4).")
        else:
            log.info("%d recipient(s) on the alarm list", len(wanted))

    recorder = FlightRecorder(bus, directory=Path("data/events"))
    recorder.start()

    engine = AlarmEngine(
        bus, config, notifier=notifier,
        # The dump happens at the moment the alarm is raised, so the buffer
        # still holds the approach to it (§9.1).
        on_alarm=lambda channel, severity, threshold: recorder.dump(
            f"{severity}_{threshold or 'quality'}", channel))
    engine.start()

    def handle_reload(topic: str, payload: str) -> None:
        try:
            new_config = load()
        except ConfigError as exc:
            log.error("reload refused, configuration is invalid: %s", exc)
            bus.publish_raw("xams/ack/alarms/reload",
                            json.dumps({"service": "alarms", "applied": False,
                                        "error": str(exc)}))
            return
        engine.reload(new_config)
        log.info("thresholds reloaded (config %s)", new_config.config_hash)
        bus.publish_raw("xams/ack/alarms/reload",
                        json.dumps({"service": "alarms", "applied": True,
                                    "config": new_config.config_hash}))

    def handle_notify(topic: str, payload: str) -> None:
        """The master switch, from the web UI or the CLI (§4.4a).

        Answered like any other command - the page that asked must never
        report a change it cannot confirm happened.
        """
        try:
            request = json.loads(payload)
            wanted = bool(request["enabled"])
        except (ValueError, KeyError, TypeError):
            log.error("notify command ignored, malformed: %r", payload)
            bus.publish_raw(ACK_NOTIFY, json.dumps(
                {"ok": False, "reason": "malformed command"}))
            return
        result = engine.set_notifications(wanted, str(request.get("by") or ""))
        # Audited on the same topic as every other consequential change, so
        # it is in the JSONL archive whether or not the database is up.
        bus.publish_raw("xams/audit", json.dumps({
            "t": result.get("at") or "", "actor": result.get("by") or "",
            "action": "alarm_notifications",
            "target": "all", "old": "on" if result["was"] else "off",
            "new": "on" if result["enabled"] else "off",
        }, separators=(",", ":")))
        bus.publish_raw(ACK_NOTIFY, json.dumps(result, separators=(",", ":")))

    def handle_arm(topic: str, payload: str) -> None:
        """One switchable alarm off or on (alarms.yaml `switchable`).

        Answered like the master switch, and audited whether or not it was
        refused, so an attempt to switch off a channel that may not be
        switched is on record too.
        """
        try:
            request = json.loads(payload)
            channel = str(request["channel"])
            wanted = bool(request["enabled"])
        except (ValueError, KeyError, TypeError):
            log.error("arm command ignored, malformed: %r", payload)
            bus.publish_raw(ACK_ARM, json.dumps(
                {"ok": False, "reason": "malformed command"}))
            return
        by = str(request.get("by") or "")
        result = engine.set_armed(channel, wanted, by)
        bus.publish_raw("xams/audit", json.dumps({
            "t": result.get("at") or "", "actor": by or "unknown",
            "action": "alarm_switch", "target": channel,
            "old": None if not result.get("ok") else
                   ("on" if result["was"] else "off"),
            "new": "on" if wanted else "off",
            "result": "ok" if result.get("ok") else "refused",
            "detail": result.get("reason", ""),
        }, separators=(",", ":")))
        bus.publish_raw(ACK_ARM, json.dumps(result, separators=(",", ":")))

    bus.subscribe("xams/cmd/all/reload", handle_reload)
    bus.subscribe(TOPIC_NOTIFY, handle_notify)
    bus.subscribe(TOPIC_ARM, handle_arm)

    bus.connect()
    bus.publish_state("alarms", ServiceState.RUNNING)
    log.info("alarm engine running (stale after %.0fs, repeat every %.0f min)",
             engine.stale_after, engine.min_repeat_s / 60)
    # Said at every start, because a switch thrown a fortnight ago and
    # restored from a file is exactly the one nobody remembers (§4.4a).
    engine.publish_notifications()
    engine.publish_armed()
    for channel, sw in engine.armed_view().items():
        if not sw["enabled"]:
            log.critical("ALARM %s IS SWITCHED OFF (by %s, %s): evaluated "
                         "and shown, nobody is told.", channel,
                         sw["by"] or "somebody", sw["at"] or "unrecorded time")
    if not engine.notifications_enabled:
        log.critical("ALARM NOTIFICATIONS ARE SWITCHED OFF (by %s, %s). "
                     "Alarms are evaluated and recorded, and NOBODY WILL BE "
                     "TOLD. Turn them back on from /alarms.",
                     engine.notify_changed_by or "somebody",
                     engine.notify_changed_at or "at an unrecorded time")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass

    # Ten seconds, matching BaseService, so the 60 s staleness window has the
    # same six-fold margin everywhere. The alarm summary below keeps its own
    # thirty-second cadence: it is a log line somebody greps, not a liveness
    # signal, and tripling it would only make it harder to read.
    beats = 0
    try:
        while not stop.is_set():
            stop.wait(10)
            bus.publish_heartbeat("alarms")
            beats += 1
            if beats % 3:
                continue
            active = engine.active()
            if active:
                # Which way the switch is sits next to the list, because this
                # is the line somebody greps during an incident, and "what is
                # wrong" and "is anybody being told" are one question (§4.4a).
                log.log(logging.CRITICAL if not engine.notifications_enabled
                        else logging.INFO,
                        "%d active alarm(s): %s%s", len(active),
                        ", ".join(f"{s.channel}={s.state}" for s in active),
                        "" if engine.notifications_enabled
                        else " - NOTIFICATIONS ARE OFF, nobody is being told")
    finally:
        log.info("shutting down; %d flight-recorder dump(s) this run",
                 recorder.dumps)
        engine.stop()
        bus.publish_state("alarms", ServiceState.STOPPED)
        bus.disconnect()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
