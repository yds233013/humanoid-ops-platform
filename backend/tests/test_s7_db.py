"""Slice 7 database guardrails against DIRECT SQL from the runtime role (ops_app), plus the oracle fixtures for the
authoritative SQL functions (docs/07a_production_guardrails_contract.md sections 2-7, 11.2, 11.8; Engineer A).

Everything is SYNTHETIC: approvals here are test fixtures by test users, never company approvals.
"""

import os
import threading
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.db import connection
from psycopg.types.json import Jsonb

from execution import incidents as inc
from execution.models import (
    EscalationDeadlineSet,
    EscalationDeadlineSetApproval,
    EscalationDeadlineSetWithdrawal,
    Incident,
)
from fleet import services as fs
from fleet.management.commands.seed_fleet import DRAFT_POLICY_RULES, TEMPLATE_ITEMS
from fleet.models import (
    CapabilityProfile,
    Hold,
    HoldPolicy,
    HoldPolicyApproval,
    HoldPolicyWithdrawal,
    HoldRebase,
    HoldRebaseActivation,
    HoldRebaseApproval,
    Inspection,
    InspectionTemplate,
    InspectionTemplateApproval,
    InspectionTemplateWithdrawal,
    ManifestPattern,
    PeriodicInspectionRule,
    PeriodicInspectionRuleApproval,
    PeriodicInspectionRuleWithdrawal,
    Robot,
)
from planning import services as ps
from planning.models import Person, Qualification, Reservation, Visit
from platform_core.commands import Invalid
from readiness import commands as rc
from readiness import evidence as rx
from readiness.evaluator import evaluate_readiness
from readiness.models import DepartureAuthorization, DepartureEvent, ReadinessEvidence
from tests.conftest import make_user
from tests.execution_world import build_execution
from tests.fleet_world import (
    SYNTHETIC_PERIODIC_INTERVAL_MIN,
    make_engineering_template,
    make_periodic_rule,
    set_setting,
)
from tests.planning_world import grant_inspector
from tests.readiness_world import REQUIREMENTS, build_readiness, record_inspection, requirements

KEY = 64942988919
CV = psycopg.errors.CheckViolation


# ================================================================== connections and helpers
def _conn(user: str = "app", autocommit: bool = True) -> psycopg.Connection:
    who = "OWNER" if user == "owner" else "APP"
    default = "ops_owner" if user == "owner" else "ops_app"
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get(f"DB_{who}_USER", default),
        password=os.environ.get(f"DB_{who}_PASSWORD", f"{default}_dev"),
        autocommit=autocommit,
    )


def _one(sql: str, params: list[Any] | None = None) -> Any:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()[0]


def _ins(cur: psycopg.Cursor, table: str, **cols: Any) -> int:
    names = list(cols)
    vals = [Jsonb(v) if isinstance(v, dict | list) else v for v in cols.values()]
    cur.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) RETURNING id",
        vals,
    )
    return int(cur.fetchone()[0])


def _refused(conn: psycopg.Connection, sql: str, params: list[Any] | None = None, match: str = "") -> None:
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as err:
        cur.execute(sql, params or [])
    assert match in str(err.value), str(err.value)


def _status(robot: str, production: bool, at: str = "clock_timestamp()") -> tuple[Any, ...]:
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT state, due_at, inspection_id, rule_id FROM readiness_periodic_inspection_status("
            f"(SELECT id FROM fleet_robot WHERE code = %s), %s, {at})",
            [robot, production],
        )
        return tuple(cur.fetchone())


def _owner_rewrite(table: str, sql: str, params: list[Any]) -> None:
    """Test-only owner rewrite of an append-only row (the documented owner trust boundary)."""
    with _conn("owner", autocommit=False) as owner, owner.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        cur.execute(sql, params)
        owner.commit()  # commit first: pending foreign-key events forbid ALTER TABLE in the same transaction
        cur.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        owner.commit()


def _pattern(code: str = "P-A") -> ManifestPattern:
    return ManifestPattern.objects.get(code=code)


def _robot(code: str = "R-01") -> Robot:
    return Robot.objects.get(code=code)


def _issue_args(visit_code: str) -> list[Any]:
    """Arguments of readiness_issue_authorization from the evaluator, exactly as authorize_departure passes them."""
    v = Visit.objects.get(code=visit_code)
    res = evaluate_readiness(v)
    mode = "demo" if fs.demo_mode() else "production"
    return [
        v.pk,
        None,  # issuer filled by the caller
        res.expires_at,
        Jsonb(res.evidence_ids),
        res.recovery_plan_id,
        Jsonb(res.guard_snapshot),
        mode,
    ]


def _raw_issue(conn: psycopg.Connection, w, visit_code: str | None = None) -> int:
    args = _issue_args(visit_code or w.visit)
    args[1] = w.ops.pk
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM readiness_issue_authorization(%s, %s, %s, %s, %s, %s, %s)", args)
        return int(cur.fetchone()[0])


def _raw_depart(conn: psycopg.Connection, w, auth_id: int, lock: bool = True) -> int:
    a = DepartureAuthorization.objects.get(pk=auth_id)
    with conn.transaction(), conn.cursor() as cur:
        if lock:
            cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY])
        return _ins(
            cur,
            "readiness_departureevent",
            authorization_id=a.pk,
            visit_id=a.visit_id,
            robot_id=a.robot_id,
            recorded_by_id=w.ops.pk,
            guard_snapshot=a.guard_snapshot,
        )


@pytest.fixture
def w(transactional_db):
    return build_readiness(prefix="s7_")


# ================================================================== section 2.2: settings-lock assertion (EM ruling 2)
def test_platform_settings_lock_held_matches_pg_locks_encoding(transactional_db):
    a, b = _conn(autocommit=False), _conn(autocommit=False)
    try:
        with a.cursor() as ca, b.cursor() as cb:
            ca.execute("SELECT platform_settings_lock_held()")
            assert ca.fetchone()[0] is False  # no lock held
            ca.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY + 1])
            ca.execute("SELECT platform_settings_lock_held()")
            assert ca.fetchone()[0] is False  # a different advisory key does not count
            ca.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY])
            ca.execute("SELECT platform_settings_lock_held()")
            assert ca.fetchone()[0] is True  # shared xact lock
            ca.execute(
                "SELECT classid::bigint, objid::bigint, objsubid FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND mode = 'ShareLock' AND objid = 518479479"
            )
            assert ca.fetchone() == (15, 518479479, 1)  # PostgreSQL 16 encoding of the bigint key
            cb.execute("SELECT platform_settings_lock_held()")
            assert cb.fetchone()[0] is False  # another session holds it: false for this session
            a.commit()
            ca.execute("SELECT platform_settings_lock_held()")
            assert ca.fetchone()[0] is False  # the completed transaction no longer owns it
            ca.execute("SELECT pg_advisory_xact_lock(%s)", [KEY])
            ca.execute("SELECT platform_settings_lock_held()")
            assert ca.fetchone()[0] is True  # exclusive xact lock
            cb.execute("SELECT platform_settings_lock_held()")
            assert cb.fetchone()[0] is False
            a.rollback()
            ca.execute("SELECT platform_settings_lock_held()")
            assert ca.fetchone()[0] is False
            b.rollback()
    finally:
        a.close()
        b.close()


def test_raw_departure_without_the_settings_lock_is_refused(w, app_role_conn):
    """EM ruling 1: the departure trigger asserts the settings lock; a raw departure without it is refused."""
    a = DepartureAuthorization.objects.get(code=rc.authorize_departure(w.ops, visit_code=w.visit)["code"])
    with pytest.raises(
        CV, match="a departure depends on the deployment mode: take pg_advisory_xact_lock_shared"
    ):
        _raw_depart(app_role_conn, w, a.pk, lock=False)
    assert not DepartureEvent.objects.exists()
    _raw_depart(
        app_role_conn, w, a.pk, lock=True
    )  # control: with the lock taken first it is decided normally
    assert DepartureEvent.objects.filter(authorization=a).count() == 1


# ================================================================== section 2.3: forced database times
def test_inspection_hold_and_qualification_times_are_forced(w, app_role_conn):
    r = _robot("R-02")
    tmpl = InspectionTemplate.objects.get(pattern=_pattern(), version="T1")
    with app_role_conn.cursor() as cur:
        iid = _ins(
            cur,
            "fleet_inspection",
            code="RC-T1",
            robot_id=r.pk,
            manifest_id=r.current_manifest_id,
            template_id=tmpl.pk,
            visit_ref="",
            answers={i["id"]: "pass" for i in TEMPLATE_ITEMS},
            result="PASS",
            complete=True,
            failed_robot_items=[],
            failed_visit_items=[],
            performer_id=w.field.pk,
            verifier_id=w.field2.pk,
            submitted_by_id=w.ops.pk,
            submitted_at="2099-01-01T00:00:00Z",
        )
        cur.execute(
            "SELECT submitted_at <= clock_timestamp() AND submitted_at > now() - interval '1 minute' "
            "FROM fleet_inspection WHERE id = %s",
            [iid],
        )
        assert cur.fetchone()[0] is True
    policy = HoldPolicy.objects.get(version=1)
    with app_role_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_hold (code, scope, robot_id, blocking, source_type, source_ref, reason, policy_id, "
            "required_slots, manifest_at_placement_id, placed_by_id, placed_at) VALUES ('HD-T1', 'robot', %s, true, "
            "'maintenance', '', 'SYNTHETIC', %s, %s, %s, %s, '2001-01-01T00:00:00Z') RETURNING placed_at > now() - "
            "interval '1 minute'",
            [r.pk, policy.pk, Jsonb(policy.rules["maintenance"]["slots"]), r.current_manifest_id, w.ops.pk],
        )
        assert cur.fetchone()[0] is True
        p3 = Person.objects.get(code="P-3")
        cur.execute(
            "INSERT INTO planning_qualification (person_id, kind, valid_from, valid_to, granted_by_id, granted_at, "
            "basis_ref) VALUES (%s, 'field_lead', now() - interval '1 day', now() + interval '1 day', %s, "
            "'2001-01-01T00:00:00Z', '') RETURNING granted_at > now() - interval '1 minute'",
            [p3.pk, w.ops.pk],
        )
        assert cur.fetchone()[0] is True


