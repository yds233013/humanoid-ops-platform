"""Readiness engine (Engineer B). See docs/03_readiness_contract.md sections 4 and 5.

Pure read: no writes, no row locks, no audit rows. SYNTHETIC data only; "ready" means "Ready to request
authorization", never "cleared to depart", and a synthetic or draft policy is never real clearance.

ADVISORY UNLESS LOCKED. A standalone call (views, lists, ``authorization_status``) is a snapshot of a moving
world: a hold, consent withdrawal or reschedule can commit a millisecond later. The result is binding only
when Engineer C runs it inside the transaction of ``authorize_departure`` / ``record_departure`` AFTER taking,
in the global lock order (``platform_core.locks.lock_guards``) and re-reading the visit:

    site S                 (visit.site)
    manifest pattern S     (pattern of the robot's current manifest)
    robot S                (visit.current_revision.robot)
    persons S              (revision lead + second, and the recovery plan's on-call engineer + escalation
                            contact, i.e. exactly the keys of guard_snapshot["person_versions"])
    vehicle S              (revision vehicle)
    visit U
    readiness policy S     (the current policy, guard_snapshot["policy_id"])
    departure authorization U   (record_departure only)

and raising RetryTransaction if visit.current_revision changed between the first read and the locks.

``take_guard_snapshot`` covers these versions (section 5), and equality of the whole dict is the
invalidation test:
    visit_id, visit_version, revision_id, visit_status   reschedule, cancel, confirm, evidence + void,
                                                          recovery plan, reservation release (all update
                                                          the visit row, which bumps its version)
    site_id, site_version                                 consent, assessment, headcount
    robot_id, robot_version, manifest_id                  holds, manifest change, lifecycle, base auth
    pattern_id, pattern_version                           pattern decisions + holds, capability profiles
    person_versions {str(person_id): version}             qualification grant/revoke (lead, second, recovery)
    vehicle_id                                            identity only (vehicles carry no guard version)
    policy_id, policy_lock_version                        approval/withdrawal of the ACTIVE policy
                                                          (readiness.evidence.current_policy, no fallback);
                                                          a newer approved policy shows up as a different
                                                          policy_id, a withdrawn active policy as None
NOT versioned, so they must be re-evaluated (not just snapshot-compared) at departure: deployment settings
(demo_mode, consent_form_version, pre_visit_inspection_min), user role grants (on-call engineer role),
and time itself (evidence age, qualification windows, expiry).

``at`` defaults to PostgreSQL time (SELECT clock_timestamp()), never the app clock: the authorization and
departure triggers compare expires_at against database time.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import connection

from fleet import services as fleet_services
from fleet.location import COLUMNS as location_columns
from fleet.location import from_row as location_from_row
from fleet.models import ConfigurationManifest, Inspection, ManifestPattern, Robot
from planning.models import Person, Reservation, Resource, Visit, VisitRevision
from planning.services import Plan, inspection_signer_problem, live_feasibility
from readiness.evidence import current_policy, latest_ever_approved_policy, policy_status
from readiness.models import DepartureEvent, ReadinessEvidence, ReadinessPolicy, RecoveryPlan

LABEL_READY = "Ready to request authorization"
LABEL_INCOMPLETE = "Readiness incomplete"
LABELS = (LABEL_READY, LABEL_INCOMPLETE)

ENGINEER_ROLES = ("engineer", "engineering_lead")
EVIDENCE_KIND_LABELS = {
    "pre_departure_inspection": "Pre-departure inspection",
    "equipment_check": "Equipment check",
    "battery_check": "Battery check",
}

# Stable blocker codes (planning blockers are copied with the prefix PLANNING_).
VISIT_NOT_CONFIRMED = "VISIT_NOT_CONFIRMED"
VISIT_ALREADY_DEPARTED = "VISIT_ALREADY_DEPARTED"
PLANNING_PREFIX = "PLANNING_"
PLANNING_NOT_FEASIBLE = "PLANNING_NOT_FEASIBLE"  # fallback if planning is not feasible without a blocker
RESERVATIONS_MISSING = "RESERVATIONS_MISSING"
POLICY_MISSING = "POLICY_MISSING"
POLICY_NOT_APPROVED_PROCEDURE = "POLICY_NOT_APPROVED_PROCEDURE"
EVIDENCE_MISSING = "EVIDENCE_MISSING"
EVIDENCE_FAILED = "EVIDENCE_FAILED"
EVIDENCE_STALE = "EVIDENCE_STALE"
EVIDENCE_WRONG_CONFIGURATION = "EVIDENCE_WRONG_CONFIGURATION"
BATTERY_MIN_UNKNOWN = "BATTERY_MIN_UNKNOWN"
BATTERY_LOW = "BATTERY_LOW"
EQUIPMENT_MISSING = "EQUIPMENT_MISSING"
RECOVERY_PLAN_MISSING = "RECOVERY_PLAN_MISSING"
RECOVERY_ONCALL_NOT_ENGINEER = "RECOVERY_ONCALL_NOT_ENGINEER"  # level "review"
EXPIRY_NOT_POSITIVE = "EXPIRY_NOT_POSITIVE"
ROBOT_NOT_AT_HUB = "ROBOT_NOT_AT_HUB"  # Slice 4 contract C3
# Slice 7 (docs/07a_production_guardrails_contract.md)
INSPECTION_TEMPLATE_NOT_APPROVED = "INSPECTION_TEMPLATE_NOT_APPROVED"  # X-1 R1-2 (production)
POLICY_LACKS_INSPECTION = "POLICY_LACKS_INSPECTION"  # X-1 R1-6 (production)
PERIODIC_INSPECTION_INTERVAL_UNKNOWN = "PERIODIC_INSPECTION_INTERVAL_UNKNOWN"  # X-3
PERIODIC_INSPECTION_MISSING = "PERIODIC_INSPECTION_MISSING"  # X-3
PERIODIC_INSPECTION_DUE_BEFORE_RETURN = "PERIODIC_INSPECTION_DUE_BEFORE_RETURN"  # X-3: due_at <= unload_end
INSPECTOR_NOT_QUALIFIED = "INSPECTOR_NOT_QUALIFIED"  # X-8
# v2 02C "Maintenance up to date": the maintenance calendar due time is not after the planned unloading end.
MAINTENANCE_DUE_BEFORE_RETURN = "MAINTENANCE_DUE_BEFORE_RETURN"
# v2 03B-4: the home moved to another zone after the visit was planned (the visit keeps its saved zone until
# it is planned again; operations_pause_gate_problem refuses authorization and departure until then)
PLANNING_HOME_ZONE_CHANGED = "PLANNING_HOME_ZONE_CHANGED"

BLOCKER_CODES = (
    VISIT_NOT_CONFIRMED,
    VISIT_ALREADY_DEPARTED,
    PLANNING_NOT_FEASIBLE,
    RESERVATIONS_MISSING,
    POLICY_MISSING,
    POLICY_NOT_APPROVED_PROCEDURE,
    EVIDENCE_MISSING,
    EVIDENCE_FAILED,
    EVIDENCE_STALE,
    EVIDENCE_WRONG_CONFIGURATION,
    BATTERY_MIN_UNKNOWN,
    BATTERY_LOW,
    EQUIPMENT_MISSING,
    RECOVERY_PLAN_MISSING,
    RECOVERY_ONCALL_NOT_ENGINEER,
    EXPIRY_NOT_POSITIVE,
    ROBOT_NOT_AT_HUB,
    INSPECTION_TEMPLATE_NOT_APPROVED,
    POLICY_LACKS_INSPECTION,
    PERIODIC_INSPECTION_INTERVAL_UNKNOWN,
    PERIODIC_INSPECTION_MISSING,
    PERIODIC_INSPECTION_DUE_BEFORE_RETURN,
    INSPECTOR_NOT_QUALIFIED,
    MAINTENANCE_DUE_BEFORE_RETURN,
    PLANNING_HOME_ZONE_CHANGED,
)


@dataclass
class ReadinessResult:
    ready: bool
    label: str  # LABELS
    blockers: list[dict[str, str]]  # {"level": "blocked"|"review", "code", "message", "action"}
    policy_id: int | None
    policy_version: int | None
    evidence_ids: list[int]
    recovery_plan_id: int | None
    manifest_id: int | None
    guard_snapshot: dict[str, Any]  # section 5
    expires_at: datetime | None  # latest allowed expiry: min(at + ttl, each evidence observed_at + max_age)


# ------------------------------------------------------------------ reads
def db_now() -> datetime:
    """PostgreSQL's clock (the clock the readiness triggers use)."""
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        value: datetime = cur.fetchone()[0]
        return value


