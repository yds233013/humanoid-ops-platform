"""v2 03C-3: the field sync endpoint at the HTTP level (docs/13_v2_phase03_contract.md 14.1, 14.8; section 6).

Every assertion here is made through ``POST /api/field/sync`` / ``/api/field/abandon`` and the domain records
(trip events, session events, incidents, holds, interruptions). None reads the ledger's storage, so the same
tests hold when the 03C-1 ledger and the 03C-2 commands replace the stand-ins at merge. "Nothing stored" is
proven by the protocol itself: the response's ``last_decided_seq`` and a later resend applying exactly once.

Two hooks of execution/field_sync.py (they stay at merge) let a test interrupt one command:
``field_sync:claimed`` (the claim is held and the command is about to be classified and applied),
``field_sync:before_reject`` (the domain refused and its savepoint is rolled back; the rejection is not written
yet) and ``field_sync:before_commit`` (the command's work is done; its transaction has not committed).

SYNTHETIC data only. Transactional tests: every command really commits or rolls back.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client

from accounts import services as account_services
from accounts.models import RoleGrant
from accounts.permissions import visible_hub_ids
from execution import field_commands, field_sync
from execution.models import Incident, Interruption, SessionEvent, TripEvent
from execution.sessions import blocking_holds
from fleet import services as fleet_services
from platform_core import testhooks
from platform_core.models import Workspace
from tests.conftest import make_user
from tests.execution_world import build_execution, go_check, raw_session, to_site
from tests.fleet_world import set_setting

pytestmark = pytest.mark.django_db(transaction=True)

SYNC = "/api/field/sync"
ABANDON = "/api/field/abandon"
DEV_A = "0b6f3c9e-0000-4000-8000-00000000000a"
DEV_B = "0b6f3c9e-0000-4000-8000-00000000000b"
CLASS_A = ("departure.record", "session.start", "session.resume", "custody.handover_to_site", "robot.recover")


@pytest.fixture(autouse=True)
def _clear_hooks() -> Iterator[None]:
    testhooks.clear()
    yield
    testhooks.clear()


# ------------------------------------------------------------------ helpers
def db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def cmd(
    command: str,
    seq: int | None = None,
    *,
    channel: str | None = None,
    payload: dict[str, Any] | None = None,
    at: datetime | str | None = "now",
    key: str | None = None,
    last: int | None = None,
) -> dict[str, Any]:
    if channel is None:
        channel = "ordinary" if seq is not None else "protective"
    if at == "now":
        at = db_now()
    return {
        "key": key or str(uuid.uuid4()),
        "channel": channel,
        "seq": seq,
        "last_decided_seq": last,
        "command": command,
        "payload": payload or {},
        "device_observed_at": at.isoformat() if isinstance(at, datetime) else at,
    }


def body(
    run: str, commands: list[dict[str, Any]], *, device: str = DEV_A, generation: int = 1, unsent: int = 0
):
    return {
        "device_id": device,
        "run": run,
        "generation": generation,
        "unsent_count": unsent,
        "commands": commands,
    }


def post(c: Client, data: Any, path: str = SYNC) -> Any:
    raw = data if isinstance(data, str | bytes) else json.dumps(data)
    return c.post(path, raw, content_type="application/json")


def sync(c: Client, run: str, commands: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    resp = post(c, body(run, commands, **kw))
    assert resp.status_code == 200, resp.content
    return resp.json()


def outcomes(resp: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r["outcome"], r["reason_code"]) for r in resp["results"]]


def obs(seq: int, note: str = "SYNTHETIC note", **kw: Any) -> dict[str, Any]:
    return cmd("session.observation", seq, payload={"note": note}, **kw)


def trips(w: Any) -> list[str]:
    return list(TripEvent.objects.filter(run=w.run).order_by("seq").values_list("kind", flat=True))


def sessions(w: Any) -> list[str]:
    return list(SessionEvent.objects.filter(run=w.run).order_by("seq").values_list("kind", flat=True))


def hub_only_user(w: Any, name: str, role: str = "field_operator") -> Any:
    """A NON-regional actor: one grant at the run's hub, not crew of the run."""
    u = get_user_model().objects.create_user(username=name, password="test-pass-123456")
    RoleGrant.objects.create(user=u, role=role, hub_id=w.run.visit.hub_id)
    return u


