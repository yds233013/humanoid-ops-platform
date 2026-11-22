"""v2 01D (P0): forced first-login password change for accounts an admin created or reset.

An account created by the admin (Settings > Invite someone, accounts.services.create_user) or given a new
temporary password (accounts.services.reset_password) reaches only /account/password/ until its owner changes
the password. The bootstrap admin and the synthetic dev users are never forced. SYNTHETIC data only.
"""

import io
import re
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client

from accounts import services
from accounts.models import PasswordState, RoleGrant, must_change_password
from accounts.roles import ALL_ROLES
from platform_core import bootstrap
from platform_core.models import AuditEvent, Workspace
from tests.conftest import make_user

pytestmark = pytest.mark.django_db

User = get_user_model()
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')
CHANGE = "/account/password/"
TEMP = "Temp-pass-8842!"
NEW = "Own-choice-5531?"


def _html(r: Any) -> str:
    return r.content.decode()


def _token(c: Client, url: str) -> str:
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    m = TOKEN_RE.search(_html(r))
    assert m, f"no form token on {url}"
    return m.group(1)


def _post(c: Client, url: str, data: dict[str, Any], token: str | None = None) -> Any:
    return c.post(url, {**data, "_form_token": token or _token(c, url)})


def _sign_in(username: str, password: str) -> Client:
    c = Client()
    r = c.post("/login/", {"username": username, "password": password})
    assert r.status_code == 302, f"{username} could not sign in"
    return c


def _change(c: Client, current: str = TEMP, new: str = NEW, token: str | None = None) -> Any:
    return _post(
        c, CHANGE, {"current_password": current, "new_password1": new, "new_password2": new}, token=token
    )


@pytest.fixture
def admin(db: None) -> Any:
    Workspace.ensure()
    return make_user("t_admin", "admin")


def _invite(admin: Any, username: str, role: str | None = None) -> Any:
    r = _post(_admin_client(admin), "/settings/users/new/", {"username": username, "password": TEMP})
    assert r.status_code == 302, _html(r)
    user = User.objects.get(username=username)
    if role:
        services.grant_role(admin, user_id=user.pk, role=role)
    return user


def _admin_client(admin: Any) -> Client:
    c = Client()
    c.force_login(admin)
    return c


def _no_temp_anywhere() -> None:
    rows = list(AuditEvent.objects.values_list("payload", "result", "reason"))
    assert TEMP not in str(rows) and NEW not in str(rows)


# ------------------------------------------------------------------ forced redirect
@pytest.mark.parametrize("role", ALL_ROLES)
def test_an_invited_account_of_every_role_is_sent_to_the_change_page(admin, role):
    user = _invite(admin, f"new_{role}", role)
    assert must_change_password(user)
    c = _sign_in(user.username, TEMP)
    for path in (
        "/",
        "/ops/",
        "/fleet/",
        "/planning/",
        "/settings/setup/",
        "/settings/users/",
        "/execution/",
    ):
        r = c.get(path)
        assert r.status_code == 302 and r["Location"] == CHANGE, (role, path, r.status_code)
    # a POST to an operational page is turned away too, and changes nothing
    robots_before = _count_robots()
    r = c.post("/fleet/new/", {"code": "R-99", "serial": "SN-99"})
    assert r.status_code == 302 and r["Location"] == CHANGE
    assert _count_robots() == robots_before
    page = c.get(CHANGE)
    assert page.status_code == 200
    html = _html(page)
    assert "Choose your own password" in html and 'name="current_password"' in html
    assert 'class="primary-nav"' in html and "/ops/" not in html.split("</header>")[0]  # no navigation yet


def _count_robots() -> int:
    from fleet.models import Robot

    return Robot.objects.count()


def test_the_api_refuses_with_a_plain_reason(admin):
    user = _invite(admin, "api_ops", "ops_lead")
    c = _sign_in(user.username, TEMP)
    for path in ("/api/auth/me", "/api/fleet/robots", "/api/audit-events", "/api/ops/overview"):
        r = c.get(path)
        assert r.status_code == 403, (path, r.status_code)
        body = r.json()
        assert body["error"] == "password_change_required" and "temporary password" in body["reason"]
    r = c.post("/api/commands/grant-role", {"user_id": admin.pk, "role": "founder"})
    assert r.status_code == 403 and r.json()["error"] == "password_change_required"
    # the login API says so up front
    api = Client()
    r = api.post("/api/auth/login", {"username": user.username, "password": TEMP})
    assert r.status_code == 200 and r.json()["password_change_required"] is True
    # health check and sign-out stay reachable
    assert c.get("/healthz").status_code in (200, 503)
    assert c.post("/logout/").status_code == 302


