"""v2 02C maintenance (docs/11_v2_phase02_contract.md 3, 4 row 02C, 5, 8.1, 8.2, 8.6, 8.8). SYNTHETIC data only:
every rule, interval and approval here is a test fixture by test users, never a company decision.

Covers the EM acceptance list: rule lifecycle (propose, two distinct approvers, withdraw, supersede from the
effective time), counters from authoritative records (calendar, closed runs, session hours, unknown), every status,
qualified records through the SECURITY DEFINER boundary, the database gates in planning and authorization (direct
SQL as ops_app included), hold separation (J7), regression evidence for existing bookings and issued
authorizations, concurrency in both orders, the activation gate, Hub tasks and the screens.
"""

import os
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

import psycopg
import pytest
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.test import Client
from psycopg.types.json import Jsonb

from fleet import maintenance as mnt
from fleet import services as fs
from fleet.models import (
    Hold,
    InspectionTemplate,
    MaintenanceRecord,
    MaintenanceRule,
    Robot,
)
from hub import derive
from hub import services as hs
from hub.models import WorkOrder
from operations.readmodels import production_blockers
from planning import services as ps
from planning.models import Person, Qualification, Visit
from planning.plain import robot_reason
from platform_core.commands import Conflict, PermissionDenied
from platform_core.models import AuditEvent
from readiness import commands as rc
from readiness.evaluator import MAINTENANCE_DUE_BEFORE_RETURN, evaluate_readiness
from readiness.models import DepartureAuthorization
from tests.conftest import make_user
from tests.fleet_world import record_periodic_inspection, set_setting
from tests.planning_world import DAY, new_visit, plan
from tests.readiness_world import build_readiness

KEY = 64942988919
APP_TABLES = [
    "fleet_maintenancerule",
    "fleet_maintenanceruleapproval",
    "fleet_maintenancerulewithdrawal",
    "fleet_maintenancerecord",
]
WRITE_FUNCTIONS = [
    "fleet_propose_maintenance_rule",
    "fleet_approve_maintenance_rule",
    "fleet_withdraw_maintenance_rule",
    "fleet_record_maintenance",
]
HELPERS = ["fleet_maint_deny", "fleet_maint_actor_has", "fleet_maint_audit"]


# ================================================================== world and helpers
@pytest.fixture
def w(transactional_db):
    w = build_readiness(prefix="mn_")
    w.tech = make_user("mn_tech", "hub_technician")
    ps.register_person(w.ops, username=w.tech.username, code="P-9")
    w.tech_q = grant_maintainer(w, "P-9")
    return w


def grant_maintainer(w: Any, person_code: str) -> int:
    out = ps.grant_qualification(
        w.safety,
        person_code=person_code,
        kind="maintenance_technician",
        valid_from=None,
        valid_to=datetime(2027, 6, 1, tzinfo=UTC),
        basis_ref="SYNTHETIC-MNT-TRAINING",
    )
    return int(out["entity_id"])


def rule(w: Any, code: str = "JOINT", *, approve: bool = True, **kw: Any) -> int:
    """A synthetic rule version by w.eng, approved by w.lead (engineering) and w.safety (safety)."""
    args: dict[str, Any] = {"calendar_days": 30, "source": "synthetic_demo", "warning_pct": 20}
    args.update(kw)
    rid = int(mnt.propose_rule(w.eng, pattern_code="P-A", requirement_code=code, **args)["rule_id"])
    if approve:
        mnt.approve_rule(w.lead, rule_id=rid, slot="engineering")
        mnt.approve_rule(w.safety, rule_id=rid, slot="safety")
    return rid


def record(w: Any, robot: str = "R-01", code: str = "JOINT", by: Any = None) -> int:
    return int(mnt.record_maintenance(by or w.tech, robot_code=robot, requirement_code=code)["record_id"])


def status(robot: str = "R-01", at: Any = None, production: bool | None = None) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT * FROM fleet_maintenance_status((SELECT id FROM fleet_robot WHERE code = %s),"
            " coalesce(%s, fleet_production_mode()), coalesce(%s, clock_timestamp()))"
            " ORDER BY requirement_code NULLS FIRST",
            [robot, production, at],
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def states(robot: str = "R-01", **kw: Any) -> dict[str | None, str]:
    return {r["requirement_code"]: r["state"] for r in status(robot, **kw)}


def gate(on: bool) -> None:
    set_setting("maintenance_gate_active", "true" if on else "false")


def one(sql: str, params: list[Any] | None = None) -> Any:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()[0]


def app_conn(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=autocommit,
    )


def owner_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_OWNER_USER", "ops_owner"),
        password=os.environ.get("DB_OWNER_PASSWORD", "ops_owner_dev"),
        autocommit=False,
    )


def refused(conn: psycopg.Connection, sql: str, params: list[Any] | None = None, match: str = "") -> str:
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as err:
        cur.execute(sql, params or [])
    if not conn.autocommit:
        conn.rollback()
    assert match in str(err.value), str(err.value)
    return str(err.value)


def issue_args(visit_code: str, issuer: int) -> list[Any]:
    v = Visit.objects.get(code=visit_code)
    res = evaluate_readiness(v)
    return [v.pk, issuer, res.expires_at, Jsonb(res.evidence_ids), res.recovery_plan_id,
            Jsonb(res.guard_snapshot), "demo" if fs.demo_mode() else "production"]  # fmt: skip


ISSUE_SQL = "SELECT id FROM readiness_issue_authorization(%s, %s, %s, %s, %s, %s, %s)"