@pytest.fixture
def w() -> Any:
    return build_execution("fs_")


# ------------------------------------------------------------------ response contract
RESULT_KEYS = {"key", "outcome", "reason_code", "message", "seq", "recorded_at", "result"}
TOP_KEYS = {"server_time", "run", "generation", "last_decided_seq", "next_expected_seq", "results"}


def test_response_schema_snapshot(w):
    k = str(uuid.uuid4())
    resp = sync(
        client(w.field),
        w.run.code,
        [cmd("trip.arrived_site", 1, key=k), obs(3), cmd("session.start", None)],
        unsent=3,
    )
    assert set(resp) == TOP_KEYS
    assert resp["run"] == w.run.code and resp["generation"] == 1
    assert (resp["last_decided_seq"], resp["next_expected_seq"]) == (1, 2)
    datetime.fromisoformat(resp["server_time"])
    assert [set(r) for r in resp["results"]] == [RESULT_KEYS] * 3
    first, held, online = resp["results"]
    assert first["key"] == k and first["outcome"] == "confirmed" and first["reason_code"] == ""
    assert first["seq"] == 1 and isinstance(first["result"], dict) and first["message"]
    assert datetime.fromisoformat(first["recorded_at"]).tzinfo is not None
    assert held["outcome"] == "held" and held["recorded_at"] is None and held["result"] == {}
    assert online["outcome"] == "rejected" and online["reason_code"] == "ONLINE_ONLY"
    assert online["message"] == "Needs a connection. Do not start." and online["seq"] is None
    types = {
        "key": str,
        "outcome": str,
        "reason_code": str,
        "message": str,
        "seq": (int, type(None)),
        "recorded_at": (str, type(None)),
        "result": dict,
    }
    for r in resp["results"]:
        for name, t in types.items():
            assert isinstance(r[name], t), (name, r)


def test_times_are_iso_in_the_workspace_zone(w):
    Workspace.objects.update_or_create(pk=1, defaults={"time_zone": "Asia/Tokyo"})
    resp = sync(client(w.field), w.run.code, [cmd("trip.arrived_site", 1)])
    assert resp["server_time"].endswith("+09:00")
    assert resp["results"][0]["recorded_at"].endswith("+09:00")
    te = TripEvent.objects.get(run=w.run)
    assert te.recorded_at.utcoffset() == timedelta(0)  # storage stays UTC


# ------------------------------------------------------------------ auth first (14.8 item 6)
def test_auth_first_identical_401_for_existing_and_missing_run(w):
    anon = Client()
    real = post(anon, body(w.run.code, [cmd("trip.arrived_site", 1)], unsent=7))
    missing = post(anon, body("EX-9999", [cmd("trip.arrived_site", 1)], unsent=7))
    garbage = post(anon, "{not json")
    assert real.status_code == missing.status_code == garbage.status_code == 401
    assert real.content == missing.content == garbage.content
    assert real.json()["error"] == "NOT_AUTHENTICATED"
    assert field_sync.last_device_report(w.run) is None  # no device report written
    assert trips(w) == []
    # and nothing was claimed: the crew's first request decides seq 1
    assert outcomes(sync(client(w.field), w.run.code, [cmd("trip.arrived_site", 1)])) == [("confirmed", "")]


def test_auth_is_checked_before_csrf(w):
    anon = Client(enforce_csrf_checks=True)
    assert post(anon, body(w.run.code, [])).status_code == 401
    signed_in = Client(enforce_csrf_checks=True)
    signed_in.force_login(w.field)
    resp = post(signed_in, body(w.run.code, [cmd("trip.arrived_site", 1)]))
    assert resp.status_code == 403 and resp.json()["error"] == "CSRF_FAILED"
    assert trips(w) == [] and field_sync.last_device_report(w.run) is None