def _has_active_role(user_id: int, roles: tuple[str, ...]) -> bool:
    with connection.cursor() as cur:
        for role in roles:
            cur.execute("SELECT has_active_role(%s, %s)", [user_id, role])
            if cur.fetchone()[0]:
                return True
    return False


@dataclass
class _World:
    """Fresh rows for one evaluation; every gate and the snapshot read from the same objects."""

    visit: Visit
    revision: VisitRevision | None
    robot: Robot | None
    manifest_id: int | None
    pattern: ManifestPattern | None
    recovery_plan: RecoveryPlan | None
    policy: ReadinessPolicy | None
    production: bool = True
    latest: dict[str, ReadinessEvidence | None] = field(default_factory=dict)
    periodic: fleet_services.PeriodicStatus | None = None
    persons: dict[int, int] = field(default_factory=dict)  # person id -> version
    # v2 02C: blocking maintenance reasons (empty while the gate is inactive) and, while it is active, the
    # earliest maintenance calendar due time; read in the same statement as the periodic status.
    maintenance: list[str] | None = None
    maintenance_due: datetime | None = None


def _select_recovery_plan(
    visit: Visit, revision: VisitRevision | None, manifest_id: int | None
) -> RecoveryPlan | None:
    """The latest recovery plan for the visit's current revision AND the robot's current manifest."""
    if revision is None or manifest_id is None:
        return None
    return (
        RecoveryPlan.objects.filter(visit=visit, revision=revision, manifest_id=manifest_id)
        .select_related("on_call_engineer", "escalation_contact")
        .order_by("-pk")
        .first()
    )


