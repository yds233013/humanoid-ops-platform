"""v2 Phase 02, milestone 02B: charging (docs/11_v2_phase02_contract.md 4 row 02B, 8.3, 8.4, 8.6, 8.8).

Write boundary (ops_app SELECT only; SECURITY DEFINER functions check the actor's role); conflict safety within
and across tables, including simultaneous attempts in both orders; append-only readings; the truthful forecast
(Enough / Needs charge / Unknown, never a percentage); Today, planner, Suggest and Hub integration; no departure
shortcut; the 02B role matrix with negative calls; screens. SYNTHETIC data only.
"""

import os
import re
import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.utils import timezone

from accounts.roles import ALL_ROLES, PERMISSIONS
from execution.models import CustodyEvent
from fleet import services as fs
from fleet.models import Hold, Robot
from hub import charging as hc
from hub import derive
from hub import services as hs
from hub.models import Charger, ChargerReservation, ChargingSession, RobotSocReading, WorkOrder
from planning.models import Visit
from platform_core.commands import Conflict, PermissionDenied
from platform_core.models import AuditEvent
from readiness.models import DepartureAuthorization, ReadinessEvidence
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.readiness_world import MIN_DEPARTURE_SOC, build_readiness

pytestmark = pytest.mark.django_db
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')
APP = settings.DB_APP_ROLE
GUARDED = ("hub_charger_reservation", "hub_charging_session", "robot_soc_reading")


# ------------------------------------------------------------------ world
def _world(prefix: str = "") -> SimpleNamespace:
    w = build_readiness(prefix=prefix)
    w.tech = make_user(f"{prefix}tech", "hub_technician")
    w.p.register_person(w.ops, username=w.tech.username, code="P-8")
    w.admin = make_user(f"{prefix}admin", "admin")
    for code in ("C-1", "C-2", "C-3"):
        hc.create_charger(w.admin, code=code, name=f"Bay {code[-1]}")
    hc.create_charger(w.ops, code="C-9", name="Spare")
    hc.set_charger_in_service(w.ops, code="C-9", in_service=False, reason="Cable damaged")
    return w


@pytest.fixture
def w() -> SimpleNamespace:
    return _world()


def _now() -> Any:
    return timezone.now()


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _app_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )


def _ids(*codes: str) -> list[int]:
    return [
        Robot.objects.get(code=c).pk if c.startswith("R-") else Charger.objects.get(code=c).pk for c in codes
    ]


# ------------------------------------------------------------------ 8.8 write boundary
def test_ops_app_privileges_are_exactly_select_on_the_guarded_tables(w):
    with connection.cursor() as cur:
        for table in GUARDED:
            cur.execute(
                "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p"
                " WHERE has_table_privilege(%s, %s, p)",
                [APP, table],
            )
            assert [r[0] for r in cur.fetchall()] == ["SELECT"], table
            cur.execute("SELECT has_sequence_privilege(%s, %s, 'USAGE')", [APP, f"{table}_id_seq"])
            assert cur.fetchone()[0] is False
        cur.execute(
            "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p"
            " WHERE has_table_privilege(%s, 'hub_charger', p)",
            [APP],
        )
        assert [r[0] for r in cur.fetchall()] == [
            "SELECT",
            "INSERT",
            "UPDATE",
        ]  # configuration, never deleted
        for fn in ("hub_reserve_charger", "hub_release_charger_reservation", "hub_start_charging",
                   "hub_end_charging", "hub_record_soc_reading"):  # fmt: skip
            cur.execute(
                "SELECT bool_and(has_function_privilege(%s, p.oid, 'EXECUTE')), bool_and(p.prosecdef), "
                "bool_and(pg_get_userbyid(p.proowner) <> %s), "
                "bool_or(has_function_privilege('public', p.oid, 'EXECUTE')) FROM pg_proc p WHERE p.proname = %s",
                [APP, APP, fn],
            )
            assert cur.fetchone() == (True, True, True, False), fn


