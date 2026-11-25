"""Slice 2 commands and the planning-feasibility engine.

Feasibility words are deliberately limited to: Planning feasible / Blocked / Requires review. Nothing here
means "cleared to depart". Unknown physical facts (travel, loading, inspection or charging durations,
vehicle capacity, restraint kit, adult count) are blockers or review items, never estimated.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from fleet import services as fleet_services
from fleet.models import CapabilityProfile, ManifestPattern, Robot
from planning.models import (
    AssessmentApproval,
    ConsentRecord,
    Person,
    Qualification,
    Reservation,
    Resource,
    Site,
    SiteAssessment,
    TripCargo,
    Vehicle,
    VehicleTrip,
    Visit,
    VisitRevision,
)
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, RetryTransaction, run_command
from platform_core.hubscope import owner_hub
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint

User = get_user_model()

LABELS = {"feasible": "Planning feasible", "blocked": "Blocked", "review": "Requires review"}
NOT_DEPARTURE = "Planning only - not cleared to depart"


# ------------------------------------------------------------------ small helpers
def _site(code: str) -> Site:
    try:
        return Site.objects.get(code=code)
    except Site.DoesNotExist:
        raise Invalid(f"No site {code}") from None


def _visit(code: str) -> Visit:
    try:
        return Visit.objects.get(code=code)
    except Visit.DoesNotExist:
        raise Invalid(f"No visit {code}") from None


def _person(code: str | None) -> Person | None:
    if not code:
        return None
    try:
        return Person.objects.get(code=code)
    except Person.DoesNotExist:
        raise Invalid(f"No person {code}") from None


def _vehicle(code: str | None) -> Vehicle | None:
    if not code:
        return None
    try:
        return Vehicle.objects.get(code=code)
    except Vehicle.DoesNotExist:
        raise Invalid(f"No vehicle {code}") from None


def _robot(code: str | None) -> Robot | None:
    if not code:
        return None
    try:
        return Robot.objects.get(code=code)
    except Robot.DoesNotExist:
        raise Invalid(f"No robot {code}") from None


def _int_setting(key: str) -> int | None:
    """A positive whole number of minutes, or None (unknown/invalid -> the caller blocks)."""
    v = fleet_services.setting(key)
    try:
        n = int(v) if v else None
    except ValueError:
        return None
    return n if n and n > 0 else None


def _positive(name: str, value: int | None) -> None:
    if value is not None and value <= 0:
        raise Invalid(f"{name} must be a positive number of minutes")


def local_to_utc(naive: datetime, tzname: str) -> datetime:
    """Convert a site-local wall time to UTC, refusing times that do not exist (spring-forward gap) or are
    ambiguous (fall-back hour) instead of guessing."""
    tz = ZoneInfo(tzname)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    # A wall time inside the spring-forward gap does not survive a round trip through UTC.
    roundtrip = first.astimezone(ZoneInfo("UTC")).astimezone(tz).replace(tzinfo=None)
    if roundtrip != naive:
        raise Invalid(f"{naive:%Y-%m-%d %H:%M} does not exist in {tzname} (clocks spring forward)")
    # A wall time in the fall-back hour exists twice, with two different offsets.
    if first.utcoffset() != second.utcoffset():
        raise Invalid(f"{naive:%Y-%m-%d %H:%M} is ambiguous in {tzname} (clocks fall back); give a UTC time")
    return first.astimezone(ZoneInfo("UTC"))


def _robot_resource(robot: Robot) -> Resource:
    res, _ = Resource.objects.get_or_create(kind="robot", robot=robot)
    return res


# ------------------------------------------------------------------ feasibility engine
@dataclass
class Plan:
    feasibility: str
    blockers: list[dict[str, str]]
    snapshot: dict[str, Any]
    intervals: dict[str, list[tuple[str, datetime, datetime]]] = field(default_factory=dict)
    trip: tuple[datetime, datetime] | None = None


def _add(blockers: list[dict[str, str]], level: str, code: str, message: str, action: str) -> None:
    blockers.append({"level": level, "code": code, "message": message, "action": action})


def current_assessment(site: Site, on: date) -> SiteAssessment | None:
    superseded = set(
        SiteAssessment.objects.filter(site=site, supersedes__isnull=False).values_list(
            "supersedes_id", flat=True
        )
    )
    for a in site.assessments.order_by("-assessed_at"):
        if a.pk in superseded:
            continue
        if hasattr(a, "approval") and a.valid_until >= on:
            return a
    return None


def consent_status(site: Site, required_scopes: list[str]) -> tuple[str, str]:
    """('ok' | 'review' | 'blocked', explanation). A count alone never establishes validity: each consent
    must be verified, current-form, not withdrawn, and cover every scope the visit needs."""
    form = fleet_services.setting("consent_form_version")
    if not form:
        return "blocked", "No current consent form version is configured"
    if site.expected_adults is None:
        return (
            "review",
            "Number of adults in the household is unknown, so consent cannot be verified complete",
        )
    valid = [
        c
        for c in site.consents.filter(withdrawn_at__isnull=True, form_version=form)
        if set(required_scopes) <= set(c.scope)
    ]
    if len(valid) < site.expected_adults:
        return "blocked", (
            f"{len(valid)} of {site.expected_adults} adults have valid {form} consent covering "
            f"{', '.join(required_scopes) or 'presence'}"
        )
    return "ok", f"{len(valid)} of {site.expected_adults} adults verified on {form}"


def _qualified(person: Person, kind: str, start: datetime, end: datetime) -> bool:
    """Valid through the WHOLE activity, not just at booking time."""
    return Qualification.objects.filter(
        person=person, kind=kind, revoked_at__isnull=True, valid_from__lte=start, valid_to__gte=end
    ).exists()


def evaluate(
    *,
    site: Site,
    start: datetime,
    end: datetime,
    travel_out: int | None,
    travel_back: int | None,
    tasks: list[str],
    scopes: list[str],
    robot: Robot | None,
    lead: Person | None,
    second: Person | None,
    vehicle: Vehicle | None,
    visit: Visit | None = None,
    periodic: fleet_services.PeriodicStatus | None = None,
    maintenance: list[str] | None = None,
) -> Plan:
    """``periodic``: an already computed X-3 periodic status of ``robot`` in the current mode, reused for its
    eligibility instead of reading it again (readiness evaluator; see fleet.services.evaluate_eligibility).
    ``maintenance``: likewise, the robot's already read blocking maintenance reasons (v2 02C)."""
    b: list[dict[str, str]] = []
    snap: dict[str, Any] = {
        "evaluated_at": timezone.now().isoformat(),
        "site_version": site.version,
        "note": NOT_DEPARTURE,
    }

    # --- site assessment
    a = current_assessment(site, start.astimezone(ZoneInfo(site.timezone)).date())
    if a is None:
        _add(
            b,
            "blocked",
            "SITE_NOT_ASSESSED",
            "No approved, current home assessment covers this date",
            "Record and approve an assessment",
        )
    else:
        snap["assessment_id"] = a.pk
        if a.decision == "not_eligible":
            _add(b, "blocked", "SITE_NOT_ELIGIBLE", "The home was assessed as not eligible", "Do not book")
        elif a.decision == "eligible_restricted":
            _add(
                b,
                "review",
                "SITE_RESTRICTED",
                f"Restrictions apply: {a.restrictions}",
                "Review the restrictions",
            )
        excluded = sorted(set(tasks) & set(a.excluded_tasks))
        if excluded:
            _add(
                b,
                "blocked",
                "TASK_EXCLUDED",
                f"Tasks excluded at this home: {', '.join(excluded)}",
                "Remove the tasks",
            )
    # --- consent
    level, why = consent_status(site, scopes)
    if level != "ok":
        _add(b, level, "CONSENT", why, "Verify consent for every adult")
    snap["consent"] = why

    # --- unknown durations
    insp = _int_setting("pre_visit_inspection_min")
    if travel_out is None or travel_back is None:
        _add(b, "blocked", "UNKNOWN_TRAVEL", "Travel time is unknown", "Enter travel time out and back")
    if insp is None:
        _add(
            b,
            "blocked",
            "UNKNOWN_INSPECTION_TIME",
            "Pre-visit inspection duration is not configured",
            "Ops lead sets pre_visit_inspection_min",
        )

    # --- robot
    load = charge = None
    if robot is None:
        _add(b, "blocked", "ROBOT_MISSING", "No robot assigned", "Assign a planning-eligible robot")
    else:
        e = fleet_services.evaluate_eligibility(robot, periodic=periodic, maintenance=maintenance)
        snap.update(
            {
                "robot": robot.code,
                "robot_version": robot.version,
                "robot_eligible": e.eligible_for_planning,
                "robot_reasons": e.reasons,
                "manifest_id": e.manifest_id,
            }
        )
        if not e.eligible_for_planning:
            _add(
                b,
                "blocked",
                "ROBOT_NOT_ELIGIBLE",
                f"{robot.code}: " + "; ".join(e.reasons),
                "Choose another robot or resolve its holds",
            )
        # v2 03A-2 (10.A): a verified hub location makes the robot available only at THAT hub (planning/0011)
        if visit is not None and e.location is not None and e.location.at_verified_hub:
            if not e.location.dispatchable_at(visit.hub_id):
                _add(
                    b,
                    "blocked",
                    "ROBOT_AT_ANOTHER_HUB",
                    f"{robot.code} is at hub {e.location.ref}, not at this visit's hub",
                    "Choose a robot at this hub, or move this one here first",
                )
        if e.pattern_code:
            pattern = ManifestPattern.objects.get(code=e.pattern_code)
            snap["pattern_version"] = pattern.version
            prof: CapabilityProfile | None = fleet_services.approved_profile(pattern)
            if prof is not None:
                load = prof.load_unload_min if prof.load_unload_min and prof.load_unload_min > 0 else None
                charge = (
                    prof.charge_turnaround_min
                    if prof.charge_turnaround_min and prof.charge_turnaround_min > 0
                    else None
                )
                snap["capability_profile_id"] = prof.pk
        if load is None:
            _add(
                b,
                "blocked",
                "UNKNOWN_LOAD_TIME",
                "Loading/unloading time for this robot is unknown",
                "Engineering publishes load_unload_min",
            )
        if charge is None:
            _add(
                b,
                "blocked",
                "UNKNOWN_CHARGE_TIME",
                "Charge turnaround for this robot is unknown",
                "Engineering publishes charge_turnaround_min",
            )

    # --- people
    if lead is None or second is None:
        _add(b, "blocked", "CREW_INCOMPLETE", "Two people are required (lead + second)", "Assign both")
    elif lead.pk == second.pk:
        _add(b, "blocked", "CREW_SAME_PERSON", "Lead and second must be different people", "Pick two people")

    # --- vehicle
    if vehicle is None:
        _add(b, "blocked", "VEHICLE_MISSING", "No vehicle assigned", "Assign a vehicle")
    else:
        if vehicle.robot_capacity is None:
            _add(
                b,
                "blocked",
                "VEHICLE_CAPACITY_UNKNOWN",
                f"{vehicle.code} robot capacity is unknown",
                "Record the vehicle's validated robot capacity",
            )
        if not vehicle.restraint_kit:
            _add(
                b,
                "blocked",
                "VEHICLE_RESTRAINT_UNKNOWN",
                f"{vehicle.code} has no recorded restraint kit",
                "Record the restraint kit",
            )

    plan = Plan("feasible", b, snap)
    if None in (travel_out, travel_back, insp, load, charge):
        plan.feasibility = "blocked"
        return plan
    assert travel_out is not None and travel_back is not None and insp is not None
    assert load is not None and charge is not None
    depart = start - timedelta(minutes=travel_out)
    load_start = depart - timedelta(minutes=load)
    insp_start = load_start - timedelta(minutes=insp)
    back = end + timedelta(minutes=travel_back)
    unload_end = back + timedelta(minutes=load)
    charge_end = unload_end + timedelta(minutes=charge)
    snap["timeline_utc"] = {
        "inspection": insp_start.isoformat(),
        "depart": depart.isoformat(),
        "return": back.isoformat(),
        "robot_free": charge_end.isoformat(),
    }

    if robot is not None:
        plan.intervals["robot"] = [
            ("inspection", insp_start, load_start),
            ("loading", load_start, depart),
            ("travel_out", depart, start),
            ("on_site", start, end),
            ("travel_back", end, back),
            ("unloading", back, unload_end),
            ("charging", unload_end, charge_end),
        ]
    for role, p in (("lead", lead), ("second", second)):
        if p is None:
            continue
        need = ["field_lead"] if role == "lead" else ["field_operator", "field_lead"]
        if not any(_qualified(p, k, insp_start, unload_end) for k in need):
            _add(
                b,
                "blocked",
                "QUALIFICATION",
                f"{p.code} is not qualified as {need[0].replace('_', ' ')} for the whole activity",
                "Grant or renew the qualification, or pick someone else",
            )
        if not _qualified(p, "robot_handler", load_start, unload_end):
            _add(
                b,
                "blocked",
                "QUALIFICATION",
                f"{p.code} is not qualified for robot handling during the activity",
                "Grant robot handling, or pick someone else",
            )
        plan.intervals[f"person:{p.pk}"] = [("field_shift", insp_start, unload_end)]
    if vehicle is not None:
        plan.intervals["vehicle"] = [("trip", load_start, unload_end)]
        plan.trip = (depart, back)

    # --- conflicts with existing reservations (the exclusion constraint is the final guard)
    targets: list[tuple[Resource | None, list[tuple[str, datetime, datetime]]]] = []
    if robot is not None:
        targets.append((Resource.objects.filter(kind="robot", robot=robot).first(), plan.intervals["robot"]))
    for p in (lead, second):
        if p is not None:
            targets.append(
                (Resource.objects.filter(kind="person", person=p).first(), plan.intervals[f"person:{p.pk}"])
            )
    if vehicle is not None:
        targets.append(
            (Resource.objects.filter(kind="vehicle", vehicle=vehicle).first(), plan.intervals["vehicle"])
        )
    for res, spans in targets:
        if res is None:
            continue
        for purpose, s0, s1 in spans:
            clash = (
                Reservation.objects.filter(
                    resource=res, released_at__isnull=True, starts_at__lt=s1, ends_at__gt=s0
                )
                .exclude(revision__visit=visit)
                .select_related("revision__visit")
                .first()
            )
            if clash:
                _add(
                    b,
                    "blocked",
                    "CONFLICT",
                    f"{res} is already booked for {clash.revision.visit.code} "
                    f"({clash.get_purpose_display()}) "
                    f"{clash.starts_at:%Y-%m-%d %H:%M}-{clash.ends_at:%H:%M} UTC, "
                    f"overlapping {purpose}",
                    "Pick another time or resource",
                )
                break
    levels = {x["level"] for x in b}
    plan.feasibility = "blocked" if "blocked" in levels else ("review" if "review" in levels else "feasible")
    return plan


