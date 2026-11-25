"""v2 Phase 02, milestone 02E (final acceptance): regression tests for the items carried from 02C and 02D
(docs/11_v2_phase02_contract.md 8.2, 8.5, 8.6, 8.7, 8.8). SYNTHETIC data only.

(a) Direct runtime SQL as ops_app racing the activation of the maintenance gate, in BOTH orders, with the waiter
    observed in pg_locks: a reservation, requested -> planned and -> confirmed for a robot the gate blocks. Either
    the writer waits for the activation and is refused, or it commits first and is then an existing booking that
    shows blocked with its reasons and is not re-planned. A mutation test puts the planning/0008 trigger bodies
    back and shows the bypass those allowed (fixed by planning/0009).
(b) A new maintenance record hides neither an open session (hours stay Unknown; fixed by fleet/0011) nor a
    required post-maintenance inspection, in planning eligibility and in readiness_issue_authorization.
(c) Issue and consume of stock for a visit whose execution run is CLOSED are refused, through the service and
    through direct SQL as ops_app (hub/0009).
(d) Reservations held for a cancelled visit: flagged on the Stock page, counted per item ("held for cancelled
    visits"), and released in one click by the hub technician and the ops lead (hub_release_stock); the Stock
    page statement count stays constant.
"""

import importlib
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client

from fleet import maintenance as mnt
from fleet import services as fs
from fleet.models import InspectionTemplate, Robot
from hub import stock as st
from hub.models import StockMovement, StockReservation
from planning import services as ps
from planning.models import Visit
from platform_core.commands import Conflict
from platform_core.locks import SETTINGS_LOCK_KEY
from readiness import commands as rc
from readiness.evaluator import evaluate_readiness
from tests.conftest import make_user
from tests.fleet_world import record_periodic_inspection
from tests.planning_world import plan
from tests.readiness_world import build_readiness
from tests.test_v2_02c_maintenance import (
    ISSUE_SQL,
    _run_world,
    app_conn,
    gate,
    grant_maintainer,
    issue_args,
    nv,
    one,
    owner_conn,
    record,
    refused,
    rule,
    states,
    status,
)
from tests.test_v2_02d_inventory import TOKEN_RE, _count, _level, _tomorrow_visit, _world

GATE_SQL = "UPDATE fleet_deploymentsetting SET value = 'true' WHERE key = 'maintenance_gate_active'"
BLOCKED = "maintenance not up to date"
RACED_RESERVATION = (
    "SELECT count(*) FROM planning_reservation WHERE revision_id = %s AND starts_at = '2027-01-05 10:00Z'"
)


@pytest.fixture
def w(transactional_db):
    w = build_readiness(prefix="ce_")
    w.tech = make_user("ce_tech", "hub_technician")
    ps.register_person(w.ops, username=w.tech.username, code="P-9")
    grant_maintainer(w, "P-9")
    return w


# ================================================================== (a) direct SQL racing gate activation
def _settings_waiter(pid: int) -> bool:
    """True while backend ``pid`` waits (not granted) on the settings advisory lock (pg_locks)."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory' AND NOT granted"
            " AND ((classid::bigint << 32) | objid::bigint) = %s)",
            [pid, SETTINGS_LOCK_KEY],
        )
        return bool(cur.fetchone()[0])


class Background:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            self.error = exc

    def join(self) -> None:
        self.thread.join(30)
        assert not self.thread.is_alive()


def _observe_waiting(pid: int, bg: Background) -> None:
    """The background backend ``pid`` is seen WAITING on the settings lock in pg_locks, and still waits a moment
    later (not a transient blip)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and bg.thread.is_alive():
        if _settings_waiter(pid):
            time.sleep(0.3)
            assert bg.thread.is_alive() and _settings_waiter(pid), "the waiter stopped waiting on its own"
            return
        time.sleep(0.05)
    pytest.fail(f"backend {pid} never waited on the settings lock (error: {bg.error!r})")


def _activate(owner: psycopg.Connection) -> None:
    """What provision_settings does: the settings lock EXCLUSIVE first, then the change (left uncommitted)."""
    with owner.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", [SETTINGS_LOCK_KEY])
        cur.execute(GATE_SQL)


