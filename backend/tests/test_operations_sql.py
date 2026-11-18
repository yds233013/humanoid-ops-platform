"""Slice 5 SQL read models (Engineer A): oracle equality with the Python derivations over seeded random worlds
and a hand-written edge-case table, status-label precedence, custody_seq_at_event, privileges, no locks, and
EXPLAIN on the scale fixture. docs/05_operations_command_center_contract.md sections 2, 4, 5, 6.1, 6.7.
SYNTHETIC data only."""

import json
import os
import threading
import time
from collections import defaultdict
from typing import Any

import psycopg
import pytest
from django.db import connection, transaction
from django.db.models import Q

from execution import policy as ipol
from execution.custody import current_custody, open_run_id
from execution.incidents import incident_status, latest_severity, session_state
from execution.models import (
    CustodyEvent,
    ExecutionRun,
    Incident,
    IncidentEscalation,
    IncidentPolicy,
    TripEvent,
)
from execution.reconciliation import latest_reconciliation
from fleet import services as fs
from fleet.models import Hold, Inspection, Robot
from operations.snapshot import read_snapshot
from planning.models import Visit
from tests import operations_world as ow

VIEWS = [
    "operations_robot_eligibility",
    "operations_robot_status",
    "operations_open_run_stage",
    "operations_incident_queue",
]
COVERAGE: dict[str, set[Any]] = defaultdict(set)


# ================================================================== Python oracle (independent of the SQL)
def db_now():
    with connection.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


def py_awaiting(run: ExecutionRun, custody_kind: str, custody_event_id: int | None) -> str:
    if custody_kind != "hub":
        return "none"
    hub = CustodyEvent.objects.get(pk=custody_event_id)
    if not Inspection.objects.filter(
        robot_id=run.robot_id, visit_ref=run.visit.code, submitted_at__gt=hub.recorded_at
    ).exists():
        return "post_visit_inspection"
    rec = latest_reconciliation(run.pk)
    if rec is None or rec.outcome != "reconciled":
        return "reconciliation"
    return "trip_closure"


def py_open_runs() -> dict[int, dict[str, Any]]:
    out = {}
    for run in ExecutionRun.objects.select_related("visit").filter(closure__isnull=True):
        c = current_custody(run.robot_id)
        kinds = list(
            TripEvent.objects.filter(trip_id=run.trip_id).order_by("seq").values_list("kind", flat=True)
        )
        awaiting = py_awaiting(run, c.kind, c.custody_event_id)
        last = kinds[-1] if kinds else None
        if last is None:
            stage = "departed"
        elif last == "arrived_site":
            stage = "at_site"
        elif last == "departed_site":
            stage = "returning"
        else:
            stage = {"reconciliation": "awaiting_reconciliation", "trip_closure": "awaiting_closure"}.get(
                awaiting, "at_hub"
            )
        rec = latest_reconciliation(run.pk)
        out[run.pk] = {
            "run_code": run.code,
            "robot_id": run.robot_id,
            "visit_code": run.visit.code,
            "last_trip_event": last,
            "session_state": session_state(run.pk),
            "custody_kind": c.kind,
            "custody_verified": c.verified,
            "latest_reconciliation_outcome": rec.outcome if rec else None,
            "awaiting": awaiting,
            "stage": stage,
        }
    return out


def py_eligibility() -> dict[int, dict[str, Any]]:
    out = {}
    for robot in Robot.objects.all():
        e = fs.evaluate_eligibility(robot)
        out[robot.pk] = {
            "eligible": e.eligible_for_planning,
            "reasons": e.reasons,
            "pattern_code": e.pattern_code,
        }
    return out


def py_status() -> dict[int, dict[str, Any]]:
    now = db_now()
    runs = py_open_runs()
    out = {}
    for robot in Robot.objects.exclude(lifecycle="retired"):
        e = fs.evaluate_eligibility(robot)
        c = current_custody(robot.pk)
        orun = open_run_id(robot.pk)
        pid = robot.current_manifest.pattern_id if robot.current_manifest_id else None
        holds = list(
            Hold.objects.filter(blocking=True, released_at__isnull=True)
            .filter(Q(scope="robot", robot=robot) | Q(scope="manifest_pattern", pattern_id=pid))
            .order_by("id")
            .values_list("code", flat=True)
        )
        nv = (
            Visit.objects.filter(
                status="confirmed",
                current_revision__robot=robot,
                current_revision__on_site_end__gt=now,
                departures__isnull=True,
            )
            .order_by("current_revision__on_site_start", "id")
            .first()
        )
        awaiting = runs[orun]["awaiting"] if orun else "none"
        if c.kind == "unaccounted":
            label = "Unaccounted"
        elif c.kind == "unknown":
            label = "Custody unknown"
        elif orun and c.kind in ("vehicle", "site"):
            label = "Deployed"
        elif orun and c.kind == "hub":
            label = (
                "Returned - awaiting "
                + {
                    "post_visit_inspection": "inspection",
                    "reconciliation": "reconciliation",
                    "trip_closure": "trip closure",
                }[awaiting]
            )
        elif c.kind != "hub":
            label = "Custody unknown"
        elif holds:
            label = "Held"
        elif nv is not None:
            label = "Assigned"
        elif not e.eligible_for_planning:
            label = "At hub - not eligible"
        else:
            label = "At hub - eligible for planning"
        out[robot.pk] = {
            "robot_code": robot.code,
            "custody_kind": c.kind,
            "custody_verified": c.verified,
            "custody_event_id": c.custody_event_id,
            "open_run_id": orun,
            "hold_codes": holds,
            "eligible": e.eligible_for_planning,
            "reasons": e.reasons,
            "next_visit_code": nv.code if nv else None,
            "awaiting": awaiting,
            "status_label": label,
        }
    return out