def raw_depart(conn: psycopg.Connection, w: Any, auth_id: int) -> None:
    a = DepartureAuthorization.objects.get(pk=auth_id)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY])
        cur.execute(
            "INSERT INTO readiness_departureevent (authorization_id, visit_id, robot_id, recorded_by_id,"
            " guard_snapshot, departed_at) VALUES (%s, %s, %s, %s, %s, now())",
            [a.pk, a.visit_id, a.robot_id, w.ops.pk, Jsonb(a.guard_snapshot)],
        )


def nv(w: Any, days: int) -> str:
    """A new requested visit ``days`` after the world's visit day (no crew or robot overlap with it)."""
    return new_visit(w, d=DAY + timedelta(days=days))


def baseline_all(w: Any, code: str = "JOINT") -> None:
    for r in Robot.objects.values_list("code", flat=True):
        record(w, r, code)


# ================================================================== 1. rule lifecycle
def test_proposal_is_never_operational_and_two_distinct_approvers_make_it_so(w):
    rid = rule(w, approve=False)
    assert states() == {None: "no_rule"}  # a proposal is not a requirement
    with pytest.raises(PermissionDenied, match="engineering lead"):
        mnt.approve_rule(w.eng, rule_id=rid, slot="engineering")  # an engineer has no engineering_lead role
    both = make_user("mn_both", "engineering_lead", "safety_reviewer")
    mnt.approve_rule(both, rule_id=rid, slot="engineering")
    with pytest.raises(Conflict, match="both approval slots"):
        mnt.approve_rule(both, rule_id=rid, slot="safety")
    with pytest.raises(PermissionDenied, match="safety reviewer"):
        mnt.approve_rule(w.lead, rule_id=rid, slot="safety")
    assert states() == {None: "no_rule"}
    mnt.approve_rule(w.safety, rule_id=rid, slot="safety")
    assert states() == {"JOINT": "unknown"}  # in force, no baseline yet
    assert mnt.rule_status(MaintenanceRule.objects.get(pk=rid)) == "approved"
    # the author cannot approve its own rule even with the role
    own = int(mnt.propose_rule(w.lead, pattern_code="P-A", requirement_code="OWN", calendar_days=5,
                               source="synthetic_demo")["rule_id"])  # fmt: skip
    with pytest.raises(Conflict, match="author of a maintenance rule cannot approve it"):
        mnt.approve_rule(w.lead, rule_id=own, slot="engineering")
    # every write is audited by the function
    cmds = set(AuditEvent.objects.filter(entity_type="maintenance_rule").values_list("command", "outcome"))
    assert ("fleet.propose_maintenance_rule", "committed") in cmds
    assert ("fleet.approve_maintenance_rule", "committed") in cmds
    assert ("fleet.approve_maintenance_rule", "denied") not in cmds  # denied rows carry no entity
    assert AuditEvent.objects.filter(command="fleet.approve_maintenance_rule", outcome="denied").count() == 4


def test_roles_for_propose_approve_withdraw(w):
    with pytest.raises(PermissionDenied):
        mnt.propose_rule(
            w.ops, pattern_code="P-A", requirement_code="X", calendar_days=1, source="synthetic_demo"
        )
    with pytest.raises(PermissionDenied):
        mnt.propose_rule(w.tech, pattern_code="P-A", requirement_code="X", calendar_days=1,
                         source="synthetic_demo")  # fmt: skip
    rid = rule(w)
    for who in (w.eng, w.ops, w.tech, w.field):
        with pytest.raises(PermissionDenied):
            mnt.withdraw_rule(who, rule_id=rid, reason="no")
    with pytest.raises(Conflict, match="reason"):
        mnt.withdraw_rule(w.safety, rule_id=rid, reason="  ")
    mnt.withdraw_rule(w.safety, rule_id=rid, reason="SYNTHETIC: superseded by the engineering document")
    with pytest.raises(Conflict, match="already withdrawn"):
        mnt.withdraw_rule(w.lead, rule_id=rid, reason="again")


def test_withdrawn_version_in_force_leaves_no_rule_no_fallback(w):
    v1 = rule(w, calendar_days=30)
    v2 = rule(w, calendar_days=20)
    assert MaintenanceRule.objects.get(pk=v2).version_no == 2
    assert status()[0]["rule_version"] == 2
    mnt.withdraw_rule(w.lead, rule_id=v2, reason="SYNTHETIC withdrawal")
    row = status()[0]
    assert (row["state"], row["rule_version"], row["withdrawn"]) == ("no_rule", 2, True)
    assert "was withdrawn" in row["reason"]
    assert MaintenanceRule.objects.get(pk=v1).version_no == 1  # still there, never used again


def test_new_version_supersedes_only_from_its_effective_time(w):
    rule(w, calendar_days=30)
    record(w)
    later = fs.db_now() + timedelta(hours=1)
    rule(w, calendar_days=1, effective_from=later)
    assert status()[0]["rule_version"] == 1  # v2 approved but not effective yet
    assert states() == {"JOINT": "not_due"}
    two_days = fs.db_now() + timedelta(days=2)
    row = status(at=two_days)[0]
    assert (row["rule_version"], row["state"]) == (2, "overdue")  # from its effective time, v2 applies
    with pytest.raises(Conflict, match="past"):
        rule(w, calendar_days=5, effective_from=fs.db_now() - timedelta(hours=1))


def test_proposal_validation(w):
    for kw, match in (
        ({"calendar_days": None}, "at least one trigger"),
        ({"warning_pct": 95}, "warning margin"),
        ({"source": "engineering_doc"}, "procedure reference"),
        ({"source": "made_up"}, "source"),
    ):
        with pytest.raises(Conflict, match=match):
            rule(w, approve=False, **kw)
    with pytest.raises(Conflict, match="requirement code"):
        rule(w, code="bad code!", approve=False)
    other = InspectionTemplate.objects.get(pattern__code="P-C", version="T1")
    with pytest.raises(Conflict, match="belong to this configuration"):
        rule(w, approve=False, inspection_template_id=other.pk)