@pytest.mark.parametrize("how", ["password_reset_by_admin", "signed_out", "deactivated"])
def test_ended_session_mid_queue_is_refused_and_nothing_applied(w, how):
    """Section 6 item 7 (signed out by an admin) and 14.6 (an ended session mid-queue)."""
    c = client(w.field)
    assert outcomes(sync(c, w.run.code, [cmd("trip.arrived_site", 1)], unsent=2)) == [("confirmed", "")]
    if how == "password_reset_by_admin":
        admin = make_user("fs_admin", "admin")
        account_services.reset_password(admin, user_id=w.field.pk, password="Temp-pass-98765!")
    elif how == "signed_out":
        c.logout()
    else:
        get_user_model().objects.filter(pk=w.field.pk).update(is_active=False)
    before = field_sync.last_device_report(w.run)
    resp = post(c, body(w.run.code, [obs(2)], unsent=1))
    assert resp.status_code == 401 and resp.json() == field_sync.NOT_AUTHENTICATED
    assert sessions(w) == []
    after = field_sync.last_device_report(w.run)
    assert after is not None and before is not None and after["unsent_count"] == before["unsent_count"] == 2


# ------------------------------------------------------------------ size and shape
def test_more_than_20_commands_is_413_and_nothing_applied(w):
    resp = post(client(w.field), body(w.run.code, [obs(i) for i in range(1, 22)]))
    assert resp.status_code == 413 and resp.json()["error"] == "TOO_LARGE"
    assert sessions(w) == [] and field_sync.last_device_report(w.run) is None


def test_payload_over_4kb_is_413_and_nothing_applied(w):
    big = cmd("session.observation", 2, payload={"note": "x" * 4100})
    resp = post(client(w.field), body(w.run.code, [cmd("trip.arrived_site", 1), big]))
    assert resp.status_code == 413
    assert trips(w) == [] and sessions(w) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.pop("generation"),
        lambda b: b.update(extra=1),
        lambda b: b.update(device_id="not-a-uuid"),
        lambda b: b.update(unsent_count=-1),
        lambda b: b["commands"][1].update(seq=None),  # ordinary without seq
        lambda b: b["commands"][1].update(channel="protective"),  # protective with a seq
        lambda b: b["commands"][1].update(device_observed_at="2026-11-27T09:41:05"),  # no offset
        lambda b: b["commands"][1].update(payload=[]),
        lambda b: b["commands"][1].pop("last_decided_seq"),
    ],
)
def test_malformed_request_is_400_and_nothing_applied(w, change):
    b = body(w.run.code, [cmd("trip.arrived_site", 1), obs(2)])
    change(b)
    resp = post(client(w.field), b)
    assert resp.status_code == 400 and resp.json()["error"] == "BAD_REQUEST"
    assert trips(w) == [] and sessions(w) == []


# ------------------------------------------------------------------ section 6 items 1 to 9
def test_item1_duplicate_batches_apply_each_key_once(w):
    c = client(w.field)
    batch = [cmd("trip.arrived_site", 1), obs(2)]
    first = sync(c, w.run.code, batch)
    again = sync(c, w.run.code, batch)
    assert outcomes(first) == outcomes(again) == [("confirmed", "")] * 2
    assert first["results"] == again["results"]  # the stored outcome, same recorded_at
    # the same command in a second, different batch
    third = sync(c, w.run.code, [batch[1], obs(3)])
    assert outcomes(third) == [("confirmed", "")] * 2
    assert third["results"][0] == first["results"][1]
    assert trips(w) == ["arrived_site"] and sessions(w) == ["observation", "observation"]