def py_incident_queue() -> dict[int, dict[str, Any]]:
    out = {}
    for i in Incident.objects.select_related("hold"):
        status = incident_status(i)
        if status == "closed":
            continue
        sev = latest_severity(i)
        rank = ipol.level_rank(IncidentPolicy.objects.get(pk=sev.policy_id), sev.level) if sev else None
        unacked = list(
            IncidentEscalation.objects.filter(incident=i, ack__isnull=True).order_by("-recorded_at", "-id")
        )
        if unacked:
            owner = unacked[0].to_role
        else:
            owner = "ops_lead" if status == "reported" else "safety_reviewer"
        out[i.pk] = {
            "incident_code": i.code,
            "status": status,
            "severity_level": sev.level if sev else None,
            "severity_rank": rank,
            "unclassified": sev is None,
            "hold_active": i.hold.released_at is None,
            "unacknowledged_count": len(unacked),
            "unacknowledged_ids": sorted(e.pk for e in unacked),
            "suggested_owner_role": owner,
            "suggested_owner_person_id": unacked[0].to_person_id if unacked else None,
        }
    return out


def sql_rows(view: str, key: str, cols: list[str]) -> dict[int, dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(f"SELECT {key}, {', '.join(cols)} FROM {view}")
        return {r[0]: dict(zip(cols, r[1:], strict=True)) for r in cur.fetchall()}


def assert_oracle_equal(context: str = "") -> dict[str, Any]:
    """Every view equals the Python derivation, row for row and column for column."""
    elig = py_eligibility()
    assert (
        sql_rows("operations_robot_eligibility", "robot_id", ["eligible", "reasons", "pattern_code"]) == elig
    ), context
    status = py_status()
    assert (
        sql_rows("operations_robot_status", "robot_id", list(next(iter(status.values())).keys())) == status
    ), context
    runs = py_open_runs()
    if runs:
        cols = list(next(iter(runs.values())).keys())
        assert sql_rows("operations_open_run_stage", "run_id", cols) == runs, context
    else:
        assert sql_rows("operations_open_run_stage", "run_id", ["stage"]) == {}, context
    queue = py_incident_queue()
    if queue:
        cols = list(next(iter(queue.values())).keys())
        assert sql_rows("operations_incident_queue", "incident_id", cols) == queue, context
    else:
        assert sql_rows("operations_incident_queue", "incident_id", ["status"]) == {}, context
    for e in elig.values():
        COVERAGE["reason"].update(_reason_kind(r) for r in e["reasons"])
        COVERAGE["eligible"].add(e["eligible"])
    for s in status.values():
        COVERAGE["label"].add(s["status_label"])
        COVERAGE["custody"].add(s["custody_kind"])
    for r in runs.values():
        COVERAGE["stage"].add(r["stage"])
        COVERAGE["session"].add(r["session_state"])
    for q in queue.values():
        COVERAGE["incident_status"].add(q["status"])
        COVERAGE["owner"].add(q["suggested_owner_role"])
        COVERAGE["severity"].add(q["severity_level"])
    return {"eligibility": elig, "status": status, "runs": runs, "queue": queue}


def _reason_kind(reason: str) -> str:
    for prefix in (
        "Lifecycle is",
        "No configuration manifest",
        "Configuration hold",
        "Configuration",
        "No base home-use",
        "No approved capability profile",
        "Capability unknown",
        "Capability profile is synthetic",
        "Robot hold",
    ):
        if reason.startswith(prefix):
            return prefix
    return reason


# ================================================================== oracle: >= 50 seeded random worlds
SEEDS = list(range(int(os.environ.get("OPS_ORACLE_SEEDS", "50"))))


@pytest.mark.django_db
@pytest.mark.parametrize("seed", SEEDS)
def test_views_equal_python_on_random_world(seed):
    w = ow.random_world(seed)
    assert_oracle_equal(f"seed {seed}: " + "; ".join(w.story))
    COVERAGE["seeds"].add(seed)


def test_random_worlds_cover_the_variety():
    """Runs after the seeded worlds (file order): the random worlds really exercised every branch."""
    if COVERAGE["seeds"] != set(SEEDS) or len(SEEDS) < 50:
        pytest.skip("only meaningful after all >= 50 seeded worlds ran in this session")
    assert COVERAGE["eligible"] == {True, False}
    assert {
        "Lifecycle is",
        "Configuration",
        "No base home-use",
        "No approved capability profile",
        "Capability unknown",
        "Capability profile is synthetic",
        "Configuration hold",
        "Robot hold",
    } <= COVERAGE["reason"], COVERAGE["reason"]
    assert {
        "Unaccounted",
        "Deployed",
        "Returned - awaiting inspection",
        "Returned - awaiting reconciliation",
        "Returned - awaiting trip closure",
        "Held",
        "Assigned",
        "At hub - not eligible",
        "At hub - eligible for planning",
    } <= COVERAGE["label"], COVERAGE["label"]
    assert {
        "departed",
        "at_site",
        "returning",
        "at_hub",
        "awaiting_reconciliation",
        "awaiting_closure",
    } <= COVERAGE["stage"], COVERAGE["stage"]
    assert {"reported", "triaged"} <= COVERAGE["incident_status"]
    assert {"ops_lead", "safety_reviewer", "engineering_lead"} <= COVERAGE["owner"]
    assert None in COVERAGE["severity"] and len(COVERAGE["severity"]) >= 3


# ================================================================== oracle: hand-written edge cases
@pytest.mark.django_db
@pytest.mark.parametrize("name,build,expected", ow.EDGE_CASES, ids=[c[0] for c in ow.EDGE_CASES])
def test_edge_case_eligibility(name, build, expected):
    w = ow.edge_world()
    code = build(w)
    want = expected(w) if callable(expected) else expected
    robot = Robot.objects.get(code=code)
    py = fs.evaluate_eligibility(robot)
    assert py.reasons == want, "the hand-written expectation must match the Python rule first"
    with connection.cursor() as cur:
        cur.execute(
            "SELECT eligible, reasons FROM operations_robot_eligibility WHERE robot_id = %s", [robot.pk]
        )
        assert cur.fetchone() == (not want, want)
    assert_oracle_equal(name)


def test_edge_cases_cover_every_reason_and_mode():
    names = {c[0] for c in ow.EDGE_CASES}
    assert {
        "lifecycle_commissioning",
        "lifecycle_validation",
        "lifecycle_retired",
        "no_manifest",
        "pattern_no_decision",
        "pattern_engineering_only",
        "pattern_withdrawn",
        "base_authorization_revoked",
        "no_profile_at_all",
        "unknown_stop_behaviour",
        "unknown_lift_crew",
        "unknown_max_runtime",
        "unknown_charge_turnaround",
        "synthetic_profile_production",
        "synthetic_profile_mode_missing",
        "pattern_hold",
        *[f"robot_hold_{s}" for s in ow.HOLD_SOURCES],
    } <= names


# ================================================================== status label precedence (section 2)
def status_of(code: str) -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute("SELECT * FROM operations_robot_status WHERE robot_code = %s", [code])
        cols = [d.name for d in cur.description]
        row = cur.fetchone()
    return dict(zip(cols, row, strict=True)) if row else {}


def label(code: str) -> str:
    return status_of(code)["status_label"]


@pytest.mark.django_db
def test_held_beats_assigned_and_assigned_shows_eligibility():
    w = ow.ops_world("sp_", robots=2, incident_policy=False)
    assert label("R-01") == "Assigned"  # build_readiness confirmed w.visit for R-01
    s = status_of("R-01")
    assert s["next_visit_code"] == w.visit and s["eligible"] is True and s["status_rule"] == 6
    fs.revoke_base_authorization(w.lead, robot_code="R-01", reason="SYNTHETIC")
    s = status_of("R-01")
    assert s["status_label"] == "Assigned" and s["eligible"] is False  # an assigned robot can be ineligible
    assert s["reasons"] == ["No base home-use authorization for configuration P-A"]
    hold = ow.place_hold(w, "robot", "R-01")
    s = status_of("R-01")
    assert (s["status_label"], s["status_rule"], s["hold_codes"]) == ("Held", 5, [hold])
    assert s["next_visit_code"] == w.visit  # the HELD_ROBOT_ASSIGNED input stays visible
    ow.release(w, hold)
    assert label("R-01") == "Assigned"
    pattern_hold = ow.place_hold(w, "manifest_pattern", "P-A")
    assert label("R-01") == "Held" and label("R-02") == "Held"
    assert status_of("R-02")["hold_codes"] == [pattern_hold]
    assert_oracle_equal()


@pytest.mark.django_db
def test_at_hub_labels_and_retired_robot_absent():
    w = ow.ops_world("sh_", robots=2, incident_policy=False)
    assert label("R-02") == "At hub - eligible for planning"
    ow.add_robot(w, "R-05", lifecycle="commissioning")
    assert label("R-05") == "At hub - not eligible"
    ow.add_robot(w, "R-06", lifecycle="retired")
    assert status_of("R-06") == {}
    with connection.cursor() as cur:
        cur.execute("SELECT eligible FROM operations_robot_eligibility WHERE robot_code = 'R-06'")
        assert cur.fetchone() == (False,)
    ow.place_hold(w, "robot", "R-02", blocking=False)
    assert label("R-02") == "At hub - eligible for planning"  # a non-blocking hold does not hold
    assert_oracle_equal()


@pytest.mark.django_db
def test_returned_awaiting_inspection_reconciliation_trip_closure_then_at_hub():
    w = ow.ops_world("sr_", robots=2, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    assert (label("R-02"), status_of("R-02")["open_run_stage"]) == ("Deployed", "departed")
    ow.advance(w, run, "at_site")
    assert (label("R-02"), status_of("R-02")["custody_kind"]) == ("Deployed", "site")
    w.cmd.move(run, "vehicle")
    w.cmd.trip(run, "departed_site")
    assert status_of("R-02")["open_run_stage"] == "returning"
    w.cmd.trip(run, "arrived_hub")
    s = status_of("R-02")
    assert (s["status_label"], s["open_run_stage"], s["awaiting"]) == ("Deployed", "at_hub", "none")
    w.cmd.move(run, "hub", receiver="P-3")
    s = status_of("R-02")
    assert (s["status_label"], s["status_rule"], s["awaiting"], s["open_run_stage"]) == (
        "Returned - awaiting inspection",
        4,
        "post_visit_inspection",
        "at_hub",
    )
    insp = w.cmd.inspect(run)
    w.cmd.reconcile(run, inspection=insp, discrepancies=["SYNTHETIC: strap frayed"])
    s = status_of("R-02")
    assert (s["status_label"], s["awaiting"], s["open_run_stage"]) == (
        "Returned - awaiting reconciliation",
        "reconciliation",
        "awaiting_reconciliation",
    )
    w.cmd.reconcile(run, inspection=insp)
    s = status_of("R-02")
    assert (s["status_label"], s["awaiting"], s["open_run_stage"]) == (
        "Returned - awaiting trip closure",
        "trip_closure",
        "awaiting_closure",
    )
    hold = ow.place_hold(w, "robot", "R-02")
    assert label("R-02") == "Returned - awaiting trip closure"  # rule 4 precedes Held
    assert_oracle_equal()
    w.cmd.close(run)
    s = status_of("R-02")
    assert (s["status_label"], s["open_run_id"], s["awaiting"]) == ("Held", None, "none")
    ow.release(w, hold)
    assert label("R-02") == "At hub - eligible for planning"
    assert_oracle_equal()


@pytest.mark.django_db
def test_unaccounted_beats_everything_and_recovery():
    w = ow.ops_world("su_", robots=2)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "verified")
    ow.report(w, "R-02", event="robot_unaccounted")
    s = status_of("R-02")
    assert (s["status_label"], s["status_rule"], s["custody_kind"]) == ("Unaccounted", 1, "unaccounted")
    assert len(s["hold_codes"]) == 1 and s["open_run_id"] == run.pk
    w.cmd.trip(run, "arrived_hub")  # 0004: the vehicle returns without the robot
    assert status_of("R-02")["open_run_stage"] == "at_hub"
    w.cmd.move(run, "hub", note="SYNTHETIC: found and received")  # recovered on the open run
    assert label("R-02") == "Returned - awaiting inspection"
    assert_oracle_equal()


@pytest.mark.django_db
def test_custody_unknown_is_its_own_label():
    """Unknown custody cannot be produced through the triggers; simulate a broken history as the schema owner
    (inside the test transaction only) to prove rule 2 and the oracle."""
    w = ow.ops_world("sk_", robots=2, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # no pending FK events, so the owner may ALTER the table
        cur.execute("ALTER TABLE execution_custodyevent DISABLE TRIGGER USER")
        cur.execute(
            "INSERT INTO execution_custodyevent (robot_id, seq, from_kind, to_kind, run_id, trip_id, visit_id,"
            " custodian_id, basis, note, recorded_by_id, recorded_at) SELECT robot_id, 5, 'site', 'hub', run_id,"
            " trip_id, visit_id, custodian_id, 'handoff', 'SYNTHETIC gap', recorded_by_id, now()"
            " FROM execution_custodyevent WHERE run_id = %s",
            [run.pk],
        )
        cur.execute("ALTER TABLE execution_custodyevent ENABLE TRIGGER USER")
    s = status_of("R-02")
    assert (s["status_label"], s["status_rule"], s["custody_kind"]) == ("Custody unknown", 2, "unknown")
    assert_oracle_equal()


@pytest.mark.django_db
def test_next_visit_is_the_earliest_confirmed_undeparted_one():
    from datetime import timedelta

    from tests.planning_world import DAY
    from tests.readiness_world import new_confirmed_visit

    w = ow.ops_world("sn_", robots=2, incident_policy=False)
    later = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=9))
    earlier = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=5))
    assert status_of("R-02")["next_visit_code"] == earlier
    w.p.cancel_visit(w.ops, visit_code=earlier, reason="SYNTHETIC")
    assert status_of("R-02")["next_visit_code"] == later
    assert status_of("R-01")["next_visit_code"] == w.visit
    run = ow.depart_new(w, "R-01")  # w.visit stays confirmed but departing another visit does not hide it
    assert status_of("R-01")["next_visit_code"] == w.visit and status_of("R-01")["open_run_id"] == run.pk
    assert_oracle_equal()