def test_demo_rule_in_production_is_no_rule_and_a_production_blocker(w):
    rule(w)
    record(w)
    assert states(production=False) == {"JOINT": "not_due"}
    row = status(production=True)[0]
    assert row["state"] == "no_rule" and "demo data" in row["reason"]
    blockers = {(b.code, b.entity_ref) for b in production_blockers(fs.db_now()).items}
    assert ("MAINTENANCE_RULE_NOT_APPROVED", "P-A") in blockers
    assert ("MAINTENANCE_GATE_INACTIVE", "maintenance-gate") in blockers
    rule(w, calendar_days=30, source="engineering_doc", procedure_ref="DOC-SYN-MNT-1")
    record(w)
    blockers = {(b.code, b.entity_ref) for b in production_blockers(fs.db_now()).items}
    assert ("MAINTENANCE_RULE_NOT_APPROVED", "P-A") not in blockers
    gate(True)
    blockers = {b.code for b in production_blockers(fs.db_now()).items}
    assert "MAINTENANCE_GATE_INACTIVE" not in blockers


# ================================================================== 2-3. counters and states
def test_no_baseline_is_unknown_and_a_record_clears_it(w):
    rid = rule(w)
    row = status()[0]
    assert row["state"] == "unknown" and "no maintenance record" in row["reason"] and row["blocking"]
    before = one("SELECT version FROM fleet_robot WHERE code = 'R-01'")
    rec = MaintenanceRecord.objects.get(pk=record(w))
    assert (rec.rule_id, rec.requirement_code, rec.performed_by_id) == (rid, "JOINT", w.tech.pk)
    assert rec.manifest_id == Robot.objects.get(code="R-01").current_manifest_id
    assert abs((rec.performed_at - fs.db_now()).total_seconds()) < 60  # database time, not the app's
    assert one("SELECT version FROM fleet_robot WHERE code = 'R-01'") == before + 1  # robot guard bumped
    row = status()[0]
    assert (row["state"], row["record_id"], row["blocking"]) == ("not_due", rec.pk, False)
    assert row["due_at"] == rec.performed_at + timedelta(days=30)


def test_calendar_due_soon_then_overdue(w):
    rule(w, calendar_days=10, warning_pct=20)
    performed = MaintenanceRecord.objects.get(pk=record(w)).performed_at
    assert states(at=performed + timedelta(days=7)) == {"JOINT": "not_due"}
    assert states(at=performed + timedelta(days=8)) == {"JOINT": "due_soon"}
    assert states(at=performed + timedelta(days=10) - timedelta(seconds=1)) == {"JOINT": "due_soon"}
    row = status(at=performed + timedelta(days=10))[0]
    assert row["state"] == "overdue" and "calendar limit reached" in row["reason"]


def test_required_post_maintenance_inspection(w):
    t1 = InspectionTemplate.objects.get(pattern__code="P-A", version="T1")
    rule(w, inspection_template_id=t1.pk)
    record(w)
    row = status()[0]
    assert row["state"] == "inspection_missing" and row["blocking"]
    assert "post-maintenance inspection is missing" in row["reason"]
    record_periodic_inspection(w, "R-01")  # a passing T1 inspection by two qualified inspectors
    assert states() == {"JOINT": "not_due"}
    record(w)  # the next record needs its own inspection again
    assert states() == {"JOINT": "inspection_missing"}


def _run_world(w: Any) -> Any:
    """Baseline, then the real departure of w.visit (gate inactive: the authorization path is unchanged)."""
    from tests import execution_world as ex  # noqa: PLC0415

    run = ex.depart(w)
    w.run = run
    w.robot = Robot.objects.get(pk=run.robot_id)
    w.template = ex.SiteCheckTemplate.objects.create(
        version="SC1", items=ex.TEMPLATE_ITEMS, source="synthetic_demo", recorded_by=w.ops
    )
    return ex


def test_visits_from_closed_runs_and_hours_from_sessions_open_run_is_unknown(w, app_role_conn):
    rule(w, calendar_days=None, visit_count=1, active_hours=2, warning_pct=0)
    record(w)
    ex = _run_world(w)
    with app_role_conn.cursor() as cur:
        ex.to_site(cur, w)
        check = ex.go_check(cur, w)
        start = ex.raw_session(cur, w, "start", site_check_id=check)
    row = status()[0]
    assert row["state"] == "unknown" and row["open_runs"] == w.run.code  # a start with no stop: unknown
    assert "has a session start but no stop" in row["reason"]
    assert row["visits_used"] == 0  # the run is not closed
    # back-date the start by 3 hours (test-only owner rewrite of an append-only row)
    with owner_conn() as owner, owner.cursor() as cur:
        cur.execute("ALTER TABLE execution_sessionevent DISABLE TRIGGER USER")
        cur.execute(
            "UPDATE execution_sessionevent SET recorded_at = recorded_at - interval '3 hours' WHERE id = %s",
            [start],
        )
        owner.commit()
        cur.execute("ALTER TABLE execution_sessionevent ENABLE TRIGGER USER")
        owner.commit()
    with app_role_conn.cursor() as cur:
        ex.raw_session(cur, w, "stop")
    row = status()[0]
    assert row["open_runs"] is None and float(row["hours_used"]) >= 3
    assert row["state"] == "overdue" and "active hours" in row["reason"]
    with app_role_conn.cursor() as cur:
        ex.to_hub(cur, w)
        ex.close_run(cur, w)
    row = status()[0]
    assert row["visits_used"] == 1 and "1 of 1 visits" in row["reason"]


