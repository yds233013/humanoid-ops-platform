"""v2 02B follow-ups (EM closure): function privileges, NULL / empty actor refusals, the Charging page
statement count, robot selects, forecast order and source wording. SYNTHETIC data only."""

from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from hub import charging as hc
from hub.models import Charger, ChargerReservation, ChargingSession, RobotSocReading
from platform_core.models import AuditEvent
from tests.test_v2_02b_charging import (
    _app_conn,
    _ids,
    _world,
)

pytestmark = pytest.mark.django_db

ENTRY = {
    "hub_reserve_charger",
    "hub_release_charger_reservation",
    "hub_start_charging",
    "hub_end_charging",
    "hub_record_soc_reading",
    "hub_receive_transfer",  # v2 03A-2 (hub/0013): receiving a stock transfer, same write boundary
}
HELPERS = {
    "hub_deny",
    "hub_actor_has",
    "hub_utc",
    "hub_audit",
    "hub_charge_conflict",
    "hub_lock_robot_charger",
    # v2 03A-1 (accounts/0005, hub/0010): the hub-qualified grant check and the same-hub / owner-change
    # triggers. Helpers too: not SECURITY DEFINER, not executable by ops_app or PUBLIC (asserted below).
    "hub_actor_has_at",
    "hub_same_hub_guard",
    "hub_owner_change_guard",
}
ROLES = ("public", "ops_app", "ops_owner")


@pytest.fixture(name="w")
def _w() -> Any:
    return _world()


# ------------------------------------------------------------------ A. function privileges
def _hub_functions() -> dict[str, dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            r"""SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner), p.provolatile,
                       has_function_privilege('public', p.oid, 'EXECUTE'),
                       has_function_privilege('ops_app', p.oid, 'EXECUTE'),
                       has_function_privilege('ops_owner', p.oid, 'EXECUTE')
                FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND p.proname LIKE 'hub\_%'"""
        )
        return {
            r[0]: {"definer": r[1], "owner": r[2], "volatile": r[3], **dict(zip(ROLES, r[4:], strict=True))}
            for r in cur.fetchall()
        }


def test_every_hub_function_has_exactly_the_intended_privileges():
    fns = {n: f for n, f in _hub_functions().items() if "stock" not in n}  # 02D's: test_v2_02d_inventory
    assert set(fns) == ENTRY | HELPERS  # nothing else was created by hub/0003-0004 (and 03A-1's helpers)
    for name, f in fns.items():
        assert f["owner"] == "ops_owner" and f["ops_owner"] is True, name
        assert f["public"] is False, name  # EXECUTE revoked from PUBLIC on every one
        if name in ENTRY:
            assert f["definer"] is True and f["ops_app"] is True, name
        else:  # helpers: not executable by the runtime role, and none of them is SECURITY DEFINER
            assert f["definer"] is False and f["ops_app"] is False, name


def test_no_security_definer_function_is_executable_by_public():
    """Across schema public. Trigger functions are excluded: PostgreSQL refuses to call them outside a trigger
    ("trigger functions can only be called as triggers"), so EXECUTE on them grants nothing."""
    with connection.cursor() as cur:
        cur.execute(
            """SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'public' AND p.prosecdef AND p.prorettype <> 'trigger'::regtype
                 AND has_function_privilege('public', p.oid, 'EXECUTE')"""
        )
        assert cur.fetchall() == []


def test_ops_app_cannot_call_a_helper_directly():
    conn = _app_conn()
    try:
        for sql in (
            "SELECT hub_utc(now())",
            "SELECT hub_deny('x')",
            "SELECT hub_audit(1, 'x', 'x', 'x', '{}', '{}', '')",
            "SELECT hub_lock_robot_charger(1, 1)",
        ):
            with (
                conn.cursor() as cur,
                pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"),
            ):
                cur.execute(sql)
    finally:
        conn.close()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor", ["NULL", "0", "''"])
