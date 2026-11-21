"""Deployment operating settings that only the deployment owner provisions (v2 01C).

The ONE allowlist of what ``manage.py provision_settings`` may write, with the same validation the readers
apply (``operations.exceptions.parse_threshold`` for minutes, zoneinfo AND ``pg_timezone_names`` for the time
zone). ``demo_mode`` and ``allow_draft_policies`` are NOT here and can never be written by this path.
The read-only Settings page and the first-run checklist read the same table.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import connection

from fleet.models import DeploymentSetting
from operations.exceptions import MAX_THRESHOLD_MIN, TIMEZONE_MAX_LEN, parse_threshold, python_knows_timezone


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    purpose: str
    kind: str  # "text" | "minutes" | "timezone"
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


def current_values() -> dict[str, str]:
    return dict(DeploymentSetting.objects.filter(key__in=ALLOWED_KEYS).values_list("key", "value"))


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