def _race_setup(w: Any, action: str) -> SimpleNamespace:
    """R-01 is planned onto a new visit while the gate is inactive; a rule is in force and R-01 has no record, so
    it is Unknown and blocked once the gate is active. Returns the direct SQL of ``action`` and how to see it."""
    rule(w)
    gate(False)
    code = nv(w, 1)
    assert plan(w, code)["feasibility"] == "feasible"
    v = Visit.objects.get(code=code)
    rev = v.current_revision
    assert rev is not None and rev.robot_id is not None
    if action == "reserve":
        res = one("SELECT id FROM planning_resource WHERE kind = 'robot' AND robot_id = %s", [rev.robot_id])
        sql = (
            "INSERT INTO planning_reservation (resource_id, revision_id, purpose, starts_at, ends_at, released_reason)"
            " VALUES (%s, %s, 'on_site', '2027-01-05 10:00Z', '2027-01-05 11:00Z', '')"
        )
        params: list[Any] = [res, rev.pk]

        def done() -> bool:
            return bool(one(RACED_RESERVATION, [rev.pk]))
    else:
        target = "planned" if action == "plan" else "confirmed"
        if action == "plan":
            with app_conn() as c:  # the safe direction is never refused
                c.execute("UPDATE planning_visit SET status = 'requested' WHERE id = %s", [v.pk])
        sql, params = f"UPDATE planning_visit SET status = '{target}' WHERE id = %s", [v.pk]

        def done() -> bool:
            return Visit.objects.get(pk=v.pk).status == target

    assert not done()
    return SimpleNamespace(visit=v, sql=sql, params=params, done=done)


@pytest.mark.parametrize("action", ["reserve", "plan", "confirm"])
def test_activation_first_then_direct_sql_waits_and_is_refused(w, action):
    s = _race_setup(w, action)
    owner, app = owner_conn(), app_conn(autocommit=False)
    try:
        _activate(owner)
        pid = app.info.backend_pid
        bg = Background(lambda: app.execute(s.sql, s.params))
        _observe_waiting(pid, bg)  # the runtime write waits for the uncommitted activation
        owner.commit()
        bg.join()
        assert isinstance(bg.error, psycopg.Error), bg.error
        assert BLOCKED in str(bg.error), str(bg.error)  # it read the gate AFTER the activation committed
        app.rollback()
    finally:
        owner.close()
        app.close()
    assert mnt.gate_active() and not s.done()


@pytest.mark.parametrize("action", ["reserve", "plan", "confirm"])
def test_direct_sql_first_then_activation_waits_and_the_booking_shows_blocked(w, action):
    s = _race_setup(w, action)
    owner, app = owner_conn(), app_conn(autocommit=False)
    try:
        app.execute(s.sql, s.params)  # gate inactive: allowed; the trigger now holds the settings lock SHARED
        pid = owner.info.backend_pid
        bg = Background(lambda: _activate(owner))
        _observe_waiting(pid, bg)  # the activation waits for the runtime write to commit
        app.commit()
        bg.join()
        assert bg.error is None, bg.error
        owner.commit()
    finally:
        owner.close()
        app.close()
    assert mnt.gate_active() and s.done()  # committed BEFORE the activation: an existing booking
    v = Visit.objects.get(pk=s.visit.pk)
    before = (v.status, v.current_revision_id, v.version)
    # existing booking: shown blocked with the reason, never re-planned silently (02C point 7)
    e = fs.evaluate_eligibility(Robot.objects.get(code="R-01"))
    assert not e.eligible_for_planning
    assert e.reasons == ["Maintenance JOINT status unknown: no maintenance record for this robot"]
    live = ps.live_feasibility(v)
    assert live.feasibility == "blocked"
    assert any("Maintenance JOINT status unknown" in b["message"] for b in live.blockers), live.blockers
    v.refresh_from_db()
    assert (v.status, v.current_revision_id, v.version) == before
    if action != "confirm":  # and it cannot be confirmed now, through the command or direct SQL
        with app_conn() as c:
            refused(c, "UPDATE planning_visit SET status = 'confirmed' WHERE id = %s", [v.pk], match=BLOCKED)