def test_item2_out_of_order_seq_is_held_then_applied_in_order(w):
    c = client(w.field)
    sync(c, w.run.code, [cmd("trip.arrived_site", 1)])
    three = obs(3, "third")
    held = sync(c, w.run.code, [three])
    assert outcomes(held) == [("held", "")] and held["next_expected_seq"] == 2
    assert sessions(w) == []  # held: nothing stored, nothing applied
    both = sync(c, w.run.code, [obs(2, "second"), three])
    assert outcomes(both) == [("confirmed", "")] * 2
    notes = list(SessionEvent.objects.filter(run=w.run).order_by("seq").values_list("note", flat=True))
    assert notes == ["second", "third"]
    assert (both["last_decided_seq"], both["next_expected_seq"]) == (3, 4)


def test_item2_abandoned_seq_rejects_the_later_held_step(w):
    c = client(w.field)
    sync(c, w.run.code, [cmd("trip.arrived_site", 1)])
    two, three = obs(2, "late"), obs(3, "after the gap")
    assert outcomes(sync(c, w.run.code, [three])) == [("held", "")]
    ab = post(c, {"run": w.run.code, "generation": 1, "seq": 2, "reason": "SYNTHETIC: phone lost"}, ABANDON)
    assert ab.status_code == 200, ab.content
    assert ab.json()["generation"] == 2 and ab.json()["abandoned"]["seq"] == 2
    late = sync(c, w.run.code, [three, two])
    assert outcomes(late) == [("rejected", "AFTER_ABANDONED"), ("rejected", "ABANDONED")]
    assert sessions(w) == []  # the old batch can never overwrite the barrier
    # the crew re-records from the server state in generation 2, seq from 1
    fresh = sync(c, w.run.code, [obs(1, "re-recorded")], generation=2)
    assert outcomes(fresh) == [("confirmed", "")] and fresh["generation"] == 2
    stale = sync(c, w.run.code, [obs(4, "stale queue")], generation=1)
    assert stale["results"][0]["outcome"] == "rejected"
    assert stale["results"][0]["reason_code"] in ("AFTER_ABANDONED", "STALE_GENERATION")
    assert sessions(w) == ["observation"]


def test_abandon_needs_crew_or_ops_lead_and_a_session(w):
    data = {"run": w.run.code, "generation": 1, "seq": 1, "reason": "SYNTHETIC"}
    assert post(Client(), data, ABANDON).status_code == 401
    other = hub_only_user(w, "fs_notcrew")
    resp = post(client(other), data, ABANDON)
    assert resp.status_code == 403 and resp.json() == field_sync.NOT_YOUR_RUN
    missing = post(client(other), {**data, "run": "EX-9999"}, ABANDON)
    assert missing.status_code == 403 and missing.content == resp.content
    assert outcomes(sync(client(w.field), w.run.code, [cmd("trip.arrived_site", 1)])) == [("confirmed", "")]


def test_item3_hold_placed_offline_record_applies_start_refused(w):
    fleet_services.place_hold(
        w.lead,
        scope="robot",
        target=w.robot.code,
        source_type="manual",
        reason="SYNTHETIC hold while offline",
    )
    resp = sync(client(w.field), w.run.code, [cmd("trip.arrived_site", 1), cmd("session.start")])
    assert outcomes(resp) == [("confirmed", ""), ("rejected", "ONLINE_ONLY")]
    assert trips(w) == ["arrived_site"] and sessions(w) == []
    assert blocking_holds(w.robot.pk)  # the online start re-checks the server state and is blocked


def test_item4_authorization_revoked_offline_nothing_departs_or_starts(w):
    c = client(w.field)
    resp = sync(c, w.run.code, [cmd("departure.record"), cmd("session.start")])
    assert outcomes(resp) == [("rejected", "ONLINE_ONLY")] * 2
    admin = make_user("fs_admin4", "admin")
    for g in RoleGrant.objects.filter(user=w.field, revoked_at__isnull=True):
        account_services.revoke_role(admin, grant_id=g.pk, reason="SYNTHETIC revoked while offline")
    queued = sync(c, w.run.code, [cmd("trip.arrived_site", 1), cmd("session.start")])
    assert outcomes(queued) == [("rejected", "NOT_PERMITTED"), ("rejected", "ONLINE_ONLY")]
    assert trips(w) == [] and sessions(w) == []


