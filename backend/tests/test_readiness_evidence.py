"""Command-level tests for readiness/evidence.py: permissions, validation, version bumps, binding to the current
revision and manifest, voids, policy approval rules and withdrawal."""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from fleet import services as fleet_services
from fleet.models import Robot
from planning.models import Visit
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.models import AuditEvent
from readiness import evidence as rx
from readiness.models import (
    ReadinessEvidence,
    ReadinessEvidenceVoid,
    ReadinessPolicy,
    RecoveryPlan,
)
from tests.conftest import make_user
from tests.planning_world import new_visit, plan
from tests.readiness_world import (
    EQUIPMENT_ALL_PRESENT,
    REQUIREMENTS,
    build_readiness,
    record_inspection,
    requirements,
)


@pytest.fixture
def w(transactional_db):
    return build_readiness(prefix="rxe_")


def _version(code: str) -> int:
    return Visit.objects.get(code=code).version


def _battery(w, actor=None, **kw):
    return rx.record_evidence(
        actor or w.field,
        visit_code=w.visit,
        kind="battery_check",
        result="pass",
        details={"soc_pct": 80},
        **kw,
    )


def _reschedule(w, code: str, hours: int = 2) -> None:
    r = Visit.objects.get(code=code).current_revision
    plan(
        w,
        code,
        on_site_start=r.on_site_start + timedelta(hours=hours),
        on_site_end=r.on_site_end + timedelta(hours=hours),
    )


# ================================================================== world
def test_world_has_confirmed_visit_with_all_evidence_and_approved_policy(w):
    v = Visit.objects.get(code=w.visit)
    assert v.status == "confirmed"
    assert set(w.evidence) == {"pre_departure_inspection", "equipment_check", "battery_check"}
    evs = ReadinessEvidence.objects.filter(visit=v)
    assert {e.code for e in evs} == set(w.evidence.values())
    assert all(e.revision_id == v.current_revision_id and e.result == "pass" for e in evs)
    assert RecoveryPlan.objects.get(pk=w.recovery_plan_id).revision_id == v.current_revision_id
    policy = rx.current_policy()
    assert policy is not None and policy.version == w.policy_version and policy.source == "synthetic_demo"
    assert rx.policy_status(policy) == "approved"
    prof = fleet_services.approved_profile(Robot.objects.get(code="R-01").current_manifest.pattern)
    assert prof.version_no == 2 and prof.min_departure_soc_pct == 60


# ================================================================== permissions
@pytest.mark.parametrize(
    "call",
    [
        lambda w: rx.create_readiness_policy(w.ops, requirements=REQUIREMENTS, source="synthetic_demo"),
        lambda w: rx.create_readiness_policy(w.safety, requirements=REQUIREMENTS, source="synthetic_demo"),
        lambda w: rx.approve_readiness_policy(w.ops, version=w.policy_version, slot="engineering"),
        lambda w: rx.withdraw_readiness_policy(w.eng, version=w.policy_version, reason="x"),
        lambda w: _battery(w, actor=w.founder),
        lambda w: _battery(w, actor=w.safety),
        lambda w: rx.void_evidence(w.lead, evidence_code=w.evidence["battery_check"], reason="x"),
        lambda w: rx.record_recovery_plan(
            w.field,
            visit_code=w.visit,
            on_call_code="P-4",
            escalation_code="P-5",
            escalation_path="x",
            required_equipment=[],
        ),
    ],
)
def test_commands_require_their_permission_and_audit_denials(w, call):
    before = AuditEvent.objects.filter(outcome="denied").count()
    with pytest.raises(PermissionDenied):
        call(w)
    assert AuditEvent.objects.filter(outcome="denied").count() == before + 1


def test_expired_role_cannot_record_evidence(w):
    stale = make_user("rxe_stale", "field_operator", expired=True)
    with pytest.raises(PermissionDenied):
        _battery(w, actor=stale)