def test_immediate_inspector_grant_takes_one_database_time(w, app_role_conn):
    """Case h (P1-6): valid_from NULL -> valid_from = granted_at = DB time, even when the app clock runs ahead."""
    p4 = Person.objects.get(code="P-4")
    with app_role_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO planning_qualification (person_id, kind, valid_from, valid_to, granted_by_id, granted_at, "
            "basis_ref) VALUES (%s, 'readiness_inspector', NULL, now() + interval '30 days', %s, "
            "now() + interval '1 hour', 'SYNTHETIC-TRAINING') "
            "RETURNING valid_from = granted_at, granted_at <= clock_timestamp()",
            [p4.pk, w.safety.pk],
        )
        assert cur.fetchone() == (True, True)
        # explicit future valid_from: accepted; explicit past: refused
        cur.execute(
            "INSERT INTO planning_qualification (person_id, kind, valid_from, valid_to, granted_by_id, granted_at, "
            "basis_ref) VALUES (%s, 'readiness_inspector', now() + interval '1 day', now() + interval '30 days', %s, "
            "now(), 'SYNTHETIC-TRAINING')",
            [p4.pk, w.safety.pk],
        )
    _refused(
        app_role_conn,
        "INSERT INTO planning_qualification (person_id, kind, valid_from, valid_to, granted_by_id, granted_at, "
        "basis_ref) VALUES (%s, 'readiness_inspector', now() - interval '1 second', now() + interval '30 days', %s, "
        "now(), 'SYNTHETIC-TRAINING')",
        [p4.pk, w.safety.pk],
        match="cannot be backdated",
    )


# ================================================================== X-8 grants and coverage
def _grant_sql(
    kind: str = "readiness_inspector", valid_from: str = "NULL", basis: str = "'SYNTHETIC-TRAINING'"
):
    return (
        "INSERT INTO planning_qualification (person_id, kind, valid_from, valid_to, granted_by_id, granted_at, "
        f"basis_ref) VALUES (%s, '{kind}', {valid_from}, now() + interval '30 days', %s, now(), {basis})"
    )


def test_inspector_grant_refusals(w, app_role_conn):
    p4 = Person.objects.get(code="P-4")
    _refused(app_role_conn, _grant_sql(), [p4.pk, w.ops.pk], match="only by a safety_reviewer")
    ps.register_person(w.ops, username=w.safety.username, code="P-S")
    _refused(
        app_role_conn,
        _grant_sql(),
        [Person.objects.get(code="P-S").pk, w.safety.pk],
        match="granted to oneself",
    )
    _refused(app_role_conn, _grant_sql(basis="'  '"), [p4.pk, w.safety.pk], match="training record reference")
    _refused(
        app_role_conn,
        _grant_sql(valid_from="now() - interval '1 hour'"),
        [p4.pk, w.safety.pk],
        match="cannot be backdated",
    )
    _refused(
        app_role_conn,
        _grant_sql(kind="wizard", valid_from="now()"),
        [p4.pk, w.safety.pk],
        match="qualification_kind_valid",
    )
    with app_role_conn.cursor() as cur:
        cur.execute(_grant_sql(), [p4.pk, w.safety.pk])  # control


def test_covered_signer_rule(w):
    insp = Inspection.objects.get(code=w.inspection)
    assert _one("SELECT planning_inspection_signer_problem(%s)", [insp.pk]) is None
    # expiry AFTER the inspection does not invalidate it
    q = Qualification.objects.get(person__code="P-3", kind="readiness_inspector")
    _owner_rewrite(
        "planning_qualification",
        "UPDATE planning_qualification SET valid_to = clock_timestamp() + interval '1 second' WHERE id = %s",
        [q.pk],
    )
    assert _one("SELECT planning_inspection_signer_problem(%s)", [insp.pk]) is None
    # a grant that started only after the inspection does not cover it
    _owner_rewrite(
        "planning_qualification",
        "UPDATE planning_qualification SET valid_from = %s + interval '1 microsecond' WHERE id = %s",
        [insp.submitted_at, q.pk],
    )
    insp.refresh_from_db()
    problem = _one("SELECT planning_inspection_signer_problem(%s)", [insp.pk])
    assert problem == f"inspector P-3 is not a qualified readiness inspector for inspection {insp.code}"
    _owner_rewrite(
        "planning_qualification",
        "UPDATE planning_qualification SET valid_from = %s WHERE id = %s",
        [insp.submitted_at, q.pk],
    )
    assert _one("SELECT planning_inspection_signer_problem(%s)", [insp.pk]) is None  # boundary inclusive
    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    assert "P-3 is not a qualified readiness inspector" in _one(
        "SELECT planning_inspection_signer_problem(%s)", [insp.pk]
    )


def _evidence_sql() -> str:
    return (
        "INSERT INTO readiness_readinessevidence (code, visit_id, revision_id, robot_id, manifest_id, kind, result, "
        "inspection_id, details, observed_at, recorded_by_id, recorded_at) VALUES (%s, %s, %s, %s, %s, "
        "'pre_departure_inspection', %s, %s, '{}', now(), %s, now())"
    )


def _evidence_args(w, code: str, result: str, inspection_code: str) -> list[Any]:
    v = Visit.objects.get(code=w.visit)
    r = _robot()
    return [
        code,
        v.pk,
        v.current_revision_id,
        r.pk,
        r.current_manifest_id,
        result,
        Inspection.objects.get(code=inspection_code).pk,
        w.ops.pk,
    ]


def test_passing_evidence_needs_qualified_signers_in_both_modes(w, app_role_conn):
    q = Qualification.objects.get(person__code="P-3", kind="readiness_inspector")
    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    make_engineering_template(w, "P-A", "E1")  # approved, so production reaches the signer check
    insp = fs.submit_inspection(  # recording is never refused (principle 1)
        w.ops,
        robot_code="R-01",
        template_version="E1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
        visit_ref=w.visit,
    )["entity_id"]
    for mode in ("true", "false"):
        set_setting("demo_mode", mode)
        _refused(
            app_role_conn,
            _evidence_sql(),
            _evidence_args(w, f"EV-Q{mode}", "pass", insp),
            match="passing pre-departure evidence refused: inspector P-3 is not a qualified readiness inspector",
        )
        with app_role_conn.cursor() as cur:  # fail evidence is always accepted
            cur.execute(_evidence_sql(), _evidence_args(w, f"EV-F{mode}", "fail", insp))


def test_production_refuses_passing_evidence_on_an_unapproved_template(w, app_role_conn):
    insp = record_inspection(w, w.visit)
    set_setting("demo_mode", "false")
    _refused(
        app_role_conn,
        _evidence_sql(),
        _evidence_args(w, "EV-P1", "pass", insp),
        match=f"passing pre-departure evidence refused: inspection {insp} uses template T1, which is not approved "
        "for pre-departure use in production",
    )
    with app_role_conn.cursor() as cur:
        cur.execute(_evidence_sql(), _evidence_args(w, "EV-P2", "fail", insp))
    set_setting("demo_mode", "true")
    with app_role_conn.cursor() as cur:
        cur.execute(_evidence_sql(), _evidence_args(w, "EV-P3", "pass", insp))  # demo: synthetic usable


# ================================================================== X-1 templates
def _template_sql(source: str = "engineering_doc", ref: str = "DOC-1") -> str:
    return (
        "INSERT INTO fleet_inspectiontemplate (pattern_id, version, items, source, created_by_id, procedure_ref) "
        f"VALUES (%s, %s, %s, '{source}', %s, '{ref}')"
    )


def test_template_insert_rules(w, app_role_conn):
    pid = _pattern().pk
    items = Jsonb(TEMPLATE_ITEMS)
    _refused(app_role_conn, _template_sql(), [pid, "X1", items, None], match="needs an author")
    _refused(app_role_conn, _template_sql(), [pid, "X2", items, w.ops.pk], match="requires engineer")
    _refused(
        app_role_conn, _template_sql(ref=""), [pid, "X3", items, w.eng.pk], match="needs a procedure_ref"
    )
    _refused(
        app_role_conn, _template_sql(source="made_up"), [pid, "X4", items, w.eng.pk], match="template_source"
    )
    for bad, msg in (
        ([], "non-empty list"),
        (
            [
                {"id": "a", "label": "x", "robot_condition": True},
                {"id": "a", "label": "y", "robot_condition": True},
            ],
            "duplicate item id",
        ),
        ([{"id": "a", "label": " ", "robot_condition": True}], "non-blank label"),
        ([{"id": "a", "label": "x", "robot_condition": "yes"}], "boolean robot_condition"),
    ):
        _refused(app_role_conn, _template_sql(), [pid, "X5", Jsonb(bad), w.eng.pk], match=msg)
    with app_role_conn.cursor() as cur:
        cur.execute(_template_sql(), [pid, "X6", items, w.eng.pk])  # control


def _approve_template_sql() -> str:
    return (
        "INSERT INTO fleet_inspectiontemplateapproval (template_id, purpose, slot, approver_id, approved_at) "
        "VALUES (%s, %s, %s, %s, now())"
    )


