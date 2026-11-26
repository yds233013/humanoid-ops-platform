"""v2 03C-2 (EM review of 03C): the full boundary of the interruption-only protective fallback.

A protective stop or pause that the session state does not allow (the session has not started in the server's
state) records an operator interruption instead (contract 10.D, 14.2). This file proves what that interruption
guarantees until someone with ``execution.triage_incident`` resolves it:

1. the invalid stop creates the protective state, or keeps an existing one (no duplicate);
2. session START and RESUME are blocked through the services, the field endpoint (class A: ONLINE_ONLY), the
   online views and the database trigger (execution_session_guard on execution_sessionevent, direct SQL as
   ops_app);
3. no other command puts the robot to work: site handover, a new departure on another run (the robot cannot
   leave on a new run while the interruption is unresolved: execution migration 0010), recovery, and
   continue_visit; resolving needs triage_incident;
4. an existing incident hold stays exactly as it was;
5. the answer says effect ``protective_state`` and never "stopped".

SYNTHETIC data only.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from execution import field_commands as fc
from execution import logistics as lg
from execution import reconciliation as recon
from execution import sessions as ses
from execution import site_checks as sc
from execution.custody import current_custody, departure_problem
from execution.incidents import session_state
from execution.models import ExecutionRun, Interruption, SessionEvent
from fleet.models import Hold
from platform_core.commands import Conflict, PermissionDenied
from readiness import commands as rc
from readiness.models import DepartureAuthorization, DepartureEvent
from tests import execution_world as ew
from tests.test_execution_db import NEXT_DAY, _owner_conn, _raw_authorization_sql
from tests.test_execution_ui import _issue

pytestmark = pytest.mark.django_db(transaction=True)

INTERRUPTION_GONE = "the run has an open interruption"


@pytest.fixture
def w(transactional_db: None) -> Any:
    return ew.build_execution(prefix="fp_")


# ------------------------------------------------------------------ shorthands
def protective(w: Any, command: str, payload: dict[str, Any] | None = None, by: Any = None) -> dict[str, Any]:
    return fc.apply(
        by or w.field,
        w.run,
        command,
        payload or {},
        channel="protective",
        causal={"last_decided_seq": 1, "outcome": "confirmed"},
    )


def verify_and_arrive(w: Any) -> None:
    lg.verify_custody(w.field, run_code=w.run.code, custody_seq=1, verifier_code="P-2")
    lg.record_trip_event(w.field, run_code=w.run.code, kind="arrived_site")


def handover(w: Any) -> dict[str, Any]:
    return lg.transfer_custody(
        w.field, run_code=w.run.code, to_kind="site", custodian_code="P-1", received_by_code="P-2"
    )


def go_check(w: Any) -> int:
    r = sc.record_site_check(
        w.field,
        run_code=w.run.code,
        template_version="SC1",
        adults_present=2,
        conditions=dict(ew.ALL_OK),
        verifier_code="P-1",
    )
    assert r["result"] == "go", r
    return int(r["id"])


def at_site_with_go_check(w: Any) -> int:
    verify_and_arrive(w)
    handover(w)
    return go_check(w)


def fallback_stop(w: Any) -> dict[str, Any]:
    assert session_state(w.run.pk) == "not_started"
    r = protective(w, "session.stop", {"note": "SYNTHETIC: crew asked to stop"})
    assert r["effect"] == "protective_state", r
    return r


def raw_session_sql(app_role_conn: psycopg.Connection, w: Any, kind: str, check_id: int) -> None:
    with app_role_conn.cursor() as cur:
        ew.raw_session(cur, w, kind, site_check_id=check_id, by=w.field)


def view_post(user: Any, url: str, data: dict[str, Any]) -> str:
    c = Client()
    c.force_login(user)
    resp = c.post(url, {**data, "_form_token": _issue(user)}, follow=True)
    assert resp.status_code == 200
    return resp.content.decode()


def sync(user: Any, w: Any, command: str, payload: dict[str, Any]) -> dict[str, Any]:
    c = Client()
    c.force_login(user)
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        now = cur.fetchone()[0]
    body = {
        "device_id": str(uuid.uuid4()),
        "run": w.run.code,
        "generation": 1,
        "unsent_count": 0,
        "commands": [
            {
                "key": str(uuid.uuid4()),
                "channel": "ordinary",
                "seq": 1,
                "last_decided_seq": None,
                "command": command,
                "payload": payload,
                "device_observed_at": now.isoformat(),
            }
        ],
    }
    resp = c.post("/api/field/sync", json.dumps(body), content_type="application/json")
    assert resp.status_code == 200, resp.content
    result: dict[str, Any] = resp.json()["results"][0]
    return result


def hold_row(hold_id: int) -> dict[str, Any]:
    return dict(Hold.objects.filter(pk=hold_id).values().get())


def starts(w: Any) -> int:
    return SessionEvent.objects.filter(run=w.run, kind__in=["start", "resume"]).count()


# ================================================================== 1 and 5: create or keep, never "stopped"
def test_an_invalid_stop_creates_the_protective_state_once_and_never_says_stopped(w):
    verify_and_arrive(w)
    r = fallback_stop(w)
    assert r["interruption_created"] is True and r["session_state"] == "not started"
    assert "stopped" not in r["effect"]
    assert "was not stopped" in r["message"] and "Session of run" not in r["message"]
    it = Interruption.objects.get(run=w.run)
    assert it.code == r["interruption"] and it.source == "operator" and it.recorded_by == w.field
    assert not SessionEvent.objects.filter(run=w.run).exists()  # no stop (and no event at all) was recorded

    # repeats, by stop or pause and by another crew member, keep the one protective state
    for command, by in (("session.stop", w.field), ("session.pause", w.ops), ("session.stop", w.ops)):
        again = protective(w, command, {"note": "again"}, by=by)
        assert again["effect"] == "protective_state" and again["interruption_created"] is False
        assert again["interruption"] == it.code and "Nothing new was recorded" in again["message"]
    assert Interruption.objects.filter(run=w.run).count() == 1

    # through the field endpoint too: confirmed, effect protective_state, message never claims stopped
    c = Client()
    c.force_login(w.field)
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        now = cur.fetchone()[0]
    body = {
        "device_id": str(uuid.uuid4()),
        "run": w.run.code,
        "generation": 1,
        "unsent_count": 0,
        "commands": [
            {
                "key": str(uuid.uuid4()),
                "channel": "protective",
                "seq": None,
                "last_decided_seq": None,
                "command": "session.stop",
                "payload": {},
                "device_observed_at": now.isoformat(),
            }
        ],
    }
    res = c.post("/api/field/sync", json.dumps(body), content_type="application/json").json()["results"][0]
    assert res["outcome"] == "confirmed" and res["result"]["effect"] == "protective_state", res
    assert "was not stopped" in res["result"]["message"]
    assert Interruption.objects.filter(run=w.run).count() == 1


def test_an_existing_incident_interruption_is_kept_not_duplicated(w):
    verify_and_arrive(w)
    inc = protective(w, "incident.report", {
        "event_type": "fall", "description": "SYNTHETIC: tipped", "knowledge_source": "witnessed",
    })  # fmt: skip
    hold_id = Hold.objects.get(code=inc["record"]["hold"]).pk
    before = hold_row(hold_id)
    r = fallback_stop(w)
    assert r["interruption_created"] is False and r["interruption"] == inc["record"]["interruption"]
    assert r["holds"] == [before["code"]]
    assert Interruption.objects.filter(run=w.run).count() == 1
    assert hold_row(hold_id) == before  # 4: the incident hold is untouched


# ================================================================== 2: start and resume blocked everywhere
def test_start_is_blocked_by_service_field_view_and_database_until_resolved(w, app_role_conn):
    check = at_site_with_go_check(w)
    it = Interruption.objects.get(code=fallback_stop(w)["interruption"])

    with pytest.raises(Conflict, match="open interruption"):  # service
        ses.record_session_event(w.field, run_code=w.run.code, kind="start", site_check_id=check)
    res = sync(w.field, w, "session.start", {"site_check_id": check})  # field endpoint
    assert (res["outcome"], res["reason_code"]) == ("rejected", "ONLINE_ONLY")
    html = view_post(
        w.field, f"/execution/runs/{w.run.code}/session/", {"kind": "start", "site_check_id": check}
    )
    assert "open interruption" in html and "Session event recorded: start" not in html  # online view
    with pytest.raises(psycopg.errors.CheckViolation, match=INTERRUPTION_GONE):  # database, as ops_app
        raw_session_sql(app_role_conn, w, "start", check)
    assert starts(w) == 0 and session_state(w.run.pk) == "not_started"

    # resolving needs triage_incident: crew field operators are refused, by the service and the database
    with pytest.raises(PermissionDenied):
        ses.resolve_interruption(w.field, code=it.code, note="SYNTHETIC: I think it is fine")
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation, match="resolving"):
        ew.raw_resolve(cur, w, it.pk, by=w.field)
    ses.resolve_interruption(
        w.ops, code=it.code, note="SYNTHETIC: ops lead checked the site"
    )  # ops_lead triages

    # control: the very same SQL insert is accepted once the interruption is resolved
    raw_session_sql(app_role_conn, w, "start", check)
    assert session_state(w.run.pk) == "active"


def test_resume_is_blocked_by_service_field_view_and_database_while_an_interruption_is_open(w, app_role_conn):
    """The fallback only arises before a start, so the session can never be paused under it; a protective
    interruption opened on an active session is the case where resume matters, with a Go check NEWER than the
    interruption so the open interruption is the only thing in the way."""
    check = at_site_with_go_check(w)
    ses.record_session_event(w.field, run_code=w.run.code, kind="start", site_check_id=check)
    r = protective(w, "interruption.open", {"reason": "SYNTHETIC: dog loose"})
    assert r["record"]["session_paused"] is True
    it = Interruption.objects.get(code=r["record"]["code"])
    fresh = go_check(w)

    with pytest.raises(Conflict, match="open interruption"):
        ses.record_session_event(w.field, run_code=w.run.code, kind="resume", site_check_id=fresh)
    res = sync(w.field, w, "session.resume", {"site_check_id": fresh})
    assert (res["outcome"], res["reason_code"]) == ("rejected", "ONLINE_ONLY")
    html = view_post(
        w.field, f"/execution/runs/{w.run.code}/session/", {"kind": "resume", "site_check_id": fresh}
    )
    assert "open interruption" in html and "Session event recorded: resume" not in html
    with pytest.raises(psycopg.errors.CheckViolation, match=INTERRUPTION_GONE):
        raw_session_sql(app_role_conn, w, "resume", fresh)
    assert session_state(w.run.pk) == "paused"

    # and a resume under the not-started fallback is refused by the state machine as well
    ses.resolve_interruption(w.ops, code=it.code, note="SYNTHETIC: dog gone")
    raw_session_sql(app_role_conn, w, "resume", fresh)  # control: accepted once resolved
    assert session_state(w.run.pk) == "active"


def test_resume_under_the_not_started_fallback_is_refused_too(w, app_role_conn):
    check = at_site_with_go_check(w)
    fallback_stop(w)
    with pytest.raises(Conflict, match="Cannot resume"):
        ses.record_session_event(w.field, run_code=w.run.code, kind="resume", site_check_id=check)
    with pytest.raises(psycopg.errors.CheckViolation, match="cannot resume"):
        raw_session_sql(app_role_conn, w, "resume", check)
    assert starts(w) == 0


# ================================================================== 3: no other path puts the robot to work
def test_site_handover_does_not_enable_work(w, app_role_conn):
    verify_and_arrive(w)
    fallback_stop(w)  # in the vehicle at the site
    res = sync(w.field, w, "custody.handover_to_site", {})
    assert (res["outcome"], res["reason_code"]) == ("rejected", "ONLINE_ONLY")
    handover(w)  # the online handover records the custody fact only
    assert current_custody(w.robot.pk).kind == "site"
    check = go_check(w)
    with pytest.raises(Conflict, match="open interruption"):
        ses.record_session_event(w.field, run_code=w.run.code, kind="start", site_check_id=check)
    with pytest.raises(psycopg.errors.CheckViolation, match=INTERRUPTION_GONE):
        raw_session_sql(app_role_conn, w, "start", check)
    assert starts(w) == 0


def test_continue_visit_is_refused_while_the_interruption_is_open(w):
    at_site_with_go_check(w)
    fallback_stop(w)
    with pytest.raises(Conflict, match="open interruption"):
        ses.record_disposition(w.ops, run_code=w.run.code, decision="continue_visit", reason="SYNTHETIC")


def _bring_back_and_close(w: Any) -> None:
    lg.transfer_custody(
        w.field, run_code=w.run.code, to_kind="vehicle", custodian_code="P-1", received_by_code="P-2"
    )
    lg.record_trip_event(w.field, run_code=w.run.code, kind="departed_site")
    lg.record_trip_event(w.field, run_code=w.run.code, kind="arrived_hub")
    lg.transfer_custody(
        w.field, run_code=w.run.code, to_kind="hub", custodian_code="P-1", received_by_code="P-3"
    )
    recon.reconcile_return(
        w.ops, run_code=w.run.code, inspection_code=ew.record_inspection(w, w.visit, robot=w.robot.code)
    )
    recon.close_trip(w.ops, run_code=w.run.code)


def test_a_new_departure_on_another_run_is_refused_while_the_interruption_is_unresolved(w):
    """The run can be brought back and closed (the return is a fact), but the robot cannot leave on a new run
    until the interruption is resolved: authorization, departure and readiness all refuse it."""
    at_site_with_go_check(w)
    it = Interruption.objects.get(code=fallback_stop(w)["interruption"])
    _bring_back_and_close(w)
    v2 = w.new_confirmed_visit(robot=w.robot.code, d=NEXT_DAY)
    w.record_all_evidence(v2, robot=w.robot.code)
    problem = departure_problem(w.robot.pk) or ""
    assert it.code in problem and "unresolved interruption" in problem
    with pytest.raises(Conflict, match="unresolved interruption"):
        rc.authorize_departure(w.ops, visit_code=v2)
    res = sync(w.field, w, "departure.record", {})
    assert (res["outcome"], res["reason_code"]) == ("rejected", "ONLINE_ONLY"), res
    assert not DepartureAuthorization.objects.filter(visit__code=v2).exists()
    assert ExecutionRun.objects.count() == 1

    ses.resolve_interruption(w.ops, code=it.code, note="SYNTHETIC: reviewed after the return")
    assert departure_problem(w.robot.pk) is None
    auth = rc.authorize_departure(w.ops, visit_code=v2)["code"]  # control: allowed once resolved
    rc.record_departure(w.ops, authorization_code=auth)
    assert DepartureEvent.objects.filter(visit__code=v2).exists()


def test_the_database_refuses_the_new_authorization_even_for_the_schema_owner(w):
    """The rule is in execution_robot_departure_problem, which the authorization trigger runs on every insert:
    a direct INSERT by the schema owner is refused too. An authorization cannot predate the interruption (it
    needs a closed run, and a closed run takes no new interruption), so this is the only way in."""
    at_site_with_go_check(w)
    it = Interruption.objects.get(code=fallback_stop(w)["interruption"])
    _bring_back_and_close(w)
    v2 = w.new_confirmed_visit(robot=w.robot.code, d=NEXT_DAY)
    w.record_all_evidence(v2, robot=w.robot.code)
    sql, args = _raw_authorization_sql(w, v2)
    with _owner_conn() as owner, owner.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match=f"unresolved interruption \\({it.code}"):
            cur.execute(sql, args)
    assert not DepartureAuthorization.objects.filter(visit__code=v2).exists()
    # and no interruption can be added to the closed run afterwards, so the set only shrinks
    with pytest.raises(Conflict, match="closed"):
        ses.open_interruption(w.field, run_code=w.run.code, reason="SYNTHETIC: late")


def test_recovery_does_not_enable_work_and_keeps_every_hold(w, app_role_conn):
    """Recovery moves custody only. With the robot lost on this run, recover_robot refuses (open run), the
    ops lead's transfer from unaccounted records custody, and the fallback interruption still blocks the start
    after the incident's own hold and interruption are cleared through their workflows."""
    at_site_with_go_check(w)
    fallback = Interruption.objects.get(code=fallback_stop(w)["interruption"])
    lost = protective(w, "incident.report", {
        "event_type": "robot_unaccounted", "description": "SYNTHETIC: lost", "knowledge_source": "witnessed",
    })  # fmt: skip
    hold = Hold.objects.get(code=lost["record"]["hold"])
    before = hold_row(hold.pk)
    res = sync(w.ops, w, "robot.recover", {})
    assert (res["outcome"], res["reason_code"]) == ("rejected", "ONLINE_ONLY")
    with pytest.raises(Conflict, match="open run"):
        lg.recover_robot(w.ops, robot_code=w.robot.code, received_by_code="P-2", note="SYNTHETIC: found")
    lg.transfer_custody(
        w.ops, run_code=w.run.code, to_kind="site", custodian_code="P-1", received_by_code="P-2",
        note="SYNTHETIC: found behind the shed",
    )  # fmt: skip
    assert hold_row(hold.pk) == before  # 4: the fallback and the recovery leave the hold exactly as it was

    ew.release_hold(w, hold.pk)  # the incident hold through its own workflow
    incident_it = Interruption.objects.get(incident__hold=hold)
    ses.resolve_interruption(w.ops, code=incident_it.code, note="SYNTHETIC: incident handled")
    check = go_check(w)
    with pytest.raises(Conflict, match="open interruption"):  # the fallback interruption alone still blocks
        ses.record_session_event(w.field, run_code=w.run.code, kind="start", site_check_id=check)
    with pytest.raises(psycopg.errors.CheckViolation, match=INTERRUPTION_GONE):
        raw_session_sql(app_role_conn, w, "start", check)
    assert list(Interruption.objects.filter(run=w.run, resolution__isnull=True)) == [fallback]


def test_the_fallback_never_writes_or_alters_a_hold(w):
    verify_and_arrive(w)
    inc = protective(w, "incident.report", {
        "event_type": "fall", "description": "SYNTHETIC: tipped", "knowledge_source": "witnessed",
    })  # fmt: skip
    hold = Hold.objects.get(code=inc["record"]["hold"])
    snapshot = {h.pk: hold_row(h.pk) for h in Hold.objects.all()}
    t0 = timezone.now()
    for command in ("session.stop", "session.pause", "session.stop"):
        assert protective(w, command)["effect"] == "protective_state"
    assert {h.pk: hold_row(h.pk) for h in Hold.objects.all()} == snapshot
    assert not Hold.objects.filter(placed_at__gte=t0).exists()
    assert hold.released_at is None