# ================================================================== policy create
@pytest.mark.parametrize(
    "req",
    [
        {**REQUIREMENTS, "extra": 1},
        {k: v for k, v in REQUIREMENTS.items() if k != "recovery_plan_required"},
        requirements(authorization_ttl_min=0),
        requirements(authorization_ttl_min=True),
        requirements(evidence=[]),
        requirements(evidence=[{"kind": "pre_departure_inspection"}]),
        requirements(evidence=[{"kind": "made_up", "max_age_min": 5}]),
        requirements(
            evidence=[
                {"kind": "battery_check", "max_age_min": 5},
                {"kind": "battery_check", "max_age_min": 6},
            ]
        ),
        requirements(required_equipment=["kit", ""]),
        requirements(recovery_plan_required=1),
    ],
)
def test_create_policy_validates_requirements(w, req):
    n = ReadinessPolicy.objects.count()
    with pytest.raises(Invalid):
        rx.create_readiness_policy(w.lead, requirements=req, source="synthetic_demo")
    assert ReadinessPolicy.objects.count() == n


def test_create_policy_rejects_unknown_source_and_numbers_consecutively(w):
    with pytest.raises(Invalid):
        rx.create_readiness_policy(w.lead, requirements=REQUIREMENTS, source="trust_me")
    r = rx.create_readiness_policy(w.lead, requirements=REQUIREMENTS, source="approved_procedure", notes="n")
    assert r["entity_id"] == str(w.policy_version + 1) and r["status"] == "draft"
    p = ReadinessPolicy.objects.get(version=w.policy_version + 1)
    assert p.lock_version == 1 and rx.policy_status(p) == "draft" and p.created_by == w.lead


# ================================================================== policy approval
def test_approval_rules(w):
    v = w.make_policy(approve=False)
    with pytest.raises(Conflict, match="author"):
        rx.approve_readiness_policy(w.lead, version=v, slot="engineering")
    with pytest.raises(PermissionDenied, match="requires role safety_reviewer"):
        rx.approve_readiness_policy(w.lead2, version=v, slot="safety")
    with pytest.raises(PermissionDenied, match="requires role engineering_lead"):
        rx.approve_readiness_policy(w.safety, version=v, slot="engineering")
    with pytest.raises(Invalid):
        rx.approve_readiness_policy(w.lead2, version=v, slot="founder")
    with pytest.raises(Invalid):
        rx.approve_readiness_policy(w.lead2, version=999, slot="engineering")
    r = rx.approve_readiness_policy(w.lead2, version=v, slot="engineering")
    assert r["status"] == "draft" and r["lock_version"] == 2
    with pytest.raises(Conflict, match="already approved"):
        rx.approve_readiness_policy(make_user("rxe_lead3", "engineering_lead"), version=v, slot="engineering")
    both = make_user("rxe_both", "engineering_lead", "safety_reviewer")
    v2 = w.make_policy(approve=False)
    rx.approve_readiness_policy(both, version=v2, slot="engineering")
    with pytest.raises(Conflict, match="another slot"):
        rx.approve_readiness_policy(both, version=v2, slot="safety")
    r = rx.approve_readiness_policy(w.safety, version=v, slot="safety")
    assert r["status"] == "approved" and r["lock_version"] == 3


def test_newer_approved_policy_supersedes_and_drafts_do_not(w):
    first = w.policy_version
    w.make_policy(approve=False)
    assert rx.current_policy().version == first
    newer = w.make_policy()
    assert rx.current_policy().version == newer
    assert rx.policy_status(ReadinessPolicy.objects.get(version=first)) == "approved"  # status is per policy


def test_current_policy_is_none_without_approved_policy(transactional_db):
    assert rx.current_policy() is None


