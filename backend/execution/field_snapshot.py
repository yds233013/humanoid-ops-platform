"""v2 03D-2: the read-only run snapshot for the offline field screen (contract 15.1, 15.10 items 1, 3 and 5).

    GET /api/field/runs/<code>/snapshot?device=<uuid>

Order (each step only when the one before passed), as the sync endpoint (field_sync):

1. **Authentication first.** No session, or an ended one: HTTP 401 with the sync endpoint's fixed body.
   Nothing is parsed and no run is looked up.
2. **GET only** (HEAD too): anything else is HTTP 405. **``device``** must be a uuid: HTTP 400 otherwise.
3. **Run access**, the sync rule (14.9 deviation 2): a crew member of the run (lead or second of the departed
   revision) or an ops lead with a grant at the run's hub (regional or that hub's). Otherwise one HTTP 403
   ``NOT_YOUR_RUN`` with the same body for a missing run, and nothing else is read.
4. **The snapshot**, read-only, ``Cache-Control: no-store``. Only data the run page already shows these
   people, cut to what the phone needs: the run's state, robot, hub and home label (code and town, no CRM
   reference); the field steps with their class (A, B, C from ``field_commands.CLASS``) and whether each is
   recorded; the crew and the ops leads at the run's hub (names only); the protective state of the run; and
   the server ledger state (generation, next expected seq, last decided seq and its outcome). No credential,
   session, consent detail, other home or other run is in it.

``ledger`` is the run's ordinary stream (contract 14.8 item 4): its sequence is shared by every device of
the run, so it is the same for every ``device``; ``device_id`` echoes the asking device so the phone can bind
the snapshot to its queue (15.10 item 5).

``protective.open`` is true while the run has an unresolved interruption, an incident that is not closed, or a
paused session (15.10 item 3: the banner stays until the snapshot shows none). A stopped session is reported
as kind ``stop`` with ``open`` false: a stop is final for the session, so it is a safe resolution.

``readiness_code`` is ``ROBOT_INTERRUPTION_UNRESOLVED`` (15.7) when the run is closed and its robot cannot
leave again because of an unresolved interruption; otherwise null.

Statements: a fixed number, whatever the number of records on the run (tests/test_v2_03d_snapshot.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db import connection
from django.db.models import OuterRef, Q, Subquery
from django.http import HttpRequest, JsonResponse
from django.utils import timezone

from execution.custody import ROBOT_INTERRUPTION_UNRESOLVED, departure_problem, unresolved_interruption
from execution.field_commands import CLASS
from execution.field_sync import NOT_AUTHENTICATED, NOT_YOUR_RUN, iso_local
from execution.models import (
    CustodyEvent,
    ExecutionRun,
    Incident,
    IncidentStatusChange,
    Interruption,
    SessionEvent,
    SiteCheck,
    TripEvent,
)

SNAPSHOT_VERSION = 1
BAD_DEVICE = {"error": "BAD_REQUEST", "message": "device must be the phone's device id (a uuid)."}

# The field steps of a run, in the order the commands accept them: (next-step key of the run page, command).
# The label is the run page's own step title (execution.views.NEXT_STEPS); the class is the 03C one. Steps
# without a field command (receive at the hub, post-visit inspection, reconcile, close) are hub work done
# online and are not listed.
FIELD_STEPS: list[tuple[str, str]] = [
    ("verify", "custody.verify"),
    ("arrive_site", "trip.arrived_site"),
    ("to_site", "custody.handover_to_site"),
    ("site_check", "site_check.record"),
    ("start", "session.start"),
    ("stop", "session.stop"),
    ("to_vehicle", "custody.back_to_vehicle"),
    ("depart_site", "trip.departed_site"),
    ("arrive_hub", "trip.arrived_hub"),
]


def _json(body: dict[str, Any], status: int = 200) -> JsonResponse:
    resp = JsonResponse(body, status=status)
    resp["Cache-Control"] = "no-store"
    return resp


def _name(user: Any) -> str:
    return str(user.get_full_name() or user.get_username())


def _signed_in(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_active)


# ------------------------------------------------------------------ access (the sync rule, one crew read)
def _run_for(actor: Any, code: str) -> tuple[ExecutionRun, list[Any]] | None:
    """The run and its crew (Person rows, lead first) when the actor may sync it, else None (missing or not
    theirs: the same answer). Same rule as field_sync.run_for_sync."""
    from accounts.permissions import active_roles
    from planning.models import Person

    run = (
        ExecutionRun.objects.select_related("revision", "visit__site", "visit__hub", "robot", "closure")
        .filter(code=code)
        .first()
    )
    if run is None:
        return None
    lead, second = run.revision.lead_id, run.revision.second_id
    crew = sorted(
        Person.objects.select_related("user").filter(pk__in=[i for i in (lead, second) if i is not None]),
        key=lambda p: 0 if p.pk == lead else 1,
    )
    if any(p.user_id == actor.pk for p in crew) or "ops_lead" in active_roles(actor, run.visit.hub_id):
        return run, crew
    return None


# ------------------------------------------------------------------ parts
LEDGER_SQL = """
SELECT s.g, s.n, clock_timestamp(),
       (SELECT fc.outcome FROM execution_field_command fc
         WHERE fc.run_id = %(run)s AND fc.generation = s.g AND fc.seq = s.n - 1)
  FROM (SELECT g, execution_field_next_seq(%(run)s, g) AS n FROM execution_field_generation_now(%(run)s) g) s
