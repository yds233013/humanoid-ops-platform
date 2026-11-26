"""v2 03C-2: field command classification, domain adapters and the protective fallback.

docs/13_v2_phase03_contract.md sections 3 (I-2, I-3), 10.D and 14 (14.8 wins). execution.field_commands.apply runs
the EXISTING services (through run_command at the run's hub, inside a savepoint), maps their refusals to stable
codes, maps infrastructure failures to Transient, and never creates a second incident, interruption or hold for a
duplicate protective report. The world is built through the real commands (tests/execution_world.py).
SYNTHETIC data only.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.db import DatabaseError, OperationalError, connection, transaction
from django.utils import timezone

from accounts import services as account_services
from execution import field_commands as fc
from execution import logistics as lg
from execution import sessions as ses
from execution import site_checks as sc
from execution.custody import current_custody
from execution.incidents import session_state
from execution.models import (
    CustodyEvent,
    CustodyVerification,
    Incident,
    Interruption,
    SessionEvent,
    SiteCheck,
    TripEvent,
)
from fleet.models import Hold
from platform_core import workspace as ws
from platform_core.commands import Conflict, RetryTransaction
from platform_core.models import AuditEvent, Hub
from readiness.models import DepartureEvent
from tests import execution_world as ew
from tests.conftest import make_user
from tests.planning_world import TZ
from tests.test_execution_db import _owner_conn

pytestmark = pytest.mark.django_db(transaction=True)

CONTRACT_NAMES = {
    "A": {"departure.record", "session.start", "session.resume", "custody.handover_to_site", "robot.recover"},
    "B": {"session.pause", "session.stop", "incident.report", "interruption.open"},
    "C": {
        "trip.arrived_site",
        "trip.departed_site",
        "trip.arrived_hub",
        "custody.back_to_vehicle",
        "custody.verify",
        "site_check.record",
        "session.observation",
    },
}


@pytest.fixture
def w(transactional_db: None) -> Any:
    return ew.build_execution(prefix="fc_")


# ------------------------------------------------------------------ shorthands
def ordinary(w: Any, command: str, payload: dict[str, Any] | None = None, by: Any = None) -> dict[str, Any]:
    return fc.apply(by or w.field, w.run, command, payload or {}, channel="ordinary", causal=None)


def protective(
    w: Any,
    command: str,
    payload: dict[str, Any] | None = None,
    by: Any = None,
    causal: dict[str, Any] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    return fc.apply(
        by or w.field,
        w.run,
        command,
        payload or {},
        channel="protective",
        causal=causal if causal is not None else {"last_decided_seq": 2, "outcome": "confirmed"},
        key=key,
    )


def refused(code: str, fn: Any, *args: Any, **kw: Any) -> fc.FieldRefusal:
    with pytest.raises(fc.FieldRefusal) as e:
        fn(*args, **kw)
    assert e.value.code == code, (e.value.code, e.value.message)
    return e.value


def incident_payload(**over: Any) -> dict[str, Any]:
    return {
        "event_type": "fall",
        "description": "SYNTHETIC: robot sat down unexpectedly",
        "knowledge_source": "witnessed",
        **over,
    }


def to_site(w: Any) -> None:
    """Verify the departure assignment, arrive, hand the robot over to the site (the handover is class A, so the
    test records it online through the service)."""
    lg.verify_custody(w.field, run_code=w.run.code, custody_seq=1, verifier_code="P-2")
    lg.record_trip_event(w.field, run_code=w.run.code, kind="arrived_site")
    lg.transfer_custody(
        w.field, run_code=w.run.code, to_kind="site", custodian_code="P-1", received_by_code="P-2"
    )


def start_session(w: Any) -> None:
    to_site(w)
    check = sc.record_site_check(
        w.field,
        run_code=w.run.code,
        template_version="SC1",
        adults_present=2,
        conditions=dict(ew.ALL_OK),
        verifier_code="P-1",
    )
    assert check["result"] == "go", check
    ses.record_session_event(w.field, run_code=w.run.code, kind="start", site_check_id=check["id"])
    assert session_state(w.run.pk) == "active"


def counts(w: Any) -> tuple[int, ...]:
    return (
        Incident.objects.filter(robot=w.robot).count(),
        Hold.objects.filter(robot=w.robot).count(),
        Interruption.objects.filter(run=w.run).count(),
        SessionEvent.objects.filter(run=w.run).count(),
        TripEvent.objects.filter(run=w.run).count(),
        CustodyEvent.objects.filter(robot=w.robot).count(),
    )


# ================================================================== classification (14.2)
def test_the_class_map_covers_the_contract_names_and_every_class_c_and_b_has_a_schema():
    for cls, names in CONTRACT_NAMES.items():
        for name in names:
            assert fc.CLASS[name] == cls, name
    assert set(fc.CLASS.values()) == {"A", "B", "C"}
    for name, cls in fc.CLASS.items():
        assert (name in fc.SCHEMAS) == (cls != "A"), name


def test_every_class_a_command_is_refused_online_only_and_nothing_is_written(w):
    before = (counts(w), DepartureEvent.objects.count(), AuditEvent.objects.count())
    names = [n for n, c in fc.CLASS.items() if c == "A"]
    assert CONTRACT_NAMES["A"] <= set(names)
    for name in names:
        for channel in ("ordinary", "protective"):
            # even with a payload claiming readiness or an authorization: nothing from the phone is read
            payload = {"authorization": "DA-0001", "ready": True, "site_check_id": 1}
            r = refused("ONLINE_ONLY", fc.apply, w.ops, w.run, name, payload, channel=channel, causal=None)
            assert r.message == "Needs a connection. Do not start."
    assert (counts(w), DepartureEvent.objects.count(), AuditEvent.objects.count()) == before


def test_unknown_commands_wrong_channels_and_unknown_fields_are_refused(w):
    refused("UNKNOWN_COMMAND", ordinary, w, "robot.teleport")
    for name, cls in fc.CLASS.items():
        if cls == "A":
            continue
        wrong = "ordinary" if cls == "B" else "protective"
        refused("WRONG_CHANNEL", fc.apply, w.field, w.run, name, {}, channel=wrong, causal=None)
    r = refused(
        "INVALID_PAYLOAD", ordinary, w, "trip.arrived_site", {"device_observed_at": "2026-11-27T09:41"}
    )
    assert "device_observed_at" in r.message
    refused("INVALID_PAYLOAD", ordinary, w, "custody.verify", {"custody_seq": True, "verifier": "P-2"})
    refused("INVALID_PAYLOAD", ordinary, w, "custody.verify", {"verifier": "P-2"})
    refused("INVALID_PAYLOAD", protective, w, "incident.report", incident_payload(occurred_at="yesterday"))
    refused("INVALID_PAYLOAD", protective, w, "session.stop", causal={"last_decided_seq": "three"})


# ================================================================== class C maps to its service and applies
def test_each_class_c_command_maps_to_its_service_and_applies(w):
    r = ordinary(w, "custody.verify", {"custody_seq": 1, "verifier": "P-2"})
    assert r["effect"] == "recorded" and r["class"] == "C" and r["record"]["verified_by"] == "P-2"
    assert CustodyVerification.objects.filter(custody_event__robot=w.robot).count() == 1

    r = ordinary(w, "trip.arrived_site")
    assert r["effect"] == "recorded" and r["record"]["kind"] == "arrived_site"
    lg.transfer_custody(
        w.field, run_code=w.run.code, to_kind="site", custodian_code="P-1", received_by_code="P-2"
    )

    r = ordinary(
        w,
        "site_check.record",
        {"template": "SC1", "adults_present": 2, "conditions": dict(ew.ALL_OK), "verifier": "P-1"},
    )
    assert r["effect"] == "recorded" and r["record"]["result"] == "go"
    check = SiteCheck.objects.get(pk=r["record"]["id"])
    assert check.recorded_by == w.field  # the session's actor, never a client-sent recorder

    ses.record_session_event(
        w.field, run_code=w.run.code, kind="start", site_check_id=check.pk
    )  # class A online
    r = ordinary(w, "session.observation", {"note": "SYNTHETIC: participants cheerful"})
    assert r["effect"] == "recorded" and r["record"]["kind"] == "observation"
    assert protective(w, "session.stop", {"note": "done"})["effect"] == "recorded"
    assert session_state(w.run.pk) == "stopped"

    r = ordinary(w, "custody.back_to_vehicle", {"custodian": "P-1", "received_by": "P-2"})
    assert r["effect"] == "recorded" and (r["record"]["from"], r["record"]["to"]) == ("site", "vehicle")
    assert ordinary(w, "trip.departed_site")["record"]["kind"] == "departed_site"
    assert ordinary(w, "trip.arrived_hub")["record"]["kind"] == "arrived_hub"

    assert list(TripEvent.objects.filter(run=w.run).order_by("seq").values_list("kind", flat=True)) == [
        "arrived_site",
        "departed_site",
        "arrived_hub",
    ]
    commands = set(AuditEvent.objects.filter(actor=w.field).values_list("command", flat=True))
    assert {
        "execution.verify_custody",
        "execution.record_trip_event",
        "execution.record_site_check",
        "execution.record_session_event",
        "execution.transfer_custody",
    } <= commands  # the existing commands ran (run_command's audit), not a parallel writer


def test_back_to_vehicle_is_never_a_recovery_or_another_move(w):
    """From unaccounted, a move to the vehicle would be a recovery (class A). It is refused, not bent."""
    to_site(w)
    protective(w, "incident.report", incident_payload(event_type="robot_unaccounted"))
    assert current_custody(w.robot.pk).kind == "unaccounted"
    before = counts(w)
    r = refused(
        "STATE_CONFLICT",
        ordinary,
        w,
        "custody.back_to_vehicle",
        {"custodian": "P-1", "received_by": "P-2"},
        w.ops,
    )
    assert "not at the site" in r.message
    assert counts(w) == before


# ================================================================== refusals keep a stable code
def test_service_and_trigger_refusals_become_field_refusals_with_stable_codes(w, monkeypatch):
    r = refused("STATE_CONFLICT", ordinary, w, "trip.departed_site")
    assert "arrived_site" in r.message  # the service's explained refusal, re-checked at apply time (I-3)
    refused("INVALID_PAYLOAD", ordinary, w, "site_check.record",
            {"template": "NOPE", "adults_present": 2, "conditions": {}, "verifier": "P-1"})  # fmt: skip

    ordinary(w, "custody.verify", {"custody_seq": 1, "verifier": "P-2"})
    ordinary(w, "trip.arrived_site")
    # Make the Python check blind so the INSERT reaches the database, whose rule refuses the second arrival.
    monkeypatch.setattr(lg, "trip_kinds", lambda run: [])
    before = counts(w)
    with pytest.raises(fc.FieldRefusal) as e:
        ordinary(w, "trip.arrived_site")
    assert e.value.code == "STATE_CONFLICT"
    assert fc._sqlstate(e.value.__cause__) in ("23514", "23505")  # the database refused it
    assert counts(w) == before
    assert TripEvent.objects.filter(run=w.run, kind="arrived_site").count() == 1


# ================================================================== another hub (03A-2)
@pytest.fixture
def outsider(w: Any) -> Any:
    admin = make_user("fc_admin", "admin")
    ws.create_hub(admin, code="HB", name="Hub B", area="", time_zone=TZ)
    user = make_user("fc_outsider")
    account_services.grant_role(admin, user_id=user.pk, role="field_operator", hub_code="HB")
    lg.verify_custody(w.field, run_code=w.run.code, custody_seq=1, verifier_code="P-2")
    return user


def test_a_field_operator_of_another_hub_is_refused_not_permitted(w, outsider, monkeypatch):
    hub = Hub.objects.exclude(code="HB").get()
    before = counts(w)
    for command, payload, send in (
        ("trip.arrived_site", {}, ordinary),
        ("session.observation", {"note": "SYNTHETIC"}, ordinary),
        ("session.pause", {}, protective),
        ("interruption.open", {"reason": "SYNTHETIC"}, protective),
        ("incident.report", incident_payload(), protective),
    ):
        r = refused("NOT_PERMITTED", send, w, command, payload, outsider)
        assert r.message.startswith("fc_outsider is not allowed") and hub.code in r.message, r.message
        assert AuditEvent.objects.filter(
            actor=outsider, command=f"field.{command}", outcome=AuditEvent.OUTCOME_DENIED
        ).exists()  # the denial is audited like an online one
    assert counts(w) == before

    # The database is the backstop: with the Python permission check bypassed, the 03A-2 trigger
    # (execution_trip_event_guard, field role AT the run's hub) refuses: "recording a trip event requires an
    # active ops_lead or field_operator role" (check_violation).
    monkeypatch.setattr("platform_core.commands.holds_permission_locked", lambda *a, **k: True)
    with pytest.raises(fc.FieldRefusal) as e:
        ordinary(w, "trip.arrived_site", by=outsider)
    assert e.value.code == "NOT_PERMITTED"
    assert fc._sqlstate(e.value.__cause__) == "23514" and "requires an active" in e.value.message
    assert counts(w) == before


# ================================================================== infrastructure errors are Transient (14.8.5)
def test_a_real_lock_timeout_is_transient_and_writes_nothing(w):
    assert not issubclass(fc.Transient, fc.FieldRefusal)
    ordinary(w, "custody.verify", {"custody_seq": 1, "verifier": "P-2"})
    before = (counts(w), AuditEvent.objects.count())
    holder = _owner_conn()
    try:
        holder.autocommit = False
        with holder.cursor() as cur:
            cur.execute("SELECT id FROM fleet_robot WHERE id = %s FOR UPDATE", [w.robot.pk])
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '150ms'")
            with pytest.raises(fc.Transient) as e:
                ordinary(w, "trip.arrived_site")
        assert isinstance(e.value.__cause__, OperationalError)
        assert fc._sqlstate(e.value.__cause__) == "55P03"
    finally:
        holder.rollback()
        holder.close()
    assert (counts(w), AuditEvent.objects.count()) == before
    assert ordinary(w, "trip.arrived_site")["effect"] == "recorded"  # a resend applies once


def _db_error(cls: type[Exception], psycopg_error: type[Exception]) -> Exception:
    err = cls("SYNTHETIC database failure")
    err.__cause__ = psycopg_error("SYNTHETIC")
    return err


@pytest.mark.parametrize(
    "make",
    [
        lambda: _db_error(OperationalError, psycopg.errors.DeadlockDetected),
        lambda: _db_error(OperationalError, psycopg.errors.SerializationFailure),
        lambda: _db_error(DatabaseError, psycopg.errors.DeadlockDetected),
        lambda: _db_error(OperationalError, psycopg.errors.QueryCanceled),
        lambda: OperationalError("server closed the connection unexpectedly"),
    ],
    ids=[
        "deadlock",
        "serialization",
        "deadlock-plain-database-error",
        "statement-timeout",
        "lost-connection",
    ],
)
def test_database_failures_are_transient_never_a_refusal(w, monkeypatch, make):
    def boom(*a: Any, **k: Any) -> Any:
        raise make()

    monkeypatch.setattr(lg, "record_trip_event", boom)
    monkeypatch.setattr(ses, "open_interruption", boom)
    with pytest.raises(fc.Transient):
        ordinary(w, "trip.arrived_site")
    with pytest.raises(fc.Transient):
        protective(w, "interruption.open", {"reason": "SYNTHETIC"})


def test_a_command_that_kept_being_retried_is_transient(w, monkeypatch):
    def changing(*a: Any, **k: Any) -> Any:
        try:
            raise RetryTransaction()
        except RetryTransaction:
            raise Conflict("The record kept changing while this command ran; try again") from None

    monkeypatch.setattr(lg, "record_trip_event", changing)
    with pytest.raises(fc.Transient):
        ordinary(w, "trip.arrived_site")


def test_an_unclassified_error_is_neither_a_refusal_nor_transient(w, monkeypatch):
    def bug(*a: Any, **k: Any) -> Any:
        raise KeyError("SYNTHETIC bug")

    monkeypatch.setattr(lg, "record_trip_event", bug)
    with pytest.raises(KeyError):  # the endpoint answers 5xx; nothing is stored
        ordinary(w, "trip.arrived_site")


# ================================================================== protective stop and pause (10.D)
def test_a_protective_stop_on_a_session_that_has_not_started_gives_protective_state_never_stopped(w):
    to_site(w)
    causal = {"last_decided_seq": 3, "outcome": "rejected"}
    r = protective(w, "session.stop", {"note": "SYNTHETIC: child ran in"}, causal=causal)
    assert r["effect"] == "protective_state"
    assert r["causal"] == causal  # kept as the device saw it, never turned into a confirmation
    assert "was not stopped" in r["message"] and "Session of run" not in r["message"]
    assert r["interruption_created"] is True
    assert session_state(w.run.pk) == "not_started"
    assert not SessionEvent.objects.filter(run=w.run, kind="stop").exists()
    it = Interruption.objects.get(run=w.run)
    assert it.code == r["interruption"] and it.recorded_by == w.field

    # the interruption blocks the start (class A, online) until it is resolved
    check = sc.record_site_check(
        w.field, run_code=w.run.code, template_version="SC1", adults_present=2,
        conditions=dict(ew.ALL_OK), verifier_code="P-1",
    )  # fmt: skip
    with pytest.raises(Conflict, match="open interruption"):
        ses.record_session_event(w.field, run_code=w.run.code, kind="start", site_check_id=check["id"])

    # a second stop or pause (another key) keeps the state: no second interruption
    for command in ("session.stop", "session.pause"):
        again = protective(w, command, {"note": "again"})
        assert again["effect"] == "protective_state" and again["interruption_created"] is False
        assert again["interruption"] == it.code
    assert Interruption.objects.filter(run=w.run).count() == 1


def test_stop_and_pause_on_an_active_session_apply_and_repeats_change_nothing(w):
    start_session(w)
    assert protective(w, "session.pause")["effect"] == "recorded"
    before = counts(w)
    again = protective(w, "session.pause")
    assert again["effect"] == "no_change" and "already paused" in again["message"]
    assert counts(w) == before

    r = protective(w, "session.stop")
    assert r["effect"] == "recorded" and r["record"]["state"] == "stopped"
    before = counts(w)
    for command in ("session.stop", "session.pause"):
        again = protective(w, command)
        assert again["effect"] == "no_change" and "already stopped" in again["message"]
    assert counts(w) == before
    assert SessionEvent.objects.filter(run=w.run, kind="stop").count() == 1


def test_protective_commands_are_still_rechecked_against_the_server_state(w):
    """A closed run is not a valid session: the protective stop is refused, not bent."""
    start_session(w)
    protective(w, "session.stop")
    lg.transfer_custody(
        w.field, run_code=w.run.code, to_kind="vehicle", custodian_code="P-1", received_by_code="P-2"
    )
    lg.record_trip_event(w.field, run_code=w.run.code, kind="departed_site")
    lg.record_trip_event(w.field, run_code=w.run.code, kind="arrived_hub")
    lg.transfer_custody(
        w.field, run_code=w.run.code, to_kind="hub", custodian_code="P-1", received_by_code="P-3"
    )
    from execution import reconciliation as rc

    rc.reconcile_return(
        w.ops, run_code=w.run.code, inspection_code=ew.record_inspection(w, w.visit, robot=w.robot.code),
        discrepancies=None,
    )  # fmt: skip
    rc.close_trip(w.ops, run_code=w.run.code)
    refused("STATE_CONFLICT", protective, w, "interruption.open", {"reason": "SYNTHETIC late"})
    refused("STATE_CONFLICT", ordinary, w, "session.observation", {"note": "late"})


# ================================================================== protective idempotency (14.8.3)
def test_duplicate_protective_incident_and_pause_create_no_second_incident_interruption_or_hold(w):
    start_session(w)
    first = protective(w, "incident.report", incident_payload(), key="k-1")
    assert first["effect"] == "recorded" and first["record"]["session_paused"] is True
    after_first = counts(w)
    assert after_first[:3] == (1, 1, 1)

    # the same report under a second key (a second phone, or a re-entered report): caught by domain state
    second = protective(w, "incident.report", incident_payload(), key="k-2")
    assert second["effect"] == "no_change" and second["incident"] == first["record"]["code"]
    assert "No second incident or hold" in second["message"]
    # the same key again (belt and braces under the ledger): run_command's idempotency key
    third = protective(w, "incident.report", incident_payload(), key="k-1")
    assert third["effect"] == "no_change"
    # the session is already paused by the incident: a pause adds nothing
    assert protective(w, "session.pause", key="k-3")["effect"] == "no_change"
    assert counts(w) == after_first

    # an interruption: the same reporter and words, still open, is the same report
    r1 = protective(w, "interruption.open", {"reason": "SYNTHETIC: dog loose"}, key="k-4")
    assert r1["effect"] == "recorded"
    r2 = protective(w, "interruption.open", {"reason": "SYNTHETIC: dog loose"}, key="k-5")
    assert r2["effect"] == "no_change" and r2["interruption"] == r1["record"]["code"]
    assert Interruption.objects.filter(run=w.run, source="operator").count() == 1

    # a genuinely different report is still recorded
    other = protective(w, "incident.report", incident_payload(description="SYNTHETIC: arm stuck"), key="k-6")
    assert other["effect"] == "recorded"
    assert Incident.objects.filter(robot=w.robot).count() == 2


def test_a_protective_incident_while_ordinary_steps_are_pending_places_the_hold(w):
    """The phone has ordinary steps waiting (seq 2 never arrived); the protective report does not wait."""
    ordinary(w, "custody.verify", {"custody_seq": 1, "verifier": "P-2"})  # seq 1 decided
    causal = {"last_decided_seq": 1, "outcome": "confirmed"}
    r = protective(w, "incident.report", incident_payload(), causal=causal)
    assert r["effect"] == "recorded" and r["causal"] == causal
    hold = Hold.objects.get(code=r["record"]["hold"])
    assert hold.robot_id == w.robot.pk and hold.blocking and hold.released_at is None
    assert ses.blocking_holds(w.robot.pk) == [hold.code]
    # the pending ordinary step arrives later and is re-checked against the real state (I-3): it still records
    # the fact; the hold keeps any start refused (class A, online).
    assert ordinary(w, "trip.arrived_site")["effect"] == "recorded"


# ================================================================== nothing from the device is trusted (14.4)
def test_apply_trusts_no_device_time_or_client_readiness(w):
    params = inspect.signature(fc.apply).parameters
    assert not {"device_observed_at", "recorded_at", "readiness", "authorization"} & set(params)

    future = (timezone.now() + timedelta(days=1)).isoformat()
    r = protective(w, "incident.report", incident_payload(occurred_at=future))
    assert r["effect"] == "recorded"  # a protective report is never refused for a clock
    assert r["occurred_at_source"] == "server" and "later than the server clock" in r["occurred_at_flag"]
    incident = Incident.objects.get(code=r["record"]["code"])
    assert incident.occurred_at <= timezone.now()

    past = (timezone.now() - timedelta(minutes=7)).isoformat()
    r = protective(w, "incident.report", incident_payload(description="SYNTHETIC: other", occurred_at=past))
    assert r["occurred_at_source"] == "reporter"

    for extra in ({"recorded_at": past}, {"ready": True}, {"authorization": "DA-0001"}):
        refused("INVALID_PAYLOAD", ordinary, w, "trip.arrived_site", extra)
    # readiness is never read from the phone: a start is class A whatever the payload says
    refused("ONLINE_ONLY", ordinary, w, "session.start", {"ready": True})