def test_template_approval_refusals_and_pattern_bump(w, app_role_conn):
    doc = make_engineering_template(w, "P-A", "E1", purposes=())
    synthetic = InspectionTemplate.objects.get(pattern=_pattern(), version="T1")
    legacy = InspectionTemplate.objects.create(
        pattern=_pattern(),
        version="L1",
        items=TEMPLATE_ITEMS,
        source="engineering_doc",
        procedure_ref="DOC-L",
        created_by=w.eng,
    )
    _owner_rewrite(
        "fleet_inspectiontemplate",
        "UPDATE fleet_inspectiontemplate SET created_by_id = NULL WHERE id = %s",
        [legacy.pk],
    )
    sql = _approve_template_sql()
    _refused(app_role_conn, sql, [doc.pk, "pre_departure", "engineering", w.eng.pk], match="requires role")
    author = make_user("s7_author_lead", "engineering_lead")
    by_lead = InspectionTemplate.objects.create(
        pattern=_pattern(),
        version="E2",
        items=TEMPLATE_ITEMS,
        source="engineering_doc",
        procedure_ref="D",
        created_by=author,
    )
    _refused(app_role_conn, sql, [by_lead.pk, "pre_departure", "engineering", author.pk], match="author")
    _refused(
        app_role_conn,
        sql,
        [synthetic.pk, "pre_departure", "engineering", w.lead.pk],
        match="never be approved",
    )
    _refused(app_role_conn, sql, [legacy.pk, "pre_departure", "engineering", w.lead.pk], match="legacy")
    both = make_user("s7_both", "engineering_lead", "safety_reviewer")
    before = _pattern().version
    with app_role_conn.cursor() as cur:
        cur.execute(sql, [doc.pk, "pre_departure", "engineering", both.pk])
    assert _pattern().version == before + 1  # R1-4
    _refused(app_role_conn, sql, [doc.pk, "pre_departure", "safety", both.pk], match="same person")
    InspectionTemplateWithdrawal.objects.create(template=doc, withdrawn_by=w.safety, reason="SYNTHETIC")
    assert _pattern().version == before + 2
    _refused(app_role_conn, sql, [doc.pk, "pre_departure", "safety", w.safety.pk], match="withdrawn")
    _refused(
        app_role_conn,
        "INSERT INTO fleet_inspectiontemplatewithdrawal (template_id, withdrawn_by_id, reason, withdrawn_at) "
        "VALUES (%s, %s, 'x', now())",
        [by_lead.pk, w.eng.pk],
        match="requires engineering_lead or safety_reviewer",
    )


def _usable(template_id: int, purpose: str, production: bool) -> bool:
    return bool(_one("SELECT fleet_template_usable(%s, %s, %s)", [template_id, purpose, production]))


def test_template_usability_truth_table(w):
    """R1-1 oracle fixture: purpose x mode x {synthetic, unapproved doc, one slot, both slots, withdrawn}."""
    synthetic = InspectionTemplate.objects.get(pattern=_pattern(), version="T1")
    unapproved = make_engineering_template(w, "P-A", "U1", purposes=())
    one = make_engineering_template(w, "P-A", "O1", purposes=())
    InspectionTemplateApproval.objects.create(
        template=one, purpose="pre_departure", slot="engineering", approver=w.lead
    )
    both = make_engineering_template(w, "P-A", "B1", purposes=("pre_departure",))
    withdrawn = make_engineering_template(w, "P-A", "W1", purposes=("pre_departure", "periodic"))
    InspectionTemplateWithdrawal.objects.create(template=withdrawn, withdrawn_by=w.lead, reason="SYNTHETIC")
    expected = {
        # (template, purpose): (production, demo)
        (synthetic, "pre_departure"): (False, True),
        (synthetic, "periodic"): (False, True),
        (unapproved, "pre_departure"): (False, False),
        (one, "pre_departure"): (False, False),
        (both, "pre_departure"): (True, True),
        (both, "periodic"): (False, False),  # each purpose is approved separately
        (withdrawn, "pre_departure"): (False, False),
        (withdrawn, "periodic"): (False, False),
    }
    for (t, purpose), (prod, demo) in expected.items():
        assert _usable(t.pk, purpose, True) is prod, (t.version, purpose, "production")
        assert _usable(t.pk, purpose, False) is demo, (t.version, purpose, "demo")
    assert _usable(999_999, "pre_departure", False) is False


# ================================================================== X-3 rules and status
def test_periodic_rule_rules_and_active_without_fallback(w, app_role_conn):
    pid = _pattern().pk
    ins = (
        "INSERT INTO fleet_periodicinspectionrule (pattern_id, version_no, interval_min, source, procedure_ref, "
        "notes, created_by_id, created_at) VALUES (%s, %s, %s, %s, %s, '', %s, now())"
    )
    _refused(app_role_conn, ins, [pid, 5, 60, "synthetic_demo", "", w.eng.pk], match="consecutive")
    _refused(app_role_conn, ins, [pid, 2, 60, "synthetic_demo", "", w.field.pk], match="requires engineer")
    _refused(app_role_conn, ins, [pid, 2, 60, "engineering_doc", "", w.eng.pk], match="needs a procedure_ref")
    _refused(app_role_conn, ins, [pid, 2, 0, "synthetic_demo", "", w.eng.pk], match="interval_range")
    v1 = PeriodicInspectionRule.objects.get(pattern_id=pid, version_no=1)
    v2 = make_periodic_rule(w, "P-A", interval_min=60, approve=False)
    appr = (
        "INSERT INTO fleet_periodicinspectionruleapproval (rule_id, slot, approver_id, approved_at) "
        "VALUES (%s, %s, %s, now())"
    )
    _refused(app_role_conn, appr, [v2.pk, "engineering", w.eng.pk], match="requires role")
    lead_author = make_user("s7_rule_author", "engineering_lead", "safety_reviewer")
    v3 = PeriodicInspectionRule.objects.create(
        pattern_id=pid, version_no=3, interval_min=60, source="synthetic_demo", created_by=lead_author
    )
    _refused(app_role_conn, appr, [v3.pk, "engineering", lead_author.pk], match="author")
    active = "SELECT fleet_active_periodic_rule_id(%s)"
    assert _one(active, [pid]) == v1.pk  # v2, v3 drafts
    with app_role_conn.cursor() as cur:
        cur.execute(appr, [v2.pk, "engineering", w.lead.pk])
    _refused(app_role_conn, appr, [v2.pk, "safety", w.lead.pk], match="requires role safety_reviewer")
    PeriodicInspectionRuleApproval.objects.create(rule=v2, slot="safety", approver=w.safety)
    assert _one(active, [pid]) == v2.pk
    PeriodicInspectionRuleWithdrawal.objects.create(rule=v2, withdrawn_by=w.safety, reason="SYNTHETIC")
    assert _one(active, [pid]) is None  # no fallback to v1
    assert _status("R-01", False)[0] == "unknown_interval"
    both = make_user("s7_rule_both", "engineering_lead", "safety_reviewer")
    with app_role_conn.cursor() as cur:
        cur.execute(appr, [v3.pk, "engineering", both.pk])
    _refused(app_role_conn, appr, [v3.pk, "safety", both.pk], match="same person")
    PeriodicInspectionRuleWithdrawal.objects.create(rule=v3, withdrawn_by=w.safety, reason="SYNTHETIC")
    _refused(app_role_conn, appr, [v3.pk, "safety", w.safety.pk], match="withdrawn periodic inspection rule")


def test_periodic_status_states_and_boundaries(w):
    state, due, iid, rid = _status("R-01", False)
    insp = Inspection.objects.get(code=w.inspection)
    assert (state, iid) == ("due", insp.pk)  # the pre-departure PASS on synthetic T1 qualifies in demo
    assert due == insp.submitted_at + timedelta(minutes=SYNTHETIC_PERIODIC_INTERVAL_MIN)
    # boundary at due_at: -1 us due, = overdue, +1 us overdue
    for delta, expected in ((-1, "due"), (0, "overdue"), (1, "overdue")):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT state FROM readiness_periodic_inspection_status(%s, false, %s + %s * interval '1 microsecond')",
                [_robot().pk, due, delta],
            )
            assert cur.fetchone()[0] == expected, delta
    assert _status("R-01", True)[0] == "unknown_interval"  # synthetic rule refused in production
    # every world robot gets a periodic inspection (fleet_world); a new manifest leaves R-02 with none
    fs.record_manifest(
        w.eng, robot_code="R-02", pattern_code="P-A", autonomy_version="a9", safety_config_hash="m" * 64
    )
    assert _status("R-02", False)[0] == "never_inspected"
    # a newer approved interval changes due_at
    make_periodic_rule(w, "P-A", interval_min=SYNTHETIC_PERIODIC_INTERVAL_MIN + 60)
    assert _status("R-01", False)[1] == due + timedelta(minutes=60)
    # NULL interval = unknown, in both modes
    make_periodic_rule(w, "P-A", interval_min=None)
    assert _status("R-01", False)[0] == "unknown_interval"
    make_periodic_rule(w, "P-A")
    # a new manifest restarts the clock with nothing qualifying
    fs.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a9", safety_config_hash="k" * 64
    )
    assert _status("R-01", False)[0] == "never_inspected"


def test_periodic_inspection_stops_qualifying_when_a_signer_is_revoked(w):
    assert _status("R-01", False)[0] == "due"
    q = Qualification.objects.get(person__code="P-2", kind="readiness_inspector")
    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    assert _status("R-01", False)[0] == "never_inspected"


# ================================================================== X-3 / X-8 / X-1: the issue function
def test_issue_function_records_the_relied_periodic_inspection(w, app_role_conn):
    aid = _raw_issue(app_role_conn, w)
    a = DepartureAuthorization.objects.get(pk=aid)
    insp = Inspection.objects.get(code=w.inspection)
    unload = Reservation.objects.filter(revision_id=a.revision_id, purpose="unloading").order_by("-ends_at")[
        0
    ]
    assert a.periodic_inspection_id == insp.pk and a.planned_unload_end == unload.ends_at
    assert str(Person.objects.get(code="P-3").pk) in a.guard_snapshot["person_versions"]  # signer-only person
    assert a.expires_at <= insp.submitted_at + timedelta(minutes=SYNTHETIC_PERIODIC_INTERVAL_MIN)
    # a newer qualifying inspection after authorization does not change what it relied on
    fs.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="T1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
    )
    a.refresh_from_db()
    assert a.periodic_inspection_id == insp.pk