# ------------------------------------------------------------------ sites, assessments, consent
def create_site(
    actor: Any, *, code: str, zone: str, tz: str, crm_ref: str, expected_adults: int | None = None
) -> dict[str, Any]:
    def handler() -> Outcome:
        if expected_adults is not None and expected_adults < 1:
            raise Invalid("Expected adults must be at least 1, or unknown")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise Invalid(f"Unknown IANA timezone {tz}") from None
        if Site.objects.filter(code=code).exists():
            raise Conflict(f"Site {code} exists")
        s = Site.objects.create(
            code=code,
            zone=zone,
            timezone=tz,
            crm_ref=crm_ref,
            expected_adults=expected_adults,
            created_by=actor,
        )
        return Outcome("site", s.code)

    return run_command(
        actor,
        "planning.create_site",
        permission="planning.manage_sites",
        payload={
            "code": code,
            "zone": zone,
            "tz": tz,
            "crm_ref": crm_ref,
            "expected_adults": expected_adults,
        },
        handler=handler,
    )


def set_expected_adults(actor: Any, *, site_code: str, expected_adults: int | None) -> dict[str, Any]:
    def handler() -> Outcome:
        if expected_adults is not None and expected_adults < 1:
            raise Invalid("Expected adults must be at least 1, or unknown")
        s = _site(site_code)
        lock_guards(update_sites=[s.pk], label="planning.set_expected_adults")
        s.refresh_from_db()
        s.expected_adults = expected_adults
        s.save(update_fields=["expected_adults", "version"])
        return Outcome("site", s.code, {"expected_adults": expected_adults})

    return run_command(
        actor,
        "planning.set_expected_adults",
        permission="planning.manage_sites",
        payload={"site": site_code, "expected_adults": expected_adults},
        handler=handler,
    )