# ================================================================== policy withdrawal
def test_withdrawal(w):
    first = w.policy_version
    newer = w.make_policy()
    with pytest.raises(Invalid):
        rx.withdraw_readiness_policy(w.safety, version=newer, reason="  ")
    r = rx.withdraw_readiness_policy(w.safety, version=newer, reason="procedure error")
    assert r["status"] == "withdrawn" and r["lock_version"] == 4  # 1 + 2 approvals + withdrawal
    p = ReadinessPolicy.objects.get(version=newer)
    assert rx.policy_status(p) == "withdrawn"
    assert rx.current_policy().version == first  # falls back to the older approved policy
    with pytest.raises(Conflict, match="already withdrawn"):
        rx.withdraw_readiness_policy(w.lead, version=newer, reason="again")
    draft = w.make_policy(approve=False)
    rx.withdraw_readiness_policy(w.lead, version=draft, reason="never mind")
    with pytest.raises(Conflict, match="withdrawn"):
        rx.approve_readiness_policy(w.lead2, version=draft, slot="engineering")
    rx.withdraw_readiness_policy(w.lead, version=first, reason="all gone")
    assert rx.current_policy() is None


# ================================================================== evidence
def test_record_evidence_binds_current_revision_and_manifest_and_bumps_visit(w):
    before = _version(w.visit)
    r = _battery(w)
    ev = ReadinessEvidence.objects.get(code=r["entity_id"])
    v = Visit.objects.get(code=w.visit)
    robot = Robot.objects.get(code="R-01")
    assert r["entity_type"] == "readiness_evidence" and ev.code.startswith("EV-")
    assert ev.revision_id == v.current_revision_id and ev.robot == robot
    assert ev.manifest_id == robot.current_manifest_id and ev.recorded_by == w.field
    assert v.version == before + 1
    audit = AuditEvent.objects.get(command="readiness.record_evidence", entity_id=ev.code)
    assert audit.outcome == "committed"


def test_evidence_follows_reschedule_and_manifest_change(w):
    old_rev = Visit.objects.get(code=w.visit).current_revision_id
    _reschedule(w, w.visit)
    ev = ReadinessEvidence.objects.get(code=_battery(w)["entity_id"])
    assert ev.revision_id != old_rev
    assert ev.revision_id == Visit.objects.get(code=w.visit).current_revision_id
    fleet_services.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a2", safety_config_hash="g" * 64
    )
    ev2 = ReadinessEvidence.objects.get(code=_battery(w)["entity_id"])
    assert ev2.manifest_id == Robot.objects.get(code="R-01").current_manifest_id != ev.manifest_id
    # the inspection was on the old manifest: it can no longer back pre-departure evidence
    with pytest.raises(Conflict, match="different robot or configuration"):
        rx.record_evidence(
            w.ops,
            visit_code=w.visit,
            kind="pre_departure_inspection",
            result="pass",
            inspection_code=w.inspection,
        )


def test_inspection_evidence_rules(w):
    with pytest.raises(Invalid, match="needs an inspection"):
        rx.record_evidence(w.ops, visit_code=w.visit, kind="pre_departure_inspection", result="pass")
    with pytest.raises(Invalid, match="Only pre-departure"):
        rx.record_evidence(
            w.ops,
            visit_code=w.visit,
            kind="battery_check",
            result="pass",
            details={"soc_pct": 80},
            inspection_code=w.inspection,
        )
    with pytest.raises(Invalid, match="No inspection"):
        rx.record_evidence(
            w.ops,
            visit_code=w.visit,
            kind="pre_departure_inspection",
            result="pass",
            inspection_code="RC-9999",
        )
    other_visit = new_visit(w, h0=14, h1=15)
    other_insp = record_inspection(w, other_visit)
    with pytest.raises(Conflict, match="not recorded for visit"):
        rx.record_evidence(
            w.ops,
            visit_code=w.visit,
            kind="pre_departure_inspection",
            result="pass",
            inspection_code=other_insp,
        )
    incomplete = fleet_services.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="T1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=None,
        visit_ref=w.visit,
    )["entity_id"]
    with pytest.raises(Conflict, match="only a complete PASS"):
        rx.record_evidence(
            w.ops,
            visit_code=w.visit,
            kind="pre_departure_inspection",
            result="pass",
            inspection_code=incomplete,
        )
    failed = rx.record_evidence(
        w.ops, visit_code=w.visit, kind="pre_departure_inspection", result="fail", inspection_code=incomplete
    )
    assert ReadinessEvidence.objects.get(code=failed["entity_id"]).result == "fail"
    with pytest.raises(Invalid, match="no details"):
        rx.record_evidence(
            w.ops,
            visit_code=w.visit,
            kind="pre_departure_inspection",
            result="pass",
            inspection_code=w.inspection,
            details={"note": "x"},
        )