def test_issue_function_refuses_each_periodic_state(w, app_role_conn):
    PeriodicInspectionRuleWithdrawal.objects.create(
        rule=PeriodicInspectionRule.objects.get(pattern=_pattern(), version_no=1),
        withdrawn_by=w.safety,
        reason="x",
    )
    with pytest.raises(
        CV, match="authorization refused: periodic inspection interval unknown for configuration P-A"
    ):
        _raw_issue(app_role_conn, w)
    make_periodic_rule(w, "P-A")
    insp = Inspection.objects.get(code=w.inspection)
    _owner_rewrite(  # every R-01 inspection (the world's periodic one too), so none still covers now
        "fleet_inspection",
        "UPDATE fleet_inspection SET submitted_at = %s WHERE robot_id = %s",
        [insp.submitted_at - timedelta(minutes=SYNTHETIC_PERIODIC_INTERVAL_MIN + 1), insp.robot_id],
    )
    q = Qualification.objects.filter(kind="readiness_inspector", person__code__in=["P-2", "P-3"])
    _owner_rewrite(
        "planning_qualification",
        "UPDATE planning_qualification SET valid_from = valid_from - interval '200 days' WHERE id = ANY(%s)",
        [[x.pk for x in q]],
    )
    with pytest.raises(CV, match="authorization refused: periodic inspection overdue since"):
        _raw_issue(app_role_conn, w)
    q = Qualification.objects.get(person__code="P-2", kind="readiness_inspector")
    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    with pytest.raises(
        CV, match="authorization refused: periodic inspection none on the current configuration"
    ):
        _raw_issue(app_role_conn, w)
    assert not DepartureAuthorization.objects.exists()


def test_due_at_must_be_strictly_after_the_planned_unloading_end(w, app_role_conn):
    """Case f (P1-4): due_at = unload_end - 1 us refused, = refused, + 1 us accepted."""
    rev = Visit.objects.get(code=w.visit).current_revision
    unload_end = Reservation.objects.filter(revision=rev, purpose="unloading").order_by("-ends_at")[0].ends_at
    now = _one("SELECT clock_timestamp()")
    interval = int((unload_end - now).total_seconds() // 60) - 1  # inspection lands just after now
    make_periodic_rule(w, "P-A", interval_min=interval)
    insp = Inspection.objects.get(code=w.inspection)
    base = unload_end - timedelta(minutes=interval)
    for delta_us, ok in ((-1, False), (0, False), (1, True)):
        _owner_rewrite(
            "fleet_inspection",
            "UPDATE fleet_inspection SET submitted_at = %s WHERE id = %s",
            [base + timedelta(microseconds=delta_us), insp.pk],
        )
        if ok:
            aid = _raw_issue(app_role_conn, w)
            assert DepartureAuthorization.objects.get(pk=aid).periodic_inspection_id == insp.pk
        else:
            with pytest.raises(CV, match="not after the planned unloading end"):
                _raw_issue(app_role_conn, w)
        assert (
            _one(
                "SELECT readiness_periodic_dependency_problem(%s, %s, false, %s)",
                [insp.pk, _robot().pk, unload_end],
            )
            is None
        ) is ok


def _production_world(w) -> None:
    """SYNTHETIC production fixtures (test approvals by test users, never company approvals): approved_procedure
    readiness policy, engineering_doc capability profile, engineering_doc periodic rule and an engineering_doc
    template E1 approved for both purposes, then fresh evidence on E1 for w.visit."""
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


def _record_e1(w) -> str:
    insp = fs.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="E1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
        visit_ref=w.visit,
    )["entity_id"]
    rx.record_evidence(
        w.ops, visit_code=w.visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    for kind, details in (
        (
            "equipment_check",
            {"items": {"restraint_kit": "present", "recovery_kit": "present", "charger": "present"}},
        ),
        ("battery_check", {"soc_pct": 85}),
    ):
        rx.record_evidence(w.field, visit_code=w.visit, kind=kind, result="pass", details=details)
    rx.record_recovery_plan(
        w.ops,
        visit_code=w.visit,
        on_call_code="P-4",
        escalation_code="P-5",
        escalation_path="SYNTHETIC path",
        required_equipment=["recovery_kit"],
    )
    return insp


def test_production_authorization_relies_only_on_approved_templates(w, app_role_conn):
    _production_world(w)
    # evidence on synthetic T1 (recorded in demo), periodic coverage from an E1 inspection
    fs.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="E1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
    )
    record_inspection(w, w.visit)
    rx.record_evidence(
        w.ops,
        visit_code=w.visit,
        kind="pre_departure_inspection",
        result="pass",
        inspection_code=Inspection.objects.filter(template__version="T1").latest("id").code,
    )
    rx.record_recovery_plan(
        w.ops,
        visit_code=w.visit,
        on_call_code="P-4",
        escalation_code="P-5",
        escalation_path="SYNTHETIC path",
        required_equipment=["recovery_kit"],
    )
    set_setting("demo_mode", "false")  # demo -> production switch after demo evidence
    with pytest.raises(
        CV, match="uses template T1, which is not approved for pre-departure use in production"
    ):
        _raw_issue(app_role_conn, w)
    insp = _record_e1(w)
    aid = _raw_issue(app_role_conn, w)
    a = DepartureAuthorization.objects.get(pk=aid)
    assert a.mode == "production" and a.periodic_inspection_id == Inspection.objects.get(code=insp).pk
    # R1-4: a template withdrawal after authorization blocks the departure (pattern version)
    InspectionTemplateWithdrawal.objects.create(
        template=InspectionTemplate.objects.get(pattern=_pattern(), version="E1"),
        withdrawn_by=w.safety,
        reason="x",
    )
    with pytest.raises(CV, match="guards changed since authorization: (robot|manifest pattern) version"):
        _raw_depart(app_role_conn, w, aid)


def test_production_policy_must_require_pre_departure_inspection(w, app_role_conn):
    _production_world(w)
    _record_e1(w)
    w.make_policy(
        source="approved_procedure",
        requirements=requirements(
            evidence=[e for e in REQUIREMENTS["evidence"] if e["kind"] != "pre_departure_inspection"]
        ),
    )
    set_setting("demo_mode", "false")
    with pytest.raises(CV, match="does not require pre_departure_inspection"):
        _raw_issue(app_role_conn, w)


# ================================================================== case a (P0-1): periodic-only signer revoked
def _periodic_only_signer_world(w) -> tuple[int, Qualification]:
    """R-01's relied periodic inspection is signed by P-4 + P-5 (on-call people, not crew, not pre-departure
    signers); the pre-departure inspection is on a NON-periodic engineering template, so only the older inspection
    qualifies as periodic."""
    for code in ("P-4", "P-5"):
        grant_inspector(w, code)
    w.eng.role_grants.create(role="field_operator")  # P-4 (eng) may sign
    w.lead2.role_grants.create(role="field_operator")  # P-5 (lead2) may sign
    fs.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="T1",
        answers=dict(w.all_pass),
        performer=w.eng.username,
        verifier=w.lead2.username,
    )
    pre = make_engineering_template(w, "P-A", "PD", purposes=("pre_departure",))
    assert pre.pk
    insp = fs.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="PD",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
        visit_ref=w.visit,
    )["entity_id"]
    rx.record_evidence(
        w.ops, visit_code=w.visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    # the T1 pre-departure inspection of the world is newer than nothing now: make the P-4/P-5 one the latest T1
    _owner_rewrite(
        "fleet_inspection",
        "UPDATE fleet_inspection SET submitted_at = submitted_at - interval '1 day' WHERE code = %s",
        [w.inspection],
    )
    a = DepartureAuthorization.objects.get(code=rc.authorize_departure(w.ops, visit_code=w.visit)["code"])
    periodic = Inspection.objects.get(pk=a.periodic_inspection_id)
    assert {periodic.performer_id, periodic.verifier_id} == {w.eng.pk, w.lead2.pk}
    for code in ("P-4", "P-5"):
        assert str(Person.objects.get(code=code).pk) in a.guard_snapshot["person_versions"]
    return a.pk, Qualification.objects.get(person__code="P-4", kind="readiness_inspector")


def test_periodic_only_signer_revoked_blocks_raw_departure(w, app_role_conn):
    aid, q = _periodic_only_signer_world(w)
    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    with pytest.raises(CV, match="guards changed since authorization: person P-4"):
        _raw_depart(app_role_conn, w, aid)
    with pytest.raises(Exception, match="person_versions"):  # the command's explained refusal
        rc.record_departure(w.ops, authorization_code=DepartureAuthorization.objects.get(pk=aid).code)
    assert not DepartureEvent.objects.exists()


MUTATIONS = {
    # each layer alone still refuses (owner replaces the OTHER layers inside a rolled-back transaction)
    "person_version_disabled": (
        [
            "readiness_guard_problem(s jsonb, vid bigint, plan_id bigint)",
            "readiness_signer_versions_problem(p_snapshot jsonb, p_evidence_ids jsonb, p_periodic_inspection bigint)",
        ],
        "the relied periodic inspection no longer qualifies: periodic inspection",
    ),
    "dependency_disabled": (
        [
            "readiness_periodic_dependency_problem(p_inspection bigint, p_robot bigint, p_production boolean, "
            "p_until timestamptz)",
            "readiness_signer_versions_problem(p_snapshot jsonb, p_evidence_ids jsonb, p_periodic_inspection bigint)",
        ],
        "guards changed since authorization: person P-4",
    ),
}


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_periodic_signer_layers_each_refuse_alone(w, name):
    aid, q = _periodic_only_signer_world(w)
    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    fns, match = MUTATIONS[name]
    a = DepartureAuthorization.objects.get(pk=aid)
    with _conn("owner", autocommit=False) as owner, owner.cursor() as cur:
        for sig in fns:
            cur.execute(
                f"CREATE OR REPLACE FUNCTION {sig} RETURNS text LANGUAGE sql STABLE AS $$ SELECT NULL::text $$"
            )
        cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY])
        with pytest.raises(CV, match=match):
            _ins(
                cur,
                "readiness_departureevent",
                authorization_id=a.pk,
                visit_id=a.visit_id,
                robot_id=a.robot_id,
                recorded_by_id=w.ops.pk,
                guard_snapshot=a.guard_snapshot,
            )
        owner.rollback()  # restores the real function bodies (DDL is transactional)
    assert "SELECT NULL" not in _one(
        "SELECT prosrc FROM pg_proc WHERE proname = 'readiness_signer_versions_problem'"
    )