def test_a_null_or_empty_actor_is_refused_by_every_entry_function(actor):
    _world("na_")
    r2, r3, c1, c2 = _ids("R-02", "R-03", "C-1", "C-2")
    from django.contrib.auth import get_user_model

    tech = get_user_model().objects.get(username="na_tech")
    now = timezone.now()
    rid = hc.reserve_charger(tech, charger_code="C-1", robot_code="R-02", starts_at=now + timedelta(hours=1),
                             ends_at=now + timedelta(hours=2))  # fmt: skip
    sid = hc.start_charging(
        tech, charger_code="C-2", robot_code="R-03", planned_until=now + timedelta(hours=1)
    )
    before = (
        list(ChargerReservation.objects.values_list("id", "released_at")),
        list(ChargingSession.objects.values_list("id", "ended_at")),
        RobotSocReading.objects.count(),
        AuditEvent.objects.count(),
    )
    a = f"{actor}::bigint" if actor != "''" else "''"
    calls = [
        f"SELECT hub_reserve_charger({a}, {c1}, {r3}, now() + interval '3 hours', now() + interval '4 hours', NULL, '')",
        f"SELECT hub_release_charger_reservation({a}, {rid}, 'x', '')",
        f"SELECT hub_start_charging({a}, {c1}, {r2}, NULL, now() + interval '30 minutes', '')",
        f"SELECT hub_end_charging({a}, {sid}, 90, '')",
        f"SELECT hub_record_soc_reading({a}, {r2}, 50, NULL, '')",
    ]
    conn = _app_conn()
    try:
        for sql in calls:
            expected = (
                psycopg.errors.InsufficientPrivilege
                if actor != "''"
                else psycopg.errors.InvalidTextRepresentation
            )
            with conn.cursor() as cur, pytest.raises(expected):
                cur.execute(sql)
    finally:
        conn.close()
    after = (
        list(ChargerReservation.objects.values_list("id", "released_at")),
        list(ChargingSession.objects.values_list("id", "ended_at")),
        RobotSocReading.objects.count(),
        AuditEvent.objects.count(),
    )
    assert after == before


# ------------------------------------------------------------------ B. Charging page statement count
def _count(c: Client) -> int:
    with CaptureQueriesContext(connection) as q:
        assert c.get("/hub/charging/").status_code == 200
    return len(q)


CHARGING_PAGE_BUDGET = 25


def test_charging_page_statement_count_is_constant_in_chargers_and_visits(monkeypatch):
    import platform_core.interaction as ia

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the count
    from tests.planning_world import new_visit

    w = _world("sc_")
    from tests.fleet_world import set_setting

    set_setting("ops_soc_fresh_min", "240")
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=30)
    day = (timezone.now() + timedelta(days=1)).date()

    def visits(n: int, offset: int) -> None:
        for k in range(offset, offset + n):
            code = new_visit(w, d=day, h0=6 + k % 12, h1=7 + k % 12)
            # a blocked plan still names the robot, which is all the forecast needs
            w.p.plan_visit(w.ops, visit_code=code, robot_code=f"R-0{1 + k % 3}", lead_code="P-1",
                           second_code="P-2", vehicle_code="VAN-1")  # fmt: skip

    visits(5, 0)  # 5 visits, 4 chargers
    c = Client()
    c.force_login(w.tech)
    small = _count(c)
    for n in range(10, 16):
        hc.create_charger(w.ops, code=f"C-{n}", name=f"Bay {n}")
    visits(35, 5)  # 40 visits, 10 chargers
    big = _count(c)
    html = c.get("/hub/charging/").content.decode()
    assert html.count('data-visit="') == 40 and Charger.objects.count() == 10
    assert small == big <= CHARGING_PAGE_BUDGET, (small, big)
    print(f"/hub/charging/ statements: {small} (budget {CHARGING_PAGE_BUDGET})")