def _latest_evidence_for(
    visit: Visit, revision: VisitRevision | None, manifest_id: int | None
) -> dict[str, ReadinessEvidence | None]:
    """Latest non-voided evidence of each kind for the current revision AND the robot's current manifest
    (same selection as SQL readiness_latest_evidence: observed_at, then id)."""
    out: dict[str, ReadinessEvidence | None] = {k: None for k in EVIDENCE_KIND_LABELS}
    if revision is None or manifest_id is None:
        return out
    qs = (
        ReadinessEvidence.objects.filter(
            visit=visit, revision=revision, manifest_id=manifest_id, void__isnull=True
        )
        .select_related("inspection", "inspection__template")
        .order_by("kind", "-observed_at", "-pk")
    )
    for ev in qs:
        if out.get(ev.kind) is None:
            out[ev.kind] = ev
    return out


def _signer_person_ids(inspection_ids: list[int]) -> list[int]:
    """Persons of the performers and verifiers of the given inspections (a signer without a Person cannot be
    versioned: the INSPECTOR_NOT_QUALIFIED gate and the database both refuse that case)."""
    users = [
        u
        for pair in Inspection.objects.filter(pk__in=inspection_ids).values_list(
            "performer_id", "verifier_id"
        )
        for u in pair
        if u is not None
    ]
    return list(Person.objects.filter(user_id__in=users).values_list("pk", flat=True))


def _periodic_status(
    robot: Robot, pattern: ManifestPattern | None, at: datetime | None, production: bool
) -> fleet_services.PeriodicStatus:
    """X-3 status from the authoritative SQL function readiness_periodic_inspection_status (EM Option B):
    ONE statement, independent of the robot's inspection history. ``at`` None means the database clock
    (clock_timestamp(), as db_now()). The independent Python implementation
    fleet_services.periodic_inspection_status stays the oracle in tests (test_s7_integration).

    v2 02C: the same statement also reads the maintenance status (fleet_maintenance_block_reasons and, while
    the gate is active, the earliest calendar due time of fleet_maintenance_status), so the evaluator's
    statement count does not grow with the new gate. v2 03A-2: and the robot's location
    (fleet_robot_location)."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT s.state, s.due_at, s.inspection_id, s.rule_id,"
            " fleet_maintenance_block_reasons(%(r)s, %(p)s, coalesce(%(at)s, clock_timestamp())),"
            " CASE WHEN fleet_maintenance_gate_active() THEN (SELECT min(m.due_at) FROM"
            " fleet_maintenance_status(%(r)s, %(p)s, coalesce(%(at)s, clock_timestamp())) m) END,"
            f" {location_columns}"
            " FROM readiness_periodic_inspection_status(%(r)s, %(p)s, coalesce(%(at)s, clock_timestamp())) s"
            " CROSS JOIN fleet_planning_location(%(r)s) l",
            {"r": robot.pk, "p": production, "at": at},
        )
        state, due_at, inspection_id, rule_id, m_reasons, m_due, *loc = cur.fetchone()
    return fleet_services.PeriodicStatus(
        state,
        due_at,
        inspection_id,
        rule_id,
        pattern.code if pattern is not None else None,
        maintenance_reasons=list(m_reasons or []),
        maintenance_due=m_due,
        # v2 03A-2: the location in the same statement, so the evaluator's statement count does not grow
        location=location_from_row(loc),
    )


def _maintenance(robot: Robot, at: datetime | None, production: bool) -> tuple[list[str], datetime | None]:
    """The maintenance part of ``_periodic_status`` on its own (used only when the periodic status came from
    elsewhere, e.g. the Python oracle in tests)."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT fleet_maintenance_block_reasons(%(r)s, %(p)s, coalesce(%(at)s, clock_timestamp())),"
            " CASE WHEN fleet_maintenance_gate_active() THEN (SELECT min(m.due_at) FROM"
            " fleet_maintenance_status(%(r)s, %(p)s, coalesce(%(at)s, clock_timestamp())) m) END",
            {"r": robot.pk, "p": production, "at": at},
        )
        reasons, due = cur.fetchone()
    return list(reasons or []), due


