"""Readiness engine (Engineer B): every gate of readiness.evaluator, the guard snapshot, the expiry computation
and the guarantee that evaluation writes nothing. Built on tests/readiness_world.py. SYNTHETIC data only."""

from datetime import timedelta

import pytest
from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from fleet import services as fleet_services
from fleet.models import CapabilityProfile, Robot
from planning.models import ConsentRecord, Person, Qualification, Reservation, Visit
from platform_core.models import AuditEvent
from readiness import evaluator as ev
from readiness import evidence as rx
from readiness.models import ReadinessEvidence, ReadinessPolicy, RecoveryPlan
from tests.fleet_world import set_setting
from tests.planning_world import DAY, plan
from tests.readiness_world import (
    EQUIPMENT_ALL_PRESENT,
    RECOVERY_EQUIPMENT,
    build_readiness,
    new_confirmed_visit,
    record_inspection,
    requirements,
)


@pytest.fixture
def w(transactional_db):
    return build_readiness(prefix="rxg_")


# ------------------------------------------------------------------ helpers
def _visit(code: str) -> Visit:
    return Visit.objects.get(code=code)


def _eval(code: str, at=None) -> ev.ReadinessResult:
    return ev.evaluate_readiness(_visit(code), at=at)


def _codes(res: ev.ReadinessResult) -> set[str]:
    return {b["code"] for b in res.blockers}


def _only(res: ev.ReadinessResult, code: str) -> list[dict[str, str]]:
    return [b for b in res.blockers if b["code"] == code]


def _evidence(w, kind: str) -> ReadinessEvidence:
    return ReadinessEvidence.objects.get(code=w.evidence[kind])


def _blocked_by(res: ev.ReadinessResult, *codes: str) -> None:
    assert not res.ready and res.label == ev.LABEL_INCOMPLETE
    assert set(codes) <= _codes(res), res.blockers


def _reschedule_and_reconfirm(w, code: str, hours: int = 2) -> None:
    r = _visit(code).current_revision
    plan(
        w,
        code,
        on_site_start=r.on_site_start + timedelta(hours=hours),
        on_site_end=r.on_site_end + timedelta(hours=hours),
    )
    w.p.confirm_visit(w.ops, visit_code=code)


