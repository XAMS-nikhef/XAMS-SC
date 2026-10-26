"""Alarm evaluation. See DESIGN.md §11.

Subscribes to `xams/meas/#`, evaluates against `alarms.yaml`, and publishes
alarm state to `xams/alarm/<channel>`. It decides and notifies; Grafana only
displays the result.

**Grafana's own alerting is deliberately not used.** It works by querying the
database on a schedule, which puts PostgreSQL and Grafana inside the alarm
path: if either is down or slow, notifications do not go out. Alarms are the
part of this system that must be most reliable, so the path is kept as short as
possible — measurement, bus, engine, SMS — with no database and no web server
involved.

Four threshold levels per channel, EPICS-style: `lolo`, `low`, `high`, `hihi`.

**Staleness is an alarm at the same severity as a threshold breach.** A dead
sensor must not read as healthy, and a channel that has stopped arriving is
indistinguishable from one that never had a problem unless something says so.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..bus import STATUS_ARMED, STATUS_NOTIFY, STATUS_READOUT, TOPIC_ALARM, TOPIC_MEAS, Bus
from ..model import Measurement, Quality, iso, utcnow

log = logging.getLogger(__name__)

# Ordered least to most severe. Used to decide whether a transition is an
# escalation (notify immediately) or a de-escalation.
SEVERITY_ORDER = ["ok", "minor", "major", "critical"]

# Which threshold names mean "too low" — they compare with < rather than >.
LOW_THRESHOLDS = {"lolo", "low"}


@dataclass
class _ViewLike:
    """The few attributes mail.py asks of a reading.

    The engine does not hold ChannelView objects - those belong to the web UI
    and are built from retained MQTT. Rather than couple the two, the engine
    hands the renderer something with the same shape.
    """

    name: str
    value: float | None
    unit: str
    healthy: bool
    quality: str = "stale"

    def formatted(self) -> str:
        if self.value is None:
            return "--"
        magnitude = abs(self.value)
        if magnitude >= 1000:
            return f"{self.value:.0f}"
        return f"{self.value:.2f}" if magnitude >= 10 else f"{self.value:.3f}"


@dataclass
class ChannelState:
    """What the engine remembers about one channel."""

    channel: str
    state: str = "ok"
    threshold: str | None = None
    since: datetime | None = None
    last_notified: float = 0.0
    acknowledged: bool = False
    silenced_until: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)
    last_value: float | None = None
    stale: bool = False


# How many notifications may wait for the gateway before the engine starts
# dropping them. Reached only when the gateway is unreachable AND alarms keep
# transitioning - the CAEN link dying takes 32 channels with it - and at that
# point the plant has a bigger problem than the queue. A bound is here so that
# a wedged gateway cannot grow this without limit; the drop is LOUD.
NOTIFY_QUEUE_MAX = 100


class AlarmEngine:
    """Evaluates measurements against thresholds and raises alarms.

    Notification is injected rather than imported, so the engine can be tested
    without a gateway and so what sits underneath SMS is replaceable without
    touching alarm logic (§11).
    """

    def __init__(self, bus: Bus, config, notifier=None, on_alarm=None,
                 notify_state_path: Path | str = "data/alarm_notify.json",
                 armed_state_path: Path | str = "data/alarm_armed.json"):
        self.bus = bus
        self.config = config
        self.notifier = notifier
        # THE MASTER SWITCH (§4.4a). Evaluation is unaffected by it: what it
        # turns off is delivery, so the pages, the history and the flight
        # recorder all carry on telling the truth while nobody is woken up.
        self.notify_state_path = Path(notify_state_path)
        self.notifications_enabled = True
        self.notify_changed_by = ""
        self.notify_changed_at = ""
        self._restore_notifications()
        # PER-CHANNEL SWITCHES, for the few alarms alarms.yaml lists as
        # `switchable`. Same rule as the master switch: delivery only, and a
        # restart does not quietly re-arm one somebody switched off.
        self.armed_state_path = Path(armed_state_path)
        self.armed: dict[str, dict] = {}
        self._restore_armed()
        # Called when an alarm is raised — the flight recorder subscribes here
        # so the dump happens at the moment of the alarm (§9.1).
        self.on_alarm = on_alarm

        alarms = config.alarms or {}
        self.defaults = alarms.get("defaults") or {}
        self.thresholds = alarms.get("channels") or {}
        self.routing = alarms.get("routing") or {}
        self.staleness = alarms.get("staleness") or {}
        self.switchable = frozenset(alarms.get("switchable") or [])

        self.stale_after = float(self.defaults.get("stale_after_seconds", 60))
        self.min_repeat_s = float(self.defaults.get("min_repeat_minutes", 15)) * 60
        self.hysteresis_fraction = float(self.defaults.get("hysteresis", 0.02))

        self._states: dict[str, ChannelState] = {}
        # Channels whose cDAQ readout is SWITCHED OFF (STATUS_READOUT, owned by
        # the cdaq service; the PT1000s while the HV is on). Treated like a
        # disabled channel: not evaluated, not stale, alarm withdrawn.
        self._readout_status: dict | None = None
        self._readout_off: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Notification is NETWORK I/O and must not happen on the thread that
        # evaluates measurements. See `_dispatch`. Created by `start()`,
        # because a queue nobody drains is worse than no queue.
        self._outbox: queue.Queue | None = None
        self._notify_thread: threading.Thread | None = None

    # ------------------------------------------------------------- evaluation

    def _threshold_for(self, channel: str, value: float, current: str | None):
        """Return (threshold_name, severity) for a value, or (None, 'ok').

        Hysteresis: a channel already in alarm must come back past the
        threshold by `hysteresis` of its own magnitude before clearing. This
        is what stops a value sitting exactly on a limit from producing a
        stream of notifications.
        """
        limits = self.thresholds.get(channel) or {}
        if not limits:
            return None, "ok"

        # Most severe first, so a value past hihi reports hihi rather than high.
        for name in ("hihi", "lolo", "high", "low"):
            spec = limits.get(name)
            if not spec:
                continue
            limit = float(spec["value"])
            margin = abs(limit) * self.hysteresis_fraction
            if name in LOW_THRESHOLDS:
                # Already alarming on this threshold: require recovery past
                # limit + margin before it clears.
                trip = limit + margin if current == name else limit
                if value < trip:
                    return name, str(spec.get("severity", "minor"))
            else:
                trip = limit - margin if current == name else limit
                if value > trip:
                    return name, str(spec.get("severity", "minor"))
        return None, "ok"

    def _watched(self, channel: str) -> bool:
        """Is this channel enabled in channels.yaml? Unknown ones are watched.

        A channel set `enabled: false` is one somebody has decided not to
        read - a failed sensor, a broken readout unit. It must not alarm, and
        it must not alarm as STALE either, which is what happens otherwise:
        its driver stops publishing and 60 s later the silence is an alarm.
        """
        if channel in self._readout_off:
            # Switched off at run time (STATUS_READOUT): the same rule, for a
            # readout somebody has stopped on purpose.
            return False
        cfg = self.config.channels.get(channel)
        return cfg is None or cfg.enabled

    def on_readout_status(self, topic: str, payload: str) -> None:
        """A cDAQ readout switched off or on (retained STATUS_READOUT).

        Off: the module's channels are withdrawn exactly as a channel disabled
        in channels.yaml is - an alarm they hold is published back to ok (so
        the history records that it ended), and they are no longer checked
        for staleness. On: nothing to do here; they are watched again from
        their next reading, and a sensor still broken announces itself then.
        """
        from ..readout import off_channels, parse_status
        status = parse_status(payload)
        off = off_channels(self.config, status)
        with self._lock:
            self._readout_status = status
            newly_off = off - self._readout_off
            back_on = self._readout_off - off
            self._readout_off = off
            for name in sorted(newly_off):
                state = self._states.pop(name, None)
                if state is None or (state.state == "ok" and not state.stale):
                    continue
                log.info("%s: readout switched off, alarm withdrawn", name)
                state.state, state.threshold, state.stale = "ok", None, False
                state.since = utcnow()
                self._publish(state, None)
        if newly_off:
            log.warning("readout switched OFF for %s: not evaluated until it "
                        "is switched back on", ", ".join(sorted(newly_off)))
        if back_on:
            log.info("readout switched back on for %s", ", ".join(sorted(back_on)))

    def _on_retained_alarm(self, topic: str, payload: str) -> None:
        """Withdraw an alarm left on the broker for a channel now disabled.

        A channel disabled while it was in alarm keeps that state RETAINED,
        and after a restart this engine never sees the channel again - so
        nothing would ever publish it back to ok, and every page would show
        it in alarm forever. Its own `ok` comes back here too, and stops.
        """
        channel = topic.rsplit("/", 1)[-1]
        if self._watched(channel) or not payload.strip():
            return
        try:
            if json.loads(payload).get("state", "ok") == "ok":
                return
        except (ValueError, AttributeError):
            return
        with self._lock:
            if channel in self._states:
                return
        log.info("%s: disabled in channels.yaml, withdrawing its retained "
                 "alarm", channel)
        self._publish(ChannelState(channel, since=utcnow()), None)

    def on_measurement(self, m: Measurement) -> None:
        """Evaluate one reading. Called from the bus handler."""
        if not self._watched(m.channel):
            # The driver may still be publishing it until it is restarted.
            return
        with self._lock:
            first_time = m.channel not in self._states
            state = self._states.setdefault(m.channel, ChannelState(m.channel))
            state.last_seen = time.monotonic()

            if first_time:
                # PUBLISH THE STATE THE FIRST TIME A CHANNEL IS SEEN, even when
                # it is ok.
                #
                # Otherwise a channel that was in alarm when the engine last
                # stopped keeps that as its recorded state forever: on restart
                # the channel is healthy, nothing "changes", so no transition
                # is published and the stored history still says alarm. The
                # Grafana table then shows permanent phantom alarms for
                # channels that are perfectly fine — which is worse than no
                # table, because it trains people to ignore it.
                #
                # One extra row per channel per engine start is a cheap price
                # for the recorded state matching reality after a restart.
                state.since = utcnow()
                self._publish(state, m.value)
            state.last_value = m.value
            if state.stale:
                state.stale = False
                log.info("%s is being published again", m.channel)

            # A reading that is not OK is itself an alarm condition: the
            # sensor is not reporting, which is not the same as being fine.
            if m.quality is not Quality.OK or m.value is None:
                self._transition(state, "major", f"quality={m.quality.value}",
                                  m.value)
                return

            threshold, severity = self._threshold_for(
                m.channel, m.value, state.threshold)
            self._transition(state, severity, threshold, m.value)

    def _transition(self, state: ChannelState, severity: str,
                    threshold: str | None, value: float | None) -> None:
        """Move a channel to a new state and notify if it deserves it."""
        now = time.monotonic()
        changed = severity != state.state or threshold != state.threshold

        if changed:
            previous = state.state
            state.state = severity
            state.threshold = threshold
            state.since = utcnow()
            # A NEW condition clears an acknowledgement. Acknowledging "high"
            # must not silence the "hihi" that follows it.
            if SEVERITY_ORDER.index(severity) > SEVERITY_ORDER.index(previous):
                state.acknowledged = False
            log.log(logging.WARNING if severity != "ok" else logging.INFO,
                    "%s: %s -> %s (%s, value=%s)",
                    state.channel, previous, severity, threshold or "-", value)
            self._publish(state, value)
            if severity != "ok":
                self._notify(state, value)
                if self.on_alarm is not None:
                    try:
                        self.on_alarm(state.channel, severity, threshold)
                    except Exception:
                        log.exception("on_alarm hook raised")
            return

        # Unchanged and still in alarm: repeat at the configured interval,
        # unless acknowledged or silenced. Deduplication (§11).
        if severity != "ok" and not state.acknowledged and now >= state.silenced_until:
            if now - state.last_notified >= self.min_repeat_s:
                self._notify(state, value)

    # ---------------------------------------------------------------- output

    def _publish(self, state: ChannelState, value: float | None) -> None:
        payload = json.dumps({
            "state": state.state,
            "threshold": state.threshold,
            "value": value,
            "since": iso(state.since) if state.since else None,
            "acknowledged": state.acknowledged,
        }, separators=(",", ":"))
        # Retained, so a UI connecting later sees the true current state (§11).
        self.bus.publish_raw(f"{TOPIC_ALARM}/{state.channel}", payload, retain=True)

    def _notify(self, state: ChannelState, value: float | None) -> None:
        # THE MASTER SWITCH, checked here rather than around the evaluation
        # (§4.4a). Everything above this line has already happened: the state
        # is published, the history has it, the flight recorder has dumped.
        # Only the waking-somebody-up is skipped.
        #
        # `last_notified` is deliberately NOT stamped while off. It is the
        # clock for the repeat, so leaving it stale means that the moment
        # alarms are switched back on, everything still wrong announces itself
        # on its next reading instead of waiting out the repeat interval in
        # silence. Coming back on must not be quiet.
        if not self.notifications_enabled:
            log.warning("NOT notifying for %s (%s): alarm notifications are "
                        "switched OFF, by %s", state.channel, state.state,
                        self.notify_changed_by or "somebody")
            return
        # The per-channel switch, by the same rules: evaluated, published and
        # recorded already; `last_notified` left alone so switching it back on
        # announces a condition still present on its next reading.
        if not self.is_armed(state.channel):
            log.warning("NOT notifying for %s (%s): this alarm is switched "
                        "OFF, by %s", state.channel, state.state,
                        self.armed.get(state.channel, {}).get("by") or "somebody")
            return
        state.last_notified = time.monotonic()
        if self.notifier is None:
            return
        text = (f"XAMS {state.state.upper()}: {state.channel} "
                f"{state.threshold or ''} value={value}")
        channels = self._routes_for(state)

        # SMS stays terse - it is charged per message and read on a lock
        # screen. Email carries the context, because the reader of an email at
        # three in the morning is not going to open the web UI to find out
        # what else was happening (section 11).
        rich = None
        try:
            rich = self._email_for(state, value)
        except Exception:
            log.exception("could not build the alarm email for %s; "
                          "falling back to plain text", state.channel)

        self._dispatch(state.channel, text, channels, rich)

    # ------------------------------------------------------------- dispatch
    #
    # WHY THE MESSAGE LEAVES ON ANOTHER THREAD.
    #
    # `on_measurement` runs on paho's network thread and holds `self._lock`
    # all the way down to here. Sending used to happen on that thread, under
    # that lock, and it is network I/O: `send_email` opens a NEW SMTP
    # connection per recipient with a 20 s socket timeout, so an unreachable
    # mail server costs 20 s per blocking step per person. The SMS client is
    # given no timeout at all - the messagebird SDK takes none - so a gateway
    # that accepts the connection and then says nothing blocks for good.
    #
    # While that ran, no measurement was evaluated, and paho could not send
    # its keepalive either, so a 30 s stall dropped the broker connection.
    # The alarm engine going deaf because it is busy telephoning about the
    # first alarm is the failure this subsystem exists to prevent, and a
    # cascade makes it worse rather than better: transitions are per channel
    # with no aggregation, so one dead CAEN link is 32 of them at once.
    #
    # What this does NOT fix is a gateway that hangs for ever: the worker is
    # then stuck on one message and the queue fills behind it. It turns a
    # dead engine into undelivered alarms that say so in the log, which is
    # the better of the two, not a cure. A timeout belongs in `notify.py`
    # the day the SDK offers one.

    def _dispatch(self, channel: str, text: str, channels: list[str],
                  rich) -> None:
        """Hand one message to whoever will send it."""
        outbox = self._outbox
        if outbox is None:
            # No worker to hand it to. The engine was built but never
            # started, which is how the unit tests drive it - there is no
            # other thread in that case, so this IS the sending thread.
            self._send(channel, text, channels, rich)
            return
        try:
            outbox.put_nowait((channel, text, channels, rich))
        except queue.Full:
            # Loud, and in the same words as an empty recipient list: both
            # mean somebody who should have been told was not (§4.4).
            log.error("ALARM NOT DELIVERED: %d notifications are already "
                      "waiting for the gateway, so this one was dropped. %s",
                      NOTIFY_QUEUE_MAX, text)

    def _send(self, channel: str, text: str, channels: list[str],
              rich) -> None:
        try:
            self.notifier.send(text, channels, rich=rich)
        except Exception:
            # A failing gateway must not stop the engine evaluating.
            log.exception("notification failed for %s", channel)

    def _notify_pump(self) -> None:
        """Send what the engine has queued, one at a time.

        Serial on purpose: the gateway is rate-limited and the order alarms
        went off in is information. `None` is the sentinel that ends it.
        """
        while True:
            item = self._outbox.get()
            try:
                if item is None:
                    return
                self._send(*item)
            finally:
                self._outbox.task_done()

    def _email_for(self, state: ChannelState, value):
        """Subject, HTML and text for this alarm, with the plant around it."""
        from . import mail
        from ..model import utcnow

        description = ""
        channel = self.config.channels.get(state.channel)
        if channel is not None:
            description = channel.description

        # Everything else the engine currently believes, which is what makes
        # the mail worth reading on its own.
        others = [
            {"channel": s.channel, "state": s.state, "threshold": s.threshold,
             "value": s.last_value, "acknowledged": s.acknowledged,
             "description": (self.config.channels[s.channel].description
                             if s.channel in self.config.channels else "")}
            for s in self._states.values()
            if s.state != "ok" and s.channel != state.channel]

        readings = []
        for name in ("pmain", "tt401", "tt402", "tt104", "tt302", "fm101"):
            other = self._states.get(name)
            if other is None:
                continue
            readings.append((name, _ViewLike(
                name, other.last_value,
                (self.config.channels[name].unit
                 if name in self.config.channels else ""),
                other.state == "ok" and not other.stale)))

        context = {"alarms": others, "readings": readings,
                   "services": [], "config_hash": self.config.config_hash}
        return mail.alarm(state.channel, state.state, state.threshold, value,
                          description, context, utcnow())

    def _routes_for(self, state: ChannelState) -> list[str]:
        limits = self.thresholds.get(state.channel) or {}
        spec = limits.get(state.threshold or "") or {}
        if spec.get("notify"):
            return list(spec["notify"])
        if state.threshold is None:          # quality or staleness
            return list(self.staleness.get("notify") or ["email"])
        return ["email"]

    # -------------------------------------------------------------- staleness

    def check_staleness(self) -> None:
        """Raise an alarm for any channel that has stopped arriving.

        This is why the engine cannot be purely event-driven: the absence of a
        message is the signal, and nothing will deliver it.
        """
        now = time.monotonic()
        severity = str(self.staleness.get("severity", "major"))
        with self._lock:
            for state in self._states.values():
                age = now - state.last_seen
                if age > self.stale_after and not state.stale:
                    state.stale = True
                    log.warning("%s has not been published for %.0fs — stale",
                                state.channel, age)
                    self._transition(state, severity, "staleness", None)

    # ----------------------------------------------------------------- reload

    def reload(self, config) -> bool:
        """Apply new thresholds without restarting. See DESIGN.md §12.

        This is the service where reload matters most: a threshold change is
        the commonest configuration edit, and restarting the engine to apply
        one loses every channel's alarm state — including which alarms are
        acknowledged, which would start the notifications again.

        Per-channel state is therefore KEPT across a reload. A channel whose
        threshold moved is re-evaluated on its next reading, which arrives
        within the log interval.
        """
        alarms = config.alarms or {}
        self.config = config
        self.defaults = alarms.get("defaults") or {}
        self.thresholds = alarms.get("channels") or {}
        self.routing = alarms.get("routing") or {}
        self.staleness = alarms.get("staleness") or {}
        self.switchable = frozenset(alarms.get("switchable") or [])
        self.stale_after = float(self.defaults.get("stale_after_seconds", 60))
        self.min_repeat_s = float(self.defaults.get("min_repeat_minutes", 15)) * 60
        self.hysteresis_fraction = float(self.defaults.get("hysteresis", 0.02))
        self.publish_armed()
        from ..readout import off_channels
        with self._lock:
            self._readout_off = off_channels(config, self._readout_status)

        # A channel that has been DISABLED is forgotten entirely: its alarm is
        # withdrawn and it is no longer checked for staleness. Withdrawn by
        # publishing it as ok rather than deleting the retained topic, so the
        # alarm history records that it ended instead of keeping "major" as
        # the channel's last word.
        with self._lock:
            for name in [n for n in self._states if not self._watched(n)]:
                state = self._states.pop(name)
                if state.state == "ok" and not state.stale:
                    continue
                log.info("%s: disabled in channels.yaml, alarm withdrawn", name)
                state.state, state.threshold, state.stale = "ok", None, False
                state.since = utcnow()
                self._publish(state, None)

        # A channel that no longer has any threshold must not stay in alarm on
        # a limit that has been deleted.
        with self._lock:
            for state in self._states.values():
                if (state.threshold in (None, "staleness")
                        or state.channel in self.thresholds):
                    continue
                log.info("%s: thresholds removed, clearing its alarm", state.channel)
                state.state, state.threshold = "ok", None
                self._publish(state, state.last_value)

        self._publish_limits()
        return True

    # ------------------------------------------------------------- operations

    # --------------------------------------------- the master switch (§4.4a)

    def _restore_notifications(self) -> None:
        """Read the switch back at startup. Absent means ON.

        **A restart does not re-arm the alarms.** It is the more dangerous of
        the two possible defaults and it is still the right one: the operator
        who switched delivery off did not ask for it back, and a service that
        quietly re-armed itself on a restart nobody noticed would send the
        3am message the switch was thrown to prevent. It is instead shouted
        about in the log at every start, on every page, and in the status
        topic, so "off" is never a state the system is quietly in.
        """
        try:
            data = json.loads(self.notify_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.notifications_enabled = bool(data.get("enabled", True))
        self.notify_changed_by = str(data.get("by") or "")
        self.notify_changed_at = str(data.get("at") or "")

    def _save_notifications(self) -> None:
        """Persist the switch, atomically, beside the other runtime state."""
        payload = {"enabled": self.notifications_enabled,
                   "by": self.notify_changed_by, "at": self.notify_changed_at}
        try:
            self.notify_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.notify_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.notify_state_path)
        except OSError:
            # Not fatal, and not silent: the switch is in force for this
            # process either way, and the retained topic carries it to the
            # pages. What is lost is only that it survives a restart.
            log.exception("could not persist the notification switch to %s",
                          self.notify_state_path)

    def publish_notifications(self) -> None:
        """Say, retained, whether anybody would be told (§4.4a).

        Retained because a page that connects later must not have to wait for
        the next change to find out that the alarms are off. There is no
        "unknown" here - the absence of this topic is what unknown looks like,
        and the pages render that as unknown rather than as enabled.
        """
        self.bus.publish_raw(STATUS_NOTIFY, json.dumps({
            "enabled": self.notifications_enabled,
            "by": self.notify_changed_by,
            "at": self.notify_changed_at,
        }, separators=(",", ":")), retain=True)

    def set_notifications(self, enabled: bool, by: str) -> dict:
        """Turn alarm delivery off or on for the whole system.

        Returns what happened, for the acknowledgement. Switching to the state
        it is already in is reported as ok and changes nothing, so a double
        click is harmless.
        """
        was = self.notifications_enabled
        with self._lock:
            self.notifications_enabled = bool(enabled)
            self.notify_changed_by = by or "unknown"
            self.notify_changed_at = iso(utcnow())
            active = [s.channel for s in self._states.values()
                      if s.state != "ok"]
        self._save_notifications()
        self.publish_notifications()

        if enabled:
            log.warning("ALARM NOTIFICATIONS SWITCHED ON by %s%s", by,
                        (" - %d channel(s) still in alarm will announce "
                         "themselves on their next reading: %s"
                         % (len(active), ", ".join(sorted(active))))
                        if active else "")
        else:
            log.critical("ALARM NOTIFICATIONS SWITCHED OFF by %s. Alarms are "
                         "still evaluated and recorded; NOBODY WILL BE TOLD "
                         "about them until this is switched back on.", by)
        return {"ok": True, "enabled": self.notifications_enabled,
                "was": was, "active": sorted(active), "by": by,
                "at": self.notify_changed_at}

    # ------------------------------------------- per-channel switches

    def is_armed(self, channel: str) -> bool:
        """Would an alarm on this channel be delivered, as far as its own
        switch goes? Always True for a channel that is not switchable: a
        switch file left over from an earlier configuration cannot silence a
        channel that alarms.yaml no longer lets anybody switch."""
        if channel not in self.switchable:
            return True
        return bool(self.armed.get(channel, {}).get("enabled", True))

    def _restore_armed(self) -> None:
        try:
            data = json.loads(self.armed_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.armed = {str(k): dict(v) for k, v in data.items()
                          if isinstance(v, dict)}

    def _save_armed(self) -> None:
        try:
            self.armed_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.armed_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.armed), encoding="utf-8")
            os.replace(tmp, self.armed_state_path)
        except OSError:
            log.exception("could not persist the alarm switches to %s",
                          self.armed_state_path)

    def armed_view(self) -> dict:
        """Every switchable channel and its switch; absent means on."""
        return {ch: {"enabled": self.is_armed(ch),
                     "by": str(self.armed.get(ch, {}).get("by") or ""),
                     "at": str(self.armed.get(ch, {}).get("at") or "")}
                for ch in sorted(self.switchable)}

    def publish_armed(self) -> None:
        """Retained, so a page connecting later sees every switch at once."""
        self.bus.publish_raw(STATUS_ARMED, json.dumps(
            self.armed_view(), separators=(",", ":")), retain=True)

    def set_armed(self, channel: str, enabled: bool, by: str) -> dict:
        """Switch one alarm's notification off or on.

        Refused for a channel alarms.yaml does not list as switchable: this
        is a switch for an expected condition, and it must not be a way to
        silence pmain or the UPS one request at a time.
        """
        if channel not in self.switchable:
            return {"ok": False, "channel": channel,
                    "reason": f"{channel} is not a switchable alarm"}
        was = self.is_armed(channel)
        with self._lock:
            self.armed[channel] = {"enabled": bool(enabled),
                                   "by": by or "unknown", "at": iso(utcnow())}
            state = self._states.get(channel)
            current = state.state if state else "ok"
        self._save_armed()
        self.publish_armed()
        if enabled:
            log.warning("ALARM %s SWITCHED ON by %s%s", channel, by,
                        f" - still {current}, will notify on its next reading"
                        if current != "ok" else "")
        else:
            log.critical("ALARM %s SWITCHED OFF by %s. Still evaluated and "
                         "shown; nobody will be told about it until it is "
                         "switched back on.", channel, by)
        return {"ok": True, "channel": channel, "enabled": bool(enabled),
                "was": was, "state": current, "by": by,
                "at": self.armed[channel]["at"]}

    def acknowledge(self, channel: str) -> bool:
        """Stop the repeating notification. The condition stays active.

        Acknowledging is not fixing, and the two must never look alike (§8.1).
        """
        with self._lock:
            state = self._states.get(channel)
            if state is None or state.state == "ok":
                return False
            state.acknowledged = True
            log.info("%s acknowledged — still %s", channel, state.state)
            self._publish(state, state.last_value)
            return True

    def silence(self, channel: str, minutes: float) -> bool:
        """Suppress notifications for a deliberate intervention."""
        with self._lock:
            state = self._states.get(channel)
            if state is None:
                return False
            state.silenced_until = time.monotonic() + minutes * 60
            log.info("%s silenced for %.0f minutes", channel, minutes)
            return True

    def active(self) -> list[ChannelState]:
        with self._lock:
            return [s for s in self._states.values() if s.state != "ok"]

    # ------------------------------------------------------------------- run

    def start(self) -> None:
        # The sender FIRST, and before the subscription: a message evaluated
        # between subscribing and starting the worker would be sent on this
        # thread, which is the whole thing being fixed.
        self._outbox = queue.Queue(maxsize=NOTIFY_QUEUE_MAX)
        self._notify_thread = threading.Thread(
            target=self._notify_pump, name="alarm-notify", daemon=True)
        self._notify_thread.start()

        def handler(topic: str, payload: str) -> None:
            try:
                self.on_measurement(Measurement.from_payload(json.loads(payload)))
            except Exception:
                log.exception("could not evaluate message on %s", topic)

        self.bus.subscribe(f"{TOPIC_MEAS}/#", handler)
        self.bus.subscribe(f"{TOPIC_ALARM}/+", self._on_retained_alarm)
        self.bus.subscribe(STATUS_READOUT, self.on_readout_status)
        self._publish_limits()
        self.publish_notifications()
        self.publish_armed()

        def pump():
            while not self._stop.is_set():
                try:
                    self.check_staleness()
                except Exception:
                    log.exception("staleness check raised")
                self._stop.wait(5.0)

        self._thread = threading.Thread(target=pump, name="alarm-staleness",
                                        daemon=True)
        self._thread.start()

    def _publish_limits(self) -> None:
        """Publish the thresholds actually loaded (§11).

        The real risk is not a missing line on a plot; it is believing a
        threshold is 2.0 when it is 20. This exposes what is in force.
        """
        summary = {
            channel: {name: spec.get("value")
                      for name, spec in (limits or {}).items()}
            for channel, limits in self.thresholds.items()
        }
        self.bus.publish_raw("xams/status/limits",
                             json.dumps(summary, separators=(",", ":")),
                             retain=True)
        if not summary:
            log.warning(
                "NO PHYSICS THRESHOLDS ARE CONFIGURED. Staleness and bad-quality "
                "alarms still work, but no channel has a lolo/low/high/hihi "
                "limit. Fill alarms.yaml from the LabVIEW Error and Alarm tab.")
        else:
            log.info("thresholds in force for %d channel(s): %s",
                     len(summary), ", ".join(sorted(summary)))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._notify_thread:
            # The sentinel goes in front of nothing: whatever is already
            # queued is sent first, so a shutdown does not swallow the alarm
            # that prompted it. Bounded, because the worker may be stuck on a
            # gateway that will never answer and a service that cannot be
            # stopped is its own fault - it is a daemon thread and the
            # process exits regardless.
            with contextlib.suppress(queue.Full):
                self._outbox.put_nowait(None)
            self._notify_thread.join(timeout=5)
            if self._notify_thread.is_alive():
                log.warning("the notification worker did not finish in 5 s; "
                            "a gateway is probably still hanging")
