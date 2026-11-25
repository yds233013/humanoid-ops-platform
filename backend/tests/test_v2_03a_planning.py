"""v2 Phase 03A-2 corrective: the FUTURE-PLANNING location predicate (docs/13_v2_phase03_contract.md section 12),
separate from physical dispatch, and the transfer-receipt rulings of section 12.

Proven here (the added 03A exit tests):

* A robot out today (on an open run of the visit's hub) with a non-overlapping visit tomorrow: planning is
  allowed, shown At risk, in the view, Python eligibility, options, the planner and the database triggers.
* A booking that overlaps the run's reserved window: refused (planner and direct SQL as ops_app).
* An overdue return (the run's reserved window ended, robot not verified back) and an unknown return (no reserved
  window): refused, no optimistic availability.
* Still away when authorization is requested: refused (strict physical predicate, unchanged).
* Verified back at the right hub: normal re-evaluation, no At risk.
* In transfer, or at another hub: planning refused.
* A direct-SQL bypass of the planning location rule as ops_app (revision, reservation, confirm): refused.
* A refused receipt never establishes custody or availability.
* An older transfer receipt followed by a newer custody event: the newer custody wins.

The overdue / unknown cases move or remove the run's robot reservations as the schema OWNER with its triggers
disabled (the documented owner trust boundary): no command can produce an overdue run in a test's time frame.

SYNTHETIC data only.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import psycopg
import pytest
from django.db import connection

from execution.models import CustodyEvent
from fleet import services as fs
from fleet import transfers as tr
from fleet.location import AT_RISK, robot_location
from fleet.models import HubTransfer, Robot
from planning import options as popt
from planning import services as ps
from planning.models import Visit
from platform_core.commands import Conflict
from readiness import commands as rc
from readiness.evaluator import ROBOT_NOT_AT_HUB, evaluate_readiness
from tests.execution_world import go_check, raw_incident, to_hub, to_site
from tests.planning_world import DAY, at, plan
from tests.test_v2_03a_transfers import (
    BOOK_SQL,
    _app_conn,
    _eligibility,
    _owner_conn,
    _requested_revision,
    _resource,
    _start,
    _world,
    _xworld,
)

pytestmark = pytest.mark.django_db(transaction=True)
NEXT = DAY + timedelta(days=1)


def _visit(w: SimpleNamespace, day=NEXT, h0: int = 10, hub: str = "HUB-1") -> str:
    return str(
        ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(day, h0), on_site_end=at(day, h0 + 1), travel_out_min=30,
            travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code=hub,
        )["entity_id"]
    )  # fmt: skip


def _blockers(code: str) -> list[dict[str, str]]:
    return list(Visit.objects.get(code=code).current_revision.blockers)  # type: ignore[union-attr]


def _robot_option(code: str, robot: str = "R-01") -> popt.Option:
    return next(o for o in popt.plan_options(Visit.objects.get(code=code)).robots if o.code == robot)


def _shift_run_window(w: SimpleNamespace, *, delta: timedelta | None = None, remove: bool = False) -> None:
    """Owner only, triggers off: move the run's robot reservations by ``delta`` (an overdue run) or remove them
    (an unknown return). No command can do either."""
    with _owner_conn(autocommit=False) as owner, owner.cursor() as cur:
        cur.execute("ALTER TABLE planning_reservation DISABLE TRIGGER USER")
        where = "revision_id = %s AND resource_id = (SELECT id FROM planning_resource WHERE robot_id = %s)"
        if remove:
            cur.execute(f"DELETE FROM planning_reservation WHERE {where}", [w.run.revision_id, w.robot.pk])
        else:
            cur.execute(
                f"UPDATE planning_reservation SET starts_at = starts_at + %s, ends_at = ends_at + %s WHERE {where}",
                [delta, delta, w.run.revision_id, w.robot.pk],
            )
        cur.execute("ALTER TABLE planning_reservation ENABLE TRIGGER USER")
        owner.commit()


# ================================================================== allowed, At risk
def test_out_today_with_a_non_overlapping_visit_tomorrow_is_allowed_and_at_risk(app_role_conn):
    w = _xworld("pa_")
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
    loc = robot_location(w.robot.pk)
    assert (loc.kind, loc.planning_problem, loc.away) == ("site", None, True)
    assert loc.problem == "Not at a hub: at site S-1"  # the physical predicate is unchanged
    # the view and Python eligibility agree: eligible (no location reason)
    assert _eligibility(w.robot.pk) == (True, [])
    assert fs.evaluate_eligibility(w.robot).eligible_for_planning
    code = _visit(w)
    opt = _robot_option(code)
    assert opt.status == popt.FREE and opt.at_risk == AT_RISK and AT_RISK in opt.label
    res = plan(w, code, robot="R-01", lead="P-1", second="P-2", vehicle="VAN-2")
    assert res["feasibility"] == "feasible", _blockers(code)
    risk = [b for b in _blockers(code) if b["code"] == "ROBOT_AWAY"]
    assert risk and risk[0]["level"] == "at_risk" and AT_RISK in risk[0]["message"]
    ps.confirm_visit(w.ops, visit_code=code)  # the database triggers allow it too (reservations, confirm)
    assert Visit.objects.get(code=code).status == "confirmed"


def test_still_away_when_authorization_is_requested_is_refused(app_role_conn):
    w = _xworld("pz_")
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
    code = _visit(w)
    plan(w, code, robot="R-01", lead="P-1", second="P-2", vehicle="VAN-2")
    ps.confirm_visit(w.ops, visit_code=code)
    assert ROBOT_NOT_AT_HUB in {b["code"] for b in evaluate_readiness(Visit.objects.get(code=code)).blockers}
    with pytest.raises(Conflict):
        rc.authorize_departure(w.ops, visit_code=code)
    with connection.cursor() as cur:
        cur.execute("SELECT execution_robot_departure_problem(%s, %s)", [w.robot.pk, w.hub_a.pk])
        assert "open run" in cur.fetchone()[0]


def test_verified_back_at_the_hub_is_planned_normally(app_role_conn):
    w = _xworld("pv_")
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
        go_check(cur, w)
        to_hub(cur, w)
    loc = robot_location(w.robot.pk)
    assert (loc.kind, loc.hub_id, loc.verified, loc.away) == ("hub", w.hub_a.pk, True, False)
    code = _visit(w)
    assert not _robot_option(code).at_risk
    assert plan(w, code, robot="R-01", lead="P-1", second="P-2", vehicle="VAN-2")["feasibility"] == "feasible"
    assert not [b for b in _blockers(code) if b["code"].startswith("ROBOT_AWAY")]


# ================================================================== refused
def test_a_booking_overlapping_the_runs_reserved_window_is_refused(app_role_conn):
    w = _xworld("po_")
    code = _visit(w, day=DAY, h0=10)  # the run's own day and hours
    assert _robot_option(code).status != popt.FREE
    res = plan(w, code, robot="R-01", lead="P-3", second="P-2", vehicle="VAN-2")
    assert res["feasibility"] == "blocked"
    assert "ROBOT_AWAY_OVERLAP" in {b["code"] for b in _blockers(code)}
    # direct SQL as ops_app: a reservation inside the window is refused by the planning trigger
    lo = w.run.revision.on_site_start
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="overlaps"):
        cur.execute(
            "INSERT INTO planning_reservation (resource_id, revision_id, purpose, starts_at, ends_at, "
            "released_reason) VALUES (%s, %s, 'on_site', %s, %s, '')",
            [_resource(w.robot), _requested_revision(w), lo, lo + timedelta(minutes=30)],
        )


@pytest.mark.parametrize("case", ["overdue", "unknown"])
def test_an_overdue_or_unknown_return_is_refused(case, app_role_conn):
    w = _xworld(f"p{case[0]}_")
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
    if case == "overdue":
        _shift_run_window(
            w, delta=-(w.run.revision.on_site_end - w.run.revision.on_site_start) - timedelta(days=60)
        )
        reason = "overdue since"
    else:
        _shift_run_window(w, remove=True)
        reason = "its return cannot be determined"
    loc = robot_location(w.robot.pk)
    assert loc.planning_problem and reason in loc.planning_problem and not loc.away
    eligible, reasons = _eligibility(w.robot.pk)
    assert not eligible and reasons[-1].startswith(f"Away on run {w.run.code}") and reason in reasons[-1]
    assert fs.evaluate_eligibility(w.robot).reasons == reasons
    code = _visit(w, day=NEXT + timedelta(days=30))  # far from any window: still no optimistic availability
    assert _robot_option(code).status == popt.UNUSABLE
    assert plan(w, code, robot="R-01", lead="P-1", second="P-2", vehicle="VAN-2")["feasibility"] == "blocked"
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match=reason):
        cur.execute(BOOK_SQL, [_resource(w.robot), _requested_revision(w)])


def test_in_transfer_or_at_another_hub_planning_is_refused(app_role_conn):
    w = _world("pt_")
    tid = _start(w)  # R-02: HUB-1 -> HB
    for hub in ("HUB-1", "HB"):
        code = _visit(w, hub=hub)
        assert _robot_option(code, "R-02").status == popt.UNUSABLE
        with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="In transfer"):
            cur.execute(BOOK_SQL, [_resource(Robot.objects.get(code="R-02")), _requested_revision(w, hub)])
    tr.receive_transfer(w.tech_b, transfer_id=tid)
    code = _visit(w)  # a HUB-1 visit; R-02 is verified at HB now
    assert _robot_option(code, "R-02").note == "at another hub"
    with pytest.raises(Conflict, match="another hub"):
        plan(w, code, robot="R-02", vehicle="VAN-2")
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="not at hub HUB-1"),
    ):
        cur.execute(BOOK_SQL, [_resource(Robot.objects.get(code="R-02")), _requested_revision(w)])


def test_a_direct_sql_bypass_of_the_planning_location_rule_is_refused(app_role_conn):
    """As ops_app: a non-blocked revision, a reservation and a confirmation for a robot whose return is overdue
    are each refused by their trigger; nothing is written."""
    w = _xworld("pb_")
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
    code = _visit(w)
    assert plan(w, code, robot="R-01", lead="P-1", second="P-2", vehicle="VAN-2")["feasibility"] == "feasible"
    v = Visit.objects.get(code=code)
    _shift_run_window(w, delta=-timedelta(days=60))  # now the return is overdue
    rev = v.current_revision
    assert rev is not None
    before = (Visit.objects.get(pk=v.pk).status, v.revisions.count())
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="overdue"):
            cur.execute(
                "INSERT INTO planning_visitrevision (visit_id, revision_no, on_site_start, on_site_end, "
                "travel_out_min, travel_back_min, tasks, required_consent_scopes, robot_id, lead_id, second_id, "
                "vehicle_id, feasibility, blockers, snapshot, reason, created_by_id, created_at) "
                "SELECT visit_id, revision_no + 1, on_site_start, on_site_end, travel_out_min, travel_back_min, "
                "tasks, required_consent_scopes, robot_id, lead_id, second_id, vehicle_id, 'feasible', '[]', '{}', "
                "'bypass', created_by_id, now() FROM planning_visitrevision WHERE id = %s",
                [rev.pk],
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="overdue"):
            cur.execute(BOOK_SQL, [_resource(w.robot), rev.pk])
        with pytest.raises(psycopg.errors.CheckViolation, match="overdue"):
            cur.execute("UPDATE planning_visit SET status = 'confirmed' WHERE id = %s", [v.pk])
    assert (Visit.objects.get(pk=v.pk).status, v.revisions.count()) == before


# ================================================================== section 12: transfer receipts
def test_a_refused_receipt_never_makes_the_robot_available():
    w = _world("pr_")
    r2 = Robot.objects.get(code="R-02")
    tid = _start(w)
    tr.refuse_transfer(w.tech_b, transfer_id=tid, reason="SYN: no space")
    r2.refresh_from_db()
    loc = robot_location(r2.pk)
    assert r2.hub_id == w.hub_a.pk  # still owned by the source: nothing completed
    assert (loc.kind, loc.verified, loc.away) == (
        "transfer_in_transit",
        False,
        False,
    ) and loc.planning_problem
    assert not HubTransfer.objects.filter(transfer_id=tid, state="received").exists()
    assert not _eligibility(r2.pk)[0]
    for hub in ("HUB-1", "HB"):
        code = _visit(w, hub=hub)
        assert _robot_option(code, "R-02").status == popt.UNUSABLE
    with connection.cursor() as cur:
        cur.execute("SELECT execution_robot_departure_problem(%s, NULL)", [r2.pk])
        assert "in transfer" in cur.fetchone()[0]


def test_an_older_receipt_never_overrides_newer_custody_evidence():
    w = _xworld("pn_")
    r2 = Robot.objects.get(code="R-02")
    tid = _start(w)
    tr.receive_transfer(w.tech_b, transfer_id=tid)
    loc = robot_location(r2.pk)
    assert (loc.kind, loc.hub_id, loc.verified) == ("hub", w.hub_b.pk, True)  # the receipt is the latest fact
    receipt = HubTransfer.objects.get(transfer_id=tid, state="received")
    # newer custody evidence: the robot is reported unaccounted (no run)
    with _app_conn() as conn:
        raw_incident(conn, w, unaccounted=True, robot_id=r2.pk)
    newer = CustodyEvent.objects.filter(robot=r2).order_by("-seq").first()
    assert newer is not None and newer.recorded_at > receipt.recorded_at
    loc = robot_location(r2.pk)
    assert (loc.kind, loc.hub_id) == ("unaccounted", None)  # the newer custody wins, not the older receipt
    assert not _eligibility(r2.pk)[0]
