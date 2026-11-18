"""Slice 4 logistics commands (Engineer B): custody verification and moves, trip events, site checks, return
reconciliation and trip closure. Every rule has a positive path and an explained refusal; the database triggers
(tests/test_execution_db.py) are the backstop. SYNTHETIC data only."""

import pytest

from accounts.services import revoke_role
from execution import logistics as lg
from execution import reconciliation as rc
from execution import site_checks as sc
from execution.custody import current_custody, departure_problem, open_run_id
from execution.models import (
    CustodyEvent,
    CustodyVerification,
    Incident,
    ReturnReconciliation,
    SiteCheck,
    SiteCheckTemplate,
    TripClosure,
    TripEvent,
)
from fleet import services as fs
from fleet.management.commands.seed_fleet import TEMPLATE_ITEMS
from fleet.models import Hold
from planning.models import ConsentRecord
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.models import AuditEvent
from tests import execution_world as ew
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.readiness_world import record_inspection
from tests.test_execution_db import _owner_conn
from tests.test_readiness_settings_race import _capture

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def w(transactional_db):
    return ew.build_execution(prefix="xl_")


# ------------------------------------------------------------------ command shorthands
def verify(w, seq=1, verifier="P-2", by=None):
    return lg.verify_custody(by or w.field, run_code=w.run.code, custody_seq=seq, verifier_code=verifier)


def trip(w, kind, by=None):
    return lg.record_trip_event(by or w.field, run_code=w.run.code, kind=kind)


def move(w, to, custodian="P-1", receiver="P-2", note="", by=None):
    return lg.transfer_custody(
        by or w.field,
        run_code=w.run.code,
        to_kind=to,
        custodian_code=custodian,
        received_by_code=receiver,
        note=note,
    )


def check(w, adults=2, conditions=None, verifier="P-1", template="SC1", by=None):
    return sc.record_site_check(
        by or w.field,
        run_code=w.run.code,
        template_version=template,
        adults_present=adults,
        conditions=dict(ew.ALL_OK) if conditions is None else conditions,
        verifier_code=verifier,
    )


def reconcile(w, inspection=None, discrepancies=None, by=None):
    return rc.reconcile_return(
        by or w.ops, run_code=w.run.code, inspection_code=inspection, discrepancies=discrepancies
    )


def close(w, by=None):
    return rc.close_trip(by or w.ops, run_code=w.run.code)


def inspect(w):
    return record_inspection(w, w.visit, robot=w.robot.code)


def custody(w):
    return current_custody(w.robot.pk)


def to_site(w):
    verify(w)
    trip(w, "arrived_site")
    move(w, "site")


def to_hub(w):
    move(w, "vehicle")
    trip(w, "departed_site")
    trip(w, "arrived_hub")
    return move(w, "hub", receiver="P-3")


def events(w):
    return list(
        CustodyEvent.objects.filter(robot=w.robot).order_by("seq").values_list("seq", "from_kind", "to_kind")
    )


# ================================================================== the full synthetic return path
def test_full_synthetic_return_path(w):
    assert custody(w).kind == "vehicle" and not custody(w).verified  # departure assignment, administrative

    v = verify(w)
    assert v["custody"] == {"kind": "vehicle", "verified": True, "label": "vehicle"}
    assert trip(w, "arrived_site")["seq"] == 1
    m = move(w, "site")
    assert (m["from"], m["to"], m["basis"], m["seq"]) == ("vehicle", "site", "handoff", 2)
    go = check(w)
    assert go["result"] == "go" and go["reasons"] == [] and go["consent_count"] == 2 and go["mode"] == "demo"
    assert move(w, "vehicle")["to"] == "vehicle"
    assert trip(w, "departed_site")["seq"] == 2
    arrived = trip(w, "arrived_hub")
    assert arrived["seq"] == 3 and "warning" not in arrived
    hub = move(w, "hub", receiver="P-3")
    assert hub["custody"] == {"kind": "hub", "verified": True, "label": "hub"}
    assert "open run" in (departure_problem(w.robot.pk) or "")  # back at hub, but the run is still open

    insp = inspect(w)  # post-visit inspection via fleet.services.submit_inspection with visit_ref
    r = reconcile(w, inspection=insp)
    assert r["outcome"] == "reconciled" and r["problems"] == [] and r["cargo_matches"] is True
    assert r["hub_receipt_seq"] == hub["seq"] and r["post_visit_inspection"] == insp
    c = close(w)
    assert c["closed"] is True and c["reconciliation"] == r["id"]

    assert open_run_id(w.robot.pk) is None and departure_problem(w.robot.pk) is None
    assert events(w) == [
        (1, "hub", "vehicle"),
        (2, "vehicle", "site"),
        (3, "site", "vehicle"),
        (4, "vehicle", "hub"),
    ]
    assert list(TripEvent.objects.filter(run=w.run).order_by("seq").values_list("kind", flat=True)) == [
        "arrived_site",
        "departed_site",
        "arrived_hub",
    ]
    for name, n in (
        ("verify_custody", 1),
        ("record_trip_event", 3),
        ("transfer_custody", 3),
        ("record_site_check", 1),
        ("reconcile_return", 1),
        ("close_trip", 1),
    ):
        assert AuditEvent.objects.filter(command=f"execution.{name}", outcome="committed").count() == n, name