def test_inspection_evidence_defaults_observed_at_to_inspection_time(w):
    ev = ReadinessEvidence.objects.get(code=w.evidence["pre_departure_inspection"])
    assert abs((ev.observed_at - ev.inspection.submitted_at).total_seconds()) < 1


@pytest.mark.parametrize(
    "kind, details",
    [
        ("battery_check", {"soc_pct": 0}),
        ("battery_check", {"soc_pct": 101}),
        ("battery_check", {"soc_pct": 50.5}),
        ("battery_check", {"soc_pct": True}),
        ("battery_check", {}),
        ("equipment_check", {"items": {}}),
        ("equipment_check", {"items": {"charger": "probably"}}),
        ("equipment_check", {"items": {"charger": "present"}, "extra": 1}),
        ("equipment_check", {"charger": "present"}),
    ],
)
def test_evidence_details_validation(w, kind, details):
    before = _version(w.visit)
    with pytest.raises(Invalid):
        rx.record_evidence(w.field, visit_code=w.visit, kind=kind, result="pass", details=details)
    assert _version(w.visit) == before


def test_evidence_kind_result_and_observed_at_validation(w):
    with pytest.raises(Invalid, match="kind"):
        rx.record_evidence(w.field, visit_code=w.visit, kind="vibes", result="pass")
    with pytest.raises(Invalid, match="result"):
        rx.record_evidence(
            w.field, visit_code=w.visit, kind="battery_check", result="ok", details={"soc_pct": 9}
        )
    with pytest.raises(Invalid, match="future"):
        _battery(w, observed_at=timezone.now() + timedelta(minutes=5))
    with pytest.raises(Invalid, match="24 hours"):
        _battery(w, observed_at=timezone.now() - timedelta(hours=25))
    with pytest.raises(Invalid, match="timezone-aware"):
        _battery(w, observed_at=timezone.now().replace(tzinfo=None))
    with pytest.raises(Invalid, match="No visit"):
        rx.record_evidence(
            w.field, visit_code="V-9999", kind="battery_check", result="pass", details={"soc_pct": 9}
        )
    when = timezone.now() - timedelta(minutes=30)
    ev = ReadinessEvidence.objects.get(code=_battery(w, observed_at=when)["entity_id"])
    assert ev.observed_at == when


def test_equipment_evidence_with_missing_item_is_recorded_as_given(w):
    items = {**EQUIPMENT_ALL_PRESENT["items"], "charger": "missing"}
    r = rx.record_evidence(
        w.field, visit_code=w.visit, kind="equipment_check", result="fail", details={"items": items}
    )
    assert ReadinessEvidence.objects.get(code=r["entity_id"]).details["items"]["charger"] == "missing"


def test_evidence_refused_for_cancelled_or_unassigned_visit(w):
    requested = new_visit(w, h0=14, h1=15)  # no robot yet
    with pytest.raises(Conflict, match="no robot"):
        rx.record_evidence(
            w.field, visit_code=requested, kind="battery_check", result="pass", details={"soc_pct": 80}
        )
    w.p.cancel_visit(w.ops, visit_code=w.visit, reason="test")
    with pytest.raises(Conflict, match="cancelled"):
        _battery(w)
    with pytest.raises(Conflict, match="cancelled"):
        rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="x")


