"""v2 02B charging: commands and the charge forecast (docs/11_v2_phase02_contract.md 8.3, 8.4, 8.6, 8.8).

COMMANDS. Chargers are configuration (run_command, permission hub.manage_chargers). Reservations, charging
sessions and battery readings are written ONLY by the SECURITY DEFINER functions of hub migration 0004; this
module calls them with the actor, maps a refusal to a plain CommandError and records a denied audit row (the
function writes the committed one). Nothing here writes readiness evidence, an authorization, a hold, custody
or eligibility: a charging session is never departure battery evidence (8.3).

FORECAST ("Estimated at departure": Enough / Needs charge / Unknown). Facts only, no invented percentage:

* Last recorded = the newest of the robot's recorded readings (robot_soc_reading: manual readings and ends of
  charging sessions) and its non-voided battery-check evidence (read in place, source "battery_check").
* Fresh while younger than the owner-provisioned ``ops_soc_fresh_min``; missing setting -> Unknown.
* The minimum is the approved capability profile's ``min_departure_soc_pct``; unknown -> Unknown.
* Needs charge: a fresh reading below the minimum. "Cannot charge in time" is claimed ONLY when the approved
  ``charge_turnaround_min`` is known and the time left before departure (from now, or from the start of a
  session already running) is shorter than it. That is the only case Suggest excludes a robot.
* Enough: a fresh reading at or above the minimum and no other visit of the robot between the reading and this
  departure. With another visit in between the drain is not known: Unknown (no discharge model is invented).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from fleet.models import Robot
from hub.models import Charger
from planning.models import Visit
from platform_core.commands import (
    CommandError,
    Conflict,
    Invalid,
    NotAuthenticated,
    Outcome,
    PermissionDenied,
    run_command,
)
from platform_core.middleware import current_request_id
from platform_core.models import AuditEvent, Hub
from platform_core.templatetags.ui import localize_utc_text

MANAGE_CHARGERS = "hub.manage_chargers"
RESERVE = "hub.reserve_charger"
RECORD = "hub.record_charging"
ENOUGH, NEEDS, UNKNOWN = "enough", "needs_charge", "unknown"
LABELS = {ENOUGH: "Enough", NEEDS: "Needs charge", UNKNOWN: "Unknown"}
SOURCE_WORDS = {
    "battery_check": "battery check",
    "charging_session_end": "end of a charging session",
    "manual_reading": "reading entered by hand",
}


# ------------------------------------------------------------------ chargers (configuration)
def create_charger(actor: Any, *, code: str, name: str, note: str = "") -> dict[str, Any]:
    code, name, note = code.strip().upper(), name.strip(), note.strip()
    payload = {"code": code, "name": name, "note": note}

    def handler() -> Outcome:
        if not code or len(code) > 12 or not all(c.isalnum() or c == "-" for c in code):
            raise Invalid("The charger code uses letters, digits and dashes, up to 12 characters")
        if not name or len(name) > 80 or len(note) > 200:
            raise Invalid("Give the charger a name (up to 80 characters)")
        if Charger.objects.filter(code=code).exists():
            raise Conflict(f"A charger with code {code} already exists")
        ch = Charger.objects.create(code=code, name=name, note=note, hub=Hub.objects.order_by("id").first(),
                                    created_by=actor)  # fmt: skip
        return Outcome("charger", ch.code, {"in_service": True})

    return run_command(
        actor, "hub.create_charger", permission=MANAGE_CHARGERS, payload=payload, handler=handler
    )


def set_charger_in_service(actor: Any, *, code: str, in_service: bool, reason: str) -> dict[str, Any]:
    reason = reason.strip()
    payload = {"code": code, "in_service": in_service, "reason": reason}

    def handler() -> Outcome:
        if not reason or len(reason) > 200:
            raise Invalid("Give a short reason (up to 200 characters)")
        ch = Charger.objects.select_for_update().filter(code=code).first()
        if ch is None:
            raise Invalid(f"No charger {code}")
        if ch.in_service == in_service:
            raise Conflict(f"Charger {code} is already {'in service' if in_service else 'out of service'}")
        ch.in_service = in_service
        ch.save(update_fields=["in_service"])
        return Outcome("charger", ch.code, {"in_service": in_service})

    return run_command(
        actor, "hub.set_charger_in_service", permission=MANAGE_CHARGERS, payload=payload, handler=handler
    )


# ------------------------------------------------------------ guarded writes (SECURITY DEFINER functions)
def _guarded(actor: Any, command: str, sql: str, params: list[Any], payload: dict[str, Any]) -> Any:
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise NotAuthenticated("Authentication required")
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, [actor.pk, *params, current_request_id.get()])
            row = cur.fetchone()
    except DatabaseError as exc:
        cause = exc.__cause__
        state = getattr(cause, "sqlstate", None)
        text = getattr(getattr(cause, "diag", None), "message_primary", None) or str(exc)
        if state == "42501":
            err: CommandError = PermissionDenied(text)
        elif state in ("23514", "23P01", "23505", "23503"):
            if state == "23P01":
                text = "Someone booked that charger or robot at the same moment; nothing was changed"
            err = Conflict(localize_utc_text(text))
        else:
            raise
        AuditEvent.objects.create(
            actor=actor, command=command, outcome=AuditEvent.OUTCOME_DENIED, payload=payload, reason=text,
            request_id=current_request_id.get(),
        )  # fmt: skip
        raise err from exc
    return row[0] if row else None


def _robot(code: str) -> Robot:
    r = Robot.objects.filter(code=code.strip().upper()).first()
    if r is None:
        raise Invalid(f"No robot {code}")
    return r


def _charger(code: str) -> Charger:
    c = Charger.objects.filter(code=code.strip().upper()).first()
    if c is None:
        raise Invalid(f"No charger {code}")
    return c


def reserve_charger(
    actor: Any, *, charger_code: str, robot_code: str, starts_at: datetime, ends_at: datetime,
    visit_code: str | None = None,
) -> int:  # fmt: skip
    ch, rb = _charger(charger_code), _robot(robot_code)
    visit_id = None
    if visit_code:
        v = Visit.objects.filter(code=visit_code).first()
        if v is None:
            raise Invalid(f"No visit {visit_code}")
        visit_id = v.pk
    payload = {"charger": ch.code, "robot": rb.code, "starts_at": starts_at.isoformat(),
               "ends_at": ends_at.isoformat(),
               "visit": visit_code}  # fmt: skip
    return int(
        _guarded(
            actor, "hub.reserve_charger", "SELECT hub_reserve_charger(%s, %s, %s, %s, %s, %s, %s)",
            [ch.pk, rb.pk, starts_at, ends_at, visit_id], payload,
        )
    )  # fmt: skip


def release_reservation(actor: Any, *, reservation_id: int, reason: str) -> None:
    _guarded(
        actor, "hub.release_charger_reservation", "SELECT hub_release_charger_reservation(%s, %s, %s, %s)",
        [reservation_id, reason], {"reservation_id": reservation_id, "reason": reason},
    )  # fmt: skip


def start_charging(
    actor: Any,
    *,
    charger_code: str,
    robot_code: str,
    planned_until: datetime,
    start_soc_pct: int | None = None,
) -> int:
    ch, rb = _charger(charger_code), _robot(robot_code)
    payload = {"charger": ch.code, "robot": rb.code, "start_soc_pct": start_soc_pct,
               "planned_until": planned_until.isoformat()}  # fmt: skip
    return int(
        _guarded(
            actor, "hub.start_charging", "SELECT hub_start_charging(%s, %s, %s, %s, %s, %s)",
            [ch.pk, rb.pk, start_soc_pct, planned_until], payload,
        )
    )  # fmt: skip


def end_charging(actor: Any, *, session_id: int, end_soc_pct: int | None = None) -> None:
    _guarded(
        actor, "hub.end_charging", "SELECT hub_end_charging(%s, %s, %s, %s)", [session_id, end_soc_pct],
        {"session_id": session_id, "end_soc_pct": end_soc_pct},
    )  # fmt: skip


def record_soc(actor: Any, *, robot_code: str, soc_pct: int, observed_at: datetime | None = None) -> int:
    rb = _robot(robot_code)
    payload = {
        "robot": rb.code,
        "soc_pct": soc_pct,
        "observed_at": observed_at.isoformat() if observed_at else None,
    }
    return int(
        _guarded(
            actor, "hub.record_soc_reading", "SELECT hub_record_soc_reading(%s, %s, %s, %s, %s)",
            [rb.pk, soc_pct, observed_at], payload,
        )
    )  # fmt: skip


# ------------------------------------------------------------------ the forecast (one statement)
FORECAST_SQL = r"""
WITH inp AS MATERIALIZED (%(inputs)s),
fresh AS MATERIALIZED (
  SELECT (SELECT CASE WHEN value ~ '^[0-9]{1,7}$' AND value::int > 0 THEN value::int END
          FROM fleet_deploymentsetting WHERE key = 'ops_soc_fresh_min') AS minutes
),
rd AS MATERIALIZED (
  SELECT DISTINCT ON (x.robot_id) x.robot_id, x.soc, x.observed_at, x.source
  FROM (
    SELECT s.robot_id, s.soc_pct::int AS soc, s.observed_at, s.source
    FROM robot_soc_reading s WHERE s.robot_id IN (SELECT robot_id FROM inp)
    UNION ALL
    SELECT e.robot_id,
           CASE WHEN (e.details ->> 'soc_pct') ~ '^[0-9]{1,3}$' THEN (e.details ->> 'soc_pct')::int END,
           e.observed_at, 'battery_check'
    FROM readiness_readinessevidence e
    WHERE e.kind = 'battery_check' AND e.robot_id IN (SELECT robot_id FROM inp)
      AND NOT EXISTS (SELECT 1 FROM readiness_readinessevidencevoid vd WHERE vd.evidence_id = e.id)
  ) x
  WHERE x.soc IS NOT NULL AND x.observed_at <= %%(at)s
  ORDER BY x.robot_id, x.observed_at DESC
),
prof AS MATERIALIZED (
  SELECT r.id AS robot_id, ap.min_departure_soc_pct AS min_soc, ap.charge_turnaround_min AS turnaround
  FROM fleet_robot r
  JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
  LEFT JOIN LATERAL (
    SELECT cp.* FROM fleet_capabilityprofile cp
    WHERE cp.pattern_id = m.pattern_id
      AND (SELECT array_agg(DISTINCT a.slot::text ORDER BY a.slot::text) FROM fleet_capabilityapproval a
           WHERE a.profile_id = cp.id) = ARRAY['engineering', 'safety']
    ORDER BY cp.version_no DESC LIMIT 1
  ) ap ON true
  WHERE r.id IN (SELECT robot_id FROM inp)
),
ses AS MATERIALIZED (
  SELECT s.robot_id, min(s.started_at) AS started_at FROM hub_charging_session s
  WHERE s.ended_at IS NULL AND s.robot_id IN (SELECT robot_id FROM inp) GROUP BY s.robot_id
)
SELECT inp.robot_id, inp.visit_id, inp.departure_at, rd.soc, rd.observed_at, rd.source, f.minutes,
       prof.min_soc, prof.turnaround, ses.started_at,
       EXISTS (
         SELECT 1 FROM planning_reservation pr
         JOIN planning_resource res
           ON res.id = pr.resource_id AND res.kind = 'robot' AND res.robot_id = inp.robot_id
         JOIN planning_visitrevision vr ON vr.id = pr.revision_id
         WHERE pr.released_at IS NULL AND pr.purpose = 'on_site' AND rd.observed_at IS NOT NULL
           AND (inp.visit_id IS NULL OR vr.visit_id <> inp.visit_id)
           AND pr.starts_at > rd.observed_at AND pr.starts_at < inp.departure_at
       ) AS used_between