# ================================================================== 4. qualified records, write boundary
def test_record_needs_role_and_valid_qualification(w):
    rule(w)
    with pytest.raises(PermissionDenied, match="qualification"):
        record(w, by=w.eng)  # engineer, not qualified
    grant_maintainer(w, "P-4")  # P-4 = w.eng
    record(w, by=w.eng)
    grant_maintainer(w, "P-1")  # P-1 = w.ops: qualified but ops_lead may not record (contract 8.6)
    with pytest.raises(PermissionDenied, match="hub technician, an engineer or the engineering lead"):
        record(w, by=w.ops)
    # revoking the technician's grant: no new record, and the records signed under it stop qualifying
    record(w, "R-02", by=w.tech)
    assert states("R-02") == {"JOINT": "not_due"}
    ps.revoke_qualification(w.safety, qualification_id=w.tech_q, reason="SYNTHETIC")
    with pytest.raises(PermissionDenied):
        record(w, by=w.tech)
    assert states("R-02") == {"JOINT": "unknown"}
    with pytest.raises(Conflict, match="no maintenance requirement NOPE"):
        mnt.record_maintenance(w.eng, robot_code="R-01", requirement_code="NOPE")


def test_maintenance_technician_is_granted_only_by_a_safety_reviewer(w):
    p5 = "P-5"
    with pytest.raises(PermissionDenied):
        ps.grant_qualification(w.ops, person_code=p5, kind="maintenance_technician", valid_from=None,
                               valid_to=datetime(2027, 1, 1, tzinfo=UTC), basis_ref="X")  # fmt: skip
    with pytest.raises(Exception, match="training record"):
        ps.grant_qualification(w.safety, person_code=p5, kind="maintenance_technician", valid_from=None,
                               valid_to=datetime(2027, 1, 1, tzinfo=UTC), basis_ref=" ")  # fmt: skip
    with pytest.raises(Exception, match="backdated"):
        ps.grant_qualification(w.safety, person_code=p5, kind="maintenance_technician",
                               valid_from=fs.db_now() - timedelta(days=1),
                               valid_to=datetime(2027, 1, 1, tzinfo=UTC), basis_ref="X")  # fmt: skip
    ps.register_person(w.ops, username=w.safety.username, code="P-8")
    with pytest.raises(Conflict, match="oneself"):
        grant_maintainer(w, "P-8")
    # the database refuses the same things for a direct insert
    with pytest.raises(Exception, match="only by a safety_reviewer"):
        Qualification.objects.create(person=Person.objects.get(code=p5), kind="maintenance_technician",
                                     valid_from=None, valid_to=datetime(2027, 1, 1, tzinfo=UTC),
                                     granted_by=w.ops, basis_ref="X")  # type: ignore[misc]  # fmt: skip


def test_ops_app_has_select_only_and_cannot_write_or_fake_records(w, app_role_conn):
    rid = rule(w)
    rec = record(w)
    for t in APP_TABLES:
        with app_role_conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {t}")
        for sql in (f"UPDATE {t} SET id = id", f"DELETE FROM {t}", f"TRUNCATE {t}"):
            refused(app_role_conn, sql, match="permission denied")
    m = Robot.objects.get(code="R-01").current_manifest_id
    refused(
        app_role_conn,
        "INSERT INTO fleet_maintenancerecord (robot_id, rule_id, requirement_code, manifest_id, performed_by_id,"
        " performed_at, notes) VALUES ((SELECT id FROM fleet_robot WHERE code = 'R-02'), %s, 'JOINT', %s, %s,"
        " now(), '')",
        [rid, m, w.tech.pk],
        match="permission denied",
    )
    refused(
        app_role_conn,
        "INSERT INTO fleet_maintenanceruleapproval (rule_id, slot, approver_id, approved_at)"
        " VALUES (%s, 'engineering', %s, now())",
        [rid, w.lead.pk],
        match="permission denied",
    )
    refused(app_role_conn, "SELECT nextval('fleet_maintenancerecord_id_seq')", match="permission denied")
    # even the owner cannot change an appended row (append-only for every role)
    with owner_conn() as owner:
        refused(owner, "UPDATE fleet_maintenancerecord SET notes = 'x' WHERE id = %s", [rec])
        refused(owner, "DELETE FROM fleet_maintenanceruleapproval")


def test_helpers_are_not_callable_and_execute_is_revoked_from_public(w, app_role_conn):
    refused(app_role_conn, "SELECT fleet_maint_deny('x')", match="permission denied")
    refused(app_role_conn, "SELECT fleet_maint_actor_has(1, ARRAY['engineer'])", match="permission denied")
    refused(
        app_role_conn,
        "SELECT fleet_maint_audit(1, 'x', 'x', 'x', '{}'::jsonb, '{}'::jsonb, 'x')",
        match="permission denied",
    )
    with connection.cursor() as cur:
        cur.execute(
            "SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner), p.proacl::text, p.proconfig::text"
            " FROM pg_proc p WHERE p.proname = ANY(%s)",
            [WRITE_FUNCTIONS + HELPERS],
        )
        rows = {r[0]: r[1:] for r in cur.fetchall()}
    for name in WRITE_FUNCTIONS:
        secdef, owner, acl, config = rows[name]
        assert secdef and owner == os.environ.get("DB_OWNER_USER", "ops_owner"), name
        assert "search_path=public, pg_temp" in config, name
        assert not any(e.startswith("=") for e in acl.strip("{}").split(",")), (name, acl)  # no PUBLIC entry
        assert "ops_app=X" in acl, (name, acl)
    for name in HELPERS:
        acl = rows[name][2]
        assert "ops_app" not in acl and not any(e.startswith("=") for e in acl.strip("{}").split(",")), acl