@pytest.mark.django_db
def test_incident_queue_rank_owner_and_closed_excluded():
    w = ow.ops_world("sq_", robots=2)
    a = ow.report(w, "R-01")
    b = ow.report(w, "R-02")
    q = sql_rows(
        "operations_incident_queue",
        "incident_code",
        ["status", "severity_rank", "unclassified", "suggested_owner_role", "hold_active"],
    )
    assert q[a] == {
        "status": "reported",
        "severity_rank": None,
        "unclassified": True,
        "suggested_owner_role": "ops_lead",
        "hold_active": True,
    }
    from execution import escalation as esc
    from execution import incidents as inc

    inc.decide_severity(w.lead, incident_code=b, level="high", reason="SYN")
    inc.change_incident_status(w.safety, incident_code=b, to_status="triaged")
    q = sql_rows(
        "operations_incident_queue",
        "incident_code",
        ["severity_level", "severity_rank", "suggested_owner_role"],
    )
    assert q[b] == {"severity_level": "high", "severity_rank": 3, "suggested_owner_role": "safety_reviewer"}
    e1 = esc.escalate_incident(w.ops, incident_code=b, to_role="founder", requested_action="SYN")
    e2 = esc.escalate_incident(
        w.ops, incident_code=b, to_role="engineering_lead", requested_action="SYN", to_person_code="P-5"
    )
    q = sql_rows(
        "operations_incident_queue",
        "incident_code",
        ["unacknowledged_count", "unacknowledged_ids", "suggested_owner_role", "suggested_owner_person_code"],
    )
    assert q[b] == {
        "unacknowledged_count": 2,
        "unacknowledged_ids": sorted([e1["id"], e2["id"]]),
        "suggested_owner_role": "engineering_lead",
        "suggested_owner_person_code": "P-5",
    }
    esc.acknowledge_escalation(w.lead2, escalation_id=e2["id"])
    q = sql_rows("operations_incident_queue", "incident_code", ["suggested_owner_role", "unacknowledged_ids"])
    assert q[b] == {"suggested_owner_role": "founder", "unacknowledged_ids": [e1["id"]]}
    esc.acknowledge_escalation(w.founder, escalation_id=e1["id"])
    for to in ["response_in_progress", "resolved", "closed"]:
        inc.change_incident_status(w.safety, incident_code=b, to_status=to)
    assert set(sql_rows("operations_incident_queue", "incident_code", ["status"])) == {a}
    assert_oracle_equal()