def record_assessment(
    actor: Any,
    *,
    site_code: str,
    decision: str,
    valid_until: date,
    restrictions: str = "",
    excluded_tasks: list[str] | None = None,
    supersedes_id: int | None = None,
) -> dict[str, Any]:
    def handler() -> Outcome:
        if decision not in dict(SiteAssessment.DECISIONS):
            raise Invalid(f"Unknown decision {decision}")
        s = _site(site_code)
        lock_guards(update_sites=[s.pk], label="planning.record_assessment")
        a = SiteAssessment.objects.create(
            site=s,
            decision=decision,
            valid_until=valid_until,
            restrictions=restrictions,
            excluded_tasks=excluded_tasks or [],
            supersedes_id=supersedes_id,
            assessed_by=actor,
        )
        return Outcome("site_assessment", str(a.pk), {"site": s.code, "decision": decision})

    return run_command(
        actor,
        "planning.record_assessment",
        permission="planning.assess",
        payload={
            "site": site_code,
            "decision": decision,
            "valid_until": str(valid_until),
            "restrictions": restrictions,
            "excluded_tasks": excluded_tasks or [],
            "supersedes_id": supersedes_id,
        },
        handler=handler,
    )


def approve_assessment(actor: Any, *, assessment_id: int) -> dict[str, Any]:
    def handler() -> Outcome:
        a = SiteAssessment.objects.get(pk=assessment_id)
        lock_guards(update_sites=[a.site_id], label="planning.approve_assessment")
        if a.assessed_by_id == actor.pk:
            raise PermissionDenied("The assessor cannot approve their own assessment")
        if hasattr(a, "approval"):
            raise Conflict("Already approved")
        AssessmentApproval.objects.create(assessment=a, approver=actor)
        return Outcome("site_assessment", str(a.pk), {"approved": True})

    return run_command(
        actor,
        "planning.approve_assessment",
        permission="planning.approve_assessment",
        payload={"assessment_id": assessment_id},
        handler=handler,
    )


