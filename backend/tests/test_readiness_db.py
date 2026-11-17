"""Readiness invariants hold against DIRECT SQL from the runtime role (ops_app), independent of the commands.
See docs/03_readiness_contract.md section 3 and migration readiness/0002."""

import itertools
import os
import threading
import time
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.db import connection
from psycopg.types.json import Jsonb

from fleet import services as fleet_services
from fleet.models import Inspection, Robot
from planning.models import Person, Visit
from readiness import evidence as rx
from readiness.evaluator import take_guard_snapshot
from readiness.models import (
    DepartureAuthorization,
    DepartureEvent,
    ReadinessEvidence,
    ReadinessPolicy,
)
from tests.fleet_world import set_setting
from tests.planning_world import new_visit, plan
from tests.readiness_world import REQUIREMENTS, build_readiness, requirements


@pytest.fixture
def w(transactional_db):
    return build_readiness(prefix="rdb_")


_SEQ = itertools.count(1)


def _conn(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=autocommit,
    )


def _ins(cur: psycopg.Cursor, table: str, **cols: Any) -> int:
    names = list(cols)
    vals = [Jsonb(v) if isinstance(v, dict | list) else v for v in cols.values()]
    cur.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) RETURNING id",
        vals,
    )
    return cur.fetchone()[0]


def _ctx(w) -> dict[str, Any]:
    v = Visit.objects.get(code=w.visit)
    rev = v.current_revision
    robot = Robot.objects.get(pk=rev.robot_id)
    policy = ReadinessPolicy.objects.get(version=w.policy_version)
    ev_ids = list(ReadinessEvidence.objects.filter(visit=v).values_list("id", flat=True))
    return {
        "visit": v,
        "rev": rev,
        "robot": robot,
        "manifest_id": robot.current_manifest_id,
        "policy": policy,
        "ev_ids": ev_ids,
    }


def _visit_version(code: str) -> int:
    return Visit.objects.get(code=code).version


# ------------------------------------------------------------------ row builders (valid by default)
def _evidence_row(w, c, **over) -> dict[str, Any]:
    row = {
        "code": f"EV-T{next(_SEQ)}",
        "visit_id": c["visit"].pk,
        "revision_id": c["rev"].pk,
        "robot_id": c["robot"].pk,
        "manifest_id": c["manifest_id"],
        "kind": "battery_check",
        "result": "pass",
        "inspection_id": None,
        "details": {"soc_pct": 70},
        "recorded_by_id": w.ops.pk,
        "recorded_at": "now()",
    }
    row.update(over)
    return row


def _ins_row(cur, table, row) -> int:
    row = dict(row)
    raw = {k: v for k, v in row.items() if v == "now()"}
    for k in raw:
        row.pop(k)
    names = list(row) + list(raw)
    vals = [Jsonb(v) if isinstance(v, dict | list) else v for v in row.values()]
    cur.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES "
        f"({', '.join(['%s'] * len(row) + ['now()'] * len(raw))}) RETURNING id",
        vals,
    )
    return cur.fetchone()[0]


def _snapshot(c, **over) -> dict[str, Any]:
    """The LIVE section 5 snapshot (migration 0003 checks every key), with c's ids and any overrides."""
    s = take_guard_snapshot(c["visit"])
    s.update(
        visit_id=c["visit"].pk,
        revision_id=c["rev"].pk,
        robot_id=c["robot"].pk,
        manifest_id=c["manifest_id"],
        policy_id=c["policy"].pk,
    )
    s.update(over)
    pol = ReadinessPolicy.objects.filter(pk=s["policy_id"]).first()
    if "policy_lock_version" not in over and pol is not None:
        s["policy_lock_version"] = pol.lock_version
    return s


def _auth_row(w, c, expires: str = "clock_timestamp() + interval '30 minutes'", **over) -> dict[str, Any]:
    row = {
        "code": f"DA-T{next(_SEQ)}",
        "visit_id": c["visit"].pk,
        "revision_id": c["rev"].pk,
        "robot_id": c["robot"].pk,
        "manifest_id": c["manifest_id"],
        "policy_id": c["policy"].pk,
        "mode": "demo",
        "guard_snapshot": _snapshot(c),
        "evidence_ids": c["ev_ids"],
        "recovery_plan_id": w.recovery_plan_id,
        "issued_by_id": w.ops.pk,
        "_expires": expires,
    }
    row.update(over)
    return row