def _read_world(visit: Visit, at: datetime | None = None) -> _World:
    v = Visit.objects.select_related("site").get(pk=visit.pk)
    rev = (
        VisitRevision.objects.select_related("robot", "lead", "second", "vehicle").get(
            pk=v.current_revision_id
        )
        if v.current_revision_id is not None
        else None
    )
    robot = Robot.objects.get(pk=rev.robot_id) if rev is not None and rev.robot_id is not None else None
    manifest_id = robot.current_manifest_id if robot is not None else None
    pattern = None
    if manifest_id is not None:
        pattern = ConfigurationManifest.objects.select_related("pattern").get(pk=manifest_id).pattern
    plan = _select_recovery_plan(v, rev, manifest_id)
    production = not fleet_services.demo_mode()
    latest = _latest_evidence_for(v, rev, manifest_id)
    periodic = _periodic_status(robot, pattern, at, production) if robot is not None else None
    m_reasons: list[str] | None = None
    m_due: datetime | None = None
    if robot is not None and periodic is not None:
        if periodic.maintenance_reasons is None:
            m_reasons, m_due = _maintenance(robot, at, production)
        else:
            m_reasons, m_due = periodic.maintenance_reasons, periodic.maintenance_due
    # Dependency persons (contract 07a section 7.2, P0-1), deduplicated: the revision's lead and second, the
    # recovery plan's two people, and the signers of BOTH relied inspections (the latest pre-departure
    # evidence's inspection and the periodic inspection the database will select).
    person_ids: list[int] = []
    if rev is not None:
        person_ids += [x for x in (rev.lead_id, rev.second_id) if x is not None]
    if plan is not None:
        person_ids += [plan.on_call_engineer_id, plan.escalation_contact_id]
    relied_inspections: list[int] = []
    pre = latest.get("pre_departure_inspection")
    if pre is not None and pre.inspection_id is not None:
        relied_inspections.append(pre.inspection_id)
    if periodic is not None and periodic.inspection_id is not None:
        relied_inspections.append(periodic.inspection_id)
    person_ids += _signer_person_ids(relied_inspections)
    persons = dict(Person.objects.filter(pk__in=set(person_ids)).values_list("pk", "version"))
    return _World(
        v,
        rev,
        robot,
        manifest_id,
        pattern,
        plan,
        current_policy(),
        production,
        latest,
        periodic,
        persons,
        m_reasons,
        m_due,
    )


def _snapshot(w: _World) -> dict[str, Any]:
    rev = w.revision
    return {
        "visit_id": w.visit.pk,
        "visit_version": w.visit.version,
        "revision_id": rev.pk if rev is not None else None,
        "visit_status": w.visit.status,
        "site_id": w.visit.site_id,
        "site_version": w.visit.site.version,
        "robot_id": w.robot.pk if w.robot is not None else None,
        "robot_version": w.robot.version if w.robot is not None else None,
        "manifest_id": w.manifest_id,
        "pattern_id": w.pattern.pk if w.pattern is not None else None,
        "pattern_version": w.pattern.version if w.pattern is not None else None,
        "person_versions": {str(pk): ver for pk, ver in sorted(w.persons.items())},
        "vehicle_id": rev.vehicle_id if rev is not None else None,
        "policy_id": w.policy.pk if w.policy is not None else None,
        "policy_lock_version": w.policy.lock_version if w.policy is not None else None,
    }


def take_guard_snapshot(visit: Visit) -> dict[str, Any]:
    """Section 5 guard snapshot from the CURRENT rows (re-read; the passed object may be stale)."""
    return _snapshot(_read_world(visit))


# ------------------------------------------------------------------ gates
class _Blockers(list[dict[str, str]]):
    def add(self, code: str, message: str, action: str, level: str = "blocked") -> None:
        self.append({"level": level, "code": code, "message": message, "action": action})


def _gate_visit(w: _World, b: _Blockers) -> None:
    if w.revision is None:
        b.add(VISIT_NOT_CONFIRMED, f"Visit {w.visit.code} has no plan revision", "Plan and confirm the visit")
    elif w.visit.status != "confirmed":
        b.add(
            VISIT_NOT_CONFIRMED,
            f"Visit {w.visit.code} is {w.visit.status}, not confirmed",
            "Confirm the visit (a reschedule returns it to planned)",
        )
    if DepartureEvent.objects.filter(visit_id=w.visit.pk).exists():
        b.add(
            VISIT_ALREADY_DEPARTED,
            f"A departure is already recorded for {w.visit.code}; one visit has exactly one departure",
            "Plan a new visit for any further trip",
        )