# ================================================================== custody_seq_at_event (section 4)
def arrival_seq(run: ExecutionRun, kind: str = "arrived_hub") -> int | None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT custody_seq_at_event FROM execution_tripevent WHERE run_id = %s AND kind = %s",
            [run.pk, kind],
        )
        return cur.fetchone()[0]


def custody_at(run: ExecutionRun, seq: int) -> str:
    return CustodyEvent.objects.get(robot_id=run.robot_id, seq=seq).to_kind


@pytest.mark.django_db
def test_custody_seq_with_robot_and_hub_receipt_in_the_same_transaction():
    w = ow.ops_world("cs_", robots=2, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "at_site")
    assert custody_at(run, arrival_seq(run, "arrived_site")) == "vehicle"  # the verified departure assignment
    w.cmd.move(run, "vehicle")
    w.cmd.trip(run, "departed_site")
    with transaction.atomic():  # arrival and hub receipt in ONE transaction
        w.cmd.trip(run, "arrived_hub")
        w.cmd.move(run, "hub", receiver="P-3")
    seq = arrival_seq(run)
    ev = CustodyEvent.objects.get(robot_id=run.robot_id, seq=seq)
    assert (ev.to_kind, ev.trip_id) == ("vehicle", run.trip_id)  # with_robot
    assert CustodyEvent.objects.filter(robot_id=run.robot_id).order_by("-seq").first().seq == seq + 1


