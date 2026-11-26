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
import inspect
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from psycopg.types.json import Jsonb

from fleet import services as fs
from fleet.models import Robot
from planning import services as planning_services
from planning.models import ConsentRecord, Qualification, Reservation, SiteAssessment, Visit
from platform_core.commands import Conflict
from platform_core.models import AuditEvent
from readiness import commands as rc
from readiness import evaluator
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
    (_revoked_qualification, "P-2 is not qualified for robot handling"),
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


# ================================================================== 6. planning gates (migration readiness/0006)
def _supersede_assessment(w, decision="eligible", valid_until=date(2027, 3, 1), excluded=None, approve=True):
    cur = SiteAssessment.objects.filter(site__code="S-1").order_by("-assessed_at", "-pk").first()
    a = w.p.record_assessment(
        w.field,
        site_code="S-1",
        decision=decision,
        valid_until=valid_until,
        restrictions="" if decision == "eligible" else "SYNTHETIC restriction",
        excluded_tasks=excluded or [],
        supersedes_id=cur.pk,
    )
    if approve:
        w.p.approve_assessment(w.ops, assessment_id=int(a["entity_id"]))


def _reservation(w, purpose: str, kind: str, person_code: str | None = None) -> Reservation:
    qs = Reservation.objects.filter(
        revision=_visit(w).current_revision, purpose=purpose, resource__kind=kind, released_at__isnull=True
    )
    if person_code:
        qs = qs.filter(resource__person__code=person_code)
    return qs.get()


def _raw_release(w, purpose: str, kind: str, person_code: str | None = None) -> None:
    res = _reservation(w, purpose, kind, person_code)
    with _app_cursor() as cur:
        cur.execute(
            "UPDATE planning_reservation SET released_at = now(), released_reason = 'raw' WHERE id = %s",
            [res.pk],
        )


@contextmanager
def _app_cursor() -> Iterator[psycopg.Cursor]:
    """A raw ops_app autocommit cursor (direct SQL as the runtime role)."""
    conn = psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=settings.DB_APP_ROLE,
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def _raw_vehicle(w, sql_set: str) -> None:
    with _app_cursor() as cur:
        cur.execute(
            f"UPDATE planning_vehicle SET {sql_set} WHERE id = %s", [_visit(w).current_revision.vehicle_id]
        )


def _timeline(w) -> dict[str, Any]:
    rev = _visit(w).current_revision
    return {
        "insp_start": _reservation(w, "inspection", "robot").starts_at,
        "load_start": _reservation(w, "loading", "robot").starts_at,
        "unload_end": _reservation(w, "unloading", "robot").ends_at,
        "on_site_end": rev.on_site_end,
    }


def _replace_qualification(w, person: str, kind: str, valid_from, valid_to) -> None:
    q = Qualification.objects.get(person__code=person, kind=kind, revoked_at__isnull=True)
    w.p.revoke_qualification(w.ops, qualification_id=q.pk, reason="boundary test")
    w.p.grant_qualification(w.ops, person_code=person, kind=kind, valid_from=valid_from, valid_to=valid_to)


def _assessment_missing(w):
    _supersede_assessment(w, approve=False)  # the old one is superseded, the new one is not approved


def _assessment_expired(w):
    _supersede_assessment(w, valid_until=date(2026, 12, 7))  # the visit is on 2026-12-08 site-local


def _assessment_not_eligible(w):
    _supersede_assessment(w, decision="not_eligible")


def _assessment_restricted(w):
    _supersede_assessment(w, decision="eligible_restricted")


def _assessment_excludes_task(w):
    _supersede_assessment(w, excluded=["T-04"])


def _released_robot_on_site(w):
    _raw_release(w, "on_site", "robot")


def _released_lead_field_shift(w):
    _raw_release(w, "field_shift", "person", "P-1")


def _released_vehicle_trip_reservation(w):
    _raw_release(w, "trip", "vehicle")


