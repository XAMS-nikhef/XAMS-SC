"""Load and validate the YAML configuration. See DESIGN.md §4.

Configuration is data, not code (principle 2). Adding a sensor touches no
Python — it is an entry in channels.yaml.

Validation is strict and fails at startup rather than at the first bad read.
A typo in a channel name is cheap to find here and expensive to find in six
months of history.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

# Repository root, found relative to this file: src/xams_sc/config.py -> ../../
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("XAMS_CONFIG_DIR", ROOT / "config"))
# Logs, pid files and lock files, anchored to the repository rather than to
# whatever directory a service happened to be started from. A relative "logs"
# put them wherever the shell was standing: services started from one place
# and a web UI started from another disagreed about where the logs were, and
# the Logs tab answered by showing nothing at all.
LOG_DIR = Path(os.environ.get("XAMS_LOG_DIR", ROOT / "logs"))

VALID_KINDS = {
    "voltage", "current", "rtd", "hv_vmon", "hv_imon", "hv_stat", "hv_vset",
    "temperature", "status", "power", "setpoint", "weight",
}


class ConfigError(Exception):
    """Raised for any problem that should stop a service from starting."""


@dataclass(frozen=True)
class Channel:
    name: str
    device: str
    phys: str
    kind: str
    unit: str
    offset: float = 0.0
    multiplier: float = 1.0
    sign: int = 1
    rtd: dict | None = None
    limits: dict | None = None
    derive: dict | None = None
    """Compute this channel from another, via a named transform.

    {from: <channel>, transform: <name in scaling.TRANSFORMS>, **parameters}

    Used where the relationship is not linear and so cannot be expressed as
    an offset and a multiplier — the Lake Shore heater wattage, which goes as
    the square of the percentage.
    """
    default_setpoint: float | None = None
    """The setpoint "load defaults" offers for this channel (section 10a).

    Configuration, not state: it is what the channel is normally run at, kept
    in git and reviewable, so that bringing the detector up does not depend on
    somebody remembering a number or on whatever was last left in the board.

    Offered, never applied. Loading a default fills a box on a page; a person
    still reads it, may change it, and presses the button. Nothing here writes
    to an instrument, and nothing applies a default on startup.
    """
    legacy: str | None = None
    enabled: bool = True
    description: str = ""
    log_minmax: bool = False
    on_pid: bool = True
    """Whether this channel appears on the P&ID (§8.2).

    Default true for anything that measures a point in the plant. Set false
    for a reading that legitimately has no place on a piping drawing — the
    ambient temperature of the room, for instance. Without this the mimic
    drift check reports it as missing forever, and a warning that is always
    on is one nobody reads.
    """

    def in_limits(self, value: float) -> bool:
        """Software write range (§8.1). Convenience, NOT protection.

        The instrument's own MAXV applies underneath regardless and always
        wins. A channel with no `limits` accepts nothing: a write range that
        was never specified is not permission to write anything.
        """
        if not self.limits:
            return False
        return self.limits["min"] <= value <= self.limits["max"]


@dataclass
class Config:
    channels: dict[str, Channel]
    devices: dict[str, Any]
    alarms: dict[str, Any]
    recipients: dict[str, Any] = field(default_factory=dict)
    config_hash: str = ""

    def enabled_channels(self) -> list[Channel]:
        return [c for c in self.channels.values() if c.enabled]

    def channels_for(self, device: str) -> list[Channel]:
        return [c for c in self.enabled_channels() if c.device == device]

    def stale_after_seconds(self) -> float:
        return float(self.alarms.get("defaults", {}).get("stale_after_seconds", 60))


def _read(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing configuration file: {path}")
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        # A malformed file is a clean failure with the line number, not a
        # traceback. The same principle as a busy device in §6.1: the reader
        # needs to know what to fix, not where the parser gave up.
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}, column {mark.column + 1}"
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise ConfigError(f"{path.name} is not valid YAML{where}: {problem}") from exc
    if data is not None and not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping, got {type(data).__name__}")
    return data if data is not None else {}


def _normalise(obj: Any) -> Any:
    """Canonical form for hashing: sorted keys, no formatting, no comments.

    Reformatting a YAML file or moving a channel must not change the hash;
    changing a value must.
    """
    if isinstance(obj, dict):
        return {k: _normalise(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, list):
        return [_normalise(v) for v in obj]
    return obj


def compute_hash(channels: dict, devices: dict, alarms: dict) -> str:
    """SHA-256 over the parsed, normalised config; first 7 hex characters (§4.5).

    recipients.yaml is excluded: it changes often and does not affect the data.
    """
    blob = json.dumps(
        _normalise({"channels": channels, "devices": devices, "alarms": alarms}),
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:7]


def _parse_channels(raw: dict, devices: dict) -> dict[str, Channel]:
    entries = raw.get("channels") or []
    if not entries:
        raise ConfigError("channels.yaml defines no channels")

    known_devices = set(devices) | {d["id"] for d in devices.get("caen", []) if "id" in d}
    known_devices.discard("caen")
    known_devices.add("derived")

    channels: dict[str, Channel] = {}
    seen_phys: dict[tuple[str, str], str] = {}

    for e in entries:
        if not isinstance(e, dict):
            raise ConfigError(f"channel entry is not a mapping: {e!r}")
        for key in ("name", "device", "phys", "kind", "unit"):
            if key not in e:
                raise ConfigError(f"channel {e.get('name', e)!r}: missing required key {key!r}")

        name = str(e["name"])
        if name in channels:
            raise ConfigError(f"duplicate channel name: {name!r}")
        if name != name.lower() or " " in name:
            raise ConfigError(f"channel {name!r}: names are lowercase with no spaces (§3)")

        kind = str(e["kind"])
        if kind not in VALID_KINDS:
            raise ConfigError(
                f"channel {name!r}: unknown kind {kind!r}; expected one of {sorted(VALID_KINDS)}"
            )

        device = str(e["device"])
        if device not in known_devices:
            raise ConfigError(
                f"channel {name!r}: unknown device {device!r}; known: {sorted(known_devices)}"
            )

        if kind == "rtd" and not e.get("rtd"):
            raise ConfigError(f"channel {name!r}: kind 'rtd' requires an `rtd:` block")

        sign = int(e.get("sign", 1))
        if sign not in (-1, 1):
            raise ConfigError(f"channel {name!r}: sign must be -1 or 1, got {sign!r}")

        derive = e.get("derive")
        if derive is not None:
            from .scaling import TRANSFORMS
            for key in ("from", "transform"):
                if key not in derive:
                    raise ConfigError(
                        f"channel {name!r}: derive block needs {key!r}")
            if derive["transform"] not in TRANSFORMS:
                raise ConfigError(
                    f"channel {name!r}: unknown transform "
                    f"{derive['transform']!r}; known: {sorted(TRANSFORMS)}")

        default = e.get("default_setpoint")
        if default is not None:
            limits = e.get("limits")
            if not limits:
                raise ConfigError(
                    f"channel {name!r}: default_setpoint needs `limits` too, "
                    f"or the default could never be applied")
            if not (limits["min"] <= float(default) <= limits["max"]):
                raise ConfigError(
                    f"channel {name!r}: default_setpoint {default} is outside "
                    f"its own limits {limits['min']}..{limits['max']}")

        limits = e.get("limits")
        if limits is not None:
            if not {"min", "max"} <= set(limits):
                raise ConfigError(f"channel {name!r}: limits need both `min` and `max`")
            if limits["min"] > limits["max"]:
                raise ConfigError(f"channel {name!r}: limits min exceeds max")

        # Two channels on one physical input is legitimate for HV (vmon and
        # imon share an index) but a mistake anywhere else.
        # A derived channel shares the physical input it is computed from,
        # exactly as hv_vmon and hv_imon share a channel index.
        phys_key = (device, str(e["phys"]))
        shares_legitimately = (kind.startswith("hv_") or derive is not None
                               or kind == "setpoint")
        if phys_key in seen_phys and not shares_legitimately:
            raise ConfigError(
                f"channel {name!r}: physical input {phys_key[1]!r} on {device!r} "
                f"is already used by {seen_phys[phys_key]!r}"
            )
        seen_phys.setdefault(phys_key, name)

        channels[name] = Channel(
            name=name,
            device=device,
            phys=str(e["phys"]),
            kind=kind,
            unit=str(e["unit"]),
            offset=float(e.get("offset", 0.0)),
            multiplier=float(e.get("multiplier", 1.0)),
            sign=sign,
            rtd=e.get("rtd"),
            limits=limits,
            derive=derive,
            default_setpoint=(None if e.get("default_setpoint") is None
                              else float(e["default_setpoint"])),
            legacy=e.get("legacy"),
            enabled=bool(e.get("enabled", True)),
            description=str(e.get("description", "")),
            log_minmax=bool(e.get("log_minmax", False)),
            on_pid=bool(e.get("on_pid", True)),
        )

    return channels


def _check_switchable(alarms: dict, channels: dict) -> None:
    """`switchable` in alarms.yaml names alarms that can be switched off one
    at a time. A name that is not a channel with thresholds is a typo, and a
    typo here would leave a switch on the page that switches nothing."""
    names = alarms.get("switchable") or []
    if not isinstance(names, list):
        raise ConfigError("alarms.yaml: `switchable` must be a list of channels")
    limits = alarms.get("channels") or {}
    for name in names:
        if name not in channels:
            raise ConfigError(f"alarms.yaml: switchable {name!r} is not a channel")
        if name not in limits:
            raise ConfigError(f"alarms.yaml: switchable {name!r} has no thresholds")


def _check_unreadable(alarms: dict, channels: dict) -> None:
    """`unreadable` groups in alarms.yaml. A channel name that is not a
    channel is a typo, and a typo here would leave that channel on the
    default rule - a major alarm at once - without anybody noticing."""
    groups = alarms.get("unreadable") or {}
    if not isinstance(groups, dict):
        raise ConfigError("alarms.yaml: `unreadable` must map group names to rules")
    for group, rule in groups.items():
        members = (rule or {}).get("channels") or []
        rep = (rule or {}).get("alarm_on")
        if not members or not rep:
            raise ConfigError(f"alarms.yaml: unreadable {group!r} needs channels and alarm_on")
        for name in list(members) + [rep]:
            if name not in channels:
                raise ConfigError(f"alarms.yaml: unreadable {group!r}: {name!r} is not a channel")
        if rep not in members:
            raise ConfigError(f"alarms.yaml: unreadable {group!r}: alarm_on {rep!r} "
                              "is not one of its channels")
        if str(rule.get("severity", "major")) not in ("minor", "major", "critical"):
            raise ConfigError(f"alarms.yaml: unreadable {group!r}: severity must be "
                              "minor, major or critical")


def load(config_dir: Path | str | None = None) -> Config:
    """Load every configuration file, validate, and compute the config hash."""
    d = Path(config_dir) if config_dir else CONFIG_DIR

    raw_channels = _read(d / "channels.yaml")
    devices = _read(d / "devices.yaml")
    alarms = _read(d / "alarms.yaml")

    # recipients.yaml is optional and excluded from the hash: it does not
    # affect the data, and it is edited from the web UI (§4.4).
    recipients_path = d / "recipients.yaml"
    recipients = _read(recipients_path) if recipients_path.exists() else {"recipients": []}

    channels = _parse_channels(raw_channels, devices)
    _check_switchable(alarms, channels)
    _check_unreadable(alarms, channels)

    # hv_defaults.yaml is optional and excluded from the hash, for the same
    # reason recipients.yaml is: it does not affect the data. A default is a
    # number offered in a box on /hv, not something that changes how a reading
    # is taken or what a stored value means (§4.5, §4.6).
    _apply_hv_defaults(channels, read_hv_defaults(d))

    return Config(
        channels=channels,
        devices=devices,
        alarms=alarms,
        recipients=recipients,
        config_hash=compute_hash(raw_channels, devices, alarms),
    )


# ---------------------------------------------------------------- HV defaults
#
# `hv_defaults.yaml` overrides `default_setpoint` for the HV setpoint channels.
# See DESIGN.md §4.6. It exists because this is an R&D setup: the operating
# point changes often, while the things around it in channels.yaml - the
# channel's identity, its sign, and above all its `limits` - do not.
#
# WHAT THIS FILE MAY AND MAY NOT DO. It may change which voltage "load
# defaults" OFFERS. It may not change `limits`, which are the range the write
# path of §10a validates against. A page that could widen its own limit and
# then write to it is not a guardrail, so the limits stay in channels.yaml,
# in git, edited by hand and reviewed.
#
# Nothing here reaches an instrument. A default is a number that appears in a
# box on /hv; a person still presses Apply, and that write is validated,
# read back and audited exactly as before.

HV_DEFAULTS_FILE = "hv_defaults.yaml"


def hv_defaults_path(config_dir: Path | str | None = None) -> Path:
    return (Path(config_dir) if config_dir else CONFIG_DIR) / HV_DEFAULTS_FILE


def read_hv_defaults(config_dir: Path | str | None = None) -> dict[str, Any]:
    """The raw contents of hv_defaults.yaml, or empty if there is none.

    Optional by design: a fresh clone has no such file and runs on the
    reviewed values in channels.yaml. The file appears the first time
    somebody saves from the web UI.
    """
    path = hv_defaults_path(config_dir)
    if not path.exists():
        return {}
    data = _read(path)
    return data if isinstance(data, dict) else {}


def _apply_hv_defaults(channels: dict[str, Channel],
                       raw: dict[str, Any]) -> None:
    """Override `default_setpoint` from hv_defaults.yaml, validating strictly.

    Strict, and raising, for the same reason every other configuration error
    raises (§4): a default that is silently dropped leaves "load defaults"
    offering a number nobody chose, and the operator has no way to tell. The
    web UI validates before it writes, so a file that fails here was edited by
    hand - and saying so is the whole point.
    """
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError(
            f"{HV_DEFAULTS_FILE}: `defaults` must be a mapping of "
            f"channel name to volts")

    for name, value in defaults.items():
        channel = channels.get(name)
        if channel is None:
            raise ConfigError(
                f"{HV_DEFAULTS_FILE}: unknown channel {name!r}")
        if channel.kind != "hv_vset":
            raise ConfigError(
                f"{HV_DEFAULTS_FILE}: {name!r} is a {channel.kind}, not a "
                f"setpoint; only hv_vset channels have a default")
        try:
            volts = float(value)
        except (TypeError, ValueError):
            raise ConfigError(
                f"{HV_DEFAULTS_FILE}: {name!r} has a non-numeric "
                f"default {value!r}") from None
        if not channel.limits:
            raise ConfigError(
                f"{HV_DEFAULTS_FILE}: {name!r} has no `limits` in "
                f"channels.yaml, so no default could ever be applied to it")
        if not channel.in_limits(volts):
            raise ConfigError(
                f"{HV_DEFAULTS_FILE}: {name!r} default {volts} is outside "
                f"its limits {channel.limits['min']}..{channel.limits['max']} "
                f"in channels.yaml. Change the limit there if that is "
                f"intended - it is not changed from the web UI (§4.6).")
        # `Channel` is frozen, and stays frozen: a configuration object that
        # can be edited in place is one that something can quietly edit in
        # place. The override replaces the entry instead.
        channels[name] = replace(channel, default_setpoint=volts)


def write_hv_defaults(values: dict[str, float], by: str,
                      config_dir: Path | str | None = None) -> None:
    """Write hv_defaults.yaml atomically.

    Atomic because the file is read by `load()` at startup and on every
    reload: a half-written file caught by a service starting at that instant
    is a configuration error on a machine where nothing is wrong. Written to a
    temporary file beside it and renamed, which is atomic on Windows and POSIX
    alike for a same-directory rename.

    Validation belongs to the caller, which has the Config to validate
    against; this writes what it is given.
    """
    path = hv_defaults_path(config_dir)
    body = {
        "updated": _utcnow_iso(),
        "by": by or "unknown",
        "defaults": {k: float(v) for k, v in values.items()},
    }
    text = (
        "# HV default setpoints - the values `load defaults` offers on /hv.\n"
        "# See DESIGN.md 4.6.\n"
        "#\n"
        "# WRITTEN BY THE WEB UI, and safe to edit by hand. Overrides\n"
        "# `default_setpoint` in channels.yaml for the channels named here.\n"
        "#\n"
        "# Volts, SIGNED, as everywhere else (section 7.2): the cathode is\n"
        "# negative, the anode positive. A value outside the channel's\n"
        "# `limits` in channels.yaml is refused - limits are NOT edited from\n"
        "# the web UI, because the write path validates against them.\n"
        "#\n"
        "# Nothing here reaches an instrument. Changing a number changes what\n"
        "# a box on /hv is filled with; a person still presses Apply.\n"
        "\n"
        + yaml.safe_dump(body, sort_keys=False, default_flow_style=False)
    )
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------- recipients
#
# Who receives alarm notifications (§4.4). Edited from /alarms, and read by the
# alarm engine at SEND time rather than at startup - so a change applies to the
# next notification, with no restart and no reload.
#
# The list is the list: everyone with `enabled: true` is notified. No shift
# roster and no escalation chain, which is a deliberate absence rather than a
# missing feature.

RECIPIENTS_FILE = "recipients.yaml"

_RECIPIENTS_HEADER = """\
# Who receives alarm notifications. See DESIGN.md 4.4.
#
# Edited from the web UI (/alarms) and applied without restarting anything:
# the alarm engine reads this file at send time. Editing it by hand works
# equally well and has the same effect.
#
# TWO LISTS IN ONE FILE, because they interrupt people differently and
# somebody who wants the quiet one should not have to take the loud one with
# it:
#
#   alarms: true   the alarm email and SMS, at whatever hour it fires
#   daily:  true   the daily report email, once a morning
#
# Neither implies the other, and either alone is a sensible thing to be. No
# shift roster, no escalation chain: the list is the list.
#
# `enabled:` is what the pair used to be, when there was one list. A row
# carrying only `enabled:` is read as BOTH - the list it described was the
# alarm list and the report list at once, and reading it as neither would
# turn somebody off silently. It is still written alongside the two, so that
# an alarm engine which has not been restarted since this change keeps
# notifying exactly who it did before.
#
# An empty phone is not a mistake - it means "do not SMS this person", and
# they are notified by email alone. Somebody with NEITHER an email nor a
# phone is saved with a warning, not refused: they are on the list and hear
# nothing, which is worth being told once, not worth blocking the save.
#
# An empty alarm list is a warning - if nobody has `alarms: true`, alarms
# reach nobody, and the engine raises a low-severity alarm saying so.

