"""Incident policy lifecycle (Slice 4, Engineer C). See docs/04_field_execution_contract.md sections 2, 4, 9.

An IncidentPolicy only classifies severity: its severity scale, the level from which an escalation is required
before closing, and the roles that may downgrade. It never decides whether a robot is held (every incident
holds it, contract D2). Same lifecycle as readiness.ReadinessPolicy: engineering + safety slots, no
self-approval, withdrawal; the ACTIVE policy is the highest version ever fully approved, and none if that one
is withdrawn (no fallback). A synthetic_demo policy is usable only while demo_mode is on.

Every command here takes the deployment-settings lock SHARED first (contract C4), then the policy row U.
PostgreSQL re-checks every rule (execution migration 0002); these checks give the explained answer.
SYNTHETIC data only.
"""

from typing import Any

from django.db import connection
from django.db.models import Count, Q

from execution.models import IncidentPolicy, IncidentPolicyApproval, IncidentPolicyWithdrawal
from fleet import services as fleet_services
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, run_command
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint

SOURCES = [s for s, _ in IncidentPolicy._meta.get_field("source").choices or []]
SLOT_ROLES = {"engineering": "engineering_lead", "safety": "safety_reviewer"}
RULE_KEYS = {"severity_scale", "escalation_required_from", "downgrade_roles"}
KNOWN_ROLES = {"ops_lead", "field_operator", "engineer", "engineering_lead", "safety_reviewer", "founder"}


# ------------------------------------------------------------------ read helpers
def incident_policy_status(policy: IncidentPolicy) -> str:
    """'withdrawn' if a withdrawal exists; else 'approved' if both slots are approved; else 'draft'."""
    if IncidentPolicyWithdrawal.objects.filter(policy=policy).exists():
        return "withdrawn"
    slots = set(policy.approvals.values_list("slot", flat=True))
    return "approved" if slots >= set(SLOT_ROLES) else "draft"


def latest_ever_approved_incident_policy() -> IncidentPolicy | None:
    """The highest-version incident policy that has ever become approved (both slots), withdrawn or not."""
    return (
        IncidentPolicy.objects.annotate(
            n_slots=Count("approvals__slot", distinct=True, filter=Q(approvals__slot__in=list(SLOT_ROLES)))
        )
        .filter(n_slots=len(SLOT_ROLES))
        .order_by("-version")
        .first()
    )


def current_incident_policy() -> IncidentPolicy | None:
    """The ACTIVE incident policy (no fallback). Same rule as SQL execution_active_incident_policy_id():
    the highest version ever fully approved; if that one is withdrawn there is NO active policy until a newer
    version becomes fully approved. It does not look at the mode: see policy_usable()."""
    policy = latest_ever_approved_incident_policy()
    if policy is None or IncidentPolicyWithdrawal.objects.filter(policy=policy).exists():
        return None
    return policy


def active_incident_policy_id_in_db() -> int | None:
    """execution_active_incident_policy_id() as PostgreSQL computes it (the rule the triggers enforce)."""
    with connection.cursor() as cur:
        cur.execute("SELECT execution_active_incident_policy_id()")
        value = cur.fetchone()[0]
        return int(value) if value is not None else None


def policy_usable(policy: IncidentPolicy) -> bool:
    """A synthetic_demo policy is usable only in demo mode. Read settings under the settings lock."""
    return policy.source == "approved_procedure" or fleet_services.demo_mode()


def level_rank(policy: IncidentPolicy, level: str) -> int | None:
    """1-based position of `level` in the policy's severity scale (low -> high); None if absent."""
    scale = policy.rules.get("severity_scale") or []
    return scale.index(level) + 1 if level in scale else None


def policy_dict(policy: IncidentPolicy) -> dict[str, Any]:
    return {
        "version": policy.version,
        "source": policy.source,
        "status": incident_policy_status(policy),
        "lock_version": policy.lock_version,
        "rules": policy.rules,
    }


def validate_rules(rules: Any) -> None:
    """Same rules as the database function execution_incident_rules_problem(); raises Invalid."""
    if not isinstance(rules, dict) or set(rules) != RULE_KEYS:
        raise Invalid(f"rules must have exactly the keys {sorted(RULE_KEYS)}")
    scale = rules["severity_scale"]
    if (
        not isinstance(scale, list)
        or not scale
        or not all(isinstance(x, str) and x.strip() for x in scale)
        or len(set(scale)) != len(scale)
    ):
        raise Invalid("severity_scale must be a non-empty list of distinct level names (low to high)")
    if (
        not isinstance(rules["escalation_required_from"], str)
        or rules["escalation_required_from"] not in scale
    ):
        raise Invalid("escalation_required_from must be a level of severity_scale")
    roles = rules["downgrade_roles"]
    if (
        not isinstance(roles, list)
        or not all(isinstance(x, str) and x.strip() for x in roles)
        or len(set(roles)) != len(roles)
    ):
        raise Invalid("downgrade_roles must be a list of distinct role names")
    unknown = [r for r in roles if r not in KNOWN_ROLES]
    if unknown:
        raise Invalid(f"unknown downgrade role {unknown[0]}")