@pytest.mark.django_db
def test_custody_seq_after_unaccounted_and_with_robot_at_site():
    w = ow.ops_world("cu_", robots=3, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "verified")
    ow.report(w, "R-02", event="robot_unaccounted")
    w.cmd.trip(run, "arrived_hub")
    assert custody_at(run, arrival_seq(run)) == "unaccounted"
    w.cmd.move(run, "hub", note="SYNTHETIC: found")  # a later recovery never changes the arrival's seq
    assert custody_at(run, arrival_seq(run)) == "unaccounted"

    run3 = ow.depart_new(w, "R-03")
    ow.advance(w, run3, "at_site")
    icode = ow.report(w, "R-03", event="other")
    w.cmd.disposition(run3, "robot_left_on_site", incident=icode)
    w.cmd.trip(run3, "departed_site")
    w.cmd.trip(run3, "arrived_hub")
    assert custody_at(run3, arrival_seq(run3)) == "site"


@pytest.mark.django_db(transaction=True)
def test_custody_seq_hub_receipt_in_a_later_transaction_and_caller_value_ignored(app_role_conn):
    w = ow.ops_world("ct_", robots=2, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "arrived_hub")  # each command commits on its own
    before = arrival_seq(run)
    w.cmd.move(run, "hub", receiver="P-3")
    assert arrival_seq(run) == before and custody_at(run, before) == "vehicle"
    # a caller-supplied value is overwritten by the trigger (direct ops_app insert on a fresh run)
    run2 = ow.depart_new(w, "R-01")
    w.cmd.verify(run2)
    with app_role_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO execution_tripevent (trip_id, run_id, seq, kind, recorded_by_id, recorded_at,"
            " custody_seq_at_event) VALUES (%s, %s, 1, 'arrived_site', %s, now(), 999) RETURNING custody_seq_at_event",
            [run2.trip_id, run2.pk, w.field.pk],
        )
        assert cur.fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "UPDATE execution_tripevent SET custody_seq_at_event = 2 WHERE run_id = %s", [run2.pk]
            )


@pytest.mark.django_db
def test_custody_seq_is_immutable_and_legacy_rows_are_null():
    w = ow.ops_world("cl_", robots=2, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "arrived_site")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT is_nullable, data_type FROM information_schema.columns WHERE table_name ="
            " 'execution_tripevent' AND column_name = 'custody_seq_at_event'"
        )
        assert cur.fetchone() == ("YES", "integer")
        with pytest.raises(Exception, match="execution_tripevent is append-only"), transaction.atomic():
            cur.execute(
                "UPDATE execution_tripevent SET custody_seq_at_event = NULL WHERE run_id = %s", [run.pk]
            )
        # A row recorded before the migration (simulated: trigger off, owner only, test transaction only) is NULL.
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE execution_tripevent DISABLE TRIGGER execution_trip_event_guard")
        cur.execute(
            "INSERT INTO execution_tripevent (trip_id, run_id, seq, kind, recorded_by_id, recorded_at)"
            " VALUES (%s, %s, 2, 'departed_site', %s, now()) RETURNING custody_seq_at_event",
            [run.trip_id, run.pk, w.field.pk],
        )
        assert cur.fetchone()[0] is None
        cur.execute("ALTER TABLE execution_tripevent ENABLE TRIGGER execution_trip_event_guard")


def _app_conn(**kw: Any) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        **kw,
    )


@pytest.mark.django_db(transaction=True)
def test_trip_event_trigger_locks_the_robot_before_the_run(app_role_conn):
    """While another transaction holds the robot, a trip-event insert waits on the ROBOT and has not yet locked
    the run (global order robot -> run): a third session can still lock the run NOWAIT."""
    w = ow.ops_world("lk_", robots=2, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    w.cmd.verify(run)
    holder = _app_conn()
    inserter = _app_conn(autocommit=True)
    result: dict[str, Any] = {}
    try:
        holder.execute("SELECT 1 FROM fleet_robot WHERE id = %s FOR UPDATE", [run.robot_id])

        def insert():
            try:
                inserter.execute(
                    "INSERT INTO execution_tripevent (trip_id, run_id, seq, kind, recorded_by_id, recorded_at)"
                    " VALUES (%s, %s, 1, 'arrived_site', %s, now())",
                    [run.trip_id, run.pk, w.field.pk],
                )
                result["ok"] = True
            except Exception as exc:  # pragma: no cover - reported below
                result["error"] = exc

        t = threading.Thread(target=insert)
        t.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # wait until the inserter is blocked on a lock
            with app_role_conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    " AND query LIKE 'INSERT INTO execution_tripevent%%'"
                )
                if cur.fetchone()[0]:
                    break
            time.sleep(0.05)
        with app_role_conn.transaction(), app_role_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM execution_executionrun WHERE id = %s FOR UPDATE NOWAIT", [run.pk])
        assert "ok" not in result
        holder.rollback()
        t.join(10)
        assert result.get("ok") is True, result
    finally:
        holder.close()
        inserter.close()