@pytest.mark.django_db(transaction=True)
def test_ops_app_cannot_write_the_guarded_tables_or_forge_an_actor():
    w = _world("wb_")
    r2, c1 = _ids("R-02", "C-1")
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=50)
    sid = hc.start_charging(
        w.tech, charger_code="C-1", robot_code="R-02", planned_until=_now() + timedelta(hours=1)
    )
    reading = RobotSocReading.objects.get()
    conn = _app_conn()
    uid = w.tech.pk
    try:
        attempts = [
            ("INSERT INTO robot_soc_reading (robot_id, soc_pct, observed_at, source, recorded_by_id, recorded_at)"
             " VALUES (%s, 99, now(), 'manual_reading', %s, now())", [r2, uid]),
            ("UPDATE robot_soc_reading SET soc_pct = 99 WHERE id = %s", [reading.pk]),
            ("DELETE FROM robot_soc_reading WHERE id = %s", [reading.pk]),
            ("INSERT INTO hub_charger_reservation (charger_id, robot_id, starts_at, ends_at, created_by_id, created_at,"
             " released_reason) VALUES (%s, %s, now(), now() + interval '1 hour', %s, now(), '')", [c1, r2, uid]),
            ("UPDATE hub_charging_session SET ended_at = now(), end_soc_pct = 100 WHERE id = %s", [sid]),
            ("DELETE FROM hub_charging_session WHERE id = %s", [sid]),
            ("INSERT INTO hub_charging_session (charger_id, robot_id, started_at, planned_until, started_by_id)"
             " VALUES (%s, %s, now(), now() + interval '1 hour', %s)", [c1, r2, uid]),
            ("DELETE FROM hub_charger WHERE id = %s", [c1]),
        ]  # fmt: skip
        for sql, params in attempts:
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql, params)
        # a forged actor: an existing user WITHOUT the role, named in a direct function call
        forger = make_user("wb_forger", "engineering_lead")  # may not record charging or reserve (8.6)
        calls = [
            ("SELECT hub_record_soc_reading(%s, %s, 99, NULL, '')", [forger.pk, r2]),
            ("SELECT hub_reserve_charger(%s, %s, %s, now() + interval '1 hour', now() + interval '2 hours', NULL, '')",
             [forger.pk, _ids("C-2")[0], _ids("R-03")[0]]),
            ("SELECT hub_end_charging(%s, %s, 100, '')", [forger.pk, sid]),
            ("SELECT hub_start_charging(%s, %s, %s, NULL, now() + interval '1 hour', '')",
             [forger.pk, _ids("C-3")[0], _ids("R-03")[0]]),
            ("SELECT hub_record_soc_reading(%s, %s, 99, NULL, '')", [987654, r2]),  # no such user
        ]  # fmt: skip
        for sql, params in calls:
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql, params)
        # the real actor through the function works for ops_app
        with conn.cursor() as cur:
            cur.execute("SELECT hub_record_soc_reading(%s, %s, 77, NULL, 'rid')", [uid, r2])
            assert cur.fetchone()[0] > 0
    finally:
        conn.close()
    assert RobotSocReading.objects.count() == 2 and ChargingSession.objects.get().ended_at is None
    assert ChargerReservation.objects.count() == 0 and Charger.objects.filter(pk=c1).exists()
    assert AuditEvent.objects.filter(command="hub.record_soc_reading", outcome="committed").count() == 2


def test_readings_are_append_only_even_for_the_owner(w):
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=50)
    from django.db import transaction

    with pytest.raises(Exception, match="append-only"), transaction.atomic(), connection.cursor() as cur:
        cur.execute("UPDATE robot_soc_reading SET soc_pct = 90")
    with pytest.raises(Conflict, match="future"):
        hc.record_soc(w.tech, robot_code="R-02", soc_pct=50, observed_at=_now() + timedelta(minutes=5))
    with pytest.raises(Conflict, match="last 24 hours"):
        hc.record_soc(w.tech, robot_code="R-02", soc_pct=50, observed_at=_now() - timedelta(hours=30))
    with pytest.raises(Conflict, match="0 to 100"):
        hc.record_soc(w.tech, robot_code="R-02", soc_pct=101)