# ================================================================== verify_custody
def test_verify_custody_rules(w):
    with pytest.raises(Conflict, match="not field crew"):
        verify(w, verifier="P-3")
    with pytest.raises(Invalid, match="No person"):
        verify(w, verifier="P-99")
    with pytest.raises(Invalid, match="no custody event #7"):
        verify(w, seq=7)
    with pytest.raises(Invalid, match="positive whole number"):
        verify(w, seq=0)
    with pytest.raises(PermissionDenied):
        verify(w, by=w.eng)  # engineers do not record field events
    assert not CustodyVerification.objects.exists()

    assert verify(w, verifier="P-1", by=w.ops)["verified_by"] == "P-1"  # either crew member may verify
    with pytest.raises(Conflict, match="already verified"):
        verify(w)
    trip(w, "arrived_site")
    move(w, "site")
    with pytest.raises(Conflict, match="handoff move; only the administrative"):
        verify(w, seq=2)
    assert CustodyVerification.objects.count() == 1
    assert AuditEvent.objects.filter(command="execution.verify_custody", outcome="denied").count() == 1


def test_verifier_must_still_hold_a_field_role(w):
    admin = make_user("xl_admin", "admin")
    for g in w.field.role_grants.filter(revoked_at__isnull=True):  # P-2 loses field_operator (synthetic)
        revoke_role(admin, grant_id=g.pk, reason="SYNTHETIC: left the field team")
    with pytest.raises(Conflict, match="does not hold an active ops_lead or field_operator"):
        verify(w, verifier="P-2", by=w.ops)


# ================================================================== transfer_custody
def test_no_move_before_the_departure_assignment_is_verified(w):
    trip(w, "arrived_site")
    with pytest.raises(Conflict, match="not verified: the field crew must verify"):
        move(w, "site")
    assert len(events(w)) == 1


def test_handoff_rules_vehicle_to_site(w):
    verify(w)
    with pytest.raises(Conflict, match="arrived at the site"):
        move(w, "site")
    trip(w, "arrived_site")
    with pytest.raises(Conflict, match="must both be field crew"):
        move(w, "site", receiver="P-3")
    with pytest.raises(Conflict, match="must both be field crew"):
        move(w, "site", custodian="P-3")
    with pytest.raises(Conflict, match="P-4 does not hold an active"):
        move(w, "site", receiver="P-4")
    with pytest.raises(Invalid, match="needs a receiver"):
        move(w, "site", receiver="")
    with pytest.raises(Conflict, match="cannot move from vehicle to vehicle|already in the vehicle"):
        move(w, "vehicle")
    with pytest.raises(Conflict, match="Custody cannot move from vehicle to hub|arrived at the hub"):
        move(w, "hub")
    with pytest.raises(Invalid, match="only by reporting a robot_unaccounted incident"):
        move(w, "unaccounted")
    with pytest.raises(Invalid, match="to_kind must be one of"):
        move(w, "garage")
    assert len(events(w)) == 1
    assert move(w, "site", custodian="P-2", receiver="P-1")["custodian"] == "P-2"  # either crew member


def test_site_to_hub_is_not_a_move_and_hub_to_vehicle_only_at_departure(w):
    to_site(w)
    with pytest.raises(Conflict, match="cannot move from site to hub"):
        move(w, "hub", receiver="P-3")
    move(w, "vehicle")
    trip(w, "departed_site")
    trip(w, "arrived_hub")
    move(w, "hub", receiver="P-3")
    with pytest.raises(Conflict, match="assigned only by recording the departure"):
        move(w, "vehicle")


