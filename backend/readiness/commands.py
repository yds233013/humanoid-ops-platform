"""Departure authorization, departure and revocation (Engineer C). See docs/03_readiness_contract.md s4.

Authorization is NOT departure. authorize_departure records a time-limited decision bound to exact guard
versions; record_departure records the operational fact, once, after re-checking everything under locks;
neither ever changes a visit's or a robot's status. SYNTHETIC data only: in demo mode nothing here is
real-world clearance, and production refuses unless a genuinely approved procedure exists.

Every command: run_command (auth, idempotency, in-transaction permission) -> read -> checkpoint("read:...")
-> lock_guards in the global order -> re-read -> RetryTransaction if what we locked is no longer what the
decision depends on -> checks -> insert. PostgreSQL re-checks the structural rules (migration readiness/0002)
and the single-use rule (unique authorization on DepartureEvent).

Locks (U = FOR UPDATE, S = FOR SHARE), after run_command's role-grant S:
  authorize_departure   site S, pattern S, robot S, persons S (lead, second, recovery-plan people), vehicle S,
                        visit U, policy S
  record_departure      same as authorize (the authorization's guards and the current ones), then
                        authorization U
  revoke_authorization  visit U, authorization U
"""

from typing import Any

from django.db import connection

from fleet import services as fleet_services
from planning.models import Person, Visit
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, RetryTransaction, run_command
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint
from readiness.authorization import db_now, describe_differences, snapshot_differences
from readiness.evaluator import ReadinessResult, evaluate_readiness, take_guard_snapshot
from readiness.models import (
    AuthorizationRevocation,
    DepartureAuthorization,
    DepartureEvent,
    ReadinessPolicy,
)

DEMO_NOTE = (
    "SYNTHETIC DEMO - not real-world clearance. This is an authorization, not a departure; "
    "record the departure separately."
)
PRODUCTION_NOTE = "This is an authorization, not a departure; record the departure separately."


# ------------------------------------------------------------------ helpers
def _visit(code: str) -> Visit:
    try:
        return Visit.objects.get(code=code)
    except Visit.DoesNotExist:
        raise Invalid(f"No visit {code}") from None


def _authorization(code: str) -> DepartureAuthorization:
    try:
        return DepartureAuthorization.objects.get(code=code)
    except DepartureAuthorization.DoesNotExist:
        raise Invalid(f"No departure authorization {code}") from None


def _has_role_now(user: Any, role: str) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT has_active_role(%s, %s)", [user.pk, role])
        return bool(cur.fetchone()[0])


def _next_code() -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT nextval('readiness_authorization_code_seq')")
        return f"DA-{cur.fetchone()[0]:04d}"


def _ids(snap: dict[str, Any], key: str) -> set[int]:
    value = snap.get(key)
    return {int(value)} if value is not None else set()


def _lock_set(*snaps: dict[str, Any]) -> dict[str, set[int]]:
    """The guard rows a decision on these snapshots depends on (union over the snapshots)."""
    out: dict[str, set[int]] = {
        k: set() for k in ("sites", "patterns", "robots", "persons", "vehicles", "visits", "policies")
    }
    for s in snaps:
        out["sites"] |= _ids(s, "site_id")
        out["patterns"] |= _ids(s, "pattern_id")
        out["robots"] |= _ids(s, "robot_id")
        out["persons"] |= {int(k) for k in (s.get("person_versions") or {})}
        out["vehicles"] |= _ids(s, "vehicle_id")
        out["visits"] |= _ids(s, "visit_id")
        out["policies"] |= _ids(s, "policy_id")
    return out


def _covered(locked: dict[str, set[int]], snap: dict[str, Any]) -> bool:
    need = _lock_set(snap)
    return all(need[k] <= locked[k] for k in need)


