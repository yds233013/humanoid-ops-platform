"""Account commands. The minimal real consumer of run_command for Slice 0."""

from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from accounts.models import PasswordState, RoleGrant
from accounts.roles import ALL_ROLES, HUB_SCOPED_ROLES
from platform_core.commands import (
    Conflict,
    Invalid,
    NotAuthenticated,
    Outcome,
    PermissionDenied,
    run_command,
)
from platform_core.middleware import current_request_id
from platform_core.models import AuditEvent, Hub

User = get_user_model()


def _grant_hub(role: str, hub_code: str | None) -> Hub | None:
    """v2 03A-1 (contract 11.2): the hub of a new grant. A hub-scoped role names its hub (the only hub
    when the workspace has one and none is given); a regional role never has one. Never the person's home
    hub."""
    code = (hub_code or "").strip().upper()
    if role not in HUB_SCOPED_ROLES:
        if code:
            raise Invalid(f"The {role.replace('_', ' ')} role applies to every hub; do not choose a hub")
        return None
    if code:
        hub = Hub.objects.filter(code=code).first()
        if hub is None:
            raise Invalid(f"No hub {code}")
        return hub
    hubs = list(Hub.objects.order_by("id")[:2])
    if len(hubs) == 1:
        return hubs[0]
    if not hubs:
        return None  # the database creates the first hub (platform_core_default_hub)
    raise Invalid(f"Choose the hub where this person works as {role.replace('_', ' ')}")


def _grant_function(sql: str, params: list[Any]) -> int:
    """Call a role-grant function of accounts/0006 inside the command's transaction. Its refusals become
    the command errors: who may do it -> PermissionDenied (audited as denied by run_command), a duplicate or
    an already revoked grant -> Conflict, anything else it refuses -> Invalid. Nothing is written on a
    refusal."""
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()[0])
    except DatabaseError as exc:
        cause = exc.__cause__
        state = getattr(cause, "sqlstate", None)
        text = getattr(getattr(cause, "diag", None), "message_primary", None) or str(exc)
        if state == "42501":
            raise PermissionDenied(text) from exc
        if state == "23505":
            raise Conflict(text) from exc
        if state == "23514":
            raise Invalid(text) from exc
        raise


def grant_role(
    actor: Any,
    *,
    user_id: int,
    role: str,
    valid_to: datetime | None = None,
    hub_code: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Grant a role. Hub-scoped roles (hub technician, field operator) are held AT one hub (``hub_code``);
    the other roles are regional. Audited by run_command, with the hub in the payload and the result."""
    payload = {"user_id": user_id, "role": role, "valid_to": valid_to.isoformat() if valid_to else None}
    if hub_code:
        payload["hub"] = hub_code.strip().upper()

    def handler() -> Outcome:
        if role not in ALL_ROLES:
            raise Invalid(f"Unknown role {role}")
        if user_id == actor.pk:
            raise Invalid("Separation of duties: you cannot grant a role to yourself")
        hub = _grant_hub(role, hub_code)
        if not User.objects.filter(pk=user_id).exists():
            raise Invalid(f"No user {user_id}")
        active = RoleGrant.objects.filter(user_id=user_id, role=role, revoked_at__isnull=True)
        if active.filter(hub=hub).exists() if hub is not None else active.exists():
            where = f" at hub {hub.code}" if hub is not None else ""
            raise Conflict(f"User {user_id} already holds an active {role} grant{where}")
        # v2 03A-3: the write itself, the re-check of every rule above under the locks, and the audit row are
        # accounts_grant_role (accounts/0006): the runtime role cannot write accounts_rolegrant directly.
        grant_id = _grant_function(
            "SELECT accounts_grant_role(%s, %s, %s, %s, NULL, %s, %s)",
            [
                actor.pk,
                user_id,
                role,
                hub.pk if hub is not None else None,
                valid_to,
                current_request_id.get(),
            ],
        )
        hub_id = RoleGrant.objects.filter(pk=grant_id).values_list("hub_id", flat=True).get()
        code = Hub.objects.filter(pk=hub_id).values_list("code", flat=True).first() if hub_id else None
        return Outcome(
            "role_grant", str(grant_id), {"user_id": user_id, "role": role, "hub": code}, audited=True
        )

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
        grant = RoleGrant.objects.filter(pk=grant_id).first()
        if grant is None:
            raise Invalid(f"No grant {grant_id}")
        if grant.revoked_at is not None:
            raise Conflict("Grant already revoked")
        # v2 03A-3: accounts_revoke_grant (accounts/0006) locks the grant FOR UPDATE (an in-flight write
        # holding it FOR SHARE finishes first), re-checks it, revokes it at database time and appends the
        # audit row.
        _grant_function(
            "SELECT accounts_revoke_grant(%s, %s, %s, %s)",
            [actor.pk, grant_id, reason, current_request_id.get()],
        )
        code = (
            Hub.objects.filter(pk=grant.hub_id).values_list("code", flat=True).first()
            if grant.hub_id
            else None
        )
        return Outcome(
            "role_grant", str(grant.pk), {"revoked": True, "role": grant.role, "hub": code}, audited=True
        )

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