def test_vehicle_to_hub_needs_arrival_crew_custodian_and_field_receiver(w):
    to_site(w)
    move(w, "vehicle")
    trip(w, "departed_site")
    with pytest.raises(Conflict, match="arrived at the hub"):
        move(w, "hub", receiver="P-3")
    trip(w, "arrived_hub")
    with pytest.raises(Conflict, match="custodian must be field crew"):
        move(w, "hub", custodian="P-3", receiver="P-3")
    with pytest.raises(Conflict, match="P-4 does not hold an active"):
        move(w, "hub", receiver="P-4")
    hub = move(w, "hub", receiver="P-3")  # a field user who is not crew may receive at the hub
    assert hub["received_by"] == "P-3" and custody(w).kind == "hub"


def test_site_to_vehicle_refused_after_the_vehicle_left(w, app_role_conn):
    to_site(w)
    iid = ew.raw_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        ew.raw_disposition(cur, w, "robot_left_on_site", incident_id=iid)
    trip(w, "departed_site")
    with pytest.raises(Conflict, match="already departed the site"):
        move(w, "vehicle")


# ================================================================== trip events
def test_trip_events_in_order_once_each(w):
    with pytest.raises(Invalid, match="kind must be one of"):
        trip(w, "lunch")
    with pytest.raises(Conflict, match="record arrived_site first"):
        trip(w, "departed_site")
    with pytest.raises(Conflict, match="record arrived_site first"):
        trip(w, "arrived_hub")
    verify(w)
    trip(w, "arrived_site")
    with pytest.raises(Conflict, match="already arrived at the site"):
        trip(w, "arrived_site")
    with pytest.raises(PermissionDenied):
        trip(w, "departed_site", by=w.eng)
    assert TripEvent.objects.filter(run=w.run).count() == 1


def test_departed_site_needs_the_robot_back_in_the_vehicle(w):
    to_site(w)
    with pytest.raises(Conflict, match=r"robot is at the site\. Record the site -> vehicle handoff"):
        trip(w, "departed_site")
    move(w, "vehicle")
    assert trip(w, "departed_site")["kind"] == "departed_site"


# ================================================================== site checks
def test_site_check_go_and_every_no_go_reason(w):
    verify(w)
    early = check(w)  # before arrival: recorded, no_go, explained
    assert early["result"] == "no_go"
    assert "the trip has not arrived at the site" in early["reasons"]
    assert any("not in site custody" in r and "in the vehicle" in r for r in early["reasons"])
    trip(w, "arrived_site")
    move(w, "site")

    bad = check(w, adults=3, conditions={"c1": "ok", "c2": "not_ok"})
    assert bad["result"] == "no_go" and bad["consent_count"] == 2
    assert bad["reasons"] == [
        "condition not ok: SYNTHETIC: participants briefed (c2)",
        "3 adults present but only 2 valid consents cover this visit",
    ]
    good = check(w, adults=0)
    assert good["result"] == "go" and good["reasons"] == []
    assert list(SiteCheck.objects.filter(run=w.run).order_by("id").values_list("result", flat=True)) == [
        "no_go",
        "no_go",
        "go",
    ]


def test_site_check_input_validation(w):
    to_site(w)
    with pytest.raises(Invalid, match="No site check template"):
        check(w, template="SC-none")
    with pytest.raises(Invalid, match="missing"):
        check(w, conditions={"c1": "ok"})
    with pytest.raises(Invalid, match="not in template SC1"):
        check(w, conditions={**ew.ALL_OK, "c9": "ok"})
    with pytest.raises(Invalid, match="'ok' or 'not_ok'"):
        check(w, conditions={"c1": "ok", "c2": "fine"})
    with pytest.raises(Invalid, match="whole number"):
        check(w, adults=-1)
    with pytest.raises(Invalid, match="whole number"):
        check(w, adults=True)
    with pytest.raises(Conflict, match="must be field crew"):
        check(w, verifier="P-3")
    assert not SiteCheck.objects.exists()


def test_site_check_unknown_expected_adults_is_no_go(w):
    to_site(w)
    with _owner_conn() as owner, owner.cursor() as cur:  # the planning command refuses unknown; set it raw
        cur.execute("UPDATE planning_site SET expected_adults = NULL WHERE id = %s", [w.run.visit.site_id])
    r = check(w)
    assert r["result"] == "no_go" and r["expected_adults"] is None
    assert r["reasons"] == ["the expected number of adults at the site is unknown"]


def test_site_check_synthetic_template_in_production_is_no_go(w):
    to_site(w)
    set_setting("demo_mode", "false")
    r = check(w)
    assert r["result"] == "no_go" and r["mode"] == "production"
    assert r["reasons"] == ["a synthetic_demo template cannot produce go outside demo mode"]
    SiteCheckTemplate.objects.create(
        version="SC-AP", items=ew.TEMPLATE_ITEMS, source="approved_procedure", recorded_by=w.ops
    )
    assert check(w, template="SC-AP")["result"] == "go"


