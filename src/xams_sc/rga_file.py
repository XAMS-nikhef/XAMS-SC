"""RGA recipes and measurement files. See docs/drivers/rga.md, "Data".

No MQTT and no HTTP here: this is shared by the RGA service (which writes the
files), the summary sink and its rebuild tool (which read them), and analysis
in a notebook (`load_rga`).

**One file per Save**, plain JSON, in `data/rga/YYYY/`:

    {
      "format": "xams-rga/1",
      "t_start": ..., "t_saved": ...,         ISO-8601 UTC
      "recipe": {"name": ..., "channels": [...]},
      "device": {...identity, ion source settings...},
      "sc_snapshot": {"p105": 3.1e-07, ...},  slow-control values at Save
      "comment": "...", "tags": [...], "background": "<file name or null>",
      "operator": "...",
      "units": "A",                           scan values are ion currents
      "scans": [{"scannum": 1, "t": ..., "system_status": ..., "values": [...]}],
      "mean": [...]
    }

The values are as the device gives them, not rounded. There is no mass axis:
`masses(recipe)` derives it from the channels, so it cannot disagree with
them.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

import yaml

FORMAT = "xams-rga/1"

# What a recipe may ask for. The device allows more; these are the bounds
# this system has a use for, and a recipe outside them is refused, not
# clipped.
MAX_CHANNELS = 20
MAX_SCANS = 100
DWELL_MS = (1, 16384)          # API reference, "Dwell": 1-16384 ms
PPAMU = (1, 20)


class RecipeError(ValueError):
    """A recipe that cannot be run as written."""


def load_recipes(path: Path | str, mass_range: int = 200) -> dict[str, dict]:
    """Read and validate every recipe in `rga_recipes.yaml`."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    recipes = raw.get("recipes") or {}
    if not isinstance(recipes, dict) or not recipes:
        raise RecipeError(f"{Path(path).name} defines no recipes")
    return {name: validate_recipe(name, r, mass_range) for name, r in recipes.items()}


def validate_recipe(name: str, recipe: dict, mass_range: int = 200) -> dict:
    """Check one recipe and return it normalised. Raises RecipeError."""
    if not re.fullmatch(r"[a-z0-9_]+", str(name)):
        raise RecipeError(f"recipe name {name!r}: lowercase, digits and _ only")
    if not isinstance(recipe, dict):
        raise RecipeError(f"{name}: not a mapping")
    channels = recipe.get("channels") or []
    if not channels or len(channels) > MAX_CHANNELS:
        raise RecipeError(f"{name}: needs 1 to {MAX_CHANNELS} channels")
    scans = int(recipe.get("scans", 5))
    if not 1 <= scans <= MAX_SCANS:
        raise RecipeError(f"{name}: scans must be 1 to {MAX_SCANS}")

    def mass(value, what):
        m = float(value)
        if not 1 <= m <= mass_range:
            raise RecipeError(f"{name}: {what} {m} is outside 1-{mass_range} amu")
        return int(m) if m == int(m) else m

    out = []
    for i, ch in enumerate(channels, start=1):
        mode = str(ch.get("mode", "")).lower()
        dwell = int(ch.get("dwell", 32))
        if not DWELL_MS[0] <= dwell <= DWELL_MS[1]:
            raise RecipeError(f"{name} channel {i}: dwell {dwell} ms outside {DWELL_MS}")
        if mode == "sweep":
            start, stop = mass(ch["start"], "start"), mass(ch["stop"], "stop")
            ppamu = int(ch.get("ppamu", 10))
            if stop <= start:
                raise RecipeError(f"{name} channel {i}: stop must exceed start")
            if not PPAMU[0] <= ppamu <= PPAMU[1]:
                raise RecipeError(f"{name} channel {i}: ppamu {ppamu} outside {PPAMU}")
            out.append({"mode": "sweep", "start": start, "stop": stop,
                        "ppamu": ppamu, "dwell": dwell})
        elif mode == "single":
            out.append({"mode": "single", "mass": mass(ch["mass"], "mass"),
                        "dwell": dwell})
        else:
            raise RecipeError(f"{name} channel {i}: mode must be sweep or single")
    return {"name": name, "description": str(recipe.get("description", "")),
            "scans": scans, "channels": out}


def points(channel: dict) -> int:
    """Points one channel contributes to a scan. 1 + (stop - start) x ppamu
    for a sweep (checked on the device: 1-50 at 4 gives 197)."""
    if channel["mode"] == "sweep":
        return 1 + round((channel["stop"] - channel["start"]) * channel["ppamu"])
    return 1


def masses(recipe: dict) -> list[float]:
    """The mass of every point of a scan, in order."""
    out: list[float] = []
    for ch in recipe["channels"]:
        if ch["mode"] == "sweep":
            n = points(ch)
            out.extend(ch["start"] + k / ch["ppamu"] for k in range(n))
        else:
            out.append(float(ch["mass"]))
    return out


def scan_seconds(recipe: dict) -> float:
    """Roughly how long one scan takes: points x dwell."""
    return sum(points(ch) * ch["dwell"] for ch in recipe["channels"]) / 1000.0


