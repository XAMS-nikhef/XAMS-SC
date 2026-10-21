# Documents

| File | Status | Notes |
|---|---|---|
| `../README.md` | **source** | Short front page. What the system is, and where the documentation is. |
| `index.md`, `install.md`, `status.md` | **source** | What it is, reinstalling from scratch, and where the work has got to. Split out of the old root `README.md`. |
| `operating/` | **source** | How to run it and what to do when it breaks. Was `OPERATIONS.md` at the repository root. Grows with each milestone. |
| `software/`, `drivers/`, `grafana/` | **source** | How it works, per subsystem. Largely stubs — see the warnings on each page. |
| `reference/` | mixed | `hardware.md` is written; the channel and alarm tables are generated from `config/*.yaml`; `api.md` is generated from the docstrings. |
| `OPTIONS.md` | **source** | The decision document: why this approach, and what was decided. |
| `DESIGN.md` | **source** | Software design specification. The document to hand to whoever writes the code. |

**`docs/` is the manual and nothing else.** The PDF toolchain, the generated
PDFs, the P&ID and the LabVIEW export used to live here too; they are working
material rather than documentation, and are now in
[`../notes/`](https://github.com/XAMS-nikhef/XAMS-SC/tree/main/notes) with a README
of their own. `EPICS.md` went with them: it specifies in full a system that was
never built, and the [decision document](OPTIONS.md) records the outcome. The
two documents above stay here because they answer *why is it built this way*,
which is a question the manual should answer.

## The manual

Everything here is also built into a searchable site and **served by the web UI
itself** at <http://127.0.0.1:8000/manual>, so it is present on the lab PC
whether or not the building network is. The link is in the navigation bar next
to Grafana.

```bash
python -m pip install -e ".[docs]"
python -m mkdocs serve -a 127.0.0.1:8001   # write and preview, live reload
python tools/build_docs.py                 # build into src/xams_sc/api/site
```

**Port 8001, not 8000** — 8000 is the web UI. `install_services.ps1` runs the
build, so a fresh install has a manual without anyone remembering to make one.

The build is `--strict`: a link to a page that does not exist fails it rather
than shipping. MkDocs resolves links against its own case-sensitive index, so
this catches a `DESIGN.md`/`design.md` mismatch even on macOS and Windows,
where the filesystem itself would not.

Two pages are **generated at every build** and must not be edited: the channel
table from `config/channels.yaml` and the alarm table from `config/alarms.yaml`.
A hand-written channel table is a table that is wrong within a month — the same
argument as `tests/test_grafana_drift.py`, applied to prose.

**Everything else is an ordinary file under `docs/`.** Nothing is duplicated and
nothing is rewritten on the way in: a page is where its link says it is, so
these files read correctly browsed on GitHub as well as in the built site. The
root `README.md` is a short front page that points here, not a second copy of
it.

The one exception is the two generated tables. They have no file on disk, so a
link to them resolves in the built manual and **404s when the source is browsed
on GitHub** — the price of their being unable to go stale.

## The PDFs

`DESIGN.md` and `OPTIONS.md` are also published as PDFs, for people who are not
going to clone a repository, as is `EPICS.md` from `notes/`. The toolchain and
its output are in `../notes/` — see `notes/README.md`. macOS only.

Its two Markdown extensions, `:::warn` callouts and `{+}` cell markers, mean
nothing to MkDocs, so `tools/mkdocs_hooks.py` translates them on the way into
the site. Without it the build **fails**: `:::` is also mkdocstrings' autodoc
marker.

## Which document gets what

| You learned | Goes in |
|---|---|
| A hardware fact, a resolved TBD, an architectural decision | `DESIGN.md`, and its §16 table |
| How to do something, or what to do when it breaks | `operating/` |
| How to reinstall from nothing | `install.md` |
| Why a particular piece of code or config is the way it is | a docstring or a YAML comment |

Rough test: if it stops being true when the code changes, it belongs in a
docstring. If it is true regardless of implementation, it belongs in
`DESIGN.md`. If someone needs it at two in the morning, it belongs in
`operating/`.

Documentation is written **during** construction, one increment per milestone
— not afterwards (`DESIGN.md` §14).