def test_mutation_without_the_shared_lock_the_activation_is_bypassed(w):
    """Put the planning/0008 trigger bodies back (no settings lock): the reservation does not wait, commits AFTER
    the activation committed, and a robot the active gate blocks has a new booking. planning/0009 is restored."""
    mig = importlib.import_module("planning.migrations.0009_maintenance_gate_settings_lock")
    s = _race_setup(w, "reserve")
    with owner_conn() as ddl:
        ddl.execute(mig.OLD)
    try:
        owner, app = owner_conn(), app_conn(autocommit=False)
        try:
            _activate(owner)
            app.execute(s.sql, s.params)  # does not wait: read the gate as inactive
            assert not _settings_waiter(app.info.backend_pid)
            owner.commit()
            app.commit()  # committed after the activation
        finally:
            owner.close()
            app.close()
        assert mnt.gate_active() and s.done()
        assert not fs.evaluate_eligibility(Robot.objects.get(code="R-01")).eligible_for_planning
    finally:
        with owner_conn() as ddl:
            ddl.execute(mig.NEW)
    assert "pg_advisory_xact_lock_shared" in one(
        "SELECT prosrc FROM pg_proc WHERE proname = 'planning_reservation_maintenance_guard'"
    )


def test_gate_triggers_take_the_settings_lock_before_reading_the_gate(w):
    for fn in (
        "planning_revision_maintenance_guard",
        "planning_reservation_maintenance_guard",
        "planning_visit_maintenance_guard",
    ):
        src = one("SELECT prosrc FROM pg_proc WHERE proname = %s", [fn])
        assert f"pg_advisory_xact_lock_shared({SETTINGS_LOCK_KEY})" in src, fn
        assert src.index("pg_advisory_xact_lock_shared") < src.index("planning_maintenance_problem"), fn


# ================================================================== (b) a record never hides open data
def test_record_while_a_session_is_open_keeps_hours_unknown_and_the_robot_blocked(w, app_role_conn):
    rule(w, calendar_days=30, active_hours=50, warning_pct=0)
    record(w)
    later = nv(w, 2)
    # R-01, planned and confirmed while the gate is inactive
    assert plan(w, later)["feasibility"] == "feasible"
    ps.confirm_visit(w.ops, visit_code=later)
    ex = _run_world(w)  # w.visit departs with R-01
    with app_role_conn.cursor() as cur:
        ex.to_site(cur, w)
        check = ex.go_check(cur, w)
        ex.raw_session(cur, w, "start", site_check_id=check)
    assert states() == {"JOINT": "unknown"}
    rec2 = record(w)  # recorded while the session is still open
    row = status()[0]
    assert row["record_id"] == rec2  # the new record is the one counted from
    assert (row["state"], row["blocking"], row["open_runs"]) == ("unknown", True, w.run.code)
    assert "has a session start but no stop" in row["reason"]
    gate(True)
    # planning eligibility (view and Python) and the plan command
    e = fs.evaluate_eligibility(Robot.objects.get(code="R-01"))
    assert not e.eligible_for_planning and "active hours unknown" in e.reasons[0]
    view = one("SELECT eligible FROM operations_robot_eligibility WHERE robot_code = 'R-01'")
    assert view is False
    assert plan(w, nv(w, 3))["feasibility"] == "blocked"
    # readiness_issue_authorization (direct SQL as ops_app) and the command
    refused(app_role_conn, ISSUE_SQL, issue_args(later, w.ops.pk), match="active hours unknown")
    with pytest.raises(Conflict):
        rc.authorize_departure(w.ops, visit_code=later)
    # once the session is stopped, only the time after the record counts and the robot is Not due
    with app_role_conn.cursor() as cur:
        ex.raw_session(cur, w, "stop")
    row = status()[0]
    assert (row["state"], row["open_runs"]) == ("not_due", None) and float(row["hours_used"]) < 1
    assert fs.evaluate_eligibility(Robot.objects.get(code="R-01")).eligible_for_planning