def _vehicle_capacity_unknown(w):
    _raw_vehicle(w, "robot_capacity = NULL")


def _vehicle_restraint_missing(w):
    _raw_vehicle(w, "restraint_kit = ''")


def _qualification_expires_mid_activity(w):
    t = _timeline(w)  # valid now and at on_site_end, but not through unloading end
    assert t["on_site_end"] + timedelta(minutes=1) < t["unload_end"]
    _replace_qualification(
        w, "P-2", "field_operator", db_now() - timedelta(days=1), t["on_site_end"] + timedelta(minutes=1)
    )


def _qualification_starts_after_inspection_start(w):
    t = _timeline(w)
    assert t["insp_start"] + timedelta(minutes=1) < t["load_start"]
    _replace_qualification(
        w,
        "P-2",
        "field_operator",
        t["insp_start"] + timedelta(minutes=1),
        t["unload_end"] + timedelta(days=30),
    )


def _robot_handling_starts_after_loading_start(w):
    t = _timeline(w)
    _replace_qualification(
        w,
        "P-1",
        "robot_handler",
        t["load_start"] + timedelta(minutes=1),
        t["unload_end"] + timedelta(days=30),
    )


PLANNING_BLOCKED: list[tuple[Callable[[Any], None], str, str]] = [
    (_assessment_missing, "no approved, current site assessment", "PLANNING_SITE_NOT_ASSESSED"),
    (_assessment_expired, "no approved, current site assessment", "PLANNING_SITE_NOT_ASSESSED"),
    (_assessment_not_eligible, "assessed as not eligible", "PLANNING_SITE_NOT_ELIGIBLE"),
    (_assessment_restricted, "has restrictions", "PLANNING_SITE_RESTRICTED"),
    (_assessment_excludes_task, "tasks excluded at this site: T-04", "PLANNING_TASK_EXCLUDED"),
    (_released_robot_on_site, "were released", "RESERVATIONS_MISSING"),
    (_released_lead_field_shift, "were released", "RESERVATIONS_MISSING"),
    (_released_vehicle_trip_reservation, "were released", "RESERVATIONS_MISSING"),
    (_vehicle_capacity_unknown, "robot capacity is unknown", "PLANNING_VEHICLE_CAPACITY_UNKNOWN"),
    (_vehicle_restraint_missing, "no recorded restraint kit", "PLANNING_VEHICLE_RESTRAINT_UNKNOWN"),
    (
        _qualification_expires_mid_activity,
        "second P-2 is not qualified as field operator",
        "PLANNING_QUALIFICATION",
    ),
    (
        _qualification_starts_after_inspection_start,
        "second P-2 is not qualified as field operator",
        "PLANNING_QUALIFICATION",
    ),
    (
        _robot_handling_starts_after_loading_start,
        "lead P-1 is not qualified for robot handling",
        "PLANNING_QUALIFICATION",
    ),
]


@pytest.mark.parametrize(
    "change,match,code", PLANNING_BLOCKED, ids=[c.__name__.strip("_") for c, _, _ in PLANNING_BLOCKED]
)
def test_function_refuses_planning_gates_with_live_arguments(w, app_role_conn, change, match, code):
    change(w)
    res = evaluate_readiness(_visit(w))
    assert not res.ready and code in {b["code"] for b in res.blockers}, res.blockers  # Python agrees
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match=match):
        issue(cur, live_args(w))
    assert not DepartureAuthorization.objects.exists()


def test_released_vehicle_trip_row_is_refused(w, app_role_conn):
    """Stricter than the evaluator (EM request): a raw release of the vehicle trip row itself, with its 'trip'
    reservation still active. The evaluator's gate 3 checks reservations, not the trip row."""
    with app_role_conn.cursor() as cur:
        cur.execute(
            "UPDATE planning_vehicletrip SET released_at = now() WHERE revision_id = %s",
            [_visit(w).current_revision_id],
        )
        assert cur.rowcount == 1
    res = evaluate_readiness(_visit(w))
    assert res.ready, "documents the evaluator's behaviour; if this changes, update the boundary docstring"
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="no unreleased vehicle trip"),
    ):
        issue(cur, live_args(w))
    with pytest.raises(Conflict, match="no unreleased vehicle trip"):  # the command fails closed on it
        rc.authorize_departure(w.ops, visit_code=w.visit)
    assert not DepartureAuthorization.objects.exists()


