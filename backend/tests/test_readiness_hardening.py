"""Slice 3 hardening (Engineer E, migration readiness/0003):

1. No policy fallback: the active policy is the highest-version policy that ever became approved; if it is
   withdrawn there is none until a NEWER version is fully approved. Python and SQL agree.
2. Direct-SQL departure bypass: as ops_app, a DepartureEvent with a copied snapshot is refused after any guard
   change, a forged snapshot or an unprivileged recorder is refused, and the trigger's reads are locked so a
   concurrent guard change serializes with the departure.
3. No re-plan after departure (commands and direct SQL).

SYNTHETIC data only.
"""

import os
import threading
import time
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.db import connection
from psycopg.types.json import Jsonb

from fleet import services as fs
from fleet.models import Robot
from planning.models import ConsentRecord, Qualification, Reservation, Visit, VisitRevision
from platform_core.commands import Conflict
from readiness import commands as rc
from readiness import evidence as rx
from readiness.authorization import authorization_status
from readiness.evaluator import db_now, evaluate_readiness, take_guard_snapshot
from readiness.models import (
    DepartureAuthorization,
    DepartureEvent,
    ReadinessEvidence,
    ReadinessPolicy,
)
from tests.fleet_world import set_setting
from tests.planning_world import plan
from tests.readiness_world import build_readiness


@pytest.fixture
def w(transactional_db):
    return build_readiness(prefix="rh_")


# ------------------------------------------------------------------ helpers
def _conn(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=autocommit,
    )


def _owner_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_OWNER_USER", "ops_owner"),
        password=os.environ.get("DB_OWNER_PASSWORD", "ops_owner_dev"),
        autocommit=True,
    )


def _visit(w) -> Visit:
    return Visit.objects.get(code=w.visit)


def authorize(w) -> DepartureAuthorization:
    return DepartureAuthorization.objects.get(code=rc.authorize_departure(w.ops, visit_code=w.visit)["code"])


def raw_depart(
    cur, a: DepartureAuthorization, recorded_by: Any, snapshot: dict[str, Any] | None = None
) -> int:
    """Direct INSERT as ops_app, copying the authorization's snapshot (what a bypass would do)."""
    cur.execute(
        "INSERT INTO readiness_departureevent (authorization_id, visit_id, robot_id, recorded_by_id, "
        "guard_snapshot) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        [
            a.pk,
            a.visit_id,
            a.robot_id,
            recorded_by.pk,
            Jsonb(snapshot if snapshot is not None else a.guard_snapshot),
        ],
    )
    return int(cur.fetchone()[0])


def raw_authorize(cur, w, policy: ReadinessPolicy, snapshot: dict[str, Any] | None = None) -> int:
    """Raw INSERT of an authorization for w.visit on `policy`, with the live snapshot unless given (as the owner,
    after asserting ops_app is refused)."""
    v = _visit(w)
    rev = v.current_revision
    robot = Robot.objects.get(pk=rev.robot_id)
    snap = snapshot
    if snap is None:
        snap = take_guard_snapshot(v)
        snap.update(policy_id=policy.pk, policy_lock_version=policy.lock_version)
    ev_ids = list(
        ReadinessEvidence.objects.filter(visit=v, revision=rev, void__isnull=True).values_list(
            "pk", flat=True
        )
    )
    sql = (
        "INSERT INTO readiness_departureauthorization (code, visit_id, revision_id, robot_id, manifest_id, "
        "policy_id, mode, guard_snapshot, evidence_ids, recovery_plan_id, issued_by_id, issued_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'demo', %s, %s, %s, %s, now(), clock_timestamp() + interval '30 minutes') "
        "RETURNING id"
    )
    vals = [
        f"DA-H{time.monotonic_ns() % 10**8}",
        v.pk,
        rev.pk,
        robot.pk,
        robot.current_manifest_id,
        policy.pk,
        Jsonb(snap),
        Jsonb(ev_ids),
        w.recovery_plan_id,
        w.ops.pk,
    ]
    # readiness/0005: ops_app (`cur`) has no INSERT privilege at all; asserted on every call. The schema owner
    # (the identity readiness_issue_authorization runs as) still meets the insert trigger this test is about.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute(sql, vals)
    with _owner_conn() as owner, owner.cursor() as ocur:
        ocur.execute(sql, vals)
        return int(ocur.fetchone()[0])


def _codes(res) -> set[str]:
    return {b["code"] for b in res.blockers}


def _policy(version: int) -> ReadinessPolicy:
    return ReadinessPolicy.objects.get(version=version)


