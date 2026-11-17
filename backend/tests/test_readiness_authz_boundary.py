"""Authorization write boundary (migration readiness/0005, EM closure condition for Slice 3).

As the runtime role ops_app:
- a raw INSERT into readiness_departureauthorization is refused for lack of privilege, even for a perfect row;
- readiness_issue_authorization() (SECURITY DEFINER) refuses every blocked readiness condition, each time with
  otherwise LIVE, well-formed arguments (current snapshot, latest evidence ids, latest recovery plan), so the
  refusal comes from the gate itself and not from a stale input; the Python evaluator agrees it is blocked;
- it succeeds for the ready world and the result can be used for departure;
- the command path (also run AS ops_app) still authorizes, departs, replays idempotently and audits.

SYNTHETIC data only.
"""

import copy
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from psycopg.types.json import Jsonb

from fleet import services as fs
from fleet.models import Robot
from planning.models import ConsentRecord, Qualification, Visit
from platform_core.commands import Conflict
from platform_core.models import AuditEvent
from readiness import commands as rc
from readiness import evidence as rx
from readiness.evaluator import db_now, evaluate_readiness, take_guard_snapshot
from readiness.models import DepartureAuthorization, DepartureEvent, ReadinessEvidence, RecoveryPlan
from tests.fleet_world import set_setting
from tests.planning_world import plan
from tests.readiness_world import EQUIPMENT_ALL_PRESENT, build_readiness

FN = "readiness_issue_authorization(bigint, bigint, timestamptz, jsonb, bigint, jsonb, text)"
KINDS = ("pre_departure_inspection", "equipment_check", "battery_check")


@pytest.fixture
def w(transactional_db):
    return build_readiness(prefix="rab_")


# ------------------------------------------------------------------ helpers
def _visit(w) -> Visit:
    return Visit.objects.get(code=w.visit)


def live_args(w, **over: Any) -> dict[str, Any]:
    """What a direct-SQL caller would pass after reading the CURRENT rows: live snapshot, latest non-voided
    evidence of each kind and latest recovery plan for the current revision + manifest, mode from settings."""
    v = _visit(w)
    rev = v.current_revision
    mid = Robot.objects.get(pk=rev.robot_id).current_manifest_id if rev and rev.robot_id else None
    ev_ids = []
    for kind in KINDS:
        ev = (
            ReadinessEvidence.objects.filter(
                visit=v, revision=rev, manifest_id=mid, kind=kind, void__isnull=True
            )
            .order_by("-observed_at", "-pk")
            .first()
        )
        if ev is not None:
            ev_ids.append(ev.pk)
    rp = RecoveryPlan.objects.filter(visit=v, revision=rev, manifest_id=mid).order_by("-pk").first()
    args: dict[str, Any] = {
        "visit_id": v.pk,
        "issued_by": w.ops.pk,
        "expires": "clock_timestamp() + interval '30 minutes'",
        "evidence_ids": sorted(ev_ids),
        "recovery_plan_id": rp.pk if rp is not None else None,
        "snapshot": take_guard_snapshot(v),
        "mode": "demo" if fs.demo_mode() else "production",
    }
    args.update(over)
    return args


def issue(cur, args: dict[str, Any]) -> tuple[int, str]:
    cur.execute(
        f"SELECT id, code FROM readiness_issue_authorization(%s, %s, {args['expires']}, %s, %s, %s, %s)",
        [
            args["visit_id"],
            args["issued_by"],
            Jsonb(args["evidence_ids"]),
            args["recovery_plan_id"],
            Jsonb(args["snapshot"]),
            args["mode"],
        ],
    )
    row = cur.fetchone()
    return int(row[0]), str(row[1])


def raw_insert(cur, w, args: dict[str, Any]) -> None:
    v = _visit(w)
    rev = v.current_revision
    robot = Robot.objects.get(pk=rev.robot_id)
    cur.execute(
        "INSERT INTO readiness_departureauthorization (code, visit_id, revision_id, robot_id, manifest_id, "
        "policy_id, mode, guard_snapshot, evidence_ids, recovery_plan_id, issued_by_id, issued_at, expires_at) "
        f"VALUES ('DA-RAW1', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), {args['expires']})",
        [
            v.pk,
            rev.pk,
            robot.pk,
            robot.current_manifest_id,
            args["snapshot"]["policy_id"],
            args["mode"],
            Jsonb(args["snapshot"]),
            Jsonb(args["evidence_ids"]),
            args["recovery_plan_id"],
            args["issued_by"],
        ],
    )