# ================================================================== 7. every blocker code is accounted for
GAP = "NOT RE-PROVEN"
META = "NO CONDITION OF ITS OWN"
# evaluator / planning blocker code -> literal text of the SQL refusal that re-proves it (asserted to exist in the
# function definitions below), or (GAP | META, reason). Allowed gaps: non-safety items only.
SQL_COVERAGE: dict[str, Any] = {
    "VISIT_NOT_CONFIRMED": "only a confirmed visit can be authorized",
    "VISIT_ALREADY_DEPARTED": "this visit has already departed",
    "PLANNING_NOT_FEASIBLE": (
        META,
        "fallback when planning is infeasible without a blocker; every PLANNING_ code is mapped",
    ),
    "RESERVATIONS_MISSING": "reservation(s) for the current revision were released",
    "POLICY_MISSING": "there is no active readiness policy",
    "POLICY_NOT_APPROVED_PROCEDURE": "synthetic_demo policy can never authorize departure in production",
    "EVIDENCE_MISSING": "for the current revision and configuration",
    "EVIDENCE_FAILED": "%s %s failed",
    "EVIDENCE_STALE": "is stale (older than",
    "EVIDENCE_WRONG_CONFIGURATION": "for the current revision and configuration",
    "BATTERY_MIN_UNKNOWN": "the departure battery minimum is unknown",
    "BATTERY_LOW": "departure minimum",
    "EQUIPMENT_MISSING": "does not show present",
    "RECOVERY_PLAN_MISSING": "no recovery plan for the current revision and configuration",
    "RECOVERY_ONCALL_NOT_ENGINEER": (GAP, "review item (role of the on-call person), never a blocker"),
    "EXPIRY_NOT_POSITIVE": "expires_at must be in the future",
    "ROBOT_NOT_AT_HUB": "is not at hub",  # Slice 4 C3: execution_robot_departure_problem (table trigger)
    "PLANNING_SITE_NOT_ASSESSED": "no approved, current site assessment covers the visit date",
    "PLANNING_SITE_NOT_ELIGIBLE": "the site was assessed as not eligible",
    "PLANNING_SITE_RESTRICTED": "the site assessment has restrictions",
    "PLANNING_TASK_EXCLUDED": "tasks excluded at this site",
    "PLANNING_CONSENT": "adults have valid",
    "PLANNING_UNKNOWN_TRAVEL": "travel time is unknown",
    "PLANNING_UNKNOWN_INSPECTION_TIME": "pre-visit inspection duration is not configured",
    "PLANNING_ROBOT_MISSING": "the current revision has no robot",
    "PLANNING_ROBOT_NOT_ELIGIBLE": "not in service",
    "PLANNING_UNKNOWN_LOAD_TIME": "loading/unloading time is unknown",
    "PLANNING_UNKNOWN_CHARGE_TIME": "charge turnaround is unknown",
    "PLANNING_CREW_INCOMPLETE": "the revision needs a lead and a second person",
    "PLANNING_CREW_SAME_PERSON": "lead and second must be different people",
    "PLANNING_VEHICLE_MISSING": "no vehicle assigned",
    "PLANNING_VEHICLE_CAPACITY_UNKNOWN": "robot capacity is unknown",
    "PLANNING_VEHICLE_RESTRAINT_UNKNOWN": "has no recorded restraint kit",
    "PLANNING_QUALIFICATION": "is not qualified for robot handling during the activity",
    "PLANNING_CONFLICT": "is not covered by an active reservation",
    # Slice 7 (contract 07a section 11.7; texts as frozen in docs/07b section 8 / issue-function list). The X-1
    # literal is split over two source lines in readiness_issue_authorization ('... which is not approved for '
    # 'pre-departure use in production'), so the contiguous tail is the text matched here.
    "INSPECTION_TEMPLATE_NOT_APPROVED": "pre-departure use in production",
    "POLICY_LACKS_INSPECTION": "does not require pre_departure_inspection",
    "PERIODIC_INSPECTION_INTERVAL_UNKNOWN": "periodic inspection interval unknown for configuration",
    "PERIODIC_INSPECTION_MISSING": "periodic inspection none on the current configuration",
    "PERIODIC_INSPECTION_DUE_BEFORE_RETURN": "not after the planned unloading end",
    "INSPECTOR_NOT_QUALIFIED": "is not a qualified readiness inspector",  # planning_inspection_signer_problem
    # v2 02C "Maintenance up to date" (readiness/0009): the calendar due time against the unloading end
    "MAINTENANCE_DUE_BEFORE_RETURN": "maintenance falls due at",
    # v2 03A-2 (10.A): a robot verified at another hub than the visit's (execution_robot_departure_problem)
    "PLANNING_ROBOT_AT_ANOTHER_HUB": "not at hub %s of this visit",
    # v2 03A-2 section 12: away on an open run (overlapping / another hub's run, or At risk): authorization and
    # departure are refused by the strict physical predicate (execution_robot_departure_problem)
    "PLANNING_ROBOT_AWAY_OVERLAP": "is not at hub (custody: ",
    "PLANNING_ROBOT_AWAY": "is not at hub (custody: ",
    # v2 03B-1: a zoned visit with no matrix value has no recorded minutes; a pause is re-proven by the gate
    "PLANNING_TRAVEL_UNKNOWN": "travel time is unknown",
    "PLANNING_PAUSED": "Paused: cannot leave until lifted",
    "PLANNING_ZONE_RETIRED": (
        GAP,
        "a planning rule for NEW decisions only (a saved plan keeps its recorded minutes); not a departure gate",
    ),
}
ALLOWED_GAPS = {"RECOVERY_ONCALL_NOT_ENGINEER", "PLANNING_ZONE_RETIRED"}