def _owner_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_OWNER_USER", "ops_owner"),
        password=os.environ.get("DB_OWNER_PASSWORD", "ops_owner_dev"),
        autocommit=True,
    )


def _ins_auth(cur, row) -> int:
    """Raw authorization INSERT. `cur` is ops_app: since readiness/0005 it has no INSERT privilege at all (only
    readiness_issue_authorization may issue), which is asserted here on every call. The same row is then
    inserted as the schema OWNER, the only identity that can still INSERT (the issue function runs as it), so
    the insert-trigger rules these tests assert are still exercised exactly as before."""
    row = dict(row)
    expires = row.pop("_expires")
    names = list(row)
    vals = [Jsonb(v) if isinstance(v, dict | list) else v for v in row.values()]
    sql = (
        f"INSERT INTO readiness_departureauthorization ({', '.join(names)}, expires_at) "
        f"VALUES ({', '.join(['%s'] * len(names))}, {expires}) RETURNING id"
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute(sql, vals)
    with _owner_conn() as owner, owner.cursor() as ocur:
        ocur.execute(sql, vals)
        return ocur.fetchone()[0]


def _departure(cur, w, auth_id: int, **over) -> int:
    a = DepartureAuthorization.objects.get(pk=auth_id)
    row = {
        "authorization_id": auth_id,
        "visit_id": a.visit_id,
        "robot_id": a.robot_id,
        "recorded_by_id": w.ops.pk,
        "guard_snapshot": a.guard_snapshot,
    }
    row.update(over)
    return _ins(cur, "readiness_departureevent", **row)


def _reschedule(w, code: str, hours: int = 2) -> None:
    v = Visit.objects.get(code=code)
    plan(
        w,
        code,
        on_site_start=v.current_revision.on_site_start + timedelta(hours=hours),
        on_site_end=v.current_revision.on_site_end + timedelta(hours=hours),
    )


def _new_manifest(robot: str = "R-01", w=None) -> None:
    fleet_services.record_manifest(
        w.eng, robot_code=robot, pattern_code="P-A", autonomy_version="a2", safety_config_hash="g" * 64
    )


# ================================================================== policy
def test_policy_versions_must_be_consecutive_and_start_at_lock_version_1(w, app_role_conn):
    nxt = ReadinessPolicy.objects.order_by("-version").first().version + 1
    base = {"requirements": REQUIREMENTS, "source": "synthetic_demo", "notes": "", "created_by_id": w.lead.pk}
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="consecutive"):
            cur.execute(
                "INSERT INTO readiness_readinesspolicy (version, requirements, source, notes, created_by_id, "
                "created_at, lock_version) VALUES (%s, %s, 'synthetic_demo', '', %s, now(), 1)",
                [nxt + 1, Jsonb(REQUIREMENTS), w.lead.pk],
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="lock_version 1"):
            cur.execute(
                "INSERT INTO readiness_readinesspolicy (version, requirements, source, notes, created_by_id, "
                "created_at, lock_version) VALUES (%s, %s, 'synthetic_demo', '', %s, now(), 5)",
                [nxt, Jsonb(REQUIREMENTS), w.lead.pk],
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO readiness_readinesspolicy (version, requirements, source, notes, created_by_id, "
                "created_at, lock_version) VALUES (%s, %s, 'made_up', '', %s, now(), 1)",
                [nxt, Jsonb(REQUIREMENTS), w.lead.pk],
            )
        cur.execute(
            "INSERT INTO readiness_readinesspolicy (version, requirements, source, notes, created_by_id, "
            "created_at, lock_version) VALUES (%s, %s, %s, %s, %s, now(), 1)",
            [nxt, Jsonb(base["requirements"]), base["source"], base["notes"], base["created_by_id"]],
        )


BAD_REQUIREMENTS = [
    pytest.param({**REQUIREMENTS, "extra": 1}, id="unknown-key"),
    pytest.param({k: v for k, v in REQUIREMENTS.items() if k != "evidence"}, id="missing-key"),
    pytest.param(requirements(authorization_ttl_min=0), id="ttl-zero"),
    pytest.param(requirements(authorization_ttl_min=1.5), id="ttl-fraction"),
    pytest.param(requirements(authorization_ttl_min="90"), id="ttl-string"),
    pytest.param(requirements(evidence=[]), id="no-evidence"),
    pytest.param(requirements(evidence=[{"kind": "vibes_check", "max_age_min": 10}]), id="unknown-kind"),
    pytest.param(requirements(evidence=[{"kind": "battery_check", "max_age_min": -5}]), id="age-negative"),
    pytest.param(
        requirements(evidence=[{"kind": "battery_check", "max_age_min": 5, "note": "x"}]),
        id="evidence-extra-key",
    ),
    pytest.param(
        requirements(
            evidence=[
                {"kind": "battery_check", "max_age_min": 5},
                {"kind": "battery_check", "max_age_min": 9},
            ]
        ),
        id="duplicate-kind",
    ),
    pytest.param(requirements(required_equipment=["kit", "kit"]), id="equipment-duplicate"),
    pytest.param(requirements(required_equipment=[" "]), id="equipment-blank"),
    pytest.param(requirements(required_equipment="kit"), id="equipment-not-list"),
    pytest.param(requirements(recovery_plan_required="yes"), id="recovery-not-bool"),
    pytest.param(["not", "an", "object"], id="not-object"),
]


@pytest.mark.parametrize("req", BAD_REQUIREMENTS)
def test_requirements_json_is_validated_by_the_database(w, app_role_conn, req):
    nxt = ReadinessPolicy.objects.order_by("-version").first().version + 1
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="invalid requirements"),
    ):
        cur.execute(
            "INSERT INTO readiness_readinesspolicy (version, requirements, source, notes, created_by_id, "
            "created_at, lock_version) VALUES (%s, %s, 'synthetic_demo', '', %s, now(), 1)",
            [nxt, Jsonb(req), w.lead.pk],
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE readiness_readinesspolicy SET lock_version = lock_version + 1",
        "UPDATE readiness_readinesspolicy SET notes = 'changed'",
        "UPDATE readiness_readinesspolicy SET source = 'approved_procedure'",
        "DELETE FROM readiness_readinesspolicy",
        "TRUNCATE readiness_readinesspolicy CASCADE",
    ],
)
def test_policy_rows_are_immutable(w, app_role_conn, sql):
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(sql)
    assert ReadinessPolicy.objects.get(version=w.policy_version).lock_version == 3  # 1 + two approvals


