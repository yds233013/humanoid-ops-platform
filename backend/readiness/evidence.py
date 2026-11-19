"""Readiness policy, evidence and recovery-plan commands (Engineer A). See docs/03_readiness_contract.md s4.

Straight writers over the readiness schema. Each one: run_command (auth, idempotency, in-transaction
permission) -> guard locks in the global order -> re-read -> domain checks with clear errors -> insert.
PostgreSQL re-checks every invariant (migration readiness/0002), so these checks are for good messages,
not the only line of defence. SYNTHETIC data only; nothing here grants real-world clearance.
"""

from datetime import datetime
from typing import Any

from django.db import connection
from django.db.models import Count, Q, Value
from django.db.models.functions import Least, Now

from fleet import services as fleet_services
from fleet.models import ConfigurationManifest, Inspection, Robot
from planning import services as planning_services
from planning.models import Person, Visit
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, RetryTransaction, run_command
from platform_core.locks import lock_guards
from readiness.models import (
    EVIDENCE_KINDS,
    ReadinessEvidence,
    ReadinessEvidenceVoid,
    ReadinessPolicy,
    ReadinessPolicyApproval,
    ReadinessPolicyWithdrawal,
    RecoveryPlan,
)

KINDS = [k for k, _ in EVIDENCE_KINDS]
RESULTS = ["pass", "fail"]
SOURCES = [s for s, _ in ReadinessPolicy.SOURCES]
SLOT_ROLES = {"engineering": "engineering_lead", "safety": "safety_reviewer"}
REQUIREMENT_KEYS = {"authorization_ttl_min", "evidence", "required_equipment", "recovery_plan_required"}
EQUIPMENT_STATES = {"present", "missing"}
MAX_EVIDENCE_AGE_H = 24


# ------------------------------------------------------------------ read helpers
def policy_status(policy: ReadinessPolicy) -> str:
    """'withdrawn' if a withdrawal exists; else 'approved' if both slots are approved; else 'draft'."""
    if ReadinessPolicyWithdrawal.objects.filter(policy=policy).exists():
        return "withdrawn"
    slots = set(policy.approvals.values_list("slot", flat=True))
    return "approved" if slots >= {"engineering", "safety"} else "draft"


def latest_ever_approved_policy() -> ReadinessPolicy | None:
    """The highest-version policy that has ever become approved (both slots), withdrawn or not."""
    return (
        ReadinessPolicy.objects.annotate(
            n_slots=Count("approvals__slot", distinct=True, filter=Q(approvals__slot__in=list(SLOT_ROLES)))
        )
        .filter(n_slots=len(SLOT_ROLES))
        .order_by("-version")
        .first()
    )


def current_policy() -> ReadinessPolicy | None:
    """The ACTIVE policy (EM decision, no fallback). Same rule as SQL readiness_active_policy_id():

    the highest-version policy that has ever become approved (both slots); if that policy is withdrawn there
    is NO active policy until a newer version becomes fully approved (its second slot approval activates
    it). An older approved policy is never used again. At most one policy is active.
    """
    policy = latest_ever_approved_policy()
    if policy is None or ReadinessPolicyWithdrawal.objects.filter(policy=policy).exists():
        return None
    return policy


def active_policy_id_in_db() -> int | None:
    """readiness_active_policy_id() as PostgreSQL computes it (the rule the triggers enforce)."""
    with connection.cursor() as cur:
        cur.execute("SELECT readiness_active_policy_id()")
        value = cur.fetchone()[0]
        return int(value) if value is not None else None


# ------------------------------------------------------------------ small helpers
def _db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        value: datetime = cur.fetchone()[0]
        return value


def _has_role_now(user: Any, role: str) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT has_active_role(%s, %s)", [user.pk, role])
        return bool(cur.fetchone()[0])


def _next_code(seq: str, prefix: str) -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT nextval(%s)", [seq])
        return f"{prefix}-{cur.fetchone()[0]:04d}"