FROM inp
CROSS JOIN fresh f
LEFT JOIN rd ON rd.robot_id = inp.robot_id
LEFT JOIN prof ON prof.robot_id = inp.robot_id
LEFT JOIN ses ON ses.robot_id = inp.robot_id
"""

VISIT_INPUTS = """
  SELECT vr.robot_id, v.id AS visit_id,
         vr.on_site_start - make_interval(mins => coalesce(vr.travel_out_min, 0)) AS departure_at
  FROM planning_visit v JOIN planning_visitrevision vr ON vr.id = v.current_revision_id
  WHERE v.id = ANY(%(visit_ids)s) AND vr.robot_id IS NOT NULL
"""
ROBOT_INPUTS = """
  SELECT r.id AS robot_id, NULL::bigint AS visit_id, %(departure)s::timestamptz AS departure_at
  FROM fleet_robot r WHERE r.id = ANY(%(robot_ids)s)
"""


@dataclass
class Forecast:
    robot_id: int
    visit_id: int | None
    departure_at: datetime
    state: str
    reason: str
    soc: int | None = None
    observed_at: datetime | None = None
    source: str | None = None
    min_soc: int | None = None
    cannot_in_time: bool = False
    fresh_min: int | None = None

    @property
    def label(self) -> str:
        return LABELS[self.state]

    @property
    def age_text(self) -> str:
        if self.observed_at is None:
            return ""
        minutes = int((timezone.now() - self.observed_at).total_seconds() // 60)
        return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


def _state(row: tuple[Any, ...], at: datetime) -> Forecast:
    robot_id, visit_id, departure, soc, observed, source, fresh, min_soc, turnaround, session_start, used = (
        row
    )
    f = Forecast(robot_id, visit_id, departure, UNKNOWN, "", soc, observed, source, min_soc, False, fresh)
    if fresh is None:
        f.reason = "the battery freshness setting (ops_soc_fresh_min) is not provisioned"
    elif min_soc is None:
        f.reason = "the approved capability profile has no departure battery minimum"
    elif soc is None:
        f.reason = "no battery level is recorded"
    elif at - observed > timedelta(minutes=fresh):
        f.reason = "the last battery level is too old to rely on"
    elif soc < min_soc:
        f.state = NEEDS
        f.reason = f"last recorded {soc}% is below the {min_soc}% departure minimum"
        if turnaround and turnaround > 0:
            available = departure - (session_start or at)
            if available < timedelta(minutes=turnaround):
                f.cannot_in_time = True
                f.reason += f"; less time is left than the approved {turnaround}-minute charge turnaround"
    elif used:
        f.reason = "the robot has another visit before this one, and battery use is not known"
    else:
        f.state = ENOUGH
        f.reason = f"last recorded {soc}% meets the {min_soc}% departure minimum"
    return f


def _run(inputs: str, params: dict[str, Any], at: datetime) -> list[Forecast]:
    with connection.cursor() as cur:
        cur.execute(FORECAST_SQL % {"inputs": inputs}, {**params, "at": at})
        return [_state(r, at) for r in cur.fetchall()]


def visit_forecasts(visit_ids: list[int], at: datetime | None = None) -> dict[int, Forecast]:
    """visit id -> forecast for its assigned robot and departure (visits without a robot are left out)."""
    if not visit_ids:
        return {}
    at = at or timezone.now()
    return {f.visit_id: f for f in _run(VISIT_INPUTS, {"visit_ids": list(visit_ids)}, at) if f.visit_id}


def robot_forecasts(
    robot_ids: list[int], departure_at: datetime, at: datetime | None = None
) -> dict[int, Forecast]:
    """robot id -> forecast if that robot left at ``departure_at`` (planner options and Suggest)."""
    if not robot_ids:
        return {}
    at = at or timezone.now()
    return {
        f.robot_id: f
        for f in _run(ROBOT_INPUTS, {"robot_ids": list(robot_ids), "departure": departure_at}, at)
    }


def forecast_configured() -> bool:
    from fleet.services import setting

    raw = setting("ops_soc_fresh_min") or ""
    return raw.isdigit() and int(raw) > 0


# ------------------------------------------------------------------ the robot page battery panel
def robot_battery_panel(robot: Robot) -> dict[str, Any]:
    """Three separate things (8.3): Last recorded; Estimated at departure (next visit); Departure evidence."""
    now = timezone.now()
    nxt = (
        Visit.objects.filter(current_revision__robot=robot, current_revision__on_site_start__gt=now)
        .exclude(status="cancelled")
        .select_related("current_revision")
        .order_by("current_revision__on_site_start")
        .first()
    )
    if nxt is not None:
        f = visit_forecasts([nxt.pk], now).get(nxt.pk)
    else:
        f = robot_forecasts([robot.pk], now, now).get(robot.pk)
    return {
        "forecast": f,
        "next_visit": nxt,
        "source_words": SOURCE_WORDS.get(f.source or "", "") if f else "",
    }
