"""v2 02E: the Northbay demo carries journey J8 ("Hub day") and the J2 / J3 / J4 facts of Phase 02.

One expensive test builds the demo once (the 01D test covers the rebuild and determinism) and checks that each J8
fact exists from records made through the real commands, that Today, the planner and the J4 visit show the
battery, maintenance and kit facts, and that completing demo work orders changes no safety state (contract 11,
section 8.1). Fictional data only.
"""

import io
import os
import re
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client
from django.utils import timezone

from execution.custody import current_custody, open_run_id
from execution.models import CustodyEvent, ExecutionRun, Incident, TripClosure
from fleet import maintenance as fm
from fleet import services as fs
from fleet.models import DeploymentSetting, Hold, Inspection, MaintenanceRecord, Robot
from hub import charging as hc
from hub import derive
from hub import services as hs
from hub import stock as st
from hub.models import WorkOrder
from operations import demo_workspace as demo
from operations.health import fleet_health
from operations.management.commands.log_predictor_signals import log_predictor_signals
from planning.models import Visit
from platform_core.models import AuditEvent
from readiness.evaluator import evaluate_readiness
from readiness.models import (
    AuthorizationRevocation,
    DepartureAuthorization,
    DepartureEvent,
    ReadinessEvidence,
)
from tests.fleet_world import set_setting

pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()
LA = ZoneInfo("America/Los_Angeles")
PASSWORD = "demo-test-password-4821"


