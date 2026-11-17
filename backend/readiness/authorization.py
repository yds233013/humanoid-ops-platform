"""Advisory, unlocked status of a departure authorization (Engineer C). See docs/03_readiness_contract.md.

An authorization is a time-limited DECISION bound to exact guard versions; it is never a departure. This
module only reports whether it still looks usable. It takes no locks: record_departure re-checks everything
under locks, and the database re-checks the structural rules. SYNTHETIC data only; nothing here grants
real-world clearance.
"""

from typing import Any

from readiness.evaluator import db_now, take_guard_snapshot
from readiness.evidence import current_policy, policy_status
from readiness.models import AuthorizationRevocation, DepartureAuthorization, DepartureEvent

# Which guard each snapshot key belongs to (contract section 5).
GUARDS: dict[str, str] = {
    "robot_id": "robot/hold or configuration",
    "robot_version": "robot/hold or configuration",
    "manifest_id": "robot/hold or configuration",
    "pattern_id": "robot/hold or configuration",
    "pattern_version": "robot/hold or configuration",
    "site_id": "site/consent/assessment",
    "site_version": "site/consent/assessment",
    "person_versions": "crew qualification",
    "visit_id": "visit reschedule/evidence",
    "visit_version": "visit reschedule/evidence",
    "revision_id": "visit reschedule/evidence",
    "visit_status": "visit reschedule/evidence",
    "vehicle_id": "visit reschedule/evidence",
    "policy_id": "policy approval/withdrawal",
    "policy_lock_version": "policy approval/withdrawal",
}


def snapshot_differences(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Keys whose values differ between two guard snapshots (sorted; unknown keys included)."""
    return sorted(k for k in set(stored) | set(current) if stored.get(k) != current.get(k))


def describe_differences(keys: list[str]) -> list[str]:
    """One reason per changed guard, naming the guard and the snapshot keys that moved."""
    by_guard: dict[str, list[str]] = {}
    for k in keys:
        by_guard.setdefault(GUARDS.get(k, "unknown guard"), []).append(k)
    return [f"{guard} changed ({', '.join(ks)})" for guard, ks in sorted(by_guard.items())]


def authorization_status(auth: DepartureAuthorization) -> dict[str, Any]:
    """{"state": "usable"|"expired"|"revoked"|"invalidated"|"used", "reasons": [...]}. Advisory only.

    State precedence: used > revoked > expired > invalidated > usable. Reasons list every problem found.
    """
    reasons: list[str] = []
    used = DepartureEvent.objects.filter(authorization_id=auth.pk).first()
    if used is not None:
        reasons.append(f"Departure recorded at {used.departed_at.isoformat()}")
    revocation = AuthorizationRevocation.objects.filter(authorization_id=auth.pk).first()
    if revocation is not None:
        reasons.append(f"Revoked: {revocation.reason}")
    now = db_now()
    expired = now >= auth.expires_at
    if expired:
        reasons.append(f"Expired at {auth.expires_at.isoformat()}")

    invalid: list[str] = []
    policy = auth.policy
    if policy_status(policy) == "withdrawn":
        invalid.append(f"Readiness policy v{policy.version} was withdrawn")
    current = current_policy()
    if current is not None and current.pk != policy.pk and current.version > policy.version:
        invalid.append(f"Readiness policy v{policy.version} is superseded by v{current.version}")
    invalid += describe_differences(
        snapshot_differences(auth.guard_snapshot, take_guard_snapshot(auth.visit))
    )
    reasons += invalid

    if used is not None:
        state = "used"
    elif revocation is not None:
        state = "revoked"
    elif expired:
        state = "expired"
    elif invalid:
        state = "invalidated"
    else:
        state = "usable"
    return {"state": state, "reasons": reasons}
