"""Plain-language helpers for the customer and booking screens (milestone 01C).

Nothing here decides anything. Eligibility, consent and feasibility all come from planning.services; this
module only (a) turns the blocker codes it returns into sentences with what to do, (b) turns refusals into
plain words that name the responsible role, and (c) issues the per-form one-time tokens that stop a double
submit.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

from django.http import HttpRequest

from accounts.roles import PERMISSIONS
from platform_core.commands import CommandError, PermissionDenied
from platform_core.templatetags.ui import local_dt, local_time

# ------------------------------------------------------------------ who can do what
ACTIONS = {
    "planning.manage_sites": "add or change customer homes",
    "planning.assess": "record a home assessment",
    "planning.approve_assessment": "approve a home assessment",
    "planning.consent": "record or withdraw consent",
    "planning.plan": "request, plan or cancel visits",
    "planning.confirm": "confirm visits",
}


def role_words(permission: str) -> str:
    """``ops lead`` / ``field operator or ops lead`` for a permission, read from accounts.roles."""
    roles = sorted(r.replace("_", " ") for r in PERMISSIONS.get(permission, set()))
    if len(roles) < 3:
        return " or ".join(roles)
    return ", ".join(roles[:-1]) + " or " + roles[-1]


def who_can(permission: str) -> str:
    return f"Only the {role_words(permission)} can {ACTIONS.get(permission, 'do this')}."


# ------------------------------------------------------------------ refusals
def refusal_text(exc: CommandError, permission: str | None = None) -> str:
    """One message in the shape base.html turns into a "Refused (...)" box: plain words, who can act."""
    text = str(exc)
    if isinstance(exc, PermissionDenied) and text.startswith("Not allowed:"):
        perm = permission or text.split(":", 1)[1].strip()
        return f"Refused (not allowed): {who_can(perm)} Nothing was changed."
    if isinstance(exc, PermissionDenied):
        return (
            f"Refused (not allowed): {text}. Ask a different person with the right role. Nothing was changed."
        )
    kind = {422: "check the form", 409: "not possible right now"}.get(exc.status, "not possible")
    return f"Refused ({kind}): {text.rstrip('.')}. Nothing was changed."


# ------------------------------------------------------------------ blockers in plain English
# code -> (what is wrong, what to do). Dynamic details (names of robots, times) are filled in below.
BLOCKERS: dict[str, tuple[str, str]] = {
    "SITE_NOT_ASSESSED": (
        "This home has no approved assessment that covers the visit date.",
        "Record an assessment on the customer page, then ask someone else (ops lead or engineering lead) "
        "to approve it.",
    ),
    "SITE_NOT_ELIGIBLE": ("This home was assessed as not eligible for visits.", "Do not book a visit here."),
    "SITE_RESTRICTED": (
        "This home is eligible only with restrictions.",
        "Check the excluded tasks on the customer page before going ahead.",
    ),
    "TASK_EXCLUDED": (
        "Some requested tasks are not allowed at this home.",
        "Remove those tasks from the visit.",
    ),
    "CONSENT": (
        "Consent from the adults at this home is not complete.",
        "Record consent for every adult on the customer page.",
    ),
    "UNKNOWN_TRAVEL": (
        "Travel time to or from the home is not known.",
        "Plan the visit again with travel times filled in.",
    ),
    "UNKNOWN_INSPECTION_TIME": (
        "The time needed for the pre-visit inspection is not set.",
        "Ask the ops lead to set the pre-visit inspection time.",
    ),
    "ROBOT_MISSING": ("No robot is chosen yet.", "Pick a robot below."),
    "ROBOT_NOT_ELIGIBLE": (
        "The chosen robot is not ready for planning.",
        "Pick another robot, or clear its holds first.",
    ),
    "UNKNOWN_LOAD_TIME": (
        "How long this robot takes to load and unload is not known.",
        "Ask engineering to publish the loading time for this robot.",
    ),
    "UNKNOWN_CHARGE_TIME": (
        "How long this robot needs to charge after a visit is not known.",
        "Ask engineering to publish the charge time for this robot.",
    ),
    "CREW_INCOMPLETE": ("Two people are needed: a lead and a second.", "Pick both people below."),
    "CREW_SAME_PERSON": ("The lead and the second are the same person.", "Pick two different people."),
    "VEHICLE_MISSING": ("No vehicle is chosen yet.", "Pick a vehicle below."),
    "VEHICLE_CAPACITY_UNKNOWN": (
        "How many robots this vehicle can carry is not recorded.",
        "Pick another vehicle, or ask the ops lead to record its capacity.",
    ),
    "VEHICLE_RESTRAINT_UNKNOWN": (
        "This vehicle has no restraint kit recorded.",
        "Pick another vehicle, or ask the ops lead to record the kit.",
    ),
    "QUALIFICATION": (
        "A crew member is not qualified for the whole activity.",
        "Pick someone else, or ask a safety reviewer to renew the qualification.",
    ),
    "CONFLICT": (
        "Something you picked is already booked at that time.",
        "Pick another time, or choose a different robot, person or vehicle.",
    ),
}
_CONFLICT = re.compile(
    r"^(?P<res>.+?) is already booked for (?P<visit>\S+) \((?P<what>.*?)\) "
    r"(?P<day>\d{4}-\d{2}-\d{2}) (?P<a>\d{2}:\d{2})-(?P<b>\d{2}:\d{2}) UTC, overlapping (?P<over>.+)$"
)
_QUAL = re.compile(
    r"^(?P<who>\S+) is not qualified "
    r"(?:as (?P<role>.+?) for the whole activity|for robot handling during the activity)$"
)
_NO_RESTRICTION_TEXT = {"SITE_RESTRICTED"}  # the stored text is free text: never echo it


def _utc(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+00:00").astimezone(UTC)


def plain_blocker(b: dict[str, str]) -> dict[str, str]:
    """{'level','sentence','action','code','raw'} for one blocker from services.evaluate."""
    code = b.get("code", "")
    sentence, action = BLOCKERS.get(
        code, (b.get("message", "Something needs attention."), b.get("action", ""))
    )
    msg = b.get("message", "")
    if code == "CONFLICT" and (m := _CONFLICT.match(msg)):
        start, end = _utc(m["day"], m["a"]), _utc(m["day"], m["b"])
        sentence = (
            f"{m['res']} is already booked for visit {m['visit']} from {local_dt(start)} "
            f"to {local_time(end)}, which overlaps this plan."
        )
    elif code == "QUALIFICATION" and (m := _QUAL.match(msg)):
        what = "robot handling" if m["role"] is None else m["role"]
        sentence = f"{m['who']} is not qualified for {what} for the whole activity."
    elif code == "ROBOT_NOT_ELIGIBLE" and ":" in msg:
        sentence = f"Robot {msg.split(':', 1)[0]} is not ready for planning."
    elif code == "CONSENT" and msg:
        sentence = msg.rstrip(".") + "."
    return {
        "level": b.get("level", "blocked"),
        "sentence": sentence,
        "action": action,
        "code": code,
        "raw": "" if code in _NO_RESTRICTION_TEXT else msg,
    }


def plain_blockers(blockers: list[dict[str, str]]) -> list[dict[str, str]]:
    return [plain_blocker(b) for b in blockers]


# ------------------------------------------------------------------ one-time form tokens
SESSION_KEY = "planning_form_tokens"
MAX_TOKENS = 200
ALREADY_DONE = (
    "This form was already submitted, or the page is out of date. Nothing was changed this time. "
    "Check the record below before trying again."
)


def issue_token(request: HttpRequest) -> str:
    tokens: list[str] = list(request.session.get(SESSION_KEY, []))
    token = secrets.token_urlsafe(16)
    tokens.append(token)
    request.session[SESSION_KEY] = tokens[-MAX_TOKENS:]
    return token


def consume_token(request: HttpRequest) -> bool:
    """True exactly once per issued token. A replayed or forged POST gets False and must do nothing."""
    token = request.POST.get("form_token", "")
    tokens: list[str] = list(request.session.get(SESSION_KEY, []))
    if not token or token not in tokens:
        return False
    tokens.remove(token)
    request.session[SESSION_KEY] = tokens
    return True