def test_policy_approval_rules_in_sql(w, app_role_conn):
    draft = ReadinessPolicy.objects.get(version=w.make_policy(approve=False))
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="requires role safety_reviewer"):
            cur.execute(
                "INSERT INTO readiness_readinesspolicyapproval (policy_id, slot, approver_id, approved_at) "
                "VALUES (%s, 'safety', %s, now())",
                [draft.pk, w.lead2.pk],
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot approve it"):
            cur.execute(
                "INSERT INTO readiness_readinesspolicyapproval (policy_id, slot, approver_id, approved_at) "
                "VALUES (%s, 'engineering', %s, now())",
                [draft.pk, w.lead.pk],
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO readiness_readinesspolicyapproval (policy_id, slot, approver_id, approved_at) "
                "VALUES (%s, 'founder', %s, now())",
                [draft.pk, w.founder.pk],
            )
        cur.execute(
            "INSERT INTO readiness_readinesspolicyapproval (policy_id, slot, approver_id, approved_at) "
            "VALUES (%s, 'engineering', %s, now())",
            [draft.pk, w.lead2.pk],
        )
        assert ReadinessPolicy.objects.get(pk=draft.pk).lock_version == 2  # bumped by the approval insert
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO readiness_readinesspolicyapproval (policy_id, slot, approver_id, approved_at) "
                "VALUES (%s, 'engineering', %s, now())",
                [draft.pk, w.lead2.pk],
            )
        cur.execute(
            "INSERT INTO readiness_readinesspolicywithdrawal (policy_id, withdrawn_by_id, reason, withdrawn_at) "
            "VALUES (%s, %s, 'superseded', now())",
            [draft.pk, w.safety.pk],
        )
        assert ReadinessPolicy.objects.get(pk=draft.pk).lock_version == 3  # bumped by the withdrawal insert
        with pytest.raises(
            psycopg.errors.CheckViolation, match="withdrawn readiness policy cannot be approved"
        ):
            cur.execute(
                "INSERT INTO readiness_readinesspolicyapproval (policy_id, slot, approver_id, approved_at) "
                "VALUES (%s, 'safety', %s, now())",
                [draft.pk, w.safety.pk],
            )
        with pytest.raises(psycopg.errors.UniqueViolation):  # one withdrawal per policy
            cur.execute(
                "INSERT INTO readiness_readinesspolicywithdrawal (policy_id, withdrawn_by_id, reason, withdrawn_at) "
                "VALUES (%s, %s, 'again', now())",
                [draft.pk, w.lead.pk],
            )