# ------------------------------------------------------------------ 8.4 conflicts
def test_reservations_conflict_on_charger_and_robot_in_plain_words(w):
    t0 = _now() + timedelta(hours=1)
    rid = hc.reserve_charger(
        w.tech, charger_code="C-1", robot_code="R-02", starts_at=t0, ends_at=t0 + timedelta(hours=2)
    )
    with pytest.raises(Conflict, match=r"Charger C-1 is already booked for R-02 from .* to "):
        hc.reserve_charger(w.ops, charger_code="C-1", robot_code="R-03", starts_at=t0 + timedelta(minutes=30),
                           ends_at=t0 + timedelta(hours=3))  # fmt: skip
    with pytest.raises(Conflict, match="Robot R-02 is already booked on charger C-1"):
        hc.reserve_charger(
            w.ops, charger_code="C-2", robot_code="R-02", starts_at=t0, ends_at=t0 + timedelta(hours=1)
        )
    with pytest.raises(Conflict, match="out of service"):
        hc.reserve_charger(
            w.ops, charger_code="C-9", robot_code="R-03", starts_at=t0, ends_at=t0 + timedelta(hours=1)
        )
    # back to back is fine (half-open periods); after a release the slot is free
    hc.reserve_charger(w.ops, charger_code="C-1", robot_code="R-03", starts_at=t0 + timedelta(hours=2),
                       ends_at=t0 + timedelta(hours=3))  # fmt: skip
    hc.release_reservation(w.tech, reservation_id=rid, reason="Robot charged elsewhere")
    hc.reserve_charger(
        w.ops, charger_code="C-1", robot_code="R-03", starts_at=t0, ends_at=t0 + timedelta(hours=1)
    )
    assert AuditEvent.objects.filter(command="hub.reserve_charger", outcome="denied").count() == 3


def test_sessions_and_reservations_do_not_overlap_incompatibly(w):
    now = _now()
    hc.reserve_charger(w.tech, charger_code="C-1", robot_code="R-02", starts_at=now - timedelta(minutes=5),
                       ends_at=now + timedelta(hours=2))  # fmt: skip
    # another robot cannot start on the booked charger; the booked robot can (and the session uses the booking)
    with pytest.raises(Conflict, match="Charger C-1 is already booked for R-02"):
        hc.start_charging(
            w.field, charger_code="C-1", robot_code="R-03", planned_until=now + timedelta(hours=1)
        )
    sid = hc.start_charging(
        w.field, charger_code="C-1", robot_code="R-02", planned_until=now + timedelta(hours=1)
    )
    assert ChargingSession.objects.get(pk=sid).reservation is not None
    # one open session per robot and per charger
    with pytest.raises(Conflict, match="Robot R-02 is (charging|already booked) on charger C-1"):
        hc.start_charging(
            w.field, charger_code="C-2", robot_code="R-02", planned_until=now + timedelta(hours=1)
        )
    # a booking of the busy charger for another robot during the session's planned time is refused
    with pytest.raises(Conflict, match="Charger C-1"):
        hc.reserve_charger(w.ops, charger_code="C-1", robot_code="R-03", starts_at=now + timedelta(minutes=30),
                           ends_at=now + timedelta(hours=2))  # fmt: skip
    hc.end_charging(w.field, session_id=sid, end_soc_pct=90)
    with pytest.raises(Conflict, match="already ended"):
        hc.end_charging(w.field, session_id=sid, end_soc_pct=95)
    # the database constraint is the last line: an overlapping open session cannot be inserted even by the owner
    with (
        connection.cursor() as cur,
        pytest.raises(Exception, match="hub_session_no_charger_overlap|conflicting key"),
    ):
        c2, r3 = _ids("C-2", "R-03")
        for _ in range(2):
            cur.execute(
                "INSERT INTO hub_charging_session (charger_id, robot_id, started_at, planned_until, started_by_id)"
                " VALUES (%s, %s, now(), now() + interval '1 hour', %s)",
                [c2, r3, w.tech.pk],
            )