def _record_evidence_only(w, code: str, robot: str = "R-01") -> None:
    """All three passing evidence kinds for the visit, but NO recovery plan."""
    insp = record_inspection(w, code, robot=robot)
    rx.record_evidence(
        w.ops, visit_code=code, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    rx.record_evidence(
        w.field, visit_code=code, kind="equipment_check", result="pass", details=EQUIPMENT_ALL_PRESENT
    )
    rx.record_evidence(w.field, visit_code=code, kind="battery_check", result="pass", details={"soc_pct": 85})


def _changed(before: dict, after: dict) -> set[str]:
    return {k for k in before if before[k] != after[k]}


# ================================================================== fully ready
def test_fully_ready_world_is_ready_with_snapshot_evidence_and_expiry(w):
    at = ev.db_now()
    res = _eval(w.visit, at=at)
    assert res.ready, res.blockers
    assert res.label == "Ready to request authorization" and res.blockers == []
    policy = ReadinessPolicy.objects.get(version=w.policy_version)
    assert (res.policy_id, res.policy_version) == (policy.pk, policy.version)
    assert res.evidence_ids == sorted(_evidence(w, k).pk for k in w.evidence)
    assert res.recovery_plan_id == w.recovery_plan_id
    robot = Robot.objects.get(code="R-01")
    assert res.manifest_id == robot.current_manifest_id

    v = _visit(w.visit)
    r = v.current_revision
    rp = RecoveryPlan.objects.get(pk=w.recovery_plan_id)
    snap = res.guard_snapshot
    assert snap == {
        "visit_id": v.pk,
        "visit_version": v.version,
        "revision_id": r.pk,
        "visit_status": "confirmed",
        "site_id": v.site_id,
        "site_version": v.site.version,
        "robot_id": robot.pk,
        "robot_version": robot.version,
        "manifest_id": robot.current_manifest_id,
        "pattern_id": robot.current_manifest.pattern_id,
        "pattern_version": robot.current_manifest.pattern.version,
        # Slice 7 (07a 7.2, P0-1): person_versions also holds the signers of the relied pre-departure inspection
        # (here also the relied periodic inspection): P-2 is crew AND signer (one entry), P-3 is signer only.
        "person_versions": {
            str(p.pk): p.version
            for p in (
                r.lead,
                r.second,
                rp.on_call_engineer,
                rp.escalation_contact,
                Person.objects.get(code="P-3"),
            )
        },
        "vehicle_id": r.vehicle_id,
        "policy_id": policy.pk,
        "policy_lock_version": policy.lock_version,
    }
    assert set(snap["person_versions"]) == {
        str(x)
        for x in (
            r.lead_id,
            r.second_id,
            rp.on_call_engineer_id,
            rp.escalation_contact_id,
            Person.objects.get(code="P-3").pk,
        )
    }
    assert policy.lock_version == 3  # created 1, +1 per approval slot
    assert snap == ev.take_guard_snapshot(v)
    # TTL 90 min is shorter than every evidence age bound right after recording.
    assert res.expires_at == at + timedelta(minutes=90)


def test_default_time_is_database_time(w):
    before = ev.db_now()
    res = _eval(w.visit)
    after = ev.db_now()
    assert before + timedelta(minutes=90) <= res.expires_at <= after + timedelta(minutes=90)


def test_evaluator_writes_nothing_and_takes_no_locks(w):
    def counts() -> dict[str, int]:
        return {m._meta.label: m.objects.count() for m in apps.get_models()}

    before = counts()
    audits = AuditEvent.objects.count()
    version = _visit(w.visit).version
    with CaptureQueriesContext(connection) as ctx:
        ev.evaluate_readiness(_visit(w.visit))
        ev.take_guard_snapshot(_visit(w.visit))
    sql = [q["sql"].upper() for q in ctx.captured_queries]
    assert sql and all(s.lstrip().startswith(("SELECT", "WITH")) for s in sql), sql
    assert not any("FOR UPDATE" in s or "FOR SHARE" in s or "FOR NO KEY" in s for s in sql)
    assert counts() == before
    assert AuditEvent.objects.count() == audits
    assert _visit(w.visit).version == version


# ================================================================== gate 1: visit
def test_cancelled_visit_is_not_confirmed(w):
    w.p.cancel_visit(w.ops, visit_code=w.visit, reason="household asked")
    res = _eval(w.visit)
    _blocked_by(res, "VISIT_NOT_CONFIRMED", "RESERVATIONS_MISSING")
    assert res.guard_snapshot["visit_status"] == "cancelled"


def test_planned_but_unconfirmed_visit_blocks(w):
    code = w.p.request_visit(
        w.ops,
        site_code="S-1",
        on_site_start=_visit(w.visit).current_revision.on_site_start + timedelta(days=1),
        on_site_end=_visit(w.visit).current_revision.on_site_end + timedelta(days=1),
        travel_out_min=30,
        travel_back_min=30,
        tasks=["T-04"],
        required_scopes=["presence"],
    )["entity_id"]
    plan(w, code, robot="R-02")
    res = _eval(code)
    _blocked_by(res, "VISIT_NOT_CONFIRMED", "EVIDENCE_MISSING", "RECOVERY_PLAN_MISSING")
    assert res.evidence_ids == [] and res.recovery_plan_id is None


# ================================================================== gate 2: live planning
def test_robot_hold_makes_planning_infeasible(w):
    fleet_services.place_hold(
        w.ops, scope="robot", target="R-01", source_type="maintenance", reason="hip noise"
    )
    res = _eval(w.visit)
    _blocked_by(res, "PLANNING_ROBOT_NOT_ELIGIBLE")
    b = _only(res, "PLANNING_ROBOT_NOT_ELIGIBLE")[0]
    assert b["level"] == "blocked" and "hip noise" in b["message"] and b["action"]


def test_consent_withdrawal_makes_planning_infeasible(w):
    c = ConsentRecord.objects.filter(site__code="S-1", withdrawn_at__isnull=True).first()
    w.p.withdraw_consent(w.field, consent_id=c.pk, reason="changed mind")
    _blocked_by(_eval(w.visit), "PLANNING_CONSENT")


def test_qualification_revoked_makes_planning_infeasible(w):
    q = Qualification.objects.get(person__code="P-1", kind="field_lead", revoked_at__isnull=True)
    w.p.revoke_qualification(w.ops, qualification_id=q.pk, reason="lapsed")
    res = _eval(w.visit)
    _blocked_by(res, "PLANNING_QUALIFICATION")
    assert all(b["code"].startswith("PLANNING_") for b in res.blockers)


# ================================================================== gate 3: reservations
def test_released_reservation_blocks(w):
    rev = _visit(w.visit).current_revision
    n = Reservation.objects.filter(revision=rev, purpose="on_site").update(
        released_at=timezone.now(), released_reason="test"
    )
    assert n == 1
    res = _eval(w.visit)
    assert _codes(res) == {"RESERVATIONS_MISSING"}
    assert "released" in res.blockers[0]["message"] and "on_site" in res.blockers[0]["message"]


# ================================================================== gate 4: policy
def test_no_policy_blocks(w):
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="replaced")
    res = _eval(w.visit)
    assert _codes(res) == {"POLICY_MISSING"}
    assert res.policy_id is None and res.policy_version is None and res.expires_at is None
    assert res.guard_snapshot["policy_id"] is None and res.guard_snapshot["policy_lock_version"] is None
    assert "withdrawn" in res.blockers[0]["message"]


