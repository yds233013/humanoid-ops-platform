"""Slice 4 database bypass protections (docs/04_field_execution_contract.md section 6), as the runtime role.

Every refusal is exercised with direct SQL through `app_role_conn` (ops_app, autocommit) and paired with a
positive control: the valid insert succeeds. The world is built through the real commands
(tests/execution_world.py); the departure that opened `w.run` went through readiness.record_departure.
"""

import os
import threading
import time
from datetime import timedelta

import psycopg
import pytest
from django.db import connection
from psycopg.types.json import Jsonb

from execution.models import (
    CustodyEvent,
    ExecutionRun,
    Incident,
    IncidentPolicy,
    ReturnReconciliation,
    SessionEvent,
    SiteCheckTemplate,
    TripClosure,
)
from fleet.models import Hold, Robot
from planning.models import ConsentRecord, Reservation, VehicleTrip, Visit, VisitRevision
from platform_core.commands import Conflict
from readiness import commands as rc
from readiness.evaluator import evaluate_readiness, take_guard_snapshot
from readiness.models import DepartureAuthorization, DepartureEvent, ReadinessEvidence
from tests import execution_world as ew
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.planning_world import DAY
from tests.readiness_world import build_readiness
from tests.test_fleet_concurrency import Interleaving

CHECK = psycopg.errors.CheckViolation
DENIED = psycopg.errors.InsufficientPrivilege
NEXT_DAY = DAY + timedelta(days=1)
RULES = {
    "severity_scale": ["low", "medium", "high"],
    "escalation_required_from": "high",
    "downgrade_roles": ["safety_reviewer"],
}


@pytest.fixture
def w(transactional_db):
    return ew.build_execution(prefix="xd_")


def refused(match: str):
    return pytest.raises(CHECK, match=match)


def _owner_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_OWNER_USER", "ops_owner"),
        password=os.environ.get("DB_OWNER_PASSWORD", "ops_owner_dev"),
        autocommit=True,
    )


def custody(cur, robot_id: int) -> tuple:
    cur.execute("SELECT kind, verified FROM execution_current_custody(%s)", [robot_id])
    return tuple(cur.fetchone())


def at_site_with_go(cur, w) -> int:
    ew.to_site(cur, w)
    return ew.go_check(cur, w)


# ================================================================== run creation, privileges, append-only
def test_departure_opens_the_run_and_assigns_custody_administratively(w, app_role_conn):
    run = w.run
    assert run.code.startswith("EX-") and run.departure.visit.code == w.visit
    assert (run.visit_id, run.robot_id) == (run.departure.visit_id, run.departure.robot_id)
    assert run.trip.revision_id == run.revision_id
    ev = CustodyEvent.objects.get(run=run)
    assert (ev.seq, ev.from_kind, ev.to_kind, ev.basis) == (1, "hub", "vehicle", "administrative")
    assert ev.custodian_id == run.revision.lead_id and ev.received_by_id is None
    with app_role_conn.cursor() as cur:
        assert custody(cur, run.robot_id) == ("vehicle", False)
        ew.raw_verify(cur, w)
        assert custody(cur, run.robot_id) == ("vehicle", True)
        other = Robot.objects.get(code="R-02")
        assert custody(cur, other.pk) == ("hub", True)  # baseline: no events, no runs


def test_runtime_role_cannot_insert_runs(w, app_role_conn):
    with app_role_conn.cursor() as cur, pytest.raises(DENIED):
        cur.execute(
            "INSERT INTO execution_executionrun (code, departure_id, visit_id, revision_id, robot_id, manifest_id, "
            "trip_id, recorded_by_id, recorded_at) SELECT 'EX-9999', departure_id, visit_id, revision_id, robot_id, "
            "manifest_id, trip_id, recorded_by_id, now() FROM execution_executionrun WHERE id = %s",
            [w.run.pk],
        )
    with _owner_conn() as owner, owner.cursor() as cur, refused("opened only by recording a departure"):
        cur.execute(  # even the owner cannot open a run outside the departure trigger
            "INSERT INTO execution_executionrun (code, departure_id, visit_id, revision_id, robot_id, manifest_id, "
            "trip_id, recorded_by_id, recorded_at) SELECT 'EX-9999', departure_id, visit_id, revision_id, robot_id, "
            "manifest_id, trip_id, recorded_by_id, now() FROM execution_executionrun WHERE id = %s",
            [w.run.pk],
        )
    with app_role_conn.cursor() as cur:  # control: the runtime role may lock the run row (lock_guards)
        cur.execute("SELECT id FROM execution_executionrun WHERE id = %s FOR UPDATE", [w.run.pk])
        assert cur.fetchone()[0] == w.run.pk
    assert ExecutionRun.objects.count() == 1


EXECUTION_TABLES = [
    "execution_executionrun",
    "execution_custodyevent",
    "execution_custodyverification",
    "execution_tripevent",
    "execution_sitechecktemplate",
    "execution_sitecheck",
    "execution_sessionevent",
    "execution_interruption",
    "execution_interruptionresolution",
    "execution_incidentpolicy",
    "execution_incidentpolicyapproval",
    "execution_incidentpolicywithdrawal",
    "execution_incident",
    "execution_faultobservation",
    "execution_severitydecision",
    "execution_incidentstatuschange",
    "execution_incidentescalation",
    "execution_escalationack",
    "execution_recoverydisposition",
    "execution_returnreconciliation",
    "execution_tripclosure",
]


def _policy(cur, w, *, source="synthetic_demo", rules=None, approve=True, by=None) -> int:
    cur.execute("SELECT coalesce(max(version), 0) + 1 FROM execution_incidentpolicy")
    version = cur.fetchone()[0]
    pid = ew.raw_insert(
        cur,
        "execution_incidentpolicy",
        version=version,
        rules=rules or RULES,
        source=source,
        notes="SYNTHETIC",
        lock_version=1,
        recorded_by_id=(by or w.lead).pk,
    )
    if approve:
        for slot, user in (("engineering", w.lead2), ("safety", w.safety)):
            cur.execute(
                "INSERT INTO execution_incidentpolicyapproval (policy_id, slot, approver_id, recorded_at) "
                "VALUES (%s, %s, %s, NULL)",
                [pid, slot, user.pk],
            )
    return pid


def _full_history(w, app_role_conn) -> None:
    """At least one row in every execution table."""
    with app_role_conn.cursor() as cur:
        sc = at_site_with_go(cur, w)
        ew.raw_session(cur, w, "start", site_check_id=sc)
        i = ew.raw_interruption(cur, w)
        ew.raw_resolve(cur, w, i)
        pid = _policy(cur, w)
        cur.execute(
            "INSERT INTO execution_incidentpolicywithdrawal (policy_id, withdrawn_by_id, reason, recorded_at) "
            "VALUES (%s, %s, 'superseded', NULL)",
            [_policy(cur, w, approve=False), w.lead.pk],
        )
    iid = ew.raw_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        ew.raw_insert(
            cur,
            "execution_faultobservation",
            incident_id=iid,
            component="SYN arm",
            status="suspected",
            evidence_ref="",
            recorded_by_id=w.ops.pk,
        )
        ew.raw_insert(
            cur,
            "execution_severitydecision",
            incident_id=iid,
            level="low",
            policy_id=pid,
            reason="r",
            recorded_by_id=w.lead.pk,
        )
        ew.raw_insert(
            cur,
            "execution_incidentstatuschange",
            incident_id=iid,
            to_status="triaged",
            recorded_by_id=w.ops.pk,
        )
        e = ew.raw_insert(
            cur,
            "execution_incidentescalation",
            incident_id=iid,
            to_role="safety_reviewer",
            to_person_id=None,
            requested_action="review",
            recorded_by_id=w.ops.pk,
        )
        ew.raw_insert(cur, "execution_escalationack", escalation_id=e, acknowledged_by_id=w.safety.pk)
        ew.raw_disposition(cur, w, "end_visit_return")
        ew.raw_session(cur, w, "stop")
        ew.to_hub(cur, w)
        ew.close_run(cur, w)