def mean(scans: list[list[float]]) -> list[float]:
    n = len(scans)
    return [sum(col) / n for col in zip(*scans)] if n else []


# ------------------------------------------------------------------ summary

# The window a peak is looked for in, around the nominal mass. Not +-0.5:
# the analyser's mass scale reads about 0.2 amu high above ~60 amu (13 October
# 2026: 69.2, 100.2, 119.2, 131.2), and a symmetric window at 132 took the
# tail of a large peak at 131 (a fluorocarbon, C3F5+) for xenon-132.
PEAK_BELOW = 0.25
PEAK_ABOVE = 0.45


def peak(recipe: dict, values: list[float], at: float) -> float | None:
    """The value at one mass: the largest from PEAK_BELOW under to PEAK_ABOVE
    over it, or None if the recipe does not cover it."""
    found = [v for m, v in zip(masses(recipe), values)
             if -PEAK_BELOW - 1e-9 <= m - at <= PEAK_ABOVE + 1e-9]
    return max(found) if found else None


def summary(record: dict, summary_masses: list[float]) -> dict:
    """What goes to `rga_summary` for one saved file.

    Currents in A, and partial pressures in mbar where the file carries the
    device's sensitivity (A per pressure unit) and its unit. The conversion is
    the device's own linear one; it is a convenience for trends, not a
    calibration.
    """
    recipe, values = record["recipe"], record.get("mean") or []
    device = record.get("device") or {}
    sens = device.get("pp_sensitivity_a_per_unit")
    to_mbar = device.get("unit_to_mbar")
    rows = []
    for m in summary_masses:
        current = peak(recipe, values, m)
        if current is None:
            continue
        pp = (current / sens * to_mbar) if sens and to_mbar else None
        rows.append({"mass": float(m), "current_a": current, "pp_mbar": pp})
    return {"t": record["t_saved"], "file": record["file"],
            "recipe": recipe["name"], "tags": record.get("tags") or [],
            "comment": record.get("comment") or "",
            "total_pressure_mbar": device.get("total_pressure_mbar"),
            "peaks": rows}


# ----------------------------------------------------------------- the file

def file_name(t_start: datetime, recipe: str) -> str:
    return f"{t_start.strftime('%Y-%m-%dT%H%M%S')}_{recipe}.json"


def write(directory: Path | str, record: dict) -> Path:
    """Write one measurement, never over an existing file. Returns the path."""
    t = datetime.fromisoformat(record["t_start"].replace("Z", "+00:00"))
    folder = Path(directory) / f"{t:%Y}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / record["file"]
    # 'x': a second Save of the same acquisition must not silently replace
    # the first, with a different comment.
    with path.open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=1, allow_nan=False)
    return path


def load_rga(path: Path | str) -> dict:
    """Read one measurement file, for analysis.

    Returns the record with two additions: `masses`, the mass axis, and - if
    numpy is installed - `scans_array` and `mean_array` as numpy arrays.
    """
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    if record.get("format") != FORMAT:
        raise ValueError(f"{path}: not an {FORMAT} file")
    record["masses"] = masses(record["recipe"])
    try:
        import numpy as np
        record["scans_array"] = np.array([s["values"] for s in record["scans"]])
        record["mean_array"] = np.array(record["mean"])
    except ImportError:   # pragma: no cover - numpy is optional here
        pass
    return record


def files(directory: Path | str):
    """Every measurement file under `directory`, oldest first. The pending
    acquisition (`pending.json`, top level) is not one of them."""
    return sorted(Path(directory).glob("*/*.json"))


# What a saved file is called; anything else is not served or listed.
FILE_NAME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}_[a-z0-9_]+\.json")


def brief(path: Path | str) -> dict:
    """The few fields the page lists for one saved file."""
    path = Path(path)
    record = json.loads(path.read_text(encoding="utf-8"))
    return {"file": path.name, "t_saved": record.get("t_saved"),
            "recipe": (record.get("recipe") or {}).get("name"),
            "scans": len(record.get("scans") or []),
            "tags": record.get("tags") or [], "comment": record.get("comment") or "",
            "emission": (record.get("device") or {}).get("emission_state"),
            "kb": round(path.stat().st_size / 1024, 1)}


def recent(directory: Path | str, n: int = 10) -> list[dict]:
    """The `n` newest saved files, newest first. Unreadable ones are skipped."""
    out = []
    for path in reversed(files(directory)):
        try:
            out.append(brief(path))
        except (OSError, ValueError):
            continue
        if len(out) >= n:
            break
    return out


def write_pending(path: Path | str, acq: dict) -> None:
    """Keep the unsaved acquisition on disk, so a restart does not lose it.
    Written to a temporary file and moved, so it is never half-written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    keep = {k: v for k, v in acq.items() if k != "mono_start"}
    tmp.write_text(json.dumps(keep), encoding="utf-8")
    tmp.replace(path)


def read_pending(path: Path | str) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def finite(values: list[float]) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
