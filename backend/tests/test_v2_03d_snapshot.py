"""v2 03D-2: the read-only run snapshot ``GET /api/field/runs/<code>/snapshot?device=<uuid>`` (contract 15.1,
15.10 items 1, 3 and 5; 03d_modules.md).

Auth first (401), then the sync endpoint's access rule (403 NOT_YOUR_RUN, the same body for a missing run,
nothing else read), read-only, ``Cache-Control: no-store``, minimal data, the server ledger state the phone
bootstraps its queue from, the protective state that keeps the "Robot must not operate" banner, and a fixed
statement count whatever the number of records on the run.

SYNTHETIC data only.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client

from accounts.models import RoleGrant
from execution import field_commands as fc
from execution import field_snapshot as fs
from execution import sessions as ses
from execution.custody import ROBOT_INTERRUPTION_UNRESOLVED
from execution.models import Interruption
from platform_core.models import Hub
from tests.conftest import make_user
from tests.execution_world import build_execution
from tests.test_v2_03c_protective import _bring_back_and_close, at_site_with_go_check, fallback_stop
from tests.test_v2_03c_sync import cmd, outcomes, post, sync

pytestmark = pytest.mark.django_db(transaction=True)

DEV = "0b6f3c9e-0000-4000-8000-0000000000d2"
TOP_KEYS = {
    "snapshot_version",
    "fetched_at",
    "user",
    "run",
    "steps",
    "crew",
    "ops_leads",
    "protective",
    "ledger",
    "readiness_code",
}
STEP_KEYS = {"command", "label", "class", "done", "recorded_at"}
DATA_TABLES = (
    "execution_field_command",
    "execution_tripevent",
    "execution_custodyevent",
    "execution_interruption",
    "execution_incident",
    "execution_sessionevent",
    "execution_sitecheck",
)
# Measured on this world: 14 statements for an open run, one more for a closed one (session and user reads
# included). The budget leaves a little room; the count must not grow with the number of records.
STATEMENT_BUDGET = 16


@pytest.fixture
def w(transactional_db: None) -> Any:
    return build_execution("sn_")


def client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def url(w: Any, code: str | None = None, device: str | None = DEV) -> str:
    base = f"/api/field/runs/{code or w.run.code}/snapshot"
    return base if device is None else f"{base}?device={device}"


def get(c: Client, w: Any, **kw: Any) -> dict[str, Any]:
    resp = c.get(url(w, **kw))
    assert resp.status_code == 200, resp.content
    assert "no-store" in resp["Cache-Control"]
    body: dict[str, Any] = resp.json()
    return body


def recorded(c: Client, w: Any) -> tuple[Any, list[str]]:
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        resp = c.get(url(w))
    return resp, seen


def step(snap: dict[str, Any], command: str) -> dict[str, Any]:
    return next(s for s in snap["steps"] if s["command"] == command)


def hub_only_user(w: Any, name: str, role: str, hub_id: int | None = None) -> Any:
    u = get_user_model().objects.create_user(username=name, password="test-pass-123456")
    RoleGrant.objects.create(user=u, role=role, hub_id=hub_id or w.run.visit.hub_id)
    return u


# ------------------------------------------------------------------ authentication and access
def test_401_first_and_identical_for_an_existing_and_a_missing_run(w):
    anon = Client()
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        real = anon.get(url(w))
        missing = anon.get(url(w, code="EX-9999"))
        no_device = anon.get(url(w, device=None))
    assert real.status_code == missing.status_code == no_device.status_code == 401
    assert real.content == missing.content == no_device.content
    assert real.json()["error"] == "NOT_AUTHENTICATED" and "no-store" in real["Cache-Control"]
    assert not [s for s in seen if "execution_executionrun" in s]


def test_an_ended_session_gets_401(w):
    c = client(w.field)
    assert c.get(url(w)).status_code == 200
    c.logout()
    assert c.get(url(w)).status_code == 401


def test_not_your_run_is_one_403_reading_nothing_else(w):
    other = hub_only_user(w, "sn_notcrew", "field_operator")  # a hub field operator, not crew of this run
    eng = make_user("sn_regional_eng", "engineer")  # regional, neither crew nor ops lead
    hub_b = Hub.objects.create(code="HB", name="Hub B (fictional)")
    # ops_lead is a regional role (role_grant_hub_scope), so the other-hub case is a field operator there
    far = hub_only_user(w, "sn_far", "field_operator", hub_id=hub_b.pk)
    for user in (other, eng, far, w.field2):
        c = client(user)
        seen: list[str] = []

        def wrapper(
            execute: Any, sql: str, params: Any, many: bool, context: Any, seen: list[str] = seen
        ) -> Any:
            seen.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(wrapper):
            foreign = c.get(url(w))
        missing = c.get(url(w, code="EX-9999"))
        assert foreign.status_code == missing.status_code == 403, user.username
        assert foreign.content == missing.content
        assert foreign.json()["error"] == "NOT_YOUR_RUN"
        assert w.run.code not in foreign.content.decode() and w.robot.code not in foreign.content.decode()
        assert "no-store" in foreign["Cache-Control"]
        assert not [s for s in seen for t in DATA_TABLES if t in s], user.username


def test_crew_and_the_hub_ops_lead_may_read(w):
    lead = make_user("sn_regional_lead", "ops_lead")  # a regional ops lead, not crew
    for user in (w.field, w.ops, lead):
        snap = get(client(user), w)
        assert snap["user"]["id"] == user.pk and snap["run"]["code"] == w.run.code


def test_bad_device_and_methods(w):
    c = client(w.field)
    for bad in (None, "", "not-a-uuid", "1234"):
        resp = c.get(url(w, device=bad))
        assert resp.status_code == 400 and resp.json()["error"] == "BAD_REQUEST", bad
    resp = c.post(url(w), {})
    assert resp.status_code == 405 and resp.json()["error"] == "METHOD_NOT_ALLOWED"
    assert c.head(url(w)).status_code == 200


# ------------------------------------------------------------------ the body
def test_schema_and_minimal_data(w):
    c = client(w.field)
    resp = c.get(url(w))
    assert resp.status_code == 200 and resp["Content-Type"].startswith("application/json")
    snap = resp.json()
    assert set(snap) == TOP_KEYS and snap["snapshot_version"] == 1
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", snap["fetched_at"])
    assert snap["user"] == {"id": w.field.pk, "name": w.field.get_full_name() or w.field.username}
    assert set(snap["run"]) == {"code", "state", "hub", "robot", "home_label"}
    assert snap["run"]["robot"] == w.robot.code and snap["run"]["hub"] == w.run.visit.hub.code
    assert snap["run"]["state"] == "to_site"
    site = w.run.visit.site
    assert snap["run"]["home_label"].startswith(site.code)
    crew_ids = [x["id"] for x in snap["crew"]]
    assert w.field.pk in crew_ids and len(crew_ids) == 2
    assert all(set(x) == {"id", "name"} for x in snap["crew"])
    assert all(set(x) == {"name"} for x in snap["ops_leads"]) and snap["ops_leads"]
    assert snap["ledger"] == {
        "device_id": DEV,
        "generation": 1,
        "next_expected_seq": 1,
        "last_decided_seq": 0,
        "last_decided_outcome": None,
    }
    assert snap["protective"] == {
        "open": False,
        "kind": None,
        "since": None,
        "message": "No interruption, open incident or pause is recorded on this run.",
    }
    assert snap["readiness_code"] is None
    # nothing beyond what the phone needs: no credentials, consent, CRM reference or other homes
    raw = resp.content.decode().lower()
    for word in ("password", "csrf", "sessionid", "consent", "crm", "token"):
        assert word not in raw, word
    if site.crm_ref:
        assert site.crm_ref.lower() not in raw
    other_visits = [
        v for v in type(w.run.visit).objects.exclude(pk=w.run.visit_id).values_list("code", flat=True)
    ]
    assert not [v for v in other_visits if v.lower() in raw]


def test_steps_have_labels_classes_and_done_from_the_records(w):
    snap = get(client(w.field), w)
    assert [s["command"] for s in snap["steps"]] == [c for _, c in fs.FIELD_STEPS]
    assert all(set(s) == STEP_KEYS for s in snap["steps"])
    for s in snap["steps"]:
        assert s["class"] == fc.CLASS[s["command"]] and s["class"] in ("A", "B", "C")
        assert s["label"] and not s["done"] and s["recorded_at"] is None
    assert step(snap, "custody.handover_to_site")["class"] == "A"
    assert step(snap, "session.stop")["class"] == "B"
    assert step(snap, "trip.arrived_site") == {
        "command": "trip.arrived_site",
        "label": "Record the arrival at the site",
        "class": "C",
        "done": False,
        "recorded_at": None,
    }

    at_site_with_go_check(w)  # verify, arrive, hand over, Go site check
    snap = get(client(w.field), w)
    for command in ("custody.verify", "trip.arrived_site", "custody.handover_to_site", "site_check.record"):
        s = step(snap, command)
        assert s["done"] and re.fullmatch(r"\d{4}-.*[+-]\d\d:\d\d", s["recorded_at"]), s
    assert not step(snap, "session.start")["done"] and snap["run"]["state"] == "on_site"


def test_ledger_follows_the_server_stream(w):
    c = client(w.field)
    assert outcomes(sync(c, w.run.code, [cmd("trip.arrived_site", 1)])) == [("confirmed", "")]
    snap = get(c, w)
    assert snap["ledger"]["next_expected_seq"] == 2 and snap["ledger"]["last_decided_seq"] == 1
    assert snap["ledger"]["last_decided_outcome"] == "confirmed"
    assert step(snap, "trip.arrived_site")["done"]
    # a refused step is decided, not confirmed (14.8): the snapshot says rejected
    resp = sync(c, w.run.code, [cmd("trip.arrived_hub", 2)])
    assert resp["results"][0]["outcome"] == "rejected", resp
    snap = get(c, w)
    assert (snap["ledger"]["last_decided_seq"], snap["ledger"]["last_decided_outcome"]) == (2, "rejected")
    assert not step(snap, "trip.arrived_hub")["done"]
    # abandoning seq 3 ends generation 1; the phone bootstraps generation 2 from seq 1
    gone = post(
        c, {"run": w.run.code, "generation": 1, "seq": 3, "reason": "SYNTHETIC"}, "/api/field/abandon"
    )
    assert gone.status_code == 200, gone.content
    snap = get(c, w)
    assert snap["ledger"] == {
        "device_id": DEV,
        "generation": 2,
        "next_expected_seq": 1,
        "last_decided_seq": 0,
        "last_decided_outcome": None,
    }
    # the stream is the run's: another device of the crew sees the same numbers, with its own id
    other = str(uuid.uuid4())
    assert get(c, w, device=other)["ledger"] == {**snap["ledger"], "device_id": other}


def test_protective_state_interruption_then_resolution(w):
    at_site_with_go_check(w)
    it = Interruption.objects.get(code=fallback_stop(w)["interruption"])
    snap = get(client(w.field), w)
    p = snap["protective"]
    assert p["open"] is True and p["kind"] == "interruption" and it.code in p["message"]
    assert p["since"] is not None and "must not operate" in p["message"]
    ses.resolve_interruption(w.ops, code=it.code, note="SYNTHETIC: reviewed")
    assert get(client(w.field), w)["protective"]["open"] is False


def test_protective_state_pause_and_stop(w):
    check = at_site_with_go_check(w)
    ses.record_session_event(w.field, run_code=w.run.code, kind="start", site_check_id=check)
    assert get(client(w.field), w)["protective"]["open"] is False
    ses.record_session_event(w.field, run_code=w.run.code, kind="pause", note="SYNTHETIC")
    p = get(client(w.field), w)["protective"]
    assert (p["open"], p["kind"]) == (True, "pause")
    ses.record_session_event(w.field, run_code=w.run.code, kind="stop", note="SYNTHETIC")
    snap = get(client(w.field), w)
    assert (snap["protective"]["open"], snap["protective"]["kind"]) == (False, "stop")
    assert step(snap, "session.start")["done"] and step(snap, "session.stop")["done"]


def test_protective_state_open_incident(w):
    at_site_with_go_check(w)
    r = fc.apply(
        w.field,
        w.run,
        "incident.report",
        {
            "event_type": "stopped_unexpectedly",
            "description": "SYNTHETIC: odd noise",
            "knowledge_source": "witnessed",
        },
        channel="protective",
        causal={"last_decided_seq": 0, "outcome": None},
    )
    p = get(client(w.field), w)["protective"]
    assert p["open"] is True, (r, p)
    assert p["kind"] in ("incident", "interruption")  # an incident report may also open its interruption


def test_closed_run_with_an_unresolved_interruption_carries_the_15_7_code(w):
    at_site_with_go_check(w)
    fallback_stop(w)
    _bring_back_and_close(w)
    snap = get(client(w.field), w)
    assert snap["run"]["state"] == "closed"
    assert snap["readiness_code"] == ROBOT_INTERRUPTION_UNRESOLVED
    assert snap["protective"]["open"] is True and snap["protective"]["kind"] == "interruption"


# ------------------------------------------------------------------ read-only and statements
def test_read_only_and_statement_budget_constant_in_the_records(w, monkeypatch):
    import platform_core.interaction as ia

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the counts
    c = client(w.field)
    c.get(url(w))  # warm (content types, zone)
    resp, fresh = recorded(c, w)
    assert resp.status_code == 200
    writes = [s for s in fresh if re.match(r"\s*(INSERT|UPDATE|DELETE)\b", s, re.I)]
    assert not writes, writes
    assert len(fresh) <= STATEMENT_BUDGET, (len(fresh), fresh)

    check = at_site_with_go_check(w)
    ses.record_session_event(w.field, run_code=w.run.code, kind="start", site_check_id=check)
    for n in range(1, 6):
        ses.record_session_event(w.field, run_code=w.run.code, kind="observation", note=f"SYNTHETIC {n}")
    ses.record_session_event(w.field, run_code=w.run.code, kind="pause", note="SYNTHETIC")
    ses.open_interruption(w.field, run_code=w.run.code, reason="SYNTHETIC: crew asked")
    resp, busy = recorded(c, w)
    assert resp.status_code == 200 and resp.json()["protective"]["open"] is True
    assert len(busy) == len(fresh), (len(fresh), len(busy))
    print(f"SNAPSHOT statements: {len(fresh)} (budget {STATEMENT_BUDGET})")