def test_item5_two_phones_same_step_one_confirmed_other_already_recorded(w):
    a = sync(client(w.ops), w.run.code, [cmd("trip.arrived_site", 1)], device=DEV_A)
    b = sync(client(w.field), w.run.code, [cmd("trip.arrived_site", 1)], device=DEV_B)
    assert outcomes(a) == [("confirmed", "")]
    assert outcomes(b) == [("rejected", "SEQ_TAKEN")]
    assert b["results"][0]["message"].startswith("Already recorded by")
    assert trips(w) == ["arrived_site"]


def test_item6_wrong_phone_clock(w):
    c = client(w.field)
    ahead, behind = db_now() + timedelta(hours=3), db_now() - timedelta(days=1)
    resp = sync(
        c,
        w.run.code,
        [
            cmd("trip.arrived_site", 1, at=ahead),
            cmd("trip.arrived_site", 2, at=behind),
            cmd("trip.arrived_site", 3, at=None),
            cmd("interruption.open", payload={"reason": "SYNTHETIC: crew stopped"}, at=ahead),
        ],
    )
    assert outcomes(resp)[:3] == [("rejected", "DEVICE_TIME_OUT_OF_BOUNDS")] * 3
    assert resp["last_decided_seq"] == 3  # rejected ordinary steps are decided and take their seq
    protective = resp["results"][3]
    assert protective["outcome"] == "confirmed"  # a protective step is never refused for the clock
    assert "Phone clock out of range" in protective["message"]
    assert protective["result"].get("device_time_flag") == "out_of_range"
    assert trips(w) == []
    i = Interruption.objects.get(run=w.run)
    assert abs((i.recorded_at - db_now()).total_seconds()) < 120  # database time, never the phone's


def test_item6_clock_skew_setting_widens_the_upper_bound(w):
    set_setting("field_clock_skew_min", "240")
    resp = sync(client(w.field), w.run.code, [cmd("trip.arrived_site", 1, at=db_now() + timedelta(hours=3))])
    assert outcomes(resp) == [("confirmed", "")]


def test_item7_foreign_run_is_refused_without_details_or_history(w):
    other = hub_only_user(w, "fs_other")
    k = str(uuid.uuid4())
    foreign = post(client(other), body(w.run.code, [cmd("trip.arrived_site", 1, key=k)], unsent=4))
    missing = post(client(other), body("EX-9999", [cmd("trip.arrived_site", 1, key=k)], unsent=4))
    assert foreign.status_code == missing.status_code == 403
    assert foreign.content == missing.content
    assert foreign.json() == field_sync.NOT_YOUR_RUN and w.run.code not in foreign.content.decode()
    assert field_sync.last_device_report(w.run) is None and trips(w) == []
    # no command-history write: the crew can still use seq 1, and the key was never claimed
    crew = sync(client(w.field), w.run.code, [cmd("trip.arrived_site", 1, key=k)])
    assert outcomes(crew) == [("confirmed", "")]


def test_item7_hub_field_operator_not_on_the_crew_gets_not_your_run(w):
    assert visible_hub_ids(w.field2) is not None  # P-3: a field operator of the hub, not crew of this run
    resp = post(client(w.field2), body(w.run.code, [cmd("trip.arrived_site", 1)]))
    assert resp.status_code == 403 and resp.json() == field_sync.NOT_YOUR_RUN


def test_item7_regional_actor_who_is_not_crew_reaches_the_domain_check(w):
    """14.4: NOT_YOUR_RUN is for a non-regional actor; a regional actor's step is decided by the domain's
    current grants (an engineer may not record field events: NOT_PERMITTED)."""
    eng = make_user("fs_regional_eng", "engineer")
    assert visible_hub_ids(eng) is None
    resp = sync(client(eng), w.run.code, [cmd("trip.arrived_site", 1)])
    assert outcomes(resp) == [("rejected", "NOT_PERMITTED")] and trips(w) == []


