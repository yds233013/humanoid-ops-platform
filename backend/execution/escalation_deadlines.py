"""Escalation deadline sets (Slice 7, X-4; docs/07a_production_guardrails_contract.md section 6). Engineer B.

A deadline set records the OWNER'S decision (founder + safety, EM decision 16) of how soon each role must have
acknowledged an escalation, per severity level of ONE incident-policy version. The platform never supplies a
value: every set is authored and approved by people, and a synthetic_demo set is never usable in production.
Nothing here pages, notifies, mails or schedules anything; the read models (Engineer C) only show lateness.

Locks: settings S only (section 9.2). The approval is mode-dependent (a synthetic set cannot be approved in
production), so every command takes the settings lock SHARED first and the approval trigger asserts it. No
guard row is taken: uniqueness constraints serialize concurrent approvals, and nothing reads these rows
transactionally. PostgreSQL re-checks every rule (execution/0005); these checks give the explained answer.
SYNTHETIC data only.
"""

from dataclasses import dataclass
from typing import Any

from django.db.models import Count, Q

from execution.models import (
    DEADLINE_SLOTS,
    DEADLINE_SOURCES,
    EscalationDeadlineSet,
    EscalationDeadlineSetApproval,
    EscalationDeadlineSetWithdrawal,
    IncidentPolicy,
)
from execution.policy import KNOWN_ROLES, current_incident_policy, has_role_now
from fleet import services as fleet_services
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, run_command
from platform_core.locks import lock_guards

SOURCES = [k for k, _ in DEADLINE_SOURCES]
SLOT_ROLES = {"safety": "safety_reviewer", "founder": "founder"}
assert set(SLOT_ROLES) == {k for k, _ in DEADLINE_SLOTS}
MAX_WITHIN_MIN = 5_256_000
UNCLASSIFIED = "unclassified"


# ------------------------------------------------------------------ rules (Python mirror)
def deadline_rules_problem(policy: IncidentPolicy | None, deadlines: Any) -> str | None:
    """Mirror of execution_deadline_rules_problem(p_policy, j), same text."""
    if policy is None:
        return "unknown incident policy"
    scale = list(policy.rules.get("severity_scale") or [])
    if not isinstance(deadlines, dict):
        return "deadlines must be an object keyed by severity level"
    if set(deadlines) != {*scale, UNCLASSIFIED}:
        return 'deadlines must have exactly the policy\'s severity levels plus "unclassified" as keys'
    for level, entries in deadlines.items():
        if not isinstance(entries, list):
            return f"deadlines for {level} must be a list"
        roles: list[str] = []
        for e in entries:
            if not isinstance(e, dict) or set(e) != {"to_role", "within_min"}:
                return f"each deadline for {level} must have exactly to_role and within_min"
            role = e["to_role"]
            if not isinstance(role, str) or role not in KNOWN_ROLES:
                return f"unknown deadline role {role} for {level}"
            if role in roles:
                return f"role {role} appears twice for {level}"
            roles.append(role)
            n = e["within_min"]
            if isinstance(n, bool) or not isinstance(n, int | float):
                return f"within_min for {level} / {role} must be a whole number of minutes"
            if n != int(n) or not 1 <= n <= MAX_WITHIN_MIN:
                return f"within_min for {level} / {role} must be a whole number from 1 to {MAX_WITHIN_MIN}"
    return None


# ------------------------------------------------------------------ read helpers
def deadline_set_withdrawn(d: EscalationDeadlineSet) -> bool:
    return EscalationDeadlineSetWithdrawal.objects.filter(deadline_set=d).exists()


def deadline_set_approved(d: EscalationDeadlineSet) -> bool:
    slots = set(
        EscalationDeadlineSetApproval.objects.filter(deadline_set=d, slot__in=list(SLOT_ROLES)).values_list(
            "slot", flat=True
        )
    )
    return slots == set(SLOT_ROLES)


def deadline_set_status(d: EscalationDeadlineSet) -> str:
    if deadline_set_withdrawn(d):
        return "withdrawn"
    return "approved" if deadline_set_approved(d) else "draft"


def active_deadline_set() -> EscalationDeadlineSet | None:
    """Mirror of execution_active_deadline_set_id(): for the ACTIVE incident policy, the highest version ever
    fully approved (safety + founder); none if that version is withdrawn (no fallback)."""
    policy = current_incident_policy()
    if policy is None:
        return None
    best = (
        EscalationDeadlineSet.objects.filter(incident_policy=policy)
        .annotate(n=Count("approvals__slot", distinct=True, filter=Q(approvals__slot__in=list(SLOT_ROLES))))
        .filter(n=len(SLOT_ROLES))
        .order_by("-version_no")
        .first()
    )
    if best is None or deadline_set_withdrawn(best):
        return None
    return best


def usable_deadline_set(*, production: bool) -> EscalationDeadlineSet | None:
    """Section 6.2 "usable": active for the active incident policy and, in production, owner_approved."""
    d = active_deadline_set()
    if d is None or (production and d.source != "owner_approved"):
        return None
    return d


@dataclass
class DeadlineSetInfo:
    id: int
    policy_version: int
    version_no: int
    source: str
    status: str
    deadlines: dict[str, Any]