def _gate_custody(w: _World, b: _Blockers) -> None:
    """Slice 4 contract C3: the robot must be at the hub (baseline or a verified hub receipt) with no open
    execution run. Same SQL definition the authorization and departure triggers use."""
    if w.robot is None:
        return
    from execution.custody import departure_problem  # execution depends on readiness, not the reverse

    problem = departure_problem(w.robot.pk)
    if problem:
        b.add(
            ROBOT_NOT_AT_HUB,
            problem[:1].upper() + problem[1:],
            "Finish the robot's current run: return it to the hub, reconcile and close the trip",
        )


def _gate_planning(w: _World, b: _Blockers) -> Plan | None:
    if w.revision is None:
        return None
    # Slice 7 X-3: the periodic status read once in _read_world (same robot, time and mode) is shared with the
    # planning eligibility check instead of being computed a second time there.
    # v2 03B-1: no pause label here (pinned statement budget); the database gates refuse under a pause
    plan = live_feasibility(w.visit, periodic=w.periodic, maintenance=w.maintenance, pause_check=False)
    # v2 03B-4: no statement (the home is loaded with the visit)
    if w.visit.zone_id != w.visit.site.zone_ref_id:
        b.add(
            PLANNING_HOME_ZONE_CHANGED,
            f"The home {w.visit.site.code} moved to another zone after this visit was planned",
            "Plan the visit again so its zone, travel time and pauses are checked",
        )
    if plan.feasibility != "feasible":
        for x in plan.blockers:
            b.add(PLANNING_PREFIX + x["code"], x["message"], x["action"], level=x.get("level", "blocked"))
        if not plan.blockers:
            b.add(
                PLANNING_NOT_FEASIBLE,
                f"Planning is {plan.feasibility} now",
                "Re-check the visit plan",
            )
    return plan


def _gate_reservations(w: _World, plan: Plan | None, b: _Blockers) -> None:
    rev = w.revision
    if rev is None:
        return
    rows = list(Reservation.objects.filter(revision=rev))
    active = [r for r in rows if r.released_at is None]
    problems: list[str] = []
    released = len(rows) - len(active)
    if released:
        problems.append(f"{released} reservation(s) for revision r{rev.revision_no} were released")
    if not active:
        problems.append(f"No active reservations for revision r{rev.revision_no}")
    elif plan is not None and plan.intervals:
        uncovered: list[str] = []
        for key, spans in plan.intervals.items():
            res = _resource_for(key, rev)
            for purpose, s0, s1 in spans:
                if key == "vehicle" and rev.robot_id is None:
                    continue  # plan_visit books the vehicle only together with a robot
                ok = res is not None and any(
                    r.resource_id == res.pk and r.purpose == purpose and r.starts_at <= s0 and r.ends_at >= s1
                    for r in active
                )
                if not ok:
                    uncovered.append(f"{key.split(':')[0]} {purpose}")
        if uncovered:
            problems.append("Not covered by an active reservation: " + ", ".join(uncovered))
    if problems:
        b.add(RESERVATIONS_MISSING, "; ".join(problems), "Re-plan the visit to restore its reservations")


def _resource_for(key: str, rev: VisitRevision) -> Resource | None:
    if key == "robot" and rev.robot_id is not None:
        return Resource.objects.filter(kind="robot", robot_id=rev.robot_id).first()
    if key == "vehicle" and rev.vehicle_id is not None:
        return Resource.objects.filter(kind="vehicle", vehicle_id=rev.vehicle_id).first()
    if key.startswith("person:"):
        return Resource.objects.filter(kind="person", person_id=int(key.split(":", 1)[1])).first()
    return None


def _gate_policy(w: _World, b: _Blockers) -> None:
    if w.policy is None:
        latest = ReadinessPolicy.objects.order_by("-version").first()
        last_active = latest_ever_approved_policy()
        if latest is None:
            why = "No readiness policy exists"
        elif last_active is not None:
            # No fallback: once the newest approved policy is withdrawn, nothing older is used again.
            why = (
                f"No active readiness policy: v{last_active.version} was withdrawn and no older policy is "
                f"used (latest is v{latest.version}, {policy_status(latest)})"
            )
        else:
            why = f"No approved readiness policy (latest is v{latest.version}, {policy_status(latest)})"
        b.add(
            POLICY_MISSING,
            why,
            "Engineering lead and safety reviewer approve a NEWER readiness policy version",
        )
        return
    if w.production and w.policy.source != "approved_procedure":
        b.add(
            POLICY_NOT_APPROVED_PROCEDURE,
            f"Readiness policy v{w.policy.version} is {w.policy.source}; "
            "production needs an approved procedure",
            "Approve a policy with source approved_procedure (synthetic policies never authorize production)",
        )