@pytest.mark.parametrize("channel", ["ordinary", "protective"])
def test_item8_class_a_posted_directly_is_refused(w, channel):
    commands = [
        cmd(name, i if channel == "ordinary" else None, channel=channel) for i, name in enumerate(CLASS_A, 1)
    ]
    resp = sync(client(w.ops), w.run.code, commands)
    assert outcomes(resp) == [("rejected", "ONLINE_ONLY")] * len(CLASS_A)
    assert {r["message"] for r in resp["results"]} == {"Needs a connection. Do not start."}
    assert sessions(w) == [] and trips(w) == []


def test_unknown_command_and_wrong_channel_are_durable_rejections(w):
    c = client(w.field)
    batch = [
        cmd("robot.teleport", 1),
        cmd("trip.departed_site", None, channel="protective"),
        cmd("incident.report", 2, payload={"description": "x"}),
    ]
    first = sync(c, w.run.code, batch)
    assert outcomes(first) == [
        ("rejected", "UNKNOWN_COMMAND"),
        ("rejected", "WRONG_CHANNEL"),
        ("rejected", "WRONG_CHANNEL"),
    ]
    assert sync(c, w.run.code, batch)["results"] == first["results"]  # durable: the replay is the same
    assert first["last_decided_seq"] == 2 and Incident.objects.count() == 0


