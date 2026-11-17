"""Command layer: authentication, authorization, atomic audit, idempotency, failure rollback."""

import pytest

from accounts.models import RoleGrant
from platform_core import commands
from platform_core.models import AuditEvent, IdempotencyKey
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


def test_unauthenticated_request_changes_nothing(plain_user):
    from rest_framework.test import APIClient

    r = APIClient().post(
        "/api/commands/grant-role", {"user_id": plain_user.pk, "role": "ops_lead"}, format="json"
    )
    assert r.status_code in (401, 403)
    assert RoleGrant.objects.count() == 0
    assert AuditEvent.objects.count() == 0


def test_authorized_command_commits_change_and_audit_together(admin, plain_user, client_for):
    r = client_for(admin).post(
        "/api/commands/grant-role", {"user_id": plain_user.pk, "role": "ops_lead"}, format="json"
    )
    assert r.status_code == 201, r.content
    grant = RoleGrant.objects.get(user=plain_user, role="ops_lead")
    events = list(AuditEvent.objects.filter(outcome="committed"))
    assert len(events) == 1
    e = events[0]
    assert (e.command, e.entity_type, e.entity_id, e.actor_id) == (
        "accounts.grant_role",
        "role_grant",
        str(grant.pk),
        admin.pk,
    )
    assert e.request_id  # correlated with the HTTP request


def test_unauthorized_command_writes_only_a_denied_audit_row(ops_lead, plain_user, client_for):
    r = client_for(ops_lead).post(
        "/api/commands/grant-role", {"user_id": plain_user.pk, "role": "founder"}, format="json"
    )
    assert r.status_code == 403
    assert not RoleGrant.objects.filter(user=plain_user).exists()
    assert list(AuditEvent.objects.values_list("outcome", flat=True)) == ["denied"]


def test_expired_role_grant_does_not_authorize(plain_user, client_for):
    lapsed_admin = make_user("t_lapsed", "admin", expired=True)
    r = client_for(lapsed_admin).post(
        "/api/commands/grant-role", {"user_id": plain_user.pk, "role": "ops_lead"}, format="json"
    )
    assert r.status_code == 403
    assert not RoleGrant.objects.filter(user=plain_user).exists()


def test_self_grant_is_refused(admin, client_for):
    r = client_for(admin).post(
        "/api/commands/grant-role", {"user_id": admin.pk, "role": "founder"}, format="json"
    )
    assert r.status_code == 422
    assert not RoleGrant.objects.filter(user=admin, role="founder").exists()
    assert not AuditEvent.objects.filter(outcome="committed").exists()


def test_failure_after_write_rolls_back_change_and_audit(admin, plain_user, monkeypatch):
    real_create = AuditEvent.objects.create

    def failing_create(**kw):
        if kw.get("outcome") == "committed":
            raise RuntimeError("injected audit failure")
        return real_create(**kw)

    monkeypatch.setattr(commands.AuditEvent.objects, "create", failing_create)
    from accounts.services import grant_role

    with pytest.raises(RuntimeError):
        grant_role(admin, user_id=plain_user.pk, role="ops_lead", idempotency_key="k-fail")
    assert not RoleGrant.objects.filter(user=plain_user).exists()
    assert not IdempotencyKey.objects.filter(key="k-fail").exists()  # a retry can run again
    assert AuditEvent.objects.count() == 0


def test_idempotent_retry_does_not_duplicate(admin, plain_user, client_for):
    c = client_for(admin)
    body = {"user_id": plain_user.pk, "role": "ops_lead"}
    r1 = c.post("/api/commands/grant-role", body, format="json", HTTP_IDEMPOTENCY_KEY="abc-1")
    r2 = c.post("/api/commands/grant-role", body, format="json", HTTP_IDEMPOTENCY_KEY="abc-1")
    assert (r1.status_code, r2.status_code) == (201, 200)
    assert r2.json()["replayed"] is True and r2.json()["entity_id"] == r1.json()["entity_id"]
    assert RoleGrant.objects.filter(user=plain_user).count() == 1
    assert AuditEvent.objects.filter(outcome="committed").count() == 1


def test_idempotency_key_reuse_with_different_payload_conflicts(admin, plain_user, client_for):
    c = client_for(admin)
    c.post(
        "/api/commands/grant-role",
        {"user_id": plain_user.pk, "role": "ops_lead"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="abc-2",
    )
    r = c.post(
        "/api/commands/grant-role",
        {"user_id": plain_user.pk, "role": "engineer"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="abc-2",
    )
    assert r.status_code == 409
    assert not RoleGrant.objects.filter(user=plain_user, role="engineer").exists()


def test_revoke_keeps_history(admin, plain_user, client_for):
    c = client_for(admin)
    gid = int(
        c.post(
            "/api/commands/grant-role", {"user_id": plain_user.pk, "role": "ops_lead"}, format="json"
        ).json()["entity_id"]
    )
    r = c.post("/api/commands/revoke-role", {"grant_id": gid, "reason": "left team"}, format="json")
    assert r.status_code == 200
    g = RoleGrant.objects.get(pk=gid)
    assert g.revoked_at is not None and g.revoked_by_id == admin.pk  # row kept, not deleted
    assert AuditEvent.objects.filter(outcome="committed").count() == 2


def test_unknown_permission_is_denied(admin):
    from accounts.permissions import has_permission

    assert has_permission(admin, "fleet.release_hold") is False


def test_login_and_me(db):
    from rest_framework.test import APIClient

    make_user("t_login", "engineer")
    c = APIClient()
    assert (
        c.post("/api/auth/login", {"username": "t_login", "password": "wrong"}, format="json").status_code
        == 401
    )
    assert (
        c.post(
            "/api/auth/login", {"username": "t_login", "password": "test-pass-123456"}, format="json"
        ).status_code
        == 200
    )
    assert c.get("/api/auth/me").json()["roles"] == ["engineer"]


def test_audit_read_requires_permission(admin, plain_user, client_for):
    assert client_for(plain_user).get("/api/audit-events").status_code == 403
    assert client_for(admin).get("/api/audit-events").status_code == 200