def _latest_evidence(w: _World) -> dict[str, ReadinessEvidence | None]:
    """Latest non-voided evidence of each kind for the current revision AND the robot's current manifest."""
    return w.latest


def _gate_evidence(
    w: _World,
    latest: dict[str, ReadinessEvidence | None],
    at: datetime,
    b: _Blockers,
    relied: dict[int, datetime | None],
) -> set[str]:
    """Returns the kinds this gate already reported, so later gates do not repeat 'no reading' blockers."""
    reported: set[str] = set()
    if w.policy is None:
        return reported
    for req in w.policy.requirements.get("evidence", []):
        kind, max_age = req["kind"], int(req["max_age_min"])
        label = EVIDENCE_KIND_LABELS.get(kind, kind)
        ev = latest.get(kind)
        if ev is None:
            reported.add(kind)
            others = ReadinessEvidence.objects.filter(visit=w.visit, kind=kind)
            live = others.filter(void__isnull=True)
            if w.revision is not None and live.filter(revision=w.revision).exists():
                b.add(
                    EVIDENCE_WRONG_CONFIGURATION,
                    f"{label} was recorded for a different robot configuration than the current one",
                    f"Record a new {label.lower()} on the current configuration",
                )
            elif live.exists():
                b.add(
                    EVIDENCE_MISSING,
                    f"{label} exists only for an earlier plan revision",
                    f"Record a new {label.lower()} for the current plan",
                )
            elif others.exists():
                b.add(EVIDENCE_MISSING, f"{label} was voided", f"Record a new {label.lower()}")
            else:
                article = "an" if label[:1].lower() in "aeiou" else "a"
                b.add(EVIDENCE_MISSING, f"No {label.lower()} recorded", f"Record {article} {label.lower()}")
            continue
        if ev.result != "pass":
            reported.add(kind)
            b.add(
                EVIDENCE_FAILED,
                f"{label} {ev.code} failed",
                f"Resolve the failure and record a new {label.lower()}",
            )
            continue
        if ev.observed_at < at - timedelta(minutes=max_age):
            reported.add(kind)
            b.add(
                EVIDENCE_STALE,
                f"{label} {ev.code} is older than {max_age} min",
                f"Record a fresh {label.lower()}",
            )
            continue
        relied[ev.pk] = ev.observed_at + timedelta(minutes=max_age)
    return reported


def _gate_battery(
    w: _World,
    latest: dict[str, ReadinessEvidence | None],
    reported: set[str],
    b: _Blockers,
    relied: dict[int, datetime | None],
) -> None:
    profile = fleet_services.approved_profile(w.pattern) if w.pattern is not None else None
    minimum = profile.min_departure_soc_pct if profile is not None else None
    if minimum is None:
        b.add(
            BATTERY_MIN_UNKNOWN,
            "The approved capability profile has no minimum departure battery level",
            "Engineering publishes min_departure_soc_pct in an approved capability profile",
        )
    ev = latest.get("battery_check")
    if ev is None or ev.result != "pass":
        if "battery_check" not in reported:
            b.add(
                BATTERY_LOW,
                "No passing battery check for the current plan and configuration; charge level unknown",
                "Record a battery check",
            )
        return
    soc = ev.details.get("soc_pct") if isinstance(ev.details, dict) else None
    if not isinstance(soc, int) or isinstance(soc, bool):
        b.add(BATTERY_LOW, f"Battery check {ev.code} has no charge level", "Record a battery check")
        return
    if minimum is not None and soc < minimum:
        b.add(
            BATTERY_LOW, f"Battery at {soc}%, below the {minimum}% departure minimum", "Charge, then re-check"
        )
        return
    if minimum is not None and "battery_check" not in reported:
        relied.setdefault(ev.pk, None)