"""


def _ledger(run: ExecutionRun, device_id: str) -> tuple[dict[str, Any], datetime]:
    with connection.cursor() as cur:
        cur.execute(LEDGER_SQL, {"run": run.pk})
        generation, nxt, now, outcome = cur.fetchone()
    return (
        {
            "device_id": device_id,
            "generation": int(generation),
            "next_expected_seq": int(nxt),
            "last_decided_seq": int(nxt) - 1,
            "last_decided_outcome": outcome,
        },
        now,
    )


def _ops_leads(hub_id: int) -> list[dict[str, str]]:
    from django.contrib.auth import get_user_model

    from accounts.models import RoleGrant

    now = timezone.now()
    grants = RoleGrant.objects.filter(
        role="ops_lead", revoked_at__isnull=True, valid_from__lte=now, user_id=OuterRef("pk")
    ).filter((Q(valid_to__isnull=True) | Q(valid_to__gt=now)) & (Q(hub__isnull=True) | Q(hub_id=hub_id)))
    users = get_user_model().objects.filter(is_active=True).filter(pk__in=grants.values("user_id"))
    names = sorted({_name(u) for u in users})
    return [{"name": n} for n in names]


def _first(rows: list[Any], test: Any) -> Any:
    return next((r for r in rows if test(r)), None)


def _steps(run: ExecutionRun) -> tuple[list[dict[str, Any]], list[str], list[SessionEvent]]:
    from execution.views import NEXT_STEPS  # the run page's step titles (labels)

    custody = list(CustodyEvent.objects.filter(run=run).select_related("verification").order_by("seq"))
    trip = list(TripEvent.objects.filter(run=run).order_by("seq"))
    go = SiteCheck.objects.filter(run=run, result="go").order_by("-recorded_at", "-id").first()
    sessions = list(SessionEvent.objects.filter(run=run).exclude(kind="observation").order_by("seq"))
    trip_at = {t.kind: t.recorded_at for t in trip}

    def departure_verified() -> datetime | None:
        if not custody:
            return None
        first = custody[0]
        if first.basis != "administrative":
            return first.recorded_at
        verification = getattr(first, "verification", None)
        return verification.recorded_at if verification is not None else None

    def at(e: Any) -> datetime | None:
        return e.recorded_at if e is not None else None

    when: dict[str, datetime | None] = {
        "verify": departure_verified(),
        "arrive_site": trip_at.get("arrived_site"),
        "to_site": at(_first(custody, lambda c: c.to_kind == "site")),
        "site_check": at(go),
        "start": at(_first(sessions, lambda e: e.kind == "start")),
        "stop": at(_first(sessions, lambda e: e.kind == "stop")),
        "to_vehicle": at(_first(custody, lambda c: c.from_kind == "site" and c.to_kind == "vehicle")),
        "depart_site": trip_at.get("departed_site"),
        "arrive_hub": trip_at.get("arrived_hub"),
    }
    steps = [
        {
            "command": command,
            "label": NEXT_STEPS[key][0],
            "class": CLASS[command],
            "done": when[key] is not None,
            "recorded_at": iso_local(when[key]),
        }
        for key, command in FIELD_STEPS
    ]
    return steps, [t.kind for t in trip], sessions


def _state(run: ExecutionRun, trip: list[str]) -> str:
    if getattr(run, "closure", None) is not None:
        return "closed"
    if "arrived_hub" in trip:
        return "back_at_hub"
    if "departed_site" in trip:
        return "returning"
    if "arrived_site" in trip:
        return "on_site"
    return "to_site"


def _protective(run: ExecutionRun, sessions: list[SessionEvent]) -> dict[str, Any]:
    robot = run.robot.code
    # both read every time, so the statement count does not depend on the state
    interruption = Interruption.objects.filter(run=run, resolution__isnull=True).order_by("id").first()
    latest = IncidentStatusChange.objects.filter(incident=OuterRef("pk")).order_by("-recorded_at", "-id")
    incident = (
        Incident.objects.filter(run=run)
        .annotate(status=Subquery(latest.values("to_status")[:1]))
        .exclude(status="closed")
        .order_by("id")
        .first()
    )
    if interruption is not None:
        return {
            "open": True,
            "kind": "interruption",
            "since": iso_local(interruption.recorded_at),
            "message": f"Robot {robot} must not operate. Interruption {interruption.code} is open; "
            "an ops lead resolves it.",
        }
    if incident is not None:
        return {
            "open": True,
            "kind": "incident",
            "since": iso_local(incident.reported_at),
            "message": f"Robot {robot} must not operate. Incident {incident.code} is open; "
            "an ops lead reviews it.",
        }
    last = sessions[-1] if sessions else None
    if last is not None and last.kind == "pause":
        return {
            "open": True,
            "kind": "pause",
            "since": iso_local(last.recorded_at),
            "message": f"The session is paused. Robot {robot} must not operate until the session is resumed "
            "after a new Go site check.",
        }
    if last is not None and last.kind == "stop":
        return {
            "open": False,
            "kind": "stop",
            "since": iso_local(last.recorded_at),
            "message": f"The session is stopped. Robot {robot} does not work again on this run.",
        }
    return {
        "open": False,
        "kind": None,
        "since": None,
        "message": "No interruption, open incident or pause is recorded on this run.",
    }


def _readiness_code(run: ExecutionRun) -> str | None:
    if getattr(run, "closure", None) is None:
        return None  # an open run: the open-run problem comes first, nothing to add (15.7 is about leaving)
    if unresolved_interruption(departure_problem(run.robot_id)) is not None:
        return ROBOT_INTERRUPTION_UNRESOLVED
    return None


def build(actor: Any, run: ExecutionRun, crew: list[Any], device_id: str) -> dict[str, Any]:
    ledger, now = _ledger(run, device_id)
    steps, trip, sessions = _steps(run)
    site = run.visit.site
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "fetched_at": iso_local(now),
        "user": {"id": int(actor.pk), "name": _name(actor)},
        "run": {
            "code": run.code,
            "state": _state(run, trip),
            "hub": run.visit.hub.code,
            "robot": run.robot.code,
            "home_label": " · ".join(x for x in (site.code, site.zone) if x),
        },
        "steps": steps,
        "crew": [{"id": int(p.user_id), "name": _name(p.user)} for p in crew],
        "ops_leads": _ops_leads(run.visit.hub_id),
        "protective": _protective(run, sessions),
        "ledger": ledger,
        "readiness_code": _readiness_code(run),
    }


# ------------------------------------------------------------------ view
def snapshot(request: HttpRequest, code: str) -> JsonResponse:
    if not _signed_in(request):
        return _json(NOT_AUTHENTICATED, 401)
    if request.method not in ("GET", "HEAD"):
        resp = _json({"error": "METHOD_NOT_ALLOWED", "message": "Use GET."}, 405)
        resp["Allow"] = "GET, HEAD"
        return resp
    raw = request.GET.get("device", "")
    try:
        device_id = str(uuid.UUID(raw))
    except ValueError:
        return _json(BAD_DEVICE, 400)
    if len(code) > 12:
        return _json(NOT_YOUR_RUN, 403)
    found = _run_for(request.user, code)
    if found is None:
        return _json(NOT_YOUR_RUN, 403)
    run, crew = found
    return _json(build(request.user, run, crew, device_id))