def _both_agree(expected: int | None) -> None:
    py = rx.current_policy()
    assert (py.version if py else None) == expected
    sql = rx.active_policy_id_in_db()
    assert sql == (py.pk if py else None)


# ================================================================== 1. active policy, no fallback
def test_active_policy_rule_python_and_sql_agree(w):
    v1 = w.policy_version
    _both_agree(v1)
    v2 = w.make_policy(approve=False)
    _both_agree(v1)  # a draft never activates
    rx.approve_readiness_policy(w.lead2, version=v2, slot="engineering")
    _both_agree(v1)  # one slot is not activation
    rx.approve_readiness_policy(w.safety, version=v2, slot="safety")
    _both_agree(v2)  # second slot = explicit activation
    v3 = w.make_policy(approve=False)
    v4 = w.make_policy()
    _both_agree(v4)
    rx.withdraw_readiness_policy(w.safety, version=v4, reason="bad numbers")
    _both_agree(None)  # v1 and v2 are still approved, but never fall back
    rx.approve_readiness_policy(w.lead2, version=v3, slot="engineering")
    rx.approve_readiness_policy(w.safety, version=v3, slot="safety")
    _both_agree(None)  # v3 is older than the withdrawn v4: approving it does not reactivate anything
    v5 = w.make_policy()
    _both_agree(v5)
    rx.withdraw_readiness_policy(w.safety, version=v1, reason="cleanup")  # withdrawing an old one: no effect
    _both_agree(v5)


def test_withdrawn_active_policy_blocks_even_with_older_approved_policy(w, app_role_conn):
    v2 = w.make_policy()
    rx.withdraw_readiness_policy(w.safety, version=v2, reason="procedure error")
    assert rx.policy_status(_policy(w.policy_version)) == "approved"  # older one is still approved...
    res = evaluate_readiness(_visit(w))
    assert not res.ready and _codes(res) == {"POLICY_MISSING"}  # ...but never used
    assert res.policy_id is None and res.guard_snapshot["policy_id"] is None
    with pytest.raises(Conflict, match="POLICY_MISSING"):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    assert not DepartureAuthorization.objects.exists()
    # Direct SQL cannot use the older approved policy either.
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="not the active policy"),
    ):
        raw_authorize(cur, w, _policy(w.policy_version))


def test_replacement_policy_authorizes_only_after_activation_and_fresh_evaluation(w, app_role_conn):
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="retired")
    v2 = w.make_policy(approve=False)
    rx.approve_readiness_policy(w.lead2, version=v2, slot="engineering")
    with pytest.raises(Conflict, match="POLICY_MISSING"):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    stale = take_guard_snapshot(_visit(w))  # before activation: no policy in it
    assert stale["policy_id"] is None
    rx.approve_readiness_policy(w.safety, version=v2, slot="safety")  # activation
    p2 = _policy(v2)
    # A snapshot taken before activation (one approval, lock_version 2) is not the live guard state.
    forged = {**stale, "policy_id": p2.pk, "policy_lock_version": 2}
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="lock version"):
        raw_authorize(cur, w, p2, snapshot=forged)
    res = evaluate_readiness(_visit(w))
    assert res.ready and res.policy_id == p2.pk and res.guard_snapshot["policy_lock_version"] == 3
    a = authorize(w)
    assert a.policy_id == p2.pk and a.guard_snapshot["policy_id"] == p2.pk
    rc.record_departure(w.ops, authorization_code=a.code)
    assert DepartureEvent.objects.filter(authorization=a).exists()


def test_existing_authorization_stays_invalidated_after_replacement(w, app_role_conn):
    a = authorize(w)
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="pulled")
    st = authorization_status(a)
    assert st["state"] == "invalidated" and any("was withdrawn" in r for r in st["reasons"])
    v2 = w.make_policy()  # replacement activated
    st = authorization_status(a)
    assert st["state"] == "invalidated", st
    assert any(f"superseded by v{v2}" in r for r in st["reasons"]), st
    with pytest.raises(Conflict, match="invalidated"):
        rc.record_departure(w.ops, authorization_code=a.code)
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="withdrawn"):
        raw_depart(cur, a, w.ops)
    assert not DepartureEvent.objects.exists()
    fresh = authorize(w)  # a fresh evaluation on v2 is needed, and enough
    assert fresh.policy.version == v2
    rc.record_departure(w.ops, authorization_code=fresh.code)


def test_authorization_on_superseded_then_withdrawn_policy_stays_invalid(w):
    a = authorize(w)
    v2 = w.make_policy()
    rx.withdraw_readiness_policy(w.safety, version=v2, reason="bad")
    st = authorization_status(a)  # v1 is NOT active again; the authorization does not come back to life
    assert st["state"] == "invalidated", st
    assert any("no longer the active policy" in r for r in st["reasons"]), st
    with pytest.raises(Conflict):
        rc.record_departure(w.ops, authorization_code=a.code)


