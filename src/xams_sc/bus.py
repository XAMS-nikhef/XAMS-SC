"""MQTT publish/subscribe. See DESIGN.md §2.1, §3, §6.4.

Every service publishes here; nothing writes to a database directly. The bus
is what keeps acquisition, scaling, storage and alarming from entangling —
it makes the wrong thing awkward to do.

Topics (§3):

    xams/meas/<channel>             measurement, JSON, retained
    xams/status/<service>/heartbeat ISO-8601 UTC, retained
    xams/status/<service>/state     starting | running | degraded | stopped
    xams/alarm/<channel>            alarm state changes
    xams/cmd/<device>/<action>      control requests
    xams/ack/<device>/<action>      command acknowledgements

The broker is a shared dependency, so an outage must cost a visible gap and
never a silent one. This client buffers while disconnected and republishes in
order when the broker returns; if the buffer overflows, the loss is counted
and reported rather than passing unnoticed.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from typing import Callable

import paho.mqtt.client as mqtt

from .model import Measurement, ServiceState, iso, utcnow

log = logging.getLogger(__name__)

TOPIC_MEAS = "xams/meas"
TOPIC_STATUS = "xams/status"
TOPIC_ALARM = "xams/alarm"
TOPIC_CMD = "xams/cmd"
TOPIC_ACK = "xams/ack"
TOPIC_RELOAD = "xams/cmd/all/reload"
TOPIC_FLOW_RESET = "xams/cmd/derived/flow_reset"
TOPIC_FLOW_PERIOD = "xams/flow/period"

# The Lake Shore control path (§10). These are the first topics in the system
# that cause an instrument to do something, as against reporting what it has
# done, which is why every one of them has an acknowledgement beside it.
TOPIC_LS_SETPOINT = "xams/cmd/lakeshore/setpoint"
TOPIC_LS_RANGE = "xams/cmd/lakeshore/range"
ACK_LS_SETPOINT = "xams/ack/lakeshore/setpoint"
ACK_LS_RANGE = "xams/ack/lakeshore/range"

# Every write, published for a sink to store (§2.1, §10 rule 5). Publishing it
# rather than writing it here means an audit record survives the database
# being down: it is in the JSONL archive either way.
TOPIC_AUDIT = "xams/audit"

# The nightly backup's last result, published RETAINED by tools/backup.ps1 so
# that anything wanting to know can ask the broker rather than read a file on
# the lab PC (section 2.1).
TOPIC_BACKUP = "xams/backup/status"

# The CAEN setpoint path (section 10a). VSET only: nothing writes MAXV, RUP,
# RDW, TRIP or ISET, and there is no command to enable or disable a channel -
# that stays a hand operation at the supply, which is the last thing standing
# between a bug and an electrode.
TOPIC_HV_VSET = "xams/cmd/caen/vset"
ACK_HV_VSET = "xams/ack/caen/vset"

# Energise or de-energise one channel: the "turn ON HV" of section 10a. This
# is NOT the enable switch - that clears the DISABLED bit and stays a hand
# operation at the front panel. A channel must already be enabled before this
# can do anything.
TOPIC_HV_OUTPUT = "xams/cmd/caen/output"
ACK_HV_OUTPUT = "xams/ack/caen/output"

# Recover a channel the board has TRIPPED. A trip latches - the channel is
# switched off and stays flagged until the board alarm is cleared (BDCLR) - and
# before this existed the only way back was a power cycle. Clearing zeroes the
# setpoint of every latched channel on that supply first, so recovery always
# comes back at 0 V and never ramps into whatever caused the trip (§10a).
TOPIC_HV_CLEAR = "xams/cmd/caen/clear"
ACK_HV_CLEAR = "xams/ack/caen/clear"

# A trip the CAEN service has detected, one RETAINED record per hv_vset
# channel: `xams/hv/trip/<channel>`. Present from the moment a trip is seen
# until somebody clears it, and emptied then. Retained so the page shows it
# from the moment it connects and a restarted service still knows the
# channel is waiting for an acknowledgement.
#
# Deliberately NOT an alarm (xams/alarm/...): an HV trip happens with people
# in the lab, and it is nothing to wake anybody up for (A.P. Colijn,
# 23 September 2026). It is shown on /hv, logged and audited.
TOPIC_HV_TRIP = "xams/hv/trip"

# THE MASTER NOTIFICATION SWITCH (§4.4a). Turns alarm DELIVERY off and on for
# the whole system - the engine keeps evaluating, publishing and recording,
# and simply stops waking people up. It exists because the slow control runs
# when the plant does not, and a fortnight of "the cryostat is warm" at three
# in the morning teaches people to ignore the one that matters.
#
# The status is RETAINED, so every page shows it from the moment it connects
# and no page can be looking at a system whose alarms are off without saying
# so.
TOPIC_NOTIFY = "xams/cmd/alarms/notify"
ACK_NOTIFY = "xams/ack/alarms/notify"
STATUS_NOTIFY = "xams/status/notify"

# What the RGA is and who controls it (docs/drivers/rga.md): identity,
# firmware, filaments, units and the controlling client, RETAINED for the RGA
# page. Descriptive, not a measurement - it is outside xams/meas, so the
# archive and the database never see it. The numbers that matter over time
# (emission, status words, trips) are ordinary channels.
TOPIC_RGA_INFO = "xams/rga/info"

# The RGA commands. Taking and releasing control, starting and stopping a
# scan, saving the last acquisition, and emission On/Off. None reaches the
# electron multiplier or the ion source; the client cannot write those.
TOPIC_RGA_CONTROL = "xams/cmd/rga/control"
ACK_RGA_CONTROL = "xams/ack/rga/control"
TOPIC_RGA_SCAN = "xams/cmd/rga/scan"
ACK_RGA_SCAN = "xams/ack/rga/scan"
TOPIC_RGA_SAVE = "xams/cmd/rga/save"
ACK_RGA_SAVE = "xams/ack/rga/save"
# Emission On or Off (step 3). On only past the p105 guard and the
# analyser's own state; the service's only automatic action is Off.
TOPIC_RGA_EMISSION = "xams/cmd/rga/emission"
ACK_RGA_EMISSION = "xams/ack/rga/emission"

# The latest scan of the running acquisition, RETAINED, for the live plot.
# Outside xams/meas: a spectrum is never archived from the bus - only a Save
# writes one, to its own file.
TOPIC_RGA_SCANDATA = "xams/rga/scan"

# One record per Save, for the summary sink (rga_summary). Not retained: the
# file is the truth, and a missed record is rebuilt from it.
TOPIC_RGA_SAVED = "xams/rga/saved"


class Bus:
    """A thin wrapper over paho-mqtt with an outage buffer.

    Bound to loopback by default and deliberately so: commands travel over
    this bus, so anything that can reach the broker can set a high voltage
    (§8). A broker on 0.0.0.0 is an unauthenticated control interface.
    """

    def __init__(
        self,
        client_id: str,
        host: str = "127.0.0.1",
        port: int = 1883,
        buffer_seconds: float = 300.0,
        publish_interval_s: float = 1.0,
    ):
        self.client_id = client_id
        self.host = host
        self.port = port

        # Bounded buffer: roughly `buffer_seconds` worth of publishes. Bounded
        # because an unbounded queue turns a broker outage into a memory leak,
        # which is a worse failure than the one it was meant to survive.
        depth = max(64, int(buffer_seconds / max(publish_interval_s, 0.01)) * 4)
        self._buffer: collections.deque[tuple[str, str, bool]] = collections.deque(maxlen=depth)
        self._buffer_depth = depth
        self._dropped = 0
        self._dropped_logged = 0
        self._lock = threading.Lock()
        self._connected = threading.Event()

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        # A LIST, not a dict keyed by topic. Several consumers legitimately
        # subscribe to the same pattern - the JSONL archive, the PostgreSQL
        # writer and the alarm engine all want xams/meas/#. Keying by topic
        # means the second subscriber silently replaces the first, which is
        # exactly the opposite of what the bus exists to provide (§2.1).
        self._handlers: list[tuple[str, Callable[[str, str], None]]] = []
        self._client.on_message = self._on_message

    # ---------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """Start the network loop. Does not block on the broker being up.

        A service must keep reading hardware whether or not the broker is
        reachable (§6.4), so connection failure here is a warning, not a fatal.
        """
        self._client.will_set(
            f"{TOPIC_STATUS}/{self.client_id}/state",
            ServiceState.STOPPED.value,
            retain=True,
        )
        try:
            self._client.connect_async(self.host, self.port, keepalive=30)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("broker %s:%s not reachable yet: %s", self.host, self.port, exc)
        self._client.loop_start()

    def disconnect(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ---------------------------------------------------------------- callbacks

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("connected to broker %s:%s", self.host, self.port)
            self._connected.set()
            for topic in {pattern for pattern, _ in self._handlers}:
                client.subscribe(topic)
            self._flush()
        else:
            log.warning("broker refused connection: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected.clear()
        log.warning("disconnected from broker (%s); buffering", reason_code)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace")
        for pattern, handler in self._handlers:
            if mqtt.topic_matches_sub(pattern, msg.topic):
                try:
                    handler(msg.topic, payload)
                except Exception:
                    # A failing subscriber must not take down the loop, and a
                    # swallowed exception must not be invisible.
                    log.exception("handler for %s raised on topic %s", pattern, msg.topic)

    # ---------------------------------------------------------------- publish

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if self._connected.is_set():
            info = self._client.publish(topic, payload, qos=0, retain=retain)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                return
            log.debug("publish to %s failed (rc=%s); buffering", topic, info.rc)

        with self._lock:
            if len(self._buffer) == self._buffer_depth:
                self._dropped += 1
            self._buffer.append((topic, payload, retain))

    def _flush(self) -> None:
        """Republish the buffer, in order, once the broker returns."""
        with self._lock:
            pending, self._buffer = list(self._buffer), collections.deque(
                maxlen=self._buffer_depth
            )
            total, since = self._dropped, self._dropped - self._dropped_logged
            self._dropped_logged = total

        if since:
            # The gap is recorded explicitly rather than passing silently.
            log.error(
                "broker outage overflowed the buffer: %d messages lost "
                "(%d since this service started)", since, total
            )
        if pending:
            log.info("republishing %d buffered messages", len(pending))
        for topic, payload, retain in pending:
            self._client.publish(topic, payload, qos=0, retain=retain)

    @property
    def dropped(self) -> int:
        """Messages lost to buffer overflow since this service started.

        CUMULATIVE, and deliberately so. It used to be zeroed by `_flush`,
        which meant that by the time anything asked, the answer was almost
        always 0 — a counter for a data loss that could not be read is not a
        counter. A caller wanting the rate keeps its own previous value; a
        caller wanting "has this system ever lost a message" can now ask.
        """
        return self._dropped

    # ---------------------------------------------------------------- public API

    def publish_measurement(self, m: Measurement) -> None:
        self._publish(f"{TOPIC_MEAS}/{m.channel}", m.to_json(), retain=True)

    def publish_heartbeat(self, service: str) -> None:
        self._publish(f"{TOPIC_STATUS}/{service}/heartbeat", iso(utcnow()), retain=True)

    def publish_state(self, service: str, state: ServiceState) -> None:
        self._publish(f"{TOPIC_STATUS}/{service}/state", state.value, retain=True)

    def publish_raw(self, topic: str, payload: str, retain: bool = False) -> None:
        self._publish(topic, payload, retain=retain)

    def subscribe(self, topic: str, handler: Callable[[str, str], None]) -> None:
        """Register a handler. Safe to call before connect().

        Every handler registered for a matching pattern is called, so adding a
        consumer never displaces one that was already there.
        """
        self._handlers.append((topic, handler))
        if self._connected.is_set():
            self._client.subscribe(topic)

    @property
    def subscriber_count(self) -> int:
        return len(self._handlers)

class AckInbox:
    """Collects acknowledgements and hands each to the command that asked.

    Every command on this bus is answered on a topic shared by every other
    command of the same kind, so "the next ack on this topic" is not the same
    question as "the answer to my command". Two operators setting two
    different HV channels at the same moment were shown each other's result,
    and since the ack carries `old` and `new`, one of them read a confident
    -4200 V for a channel they had never touched.

    `match` names the fields that must agree between the command and the ack.
    The discriminator is already on the wire — `channel` for the HV path,
    `output` for the Lake Shore — so nothing new had to be published and no
    instrument service had to change.

    With no `match`, any ack on the topic will do, which is right for a
    command with a single target such as the master notification switch.

    Shared by the web UI, which keeps one of these for the life of the
    process, and by `CommandSession`, which keeps one for the length of a
    single CLI invocation.
    """

    def __init__(self, depth: int = 32):
        self._lock = threading.Lock()
        # A bounded deque per topic, not one slot. Two commands can be in
        # flight on the same topic and the second ack must not evict the
        # first before its waiter has looked. Bounded, because an ack nobody
        # is waiting for — a command that already timed out — must not
        # accumulate.
        self._acks: dict[str, collections.deque] = {}
        self._depth = depth

    def deliver(self, topic: str, payload: str) -> None:
        """Take an ack off the bus. Safe as a subscription handler."""
        try:
            ack = json.loads(payload)
        except ValueError:
            log.warning("unparseable acknowledgement on %s: %r", topic,
                        payload[:80])
            return
        with self._lock:
            self._acks.setdefault(
                topic, collections.deque(maxlen=self._depth)).append(ack)

    def open(self, topic: str, expected: dict) -> None:
        """Discard what is stale FOR THIS COMMAND, and only that.

        An ack already queued cannot be an answer to a command that has not
        been published yet, so it is one of ours that already timed out.
        Returning it would tell somebody a setpoint had moved when this
        attempt was never answered at all.

        Clearing the WHOLE queue here is what used to make the second
        operator time out: their ack was already in it, and we threw it away
        on the way past.
        """
        with self._lock:
            queue = self._acks.setdefault(
                topic, collections.deque(maxlen=self._depth))
            keep = [a for a in queue if not _matches(a, expected)]
            queue.clear()
            queue.extend(keep)

    def claim(self, topic: str, expected: dict) -> dict | None:
        """The first queued ack that answers this command, if one has come."""
        with self._lock:
            queue = self._acks.get(topic)
            if not queue:
                return None
            for ack in list(queue):
                if _matches(ack, expected):
                    queue.remove(ack)
                    return ack
        return None

    def pending(self, topic: str) -> list[dict]:
        with self._lock:
            return list(self._acks.get(topic, ()))


def _matches(ack: dict, expected: dict) -> bool:
    return all(ack.get(k) == v for k, v in expected.items())


class BrokerUnreachable(RuntimeError):
    """The broker did not answer, so nothing was sent."""


class CommandSession:
    """One short-lived connection for sending commands and reading the answers.

    This is the CLI's half of §10. It was written out by hand in four places —
    `hv set`, `hv on`/`off`, `alarms on`/`off` and `flow-reset` — each with
    its own connect-wait, its own settle, its own poll loop and its own
    timeout. The timeouts disagreed with each other (8 s and 15 s) and with
    the web UI (10 s), for no reason anybody had decided.

    Used as a context manager, and it refuses rather than pretending:

        with CommandSession("xams-ctl-hv", ACK_HV_VSET, host=..., port=...) as s:
            ack = s.send(TOPIC_HV_VSET, ACK_HV_VSET, cmd, match=("channel",))

    `send` returns the acknowledgement, or None if the service did not
    answer. None is a FAILURE and callers must read it as one: if the service
    is down the write did not happen, and saying otherwise would leave
    somebody believing a setpoint had moved.
    """

    #: One timeout for every command from anywhere. The old values were 8 s
    #: in flow-reset and 15 s in the HV and notification paths; the web UI
    #: used 10 s. A CAEN write that has not been confirmed in this long is
    #: not going to be.
    DEFAULT_TIMEOUT_S = 15.0

    def __init__(self, client_id: str, *ack_topics: str,
                 host: str = "127.0.0.1", port: int = 1883,
                 connect_timeout_s: float = 5.0, settle_s: float = 0.3,
                 poll_s: float = 0.2, sleep=None, monotonic=None):
        self.bus = Bus(client_id=client_id, host=host, port=port)
        self.host, self.port = host, port
        self.inbox = AckInbox()
        self.connect_timeout_s = connect_timeout_s
        self.settle_s = settle_s
        self.poll_s = poll_s
        # Injected so the CLI's fake clock reaches in here too; a real
        # timeout in a test is a slow test that still proves nothing.
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        for topic in ack_topics:
            self.bus.subscribe(topic, self.inbox.deliver)

    def __enter__(self) -> "CommandSession":
        self.bus.connect()
        deadline = self._monotonic() + self.connect_timeout_s
        while self._monotonic() < deadline and not self.bus.connected:
            self._sleep(0.1)
        if not self.bus.connected:
            self.bus.disconnect()
            raise BrokerUnreachable(
                "the broker at %s:%s did not answer" % (self.host, self.port))
        # Let the subscriptions settle before publishing. Without this the
        # command can go out before the ack subscription is live, and the
        # answer is missed while the write has already happened — the worst
        # of the two outcomes.
        self._sleep(self.settle_s)
        return self

    def __exit__(self, *exc) -> bool:
        self.bus.disconnect()
        return False

    def send(self, topic: str, ack_topic: str, payload: dict,
             timeout_s: float | None = None,
             match: tuple[str, ...] = ()) -> dict | None:
        """Publish one command and wait for the ack that answers it."""
        timeout_s = self.DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        expected = {k: payload[k] for k in match if k in payload}

        self.inbox.open(ack_topic, expected)
        self.bus.publish_raw(topic, json.dumps(payload))

        deadline = self._monotonic() + timeout_s
        while True:
            ack = self.inbox.claim(ack_topic, expected)
            if ack is not None:
                return ack
            if self._monotonic() >= deadline:
                return None
            self._sleep(self.poll_s)