def deadline_set_info(d: EscalationDeadlineSet) -> DeadlineSetInfo:
    return DeadlineSetInfo(
        d.pk, d.incident_policy.version, d.version_no, d.source, deadline_set_status(d), d.deadlines
    )


def _set(set_id: int) -> EscalationDeadlineSet:
    try:
        return EscalationDeadlineSet.objects.select_related("incident_policy").get(pk=set_id)
    except EscalationDeadlineSet.DoesNotExist:
        raise Invalid(f"No escalation deadline set {set_id}") from None


def _incident_policy(version: int) -> IncidentPolicy:
    try:
        return IncidentPolicy.objects.get(version=version)
    except IncidentPolicy.DoesNotExist:
        raise Invalid(f"No incident policy v{version}") from None


# ------------------------------------------------------------------ commands
def create_deadline_set(
    actor: Any,
    *,
    incident_policy_version: int,
    deadlines: dict[str, Any],
    source: str,
    notes: str = "",
) -> dict[str, Any]:
    """Version N+1 of the deadline set bound to one incident-policy version (ops_lead / safety_reviewer).
    ``deadlines`` keys are exactly the policy's severity levels plus "unclassified"; an empty list is the
    stated decision "no clocked notification at this level". Locks: settings S only."""

    def handler() -> Outcome:
        if source not in SOURCES:
            raise Invalid(f"source must be one of {SOURCES}")
        lock_guards(share_settings=True, label="execution.create_deadline_set")
        policy = _incident_policy(incident_policy_version)
        problem = deadline_rules_problem(policy, deadlines)
        if problem:
            raise Invalid(f"Invalid escalation deadlines: {problem}")
        last = EscalationDeadlineSet.objects.filter(incident_policy=policy).order_by("-version_no").first()
        d = EscalationDeadlineSet.objects.create(
            incident_policy=policy,
            version_no=(last.version_no if last else 0) + 1,
            deadlines=deadlines,
            source=source,
            notes=notes,
            created_by=actor,
        )
        return Outcome(
            "escalation_deadline_set",
            str(d.pk),
            {"incident_policy": policy.version, "version": d.version_no, "source": source, "status": "draft"},
        )

    return run_command(
        actor,
        "execution.create_deadline_set",
        permission="execution.manage_escalation_deadlines",
        payload={
            "incident_policy": incident_policy_version,
            "deadlines": deadlines,
            "source": source,
            "notes": notes,
        },
        handler=handler,
    )


def approve_deadline_set(actor: Any, *, set_id: int, slot: str) -> dict[str, Any]:
    """One slot (safety -> safety_reviewer, founder -> founder): distinct people, never the author, not
    withdrawn, and a synthetic_demo set never in production. Locks: settings S FIRST (mode-dependent; the
    trigger asserts it), nothing else."""

    def handler() -> Outcome:
        if slot not in SLOT_ROLES:
            raise Invalid(f"slot must be one of {sorted(SLOT_ROLES)}")
        lock_guards(share_settings=True, label="execution.approve_deadline_set")
        d = _set(set_id)
        if deadline_set_withdrawn(d):
            raise Conflict("A withdrawn escalation deadline set cannot be approved")
        if not has_role_now(actor.pk, SLOT_ROLES[slot]):
            raise PermissionDenied(f"Slot {slot} requires role {SLOT_ROLES[slot]}")
        if d.created_by_id == actor.pk:
            raise Conflict("The author of an escalation deadline set cannot approve it")
        if EscalationDeadlineSetApproval.objects.filter(deadline_set=d, approver=actor).exists():
            raise Conflict("The same person cannot fill two approval slots on one escalation deadline set")
        if EscalationDeadlineSetApproval.objects.filter(deadline_set=d, slot=slot).exists():
            raise Conflict(f"Slot {slot} is already approved")
        if d.source == "synthetic_demo" and fleet_services.production_mode():
            raise Conflict("A synthetic_demo escalation deadline set cannot be approved in production")
        EscalationDeadlineSetApproval.objects.create(deadline_set=d, slot=slot, approver=actor)
        active = active_deadline_set()
        return Outcome(
            "escalation_deadline_set",
            str(d.pk),
            {
                "slot": slot,
                "status": deadline_set_status(d),
                "active": active is not None and active.pk == d.pk,
            },
        )

    return run_command(
        actor,
        "execution.approve_deadline_set",
        permission="execution.approve_escalation_deadlines",
        payload={"set_id": set_id, "slot": slot},
        handler=handler,
    )


def withdraw_deadline_set(actor: Any, *, set_id: int, reason: str) -> dict[str, Any]:
    """Withdraw a set (safety_reviewer / founder). A withdrawn active set leaves none (no fallback): the read
    models then show ESCALATION_DEADLINE_NOT_CONFIGURED. Locks: settings S only."""

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        lock_guards(share_settings=True, label="execution.withdraw_deadline_set")
        d = _set(set_id)
        if deadline_set_withdrawn(d):
            raise Conflict("The escalation deadline set is already withdrawn")
        EscalationDeadlineSetWithdrawal.objects.create(deadline_set=d, withdrawn_by=actor, reason=reason)
        return Outcome("escalation_deadline_set", str(d.pk), {"status": "withdrawn"})

    return run_command(
        actor,
        "execution.withdraw_deadline_set",
        permission="execution.withdraw_escalation_deadlines",
        payload={"set_id": set_id, "reason": reason},
        handler=handler,
    )