# ================================================================== 2. direct-SQL departure bypass
def _hold(w):
    fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="raw test")


def _consent(w):
    c = ConsentRecord.objects.get(site__code="S-1", participant_ref="P1")
    w.p.withdraw_consent(w.field, consent_id=c.pk, reason="withdrawn")


def _headcount(w):
    w.p.set_expected_adults(w.ops, site_code="S-1", expected_adults=3)


def _qualification(w):
    q = Qualification.objects.get(person__code="P-2", kind="robot_handler", revoked_at__isnull=True)
    w.p.revoke_qualification(w.ops, qualification_id=q.pk, reason="expired training")


def _recovery_person(w):
    w.p.grant_qualification(
        w.ops,
        person_code="P-4",
        kind="field_operator",
        valid_from=db_now(),
        valid_to=db_now() + timedelta(days=1),
    )


def _manifest(w):
    fs.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a9", safety_config_hash="h" * 64
    )


def _reschedule(w):
    r = _visit(w).current_revision
    plan(
        w,
        w.visit,
        on_site_start=r.on_site_start + timedelta(hours=1),
        on_site_end=r.on_site_end + timedelta(hours=1),
    )


def _reschedule_and_confirm(w):
    _reschedule(w)
    w.p.confirm_visit(w.ops, visit_code=w.visit)


def _evidence(w):
    rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result="pass", details={"soc_pct": 90}
    )


def _withdraw_policy(w):
    rx.withdraw_readiness_policy(w.safety, version=w.policy_version, reason="pulled")


def _replacement_policy(w):
    w.make_policy()


def _production(w):
    set_setting("demo_mode", "false")


@pytest.mark.parametrize(
    "change,match",
    [
        (_hold, "robot version"),
        (_consent, "site version"),
        (_headcount, "site version"),
        (_qualification, "person P-2"),
        (_recovery_person, "person P-4"),
        (_manifest, "configuration changed"),
        (_reschedule, "no longer confirmed"),
        (_reschedule_and_confirm, "re-planned"),
        (_evidence, "visit version"),
        (_withdraw_policy, "policy was withdrawn"),
        (_replacement_policy, "active readiness policy is now"),
        (_production, "outside demo mode"),
    ],
)
def test_copied_snapshot_departure_refused_after_guard_change(w, app_role_conn, change, match):
    a = authorize(w)
    change(w)
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match=match):
        raw_depart(cur, a, w.ops)
    assert not DepartureEvent.objects.exists()


def test_untouched_direct_departure_still_succeeds(w, app_role_conn):
    a = authorize(w)
    with app_role_conn.cursor() as cur:
        raw_depart(cur, a, w.field)  # field_operator role
    assert DepartureEvent.objects.get().authorization_id == a.pk
    assert authorization_status(a)["state"] == "used"


def test_forged_snapshots_are_refused(w, app_role_conn):
    a = authorize(w)
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="snapshot differs"):
            raw_depart(cur, a, w.ops, snapshot={**a.guard_snapshot, "robot_version": 1})
        # An authorization row cannot carry a made-up snapshot either.
        live = take_guard_snapshot(_visit(w))
        for key, value in (
            ("robot_version", live["robot_version"] - 1),
            ("site_version", 999),
            ("pattern_version", 999),
            ("visit_version", 999),
            ("person_versions", {}),
            ("vehicle_id", None),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="live guards"):
                raw_authorize(cur, w, a.policy, snapshot={**live, key: value})
        with pytest.raises(psycopg.errors.CheckViolation, match="exactly the guard keys"):
            raw_authorize(cur, w, a.policy, snapshot={k: v for k, v in live.items() if k != "site_version"})
        with pytest.raises(psycopg.errors.CheckViolation, match="exactly the guard keys"):
            raw_authorize(cur, w, a.policy, snapshot={**live, "extra": 1})
    assert not DepartureEvent.objects.exists()


@pytest.mark.parametrize("who", ["eng", "lead", "safety", "founder"])
def test_unprivileged_recorder_is_refused(w, app_role_conn, who):
    a = authorize(w)
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="ops_lead or field_operator"),
    ):
        raw_depart(cur, a, getattr(w, who))
    assert not DepartureEvent.objects.exists()