# ------------------------------------------------------------------ the change
def test_change_succeeds_rotates_the_session_and_pages_work(admin):
    user = _invite(admin, "maria", "ops_lead")
    c = _sign_in(user.username, TEMP)
    old_session = c.session.session_key
    r = _change(c)
    assert r.status_code == 302 and r["Location"] == "/"
    assert c.session.session_key != old_session  # session rotated
    assert not must_change_password(user)
    user.refresh_from_db()
    assert user.check_password(NEW) and not user.check_password(TEMP)
    assert c.get("/fleet/").status_code == 200
    assert c.get("/api/auth/me").status_code == 200
    # the old session id is gone
    stale = Client()
    stale.cookies["sessionid"] = old_session
    assert stale.get("/fleet/").status_code == 302 and "/login/" in stale.get("/fleet/")["Location"]
    # audit: one committed row naming the user, no password anywhere
    row = AuditEvent.objects.get(command="accounts.change_password", outcome="committed")
    assert row.actor_id == user.pk and row.payload == {"username": "maria"}
    assert row.result == {"user_id": user.pk, "was_temporary": True}
    _no_temp_anywhere()
    # the new password signs in and is not forced
    c2 = _sign_in("maria", NEW)
    assert c2.get("/fleet/").status_code == 200


def test_replaying_the_change_post_after_success_does_nothing(admin):
    user = _invite(admin, "replay", "field_operator")
    c = _sign_in(user.username, TEMP)
    token = _token(c, CHANGE)
    body = {"current_password": TEMP, "new_password1": NEW, "new_password2": NEW, "_form_token": token}
    assert c.post(CHANGE, body).status_code == 302
    hash_after = User.objects.get(pk=user.pk).password
    again = c.post(CHANGE, body, follow=True)
    assert "already sent" in _html(again)
    assert User.objects.get(pk=user.pk).password == hash_after
    # even with a fresh token, the old temporary password is no longer the current one
    r = _change(c, current=TEMP, new="Another-pass-7788!")
    assert r.status_code == 422 and "current password is not correct" in _html(r)
    assert User.objects.get(pk=user.pk).password == hash_after
    assert AuditEvent.objects.filter(command="accounts.change_password", outcome="committed").count() == 1


def test_a_signed_out_or_expired_session_cannot_change(admin):
    user = _invite(admin, "gone", "engineer")
    c = _sign_in(user.username, TEMP)
    token = _token(c, CHANGE)
    c.post("/logout/")
    body = {"current_password": TEMP, "new_password1": NEW, "new_password2": NEW, "_form_token": token}
    r = c.post(CHANGE, body)
    assert r.status_code == 302 and r["Location"].startswith("/login/")
    assert TEMP not in r["Location"] and NEW not in r["Location"]
    assert User.objects.get(pk=user.pk).check_password(TEMP)
    assert must_change_password(user)
    # an expired session (deleted server side) is the same
    c = _sign_in(user.username, TEMP)
    token = _token(c, CHANGE)
    from django.contrib.sessions.models import Session

    Session.objects.all().delete()
    r = c.post(CHANGE, {**body, "_form_token": token})
    assert r.status_code == 302 and r["Location"].startswith("/login/")
    assert User.objects.get(pk=user.pk).check_password(TEMP)


def test_wrong_current_password_is_refused(admin):
    user = _invite(admin, "wrong", "founder")
    c = _sign_in(user.username, TEMP)
    r = _change(c, current="not-the-temp-1")
    assert r.status_code == 422 and "current password is not correct" in _html(r)
    assert User.objects.get(pk=user.pk).check_password(TEMP) and must_change_password(user)
    denied = AuditEvent.objects.get(command="accounts.change_password", outcome="denied")
    assert "not correct" in denied.reason
    _no_temp_anywhere()
    assert "not-the-temp-1" not in str(list(AuditEvent.objects.values_list("payload", "result", "reason")))


def test_new_password_equal_to_the_temporary_one_is_refused(admin):
    user = _invite(admin, "same", "safety_reviewer")
    c = _sign_in(user.username, TEMP)
    r = _change(c, current=TEMP, new=TEMP)
    assert r.status_code == 422 and "cannot be the temporary" in _html(r)
    assert must_change_password(user)
    # Django validators apply (too short), and the two new fields must match
    r = _change(c, current=TEMP, new="short")
    assert r.status_code == 422 and "too short" in _html(r)
    r = _post(c, CHANGE, {"current_password": TEMP, "new_password1": NEW, "new_password2": NEW + "x"})
    assert r.status_code == 400 and "do not match" in _html(r)
    assert must_change_password(user)
    for page_html in (_html(r),):
        assert TEMP not in page_html and NEW not in page_html  # inputs never echo a password