def test_policy_withdrawal_needs_reason_and_role_in_sql(w, app_role_conn):
    pid = ReadinessPolicy.objects.get(version=w.policy_version).pk
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="needs a reason"):
            cur.execute(
                "INSERT INTO readiness_readinesspolicywithdrawal (policy_id, withdrawn_by_id, reason, withdrawn_at) "
                "VALUES (%s, %s, '  ', now())",
                [pid, w.safety.pk],
            )
        with pytest.raises(
            psycopg.errors.CheckViolation, match="requires engineering_lead or safety_reviewer"
        ):
            cur.execute(
                "INSERT INTO readiness_readinesspolicywithdrawal (policy_id, withdrawn_by_id, reason, withdrawn_at) "
                "VALUES (%s, %s, 'r', now())",
                [pid, w.ops.pk],
            )


# ================================================================== evidence
def test_valid_evidence_insert_bumps_the_visit_version(w, app_role_conn):
    c = _ctx(w)
    before = _visit_version(w.visit)
    with app_role_conn.cursor() as cur:
        _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c))
    assert _visit_version(w.visit) == before + 1


def test_evidence_must_be_bound_to_current_revision_robot_and_manifest(w, app_role_conn):
    c = _ctx(w)
    other_robot = Robot.objects.get(code="R-02")
    _reschedule(w, w.visit)
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="current revision"):
            _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c))  # c["rev"] is now the old one
    c = _ctx(w)
    other_visit = Visit.objects.get(code=new_visit(w, h0=14, h1=15))
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="different visit"):
            _ins_row(
                cur,
                "readiness_readinessevidence",
                _evidence_row(w, c, revision_id=other_visit.current_revision_id),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="robot assigned on the revision"):
            _ins_row(
                cur,
                "readiness_readinessevidence",
                _evidence_row(w, c, robot_id=other_robot.pk, manifest_id=other_robot.current_manifest_id),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="different robot"):
            _ins_row(
                cur,
                "readiness_readinessevidence",
                _evidence_row(w, c, manifest_id=other_robot.current_manifest_id),
            )
    old_manifest = c["manifest_id"]
    _new_manifest("R-01", w)
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="current configuration"):
            _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c, manifest_id=old_manifest))
        _ins_row(
            cur,
            "readiness_readinessevidence",
            _evidence_row(w, c, manifest_id=Robot.objects.get(code="R-01").current_manifest_id),
        )


def test_evidence_inspection_linkage_and_pass_rule(w, app_role_conn):
    c = _ctx(w)
    good = Inspection.objects.get(code=w.inspection)
    incomplete = fleet_services.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="T1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=None,
        visit_ref=w.visit,
    )["entity_id"]
    elsewhere = fleet_services.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="T1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
        visit_ref="V-9999",
    )["entity_id"]
    insp_row = {"kind": "pre_departure_inspection", "details": {}}
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):  # inspection kind without an inspection
            _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c, **insp_row))
        with pytest.raises(psycopg.errors.CheckViolation):  # inspection on another kind
            _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c, inspection_id=good.pk))
        with pytest.raises(psycopg.errors.CheckViolation, match="not recorded for this visit"):
            _ins_row(
                cur,
                "readiness_readinessevidence",
                _evidence_row(w, c, inspection_id=Inspection.objects.get(code=elsewhere).pk, **insp_row),
            )
        inc = Inspection.objects.get(code=incomplete)
        with pytest.raises(psycopg.errors.CheckViolation, match="complete PASS"):
            _ins_row(
                cur, "readiness_readinessevidence", _evidence_row(w, c, inspection_id=inc.pk, **insp_row)
            )
        # a FAIL evidence row may cite an incomplete inspection
        _ins_row(
            cur,
            "readiness_readinessevidence",
            _evidence_row(w, c, inspection_id=inc.pk, result="fail", **insp_row),
        )
        _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c, inspection_id=good.pk, **insp_row))
    _new_manifest("R-01", w)
    c = _ctx(w)
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="different robot or configuration"),
    ):
        _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c, inspection_id=good.pk, **insp_row))