def test_draft_policy_does_not_count(w):
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="replaced")
    v2 = w.make_policy(approve=False)
    rx.approve_readiness_policy(w.lead2, version=v2, slot="engineering")  # one slot only: still draft
    res = _eval(w.visit)
    assert _codes(res) == {"POLICY_MISSING"}
    assert f"v{v2}, draft" in res.blockers[0]["message"]


def test_withdrawn_policy_does_not_fall_back_to_older_approved_policy(w):
    v2 = w.make_policy()
    assert _eval(w.visit).policy_version == v2
    rx.withdraw_readiness_policy(w.safety, version=v2, reason="bad numbers")
    res = _eval(w.visit)  # v1 is still approved, but it is never used again (EM decision: no fallback)
    assert not res.ready and _codes(res) == {"POLICY_MISSING"}
    assert res.policy_id is None and res.policy_version is None
    assert f"v{v2} was withdrawn" in res.blockers[0]["message"]


def test_superseded_policy_is_not_used(w):
    old = ReadinessPolicy.objects.get(version=w.policy_version)
    v2 = w.make_policy(requirements=requirements(authorization_ttl_min=30))
    new = ReadinessPolicy.objects.get(version=v2)
    at = ev.db_now()
    res = _eval(w.visit, at=at)
    assert res.ready and res.policy_id == new.pk != old.pk
    assert res.guard_snapshot["policy_id"] == new.pk
    assert res.expires_at == at + timedelta(minutes=30)  # the new policy's TTL, not the superseded one's


def test_production_mode_refuses_synthetic_policy(w):
    set_setting("demo_mode", "false")
    res = _eval(w.visit)
    _blocked_by(res, "POLICY_NOT_APPROVED_PROCEDURE")
    assert res.policy_version == w.policy_version  # still reported, never treated as clearance


def test_production_mode_accepts_approved_procedure_policy_gate(w):
    w.make_policy(source="approved_procedure")
    set_setting("demo_mode", "false")
    res = _eval(w.visit)
    assert "POLICY_NOT_APPROVED_PROCEDURE" not in _codes(res)
    # The synthetic capability profile still makes the robot ineligible in production (Slice 1).
    assert "PLANNING_ROBOT_NOT_ELIGIBLE" in _codes(res)


def test_demo_mode_accepts_synthetic_policy(w):
    assert "POLICY_NOT_APPROVED_PROCEDURE" not in _codes(_eval(w.visit))


# ================================================================== gate 5: evidence
def test_missing_evidence_on_a_fresh_visit(w):
    code = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=1))
    res = _eval(code)
    assert len(_only(res, "EVIDENCE_MISSING")) == 3
    _blocked_by(res, "RECOVERY_PLAN_MISSING")
    assert "BATTERY_LOW" not in _codes(res) and "EQUIPMENT_MISSING" not in _codes(res)  # not repeated
    assert res.evidence_ids == []


def test_failed_evidence_blocks(w):
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="equipment_check", result="fail", details=EQUIPMENT_ALL_PRESENT
    )
    res = _eval(w.visit)
    assert _codes(res) == {"EVIDENCE_FAILED"}
    assert _evidence(w, "equipment_check").pk not in res.evidence_ids


def test_stale_evidence_blocks(w):
    battery = _evidence(w, "battery_check")
    res = _eval(w.visit, at=battery.observed_at + timedelta(minutes=121))
    assert _codes(res) == {"EVIDENCE_STALE"}
    assert "Battery check" in res.blockers[0]["message"]
    assert battery.pk not in res.evidence_ids