# ================================================================== G-01 hold policies
def _policy_sql() -> str:
    return (
        "INSERT INTO fleet_holdpolicy (version, rules, source, procedure_ref, created_by_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, now()) RETURNING id"
    )


def _floor_rules() -> dict[str, Any]:
    rule = {"slots": [{"slot": "engineering", "roles": ["engineering_lead"]}], "distinct_approvers": True}
    rules = {s: dict(rule) for s, _ in Hold.SOURCES}
    rules["incident"] = {
        "slots": [
            {"slot": "engineering", "roles": ["engineering_lead"]},
            {"slot": "safety", "roles": ["safety_reviewer"]},
        ],
        "distinct_approvers": True,
        "independent_of_placer": True,
    }
    rules["inspection"] = {**rule, "requires_newer_pass_inspection": True}
    return rules


def _procedure_policy(w, rules: dict[str, Any] | None = None, approve: bool = True) -> HoldPolicy:
    nxt = HoldPolicy.objects.order_by("-version")[0].version + 1 if HoldPolicy.objects.exists() else 1
    p = HoldPolicy.objects.create(
        version=nxt,
        rules=rules or _floor_rules(),
        source="approved_procedure",
        procedure_ref="DOC-SYNTHETIC-TEST",
        created_by=w.eng,
    )
    if approve:
        for slot, who in (("engineering", w.lead), ("safety", w.safety), ("founder", w.founder)):
            HoldPolicyApproval.objects.create(policy=p, slot=slot, approver=who)
    return p


def test_hold_policy_lifecycle_rules(w, app_role_conn):
    v = HoldPolicy.objects.order_by("-version")[0].version
    rules = Jsonb(_floor_rules())
    _refused(
        app_role_conn, _policy_sql(), [v + 2, rules, "synthetic_demo", "", w.eng.pk], match="consecutive"
    )
    _refused(
        app_role_conn,
        _policy_sql(),
        [v + 1, rules, "synthetic_demo", "", w.ops.pk],
        match="requires engineer",
    )
    _refused(
        app_role_conn,
        _policy_sql(),
        [v + 1, rules, "approved_procedure", "", w.eng.pk],
        match="procedure_ref",
    )
    bad = _floor_rules()
    del bad["manual"]
    _refused(
        app_role_conn,
        _policy_sql(),
        [v + 1, Jsonb(bad), "approved_procedure", "D", w.eng.pk],
        match="missing manual",
    )
    bad = _floor_rules()
    bad["incident"]["independent_of_placer"] = False
    _refused(
        app_role_conn,
        _policy_sql(),
        [v + 1, Jsonb(bad), "approved_procedure", "D", w.eng.pk],
        match="independent",
    )
    bad = _floor_rules()
    bad["manual"]["slots"] = [{"slot": "x", "roles": ["admin"]}]
    _refused(
        app_role_conn,
        _policy_sql(),
        [v + 1, Jsonb(bad), "approved_procedure", "D", w.eng.pk],
        match="may not approve",
    )
    with app_role_conn.cursor() as cur:
        cur.execute(_policy_sql(), [v + 1, rules, "approved_procedure", "DOC-SYNTHETIC-TEST", w.eng.pk])
        pid = cur.fetchone()[0]
    appr = "INSERT INTO fleet_holdpolicyapproval (policy_id, slot, approver_id, approved_at) VALUES (%s, %s, %s, now())"
    eng_lead_author = HoldPolicy.objects.get(version=1)  # authored by w.lead
    _refused(
        app_role_conn, appr, [eng_lead_author.pk, "engineering", w.lead.pk], match="author of a hold policy"
    )
    _refused(
        app_role_conn, appr, [eng_lead_author.pk, "founder", w.founder.pk], match="founder slot exists only"
    )
    _refused(app_role_conn, appr, [pid, "founder", w.lead.pk], match="requires role founder")
    for slot, who in (("engineering", w.lead), ("safety", w.safety)):
        with app_role_conn.cursor() as cur:
            cur.execute(appr, [pid, slot, who.pk])
    assert _one("SELECT fleet_active_hold_policy_id()") is None  # approved_procedure needs all three slots
    with app_role_conn.cursor() as cur:
        cur.execute(appr, [pid, "founder", w.founder.pk])
    assert _one("SELECT fleet_active_hold_policy_id()") == pid
    assert _one("SELECT fleet_policy_production_usable(%s)", [pid]) is True
    HoldPolicyWithdrawal.objects.create(policy_id=pid, withdrawn_by=w.safety, reason="SYNTHETIC")
    assert _one("SELECT fleet_active_hold_policy_id()") is None  # no fallback
    _refused(app_role_conn, appr, [eng_lead_author.pk, "safety", w.eng.pk], match="requires role")


def test_binding_rules_and_mode_independence(w, app_role_conn):
    binding = "SELECT fleet_hold_binding_policy_id()"
    draft1 = HoldPolicy.objects.get(version=1)
    assert _one(binding) == draft1.pk  # no active: the newest
    approved = _procedure_policy(w)
    HoldPolicy.objects.create(version=approved.version + 1, rules=DRAFT_POLICY_RULES, created_by=w.eng)
    assert _one(binding) == approved.pk  # a newer draft does not capture new holds
    for demo, drafts in (("true", "true"), ("false", "true"), ("true", "false"), ("false", "false")):
        set_setting("demo_mode", demo)
        set_setting("allow_draft_policies", drafts)
        assert _one(binding) == approved.pk  # binding reads no setting
    h = Hold.objects.get(
        code=fs.place_hold(w.ops, scope="robot", target="R-02", source_type="manual", reason="SYNTHETIC")[
            "entity_id"
        ]
    )
    assert h.policy_id == approved.pk and h.required_slots == approved.rules["manual"]["slots"]
    r = _robot("R-02")
    _refused(
        app_role_conn,
        "INSERT INTO fleet_hold (code, scope, robot_id, blocking, source_type, source_ref, reason, policy_id, "
        "required_slots, manifest_at_placement_id, placed_by_id, placed_at) VALUES ('HD-B1', 'robot', %s, true, "
        "'manual', '', 'x', %s, %s, %s, %s, now())",
        [r.pk, draft1.pk, Jsonb(DRAFT_POLICY_RULES["manual"]["slots"]), r.current_manifest_id, w.ops.pk],
        match="a new hold binds to policy v",
    )


def test_no_policy_at_all_binds_null(transactional_db):
    from tests.fleet_world import build_world  # noqa: PLC0415

    w = build_world(robots=1, prefix="np_")
    with _conn("owner", autocommit=False) as owner, owner.cursor() as cur:
        cur.execute("ALTER TABLE fleet_holdpolicy DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE fleet_hold DISABLE TRIGGER USER")
        cur.execute("DELETE FROM fleet_hold")
        cur.execute("DELETE FROM fleet_holdpolicy")
        cur.execute("SELECT fleet_hold_binding_policy_id()")
        assert cur.fetchone()[0] is None
        owner.rollback()
    assert w.robots


def test_allow_draft_policies_ignored_in_production(w):
    h = Hold.objects.get(
        code=fs.place_hold(w.ops, scope="robot", target="R-02", source_type="manual", reason="SYNTHETIC")[
            "entity_id"
        ]
    )
    assert _one("SELECT fleet_policy_usable(%s)", [h.policy_id]) is True  # demo + allow_draft
    set_setting("demo_mode", "false")
    assert _one("SELECT fleet_policy_usable(%s)", [h.policy_id]) is False
    fs.approve_release(w.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    with pytest.raises(Exception, match="not usable in production"):
        fs.release_hold(w.ops, hold_code=h.code)
    assert Hold.objects.get(pk=h.pk).released_at is None


def test_raw_release_needs_the_lock_and_is_decided_by_the_mode_read_after_it(w, app_role_conn):
    h = Hold.objects.get(
        code=fs.place_hold(w.ops, scope="robot", target="R-02", source_type="manual", reason="SYNTHETIC")[
            "entity_id"
        ]
    )
    fs.approve_release(w.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    upd = "UPDATE fleet_hold SET released_at = now(), released_by_id = %s WHERE id = %s"
    _refused(
        app_role_conn,
        upd,
        [w.ops.pk, h.pk],
        match="releasing a hold depends on the deployment mode: take pg_advisory_xact_lock_shared(64942988919) first",
    )
    set_setting("demo_mode", "false")
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY])
        with pytest.raises(CV, match="not usable in production"), app_role_conn.transaction():
            cur.execute(upd, [w.ops.pk, h.pk])
    set_setting("demo_mode", "true")
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY])
        cur.execute(upd, [w.ops.pk, h.pk])
    assert Hold.objects.get(pk=h.pk).released_at is not None