def test_a_visit_booking_of_the_robot_blocks_charging_but_its_charging_window_does_not(w):
    visit = Visit.objects.get(
        code=w.visit
    )  # R-01 on DAY 10:00-11:00 in Los Angeles, planned with reservations
    rev = visit.current_revision
    with pytest.raises(Conflict, match=f"Robot R-01 is booked for visit {w.visit} \\(on site\\)"):
        hc.reserve_charger(w.tech, charger_code="C-1", robot_code="R-01", starts_at=rev.on_site_start,
                           ends_at=rev.on_site_end)  # fmt: skip
    charging = visit.current_revision.reservations.get(purpose="charging", released_at__isnull=True)
    hc.reserve_charger(w.tech, charger_code="C-1", robot_code="R-01", starts_at=charging.starts_at,
                       ends_at=charging.ends_at)  # the planned post-visit charging: compatible  # fmt: skip


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "case",
    [
        "reservation_vs_reservation_charger",
        "reservation_vs_reservation_robot",
        "session_vs_session_robot",
        "session_vs_session_charger",
        "reservation_then_session_charger",
        "session_then_reservation_charger",
        "reservation_then_session_robot",
        "session_then_reservation_robot",
    ],
)
def test_two_simultaneous_conflicting_attempts_cannot_both_commit(case):
    w = _world("cc_")
    c1, c2, r2, r3 = _ids("C-1", "C-2", "R-02", "R-03")
    uid = w.tech.pk
    res = "SELECT hub_reserve_charger(%s, %s, %s, now() - interval '1 minute', now() + interval '2 hours', NULL, '')"
    ses = "SELECT hub_start_charging(%s, %s, %s, NULL, now() + interval '1 hour', '')"
    first, second = {
        "reservation_vs_reservation_charger": ((res, [uid, c1, r2]), (res, [uid, c1, r3])),
        "reservation_vs_reservation_robot": ((res, [uid, c1, r2]), (res, [uid, c2, r2])),
        "session_vs_session_robot": ((ses, [uid, c1, r2]), (ses, [uid, c2, r2])),
        "session_vs_session_charger": ((ses, [uid, c1, r2]), (ses, [uid, c1, r3])),
        "reservation_then_session_charger": ((res, [uid, c1, r2]), (ses, [uid, c1, r3])),
        "session_then_reservation_charger": ((ses, [uid, c1, r2]), (res, [uid, c1, r3])),
        "reservation_then_session_robot": ((res, [uid, c1, r2]), (ses, [uid, c2, r2])),
        "session_then_reservation_robot": ((ses, [uid, c1, r2]), (res, [uid, c2, r2])),
    }[case]
    a, b = _app_conn(), _app_conn()
    out: dict[str, Any] = {}
    try:
        a.autocommit = False
        with a.cursor() as ca:
            ca.execute(*first)  # holds the robot row and charger row locks, uncommitted

        def go() -> None:
            try:
                with b.cursor() as cb:
                    cb.execute(*second)
                out["second"] = "committed"
            except psycopg.Error as exc:
                out["second"] = exc

        t = threading.Thread(target=go)
        t.start()
        t.join(1.0)
        assert t.is_alive(), "the second attempt must wait for the first one's locks"
        a.commit()
        t.join(20)
    finally:
        a.close()
        b.close()
    assert isinstance(out["second"], psycopg.errors.CheckViolation), out
    assert ChargerReservation.objects.count() + ChargingSession.objects.count() == 1


# ------------------------------------------------------------------ 8.3 forecast
def _visit_forecast(w: SimpleNamespace, robot: str = "R-02") -> hc.Forecast:
    rid = Robot.objects.get(code=robot).pk
    return hc.robot_forecasts([rid], _now() + timedelta(days=1))[rid]


