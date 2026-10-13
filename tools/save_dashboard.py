r"""Pull dashboards out of Grafana and into git. See DESIGN.md §12.

    .venv\Scripts\python.exe tools/save_dashboard.py --save    # Grafana -> git
    .venv\Scripts\python.exe tools/save_dashboard.py --check   # report drift only
    .venv\Scripts\python.exe tools/save_dashboard.py xams-overview   # just one

**Run it with the venv Python, not by double-clicking or `.\tools\...py`.**
Windows maps .py to the py launcher, which runs a different interpreter and
sends this script's errors to stderr where PowerShell's file-association path
discards them - so a failed run looks exactly like a successful one.

`--save` and `--check` need NO password: they only read, and they use the
read-only Grafana token in config/secrets.yaml. `--load` writes and still
requires `--password` or GRAFANA_PASSWORD.

**Run this after editing a dashboard in the Grafana UI.** Grafana keeps
dashboards in its own internal database; without them in git they are lost when
that database is lost or Grafana is reinstalled, taking hours of work with
them. That is the whole reason §12 asks for provisioning.

WHICH DIRECTION WINS, AND WHEN.

`allowUiUpdates: true` in the provisioning config means the UI can save, and
Grafana then treats its stored copy as newer than the file. Two consequences,
and the second one cost twenty minutes on 17 September 2026:

  * Grafana -> file: this tool, with `--save`. Run it after a UI edit, then
    commit `grafana/dashboards-archive/`.
  * file -> Grafana: `--load`. Editing the archived JSON by hand does NOT
    reach Grafana on its own.

Edit in ONE place. Both copies changed at once on 17 September 2026 - a panel
setting into the file, a layout in the UI - and `--load` would have reverted
the layout without a word.

`--check` reports whether the live dashboard and the file have diverged, which
is the question worth asking before a `git pull` or a reinstall.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# NOT the provisioned directory.
#
# Grafana refuses every UI save of a provisioned dashboard, and the setting
# that would allow it (allowUiUpdates) is only read at service start — which
# needs admin on this machine and could not be done. Fighting that cost an
# afternoon, so dashboards are no longer provisioned from a file at all.
#
# Instead: Grafana owns the live dashboard and the UI can edit it freely, and
# this directory is the ARCHIVE that git tracks. `--save` (the default) copies
# Grafana into it, `--load` puts it back into a fresh Grafana. The §12
# requirement is that
# a dashboard survives losing Grafana's database, and an archive plus a load
# command satisfies that just as well as provisioning did.
DASHBOARD_DIR = ROOT / "grafana" / "dashboards-archive"

# Fields Grafana adds that describe the stored copy rather than the dashboard.
# Keeping them would make every export differ from the last for no real reason.
VOLATILE = ("id", "version", "iteration")


def read_token() -> str | None:
    """The read-only Grafana token from config/secrets.yaml, if there is one.

    `--save` and `--check` only READ, so the Viewer token the drift check
    already uses is enough for both. `--load` writes and still wants the
    admin password — that asymmetry is the point, not an oversight.
    """
    try:
        import yaml
    except ImportError:
        return None
    path = ROOT / "config" / "secrets.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return ((data.get("grafana") or {}).get("token")) or None


def api(base: str, auth: str, path: str):
    """`auth` is a complete Authorization header value, scheme included."""
    req = urllib.request.Request(base + path, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def path_for(uid: str) -> Path:
    """Where this dashboard's file already lives, or where it should go.

    Matches on the uid INSIDE each file rather than on the filename, so a
    dashboard keeps its existing file whatever that file is called. Naming the
    file after the uid unconditionally would quietly create a second copy
    beside the first, and provisioning would then load both.
    """
    for candidate in sorted(DASHBOARD_DIR.glob("*.json")):
        try:
            if json.loads(candidate.read_text(encoding="utf-8")).get("uid") == uid:
                return candidate
        except Exception:
            continue
    return DASHBOARD_DIR / f"{uid}.json"


def load_into_grafana(args) -> int:
    """Restore the archived dashboards into Grafana.

    This is what makes the install reproducible (§12): after a reinstall,
    Grafana has no dashboards and this puts them all back.
    """
    if not args.password:
        print("--load WRITES to Grafana, so it needs the admin password: "
              "pass --password, or set GRAFANA_PASSWORD. The read-only token "
              "in secrets.yaml deliberately cannot do this.", file=sys.stderr)
        return 2
    auth = "Basic " + base64.b64encode(
        f"{args.user}:{args.password}".encode()).decode()

    files = sorted(DASHBOARD_DIR.glob("*.json"))
    if not files:
        print(f"No dashboards in {DASHBOARD_DIR.relative_to(ROOT)}.")
        return 1

    for path in files:
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        dashboard.pop("id", None)
        dashboard["version"] = 0
        payload = json.dumps({"dashboard": dashboard, "overwrite": True,
                              "message": f"loaded from {path.name}"}).encode()
        req = urllib.request.Request(args.url + "/api/dashboards/db", data=payload,
            headers={"Authorization": auth,
                     "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status = json.loads(r.read()).get("status")
            print(f"  {path.name:24s} loaded ({status})")
        except urllib.error.HTTPError as exc:
            print(f"  {path.name:24s} FAILED {exc.code}: "
                  f"{exc.read()[:120].decode(errors='replace')}")
            return 1
    return 0


def normalise(dashboard: dict) -> dict:
    clean = {k: v for k, v in dashboard.items() if k not in VOLATILE}
    clean["id"] = None
    return clean


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("uid", nargs="*", help="dashboard uid(s); default is all XAMS ones")
    p.add_argument("--url", default="http://127.0.0.1:3000")
    p.add_argument("--user", default=os.environ.get("GRAFANA_USER", "admin"))
    p.add_argument("--password", default=os.environ.get("GRAFANA_PASSWORD"))
    # --save is the default and needs no flag, but it exists so that all three
    # directions can be written out explicitly. A command whose most common
    # action is the one you cannot name is a command people get wrong.
    p.add_argument("--save", action="store_true",
                   help="pull Grafana INTO the files (the default)")
    p.add_argument("--check", action="store_true",
                   help="report drift between Grafana and the files; write nothing")
    p.add_argument("--load", action="store_true",
                   help="push the archived files INTO Grafana (use on a fresh install)")
    args = p.parse_args(argv)

    if args.load and (args.save or args.check):
        print("--load goes the other way from --save/--check; pick one.",
              file=sys.stderr)
        return 2

    if args.load:
        return load_into_grafana(args)

    # Read-only work, so the Viewer token in secrets.yaml is enough and is
    # preferred. Asking for the admin password to copy a dashboard INTO git
    # is friction with nothing behind it, and friction is why this step gets
    # skipped and the archive falls behind.
    token = None if args.password else read_token()
    if token:
        auth = "Bearer " + token
    elif args.password:
        auth = "Basic " + base64.b64encode(
            f"{args.user}:{args.password}".encode()).decode()
    else:
        print("Grafana credentials needed. Either:", file=sys.stderr)
        print("  * put the read-only token in config/secrets.yaml under "
              "grafana.token (see OPERATIONS.md), or", file=sys.stderr)
        print("  * pass --password, or set GRAFANA_PASSWORD.",
              file=sys.stderr)
        return 2

    try:
        if args.uid:
            uids = args.uid
        else:
            found = api(args.url, auth, "/api/search?type=dash-db&query=")
            uids = [d["uid"] for d in found
                    if "xams" in (d.get("uid", "") + d.get("title", "")).lower()]
    except urllib.error.HTTPError as exc:
        print(f"Grafana refused the request: {exc.code} {exc.reason}", file=sys.stderr)
        if exc.code == 401:
            print("Check the password.", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"Cannot reach Grafana at {args.url}: {exc.reason}", file=sys.stderr)
        return 2

    if not uids:
        print("No XAMS dashboards found in Grafana.")
        return 1

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    drifted, written = [], []

    # Everything the archive holds, so the comparison can run in BOTH
    # directions. The loop below is driven by what Grafana returns, which
    # cannot see a dashboard that exists only as a file - exactly the state a
    # half-finished `--load` leaves behind, and the one where `--check` used
    # to print "everything matches" while the UI linking to that dashboard
    # showed an empty panel. src/xams_sc/grafana.py's watcher has always
    # compared `uids | set(archive)`; this is the same check in this tool.
    #
    # Skipped when --uid names dashboards explicitly: the caller asked about
    # those, and nothing else is theirs to answer for.
    archived = {}
    if not args.uid:
        for candidate in sorted(DASHBOARD_DIR.glob("*.json")):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("uid"):
                archived[data["uid"]] = (candidate, data.get("title", ""))
    absent = [uid for uid in sorted(archived) if uid not in set(uids)]

    for uid in uids:
        payload = api(args.url, auth, f"/api/dashboards/uid/{uid}")
        dashboard = normalise(payload["dashboard"])
        title = dashboard.get("title", uid)

        path = path_for(uid)
        new = json.dumps(dashboard, indent=2, ensure_ascii=False)

        # Compared as CONTENT, keys sorted - the way src/xams_sc/grafana.py's
        # drift watcher compares. Grafana returns keys in its own order, and a
        # file edited by hand (or by a script) has the same content in another
        # order; comparing the text reported that as "DIFFERS" right after a
        # --load had made the two identical (13 October 2026).
        same = False
        if path.exists():
            try:
                same = (json.dumps(normalise(json.loads(path.read_text(encoding="utf-8"))),
                                   sort_keys=True)
                        == json.dumps(dashboard, sort_keys=True))
            except Exception:
                same = False

        if same:
            print(f"  {uid:20s} unchanged  ({title})")
            continue

        drifted.append(uid)
        if args.check:
            print(f"  {uid:20s} DIFFERS from {path.relative_to(ROOT)}  ({title})")
            continue

        path.write_text(new, encoding="utf-8")
        written.append(path)
        print(f"  {uid:20s} saved to {path.relative_to(ROOT)}  ({title})")

    for uid in absent:
        path, title = archived[uid]
        print(f"  {uid:20s} NOT IN GRAFANA, only in "
              f"{path.relative_to(ROOT)}  ({title})")

    if args.check:
        if drifted or absent:
            if drifted:
                print(f"\n{len(drifted)} dashboard(s) differ from the files in git.")
                print("Run without --check to bring the files up to date.")
            if absent:
                print(f"\n{len(absent)} dashboard(s) exist only as a file. "
                      "Anything linking to one shows an empty panel.")
                print("  python tools/save_dashboard.py --load --password <pw>")
            return 1
        print("\nEverything in Grafana matches the files in git.")
        return 0

    if absent:
        # Not an error on a --save: saving is how a dashboard REACHES the
        # archive, and a file Grafana has never seen is a normal state
        # mid-edit. It still has to be said out loud, because the whole point
        # of the archive is that it can be put back.
        print(f"\n{len(absent)} dashboard(s) above exist only as a file. "
              "To put them into Grafana:")
        print("  python tools/save_dashboard.py --load --password <pw>")

    if written:
        print(f"\n{len(written)} file(s) updated. Commit them:")
        # The ARCHIVE directory, which is where this tool actually writes.
        # It printed grafana/dashboards for a while, which stages nothing and
        # leaves the save looking done when it is not committed.
        print("  git add grafana/dashboards-archive && "
              "git commit -m 'grafana: update dashboards'")
        print("\nA dashboard that exists only in Grafana's own database is lost")
        print("when that database is.")
    else:
        print("\nNothing to do — the files already match Grafana.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
