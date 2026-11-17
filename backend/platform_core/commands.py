"""The one way state changes happen: run_command().

Order inside every command (one database transaction, retried on deadlock / lock timeout):
  1. caller must be authenticated                                  -> 401, nothing written
  2. idempotency claim (unique (actor, key)); a committed duplicate returns the stored response
  3. permission is checked INSIDE the transaction, locking the actor's qualifying role-grant rows
     FOR SHARE. revoke_role locks the grant FOR UPDATE, so a revocation and a privileged command are
     totally ordered: a command that runs after the revocation commits is denied.
  4. handler (takes its own guard locks via platform_core.locks, in the global order)
  5. audit row, stored response, commit
Any exception rolls back everything. A denial (step 3, or a PermissionDenied raised by the handler)
is then recorded as a separate 'denied' audit row.

Global lock order: platform_core/locks.py and docs/02_lock_graph.md (role grants -> settings -> sites...).
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser, User
from django.db import DatabaseError, IntegrityError, connection, transaction

from accounts.roles import PERMISSIONS
from platform_core.middleware import current_request_id
from platform_core.models import AuditEvent, IdempotencyKey
from platform_core.testhooks import checkpoint

RETRYABLE_SQLSTATES = {"40P01", "40001", "55P03"}  # deadlock, serialization failure, lock not available
MAX_ATTEMPTS = 3
REFUSED_SQLSTATES = {"23514", "23505", "42501"}  # check violation, unique violation, insufficient privilege


class RetryTransaction(Exception):  # noqa: N818 - control-flow signal, not an error
    """Raised by a handler that found its preconditions changed after taking locks (e.g. a visit was
    rescheduled between reading it and locking its resources). run_command restarts the whole transaction
    so the handler re-reads and locks the CURRENT rows."""


class CommandError(Exception):
    status = 400
    code = "command_error"


class NotAuthenticated(CommandError):
    status = 401
    code = "not_authenticated"


class PermissionDenied(CommandError):
    status = 403
    code = "permission_denied"


class Conflict(CommandError):
    status = 409
    code = "conflict"


class Invalid(CommandError):
    status = 422
    code = "invalid"


@dataclass
class Outcome:
    entity_type: str
    entity_id: str
    result: dict[str, Any] = field(default_factory=dict)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def holds_permission_locked(actor: User, permission: str) -> bool:
    """Permission check that takes FOR SHARE locks on the qualifying grants. Call inside a transaction."""
    allowed = sorted(PERMISSIONS.get(permission, set()))
    if not allowed or not actor.is_active:
        return False
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id FROM accounts_rolegrant WHERE user_id = %s AND role = ANY(%s) AND revoked_at IS NULL "
            "AND valid_from <= clock_timestamp() AND (valid_to IS NULL OR valid_to > clock_timestamp()) "
            "ORDER BY id FOR SHARE",
            [actor.pk, allowed],
        )
        return cur.fetchone() is not None


def _sqlstate(exc: BaseException) -> str | None:
    cause = exc.__cause__
    return getattr(cause, "sqlstate", None) if cause is not None else None


def run_command(
    actor: AbstractBaseUser | AnonymousUser | None,
    name: str,
    *,
    permission: str,
    payload: dict[str, Any],
    handler: Callable[[], Outcome],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if actor is None or not actor.is_authenticated:
        raise NotAuthenticated("Authentication required")
    assert isinstance(actor, User)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _attempt(actor, name, permission, payload, handler, idempotency_key)
        except RetryTransaction:
            if attempt < MAX_ATTEMPTS:
                continue
            raise Conflict("The record kept changing while this command ran; try again") from None
        except PermissionDenied as denied:
            AuditEvent.objects.create(
                actor=actor,
                command=name,
                outcome=AuditEvent.OUTCOME_DENIED,
                payload=payload,
                reason=str(denied),
                request_id=current_request_id.get(),
            )
            raise
        except DatabaseError as exc:
            state = _sqlstate(exc)
            if state in RETRYABLE_SQLSTATES and attempt < MAX_ATTEMPTS:
                continue
            if state in REFUSED_SQLSTATES:  # the database refused an invariant violation
                detail = getattr(getattr(exc.__cause__, "diag", None), "message_primary", None) or str(exc)
                raise Conflict(detail) from exc
            raise
    raise AssertionError("unreachable")


def _attempt(
    actor: User,
    name: str,
    permission: str,
    payload: dict[str, Any],
    handler: Callable[[], Outcome],
    idempotency_key: str | None,
) -> dict[str, Any]:
    payload_hash = _hash(payload)
    with transaction.atomic():
        claim = None
        if idempotency_key:
            try:
                with transaction.atomic():
                    claim = IdempotencyKey.objects.create(
                        actor=actor, key=idempotency_key, command=name, payload_hash=payload_hash
                    )
            except IntegrityError:
                prior = IdempotencyKey.objects.get(actor=actor, key=idempotency_key)
                if prior.command != name or prior.payload_hash != payload_hash:
                    raise Conflict("Idempotency key reused for a different request") from None
                # A replay is still a request: the caller must hold the permission NOW.
                if not holds_permission_locked(actor, permission):
                    raise PermissionDenied(f"Not allowed: {permission}") from None
                return {**prior.response, "replayed": True}

        if not holds_permission_locked(actor, permission):
            raise PermissionDenied(f"Not allowed: {permission}")
        checkpoint(f"authorized:{name}")

        outcome = handler()
        response = {"entity_type": outcome.entity_type, "entity_id": outcome.entity_id, **outcome.result}
        AuditEvent.objects.create(
            actor=actor,
            command=name,
            outcome=AuditEvent.OUTCOME_COMMITTED,
            entity_type=outcome.entity_type,
            entity_id=outcome.entity_id,
            payload=payload,
            result=outcome.result,
            request_id=current_request_id.get(),
        )
        if claim is not None:
            claim.response = response
            claim.save(update_fields=["response"])
        checkpoint(f"before_commit:{name}")
    return {**response, "replayed": False}