@contextmanager
def as_runtime_role() -> Iterator[None]:
    """Run Django's own connection as ops_app (the production identity) for the duration."""
    saved = {k: connection.settings_dict[k] for k in ("USER", "PASSWORD")}
    connection.close()
    connection.settings_dict.update(
        USER=settings.DB_APP_ROLE, PASSWORD=os.environ.get("DB_APP_PASSWORD", "ops_app_dev")
    )
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == settings.DB_APP_ROLE
        yield
    finally:
        connection.close()
        connection.settings_dict.update(saved)


# ================================================================== 1. no direct INSERT
def test_raw_insert_is_refused_even_for_a_perfect_row(w, app_role_conn):
    args = live_args(w)
    assert evaluate_readiness(_visit(w)).ready  # the world IS ready; the row would pass every trigger
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw_insert(cur, w, args)
    assert not DepartureAuthorization.objects.exists()
    # Control: the same row through the boundary succeeds.
    with app_role_conn.cursor() as cur:
        issue(cur, args)
    assert DepartureAuthorization.objects.count() == 1


# ================================================================== 2. every blocked condition is refused
def _hold(w):
    fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="boundary test")


def _consent(w):
    c = ConsentRecord.objects.get(site__code="S-1", participant_ref="P1")
    w.p.withdraw_consent(w.field, consent_id=c.pk, reason="withdrawn")


def _void(w, kind: str) -> None:
    rx.void_evidence(w.ops, evidence_code=w.evidence[kind], reason="boundary test")


def _missing_inspection(w):
    _void(w, "pre_departure_inspection")


def _failed_equipment(w):
    rx.record_evidence(
        w.field,
        visit_code=w.visit,
        kind="equipment_check",
        result="fail",
        details={"items": {"restraint_kit": "present", "recovery_kit": "present", "charger": "present"}},
    )


def _stale_battery(w):
    rx.record_evidence(
        w.field,
        visit_code=w.visit,
        kind="battery_check",
        result="pass",
        details={"soc_pct": 90},
        observed_at=db_now() - timedelta(minutes=121),
    )
    _void(w, "battery_check")  # the old observation is now the latest one left


def _voided_battery(w):
    _void(w, "battery_check")


def _low_battery(w):
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result="pass", details={"soc_pct": 40}
    )


def _missing_equipment(w):
    items = dict(EQUIPMENT_ALL_PRESENT["items"], charger="missing")
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="equipment_check", result="pass", details={"items": items}
    )


def _withdrawn_policy(w):
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="pulled")


def _production(w):
    set_setting("demo_mode", "false")


def _unconfirmed(w):
    r = _visit(w).current_revision
    plan(
        w,
        w.visit,
        on_site_start=r.on_site_start + timedelta(hours=1),
        on_site_end=r.on_site_end + timedelta(hours=1),
    )  # reschedule: confirmed -> planned


def _revoked_qualification(w):
    q = Qualification.objects.get(person__code="P-2", kind="robot_handler", revoked_at__isnull=True)
    w.p.revoke_qualification(w.ops, qualification_id=q.pk, reason="expired training")


def _out_of_service(w):
    fs.transition_lifecycle(w.lead, robot_code="R-01", to="validation", reason="boundary test")


def _base_auth_revoked(w):
    fs.revoke_base_authorization(w.lead, robot_code="R-01", reason="boundary test")


BLOCKED: list[tuple[Callable[[Any], None], str]] = [
    (_hold, "unreleased blocking hold"),
    (_consent, "adults have valid"),
    (_missing_inspection, "no pre_departure_inspection"),
    (_failed_equipment, "equipment_check EV-.* failed"),
    (_stale_battery, "battery_check EV-.* is stale"),
    (_voided_battery, "no battery_check"),
    (_low_battery, "below the 60% departure minimum"),
    (_missing_equipment, "does not show present: charger"),
    (_withdrawn_policy, "no active readiness policy"),
    (_production, "synthetic_demo policy can never authorize"),
    (_unconfirmed, "only a confirmed visit"),
    (_revoked_qualification, "robot handlers"),
    (_out_of_service, "not in service"),
    (_base_auth_revoked, "base home-use authorization"),
]


@pytest.mark.parametrize("change,match", BLOCKED, ids=[c.__name__.strip("_") for c, _ in BLOCKED])
def test_function_refuses_blocked_readiness_with_live_arguments(w, app_role_conn, change, match):
    change(w)
    assert not evaluate_readiness(_visit(w)).ready  # the authoritative decision says blocked...
    args = live_args(w)  # ...and the direct caller passes the CURRENT, otherwise valid inputs
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match=match):
        issue(cur, args)
    assert not DepartureAuthorization.objects.exists()