@pytest.fixture
def active(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
        raw_session(cur, w, "start", site_check_id=go_check(cur, w))
    return w


INCIDENT = {
    "event_type": "contact",
    "description": "SYNTHETIC: light contact with a chair",
    "knowledge_source": "witnessed",
}


def test_item9_queued_incident_holds_pauses_and_keeps_order_before_stop(active):
    w = active
    resp = sync(
        client(w.field),
        w.run.code,
        [cmd("incident.report", payload=INCIDENT, last=0), cmd("session.stop", last=0)],
    )
    assert outcomes(resp) == [("confirmed", "")] * 2
    i = Incident.objects.get(run=w.run)
    assert i.hold_id is not None and blocking_holds(w.robot.pk)
    assert sessions(w) == ["start", "pause", "stop"]  # paused by the incident, then stopped, in that order


def test_item9_stop_queued_before_the_incident_keeps_that_order(active):
    w = active
    resp = sync(client(w.field), w.run.code, [cmd("session.stop"), cmd("incident.report", payload=INCIDENT)])
    assert outcomes(resp) == [("confirmed", "")] * 2
    assert sessions(w) == ["start", "stop"] and Incident.objects.filter(run=w.run).count() == 1


def test_protective_incident_with_a_gap_places_the_hold_at_once(w):
    c = client(w.field)
    sync(c, w.run.code, [cmd("trip.arrived_site", 1)])
    resp = sync(c, w.run.code, [obs(3), cmd("incident.report", payload=INCIDENT, last=1)])
    assert outcomes(resp) == [("held", ""), ("confirmed", "")]
    assert Incident.objects.filter(robot=w.robot).count() == 1 and blocking_holds(w.robot.pk)


def test_protective_stop_on_a_session_not_active_gives_the_protective_state(w):
    resp = sync(client(w.field), w.run.code, [cmd("session.stop", payload={"note": "SYNTHETIC"})])
    r = resp["results"][0]
    assert r["outcome"] == "confirmed" and r["result"]["effect"] == "protective_state"
    assert "stop" not in sessions(w)  # never claims the session stopped
    assert Interruption.objects.filter(run=w.run).exists()


def test_duplicate_protective_key_never_creates_a_second_incident(w):
    k = cmd("incident.report", payload=INCIDENT)
    c = client(w.field)
    first = sync(c, w.run.code, [k])
    again = sync(c, w.run.code, [k])
    assert first["results"] == again["results"]
    assert Incident.objects.filter(robot=w.robot).count() == 1


def test_key_reused_with_a_changed_field_is_refused(w):
    c = client(w.field)
    original = obs(1, "original")
    sync(c, w.run.code, [original])
    for change in (
        {"payload": {"note": "changed"}},
        {"seq": 2},
        {"device_observed_at": db_now().isoformat()},
    ):
        resp = sync(c, w.run.code, [{**original, **change}])
        assert outcomes(resp) == [("rejected", "KEY_REUSED")], change
    other_device = sync(c, w.run.code, [original], device=DEV_B)
    assert outcomes(other_device) == [("rejected", "KEY_REUSED")]
    assert list(SessionEvent.objects.filter(run=w.run).values_list("note", flat=True)) == ["original"]


# ------------------------------------------------------------------ 14.8 item 5 failure injection
def _raise_once(name: str) -> list[int]:
    calls: list[int] = []

    def hook() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError(f"SYNTHETIC crash at {name}")

    testhooks.register(name, hook)
    return calls


def test_crash_just_before_commit_claims_nothing_and_a_resend_applies_once(w):
    calls = _raise_once("field_sync:before_commit")
    c = client(w.field)
    step = cmd("trip.arrived_site", 1)
    crashed = sync(c, w.run.code, [step])
    assert outcomes(crashed) == [("retry", "")] and crashed["last_decided_seq"] == 0
    assert trips(w) == []
    resent = sync(c, w.run.code, [step])
    assert outcomes(resent) == [("confirmed", "")] and trips(w) == ["arrived_site"]
    assert sync(c, w.run.code, [step])["results"] == resent["results"] and len(calls) >= 2


def test_lost_response_after_commit_then_replay_gets_the_same_confirmed(w):
    c = client(w.field)
    step = cmd("trip.arrived_site", 1)
    lost = sync(c, w.run.code, [step])  # the phone never sees this answer
    replay = sync(c, w.run.code, [step])
    assert replay["results"] == lost["results"] and replay["results"][0]["outcome"] == "confirmed"
    assert trips(w) == ["arrived_site"]


def test_exception_after_savepoint_before_the_rejection_is_written_is_retry(w):
    _raise_once("field_sync:before_reject")
    c = client(w.field)
    refused = cmd("trip.departed_site", 1)  # the domain refuses: the vehicle has not arrived at the site
    crashed = sync(c, w.run.code, [refused])
    assert outcomes(crashed) == [("retry", "")] and crashed["last_decided_seq"] == 0
    resent = sync(c, w.run.code, [refused])
    assert resent["results"][0]["outcome"] == "rejected" and resent["last_decided_seq"] == 1
    assert sync(c, w.run.code, [refused])["results"] == resent["results"]
    assert trips(w) == []


def test_committed_rejection_then_a_dependent_command_rechecks_the_real_state(w):
    c = client(w.field)
    resp = sync(
        c,
        w.run.code,
        [
            cmd("trip.arrived_site", 1, at=db_now() + timedelta(hours=5)),  # rejected: decided, not done
            cmd("trip.departed_site", 2),  # assumed the arrival: the server state says otherwise
            cmd("trip.arrived_site", 3),
        ],
    )
    assert outcomes(resp)[0] == ("rejected", "DEVICE_TIME_OUT_OF_BOUNDS")
    assert resp["results"][1]["outcome"] == "rejected"
    assert resp["results"][1]["reason_code"] not in ("", "DEVICE_TIME_OUT_OF_BOUNDS")
    assert resp["results"][2]["outcome"] == "confirmed"
    assert trips(w) == ["arrived_site"] and resp["last_decided_seq"] == 3


def test_one_commands_retry_does_not_undo_anothers_confirmed_result(w, monkeypatch):
    """Each command runs in its own transaction (14.3 item 7)."""
    real = field_commands.apply

    def flaky(actor, run, command, payload, **kw):
        out = real(actor, run, command, payload, **kw)  # the domain work is done...
        if command == "session.observation":
            raise field_commands.Transient("SYNTHETIC deadlock")  # ...then a transient error
        return out

    monkeypatch.setattr(field_sync.field_commands, "apply", flaky)
    c = client(w.field)
    second = obs(2)
    resp = sync(
        c,
        w.run.code,
        [cmd("trip.arrived_site", 1), second, cmd("interruption.open", payload={"reason": "x"})],
    )
    assert outcomes(resp) == [("confirmed", ""), ("retry", ""), ("confirmed", "")]
    assert trips(w) == ["arrived_site"] and sessions(w) == []  # the observation was rolled back entirely
    assert resp["last_decided_seq"] == 1
    monkeypatch.setattr(field_sync.field_commands, "apply", real)
    assert outcomes(sync(c, w.run.code, [second])) == [("confirmed", "")]
    assert sessions(w) == ["observation"]


def _waiters() -> int:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE NOT l.granted AND a.datname = current_database()"
        )
        return int(cur.fetchone()[0])