# ------------------------------------------------------------------ concurrency (raw transactions)
def _raw_hold(cur, w) -> None:
    robot = Robot.objects.get(code="R-01")
    cur.execute(
        "INSERT INTO fleet_hold (code, scope, robot_id, pattern_id, blocking, source_type, source_ref, reason, "
        "policy_id, required_slots, manifest_at_placement_id, placed_by_id, placed_at) "
        "VALUES (%s, 'robot', %s, NULL, true, 'maintenance', '', 'raw concurrent hold', NULL, '[]', %s, %s, now())",
        [f"HD-R{time.monotonic_ns() % 10**6}", robot.pk, robot.current_manifest_id, w.ops.pk],
    )


def _in_thread(fn) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}

    def run():
        try:
            out["result"] = fn()
        except Exception as e:  # noqa: BLE001 - asserted by the caller
            out["error"] = e
        out["done_at"] = time.monotonic()

    t = threading.Thread(target=run)
    t.start()
    return t, out


def test_uncommitted_hold_makes_raw_departure_wait_then_fail(w):
    a = authorize(w)
    holder, departer = _conn(autocommit=False), _conn(autocommit=False)
    try:
        with holder.cursor() as hc:
            _raw_hold(hc, w)  # bumps the robot version, row lock held until commit

            def depart():
                with departer.cursor() as dc:
                    raw_depart(dc, a, w.ops)
                departer.commit()

            t, out = _in_thread(depart)
            time.sleep(0.5)
            assert t.is_alive(), out  # the departure trigger waits on the robot row
            committed_at = time.monotonic()
            holder.commit()
            t.join(10)
        assert isinstance(out.get("error"), psycopg.errors.CheckViolation), out
        assert "robot version" in str(out["error"])
        assert out["done_at"] >= committed_at
        departer.rollback()
    finally:
        holder.close()
        departer.close()
    assert not DepartureEvent.objects.exists()


def test_uncommitted_raw_departure_makes_hold_wait(w):
    a = authorize(w)
    departer, holder = _conn(autocommit=False), _conn(autocommit=False)
    try:
        with departer.cursor() as dc:
            raw_depart(dc, a, w.ops)  # holds the robot (and every guard row) FOR SHARE

            def hold():
                with holder.cursor() as hc:
                    _raw_hold(hc, w)
                holder.commit()

            t, out = _in_thread(hold)
            time.sleep(0.5)
            assert t.is_alive(), out  # the hold's robot-version bump waits for the departure
            committed_at = time.monotonic()
            departer.commit()
            t.join(10)
        assert "error" not in out, out  # holds can always be placed; it just comes after the departure
        assert out["done_at"] >= committed_at
    finally:
        departer.close()
        holder.close()
    assert DepartureEvent.objects.filter(authorization=a).count() == 1


def test_uncommitted_policy_activation_makes_raw_departure_wait_then_fail(w):
    a = authorize(w)
    v2 = w.make_policy(approve=False)
    rx.approve_readiness_policy(w.lead2, version=v2, slot="engineering")
    approver, departer = _conn(autocommit=False), _conn(autocommit=False)
    try:
        with approver.cursor() as pc:
            pc.execute(
                "INSERT INTO readiness_readinesspolicyapproval (policy_id, slot, approver_id, approved_at) "
                "VALUES (%s, 'safety', %s, now())",
                [_policy(v2).pk, w.safety.pk],
            )  # activation: locks + bumps the v2 row, uncommitted

            def depart():
                with departer.cursor() as dc:
                    raw_depart(dc, a, w.ops)
                departer.commit()

            t, out = _in_thread(depart)
            time.sleep(0.5)
            assert t.is_alive(), out  # the departure locks every policy row >= its own
            approver.commit()
            t.join(10)
        assert isinstance(out.get("error"), psycopg.errors.CheckViolation), out
        assert f"active readiness policy is now v{v2}" in str(out["error"])
        departer.rollback()
    finally:
        approver.close()
        departer.close()
    assert not DepartureEvent.objects.exists()


# ------------------------------------------------------------------ writers without their own guard version
def test_raw_reservation_release_and_vehicle_update_bump_the_visit(w, app_role_conn):
    a = authorize(w)
    before = _visit(w).version
    res = Reservation.objects.filter(revision=_visit(w).current_revision, released_at__isnull=True).first()
    with app_role_conn.cursor() as cur:
        cur.execute(
            "UPDATE planning_reservation SET released_at = now(), released_reason = 'raw' WHERE id = %s",
            [res.pk],
        )
    assert _visit(w).version == before + 1
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="visit version"):
        raw_depart(cur, a, w.ops)
    vehicle_id = _visit(w).current_revision.vehicle_id
    with app_role_conn.cursor() as cur:
        cur.execute("UPDATE planning_vehicle SET notes = notes || ' raw' WHERE id = %s", [vehicle_id])
    assert _visit(w).version == before + 2