def _gate_equipment(
    w: _World,
    latest: dict[str, ReadinessEvidence | None],
    reported: set[str],
    b: _Blockers,
    relied: dict[int, datetime | None],
) -> None:
    required: list[str] = []
    if w.policy is not None:
        required += list(w.policy.requirements.get("required_equipment", []))
    if w.recovery_plan is not None:
        required += [x for x in w.recovery_plan.required_equipment if x not in required]
    if not required:
        return
    ev = latest.get("equipment_check")
    if ev is None or ev.result != "pass":
        if "equipment_check" not in reported:
            b.add(
                EQUIPMENT_MISSING,
                "No passing equipment check covers: " + ", ".join(required),
                "Record an equipment check",
            )
        return
    items = ev.details.get("items") if isinstance(ev.details, dict) else None
    items = items if isinstance(items, dict) else {}
    marked = [x for x in required if items.get(x) == "missing"]
    unchecked = [x for x in required if x not in items]
    other = [x for x in required if x in items and items[x] not in ("present", "missing")]
    if marked or unchecked or other:
        parts = []
        if marked:
            parts.append("marked missing: " + ", ".join(marked))
        if unchecked:
            parts.append("not checked: " + ", ".join(unchecked))
        if other:
            parts.append("unknown state: " + ", ".join(other))
        b.add(
            EQUIPMENT_MISSING,
            f"Equipment check {ev.code} " + "; ".join(parts),
            "Load the equipment and record a new equipment check",
        )
        return
    if "equipment_check" not in reported:
        relied.setdefault(ev.pk, None)


def _gate_recovery_plan(w: _World, b: _Blockers) -> None:
    required = bool(w.policy.requirements.get("recovery_plan_required", True)) if w.policy else True
    plan = w.recovery_plan
    if plan is None:
        if required:
            older = RecoveryPlan.objects.filter(visit=w.visit).exists()
            b.add(
                RECOVERY_PLAN_MISSING,
                "Recovery plan exists only for an earlier plan revision or robot configuration"
                if older
                else "No recovery plan recorded",
                "Record a recovery plan for the current plan and configuration",
            )
        return
    on_call = plan.on_call_engineer
    if not _has_active_role(on_call.user_id, ENGINEER_ROLES):
        b.add(
            RECOVERY_ONCALL_NOT_ENGINEER,
            f"On-call person {on_call.code} does not hold an active engineer or engineering lead role",
            "Confirm the on-call person or record a plan with an engineer on call",
            level="review",
        )


def _unload_end(plan: Plan | None) -> datetime | None:
    """The planned unloading end of the live plan timeline (readiness/0006 unload_end), if computable."""
    if plan is None:
        return None
    for purpose, _s0, s1 in plan.intervals.get("robot", []):
        if purpose == "unloading":
            return s1
    return None


def _gate_periodic_inspection(w: _World, plan: Plan | None, b: _Blockers) -> datetime | None:
    """Gate 1c (X-3). Returns the due date that bounds the expiry, if the state is due. Overdue already
    arrives through PLANNING_ROBOT_NOT_ELIGIBLE (eligibility) and is not repeated here."""
    st = w.periodic
    if st is None or w.robot is None:
        return None
    mode = "production" if w.production else "demo"
    if st.state == "unknown_interval":
        b.add(
            PERIODIC_INSPECTION_INTERVAL_UNKNOWN,
            f"Periodic inspection interval unknown for configuration {st.pattern_code} ({mode} mode): no "
            "approved interval rule usable in this mode",
            "Engineering supplies the interval with a procedure reference; engineering lead and safety "
            "reviewer approve it",
        )
        return None
    if st.state == "never_inspected":
        b.add(
            PERIODIC_INSPECTION_MISSING,
            f"No qualifying periodic inspection of {w.robot.code} on the current configuration (complete "
            "PASS, template approved for periodic use, both signers qualified readiness inspectors)",
            "Record a periodic inspection on an approved template, signed by two qualified inspectors",
        )
        return None
    if st.state != "due" or st.due_at is None:
        return None
    unload_end = _unload_end(plan)
    # due_at <= unload_end blocks: the complement of the strict SQL rule due_at > unload_end (EM ruling 6).
    if unload_end is not None and st.due_at <= unload_end:
        due, end = fleet_services.format_db_time(st.due_at), fleet_services.format_db_time(unload_end)
        b.add(
            PERIODIC_INSPECTION_DUE_BEFORE_RETURN,
            f"Periodic inspection of {w.robot.code} is due at {due}, "
            f"not after the planned unloading end {end}",
            "Record a new periodic inspection before the visit, or re-plan the visit",
        )
    return st.due_at


def _gate_maintenance(w: _World, plan: Plan | None, b: _Blockers) -> datetime | None:
    """Gate 1d, v2 02C "Maintenance up to date" (contract 11, 8.2), backed by fleet_maintenance_status, the
    function readiness_issue_authorization calls. Overdue, unknown, no approved rule and a missing required
    inspection already arrive through PLANNING_ROBOT_NOT_ELIGIBLE (the same reasons from the same function)
    and are not repeated here, as the periodic gate does. This gate adds the calendar due time: it must be
    strictly after the planned unloading end, and it bounds the expiry. Nothing while the gate is inactive."""
    due = w.maintenance_due
    if due is None or w.robot is None:
        return None
    unload_end = _unload_end(plan)
    if unload_end is not None and due <= unload_end:
        b.add(
            MAINTENANCE_DUE_BEFORE_RETURN,
            f"Maintenance of {w.robot.code} falls due at {fleet_services.format_db_time(due)}, "
            f"not after the planned unloading end {fleet_services.format_db_time(unload_end)}",
            "Record the maintenance before the visit, or re-plan the visit",
        )
    return due