@pytest.mark.parametrize(
    "over, match",
    [
        ({"details": {"soc_pct": 0}}, "1..100"),
        ({"details": {"soc_pct": 101}}, "1..100"),
        ({"details": {"soc_pct": "85"}}, "1..100"),
        ({"details": {"soc_pct": 85, "x": 1}}, "1..100"),
        ({"kind": "equipment_check", "details": {"items": {"charger": "maybe"}}}, "present or missing"),
        ({"kind": "equipment_check", "details": {"items": {}}}, "at least one"),
        ({"kind": "equipment_check", "details": {"charger": "present"}}, "items"),
        ({"kind": "pre_departure_inspection", "details": {"x": 1}}, None),
    ],
)
def test_evidence_details_are_validated(w, app_role_conn, over, match):
    c = _ctx(w)
    if over.get("kind") == "pre_departure_inspection":
        over = {**over, "inspection_id": Inspection.objects.get(code=w.inspection).pk}
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match=match):
        _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c, **over))


def test_evidence_observed_at_window(w, app_role_conn):
    c = _ctx(w)
    with app_role_conn.cursor() as cur:
        for expr, match in (
            ("clock_timestamp() + interval '5 minutes'", "future"),
            ("clock_timestamp() - interval '25 hours'", "24 hours"),
        ):
            cur.execute(f"SELECT {expr}")
            ts = cur.fetchone()[0]
            with pytest.raises(psycopg.errors.CheckViolation, match=match):
                _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c, observed_at=ts))
        cur.execute("SELECT clock_timestamp() - interval '23 hours'")
        _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c, observed_at=cur.fetchone()[0]))


def test_evidence_void_and_recovery_plan_rejected_on_cancelled_visit(w, app_role_conn):
    c = _ctx(w)
    ev = ReadinessEvidence.objects.get(code=w.evidence["battery_check"])
    w.p.cancel_visit(w.ops, visit_code=w.visit, reason="test")
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="cancelled"):
            _ins_row(cur, "readiness_readinessevidence", _evidence_row(w, c))
        with pytest.raises(psycopg.errors.CheckViolation, match="cancelled"):
            cur.execute(
                "INSERT INTO readiness_readinessevidencevoid (evidence_id, voided_by_id, reason, voided_at) "
                "VALUES (%s, %s, 'r', now())",
                [ev.pk, w.ops.pk],
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="cancelled"):
            _ins_row(cur, "readiness_recoveryplan", _plan_row(w, c))


def test_void_needs_reason_bumps_visit_and_is_one_to_one(w, app_role_conn):
    ev = ReadinessEvidence.objects.get(code=w.evidence["battery_check"])
    before = _visit_version(w.visit)
    sql = (
        "INSERT INTO readiness_readinessevidencevoid (evidence_id, voided_by_id, reason, voided_at) "
        "VALUES (%s, %s, %s, now())"
    )
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="reason"):
            cur.execute(sql, [ev.pk, w.ops.pk, ""])
        cur.execute(sql, [ev.pk, w.ops.pk, "wrong robot"])
        assert _visit_version(w.visit) == before + 1
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(sql, [ev.pk, w.ops.pk, "again"])


# ================================================================== recovery plan
def _plan_row(w, c, **over) -> dict[str, Any]:
    row = {
        "visit_id": c["visit"].pk,
        "revision_id": c["rev"].pk,
        "manifest_id": c["manifest_id"],
        "on_call_engineer_id": Person.objects.get(code="P-4").pk,
        "escalation_contact_id": Person.objects.get(code="P-5").pk,
        "escalation_path": "on-call -> lead",
        "required_equipment": ["recovery_kit"],
        "recorded_by_id": w.ops.pk,
        "recorded_at": "now()",
    }
    row.update(over)
    return row