def record_consent(
    actor: Any,
    *,
    site_code: str,
    participant_ref: str,
    form_version: str,
    scope: list[str],
    evidence_ref: str,
) -> dict[str, Any]:
    def handler() -> Outcome:
        if not scope or set(scope) - set(ConsentRecord.SCOPES):
            raise Invalid(f"Scope must be a non-empty subset of {ConsentRecord.SCOPES}")
        if not evidence_ref.strip():
            raise Invalid("A reference to the signed form is required")
        s = _site(site_code)
        lock_guards(update_sites=[s.pk], label="planning.record_consent")
        if s.consents.filter(participant_ref=participant_ref, withdrawn_at__isnull=True).exists():
            raise Conflict(f"{participant_ref} already has an active consent; withdraw it first")
        c = ConsentRecord.objects.create(
            site=s,
            participant_ref=participant_ref,
            form_version=form_version,
            scope=sorted(scope),
            evidence_ref=evidence_ref,
            verified_by=actor,
        )
        return Outcome("consent", str(c.pk), {"site": s.code, "participant": participant_ref})

    return run_command(
        actor,
        "planning.record_consent",
        permission="planning.consent",
        payload={
            "site": site_code,
            "participant_ref": participant_ref,
            "form_version": form_version,
            "scope": scope,
            "evidence_ref": evidence_ref,
        },
        handler=handler,
    )


def withdraw_consent(actor: Any, *, consent_id: int, reason: str) -> dict[str, Any]:
    """Immediately effective: feasibility is evaluated live and every planning transition re-checks it."""

    def handler() -> Outcome:
        c = ConsentRecord.objects.get(pk=consent_id)
        lock_guards(update_sites=[c.site_id], label="planning.withdraw_consent")
        c.refresh_from_db()
        if c.withdrawn_at is not None:
            raise Conflict("Already withdrawn")
        c.withdrawn_at, c.withdrawn_by = timezone.now(), actor
        c.save(update_fields=["withdrawn_at", "withdrawn_by"])
        return Outcome("consent", str(c.pk), {"withdrawn": True, "reason": reason})

    return run_command(
        actor,
        "planning.withdraw_consent",
        permission="planning.consent",
        payload={"consent_id": consent_id, "reason": reason},
        handler=handler,
    )