# ================================================================== 3. no re-plan after departure
def _depart_now(w) -> None:
    a = authorize(w)
    rc.record_departure(w.ops, authorization_code=a.code)


def test_departed_visit_cannot_be_rescheduled_or_cancelled_by_command(w):
    """Slice 4 contract C1 (EM-approved change): a departed visit can no longer be cancelled either."""
    _depart_now(w)
    n = VisitRevision.objects.filter(visit=_visit(w)).count()
    with pytest.raises(Conflict, match="departed"):
        _reschedule(w)
    with pytest.raises(Conflict, match="departed"):
        plan(w, w.visit, robot="R-02")
    assert VisitRevision.objects.filter(visit=_visit(w)).count() == n
    assert _visit(w).status == "confirmed"
    with pytest.raises(Conflict, match="departed"):
        w.p.cancel_visit(w.ops, visit_code=w.visit, reason="returned early")
    assert _visit(w).status == "confirmed"


def _raw_new_revision(cur, rev: VisitRevision) -> None:
    cur.execute(
        "INSERT INTO planning_visitrevision (visit_id, revision_no, on_site_start, on_site_end, travel_out_min, "
        "travel_back_min, tasks, required_consent_scopes, robot_id, lead_id, second_id, vehicle_id, feasibility, "
        "blockers, snapshot, reason, created_by_id, created_at) "
        "SELECT visit_id, revision_no + 1, on_site_start + interval '1 hour', on_site_end + interval '1 hour', "
        "travel_out_min, travel_back_min, tasks, required_consent_scopes, robot_id, lead_id, second_id, vehicle_id, "
        "feasibility, blockers, snapshot, 'raw', created_by_id, now() FROM planning_visitrevision WHERE id = %s",
        [rev.pk],
    )


def test_departed_visit_cannot_be_replanned_by_direct_sql(w, app_role_conn):
    _reschedule_and_confirm(w)  # two revisions, so there is an older one to point back to
    w.record_all_evidence(w.visit)  # evidence + recovery plan for the new revision
    first = VisitRevision.objects.get(visit=_visit(w), revision_no=1)
    with app_role_conn.cursor() as cur:  # control: before departure the raw revision insert is accepted
        other = Visit.objects.get(code=w.new_confirmed_visit(robot="R-02", h0=15, h1=16))
        _raw_new_revision(cur, other.current_revision)
    _depart_now(w)
    v = _visit(w)
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="departed"):
            _raw_new_revision(cur, v.current_revision)
        for sql, args in (
            ("UPDATE planning_visit SET status = 'planned' WHERE id = %s", [v.pk]),
            ("UPDATE planning_visit SET status = 'requested' WHERE id = %s", [v.pk]),
            ("UPDATE planning_visit SET current_revision_id = %s WHERE id = %s", [first.pk, v.pk]),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="departed"):
                cur.execute(sql, args)
    v2 = _visit(w)
    assert (v2.status, v2.current_revision_id) == ("confirmed", v.current_revision_id)


# ------------------------------------------------------------------ one visit, one departure (integration finding)
def test_after_departure_the_visit_cannot_be_authorized_again(w):
    a = authorize(w)
    rc.record_departure(w.ops, authorization_code=a.code)
    r = evaluate_readiness(_visit(w))
    assert not r.ready and "VISIT_ALREADY_DEPARTED" in {x["code"] for x in r.blockers}
    with pytest.raises(Conflict, match="already"):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    assert DepartureAuthorization.objects.count() == 1


def test_second_authorization_cannot_record_a_second_departure_even_by_direct_sql(w, app_role_conn):
    first, second = authorize(w), authorize(w)  # two authorizations issued before anyone departs
    rc.record_departure(w.ops, authorization_code=first.code)
    with pytest.raises(Conflict):
        rc.record_departure(w.ops, authorization_code=second.code)
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.UniqueViolation):
        raw_depart(cur, second, w.ops)
    assert DepartureEvent.objects.filter(visit__code=w.visit).count() == 1


def test_concurrent_departures_on_two_authorizations_record_exactly_one(w):
    first, second = authorize(w), authorize(w)
    barrier, out = threading.Barrier(2), []

    def go(code: str) -> None:
        try:
            barrier.wait()
            out.append(rc.record_departure(w.ops, authorization_code=code))
        except Conflict as e:
            out.append(e)
        finally:
            connection.close()

    ts = [threading.Thread(target=go, args=(c,)) for c in (first.code, second.code)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert sum(isinstance(x, dict) for x in out) == 1, out
    assert DepartureEvent.objects.filter(visit__code=w.visit).count() == 1
