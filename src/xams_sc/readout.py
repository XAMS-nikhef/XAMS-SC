"""Switchable cDAQ readouts: which modules can be switched, and which
channels are off (26 October 2026).

One place for the rule, because three processes need it - the cdaq service
that closes the task, the alarm engine that withdraws the alarms, and the web
UI and daily report that show the channels as off. A module is switchable
when its devices.yaml entry has a `switch` name (the word used on the command
line, `xams-ctl readout pt1000 off`) and a `label` for the pages.

The state comes from the retained STATUS_READOUT, a dict keyed by module
alias: {"9226": {"enabled": false, "by": "...", "at": "..."}}. A module not
in it is on.
"""

from __future__ import annotations

import json


def switchable_modules(config) -> dict[str, dict]:
    """alias -> {"switch": name, "label": text} for every switchable module."""
    out = {}
    for m in (config.devices.get("cdaq") or {}).get("modules") or []:
        if m.get("switch"):
            out[str(m["alias"])] = {"switch": str(m["switch"]),
                                    "label": str(m.get("label") or m["switch"])}
    return out


def module_for_switch(config, name: str) -> str | None:
    """The module alias behind a switch name (`pt1000` -> `9226`)."""
    for alias, spec in switchable_modules(config).items():
        if spec["switch"] == name or alias == name:
            return alias
    return None


def channels_of(config, alias: str) -> list[str]:
    """The enabled channels read from one module, in channels.yaml order.
    Derived channels share the physical input of their source and are
    included: they are read from the module as much as the source is."""
    return [c.name for c in config.enabled_channels()
            if c.device == "cdaq" and str(c.phys).split("/")[0] == alias]


def parse_status(payload: str) -> dict | None:
    """The retained STATUS_READOUT, or None when it is missing or malformed."""
    if not payload or not payload.strip():
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def off_modules(status: dict | None) -> set[str]:
    return {alias for alias, s in (status or {}).items()
            if not bool(s.get("enabled", True))}


def off_channels(config, status: dict | None) -> set[str]:
    """Every channel whose module readout is switched off."""
    out: set[str] = set()
    for alias in off_modules(status):
        out.update(channels_of(config, alias))
    return out
