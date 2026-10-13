"""Store the summary of every RGA Save. See docs/drivers/rga.md, "Data".

An ordinary MQTT subscriber, like every other sink (§2.1). The RGA service
writes the measurement FILE and publishes its summary on `xams/rga/saved`;
this puts the summary in `rga_saves` and `rga_summary`, for Grafana.

The file is the truth and these tables an index over it, in the same
relationship as `meas` to the JSONL archive. A record missed while the
database was down is rebuilt from the files (`tools/rga_rebuild_summary.py`),
and the primary keys make a second insert of the same Save do nothing.
"""

from __future__ import annotations

import json
import logging

from ..bus import TOPIC_RGA_SAVED, Bus
from ..model import parse_iso

log = logging.getLogger(__name__)

INSERT_SAVE = """
INSERT INTO rga_saves (t, file, recipe, tags, comment, total_pressure_mbar, detector)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (file) DO UPDATE SET detector = EXCLUDED.detector
"""
INSERT_PEAK = """
INSERT INTO rga_summary (t, file, mass, current_a, pp_mbar)
VALUES (%s, %s, %s, %s, %s) ON CONFLICT (file, mass) DO NOTHING
"""


def rows(summary: dict):
    """(save row, [peak rows]) for one summary record."""
    t = parse_iso(summary["t"])
    save = (t, summary["file"], summary["recipe"],
            ",".join(summary.get("tags") or []), summary.get("comment") or "",
            summary.get("total_pressure_mbar"), summary.get("detector") or "Faraday")
    peaks = [(t, summary["file"], p["mass"], p["current_a"], p.get("pp_mbar"))
             for p in summary.get("peaks") or []]
    return save, peaks


def store(conn, summary: dict) -> None:
    save, peaks = rows(summary)
    with conn.cursor() as cur:
        cur.execute(INSERT_SAVE, save)
        for p in peaks:
            cur.execute(INSERT_PEAK, p)


class RgaSummaryWriter:
    def __init__(self, bus: Bus, dsn: str):
        self.bus = bus
        self.dsn = dsn
        self._conn = None
        self._written = 0

    def _connect(self):
        import psycopg

        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
        return self._conn

    def handle(self, payload: str) -> None:
        try:
            summary = json.loads(payload)
            rows(summary)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("unusable RGA summary (%s): %r", exc, payload[:120])
            return
        try:
            store(self._connect(), summary)
            self._written += 1
        except Exception as exc:
            # Not fatal: the file exists, and the rebuild tool restores this.
            log.error("could not store the RGA summary of %s (%s); rebuild it "
                      "with tools/rga_rebuild_summary.py", summary.get("file"), exc)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    def start(self) -> None:
        self.bus.subscribe(TOPIC_RGA_SAVED,
                           lambda topic, payload: self.handle(payload))

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass

    @property
    def written(self) -> int:
        return self._written