def test_newer_pass_release_clause_needs_an_approved_template_in_production(w):
    """R1-5: production counts only a PASS on a template usable for pre_departure or periodic."""
    answers = dict(w.all_pass, I1="fail")
    out = fs.submit_inspection(
        w.ops,
        robot_code="R-02",
        template_version="T1",
        answers=answers,
        performer=w.field.username,
        verifier=w.field2.username,
    )
    h = Hold.objects.get(code=out["hold"])
    fs.approve_release(w.eng, hold_code=h.code, slot="engineering_review", role_used="engineer")
    fs.submit_inspection(
        w.ops,
        robot_code="R-02",
        template_version="T1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
    )
    h.refresh_from_db()
    # demo: the synthetic PASS satisfies the clause
    assert _one("SELECT fleet_hold_release_problem(h) FROM fleet_hold h WHERE id = %s", [h.pk]) is None
    # production: rebase the hold onto an approved procedure whose inspection rule asks for a newer PASS; the
    # synthetic PASS no longer counts, a PASS on a template approved for periodic use does.
    target = _procedure_policy(w)
    rb = _request(w, h, target)
    HoldRebaseApproval.objects.create(rebase=rb, slot="engineering", approver=w.lead)
    HoldRebaseApproval.objects.create(rebase=rb, slot="safety", approver=w.safety)
    fs.approve_release(w.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    set_setting("demo_mode", "false")
    problem = "SELECT fleet_hold_release_problem(h) FROM fleet_hold h WHERE id = %s"
    assert "on a template approved for pre-departure or periodic use" in _one(problem, [h.pk])
    make_engineering_template(w, "P-A", "E1", ("periodic",))
    fs.submit_inspection(
        w.ops,
        robot_code="R-02",
        template_version="E1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
    )
    assert _one(problem, [h.pk]) is None


# ================================================================== G-01 rebasing
def _unreleasable_hold(w) -> Hold:
    return Hold.objects.get(
        code=fs.place_hold(w.ops, scope="robot", target="R-02", source_type="manual", reason="SYNTHETIC")[
            "entity_id"
        ]
    )  # bound to draft v1


def _request(w, h: Hold, to: HoldPolicy, by=None) -> HoldRebase:
    e = _effective(h)
    seq = _one("SELECT nextval('fleet_hold_rebase_code_seq')")
    code = f"RB-{seq:04d}"
    return HoldRebase.objects.create(
        code=code,
        hold=h,
        from_policy_id=e[0],
        to_policy=to,
        based_on_rebase_id=e[1],
        required_slots=to.rules[h.source_type]["slots"],
        reason="SYNTHETIC re-basing",
        requested_by=by or w.lead,
    )


def _effective(h: Hold) -> tuple[Any, ...]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT e.policy_id, e.rebase_id FROM fleet_hold h, LATERAL fleet_hold_effective_rule(h) e WHERE h.id = %s",
            [h.pk],
        )
        return tuple(cur.fetchone())


def _rebase_approve_sql() -> str:
    return "INSERT INTO fleet_holdrebaseapproval (rebase_id, slot, approver_id, approved_at) VALUES (%s, %s, %s, now())"


def test_rebase_rules_and_activation(w, app_role_conn):
    h = _unreleasable_hold(w)
    placer_lead = make_user("s7_placer", "engineering_lead", "ops_lead")
    h2 = Hold.objects.get(
        code=fs.place_hold(placer_lead, scope="robot", target="R-03", source_type="manual", reason="x")[
            "entity_id"
        ]
    )
    target = _procedure_policy(w)
    old_approval = fs.approve_release(
        w.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead"
    )
    assert old_approval
    rb = _request(w, h, target)
    _refused(app_role_conn, _rebase_approve_sql(), [rb.pk, "engineering", w.ops.pk], match="requires role")
    rb2 = _request(w, h2, target)
    _refused(
        app_role_conn, _rebase_approve_sql(), [rb2.pk, "engineering", placer_lead.pk], match="placed the hold"
    )
    both = make_user("s7_both_rb", "engineering_lead", "safety_reviewer")
    with app_role_conn.cursor() as cur:
        cur.execute(_rebase_approve_sql(), [rb.pk, "engineering", both.pk])
    _refused(app_role_conn, _rebase_approve_sql(), [rb.pk, "safety", both.pk], match="same person")
    assert not HoldRebaseActivation.objects.exists()
    with app_role_conn.cursor() as cur:
        cur.execute(_rebase_approve_sql(), [rb.pk, "safety", w.safety.pk])
    act = HoldRebaseActivation.objects.get(rebase=rb)
    assert act.based_on_rebase_id is None and _effective(h) == (target.pk, rb.pk)
    # release approvals given before the rebase do not count; new ones are forced to the rebase
    assert "slot engineering has no valid approval" in _one(
        "SELECT fleet_hold_release_problem(h) FROM fleet_hold h WHERE id = %s", [h.pk]
    )
    fs.approve_release(w.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    assert h.approvals.order_by("-id")[0].rebase_id == rb.pk
    set_setting("demo_mode", "false")
    fs.release_hold(w.ops, hold_code=h.code)  # production: released under the approved procedure
    assert Hold.objects.get(pk=h.pk).released_at is not None
    # a hold already under a production-usable policy cannot be shopped to another
    h3 = Hold.objects.get(
        code=fs.place_hold(w.ops, scope="robot", target="R-03", source_type="maintenance", reason="x")[
            "entity_id"
        ]
    )
    assert h3.policy_id == target.pk
    with pytest.raises(Exception, match="already under a production-usable policy"):
        _request(w, h3, target)


def test_rebase_request_must_target_the_active_production_policy(w):
    h = _unreleasable_hold(w)
    synthetic_approved = HoldPolicy.objects.create(version=2, rules=DRAFT_POLICY_RULES, created_by=w.eng)
    HoldPolicyApproval.objects.create(policy=synthetic_approved, slot="engineering", approver=w.lead)
    HoldPolicyApproval.objects.create(policy=synthetic_approved, slot="safety", approver=w.safety)
    with pytest.raises(Exception, match="destination must be the active, production-usable"):
        _request(w, h, synthetic_approved)
    target = _procedure_policy(w)
    with pytest.raises(Exception, match="must be the hold's current effective policy"):
        HoldRebase.objects.create(
            code="RB-X1",
            hold=h,
            from_policy=None,
            to_policy=target,
            required_slots=target.rules["manual"]["slots"],
            reason="x",
            requested_by=w.lead,
        )
    with pytest.raises(Exception, match="copied exactly"):
        HoldRebase.objects.create(
            code="RB-X2",
            hold=h,
            from_policy_id=h.policy_id,
            to_policy=target,
            required_slots=[],
            reason="x",
            requested_by=w.lead,
        )
    with pytest.raises(Exception, match="requires engineering_lead or safety_reviewer"):
        _request(w, h, target, by=w.ops)


def test_competing_rebases_at_most_one_activation(w, app_role_conn):
    """Case e: both orders are covered by running the final approvals in either order across two holds."""
    holds = [_unreleasable_hold(w), _unreleasable_hold(w)]  # both bound to draft v1
    target = _procedure_policy(w)
    for h, (first, second) in zip(holds, ((0, 1), (1, 0)), strict=True):
        rbs = [_request(w, h, target), _request(w, h, target)]
        for rb in rbs:
            HoldRebaseApproval.objects.create(rebase=rb, slot="engineering", approver=w.lead)
        HoldRebaseApproval.objects.create(rebase=rbs[first], slot="safety", approver=w.safety)
        with pytest.raises(
            CV, match=f"rebase {rbs[second].code} refused: superseded by rebase {rbs[first].code}"
        ):
            with app_role_conn.cursor() as cur:
                cur.execute(_rebase_approve_sql(), [rbs[second].pk, "safety", w.safety.pk])
        assert HoldRebaseActivation.objects.filter(hold=h).count() == 1


def test_destination_withdrawn_or_superseded_before_final_approval(w, app_role_conn):
    """Case d."""
    h = _unreleasable_hold(w)
    v_a = _procedure_policy(w)
    rb = _request(w, h, v_a)
    HoldRebaseApproval.objects.create(rebase=rb, slot="engineering", approver=w.lead)
    v_b = _procedure_policy(w)  # a newer active production policy supersedes v_a
    _refused(
        app_role_conn,
        _rebase_approve_sql(),
        [rb.pk, "safety", w.safety.pk],
        match=f"rebase {rb.code} refused: policy v{v_a.version} is no longer the active production policy "
        f"(active: v{v_b.version})",
    )
    rb2 = _request(w, h, v_b)
    HoldRebaseApproval.objects.create(rebase=rb2, slot="engineering", approver=w.lead)
    HoldPolicyWithdrawal.objects.create(policy=v_b, withdrawn_by=w.safety, reason="SYNTHETIC")
    _refused(
        app_role_conn,
        _rebase_approve_sql(),
        [rb2.pk, "safety", w.safety.pk],
        match=f"policy v{v_b.version} is no longer the active production policy (active: none)",
    )
    assert not HoldRebaseActivation.objects.exists()


def test_activation_rows_are_trigger_only_and_unique_per_base(w, app_role_conn):
    h = _unreleasable_hold(w)
    target = _procedure_policy(w)
    rb1, rb2 = _request(w, h, target), _request(w, h, target)
    _refused(
        app_role_conn,
        "INSERT INTO fleet_holdrebaseactivation (hold_id, rebase_id, activated_at) VALUES (%s, %s, now())",
        [h.pk, rb1.pk],
        match="permission denied",
    )
    # mutation: with every guard disabled (owner), two activations on the same base still violate uniqueness
    with _conn("owner", autocommit=False) as owner, owner.cursor() as cur:
        cur.execute("ALTER TABLE fleet_holdrebaseactivation DISABLE TRIGGER USER")
        cur.execute(
            "INSERT INTO fleet_holdrebaseactivation (hold_id, rebase_id, activated_at) VALUES (%s, %s, now())",
            [h.pk, rb1.pk],
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO fleet_holdrebaseactivation (hold_id, rebase_id, activated_at) "
                "VALUES (%s, %s, now())",
                [h.pk, rb2.pk],
            )
        owner.rollback()


def _synthetic_policy(w, approve: tuple[str, ...] = ("engineering", "safety")) -> HoldPolicy:
    nxt = HoldPolicy.objects.order_by("-version")[0].version + 1
    p = HoldPolicy.objects.create(version=nxt, rules=DRAFT_POLICY_RULES, created_by=w.eng)
    for slot in approve:
        HoldPolicyApproval.objects.create(
            policy=p, slot=slot, approver=w.lead if slot == "engineering" else w.safety
        )
    return p


PLACE_SQL = (
    "INSERT INTO fleet_hold (code, scope, robot_id, blocking, source_type, source_ref, reason, policy_id, "
    "required_slots, manifest_at_placement_id, placed_by_id, placed_at) "
    "SELECT %s, 'robot', r.id, true, 'manual', '', 'SYNTHETIC', p.id, coalesce(p.rules -> 'manual' -> 'slots', '[]'), "
    "r.current_manifest_id, %s, now() FROM fleet_robot r "
    "LEFT JOIN fleet_holdpolicy p ON p.id = fleet_hold_binding_policy_id() WHERE r.code = 'R-02'"
)
APPROVE_SQL = "INSERT INTO fleet_holdpolicyapproval (policy_id, slot, approver_id, approved_at) VALUES (%s, %s, %s, now())"


def _race(first: list[tuple[str, list[Any]]], second: list[tuple[str, list[Any]]]) -> list[str]:
    """`first` runs and stays uncommitted; `second` runs in a thread and must wait on a lock (observed in
    pg_stat_activity); then `first` commits."""
    c1, c2 = _conn(autocommit=False), _conn(autocommit=False)
    outcome: list[str] = []
    try:
        with c1.cursor() as cur:
            for stmt in first:
                cur.execute(*stmt)

        def run_second() -> None:
            try:
                with c2.cursor() as cur:
                    for stmt in second:
                        cur.execute(*stmt)
                c2.commit()
                outcome.append("ok")
            except psycopg.Error as e:
                c2.rollback()
                outcome.append(str(e))

        t = threading.Thread(target=run_second)
        t.start()
        _wait_for_lock_wait(c2)
        assert t.is_alive()
        c1.commit()
        t.join(15)
        assert outcome, "the second transaction never finished"
    finally:
        c1.close()
        c2.close()
    return outcome


# A direct-SQL placement that resolves its binding AFTER the registry lock (as _create_hold does). Without the
# lock, a binding computed before a concurrent activation commits is refused by fleet_hold_guard (fail closed).
LOCKED_PLACE = [("SELECT fleet_lock_hold_policy_registry(false)", [])]


def _place(code: str, w) -> list[tuple[str, list[Any]]]:
    return [*LOCKED_PLACE, (PLACE_SQL, [code, w.ops.pk])]


@pytest.mark.parametrize("start", ["active_older", "draft_only", "no_policy"])
def test_activation_vs_placement_serialized_by_the_registry(w, start):
    """Case c (P0-3): placement S first keeps the older binding (the activation waits); activation U first gives
    the new one (the placement waits). From an older active policy, from only drafts, and from no policy."""
    if start == "no_policy":
        _owner_rewrite("fleet_holdpolicy", "DELETE FROM fleet_holdpolicy", [])
        create_v1 = (
            "INSERT INTO fleet_holdpolicy (version, rules, source, procedure_ref, created_by_id, created_at) "
            "VALUES (1, %s, 'synthetic_demo', '', %s, now())",
            [Jsonb(DRAFT_POLICY_RULES), w.eng.pk],
        )
        assert _race([(PLACE_SQL, ["HD-C1", w.ops.pk])], [create_v1]) == ["ok"]
        assert Hold.objects.get(code="HD-C1").policy_id is None  # bound to nothing, for life
        expected_after = HoldPolicy.objects.get(version=1).pk  # newest
    else:
        if start == "active_older":
            before = _synthetic_policy(w).pk  # active
            activating = _synthetic_policy(w, approve=("engineering",))
            activate = (APPROVE_SQL, [activating.pk, "safety", w.safety.pk])
        else:
            v1 = HoldPolicy.objects.get(version=1)  # draft by w.lead
            HoldPolicyApproval.objects.create(policy=v1, slot="engineering", approver=w.lead2)
            before = _synthetic_policy(w, approve=()).pk  # a newer draft: the newest, so the binding
            activating = v1
            activate = (APPROVE_SQL, [v1.pk, "safety", w.safety.pk])
        assert _one("SELECT fleet_hold_binding_policy_id()") == before
        assert _race([(PLACE_SQL, ["HD-C1", w.ops.pk])], [activate]) == ["ok"]
        assert Hold.objects.get(code="HD-C1").policy_id == before  # placement first: older binding
        assert _one("SELECT fleet_active_hold_policy_id()") == activating.pk
        expected_after = activating.pk
    assert _one("SELECT fleet_hold_binding_policy_id()") == expected_after
    # activation first: a newer version is activated while the placement waits, which then binds to it
    newer = _synthetic_policy(w, approve=("engineering",))
    out = _race([(APPROVE_SQL, [newer.pk, "safety", w.safety.pk])], _place("HD-C2", w))
    assert out == ["ok"] and Hold.objects.get(code="HD-C2").policy_id == newer.pk
    # and without the registry lock first, a stale binding read before the activation is refused, never wrong
    newest = _synthetic_policy(w, approve=("engineering",))
    out = _race([(APPROVE_SQL, [newest.pk, "safety", w.safety.pk])], [(PLACE_SQL, ["HD-C3", w.ops.pk])])
    assert "a new hold binds to policy" in out[0] and not Hold.objects.filter(code="HD-C3").exists()


def _wait_for_lock_wait(conn: psycopg.Connection, timeout: float = 10.0) -> None:
    pid = conn.info.backend_pid
    with _conn() as obs, obs.cursor() as cur:
        for _ in range(int(timeout / 0.05)):
            cur.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [pid])
            row = cur.fetchone()
            if row and row[0] == "Lock":
                return
            threading.Event().wait(0.05)
    raise AssertionError("the second transaction never waited on a lock")