def test_stale_by_observed_at_blocks_even_if_recorded_now(w):
    rx.record_evidence(
        w.field,
        visit_code=w.visit,
        kind="battery_check",
        result="pass",
        details={"soc_pct": 90},
        observed_at=ev.db_now() - timedelta(hours=3),
    )
    # The older observation is not the latest; void the fresh one so the stale one is the latest left.
    rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="wrong robot read")
    assert _codes(_eval(w.visit)) == {"EVIDENCE_STALE"}


def test_voided_evidence_blocks(w):
    rx.void_evidence(w.ops, evidence_code=w.evidence["pre_departure_inspection"], reason="wrong checklist")
    res = _eval(w.visit)
    assert _codes(res) == {"EVIDENCE_MISSING"}
    assert "voided" in res.blockers[0]["message"]
    assert _evidence(w, "pre_departure_inspection").pk not in res.evidence_ids


def test_evidence_for_older_revision_after_reschedule(w):
    _reschedule_and_reconfirm(w, w.visit)
    res = _eval(w.visit)
    missing = _only(res, "EVIDENCE_MISSING")
    assert len(missing) == 3 and all("earlier plan revision" in b["message"] for b in missing)
    assert _codes(res) == {"EVIDENCE_MISSING", "RECOVERY_PLAN_MISSING"}
    assert res.evidence_ids == [] and res.recovery_plan_id is None
    # Fresh evidence for the new revision restores readiness.
    w.record_all_evidence(w.visit)
    assert _eval(w.visit).ready


def test_evidence_for_old_manifest_after_manifest_change(w):
    fleet_services.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a2", safety_config_hash="h" * 64
    )
    res = _eval(w.visit)
    assert len(_only(res, "EVIDENCE_WRONG_CONFIGURATION")) == 3
    assert _codes(res) == {"EVIDENCE_WRONG_CONFIGURATION", "RECOVERY_PLAN_MISSING"}
    assert res.manifest_id == Robot.objects.get(code="R-01").current_manifest_id
    assert res.evidence_ids == []
    w.record_all_evidence(w.visit)
    assert _eval(w.visit).ready


# ================================================================== gate 6: battery
def test_battery_below_minimum_blocks(w):
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result="pass", details={"soc_pct": 59}
    )
    res = _eval(w.visit)
    assert _codes(res) == {"BATTERY_LOW"}
    assert "59%" in res.blockers[0]["message"] and "60%" in res.blockers[0]["message"]


def test_battery_at_minimum_is_enough(w):
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result="pass", details={"soc_pct": 60}
    )
    assert _eval(w.visit).ready


def test_battery_minimum_unknown_blocks(w):
    fleet_services.publish_capability_profile(
        w.eng,
        pattern_code="P-A",
        stop_behaviour="SYNTHETIC",
        lift_crew=2,
        max_runtime_min=120,
        charge_turnaround_min=180,
        load_unload_min=20,
    )  # v3, no min_departure_soc_pct
    prof = CapabilityProfile.objects.get(pattern__code="P-A", version_no=3)
    fleet_services.approve_capability_profile(w.lead, profile_id=prof.pk, slot="engineering")
    fleet_services.approve_capability_profile(w.safety, profile_id=prof.pk, slot="safety")
    res = _eval(w.visit)
    assert _codes(res) == {"BATTERY_MIN_UNKNOWN"}


def test_battery_reading_is_required_even_if_policy_omits_it(w):
    reqs = requirements(evidence=[{"kind": "pre_departure_inspection", "max_age_min": 240}])
    w.make_policy(requirements=reqs)
    res = _eval(w.visit)
    assert res.ready  # the world's battery and equipment checks still satisfy gates 6 and 7
    rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="misread")
    res = _eval(w.visit)
    assert _codes(res) == {"BATTERY_LOW"}


# ================================================================== gate 7: equipment
def test_equipment_check_pass_with_policy_item_missing(w):
    rx.record_evidence(
        w.field,
        visit_code=w.visit,
        kind="equipment_check",
        result="pass",
        details={"items": {"restraint_kit": "present", "recovery_kit": "present", "charger": "missing"}},
    )
    res = _eval(w.visit)
    assert _codes(res) == {"EQUIPMENT_MISSING"}
    assert "marked missing: charger" in res.blockers[0]["message"]