def test_forged_null_empty_or_inactive_actor_is_refused(w, app_role_conn):
    rid = rule(w, approve=False)
    robot = Robot.objects.get(code="R-01").pk
    inactive = make_user("mn_gone", "hub_technician")
    inactive.is_active = False
    inactive.save()
    for actor in (None, 0, -1, 999_999_999, inactive.pk, w.ops.pk):
        refused(app_role_conn, "SELECT fleet_record_maintenance(%s, %s, 'JOINT', '', 'rid')", [actor, robot],
                match="hub technician")  # fmt: skip
        refused(app_role_conn, "SELECT fleet_approve_maintenance_rule(%s, %s, 'engineering', 'rid')", [actor, rid],
                match="engineering approval")  # fmt: skip
        refused(app_role_conn, "SELECT fleet_withdraw_maintenance_rule(%s, %s, 'r', 'rid')", [actor, rid],
                match="withdraw")  # fmt: skip
        refused(
            app_role_conn,
            "SELECT fleet_propose_maintenance_rule(%s, (SELECT id FROM fleet_manifestpattern WHERE code = 'P-A'),"
            " 'X', 1, NULL, NULL, 10, NULL, NULL, 'synthetic_demo', '', '', 'rid')",
            [actor],
            match="propose",
        )
    refused(app_role_conn, "SELECT fleet_approve_maintenance_rule(%s, %s, NULL, 'rid')", [w.lead.pk, rid],
            match="slot")  # fmt: skip
    mnt.approve_rule(w.lead, rule_id=rid, slot="engineering")
    mnt.approve_rule(w.safety, rule_id=rid, slot="safety")
    refused(app_role_conn, "SELECT fleet_record_maintenance(%s, %s, '', '', 'rid')", [w.tech.pk, robot],
            match="no maintenance requirement")  # fmt: skip
    refused(app_role_conn, "SELECT fleet_record_maintenance(%s, %s, NULL, '', 'rid')", [w.tech.pk, robot],
            match="no maintenance requirement")  # fmt: skip
    assert not MaintenanceRecord.objects.exists()
    with app_role_conn.cursor() as cur:  # the real path works as ops_app
        cur.execute("SELECT fleet_record_maintenance(%s, %s, 'joint', 'SYN', 'rid')", [w.tech.pk, robot])
    assert MaintenanceRecord.objects.get().requirement_code == "JOINT"


# ================================================================== 5. gates
def test_inactive_gate_computes_status_but_blocks_nothing(w, app_role_conn):
    rule(w)
    assert states() == {"JOINT": "unknown"}
    assert not mnt.gate_active()
    assert fs.evaluate_eligibility(Robot.objects.get(code="R-01")).eligible_for_planning
    assert evaluate_readiness(Visit.objects.get(code=w.visit)).ready
    code = nv(w, 1)
    assert plan(w, code)["feasibility"] == "feasible"
    with app_role_conn.cursor() as cur:
        cur.execute(ISSUE_SQL, issue_args(w.visit, w.ops.pk))


def test_active_gate_view_and_python_eligibility_say_the_same(w):
    rule(w)
    rule(w, "HAND", visit_count=5)
    record(w, "R-02")
    record(w, "R-02", "HAND")
    record(w, "R-03", "HAND")
    gate(True)
    with connection.cursor() as cur:
        cur.execute(
            "SELECT robot_code, eligible, reasons FROM operations_robot_eligibility ORDER BY robot_code"
        )
        view = {r[0]: (r[1], list(r[2])) for r in cur.fetchall()}
    for r in Robot.objects.all():
        e = fs.evaluate_eligibility(r)
        assert view[r.code] == (e.eligible_for_planning, e.reasons), r.code
    assert view["R-02"][0]
    assert view["R-01"][1] == [
        "Maintenance HAND status unknown: no maintenance record for this robot",
        "Maintenance JOINT status unknown: no maintenance record for this robot",
    ]
    assert view["R-03"][1] == ["Maintenance JOINT status unknown: no maintenance record for this robot"]
    assert robot_reason(view["R-03"][1][0]).short == "maintenance not up to date"
    assert "can record the maintenance" in robot_reason(view["R-03"][1][0]).fix


def test_existing_booking_shows_blocked_with_reason_and_is_not_replanned(w):
    rule(w, visit_count=1)
    baseline_all(w)
    gate(True)
    v = Visit.objects.get(code=w.visit)
    before = (v.status, v.current_revision_id, v.version)
    # the robot becomes overdue: a new, shorter rule version applies from its effective time
    rule(w, calendar_days=1, effective_from=fs.db_now() + timedelta(minutes=1))
    late = fs.db_now() + timedelta(days=2)
    assert states(at=late) == {"JOINT": "overdue"}
    # the plan as it is now (the planner's live check) shows the block and who clears it
    e = fs.evaluate_eligibility(Robot.objects.get(code="R-01"), at=late)
    assert not e.eligible_for_planning and e.reasons[0].startswith("Maintenance JOINT overdue")
    assert "can record the maintenance" in robot_reason(e.reasons[0]).fix
    v.refresh_from_db()
    assert (v.status, v.current_revision_id, v.version) == before  # nothing was re-planned silently