def test_no_recovery_plan_is_refused(w, app_role_conn):
    _unconfirmed(w)
    w.p.confirm_visit(w.ops, visit_code=w.visit)  # confirmed again, on a NEW revision with no evidence/plan
    insp = w.record_inspection(w.visit)
    rx.record_evidence(
        w.ops, visit_code=w.visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    rx.record_evidence(
        w.field,
        visit_code=w.visit,
        kind="equipment_check",
        result="pass",
        details=copy.deepcopy(EQUIPMENT_ALL_PRESENT),
    )
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result="pass", details={"soc_pct": 85}
    )
    res = evaluate_readiness(_visit(w))
    assert {b["code"] for b in res.blockers} == {"RECOVERY_PLAN_MISSING"}
    args = live_args(w)
    assert args["recovery_plan_id"] is None
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="no recovery plan"):
            issue(cur, args)
        # The old revision's plan cannot stand in for it.
        with pytest.raises(psycopg.errors.CheckViolation):
            issue(cur, {**args, "recovery_plan_id": w.recovery_plan_id})
    assert not DepartureAuthorization.objects.exists()


def test_already_departed_is_refused(w, app_role_conn):
    a = rc.authorize_departure(w.ops, visit_code=w.visit)
    rc.record_departure(w.ops, authorization_code=a["code"])
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="already departed"),
    ):
        issue(cur, live_args(w))
    assert DepartureAuthorization.objects.count() == 1


def test_forged_and_stale_snapshots_are_refused(w, app_role_conn):
    stale = take_guard_snapshot(_visit(w))
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result="pass", details={"soc_pct": 90}
    )
    args = live_args(w)
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="live guards: visit version"):
            issue(cur, {**args, "snapshot": stale})
        for key, value in (("robot_version", 1), ("site_version", 999), ("person_versions", {})):
            with pytest.raises(psycopg.errors.CheckViolation, match="live guards"):
                issue(cur, {**args, "snapshot": {**args["snapshot"], key: value}})
        with pytest.raises(psycopg.errors.CheckViolation, match="must be an object"):
            issue(cur, {**args, "snapshot": ["x"]})
    assert not DepartureAuthorization.objects.exists()


def test_expiry_bounds_are_enforced(w, app_role_conn):
    args = live_args(w)
    with app_role_conn.cursor() as cur:
        for expires in (
            "clock_timestamp() + interval '91 minutes'",  # beyond the 90 min TTL
            "clock_timestamp() - interval '1 second'",
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="expires_at"):
                issue(cur, {**args, "expires": expires})
    # Evidence age clips it too: the only battery reading left is 100 min old (max age 120 -> 20 min left).
    rx.record_evidence(
        w.field,
        visit_code=w.visit,
        kind="battery_check",
        result="pass",
        details={"soc_pct": 90},
        observed_at=db_now() - timedelta(minutes=100),
    )
    _void(w, "battery_check")
    args = live_args(w)
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="later than allowed"):
            issue(cur, {**args, "expires": "clock_timestamp() + interval '30 minutes'"})
        issue(cur, {**args, "expires": "clock_timestamp() + interval '15 minutes'"})
    assert DepartureAuthorization.objects.count() == 1


@pytest.mark.parametrize("who", ["field", "eng", "lead", "safety", "founder"])
def test_issuer_without_ops_lead_is_refused(w, app_role_conn, who):
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="ops_lead"):
        issue(cur, live_args(w, issued_by=getattr(w, who).pk))
    assert not DepartureAuthorization.objects.exists()


def test_inputs_must_match_the_live_decision(w, app_role_conn):
    args = live_args(w)
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="mode must be demo"):
            issue(cur, {**args, "mode": "production"})
        with pytest.raises(psycopg.errors.CheckViolation, match="exactly the evidence relied on"):
            issue(cur, {**args, "evidence_ids": args["evidence_ids"][:-1]})
        with pytest.raises(psycopg.errors.CheckViolation, match="exactly the evidence relied on"):
            issue(cur, {**args, "evidence_ids": [*args["evidence_ids"], args["evidence_ids"][0]]})
        with pytest.raises(psycopg.errors.CheckViolation, match="no visit"):
            issue(cur, {**args, "visit_id": 10**9})
    assert not DepartureAuthorization.objects.exists()