def test_recovery_plan_rules_in_sql(w, app_role_conn):
    c = _ctx(w)
    before = _visit_version(w.visit)
    p4 = Person.objects.get(code="P-4").pk
    with app_role_conn.cursor() as cur:
        _ins_row(cur, "readiness_recoveryplan", _plan_row(w, c))
        assert _visit_version(w.visit) == before + 1
        with pytest.raises(psycopg.errors.CheckViolation, match="must differ"):
            _ins_row(cur, "readiness_recoveryplan", _plan_row(w, c, escalation_contact_id=p4))
        with pytest.raises(psycopg.errors.CheckViolation, match="escalation path"):
            _ins_row(cur, "readiness_recoveryplan", _plan_row(w, c, escalation_path=" "))
        with pytest.raises(psycopg.errors.CheckViolation, match="required_equipment"):
            _ins_row(cur, "readiness_recoveryplan", _plan_row(w, c, required_equipment=["a", "a"]))
        other = Robot.objects.get(code="R-02").current_manifest_id
        with pytest.raises(psycopg.errors.CheckViolation, match="different robot"):
            _ins_row(cur, "readiness_recoveryplan", _plan_row(w, c, manifest_id=other))
    old_manifest = c["manifest_id"]
    _new_manifest("R-01", w)
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="current configuration"),
    ):
        _ins_row(cur, "readiness_recoveryplan", _plan_row(w, c, manifest_id=old_manifest))
    _reschedule(w, w.visit)
    c2 = _ctx(w)
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="current revision"),
    ):
        _ins_row(cur, "readiness_recoveryplan", _plan_row(w, c, manifest_id=c2["manifest_id"]))


# ================================================================== departure authorization
def test_valid_authorization_gets_database_issue_time(w, app_role_conn):
    c = _ctx(w)
    with app_role_conn.cursor() as cur:
        aid = _ins_auth(cur, _auth_row(w, c, issued_at="2001-01-01T00:00:00Z"))
        cur.execute(
            "SELECT issued_at > now() - interval '1 minute' FROM readiness_departureauthorization WHERE id = %s",
            [aid],
        )
        assert cur.fetchone()[0] is True


def test_authorization_policy_and_mode_rules(w, app_role_conn):
    c = _ctx(w)
    draft = ReadinessPolicy.objects.get(version=w.make_policy(approve=False))
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="not approved"):
            _ins_auth(
                cur, _auth_row(w, c, policy_id=draft.pk, guard_snapshot=_snapshot(c, policy_id=draft.pk))
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="mode must be demo"):
            _ins_auth(cur, _auth_row(w, c, mode="production"))
    set_setting("demo_mode", "false")
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="mode must be production"):
            _ins_auth(cur, _auth_row(w, c, mode="demo"))
        with pytest.raises(psycopg.errors.CheckViolation, match="synthetic_demo policy can never authorize"):
            _ins_auth(cur, _auth_row(w, c, mode="production"))
    set_setting("demo_mode", "true")
    procedure = ReadinessPolicy.objects.get(version=w.make_policy(source="approved_procedure"))
    set_setting("demo_mode", "false")
    with app_role_conn.cursor() as cur:  # structurally acceptable: approved procedure in production
        _ins_auth(
            cur,
            _auth_row(
                w,
                c,
                mode="production",
                policy_id=procedure.pk,
                guard_snapshot=_snapshot(c, policy_id=procedure.pk),
            ),
        )
    set_setting("demo_mode", "true")
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="test")
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="withdrawn"):
        _ins_auth(cur, _auth_row(w, c))


def test_authorization_binding_and_expiry_rules(w, app_role_conn):
    c = _ctx(w)
    other = Robot.objects.get(code="R-02")
    voided = ReadinessEvidence.objects.get(code=w.evidence["battery_check"])
    with app_role_conn.cursor() as cur:
        for over, match in (
            ({"_expires": "clock_timestamp() - interval '1 minute'"}, "after issued_at"),
            ({"_expires": "clock_timestamp() + interval '91 minutes'"}, "TTL of 90"),
            ({"recovery_plan_id": None}, "requires a recovery plan"),
            ({"evidence_ids": [10**9]}, "not valid"),
            ({"evidence_ids": ["x"]}, "list of ids"),
            ({"evidence_ids": {"a": 1}}, "must be a list"),
            ({"guard_snapshot": _snapshot(c, visit_id=0)}, "guard snapshot"),
            ({"guard_snapshot": ["x"]}, "guard snapshot"),
            (
                {
                    "robot_id": other.pk,
                    "manifest_id": other.current_manifest_id,
                    "guard_snapshot": _snapshot(c, robot_id=other.pk),
                },
                "robot assigned on the revision",
            ),
            (
                {
                    "manifest_id": other.current_manifest_id,
                    "guard_snapshot": _snapshot(c, manifest_id=other.current_manifest_id),
                },
                "different robot",
            ),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match=match):
                _ins_auth(cur, _auth_row(w, c, **over))
        _ins_auth(cur, _auth_row(w, c, _expires="clock_timestamp() + interval '89 minutes'"))
    rx.void_evidence(w.ops, evidence_code=voided.code, reason="test")
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="not valid"):
        _ins_auth(cur, _auth_row(w, c))
    # Recovery plan for another revision.
    c_old = c
    _reschedule(w, w.visit)
    w.p.confirm_visit(w.ops, visit_code=w.visit)
    c = _ctx(w)
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="current revision"):
            _ins_auth(cur, _auth_row(w, c_old, evidence_ids=[]))
        with pytest.raises(psycopg.errors.CheckViolation, match="recovery plan is for a different"):
            _ins_auth(cur, _auth_row(w, c, evidence_ids=[]))