def test_overdue_robot_blocks_plan_confirm_readiness_and_authorization(w, app_role_conn):
    rule(w)
    record(w, "R-02")  # R-01 (the booked robot) has no baseline: Unknown
    code = nv(w, 1)
    assert plan(w, code)["feasibility"] == "feasible"  # planned before the gate is active
    gate(True)
    # readiness: blocked through planning eligibility, with the plain reason
    res = evaluate_readiness(Visit.objects.get(code=w.visit))
    msgs = [b["message"] for b in res.blockers if b["code"] == "PLANNING_ROBOT_NOT_ELIGIBLE"]
    assert msgs and "Maintenance JOINT status unknown" in msgs[0]
    with pytest.raises(Conflict):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    refused(app_role_conn, ISSUE_SQL, issue_args(w.visit, w.ops.pk), match="maintenance not up to date")
    # planning: the command records a blocked revision, confirm is refused
    out = plan(w, nv(w, 2))
    assert out["feasibility"] == "blocked" and any("Maintenance JOINT" in b for b in out["blockers"])
    with pytest.raises(Conflict, match="Maintenance JOINT"):
        ps.confirm_visit(w.ops, visit_code=code)
    # another robot with a baseline is fine
    assert plan(w, nv(w, 3), robot="R-02")["feasibility"] == "feasible"


def test_direct_sql_as_ops_app_cannot_plan_past_the_gate(w, app_role_conn):
    rule(w)
    code = nv(w, 1)
    plan(w, code)  # planned while the gate was inactive
    v = Visit.objects.get(code=code)
    rev = v.current_revision
    gate(True)
    robot_res = one("SELECT id FROM planning_resource WHERE kind = 'robot' AND robot_id = %s", [rev.robot_id])
    refused(
        app_role_conn,
        "INSERT INTO planning_reservation (resource_id, revision_id, purpose, starts_at, ends_at)"
        " VALUES (%s, %s, 'on_site', '2027-01-05 10:00Z', '2027-01-05 11:00Z')",
        [robot_res, rev.pk],
        match="maintenance not up to date",
    )
    refused(
        app_role_conn,
        "INSERT INTO planning_visitrevision (visit_id, revision_no, on_site_start, on_site_end, travel_out_min,"
        " travel_back_min, tasks, required_consent_scopes, robot_id, lead_id, second_id, vehicle_id, feasibility,"
        " blockers, snapshot, reason, created_by_id, created_at)"
        " SELECT visit_id, revision_no + 1, on_site_start, on_site_end, travel_out_min, travel_back_min, tasks,"
        " required_consent_scopes, robot_id, lead_id, second_id, vehicle_id, 'feasible', '[]', '{}', 'x',"
        " created_by_id, now() FROM planning_visitrevision WHERE id = %s",
        [rev.pk],
        match="maintenance not up to date",
    )
    refused(app_role_conn, "UPDATE planning_visit SET status = 'confirmed' WHERE id = %s", [v.pk],
            match="maintenance not up to date")  # fmt: skip
    v.refresh_from_db()
    assert v.status == "planned"
    # downgrade and cancel are never refused by the gate (safe direction)
    with app_role_conn.cursor() as cur:
        cur.execute("UPDATE planning_visit SET status = 'requested' WHERE id = %s", [v.pk])


def test_calendar_due_before_return_blocks_and_bounds_the_expiry(w, app_role_conn):
    rule(w, calendar_days=1, warning_pct=0)
    baseline_all(w)
    gate(True)
    res = evaluate_readiness(
        Visit.objects.get(code=w.visit)
    )  # the visit is on 2026-12-08, after the due time
    assert MAINTENANCE_DUE_BEFORE_RETURN in {b["code"] for b in res.blockers}
    refused(app_role_conn, ISSUE_SQL, issue_args(w.visit, w.ops.pk), match="maintenance falls due")


def test_maintenance_due_bounds_expiry_when_not_before_return(w):
    rule(w, calendar_days=60)
    baseline_all(w)
    gate(True)
    res = evaluate_readiness(Visit.objects.get(code=w.visit))
    assert res.ready
    due = status()[0]["due_at"]
    assert res.expires_at is not None and res.expires_at <= due


# ================================================================== 7. issued authorizations, concurrency
def test_authorization_issued_before_robot_became_blocked_cannot_depart(w, app_role_conn):
    """Slice 7 treats a periodic inspection that stops qualifying after authorization by re-proving it at the
    departure (and bounding expires_at by the due date). Maintenance follows the same rule: here the gate is
    activated after issue (a settings change, no guard version moves), and the raw departure is refused."""
    rule(w)  # R-01 has no baseline: Unknown, not blocking while the gate is inactive
    with app_role_conn.cursor() as cur:
        cur.execute(ISSUE_SQL, issue_args(w.visit, w.ops.pk))
        auth = int(cur.fetchone()[0])
    gate(True)
    with pytest.raises(psycopg.Error, match="maintenance is not up to date"):
        raw_depart(app_role_conn, w, auth)
    gate(False)
    raw_depart(app_role_conn, w, auth)  # the same authorization departs once the block is gone


def test_rule_withdrawal_after_issue_refuses_departure(w, app_role_conn):
    rid = rule(w)
    baseline_all(w)
    gate(True)
    with app_role_conn.cursor() as cur:
        cur.execute(ISSUE_SQL, issue_args(w.visit, w.ops.pk))
        auth = int(cur.fetchone()[0])
    mnt.withdraw_rule(w.safety, rule_id=rid, reason="SYNTHETIC")
    with pytest.raises(psycopg.Error, match="guards changed since authorization"):
        raw_depart(app_role_conn, w, auth)  # the pattern was bumped by the withdrawal