def test_equipment_check_that_does_not_mention_an_item(w):
    rx.record_evidence(
        w.field,
        visit_code=w.visit,
        kind="equipment_check",
        result="pass",
        details={"items": {"restraint_kit": "present", "charger": "present"}},
    )
    res = _eval(w.visit)
    assert _codes(res) == {"EQUIPMENT_MISSING"}
    assert "not checked: recovery_kit" in res.blockers[0]["message"]


def test_equipment_check_missing_recovery_plan_item(w):
    rx.record_recovery_plan(
        w.ops,
        visit_code=w.visit,
        on_call_code="P-4",
        escalation_code="P-5",
        escalation_path="SYNTHETIC: on-call -> lead",
        required_equipment=[*RECOVERY_EQUIPMENT, "tow_strap"],
    )
    res = _eval(w.visit)
    assert _codes(res) == {"EQUIPMENT_MISSING"}
    assert "tow_strap" in res.blockers[0]["message"]
    assert res.recovery_plan_id == RecoveryPlan.objects.order_by("-pk").first().pk


# ================================================================== gate 8: recovery plan
def test_recovery_plan_missing_blocks(w):
    code = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=1))
    _record_evidence_only(w, code, robot="R-02")
    res = _eval(code)
    assert _codes(res) == {"RECOVERY_PLAN_MISSING"}
    assert res.recovery_plan_id is None
    r = _visit(code).current_revision
    # Slice 7 (07a 7.2): plus the signer-only person P-3 of the relied inspections (no recovery plan people)
    assert set(res.guard_snapshot["person_versions"]) == {
        str(r.lead_id),
        str(r.second_id),
        str(Person.objects.get(code="P-3").pk),
    }


def test_recovery_plan_not_required_by_policy(w):
    w.make_policy(requirements=requirements(recovery_plan_required=False))
    code = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=1))
    _record_evidence_only(w, code, robot="R-02")
    res = _eval(code)
    assert res.ready and res.recovery_plan_id is None


def test_recovery_on_call_not_engineer_is_review(w):
    rx.record_recovery_plan(
        w.ops,
        visit_code=w.visit,
        on_call_code="P-1",  # an ops lead, not an engineer
        escalation_code="P-5",
        escalation_path="SYNTHETIC: on-call -> lead",
        required_equipment=list(RECOVERY_EQUIPMENT),
    )
    res = _eval(w.visit)
    assert not res.ready and res.label == ev.LABEL_INCOMPLETE
    assert res.blockers == [
        {
            "level": "review",
            "code": "RECOVERY_ONCALL_NOT_ENGINEER",
            "message": res.blockers[0]["message"],
            "action": res.blockers[0]["action"],
        }
    ]
    assert "P-1" in res.blockers[0]["message"]


def test_recovery_on_call_engineer_role_revoked_is_review(w):
    w.eng.role_grants.filter(role="engineer").update(revoked_at=timezone.now())
    assert _codes(_eval(w.visit)) == {"RECOVERY_ONCALL_NOT_ENGINEER"}


# ================================================================== gate 9: expiry
def test_expiry_is_shortest_of_ttl_and_evidence_ages(w):
    battery = _evidence(w, "battery_check")
    at = battery.observed_at + timedelta(minutes=100)
    res = _eval(w.visit, at=at)
    assert res.ready
    assert res.expires_at == battery.observed_at + timedelta(minutes=120)  # 20 min left, < TTL 90


def test_expiry_uses_ttl_when_evidence_is_fresh(w):
    w.make_policy(requirements=requirements(authorization_ttl_min=5))
    at = ev.db_now()
    assert _eval(w.visit, at=at).expires_at == at + timedelta(minutes=5)


def test_expiry_not_positive_at_the_exact_age_boundary(w):
    battery = _evidence(w, "battery_check")
    at = battery.observed_at + timedelta(minutes=120)  # not stale yet (>=), but zero time left
    res = _eval(w.visit, at=at)
    assert _codes(res) == {"EXPIRY_NOT_POSITIVE"}
    assert res.expires_at == at


# ================================================================== snapshot changes per guard
def test_snapshot_robot_version_changes_on_hold(w):
    before = ev.take_guard_snapshot(_visit(w.visit))
    fleet_services.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="check")
    assert _changed(before, ev.take_guard_snapshot(_visit(w.visit))) == {"robot_version"}


def test_snapshot_site_version_changes_on_consent(w):
    before = ev.take_guard_snapshot(_visit(w.visit))
    c = ConsentRecord.objects.filter(site__code="S-1", withdrawn_at__isnull=True).first()
    w.p.withdraw_consent(w.field, consent_id=c.pk, reason="changed mind")
    assert _changed(before, ev.take_guard_snapshot(_visit(w.visit))) == {"site_version"}