def _policy(version: int) -> ReadinessPolicy:
    try:
        return ReadinessPolicy.objects.get(version=version)
    except ReadinessPolicy.DoesNotExist:
        raise Invalid(f"No readiness policy v{version}") from None


def _visit(code: str) -> Visit:
    try:
        return Visit.objects.get(code=code)
    except Visit.DoesNotExist:
        raise Invalid(f"No visit {code}") from None


def _person(code: str) -> Person:
    try:
        return Person.objects.get(code=code)
    except Person.DoesNotExist:
        raise Invalid(f"No person {code}") from None


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2_147_483_647


def _string_list_ok(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(x, str) and x.strip() for x in value)
        and len(set(value)) == len(value)
    )


def validate_requirements(req: Any) -> None:
    """Same rules as the database trigger readiness_requirements_problem(); raises Invalid."""
    if not isinstance(req, dict) or set(req) != REQUIREMENT_KEYS:
        raise Invalid(f"requirements must have exactly the keys {sorted(REQUIREMENT_KEYS)}")
    if not _positive_int(req["authorization_ttl_min"]):
        raise Invalid("authorization_ttl_min must be a whole number > 0")
    ev = req["evidence"]
    if not isinstance(ev, list) or not ev:
        raise Invalid("evidence must be a non-empty list")
    for item in ev:
        if not isinstance(item, dict) or set(item) != {"kind", "max_age_min"}:
            raise Invalid("each evidence requirement must have exactly the keys kind and max_age_min")
        if item["kind"] not in KINDS:
            raise Invalid(f"unknown evidence kind {item['kind']!r}")
        if not _positive_int(item["max_age_min"]):
            raise Invalid("max_age_min must be a whole number > 0")
    if len({i["kind"] for i in ev}) != len(ev):
        raise Invalid("each evidence kind may appear only once")
    if not _string_list_ok(req["required_equipment"]):
        raise Invalid("required_equipment must be a list of distinct, non-empty names")
    if not isinstance(req["recovery_plan_required"], bool):
        raise Invalid("recovery_plan_required must be true or false")


def _validate_details(kind: str, details: dict[str, Any]) -> None:
    if not isinstance(details, dict):
        raise Invalid("details must be an object")
    if kind == "pre_departure_inspection":
        if details:
            raise Invalid("Inspection evidence takes no details; the inspection itself is the record")
    elif kind == "equipment_check":
        items = details.get("items")
        if set(details) != {"items"} or not isinstance(items, dict) or not items:
            raise Invalid('Equipment check details must be {"items": {name: "present" | "missing"}}')
        if any(not str(k).strip() or v not in EQUIPMENT_STATES for k, v in items.items()):
            raise Invalid("Each equipment item must be named and marked present or missing")
    elif kind == "battery_check":
        soc: Any = details.get("soc_pct")
        if set(details) != {"soc_pct"} or not _positive_int(soc) or soc > 100:
            raise Invalid('Battery check details must be {"soc_pct": 1..100}')


# ------------------------------------------------------------------ policy commands
def create_readiness_policy(
    actor: Any, *, requirements: dict[str, Any], source: str, notes: str = ""
) -> dict[str, Any]:
    def handler() -> Outcome:
        if source not in SOURCES:
            raise Invalid(f"source must be one of {SOURCES}")
        validate_requirements(requirements)
        version = (
            ReadinessPolicy.objects.order_by("-version").values_list("version", flat=True).first() or 0
        ) + 1
        p = ReadinessPolicy.objects.create(
            version=version, requirements=requirements, source=source, notes=notes, created_by=actor
        )
        return Outcome(
            "readiness_policy", str(p.version), {"version": p.version, "source": source, "status": "draft"}
        )

    return run_command(
        actor,
        "readiness.create_readiness_policy",
        permission="readiness.manage_policy",
        payload={"requirements": requirements, "source": source, "notes": notes},
        handler=handler,
    )