def test_record_and_authorization_are_ordered_both_ways(w):
    rule(w)
    record(w, "R-02")
    gate(True)
    r1 = Robot.objects.get(code="R-01").pk
    args_blocked = issue_args(w.visit, w.ops.pk)
    # order 1: the record holds the robot; an issue waits, then sees the new robot version and is refused
    a, b = app_conn(autocommit=False), app_conn(autocommit=False)
    try:
        with a.cursor() as cur:
            cur.execute("SELECT fleet_record_maintenance(%s, %s, 'JOINT', '', 'c1')", [w.tech.pk, r1])
        with b.cursor() as cur:
            cur.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                cur.execute(ISSUE_SQL, args_blocked)
        b.rollback()
        a.commit()
        refused(b, ISSUE_SQL, args_blocked, match="robot version")  # the snapshot predates the record
        with b.cursor() as cur:
            cur.execute(ISSUE_SQL, issue_args(w.visit, w.ops.pk))  # a fresh evaluation passes
            auth = int(cur.fetchone()[0])
        # order 2: the issue holds the robot (FOR SHARE); a record waits until it commits
        with a.cursor() as cur:
            cur.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                cur.execute("SELECT fleet_record_maintenance(%s, %s, 'JOINT', '', 'c2')", [w.tech.pk, r1])
        a.rollback()
        b.commit()
        with a.cursor() as cur:
            cur.execute("SELECT fleet_record_maintenance(%s, %s, 'JOINT', '', 'c3')", [w.tech.pk, r1])
        a.commit()
    finally:
        a.close()
        b.close()
    # the record after the authorization moved the robot's guard version: that authorization cannot depart
    with app_conn() as c, pytest.raises(psycopg.Error, match="guards changed since authorization"):
        raw_depart(c, w, auth)


def test_record_and_booking_are_ordered_both_ways(w):
    rule(w)
    gate(True)
    r1 = Robot.objects.get(code="R-01").pk
    code = nv(w, 1)
    a = app_conn(autocommit=False)
    try:
        # order 1: the record holds the robot; the plan command waits (then sees the record and plans)
        with a.cursor() as cur:
            cur.execute("SELECT fleet_record_maintenance(%s, %s, 'JOINT', '', 'c1')", [w.tech.pk, r1])
        with pytest.raises(Exception), transaction.atomic():  # noqa: B017 - lock timeout, maybe retried
            with connection.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '300ms'")
            plan(w, code)
        a.commit()
        assert plan(w, code)["feasibility"] == "feasible"
        # order 2: a plan holds the robot (FOR UPDATE); a record waits until it commits
        other = nv(w, 2)
        with transaction.atomic():
            plan(w, other)
            with a.cursor() as cur:
                cur.execute("SET lock_timeout = '300ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    cur.execute("SELECT fleet_record_maintenance(%s, %s, 'JOINT', '', 'c2')", [w.tech.pk, r1])
            a.rollback()
        with a.cursor() as cur:
            cur.execute("SELECT fleet_record_maintenance(%s, %s, 'JOINT', '', 'c3')", [w.tech.pk, r1])
        a.commit()
    finally:
        a.close()
    assert MaintenanceRecord.objects.count() == 2


# ================================================================== 6, 8.1. holds and work orders
def test_maintenance_record_never_releases_a_hold_j7_unchanged(w):
    rule(w)
    baseline_all(w)
    gate(True)
    out = fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="SYNTHETIC")
    h = Hold.objects.get(code=out["entity_id"])
    problem = fs.hold_release_problem(h)
    record(w)
    h.refresh_from_db()
    assert h.released_at is None and fs.hold_release_problem(h) == problem
    e = fs.evaluate_eligibility(Robot.objects.get(code="R-01"))
    assert not e.eligible_for_planning and any(r.startswith("Robot hold") for r in e.reasons)
    assert not [r for r in e.reasons if r.startswith("Maintenance")]
    with pytest.raises(Exception):  # noqa: B017 - the J7 refusal (approvals missing), whatever its type
        fs.release_hold(w.ops, hold_code=h.code)


def test_hub_maintenance_tasks_with_stable_refs_and_completion_changes_nothing(w):
    rule(w, calendar_days=10, warning_pct=20)
    rule(w, "HAND", visit_count=3)
    rec = record(w, "R-02")
    derive.refresh()
    tasks = {t.source_ref: t for t in WorkOrder.objects.filter(source_kind="maintenance_status")}
    assert {"R-01:JOINT:1:none", "R-01:HAND:1:none", "R-02:HAND:1:none"} <= set(tasks)
    assert f"R-02:JOINT:1:{rec}" not in tasks  # not due: no task
    assert tasks["R-01:JOINT:1:none"].type == "maintenance_due"
    assert tasks["R-01:JOINT:1:none"].link_url == "/fleet/R-01/maintenance/"
    assert "unknown status" in tasks["R-01:JOINT:1:none"].title
    derive.refresh()  # idempotent
    assert WorkOrder.objects.filter(source_kind="maintenance_status").count() == len(tasks)
    # completing a maintenance task changes no maintenance status, eligibility or hold (contract 8.1)
    gate(True)
    before = (states("R-01"), fs.evaluate_eligibility(Robot.objects.get(code="R-01")).reasons)
    t = tasks["R-01:JOINT:1:none"]
    hs.assign_task(w.ops, task_id=t.pk, owner_code="P-9")
    hs.start_task(w.ops, task_id=t.pk)
    hs.complete_task(w.ops, task_id=t.pk, note="done")
    assert (states("R-01"), fs.evaluate_eligibility(Robot.objects.get(code="R-01")).reasons) == before
    assert not MaintenanceRecord.objects.filter(robot__code="R-01").exists()
    # the record clears the source of the open HAND task; the next cycle (due soon) is a new fact
    hand = tasks["R-01:HAND:1:none"]
    rec1 = record(w, "R-01", "HAND")
    derive.refresh()
    hand.refresh_from_db()
    assert hand.source_cleared_at is not None and hand.status == "open"  # nobody did the task's work
    late = fs.db_now() + timedelta(days=9)
    derive.refresh(late)
    assert WorkOrder.objects.filter(source_ref=f"R-02:JOINT:1:{rec}", title__contains="due soon").exists()
    assert not WorkOrder.objects.filter(source_ref=f"R-01:HAND:1:{rec1}").exists()  # not due