def _gate_inspection_provenance(
    w: _World, latest: dict[str, ReadinessEvidence | None], reported: set[str], b: _Blockers
) -> None:
    """Gate 5b (X-1 R1-2 / R1-6, X-8): the relied pre-departure inspection's template must be approved for
    pre-departure use in production, and both its signers must be qualified readiness inspectors (both
    modes). A production policy must require pre-departure inspection evidence."""
    if w.policy is None:
        return
    kinds = {req["kind"] for req in w.policy.requirements.get("evidence", [])}
    if w.production and "pre_departure_inspection" not in kinds:
        b.add(
            POLICY_LACKS_INSPECTION,
            f"Readiness policy v{w.policy.version} does not require pre-departure inspection evidence "
            "(required in production)",
            "Approve a readiness policy that requires pre_departure_inspection evidence",
        )
    if "pre_departure_inspection" not in kinds or "pre_departure_inspection" in reported:
        return
    ev = latest.get("pre_departure_inspection")
    if ev is None or ev.result != "pass" or ev.inspection is None:
        return
    insp = ev.inspection
    if w.production and not fleet_services.template_usable(insp.template, "pre_departure", production=True):
        b.add(
            INSPECTION_TEMPLATE_NOT_APPROVED,
            f"Pre-departure inspection {insp.code} uses template {insp.template.version}, which is not "
            "approved for pre-departure use in production",
            "Re-inspect on a template approved by engineering and safety for pre-departure use",
        )
    problem = inspection_signer_problem(insp)
    if problem:
        b.add(
            INSPECTOR_NOT_QUALIFIED,
            f"Pre-departure inspection {insp.code}: {problem}",
            "Re-inspect with two qualified readiness inspectors",
        )


# ------------------------------------------------------------------ entry point
def evaluate_readiness(visit: Visit, at: datetime | None = None) -> ReadinessResult:
    """Run gates 1-9 in order, never stopping early. Read-only (see the module docstring)."""
    if at is None:
        at = db_now()
    w = _read_world(visit, at)
    b = _Blockers()
    relied: dict[int, datetime | None] = {}  # evidence id -> its expiry bound (None: no age requirement)

    _gate_visit(w, b)  # 1
    _gate_custody(w, b)  # 1b (Slice 4, C3)
    plan = _gate_planning(w, b)  # 2
    periodic_due = _gate_periodic_inspection(w, plan, b)  # 1c (Slice 7 X-3; needs the plan timeline)
    maintenance_due = _gate_maintenance(w, plan, b)  # 1d (v2 02C)
    _gate_reservations(w, plan, b)  # 3
    _gate_policy(w, b)  # 4
    latest = _latest_evidence(w)
    reported = _gate_evidence(w, latest, at, b, relied)  # 5
    _gate_inspection_provenance(w, latest, reported, b)  # 5b (Slice 7 X-1, X-8)
    _gate_battery(w, latest, reported, b, relied)  # 6
    _gate_equipment(w, latest, reported, b, relied)  # 7
    _gate_recovery_plan(w, b)  # 8

    expires_at: datetime | None = None
    if w.policy is not None:  # 9
        bounds = [at + timedelta(minutes=int(w.policy.requirements["authorization_ttl_min"]))]
        bounds += [x for x in relied.values() if x is not None]
        if periodic_due is not None:
            bounds.append(periodic_due)  # X-3: expires_at <= due_at (defence in depth)
        if maintenance_due is not None:
            bounds.append(maintenance_due)  # v2 02C: the same rule for the maintenance calendar due time
        expires_at = min(bounds)
        if expires_at <= at:
            b.add(
                EXPIRY_NOT_POSITIVE,
                "An authorization issued now would already be expired",
                "Record fresh evidence",
            )

    ready = not b
    return ReadinessResult(
        ready=ready,
        label=LABEL_READY if ready else LABEL_INCOMPLETE,
        blockers=list(b),
        policy_id=w.policy.pk if w.policy is not None else None,
        policy_version=w.policy.version if w.policy is not None else None,
        evidence_ids=sorted(relied),
        recovery_plan_id=w.recovery_plan.pk if w.recovery_plan is not None else None,
        manifest_id=w.manifest_id,
        guard_snapshot=_snapshot(w),
        expires_at=expires_at,
    )