# ================================================================== privileges (section 5, R5)
@pytest.mark.django_db(transaction=True)
def test_ops_app_can_only_select_the_views(app_role_conn):
    ow.ops_world("pv_", robots=2, incident_policy=False)
    with app_role_conn.cursor() as cur:
        for v in VIEWS:
            cur.execute(f"SELECT count(*) FROM {v}")
            assert cur.fetchone()[0] >= 0
            for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                cur.execute("SELECT has_table_privilege(current_user, %s, %s)", [v, priv])
                assert cur.fetchone() == (False,), (v, priv)
            cur.execute("SELECT has_table_privilege(current_user, %s, 'SELECT')", [v])
            assert cur.fetchone() == (True,)
        for sql in (
            "INSERT INTO operations_robot_eligibility (robot_id) VALUES (1)",
            "UPDATE operations_robot_status SET robot_code = 'X'",
            "DELETE FROM operations_incident_queue",
            "DELETE FROM operations_open_run_stage",
        ):
            # refused: no privilege, and the multi-table views are not updatable either
            with (
                pytest.raises(
                    (psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectNotInPrerequisiteState)
                ),
                app_role_conn.transaction(),
            ):
                cur.execute(sql)


@pytest.mark.django_db
def test_views_are_owner_owned_security_invoker_without_rules_or_triggers():
    with connection.cursor() as cur:
        for v in VIEWS:
            cur.execute(
                "SELECT c.relkind, pg_get_userbyid(c.relowner) = current_user, c.reloptions FROM pg_class c"
                " WHERE c.relname = %s AND c.relnamespace = 'public'::regnamespace",
                [v],
            )
            kind, owned, opts = cur.fetchone()
            assert (kind, owned) == ("v", True) and "security_invoker=true" in (opts or []), v
            cur.execute("SELECT count(*) FROM pg_rules WHERE tablename = %s", [v])
            assert cur.fetchone() == (0,)
            cur.execute("SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass", [v])
            assert cur.fetchone() == (0,)
            cur.execute("SELECT has_table_privilege('public', %s, 'SELECT')", [v])
            assert cur.fetchone() == (False,)
        # Every function the views call is read-only (STABLE/IMMUTABLE) and not SECURITY DEFINER.
        cur.execute(
            """
            WITH RECURSIVE deps(oid) AS (
              SELECT DISTINCT d.refobjid FROM pg_depend d JOIN pg_rewrite rw ON rw.oid = d.objid
              WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_proc'::regclass
                AND rw.ev_class IN (SELECT oid FROM pg_class WHERE relname = ANY(%s)))
            SELECT p.proname, p.provolatile, p.prosecdef FROM deps JOIN pg_proc p ON p.oid = deps.oid
            WHERE p.pronamespace = 'public'::regnamespace
            """,
            [VIEWS],
        )
        funcs = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert {
        "execution_current_custody",
        "execution_open_run_id",
        "execution_session_state",
        "execution_incident_status",
        "execution_level_rank",
        "execution_demo_mode",
    } <= set(funcs)
    assert all(vol in ("s", "i") and not secdef for vol, secdef in funcs.values()), funcs


# ================================================================== no locks, no blocking (sections 0.3, 6.7)
@pytest.mark.django_db(transaction=True)
def test_views_take_no_row_locks_and_never_wait_for_a_writer(app_role_conn):
    w = ow.ops_world("nl_", robots=2)
    run = ow.depart_new(w, "R-02")
    ow.report(w, "R-01")
    writer = _app_conn()
    try:  # a writer holds the robots, the run and the incident FOR UPDATE (as the commands do)
        writer.execute("SELECT 1 FROM fleet_robot ORDER BY id FOR UPDATE")
        writer.execute("SELECT 1 FROM execution_executionrun WHERE id = %s FOR UPDATE", [run.pk])
        writer.execute("SELECT 1 FROM execution_incident FOR UPDATE")
        writer.execute("SELECT 1 FROM fleet_hold FOR UPDATE")
        with app_role_conn.transaction(), app_role_conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("SET LOCAL lock_timeout = '500ms'")
            for v in VIEWS:
                cur.execute(f"SELECT * FROM {v}")
                assert cur.fetchall() or v == "operations_incident_queue"
            cur.execute(
                "SELECT locktype, mode FROM pg_locks WHERE pid = pg_backend_pid()"
                " AND NOT (locktype = 'relation' AND mode = 'AccessShareLock') AND locktype <> 'virtualxid'"
            )
            assert cur.fetchall() == []
            cur.execute("SELECT pg_current_xact_id_if_assigned()")
            assert cur.fetchone() == (None,)  # no write, not even a transaction id
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.django_db(transaction=True)
def test_views_run_inside_the_read_only_page_snapshot():
    ow.ops_world("rs_", robots=2, incident_policy=False)
    with read_snapshot(), connection.cursor() as cur:
        for v in VIEWS:
            cur.execute(f"SELECT * FROM {v}")
            cur.fetchall()