# ------------------------------------------------------------------ leaks
def test_the_temporary_password_is_never_in_audit_rows_urls_or_messages(admin):
    ac = _admin_client(admin)
    r = _post(ac, "/settings/users/new/", {"username": "leak", "password": TEMP})
    assert r.status_code == 302 and TEMP not in r["Location"]
    followed = ac.get(r["Location"])
    assert TEMP not in _html(followed)
    user = User.objects.get(username="leak")
    services.grant_role(admin, user_id=user.pk, role="ops_lead")
    # every redirect a forced user sees carries no password and no query string
    c = Client()
    r = c.post("/login/", {"username": "leak", "password": TEMP})
    assert TEMP not in r["Location"]
    r = c.get("/ops/?x=1")
    assert r["Location"] == CHANGE
    r = _change(c)
    assert r["Location"] == "/" and TEMP not in r["Location"] and NEW not in r["Location"]
    assert TEMP not in _html(c.get("/fleet/")) and NEW not in _html(c.get("/fleet/"))
    # admin reset: the same
    r = _post(ac, f"/settings/users/{user.pk}/password/", {"password": "Reset-pass-6610!", "confirm": "on"})
    assert r.status_code == 302 and "Reset-pass" not in r["Location"]
    assert "Reset-pass" not in _html(ac.get(r["Location"]))
    _no_temp_anywhere()
    assert "Reset-pass" not in str(list(AuditEvent.objects.values_list("payload", "result", "reason")))


# ------------------------------------------------------------------ admin reset
def test_admin_reset_sets_the_flag_again_and_ends_the_old_session(admin):
    user = _invite(admin, "again", "ops_lead")
    c = _sign_in(user.username, TEMP)
    _change(c)
    assert not must_change_password(user) and c.get("/fleet/").status_code == 200
    ac = _admin_client(admin)
    page = ac.get(f"/settings/users/{user.pk}/")
    assert f"/settings/users/{user.pk}/password/" in _html(page)
    # the tick box is required
    r = _post(ac, f"/settings/users/{user.pk}/password/", {"password": "Reset-pass-6610!"})
    assert r.status_code == 400 and "Tick the box" in _html(r)
    r = _post(ac, f"/settings/users/{user.pk}/password/", {"password": "Reset-pass-6610!", "confirm": "on"})
    assert r.status_code == 302
    assert must_change_password(user)
    assert c.get("/fleet/").status_code == 302  # the old session ended (password hash changed)
    c = _sign_in(user.username, "Reset-pass-6610!")
    assert c.get("/ops/")["Location"] == CHANGE
    row = AuditEvent.objects.get(command="accounts.reset_password", outcome="committed")
    assert row.payload == {"user_id": user.pk} and row.actor_id == admin.pk
    _change(c, current="Reset-pass-6610!", new="Third-choice-2290!")
    assert not must_change_password(user)


def test_only_the_admin_may_reset_and_not_their_own_password(admin):
    target = _invite(admin, "target", "field_operator")
    for role in ALL_ROLES:
        if role == "admin":
            continue
        other = make_user(f"o_{role}", role)
        c = Client()
        c.force_login(other)
        assert c.get(f"/settings/users/{target.pk}/password/").status_code == 403, role
        with pytest.raises(Exception, match="Not allowed"):
            services.reset_password(other, user_id=target.pk, password="Reset-pass-6610!")
    with pytest.raises(Exception, match="Change password"):
        services.reset_password(admin, user_id=admin.pk, password="Reset-pass-6610!")
    assert not PasswordState.objects.filter(user=admin).exists()


# ------------------------------------------------------------------ not forced
def test_bootstrap_admin_and_seeded_dev_users_are_not_forced(monkeypatch, db):
    Workspace.ensure()
    monkeypatch.setenv(bootstrap.TOKEN_ENV, "boot-token-123")
    uid = bootstrap.bootstrap_first_admin(
        token="boot-token-123", username="first", password="First-pass-9911"
    )
    first = User.objects.get(pk=uid)
    assert not must_change_password(first)
    c = _sign_in("first", "First-pass-9911")
    assert c.get("/settings/setup/").status_code == 200
    monkeypatch.setenv("DEV_SEED_PASSWORD", "dev-pass-12345")
    call_command("seed_dev", stdout=io.StringIO())
    for role in ALL_ROLES:
        u = User.objects.get(username=f"dev_{role}")
        assert not must_change_password(u)
        assert RoleGrant.objects.filter(user=u, role=role).exists()
    c = _sign_in("dev_ops_lead", "dev-pass-12345")
    assert c.get("/fleet/").status_code == 200


def test_a_voluntary_change_works_for_a_user_who_is_not_forced(admin):
    c = _sign_in("t_admin", "test-pass-123456")
    assert c.get("/settings/setup/").status_code == 200
    page = c.get(CHANGE)
    assert page.status_code == 200 and "Change password" in _html(page)
    r = _change(c, current="test-pass-123456", new=NEW)
    assert r.status_code == 302
    row = AuditEvent.objects.get(command="accounts.change_password", outcome="committed")
    assert row.result["was_temporary"] is False