def test_authorization_requires_confirmed_visit_and_current_manifest(w, app_role_conn):
    c = _ctx(w)
    _new_manifest("R-01", w)
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="current configuration"),
    ):
        _ins_auth(cur, _auth_row(w, c))
    _reschedule(w, w.visit)  # confirmed -> planned
    c = _ctx(w)
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="confirmed visit"):
        _ins_auth(cur, _auth_row(w, c, evidence_ids=[], recovery_plan_id=None))


def test_revocation_needs_reason_and_is_one_to_one(w, app_role_conn):
    c = _ctx(w)
    sql = (
        "INSERT INTO readiness_authorizationrevocation (authorization_id, revoked_by_id, reason, revoked_at) "
        "VALUES (%s, %s, %s, now())"
    )
    with app_role_conn.cursor() as cur:
        aid = _ins_auth(cur, _auth_row(w, c))
        with pytest.raises(psycopg.errors.CheckViolation, match="reason"):
            cur.execute(sql, [aid, w.ops.pk, " "])
        cur.execute(sql, [aid, w.ops.pk, "weather"])
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(sql, [aid, w.ops.pk, "again"])


# ================================================================== departure event
def test_departure_is_single_use_and_gets_database_time(w, app_role_conn):
    c = _ctx(w)
    with app_role_conn.cursor() as cur:
        aid = _ins_auth(cur, _auth_row(w, c))
        eid = _departure(cur, w, aid, departed_at="2001-01-01T00:00:00Z")
        cur.execute(
            "SELECT departed_at > now() - interval '1 minute' FROM readiness_departureevent WHERE id = %s",
            [eid],
        )
        assert cur.fetchone()[0] is True
        with pytest.raises(psycopg.errors.CheckViolation, match="already used"):
            _departure(cur, w, aid)


def test_departure_structural_rejections(w, app_role_conn):
    c = _ctx(w)
    other = Robot.objects.get(code="R-02")
    with app_role_conn.cursor() as cur:
        aid = _ins_auth(cur, _auth_row(w, c))
        with pytest.raises(psycopg.errors.CheckViolation, match="visit and robot"):
            _departure(cur, w, aid, robot_id=other.pk)
        with pytest.raises(psycopg.errors.CheckViolation, match="snapshot differs"):
            _departure(cur, w, aid, guard_snapshot={**_snapshot(c), "visit_version": 999})
        revoked = _ins_auth(cur, _auth_row(w, c))
        cur.execute(
            "INSERT INTO readiness_authorizationrevocation (authorization_id, revoked_by_id, reason, revoked_at) "
            "VALUES (%s, %s, 'r', now())",
            [revoked, w.ops.pk],
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="revoked"):
            _departure(cur, w, revoked)
        short = _ins_auth(cur, _auth_row(w, c, _expires="clock_timestamp() + interval '300 milliseconds'"))
        time.sleep(0.5)
        with pytest.raises(psycopg.errors.CheckViolation, match="expired"):
            _departure(cur, w, short)


def test_departure_rejected_after_policy_withdrawal(w, app_role_conn):
    c = _ctx(w)
    with app_role_conn.cursor() as cur:
        aid = _ins_auth(cur, _auth_row(w, c))
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="test")
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="policy was withdrawn"),
    ):
        _departure(cur, w, aid)


def test_departure_rejected_after_manifest_change(w, app_role_conn):
    c = _ctx(w)
    with app_role_conn.cursor() as cur:
        aid = _ins_auth(cur, _auth_row(w, c))
    _new_manifest("R-01", w)
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="configuration changed"),
    ):
        _departure(cur, w, aid)