# ================================================================== scale fixture: oracle + EXPLAIN (sections 5, 6.5)
# Tables that grow with operations; a sequential scan of one of them where an index could serve the lookup
# would make a page's cost grow with history instead of with the fleet.
GROWING = {
    "planning_visit",
    "planning_visitrevision",
    "planning_reservation",
    "planning_vehicletrip",
    "readiness_departureevent",
    "readiness_readinessevidence",
    "fleet_hold",
    "fleet_inspection",
    "fleet_patternapproval",
    "fleet_baseauthorization",
    "fleet_capabilityprofile",
    "fleet_capabilityapproval",
    "fleet_configurationmanifest",
    "execution_executionrun",
    "execution_custodyevent",
    "execution_tripevent",
    "execution_returnreconciliation",
    "execution_tripclosure",
    "execution_severitydecision",
    "execution_incidentstatuschange",
    "execution_incidentescalation",
    "execution_escalationack",
    "execution_incident",
}
# The incident queue must look at every incident: an incident's openness is derived from its status stream,
# so no index can pre-select the open ones (no stored status, contract 0.1). One scan, nothing per row.
INHERENT_SCANS = {"operations_incident_queue": {"execution_incident"}}
SMALL_PAGES = 8


def seq_scans(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every Seq Scan node of an EXPLAIN (ANALYZE, FORMAT JSON) plan with its loops and row counts."""
    found = []
    if plan.get("Node Type") == "Seq Scan":
        found.append(
            {
                "rel": plan["Relation Name"],
                "loops": plan.get("Actual Loops", 1),
                "rows": plan.get("Actual Rows", 0),
                "removed": plan.get("Rows Removed by Filter", 0),
            }
        )
    for child in plan.get("Plans", []):
        found += seq_scans(child)
    return found


def growing_scan_problems(view: str, plan: dict[str, Any], pages: dict[str, int]) -> list[dict[str, Any]]:
    """A sequential scan of a growing table is a problem when an index would serve it: it runs once per outer
    row (loops > 1), or it reads mostly rows its filter throws away. Allowed: one scan whose rows all feed a
    hash join (the view needs every row of a fleet-sized table), and scans of tables of at most SMALL_PAGES
    pages (64 kB), where the planner rightly prefers reading the pages to an index probe; every per-row lookup
    column has an index (FK or the operations/0001 indexes), so the planner switches once such a table grows."""
    return [
        s
        for s in seq_scans(plan)
        if s["rel"] in GROWING
        and s["rel"] not in INHERENT_SCANS.get(view, set())
        and pages.get(s["rel"], 0) > SMALL_PAGES
        and (s["loops"] > 1 or s["removed"] > max(s["rows"], 50))
    ]


@pytest.mark.django_db
def test_scale_fixture_oracle_and_explain_without_growing_seq_scans():
    t0 = time.perf_counter()
    w = ow.scale_world()
    built = time.perf_counter() - t0
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM fleet_robot")
        robots = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM planning_visit")
        visits = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM execution_executionrun")
        runs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM execution_incident")
        incidents = cur.fetchone()[0]
    print(
        f"\nscale: {robots} robots, {visits} visits, {runs} runs, {incidents} incidents, built in {built:.0f}s "
        f"(laps {w.timings})"
    )
    assert robots >= 200 and visits >= 2000 and runs >= 600 and incidents >= 300
    assert built < 180, f"scale fixture took {built:.0f}s"

    out = assert_oracle_equal("scale fixture")
    assert len(out["runs"]) >= 50 and len(out["queue"]) >= 100

    with connection.cursor() as cur:
        for t in sorted(GROWING | {"fleet_robot", "planning_person", "planning_resource"}):
            cur.execute(f"ANALYZE {t}")
        cur.execute("SELECT relname, relpages FROM pg_class WHERE relname = ANY(%s)", [sorted(GROWING)])
        pages = dict(cur.fetchall())
        problems = {}
        for v in VIEWS:
            cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM {v}")
            raw = cur.fetchone()[0]
            top = (raw if isinstance(raw, list) else json.loads(raw))[0]
            plan = top["Plan"]
            print(v, f"{top['Execution Time']:.0f} ms", [s for s in seq_scans(plan) if s["rel"] in GROWING])
            bad = growing_scan_problems(v, plan, pages)
            if bad:
                problems[v] = bad
            # Honest row estimates keep the plan cost under jit_above_cost: JIT compilation alone cost ~2 s
            # per page before the custody LATERALs were capped with LIMIT 1.
            assert "JIT" not in top, f"{v}: JIT compiled (estimated cost {plan['Total Cost']:.0f})"
            assert top["Execution Time"] < 2000, f"{v}: {top['Execution Time']:.0f} ms on the scale fixture"
        print("pages", {k: n for k, n in pages.items() if n > 1})
        assert not problems, f"sequential scans of growing tables where an index would help: {problems}"


# ================================================================== custody vs trip-event race (EM verification)
# Two real ops_app connections race on the SAME robot/run. The first transaction holds the robot row lock (taken
# by its insert trigger); the second insert's trigger must wait on that robot row (observed in pg_stat_activity /
# pg_locks, never a sleep used as synchronization) and, once the first commits, see exactly what it committed.
def _pid(conn: psycopg.Connection) -> int:
    return int(conn.execute("SELECT pg_backend_pid()").fetchone()[0])


def _person_id(code: str) -> int:
    from planning.models import Person

    return Person.objects.get(code=code).pk


def _insert_custody(
    conn, run: ExecutionRun, frm: str, to: str, seq: int, by: int, receiver: str = "P-2"
) -> None:
    conn.execute(
        "INSERT INTO execution_custodyevent (robot_id, seq, from_kind, to_kind, run_id, trip_id, visit_id,"
        " custodian_id, received_by_id, basis, note, recorded_by_id, recorded_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'handoff', '', %s, now())",
        [
            run.robot_id,
            seq,
            frm,
            to,
            run.pk,
            run.trip_id,
            run.visit_id,
            _person_id("P-1"),
            _person_id(receiver),
            by,
        ],
    )


def _insert_trip(conn, run: ExecutionRun, kind: str, by: int) -> None:
    seq = {"arrived_site": 1, "departed_site": 2, "arrived_hub": 3}[kind]
    conn.execute(
        "INSERT INTO execution_tripevent (trip_id, run_id, seq, kind, recorded_by_id, recorded_at)"
        " VALUES (%s, %s, %s, %s, %s, now())",
        [run.trip_id, run.pk, seq, kind, by],
    )


def _await_robot_lock_wait(observer, waiter_pid: int, holder_pid: int) -> None:
    """Block until `waiter_pid` is observed waiting (wait_event_type 'Lock') on the robot ROW held by
    `holder_pid`: it holds the tuple lock on that fleet_robot row and waits on the holder's transaction."""
    deadline = time.monotonic() + 15
    while True:
        row = observer.execute(
            "SELECT a.wait_event_type, pg_blocking_pids(a.pid),"
            " EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid = a.pid AND l.locktype = 'tuple'"
            "         AND l.relation = 'fleet_robot'::regclass AND l.granted),"
            " EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid = a.pid AND l.locktype = 'transactionid'"
            "         AND NOT l.granted)"
            " FROM pg_stat_activity a WHERE a.pid = %s",
            [waiter_pid],
        ).fetchone()
        if row and row[0] == "Lock" and holder_pid in row[1] and row[2] and row[3]:
            break
        assert time.monotonic() < deadline, (
            f"second transaction never observed waiting on the robot row: {row}"
        )
        time.sleep(0.02)  # polling interval of the observation only; ordering comes from the lock itself


def _race(first_sql, second_sql) -> dict[str, Any]:
    """first_sql(conn) runs in an open transaction; second_sql(conn) runs (autocommit) in a thread and must wait
    on the robot lock until the first commits. Returns the outcome of the second."""
    first, second, observer = _app_conn(), _app_conn(autocommit=True), _app_conn(autocommit=True)
    result: dict[str, Any] = {}
    try:
        first_sql(first)  # holds the robot row lock (insert trigger FOR UPDATE), not committed
        ready = threading.Event()

        def run_second():
            result["pid"] = _pid(second)
            ready.set()
            try:
                second_sql(second)
                result["ok"] = True
            except psycopg.Error as exc:
                result["error"] = exc

        t = threading.Thread(target=run_second)
        t.start()
        assert ready.wait(10)
        _await_robot_lock_wait(observer, result["pid"], _pid(first))
        assert "ok" not in result and "error" not in result  # still waiting before the first commits
        result["observed_waiting"] = True
        first.commit()
        t.join(15)
        assert not t.is_alive()
    finally:
        first.close()
        second.close()
        observer.close()
    return result


def _event_seq(run: ExecutionRun, kind: str) -> int | None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT custody_seq_at_event FROM execution_tripevent WHERE run_id = %s AND kind = %s",
            [run.pk, kind],
        )
        row = cur.fetchone()
    return row[0] if row else None


