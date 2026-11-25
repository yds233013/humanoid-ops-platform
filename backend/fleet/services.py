"""Fleet commands. Each one: run_command (auth, idempotency, in-transaction permission) -> guard locks in
the global order -> domain checks with clear errors -> writes (the database re-checks the invariants)."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count, Q
from django.utils import timezone

from accounts.permissions import active_roles
from accounts.roles import INSPECTION_SIGNER_ROLES
from fleet.models import (
    HOLD_POLICY_SOURCES,
    MAX_INTERVAL_MIN,
    RULE_SOURCES,
    TEMPLATE_PURPOSES,
    TEMPLATE_SOURCES,
    BaseAuthorization,
    CapabilityApproval,
    CapabilityProfile,
    ConfigurationManifest,
    EligibilityCheck,
    Hold,
    HoldPolicy,
    HoldPolicyApproval,
    HoldPolicyWithdrawal,
    HoldRebase,
    HoldRebaseActivation,
    HoldRebaseApproval,
    Inspection,
    InspectionTemplate,
    InspectionTemplateApproval,
    InspectionTemplateWithdrawal,
    ManifestPattern,
    PatternApproval,
    PeriodicInspectionRule,
    PeriodicInspectionRuleApproval,
    PeriodicInspectionRuleWithdrawal,
    ReleaseApproval,
    ReleaseApprovalWithdrawal,
    Robot,
)
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, run_command
from platform_core.hubscope import owner_hub, robot_hub
from platform_core.locks import lock_guards

User = get_user_model()

ELIGIBLE_LABEL = "Eligible for planning - not cleared to depart"
NOT_ELIGIBLE_LABEL = "Not eligible for planning"


# ---------------------------------------------------------------- lookups
def _robot(code: str) -> Robot:
    try:
        return Robot.objects.get(code=code)
    except Robot.DoesNotExist:
        raise Invalid(f"No robot {code}") from None


def _pattern(code: str) -> ManifestPattern:
    try:
        return ManifestPattern.objects.get(code=code)
    except ManifestPattern.DoesNotExist:
        raise Invalid(f"No configuration pattern {code}") from None


def _hold(code: str) -> Hold:
    try:
        return Hold.objects.get(code=code)
    except Hold.DoesNotExist:
        raise Invalid(f"No hold {code}") from None


def _next_code(seq: str, prefix: str) -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT nextval(%s)", [seq])
        return f"{prefix}-{cur.fetchone()[0]:04d}"


def _current_pattern_id(robot: Robot) -> int | None:
    if robot.current_manifest_id is None:
        return None
    return ConfigurationManifest.objects.only("pattern_id").get(pk=robot.current_manifest_id).pattern_id


def setting(key: str) -> str | None:
    from fleet.models import DeploymentSetting

    row = DeploymentSetting.objects.filter(key=key, hub__isnull=True).first()  # the global row (v2 03A-1)
    return row.value if row else None


def demo_mode() -> bool:
    return setting("demo_mode") == "true"


def production_mode() -> bool:
    """Contract 07a section 2.2: production unless demo_mode is exactly 'true' (a missing setting is
    production). Read it only after taking the settings lock when the decision depends on it."""
    return not demo_mode()


def db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        value: datetime = cur.fetchone()[0]
        return value


def _has_role_now(user_id: int | None, role: str) -> bool:
    if user_id is None:
        return False
    with connection.cursor() as cur:
        cur.execute("SELECT has_active_role(%s, %s)", [user_id, role])
        return bool(cur.fetchone()[0])


def _has_any_role_now(user_id: int | None, roles: tuple[str, ...]) -> bool:
    return any(_has_role_now(user_id, r) for r in roles)


# Slot -> role. Templates, periodic rules and rebases use fleet_slot_role (no founder); hold policies use
# fleet_hold_policy_slot_role (adds founder, approved_procedure only).
SLOT_ROLES = {"engineering": "engineering_lead", "safety": "safety_reviewer"}
HOLD_POLICY_SLOT_ROLES = {**SLOT_ROLES, "founder": "founder"}
AUTHOR_ROLES = ("engineer", "engineering_lead")
WITHDRAW_ROLES = ("engineering_lead", "safety_reviewer")
RELEASE_ROLES = {
    "ops_lead",
    "engineer",
    "engineering_lead",
    "safety_reviewer",
    "founder",
}  # fleet.approve_release


# ---------------------------------------------------------------- hold policies (G-01 Python mirrors)
def hold_policy_withdrawn(policy: HoldPolicy | None) -> bool:
    return policy is not None and HoldPolicyWithdrawal.objects.filter(policy=policy).exists()


def hold_policy_approved(policy: HoldPolicy | None) -> bool:
    """Mirror of fleet_hold_policy_approved: engineering + safety (synthetic_demo), plus founder
    (approved_procedure). Withdrawal is not considered here."""
    if policy is None:
        return False
    slots = set(
        HoldPolicyApproval.objects.filter(policy=policy, slot__in=list(HOLD_POLICY_SLOT_ROLES)).values_list(
            "slot", flat=True
        )
    )
    return len(slots) == (3 if policy.source == "approved_procedure" else 2)


def policy_production_usable(policy: HoldPolicy | None) -> bool:
    """Mirror of fleet_policy_production_usable (mode-independent): approved_procedure, fully approved, not
    withdrawn. Used by rebasing and the production-blocker panel."""
    return (
        policy is not None
        and policy.source == "approved_procedure"
        and not hold_policy_withdrawn(policy)
        and hold_policy_approved(policy)
    )


def policy_usable(policy: HoldPolicy | None) -> bool:
    """Mirror of fleet_policy_usable (mode-aware, reads settings: hold the settings lock when it decides).
    allow_draft_policies has no effect outside demo mode (EM decision 14)."""
    if policy is None or hold_policy_withdrawn(policy):
        return False
    demo = demo_mode()
    if hold_policy_approved(policy) and (policy.source == "approved_procedure" or demo):
        return True
    return demo and setting("allow_draft_policies") == "true"


def active_hold_policy() -> HoldPolicy | None:
    """Mirror of fleet_active_hold_policy_id: the highest version ever fully approved; none if that one is
    withdrawn (no fallback)."""
    for policy in HoldPolicy.objects.order_by("-version"):
        if hold_policy_approved(policy):
            return None if hold_policy_withdrawn(policy) else policy
    return None


def binding_policy() -> HoldPolicy | None:
    """Mirror of fleet_hold_binding_policy_id (mode-independent, reads no setting): the active policy, else
    the newest, else none. Resolve it only while holding the hold-policy registry (S or U)."""
    return active_hold_policy() or HoldPolicy.objects.order_by("-version").first()


def current_policy() -> HoldPolicy:
    """The policy a hold placed now would bind to (active, else newest). Kept for the fleet pages."""
    policy = binding_policy()
    if policy is None:
        raise Conflict("No hold policy exists yet; an engineer or engineering lead must create one")
    return policy


def policy_status(policy: HoldPolicy) -> str:
    """'withdrawn' | 'approved' (fully approved for its source) | 'draft'."""
    if hold_policy_withdrawn(policy):
        return "withdrawn"
    return "approved" if hold_policy_approved(policy) else "draft"


def policy_label(policy: HoldPolicy | None) -> str:
    """Section 5.6 wording: synthetic draft | synthetic approved | approved procedure | withdrawn | none."""
    if policy is None:
        return "none"
    status = policy_status(policy)
    if status == "withdrawn":
        return "withdrawn"
    if policy.source == "approved_procedure":
        return "approved procedure" if status == "approved" else "approved procedure (awaiting approval)"
    return "synthetic approved" if status == "approved" else "synthetic draft"


def hold_policy_floor_problem(rules: Any) -> str | None:
    """Mirror of fleet_hold_policy_floor_problem (an approved_procedure policy's platform floor)."""
    sources = [k for k, _ in Hold.SOURCES]
    if not isinstance(rules, dict):
        return "rules must be an object keyed by hold source"
    for src in rules:
        if src not in sources:
            return f"unknown hold source {src}"
    for src in sources:
        rule = rules.get(src)
        if not isinstance(rule, dict):
            return f"an approved_procedure policy needs a rule for every hold source (missing {src})"
        slots = rule.get("slots")
        if not isinstance(slots, list) or not slots:
            return f"rule {src} needs at least one slot"
        names: list[str] = []
        for x in slots:
            name = x.get("slot") if isinstance(x, dict) else None
            if not isinstance(name, str) or not name.strip():
                return f"rule {src}: every slot needs a name"
            if name in names:
                return f"rule {src}: duplicate slot {name}"
            names.append(name)
            roles = x.get("roles")
            if not isinstance(roles, list) or not roles:
                return f"rule {src}: slot {name} needs roles"
            for role in roles:
                if role not in RELEASE_ROLES:
                    return f"rule {src}: role {role} may not approve a release"
    inc = rules["incident"]
    if inc.get("distinct_approvers") is not True or inc.get("independent_of_placer") is not True:
        return "the incident rule must require distinct approvers independent of the placer"
    return None


def pattern_status(pattern: ManifestPattern) -> str:
    last = pattern.approvals.order_by("-id").first()
    return last.status if last else "none"


def approved_profile(pattern: ManifestPattern) -> CapabilityProfile | None:
    for profile in pattern.profiles.order_by("-version_no"):
        if set(profile.approvals.values_list("slot", flat=True)) == {"engineering", "safety"}:
            return profile
    return None


# ---------------------------------------------------------------- X-1 template usability (Python mirror)
def template_withdrawn(template: InspectionTemplate) -> bool:
    return InspectionTemplateWithdrawal.objects.filter(template=template).exists()


def template_approved_for(template: InspectionTemplate, purpose: str) -> bool:
    slots = set(
        InspectionTemplateApproval.objects.filter(
            template=template, purpose=purpose, slot__in=list(SLOT_ROLES)
        ).values_list("slot", flat=True)
    )
    return slots == set(SLOT_ROLES)


def template_usable(template: InspectionTemplate | None, purpose: str, *, production: bool) -> bool:
    """R1-1, mirror of fleet_template_usable(p_template, p_purpose, p_production): not withdrawn AND (an
    engineering + a safety approval for the purpose OR (demo AND source = 'synthetic_demo'))."""
    if template is None or template_withdrawn(template):
        return False
    if template_approved_for(template, purpose):
        return True
    return (not production) and template.source == "synthetic_demo"


def template_state(template: InspectionTemplate, purpose: str) -> str:
    """UI wording (section 3.6): approved | synthetic, demo only | awaiting approval | withdrawn."""
    if template_withdrawn(template):
        return "withdrawn"
    if template_approved_for(template, purpose):
        return "approved"
    if template.source == "synthetic_demo":
        return "synthetic, demo only"
    return "awaiting approval"


def template_items_problem(items: Any) -> str | None:
    """Mirror of fleet_template_items_problem."""
    if not isinstance(items, list) or not items:
        return "items must be a non-empty list"
    ids: list[str] = []
    for item in items:
        iid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(iid, str) or not iid.strip():
            return "every item needs a non-blank id"
        if iid in ids:
            return f"duplicate item id {iid}"
        ids.append(iid)
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            return f"item {iid} needs a non-blank label"
        if not isinstance(item.get("robot_condition"), bool):
            return f"item {iid} needs a boolean robot_condition"
    return None


# ---------------------------------------------------------------- X-3 periodic inspection (Python mirror)
def periodic_rule_withdrawn(rule: PeriodicInspectionRule) -> bool:
    return PeriodicInspectionRuleWithdrawal.objects.filter(rule=rule).exists()


def periodic_rule_approved(rule: PeriodicInspectionRule) -> bool:
    slots = set(
        PeriodicInspectionRuleApproval.objects.filter(rule=rule, slot__in=list(SLOT_ROLES)).values_list(
            "slot", flat=True
        )
    )
    return slots == set(SLOT_ROLES)


def active_periodic_rule(pattern_id: int | None) -> PeriodicInspectionRule | None:
    """Mirror of fleet_active_periodic_rule_id: the highest version ever fully approved; none if it is
    withdrawn (no fallback)."""
    if pattern_id is None:
        return None
    rule = (
        PeriodicInspectionRule.objects.filter(pattern_id=pattern_id)
        .annotate(n=Count("approvals__slot", distinct=True, filter=Q(approvals__slot__in=list(SLOT_ROLES))))
        .filter(n=len(SLOT_ROLES))
        .order_by("-version_no")
        .first()
    )
    if rule is None or periodic_rule_withdrawn(rule):
        return None
    return rule


def usable_periodic_rule(pattern_id: int | None, *, production: bool) -> PeriodicInspectionRule | None:
    """Mirror of fleet_usable_periodic_rule_id: the active rule with a known interval, and in production an
    engineering_doc source."""
    rule = active_periodic_rule(pattern_id)
    if rule is None or rule.interval_min is None:
        return None
    if production and rule.source != "engineering_doc":
        return None
    return rule


@dataclass
class PeriodicStatus:
    """state: unknown_interval | never_inspected | overdue | due (contract 07a section 4.2)."""

    state: str
    due_at: datetime | None
    inspection_id: int | None
    rule_id: int | None
    pattern_code: str | None = None
    rule_version: int | None = None
    rule_source: str | None = None
    interval_min: int | None = None
    # v2 02C: read in the same statement by the readiness evaluator (None = not read with this status)
    maintenance_reasons: list[str] | None = None
    maintenance_due: datetime | None = None


def qualifying_periodic_inspection(robot: Robot, *, production: bool) -> Inspection | None:
    """Section 4.2 qualifying inspection: this robot, its CURRENT manifest, complete PASS, template usable for
    'periodic' in the mode, both signers covered (X-8). The latest by (submitted_at, id); no time filter."""
    from planning.services import inspection_signer_problem  # planning imports fleet.services

    if robot.current_manifest_id is None:
        return None
    candidates = (
        Inspection.objects.filter(
            robot_id=robot.pk, manifest_id=robot.current_manifest_id, result="PASS", complete=True
        )
        .select_related("template")
        .order_by("-submitted_at", "-id")
    )
    for i in candidates:
        if template_usable(i.template, "periodic", production=production) and (
            inspection_signer_problem(i) is None
        ):
            return i
    return None


def periodic_inspection_status(
    robot: Robot, at: datetime | None = None, *, production: bool
) -> PeriodicStatus:
    """Independent Python implementation of readiness_periodic_inspection_status(p_robot, p_production, p_at)
    (oracle-tested). ``at`` None means database time. Reads fresh rows."""
    robot = Robot.objects.get(pk=robot.pk)
    if at is None:
        at = db_now()
    pattern = None
    if robot.current_manifest_id is not None:
        pattern = (
            ConfigurationManifest.objects.select_related("pattern").get(pk=robot.current_manifest_id).pattern
        )
    pid = pattern.pk if pattern is not None else None
    code = pattern.code if pattern is not None else None
    rule = usable_periodic_rule(pid, production=production)
    if rule is None:
        active = active_periodic_rule(pid)
        return PeriodicStatus(
            "unknown_interval",
            None,
            None,
            active.pk if active else None,
            code,
            active.version_no if active else None,
            active.source if active else None,
            active.interval_min if active else None,
        )
    info = (code, rule.version_no, rule.source, rule.interval_min)
    insp = qualifying_periodic_inspection(robot, production=production)
    if insp is None:
        return PeriodicStatus("never_inspected", None, None, rule.pk, *info)
    assert rule.interval_min is not None
    due = insp.submitted_at + timedelta(minutes=rule.interval_min)
    return PeriodicStatus("overdue" if at >= due else "due", due, insp.pk, rule.pk, *info)


def format_db_time(value: datetime) -> str:
    """UTC, to the second. SQL equivalent:
    to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' UTC'."""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def periodic_eligibility_reason(status: PeriodicStatus) -> str | None:
    """EM decision 7 reasons, worded as contract 07a section 4.2."""
    if status.state == "unknown_interval":
        return f"Periodic inspection interval unknown for configuration {status.pattern_code}"
    if status.state == "never_inspected":
        return "No qualifying periodic inspection on the current configuration"
    if status.state == "overdue":
        assert status.due_at is not None
        return f"Periodic inspection overdue since {format_db_time(status.due_at)}"
    return None


# ---------------------------------------------------------------- eligibility (read model)
@dataclass
class Eligibility:
    eligible_for_planning: bool
    label: str
    reasons: list[str]
    manifest_id: int | None
    pattern_code: str | None
    periodic: PeriodicStatus | None = field(default=None)


def maintenance_block_reasons(robot: Robot, at: datetime | None = None) -> list[str]:
    """v2 02C: the blocking maintenance reasons of ``robot`` from fleet_maintenance_block_reasons (the SQL the
    eligibility view, the planning triggers and the authorization function use), in the current mode at ``at``
    (None = database time). Empty while the owner-provisioned gate is not active. One statement."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT fleet_maintenance_block_reasons(%s, fleet_production_mode(),"
            " coalesce(%s, clock_timestamp()))",
            [robot.pk, at],
        )
        return list(cur.fetchone()[0] or [])


def evaluate_eligibility(
    robot: Robot,
    at: datetime | None = None,
    *,
    periodic: PeriodicStatus | None = None,
    maintenance: list[str] | None = None,
) -> Eligibility:
    """Fleet eligibility only: may this robot be CONSIDERED when planning a visit? It never means the
    robot may depart - that needs a visit-specific departure authorization (later slice).

    Slice 7 (X-3, EM decision 7): time-dependent. The periodic inspection must be known, present and not
    overdue at ``at`` (None = database time), in the current deployment mode. ``periodic``: the caller's
    already computed ``periodic_inspection_status(robot, at, production=production_mode())`` for this same
    robot, time and mode (the readiness evaluator computes it once and shares it, so it is not read twice).

    v2 02C: while the maintenance gate is active, the blocking maintenance reasons follow the periodic reason
    (same wording and position as operations_robot_eligibility). ``maintenance``: the caller's already read
    ``maintenance_block_reasons(robot, at)`` (the readiness evaluator reads it with the periodic status)."""
    reasons: list[str] = []
    pattern = None
    shared, periodic = periodic, None
    if robot.lifecycle != "in_service":
        reasons.append(f"Lifecycle is {robot.get_lifecycle_display()}, not In service")
    if robot.current_manifest_id is None:
        reasons.append("No configuration manifest recorded")
    else:
        manifest = ConfigurationManifest.objects.select_related("pattern").get(pk=robot.current_manifest_id)
        pattern = manifest.pattern
        status = pattern_status(pattern)
        if status != "approved_home":
            reasons.append(f"Configuration {pattern.code} is not approved for home use ({status})")
        if not BaseAuthorization.objects.filter(
            robot=robot, pattern=pattern, revoked_at__isnull=True
        ).exists():
            reasons.append(f"No base home-use authorization for configuration {pattern.code}")
        profile = approved_profile(pattern)
        if profile is None:
            reasons.append(f"No approved capability profile for {pattern.code}")
        else:
            for f in profile.unknown_fields():
                reasons.append(f"Capability unknown: {f.replace('_', ' ')}")
            if profile.source == "synthetic_demo" and not demo_mode():
                reasons.append("Capability profile is synthetic demo data, not engineering-supplied")
        for h in Hold.objects.filter(
            scope="manifest_pattern", pattern=pattern, released_at__isnull=True, blocking=True
        ).order_by("id"):
            reasons.append(f"Configuration hold {h.code}: {h.reason}")
        periodic = (
            shared
            if shared is not None
            else periodic_inspection_status(robot, at, production=production_mode())
        )
        why = periodic_eligibility_reason(periodic)
        if why:
            reasons.append(why)
        reasons.extend(maintenance if maintenance is not None else maintenance_block_reasons(robot, at))
    # Same order as the SQL view (ORDER BY h.id); without it the row order is the heap order and the reasons
    # list could differ from the view after an update (seen once in CI on the scale fixture).
    for h in Hold.objects.filter(
        scope="robot", robot=robot, released_at__isnull=True, blocking=True
    ).order_by("id"):
        reasons.append(f"Robot hold {h.code} ({h.get_source_type_display()}): {h.reason}")
    ok = not reasons
    return Eligibility(
        ok,
        ELIGIBLE_LABEL if ok else NOT_ELIGIBLE_LABEL,
        reasons,
        robot.current_manifest_id,
        pattern.code if pattern else None,
        periodic,
    )


def check_eligibility(actor: Any, *, robot_code: str) -> dict[str, Any]:
    """Locked, recorded eligibility evaluation - the primitive later departure authorization builds on."""

    def handler() -> Outcome:
        robot = _robot(robot_code)
        pid = _current_pattern_id(robot)
        lock_guards(
            share_patterns=[pid] if pid else [], share_robots=[robot.pk], label="fleet.check_eligibility"
        )
        robot.refresh_from_db()
        pattern_version = ManifestPattern.objects.get(pk=pid).version if pid else None
        e = evaluate_eligibility(robot)
        rec = EligibilityCheck.objects.create(
            robot=robot,
            robot_version_observed=robot.version,
            pattern_version_observed=pattern_version,
            manifest_id=e.manifest_id,
            eligible_for_planning=e.eligible_for_planning,
            reasons=e.reasons,
            checked_by=actor,
        )
        return Outcome(
            "eligibility_check",
            str(rec.pk),
            {
                "robot": robot.code,
                "eligible_for_planning": e.eligible_for_planning,
                "label": e.label,
                "reasons": e.reasons,
                "robot_version_observed": robot.version,
            },
        )

    return run_command(
        actor,
        "fleet.check_eligibility",
        permission="fleet.check_eligibility",
        payload={"robot": robot_code},
        handler=handler,
        hub=robot_hub(robot_code),
    )


# ---------------------------------------------------------------- robots and lifecycle
def register_robot(
    actor: Any, *, code: str, serial: str, hub_code: str | None = None, idempotency_key: str | None = None
) -> dict[str, Any]:
    """Register a robot at its HOME hub (v2 03A-1): ``hub_code``, or the only hub when there is one (the
    database default refuses to guess when there are several)."""
    hub = owner_hub(hub_code)
    payload: dict[str, Any] = {"code": code, "serial": serial}
    if hub is not None:
        payload["hub"] = hub.code

    def handler() -> Outcome:
        if Robot.objects.filter(code=code).exists() or Robot.objects.filter(serial=serial).exists():
            raise Conflict(f"A robot with code {code} or serial {serial} already exists")
        extra = {"hub": hub} if hub is not None else {}
        robot = Robot.objects.create(code=code, serial=serial, registered_by=actor, **extra)
        return Outcome("robot", robot.code, {"lifecycle": robot.lifecycle, "hub_id": robot.hub_id})

    return run_command(
        actor,
        "fleet.register_robot",
        permission="fleet.register_robot",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
        hub=hub,
    )


def transition_lifecycle(actor: Any, *, robot_code: str, to: str, reason: str) -> dict[str, Any]:
    permission = "fleet.retire_robot" if to == "retired" else "fleet.transition_lifecycle"

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        robot = _robot(robot_code)
        lock_guards(update_robots=[robot.pk], label="fleet.transition_lifecycle")
        robot.refresh_from_db()
        if to == "in_service":
            if robot.current_manifest_id is None:
                raise Conflict("Record a configuration manifest before putting a robot in service")
            pid = _current_pattern_id(robot)
            profile = approved_profile(ManifestPattern.objects.get(pk=pid or 0))
            if profile is None or profile.stop_behaviour in (None, ""):
                raise Conflict("Stop behaviour must be documented in an approved capability profile first")
        frm = robot.lifecycle
        robot.lifecycle = to
        robot.save(update_fields=["lifecycle", "version"])  # the database validates the transition
        return Outcome("robot", robot.code, {"from": frm, "to": to})

    return run_command(
        actor,
        f"fleet.lifecycle.{to}",
        permission=permission,
        payload={"robot": robot_code, "to": to, "reason": reason},
        handler=handler,
    )


# ---------------------------------------------------------------- configuration
def create_pattern(
    actor: Any, *, code: str, hardware_rev: str, release: str, firmware_set: str
) -> dict[str, Any]:
    def handler() -> Outcome:
        if ManifestPattern.objects.filter(code=code).exists():
            raise Conflict(f"Pattern {code} exists")
        p = ManifestPattern.objects.create(
            code=code, hardware_rev=hardware_rev, release=release, firmware_set=firmware_set
        )
        return Outcome("manifest_pattern", p.code)

    return run_command(
        actor,
        "fleet.create_pattern",
        permission="fleet.manage_patterns",
        payload={
            "code": code,
            "hardware_rev": hardware_rev,
            "release": release,
            "firmware_set": firmware_set,
        },
        handler=handler,
    )


def decide_pattern(actor: Any, *, pattern_code: str, status: str, reason: str) -> dict[str, Any]:
    def handler() -> Outcome:
        if status not in dict(PatternApproval.STATUS):
            raise Invalid(f"Unknown status {status}")
        p = _pattern(pattern_code)
        robot_ids = list(Robot.objects.filter(current_manifest__pattern=p).values_list("pk", flat=True))
        lock_guards(update_patterns=[p.pk], update_robots=robot_ids, label="fleet.decide_pattern")
        PatternApproval.objects.create(pattern=p, status=status, reason=reason, decided_by=actor)
        return Outcome("manifest_pattern", p.code, {"status": status})

    return run_command(
        actor,
        "fleet.decide_pattern",
        permission="fleet.decide_pattern",
        payload={"pattern": pattern_code, "status": status, "reason": reason},
        handler=handler,
    )


def record_manifest(
    actor: Any,
    *,
    robot_code: str,
    pattern_code: str,
    autonomy_version: str,
    safety_config_hash: str,
    model_versions: dict[str, str] | None = None,
    calibration_ref: str = "",
    provenance: str = "manual",
) -> dict[str, Any]:
    payload = {
        "robot": robot_code,
        "pattern": pattern_code,
        "autonomy_version": autonomy_version,
        "model_versions": model_versions or {},
        "safety_config_hash": safety_config_hash,
        "calibration_ref": calibration_ref,
        "provenance": provenance,
    }

    def handler() -> Outcome:
        robot = _robot(robot_code)
        new_p = _pattern(pattern_code)
        old_pid = _current_pattern_id(robot)
        lock_guards(
            share_patterns=[x for x in (old_pid, new_p.pk) if x],
            update_robots=[robot.pk],
            label="fleet.record_manifest",
        )
        content_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        m = ConfigurationManifest.objects.create(
            robot=robot,
            pattern=new_p,
            autonomy_version=autonomy_version,
            model_versions=model_versions or {},
            safety_config_hash=safety_config_hash,
            calibration_ref=calibration_ref,
            provenance=provenance,
            content_hash=content_hash,
            recorded_by=actor,
        )
        robot.refresh_from_db()
        robot.current_manifest = m
        robot.save(update_fields=["current_manifest", "version"])
        return Outcome("robot", robot.code, {"manifest_id": m.pk, "pattern": new_p.code})

    return run_command(
        actor, "fleet.record_manifest", permission="fleet.record_manifest", payload=payload, handler=handler
    )


def publish_capability_profile(
    actor: Any, *, pattern_code: str, source: str = "synthetic_demo", **fields: Any
) -> dict[str, Any]:
    allowed = {
        "stop_behaviour",
        "lift_crew",
        "transport_posture",
        "max_runtime_min",
        "min_departure_soc_pct",
        "charge_turnaround_min",
        "load_unload_min",
    }
    if set(fields) - allowed:
        raise Invalid(f"Unknown capability fields: {sorted(set(fields) - allowed)}")

    def handler() -> Outcome:
        p = _pattern(pattern_code)
        lock_guards(update_patterns=[p.pk], label="fleet.publish_capability")
        n = (p.profiles.order_by("-version_no").values_list("version_no", flat=True).first() or 0) + 1
        prof = CapabilityProfile.objects.create(
            pattern=p, version_no=n, source=source, published_by=actor, **fields
        )
        return Outcome(
            "capability_profile",
            str(prof.pk),
            {"pattern": p.code, "version": n, "unknown": prof.unknown_fields()},
        )

    return run_command(
        actor,
        "fleet.publish_capability",
        permission="fleet.publish_capability",
        payload={"pattern": pattern_code, "source": source, **fields},
        handler=handler,
    )


def approve_capability_profile(actor: Any, *, profile_id: int, slot: str) -> dict[str, Any]:
    def handler() -> Outcome:
        prof = CapabilityProfile.objects.get(pk=profile_id)
        lock_guards(update_patterns=[prof.pattern_id], label="fleet.approve_capability")
        if CapabilityApproval.objects.filter(profile=prof, approver=actor).exists():
            raise Conflict("You already approved this profile in another slot")
        if CapabilityApproval.objects.filter(profile=prof, slot=slot).exists():
            raise Conflict(f"Slot {slot} already approved")
        CapabilityApproval.objects.create(profile=prof, slot=slot, approver=actor)
        return Outcome("capability_profile", str(prof.pk), {"slot": slot})

    return run_command(
        actor,
        "fleet.approve_capability",
        permission="fleet.approve_capability",
        payload={"profile_id": profile_id, "slot": slot},
        handler=handler,
    )


def grant_base_authorization(actor: Any, *, robot_code: str) -> dict[str, Any]:
    def handler() -> Outcome:
        robot = _robot(robot_code)
        pid = _current_pattern_id(robot)
        if pid is None:
            raise Conflict("Robot has no configuration manifest")
        lock_guards(share_patterns=[pid], update_robots=[robot.pk], label="fleet.base_authorization")
        if BaseAuthorization.objects.filter(robot=robot, pattern_id=pid, revoked_at__isnull=True).exists():
            raise Conflict("Already authorized for this configuration")
        a = BaseAuthorization.objects.create(robot=robot, pattern_id=pid, granted_by=actor)
        return Outcome("base_authorization", str(a.pk), {"robot": robot.code})

    return run_command(
        actor,
        "fleet.grant_base_authorization",
        permission="fleet.base_authorization",
        payload={"robot": robot_code},
        handler=handler,
    )


def revoke_base_authorization(actor: Any, *, robot_code: str, reason: str) -> dict[str, Any]:
    def handler() -> Outcome:
        robot = _robot(robot_code)
        lock_guards(update_robots=[robot.pk], label="fleet.revoke_base_authorization")
        a = BaseAuthorization.objects.filter(robot=robot, revoked_at__isnull=True).first()
        if a is None:
            raise Conflict("No active base authorization")
        a.revoked_at = timezone.now()
        a.revoked_by = actor
        a.save(update_fields=["revoked_at", "revoked_by"])
        return Outcome("base_authorization", str(a.pk), {"revoked": True})

    return run_command(
        actor,
        "fleet.revoke_base_authorization",
        permission="fleet.base_authorization",
        payload={"robot": robot_code, "reason": reason},
        handler=handler,
    )


# ---------------------------------------------------------------- hold policy (G-01 lifecycle)
def _hold_policy(version: int) -> HoldPolicy:
    try:
        return HoldPolicy.objects.get(version=version)
    except HoldPolicy.DoesNotExist:
        raise Invalid(f"No hold policy v{version}") from None


def create_hold_policy(
    actor: Any, *, rules: dict[str, Any], source: str = "synthetic_demo", procedure_ref: str = ""
) -> dict[str, Any]:
    """Create hold-policy version N+1 (engineer / engineering_lead). Locks: hold-policy registry U only
    (creation changes the "newest" binding). An approved_procedure policy needs a procedure reference and the
    platform floor (section 5.2); the database re-checks both."""

    def handler() -> Outcome:
        if source not in dict(HOLD_POLICY_SOURCES):
            raise Invalid(f"source must be one of {[k for k, _ in HOLD_POLICY_SOURCES]}")
        for src, rule in rules.items():
            if src not in dict(Hold.SOURCES):
                raise Invalid(f"Unknown hold source {src}")
            slots = rule.get("slots") or [] if isinstance(rule, dict) else []
            if not slots or any(not s.get("slot") or not s.get("roles") for s in slots):
                raise Invalid(f"Rule for {src} needs slots with names and roles")
            if len({s["slot"] for s in slots}) != len(slots):
                raise Invalid(f"Duplicate slot names in {src}")
        if source == "approved_procedure":
            if not procedure_ref.strip():
                raise Invalid("An approved_procedure hold policy needs a procedure reference")
            problem = hold_policy_floor_problem(rules)
            if problem:
                raise Invalid(f"Hold policy floor: {problem}")
        lock_guards(update_hold_policy_registry=True, label="fleet.create_hold_policy")
        version = (HoldPolicy.objects.order_by("-version").values_list("version", flat=True).first() or 0) + 1
        p = HoldPolicy.objects.create(
            version=version, rules=rules, source=source, procedure_ref=procedure_ref, created_by=actor
        )
        return Outcome("hold_policy", str(p.version), {"status": "draft", "source": source})

    return run_command(
        actor,
        "fleet.create_hold_policy",
        permission="fleet.manage_hold_policy",
        payload={"rules": rules, "source": source, "procedure_ref": procedure_ref},
        handler=handler,
    )


def approve_hold_policy(actor: Any, *, version: int, slot: str) -> dict[str, Any]:
    """One approval slot (engineering / safety, plus founder on approved_procedure). Distinct people, not
    the author, role held now. Locks: registry U only (an approval may change the active policy)."""

    def handler() -> Outcome:
        if slot not in HOLD_POLICY_SLOT_ROLES:
            raise Invalid(f"slot must be one of {sorted(HOLD_POLICY_SLOT_ROLES)}")
        lock_guards(update_hold_policy_registry=True, label="fleet.approve_hold_policy")
        p = _hold_policy(version)
        if hold_policy_withdrawn(p):
            raise Conflict(f"Hold policy v{version} is withdrawn and cannot be approved")
        if slot == "founder" and p.source != "approved_procedure":
            raise Conflict("The founder slot exists only on an approved_procedure hold policy")
        if not _has_role_now(actor.pk, HOLD_POLICY_SLOT_ROLES[slot]):
            raise PermissionDenied(f"Slot {slot} requires role {HOLD_POLICY_SLOT_ROLES[slot]}")
        if p.created_by_id == actor.pk:
            raise Conflict("The author of a hold policy cannot approve it")
        if HoldPolicyApproval.objects.filter(policy=p, approver=actor).exists():
            raise Conflict("You already approved this policy in another slot")
        if HoldPolicyApproval.objects.filter(policy=p, slot=slot).exists():
            raise Conflict(f"Slot {slot} is already approved")
        HoldPolicyApproval.objects.create(policy=p, slot=slot, approver=actor)
        active = active_hold_policy()
        return Outcome(
            "hold_policy",
            str(p.version),
            {"slot": slot, "status": policy_status(p), "active": active is not None and active.pk == p.pk},
        )

    return run_command(
        actor,
        "fleet.approve_hold_policy",
        permission="fleet.approve_hold_policy",
        payload={"version": version, "slot": slot},
        handler=handler,
    )


def withdraw_hold_policy(actor: Any, *, version: int, reason: str) -> dict[str, Any]:
    """Withdraw a policy version (engineering_lead / safety_reviewer). A withdrawn active policy leaves no
    active policy (no fallback). Locks: registry U only."""

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        lock_guards(update_hold_policy_registry=True, label="fleet.withdraw_hold_policy")
        p = _hold_policy(version)
        if hold_policy_withdrawn(p):
            raise Conflict(f"Hold policy v{version} is already withdrawn")
        HoldPolicyWithdrawal.objects.create(policy=p, withdrawn_by=actor, reason=reason)
        return Outcome("hold_policy", str(p.version), {"status": "withdrawn"})

    return run_command(
        actor,
        "fleet.withdraw_hold_policy",
        permission="fleet.withdraw_hold_policy",
        payload={"version": version, "reason": reason},
        handler=handler,
    )


# ---------------------------------------------------------------- holds
def _create_hold(
    actor: Any,
    *,
    scope: str,
    robot: Robot | None,
    pattern: ManifestPattern | None,
    source_type: str,
    source_ref: str,
    reason: str,
    blocking: bool = True,
) -> Hold:
    """Placing a hold never depends on a policy being approved, on a setting or on the mode (section 0a): no
    settings lock, no setting read, never refused for policy reasons. The caller already holds the robot U (or
    pattern U + robots U). The hold-policy registry is taken S LAST, and only then is the binding resolved
    (active, else newest, else none; section 5.2, P0-3), so it cannot change before this transaction commits.
    Without a release rule the hold has no slots and stays unreleasable until a rebase gives it one."""
    lock_guards(share_hold_policy_registry=True, label="fleet.hold_binding")
    policy = binding_policy()
    rule = policy.rules.get(source_type) if policy else None
    return Hold.objects.create(
        code=_next_code("fleet_hold_code_seq", "HD"),
        scope=scope,
        robot=robot,
        pattern=pattern,
        blocking=blocking,
        source_type=source_type,
        source_ref=source_ref,
        reason=reason,
        policy=policy if rule else None,
        required_slots=rule["slots"] if rule else [],
        manifest_at_placement_id=robot.current_manifest_id if robot else None,
        placed_by=actor,
    )


def _pattern_robot_ids(pattern_id: int) -> list[int]:
    return list(Robot.objects.filter(current_manifest__pattern_id=pattern_id).values_list("pk", flat=True))


def _lock_hold_target(h: Hold, label: str, *, share_settings: bool = False, incident: bool = False) -> None:
    """Global order: [settings S] -> robot U (or pattern U + its robots U) -> [incident S] -> registry S."""
    incidents: list[int] = []
    if incident:
        from execution.models import Incident  # execution depends on fleet, not the reverse

        incidents = list(Incident.objects.filter(hold_id=h.pk).values_list("pk", flat=True))
    if h.scope == "robot":
        assert h.robot_id is not None
        lock_guards(
            share_settings=share_settings,
            update_robots=[h.robot_id],
            share_incidents=incidents,
            share_hold_policy_registry=True,
            label=label,
        )
    else:
        assert h.pattern_id is not None
        lock_guards(
            share_settings=share_settings,
            update_patterns=[h.pattern_id],
            update_robots=_pattern_robot_ids(h.pattern_id),
            share_incidents=incidents,
            share_hold_policy_registry=True,
            label=label,
        )


def place_hold(
    actor: Any,
    *,
    scope: str,
    target: str,
    source_type: str,
    reason: str,
    source_ref: str = "",
    blocking: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = {
        "scope": scope,
        "target": target,
        "source_type": source_type,
        "source_ref": source_ref,
        "reason": reason,
        "blocking": blocking,
    }

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        if source_type == "incident":
            # Slice 7 (G-01): an incident hold comes only from execution.report_incident, which records the
            # Incident its release depends on (EM decision 12).
            raise Invalid(
                "Incident holds are placed only by reporting the incident (execution.report_incident)"
            )
        if source_type not in dict(Hold.SOURCES):
            raise Invalid(f"Unknown hold source {source_type}")
        if scope == "robot":
            robot = _robot(target)
            lock_guards(update_robots=[robot.pk], label="fleet.place_hold")
            robot.refresh_from_db()
            h = _create_hold(
                actor,
                scope=scope,
                robot=robot,
                pattern=None,
                source_type=source_type,
                source_ref=source_ref,
                reason=reason,
                blocking=blocking,
            )
        elif scope == "manifest_pattern":
            p = _pattern(target)
            lock_guards(
                update_patterns=[p.pk], update_robots=_pattern_robot_ids(p.pk), label="fleet.place_hold"
            )
            h = _create_hold(
                actor,
                scope=scope,
                robot=None,
                pattern=p,
                source_type=source_type,
                source_ref=source_ref,
                reason=reason,
                blocking=blocking,
            )
        else:
            raise Invalid("Slice 1 supports robot and manifest_pattern hold scopes only")
        h.refresh_from_db()
        return Outcome(
            "hold",
            h.code,
            {
                "scope": scope,
                "target": target,
                "policy_version": h.policy.version if h.policy else None,
                "required_slots": [x["slot"] for x in h.required_slots],
            },
        )

    return run_command(
        actor,
        "fleet.place_hold",
        permission="fleet.place_hold",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
    )


@dataclass
class EffectiveRule:
    """Mirror of fleet_hold_effective_rule(h): the latest effective rebase, else the placement values."""

    policy: HoldPolicy | None
    rebase: HoldRebase | None
    slots: list[dict[str, Any]]
    rule: dict[str, Any] | None

    @property
    def origin(self) -> str:
        return "rebase" if self.rebase is not None else "placement"


def hold_effective_rule(h: Hold) -> EffectiveRule:
    act = (
        HoldRebaseActivation.objects.filter(hold_id=h.pk)
        .select_related("rebase", "rebase__to_policy")
        .order_by("-id")
        .first()
    )
    if act is not None:
        b = act.rebase
        return EffectiveRule(
            b.to_policy, b, list(b.required_slots or []), b.to_policy.rules.get(h.source_type)
        )
    policy = HoldPolicy.objects.filter(pk=h.policy_id).first() if h.policy_id else None
    return EffectiveRule(
        policy, None, list(h.required_slots or []), policy.rules.get(h.source_type) if policy else None
    )


def _current_manifest_for(h: Hold) -> int | None:
    if h.scope != "robot" or h.robot_id is None:
        return None
    return Robot.objects.only("current_manifest_id").get(pk=h.robot_id).current_manifest_id


def _valid_release_approval(
    h: Hold, a: ReleaseApproval, eff: EffectiveRule, cur_manifest: int | None
) -> bool:
    if hasattr(a, "withdrawal"):
        return False
    if a.rebase_id != (eff.rebase.pk if eff.rebase else None):
        return False
    return h.scope != "robot" or a.manifest_id == cur_manifest


def _incident_of_hold(h: Hold) -> Any:
    from execution.models import Incident  # execution depends on fleet, not the reverse

    return Incident.objects.filter(hold_id=h.pk).first()


def hold_source_problem(h: Hold, *, production: bool) -> str | None:
    """Mirror of fleet_hold_source_problem (execution/0005, EM decision 12)."""
    incident = _incident_of_hold(h)
    if incident is not None:
        from execution.incidents import incident_status

        st = incident_status(incident)
        if st not in ("resolved", "closed"):
            return (
                f"incident {incident.code} is {st}; its hold can be released only once the incident is "
                "resolved or closed"
            )
    elif h.source_type == "incident" and production:
        return "an incident hold with no incident record cannot be released in production"
    return None


def _newer_pass_exists(h: Hold, cur_manifest: int | None, *, production: bool) -> bool:
    """R1-5: in production only a PASS on a template usable for pre_departure or periodic counts."""
    if h.robot_id is None:
        return False
    for i in Inspection.objects.filter(
        robot_id=h.robot_id, result="PASS", submitted_at__gt=h.placed_at, manifest_id=cur_manifest
    ).select_related("template"):
        if not production:
            return True
        if template_usable(i.template, "pre_departure", production=True) or template_usable(
            i.template, "periodic", production=True
        ):
            return True
    return False


def hold_release_problem(h: Hold) -> str | None:
    """Python mirror of fleet_hold_release_problem(h), same order and text (oracle-tested). Mode-aware: read
    it under the settings lock when it decides."""
    h = Hold.objects.get(pk=h.pk)
    eff = hold_effective_rule(h)
    if eff.policy is None or not eff.slots:
        return "no release rule exists for this hold; an approved policy must provide one"
    production = production_mode()
    if not policy_usable(eff.policy):
        if hold_policy_withdrawn(eff.policy):
            return (
                f"hold policy v{eff.policy.version} was withdrawn; a re-basing decision to the active "
                "approved policy is needed"
            )
        if production:
            return (
                f"hold policy v{eff.policy.version} is not usable in production (it must be a fully approved "
                "approved_procedure policy); a re-basing decision to the active approved policy is needed"
            )
        return "the hold policy is a draft and drafts are not allowed in this deployment"
    cur_manifest = _current_manifest_for(h)
    approvals = list(h.approvals.all())
    for s in eff.slots:
        if not any(
            a.slot == s["slot"] and _valid_release_approval(h, a, eff, cur_manifest) for a in approvals
        ):
            return f"slot {s['slot']} has no valid approval on the current configuration"
    rule = eff.rule or {}
    if bool(rule.get("requires_newer_pass_inspection")) and h.scope == "robot":
        if not _newer_pass_exists(h, cur_manifest, production=production):
            if production:
                return (
                    "needs a newer PASS inspection on the current configuration, on a template approved for "
                    "pre-departure or periodic use (production)"
                )
            return "needs a newer PASS inspection on the current configuration"
    return hold_source_problem(h, production=production)


def hold_release_status(h: Hold) -> dict[str, Any]:
    """Explained release state (section 5.5): the effective rule and its origin (placement / rebase), which
    effective slots are filled by valid approvals on the CURRENT configuration (approvals given before a
    rebase do not count), production usability, the newer-PASS and incident conditions, and the exact database
    problem (``problem``; None means releasable)."""
    h = Hold.objects.select_related("robot").get(pk=h.pk)
    eff = hold_effective_rule(h)
    production = production_mode()
    cur_manifest = _current_manifest_for(h)
    rebase_id = eff.rebase.pk if eff.rebase else None
    slots = []
    for s in eff.slots:
        valid, stale, withdrawn, superseded = None, [], [], []
        for a in h.approvals.all():
            if a.slot != s["slot"]:
                continue
            if hasattr(a, "withdrawal"):
                withdrawn.append(a)
            elif a.rebase_id != rebase_id:
                superseded.append(a)  # given under an earlier rule: never fills a slot
            elif h.scope == "robot" and a.manifest_id != cur_manifest:
                stale.append(a)
            else:
                valid = a
        slots.append(
            {
                "slot": s["slot"],
                "roles": s["roles"],
                "valid": valid,
                "stale": stale,
                "withdrawn": withdrawn,
                "superseded": superseded,
            }
        )
    missing = [s["slot"] for s in slots if s["valid"] is None]
    if eff.policy is None or not eff.slots:
        missing = ["(no release rule in an approved policy)"]
    rule = eff.rule or {}
    needs_pass = bool(rule.get("requires_newer_pass_inspection")) and h.scope == "robot"
    has_pass = (not needs_pass) or _newer_pass_exists(h, cur_manifest, production=production)
    problem = hold_release_problem(h)
    incident = _incident_of_hold(h)
    return {
        "slots": slots,
        "missing": missing,
        "needs_newer_pass": needs_pass and not has_pass,
        "policy_status": policy_status(eff.policy) if eff.policy else "none",
        "policy_label": policy_label(eff.policy),
        "policy_version": eff.policy.version if eff.policy else None,
        "placement_policy_version": h.policy.version if h.policy else None,
        "origin": eff.origin,
        "rebase": eff.rebase.code if eff.rebase else None,
        "production_usable": policy_production_usable(eff.policy),
        "usable_now": policy_usable(eff.policy),
        "needs_rebase": not policy_production_usable(eff.policy),
        "incident": incident.code if incident is not None else None,
        "incident_problem": hold_source_problem(h, production=production),
        "production": production,
        "problem": problem,
        "releasable": problem is None,
    }


def approve_release(
    actor: Any,
    *,
    hold_code: str,
    slot: str,
    role_used: str,
    note: str = "",
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "hold": hold_code,
        "slot": slot,
        "role_used": role_used,
        "note": note,
        "evidence_refs": evidence_refs or [],
    }

    def handler() -> Outcome:
        h = _hold(hold_code)
        if role_used not in active_roles(actor):
            raise PermissionDenied(f"You do not hold role {role_used}")
        _lock_hold_target(h, "fleet.approve_release")  # robot U (or pattern U + robots U), registry S
        manifest_id = _current_manifest_for(h)
        a = ReleaseApproval.objects.create(
            hold=h,
            slot=slot,
            approver=actor,
            role_used=role_used,
            manifest_id=manifest_id,
            evidence_refs=evidence_refs or [],
            note=note,
        )
        a.refresh_from_db()  # rebase_id is forced by the trigger
        return Outcome(
            "hold",
            h.code,
            {"approval_id": a.pk, "slot": slot, "rebase": a.rebase.code if a.rebase else None},
        )

    return run_command(
        actor, "fleet.approve_release", permission="fleet.approve_release", payload=payload, handler=handler
    )


def withdraw_release_approval(actor: Any, *, approval_id: int, reason: str) -> dict[str, Any]:
    def handler() -> Outcome:
        a = ReleaseApproval.objects.select_related("hold").get(pk=approval_id)
        if a.approver_id != actor.pk:
            raise PermissionDenied("Only the approver can withdraw their approval")
        _lock_hold_target(a.hold, "fleet.withdraw_release_approval")
        a.hold.refresh_from_db()
        if a.hold.released_at is not None:
            raise Conflict("Hold already released")
        ReleaseApprovalWithdrawal.objects.create(approval=a, withdrawn_by=actor, reason=reason)
        return Outcome("hold", a.hold.code, {"withdrawn_approval": a.pk})

    return run_command(
        actor,
        "fleet.withdraw_release_approval",
        permission="fleet.approve_release",
        payload={"approval_id": approval_id, "reason": reason},
        handler=handler,
    )


def release_hold(actor: Any, *, hold_code: str) -> dict[str, Any]:
    """Release is mode-dependent (fleet_policy_usable, R1-5, incident hold without an Incident row), so it
    takes the settings lock SHARED FIRST, then robot U (or pattern U + robots U), the referencing incident S
    and the registry S (section 9.1). The database trigger asserts the settings lock and re-decides it all."""

    def handler() -> Outcome:
        h = _hold(hold_code)
        _lock_hold_target(h, "fleet.release_hold", share_settings=True, incident=True)
        h = Hold.objects.select_related("robot", "policy").get(pk=h.pk)
        if h.released_at is not None:
            raise Conflict("Hold already released")
        problem = hold_release_problem(h)
        if problem is not None:
            if problem.startswith("slot "):
                missing = hold_release_status(h)["missing"]
                raise Conflict(f"Cannot release {h.code}: missing approvals for {', '.join(missing)}")
            raise Conflict(f"Cannot release {h.code}: {problem}")
        h.released_at = timezone.now()
        h.released_by = actor
        h.save(update_fields=["released_at", "released_by"])  # the database re-checks everything
        return Outcome("hold", h.code, {"released": True})

    return run_command(
        actor,
        "fleet.release_hold",
        permission="fleet.release_hold",
        payload={"hold": hold_code},
        handler=handler,
    )


# ---------------------------------------------------------------- hold re-basing (G-01, EM decision 11)
def _rebase(rebase_id: int) -> HoldRebase:
    try:
        return HoldRebase.objects.select_related("hold", "to_policy").get(pk=rebase_id)
    except HoldRebase.DoesNotExist:
        raise Invalid(f"No rebase {rebase_id}") from None


def rebase_state(b: HoldRebase) -> str:
    """effective | superseded | destination no longer active | released | pending (section 5.2)."""
    if HoldRebaseActivation.objects.filter(rebase=b).exists():
        return "effective"
    h = Hold.objects.get(pk=b.hold_id)
    if h.released_at is not None:
        return "released"
    eff = hold_effective_rule(h)
    if (eff.rebase.pk if eff.rebase else None) != b.based_on_rebase_id:
        return "superseded"
    active = active_hold_policy()
    if active is None or active.pk != b.to_policy_id or not policy_production_usable(active):
        return "destination no longer active"
    return "pending"


def request_hold_rebase(actor: Any, *, hold_code: str, reason: str) -> dict[str, Any]:
    """Request re-basing an unreleased hold whose effective policy is not production-usable onto the ACTIVE
    production-usable policy (slots copied exactly). Mode-independent (no settings lock). Locks: robot U (or
    pattern U + robots U), then registry S."""

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A re-basing request needs a reason")
        h = _hold(hold_code)
        _lock_hold_target(h, "fleet.request_hold_rebase")
        h.refresh_from_db()
        if h.released_at is not None:
            raise Conflict(f"Hold {h.code} is already released")
        eff = hold_effective_rule(h)
        if policy_production_usable(eff.policy):
            raise Conflict(
                f"Hold {h.code} is already under a production-usable policy; "
                "it is released under its own rules"
            )
        active = active_hold_policy()
        if active is None or not policy_production_usable(active):
            raise Conflict(
                "There is no active production-usable (approved_procedure) hold policy to re-base onto"
            )
        rule = active.rules.get(h.source_type) or {}
        slots = rule.get("slots") or []
        if not slots:
            raise Conflict(f"Hold policy v{active.version} has no release rule for {h.source_type} holds")
        b = HoldRebase.objects.create(
            code=_next_code("fleet_hold_rebase_code_seq", "RB"),
            hold=h,
            from_policy=eff.policy,
            to_policy=active,
            based_on_rebase=eff.rebase,
            required_slots=slots,
            reason=reason,
            requested_by=actor,
        )
        return Outcome(
            "hold_rebase",
            b.code,
            {
                "id": b.pk,
                "hold": h.code,
                "from_policy": eff.policy.version if eff.policy else None,
                "to_policy": active.version,
                "based_on": eff.rebase.code if eff.rebase else None,
            },
        )

    return run_command(
        actor,
        "fleet.request_hold_rebase",
        permission="fleet.rebase_hold",
        payload={"hold": hold_code, "reason": reason},
        handler=handler,
    )


def approve_hold_rebase(actor: Any, *, rebase_id: int, slot: str) -> dict[str, Any]:
    """One of two slots (engineering_lead, safety_reviewer), distinct people, neither the placer. The FINAL
    approval makes the rebase effective only if, at that moment, the hold is unreleased, the destination is
    still the active production policy, no other rebase became effective first, and the hold's effective
    policy is still not production-usable (the trigger re-checks and inserts the activation). Locks: robot U
    (or pattern U + robots U), registry S. Mode-independent."""

    def handler() -> Outcome:
        if slot not in SLOT_ROLES:
            raise Invalid(f"slot must be one of {sorted(SLOT_ROLES)}")
        b = _rebase(rebase_id)
        h = b.hold
        _lock_hold_target(h, "fleet.approve_hold_rebase")
        h.refresh_from_db()
        if not _has_role_now(actor.pk, SLOT_ROLES[slot]):
            raise PermissionDenied(f"Slot {slot} requires role {SLOT_ROLES[slot]}")
        if h.placed_by_id == actor.pk:
            raise Conflict("The person who placed the hold cannot approve its re-basing")
        if HoldRebaseApproval.objects.filter(rebase=b, approver=actor).exists():
            raise Conflict("The same person cannot fill both approval slots of a re-basing")
        if HoldRebaseApproval.objects.filter(rebase=b, slot=slot).exists():
            raise Conflict(f"Slot {slot} of rebase {b.code} is already approved")
        if HoldRebaseActivation.objects.filter(rebase=b).exists():
            raise Conflict(f"Rebase {b.code} is already effective")
        final = (
            HoldRebaseApproval.objects.filter(rebase=b, slot__in=list(SLOT_ROLES)).exclude(slot=slot).exists()
        )
        if final:
            if h.released_at is not None:
                raise Conflict(f"Rebase {b.code} refused: hold {h.code} is already released")
            active = active_hold_policy()
            if active is None or active.pk != b.to_policy_id or not policy_production_usable(active):
                shown = (
                    f"v{active.version}"
                    if active is not None and policy_production_usable(active)
                    else "none"
                )
                raise Conflict(
                    f"Rebase {b.code} refused: policy v{b.to_policy.version} is no longer the active "
                    f"production policy (active: {shown})"
                )
            eff = hold_effective_rule(h)
            if (eff.rebase.pk if eff.rebase else None) != b.based_on_rebase_id:
                cur = eff.rebase.code if eff.rebase else "(none)"
                raise Conflict(
                    f"Rebase {b.code} refused: superseded by rebase {cur}, which became effective first"
                )
            if policy_production_usable(eff.policy):
                raise Conflict(
                    f"Rebase {b.code} refused: hold {h.code} is already under a production-usable policy"
                )
        HoldRebaseApproval.objects.create(rebase=b, slot=slot, approver=actor)
        effective = HoldRebaseActivation.objects.filter(rebase=b).exists()
        return Outcome("hold_rebase", b.code, {"hold": h.code, "slot": slot, "effective": effective})

    return run_command(
        actor,
        "fleet.approve_hold_rebase",
        permission="fleet.rebase_hold",
        payload={"rebase_id": rebase_id, "slot": slot},
        handler=handler,
    )


# ---------------------------------------------------------------- inspection templates (X-1)
def _template(template_id: int) -> InspectionTemplate:
    try:
        return InspectionTemplate.objects.select_related("pattern").get(pk=template_id)
    except InspectionTemplate.DoesNotExist:
        raise Invalid(f"No inspection template {template_id}") from None


def _lock_pattern_and_robots(pattern_id: int, label: str) -> None:
    """pattern U + robots on it U, as decide_pattern: the approval / withdrawal trigger bumps them."""
    lock_guards(update_patterns=[pattern_id], update_robots=_pattern_robot_ids(pattern_id), label=label)


def create_inspection_template(
    actor: Any,
    *,
    pattern_code: str,
    version: str,
    items: list[dict[str, Any]],
    source: str,
    procedure_ref: str = "",
) -> dict[str, Any]:
    """A new template (engineer / engineering_lead). Locks: pattern S. Content is never supplied by the
    platform; an engineering_doc template carries a procedure reference, never the procedure."""

    def handler() -> Outcome:
        if source not in dict(TEMPLATE_SOURCES):
            raise Invalid(f"source must be one of {[k for k, _ in TEMPLATE_SOURCES]}")
        if not version.strip():
            raise Invalid("A template version is required")
        problem = template_items_problem(items)
        if problem:
            raise Invalid(f"Invalid inspection template: {problem}")
        if source == "engineering_doc" and not procedure_ref.strip():
            raise Invalid("An engineering_doc inspection template needs a procedure reference")
        p = _pattern(pattern_code)
        lock_guards(share_patterns=[p.pk], label="fleet.create_inspection_template")
        if InspectionTemplate.objects.filter(pattern=p, version=version).exists():
            raise Conflict(f"Template {version} already exists for {p.code}")
        t = InspectionTemplate.objects.create(
            pattern=p,
            version=version,
            items=items,
            source=source,
            procedure_ref=procedure_ref,
            created_by=actor,
        )
        return Outcome("inspection_template", str(t.pk), {"pattern": p.code, "version": version})

    return run_command(
        actor,
        "fleet.create_inspection_template",
        permission="fleet.manage_inspection_template",
        payload={
            "pattern": pattern_code,
            "version": version,
            "items": items,
            "source": source,
            "procedure_ref": procedure_ref,
        },
        handler=handler,
    )


def approve_inspection_template(actor: Any, *, template_id: int, purpose: str, slot: str) -> dict[str, Any]:
    """One slot for one purpose (pre_departure / periodic). Locks: pattern U + robots U (R1-4 bump)."""

    def handler() -> Outcome:
        if purpose not in dict(TEMPLATE_PURPOSES):
            raise Invalid(f"purpose must be one of {[k for k, _ in TEMPLATE_PURPOSES]}")
        if slot not in SLOT_ROLES:
            raise Invalid(f"slot must be one of {sorted(SLOT_ROLES)}")
        t = _template(template_id)
        _lock_pattern_and_robots(t.pattern_id, "fleet.approve_inspection_template")
        if t.source != "engineering_doc":
            raise Conflict("A synthetic_demo inspection template can never be approved")
        if t.created_by_id is None:
            raise Conflict("A legacy inspection template (no author) can never be approved")
        if template_withdrawn(t):
            raise Conflict("A withdrawn inspection template cannot be approved")
        if not _has_role_now(actor.pk, SLOT_ROLES[slot]):
            raise PermissionDenied(f"Slot {slot} requires role {SLOT_ROLES[slot]}")
        if t.created_by_id == actor.pk:
            raise Conflict("The author of an inspection template cannot approve it")
        if InspectionTemplateApproval.objects.filter(template=t, purpose=purpose, approver=actor).exists():
            raise Conflict("The same person cannot fill two approval slots for one purpose")
        if InspectionTemplateApproval.objects.filter(template=t, purpose=purpose, slot=slot).exists():
            raise Conflict(f"Slot {slot} for {purpose} is already approved")
        InspectionTemplateApproval.objects.create(template=t, purpose=purpose, slot=slot, approver=actor)
        return Outcome(
            "inspection_template",
            str(t.pk),
            {"purpose": purpose, "slot": slot, "state": template_state(t, purpose)},
        )

    return run_command(
        actor,
        "fleet.approve_inspection_template",
        permission="fleet.approve_inspection_template",
        payload={"template_id": template_id, "purpose": purpose, "slot": slot},
        handler=handler,
    )


def withdraw_inspection_template(actor: Any, *, template_id: int, reason: str) -> dict[str, Any]:
    """Withdraw for every purpose. Locks: pattern U + robots U (bump: authorizations relying on it fail)."""

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        t = _template(template_id)
        _lock_pattern_and_robots(t.pattern_id, "fleet.withdraw_inspection_template")
        if template_withdrawn(t):
            raise Conflict("The template is already withdrawn")
        InspectionTemplateWithdrawal.objects.create(template=t, withdrawn_by=actor, reason=reason)
        return Outcome("inspection_template", str(t.pk), {"state": "withdrawn"})

    return run_command(
        actor,
        "fleet.withdraw_inspection_template",
        permission="fleet.withdraw_inspection_template",
        payload={"template_id": template_id, "reason": reason},
        handler=handler,
    )


# ---------------------------------------------------------------- periodic inspection rules (X-3)
def _periodic_rule(rule_id: int) -> PeriodicInspectionRule:
    try:
        return PeriodicInspectionRule.objects.get(pk=rule_id)
    except PeriodicInspectionRule.DoesNotExist:
        raise Invalid(f"No periodic inspection rule {rule_id}") from None


def create_periodic_rule(
    actor: Any,
    *,
    pattern_code: str,
    interval_min: int | None,
    source: str,
    procedure_ref: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Version N+1 of a pattern's periodic interval rule. ``interval_min`` None records "unknown"
    (blocks). The value is the company's (E7 / B-08); the platform never supplies one.
    Locks: pattern U + robots U."""

    def handler() -> Outcome:
        if source not in dict(RULE_SOURCES):
            raise Invalid(f"source must be one of {[k for k, _ in RULE_SOURCES]}")
        if interval_min is not None and (
            not isinstance(interval_min, int)
            or isinstance(interval_min, bool)
            or not 1 <= interval_min <= MAX_INTERVAL_MIN
        ):
            raise Invalid(
                f"interval_min must be empty (unknown) or a whole number from 1 to {MAX_INTERVAL_MIN}"
            )
        if source == "engineering_doc" and not procedure_ref.strip():
            raise Invalid("An engineering_doc periodic inspection rule needs a procedure reference")
        p = _pattern(pattern_code)
        _lock_pattern_and_robots(p.pk, "fleet.create_periodic_rule")
        last = PeriodicInspectionRule.objects.filter(pattern=p).order_by("-version_no").first()
        rule = PeriodicInspectionRule.objects.create(
            pattern=p,
            version_no=(last.version_no if last else 0) + 1,
            interval_min=interval_min,
            source=source,
            procedure_ref=procedure_ref,
            notes=notes,
            created_by=actor,
        )
        return Outcome(
            "periodic_rule", str(rule.pk), {"pattern": p.code, "version": rule.version_no, "status": "draft"}
        )

    return run_command(
        actor,
        "fleet.create_periodic_rule",
        permission="fleet.manage_periodic_rule",
        payload={
            "pattern": pattern_code,
            "interval_min": interval_min,
            "source": source,
            "procedure_ref": procedure_ref,
            "notes": notes,
        },
        handler=handler,
    )


def approve_periodic_rule(actor: Any, *, rule_id: int, slot: str) -> dict[str, Any]:
    def handler() -> Outcome:
        if slot not in SLOT_ROLES:
            raise Invalid(f"slot must be one of {sorted(SLOT_ROLES)}")
        rule = _periodic_rule(rule_id)
        _lock_pattern_and_robots(rule.pattern_id, "fleet.approve_periodic_rule")
        if periodic_rule_withdrawn(rule):
            raise Conflict("A withdrawn periodic inspection rule cannot be approved")
        if not _has_role_now(actor.pk, SLOT_ROLES[slot]):
            raise PermissionDenied(f"Slot {slot} requires role {SLOT_ROLES[slot]}")
        if rule.created_by_id == actor.pk:
            raise Conflict("The author of a periodic inspection rule cannot approve it")
        if PeriodicInspectionRuleApproval.objects.filter(rule=rule, approver=actor).exists():
            raise Conflict("The same person cannot fill two approval slots on one periodic inspection rule")
        if PeriodicInspectionRuleApproval.objects.filter(rule=rule, slot=slot).exists():
            raise Conflict(f"Slot {slot} is already approved")
        PeriodicInspectionRuleApproval.objects.create(rule=rule, slot=slot, approver=actor)
        active = active_periodic_rule(rule.pattern_id)
        return Outcome(
            "periodic_rule",
            str(rule.pk),
            {"slot": slot, "active": active is not None and active.pk == rule.pk},
        )

    return run_command(
        actor,
        "fleet.approve_periodic_rule",
        permission="fleet.approve_periodic_rule",
        payload={"rule_id": rule_id, "slot": slot},
        handler=handler,
    )


def withdraw_periodic_rule(actor: Any, *, rule_id: int, reason: str) -> dict[str, Any]:
    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        rule = _periodic_rule(rule_id)
        _lock_pattern_and_robots(rule.pattern_id, "fleet.withdraw_periodic_rule")
        if periodic_rule_withdrawn(rule):
            raise Conflict("The rule is already withdrawn")
        PeriodicInspectionRuleWithdrawal.objects.create(rule=rule, withdrawn_by=actor, reason=reason)
        return Outcome("periodic_rule", str(rule.pk), {"status": "withdrawn"})

    return run_command(
        actor,
        "fleet.withdraw_periodic_rule",
        permission="fleet.withdraw_periodic_rule",
        payload={"rule_id": rule_id, "reason": reason},
        handler=handler,
    )


# ---------------------------------------------------------------- inspections
def submit_inspection(
    actor: Any,
    *,
    robot_code: str,
    template_version: str,
    answers: dict[str, str],
    performer: str | None,
    verifier: str | None,
    visit_ref: str = "",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = {
        "robot": robot_code,
        "template_version": template_version,
        "answers": answers,
        "performer": performer,
        "verifier": verifier,
        "visit_ref": visit_ref,
    }

    def handler() -> Outcome:
        robot = _robot(robot_code)
        if robot.current_manifest_id is None:
            raise Invalid("Robot has no configuration manifest")
        lock_guards(update_robots=[robot.pk], label="fleet.submit_inspection")
        robot.refresh_from_db()
        manifest = ConfigurationManifest.objects.get(pk=robot.current_manifest_id)
        try:
            template = InspectionTemplate.objects.get(
                pattern_id=manifest.pattern_id, version=template_version
            )
        except InspectionTemplate.DoesNotExist:
            raise Invalid(
                f"No inspection template {template_version} for this robot's configuration"
            ) from None
        item_ids = {i["id"] for i in template.items}
        if set(answers) - item_ids:
            raise Invalid(f"Unknown checklist items: {sorted(set(answers) - item_ids)}")
        if any(v not in ("pass", "fail") for v in answers.values()):
            raise Invalid("Each answer must be 'pass' or 'fail'")

        def signer(username: str | None) -> Any:
            if not username:
                return None
            try:
                u = User.objects.get(username=username)
            except User.DoesNotExist:
                raise Invalid(f"No user {username}") from None
            if not (active_roles(u) & INSPECTION_SIGNER_ROLES):
                raise Invalid(f"{username} is not qualified to sign inspections")
            return u

        perf, ver = signer(performer), signer(verifier)
        robot_fail = [
            i["id"] for i in template.items if i["robot_condition"] and answers.get(i["id"]) == "fail"
        ]
        visit_fail = [
            i["id"] for i in template.items if not i["robot_condition"] and answers.get(i["id"]) == "fail"
        ]
        complete = (item_ids <= set(answers)) and perf is not None and ver is not None and perf != ver
        result = "FAIL" if (robot_fail or visit_fail) else ("PASS" if complete else "INCOMPLETE")
        code = _next_code("fleet_inspection_code_seq", "RC")
        hold = None
        if robot_fail:  # an observed robot-condition failure ALWAYS holds the robot, even if incomplete
            hold = _create_hold(
                actor,
                scope="robot",
                robot=robot,
                pattern=None,
                source_type="inspection",
                source_ref=code,
                reason=f"Inspection {code} failed: {', '.join(robot_fail)}",
            )
        insp = Inspection.objects.create(
            code=code,
            robot=robot,
            manifest=manifest,
            template=template,
            visit_ref=visit_ref,
            answers=answers,
            result=result,
            complete=complete,
            failed_robot_items=robot_fail,
            failed_visit_items=visit_fail,
            performer=perf,
            verifier=ver,
            hold=hold,
            submitted_by=actor,
        )
        return Outcome(
            "inspection",
            insp.code,
            {"result": result, "complete": complete, "hold": hold.code if hold else None},
        )

    return run_command(
        actor,
        "fleet.submit_inspection",
        permission="fleet.submit_inspection",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
        hub=robot_hub(robot_code),
    )


# ---------------------------------------------------------------- history
def robot_history(robot: Robot) -> list[dict[str, Any]]:
    """Everything that happened to a robot, oldest first, each item with the configuration in force."""
    events: list[dict[str, Any]] = []
    for m in robot.manifests.select_related("pattern"):
        events.append(
            {
                "at": m.recorded_at,
                "kind": "manifest",
                "text": f"Configuration recorded: {m.pattern.code}, autonomy {m.autonomy_version} "
                f"({m.get_provenance_display()})",
                "manifest_id": m.pk,
            }
        )
    for h in robot.holds.all():
        events.append(
            {
                "at": h.placed_at,
                "kind": "hold",
                "text": f"Hold {h.code} placed ({h.get_source_type_display()}): {h.reason}",
                "manifest_id": h.manifest_at_placement_id,
            }
        )
        for a in h.approvals.all():
            events.append(
                {
                    "at": a.approved_at,
                    "kind": "approval",
                    "text": f"{h.code} slot {a.slot} approved as {a.role_used}",
                    "manifest_id": a.manifest_id,
                }
            )
        if h.released_at:
            events.append(
                {
                    "at": h.released_at,
                    "kind": "release",
                    "text": f"Hold {h.code} released",
                    "manifest_id": None,
                }
            )
    for i in robot.inspections.all():
        events.append(
            {
                "at": i.submitted_at,
                "kind": "inspection",
                "text": f"Inspection {i.code}: {i.result}{'' if i.complete else ' (incomplete)'}",
                "manifest_id": i.manifest_id,
            }
        )
    for b in robot.base_authorizations.select_related("pattern"):
        events.append(
            {
                "at": b.granted_at,
                "kind": "authorization",
                "text": f"Base home-use authorization for {b.pattern.code}",
                "manifest_id": None,
            }
        )
        if b.revoked_at:
            events.append(
                {
                    "at": b.revoked_at,
                    "kind": "authorization",
                    "text": f"Base authorization for {b.pattern.code} revoked",
                    "manifest_id": None,
                }
            )
    return sorted(events, key=lambda e: e["at"])