def _text(html: str) -> str:
    html = re.sub(r"<style.*?</style>|<script.*?</script>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _client(username: str) -> Client:
    c = Client()
    c.force_login(User.objects.get(username=username))
    return c


def _maintenance() -> list[tuple[Any, ...]]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT robot_code, requirement_code, state, blocking, visits_used FROM fleet_maintenance_status_set("
            "(SELECT array_agg(id) FROM fleet_robot), fleet_production_mode(), now()) ORDER BY 1, 2"
        )
        return list(cur.fetchall())


def _stock() -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute("SELECT count(*), coalesce(sum(on_hand_delta), 0), coalesce(sum(reserved_delta), 0) "
                    "FROM hub_stock_movement")  # fmt: skip
        movements = cur.fetchone()
        cur.execute("SELECT id, quantity, released_at FROM hub_stock_reservation ORDER BY id")
        reservations = cur.fetchall()
    return {"movements": movements, "reservations": reservations}


def _safety_snapshot() -> dict[str, Any]:
    """Every safety-relevant fact a work order must never change (contract 11, 8.1)."""
    return {
        "eligibility": {
            r.code: (e.eligible_for_planning, tuple(sorted(map(str, e.reasons))))
            for r in Robot.objects.order_by("code")
            for e in [fs.evaluate_eligibility(r)]
        },
        "holds": list(Hold.objects.order_by("id").values_list("id", "released_at")),
        "evidence": list(ReadinessEvidence.objects.order_by("id").values_list("id", "result")),
        "authorizations": list(DepartureAuthorization.objects.order_by("id").values_list("id", "expires_at")),
        "revocations": AuthorizationRevocation.objects.count(),
        "departures": DepartureEvent.objects.count(),
        "custody": CustodyEvent.objects.count(),
        "inspections": Inspection.objects.count(),
        "maintenance_records": MaintenanceRecord.objects.count(),
        "maintenance_status": _maintenance(),
        "stock": _stock(),
    }


def _states() -> dict[str, Any]:
    """What a rebuild of the same date must reproduce."""
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM operations_predictor_observation")
        predictor = cur.fetchone()[0]
    return {
        "maintenance": [(r[0], r[1], r[2]) for r in _maintenance()],
        "holds": sorted(
            Hold.objects.filter(released_at__isnull=True).values_list("robot__code", "source_type")
        ),
        "runs": sorted(ExecutionRun.objects.values_list("code", "visit__code", "robot__code")),
        "closures": TripClosure.objects.count(),
        "authorizations": sorted(DepartureAuthorization.objects.values_list("visit__code", flat=True)),
        "predictor": predictor,
        "kit_short": sorted(n.visit_code for n in st.visit_needs() if n.short),
        "visits": sorted(Visit.objects.values_list("code", "status", "current_revision__robot__code")),
    }


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> tuple[date, str]:
    today = demo.workspace_today()
    monkeypatch.setattr(demo, "workspace_today", lambda: today)
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    out = io.StringIO()
    call_command("build_demo_workspace", stdout=out)
    return today, out.getvalue()


def test_hub_day_facts_exist_after_a_build_and_work_orders_change_no_safety_state(built: tuple[date, str]):
    today, out = built
    assert PASSWORD not in out
    m = re.search(r"Maintenance due soon on (NB-\d+); overdue \(blocked\) on (NB-\d+)", out)
    assert m, out
    due_soon, overdue = m[1], m[2]
    j4 = re.search(r"Journey J4: visit (V-\d+)", out)[1]
    lily, ops, kenji = _client("lily.tran"), _client("dana.ortiz"), _client("kenji.watanabe")
    hub_html = lily.get("/hub/").content.decode()  # the first Hub load derives the tasks
    hub = _text(hub_html)
    assert "Hub work queue" in hub_html
    tasks = WorkOrder.objects.all()

    # ---- EM ruling 8.9: exactly two closed practice runs, labelled, through the real commands, stamped now
    runs = list(ExecutionRun.objects.select_related("robot", "visit", "visit__site").order_by("code"))
    practice = {r.visit.code for r in runs}
    assert len(runs) == 2 and {r.robot.code for r in runs} == {due_soon, overdue}
    assert all(r.visit.site.code == demo.PRACTICE_HOME for r in runs)
    assert TripClosure.objects.filter(run__in=runs).count() == 2 == TripClosure.objects.count()
    assert set(DepartureAuthorization.objects.values_list("visit__code", flat=True)) == practice
    assert DepartureAuthorization.objects.count() == 2 == DepartureEvent.objects.count()
    assert all(timezone.now() - d < timedelta(hours=1) for d in DepartureEvent.objects.values_list(
        "departed_at", flat=True))  # fmt: skip
    # no open run and no unfinished custody transfer: every robot that moved is back at the hub
    assert not ExecutionRun.objects.exclude(pk__in=TripClosure.objects.values("run_id")).exists()
    for robot_id in CustodyEvent.objects.values_list("robot_id", flat=True).distinct():
        c = current_custody(robot_id)
        assert c.kind == "hub" and c.verified and open_run_id(robot_id) is None, c
    for r in runs:  # the label is visible on the visit, readiness and run pages
        for url in (f"/planning/visits/{r.visit.code}/", f"/readiness/visits/{r.visit.code}/",
                    f"/execution/runs/{r.code}/"):  # fmt: skip
            assert "Fictional practice visit" in ops.get(url).content.decode(), url

    # ---- 1. charging tasks / needs-charge risk
    soon = Visit.objects.filter(
        current_revision__on_site_start__gt=timezone.now(), current_revision__robot__isnull=False
    ).values_list("id", flat=True)
    assert hc.NEEDS in {f.state for f in hc.visit_forecasts(list(soon)).values()}
    assert tasks.filter(type="charging", source_kind="charge_forecast").exists()
    assert "Needs charge" in _text(lily.get("/hub/charging/").content.decode())

    # ---- 2. maintenance: gate active, demo rules labelled, due soon, overdue (blocked), unknown
    assert DeploymentSetting.objects.get(key="maintenance_gate_active").value == "true" and fm.gate_active()
    states = {(r[0], r[1]): r for r in _maintenance()}
    assert states[(due_soon, "GRIPPER-PADS")][2] == "due_soon"
    assert states[(due_soon, "SENSOR-CLEAN")][2] == "not_due"  # recorded after its run
    assert states[(overdue, "SENSOR-CLEAN")][2:4] == ("overdue", True)
    assert states[(overdue, "SENSOR-CLEAN")][4] == 1  # 1 closed visit
    assert states[(demo.NO_MAINTENANCE_BASELINE, "JOINT-SERVICE")][2] == "unknown"
    assert not fs.evaluate_eligibility(Robot.objects.get(code=overdue)).eligible_for_planning
    titles = set(tasks.filter(type="maintenance_due").values_list("title", flat=True))
    assert f"Maintenance GRIPPER-PADS of {due_soon} is due soon" in titles
    assert f"Maintenance SENSOR-CLEAN of {overdue} is overdue" in titles
    assert f"Maintenance JOINT-SERVICE of {demo.NO_MAINTENANCE_BASELINE} has an unknown status" in titles
    page = kenji.get(f"/fleet/{overdue}/maintenance/").content.decode()
    assert "Demo value" in page and "SENSOR-CLEAN" in page
    upcoming = Visit.objects.filter(
        current_revision__robot__code=overdue, current_revision__on_site_start__gt=timezone.now(),
        status="confirmed",
    ).order_by("current_revision__on_site_start").first()  # fmt: skip
    assert upcoming is not None
    blocked = evaluate_readiness(upcoming).blockers
    assert any("SENSOR-CLEAN overdue" in b["message"] for b in blocked), blocked
    assert "due soon" in _text(kenji.get(f"/fleet/{due_soon}/maintenance/").content.decode()).lower()

    # ---- 3. a periodic inspection due (the beta build: every day, demo value) and one never done
    inspection_titles = set(tasks.filter(type="inspection_due").values_list("title", flat=True))
    assert "Periodic inspection of NB-046 is due soon" in inspection_titles
    assert (
        f"Robot {demo.NEVER_INSPECTED} has no periodic inspection on its configuration" in inspection_titles
    )

    # ---- 4. a kit shortage on an upcoming visit and a restock task
    kit_visit = re.search(r"Kit short \(its only At-risk cause from the build\): (V-\d+)", out)[1]
    short = [n for n in st.visit_needs() if n.short]
    assert {(n.visit_code, n.item_code) for n in short} == {(kit_visit, "RK")}
    kv = Visit.objects.get(code=kit_visit)
    assert kv.site.code not in demo.SPECIAL_HOMES and kv.code != j4
    assert {b["code"] for b in evaluate_readiness(kv).blockers} == {"EVIDENCE_MISSING"}  # day's checks only
    assert hc.visit_forecasts([kv.pk])[kv.pk].state == hc.ENOUGH
    assert tasks.filter(type="kit_low", source_kind="stock_level").exists()  # gloves below their minimum
    assert tasks.filter(type="kit_low", source_kind="manual").exists()

    # ---- 5. a held robot
    assert Hold.objects.filter(released_at__isnull=True, robot__code=demo.MAINTENANCE_ROBOT).exists()
    assert "is on hold" in hub

    # ---- 6. fleet health and the predictor log
    assert log_predictor_signals() == 0  # the build already logged both departures
    with connection.cursor() as cur:
        cur.execute("SELECT count(*), count(*) FILTER (WHERE battery_state = 'known') "
                    "FROM operations_predictor_observation")  # fmt: skip
        assert cur.fetchone() == (2, 2)
    health = _text(ops.get("/ops/health/").content.decode())
    assert "Showing 2 of 2 logged departure(s)" in health
    assert re.search(r"Recorded availability .*? \d+(\.\d+)?%", health)
    assert "Nothing logged yet" not in health
    assert "configuration history is not reconstructed" in health
    # the figures are derived from recorded facts: recompute them from the lifecycle audit rows and the holds
    at = timezone.now()
    h = fleet_health(at - timedelta(days=7), at, at)
    in_service = {
        a.entity_id: a.occurred_at
        for a in AuditEvent.objects.filter(entity_type="robot", outcome="committed",
                                           command__startswith="fleet.lifecycle.").order_by("occurred_at")
        if a.result.get("to") == "in_service"
    }  # fmt: skip
    assert len(in_service) == demo.IN_SERVICE
    denominator = sum((at - t).total_seconds() for t in in_service.values())
    held = sum((at - max(hd.placed_at, in_service[hd.robot.code])).total_seconds()
               for hd in Hold.objects.filter(released_at__isnull=True, blocking=True, robot__isnull=False)
               if hd.robot.code in in_service)  # fmt: skip
    assert h.uptime.denominator_s == pytest.approx(denominator, rel=1e-3)
    assert h.uptime.numerator_s == pytest.approx(denominator - held, rel=1e-3)
    assert h.holds_total == Hold.objects.count() and h.incidents_total == Incident.objects.count()

    # ---- J2 Today: battery, maintenance and kit facts
    today_page = _text(ops.get("/ops/").content.decode())
    assert "Needs charge before departure" in today_page
    assert "SENSOR-CLEAN overdue" in today_page  # the overdue robot's visit tomorrow is blocked
    kit_line = today_page[today_page.index(f"{kit_visit} robot") :]
    kit_line = kit_line[: kit_line.index(" V-", 5)]
    # At risk with Kit short; when the build runs late in the day, Today's readiness look-ahead also evaluates
    # tomorrow's visit and adds the day's own check, which is expected (the visit misses only the day's checks).
    assert "At risk" in kit_line and "Kit short" in kit_line, kit_line
    rest = kit_line.split("At risk", 1)[1].replace("A pre-departure check has not been recorded", "")
    assert rest.strip().startswith("Kit short"), kit_line
    for other in ("Charge forecast", "Needs charge", "Blocked", "recovery plan", "Maintenance"):
        assert other not in kit_line, kit_line

    # ---- J3 Customers > Book visit / Suggest: the overdue robot is offered as not ready
    assert ops.get("/planning/book/").status_code == 200
    request = Visit.objects.filter(status="requested", site__code="H-120").get()
    plan = _text(ops.get(f"/planning/visits/{request.code}/?suggest=1").content.decode())
    assert f"{overdue} · not ready" in plan

    # ---- J4: the prepared visit misses only the day's checks
    assert {b["code"] for b in evaluate_readiness(Visit.objects.get(code=j4)).blockers} == {
        "EVIDENCE_MISSING"
    }
    assert ops.get(f"/readiness/visits/{j4}/").status_code == 200

    # ---- 8.1: completing demo work orders of every kind changes no safety state
    before = _safety_snapshot()
    done = []
    for kind in ("maintenance_due", "inspection_due", "kit_low", "charging", "hold_work", "other"):
        task = tasks.filter(type=kind, status="open").order_by("id").first()
        assert task is not None, kind
        hs.assign_task(User.objects.get(username="dana.ortiz"), task_id=task.pk, owner_code=demo.HUB_TECH,
                       version=task.version)  # fmt: skip
        task.refresh_from_db()
        hs.start_task(User.objects.get(username="lily.tran"), task_id=task.pk, version=task.version)
        task.refresh_from_db()
        hs.complete_task(User.objects.get(username="lily.tran"), task_id=task.pk, version=task.version,
                         note="Done in the demo walk")  # fmt: skip
        done.append(task.pk)
    derive.refresh()
    assert WorkOrder.objects.filter(pk__in=done, status="done").count() == len(done)
    assert _safety_snapshot() == before

    # ---- production mode: no practice runs, and the demo maintenance rules are not accepted
    states = _states()
    set_setting("demo_mode", "false")
    try:
        with pytest.raises(demo.DemoRefused, match="only in a demo workspace"):
            demo._Builder(PASSWORD, today, lambda _m: None).practice_runs()
        prod = {(r[1], r[2], r[3]) for r in _maintenance() if r[1] is not None}
        assert prod and all(state == "no_rule" and blocking for _req, state, blocking in prod)
        with pytest.raises(CommandError):
            call_command("build_demo_workspace", "--reset", stdout=io.StringIO())
        assert ExecutionRun.objects.count() == 2  # nothing generated
    finally:
        set_setting("demo_mode", "true")

    # ---- a rebuild of the same date gives the same states and never accumulates history
    os.environ[demo.FINGERPRINT_ENV] = demo.database_fingerprint()
    try:
        call_command("build_demo_workspace", "--reset", stdout=io.StringIO())
    finally:
        del os.environ[demo.FINGERPRINT_ENV]
    assert _states() == states

    # ---- J3 (walk D6): Suggest proposes a full plan for the open request, and saving it is feasible
    from planning import services as ps
    from planning.options import plan_options, suggest

    j3 = Visit.objects.get(status="requested", site__code="H-120")
    pick = suggest(j3, plan_options(j3))
    assert pick.found and None not in (pick.robot, pick.lead, pick.second, pick.vehicle), pick.why_not
    assert pick.robot is not None and pick.robot.code != overdue
    for other in Visit.objects.filter(status="requested", site__code__in=("H-133", "H-138")):
        assert suggest(other, plan_options(other)).found, other.code
    saved = ps.plan_visit(
        User.objects.get(username="dana.ortiz"), visit_code=j3.code, robot_code=pick.robot.code,
        lead_code=pick.lead.code, second_code=pick.second.code, vehicle_code=pick.vehicle.code,
    )  # fmt: skip
    assert saved["feasibility"] == "feasible", saved
    j3.refresh_from_db()
    assert j3.status == "planned" and j3.current_revision.get_feasibility_display() == "Planning feasible"