@pytest.mark.django_db(transaction=True)
def test_race_custody_commits_first_then_trip_event_records_the_new_seq():
    w = ow.ops_world("ra_", robots=2, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "at_site")  # custody #2 = site
    fid = w.field.pk
    out = _race(
        lambda c: _insert_custody(c, run, "site", "vehicle", 3, fid),  # site -> vehicle, holds the robot
        lambda c: _insert_trip(
            c, run, "departed_site", fid
        ),  # waits on the robot, then needs vehicle custody
    )
    assert out.get("observed_waiting") and out.get("ok") is True, out
    seq = _event_seq(run, "departed_site")
    assert seq == 3 and custody_at(run, seq) == "vehicle"  # the NEW seq: the committed handoff


@pytest.mark.django_db(transaction=True)
def test_race_trip_event_commits_first_custody_waits_then_follows_its_own_rules():
    w = ow.ops_world("rb_", robots=3, incident_policy=False)
    fid = w.field.pk

    # (a) arrived_hub holds the robot; vehicle -> hub waits, then SUCCEEDS (its rule needs arrived_hub, now committed)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "returning")  # custody #3 = vehicle, departed_site recorded
    out = _race(
        lambda c: _insert_trip(c, run, "arrived_hub", fid),
        lambda c: _insert_custody(c, run, "vehicle", "hub", 4, fid, receiver="P-3"),
    )
    assert out.get("observed_waiting") and out.get("ok") is True, out
    seq = _event_seq(run, "arrived_hub")
    assert seq == 3 and custody_at(run, seq) == "vehicle"  # the OLD seq: the robot was still in the vehicle
    assert CustodyEvent.objects.filter(robot_id=run.robot_id).order_by("-seq").first().to_kind == "hub"

    # (b) departed_site (robot left on site) holds the robot; site -> vehicle waits, then is REFUSED by its own rule
    run3 = ow.depart_new(w, "R-03")
    ow.advance(w, run3, "at_site")  # custody #2 = site
    icode = ow.report(w, "R-03", event="other")
    w.cmd.disposition(run3, "robot_left_on_site", incident=icode)
    out = _race(
        lambda c: _insert_trip(c, run3, "departed_site", fid),
        lambda c: _insert_custody(c, run3, "site", "vehicle", 3, fid),
    )
    assert out.get("observed_waiting") and "ok" not in out, out
    assert isinstance(out["error"], psycopg.errors.CheckViolation)
    assert "site -> vehicle: the trip has already departed the site" in str(out["error"])
    seq = _event_seq(run3, "departed_site")
    assert seq == 2 and custody_at(run3, seq) == "site"  # the OLD seq, and no custody row was added
    assert CustodyEvent.objects.filter(robot_id=run3.robot_id).count() == 2