def _lock(locked: dict[str, set[int]], label: str, authorization_id: int | None = None) -> None:
    lock_guards(
        share_sites=locked["sites"],
        share_patterns=locked["patterns"],
        share_robots=locked["robots"],
        share_persons=locked["persons"],
        share_vehicles=locked["vehicles"],
        update_visits=locked["visits"],
        share_policies=locked["policies"],
        update_authorizations=[authorization_id] if authorization_id is not None else [],
        label=label,
    )


def _blocked(prefix: str, result: ReadinessResult) -> Conflict:
    exc = Conflict(
        f"{prefix}: {result.label} - " + "; ".join(f"{b['code']}: {b['message']}" for b in result.blockers)
    )
    exc.blockers = result.blockers  # type: ignore[attr-defined]
    return exc


# ------------------------------------------------------------------ authorize
def authorize_departure(actor: Any, *, visit_code: str, idempotency_key: str | None = None) -> dict[str, Any]:
    def handler() -> Outcome:
        v = _visit(visit_code)
        read = take_guard_snapshot(v)
        checkpoint("read:readiness.authorize_departure")
        if read.get("revision_id") is None:
            raise Conflict(f"Visit {visit_code} has no plan to authorize")
        locked = _lock_set(read)
        _lock(locked, "readiness.authorize_departure")
        v.refresh_from_db()
        locked_snap = take_guard_snapshot(v)
        if v.current_revision_id != read["revision_id"] or not _covered(locked, locked_snap):
            raise RetryTransaction()  # re-planned / new plan people / new policy: lock the CURRENT rows

        at = db_now()
        result = evaluate_readiness(v, at=at)
        if not _covered(locked, result.guard_snapshot) or result.guard_snapshot != locked_snap:
            raise RetryTransaction()  # e.g. a newer policy was approved between our locks and the evaluation
        if not result.ready:
            raise _blocked("Departure not authorized", result)
        assert (
            result.policy_id is not None and result.expires_at is not None and result.manifest_id is not None
        )
        policy = ReadinessPolicy.objects.get(pk=result.policy_id)
        mode = "demo" if fleet_services.demo_mode() else "production"
        if mode == "production" and policy.source != "approved_procedure":
            # Fail closed even if an evaluator ever said otherwise (the database refuses it too).
            raise Conflict(
                "Production mode needs an approved procedure; a synthetic demo policy never authorizes"
            )
        rev = v.current_revision
        assert rev is not None and rev.robot_id is not None
        auth = DepartureAuthorization.objects.create(
            code=_next_code(),
            visit=v,
            revision=rev,
            robot_id=rev.robot_id,
            manifest_id=result.manifest_id,
            policy=policy,
            mode=mode,
            guard_snapshot=result.guard_snapshot,
            evidence_ids=list(result.evidence_ids),
            recovery_plan_id=result.recovery_plan_id,
            issued_by=actor,
            expires_at=result.expires_at,
        )
        auth.refresh_from_db(fields=["issued_at", "expires_at"])  # issued_at is forced to DB time by trigger
        return Outcome(
            "departure_authorization",
            auth.code,
            {
                "code": auth.code,
                "visit": v.code,
                "revision": rev.revision_no,
                "mode": mode,
                "policy_version": policy.version,
                "issued_at": auth.issued_at.isoformat(),
                "expires_at": auth.expires_at.isoformat(),
                "snapshot": auth.guard_snapshot,
                "evidence_ids": auth.evidence_ids,
                "recovery_plan_id": auth.recovery_plan_id,
                "note": DEMO_NOTE if mode == "demo" else PRODUCTION_NOTE,
            },
        )

    return run_command(
        actor,
        "readiness.authorize_departure",
        permission="readiness.authorize_departure",
        payload={"visit": visit_code},
        handler=handler,
        idempotency_key=idempotency_key,
    )


# ------------------------------------------------------------------ depart
def _may_record_departure(actor: Any, auth: DepartureAuthorization) -> bool:
    """ops_lead, or the user of the authorized revision's lead or second person."""
    if _has_role_now(actor, "ops_lead"):
        return True
    rev = auth.revision
    crew = [x for x in (rev.lead_id, rev.second_id) if x]
    return Person.objects.filter(pk__in=crew, user_id=actor.pk).exists()