def approve_readiness_policy(actor: Any, *, version: int, slot: str) -> dict[str, Any]:
    def handler() -> Outcome:
        if slot not in SLOT_ROLES:
            raise Invalid(f"slot must be one of {sorted(SLOT_ROLES)}")
        p = _policy(version)
        lock_guards(update_policies=[p.pk], label="readiness.approve_readiness_policy")
        p.refresh_from_db()
        if ReadinessPolicyWithdrawal.objects.filter(policy=p).exists():
            raise Conflict(f"Policy v{version} is withdrawn and cannot be approved")
        if not _has_role_now(actor, SLOT_ROLES[slot]):
            raise PermissionDenied(f"Slot {slot} requires role {SLOT_ROLES[slot]}")
        if p.created_by_id == actor.pk:
            raise Conflict("The author of a readiness policy cannot approve it")
        if ReadinessPolicyApproval.objects.filter(policy=p, approver=actor).exists():
            raise Conflict("You already approved this policy in another slot")
        if ReadinessPolicyApproval.objects.filter(policy=p, slot=slot).exists():
            raise Conflict(f"Slot {slot} is already approved")
        ReadinessPolicyApproval.objects.create(policy=p, slot=slot, approver=actor)
        p.refresh_from_db()
        return Outcome(
            "readiness_policy",
            str(p.version),
            {"slot": slot, "status": policy_status(p), "lock_version": p.lock_version},
        )

    return run_command(
        actor,
        "readiness.approve_readiness_policy",
        permission="readiness.approve_policy",
        payload={"version": version, "slot": slot},
        handler=handler,
    )


def withdraw_readiness_policy(actor: Any, *, version: int, reason: str) -> dict[str, Any]:
    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        p = _policy(version)
        lock_guards(update_policies=[p.pk], label="readiness.withdraw_readiness_policy")
        p.refresh_from_db()
        if ReadinessPolicyWithdrawal.objects.filter(policy=p).exists():
            raise Conflict(f"Policy v{version} is already withdrawn")
        ReadinessPolicyWithdrawal.objects.create(policy=p, withdrawn_by=actor, reason=reason)
        p.refresh_from_db()
        return Outcome(
            "readiness_policy",
            str(p.version),
            {"status": "withdrawn", "lock_version": p.lock_version},
        )

    return run_command(
        actor,
        "readiness.withdraw_readiness_policy",
        permission="readiness.withdraw_policy",
        payload={"version": version, "reason": reason},
        handler=handler,
    )


# ------------------------------------------------------------------ evidence
def _lock_visit_and_robot(
    visit_code: str, label: str, share_persons: list[int] | None = None, *, share_settings: bool = False
) -> tuple[Visit, Robot, int]:
    """Read the visit, lock [settings S] robot S (+ persons S) then visit U, re-read; retry if the visit was
    re-planned."""
    v = _visit(visit_code)
    r = v.current_revision
    if r is None or r.robot_id is None:
        raise Conflict(f"Visit {visit_code} has no robot assigned")
    robot_id: int = r.robot_id
    lock_guards(
        share_settings=share_settings,
        share_robots=[robot_id],
        share_persons=share_persons or [],
        update_visits=[v.pk],
        label=label,
    )
    v.refresh_from_db()
    if v.current_revision_id != r.pk:
        raise RetryTransaction()  # re-planned between our read and our locks: we may hold the old robot
    if v.status == "cancelled":
        raise Conflict(f"Visit {visit_code} is cancelled")
    robot = Robot.objects.get(pk=robot_id)
    if robot.current_manifest_id is None:
        raise Conflict(f"Robot {robot.code} has no configuration manifest")
    return v, robot, robot.current_manifest_id


