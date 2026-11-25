"""v2 Phase 03A-3: the role-grant write boundary and the read-scope gaps of 03A-1
(docs/13_v2_phase03_contract.md I-8, 10.B, 11.2; docs/11_v2_phase02_contract.md 8.8).

Proven here:

* Grants and revocations go through accounts_grant_role / accounts_revoke_grant (accounts/0006): one committed
  audit row each, written by the function, with the request id; a direct call as ops_app is audited too.
* ops_app cannot INSERT, UPDATE, DELETE or TRUNCATE accounts_rolegrant, use its sequence or lock its rows;
  it can still read it and take the permission check's locks through accounts_lock_grants.
* Forged (a real non-admin, a missing user, an inactive admin), NULL, zero and empty actors are refused by both
  functions and write nothing (no grant, no audit row).
* The existing rules, unchanged: only an active admin grants or revokes; nobody grants to themselves; a regional
  role names no hub; one active grant per (user, role, hub); a revoked grant cannot be revoked again.
* The first admin of an empty workspace (the /setup/ bootstrap) still works, once; the bootstrap function is
  refused as soon as any admin grant exists, even with the workspace flag reset; the seed function is owner
  only.
* A revocation of an admin's grant racing that admin's grant call: the call waits (pg_locks) and is then
  refused, writing nothing.
* Detail pages of another hub's robot, visit (planning and readiness), readiness inspection, run, dossier and
  work order answer 403 with a plain sentence for a hub-scoped viewer; a regional viewer and a viewer of the
  record's hub open them. Readiness, execution, fleet, dispatch and vehicle lists drop the other hub's links.
* Today: the hub scope is applied before the readiness cap, so a scoped viewer's coverage counts and notice
  describe their own hubs, and no evaluator runs for another hub's visits.
* Week and view links of the scoped pages keep ?hub=.

Identity: every guarded call runs as ops_app, through a raw ops_app connection or through Django's own
connection switched to ops_app (as_runtime_role) for the service path. Fixture setup and the owner-only steps
(resetting the workspace flag, revoking by UPDATE to set up a state) run as the owner, as the seeds do. The page
tests read through the Django client as the owner; their scope is a view check, not a database privilege.

SYNTHETIC data only.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from accounts import services as account_services
from accounts.models import RoleGrant
from hub import services as hs
from hub.models import WorkOrder
from operations import readmodels as rm
from operations import today
from platform_core import workspace as ws
from platform_core.bootstrap import BootstrapRefused, bootstrap_first_admin
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.hubscope import OTHER_HUB_TEXT
from platform_core.models import AuditEvent, Hub, Workspace
from tests.conftest import make_user
from tests.execution_world import build_execution
from tests.planning_world import DAY, TZ
from tests.test_readiness_authz_boundary import as_runtime_role

GRANT_SQL = "SELECT accounts_grant_role(%s, %s, %s, %s, NULL, NULL, 'rid-x')"
REVOKE_SQL = "SELECT accounts_revoke_grant(%s, %s, 'left', 'rid-x')"


def _app_conn(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=autocommit,
    )


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _state() -> tuple[int, int, int]:
    """(grants, active grants, audit rows): what a refused call must leave unchanged."""
    return (
        RoleGrant.objects.count(),
        RoleGrant.objects.filter(revoked_at__isnull=True).count(),
        AuditEvent.objects.count(),
    )


def _wait_for_waiter(pid: int) -> bool:
    for _ in range(100):
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted", [pid])
            if cur.fetchone()[0] > 0:
                return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ grant and revoke through the functions
@pytest.mark.django_db(transaction=True)
def test_grant_and_revoke_go_through_the_functions_with_one_audit_row_each(app_role_conn):
    admin, person = make_user("ga_admin", "admin"), make_user("ga_person")
    hub = Hub.objects.create(code="H1", name="Hub one")
    before = AuditEvent.objects.count()
    with as_runtime_role():  # the production identity: run_command, accounts_lock_grants and the function
        out = account_services.grant_role(admin, user_id=person.pk, role="hub_technician", hub_code="H1")
    grant = RoleGrant.objects.get(pk=int(out["entity_id"]))
    assert (grant.user_id, grant.role, grant.hub_id, grant.granted_by_id) == (person.pk, "hub_technician",
                                                                             hub.pk, admin.pk)  # fmt: skip
    rows = list(AuditEvent.objects.filter(pk__gt=0).order_by("id"))[before:]
    assert [(a.command, a.outcome, a.entity_id, a.actor_id) for a in rows] == [
        ("accounts.grant_role", "committed", str(grant.pk), admin.pk)
    ]
    assert rows[0].result["hub"] == "H1" and rows[0].payload["user_id"] == person.pk
    with as_runtime_role():
        account_services.revoke_role(admin, grant_id=grant.pk, reason="moved on")
    grant.refresh_from_db()
    assert grant.revoked_at is not None and grant.revoked_by_id == admin.pk
    revoke = AuditEvent.objects.filter(command="accounts.revoke_role")
    assert [(a.outcome, a.entity_id, a.payload["reason"]) for a in revoke] == [
        ("committed", str(grant.pk), "moved on")
    ]
    # a direct call as the runtime role is audited by the function itself
    with app_role_conn.cursor() as cur:
        cur.execute(GRANT_SQL, [admin.pk, person.pk, "engineer", None])
        gid = cur.fetchone()[0]
    assert RoleGrant.objects.get(pk=gid).role == "engineer"
    direct = AuditEvent.objects.get(command="accounts.grant_role", entity_id=str(gid))
    assert (direct.actor_id, direct.request_id, direct.result["hub"]) == (admin.pk, "rid-x", None)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO accounts_rolegrant (user_id, role, valid_from, granted_at, hub_id) "
        "VALUES ({person}, 'ops_lead', now(), now(), NULL)",
        "UPDATE accounts_rolegrant SET revoked_at = NULL WHERE user_id = {admin}",
        "UPDATE accounts_rolegrant SET hub_id = NULL",
        "DELETE FROM accounts_rolegrant WHERE user_id = {admin}",
        "TRUNCATE accounts_rolegrant",
        "SELECT nextval('accounts_rolegrant_id_seq')",
        "SELECT id FROM accounts_rolegrant WHERE user_id = {admin} FOR SHARE",
    ],
)
def test_the_runtime_role_cannot_write_role_grants_directly(app_role_conn, sql):
    admin, person = make_user("dw_admin", "admin"), make_user("dw_person")
    before = _state()
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(sql.format(admin=admin.pk, person=person.pk))
        cur.execute("SELECT count(*) FROM accounts_rolegrant")  # reading stays allowed
        assert cur.fetchone()[0] == before[0]
        # the permission check of run_command takes its locks through the owner function
        cur.execute("SELECT accounts_lock_grants(%s, ARRAY['admin'], NULL)", [admin.pk])
        assert cur.fetchone()[0] is True
        cur.execute("SELECT accounts_lock_grants(%s, ARRAY['admin'], NULL)", [person.pk])
        assert cur.fetchone()[0] is False
    assert _state() == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor", ["forged_non_admin", "forged_missing", "inactive_admin", "NULL", "0", "''"])
@pytest.mark.parametrize("call", ["grant", "revoke"])
def test_forged_null_and_empty_actors_are_refused_and_write_nothing(actor, call):
    admin, person = make_user("fa_admin", "admin"), make_user("fa_person", "ops_lead")
    lead = make_user("fa_lead", "ops_lead", "engineering_lead")
    gone = make_user("fa_gone", "admin")
    gone.is_active = False
    gone.save(update_fields=["is_active"])
    target = RoleGrant.objects.get(user=person, role="ops_lead")
    value = {
        "forged_non_admin": str(lead.pk),
        "forged_missing": "987654",
        "inactive_admin": str(gone.pk),
    }.get(actor, actor)
    sql = (
        f"SELECT accounts_grant_role({value}, {person.pk}, 'founder', NULL, NULL, NULL, '')"
        if call == "grant"
        else f"SELECT accounts_revoke_grant({value}, {target.pk}, 'x', '')"
    )
    before = _state()
    with _app_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.Error) as e:
            cur.execute(sql)
    if actor != "''":
        assert isinstance(e.value, psycopg.errors.InsufficientPrivilege), e.value
        assert "Only the workspace admin" in str(e.value)
    assert _state() == before
    assert admin.is_active  # the real admin exists; the forged ids are refused regardless


@pytest.mark.django_db(transaction=True)
def test_only_an_active_admin_grants_and_the_existing_rules_hold(app_role_conn):
    admin, person = make_user("ru_admin", "admin"), make_user("ru_person")
    lead = make_user("ru_lead", "ops_lead")
    lapsed = make_user("ru_lapsed", "admin", expired=True)
    Hub.objects.create(code="H1", name="Hub one")
    # a non-admin through the service: refused before the function, audited as denied
    before = _state()
    with as_runtime_role(), pytest.raises(PermissionDenied):
        account_services.grant_role(lead, user_id=person.pk, role="founder")
    assert _state()[:2] == before[:2]
    assert AuditEvent.objects.filter(command="accounts.grant_role", outcome="denied", actor=lead).count() == 1
    with app_role_conn.cursor() as cur:
        for who in (lead, lapsed):  # ... and by direct SQL
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(GRANT_SQL, [who.pk, person.pk, "founder", None])
        # separation of duties: never to yourself (the service and the function)
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot grant a role to yourself"):
            cur.execute(GRANT_SQL, [admin.pk, admin.pk, "founder", None])
        # a regional role names no hub; an unknown role is refused
        with pytest.raises(psycopg.errors.CheckViolation, match="applies to every hub"):
            cur.execute(GRANT_SQL, [admin.pk, person.pk, "ops_lead", Hub.objects.get().pk])
        with pytest.raises(psycopg.errors.CheckViolation, match="Unknown role"):
            cur.execute(GRANT_SQL, [admin.pk, person.pk, "superuser", None])
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot start in the past"):
            cur.execute(
                "SELECT accounts_grant_role(%s, %s, 'founder', NULL, now() - interval '1 day', NULL, '')",
                [admin.pk, person.pk],
            )
        cur.execute(GRANT_SQL, [admin.pk, person.pk, "founder", None])
        gid = cur.fetchone()[0]
        # one active grant per (user, role, hub)
        with pytest.raises(psycopg.errors.UniqueViolation, match="already holds an active founder grant"):
            cur.execute(GRANT_SQL, [admin.pk, person.pk, "founder", None])
        cur.execute(REVOKE_SQL, [admin.pk, gid])
        with pytest.raises(psycopg.errors.UniqueViolation, match="already revoked"):
            cur.execute(REVOKE_SQL, [admin.pk, gid])
        with pytest.raises(psycopg.errors.CheckViolation, match="A reason is required"):
            cur.execute("SELECT accounts_revoke_grant(%s, %s, '  ', '')", [admin.pk, gid])
    with as_runtime_role():
        with pytest.raises(Invalid, match="cannot grant a role to yourself"):
            account_services.grant_role(admin, user_id=admin.pk, role="founder")
        with pytest.raises(Conflict, match="already revoked"):
            account_services.revoke_role(admin, grant_id=gid, reason="again")
        # an admin can still grant and revoke anything, as before (all roles, any hub)
        account_services.grant_role(admin, user_id=person.pk, role="field_operator", hub_code="H1")
    assert RoleGrant.objects.filter(user=person, role="field_operator", revoked_at__isnull=True).count() == 1


@pytest.mark.django_db(transaction=True)
def test_the_first_admin_bootstrap_works_once(monkeypatch, app_role_conn):
    monkeypatch.setenv("WORKSPACE_BOOTSTRAP_TOKEN", "t0ken-for-tests-only")
    assert not RoleGrant.objects.exists()
    with as_runtime_role():  # /setup/ runs as the runtime role in production
        uid = bootstrap_first_admin(
            token="t0ken-for-tests-only", username="boss", password="a-long-enough-password-1"
        )
    assert list(RoleGrant.objects.values_list("user_id", "role", "granted_by_id")) == [(uid, "admin", None)]
    assert AuditEvent.objects.filter(command="accounts.bootstrap_admin", actor_id=uid).count() == 1
    with as_runtime_role(), pytest.raises(BootstrapRefused):
        bootstrap_first_admin(
            token="t0ken-for-tests-only", username="boss2", password="a-long-enough-password-1"
        )
    # the function itself refuses once an admin grant exists, even with the workspace flag reset (owner) and
    # even after that admin grant was revoked
    other = make_user("bs_other")
    Workspace.objects.filter(pk=1).update(initialized=False)
    RoleGrant.objects.filter(user_id=uid).update(revoked_at=timezone.now())
    before = _state()
    with (
        app_role_conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="already has"),
    ):
        cur.execute("SELECT accounts_bootstrap_admin(%s, '')", [other.pk])
    assert _state() == before
    # the seed path is owner only
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("SELECT accounts_seed_grant(%s, 'admin', NULL, '')", [other.pk])
    assert _state() == before


@pytest.mark.django_db(transaction=True)
def test_bootstrap_refuses_an_initialized_workspace_without_an_admin(app_role_conn):
    Workspace.ensure()
    Workspace.objects.filter(pk=1).update(initialized=True)
    user = make_user("bi_user")
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("SELECT accounts_bootstrap_admin(%s, '')", [user.pk])
    assert not RoleGrant.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_an_admin_revoked_while_granting_is_refused_after_waiting():
    """Revocation first (uncommitted, holding the admin grant FOR UPDATE); the revoked admin's grant call takes
    that grant FOR SHARE, so it waits (pg_locks) and, once the revocation commits, is refused: nothing written.
    (03A-1 covered both orders for the guarded writes; the grant function uses the same pattern.)"""
    boss, admin, person = (
        make_user("rr_boss", "admin"),
        make_user("rr_admin", "admin"),
        make_user("rr_person"),
    )
    grant = RoleGrant.objects.get(user=admin, role="admin")
    holder, waiter = _app_conn(autocommit=False), _app_conn()
    out: dict[str, Any] = {}
    before = RoleGrant.objects.filter(user=person).count()
    try:
        with holder.cursor() as cur:
            cur.execute(REVOKE_SQL, [boss.pk, grant.pk])

        def go() -> None:
            try:
                with waiter.cursor() as cur:
                    cur.execute(GRANT_SQL, [admin.pk, person.pk, "founder", None])
                out["second"] = "committed"
            except psycopg.Error as exc:
                out["second"] = exc

        t = threading.Thread(target=go)
        t.start()
        assert _wait_for_waiter(waiter.info.backend_pid), "the grant call never waited"
        assert "second" not in out
        holder.commit()
        t.join(10)
        assert not t.is_alive()
    finally:
        holder.close()
        waiter.close()
    assert isinstance(out["second"], psycopg.errors.InsufficientPrivilege), out["second"]
    assert RoleGrant.objects.filter(user=person).count() == before


# ------------------------------------------------------------------ detail pages and lists (I-8)
@pytest.fixture
def two_hub_run(transactional_db: None) -> Any:
    """The execution world (its run, visit, robot, van and task at HUB-1), a second hub HB, and a person who
    works only at HB (field operator and hub technician there)."""
    w = build_execution("dp_")
    # a confirmed visit still ahead (listed on /readiness/), the day after the run's
    w.future = w.new_confirmed_visit(robot="R-02", d=DAY + timedelta(days=1))
    w.hub_a = Hub.objects.get()
    admin = make_user("dp_admin", "admin")
    ws.create_hub(admin, code="HB", name="Hub B", area="", time_zone=TZ)
    w.hub_b = Hub.objects.get(code="HB")
    w.outsider = make_user("dp_outsider")
    for role in ("field_operator", "hub_technician"):
        account_services.grant_role(admin, user_id=w.outsider.pk, role=role, hub_code="HB")
    w.task = int(hs.create_task(w.ops, type="other", title="Shelf at A", hub_code="HUB-1")["entity_id"])
    w.a_tech = make_user("dp_tech")
    account_services.grant_role(admin, user_id=w.a_tech.pk, role="hub_technician", hub_code="HUB-1")
    return w


def _detail_urls(w: Any) -> dict[str, str]:
    robot, visit, run = w.robot.code, w.visit, w.run.code
    return {
        "robot": f"/fleet/{robot}/",
        "robot maintenance": f"/fleet/{robot}/maintenance/",
        "robot inspection": f"/readiness/robots/{robot}/inspection/",
        "visit": f"/planning/visits/{visit}/",
        "readiness visit": f"/readiness/visits/{visit}/",
        "readiness visit inspection": f"/readiness/visits/{visit}/inspection/",
        "run": f"/execution/runs/{run}/",
        "dossier": f"/execution/visits/{visit}/dossier/",
        "work order": f"/hub/tasks/{w.task}/",
    }


def test_detail_pages_of_another_hub_are_refused_with_a_plain_sentence(two_hub_run):
    w = two_hub_run
    outsider, regional, local = _client(w.outsider), _client(w.ops), _client(w.field)
    for name, url in _detail_urls(w).items():
        r = outsider.get(url)
        assert r.status_code == 403, (name, url, r.status_code)
        if name != "work order":  # the work order keeps its 03A-1 refusal page
            assert r.content.decode() == OTHER_HUB_TEXT, name
        assert regional.get(url).status_code in (200, 302), (name, url)  # the ops lead is regional
    # a field operator of HUB-1 opens the HUB-1 records (the work order page shows a field operator only
    # their own tasks, so the HUB-1 technician opens that one)
    for name, url in _detail_urls(w).items():
        c = _client(w.a_tech) if name in ("work order", "robot maintenance") else local
        assert c.get(url).status_code in (200, 302), (name, url)


def test_lists_drop_the_links_to_another_hubs_records(two_hub_run):
    w = two_hub_run
    outsider, regional = _client(w.outsider), _client(w.ops)
    run_link, visit_link = f"/execution/runs/{w.run.code}/", f"/readiness/visits/{w.visit}/"
    pages = {
        "/execution/": run_link,
        "/readiness/": f"/readiness/visits/{w.future}/",
        "/fleet/": "/fleet/R-02/",
        "/vehicles/": "VAN-1",
        f"/planning/?week={DAY.isoformat()}": f"/planning/visits/{w.visit}/",
    }
    for url, needle in pages.items():
        mine, theirs = regional.get(url), outsider.get(url)
        assert mine.status_code == theirs.status_code == 200, url
        assert needle in mine.content.decode(), url
        assert needle not in theirs.content.decode(), url
    assert visit_link not in outsider.get("/readiness/").content.decode()


# ------------------------------------------------------------------ Today: scope first, then the cap
@pytest.mark.django_db(transaction=True)
def test_today_applies_the_hub_scope_before_the_readiness_cap(monkeypatch):
    from tests.fleet_world import set_setting
    from tests.test_v2_01b_today import build

    set_setting("demo_mode", "true")
    monkeypatch.setattr(today, "local_day", lambda at: DAY)
    w = build()
    monkeypatch.setattr(rm, "READINESS_CAP", 1)
    monkeypatch.setattr(today, "READINESS_CAP", 1)
    admin = make_user("td_admin", "admin")
    ws.create_hub(admin, code="HB", name="Hub B", area="", time_zone=TZ)
    hb_only = make_user("td_hb")
    account_services.grant_role(admin, user_id=hb_only.pk, role="field_operator", hub_code="HB")
    import readiness.evaluator as ev

    calls: list[str] = []
    real = ev.evaluate_readiness

    def counting(visit: Any, **kw: Any) -> Any:
        calls.append(visit.code)
        return real(visit, **kw)

    monkeypatch.setattr(ev, "evaluate_readiness", counting)

    def page(user: Any) -> Any:
        calls.clear()
        r = _client(user).get("/ops/")
        assert r.status_code == 200
        return r.context["t"], r.content.decode()

    t_all, body_all = page(w.ops)  # regional: every HUB-1 visit, one evaluated
    assert t_all.coverage_counts["all"] >= 3 and len(calls) == 1
    assert "Readiness checked for 1 of" in body_all
    t_a, body_a = page(w.field)  # HUB-1 field operator: the same visits
    assert t_a.coverage_counts == t_all.coverage_counts and "Readiness checked for 1 of" in body_a
    t_b, body_b = page(hb_only)  # HB only: none of HUB-1's visits, no evaluator run, no global notice
    assert calls == []
    assert t_b.coverage_counts["all"] == 0 and t_b.coverage is None and t_b.visits == []
    assert "Readiness checked for" not in body_b and 'data-coverage-note="none"' in body_b
    # ?hub= on a regional viewer narrows before the cap as well
    calls.clear()
    r = _client(w.ops).get("/ops/?hub=HB")
    assert r.context["t"].coverage_counts["all"] == 0 and calls == []


# ------------------------------------------------------------------ ?hub= kept on links
@pytest.mark.django_db(transaction=True)
def test_week_and_view_links_keep_the_hub_filter():
    from tests.test_v2_03a_ownership import _two_hubs

    w = _two_hubs("hl_")
    hs.create_task(w.ops, type="other", title="Shelf at B", hub_code="HB")
    c = _client(w.ops)
    week = c.get(f"/planning/?week={DAY.isoformat()}&hub=HB").content.decode()
    prev, nxt = (DAY - timedelta(days=7)), (DAY + timedelta(days=7))
    links = set(re.findall(r'href="(\?[^"]*)"', week))
    assert any(f"week={prev:%Y}" in x and x.endswith("&hub=HB") for x in links), links
    assert any(f"week={nxt:%Y}" in x and x.endswith("&hub=HB") for x in links), links
    assert "?hub=HB" in links  # "This week"
    queue = c.get("/hub/?hub=HB").content.decode()
    chips = re.findall(r'href="(/hub/[^"]*)" data-(?:count|type|owner)=', queue)
    assert chips and all(x.endswith("hub=HB") for x in chips), chips
    plain = c.get("/hub/").content.decode()  # without a filter the links stay as they were
    assert 'href="/hub/?view=mine"' in plain and 'href="/hub/" data-count="all"' in plain
    assert WorkOrder.objects.filter(hub__code="HB").exists()