# ------------------------------------------------------------------ people and vehicles
def register_person(actor: Any, *, username: str, code: str, hub_code: str | None = None) -> dict[str, Any]:
    """Register a person at their HOME hub (v2 03A-1). The home hub confers no authority: roles come only from
    hub-qualified grants."""
    hub = owner_hub(hub_code)
    payload: dict[str, Any] = {"username": username, "code": code}
    if hub is not None:
        payload["hub"] = hub.code

    def handler() -> Outcome:
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise Invalid(f"No user {username}") from None
        if Person.objects.filter(code=code).exists() or Person.objects.filter(user=user).exists():
            raise Conflict("Person already registered")
        p = Person.objects.create(user=user, code=code, **({"hub": hub} if hub is not None else {}))
        Resource.objects.create(kind="person", person=p)
        return Outcome("person", p.code)

    return run_command(
        actor,
        "planning.register_person",
        permission="planning.manage_people",
        payload=payload,
        handler=handler,
        hub=hub,
    )


INSPECTOR_KIND = "readiness_inspector"


MAINTAINER_KIND = "maintenance_technician"
# Kinds a safety reviewer grants (never to oneself, never backdated, with a training record):
# readiness_inspector (07a section 7.2) and, v2 02C, maintenance_technician (contract 11, 8.6).
SAFETY_GRANTED_KINDS = (INSPECTOR_KIND, MAINTAINER_KIND)


def _qualification_permission(kind: str, *, revoke: bool) -> str:
    """readiness_inspector and maintenance_technician have their own grant / revoke permissions (07a section
    7.2, EM decision 18; contract 11, 8.6)."""
    if kind in SAFETY_GRANTED_KINDS:
        return (
            "planning.revoke_inspector_qualification" if revoke else "planning.grant_inspector_qualification"
        )
    return "planning.manage_people"


def grant_qualification(
    actor: Any,
    *,
    person_code: str,
    kind: str,
    valid_from: datetime | None,
    valid_to: datetime,
    basis_ref: str = "",
) -> dict[str, Any]:
    """Grant a qualification (person U). For readiness_inspector (X-8): safety_reviewer only, never to
    oneself, a non-blank training-record reference, and no backdating. ``valid_from=None`` means "effective
    now": the command sends NULL and the database sets valid_from = granted_at = one clock_timestamp()
    (P1-6), so the app clock never decides the start. Other kinds keep an explicit window."""

    def handler() -> Outcome:
        if kind not in dict(Qualification.KINDS):
            raise Invalid(f"Unknown qualification {kind}")
        if valid_from is None and kind not in SAFETY_GRANTED_KINDS:
            raise Invalid("valid_from is required for this qualification")
        for name, value in (("valid_from", valid_from), ("valid_to", valid_to)):
            if value is not None and value.tzinfo is None:
                raise Invalid(f"{name} must be timezone-aware")
        p = _person(person_code)
        assert p is not None
        lock_guards(update_persons=[p.pk], label="planning.grant_qualification")
        if kind in SAFETY_GRANTED_KINDS:
            if p.user_id == actor.pk:
                raise Conflict(f"A {kind} qualification cannot be granted to oneself")
            if not basis_ref.strip():
                raise Invalid(f"A {kind} qualification needs a training record reference (basis_ref)")
            now = _db_now()
            if valid_from is not None and valid_from < now:
                raise Invalid(
                    f"A {kind} qualification cannot be backdated; "
                    "leave valid_from empty for an immediate grant"
                )
            if valid_to <= (valid_from or now):
                raise Invalid("valid_to must be after valid_from")
        q = Qualification.objects.create(
            person=p,
            kind=kind,
            valid_from=valid_from,  # type: ignore[misc]  # None: the insert trigger sets it to granted_at
            valid_to=valid_to,
            granted_by=actor,
            basis_ref=basis_ref,
        )
        q.refresh_from_db()  # granted_at (and an immediate valid_from) are database values
        return Outcome(
            "qualification",
            str(q.pk),
            {
                "person": p.code,
                "kind": kind,
                "valid_from": q.valid_from.isoformat(),
                "granted_at": q.granted_at.isoformat(),
            },
        )

    return run_command(
        actor,
        "planning.grant_qualification",
        permission=_qualification_permission(kind, revoke=False),
        payload={
            "person": person_code,
            "kind": kind,
            "valid_from": valid_from.isoformat() if valid_from is not None else None,
            "valid_to": valid_to.isoformat(),
            "basis_ref": basis_ref,
        },
        handler=handler,
    )


def revoke_qualification(actor: Any, *, qualification_id: int, reason: str) -> dict[str, Any]:
    try:
        kind = Qualification.objects.values_list("kind", flat=True).get(pk=qualification_id)
    except Qualification.DoesNotExist:
        raise Invalid(f"No qualification {qualification_id}") from None

    def handler() -> Outcome:
        q = Qualification.objects.get(pk=qualification_id)
        lock_guards(update_persons=[q.person_id], label="planning.revoke_qualification")
        q.refresh_from_db()
        if q.revoked_at:
            raise Conflict("Already revoked")
        q.revoked_at = timezone.now()
        q.save(update_fields=["revoked_at"])
        return Outcome("qualification", str(q.pk), {"revoked": True})

    return run_command(
        actor,
        "planning.revoke_qualification",
        permission=_qualification_permission(kind, revoke=True),
        payload={"qualification_id": qualification_id, "reason": reason},
        handler=handler,
    )


# ------------------------------------------------------------------ X-8 covered-signer rule (Python mirror)
def _db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        value: datetime = cur.fetchone()[0]
        return value