def test_site_check_consent_withdrawal_counts(w):
    to_site(w)
    c = ConsentRecord.objects.get(site__code="S-1", participant_ref="P1")
    w.p.withdraw_consent(w.field, consent_id=c.pk, reason="withdrawn")
    r = check(w)
    assert r["result"] == "no_go" and r["consent_count"] == 1
    assert r["reasons"] == ["2 adults present but only 1 valid consents cover this visit"]
    assert check(w, adults=1)["result"] == "go"


# ================================================================== stranded robot: left on site / unaccounted
def test_robot_left_on_site_keeps_the_trip_unclosable(w, app_role_conn):
    to_site(w)
    iid = ew.raw_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        ew.raw_disposition(cur, w, "robot_left_on_site", incident_id=iid)
    trip(w, "departed_site")
    back = trip(w, "arrived_hub")
    assert "robot is at the site" in back["warning"]
    with pytest.raises(Conflict, match=r"not been reconciled \(the robot is at the site\)"):
        close(w)
    insp = inspect(w)
    r = reconcile(w, inspection=insp, discrepancies=["robot left on site"])
    assert r["outcome"] == "unresolved" and r["hub_receipt_seq"] is None
    assert "the robot has not been received at the hub (it is at the site)" in r["problems"]
    with pytest.raises(Conflict, match="latest reconciliation is unresolved"):
        close(w)
    r2 = reconcile(w, inspection=insp)  # even with nothing written down, a missing robot never reconciles
    assert r2["outcome"] == "unresolved"
    with pytest.raises(Conflict, match="unresolved"):
        close(w)
    assert custody(w).kind == "site" and open_run_id(w.robot.pk) == w.run.pk
    assert not TripClosure.objects.exists()


def test_unaccounted_robot_recovery_then_a_held_robots_trip_closes(w, app_role_conn):
    to_site(w)
    iid = ew.raw_incident(app_role_conn, w, unaccounted=True)
    assert custody(w).kind == "unaccounted"
    with app_role_conn.cursor() as cur:
        ew.raw_disposition(cur, w, "robot_unaccounted", incident_id=iid)
    trip(w, "departed_site")
    trip(w, "arrived_hub")
    with pytest.raises(Conflict, match="not been reconciled"):
        close(w)
    assert reconcile(w)["outcome"] == "unresolved"
    with pytest.raises(Conflict, match="unresolved"):
        close(w)

    with pytest.raises(PermissionDenied, match="requires an ops_lead"):
        move(w, "vehicle", note="found it", by=w.field)
    with pytest.raises(Invalid, match="needs a note"):
        move(w, "vehicle", note="  ", by=w.ops)
    with pytest.raises(Conflict, match="P-4 does not hold"):
        move(w, "vehicle", receiver="P-4", note="found", by=w.ops)
    rec = move(w, "vehicle", note="SYNTHETIC: recovered and acknowledged", by=w.ops)
    assert rec["basis"] == "recovered" and rec["custody"]["verified"]
    ev = CustodyEvent.objects.get(robot=w.robot, seq=rec["seq"])
    assert ev.incident_id == iid  # a recovery references the incident; it does not resolve it

    hub = move(w, "hub", receiver="P-3")
    r = reconcile(w, inspection=inspect(w))
    assert r["outcome"] == "reconciled", r["problems"]
    hold = Incident.objects.get(pk=iid).hold
    assert Hold.objects.get(pk=hold.pk).released_at is None  # still held
    assert close(w)["closed"] and hub["seq"] == r["hub_receipt_seq"]
    assert Hold.objects.get(pk=hold.pk).released_at is None  # closure never touches the hold


# ================================================================== reconciliation
def test_reconciliation_unresolved_then_reconciled(w):
    to_site(w)
    early = inspect(w)  # submitted BEFORE the hub receipt
    to_hub(w)
    none = reconcile(w)
    assert none["outcome"] == "unresolved"
    assert none["problems"] == ["no post-visit inspection of this robot for this visit was given"]
    stale = reconcile(w, inspection=early)
    assert stale["problems"] == [f"inspection {early} was not submitted after the hub receipt"]
    insp = inspect(w)
    noted = reconcile(w, inspection=insp, discrepancies=["SYNTHETIC: charger missing"])
    assert noted["outcome"] == "unresolved" and noted["problems"] == ["1 open discrepancy"]
    with pytest.raises(Conflict, match="latest reconciliation is unresolved"):
        close(w)
    ok = reconcile(w, inspection=insp)
    assert ok["outcome"] == "reconciled"
    later = reconcile(w, inspection=insp, discrepancies=["second look"])  # the newest one counts
    with pytest.raises(Conflict, match="latest reconciliation is unresolved"):
        close(w)
    assert later["outcome"] == "unresolved"
    reconcile(w, inspection=insp)
    close(w)
    assert ReturnReconciliation.objects.filter(run=w.run).count() == 6


