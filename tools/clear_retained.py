"""Clear stale retained MQTT topics.

    python tools/clear_retained.py                 # report only
    python tools/clear_retained.py --apply         # actually clear them

WHY THIS IS NEEDED, AND WHY IT BITES.

Measurements and status are published retained (§3), so a newly started
subscriber immediately sees the current state instead of waiting a cycle. The
cost is that **a retained message outlives the process that published it, and
is re-delivered to every new subscriber**.

Two consequences, both seen on 17 September 2026:

  * A service that has been retired — `sim`, once the real drivers exist —
    keeps a heartbeat on the broker forever, and the staleness monitor will
    eventually alarm on something that was stopped deliberately.

  * Worse: every time the sinks reconnect, they re-ingest the retained value of
    every channel that was ever published, including channels no running
    service produces any more. Deleting those rows from the database does not
    help, because the next reconnect writes them again. The broker has to be
    cleared, or they come back.

A topic is deleted by publishing an empty payload to it with the retain flag
set. That is the only way; there is no "delete" in MQTT.

A retained measurement is treated as stale when any of these hold:

  * its payload says `"src":"sim"` — synthetic data must never persist as
    though it were a measurement;
  * no channel of that name exists in channels.yaml at all;
  * it is older than --max-age (default 1 hour). Nothing is publishing it, so
    whatever the status page or the mimic would show from it is not current.

Note that "enabled in channels.yaml" is NOT the test. The hv_*, ls_* and ups_*
channels are configured and legitimate but have no service until milestones 5
and 6, so a retained value for them is stale even though the channel is real.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xams_sc.config import load  # noqa: E402
from xams_sc.model import parse_iso, utcnow  # noqa: E402

KNOWN_SERVICES = {"cdaq", "caen", "lakeshore", "ups", "turbo", "scale", "sinks", "derived", "alarms"}


def collect(host: str, port: int, seconds: float) -> dict[str, str]:
    """Subscribe to everything and record what the broker replays."""
    found: dict[str, str] = {}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="xams-clear-retained")
    client.on_connect = lambda c, u, f, rc, p=None: c.subscribe("xams/#")
    client.on_message = lambda c, u, m: found.__setitem__(
        m.topic, m.payload.decode("utf-8", errors="replace"))
    client.connect(host, port, keepalive=15)
    client.loop_start()
    time.sleep(seconds)
    client.loop_stop()
    client.disconnect()
    return found


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--broker", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--wait", type=float, default=4.0,
                   help="seconds to listen for retained messages")
    p.add_argument("--apply", action="store_true",
                   help="clear the stale topics; without this, report only")
    p.add_argument("--max-age", type=float, default=600.0,
                   help="a retained measurement older than this many seconds "
                        "is stale (default 600 = 60x the 10 s log interval)")
    args = p.parse_args(argv)

    config = load()
    known = set(config.channels)

    print(f"listening to {args.broker}:{args.port} for {args.wait:.0f}s ...")
    found = collect(args.broker, args.port, args.wait)
    print(f"{len(found)} retained topics\n")

    now = utcnow()
    stale: list[tuple[str, str]] = []
    for topic in sorted(found):
        parts = topic.split("/")
        payload = found[topic]

        if len(parts) == 3 and parts[1] == "meas":
            channel = parts[2]
            if channel not in known:
                stale.append((topic, f"no channel named {channel!r} in channels.yaml"))
                continue
            try:
                d = json.loads(payload)
            except ValueError:
                stale.append((topic, "unparseable payload"))
                continue
            if d.get("src") == "sim":
                stale.append((topic, "SYNTHETIC (src=sim) — must not persist"))
                continue
            try:
                age = (now - parse_iso(d["t"])).total_seconds()
            except Exception:
                stale.append((topic, "unparseable timestamp"))
                continue
            if age > args.max_age:
                stale.append((topic, f"last published {age/3600:.1f} h ago — "
                                     f"nothing is producing it"))

        elif len(parts) == 3 and parts[1] == "alarm":
            # Alarm state is retained too, and a retained alarm for a channel
            # that no longer exists can never clear: nothing will ever publish
            # an "ok" for it. It then sits in the active-alarms table forever,
            # which trains people to ignore that table.
            channel = parts[2]
            if channel not in known:
                stale.append((topic, f"alarm for {channel!r}, which is not in "
                                     f"channels.yaml"))

        elif len(parts) == 4 and parts[1] == "status":
            if parts[2] not in KNOWN_SERVICES:
                stale.append((topic, f"unknown service {parts[2]!r}"))

    if not stale:
        print("  nothing stale — every retained topic matches a live channel "
              "or a known service")
        return 0

    for topic, why in stale:
        print(f"  {topic:45s} {why}")
    print(f"\n{len(stale)} stale topic(s)")

    if not args.apply:
        print("\nReport only. Re-run with --apply to clear them.")
        return 0

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="xams-clear-retained-apply")
    client.connect(args.broker, args.port, keepalive=15)
    client.loop_start()
    for topic, _ in stale:
        # Empty payload + retain = delete. This is the only way to remove one.
        client.publish(topic, payload=None, qos=1, retain=True)
    time.sleep(1.5)
    client.loop_stop()
    client.disconnect()
    print(f"\ncleared {len(stale)} retained topic(s)")
    print("Any subscriber that reconnects will no longer receive them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