def test_departure_rejected_after_replan_and_after_cancel(w, app_role_conn):
    c = _ctx(w)
    with app_role_conn.cursor() as cur:
        replanned = _ins_auth(cur, _auth_row(w, c))
        cancelled = _ins_auth(cur, _auth_row(w, c))
    _reschedule(w, w.visit)
    w.p.confirm_visit(w.ops, visit_code=w.visit)  # confirmed again, but on a new revision
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="re-planned"):
        _departure(cur, w, replanned)
    w.p.cancel_visit(w.ops, visit_code=w.visit, reason="test")
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="no longer confirmed"),
    ):
        _departure(cur, w, cancelled)


def test_two_raw_transactions_cannot_both_depart_on_one_authorization(w, app_role_conn):
    """No application code, no application locks: the unique constraint alone decides."""
    c = _ctx(w)
    with app_role_conn.cursor() as cur:
        aid = _ins_auth(cur, _auth_row(w, c))
    a = DepartureAuthorization.objects.get(pk=aid)
    conns = [_conn(autocommit=False), _conn(autocommit=False)]
    barrier, outcomes = threading.Barrier(2), []

    def depart(conn):
        try:
            with conn.cursor() as cur:
                barrier.wait()
                cur.execute(
                    "INSERT INTO readiness_departureevent (authorization_id, visit_id, robot_id, recorded_by_id, "
                    "guard_snapshot) VALUES (%s, %s, %s, %s, %s)",
                    [aid, a.visit_id, a.robot_id, w.ops.pk, Jsonb(a.guard_snapshot)],
                )
                time.sleep(0.3)  # hold the row uncommitted so the other insert must wait on the unique index
            conn.commit()
            outcomes.append("ok")
        except (psycopg.errors.UniqueViolation, psycopg.errors.CheckViolation):
            conn.rollback()
            outcomes.append("rejected")
        except Exception as e:  # noqa: BLE001 - surface anything unexpected in the assertion
            outcomes.append(repr(e))

    threads = [threading.Thread(target=depart, args=(cn,)) for cn in conns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    for cn in conns:
        cn.close()
    assert sorted(outcomes) == ["ok", "rejected"]
    assert DepartureEvent.objects.filter(authorization_id=aid).count() == 1


# ================================================================== append-only
TABLES = [
    "readiness_readinesspolicyapproval",
    "readiness_readinesspolicywithdrawal",
    "readiness_readinessevidence",
    "readiness_readinessevidencevoid",
    "readiness_recoveryplan",
    "readiness_departureauthorization",
    "readiness_authorizationrevocation",
    "readiness_departureevent",
]


def _fill_every_table(w, cur) -> None:
    c = _ctx(w)
    aid = _ins_auth(cur, _auth_row(w, c))
    second = _ins_auth(cur, _auth_row(w, c))  # issued before departure: after it, no new authorization
    _departure(cur, w, aid)
    cur.execute(
        "INSERT INTO readiness_authorizationrevocation (authorization_id, revoked_by_id, reason, revoked_at) "
        "VALUES (%s, %s, 'r', now())",
        [second, w.ops.pk],
    )
    draft = ReadinessPolicy.objects.get(version=w.make_policy(approve=False))
    cur.execute(
        "INSERT INTO readiness_readinesspolicywithdrawal (policy_id, withdrawn_by_id, reason, withdrawn_at) "
        "VALUES (%s, %s, 'r', now())",
        [draft.pk, w.safety.pk],
    )
    ev = ReadinessEvidence.objects.get(code=w.evidence["equipment_check"])
    cur.execute(
        "INSERT INTO readiness_readinessevidencevoid (evidence_id, voided_by_id, reason, voided_at) "
        "VALUES (%s, %s, 'r', now())",
        [ev.pk, w.ops.pk],
    )


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("op", ["UPDATE {t} SET id = id", "DELETE FROM {t}", "TRUNCATE {t} CASCADE"])
def test_every_readiness_table_is_append_only(w, app_role_conn, table, op):
    with app_role_conn.cursor() as cur:
        _fill_every_table(w, cur)
        cur.execute(f"SELECT count(*) FROM {table}")
        n = cur.fetchone()[0]
        assert n > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(op.format(t=table))
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == n
