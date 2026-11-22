"""Account commands. The minimal real consumer of run_command for Slice 0."""

from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import PasswordState, RoleGrant
from accounts.roles import ALL_ROLES
from platform_core.commands import Conflict, Invalid, NotAuthenticated, Outcome, run_command
from platform_core.middleware import current_request_id
from platform_core.models import AuditEvent

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


def create_user(
    actor: Any, *, username: str, password: str, email: str = "", idempotency_key: str | None = None
) -> dict[str, Any]:
    """Admin invites a person: a user account with a temporary password and NO role. Roles are granted
    afterwards with ``grant_role`` (the admin cannot give anything beyond what that command allows). The
    password is never written to the audit payload. The account must change the temporary password at its
    first sign-in (PasswordState.must_change; enforced by accounts.middleware)."""
    username = username.strip()
    payload = {"username": username, "email": email.strip()}

    def handler() -> Outcome:
        if not username:
            raise Invalid("Choose a username")
        if User.objects.filter(username__iexact=username).exists():
            raise Conflict(f"The username {username} is taken")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise Invalid(" ".join(exc.messages)) from None
        user = User.objects.create_user(username=username, password=password, email=email.strip())
        _mark_temporary(user, actor)
        return Outcome("user", str(user.pk), {"username": username, "must_change_password": True})

    return run_command(
        actor,
        "accounts.create_user",
        permission="accounts.create_user",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
    )


# ------------------------------------------------------------------ temporary passwords (v2 01D)
def _mark_temporary(user: Any, actor: Any) -> None:
    PasswordState.objects.update_or_create(
        user=user,
        defaults={
            "must_change": True,
            "temporary_set_at": timezone.now(),
            "temporary_set_by": actor,
            "changed_at": None,
        },
    )


def reset_password(
    actor: Any, *, user_id: int, password: str, idempotency_key: str | None = None
) -> dict[str, Any]:
    """Admin sets a new TEMPORARY password for someone else. The person must change it at their next sign-in;
    their existing sessions end (Django ties a session to the password hash). The password is never written
    to the audit payload or result."""
    payload = {"user_id": user_id}

    def handler() -> Outcome:
        if user_id == actor.pk:
            raise Invalid("Use Change password for your own account")
        try:
            target = User.objects.select_for_update().get(pk=user_id)
        except User.DoesNotExist:
            raise Invalid(f"No user {user_id}") from None
        try:
            validate_password(password, user=target)
        except ValidationError as exc:
            raise Invalid(" ".join(exc.messages)) from None
        target.set_password(password)
        target.save(update_fields=["password"])
        _mark_temporary(target, actor)
        return Outcome(
            "user", str(target.pk), {"username": target.get_username(), "must_change_password": True}
        )

    return run_command(
        actor,
        "accounts.reset_password",
        permission="accounts.reset_password",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
    )


def _audit_self(user: Any, outcome: str, result: dict[str, Any], reason: str = "") -> None:
    AuditEvent.objects.create(
        actor=user,
        command="accounts.change_password",
        outcome=outcome,
        entity_type="user",
        entity_id=str(user.pk),
        payload={"username": user.get_username()},
        result=result,
        reason=reason,
        request_id=current_request_id.get(),
    )


def change_own_password(user: Any, *, current: str, new: str) -> dict[str, Any]:
    """The signed-in user replaces their own password (the only way ``must_change`` is cleared).

    Needs no role: a newly invited person has none yet. Refuses a wrong current password and a new password
    equal to the current (temporary) one, and applies Django's password validators. The audit row names the
    user only; no password is ever written. The caller must rotate the session (update_session_auth_hash)."""
    if user is None or not user.is_authenticated:
        raise NotAuthenticated("Sign in again to change your password")
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)  # one change at a time per account
        if not locked.check_password(current):
            refusal: Exception = Invalid("Your current password is not correct")
        elif new == current:
            refusal = Invalid("Choose a new password: it cannot be the temporary or current password")
        else:
            try:
                validate_password(new, user=locked)
            except ValidationError as exc:
                refusal = Invalid(" ".join(exc.messages))
            else:
                state = PasswordState.objects.select_for_update().filter(user=locked).first()
                was_forced = bool(state and state.must_change)
                locked.set_password(new)
                locked.save(update_fields=["password"])
                PasswordState.objects.update_or_create(
                    user=locked, defaults={"must_change": False, "changed_at": timezone.now()}
                )
                result = {"user_id": locked.pk, "was_temporary": was_forced}
                _audit_self(locked, AuditEvent.OUTCOME_COMMITTED, result)
                user.password = locked.password  # the caller's object, for update_session_auth_hash
                return result
    # refused: nothing changed; record the attempt (reason only, never the password)
    _audit_self(user, AuditEvent.OUTCOME_DENIED, {}, reason=str(refusal))
    raise refusal