def test_the_forecast_is_unknown_without_the_setting_a_reading_or_a_fresh_reading(w):
    f = _visit_forecast(w)
    assert (f.state, f.soc) == ("unknown", None) and "ops_soc_fresh_min" in f.reason
    set_setting("ops_soc_fresh_min", "120")
    assert _visit_forecast(w).reason == "no battery level is recorded"
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=90, observed_at=_now() - timedelta(hours=3))
    f = _visit_forecast(w)
    assert f.state == "unknown" and "too old" in f.reason and f.soc == 90  # last recorded is still shown
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=90)
    f = _visit_forecast(w)
    assert (f.state, f.label, f.min_soc) == ("enough", "Enough", MIN_DEPARTURE_SOC)
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=MIN_DEPARTURE_SOC - 1)
    f = _visit_forecast(w)
    assert f.state == "needs_charge" and not f.cannot_in_time and f.source == "manual_reading"
    # only the approved charge turnaround (180 min in this world) can prove "cannot charge in time"
    rid = Robot.objects.get(code="R-02").pk
    soon = hc.robot_forecasts([rid], _now() + timedelta(minutes=90))[rid]
    assert soon.cannot_in_time and "180-minute charge turnaround" in soon.reason
    # no percentage is ever invented: the only numbers shown are recorded ones
    assert soon.soc == MIN_DEPARTURE_SOC - 1


def test_battery_check_evidence_counts_as_a_reading_without_being_copied(w):
    set_setting("ops_soc_fresh_min", "120")
    f = hc.visit_forecasts([Visit.objects.get(code=w.visit).pk])[Visit.objects.get(code=w.visit).pk]
    assert f.source == "battery_check" and f.state == "enough"  # the world's passing battery check (85%)
    assert RobotSocReading.objects.count() == 0


# ------------------------------------------------------------------ 8.1 / EM 5: no departure shortcut
def _safety() -> dict[str, Any]:
    return {
        "evidence": list(ReadinessEvidence.objects.order_by("id").values_list("id", "kind", "result")),
        "authorizations": DepartureAuthorization.objects.count(),
        "holds": list(Hold.objects.order_by("id").values_list("id", "released_at")),
        "custody": CustodyEvent.objects.count(),
        "eligibility": {
            r.code: fs.evaluate_eligibility(r).eligible_for_planning for r in Robot.objects.all()
        },
    }


def test_charging_and_charging_tasks_never_create_evidence_or_authorize(w):
    set_setting("ops_soc_fresh_min", "120")
    fs.place_hold(w.ops, scope="robot", target="R-03", source_type="maintenance", reason="noisy wrist")
    before = _safety()
    now = _now()
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=20)
    hc.reserve_charger(w.tech, charger_code="C-2", robot_code="R-02", starts_at=now - timedelta(minutes=1),
                       ends_at=now + timedelta(hours=2))  # fmt: skip
    sid = hc.start_charging(
        w.tech, charger_code="C-2", robot_code="R-02", planned_until=now + timedelta(hours=1)
    )
    hc.end_charging(w.tech, session_id=sid, end_soc_pct=100)
    reading = RobotSocReading.objects.filter(source="charging_session_end").get()
    assert reading.soc_pct == 100 and reading.session_id == sid
    task = int(
        hs.create_task(w.ops, type="charging", title="Charge R-02", robot_code="R-02", owner_code="P-8")[
            "entity_id"
        ]
    )
    hs.start_task(w.tech, task_id=task)
    hs.complete_task(w.tech, task_id=task)
    hc.start_charging(w.tech, charger_code="C-1", robot_code="R-03", planned_until=now + timedelta(hours=1))
    assert (
        _safety() == before
    )  # no battery evidence, no authorization, hold still there, custody, eligibility


# ------------------------------------------------------------------ 8.6 roles
def test_the_02b_role_matrix():
    assert PERMISSIONS["hub.record_charging"] == {"hub_technician", "field_operator", "ops_lead", "engineer"}
    assert PERMISSIONS["hub.reserve_charger"] == {"hub_technician", "ops_lead"}
    assert PERMISSIONS["hub.manage_chargers"] == {"admin", "ops_lead"}
    assert {p for p, roles in PERMISSIONS.items() if "hub_technician" in roles} == {
        "hub.view", "hub.view_all", "hub.work", "hub.claim", "hub.nav", "hub.record_charging", "hub.reserve_charger",
        "fleet.record_maintenance",  # v2 02C (8.6 "Record maintenance: if qualified")
    }  # fmt: skip


