# Drift: has the dashboard been saved to git?

A dashboard that exists only in Grafana's own database is lost when that
database is. `tools\save_dashboard.py --save` is what prevents that, and
**nothing runs it for you** — so forgetting it is silent, and the archive falls
behind without a word. It did, which is why this check exists.

The system therefore answers the question itself, on
[`xams-ctl status`](../operating/index.md#every-day) and on the System health page of
[the web interface](../operating/webui.md#system-health):

```
  grafana   dashboards saved to git (1)
```

and when they have diverged:

```
  grafana   NOT SAVED TO GIT — XAMS Overview (differs)
            a dashboard only in Grafana is lost with Grafana:
            .\.venv\Scripts\python.exe tools\save_dashboard.py --save
```

`--save` writes a file and stops there. Until that file is committed the
dashboard is still on one disk, so the export alone does not answer the
question in the title — and the check says so:

```
  grafana   NOT SAVED TO GIT — XAMS Heaters (uncommitted)
            exported but NOT COMMITTED — on this disk only:
            git add grafana\dashboards-archive && git commit
```

---

## What it compares

Every dashboard Grafana holds whose uid or title mentions XAMS, against every
`*.json` in `grafana/dashboards-archive/`. Files are keyed on the **uid inside
the file**, not on the filename, because that is what `save_dashboard.py`
matches on.

…and each of those files against git, by asking
`git status --porcelain` about the archive directory.

Five per-dashboard verdicts:

| | meaning |
|---|---|
| `ok` | live and file agree, and git has that file's contents |
| `differs` | both exist, contents disagree — **run `--save`** |
| `unsaved` | in Grafana, not in the archive — a new dashboard nobody has exported |
| `missing` | in the archive, not in Grafana — deleted in the UI, or a fresh install that needs `--load` |
| `uncommitted` | file matches Grafana but git does not have it — **commit it**; `--save` has nothing left to do |

**Staged counts as uncommitted.** `git add` is not a backup either: until there
is a commit the dashboard exists on this machine and nowhere else, which is the
whole thing §12 is about.

**A working tree git cannot be asked about is not drift.** No checkout, no git
installed, a deployment from a tarball: the file-versus-Grafana comparison still
runs and the git question is skipped, rather than reported as a fault.

**A version bump alone is not drift.** Grafana keeps `id`, `version` and
`iteration` about its stored copy rather than about the dashboard, and they
change on every save. They are dropped before the comparison; the rest is
compared as canonical JSON, so key order does not matter and a genuine one-panel
change does.

---

## The three states, and why `unknown` exists

| state | |
|---|---|
| `ok` | every dashboard matches its file, and git has every one of those files |
| `drift` | at least one differs, exists on only one side, or was exported but never committed |
| `unknown` | Grafana could not be reached, the token is missing, revoked or rejected, or no XAMS dashboard was found anywhere |

**Grafana being stopped, uninstalled or unreachable reports `unknown` — never
`ok`, and never `drift`.** Both wrong answers are worse than no answer: a check
that cries wolf about its own plumbing trains people to ignore it, and one that
reports green because it could not reach anything lets the archive rot while
showing a healthy page. `tests/test_grafana_drift.py` mostly pins down these
*must not say* cases rather than the happy path.

Deleting the `grafana:` section from `secrets.yaml` turns the check off. It then
reports nothing rather than complaining, on the grounds that a check nobody
configured is not a fault.

---

## The token it uses

A Grafana **service account with the Viewer role**, `xams-drift-check`, whose
token lives in `config/secrets.yaml` — which is gitignored. It can look at
dashboards and nothing else: it cannot edit a panel, delete anything or touch a
datasource.

`--save` and `--check` use that same token, so neither needs a password: both
only read from Grafana and write to disk. **`--load` still requires the admin
password**, because it is the one that writes back into Grafana.

To re-issue the token — after rotating the admin password, say:

```powershell
# In Grafana: Administration > Users and access > Service accounts
#   xams-drift-check > Add service account token
# then put it in config/secrets.yaml under:
#   grafana:
#     url: http://127.0.0.1:3000
#     token: <the new token>
```

The web UI asks Grafana at most once every two minutes and reuses the answer in
between, so a page that refreshes every 10 s does not turn into an HTTP request
per view.

---

## Clearing it

Almost always: you edited in the UI and have not saved to git yet.

```powershell
.\.venv\Scripts\python.exe tools\save_dashboard.py --save
git add grafana/dashboards-archive
git commit -m "grafana: <what changed>"
```

The full round trip, including the trap where a failed save looks like a
successful one, is in [Editing dashboards](dashboards.md#getting-an-edit-into-git).

If the state is `missing` on a machine whose Grafana is empty, the direction is
the other way — `--load`, with the admin password. That is a restore, not a
routine action.