def _planning_codes() -> set[str]:
    src = inspect.getsource(planning_services.evaluate)
    return set(re.findall(r'_add\(\s*b,\s*(?:"\w+"|level),\s*"([A-Z_]+)"', src))


@pytest.mark.django_db
def test_every_blocker_code_is_mapped_to_its_sql_refusal():
    planning = _planning_codes()
    assert {"SITE_NOT_ASSESSED", "CONFLICT", "QUALIFICATION", "VEHICLE_RESTRAINT_UNKNOWN"} <= planning
    expected = set(evaluator.BLOCKER_CODES) | {evaluator.PLANNING_PREFIX + c for c in planning}
    assert set(SQL_COVERAGE) == expected, (
        f"unmapped: {sorted(expected - set(SQL_COVERAGE))}, stale: {sorted(set(SQL_COVERAGE) - expected)}"
    )
    with connection.cursor() as cur:
        cur.execute(
            "SELECT string_agg(pg_get_functiondef(p.oid), E'\n') FROM pg_proc p "
            "WHERE p.proname IN ('readiness_issue_authorization', 'readiness_guard_problem', "
            "'execution_robot_departure_problem', 'planning_inspection_signer_problem', "
            "'operations_pause_gate_problem')"
        )
        source = cur.fetchone()[0]
    for code, target in SQL_COVERAGE.items():
        if isinstance(target, tuple):
            kind, _why = target
            assert kind in (GAP, META), code
            if kind == GAP:
                assert code in ALLOWED_GAPS, f"{code} is a safety gate and must be re-proven in SQL"
            else:
                assert code == evaluator.PLANNING_NOT_FEASIBLE
        else:
            assert target in source, f"{code}: refusal text {target!r} not found in the SQL functions"