"""


def recipients_path(config_dir: Path | str | None = None) -> Path:
    return (Path(config_dir) if config_dir else CONFIG_DIR) / RECIPIENTS_FILE


def wants_alarms(person: dict) -> bool:
    """Does this person get the alarm email and SMS? (§4.4)

    `alarms:` if the row has one, and `enabled:` if it does not. The second
    half is what makes a file written before the two lists were split keep
    notifying the people it always did: `enabled: true` meant "tell me when
    something is wrong", and that is this list.
    """
    if "alarms" in person:
        return bool(person.get("alarms"))
    return bool(person.get("enabled"))


def wants_daily(person: dict) -> bool:
    """Does this person get the daily report email? (§4.4)

    Same fallback, for the same reason: the one list used to feed both, so a
    row that only says `enabled: true` is on both until somebody says
    otherwise. Read it as neither and a morning report would go quiet with
    nothing on any page saying why.
    """
    if "daily" in person:
        return bool(person.get("daily"))
    return bool(person.get("enabled"))


def read_recipients(config_dir: Path | str | None = None) -> list[dict]:
    """The recipient list, or empty if there is no file.

    Never raises on an absent file: alarms with nobody to notify is a
    condition the engine warns about (§4.4), not a reason to refuse to start.

    Every row comes back with an explicit `alarms` and `daily`, resolved from
    a legacy `enabled` where that is all the file has. Nothing downstream -
    the page, the audit line, the save - then has to remember the fallback,
    which is the kind of thing that gets remembered in three places and
    forgotten in the fourth.
    """
    path = recipients_path(config_dir)
    if not path.exists():
        return []
    data = _read(path)
    people = (data or {}).get("recipients") or []
    rows = []
    for person in people:
        if not isinstance(person, dict):
            continue
        row = dict(person)
        row["alarms"] = wants_alarms(person)
        row["daily"] = wants_daily(person)
        row.pop("enabled", None)
        rows.append(row)
    return rows


def validate_recipients(people: list[dict]) -> list[str]:
    """Reasons to REFUSE a proposed recipient list, as sentences.

    Only things that would make the list wrong are here. A blank field is not
    one of them - see `recipient_warnings` for what is said loudly and saved
    anyway.

    Deliberately NOT a schema check on the file at load time. The engine must
    keep running on a list it finds odd - refusing to start because somebody
    mistyped an address would take the alarms down to protect the alarms.
    This is checked where a change is MADE, which is the page.
    """
    problems = []
    seen = set()
    for i, person in enumerate(people, start=1):
        name = (person.get("name") or "").strip()
        email = (person.get("email") or "").strip()
        phone = (person.get("phone") or "").strip()
        where = f"row {i}" if not name else name
        if not name:
            problems.append(f"{where}: no name")
        if email and ("@" not in email or email.startswith("@")
                      or email.endswith("@")):
            problems.append(f"{where}: {email!r} is not an email address")
        if phone and not phone.startswith("+"):
            # The gateway wants international form. A number that looks right
            # to a Dutch reader and is refused at 3am is worse than one
            # refused here.
            problems.append(
                f"{where}: {phone!r} must start with + and a country code")
        key = name.casefold()
        if key and key in seen:
            problems.append(f"{name}: listed twice")
        seen.add(key)
    return problems


def recipient_warnings(people: list[dict]) -> list[str]:
    """What is worth saying out loud about a list that is saved anyway.

    Somebody with neither an email nor a phone sits on the list looking
    notified and hears nothing. That used to be a refusal, which meant a
    half-filled row could not be parked while somebody went to look up a
    number - and refusing the whole list over one blank field takes the
    recipient list hostage to a detail. It is now saved and said loudly,
    like "nobody is enabled" already was: the same warning, the same place,
    and the operator decides.

    The daily report is the same trap one column further along: it is email
    only, so somebody ticked for it with a phone and no address is on a list
    that cannot reach them.
    """
    notes = []
    for i, person in enumerate(people, start=1):
        name = (person.get("name") or "").strip()
        email = (person.get("email") or "").strip()
        phone = (person.get("phone") or "").strip()
        where = f"row {i}" if not name else name
        if not email and not phone:
            notes.append(
                f"{where} has no email and no phone, so they are on the "
                f"list and hear nothing")
        elif wants_daily(person) and not email:
            # Only when they have a phone - with neither, the line above has
            # already said it, and saying it twice about one row reads like
            # two problems.
            notes.append(
                f"{where} is on the daily report and has no email, so the "
                f"report does not reach them")
    return notes


def write_recipients(people: list[dict], by: str,
                     config_dir: Path | str | None = None) -> None:
    """Write recipients.yaml atomically, keeping the explanatory header.

    `yaml.safe_dump` of the list alone would drop the header, and this file is
    meant to stay hand-editable - so the prose is prepended every time rather
    than being something a save quietly eats.
    """
    path = recipients_path(config_dir)
    rows = []
    for person in people:
        rows.append({
            "name": (person.get("name") or "").strip(),
            "email": (person.get("email") or "").strip(),
            "phone": (person.get("phone") or "").strip(),
            "alarms": wants_alarms(person),
            "daily": wants_daily(person),
            # The one key both used to be. Written as a mirror of `alarms`
            # so that an alarm engine still running the older code - the
            # services are not restarted by a save - goes on telling the
            # same people it was told to tell.
            "enabled": wants_alarms(person),
        })
    text = _RECIPIENTS_HEADER + yaml.safe_dump(
        {"recipients": rows}, sort_keys=False, default_flow_style=False,
        allow_unicode=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