# ================================================================== G-01 incident holds (EM decision 12, case i)
@pytest.fixture
def ew(transactional_db):
    return build_execution(prefix="s7x_")


def test_incident_hold_release_needs_resolved_or_closed(ew):
    out = inc.report_incident(
        ew.ops,
        robot_code=ew.robot.code,
        event_type="fall",
        description="SYNTHETIC",
        occurred_at=_one("SELECT now()") - timedelta(minutes=1),
        knowledge_source="witnessed",
    )
    i = Incident.objects.get(code=out["entity_id"])
    h = i.hold
    for slot, (who, role) in {
        "eng_review_1": (ew.eng, "engineer"),
        "eng_review_2": (ew.eng2, "engineer"),
        "safety": (ew.safety, "safety_reviewer"),
        "founder": (ew.founder, "founder"),
    }.items():
        fs.approve_release(who, hold_code=h.code, slot=slot, role_used=role)
    for step in ("triaged", "response_in_progress"):
        with pytest.raises(Exception, match=f"incident {i.code} is"):
            fs.release_hold(ew.ops, hold_code=h.code)
        inc.change_incident_status(ew.ops, incident_code=i.code, to_status=step)
    with pytest.raises(Exception, match="is response_in_progress"):
        fs.release_hold(ew.ops, hold_code=h.code)
    inc.change_incident_status(ew.ops, incident_code=i.code, to_status="resolved")
    fs.release_hold(ew.ops, hold_code=h.code)
    assert Hold.objects.get(pk=h.pk).released_at is not None


def test_incident_source_hold_without_incident_row(w):
    # Slice 7 (G-01): the command path can no longer create such a hold ...
    with pytest.raises(Invalid, match="placed only by reporting the incident"):
        fs.place_hold(w.ops, scope="robot", target="R-02", source_type="incident", reason="SYNTHETIC")
    assert not Hold.objects.filter(source_type="incident").exists()
    # ... but rows placed that way before Slice 7 remain: a legacy-shaped row (owner-written test fixture, through
    # the same insert trigger as any hold), bound like any new hold, with no Incident row.
    template = Hold.objects.get(
        code=fs.place_hold(w.ops, scope="robot", target="R-02", source_type="manual", reason="SYNTHETIC")[
            "entity_id"
        ]
    )
    h = Hold.objects.create(
        code="HD-LEG-INC",  # fits Hold.code max_length=12
        scope="robot",
        robot=template.robot,
        source_type="incident",
        source_ref="",
        reason="SYNTHETIC pre-Slice 7 incident hold",
        policy=template.policy,
        required_slots=template.policy.rules["incident"]["slots"] if template.policy else [],
        manifest_at_placement_id=template.robot.current_manifest_id,
        placed_by=w.ops,
    )
    problem = "SELECT fleet_hold_source_problem(h) FROM fleet_hold h WHERE id = %s"
    assert _one(problem, [h.pk]) is None  # demo: a legacy incident hold without an incident stays releasable
    set_setting("demo_mode", "false")
    assert (
        _one(problem, [h.pk]) == "an incident hold with no incident record cannot be released in production"
    )


@pytest.mark.parametrize("policy_state", ["none", "draft_only", "withdrawn"])
@pytest.mark.parametrize("mode", ["true", "false"])
def test_incident_always_recordable(ew, policy_state, mode):
    """Case i / section 0a: no policy, only a draft, after a withdrawal, both modes, during a settings change."""
    if policy_state == "withdrawn":
        p = _procedure_policy(ew)
        HoldPolicyWithdrawal.objects.create(policy=p, withdrawn_by=ew.safety, reason="SYNTHETIC")
    set_setting("demo_mode", mode)
    owner = _conn("owner", autocommit=False)
    try:
        with owner.cursor() as cur:
            if policy_state == "none":
                assert not Hold.objects.exists()
                cur.execute("ALTER TABLE fleet_holdpolicy DISABLE TRIGGER USER")
                cur.execute("DELETE FROM fleet_holdpolicy")
                owner.commit()
                cur.execute("ALTER TABLE fleet_holdpolicy ENABLE TRIGGER USER")
                owner.commit()
            # a settings change in flight: holds the settings lock EXCLUSIVE until we commit
            cur.execute("UPDATE fleet_deploymentsetting SET value = value WHERE key = 'consent_form_version'")
        with connection.cursor() as c:
            c.execute("SET lock_timeout = '2s'")
        out = inc.report_incident(
            ew.ops,
            robot_code=ew.robot.code,
            event_type="fall",
            description="SYNTHETIC",
            occurred_at=_one("SELECT now()") - timedelta(minutes=1),
            knowledge_source="witnessed",
        )
        i = Incident.objects.get(code=out["entity_id"])
        assert i.hold.released_at is None and i.hold.blocking
        if policy_state == "none":
            assert i.hold.policy_id is None and i.hold.required_slots == []
        owner.commit()
    finally:
        owner.close()