def test_every_execution_table_is_append_only(w, app_role_conn):
    _full_history(w, app_role_conn)
    with app_role_conn.cursor() as cur:
        for t in EXECUTION_TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")
            assert cur.fetchone()[0] >= 1, t
            for stmt in (f"UPDATE {t} SET id = id", f"DELETE FROM {t}", f"TRUNCATE {t} CASCADE"):
                # execution_incidentpolicy keeps UPDATE (approval bumps lock_version); its guard refuses it
                with pytest.raises(CHECK if stmt.startswith("UPDATE execution_incidentpolicy ") else DENIED):
                    cur.execute(stmt)
    with _owner_conn() as owner, owner.cursor() as cur:  # the triggers refuse even the owner
        for t in EXECUTION_TABLES:
            for stmt in (f"UPDATE {t} SET id = id", f"DELETE FROM {t}"):
                with pytest.raises(psycopg.errors.Error):
                    cur.execute(stmt)


def test_recorded_at_is_database_time(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        vid = ew.raw_insert(
            cur,
            "execution_custodyverification",
            custody_event_id=ew.latest_custody(w).pk,
            verified_by_id=ew.pid("P-2"),
            recorded_by_id=w.ops.pk,
            recorded_at="2001-01-01T00:00Z",
        )
        cur.execute(
            "SELECT recorded_at > now() - interval '1 minute' FROM execution_custodyverification "
            "WHERE id = %s",
            [vid],
        )
        assert cur.fetchone()[0]


# ================================================================== custody
def test_custody_wrong_from_and_sequence_gap(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
        ew.raw_trip_event(cur, w, "arrived_site")
        with refused("wrong custody origin"):
            ew.raw_custody(cur, w, "hub", "site")
        with refused("sequence"):
            ew.raw_custody(cur, w, "vehicle", "site", seq=ew.next_custody_seq(w.run.robot_id) + 1)
        with refused("sequence"):
            ew.raw_custody(cur, w, "vehicle", "site", seq=1)
        ew.raw_custody(cur, w, "vehicle", "site")  # control
        assert custody(cur, w.run.robot_id) == ("site", True)


def test_vehicle_to_site_needs_verification_arrival_and_crew(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_trip_event(cur, w, "arrived_site")
        with refused("not verified"):
            ew.raw_custody(cur, w, "vehicle", "site")
        with refused("field crew"):
            ew.raw_verify(cur, w, verifier="P-3")  # a field user, but not crew of this run
        ew.raw_verify(cur, w)
        with refused("receiver must hold"):
            ew.raw_custody(cur, w, "vehicle", "site", received_by="P-4")  # P-4: engineer, no field role
        with refused("field crew"):
            ew.raw_custody(cur, w, "vehicle", "site", received_by="P-3")
        with refused("handoff"):
            ew.raw_custody(cur, w, "vehicle", "site", basis="administrative")
        ew.raw_custody(cur, w, "vehicle", "site")


def test_vehicle_to_site_before_arrival_is_refused(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
        with refused("arrived at the site"):
            ew.raw_custody(cur, w, "vehicle", "site")
        ew.raw_trip_event(cur, w, "arrived_site")
        ew.raw_custody(cur, w, "vehicle", "site")


def test_hub_receipt_needs_arrival_and_a_field_receiver(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        ew.raw_custody(cur, w, "site", "vehicle")
        ew.raw_trip_event(cur, w, "departed_site")
        with refused("arrived at the hub"):
            ew.raw_custody(cur, w, "vehicle", "hub", received_by="P-3")
        ew.raw_trip_event(cur, w, "arrived_hub")
        with refused("receiver must hold"):
            ew.raw_custody(cur, w, "vehicle", "hub", received_by="P-4")
        with refused("needs a receiver"):
            ew.raw_custody(cur, w, "vehicle", "hub", received_by=None)
        ew.raw_custody(cur, w, "vehicle", "hub", received_by="P-3")  # any field user may receive at hub
        assert custody(cur, w.run.robot_id) == ("hub", True)


def test_site_to_vehicle_refused_after_departed_site(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
    iid = ew.raw_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        ew.raw_disposition(cur, w, "robot_left_on_site", incident_id=iid)
        ew.raw_trip_event(cur, w, "departed_site")  # allowed by the disposition, robot still at site
        with refused("already departed"):
            ew.raw_custody(cur, w, "site", "vehicle")


def test_hub_to_vehicle_only_from_the_departure(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        ew.raw_custody(cur, w, "site", "vehicle")
        ew.raw_trip_event(cur, w, "departed_site")
        ew.raw_trip_event(cur, w, "arrived_hub")
        ew.raw_custody(cur, w, "vehicle", "hub", received_by="P-3")
        with refused("only by recording the departure"):
            ew.raw_custody(cur, w, "hub", "vehicle", basis="administrative", received_by=None)
        with refused("cannot move from hub to site"):
            ew.raw_custody(cur, w, "hub", "site")


def test_no_custody_move_on_a_closed_run(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        ew.to_hub(cur, w)
        ew.close_run(cur, w)
        with refused("closed"):
            ew.raw_custody(cur, w, "hub", "vehicle", basis="administrative", received_by=None)
        with refused("closed"):
            ew.raw_verify(cur, w)
        with refused("closed"):
            ew.raw_trip_event(cur, w, "arrived_hub", seq=4)
        with refused("only an unaccounted report"):
            ew.raw_custody(cur, w, "hub", "vehicle", run=False)


def test_unaccounted_needs_a_same_transaction_robot_unaccounted_incident(w, app_role_conn):
    fall = ew.raw_incident(app_role_conn, w)  # an earlier, committed incident of another type
    lost_earlier = ew.raw_incident(app_role_conn, w, event_type="robot_unaccounted")
    with app_role_conn.cursor() as cur:
        with refused("same transaction"):
            ew.raw_custody(
                cur, w, "vehicle", "unaccounted", custodian=None, received_by=None, incident_id=fall
            )
        with refused("same transaction"):
            ew.raw_custody(
                cur, w, "vehicle", "unaccounted", custodian=None, received_by=None, incident_id=lost_earlier
            )
        with pytest.raises(CHECK):  # no incident at all: the column CHECK
            ew.raw_custody(cur, w, "vehicle", "unaccounted", custodian=None, received_by=None)
    with refused("custodian"), app_role_conn.transaction(), app_role_conn.cursor() as cur:
        code = ew._code(cur, "execution_incident_code_seq", "IC")
        iid = ew.incident_row(
            cur, w, code, ew.raw_hold(cur, w, source_ref=code), event_type="robot_unaccounted"
        )
        ew.raw_custody(cur, w, "vehicle", "unaccounted", custodian="P-1", received_by=None, incident_id=iid)
    ew.raw_incident(app_role_conn, w, unaccounted=True)  # control: report_incident's order, one transaction
    with app_role_conn.cursor() as cur:
        assert custody(cur, w.run.robot_id) == ("unaccounted", False)
        with refused("open run"):  # an unaccounted robot is never "at hub"
            cur.execute("SELECT fleet_fail(execution_robot_departure_problem(%s))", [w.run.robot_id])


def test_recovered_needs_ops_lead_receiver_note_and_the_incident(w, app_role_conn):
    iid = ew.raw_incident(app_role_conn, w, unaccounted=True)
    other = ew.raw_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        args = {
            "basis": "recovered",
            "custodian": "P-1",
            "received_by": "P-2",
            "incident_id": iid,
            "note": "SYNTHETIC: found by crew",
        }
        with refused("ops_lead"):
            ew.raw_custody(cur, w, "unaccounted", "vehicle", **args, by=w.field)
        with refused("needs a note"):
            ew.raw_custody(cur, w, "unaccounted", "vehicle", **{**args, "note": " "})
        with refused("needs a receiver"):
            ew.raw_custody(cur, w, "unaccounted", "vehicle", **{**args, "received_by": None})
        with refused("receiver must hold"):
            ew.raw_custody(cur, w, "unaccounted", "vehicle", **{**args, "received_by": "P-4"})
        with refused("incident that made the robot unaccounted"):
            ew.raw_custody(cur, w, "unaccounted", "vehicle", **{**args, "incident_id": other})
        with refused("recovery"):
            ew.raw_custody(cur, w, "unaccounted", "vehicle", **{**args, "basis": "handoff"})
        ew.raw_custody(cur, w, "unaccounted", "vehicle", **args)
        assert custody(cur, w.run.robot_id) == ("vehicle", True)
    assert Hold.objects.get(incident__pk=iid).released_at is None  # recovery never touches the hold


def test_no_run_unaccounted_report_for_a_robot_without_runs(w, app_role_conn):
    r2 = Robot.objects.get(code="R-02")
    ew.raw_incident(app_role_conn, w, unaccounted=True, robot_id=r2.pk)  # control: allowed with no run
    with app_role_conn.cursor() as cur:
        assert custody(cur, r2.pk) == ("unaccounted", False)
    ev = CustodyEvent.objects.get(robot=r2)
    assert (ev.run_id, ev.from_kind, ev.seq) == (None, "hub", 1)


# ================================================================== trip events and site checks
def test_trip_events_in_order_and_departed_site_needs_vehicle_custody(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        with refused("aborted outbound"):  # arrived_hub first needs VERIFIED vehicle custody (0003)
            ew.raw_trip_event(cur, w, "arrived_hub")
        with refused("one at a time"):
            ew.raw_trip_event(cur, w, "arrived_site", seq=2)
        ew.to_site(cur, w)
        with refused("verified vehicle custody"):
            ew.raw_trip_event(cur, w, "departed_site")
        ew.raw_custody(cur, w, "site", "vehicle")
        ew.raw_trip_event(cur, w, "departed_site")
        with refused("one at a time"):  # a second departed_site
            ew.raw_trip_event(cur, w, "departed_site")


def test_site_check_result_and_consent_count_are_recomputed(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
        ew.raw_trip_event(cur, w, "arrived_site")
        with refused("must be no_go: the robot is not in site custody"):
            ew.raw_site_check(cur, w)
        ew.raw_custody(cur, w, "vehicle", "site")
        with refused("must be no_go: a condition is not ok"):
            ew.raw_site_check(cur, w, conditions={"c1": "ok", "c2": "not_ok"})
        with refused("must be no_go: 3 adults present, 2 valid consents"):
            ew.raw_site_check(cur, w, adults=3)
        with refused("consent_count must be 2"):
            ew.raw_site_check(cur, w, consent_count=5)
        with refused("exactly the template"):
            ew.raw_site_check(cur, w, conditions={"c1": "ok"})
        with refused("verifier must be field crew"):
            ew.raw_site_check(cur, w, verifier="P-3")
        with refused("must be go"):
            ew.raw_site_check(cur, w, result="no_go")
        ew.raw_site_check(cur, w, conditions={"c1": "ok", "c2": "not_ok"}, result="no_go")
        ew.raw_site_check(cur, w)


def test_synthetic_template_cannot_produce_go_in_production(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
    set_setting("demo_mode", "false")
    with app_role_conn.cursor() as cur:
        with refused("synthetic_demo template"):
            ew.raw_site_check(cur, w)
        ew.raw_site_check(cur, w, result="no_go")
        real = SiteCheckTemplate.objects.create(
            version="SC-AP", items=ew.TEMPLATE_ITEMS, source="approved_procedure", recorded_by=w.ops
        )
        ew.raw_site_check(cur, w, template_id=real.pk)


def test_unknown_expected_adults_blocks_go(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
    with _owner_conn() as owner, owner.cursor() as cur:  # the planning command refuses unknown; set it raw
        cur.execute("UPDATE planning_site SET expected_adults = NULL WHERE id = %s", [w.run.visit.site_id])
    with app_role_conn.cursor() as cur:
        with refused("expected number of adults"):
            ew.raw_site_check(cur, w)
        ew.raw_site_check(cur, w, result="no_go")


# ================================================================== sessions (section 3 gates)
def test_session_start_gates(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        nogo = ew.raw_site_check(cur, w, conditions={"c1": "not_ok", "c2": "ok"}, result="no_go")
        with refused("no_go"):
            ew.raw_session(cur, w, "start", site_check_id=nogo)
        old = ew.raw_site_check(cur, w)
        new = ew.raw_site_check(cur, w)
        with refused("stale site check"):
            ew.raw_session(cur, w, "start", site_check_id=old)
        with pytest.raises(CHECK):  # start without a check: column CHECK
            ew.raw_session(cur, w, "start")
        with refused("field crew"):
            ew.raw_session(cur, w, "start", site_check_id=new, by=w.field2)  # field user, not crew
        i = ew.raw_interruption(cur, w)
        with refused("open interruption"):
            ew.raw_session(cur, w, "start", site_check_id=new)
        ew.raw_resolve(cur, w, i)
        new = ew.raw_site_check(cur, w)
        ew.raw_session(cur, w, "start", site_check_id=new, by=w.field)  # control: crew field user
        with refused("cannot start: session is active"):
            ew.raw_session(cur, w, "start", site_check_id=new)


def test_session_start_refused_while_the_robot_is_held(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        sc = at_site_with_go(cur, w)
        ew.raw_hold(cur, w, source_ref="", source_type="manual")
        with refused("blocking hold"):
            ew.raw_session(cur, w, "start", site_check_id=sc)
        ew.raw_session(cur, w, "observation", note="held: start refused, observations still allowed")


def test_session_start_refused_while_the_pattern_is_held(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        sc = at_site_with_go(cur, w)
        cur.execute(
            "INSERT INTO fleet_hold (code, scope, robot_id, pattern_id, blocking, source_type, source_ref, "
            "reason, policy_id, required_slots, manifest_at_placement_id, placed_by_id, placed_at) "
            "VALUES ('HD-PAT', 'manifest_pattern', NULL, %s, true, 'manual', '', 'SYN', NULL, '[]', NULL, "
            "%s, now())",
            [w.robot.current_manifest.pattern_id, w.ops.pk],
        )
        with refused("blocking hold"):
            ew.raw_session(cur, w, "start", site_check_id=sc)


def test_session_start_refused_when_custody_left_the_site(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        sc = at_site_with_go(cur, w)
        ew.raw_custody(cur, w, "site", "vehicle")
        with refused("verified site custody"):
            ew.raw_session(cur, w, "start", site_check_id=sc)


def test_consent_withdrawn_after_the_check_blocks_start(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        sc = at_site_with_go(cur, w)
    c = ConsentRecord.objects.filter(site_id=w.run.visit.site_id, withdrawn_at__isnull=True).first()
    w.p.withdraw_consent(w.field, consent_id=c.pk, reason="SYNTHETIC withdrawal")
    with app_role_conn.cursor() as cur:
        with refused("only 1 valid consents for 2 adults"):
            ew.raw_session(cur, w, "start", site_check_id=sc)
        one = ew.raw_site_check(cur, w, adults=1)
        ew.raw_session(cur, w, "start", site_check_id=one)


def test_resume_needs_a_go_check_newer_than_pause_and_interruptions(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        sc = at_site_with_go(cur, w)
        ew.raw_session(cur, w, "start", site_check_id=sc)
        with refused("cannot resume: session is active"):
            ew.raw_session(cur, w, "resume", site_check_id=sc)
        i = ew.raw_interruption(cur, w)
        ew.raw_session(cur, w, "pause", interruption_id=i)
        ew.raw_resolve(cur, w, i)
        with refused("stale site check"):
            ew.raw_session(cur, w, "resume", site_check_id=sc)
        fresh = ew.raw_site_check(cur, w)
        ew.raw_session(cur, w, "observation", note="SYNTHETIC note")
        with refused("needs a note"):
            ew.raw_session(cur, w, "observation")
        ew.raw_session(cur, w, "resume", site_check_id=fresh)
        ew.raw_session(cur, w, "stop")
        with refused("cannot start: session is stopped"):
            ew.raw_session(cur, w, "start", site_check_id=fresh)
        with refused("cannot stop: session is stopped"):
            ew.raw_session(cur, w, "stop")
        ew.raw_session(cur, w, "observation", note="after stop")  # observation after stop, run open
        with refused("sequence"):
            ew.raw_session(cur, w, "observation", note="x", seq=99)


# ================================================================== incidents and holds
NEW_HOLD = object()


def _incident_in_txn(app_role_conn, w, hold_kw=None, hold_from_earlier=NEW_HOLD, **incident_kw):
    """One transaction: draw a code, place a hold (customised) unless one is given, insert the incident."""
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        code = ew._code(cur, "execution_incident_code_seq", "IC")
        hold = (
            ew.raw_hold(cur, w, source_ref=code, **(hold_kw or {}))
            if hold_from_earlier is NEW_HOLD
            else hold_from_earlier
        )
        return ew.incident_row(cur, w, code, hold, **incident_kw)


def test_incident_needs_its_own_same_transaction_hold(w, app_role_conn):
    with refused("must place its own blocking robot hold"):
        _incident_in_txn(app_role_conn, w, hold_from_earlier=None)  # NULL hold
    with app_role_conn.cursor() as cur:  # a hold committed earlier, with the code the incident will use
        code = ew._code(cur, "execution_incident_code_seq", "IC")
        earlier = ew.raw_hold(cur, w, source_ref=code)
    with refused("same transaction"), app_role_conn.transaction(), app_role_conn.cursor() as cur:
        ew.incident_row(cur, w, code, earlier)
    r2 = Robot.objects.get(code="R-02")
    for kw, _why in (
        ({"blocking": False}, "non-blocking"),
        ({"source_type": "manual"}, "non-incident"),
        ({"robot_id": r2.pk, "manifest_at_placement_id": r2.current_manifest_id}, "other robot"),
    ):
        with refused("must place its own blocking robot hold"):
            _incident_in_txn(app_role_conn, w, hold_kw=kw)
    pattern_id = w.robot.current_manifest.pattern_id
    with refused("must place its own blocking robot hold"):
        _incident_in_txn(
            app_role_conn,
            w,
            hold_kw={
                "scope": "manifest_pattern",
                "robot_id": None,
                "pattern_id": pattern_id,
                "manifest_at_placement_id": None,
            },
        )
    iid = ew.raw_incident(app_role_conn, w)  # control
    inc = Incident.objects.get(pk=iid)
    with refused("must place its own blocking robot hold"):  # a second incident on the same hold
        _incident_in_txn(app_role_conn, w, hold_from_earlier=inc.hold_id)
    assert Incident.objects.count() == 1 and inc.run_id == w.run.pk and inc.visit_id == w.run.visit_id


def test_incident_on_a_released_hold_is_refused(w, app_role_conn):
    with (
        refused("must place its own blocking robot hold"),
        app_role_conn.transaction(),
        app_role_conn.cursor() as cur,
    ):
        code = ew._code(cur, "execution_incident_code_seq", "IC")
        hold = ew.raw_hold(cur, w, source_ref=code)
        cur.execute("SELECT required_slots FROM fleet_hold WHERE id = %s", [hold])
        signers = {
            "eng_review_1": (w.eng, "engineer"),
            "eng_review_2": (w.eng2, "engineer"),
            "safety": (w.safety, "safety_reviewer"),
            "founder": (w.founder, "founder"),
        }
        for s in cur.fetchone()[0]:
            user, role = signers[s["slot"]]
            cur.execute(
                "INSERT INTO fleet_releaseapproval (hold_id, slot, approver_id, role_used, manifest_id, "
                "evidence_refs, note, approved_at) VALUES (%s, %s, %s, %s, %s, '[]', '', now())",
                [hold, s["slot"], user.pk, role, w.robot.current_manifest_id],
            )
        cur.execute(
            "UPDATE fleet_hold SET released_at = now(), released_by_id = %s WHERE id = %s", [w.ops.pk, hold]
        )
        ew.incident_row(cur, w, code, hold)
    assert not Incident.objects.exists()


def test_incident_run_manifest_and_reporter_are_checked(w, app_role_conn):
    with refused("open run"):
        _incident_in_txn(app_role_conn, w, run_id=None, visit_id=None)
    with refused("current configuration"):
        _incident_in_txn(app_role_conn, w, manifest_id=None)
    with refused("operational role"):
        _incident_in_txn(app_role_conn, w, recorded_by_id=make_user("xd_nobody").pk)
    with refused("occurred_at"):
        _incident_in_txn(app_role_conn, w, occurred_at="2999-01-01T00:00Z")
    _incident_in_txn(app_role_conn, w, recorded_by_id=w.eng.pk)  # control: an engineer may report


def test_interruption_resolution_refused_while_the_incident_hold_is_unreleased(w, app_role_conn):
    iid = ew.raw_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        i = ew.raw_interruption(cur, w, source="incident", incident_id=iid, by=w.eng)
        with refused("not released"):
            ew.raw_resolve(cur, w, i)
    ew.release_hold(w, Incident.objects.get(pk=iid).hold_id)
    with app_role_conn.cursor() as cur:
        with refused("requires ops_lead"):
            ew.raw_resolve(cur, w, i, by=w.field)
        ew.raw_resolve(cur, w, i)


def test_continue_visit_refused_with_a_hold_or_an_open_interruption(w, app_role_conn):
    iid = ew.raw_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        with refused("unreleased blocking hold"):
            ew.raw_disposition(cur, w, "continue_visit")
    ew.release_hold(w, Incident.objects.get(pk=iid).hold_id)
    with app_role_conn.cursor() as cur:
        i = ew.raw_interruption(cur, w)
        with refused("open interruption"):
            ew.raw_disposition(cur, w, "continue_visit")
        ew.raw_resolve(cur, w, i)
        with refused("requires an ops_lead"):
            ew.raw_disposition(cur, w, "continue_visit", by=w.field)
        with pytest.raises(CHECK):  # robot_left_on_site needs an incident (column CHECK)
            ew.raw_disposition(cur, w, "robot_left_on_site")
        with refused("robot_unaccounted incident"):
            ew.raw_disposition(cur, w, "robot_unaccounted", incident_id=iid)
        ew.raw_disposition(cur, w, "continue_visit")


# ================================================================== reconciliation and closure
def _returned(cur, w) -> int:
    ew.to_site(cur, w)
    return ew.to_hub(cur, w)


def test_reconciled_needs_every_condition(w, app_role_conn):
    early_inspection = ew.post_visit_inspection(w)  # submitted BEFORE the hub receipt
    with app_role_conn.cursor() as cur:
        hub = _returned(cur, w)
    insp = ew.post_visit_inspection(w)
    with app_role_conn.cursor() as cur:
        with refused("post-visit inspection"):
            ew.raw_reconcile(cur, w, hub_receipt_id=hub)
        with refused("post-visit inspection"):
            ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=early_inspection)
        with refused("latest custody is not the hub receipt"):
            ew.raw_reconcile(cur, w, hub_receipt_id=None, inspection_id=insp)
        with refused("discrepancies"):
            ew.raw_reconcile(
                cur, w, hub_receipt_id=hub, inspection_id=insp, discrepancies=["SYN charger missing"]
            )
        with refused("cargo_matches must be True"):
            ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp, cargo_matches=False)
        with refused("requires an ops_lead"):
            ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp, by=w.field)
        ew.raw_reconcile(cur, w, outcome="unresolved", discrepancies=["SYN"])  # unresolved: anything goes
        ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp)  # control


def test_reconciled_refused_when_the_robot_is_not_back(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
    iid = ew.raw_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        ew.raw_disposition(cur, w, "robot_left_on_site", incident_id=iid)
        ew.raw_trip_event(cur, w, "departed_site")
        ew.raw_trip_event(cur, w, "arrived_hub")  # the vehicle returned, the robot did not
        insp = ew.post_visit_inspection(w)
        with refused("moves the robot to the hub"):
            ew.raw_reconcile(cur, w, hub_receipt_id=ew.latest_custody(w).pk, inspection_id=insp)
        with refused("latest custody is not the hub receipt"):
            ew.raw_reconcile(cur, w, hub_receipt_id=None, inspection_id=insp)
        rec = ew.raw_reconcile(cur, w, outcome="unresolved", discrepancies=["robot left on site"])
        with refused("unresolved"):
            ew.raw_close(cur, w, rec)
        assert custody(cur, w.run.robot_id) == ("site", True)


def test_reconciled_refused_with_an_unverified_custody_event(w, app_role_conn):
    """A robot that was unaccounted and recovered: the unaccounted event is a handoff, the recovery is verified;
    an unverified administrative assignment, however, can never reach the hub (moves need verification)."""
    with app_role_conn.cursor() as cur:
        ew.raw_trip_event(cur, w, "arrived_site")
        with refused("not verified"):
            ew.raw_custody(cur, w, "vehicle", "site")


def test_closure_conditions(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        ew.raw_custody(cur, w, "site", "vehicle")
        ew.raw_trip_event(cur, w, "departed_site")
        rec0 = ew.raw_reconcile(cur, w, outcome="unresolved", discrepancies=["in transit"])
        with refused("not arrived at the hub"):
            ew.raw_close(cur, w, rec0)
        ew.raw_trip_event(cur, w, "arrived_hub")
        hub = ew.raw_custody(cur, w, "vehicle", "hub", received_by="P-3")
    insp = ew.post_visit_inspection(w)
    with app_role_conn.cursor() as cur:
        good = ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp)
        later = ew.raw_reconcile(cur, w, outcome="unresolved", discrepancies=["second look"])
        with refused("latest reconciliation"):
            ew.raw_close(cur, w, good)
        with refused("unresolved"):
            ew.raw_close(cur, w, later)
        good2 = ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp)
        with refused("requires an ops_lead"):
            ew.raw_close(cur, w, good2, by=w.field)
        ew.raw_close(cur, w, good2)  # control
        with refused("closed"):  # no reconciliation after closure, of any outcome
            ew.raw_reconcile(cur, w, outcome="unresolved", discrepancies=["late"])
        with refused("closed"):
            ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp)
        with refused("closed"):
            ew.raw_session(cur, w, "observation", note="late")
    assert TripClosure.objects.filter(run=w.run, reconciliation_id=good2).exists()
    assert ReturnReconciliation.objects.filter(run=w.run).count() == 4


def test_a_held_robots_trip_can_close_and_a_late_incident_still_holds(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        hub = _returned(cur, w)
    iid = ew.raw_incident(app_role_conn, w)  # held while the run is open
    insp = ew.post_visit_inspection(w)
    with app_role_conn.cursor() as cur:
        ew.raw_close(cur, w, ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp))
    late = ew.raw_incident(app_role_conn, w)  # after closure: no run, still a hold
    inc = Incident.objects.get(pk=late)
    assert (
        inc.run_id is None
        and inc.hold.released_at is None
        and Incident.objects.get(pk=iid).run_id == w.run.pk
    )


# ================================================================== severity, status, escalation
def _new_incident(app_role_conn, w, by=None) -> int:
    return ew.raw_incident(app_role_conn, w, recorded_by_id=(by or w.ops).pk)


def _severity(cur, iid, level, policy_id, by) -> int:
    return ew.raw_insert(
        cur,
        "execution_severitydecision",
        incident_id=iid,
        level=level,
        policy_id=policy_id,
        reason="SYNTHETIC",
        recorded_by_id=by.pk,
    )


def test_severity_needs_an_active_usable_policy(w, app_role_conn):
    iid = _new_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:
        draft = _policy(cur, w, approve=False)
        with refused("no active incident policy"):
            _severity(cur, iid, "low", draft, w.lead)
        pid = _policy(cur, w)
        with refused("not in the policy's severity scale"):
            _severity(cur, iid, "catastrophic", pid, w.lead)
        with refused("requires engineering_lead or safety_reviewer"):
            _severity(cur, iid, "low", pid, w.ops)
        with refused("active incident policy"):
            _severity(cur, iid, "low", draft, w.lead)
        _severity(cur, iid, "low", pid, w.lead)  # control
    set_setting("demo_mode", "false")
    with app_role_conn.cursor() as cur, refused("synthetic_demo incident policy"):
        _severity(cur, iid, "medium", pid, w.lead)
    with app_role_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO execution_incidentpolicywithdrawal (policy_id, withdrawn_by_id, reason, recorded_at) "
            "VALUES (%s, %s, 'withdrawn', NULL)",
            [pid, w.lead.pk],
        )
    set_setting("demo_mode", "true")
    with app_role_conn.cursor() as cur, refused("no active incident policy"):  # no fallback to an older one
        _severity(cur, iid, "medium", pid, w.lead)


def test_severity_downgrade_rules(w, app_role_conn):
    safety2 = make_user("xd_safety2", "safety_reviewer")
    iid = _new_incident(app_role_conn, w, by=w.safety)  # the reporter is a safety reviewer
    with app_role_conn.cursor() as cur:
        pid = _policy(cur, w)
        _severity(cur, iid, "high", pid, w.lead)
        _severity(cur, iid, "high", pid, w.lead)  # same level: not a downgrade
        with refused("downgrade_roles"):
            _severity(cur, iid, "low", pid, w.lead)  # engineering_lead is not in downgrade_roles
        with refused("reporter"):
            _severity(cur, iid, "low", pid, w.safety)
        _severity(cur, iid, "low", pid, safety2)  # control
        _severity(cur, iid, "medium", pid, w.lead)  # upgrade: no restriction


def test_policy_lifecycle_rules(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        with refused("consecutive"):
            ew.raw_insert(
                cur,
                "execution_incidentpolicy",
                version=7,
                rules=RULES,
                source="synthetic_demo",
                notes="",
                lock_version=1,
                recorded_by_id=w.lead.pk,
            )
        with refused("escalation_required_from"):
            _policy(cur, w, rules={**RULES, "escalation_required_from": "extreme"})
        with refused("engineering_lead"):
            _policy(cur, w, by=w.safety)
        pid = _policy(cur, w, approve=False)
        with refused("author"):
            cur.execute(
                "INSERT INTO execution_incidentpolicyapproval (policy_id, slot, approver_id, recorded_at) "
                "VALUES (%s, 'engineering', %s, NULL)",
                [pid, w.lead.pk],
            )
        with refused("requires role safety_reviewer"):
            cur.execute(
                "INSERT INTO execution_incidentpolicyapproval (policy_id, slot, approver_id, recorded_at) "
                "VALUES (%s, 'safety', %s, NULL)",
                [pid, w.lead2.pk],
            )
        cur.execute(
            "INSERT INTO execution_incidentpolicyapproval (policy_id, slot, approver_id, recorded_at) "
            "VALUES (%s, 'engineering', %s, NULL)",
            [pid, w.lead2.pk],
        )
        with refused("immutable"):  # UPDATE stays granted (lock target); the guard refuses it
            cur.execute("UPDATE execution_incidentpolicy SET lock_version = 9 WHERE id = %s", [pid])
        with pytest.raises(DENIED):
            cur.execute("DELETE FROM execution_incidentpolicy WHERE id = %s", [pid])
    assert IncidentPolicy.objects.get(pk=pid).lock_version == 2  # bumped by the approval


def test_status_steps_escalations_and_closing(w, app_role_conn):
    iid = _new_incident(app_role_conn, w)
    with app_role_conn.cursor() as cur:

        def status(to, by=w.ops):
            return ew.raw_insert(
                cur, "execution_incidentstatuschange", incident_id=iid, to_status=to, recorded_by_id=by.pk
            )

        with refused("one step at a time"):
            status("resolved")
        with refused("requires ops_lead"):
            status("triaged", by=w.field)
        status("triaged")
        status("response_in_progress")
        status("resolved")
        pid = _policy(cur, w)
        _severity(cur, iid, "high", pid, w.lead)
        with refused("requires at least one escalation"):
            status("closed", by=w.lead)
        p5 = ew.person("P-5")  # lead2, engineering_lead
        with refused("does not hold the target role"):
            ew.raw_insert(
                cur,
                "execution_incidentescalation",
                incident_id=iid,
                to_role="safety_reviewer",
                to_person_id=p5.pk,
                requested_action="review",
                recorded_by_id=w.ops.pk,
            )
        e = ew.raw_insert(
            cur,
            "execution_incidentescalation",
            incident_id=iid,
            to_role="engineering_lead",
            to_person_id=p5.pk,
            requested_action="review",
            recorded_by_id=w.ops.pk,
        )
        with refused("acknowledged"):
            status("closed", by=w.lead)
        with refused("only the named person"):
            ew.raw_insert(cur, "execution_escalationack", escalation_id=e, acknowledged_by_id=w.lead.pk)
        with refused("must hold role"):
            ew.raw_insert(cur, "execution_escalationack", escalation_id=e, acknowledged_by_id=w.safety.pk)
        ew.raw_insert(cur, "execution_escalationack", escalation_id=e, acknowledged_by_id=w.lead2.pk)
        with refused("closing an incident requires"):
            status("closed", by=w.ops)
        status("closed", by=w.lead)
        with refused("closed"):
            ew.raw_insert(
                cur,
                "execution_incidentescalation",
                incident_id=iid,
                to_role="founder",
                to_person_id=None,
                requested_action="late",
                recorded_by_id=w.ops.pk,
            )
    assert Incident.objects.get(pk=iid).hold.released_at is None  # closing never releases the hold


# ================================================================== C1: a departed visit has happened
def test_departed_visit_cannot_be_cancelled_replanned_or_released(w, app_role_conn):
    v = Visit.objects.get(code=w.visit)
    first_rev = v.current_revision_id
    n_revisions = VisitRevision.objects.filter(visit=v).count()
    res = Reservation.objects.filter(revision_id=first_rev, released_at__isnull=True).first()
    trip = VehicleTrip.objects.get(revision_id=first_rev)
    with app_role_conn.cursor() as cur:
        for sql, args, match in (
            (
                "UPDATE planning_visit SET status = 'cancelled', cancelled_reason = 'raw' WHERE id = %s",
                [v.pk],
                "departed",
            ),
            ("UPDATE planning_visit SET status = 'planned' WHERE id = %s", [v.pk], "departed"),
            (
                "UPDATE planning_reservation SET released_at = now(), released_reason = 'raw' WHERE id = %s",
                [res.pk],
                "reservations can never be released",
            ),
            (
                "UPDATE planning_vehicletrip SET released_at = now() WHERE id = %s",
                [trip.pk],
                "vehicle trip can never be released",
            ),
        ):
            with refused(match):
                cur.execute(sql, args)
        with refused("departed"):
            cur.execute(
                "INSERT INTO planning_visitrevision (visit_id, revision_no, on_site_start, on_site_end, travel_out_min, "
                "travel_back_min, tasks, required_consent_scopes, robot_id, lead_id, second_id, vehicle_id, "
                "feasibility, blockers, snapshot, reason, created_by_id, created_at) SELECT visit_id, revision_no + 1, "
                "on_site_start, on_site_end, travel_out_min, travel_back_min, tasks, required_consent_scopes, robot_id, "
                "lead_id, second_id, vehicle_id, feasibility, blockers, snapshot, 'raw', created_by_id, now() "
                "FROM planning_visitrevision WHERE id = %s",
                [first_rev],
            )
    with pytest.raises(Conflict, match="departed"):  # the command path (_release_active) too
        w.p.cancel_visit(w.ops, visit_code=w.visit, reason="SYNTHETIC")
    v.refresh_from_db()
    assert (v.status, v.current_revision_id) == ("confirmed", first_rev)
    # control: a visit that has NOT departed can still have its reservation and trip released, and be cancelled
    other = Visit.objects.get(code=w.new_confirmed_visit(robot="R-02", h0=15, h1=16))
    ores = Reservation.objects.filter(revision_id=other.current_revision_id, released_at__isnull=True).first()
    with app_role_conn.cursor() as cur:
        cur.execute(
            "UPDATE planning_reservation SET released_at = now(), released_reason = 'raw' WHERE id = %s",
            [ores.pk],
        )
    w.p.cancel_visit(w.ops, visit_code=other.code, reason="SYNTHETIC")
    assert Visit.objects.get(pk=other.pk).status == "cancelled"
    assert VisitRevision.objects.filter(visit=v).count() == n_revisions


# ================================================================== C3: robot at hub, no open run
def _raw_authorization_sql(w, visit_code: str) -> tuple[str, list]:
    v = Visit.objects.get(code=visit_code)
    rev = v.current_revision
    robot = Robot.objects.get(pk=rev.robot_id)
    snap = take_guard_snapshot(v)
    policy_id = snap["policy_id"]
    ev_ids = list(
        ReadinessEvidence.objects.filter(visit=v, revision=rev, void__isnull=True).values_list(
            "pk", flat=True
        )
    )
    plan_id = v.recovery_plans.order_by("-pk").first().pk
    sql = (
        "INSERT INTO readiness_departureauthorization (code, visit_id, revision_id, robot_id, manifest_id, "
        "policy_id, mode, guard_snapshot, evidence_ids, recovery_plan_id, issued_by_id, issued_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'demo', %s, %s, %s, %s, now(), clock_timestamp() + interval '30 minutes') "
        "RETURNING id"
    )
    return sql, [
        f"DA-C3{v.pk}",
        v.pk,
        rev.pk,
        robot.pk,
        robot.current_manifest_id,
        policy_id,
        Jsonb(snap),
        Jsonb(ev_ids),
        plan_id,
        w.ops.pk,
    ]


def _second_visit(w, robot: str) -> str:
    code = w.new_confirmed_visit(robot=robot, d=NEXT_DAY)
    w.record_all_evidence(code, robot=robot)
    return code


def test_authorization_refused_while_the_robot_has_an_open_run(w):
    v2 = _second_visit(w, "R-01")
    with pytest.raises(Conflict, match="open run"):
        rc.authorize_departure(w.ops, visit_code=v2)  # through readiness_issue_authorization
    sql, args = _raw_authorization_sql(w, v2)
    with _owner_conn() as owner, owner.cursor() as cur, refused("open run"):
        cur.execute(sql, args)  # the table trigger refuses even the owner's insert
    assert not DepartureAuthorization.objects.filter(visit__code=v2).exists()


def test_departure_refused_while_the_robot_has_an_open_run(transactional_db, app_role_conn):
    w = build_readiness(prefix="xc_")
    v2 = _second_visit(w, "R-01")
    a1 = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    a2 = DepartureAuthorization.objects.get(code=rc.authorize_departure(w.ops, visit_code=v2)["code"])
    rc.record_departure(w.ops, authorization_code=a1)  # opens a run for R-01
    with app_role_conn.cursor() as cur, refused("open run"):
        cur.execute(
            "INSERT INTO readiness_departureevent (authorization_id, visit_id, robot_id, recorded_by_id, "
            "guard_snapshot) VALUES (%s, %s, %s, %s, %s)",
            [a2.pk, a2.visit_id, a2.robot_id, w.ops.pk, Jsonb(a2.guard_snapshot)],
        )
    with pytest.raises(Conflict, match="open run|robot"):
        rc.record_departure(w.ops, authorization_code=a2.code)
    assert DepartureEvent.objects.count() == 1 and ExecutionRun.objects.count() == 1


def test_robot_not_at_hub_refused_and_baseline_hub_allowed(w, app_role_conn):
    v3 = _second_visit(w, "R-02")  # R-02 has never run: baseline hub
    a3 = DepartureAuthorization.objects.get(
        code=rc.authorize_departure(w.ops, visit_code=v3)["code"]
    )  # control
    r2 = Robot.objects.get(code="R-02")
    ew.raw_incident(app_role_conn, w, unaccounted=True, robot_id=r2.pk)  # no run: custody -> unaccounted
    with app_role_conn.cursor() as cur, refused("not at hub"):
        cur.execute(
            "INSERT INTO readiness_departureevent (authorization_id, visit_id, robot_id, recorded_by_id, "
            "guard_snapshot) VALUES (%s, %s, %s, %s, %s)",
            [a3.pk, a3.visit_id, a3.robot_id, w.ops.pk, Jsonb(a3.guard_snapshot)],
        )
    sql, args = _raw_authorization_sql(w, v3)
    with _owner_conn() as owner, owner.cursor() as cur, refused("not at hub"):
        cur.execute(sql, args)


def test_after_closure_the_robot_is_at_hub_and_may_depart_again(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        ew.to_hub(cur, w)
        ew.close_run(cur, w)
        assert custody(cur, w.run.robot_id) == ("hub", True)
    v2 = _second_visit(w, "R-01")
    run2 = ew.depart(w, v2)  # control: a verified hub receipt and no open run
    ev = CustodyEvent.objects.get(run=run2)
    assert (ev.seq, ev.from_kind, ev.to_kind) == (5, "hub", "vehicle")
    assert SessionEvent.objects.filter(run=run2).count() == 0


# ================================================================== 0003 (a): recovery with no open run
def _closed_then_lost(w, app_role_conn) -> tuple[str, int]:
    """Close w.run, plan + evidence a next-day visit for the same robot, then report it unaccounted (no run)."""
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        ew.to_hub(cur, w)
        ew.close_run(cur, w)
    v2 = _second_visit(w, "R-01")
    iid = ew.raw_incident(app_role_conn, w, unaccounted=True)
    assert Incident.objects.get(pk=iid).run_id is None
    return v2, iid


def test_no_run_recovery_to_hub_after_a_closed_run(w, app_role_conn):
    v2, iid = _closed_then_lost(w, app_role_conn)
    other = ew.raw_incident(app_role_conn, w)  # a later, unrelated incident on the same robot
    rid = w.run.robot_id
    with app_role_conn.cursor() as cur:
        with refused("ops_lead"):
            ew.raw_recover_to_hub(cur, w, iid, by=w.field)
        with refused("incident that made the robot unaccounted"):
            ew.raw_recover_to_hub(cur, w, other)
        with refused("incident that made the robot unaccounted"):
            ew.raw_recover_to_hub(cur, w, None)
        with refused("receiver must hold"):
            ew.raw_recover_to_hub(cur, w, iid, received_by="P-4")
        with refused("needs a receiver"):
            ew.raw_recover_to_hub(cur, w, iid, received_by=None)
        with refused("needs a note"):
            ew.raw_recover_to_hub(cur, w, iid, note="  ")
        with refused("only an unaccounted report or a recovery to hub"):
            ew.raw_custody(
                cur, w, "unaccounted", "vehicle", basis="recovered", incident_id=iid, note="x", run=False
            )
        with refused("recovery"):
            ew.raw_custody(
                cur, w, "unaccounted", "hub", basis="handoff", incident_id=iid, note="x", run=False
            )
        ew.raw_recover_to_hub(cur, w, iid)  # control
        assert custody(cur, rid) == ("hub", True)
        cur.execute("SELECT execution_robot_departure_problem(%s)", [rid])
        assert cur.fetchone()[0] is None  # C3 custody satisfied: verified hub, no open run
        with refused("custody cannot move from hub to hub|wrong custody origin|custody_moves"):
            ew.raw_recover_to_hub(cur, w, iid)  # not unaccounted any more
    assert ExecutionRun.objects.filter(robot_id=rid).count() == 1  # no run opened or reopened
    assert TripClosure.objects.filter(run=w.run).exists()
    inc = Incident.objects.get(pk=iid)
    assert inc.hold.released_at is None and inc.status_changes.count() == 0  # incident + hold untouched
    # ... and the hold still blocks readiness: evaluator blocker, authorization refused
    blockers = {b["code"] for b in evaluate_readiness(Visit.objects.get(code=v2)).blockers}
    assert "PLANNING_ROBOT_NOT_ELIGIBLE" in blockers, blockers
    with pytest.raises(Conflict):
        rc.authorize_departure(w.ops, visit_code=v2)
    assert not DepartureAuthorization.objects.filter(visit__code=v2).exists()


def test_no_run_recovery_refused_while_the_robot_has_an_open_run(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
    iid = ew.raw_incident(app_role_conn, w, unaccounted=True)  # bound to the open run
    with app_role_conn.cursor() as cur:
        with refused("open run must reference that run"):
            ew.raw_recover_to_hub(cur, w, iid)
        ew.raw_custody(
            cur, w, "unaccounted", "vehicle", basis="recovered", incident_id=iid, note="SYNTHETIC found"
        )  # control: the in-run recovery path is unchanged
    assert ExecutionRun.objects.count() == 1


def _recovery_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=False,
    )


def test_recovery_first_then_authorize_waits_and_is_still_refused_by_the_hold(w, app_role_conn):
    v2, iid = _closed_then_lost(w, app_role_conn)
    out: dict = {}
    rec = _recovery_conn()
    try:
        with rec.cursor() as cur:
            ew.raw_recover_to_hub(cur, w, iid)  # robot FOR UPDATE held, uncommitted

            def authorize():
                try:
                    out["result"] = rc.authorize_departure(w.ops, visit_code=v2)
                except Exception as e:  # noqa: BLE001 - asserted below
                    out["error"] = e
                finally:
                    connection.close()

            t = threading.Thread(target=authorize)
            t.start()
            time.sleep(0.6)
            assert t.is_alive(), out  # authorize waits on the robot row
            rec.commit()
            t.join(30)
    finally:
        rec.close()
    assert isinstance(out.get("error"), Conflict), out
    assert "PLANNING_ROBOT_NOT_ELIGIBLE" in str(out["error"]) or "hold" in str(out["error"]), out
    with app_role_conn.cursor() as cur:
        assert custody(cur, w.run.robot_id) == ("hub", True)
    assert not DepartureAuthorization.objects.filter(visit__code=v2).exists()


def test_authorize_first_then_recovery_waits_and_authorization_never_bypasses(w, app_role_conn):
    v2, iid = _closed_then_lost(w, app_role_conn)

    def recover():
        with _recovery_conn() as conn, conn.cursor() as cur:
            rid = ew.raw_recover_to_hub(cur, w, iid)
            conn.commit()
            return rid

    blocked, first, second = Interleaving("locked:readiness.authorize_departure").run(
        lambda: rc.authorize_departure(w.ops, visit_code=v2), recover
    )
    assert blocked, "the recovery must wait for authorize's robot lock"
    assert isinstance(first, Conflict), first  # unaccounted + held at authorize time
    assert isinstance(second, int), second  # the recovery then commits
    assert not DepartureAuthorization.objects.filter(visit__code=v2).exists()
    with pytest.raises(Conflict):  # and afterwards the hold still refuses it
        rc.authorize_departure(w.ops, visit_code=v2)


# ================================================================== 0003 (b): aborted outbound
def test_aborted_outbound_needs_verified_vehicle_custody(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        with refused("aborted outbound"):
            ew.raw_trip_event(cur, w, "arrived_hub")  # departure assignment not verified
        ew.raw_verify(cur, w)
        ew.raw_trip_event(cur, w, "arrived_hub")  # control
        for kind in ("arrived_site", "departed_site", "arrived_hub"):
            with refused("already arrived at the hub"):
                ew.raw_trip_event(cur, w, kind)
        with refused("arrived at the site"):  # the robot can no longer be handed to the site
            ew.raw_custody(cur, w, "vehicle", "site")


def test_aborted_outbound_returns_and_the_trip_closes(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
        ew.raw_trip_event(cur, w, "arrived_hub")
        hub = ew.raw_custody(cur, w, "vehicle", "hub", received_by="P-3")
    insp = ew.post_visit_inspection(w)
    with app_role_conn.cursor() as cur:
        rec = ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp)
        ew.raw_close(cur, w, rec)
        assert custody(cur, w.run.robot_id) == ("hub", True)
        cur.execute("SELECT execution_robot_departure_problem(%s)", [w.run.robot_id])
        assert cur.fetchone()[0] is None


def test_aborted_outbound_refused_for_an_unaccounted_robot_and_reconciliation_stays_unresolved(
    w, app_role_conn
):
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
    iid = ew.raw_incident(app_role_conn, w, unaccounted=True)
    with app_role_conn.cursor() as cur:
        ew.raw_disposition(cur, w, "robot_unaccounted", incident_id=iid)
        ew.raw_trip_event(cur, w, "departed_site")
        ew.raw_trip_event(cur, w, "arrived_hub")  # the vehicle is back, the robot is not
        with refused("latest custody is not the hub receipt"):
            ew.raw_reconcile(cur, w, inspection_id=None)
        ew.raw_reconcile(cur, w, outcome="unresolved", discrepancies=["robot unaccounted"])


# ================================================================== incident code / blank source_ref
def test_incident_code_must_be_a_real_code_so_blank_holds_never_qualify(w, app_role_conn):
    with refused("IC-NNNN"), app_role_conn.transaction(), app_role_conn.cursor() as cur:
        hold = ew.raw_hold(cur, w, source_ref="")  # a blank manual-style incident hold
        ew.incident_row(cur, w, "", hold)
    with (
        refused("must place its own blocking robot hold"),
        app_role_conn.transaction(),
        app_role_conn.cursor() as cur,
    ):
        code = ew._code(cur, "execution_incident_code_seq", "IC")
        hold = ew.raw_hold(cur, w, source_ref="")
        ew.incident_row(cur, w, code, hold)
    assert not Incident.objects.exists()


# ================================================================== 0004 (1): unaccounted during outbound
def _lost_outbound(w, app_role_conn) -> int:
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
    return ew.raw_incident(app_role_conn, w, unaccounted=True)  # custody -> unaccounted on this run


def test_unaccounted_outbound_arrived_hub_then_recovery_and_closure(w, app_role_conn):
    iid = _lost_outbound(w, app_role_conn)
    n_custody = CustodyEvent.objects.count()
    with app_role_conn.cursor() as cur:
        ew.raw_trip_event(cur, w, "arrived_hub")  # the vehicle returns without its robot
        assert CustodyEvent.objects.count() == n_custody  # no custody row: not a robot hub receipt
        assert custody(cur, w.run.robot_id) == ("unaccounted", False)
        for kind in ("arrived_site", "departed_site"):
            with refused("already arrived at the hub"):
                ew.raw_trip_event(cur, w, kind)
    insp = ew.post_visit_inspection(w)
    with app_role_conn.cursor() as cur:
        with refused("latest custody is not the hub receipt"):
            ew.raw_reconcile(cur, w, hub_receipt_id=None, inspection_id=insp)
        rec = ew.raw_reconcile(cur, w, outcome="unresolved", discrepancies=["robot unaccounted"])
        with refused("unresolved"):
            ew.raw_close(cur, w, rec)
        hub = ew.raw_custody(
            cur,
            w,
            "unaccounted",
            "hub",
            basis="recovered",
            incident_id=iid,
            received_by="P-3",
            note="SYNTHETIC: robot recovered and received at hub",
        )  # recovery on the still-open run
        assert custody(cur, w.run.robot_id) == ("hub", True)
    insp = ew.post_visit_inspection(w)  # after the hub receipt
    with app_role_conn.cursor() as cur:
        ew.raw_close(cur, w, ew.raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=insp))
    assert TripClosure.objects.filter(run=w.run).exists()
    assert Incident.objects.get(pk=iid).hold.released_at is None  # the hold stays


def test_unaccounted_outbound_refused_after_a_prior_trip_event(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
        ew.raw_trip_event(cur, w, "arrived_site")
    ew.raw_incident(app_role_conn, w, unaccounted=True)
    with app_role_conn.cursor() as cur:
        with refused("one at a time"):
            ew.raw_trip_event(cur, w, "arrived_hub")


def test_unaccounted_outbound_refused_without_a_prior_unaccounted_event(w, app_role_conn):
    ew.raw_incident(app_role_conn, w, event_type="robot_unaccounted")  # incident but no custody move
    with app_role_conn.cursor() as cur:
        with refused("unaccounted on this run"):
            ew.raw_trip_event(cur, w, "arrived_hub")  # custody: unverified vehicle


def _forge_unaccounted(w, incident_id: int, run_id) -> None:
    """Owner-only test setup (the trusted administrator may disable its own triggers): append an unaccounted
    custody event that the triggers would never accept, to prove the trip trigger checks it independently."""
    with _owner_conn() as owner, owner.cursor() as cur:
        cur.execute("ALTER TABLE execution_custodyevent DISABLE TRIGGER execution_custody_guard")
        try:
            cur.execute(
                "INSERT INTO execution_custodyevent (robot_id, seq, from_kind, to_kind, run_id, trip_id, visit_id, "
                "custodian_id, received_by_id, basis, incident_id, note, recorded_by_id, recorded_at) "
                "VALUES (%s, %s, 'vehicle', 'unaccounted', %s, %s, %s, NULL, NULL, 'handoff', %s, '', %s, now())",
                [
                    w.run.robot_id,
                    ew.next_custody_seq(w.run.robot_id),
                    run_id,
                    w.run.trip_id if run_id else None,
                    w.run.visit_id if run_id else None,
                    incident_id,
                    w.ops.pk,
                ],
            )
        finally:
            cur.execute("ALTER TABLE execution_custodyevent ENABLE TRIGGER execution_custody_guard")


def test_unaccounted_outbound_refused_when_the_unaccounted_event_is_not_this_runs(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
    lost = ew.raw_incident(app_role_conn, w, event_type="robot_unaccounted")
    _forge_unaccounted(w, lost, run_id=None)  # an unaccounted event of no run / another run
    with app_role_conn.cursor() as cur:
        assert custody(cur, w.run.robot_id)[0] == "unaccounted"
        with refused("unaccounted on this run"):
            ew.raw_trip_event(cur, w, "arrived_hub")


def test_unaccounted_outbound_refused_without_a_robot_unaccounted_incident(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        ew.raw_verify(cur, w)
    fall = ew.raw_incident(app_role_conn, w)  # event_type fall
    _forge_unaccounted(w, fall, run_id=w.run.pk)
    with app_role_conn.cursor() as cur, refused("unaccounted on this run"):
        ew.raw_trip_event(cur, w, "arrived_hub")


# ================================================================== 0004 (2): incident policy withdrawal
def test_only_an_engineering_lead_may_withdraw_an_incident_policy(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        pid = _policy(cur, w)
        sql = (
            "INSERT INTO execution_incidentpolicywithdrawal (policy_id, withdrawn_by_id, reason, recorded_at) "
            "VALUES (%s, %s, 'pulled', NULL)"
        )
        with refused("active engineering_lead"):
            cur.execute(sql, [pid, w.safety.pk])
        cur.execute(sql, [pid, w.lead2.pk])  # control
        cur.execute("SELECT execution_active_incident_policy_id()")
        assert cur.fetchone()[0] is None