def inspector_covered(user_id: int | None, at: datetime | None) -> bool:
    """Mirror of planning_inspector_covered(p_user, p_at): an unrevoked readiness_inspector grant of the
    user's Person with valid_from <= at <= valid_to. Expiry after `at` does not matter; revocation does."""
    if user_id is None or at is None:
        return False
    return Qualification.objects.filter(
        person__user_id=user_id,
        kind=INSPECTOR_KIND,
        revoked_at__isnull=True,
        valid_from__lte=at,
        valid_to__gte=at,
    ).exists()


def signer_label(user_id: int) -> str:
    """Person code, else username (the label planning_inspection_signer_problem uses)."""
    code = Person.objects.filter(user_id=user_id).values_list("code", flat=True).first()
    if code:
        return str(code)
    name = User.objects.filter(pk=user_id).values_list("username", flat=True).first()
    return str(name) if name else str(user_id)


def inspection_signer_problem(inspection: Any) -> str | None:
    """Mirror of planning_inspection_signer_problem(p_inspection), same text: both signers covered at the
    inspection's (database) submitted_at. ``inspection`` is a fleet Inspection read from the database (its
    submitted_at is the forced database time), or an inspection id."""
    from fleet.models import Inspection

    i = inspection if isinstance(inspection, Inspection) else Inspection.objects.filter(pk=inspection).first()
    if i is None:
        return f"inspection {inspection} does not exist"
    for uid in (i.performer_id, i.verifier_id):
        if uid is None:
            return f"inspection {i.code} is missing a signer"
        if not inspector_covered(uid, i.submitted_at):
            who = signer_label(uid)
            return f"inspector {who} is not a qualified readiness inspector for inspection {i.code}"
    return None


def register_vehicle(
    actor: Any,
    *,
    code: str,
    robot_capacity: int | None = None,
    restraint_kit: str = "",
    hub_code: str | None = None,
) -> dict[str, Any]:
    hub = owner_hub(hub_code)
    payload: dict[str, Any] = {"code": code, "robot_capacity": robot_capacity, "restraint_kit": restraint_kit}
    if hub is not None:
        payload["hub"] = hub.code

    def handler() -> Outcome:
        if robot_capacity is not None and robot_capacity < 1:
            raise Invalid("Robot capacity must be at least 1, or unknown")
        if Vehicle.objects.filter(code=code).exists():
            raise Conflict(f"Vehicle {code} exists")
        extra = {"hub": hub} if hub is not None else {}
        v = Vehicle.objects.create(
            code=code, robot_capacity=robot_capacity, restraint_kit=restraint_kit, **extra
        )
        Resource.objects.create(kind="vehicle", vehicle=v)
        return Outcome("vehicle", v.code, {"robot_capacity": robot_capacity})

    return run_command(
        actor,
        "planning.register_vehicle",
        permission="planning.manage_vehicles",
        payload=payload,
        handler=handler,
        hub=hub,
    )


# ------------------------------------------------------------------ visits
def _next_visit_code() -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT nextval('planning_visit_code_seq')")
        return f"V-{cur.fetchone()[0]:04d}"


def request_visit(
    actor: Any,
    *,
    site_code: str,
    on_site_start: datetime,
    on_site_end: datetime,
    travel_out_min: int | None = None,
    travel_back_min: int | None = None,
    tasks: list[str] | None = None,
    required_scopes: list[str] | None = None,
    hub_code: str | None = None,
) -> dict[str, Any]:
    hub = owner_hub(hub_code)  # v2 03A-1: the DISPATCH hub (the only hub when none is named)
    payload: dict[str, Any] = {
        "site": site_code,
        "start": on_site_start.isoformat(),
        "end": on_site_end.isoformat(),
        "travel_out_min": travel_out_min,
        "travel_back_min": travel_back_min,
        "tasks": tasks or [],
        "required_scopes": required_scopes or ["presence"],
    }
    if hub is not None:
        payload["hub"] = hub.code

    def handler() -> Outcome:
        if on_site_start.tzinfo is None or on_site_end.tzinfo is None:
            raise Invalid("Times must be timezone-aware (convert site-local times with local_to_utc)")
        if on_site_end <= on_site_start:
            raise Invalid("The visit must end after it starts")
        _positive("Travel out", travel_out_min)
        _positive("Travel back", travel_back_min)
        s = _site(site_code)
        lock_guards(share_sites=[s.pk], label="planning.request_visit")
        v = Visit.objects.create(
            code=_next_visit_code(), site=s, created_by=actor, **({"hub": hub} if hub is not None else {})
        )
        p = evaluate(
            site=s,
            start=on_site_start,
            end=on_site_end,
            travel_out=travel_out_min,
            travel_back=travel_back_min,
            tasks=tasks or [],
            scopes=required_scopes or ["presence"],
            robot=None,
            lead=None,
            second=None,
            vehicle=None,
            visit=v,
        )
        r = VisitRevision.objects.create(
            visit=v,
            revision_no=1,
            on_site_start=on_site_start,
            on_site_end=on_site_end,
            travel_out_min=travel_out_min,
            travel_back_min=travel_back_min,
            tasks=tasks or [],
            required_consent_scopes=required_scopes or ["presence"],
            feasibility=p.feasibility,
            blockers=p.blockers,
            snapshot=p.snapshot,
            reason="requested",
            created_by=actor,
        )
        v.current_revision = r
        v.save(update_fields=["current_revision"])
        return Outcome("visit", v.code, {"feasibility": p.feasibility})

    return run_command(
        actor, "planning.request_visit", permission="planning.plan", payload=payload, handler=handler, hub=hub
    )


