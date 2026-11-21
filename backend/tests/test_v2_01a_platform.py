"""v2 Phase 01A (platform part): security headers, error pages, anonymous redirect, /healthz, workspace
singleton, first-admin bootstrap. Contract: docs/10_v2_phase01_contract.md sections 4, 5 (01A 6-7), 12."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client
from django.urls import reverse

from accounts.models import RoleGrant
from platform_core.bootstrap import BootstrapRefused, bootstrap_first_admin
from platform_core.models import AuditEvent, Workspace
from tests.conftest import make_user

User = get_user_model()
TOKEN = "t0ken-for-tests-only"
GOOD_PW = "a-long-enough-password-1"
BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("WORKSPACE_BOOTSTRAP_TOKEN", TOKEN)


# ------------------------------------------------------------------ security headers
def _settings_with_debug_off() -> dict:
    code = (
        "import json, config.settings as s;"
        "print(json.dumps({k: getattr(s, k, None) for k in ("
        "'SECURE_HSTS_SECONDS','SECURE_HSTS_INCLUDE_SUBDOMAINS','X_FRAME_OPTIONS','SECURE_CONTENT_TYPE_NOSNIFF',"
        "'SECURE_REFERRER_POLICY','CSP_ENABLED','SESSION_COOKIE_SECURE')}))"
    )
    env = {**os.environ, "DJANGO_DEBUG": "false"}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_production_settings_enable_hardening():
    s = _settings_with_debug_off()
    assert s["SECURE_HSTS_SECONDS"] == 365 * 24 * 60 * 60
    assert s["SECURE_HSTS_INCLUDE_SUBDOMAINS"] is True
    assert s["X_FRAME_OPTIONS"] == "DENY"
    assert s["SECURE_CONTENT_TYPE_NOSNIFF"] is True
    assert s["SECURE_REFERRER_POLICY"] == "same-origin"
    assert s["CSP_ENABLED"] is True and s["SESSION_COOKIE_SECURE"] is True


def test_debug_settings_leave_http_dev_working():
    code = "import config.settings as s; print(s.CSP_ENABLED, getattr(s, 'SECURE_HSTS_SECONDS', 0))"
    env = {**os.environ, "DJANGO_DEBUG": "true"}
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["False", "0"]


def test_headers_present_with_hardening_on(settings, db):
    settings.DEBUG = False
    settings.SECURE_HSTS_SECONDS = 31536000
    settings.SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    settings.SECURE_CONTENT_TYPE_NOSNIFF = True
    settings.SECURE_REFERRER_POLICY = "same-origin"
    settings.X_FRAME_OPTIONS = "DENY"
    settings.CSP_ENABLED = True
    settings.ALLOWED_HOSTS = ["testserver"]
    r = Client().get("/login/", secure=True)  # a fresh Client builds its middleware chain from these settings
    assert r.status_code == 200
    assert r["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    assert r["X-Frame-Options"] == "DENY"
    assert r["X-Content-Type-Options"] == "nosniff"
    assert r["Referrer-Policy"] == "same-origin"
    csp = r["Content-Security-Policy"]
    for part in ("default-src 'self'", "img-src 'self' data:", "frame-ancestors 'none'"):
        assert part in csp


def test_no_csp_in_debug_and_no_store_only_when_signed_in(db):
    anon = Client().get("/no-such-page/")
    assert "Content-Security-Policy" not in anon
    assert "no-store" not in anon.get("Cache-Control", "")
    user = make_user("hdr_ops", "ops_lead")
    c = Client()
    c.force_login(user)
    r = c.get("/fleet/")
    assert r.status_code == 200
    assert "no-store" in r["Cache-Control"]


# ------------------------------------------------------------------ anonymous and errors
@pytest.mark.parametrize("url", ["/ops/", "/ops/exceptions/", "/ops/metrics/", "/ops/capacity/"])
@pytest.mark.django_db(transaction=True)
def test_anonymous_html_pages_redirect_to_login_with_next(url, db):
    r = Client().get(url)
    assert r.status_code == 302
    assert r["Location"].startswith("/login/?next=")
    assert "%2F" in r["Location"]


@pytest.mark.django_db(transaction=True)
def test_anonymous_api_still_json_not_redirect(db):
    for url in ("/api/ops/overview", "/api/audit-events", "/api/fleet/robots"):
        r = Client().get(url)
        assert r.status_code in (401, 403), url
        assert "json" in r["Content-Type"], url


@pytest.mark.django_db(transaction=True)
def test_signed_in_without_permission_still_403_not_redirect(db):
    c = Client()
    c.force_login(make_user("noperm", "admin"))
    r = c.get("/ops/")
    assert r.status_code == 403


def test_404_page_is_friendly_with_way_back(db):
    r = Client().get("/no-such-page/")
    assert r.status_code == 404
    body = r.content.decode()
    assert "We could not find that page" in body and 'href="/"' in body


def test_api_404_is_json(db):
    r = Client().get("/api/no-such")
    assert r.status_code == 404 and r.json() == {"error": "not_found"}


def test_403_template_renders(db, rf):
    from platform_core.views import permission_denied

    r = permission_denied(rf.get("/x/"))
    assert r.status_code == 403 and b"do not have access" in r.content


def test_csrf_failure_page(db):
    c = Client(enforce_csrf_checks=True)
    r = c.post("/logout/")
    assert r.status_code == 403
    body = r.content.decode()
    assert "That form expired" in body and 'href="/"' in body


def test_500_page_and_plain_fallback(db, rf, monkeypatch):
    from platform_core import views

    r = views.server_error(rf.get("/x/"))
    assert r.status_code == 500 and b"Something went wrong" in r.content

    def boom(*a, **k):
        raise RuntimeError("template engine down")

    monkeypatch.setattr(views, "render", boom)
    r = views.server_error(rf.get("/x/"))
    assert r.status_code == 500 and b"Back to the start page" in r.content
    assert views.server_error(rf.get("/api/x")).status_code == 500


# ------------------------------------------------------------------ healthz
def test_healthz_ok_without_auth_and_no_secrets(db):
    r = Client().get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "migrations": "ok"}
    assert "password" not in r.content.decode().lower()


def test_healthz_503_on_pending_migration(db, monkeypatch):
    from django.db.migrations.executor import MigrationExecutor

    monkeypatch.setattr(
        MigrationExecutor, "migration_plan", lambda self, targets, clean_start=False: [("x", False)]
    )
    r = Client().get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "ok", "migrations": "pending"}


def test_healthz_503_when_database_down(db, monkeypatch):
    from django.db import DatabaseError
    from django.db.backends.utils import CursorWrapper

    real = CursorWrapper.execute

    def failing(self, sql, params=None):
        if str(sql).strip().upper() == "SELECT 1":
            raise DatabaseError("connection refused (simulated)")
        return real(self, sql, params)

    monkeypatch.setattr(CursorWrapper, "execute", failing)
    r = Client().get("/healthz")
    assert r.status_code == 503
    assert r.json()["database"] == "unreachable"
    assert "simulated" not in r.content.decode()


# ------------------------------------------------------------------ workspace singleton
def test_workspace_row_exists_after_migrate_uninitialized_in_fresh_db(db):
    Workspace.ensure()
    ws = Workspace.objects.get(pk=1)
    assert ws.initialized is False and ws.demo is False and ws.time_zone


def test_second_workspace_row_rejected_by_database(db):
    Workspace.ensure()
    with pytest.raises(IntegrityError), transaction.atomic():
        Workspace.objects.create(id=2, name="Other")  # CHECK (id = 1)
    with pytest.raises(IntegrityError), transaction.atomic():
        Workspace.objects.create(id=1, name="Dup")  # primary key
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            "INSERT INTO platform_core_workspace (id, name, region, time_zone, demo, created_at, initialized)"
            " VALUES (2, 'raw', '', 'UTC', false, now(), false)"
        )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            "INSERT INTO platform_core_workspace (id, name, region, time_zone, demo, created_at, initialized)"
            " VALUES (1, 'raw', '', 'UTC', false, now(), false)"
        )
    assert Workspace.objects.count() == 1


def test_seed_dev_marks_workspace_initialized(db, monkeypatch):
    monkeypatch.setenv("DEV_SEED_PASSWORD", "dev-only-password-123")
    call_command("seed_dev")
    ws = Workspace.objects.get(pk=1)
    assert ws.initialized and ws.demo and ws.initialized_at is not None


# ------------------------------------------------------------------ bootstrap
def test_setup_disabled_without_token_env(db, monkeypatch):
    monkeypatch.delenv("WORKSPACE_BOOTSTRAP_TOKEN", raising=False)
    assert Client().get("/setup/").status_code == 404
    with pytest.raises(BootstrapRefused) as e:
        bootstrap_first_admin(token="", username="a", password=GOOD_PW)
    assert e.value.reason == "disabled"
    assert not User.objects.exists()


def test_wrong_token_refused_nothing_created(db, token_env):
    c = Client()
    assert c.get("/setup/").status_code == 200
    r = c.post("/setup/", {"token": "nope", "username": "boss", "password": GOOD_PW})
    assert r.status_code == 403
    assert not User.objects.filter(username="boss").exists()
    assert Workspace.objects.get(pk=1).initialized is False
    r = c.post("/setup/", {"token": "", "username": "boss", "password": GOOD_PW})
    assert r.status_code == 403 and not User.objects.exists()


def test_success_creates_admin_and_marks_initialized_and_replay_refused(db, token_env):
    c = Client()
    r = c.post(
        "/setup/",
        {"token": TOKEN, "username": "boss", "password": GOOD_PW, "workspace_name": "Acme Robotics"},
    )
    assert r.status_code == 302 and r["Location"] == "/login/"
    user = User.objects.get(username="boss")
    assert user.check_password(GOOD_PW)
    grants = list(RoleGrant.objects.filter(user=user))
    assert [g.role for g in grants] == ["admin"]
    ws = Workspace.objects.get(pk=1)
    assert ws.initialized and ws.initialized_at and ws.name == "Acme Robotics"
    assert AuditEvent.objects.filter(command="platform_core.bootstrap_first_admin").count() == 1
    # replay with the right token: permanently refused
    assert c.get("/setup/").status_code == 404
    r = c.post("/setup/", {"token": TOKEN, "username": "second", "password": GOOD_PW})
    assert r.status_code == 404
    assert not User.objects.filter(username="second").exists()
    with pytest.raises(BootstrapRefused) as e:
        bootstrap_first_admin(token=TOKEN, username="third", password=GOOD_PW)
    assert e.value.reason == "initialized"
    assert RoleGrant.objects.filter(role="admin").count() == 1
    # the new admin can sign in
    assert Client().login(username="boss", password=GOOD_PW)


def test_weak_password_refused_and_not_initialized(db, token_env):
    with pytest.raises(BootstrapRefused) as e:
        bootstrap_first_admin(token=TOKEN, username="boss", password="short")
    assert e.value.reason == "invalid"
    assert not User.objects.exists() and Workspace.objects.get(pk=1).initialized is False


def test_setup_creates_no_approvals(db, token_env):
    from django.apps import apps

    bootstrap_first_admin(token=TOKEN, username="boss", password=GOOD_PW)
    checked = 0
    for model in apps.get_models():
        if "approv" in model._meta.model_name:
            checked += 1
            assert not model.objects.exists(), model
    assert checked >= 1
    # the only rows setup wrote: one user, one admin grant, one audit row, the workspace flag
    assert User.objects.count() == 1 and RoleGrant.objects.count() == 1


def test_management_command_same_rules(db, token_env, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", GOOD_PW)
    with pytest.raises(CommandError):
        call_command("bootstrap_admin", username="boss", token="wrong")
    call_command("bootstrap_admin", username="boss", token=TOKEN)
    with pytest.raises(CommandError):
        call_command("bootstrap_admin", username="boss2", token=TOKEN)
    assert User.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_bootstraps_exactly_one_wins(token_env):
    Workspace.ensure()
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def attempt(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            bootstrap_first_admin(token=TOKEN, username=name, password=GOOD_PW)
            outcome = "won"
        except BootstrapRefused as refusal:
            outcome = f"refused:{refusal.reason}"
        except Exception as exc:  # noqa: BLE001
            outcome = f"error:{exc!r}"
        finally:
            connections.close_all()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("admin_a", "admin_b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["refused:initialized", "won"], results
    assert RoleGrant.objects.filter(role="admin").count() == 1
    assert User.objects.count() == 1
    assert Workspace.objects.get(pk=1).initialized is True


def test_existing_deployment_with_users_is_initialized_by_migration(db):
    """The migration's rule, exercised directly: a database that already has users becomes initialized."""
    import importlib

    from django.apps import apps as global_apps

    mod = importlib.import_module("platform_core.migrations.0003_workspace_singleton")
    Workspace.objects.all().delete()
    make_user("already_here", "ops_lead")
    mod.seed_singleton(global_apps, None)
    ws = Workspace.objects.get(pk=1)
    assert ws.initialized and ws.demo
    assert reverse("setup") == "/setup/"
    assert Client().get("/setup/").status_code == 404