@pytest.mark.parametrize("role", ALL_ROLES)
def test_every_role_is_refused_what_it_may_not_do(w, role):
    user = make_user(f"cr_{role}", role)
    now = _now()
    counts = (RobotSocReading.objects.count(), ChargerReservation.objects.count(), ChargingSession.objects.count(),
              Charger.objects.count())  # fmt: skip
    record_ok = role in PERMISSIONS["hub.record_charging"]
    reserve_ok = role in PERMISSIONS["hub.reserve_charger"]
    manage_ok = role in PERMISSIONS["hub.manage_chargers"]
    if not record_ok:
        with pytest.raises(PermissionDenied):
            hc.record_soc(user, robot_code="R-02", soc_pct=50)
        with pytest.raises(PermissionDenied):
            hc.start_charging(
                user, charger_code="C-3", robot_code="R-03", planned_until=now + timedelta(hours=1)
            )
    if not reserve_ok:
        with pytest.raises(PermissionDenied):
            hc.reserve_charger(user, charger_code="C-2", robot_code="R-02", starts_at=now + timedelta(hours=1),
                               ends_at=now + timedelta(hours=2))  # fmt: skip
    if not manage_ok:
        with pytest.raises(PermissionDenied):
            hc.create_charger(user, code="C-7", name="x")
    assert counts == (RobotSocReading.objects.count(), ChargerReservation.objects.count(),
                      ChargingSession.objects.count(), Charger.objects.count())  # fmt: skip
    # the screens: POSTs without the permission are 403 and write nothing
    c = _client(user)
    for url, data, ok in (
        ("/hub/charging/soc/", {"robot": "R-02", "soc": "50"}, record_ok),
        ("/hub/charging/start/", {"charger": "C-3", "robot": "R-03", "until": "2030-01-01T10:00"}, record_ok),
        ("/hub/charging/reserve/", {"charger": "C-2", "robot": "R-02", "starts": "2030-01-01T10:00",
                                    "ends": "2030-01-01T11:00"}, reserve_ok),
    ):  # fmt: skip
        if not ok:
            assert c.post(url, data).status_code == 403, (role, url)
    if not manage_ok:
        assert c.get("/hub/charging/chargers/").status_code == 403
    assert counts == (RobotSocReading.objects.count(), ChargerReservation.objects.count(),
                      ChargingSession.objects.count(), Charger.objects.count())  # fmt: skip
    page = c.get("/hub/charging/")
    assert page.status_code == (200 if role in PERMISSIONS["hub.view"] else 403), role


# ------------------------------------------------------------------ integration: planner, Suggest, Hub, robot page
def test_planner_labels_unknown_and_excludes_only_robots_that_cannot_charge_in_time(w):
    from planning import options as opt
    from tests.planning_world import new_visit

    set_setting("ops_soc_fresh_min", "120")
    soon = _now() + timedelta(hours=3)  # departure in ~2.5 h: shorter than the 180-minute approved turnaround
    code = w.p.request_visit(
        w.ops, site_code="S-1", on_site_start=soon, on_site_end=soon + timedelta(hours=1), travel_out_min=30,
        travel_back_min=30, tasks=["T-04"],
    )["entity_id"]  # fmt: skip
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=10)  # needs charge, cannot be charged in time
    opts = opt.plan_options(Visit.objects.get(code=code))
    labels = {o.code: (o.status, o.label) for o in opts.robots}
    assert labels["R-02"] == ("unusable", "R-02 · cannot charge in time")
    assert labels["R-01"][1] == "R-01 · free"  # enough: its fresh passing battery check (85%)
    assert labels["R-03"] == ("free", "R-03 · free · charge forecast unknown")  # nothing recorded: selectable
    s = opt.suggest(Visit.objects.get(code=code), opts)
    assert s.robot is None or s.robot.code != "R-02"
    later = new_visit(w, d=(_now() + timedelta(days=3)).date())
    far = {o.code: o for o in opt.plan_options(Visit.objects.get(code=later)).robots}
    assert far["R-02"].status != "unusable" and far["R-02"].label.endswith(
        "needs charge"
    )  # time enough to charge
    html = _client(w.ops).get(f"/planning/visits/{w.visit}/").content.decode()
    assert 'data-charge="enough"' in html and "departure still needs the battery check" in html


