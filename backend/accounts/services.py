"""Account commands. The minimal real consumer of run_command for Slice 0."""

from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.models import RoleGrant
from accounts.roles import ALL_ROLES
from platform_core.commands import Conflict, Invalid, Outcome, run_command

User = get_user_model()


def grant_role(
    actor: Any,
    *,
    user_id: int,
    role: str,
    valid_to: datetime | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = {"user_id": user_id, "role": role, "valid_to": valid_to.isoformat() if valid_to else None}

    def handler() -> Outcome:
        if role not in ALL_ROLES:
            raise Invalid(f"Unknown role {role}")
        if user_id == actor.pk:
            raise Invalid("Separation of duties: you cannot grant a role to yourself")
        try:
            target = User.objects.select_for_update().get(pk=user_id)
        except User.DoesNotExist:
            raise Invalid(f"No user {user_id}") from None
        if RoleGrant.objects.filter(user=target, role=role, revoked_at__isnull=True).exists():
            raise Conflict(f"User {user_id} already holds an active {role} grant")
        grant = RoleGrant.objects.create(user=target, role=role, valid_to=valid_to, granted_by=actor)
        return Outcome("role_grant", str(grant.pk), {"user_id": user_id, "role": role})

    return run_command(
        actor,
        "accounts.grant_role",
        permission="accounts.grant_role",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
    )


def revoke_role(
    actor: Any, *, grant_id: int, reason: str, idempotency_key: str | None = None
) -> dict[str, Any]:
    payload = {"grant_id": grant_id, "reason": reason}

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        try:
            grant = RoleGrant.objects.select_for_update().get(pk=grant_id)
        except RoleGrant.DoesNotExist:
            raise Invalid(f"No grant {grant_id}") from None
        if grant.revoked_at is not None:
            raise Conflict("Grant already revoked")
        grant.revoked_at = timezone.now()
        grant.revoked_by = actor
        grant.save(update_fields=["revoked_at", "revoked_by"])
        return Outcome("role_grant", str(grant.pk), {"revoked": True})

    return run_command(
        actor,
        "accounts.revoke_role",
        permission="accounts.revoke_role",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
    )