def test_snapshot_person_versions_change_on_qualification(w):
    before = ev.take_guard_snapshot(_visit(w.visit))
    q = Qualification.objects.get(person__code="P-2", kind="field_operator", revoked_at__isnull=True)
    w.p.revoke_qualification(w.ops, qualification_id=q.pk, reason="lapsed")
    after = ev.take_guard_snapshot(_visit(w.visit))
    assert _changed(before, after) == {"person_versions"}
    pid = str(q.person_id)
    assert after["person_versions"][pid] == before["person_versions"][pid] + 1


def test_snapshot_person_versions_cover_recovery_plan_people(w):
    before = ev.take_guard_snapshot(_visit(w.visit))
    w.p.grant_qualification(
        w.ops,
        person_code="P-4",
        kind="robot_handler",
        valid_from=timezone.now(),
        valid_to=timezone.now() + timedelta(days=30),
    )
    assert _changed(before, ev.take_guard_snapshot(_visit(w.visit))) == {"person_versions"}


def test_snapshot_visit_version_changes_on_reschedule_and_evidence(w):
    s0 = ev.take_guard_snapshot(_visit(w.visit))
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result="pass", details={"soc_pct": 80}
    )
    s1 = ev.take_guard_snapshot(_visit(w.visit))
    assert _changed(s0, s1) == {"visit_version"}
    rx.void_evidence(w.ops, evidence_code=w.evidence["equipment_check"], reason="redo")
    s2 = ev.take_guard_snapshot(_visit(w.visit))
    assert _changed(s1, s2) == {"visit_version"}
    _reschedule_and_reconfirm(w, w.visit)
    s3 = ev.take_guard_snapshot(_visit(w.visit))
    assert {"visit_version", "revision_id"} <= _changed(s2, s3)
    assert s3["visit_version"] > s2["visit_version"]


def test_snapshot_policy_changes_on_approval_and_withdrawal(w):
    p1 = ReadinessPolicy.objects.get(version=w.policy_version)
    s0 = ev.take_guard_snapshot(_visit(w.visit))
    assert (s0["policy_id"], s0["policy_lock_version"]) == (p1.pk, p1.lock_version)
    v2 = w.make_policy(approve=False)
    rx.approve_readiness_policy(w.lead2, version=v2, slot="engineering")
    assert ev.take_guard_snapshot(_visit(w.visit)) == s0  # a draft is not a guard
    rx.approve_readiness_policy(w.safety, version=v2, slot="safety")
    p2 = ReadinessPolicy.objects.get(version=v2)
    s1 = ev.take_guard_snapshot(_visit(w.visit))
    assert _changed(s0, s1) == {"policy_id"} and s1["policy_id"] == p2.pk
    assert s1["policy_lock_version"] == p2.lock_version == 3
    rx.withdraw_readiness_policy(w.safety, version=v2, reason="numbers wrong")
    p2.refresh_from_db()
    assert p2.lock_version == 4  # withdrawal bumped the guard version of the policy C would hold
    s2 = ev.take_guard_snapshot(_visit(w.visit))
    assert s2 != s1 and (s2["policy_id"], s2["policy_lock_version"]) == (None, None)  # no fallback to p1
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="retired")
    s3 = ev.take_guard_snapshot(_visit(w.visit))
    assert (s3["policy_id"], s3["policy_lock_version"]) == (None, None)


def test_evaluate_snapshot_matches_take_guard_snapshot_on_stale_object(w):
    stale = _visit(w.visit)
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result="pass", details={"soc_pct": 80}
    )
    res = ev.evaluate_readiness(stale)  # the evaluator re-reads; it does not trust the passed object
    assert res.guard_snapshot == ev.take_guard_snapshot(_visit(w.visit))
    assert res.guard_snapshot["visit_version"] == _visit(w.visit).version


def test_blocker_shape_and_known_codes(w):
    rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="x")
    set_setting("demo_mode", "false")
    res = _eval(w.visit)
    assert res.blockers
    for b in res.blockers:
        assert set(b) == {"level", "code", "message", "action"}
        assert b["level"] in ("blocked", "review") and b["message"] and b["action"]
        assert b["code"] in ev.BLOCKER_CODES or b["code"].startswith("PLANNING_")