# ------------------------------------------------------------------ D, E, F. selects, order, wording
def test_robot_fields_are_selects_of_named_robots_in_service_first(w):
    from fleet import services as fs

    fs.transition_lifecycle(w.lead, robot_code="R-03", to="validation", reason="back to validation")
    html = Client()
    html.force_login(w.tech)
    page = html.get("/hub/charging/").content.decode()
    import re

    for form in ("start", "soc", "reserve"):
        block = page.split(f'action="/hub/charging/{form}/"', 1)[1].split("</form>", 1)[0]
        assert '<input name="robot"' not in block, form
        options = re.findall(
            r'<option value="(R-\d+)"[^>]*>([^<]+)</option>', block.split('name="robot"', 1)[1]
        )
        assert [code for code, _ in options] == ["R-01", "R-02", "R-03"], (form, options)
        assert options[0][1].startswith("R-01 · in service") and "not in service" in options[2][1]
    # the server still validates: an unknown robot is refused in plain words
    token = re.search(r'name="_form_token" value="([^"]+)"', page).group(1)
    r = html.post("/hub/charging/soc/", {"_form_token": token, "robot": "R-99", "soc": "50"}, follow=True)
    assert "No robot R-99" in r.content.decode()


def test_forecast_table_is_sorted_by_departure_time(w):
    from tests.fleet_world import set_setting
    from tests.planning_world import new_visit

    set_setting("ops_soc_fresh_min", "240")
    day = (timezone.now() + timedelta(days=1)).date()
    late = new_visit(w, d=day, h0=15, h1=16, travel=30)
    early = new_visit(w, d=day, h0=11, h1=12, travel=120)  # leaves 09:00: before the 14:30 departure
    for code, robot in ((late, "R-02"), (early, "R-03")):
        w.p.plan_visit(w.ops, visit_code=code, robot_code=robot, lead_code="P-1", second_code="P-2",
                       vehicle_code="VAN-1")  # fmt: skip
    c = Client()
    c.force_login(w.tech)
    page = c.get("/hub/charging/").content.decode()
    assert page.index(f'data-visit="{early}"') < page.index(f'data-visit="{late}"')


def test_source_words_read_naturally(w):
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=40)
    page = Client()
    page.force_login(w.ops)
    html = page.get("/fleet/R-02/").content.decode()
    assert "from a reading entered by hand" in html
    assert hc.SOURCE_PHRASES == {
        "battery_check": "from a battery check",
        "charging_session_end": "from the end of a charging session",
        "manual_reading": "from a reading entered by hand",
    }
    assert "from a end" not in html


# ------------------------------------------------------------------ C. Hub statement count with charging tasks
def _hub_count(c: Client) -> int:
    assert c.get("/hub/").status_code == 200  # warm-up: derives any new task once
    with CaptureQueriesContext(connection) as q:
        assert c.get("/hub/").status_code == 200
    return len(q)


def test_hub_statement_count_is_identical_with_2_and_20_derived_charging_tasks(monkeypatch):
    import platform_core.interaction as ia
    from hub.models import WorkOrder
    from tests.fleet_world import set_setting

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the count
    w = _world("ct_")
    set_setting("ops_soc_fresh_min", "240")
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=20)  # fresh, below the departure minimum
    base = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)

    def visits(ks: range) -> None:
        for k in ks:  # one visit every 2 hours, all inside the two-day charge horizon
            start = base + timedelta(hours=2 * k)
            code = w.p.request_visit(
                w.ops, site_code="S-1", on_site_start=start, on_site_end=start + timedelta(hours=1),
                travel_out_min=30, travel_back_min=30, tasks=["T-04"],
            )["entity_id"]  # fmt: skip
            w.p.plan_visit(w.ops, visit_code=code, robot_code="R-02", lead_code="P-1", second_code="P-2",
                           vehicle_code="VAN-1")  # fmt: skip

    def charging() -> int:
        return WorkOrder.objects.filter(source_kind="charge_forecast").count()

    c = Client()
    c.force_login(w.ops)
    visits(range(2))
    two = _hub_count(c)
    assert charging() == 2
    visits(range(2, 20))
    twenty = _hub_count(c)
    assert charging() == 20
    assert two == twenty, (two, twenty)
    print(f"/hub/ statements: {two} with 2 and with 20 derived charging tasks")