# ================================================================== 3. ready world
def test_function_succeeds_for_ready_world_and_is_usable_for_departure(w, app_role_conn):
    res = evaluate_readiness(_visit(w))
    assert res.ready
    args = live_args(w)
    assert args["evidence_ids"] == res.evidence_ids and args["recovery_plan_id"] == res.recovery_plan_id
    with app_role_conn.cursor() as cur:
        aid, code = issue(cur, args)
    a = DepartureAuthorization.objects.get(pk=aid)
    assert a.code == code and code.startswith("DA-")
    assert a.issued_by_id == w.ops.pk and a.mode == "demo" and a.policy.version == w.policy_version
    assert a.guard_snapshot == res.guard_snapshot and a.evidence_ids == res.evidence_ids
    assert a.recovery_plan_id == w.recovery_plan_id
    out = rc.record_departure(w.ops, authorization_code=code)
    assert out["authorization"] == code
    assert DepartureEvent.objects.get().authorization_id == aid


# ================================================================== 4. command path, as the runtime role
def test_command_path_as_runtime_role_authorizes_departs_replays_and_audits(w):
    with as_runtime_role():
        first = rc.authorize_departure(w.ops, visit_code=w.visit, idempotency_key="boundary-1")
        again = rc.authorize_departure(w.ops, visit_code=w.visit, idempotency_key="boundary-1")
        assert DepartureAuthorization.objects.count() == 1
        assert first["replayed"] is False and again["replayed"] is True
        assert {**again, "replayed": False} == first
        dep = rc.record_departure(w.ops, authorization_code=first["code"])
    assert dep["authorization"] == first["code"]
    a = DepartureAuthorization.objects.get(code=first["code"])
    assert a.issued_by_id == w.ops.pk and DepartureEvent.objects.get().authorization_id == a.pk
    committed = AuditEvent.objects.filter(command="readiness.authorize_departure", outcome="committed")
    assert committed.count() == 1 and committed.get().entity_id == first["code"]
    assert AuditEvent.objects.filter(command="readiness.record_departure", outcome="committed").count() == 1


def test_command_as_runtime_role_still_refuses_blocked_readiness(w):
    _hold(w)
    with as_runtime_role():
        with pytest.raises(Conflict, match="PLANNING_"):
            rc.authorize_departure(w.ops, visit_code=w.visit)
    assert not DepartureAuthorization.objects.exists()


# ================================================================== 5. security-definer hygiene and grants
def test_function_is_security_definer_with_fixed_search_path_and_runtime_role_owns_nothing(app_role_conn):
    app = settings.DB_APP_ROLE
    with app_role_conn.cursor() as cur:
        cur.execute(
            "SELECT p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner), "
            "has_function_privilege(%s, p.oid, 'EXECUTE'), "
            "coalesce(array_to_string(p.proacl, ','), '') "
            "FROM pg_proc p WHERE p.oid = %s::regprocedure",
            [app, FN],
        )
        secdef, config, owner, can_exec, acl = cur.fetchone()
        assert secdef is True
        assert config == ["search_path=public, pg_temp"]
        assert owner != app
        assert can_exec is True
        assert not any(e.startswith("=") for e in acl.split(",") if e), acl  # no EXECUTE for PUBLIC
        cur.execute(
            "SELECT has_table_privilege(%s, 'readiness_departureauthorization', 'INSERT'), "
            "has_table_privilege(%s, 'readiness_departureauthorization', 'SELECT'), "
            "has_schema_privilege(%s, 'public', 'CREATE')",
            [app, app, app],
        )
        assert cur.fetchone() == (False, True, False)
        cur.execute(
            "SELECT count(*) FROM pg_class WHERE relowner = (SELECT oid FROM pg_roles WHERE rolname = %s)",
            [app],
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM pg_proc WHERE proowner = (SELECT oid FROM pg_roles WHERE rolname = %s)",
            [app],
        )
        assert cur.fetchone()[0] == 0
        # ops_app cannot redefine or re-grant the boundary.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(f"ALTER FUNCTION {FN} SECURITY INVOKER")
        try:  # not the owner, no grant option: an error or a "no privileges were granted" warning
            cur.execute(f"GRANT INSERT ON readiness_departureauthorization TO {app}")
        except psycopg.errors.InsufficientPrivilege:
            pass
        cur.execute("SELECT has_table_privilege(%s, 'readiness_departureauthorization', 'INSERT')", [app])
        assert cur.fetchone()[0] is False