def test_record_before_the_required_inspection_keeps_the_robot_blocked(w, app_role_conn):
    t1 = InspectionTemplate.objects.get(pattern__code="P-A", version="T1")
    rule(w, inspection_template_id=t1.pk)
    record(w)
    record_periodic_inspection(w, "R-01")
    assert states() == {"JOINT": "not_due"}
    gate(True)
    assert fs.evaluate_eligibility(Robot.objects.get(code="R-01")).eligible_for_planning
    record(w)  # a new record: the earlier inspection was submitted before it and does not qualify
    row = status()[0]
    assert (row["state"], row["blocking"], row["inspection_id"]) == ("inspection_missing", True, None)
    words = "required post-maintenance inspection is missing"
    e = fs.evaluate_eligibility(Robot.objects.get(code="R-01"))
    assert not e.eligible_for_planning and words in e.reasons[0]
    res = evaluate_readiness(Visit.objects.get(code=w.visit))
    assert not res.ready
    assert any(words in b["message"] for b in res.blockers if b["code"] == "PLANNING_ROBOT_NOT_ELIGIBLE")
    refused(app_role_conn, ISSUE_SQL, issue_args(w.visit, w.ops.pk), match=words)
    with pytest.raises(Conflict):
        rc.authorize_departure(w.ops, visit_code=w.visit)
    assert plan(w, nv(w, 1))["feasibility"] == "blocked"
    # a qualifying inspection submitted after that record clears it
    record_periodic_inspection(w, "R-01")
    assert states() == {"JOINT": "not_due"}
    with app_role_conn.cursor() as cur:
        cur.execute(ISSUE_SQL, issue_args(w.visit, w.ops.pk))


# ================================================================== (c) stock for a visit whose run is closed
def test_issue_and_consume_for_a_visit_whose_run_is_closed_are_refused(w, app_role_conn):
    st.create_item(w.ops, code="RK", name="Restraint kit", kind="reusable", unit="kits")
    st.create_item(w.ops, code="GLOVE", name="Nitrile gloves", kind="consumable", unit="boxes")
    st.receive(w.tech, item_code="RK", quantity=5)
    st.receive(w.tech, item_code="GLOVE", quantity=5)
    ex = _run_world(w)
    with app_role_conn.cursor() as cur:
        ex.to_site(cur, w)
        check = ex.go_check(cur, w)
        ex.raw_session(cur, w, "start", site_check_id=check)
        ex.raw_session(cur, w, "stop")
        ex.to_hub(cur, w)
        ex.close_run(cur, w)
    assert one("SELECT count(*) FROM execution_tripclosure WHERE run_id = %s", [w.run.pk]) == 1
    v = Visit.objects.get(code=w.visit)
    # neither cancelled nor over: only the closure refuses
    assert v.status != "cancelled" and v.current_revision.on_site_end > fs.db_now()
    before = (StockMovement.objects.count(), StockReservation.objects.count())
    words = f"Visit {w.visit} is already closed"
    for call in (
        lambda: st.issue(w.tech, item_code="RK", quantity=1, visit_code=w.visit),
        lambda: st.consume(w.tech, item_code="GLOVE", quantity=1, visit_code=w.visit),
        lambda: st.reserve(w.tech, item_code="RK", visit_code=w.visit, quantity=1),
    ):
        with pytest.raises(Conflict, match=words):
            call()
    rk, glove = (one("SELECT id FROM hub_stock_item WHERE code = %s", [c]) for c in ("RK", "GLOVE"))
    refused(app_role_conn, "SELECT hub_issue_stock(%s, %s, 1, %s, '')", [w.tech.pk, rk, v.pk], match=words)
    refused(
        app_role_conn, "SELECT hub_consume_stock(%s, %s, 1, %s, '')", [w.tech.pk, glove, v.pk], match=words
    )
    assert (StockMovement.objects.count(), StockReservation.objects.count()) == before
    # without a visit, issue and consume are unchanged
    st.issue(w.tech, item_code="RK", quantity=1)
    st.consume(w.tech, item_code="GLOVE", quantity=1)


# ================================================================== (d) reservations of a cancelled visit
def _release_now(user: Any, reservation_id: int) -> str:
    c = Client()
    c.force_login(user)
    token = TOKEN_RE.search(c.get("/hub/stock/").content.decode()).group(1)
    r = c.post("/hub/stock/release-cancelled/", {"_form_token": token, "reservation": str(reservation_id)},
               follow=True)  # fmt: skip
    return r.content.decode()