def record_evidence(
    actor: Any,
    *,
    visit_code: str,
    kind: str,
    result: str,
    details: dict[str, Any] | None = None,
    inspection_code: str | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Record one piece of readiness evidence bound to the visit's CURRENT revision and the robot's CURRENT
    manifest. observed_at defaults to the inspection's submission time (inspection evidence) or to database
    time (other kinds). Bumps the visit's guard version (database trigger)."""
    payload = {
        "visit": visit_code,
        "kind": kind,
        "result": result,
        "details": details or {},
        "inspection": inspection_code,
        "observed_at": observed_at.isoformat() if observed_at else None,
    }

    def handler() -> Outcome:
        d = details or {}
        if kind not in KINDS:
            raise Invalid(f"kind must be one of {KINDS}")
        if result not in RESULTS:
            raise Invalid(f"result must be one of {RESULTS}")
        _validate_details(kind, d)
        if kind == "pre_departure_inspection" and not inspection_code:
            raise Invalid("Pre-departure inspection evidence needs an inspection code")
        if kind != "pre_departure_inspection" and inspection_code:
            raise Invalid("Only pre-departure inspection evidence may reference an inspection")
        if observed_at is not None and observed_at.tzinfo is None:
            raise Invalid("observed_at must be timezone-aware")

        inspection = None
        signer_persons: list[int] = []
        if inspection_code:
            try:
                inspection = Inspection.objects.select_related("template").get(code=inspection_code)
            except Inspection.DoesNotExist:
                raise Invalid(f"No inspection {inspection_code}") from None
            users = [x for x in (inspection.performer_id, inspection.verifier_id) if x is not None]
            signer_persons = list(Person.objects.filter(user_id__in=users).values_list("pk", flat=True))
        # Slice 7 (section 9.2): settings S FIRST (the X-1 refusal depends on the mode), robot S, the signer
        # persons S (X-8: a revocation and this evidence are totally ordered), then the visit U.
        v, robot, manifest_id = _lock_visit_and_robot(
            visit_code, "readiness.record_evidence", share_persons=signer_persons, share_settings=True
        )
        rev = v.current_revision
        assert rev is not None
        manifest = ConfigurationManifest.objects.get(pk=manifest_id)
        when = observed_at
        if inspection is not None:
            if inspection.robot_id != robot.pk or inspection.manifest_id != manifest.pk:
                raise Conflict(
                    f"Inspection {inspection_code} is for a different robot or configuration than "
                    f"{robot.code}'s current one"
                )
            if inspection.visit_ref != v.code:
                raise Conflict(f"Inspection {inspection_code} was not recorded for visit {v.code}")
            if result == "pass" and not (inspection.result == "PASS" and inspection.complete):
                raise Conflict(
                    f"Inspection {inspection_code} is {inspection.result}; "
                    "only a complete PASS counts as passing"
                )
            if result == "pass":
                # X-1 (R1-2): production pass evidence only on a template approved for pre-departure use.
                # A fail is always recorded (principle 1).
                if not fleet_services.demo_mode() and not fleet_services.template_usable(
                    inspection.template, "pre_departure", production=True
                ):
                    raise Conflict(
                        f"Passing pre-departure evidence refused: inspection {inspection.code} uses template "
                        f"{inspection.template.version}, which is not approved for pre-departure use in "
                        "production. Record it as a fail, or re-inspect on an approved template"
                    )
                # X-8 (both modes): both signers covered at the inspection time and unrevoked now.
                problem = planning_services.inspection_signer_problem(inspection)
                if problem:
                    raise Conflict(f"Passing pre-departure evidence refused: {problem}")
        now = _db_now()
        fields: dict[str, Any] = {}
        if when is not None:  # a caller-supplied time, checked here and again by readiness_evidence_guard
            if when > now:
                raise Invalid("observed_at cannot be in the future")
            if (now - when).total_seconds() > MAX_EVIDENCE_AGE_H * 3600:
                raise Invalid(f"observed_at is more than {MAX_EVIDENCE_AGE_H} hours ago")
            fields["observed_at"] = when
        elif inspection is not None:
            # Default: the inspection's (database) submission time, never after this INSERT's own statement
            # time, computed IN the INSERT (readiness/0008: the guard's bound includes statement_timestamp()).
            # A value read in an earlier statement could be refused if the database clock stepped back since.
            if (now - inspection.submitted_at).total_seconds() > MAX_EVIDENCE_AGE_H * 3600:
                raise Invalid(f"observed_at is more than {MAX_EVIDENCE_AGE_H} hours ago")
            fields["observed_at"] = Least(Value(inspection.submitted_at), Now())
        # else: the column default, statement_timestamp() of the INSERT (readiness/0008)
        ev = ReadinessEvidence.objects.create(
            code=_next_code("readiness_evidence_code_seq", "EV"),
            visit=v,
            revision=rev,
            robot=robot,
            manifest=manifest,
            kind=kind,
            result=result,
            inspection=inspection,
            details=d,
            recorded_by=actor,
            **fields,
        )
        return Outcome(
            "readiness_evidence",
            ev.code,
            {
                "id": ev.pk,
                "visit": v.code,
                "kind": kind,
                "result": result,
                "revision": rev.revision_no,
                "manifest_id": manifest.pk,
                "observed_at": ev.observed_at.isoformat(),
            },
        )

    return run_command(
        actor,
        "readiness.record_evidence",
        permission="readiness.record_evidence",
        payload=payload,
        handler=handler,
    )


def void_evidence(actor: Any, *, evidence_code: str, reason: str) -> dict[str, Any]:
    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        try:
            ev = ReadinessEvidence.objects.get(code=evidence_code)
        except ReadinessEvidence.DoesNotExist:
            raise Invalid(f"No evidence {evidence_code}") from None
        lock_guards(share_robots=[ev.robot_id], update_visits=[ev.visit_id], label="readiness.void_evidence")
        v = Visit.objects.get(pk=ev.visit_id)
        if v.status == "cancelled":
            raise Conflict(f"Visit {v.code} is cancelled")
        if ReadinessEvidenceVoid.objects.filter(evidence=ev).exists():
            raise Conflict(f"Evidence {evidence_code} is already void")
        ReadinessEvidenceVoid.objects.create(evidence=ev, voided_by=actor, reason=reason)
        return Outcome("readiness_evidence", ev.code, {"voided": True, "visit": v.code})

    return run_command(
        actor,
        "readiness.void_evidence",
        permission="readiness.record_evidence",
        payload={"evidence": evidence_code, "reason": reason},
        handler=handler,
    )


# ------------------------------------------------------------------ recovery plan
def record_recovery_plan(
    actor: Any,
    *,
    visit_code: str,
    on_call_code: str,
    escalation_code: str,
    escalation_path: str,
    required_equipment: list[str],
) -> dict[str, Any]:
    payload = {
        "visit": visit_code,
        "on_call": on_call_code,
        "escalation": escalation_code,
        "escalation_path": escalation_path,
        "required_equipment": required_equipment,
    }

    def handler() -> Outcome:
        if not escalation_path.strip():
            raise Invalid("An escalation path is required")
        if not _string_list_ok(required_equipment):
            raise Invalid("required_equipment must be a list of distinct, non-empty names")
        on_call, escalation = _person(on_call_code), _person(escalation_code)
        if on_call.pk == escalation.pk:
            raise Invalid("The escalation contact must differ from the on-call engineer")
        v, robot, manifest_id = _lock_visit_and_robot(
            visit_code, "readiness.record_recovery_plan", share_persons=[on_call.pk, escalation.pk]
        )
        rev = v.current_revision
        assert rev is not None
        plan = RecoveryPlan.objects.create(
            visit=v,
            revision=rev,
            manifest_id=manifest_id,
            on_call_engineer=on_call,
            escalation_contact=escalation,
            escalation_path=escalation_path,
            required_equipment=required_equipment,
            recorded_by=actor,
        )
        return Outcome(
            "recovery_plan",
            str(plan.pk),
            {"visit": v.code, "revision": rev.revision_no, "manifest_id": manifest_id},
        )

    return run_command(
        actor,
        "readiness.record_recovery_plan",
        permission="readiness.record_recovery_plan",
        payload=payload,
        handler=handler,
    )