def record_departure(
    actor: Any, *, authorization_code: str, idempotency_key: str | None = None
) -> dict[str, Any]:
    def handler() -> Outcome:
        auth = _authorization(authorization_code)
        if not _may_record_departure(actor, auth):
            raise PermissionDenied("Only an ops lead or the visit's lead/second person may record departure")
        v = Visit.objects.get(pk=auth.visit_id)
        stored: dict[str, Any] = auth.guard_snapshot
        read = take_guard_snapshot(v)
        checkpoint("read:readiness.record_departure")
        locked = _lock_set(stored, read)
        _lock(locked, "readiness.record_departure", authorization_id=auth.pk)
        v.refresh_from_db()
        now_snap = take_guard_snapshot(v)
        if v.current_revision_id != read.get("revision_id") or not _covered(locked, now_snap):
            raise RetryTransaction()

        if DepartureEvent.objects.filter(authorization_id=auth.pk).exists():
            raise Conflict(f"Authorization {auth.code} was already used")
        if AuthorizationRevocation.objects.filter(authorization_id=auth.pk).exists():
            raise Conflict(f"Authorization {auth.code} was revoked")
        now = db_now()
        if now >= auth.expires_at:
            raise Conflict(f"Authorization {auth.code} expired at {auth.expires_at.isoformat()}")
        diff = snapshot_differences(stored, now_snap)
        if diff:
            raise Conflict(
                f"Authorization {auth.code} is invalidated; guards changed since it was issued: "
                + "; ".join(describe_differences(diff))
            )
        result = evaluate_readiness(v, at=now)
        if not result.ready:
            raise _blocked(f"Departure refused on {auth.code}", result)
        if snapshot_differences(stored, result.guard_snapshot):
            raise RetryTransaction()  # e.g. a newer policy approved mid-check; re-judge under fresh locks
        ev = DepartureEvent.objects.create(
            authorization=auth,
            visit=v,
            robot_id=auth.robot_id,
            recorded_by=actor,
            guard_snapshot=now_snap,
        )
        ev.refresh_from_db(fields=["departed_at"])  # forced to DB time by trigger
        return Outcome(
            "departure_event",
            str(ev.pk),
            {
                "authorization": auth.code,
                "visit": v.code,
                "departed_at": ev.departed_at.isoformat(),
                "mode": auth.mode,
                "note": "SYNTHETIC DEMO - not a real-world departure" if auth.mode == "demo" else "",
            },
        )

    return run_command(
        actor,
        "readiness.record_departure",
        permission="readiness.record_departure",
        payload={"authorization": authorization_code},
        handler=handler,
        idempotency_key=idempotency_key,
    )


# ------------------------------------------------------------------ revoke
def revoke_authorization(
    actor: Any, *, authorization_code: str, reason: str, idempotency_key: str | None = None
) -> dict[str, Any]:
    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        auth = _authorization(authorization_code)
        lock_guards(
            update_visits=[auth.visit_id],
            update_authorizations=[auth.pk],
            label="readiness.revoke_authorization",
        )
        if DepartureEvent.objects.filter(authorization_id=auth.pk).exists():
            raise Conflict(f"Authorization {auth.code} was already used; a departure cannot be revoked")
        if AuthorizationRevocation.objects.filter(authorization_id=auth.pk).exists():
            raise Conflict(f"Authorization {auth.code} is already revoked")
        AuthorizationRevocation.objects.create(authorization=auth, revoked_by=actor, reason=reason)
        return Outcome("departure_authorization", auth.code, {"revoked": True, "reason": reason})

    return run_command(
        actor,
        "readiness.revoke_authorization",
        permission="readiness.revoke_authorization",
        payload={"authorization": authorization_code, "reason": reason},
        handler=handler,
        idempotency_key=idempotency_key,
    )
