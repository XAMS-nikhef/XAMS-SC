"""Rebuild the RGA summary tables from the measurement files.

    python tools/rga_rebuild_summary.py            # all of data/rga/
    python tools/rga_rebuild_summary.py --dry-run  # say what would be stored
    python tools/rga_rebuild_summary.py --replace  # recompute rows already there

The files in `data/rga/` are the truth; `rga_saves` and `rga_summary` are an
index over them (docs/drivers/rga.md, "Data"). This puts back what the
database lost - or never got, because it was down at the moment of a Save -
and it is safe to run at any time: the primary keys make a row that is
already there a no-op.

The summary masses are the ones in `devices.yaml` `rga.summary_masses` NOW,
which is also how to add a mass to the history after the fact: add it there
and run this. When the way a peak is read changes (rga_file.peak), rows
already stored keep their old values unless --replace: then each file's
summary rows are deleted and written again, from the file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xams_sc import rga_file  # noqa: E402
from xams_sc.config import load  # noqa: E402
from xams_sc.devices.rga import SUMMARY_MASSES  # noqa: E402
from xams_sc.sinks.__main__ import dsn_from_secrets  # noqa: E402
from xams_sc.sinks.rga_writer import store  # noqa: E402


def main(argv=None) -> int:
    spec = load().devices.get("rga", {}) or {}
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", default=str(ROOT / spec.get("data_dir", "data/rga")))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--replace", action="store_true",
                   help="delete and rewrite the summary rows of every file")
    args = p.parse_args(argv)

    masses = list(spec.get("summary_masses", SUMMARY_MASSES))
    found = rga_file.files(args.data_dir)
    print(f"{len(found)} RGA files in {args.data_dir}; summary masses {masses}")
    if not found:
        return 0

    conn = None
    if not args.dry_run:
        import psycopg
        dsn = dsn_from_secrets()
        if not dsn:
            print("no PostgreSQL DSN in config/secrets.yaml", file=sys.stderr)
            return 2
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)

    bad = 0
    for path in found:
        try:
            record = rga_file.load_rga(path)
            summary = rga_file.summary(record, masses)
        except (ValueError, KeyError, TypeError) as exc:
            print(f"  SKIP {path.name}: {exc}")
            bad += 1
            continue
        print(f"  {path.name}: {len(summary['peaks'])} masses")
        if conn is not None:
            if args.replace:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM rga_summary WHERE file = %s", (summary["file"],))
            store(conn, summary)
    if conn is not None:
        conn.close()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