def _in_thread(fn, out: dict[str, Any], name: str) -> threading.Thread:
    def run() -> None:
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 - reported by the test
            out[name] = exc
        finally:
            connections.close_all()

    t = threading.Thread(target=run, name=name)
    t.start()
    return t


@pytest.mark.parametrize("second", ["same_identity", "changed_payload", "other_key_same_seq"])
def test_competing_request_waits_on_the_claim_then_gets_replay_or_refusal(w, second):
    """Two connections race for one key or one seq; the waiter is seen in pg_locks (14.3 item 4, 14.6)."""
    first = cmd("trip.arrived_site", 1)
    other = {
        "same_identity": first,
        "changed_payload": {**first, "payload": {"note": "changed"}},
        "other_key_same_seq": {**first, "key": str(uuid.uuid4())},
    }[second]
    entered, release = threading.Event(), threading.Event()

    def hold_first() -> None:
        if threading.current_thread().name == "A":
            entered.set()
            assert release.wait(30)

    testhooks.register("field_sync:claimed", hold_first)
    out: dict[str, Any] = {}
    a = _in_thread(lambda: sync(client(w.field), w.run.code, [first]), out, "A")
    assert entered.wait(30)
    b = _in_thread(lambda: sync(client(w.field), w.run.code, [other]), out, "B")
    deadline = time.monotonic() + 20
    while _waiters() == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    seen = _waiters()
    release.set()
    a.join(30)
    b.join(30)
    assert seen >= 1, "the competing request did not wait on the claim"
    assert not isinstance(out["A"], Exception), out["A"]
    assert not isinstance(out["B"], Exception), out["B"]
    assert outcomes(out["A"]) == [("confirmed", "")]
    expected = {
        "same_identity": ("confirmed", ""),
        "changed_payload": ("rejected", "KEY_REUSED"),
        "other_key_same_seq": ("rejected", "SEQ_TAKEN"),
    }[second]
    assert outcomes(out["B"]) == [expected]
    if second == "same_identity":
        assert out["B"]["results"] == out["A"]["results"]
    assert trips(w) == ["arrived_site"]


# ------------------------------------------------------------------ device report and the ops lead view
def test_unsent_count_is_shown_to_the_ops_lead_as_last_reported_never_live(w):
    sync(client(w.field), w.run.code, [], unsent=3, device=DEV_B)
    report = field_sync.last_device_report(w.run)
    assert report is not None and report["unsent_count"] == 3
    page = client(w.ops).get(f"/execution/runs/{w.run.code}/").content.decode()
    assert "Last reported unsent count: 3 (at " in page and f"device …{DEV_B[-4:]}" in page
    assert "not a live count" in page
    crew_page = client(w.field).get(f"/execution/runs/{w.run.code}/").content.decode()
    assert "Last reported unsent count" not in crew_page
