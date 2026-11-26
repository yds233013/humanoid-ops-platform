"""Deployment operating settings that only the deployment owner provisions (v2 01C).

The ONE allowlist of what ``manage.py provision_settings`` may write, with the same validation the readers
apply (``operations.exceptions.parse_threshold`` for minutes, zoneinfo AND ``pg_timezone_names`` for the time
zone). ``demo_mode`` and ``allow_draft_policies`` are NOT here and can never be written by this path.
The read-only Settings page and the first-run checklist read the same table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time as dtime
from typing import Any

from django.db import connection

from fleet.models import DeploymentSetting
from operations.exceptions import MAX_THRESHOLD_MIN, TIMEZONE_MAX_LEN, parse_threshold, python_knows_timezone


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    purpose: str
    kind: str  # "text" | "minutes" | "timezone" | "boolean" | "stock_levels" | "days" | "hours"
    required: bool = False
    default: str | None = None  # offered only with --with-defaults


SPECS: tuple[Spec, ...] = (
    Spec(
        "consent_form_version",
        "Consent form version",
        "The version of the customer consent form people sign; visits cannot record consent without it.",
        "text",
        required=True,
    ),
    Spec(
        "pre_visit_inspection_min",
        "Pre-visit inspection time (minutes)",
        "How long the pre-visit robot inspection takes; planning blocks a visit while it is unknown.",
        "minutes",
        required=True,
    ),
    Spec(
        "ops_readiness_lookahead_min",
        "Readiness look-ahead (minutes)",
        "How far ahead Today looks for visits whose departure checklist is not complete.",
        "minutes",
        required=True,
        default="2880",
    ),
    Spec(
        "ops_closeout_timezone",
        "Daily close-out time zone",
        "The time zone that decides where one day ends for the daily close-out.",
        "timezone",
    ),
    Spec(
        "ops_custody_verify_grace_min",
        "Handover confirmation grace (minutes)",
        "How long a crew has to confirm the robot handover before an exception is raised.",
        "minutes",
    ),
    Spec(
        "ops_return_grace_min",
        "Late return grace (minutes)",
        "How long after the planned end of unloading a return may be late before an exception is raised.",
        "minutes",
    ),
    Spec(
        "ops_inspection_grace_min",
        "Post-visit inspection grace (minutes)",
        "How long after a visit the post-visit inspection may be missing before an exception is raised.",
        "minutes",
    ),
    Spec(
        "ops_periodic_due_lookahead_min",
        "Regular inspection look-ahead (minutes)",
        "How far ahead Today warns that a robot's regular inspection is coming due.",
        "minutes",
    ),
    Spec(
        "ops_soc_fresh_min",
        "Battery reading freshness (minutes)",
        "How long a recorded battery level counts as current for the charge forecast; without it the "
        "forecast is Unknown.",
        "minutes",
    ),
    # v2 02C (incremental deployment safety): absent = inactive. While inactive the maintenance status is
    # computed and shown but does not block planning or departure, and production lists the inactive gate as a
    # blocker. Switching it OFF again needs --allow-change (see the provision_settings command).
    Spec(
        "maintenance_gate_active",
        "Maintenance gate active",
        "When true, overdue or unknown maintenance blocks planning and departure. Turn it on only after the "
        "maintenance rules are approved, technicians are qualified and every robot has a baseline record.",
        "boolean",
    ),
    # v2 02D kit inventory (contract 11, 8.5): per-item operating thresholds, owner-provisioned. Absent (or an
    # item not listed) = no minimum / not counted per visit, and the screens say so.
    Spec(
        "stock_minimums",
        "Stock minimum levels",
        "Per stock item, the level below which the hub restocks (CODE=N, comma separated, for example "
        "RK=3,WIPES=20). An item not listed has no minimum.",
        "stock_levels",
    ),
    Spec(
        "stock_per_visit",
        "Stock each visit takes",
        "Per stock item, how many each visit takes (CODE=N, comma separated). A visit that cannot get them "
        "is At risk. An item not listed is not counted per visit.",
        "stock_levels",
    ),
    # v2 03B-3 (contract 13.4 item 4, 13.5): the next-free-time search. The look-ahead is global; the
    # operating hours are per hub (``--hub CODE``), with the global row as the fallback. Absent = the search
    # says it cannot run, and why.
    Spec(
        "dispatch_lookahead_days",
        "Dispatch look-ahead (days)",
        "How many days ahead the next free time search looks for a robot, a crew and a vehicle.",
        "days",
    ),
    Spec(
        "hub_operating_hours",
        "Hub operating hours",
        "When visits may be on site, in the hub's own time zone (HH:MM-HH:MM, for example 08:00-20:00). "
        "Set per hub with --hub; the value without a hub applies to every hub that has none.",
        "hours",
    ),
    # v2 03C-3 (contract 14.4): how far ahead of the server's receipt time a phone's clock may be before an
    # ordinary field step is refused (protective steps are never refused for the clock; they are flagged).
    # Absent = 2 minutes (the contract's demo value), so a missing setting never widens the bound.
    Spec(
        "field_clock_skew_min",
        "Phone clock tolerance (minutes)",
        "How far ahead of the server's time a phone's clock may be when it sends queued field steps. A step "
        "timed later than that is refused with a reason (safety reports are kept and flagged instead).",
        "minutes",
        default="2",
    ),
)
BY_KEY = {s.key: s for s in SPECS}
ALLOWED_KEYS = tuple(BY_KEY)
REQUIRED_KEYS = tuple(s.key for s in SPECS if s.required)
MAX_TEXT = 200


def validate(key: str, raw: str) -> str:
    """The normalized value, or ValueError with a plain message."""
    spec = BY_KEY[key]
    value = raw.strip()
    if spec.kind == "minutes":
        if parse_threshold(value) is None:
            raise ValueError(f"{key} must be a whole number of minutes from 1 to {MAX_THRESHOLD_MIN}")
        return str(int(value))
    if spec.kind == "boolean":
        if value not in ("true", "false"):
            raise ValueError(f"{key} must be true or false")
        return value
    if spec.kind == "stock_levels":
        return normalize_stock_levels(key, value)
    if spec.kind == "days":
        if not value.isdigit() or not 1 <= int(value) <= MAX_LOOKAHEAD_DAYS:
            raise ValueError(f"{key} must be a whole number of days from 1 to {MAX_LOOKAHEAD_DAYS}")
        return str(int(value))
    if spec.kind == "hours":
        hours = parse_hours(value)
        if hours is None:
            raise ValueError(
                f"{key} must look like 08:00-20:00 (opening before closing, on the same day, 24-hour clock)"
            )
        return f"{hours[0]:%H:%M}-{hours[1]:%H:%M}"
    if spec.kind == "timezone":
        if len(value) > TIMEZONE_MAX_LEN or not python_knows_timezone(value):
            raise ValueError(f"{key} must be a known time zone name such as America/Los_Angeles")
        with connection.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = %s)", [value])
            row = cur.fetchone()
        if not (row and row[0]):
            raise ValueError(f"{key} must be a time zone name the database knows")
        return value
    if not value or len(value) > MAX_TEXT or any(ord(c) < 32 for c in value):
        raise ValueError(f"{key} must be 1 to {MAX_TEXT} characters of plain text")
    return value


MAX_LOOKAHEAD_DAYS = 31
HOURS_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def parse_hours(raw: str | None) -> tuple[dtime, dtime] | None:
    """``08:00-20:00`` -> (08:00, 20:00); None when malformed or not opening before closing."""
    m = HOURS_RE.match((raw or "").replace(" ", ""))
    if m is None:
        return None
    a, b = dtime(int(m[1]), int(m[2])), dtime(int(m[3]), int(m[4]))
    return (a, b) if a < b else None


STOCK_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,11}$")
MAX_STOCK_LEVEL = 100000


def normalize_stock_levels(key: str, value: str) -> str:
    """``CODE=N,CODE=N``: item codes (letters, digits, dashes, up to 12) each once, N a whole number from 0 to
    100000. Normalized to upper case without spaces. The database reads the same format (hub/0008)."""
    pairs: list[str] = []
    seen: set[str] = set()
    for part in value.replace(" ", "").split(","):
        code, sep, number = part.upper().partition("=")
        if not sep or not STOCK_CODE_RE.match(code) or not number.isdigit() or len(number) > 6:
            raise ValueError(f"{key} must look like RK=3,WIPES=20 (item code = whole number)")
        if int(number) > MAX_STOCK_LEVEL or code in seen:
            raise ValueError(f"{key}: each item once, with a number from 0 to {MAX_STOCK_LEVEL}")
        seen.add(code)
        pairs.append(f"{code}={int(number)}")
    text = ",".join(pairs)
    if len(text) > MAX_TEXT:
        raise ValueError(f"{key} must be at most {MAX_TEXT} characters")
    return text


def parse_stock_levels(raw: str | None) -> dict[str, int]:
    """The provisioned value as {code: n}; a malformed value counts as not provisioned (empty)."""
    if not raw:
        return {}
    try:
        text = normalize_stock_levels("stock", raw)
    except ValueError:
        return {}
    return {c: int(n) for c, n in (p.split("=") for p in text.split(","))}


def current_values(hub: Any = None) -> dict[str, str]:
    """The GLOBAL values (hub rows never mix in); with ``hub``, that hub's own override rows only
    (v2 03A-1)."""
    qs = DeploymentSetting.objects.filter(key__in=ALLOWED_KEYS)
    qs = qs.filter(hub__isnull=True) if hub is None else qs.filter(hub=hub)
    return dict(qs.values_list("key", "value"))


def hub_value(key: str, hub_id: int | None) -> str | None:
    """The value that applies at a hub: its own row when it has one, else the global row (v2 03A-1, 11.3).
    Only PER_HUB_SETTING_KEYS have hub rows; every other key is global."""
    from django.db.models import Q

    rows = list(
        DeploymentSetting.objects.filter(key=key)
        .filter(Q(hub__isnull=True) | Q(hub_id=hub_id))
        .values_list("hub_id", "value")
    )
    own = [v for h, v in rows if h is not None]
    glob = [v for h, v in rows if h is None]
    return own[0] if own else (glob[0] if glob else None)


def rows() -> list[dict[str, object]]:
    """For the read-only page: every allowlisted setting with its current value (None = not set)."""
    have = current_values()
    out: list[dict[str, object]] = []
    for s in SPECS:
        value = have.get(s.key)
        valid = False
        if value is not None:
            try:
                validate(s.key, value)
                valid = True
            except ValueError:
                valid = False
        out.append({"spec": s, "value": value, "valid": valid})
    return out


def required_complete() -> bool:
    have = current_values()
    for k in REQUIRED_KEYS:
        if k not in have:
            return False
        try:
            validate(k, have[k])
        except ValueError:
            return False
    return True
