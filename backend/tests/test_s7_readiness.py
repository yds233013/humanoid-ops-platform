"""Slice 7 readiness side (Engineer B): readiness_inspector grants (X-8), the evidence refusals (X-1, X-8), the
evaluator gates and blocker codes (X-1, X-3, X-8), dependency persons, time-dependent eligibility (X-3) and
escalation deadline-set commands (X-4). SYNTHETIC data only; every approval is a test approval.

Contract: docs/07a_production_guardrails_contract.md sections 3, 4, 6, 7 and 11.8 (cases a, f, h).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import connection
from django.utils import timezone

from execution import escalation_deadlines as ed
from execution.models import EscalationDeadlineSet, IncidentPolicy
from fleet import services as fs
from fleet.models import (
    CapabilityProfile,
    Inspection,
    InspectionTemplate,
    PeriodicInspectionRule,
    PeriodicInspectionRuleWithdrawal,
    Robot,
)
from planning import services as ps
from planning.models import Person, Qualification, Visit
from platform_core.commands import Conflict, Invalid, PermissionDenied
from readiness import commands as rc
from readiness import evaluator as ev
from readiness import evidence as rx
from readiness.models import DepartureAuthorization
from tests import readiness_world
from tests.conftest import make_user
from tests.fleet_world import make_engineering_template, make_periodic_rule, set_setting
from tests.readiness_world import REQUIREMENTS, requirements

pytestmark = pytest.mark.django_db


# ================================================================== world
def submit(
    w: Any, robot: str = "R-01", template: str = "T1", by: tuple[Any, Any] | None = None, **kw: Any
) -> str:
    perf, ver = by or (w.field, w.field2)
    return fs.submit_inspection(
        w.ops,
        robot_code=robot,
        template_version=template,
        answers=dict(w.all_pass),
        performer=perf.username,
        verifier=ver.username,
        **kw,
    )["entity_id"]


def build_b7(monkeypatch: Any, prefix: str) -> SimpleNamespace:
    """tests.readiness_world.build_readiness. The shared worlds now record one SYNTHETIC periodic inspection per
    robot right after the inspector grants and before any visit is planned (X-3, EM decision 7; integration of
    Engineer B's change request), so no per-test patch is needed. ``monkeypatch`` is kept for the callers."""
    del monkeypatch
    return readiness_world.build_readiness(prefix=prefix)


@pytest.fixture
def w(monkeypatch):
    return build_b7(monkeypatch, "r7_")


def _one(sql: str, params: list[Any] | None = None) -> Any:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()[0]


def codes(visit: str) -> list[str]:
    return [b["code"] for b in ev.evaluate_readiness(Visit.objects.get(code=visit)).blockers]


def inspector(person: str) -> Qualification:
    return Qualification.objects.get(person__code=person, kind="readiness_inspector", revoked_at__isnull=True)


def production_world(w: Any) -> None:
    """SYNTHETIC production fixtures (test approvals by test users): approved_procedure readiness policy,
    engineering_doc capability profile and periodic rule, and an engineering_doc template E1 approved for both
    purposes. The mode is still demo; callers switch."""
    w.make_policy(source="approved_procedure")
    fs.publish_capability_profile(
        w.eng,
        pattern_code="P-A",
        source="engineering_doc",
        stop_behaviour="SYNTHETIC",
        lift_crew=2,
        max_runtime_min=120,
        charge_turnaround_min=180,
        load_unload_min=20,
        min_departure_soc_pct=60,
    )
    prof = CapabilityProfile.objects.filter(pattern__code="P-A").order_by("-version_no")[0]
    fs.approve_capability_profile(w.lead, profile_id=prof.pk, slot="engineering")
    fs.approve_capability_profile(w.safety, profile_id=prof.pk, slot="safety")
    make_periodic_rule(w, "P-A", source="engineering_doc")
    make_engineering_template(w, "P-A", "E1")


def record_rest(w: Any, visit: str) -> None:
    rx.record_evidence(
        w.field,
        visit_code=visit,
        kind="equipment_check",
        result="pass",
        details=readiness_world.EQUIPMENT_ALL_PRESENT,
    )
    rx.record_evidence(
        w.field, visit_code=visit, kind="battery_check", result="pass", details={"soc_pct": 85}
    )
    rx.record_recovery_plan(
        w.ops,
        visit_code=visit,
        on_call_code="P-4",
        escalation_code="P-5",
        escalation_path="SYNTHETIC path",
        required_equipment=["recovery_kit"],
    )


# ================================================================== X-8 grants (case h)
def test_inspector_grant_permissions_and_refusals(w):
    p4 = "P-4"
    far = datetime(2027, 6, 1, tzinfo=UTC)
    with pytest.raises(PermissionDenied):  # ops_lead manages people but never grants inspectors
        ps.grant_qualification(
            w.ops, person_code=p4, kind="readiness_inspector", valid_from=None, valid_to=far, basis_ref="T"
        )
    with pytest.raises(Invalid, match="basis_ref"):
        ps.grant_qualification(
            w.safety, person_code=p4, kind="readiness_inspector", valid_from=None, valid_to=far, basis_ref=" "
        )
    with pytest.raises(Invalid, match="backdated"):
        ps.grant_qualification(
            w.safety,
            person_code=p4,
            kind="readiness_inspector",
            valid_from=timezone.now() - timedelta(minutes=5),
            valid_to=far,
            basis_ref="SYNTHETIC-TRAINING",
        )
    ps.register_person(w.ops, username=w.safety.username, code="P-S")
    with pytest.raises(Conflict, match="oneself"):
        ps.grant_qualification(
            w.safety,
            person_code="P-S",
            kind="readiness_inspector",
            valid_from=None,
            valid_to=far,
            basis_ref="T",
        )
    with pytest.raises(Invalid, match="valid_from is required"):
        ps.grant_qualification(w.ops, person_code=p4, kind="field_lead", valid_from=None, valid_to=far)
    with pytest.raises(Invalid, match="Unknown qualification"):
        ps.grant_qualification(w.ops, person_code=p4, kind="checker", valid_from=timezone.now(), valid_to=far)


def test_immediate_grant_takes_database_time_even_with_the_app_clock_ahead(w, monkeypatch):
    """Case h (P1-6): valid_from None -> valid_from = granted_at = one database clock_timestamp()."""
    real_now = timezone.now
    monkeypatch.setattr(timezone, "now", lambda: real_now() + timedelta(hours=3))  # app clock runs ahead
    before = _one("SELECT clock_timestamp()")
    out = ps.grant_qualification(
        w.safety,
        person_code="P-4",
        kind="readiness_inspector",
        valid_from=None,
        valid_to=datetime(2027, 6, 1, tzinfo=UTC),
        basis_ref="SYNTHETIC-TRAINING",
    )
    after = _one("SELECT clock_timestamp()")
    q = Qualification.objects.get(pk=int(out["entity_id"]))
    assert q.valid_from == q.granted_at and before <= q.granted_at <= after
    monkeypatch.setattr(timezone, "now", real_now)
    future = _one("SELECT clock_timestamp()") + timedelta(days=1)
    out = ps.grant_qualification(
        w.safety,
        person_code="P-5",
        kind="readiness_inspector",
        valid_from=future,
        valid_to=datetime(2027, 6, 1, tzinfo=UTC),
        basis_ref="SYNTHETIC-TRAINING",
    )
    q = Qualification.objects.get(pk=int(out["entity_id"]))
    assert q.valid_from == future and q.granted_at < future


def test_inspector_revocation_permission_is_separate(w):
    q = inspector("P-2")
    with pytest.raises(PermissionDenied):
        ps.revoke_qualification(w.field, qualification_id=q.pk, reason="x")
    ps.revoke_qualification(w.lead, qualification_id=q.pk, reason="SYNTHETIC")  # engineering_lead may revoke
    lead_q = Qualification.objects.filter(person__code="P-1", kind="field_lead").first()
    with pytest.raises(PermissionDenied):  # but not a field qualification (planning.manage_people)
        ps.revoke_qualification(w.lead, qualification_id=lead_q.pk, reason="x")


def test_covered_signer_mirror(w):
    insp = Inspection.objects.get(code=w.inspection)
    sql = "SELECT planning_inspection_signer_problem(%s)"
    assert ps.inspection_signer_problem(insp) is None is _one(sql, [insp.pk])
    ps.revoke_qualification(w.ops, qualification_id=inspector("P-3").pk, reason="SYNTHETIC")
    assert ps.inspection_signer_problem(insp) == _one(sql, [insp.pk])
    assert "inspector P-3 is not a qualified readiness inspector" in ps.inspection_signer_problem(insp)


# ================================================================== evidence (X-1, X-8)
def test_evidence_pass_refused_for_an_unqualified_signer_fail_accepted(w):
    visit = w.new_confirmed_visit(robot="R-02", h0=15, h1=16)
    fo = make_user("r7_fo", "field_operator")  # may sign inspections, but holds no inspector grant
    ps.register_person(w.ops, username=fo.username, code="P-9")
    insp = submit(w, "R-02", visit_ref=visit, by=(w.ops, fo))
    assert insp  # recorded anyway (principle 1)
    with pytest.raises(Conflict, match="Passing pre-departure evidence refused: inspector P-9"):
        rx.record_evidence(
            w.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
        )
    out = rx.record_evidence(
        w.ops, visit_code=visit, kind="pre_departure_inspection", result="fail", inspection_code=insp
    )
    assert out["result"] == "fail"


def test_production_pass_on_synthetic_template_refused_fail_accepted(w):
    production_world(w)
    visit = w.new_confirmed_visit(robot="R-02", h0=15, h1=16)
    insp = submit(w, "R-02", visit_ref=visit)
    set_setting("demo_mode", "false")
    with pytest.raises(
        Conflict, match="uses template T1, which is not approved for pre-departure use in production"
    ):
        rx.record_evidence(
            w.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
        )
    rx.record_evidence(
        w.ops, visit_code=visit, kind="pre_departure_inspection", result="fail", inspection_code=insp
    )
    e1 = submit(w, "R-02", template="E1", visit_ref=visit)
    rx.record_evidence(
        w.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=e1
    )


# ================================================================== evaluator gates
def test_world_is_ready_and_snapshot_holds_every_dependency_person(w):
    res = ev.evaluate_readiness(Visit.objects.get(code=w.visit))
    assert res.ready, res.blockers
    persons = {Person.objects.get(pk=int(k)).code for k in res.guard_snapshot["person_versions"]}
    # crew P-1/P-2, recovery P-4/P-5, inspection signers P-2/P-3 (P-2 appears once)
    assert persons == {"P-1", "P-2", "P-3", "P-4", "P-5"}
    st = fs.periodic_inspection_status(Robot.objects.get(code="R-01"), production=False)
    assert res.expires_at is not None and st.due_at is not None and res.expires_at <= st.due_at


def test_signer_equal_to_crew_lead_appears_once(w):
    visit = w.new_confirmed_visit(robot="R-02", h0=15, h1=16)
    insp = submit(w, "R-02", visit_ref=visit, by=(w.ops, w.field))  # P-1 is the crew lead
    rx.record_evidence(
        w.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    record_rest(w, visit)
    snap = ev.evaluate_readiness(Visit.objects.get(code=visit)).guard_snapshot
    keys = list(snap["person_versions"])
    assert len(keys) == len(set(keys)) and str(Person.objects.get(code="P-1").pk) in keys


def test_production_blocks_synthetic_pass_with_template_code(w):
    production_world(w)
    set_setting("demo_mode", "false")
    got = codes(w.visit)  # the world's demo-recorded PASS on synthetic T1
    assert ev.INSPECTION_TEMPLATE_NOT_APPROVED in got
    set_setting("demo_mode", "true")
    assert ev.INSPECTION_TEMPLATE_NOT_APPROVED not in codes(w.visit)


def test_policy_lacks_inspection_in_production_only(w):
    production_world(w)
    w.make_policy(
        source="approved_procedure",
        requirements=requirements(
            evidence=[e for e in REQUIREMENTS["evidence"] if e["kind"] != "pre_departure_inspection"]
        ),
    )
    assert ev.POLICY_LACKS_INSPECTION not in codes(w.visit)
    set_setting("demo_mode", "false")
    assert ev.POLICY_LACKS_INSPECTION in codes(w.visit)


def test_inspector_not_qualified_after_revocation(w):
    assert ev.INSPECTOR_NOT_QUALIFIED not in codes(w.visit)
    ps.revoke_qualification(w.ops, qualification_id=inspector("P-3").pk, reason="SYNTHETIC")
    res = ev.evaluate_readiness(Visit.objects.get(code=w.visit))
    blocked = {b["code"]: b["message"] for b in res.blockers}
    assert "pre-departure inspection" in blocked[ev.INSPECTOR_NOT_QUALIFIED].lower()
    with pytest.raises(Conflict, match="INSPECTOR_NOT_QUALIFIED"):
        rc.authorize_departure(w.ops, visit_code=w.visit)


def test_periodic_gate_codes(w):
    rule = PeriodicInspectionRule.objects.filter(pattern__code="P-A").order_by("-version_no")[0]
    PeriodicInspectionRuleWithdrawal.objects.create(rule=rule, withdrawn_by=w.safety, reason="SYNTHETIC")
    assert ev.PERIODIC_INSPECTION_INTERVAL_UNKNOWN in codes(w.visit)
    make_periodic_rule(w, "P-A")
    assert ev.PERIODIC_INSPECTION_INTERVAL_UNKNOWN not in codes(w.visit)
    # production never accepts the synthetic rule
    set_setting("demo_mode", "false")
    assert ev.PERIODIC_INSPECTION_INTERVAL_UNKNOWN in codes(w.visit)
    set_setting("demo_mode", "true")
    # every qualifying inspection loses a signer: none qualifies
    ps.revoke_qualification(w.ops, qualification_id=inspector("P-2").pk, reason="SYNTHETIC")
    got = codes(w.visit)
    assert ev.PERIODIC_INSPECTION_MISSING in got and ev.INSPECTOR_NOT_QUALIFIED in got


def owner_set_submitted_at(inspection_id: int, value: datetime) -> None:
    """Test-only owner rewrite of a forced database time (the documented owner trust boundary). The tests run
    as the schema owner inside one transaction: fire pending deferred checks first, then bypass the triggers."""
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE fleet_inspection DISABLE TRIGGER USER")
        cur.execute("UPDATE fleet_inspection SET submitted_at = %s WHERE id = %s", [value, inspection_id])
        cur.execute("ALTER TABLE fleet_inspection ENABLE TRIGGER USER")
        cur.execute("SET CONSTRAINTS ALL DEFERRED")


def _unload_end(visit: str) -> datetime:
    from planning.services import live_feasibility

    end = ev._unload_end(live_feasibility(Visit.objects.get(code=visit)))
    assert end is not None
    return end


def test_due_before_return_boundary_agrees_with_the_issue_function(w):
    """Case f: due_at = unload_end - 1 us and = unload_end both block (evaluator) / refuse (SQL); + 1 us passes."""
    unload_end = _unload_end(w.visit)
    now = _one("SELECT clock_timestamp()")
    interval = int((unload_end - now).total_seconds() // 60) - 1
    make_periodic_rule(w, "P-A", interval_min=interval)
    insp = Inspection.objects.get(code=w.inspection)
    base = unload_end - timedelta(minutes=interval)
    for delta_us, ok in ((-1, False), (0, False), (1, True)):
        owner_set_submitted_at(insp.pk, base + timedelta(microseconds=delta_us))
        got = codes(w.visit)
        assert (ev.PERIODIC_INSPECTION_DUE_BEFORE_RETURN not in got) is ok, (delta_us, got)
        sql_ok = (
            _one(
                "SELECT readiness_periodic_dependency_problem(%s, %s, false, %s)",
                [insp.pk, insp.robot_id, unload_end],
            )
            is None
        )
        assert sql_ok is ok, delta_us


# ================================================================== X-3 eligibility (EM decision 7)
def test_eligibility_periodic_reasons_and_time(w):
    robot = Robot.objects.get(code="R-01")
    st = fs.periodic_inspection_status(robot, production=False)
    assert st.state == "due" and st.due_at is not None
    assert fs.evaluate_eligibility(robot).eligible_for_planning
    late = fs.evaluate_eligibility(robot, at=st.due_at)
    assert f"Periodic inspection overdue since {fs.format_db_time(st.due_at)}" in late.reasons
    assert fs.evaluate_eligibility(robot, at=st.due_at - timedelta(microseconds=1)).eligible_for_planning
    set_setting("demo_mode", "false")
    assert (
        "Periodic inspection interval unknown for configuration P-A" in fs.evaluate_eligibility(robot).reasons
    )
    set_setting("demo_mode", "true")
    fs.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a7", safety_config_hash="z" * 64
    )
    robot.refresh_from_db()
    assert (
        "No qualifying periodic inspection on the current configuration"
        in fs.evaluate_eligibility(robot).reasons
    )


# ================================================================== X-4 escalation deadline sets
def _deadlines(policy: IncidentPolicy, minutes: int = 600) -> dict[str, Any]:
    out: dict[str, Any] = {
        lvl: [{"to_role": "safety_reviewer", "within_min": minutes}] for lvl in policy.rules["severity_scale"]
    }
    out["unclassified"] = []
    return out


def _incident_policy(w: Any) -> IncidentPolicy:
    from execution import policy as pol

    rules = {
        "severity_scale": ["S3", "S2"],
        "escalation_required_from": "S2",
        "downgrade_roles": ["safety_reviewer"],
    }
    v = int(pol.create_incident_policy(w.lead, rules=rules, source="synthetic_demo")["entity_id"])
    pol.approve_incident_policy(w.lead2, version=v, slot="engineering")
    pol.approve_incident_policy(w.safety, version=v, slot="safety")
    return IncidentPolicy.objects.get(version=v)


def test_deadline_set_commands(w):
    policy = _incident_policy(w)
    with pytest.raises(PermissionDenied):
        ed.create_deadline_set(
            w.eng,
            incident_policy_version=policy.version,
            deadlines=_deadlines(policy),
            source="synthetic_demo",
        )
    bad = _deadlines(policy)
    del bad["unclassified"]
    with pytest.raises(Invalid, match='plus "unclassified"'):
        ed.create_deadline_set(
            w.ops, incident_policy_version=policy.version, deadlines=bad, source="synthetic_demo"
        )
    bad = _deadlines(policy, minutes=0)
    with pytest.raises(Invalid, match="from 1 to 5256000"):
        ed.create_deadline_set(
            w.ops, incident_policy_version=policy.version, deadlines=bad, source="synthetic_demo"
        )
    sid = int(
        ed.create_deadline_set(
            w.ops,
            incident_policy_version=policy.version,
            deadlines=_deadlines(policy),
            source="synthetic_demo",
        )["entity_id"]
    )
    founder = w.founder
    with pytest.raises(PermissionDenied):
        ed.approve_deadline_set(w.ops, set_id=sid, slot="safety")
    with pytest.raises(PermissionDenied, match="requires role founder"):
        ed.approve_deadline_set(w.safety, set_id=sid, slot="founder")
    ed.approve_deadline_set(w.safety, set_id=sid, slot="safety")
    out = ed.approve_deadline_set(founder, set_id=sid, slot="founder")
    d = EscalationDeadlineSet.objects.get(pk=sid)
    assert out["active"] and ed.active_deadline_set() == d
    assert _one("SELECT execution_active_deadline_set_id()") == sid
    assert ed.usable_deadline_set(production=False) == d and ed.usable_deadline_set(production=True) is None
    # a synthetic set can never be approved in production
    sid2 = int(
        ed.create_deadline_set(
            w.safety,
            incident_policy_version=policy.version,
            deadlines=_deadlines(policy, 30),
            source="synthetic_demo",
        )["entity_id"]
    )
    with pytest.raises(Conflict, match="author"):
        ed.approve_deadline_set(w.safety, set_id=sid2, slot="safety")
    set_setting("demo_mode", "false")
    with pytest.raises(Conflict, match="cannot be approved in production"):
        ed.approve_deadline_set(founder, set_id=sid2, slot="founder")
    set_setting("demo_mode", "true")
    ed.withdraw_deadline_set(founder, set_id=sid, reason="SYNTHETIC")
    assert ed.active_deadline_set() is None and _one("SELECT execution_active_deadline_set_id()") is None


def test_no_notification_code_path_exists():
    """X-4 is read-only visibility: the deadline module sends nothing (no mail, outbox, timer or worker)."""
    import inspect

    src = inspect.getsource(ed)
    for word in ("send_mail", "EmailMessage", "outbox", "Timer(", "threading", "celery", "import sched"):
        assert word not in src, word


def test_template_rows_stay_usable_in_demo_only(w):
    t1 = InspectionTemplate.objects.get(pattern__code="P-A", version="T1")
    assert fs.template_usable(t1, "periodic", production=False)
    assert not fs.template_usable(t1, "periodic", production=True)
    assert DepartureAuthorization.objects.count() == 0


def test_evaluator_statement_count_is_measured(w):
    """Contract 07a section 11.7: the Slice 5 E pin (70) is re-measured with the Slice 7 gates. The figure is
    printed for the lead; the pin itself is asserted by tests/test_operations_readmodels.py."""
    from django.test.utils import CaptureQueriesContext

    visit = Visit.objects.get(code=w.visit)
    at = _one("SELECT clock_timestamp()")
    with CaptureQueriesContext(connection) as q:
        res = ev.evaluate_readiness(visit, at=at)
    assert res.ready
    print(f"\nSlice 7 evaluator statements on the ready world: E = {len(q.captured_queries)}")
    assert not [
        x for x in q.captured_queries if x["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert not [x for x in q.captured_queries if "FOR UPDATE" in x["sql"] or "FOR SHARE" in x["sql"]]
