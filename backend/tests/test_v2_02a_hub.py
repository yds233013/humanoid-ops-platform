"""v2 Phase 02, milestone 02A: the hub work queue (docs/11_v2_phase02_contract.md sections 4, 8.1, 8.6, 8.8).

Transitions and audit rows; the 02A role matrix with negative API and POST calls per role; task ownership;
derivation from records (idempotent, concurrent, source cleared); concurrent updates of one task; work orders
never change a safety fact; ops_app privileges on hub_workorder; Hub page statement count constant in fleet
size; page wording. SYNTHETIC data only.
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
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.roles import ALL_ROLES, PERMISSIONS
from execution.models import CustodyEvent
from fleet import services as fs
from fleet.models import Hold, Inspection, Robot
from hub import derive
from hub import services as hs
from hub.models import TYPES, WorkOrder
from planning.models import Person
from platform_core import testhooks
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.models import AuditEvent
from readiness.models import ReadinessEvidence
from tests.conftest import make_user
from tests.fleet_world import record_periodic_inspection, set_setting
from tests.readiness_world import build_readiness
from tests.test_fleet_concurrency import Interleaving

pytestmark = pytest.mark.django_db
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')
# /hub/ statements (session, user, roles, person, the one-statement refresh, the task list, page chrome).
# Measured 10 on 2026-11-23; constant in fleet size.
HUB_PAGE_BUDGET = 15


# ------------------------------------------------------------------ world
def _world(prefix: str = "") -> SimpleNamespace:
    w = build_readiness(prefix=prefix)
    w.tech = make_user(f"{prefix}tech", "hub_technician")
    w.tech2 = make_user(f"{prefix}tech2", "hub_technician")
    w.p.register_person(w.ops, username=w.tech.username, code="P-8")
    w.p.register_person(w.ops, username=w.tech2.username, code="P-9")
    w.admin = make_user(f"{prefix}admin", "admin")
    w.founder2 = make_user(f"{prefix}founder2", "founder")
    return w


@pytest.fixture
def w() -> SimpleNamespace:
    return _world()


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _api(user: Any) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _task(w: SimpleNamespace, owner: str | None = None, type_: str = "other", by: Any = None) -> int:
    return int(
        hs.create_task(by or w.ops, type=type_, title=f"Synthetic {type_}", owner_code=owner)["entity_id"]
    )


def _wo(pk: int) -> WorkOrder:
    return WorkOrder.objects.get(pk=pk)


# ------------------------------------------------------------------ transitions and audit
def test_create_assign_start_complete_with_audit_rows(w):
    tid = _task(w)
    assert _wo(tid).status == "open" and _wo(tid).owner is None
    hs.assign_task(w.ops, task_id=tid, owner_code="P-8")
    hs.start_task(w.tech, task_id=tid)
    hs.complete_task(w.tech, task_id=tid, note="Bay tidied")
    t = _wo(tid)
    assert (t.status, t.owner.code, t.close_reason) == ("done", "P-8", "Bay tidied")
    assert t.started_at and t.finished_at and t.version == 4
    cmds = list(
        AuditEvent.objects.filter(entity_type="work_order", entity_id=str(tid))
        .order_by("id")
        .values_list("command", "outcome")
    )
    assert cmds == [
        ("hub.create_task", "committed"),
        ("hub.assign_task", "committed"),
        ("hub.start_task", "committed"),
        ("hub.complete_task", "committed"),
    ]


def test_the_transition_graph_is_fixed(w):
    tid = _task(w, owner="P-8")
    with pytest.raises(Conflict, match="start it before"):
        hs.complete_task(w.ops, task_id=tid)  # assigned -> done is not an edge
    hs.start_task(w.ops, task_id=tid)
    with pytest.raises(Conflict, match="only an assigned task"):
        hs.start_task(w.ops, task_id=tid)
    with pytest.raises(Conflict, match="already in progress"):
        hs.assign_task(w.ops, task_id=tid, owner_code="P-9")
    hs.complete_task(w.ops, task_id=tid)
    for fn in (
        lambda: hs.start_task(w.ops, task_id=tid),
        lambda: hs.complete_task(w.ops, task_id=tid),
        lambda: hs.assign_task(w.ops, task_id=tid, owner_code="P-8"),
        lambda: hs.cancel_task(w.ops, task_id=tid, reason="x"),
    ):
        with pytest.raises(Conflict, match="already done"):
            fn()
    other = _task(w)
    with pytest.raises(Invalid, match="reason"):
        hs.cancel_task(w.ops, task_id=other, reason=" ")
    hs.cancel_task(w.ops, task_id=other, reason="Duplicate")
    assert _wo(other).status == "cancelled" and _wo(other).close_reason == "Duplicate"


def test_a_stale_version_is_refused_in_plain_words(w):
    tid = _task(w, owner="P-8")
    v = _wo(tid).version
    hs.start_task(w.tech, task_id=tid, version=v)
    with pytest.raises(Conflict, match="changed since you opened it"):
        hs.complete_task(w.tech, task_id=tid, version=v)


# ------------------------------------------------------------------ roles (8.6, 02A rows)
def test_the_02a_role_matrix():
    assert PERMISSIONS["hub.view"] == {
        "hub_technician", "field_operator", "ops_lead", "engineer", "engineering_lead", "safety_reviewer"
    }  # fmt: skip
    assert PERMISSIONS["hub.view_all"] == PERMISSIONS["hub.view"] - {"field_operator"}  # own tasks only
    assert PERMISSIONS["hub.work"] == {
        "hub_technician",
        "field_operator",
        "ops_lead",
        "engineer",
        "engineering_lead",
    }
    assert PERMISSIONS["hub.manage_all"] == {"ops_lead", "engineer", "engineering_lead"}
    assert PERMISSIONS["hub.claim"] == {"hub_technician"}
    # hub_technician holds ONLY hub permissions (02A, plus the 02B charging rows of 8.6), plus 02C "Record
    # maintenance" (8.6: "if qualified"; the database also requires the maintenance_technician qualification)
    assert {p for p, roles in PERMISSIONS.items() if "hub_technician" in roles} == {
        "hub.view", "hub.view_all", "hub.work", "hub.claim", "hub.nav", "hub.record_charging", "hub.reserve_charger",
        "fleet.record_maintenance",
    }  # fmt: skip
    assert "hub_technician" in ALL_ROLES


@pytest.mark.parametrize("role", ALL_ROLES)
def test_unauthorized_api_and_post_calls_are_refused_and_write_nothing(w, role):
    user = make_user(f"r_{role}", role)
    tid = _task(w, owner="P-3")  # owned by w.field2, not by this user
    before = (
        WorkOrder.objects.count(),
        list(WorkOrder.objects.values_list("id", "status", "owner_id", "version")),
    )
    api = _api(user)
    can_work = role in PERMISSIONS["hub.work"]
    can_view = role in PERMISSIONS["hub.view"]
    r = api.get("/api/hub/tasks")
    assert r.status_code == (200 if can_view else 403), role
    r = api.post("/api/hub/tasks", {"type": "other", "title": "x", "owner": "P-3"}, format="json")
    if role in PERMISSIONS["hub.manage_all"]:
        assert r.status_code == 201
        WorkOrder.objects.filter(pk=r.json()["entity_id"]).update(title="manager-created")  # set aside
    else:
        assert r.status_code == 403, (role, r.status_code)  # no role here may create for someone else
    for action, body in (("start", {}), ("assign", {"owner": "P-8"}), ("cancel", {"reason": "x"})):
        if role in PERMISSIONS["hub.manage_all"]:
            continue  # managers may (covered elsewhere); stop before changing the shared task
        r = api.post(f"/api/hub/tasks/{tid}/{action}", {**body, "version": _wo(tid).version}, format="json")
        assert r.status_code == 403, (role, action, r.status_code)
    if role not in PERMISSIONS["hub.manage_all"]:
        after = list(WorkOrder.objects.values_list("id", "status", "owner_id", "version"))
        assert (WorkOrder.objects.count(), after) == before
        page = _client(user)
        r = page.get(f"/hub/tasks/{tid}/")
        if role not in PERMISSIONS["hub.view_all"]:
            assert r.status_code == 403
        if not can_work:
            r = page.post(f"/hub/tasks/{tid}/start/", {"version": "1"})
            assert r.status_code == 403
    html = _client(user).get("/hub/")
    if can_view:
        assert html.status_code == 200
    elif role == "founder":
        assert html.status_code == 302 and html["Location"] == "/ops/closeout/"
    else:
        assert html.status_code == 403


def test_ownership_is_checked_per_task(w):
    theirs = _task(w, owner="P-3")  # w.field2
    mine = _task(w, owner="P-2")  # w.field
    # a field operator works their own task only
    hs.start_task(w.field, task_id=mine)
    hs.complete_task(w.field, task_id=mine)
    for fn in (
        lambda: hs.start_task(w.field, task_id=theirs),
        lambda: hs.assign_task(w.field, task_id=theirs, owner_code="P-2"),
        lambda: hs.cancel_task(w.field, task_id=theirs, reason="x"),
        lambda: hs.create_task(w.field, type="other", title="for someone", owner_code="P-3"),
    ):
        with pytest.raises(PermissionDenied):
            fn()
    own = int(hs.create_task(w.field, type="other", title="mine")["entity_id"])
    assert _wo(own).owner.code == "P-2" and _wo(own).status == "assigned"
    hs.cancel_task(w.field, task_id=own, reason="Not needed")  # their own creation
    # a hub technician takes an unowned task for themselves, never for others, never someone else's
    free = _task(w)
    with pytest.raises(PermissionDenied):
        hs.assign_task(w.tech, task_id=free, owner_code="P-9")
    hs.assign_task(w.tech, task_id=free, owner_code="P-8")
    with pytest.raises(PermissionDenied):
        hs.assign_task(w.tech2, task_id=free, owner_code="P-9")
    with pytest.raises(PermissionDenied):
        hs.start_task(w.tech2, task_id=free)
    hs.start_task(w.tech, task_id=free)
    hs.complete_task(w.tech, task_id=free)
    with pytest.raises(PermissionDenied):
        hs.assign_task(w.tech, task_id=theirs, owner_code="P-8")
    # managers may do anything
    hs.assign_task(w.eng, task_id=theirs, owner_code="P-9")
    hs.cancel_task(w.lead, task_id=theirs, reason="Reorganised")
    denied = AuditEvent.objects.filter(command__startswith="hub.", outcome="denied").count()
    assert denied == 8


def test_field_operators_see_only_their_own_tasks(w):
    _task(w, owner="P-3")
    mine = _task(w, owner="P-2", type_="kit_low")
    html = _client(w.field).get("/hub/").content.decode()
    assert f'data-task="{mine}"' in html and html.count('data-task="') == 1
    assert "You see the tasks assigned to you" in html
    assert [t["id"] for t in _api(w.field).get("/api/hub/tasks").json()["tasks"]] == [mine]
    assert len(_api(w.tech).get("/api/hub/tasks").json()["tasks"]) >= 2


# ------------------------------------------------------------------ derivation
def _derived(kind: str) -> list[WorkOrder]:
    return list(WorkOrder.objects.filter(source_kind=kind).order_by("id"))


def test_tasks_are_derived_from_holds_inspections_and_failed_equipment_checks(w):
    set_setting("ops_periodic_due_lookahead_min", str(60 * 24 * 365))  # every periodic inspection is due soon
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="maintenance", reason="noisy wrist")
    w.rx.record_evidence(
        w.field, visit_code=w.visit, kind="equipment_check", result="fail",
        details={"items": {"restraint_kit": "missing"}},
    )  # fmt: skip
    out = derive.refresh()
    assert out["created"] == 5  # 3 robots due soon + 1 hold + 1 failed equipment check
    hold = Hold.objects.get(robot__code="R-02", released_at__isnull=True)
    (h,) = _derived("hold")
    assert (h.type, h.source_ref, h.robot.code, h.link_url) == (
        "hold_work",
        hold.code,
        "R-02",
        "/fleet/R-02/",
    )
    (e,) = _derived("equipment_check")
    ev = ReadinessEvidence.objects.filter(kind="equipment_check", result="fail").latest("id")
    assert (e.type, e.source_ref, e.visit.code) == ("equipment_failure", ev.code, w.visit)
    assert e.link_url == f"/readiness/visits/{w.visit}/"
    periodic = _derived("periodic_inspection")
    assert {t.robot.code for t in periodic} == {"R-01", "R-02", "R-03"}
    assert all(t.type == "inspection_due" and t.link_url.endswith("/inspection/") for t in periodic)
    assert AuditEvent.objects.filter(command="hub.derive_task").count() == 5


def test_refreshing_five_times_keeps_one_active_task_per_source(w):
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="maintenance", reason="noisy wrist")
    for _ in range(5):
        derive.refresh()
    assert WorkOrder.objects.filter(source_kind="hold").count() == 1
    assert AuditEvent.objects.filter(command="hub.derive_task").count() == 1
    # done is final for that fact: a closed task is not recreated while the hold stays
    (t,) = _derived("hold")
    hs.assign_task(w.ops, task_id=t.pk, owner_code="P-8")
    hs.start_task(w.tech, task_id=t.pk)
    hs.complete_task(w.tech, task_id=t.pk)
    derive.refresh()
    assert WorkOrder.objects.filter(source_kind="hold").count() == 1


def test_a_cleared_source_is_marked_never_completed(w):
    set_setting("ops_periodic_due_lookahead_min", str(60 * 24 * 365))
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="maintenance", reason="noisy wrist")
    derive.refresh()
    (t,) = _derived("hold")
    hs.assign_task(w.ops, task_id=t.pk, owner_code="P-8")
    hold = Hold.objects.get(robot__code="R-02", released_at__isnull=True)
    # the J7 release, through its own approvals (never through the task)
    fs.approve_release(w.lead, hold_code=hold.code, slot="engineering", role_used="engineering_lead")
    fs.release_hold(w.lead, hold_code=hold.code)
    derive.refresh()
    t.refresh_from_db()
    assert t.status == "assigned" and t.source_cleared_at is not None  # NOT done: nobody recorded the work
    assert AuditEvent.objects.filter(command="hub.source_cleared", entity_id=str(t.pk)).count() == 1
    html = _client(w.ops).get("/hub/?view=resolved").content.decode()
    assert f'data-task="{t.pk}"' in html and "Resolved by the underlying record" in html
    detail = _client(w.tech).get(f"/hub/tasks/{t.pk}/").content.decode()
    assert "Close task" in detail and 'value="Resolved by the underlying record"' in detail
    hs.cancel_task(w.tech, task_id=t.pk, reason="Resolved by the underlying record")  # the owner may close it
    # a new periodic inspection clears the old cycle's task and starts a new one
    old = {x.robot.code: x for x in _derived("periodic_inspection")}["R-01"]
    record_periodic_inspection(w, "R-01")
    derive.refresh()
    old.refresh_from_db()
    assert old.source_cleared_at is not None and old.status == "open"
    assert WorkOrder.objects.filter(source_kind="periodic_inspection", robot__code="R-01").count() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_commits", [True, False])
def test_concurrent_refreshes_never_duplicate(first_commits):
    _world("cd_")
    fs.place_hold(_ops("cd_"), scope="robot", target="R-02", source_type="maintenance", reason="noisy wrist")
    params = {"at": timezone.now(), "rid": "", "charge": "[]"}
    a, b = _app_conn(), _app_conn()
    try:
        a.autocommit = b.autocommit = False
        with a.cursor() as ca:
            ca.execute(derive.REFRESH_SQL, params)
            n = ca.fetchone()[0]
            assert n >= 1
        result: dict[str, Any] = {}

        def second() -> None:
            with b.cursor() as cb:
                cb.execute(derive.REFRESH_SQL, params)
                result["row"] = cb.fetchone()
            b.commit()

        t = threading.Thread(target=second)
        t.start()
        t.join(1.0)
        assert t.is_alive(), "the second refresh must wait for the first one's uncommitted task"
        a.commit() if first_commits else a.rollback()
        t.join(20)
        assert result["row"][0] == (0 if first_commits else n)
    finally:
        a.close()
        b.close()
    assert WorkOrder.objects.filter(source_kind="hold").count() == 1
    assert AuditEvent.objects.filter(command="hub.derive_task").count() == n
    assert (
        WorkOrder.objects.values("source_kind", "source_ref").distinct().count() == WorkOrder.objects.count()
    )


def _ops(prefix: str) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(username=f"{prefix}ops")


def _app_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )


# ------------------------------------------------------------------ concurrent updates of one task
@pytest.mark.django_db(transaction=True)
def test_two_people_starting_the_same_task_one_wins():
    w = _world("cu_")
    tid = _task(w, owner="P-8")
    try:
        it = Interleaving("before_commit:hub.start_task")
        blocked, first, second = it.run(
            lambda: hs.start_task(w.tech, task_id=tid), lambda: hs.start_task(w.ops, task_id=tid)
        )
    finally:
        testhooks.clear()
    assert blocked, "the second start must wait for the first one's row lock"
    assert isinstance(first, dict) and isinstance(second, Conflict)
    assert "only an assigned task can be started" in str(second)
    t = _wo(tid)
    assert t.status == "in_progress" and t.version == 2


@pytest.mark.django_db(transaction=True)
def test_assign_and_cancel_at_once_one_wins_the_other_is_refused():
    w = _world("cu2_")
    tid = _task(w)
    v = _wo(tid).version
    try:
        it = Interleaving("before_commit:hub.cancel_task")
        _blocked, first, second = it.run(
            lambda: hs.cancel_task(w.ops, task_id=tid, reason="Not needed", version=v),
            lambda: hs.assign_task(w.tech, task_id=tid, owner_code="P-8", version=v),
        )
    finally:
        testhooks.clear()
    assert isinstance(first, dict) and isinstance(second, Conflict)
    assert "changed since you opened it" in str(second)
    assert _wo(tid).status == "cancelled"


# ------------------------------------------------------------------ 8.1: work orders are not safety evidence
def _safety_snapshot() -> dict[str, Any]:
    return {
        "holds": list(Hold.objects.order_by("id").values_list("id", "released_at")),
        "inspections": Inspection.objects.count(),
        "custody": CustodyEvent.objects.count(),
        "evidence": ReadinessEvidence.objects.count(),
        "eligibility": {
            r.code: fs.evaluate_eligibility(r).eligible_for_planning for r in Robot.objects.all()
        },
        "audit_non_hub": AuditEvent.objects.exclude(command__startswith="hub.").count(),
    }


def test_completing_or_cancelling_any_task_type_changes_no_safety_fact(w):
    set_setting("ops_periodic_due_lookahead_min", str(60 * 24 * 365))
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="maintenance", reason="noisy wrist")
    w.rx.record_evidence(
        w.field, visit_code=w.visit, kind="equipment_check", result="fail",
        details={"items": {"restraint_kit": "missing"}},
    )  # fmt: skip
    derive.refresh()
    for t, _w in TYPES:
        if not WorkOrder.objects.filter(type=t).exists():
            hs.create_task(w.ops, type=t, title=f"Manual {t}", robot_code="R-02")
    before = _safety_snapshot()
    assert before["eligibility"]["R-02"] is False
    tasks = list(WorkOrder.objects.order_by("id"))
    assert {t.type for t in tasks} == {t for t, _ in TYPES}
    for i, t in enumerate(tasks):
        if i % 2:
            hs.cancel_task(w.ops, task_id=t.pk, reason="Not needed")
        else:
            hs.assign_task(w.ops, task_id=t.pk, owner_code="P-8")
            hs.assign_task(w.ops, task_id=t.pk, owner_code="P-9")  # reassign
            hs.start_task(w.tech2, task_id=t.pk)
            hs.complete_task(w.tech2, task_id=t.pk)
    derive.refresh()
    assert _safety_snapshot() == before
    assert WorkOrder.objects.exclude(status__in=["done", "cancelled"]).count() == 0


# ------------------------------------------------------------------ 8.8 privileges
def test_ops_app_may_select_insert_update_but_never_delete_work_orders(w):
    _task(w)
    app = settings.DB_APP_ROLE
    with connection.cursor() as cur:
        cur.execute(
            "SELECT p, has_table_privilege(%s, 'hub_workorder', p) FROM unnest(ARRAY['SELECT', 'INSERT', "
            "'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) p",
            [app],
        )
        privs = dict(cur.fetchall())
    assert privs == {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": False, "TRUNCATE": False,
                     "REFERENCES": False, "TRIGGER": False}  # fmt: skip


@pytest.mark.django_db(transaction=True)
def test_ops_app_cannot_delete_a_work_order_with_direct_sql():
    w = _world("pv_")
    tid = _task(w)
    conn = _app_conn()
    try:
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM hub_workorder WHERE id = %s", [tid])
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("TRUNCATE hub_workorder")
    finally:
        conn.close()
    assert WorkOrder.objects.filter(pk=tid).exists()


# ------------------------------------------------------------------ the page
def test_hub_page_statement_count_is_constant_in_fleet_size(w):
    set_setting("ops_periodic_due_lookahead_min", str(60 * 24 * 365))
    c = _client(w.ops)
    c.get("/hub/")  # first load creates the derived tasks
    with CaptureQueriesContext(connection) as small:
        c.get("/hub/")
    for code in ("R-01", "R-03"):
        fs.place_hold(w.ops, scope="robot", target=code, source_type="maintenance", reason="synthetic")
    for n in range(10):
        _task(w, owner="P-8" if n % 2 else None)
    c.get("/hub/")
    with CaptureQueriesContext(connection) as big:
        c.get("/hub/")
    assert len(small) == len(big) <= HUB_PAGE_BUDGET, (len(small), len(big))
    print(f"/hub/ statements: {len(small)} (budget {HUB_PAGE_BUDGET})")


def test_hub_page_shows_queues_next_steps_and_workflow_links(w):
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="maintenance", reason="noisy wrist")
    tid = _task(w, owner="P-8", type_="kit_low")
    WorkOrder.objects.filter(pk=tid).update(due_at=timezone.now() - timedelta(hours=1))
    html = _client(w.ops).get("/hub/").content.decode()
    assert '<a href="/hub/" aria-current="page">Hub</a>' in html
    for words in ("All open tasks", "Assigned to me", "Overdue", "No owner yet", "By type", "By owner",
                  "Robot on hold", "Kits low", "Needs an owner"):  # fmt: skip
        assert words in html, words
    assert 'href="/fleet/R-02/">Open the hold page</a>' in html
    assert "Overdue</span>" in html  # the chip on the overdue row
    assert "<script" not in html.split("<main", 1)[-1].split("</main>")[0]
    # a hub technician has no fleet access: the link is not offered, the page says why on the task
    tech = _client(w.tech).get("/hub/").content.decode()
    assert 'href="/fleet/R-02/"' not in tech
    hold_task = WorkOrder.objects.get(source_kind="hold")
    assert "needs another role" in _client(w.tech).get(f"/hub/tasks/{hold_task.pk}/").content.decode()
    # the hub technician lands on the queue and can take a task through the page (one-time token)
    assert _client(w.tech).get("/")["Location"] == "/hub/"
    c = _client(w.tech)
    page = c.get(f"/hub/tasks/{hold_task.pk}/").content.decode()
    token = TOKEN_RE.search(page).group(1)
    data = {"_form_token": token, "version": str(hold_task.version), "back": f"/hub/tasks/{hold_task.pk}/"}
    c.post(f"/hub/tasks/{hold_task.pk}/take/", data)
    assert _wo(hold_task.pk).owner.code == "P-8"
    c.post(f"/hub/tasks/{hold_task.pk}/take/", data)  # replay: nothing runs
    assert AuditEvent.objects.filter(command="hub.assign_task", entity_id=str(hold_task.pk)).count() == 1


def test_people_without_a_person_record_get_a_plain_refusal(w):
    loner = make_user("loner", "field_operator")
    with pytest.raises(PermissionDenied, match="person record"):
        hs.create_task(loner, type="other", title="x")
    assert not Person.objects.filter(user=loner).exists()


def test_adding_a_task_through_the_page(w):
    c = _client(w.tech)
    page = c.get("/hub/tasks/new/").content.decode()
    assert 'value="hold_work"' not in page and 'value="kit_low"' in page  # derived-only types are not offered
    token = TOKEN_RE.search(page).group(1)
    r = c.post(
        "/hub/tasks/new/",
        {"type": "charging", "title": "Charge NB robots on bay 1", "robot": "r-01", "due_at": "2026-12-08T07:30",
         "owner": "P-8", "note": "", "_form_token": token},
    )  # fmt: skip
    assert r.status_code == 302 and r["Location"] == "/hub/"
    t = WorkOrder.objects.get(title="Charge NB robots on bay 1")
    assert (t.type, t.robot.code, t.owner.code, t.status, t.created_by_id) == (
        "charging",
        "R-01",
        "P-8",
        "assigned",
        w.tech.pk,
    )
    assert t.due_at.astimezone(timezone.get_default_timezone()).strftime("%H:%M") == "07:30"


def test_queue_chips_separate_the_number_from_the_label(w):
    _task(w, owner="P-8", type_="kit_low")
    html = _client(w.ops).get("/hub/").content.decode()
    assert re.search(
        r'<a class="chip [^"]+" href="/hub/" data-count="all"><b class="n">\d+</b> <span>All open tasks</span></a>',
        html,
    )
    assert re.search(r'data-type="kit_low"><b class="n">1</b> <span>Kits low</span></a>', html)
    assert re.search(r'data-owner="P-8"><b class="n">1</b> <span>P-8 · [^<]+</span></a>', html)
    assert html.count('class="row hubchips"') == 3 and 'class="tile' not in html
    # the action link sits on its own line under the next step
    assert '<div class="hub-act"><a class="btn"' in html


def test_next_step_says_you_to_the_owner_and_names_the_owner_to_others(w):
    tid = _task(w, owner="P-8", type_="kit_low")
    mine = _client(w.tech).get(f"/hub/tasks/{tid}/").content.decode()
    assert "You start it. Then: restock the kit." in mine
    other = _client(w.ops).get(f"/hub/tasks/{tid}/").content.decode()
    assert (
        f"{w.tech.username}" in other
        and "starts it. Then: restock the kit." in other
        and "You start" not in other
    )
    hs.start_task(w.tech, task_id=tid)
    other_task = _task(w, owner="P-8")
    hs.start_task(w.tech, task_id=other_task)
    queue = _client(w.tech).get("/hub/").content.decode()
    assert "You: do the work described, then mark it done." in queue
    assert "You: restock the kit, then mark it done." in queue


def test_hub_technicians_land_on_the_hub_queue_even_with_field_operator():
    from platform_core.templatetags.ui import landing_url

    assert landing_url({"hub_technician"}) == "/hub/"
    assert landing_url({"hub_technician", "field_operator"}) == "/hub/"
    assert landing_url({"hub_technician", "field_operator", "admin"}) == "/hub/"
    for other in ("ops_lead", "engineer", "engineering_lead", "safety_reviewer", "founder"):
        assert landing_url({"hub_technician", "field_operator", other}) != "/hub/", other
    lily = make_user("lily_dual", "field_operator", "hub_technician")
    assert _client(lily).get("/")["Location"] == "/hub/"


# ------------------------------------------------------------------ EM 02B hardening: the version is required
@pytest.mark.parametrize("bad", [None, "", "abc", "0", "-1", "1.5", "9999999999"])
def test_api_transitions_require_a_valid_version(w, bad):
    tid = _task(w, owner="P-8")
    before = list(WorkOrder.objects.values_list("id", "status", "owner_id", "version"))
    audit = AuditEvent.objects.count()
    for action, body in (
        ("start", {}),
        ("assign", {"owner": "P-9"}),
        ("complete", {}),
        ("cancel", {"reason": "x"}),
    ):
        data = dict(body) if bad is None else {**body, "version": bad}
        r = _api(w.ops).post(f"/api/hub/tasks/{tid}/{action}", data, format="json")
        assert r.status_code == 400, (action, bad, r.status_code)
        assert r.json()["error"] == "invalid" and "nothing was changed" in r.json()["detail"]
    assert list(WorkOrder.objects.values_list("id", "status", "owner_id", "version")) == before
    assert AuditEvent.objects.count() == audit


def test_a_stale_version_changes_nothing_through_api_or_page(w):
    tid = _task(w, owner="P-8")
    stale = _wo(tid).version
    hs.assign_task(w.ops, task_id=tid, owner_code="P-9", version=stale)  # someone else changed it
    now = _wo(tid)
    r = _api(w.ops).post(f"/api/hub/tasks/{tid}/start", {"version": stale}, format="json")
    assert r.status_code == 409 and "changed since you opened it" in r.json()["detail"]
    c = _client(w.ops)
    for version in (str(stale), ""):  # stale, then missing
        page = c.get(f"/hub/tasks/{tid}/").content.decode()
        token = TOKEN_RE.search(page).group(1)
        r = c.post(f"/hub/tasks/{tid}/start/", {"_form_token": token, "version": version}, follow=True)
        assert "Nothing was changed" in r.content.decode() or "nothing was changed" in r.content.decode()
    t = _wo(tid)
    assert (t.status, t.owner_id, t.version) == (now.status, now.owner_id, now.version)
