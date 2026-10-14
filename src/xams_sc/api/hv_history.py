"""Seed the Control page's HV plots from the JSONL archive at start.

The web UI keeps the last few hours of HV readings in memory (state.py,
`HISTORY_S`), filled from the bus. Without a seed the plots would start empty
on every restart of the web UI - which is exactly when somebody is looking -
so at start the same window is read back from the JSONL archive, the raw truth
on this PC (§9.3). A file, not the database: the Control page must keep
working when PostgreSQL does not (§8.1).

Read once, in a background thread, and only the lines of the HV channels; a
day's file is a few hundred thousand lines and most of them are skipped on a
substring test before any JSON is parsed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..model import parse_iso
from .state import HISTORY_S, PLOTTED_CHANNELS, keeps_history

log = logging.getLogger(__name__)

# What a line of a plotted channel contains: `"ch":"hv_...` for the HV, and
# the channel name in quotes for the others (state.PLOTTED_CHANNELS).
MARKS = ('"hv_',) + tuple(f'"{ch}"' for ch in sorted(PLOTTED_CHANNELS))


def read_archive(directory: Path, since_epoch: float) -> dict[str, list[tuple[float, float]]]:
    """HV readings newer than `since_epoch` from the daily JSONL files."""
    points: dict[str, list[tuple[float, float]]] = {}
    start = datetime.fromtimestamp(since_epoch, timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    day = start
    while day <= today:
        path = Path(directory) / f"{day.isoformat()}.jsonl"
        day += timedelta(days=1)
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    # A cheap substring test before any JSON is parsed: the
                    # HV channels and the few others the page plots.
                    if not any(mark in line for mark in MARKS):
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue          # a half-written last line
                    ch = d.get("ch", "")
                    v = d.get("v")
                    if not keeps_history(ch) or v is None or d.get("q", "ok") != "ok":
                        continue
                    t = parse_iso(d["t"]).timestamp()
                    if t >= since_epoch:
                        points.setdefault(ch, []).append((t, float(v)))
        except OSError as exc:
            log.warning("HV history: could not read %s: %s", path, exc)
    return points


def seed_in_background(state, directory: Path) -> threading.Thread:
    """Read the archive without holding up the start of the web UI."""
    def run():
        t0 = time.monotonic()
        try:
            points = read_archive(directory, time.time() - HISTORY_S)
            n = state.seed_history(points)
            log.info("HV history: %d readings of %d channels seeded from %s in %.1f s",
                     n, len(points), directory, time.monotonic() - t0)
        except Exception:                     # never take the web UI down for a plot
            log.exception("HV history: seeding from %s failed", directory)

    thread = threading.Thread(target=run, name="hv-history-seed", daemon=True)
    thread.start()
    return thread
