"""v2 02E: the Northbay demo carries journey J8 ("Hub day") and the J2 / J3 / J4 facts of Phase 02.

One expensive test builds the demo once (the 01D test covers the rebuild and determinism) and checks that each J8
fact exists from records made through the real commands, that Today, the planner and the J4 visit show the
battery, maintenance and kit facts, and that completing demo work orders changes no safety state (contract 11,
section 8.1). Fictional data only.
"""

import io
import re
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.utils import timezone

from execution.models import CustodyEvent, ExecutionRun, TripClosure
from fleet import maintenance as fm
from fleet import services as fs
from fleet.models import DeploymentSetting, Hold, Inspection, MaintenanceRecord, Robot
from hub import charging as hc
from hub import derive
from hub import services as hs
from hub import stock as st
from hub.models import WorkOrder
from operations import demo_workspace as demo
from operations.management.commands.log_predictor_signals import log_predictor_signals
from planning.models import Visit
from readiness.models import (
    AuthorizationRevocation,
    DepartureAuthorization,
    DepartureEvent,
    ReadinessEvidence,
)

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

    # ---- the practice runs: departed and closed through the real commands, stamped now
    runs = list(ExecutionRun.objects.select_related("robot", "visit").order_by("code"))
    assert {r.robot.code for r in runs} == {due_soon, overdue}
    assert TripClosure.objects.count() == 2 and DepartureEvent.objects.count() == 2
    assert all(r.visit.site.code == demo.PRACTICE_HOME for r in runs)
    assert all(timezone.now() - d < timedelta(hours=1) for d in DepartureEvent.objects.values_list(
        "departed_at", flat=True))  # fmt: skip

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

    # ---- 3. a periodic inspection due (the beta build: every day, demo value) and one never done
    inspection_titles = set(tasks.filter(type="inspection_due").values_list("title", flat=True))
    assert "Periodic inspection of NB-046 is due soon" in inspection_titles
    assert (
        f"Robot {demo.NEVER_INSPECTED} has no periodic inspection on its configuration" in inspection_titles
    )

    # ---- 4. a kit shortage on an upcoming visit and a restock task
    short = [n for n in st.visit_needs() if n.short]
    assert short and short[0].item_code == "RK"
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
    assert re.search(r"Uptime share .*? \d+(\.\d+)?%", health)
    assert "Nothing logged yet" not in health

    # ---- J2 Today: battery, maintenance and kit facts
    today_page = _text(ops.get("/ops/").content.decode())
    assert "Needs charge before departure" in today_page
    assert "SENSOR-CLEAN overdue" in today_page  # the overdue robot's visit tomorrow is blocked
    assert "Kit short" in today_page

    # ---- J3 Customers > Book visit / Suggest: the overdue robot is offered as not ready
    assert ops.get("/planning/book/").status_code == 200
    request = Visit.objects.filter(status="requested", site__code="H-120").get()
    plan = _text(ops.get(f"/planning/visits/{request.code}/?suggest=1").content.decode())
    assert f"{overdue} · not ready" in plan

    # ---- J4: the prepared visit misses only the day's checks
    from readiness.evaluator import evaluate_readiness

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