def test_reconcile_input_and_permission(w):
    to_site(w)
    to_hub(w)
    with pytest.raises(Invalid, match="list of non-empty notes"):
        reconcile(w, discrepancies=[" "])
    with pytest.raises(Invalid, match="must not repeat"):
        reconcile(w, discrepancies=["a", "a"])
    with pytest.raises(Invalid, match="No inspection"):
        reconcile(w, inspection="IN-none")
    other = record_inspection(w, "V-OTHER", robot=w.robot.code)
    with pytest.raises(Conflict, match="not a post-visit inspection of this robot"):
        reconcile(w, inspection=other)
    with pytest.raises(PermissionDenied):
        reconcile(w, by=w.field)
    with pytest.raises(PermissionDenied):
        close(w, by=w.field)
    assert not ReturnReconciliation.objects.exists()


def test_failed_post_visit_inspection_still_reconciles_and_holds(w):
    """Any result counts for reconciliation (a FAIL already holds the robot through the fleet workflow)."""
    to_site(w)
    to_hub(w)
    answers = dict(w.all_pass)
    answers[next(i["id"] for i in TEMPLATE_ITEMS if i["robot_condition"])] = "fail"
    code = fs.submit_inspection(
        w.ops,
        robot_code=w.robot.code,
        template_version="T1",
        answers=answers,
        performer=w.field.username,
        verifier=w.field2.username,
        visit_ref=w.visit,
    )["entity_id"]
    r = reconcile(w, inspection=code)
    assert r["outcome"] == "reconciled" and r["post_visit_inspection_result"] == "FAIL"
    assert Hold.objects.filter(robot=w.robot, released_at__isnull=True).exists()
    assert close(w)["closed"]


def test_close_needs_the_vehicle_back(w):
    to_site(w)
    move(w, "vehicle")
    trip(w, "departed_site")
    with pytest.raises(Conflict, match="has not arrived at the hub"):
        close(w)


# ================================================================== after closure
def test_nothing_after_closure(w):
    to_site(w)
    to_hub(w)
    reconcile(w, inspection=inspect(w))
    close(w)
    with pytest.raises(Conflict, match="already closed"):
        close(w)
    with pytest.raises(Conflict, match="is closed"):
        reconcile(w, inspection=inspect(w))
    with pytest.raises(Conflict, match="is closed"):
        reconcile(w, discrepancies=["late"])
    with pytest.raises(Conflict, match="is closed"):
        check(w)
    with pytest.raises(Conflict, match="is closed"):
        verify(w)
    with pytest.raises(Conflict, match="is closed"):
        move(w, "vehicle")
    assert ReturnReconciliation.objects.count() == 1 and TripClosure.objects.count() == 1


# ================================================================== lock order (contract section 4)
def test_each_command_locks_in_the_contract_order(w):
    run = ["execution_executionrun"]
    seen = _capture(lambda: verify(w))
    assert seen == ["accounts_rolegrant", "fleet_robot", "planning_person", *run], seen
    seen = _capture(lambda: trip(w, "arrived_site"))
    assert seen == ["accounts_rolegrant", "fleet_robot", "planning_visit", "planning_vehicletrip", *run], seen
    seen = _capture(lambda: move(w, "site"))
    assert seen == [
        "accounts_rolegrant",
        "fleet_robot",
        "planning_person",
        "planning_person",
        "planning_visit",
        "planning_vehicletrip",
        *run,
    ], seen
    seen = _capture(lambda: check(w))
    assert seen == [
        "accounts_rolegrant",
        "settings",
        "planning_site",
        "fleet_robot",
        "planning_person",
        "planning_visit",
        *run,
    ], seen
    move(w, "vehicle")
    trip(w, "departed_site")
    trip(w, "arrived_hub")
    move(w, "hub", receiver="P-3")
    insp = inspect(w)
    seen = _capture(lambda: reconcile(w, inspection=insp))
    assert seen == ["accounts_rolegrant", "fleet_robot", "planning_visit", "planning_vehicletrip", *run], seen
    seen = _capture(lambda: close(w))
    assert seen == ["accounts_rolegrant", "fleet_robot", "planning_visit", "planning_vehicletrip", *run], seen