def has_role_now(user_id: int, role: str) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT has_active_role(%s, %s)", [user_id, role])
        return bool(cur.fetchone()[0])


def _policy(version: int) -> IncidentPolicy:
    try:
        return IncidentPolicy.objects.get(version=version)
    except IncidentPolicy.DoesNotExist:
        raise Invalid(f"No incident policy v{version}") from None


def _not_usable(source: str) -> Conflict:
    return Conflict(
        f"A {source} incident policy is usable only in demo mode; production needs source approved_procedure"
    )


# ------------------------------------------------------------------ commands
def create_incident_policy(
    actor: Any, *, rules: dict[str, Any], source: str, notes: str = ""
) -> dict[str, Any]:
    """Draft a new version (engineering_lead). Locks: settings S, the newest existing policy U (so two authors
    are serialized on the next version number). A synthetic_demo policy is refused outside demo mode."""

    def handler() -> Outcome:
        if source not in SOURCES:
            raise Invalid(f"source must be one of {SOURCES}")
        validate_rules(rules)
        newest = IncidentPolicy.objects.order_by("-version").first()
        checkpoint("read:execution.create_incident_policy")
        lock_guards(
            share_settings=True,
            update_incident_policies=[newest.pk] if newest else [],
            label="execution.create_incident_policy",
        )
        if source != "approved_procedure" and not fleet_services.demo_mode():
            raise _not_usable(source)
        version = (
            IncidentPolicy.objects.order_by("-version").values_list("version", flat=True).first() or 0
        ) + 1
        p = IncidentPolicy.objects.create(
            version=version, rules=rules, source=source, notes=notes, recorded_by=actor
        )
        return Outcome("incident_policy", str(p.version), policy_dict(p))

    return run_command(
        actor,
        "execution.create_incident_policy",
        permission="execution.manage_incident_policy",
        payload={"rules": rules, "source": source, "notes": notes},
        handler=handler,
    )


def approve_incident_policy(actor: Any, *, version: int, slot: str) -> dict[str, Any]:
    """Approve one slot (engineering -> engineering_lead, safety -> safety_reviewer). The author cannot
    approve, one approver per policy, no approval of a withdrawn policy, and a synthetic_demo policy cannot be
    approved outside demo mode. Locks: settings S, policy U. The second slot makes the policy ACTIVE."""

    def handler() -> Outcome:
        if slot not in SLOT_ROLES:
            raise Invalid(f"slot must be one of {sorted(SLOT_ROLES)}")
        p = _policy(version)
        checkpoint("read:execution.approve_incident_policy")
        lock_guards(
            share_settings=True, update_incident_policies=[p.pk], label="execution.approve_incident_policy"
        )
        p.refresh_from_db()
        if IncidentPolicyWithdrawal.objects.filter(policy=p).exists():
            raise Conflict(f"Incident policy v{version} is withdrawn and cannot be approved")
        if not has_role_now(actor.pk, SLOT_ROLES[slot]):
            raise PermissionDenied(f"Slot {slot} requires role {SLOT_ROLES[slot]}")
        if p.recorded_by_id == actor.pk:
            raise Conflict("The author of an incident policy cannot approve it")
        if IncidentPolicyApproval.objects.filter(policy=p, approver=actor).exists():
            raise Conflict("You already approved this incident policy in another slot")
        if IncidentPolicyApproval.objects.filter(policy=p, slot=slot).exists():
            raise Conflict(f"Slot {slot} is already approved")
        if not policy_usable(p):
            raise _not_usable(p.source)
        IncidentPolicyApproval.objects.create(policy=p, slot=slot, approver=actor)
        p.refresh_from_db()
        active = current_incident_policy()
        return Outcome(
            "incident_policy",
            str(p.version),
            {**policy_dict(p), "slot": slot, "active": active is not None and active.pk == p.pk},
        )

    return run_command(
        actor,
        "execution.approve_incident_policy",
        permission="execution.approve_incident_policy",
        payload={"version": version, "slot": slot},
        handler=handler,
    )


def withdraw_incident_policy(actor: Any, *, version: int, reason: str) -> dict[str, Any]:
    """Withdraw a policy. Withdrawing the active one leaves NO active policy (no fallback to an older one):
    new severity decisions are refused until a newer version is fully approved.
    Locks: settings S, policy U."""

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        p = _policy(version)
        checkpoint("read:execution.withdraw_incident_policy")
        lock_guards(
            share_settings=True, update_incident_policies=[p.pk], label="execution.withdraw_incident_policy"
        )
        p.refresh_from_db()
        if IncidentPolicyWithdrawal.objects.filter(policy=p).exists():
            raise Conflict(f"Incident policy v{version} is already withdrawn")
        IncidentPolicyWithdrawal.objects.create(policy=p, withdrawn_by=actor, reason=reason)
        p.refresh_from_db()
        return Outcome("incident_policy", str(p.version), policy_dict(p))

    return run_command(
        actor,
        "execution.withdraw_incident_policy",
        permission="execution.manage_incident_policy",
        payload={"version": version, "reason": reason},
        handler=handler,
    )