def _departed(visit: Visit) -> bool:
    from readiness.models import DepartureEvent  # readiness depends on planning, not the reverse

    return DepartureEvent.objects.filter(visit_id=visit.pk).exists()


def _release_active(visit: Visit, reason: str) -> None:
    """Never for a departed visit (Slice 4 contract C1; the database refuses it too)."""
    if _departed(visit):
        raise Conflict(
            f"Visit {visit.code} has departed; its reservations and trip can no longer be released"
        )
    now = timezone.now()
    for res in Reservation.objects.filter(revision__visit=visit, released_at__isnull=True):
        res.released_at, res.released_reason = now, reason
        res.save(update_fields=["released_at", "released_reason"])
    for trip in VehicleTrip.objects.filter(revision__visit=visit, released_at__isnull=True):
        trip.released_at = now
        trip.save(update_fields=["released_at"])


def plan_visit(
    actor: Any,
    *,
    visit_code: str,
    robot_code: str | None,
    lead_code: str | None,
    second_code: str | None,
    vehicle_code: str | None,
    reason: str = "planned",
    on_site_start: datetime | None = None,
    on_site_end: datetime | None = None,
    travel_out_min: int | None = None,
    travel_back_min: int | None = None,
) -> dict[str, Any]:
    """Assign resources (and optionally a new time). Atomic: old reservations are released and new ones made
    in ONE transaction; if any new reservation fails, nothing changes and the previous plan stays intact.
    A blocked result on an already planned/confirmed visit is refused (old plan kept); on a requested
    visit it is recorded as a revision with its blockers so the operator can see why."""
    payload = {
        "visit": visit_code,
        "robot": robot_code,
        "lead": lead_code,
        "second": second_code,
        "vehicle": vehicle_code,
        "reason": reason,
        "start": on_site_start.isoformat() if on_site_start else None,
        "end": on_site_end.isoformat() if on_site_end else None,
        "travel_out_min": travel_out_min,
        "travel_back_min": travel_back_min,
    }

    def handler() -> Outcome:
        _positive("Travel out", travel_out_min)
        _positive("Travel back", travel_back_min)
        if on_site_start and on_site_end and on_site_end <= on_site_start:
            raise Invalid("The visit must end after it starts")
        v = _visit(visit_code)
        robot, lead, second, vehicle = (
            _robot(robot_code),
            _person(lead_code),
            _person(second_code),
            _vehicle(vehicle_code),
        )
        pid = fleet_services._current_pattern_id(robot) if robot else None
        lock_guards(
            # v2 02C: the maintenance gate setting (and the mode) cannot change until this plan commits
            share_settings=True,
            share_sites=[v.site_id],
            share_patterns=[pid] if pid else [],
            update_robots=[robot.pk] if robot else [],
            update_persons=[p.pk for p in (lead, second) if p],
            update_vehicles=[vehicle.pk] if vehicle else [],
            update_visits=[v.pk],
            label="planning.plan_visit",
        )
        v.refresh_from_db()
        if v.status == "cancelled":
            raise Conflict("Visit is cancelled")
        if robot is not None and v.status in ("planned", "confirmed") and _departed(v):
            # v2 03A-2: the robot is out on this visit, so the location check would otherwise answer first
            raise Conflict(f"Visit {v.code} has departed; it can never be re-planned")
        prev = v.current_revision
        assert prev is not None
        if robot:
            robot.refresh_from_db()
        start = on_site_start or prev.on_site_start
        end = on_site_end or prev.on_site_end
        t_out = travel_out_min if travel_out_min is not None else prev.travel_out_min
        t_back = travel_back_min if travel_back_min is not None else prev.travel_back_min
        site = Site.objects.get(pk=v.site_id)
        plan = evaluate(
            site=site,
            start=start,
            end=end,
            travel_out=t_out,
            travel_back=t_back,
            tasks=prev.tasks,
            scopes=prev.required_consent_scopes,
            robot=robot,
            lead=lead,
            second=second,
            vehicle=vehicle,
            visit=v,
        )
        if plan.feasibility == "blocked" and v.status in ("planned", "confirmed"):
            raise Conflict(
                "Rescheduling refused; the current plan is unchanged. Blockers: "
                + "; ".join(x["message"] for x in plan.blockers if x["level"] == "blocked")
            )
        rev = VisitRevision.objects.create(
            visit=v,
            revision_no=prev.revision_no + 1,
            on_site_start=start,
            on_site_end=end,
            travel_out_min=t_out,
            travel_back_min=t_back,
            tasks=prev.tasks,
            required_consent_scopes=prev.required_consent_scopes,
            robot=robot,
            lead=lead,
            second=second,
            vehicle=vehicle,
            feasibility=plan.feasibility,
            blockers=plan.blockers,
            snapshot=plan.snapshot,
            reason=reason,
            created_by=actor,
        )
        _release_active(v, f"superseded by r{rev.revision_no}")
        if plan.feasibility != "blocked":
            try:
                with transaction.atomic():
                    if robot is not None:
                        rr = _robot_resource(robot)
                        for purpose, s0, s1 in plan.intervals["robot"]:
                            Reservation.objects.create(
                                resource=rr, revision=rev, purpose=purpose, starts_at=s0, ends_at=s1
                            )
                    for p in (lead, second):
                        if p is not None:
                            pr = Resource.objects.get(kind="person", person=p)
                            for purpose, s0, s1 in plan.intervals[f"person:{p.pk}"]:
                                Reservation.objects.create(
                                    resource=pr, revision=rev, purpose=purpose, starts_at=s0, ends_at=s1
                                )
                    if vehicle is not None and robot is not None and plan.trip is not None:
                        vr = Resource.objects.get(kind="vehicle", vehicle=vehicle)
                        for purpose, s0, s1 in plan.intervals["vehicle"]:
                            Reservation.objects.create(
                                resource=vr, revision=rev, purpose=purpose, starts_at=s0, ends_at=s1
                            )
                        trip = VehicleTrip.objects.create(
                            revision=rev, vehicle=vehicle, depart_at=plan.trip[0], return_at=plan.trip[1]
                        )
                        TripCargo.objects.create(trip=trip, robot=robot)
            except IntegrityError as exc:
                if getattr(exc.__cause__, "sqlstate", None) == "23P01":  # exclusion constraint caught a race
                    raise Conflict(
                        "Another booking took one of these resources at the same time; nothing was changed"
                    ) from exc
                raise
        v.current_revision = rev
        v.status = "requested" if plan.feasibility == "blocked" else "planned"
        v.save(update_fields=["current_revision", "status"])
        return Outcome(
            "visit",
            v.code,
            {
                "revision": rev.revision_no,
                "feasibility": plan.feasibility,
                "label": LABELS[plan.feasibility],
                "blockers": [x["message"] for x in plan.blockers],
            },
        )

    return run_command(
        actor, "planning.plan_visit", permission="planning.plan", payload=payload, handler=handler
    )