# ================================================================== X-4 escalation deadline sets
def _deadlines(scale: list[str], minutes: int = 600) -> dict[str, Any]:
    return {lvl: [{"to_role": "safety_reviewer", "within_min": minutes}] for lvl in [*scale, "unclassified"]}


def test_deadline_set_rules(ew, app_role_conn):
    from tests.operations_world import make_incident_policy  # noqa: PLC0415

    make_incident_policy(ew)
    pid = _one("SELECT execution_active_incident_policy_id()")
    scale = ["low", "medium", "high"]
    ins = (
        "INSERT INTO execution_escalationdeadlineset (incident_policy_id, version_no, deadlines, source, notes, "
        "created_by_id, created_at) VALUES (%s, %s, %s, %s, '', %s, now()) RETURNING id"
    )
    _refused(
        app_role_conn,
        ins,
        [pid, 2, Jsonb(_deadlines(scale)), "synthetic_demo", ew.ops.pk],
        match="consecutive",
    )
    _refused(
        app_role_conn,
        ins,
        [pid, 1, Jsonb(_deadlines(scale)), "synthetic_demo", ew.field.pk],
        match="requires ops_lead",
    )
    bad = _deadlines(scale)
    del bad["unclassified"]
    _refused(
        app_role_conn, ins, [pid, 1, Jsonb(bad), "synthetic_demo", ew.ops.pk], match='plus "unclassified"'
    )
    bad = _deadlines(scale)
    bad["low"] = [{"to_role": "wizard", "within_min": 5}]
    _refused(
        app_role_conn, ins, [pid, 1, Jsonb(bad), "synthetic_demo", ew.ops.pk], match="unknown deadline role"
    )
    bad = _deadlines(scale)
    bad["low"] = [{"to_role": "ops_lead", "within_min": 5}, {"to_role": "ops_lead", "within_min": 9}]
    _refused(app_role_conn, ins, [pid, 1, Jsonb(bad), "synthetic_demo", ew.ops.pk], match="appears twice")
    bad = _deadlines(scale)
    bad["low"] = [{"to_role": "ops_lead", "within_min": 1.5}]
    _refused(app_role_conn, ins, [pid, 1, Jsonb(bad), "synthetic_demo", ew.ops.pk], match="whole number")
    ok = _deadlines(scale)
    ok["low"] = []  # a stated "no clocked notification"
    with app_role_conn.cursor() as cur:
        cur.execute(ins, [pid, 1, Jsonb(ok), "synthetic_demo", ew.ops.pk])
        sid = cur.fetchone()[0]
    appr = (
        "INSERT INTO execution_escalationdeadlinesetapproval (deadline_set_id, slot, approver_id, approved_at) "
        "VALUES (%s, %s, %s, now())"
    )
    _refused(
        app_role_conn,
        appr,
        [sid, "safety", ew.safety.pk],
        match="approving an escalation deadline set depends on the deployment mode",
    )
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY])
        with pytest.raises(CV, match="requires role founder"), app_role_conn.transaction():
            cur.execute(appr, [sid, "founder", ew.safety.pk])
        cur.execute(appr, [sid, "safety", ew.safety.pk])
        cur.execute(appr, [sid, "founder", ew.founder.pk])
    assert _one("SELECT execution_active_deadline_set_id()") == sid
    EscalationDeadlineSetWithdrawal.objects.create(
        deadline_set_id=sid, withdrawn_by=ew.safety, reason="SYNTHETIC"
    )
    assert _one("SELECT execution_active_deadline_set_id()") is None
    # a synthetic set cannot be approved in production
    d2 = EscalationDeadlineSet.objects.create(
        incident_policy_id=pid, version_no=2, deadlines=ok, source="synthetic_demo", created_by=ew.ops
    )
    set_setting("demo_mode", "false")
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [KEY])
        with pytest.raises(CV, match="cannot be approved in production"), app_role_conn.transaction():
            cur.execute(appr, [d2.pk, "safety", ew.safety.pk])
    assert not EscalationDeadlineSetApproval.objects.filter(deadline_set=d2).exists()


# ================================================================== privileges (section 11.2)
NEW_TABLES = [
    "fleet_inspectiontemplateapproval",
    "fleet_inspectiontemplatewithdrawal",
    "fleet_periodicinspectionrule",
    "fleet_periodicinspectionruleapproval",
    "fleet_periodicinspectionrulewithdrawal",
    "fleet_holdpolicywithdrawal",
    "fleet_holdpolicyregistry",
    "fleet_holdrebase",
    "fleet_holdrebaseapproval",
    "fleet_holdrebaseactivation",
    "execution_escalationdeadlineset",
    "execution_escalationdeadlinesetapproval",
    "execution_escalationdeadlinesetwithdrawal",
]


@pytest.mark.parametrize("table", NEW_TABLES)
@pytest.mark.parametrize("stmt", ["UPDATE {t} SET id = id", "DELETE FROM {t}", "TRUNCATE {t}"])
def test_runtime_role_cannot_mutate_new_tables(transactional_db, app_role_conn, table, stmt):
    _refused(app_role_conn, stmt.format(t=table))


def test_registry_is_a_lock_only_row(transactional_db, app_role_conn):
    with app_role_conn.cursor() as cur:
        cur.execute("SELECT id FROM fleet_holdpolicyregistry")
        assert cur.fetchall() == [(1,)]
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM fleet_holdpolicyregistry WHERE id = 1 FOR UPDATE"
        )  # row locks are allowed
    _refused(app_role_conn, "INSERT INTO fleet_holdpolicyregistry (id) VALUES (1)", match="permission denied")
    _refused(app_role_conn, "UPDATE fleet_holdpolicyregistry SET id = 1", match="append-only")


# ================================================================== views
def test_production_blockers_view(w):
    with connection.cursor() as cur:
        cur.execute("SELECT code, entity_ref FROM operations_production_blockers ORDER BY 1, 2")
        rows = set(cur.fetchall())
    assert {
        ("HOLD_POLICY_NOT_APPROVED", "hold-policy"),
        ("INSPECTION_TEMPLATE_NOT_APPROVED", "P-A"),
        ("PERIODIC_INTERVAL_NOT_APPROVED", "P-A"),
        ("ESCALATION_DEADLINES_NOT_APPROVED", "deadline-set"),
    } <= rows
    _production_world(w)
    with connection.cursor() as cur:
        # v2 02C adds MAINTENANCE_RULE_NOT_APPROVED (no maintenance rule in this Slice 7 world); it has its own
        # tests in tests/test_v2_02c_maintenance.py. Every Slice 7 code stays cleared exactly as before.
        cur.execute(
            "SELECT code FROM operations_production_blockers WHERE entity_ref = 'P-A' "
            "AND code <> 'MAINTENANCE_RULE_NOT_APPROVED'"
        )
        assert cur.fetchall() == []


def test_closeout_view_lists_runs(ew):
    with connection.cursor() as cur:
        cur.execute("SELECT run_code, closed, stage, assignment_verified FROM operations_closeout_runs")
        assert cur.fetchall() == [(ew.run.code, False, "departed", False)]


# ================================================================== section 0a / case j: seeds
def test_seeds_create_no_production_approval_and_production_shows_every_p0_blocker(transactional_db):
    set_setting("demo_mode", "false")
    with pytest.raises(Exception, match="runs only in demo mode"):
        call_command("seed_guardrails")
    from fleet.models import DeploymentSetting  # noqa: PLC0415

    DeploymentSetting.objects.filter(key="demo_mode").delete()
    os.environ.setdefault("DEV_SEED_PASSWORD", "dev-only-password-123")
    for cmd in (
        "seed_dev",
        "seed_fleet",
        "seed_planning",
        "seed_guardrails",
        "seed_readiness",
        "seed_execution",
        "seed_operations",
        "seed_guardrails",
    ):
        call_command(cmd)
    assert not HoldPolicy.objects.filter(source="approved_procedure").exists()
    assert not InspectionTemplate.objects.filter(source="engineering_doc").exists()
    assert not InspectionTemplateApproval.objects.exists()
    assert not PeriodicInspectionRule.objects.filter(source="engineering_doc").exists()
    assert not EscalationDeadlineSet.objects.filter(source="owner_approved").exists()
    assert set(
        Qualification.objects.filter(kind="readiness_inspector").values_list("basis_ref", flat=True)
    ) == {"SYNTHETIC-TRAINING"}
    assert PeriodicInspectionRule.objects.filter(source="synthetic_demo", interval_min=4320).count() == 2
    assert (
        _one(
            "SELECT state FROM readiness_periodic_inspection_status((SELECT id FROM fleet_robot WHERE code = "
            "'R-01'), false, clock_timestamp())"
        )
        == "due"
    )
    set_setting("demo_mode", "false")
    with connection.cursor() as cur:
        cur.execute("SELECT DISTINCT code FROM operations_production_blockers")
        codes = {r[0] for r in cur.fetchall()}
    assert {
        "HOLD_POLICY_NOT_APPROVED",
        "HOLD_UNRELEASABLE",
        "INSPECTION_TEMPLATE_NOT_APPROVED",
        "PERIODIC_INTERVAL_NOT_APPROVED",
    } <= codes
    v = Visit.objects.filter(status="confirmed").order_by("id")[0]  # the seeded demo visit (codes not reset)
    assert not evaluate_readiness(v).ready
    with connection.cursor() as cur, pytest.raises(Exception, match="authorization refused"):
        args = _issue_args(v.code)
        args[1] = _one("SELECT id FROM auth_user WHERE username = 'dev_ops_lead'")
        cur.execute("SELECT id FROM readiness_issue_authorization(%s, %s, %s, %s, %s, %s, %s)", args)
    assert ReadinessEvidence.objects.exists()
