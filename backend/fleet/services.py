"""Fleet commands. Each one: run_command (auth, idempotency, in-transaction permission) -> guard locks in
the global order -> domain checks with clear errors -> writes (the database re-checks the invariants)."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from accounts.permissions import active_roles
from accounts.roles import INSPECTION_SIGNER_ROLES
from fleet.models import (
    BaseAuthorization,
    CapabilityApproval,
    CapabilityProfile,
    ConfigurationManifest,
    EligibilityCheck,
    Hold,
    HoldPolicy,
    HoldPolicyApproval,
    Inspection,
    InspectionTemplate,
    ManifestPattern,
    PatternApproval,
    ReleaseApproval,
    ReleaseApprovalWithdrawal,
    Robot,
)
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, run_command
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

    row = DeploymentSetting.objects.filter(key=key).first()
    return row.value if row else None


def demo_mode() -> bool:
    return setting("demo_mode") == "true"


def current_policy() -> HoldPolicy:
    policy = HoldPolicy.objects.order_by("-version").first()
    if policy is None:
        raise Conflict("No hold policy exists yet; an engineering lead must create one")
    return policy


def policy_status(policy: HoldPolicy) -> str:
    slots = set(policy.approvals.values_list("slot", flat=True))
    return "approved" if slots == {"engineering", "safety"} else "draft"


def pattern_status(pattern: ManifestPattern) -> str:
    last = pattern.approvals.order_by("-id").first()
    return last.status if last else "none"


def approved_profile(pattern: ManifestPattern) -> CapabilityProfile | None:
    for profile in pattern.profiles.order_by("-version_no"):
        if set(profile.approvals.values_list("slot", flat=True)) == {"engineering", "safety"}:
            return profile
    return None


# ---------------------------------------------------------------- eligibility (read model)
@dataclass
class Eligibility:
    eligible_for_planning: bool
    label: str
    reasons: list[str]
    manifest_id: int | None
    pattern_code: str | None


def evaluate_eligibility(robot: Robot) -> Eligibility:
    """Fleet eligibility only: may this robot be CONSIDERED when planning a visit? It never means the
    robot may depart - that needs a visit-specific departure authorization (later slice)."""
    reasons: list[str] = []
    pattern = None
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
        ):
            reasons.append(f"Configuration hold {h.code}: {h.reason}")
    for h in Hold.objects.filter(scope="robot", robot=robot, released_at__isnull=True, blocking=True):
        reasons.append(f"Robot hold {h.code} ({h.get_source_type_display()}): {h.reason}")
    ok = not reasons
    return Eligibility(
        ok,
        ELIGIBLE_LABEL if ok else NOT_ELIGIBLE_LABEL,
        reasons,
        robot.current_manifest_id,
        pattern.code if pattern else None,
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
    )


# ---------------------------------------------------------------- robots and lifecycle
def register_robot(
    actor: Any, *, code: str, serial: str, idempotency_key: str | None = None
) -> dict[str, Any]:
    def handler() -> Outcome:
        if Robot.objects.filter(code=code).exists() or Robot.objects.filter(serial=serial).exists():
            raise Conflict(f"A robot with code {code} or serial {serial} already exists")
        robot = Robot.objects.create(code=code, serial=serial, registered_by=actor)
        return Outcome("robot", robot.code, {"lifecycle": robot.lifecycle})

    return run_command(
        actor,
        "fleet.register_robot",
        permission="fleet.register_robot",
        payload={"code": code, "serial": serial},
        handler=handler,
        idempotency_key=idempotency_key,
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


# ---------------------------------------------------------------- hold policy
def create_hold_policy(actor: Any, *, rules: dict[str, Any]) -> dict[str, Any]:
    def handler() -> Outcome:
        for source, rule in rules.items():
            if source not in dict(Hold.SOURCES):
                raise Invalid(f"Unknown hold source {source}")
            slots = rule.get("slots") or []
            if not slots or any(not s.get("slot") or not s.get("roles") for s in slots):
                raise Invalid(f"Rule for {source} needs slots with names and roles")
            if len({s["slot"] for s in slots}) != len(slots):
                raise Invalid(f"Duplicate slot names in {source}")
        version = (HoldPolicy.objects.order_by("-version").values_list("version", flat=True).first() or 0) + 1
        p = HoldPolicy.objects.create(version=version, rules=rules, created_by=actor)
        return Outcome("hold_policy", str(p.version), {"status": "draft"})

    return run_command(
        actor,
        "fleet.create_hold_policy",
        permission="fleet.manage_hold_policy",
        payload={"rules": rules},
        handler=handler,
    )


def approve_hold_policy(actor: Any, *, version: int, slot: str) -> dict[str, Any]:
    def handler() -> Outcome:
        p = HoldPolicy.objects.get(version=version)
        if HoldPolicyApproval.objects.filter(policy=p, approver=actor).exists():
            raise Conflict("You already approved this policy in another slot")
        HoldPolicyApproval.objects.create(policy=p, slot=slot, approver=actor)
        return Outcome("hold_policy", str(p.version), {"slot": slot, "status": policy_status(p)})

    return run_command(
        actor,
        "fleet.approve_hold_policy",
        permission="fleet.approve_hold_policy",
        payload={"version": version, "slot": slot},
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
    """Placing a hold never depends on a policy being approved (fail closed). Without a release rule the
    hold is created with no release slots and stays unreleasable until a policy provides one."""
    policy = HoldPolicy.objects.order_by("-version").first()
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
            robot_ids = list(Robot.objects.filter(current_manifest__pattern=p).values_list("pk", flat=True))
            lock_guards(update_patterns=[p.pk], update_robots=robot_ids, label="fleet.place_hold")
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
        return Outcome("hold", h.code, {"scope": scope, "target": target})

    return run_command(
        actor,
        "fleet.place_hold",
        permission="fleet.place_hold",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
    )


def hold_release_status(h: Hold) -> dict[str, Any]:
    """Which required slots are filled by valid approvals on the CURRENT configuration, which are stale."""
    current_manifest = h.robot.current_manifest_id if h.robot_id and h.robot else None
    slots = []
    for s in h.required_slots:
        approvals = [a for a in h.approvals.all() if a.slot == s["slot"]]
        valid, stale, withdrawn = None, [], []
        for a in approvals:
            if hasattr(a, "withdrawal"):
                withdrawn.append(a)
            elif h.scope == "robot" and a.manifest_id != current_manifest:
                stale.append(a)
            else:
                valid = a
        slots.append(
            {"slot": s["slot"], "roles": s["roles"], "valid": valid, "stale": stale, "withdrawn": withdrawn}
        )
    rule = h.policy.rules.get(h.source_type, {}) if h.policy else {}
    needs_pass = bool(rule.get("requires_newer_pass_inspection")) and h.scope == "robot"
    has_pass = (not needs_pass) or Inspection.objects.filter(
        robot_id=h.robot_id or 0, result="PASS", submitted_at__gt=h.placed_at, manifest_id=current_manifest
    ).exists()
    missing = [s["slot"] for s in slots if s["valid"] is None]
    if not h.required_slots:
        missing = ["(no release rule in an approved policy)"]
    return {
        "slots": slots,
        "missing": missing,
        "needs_newer_pass": needs_pass and not has_pass,
        "policy_status": policy_status(h.policy) if h.policy else "none",
        "releasable": not missing and has_pass,
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
        if h.scope == "robot":
            assert h.robot_id is not None
            lock_guards(update_robots=[h.robot_id], label="fleet.approve_release")
            manifest_id = Robot.objects.get(pk=h.robot_id).current_manifest_id
        else:
            assert h.pattern_id is not None
            lock_guards(update_patterns=[h.pattern_id], label="fleet.approve_release")
            manifest_id = None
        a = ReleaseApproval.objects.create(
            hold=h,
            slot=slot,
            approver=actor,
            role_used=role_used,
            manifest_id=manifest_id,
            evidence_refs=evidence_refs or [],
            note=note,
        )
        return Outcome("hold", h.code, {"approval_id": a.pk, "slot": slot})

    return run_command(
        actor, "fleet.approve_release", permission="fleet.approve_release", payload=payload, handler=handler
    )


def withdraw_release_approval(actor: Any, *, approval_id: int, reason: str) -> dict[str, Any]:
    def handler() -> Outcome:
        a = ReleaseApproval.objects.select_related("hold").get(pk=approval_id)
        if a.approver_id != actor.pk:
            raise PermissionDenied("Only the approver can withdraw their approval")
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
    def handler() -> Outcome:
        h = _hold(hold_code)
        if h.scope == "robot":
            assert h.robot_id is not None
            lock_guards(update_robots=[h.robot_id], label="fleet.release_hold")
        else:
            assert h.pattern_id is not None
            robot_ids = list(
                Robot.objects.filter(current_manifest__pattern_id=h.pattern_id).values_list("pk", flat=True)
            )
            lock_guards(update_patterns=[h.pattern_id], update_robots=robot_ids, label="fleet.release_hold")
        h = Hold.objects.select_related("robot", "policy").get(pk=h.pk)
        if h.released_at is not None:
            raise Conflict("Hold already released")
        status = hold_release_status(h)
        if status["missing"]:
            raise Conflict(f"Cannot release {h.code}: missing approvals for {', '.join(status['missing'])}")
        if status["needs_newer_pass"]:
            raise Conflict(
                f"Cannot release {h.code}: needs a newer PASS inspection on the current configuration"
            )
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