def live_feasibility(
    v: Visit,
    *,
    periodic: fleet_services.PeriodicStatus | None = None,
    maintenance: list[str] | None = None,
) -> Plan:
    """Re-evaluate the current revision against the world as it is NOW (consent withdrawn, hold placed...).
    Never trusts the stored result or the existence of reservations. ``periodic``, ``maintenance``: see
    ``evaluate``."""
    r = v.current_revision
    assert r is not None
    return evaluate(
        site=v.site,
        start=r.on_site_start,
        end=r.on_site_end,
        travel_out=r.travel_out_min,
        travel_back=r.travel_back_min,
        tasks=r.tasks,
        scopes=r.required_consent_scopes,
        robot=r.robot,
        lead=r.lead,
        second=r.second,
        vehicle=r.vehicle,
        visit=v,
        periodic=periodic,
        maintenance=maintenance,
    )


def confirm_visit(actor: Any, *, visit_code: str) -> dict[str, Any]:
    def handler() -> Outcome:
        v = _visit(visit_code)
        r = v.current_revision
        assert r is not None
        checkpoint("read:planning.confirm_visit")
        pid = fleet_services._current_pattern_id(r.robot) if r.robot else None
        lock_guards(
            share_sites=[v.site_id],
            share_patterns=[pid] if pid else [],
            share_robots=[r.robot_id] if r.robot_id else [],
            share_persons=[x for x in (r.lead_id, r.second_id) if x],
            share_vehicles=[r.vehicle_id] if r.vehicle_id else [],
            update_visits=[v.pk],
            label="planning.confirm_visit",
        )
        v.refresh_from_db()
        if v.current_revision_id != r.pk:
            # Rescheduled between our read and our locks: we may hold the OLD resources' locks.
            raise RetryTransaction()
        if v.status != "planned":
            raise Conflict(f"Only a planned visit can be confirmed (status {v.status})")
        plan = live_feasibility(v)
        if plan.feasibility != "feasible":
            raise Conflict(
                f"Cannot confirm: {LABELS[plan.feasibility]} - "
                + "; ".join(x["message"] for x in plan.blockers)
            )
        v.status = "confirmed"
        v.save(update_fields=["status"])
        return Outcome(
            "visit",
            v.code,
            {
                "status": "confirmed",
                "revision": r.revision_no,
                "note": NOT_DEPARTURE,
                "snapshot": plan.snapshot,
            },
        )

    return run_command(
        actor,
        "planning.confirm_visit",
        permission="planning.confirm",
        payload={"visit": visit_code},
        handler=handler,
    )


def cancel_visit(actor: Any, *, visit_code: str, reason: str) -> dict[str, Any]:
    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        v = _visit(visit_code)
        lock_guards(update_visits=[v.pk], label="planning.cancel_visit")
        v.refresh_from_db()
        if v.status == "cancelled":
            raise Conflict("Already cancelled")
        if _departed(v):
            raise Conflict(
                f"Visit {v.code} has departed; a departed visit can never be cancelled. "
                "It ends through its execution record (return reconciliation and trip closure)."
            )
        _release_active(v, "visit cancelled")
        v.status, v.cancelled_reason = "cancelled", reason
        v.save(update_fields=["status", "cancelled_reason"])
        return Outcome("visit", v.code, {"status": "cancelled"})

    return run_command(
        actor,
        "planning.cancel_visit",
        permission="planning.plan",
        payload={"visit": visit_code, "reason": reason},
        handler=handler,
    )