def test_needs_charge_visits_become_hub_charging_tasks_and_clear_when_charged(w):
    set_setting("ops_soc_fresh_min", "120")
    start = _now() + timedelta(hours=20)
    code = w.p.request_visit(
        w.ops, site_code="S-1", on_site_start=start, on_site_end=start + timedelta(hours=1), travel_out_min=30,
        travel_back_min=30, tasks=["T-04"],
    )["entity_id"]  # fmt: skip
    w.p.plan_visit(
        w.ops, visit_code=code, robot_code="R-02", lead_code="P-1", second_code="P-2", vehicle_code="VAN-1"
    )
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=20)
    for _ in range(3):
        derive.refresh()
    (t,) = WorkOrder.objects.filter(source_kind="charge_forecast")
    assert (t.type, t.source_ref, t.link_url) == ("charging", f"{code}:R-02", "/hub/charging/")
    assert t.title == f"Charge R-02 before visit {code}"
    hc.record_soc(w.tech, robot_code="R-02", soc_pct=95)
    derive.refresh()
    t.refresh_from_db()
    assert t.source_cleared_at is not None and t.status == "open"  # resolved by the record, never auto-done


def test_screens_show_the_three_battery_facts_and_the_charging_section(w):
    set_setting("ops_soc_fresh_min", "120")
    hc.record_soc(w.tech, robot_code="R-01", soc_pct=40)
    robot = _client(w.ops).get("/fleet/R-01/").content.decode()
    for marker in ('data-battery-last="1"', 'data-battery-estimate="1"', 'data-battery-evidence="1"'):
        assert marker in robot
    assert "40%" in robot and "Needs charge" in robot and "never count as that check" in robot
    page = _client(w.tech).get("/hub/charging/").content.decode()
    for words in ("Chargers", "Charging now", "Bookings today and tomorrow", "Start charging",
                  "Record a battery level", "Book a charger", "Out of service"):  # fmt: skip
        assert words in page, words
    assert "<script" not in page.split("<main", 1)[-1].split("</main>")[0]
    # a start through the page (one-time token), then a replay does nothing
    c = _client(w.tech)
    token = TOKEN_RE.search(c.get("/hub/charging/").content.decode()).group(1)
    until = (
        (_now() + timedelta(hours=2)).astimezone(timezone.get_default_timezone()).strftime("%Y-%m-%dT%H:%M")
    )
    data = {"_form_token": token, "charger": "C-1", "robot": "R-02", "until": until, "start_soc": "30"}
    c.post("/hub/charging/start/", data)
    c.post("/hub/charging/start/", data)
    assert ChargingSession.objects.filter(robot__code="R-02").count() == 1
    off = _client(w.tech)
    set_setting("ops_soc_fresh_min", "")
    assert "ops_soc_fresh_min) is not provisioned" in off.get("/hub/charging/").content.decode()


@pytest.mark.django_db(transaction=True)
def test_today_marks_visits_at_risk_with_the_charge_reason(monkeypatch):
    from operations import today
    from tests.planning_world import DAY

    w = _world("td_")
    monkeypatch.setattr(today, "local_day", lambda at: DAY)
    set_setting("ops_readiness_lookahead_min", str(60 * 24 * 90))
    c = _client(w.ops)
    body = c.get("/ops/").content.decode()
    assert "The charge forecast is off" in body
    set_setting("ops_soc_fresh_min", "60")
    hc.record_soc(w.tech, robot_code="R-01", soc_pct=10)  # newer than the visit's 85% battery check
    body = c.get("/ops/").content.decode()
    row = body.split(f">{w.visit}<", 1)[1].split("</li>", 1)[0] if f">{w.visit}<" in body else body
    assert "Needs charge before departure" in body and "At risk" in row