def test_record_evidence_retries_when_visit_is_replanned_between_read_and_lock(w, monkeypatch):
    """A reschedule commits after record_evidence read the visit but before it took its locks: the command
    restarts (RetryTransaction) and binds to the NEW revision."""
    real_lock = rx.lock_guards
    calls = {"n": 0}

    def racing_lock(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            t = threading.Thread(target=lambda: (_reschedule(w, w.visit), connection.close()))
            t.start()
            t.join(30)
        real_lock(**kw)

    monkeypatch.setattr(rx, "lock_guards", racing_lock)
    r = _battery(w)
    assert calls["n"] == 2
    ev = ReadinessEvidence.objects.get(code=r["entity_id"])
    assert ev.revision_id == Visit.objects.get(code=w.visit).current_revision_id
    assert ev.revision.revision_no == 3


# ================================================================== voids
def test_void_evidence(w):
    code = w.evidence["battery_check"]
    before = _version(w.visit)
    with pytest.raises(Invalid):
        rx.void_evidence(w.ops, evidence_code=code, reason=" ")
    with pytest.raises(Invalid, match="No evidence"):
        rx.void_evidence(w.ops, evidence_code="EV-9999", reason="x")
    r = rx.void_evidence(w.field, evidence_code=code, reason="wrong battery")
    assert r["voided"] is True
    void = ReadinessEvidenceVoid.objects.get(evidence__code=code)
    assert void.voided_by == w.field and void.reason == "wrong battery"
    assert _version(w.visit) == before + 1
    with pytest.raises(Conflict, match="already void"):
        rx.void_evidence(w.ops, evidence_code=code, reason="again")


# ================================================================== recovery plan
def test_record_recovery_plan(w):
    before = _version(w.visit)
    r = rx.record_recovery_plan(
        w.lead,
        visit_code=w.visit,
        on_call_code="P-4",
        escalation_code="P-1",
        escalation_path="on-call -> ops lead",
        required_equipment=["recovery_kit", "jack"],
    )
    plan_row = RecoveryPlan.objects.get(pk=int(r["entity_id"]))
    v = Visit.objects.get(code=w.visit)
    assert plan_row.revision_id == v.current_revision_id
    assert plan_row.manifest_id == Robot.objects.get(code="R-01").current_manifest_id
    assert plan_row.on_call_engineer.code == "P-4" and plan_row.escalation_contact.code == "P-1"
    assert v.version == before + 1


@pytest.mark.parametrize(
    "kw, exc",
    [
        ({"escalation_code": "P-4"}, Invalid),
        ({"escalation_path": "  "}, Invalid),
        ({"required_equipment": ["a", "a"]}, Invalid),
        ({"required_equipment": [""]}, Invalid),
        ({"on_call_code": "P-99"}, Invalid),
        ({"visit_code": "V-9999"}, Invalid),
    ],
)
def test_recovery_plan_validation(w, kw, exc):
    args = {
        "visit_code": w.visit,
        "on_call_code": "P-4",
        "escalation_code": "P-5",
        "escalation_path": "path",
        "required_equipment": ["recovery_kit"],
        **kw,
    }
    n = RecoveryPlan.objects.count()
    with pytest.raises(exc):
        rx.record_recovery_plan(w.ops, **args)
    assert RecoveryPlan.objects.count() == n


def test_recovery_plan_refused_for_cancelled_visit(w):
    w.p.cancel_visit(w.ops, visit_code=w.visit, reason="test")
    with pytest.raises(Conflict, match="cancelled"):
        rx.record_recovery_plan(
            w.ops,
            visit_code=w.visit,
            on_call_code="P-4",
            escalation_code="P-5",
            escalation_path="path",
            required_equipment=[],
        )