@pytest.mark.django_db
def test_cancelled_visit_reservations_are_flagged_counted_and_released_in_one_click():
    w = _world("cv_")
    st.receive(w.tech, item_code="RK", quantity=6)
    keep, gone1, gone2 = (_tomorrow_visit(w, h) for h in (9, 11, 13))
    st.reserve(w.tech, item_code="RK", visit_code=keep, quantity=1)
    st.reserve(w.tech, item_code="RK", visit_code=gone1, quantity=2)
    st.reserve(w.tech, item_code="RK", visit_code=gone2, quantity=1)
    for code in (gone1, gone2):
        w.p.cancel_visit(w.ops, visit_code=code, reason="customer cancelled")
    lv = _level("RK")
    assert (lv.reserved, lv.cancelled, lv.available) == (4, 3, 2)  # cancelling released nothing by itself
    rid = {r.visit.code: r.pk for r in StockReservation.objects.select_related("visit")}
    c = Client()
    c.force_login(w.tech)
    html = c.get("/hub/stock/").content.decode()
    assert "(3 held for cancelled visits)" in html and 'data-cancelled-held="3"' in html
    rows = {code: html.split(f'data-reservation="{rid[code]}"', 1)[1].split("</tr>", 1)[0] for code in rid}
    for code in (gone1, gone2):
        assert (
            "Visit cancelled: release this" in rows[code]
            and f'data-release-cancelled="{rid[code]}"' in rows[code]
        )
    assert "Visit cancelled" not in rows[keep] and "data-release-cancelled" not in rows[keep]
    assert 'action="/hub/stock/release-cancelled/"' in html
    # roles without stock reservation rights are refused and nothing changes
    for role in ("field_operator", "engineer", "safety_reviewer", "admin"):
        u = make_user(f"cvx_{role}", role)
        c2 = Client()
        c2.force_login(u)
        r = c2.post("/hub/stock/release-cancelled/", {"_form_token": "x", "reservation": str(rid[gone1])})
        assert r.status_code == 403, role
    assert _level("RK").cancelled == 3
    # a visit that is not cancelled keeps its reservation: it needs the release page and a reason
    text = _release_now(w.tech, rid[keep])
    assert f"Visit {keep} is not cancelled" in text and "Nothing was changed." in text
    with pytest.raises(Conflict, match="is not cancelled"):
        st.release_for_cancelled_visit(w.ops, reservation_id=rid[keep])
    # the hub technician and the ops lead each release one in one click (hub_release_stock, audited reason)
    assert "Reservation released" in _release_now(w.tech, rid[gone1])
    assert "Reservation released" in _release_now(w.ops, rid[gone2])
    for code, who in ((gone1, w.tech), (gone2, w.ops)):
        r = StockReservation.objects.get(pk=rid[code])
        assert r.released_at is not None and r.released_by_id == who.pk
        assert r.released_reason == f"Visit {code} was cancelled"
        assert StockMovement.objects.filter(kind="release", reservation_id=r.pk).count() == 1
    lv = _level("RK")
    assert (lv.reserved, lv.cancelled, lv.available) == (1, 0, 5)
    html = c.get("/hub/stock/").content.decode()
    assert "held for cancelled visits" not in html and 'action="/hub/stock/release-cancelled/"' not in html
    # a second click on a released row changes nothing
    assert "already released" in _release_now(w.tech, rid[gone1])
    assert StockMovement.objects.filter(kind="release").count() == 2


@pytest.mark.django_db
def test_stock_page_statement_count_is_constant_with_1_and_6_cancelled_visit_reservations(monkeypatch):
    import platform_core.interaction as ia

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)
    w = _world("cc_")
    st.receive(w.tech, item_code="RK", quantity=20)
    c = Client()
    c.force_login(w.tech)

    def cancelled(n: int, offset: int) -> None:
        for k in range(offset, offset + n):
            code = _tomorrow_visit(w, 6 + k)
            st.reserve(w.tech, item_code="RK", visit_code=code, quantity=1)
            w.p.cancel_visit(w.ops, visit_code=code, reason="customer cancelled")

    cancelled(1, 0)
    one_row = _count(c, "/hub/stock/")
    cancelled(5, 1)
    six = _count(c, "/hub/stock/")
    html = c.get("/hub/stock/").content.decode()
    assert html.count("data-release-cancelled=") == 6 and 'data-cancelled-held="6"' in html
    assert html.count('action="/hub/stock/release-cancelled/"') == 1  # one form (one token) for every row
    assert one_row == six <= 20, (one_row, six)