# ================================================================== 8. activation gate (owner command)
def test_gate_activation_and_deactivation_only_through_the_owner_command(w):
    set_setting("ops_readiness_lookahead_min", "2880")  # the other required owner settings exist already
    out = StringIO()
    call_command("provision_settings", maintenance_gate_active="true", stdout=out)
    assert "set maintenance_gate_active = true" in out.getvalue() and mnt.gate_active()
    with pytest.raises(CommandError, match="--allow-change"):
        call_command("provision_settings", maintenance_gate_active="false", stdout=StringIO())
    assert mnt.gate_active()
    with pytest.raises(CommandError, match="true or false"):
        call_command("provision_settings", maintenance_gate_active="yes", stdout=StringIO())
    call_command("provision_settings", maintenance_gate_active="false", allow_change=True, stdout=StringIO())
    assert not mnt.gate_active()
    call_command(
        "provision_settings", maintenance_gate_active="true", stdout=StringIO()
    )  # re-activation: safe
    changes = [a.result["changed"] for a in AuditEvent.objects.filter(command="fleet.provision_settings")]
    assert [c["maintenance_gate_active"]["to"] for c in changes] == ["true", "false", "true"]
    with app_conn() as c:  # the runtime role cannot switch it
        refused(c, "UPDATE fleet_deploymentsetting SET value = 'false' WHERE key = 'maintenance_gate_active'",
                match="permission denied")  # fmt: skip
        refused(c, "INSERT INTO fleet_deploymentsetting (key, value) VALUES ('maintenance_gate_active', 'x')",
                match="permission denied")  # fmt: skip


def test_migrated_database_stays_usable_until_activation(w):
    """Incremental deployment: with no maintenance rule at all and no setting, everything works as before;
    production lists the missing rules and the inactive gate as blockers."""
    assert not MaintenanceRule.objects.exists()
    assert states() == {None: "no_rule"}
    assert evaluate_readiness(Visit.objects.get(code=w.visit)).ready
    set_setting("demo_mode", "false")
    codes = {b.code for b in production_blockers(fs.db_now()).items}
    assert {"MAINTENANCE_RULE_NOT_APPROVED", "MAINTENANCE_GATE_INACTIVE"} <= codes


def test_runbook_documents_the_rollout_order():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "runbook_deploy_backup_rollback.md")
    with open(path) as f:
        text = f.read()
    i = text.index("Order (each step is safe to stop after", text.index("Maintenance gate rollout"))
    order = [
        "migrate",
        "propose",
        "approve",
        "maintenance_technician",
        "baseline",
        "--maintenance-gate-active true",
    ]
    pos = [text.index(x, i) for x in order]
    assert pos == sorted(pos), pos


# ================================================================== 9. screens
def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _token(c: Client, url: str) -> str:
    import re  # noqa: PLC0415

    body = c.get(url).content.decode()
    m = re.search(r'name="_form_token" value="([^"]+)"', body)
    assert m, url
    return m[1]


def test_configuration_page_rules_propose_and_approve(w):
    c = _client(w.eng)
    page = "/fleet/patterns/P-A/"
    body = c.get(page).content.decode()
    assert "Maintenance rules" in body and "Propose a rule version" in body
    r = c.post(f"{page}maintenance-rules/create/", {
        "requirement_code": "UI-JOINT", "calendar_days": "30", "warning_pct": "10", "source": "synthetic_demo",
        "_form_token": _token(c, page),
    })  # fmt: skip
    assert r.status_code == 302
    rid = MaintenanceRule.objects.get(requirement_code="UI-JOINT").pk
    body = _client(w.lead).get(page).content.decode()
    assert (
        'data-maintenance-rule="UI-JOINT:1" data-status="proposed"' in body
        and "Proposal, not in force" in body
    )
    assert "Demo value" in body
    for user, slot in ((w.lead, "engineering"), (w.safety, "safety")):
        uc = _client(user)
        uc.post(
            f"{page}maintenance-rules/approve/", {"rule": rid, "slot": slot, "_form_token": _token(uc, page)}
        )
    assert mnt.rule_status(MaintenanceRule.objects.get(pk=rid)) == "approved"
    assert (
        _client(w.ops).post(f"{page}maintenance-rules/create/", {"requirement_code": "X"}).status_code == 403
    )
    assert "<script" not in body.split("<main", 1)[-1].split("</main>")[0]


def test_robot_panel_and_maintenance_page_with_record_for_qualified_users(w):
    rule(w)
    body = _client(w.ops).get("/fleet/R-01/").content.decode()
    assert 'data-maintenance="1"' in body and "Maintenance gate not yet activated" in body
    page = "/fleet/R-01/maintenance/"
    tc = _client(w.tech)  # the hub technician has no fleet.read, but may open this page and record
    body = tc.get(page).content.decode()
    assert "no maintenance record for this robot" in body and "Record maintenance" in body
    assert 'data-state="unknown"' in body
    r = tc.post(page, {"requirement_code": "JOINT", "notes": "SYN", "_form_token": _token(tc, page)})
    assert r.status_code == 302 and MaintenanceRecord.objects.filter(robot__code="R-01").count() == 1
    body = tc.get(page).content.decode()
    assert 'data-state="not_due"' in body and "source: time since the record" in body
    # an engineer without the qualification sees why, not the form
    body = _client(w.eng).get(page).content.decode()
    assert "needs a valid maintenance technician qualification" in body
    assert 'name="requirement_code"' not in body
    # a field operator may read the robot page but not record
    assert _client(w.field).post(page, {"requirement_code": "JOINT"}).status_code == 403
    assert _client(make_user("mn_admin", "admin")).get(page).status_code == 403
    assert "<script" not in body.split("<main", 1)[-1].split("</main>")[0]
