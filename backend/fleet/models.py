"""Fleet core (Slice 1). Every table here is append-only or changes only through commands; the
database (migration 0002) enforces immutability, single-transition release and approval validity
against the runtime role, so the Python layer is not the only line of defence.

All demo values are SYNTHETIC. Nothing here describes a real robot.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

USER = settings.AUTH_USER_MODEL

# Slice 7 (docs/07a_production_guardrails_contract.md). Sources are descriptive only: no production gate reads
# them as proof of approval; they are read only to REFUSE (synthetic content is never production-usable).
TEMPLATE_SOURCES = [("synthetic_demo", "Synthetic demo"), ("engineering_doc", "Engineering document")]
RULE_SOURCES = TEMPLATE_SOURCES
HOLD_POLICY_SOURCES = [("synthetic_demo", "Synthetic demo"), ("approved_procedure", "Approved procedure")]
APPROVAL_SLOTS = [("engineering", "Engineering lead"), ("safety", "Safety reviewer")]
TEMPLATE_PURPOSES = [("pre_departure", "Pre-departure"), ("periodic", "Periodic")]
MAX_INTERVAL_MIN = 5_256_000  # ten years


def _in(field: str, choices: list[tuple[str, str]]) -> Q:
    return Q(**{f"{field}__in": [k for k, _ in choices]})


class DeploymentSetting(models.Model):
    """Environment switches readable by the runtime role, writable only by the schema owner."""

    key = models.CharField(max_length=60, primary_key=True)
    value = models.CharField(max_length=200)

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


class ManifestPattern(models.Model):
    """The approvable combination: hardware revision + release + firmware set."""

    code = models.CharField(max_length=30, unique=True)
    hardware_rev = models.CharField(max_length=40)
    release = models.CharField(max_length=40)
    firmware_set = models.CharField(max_length=80)
    version = models.PositiveIntegerField(default=1)  # guard version, bumped by triggers
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["hardware_rev", "release", "firmware_set"], name="uniq_pattern_combo"
            )
        ]

    def __str__(self) -> str:
        return self.code


class PatternApproval(models.Model):
    STATUS = [
        ("approved_home", "Approved for home use"),
        ("engineering_only", "Engineering only"),
        ("withdrawn", "Withdrawn"),
    ]
    pattern = models.ForeignKey(ManifestPattern, on_delete=models.PROTECT, related_name="approvals")
    status = models.CharField(max_length=20, choices=STATUS)
    reason = models.TextField(blank=True)
    decided_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.pattern_id}:{self.status}"


class CapabilityProfile(models.Model):
    """Versioned per pattern. NULL means UNKNOWN; unknown fields required for planning block eligibility."""

    REQUIRED_FOR_PLANNING = ["stop_behaviour", "lift_crew", "max_runtime_min", "charge_turnaround_min"]

    pattern = models.ForeignKey(ManifestPattern, on_delete=models.PROTECT, related_name="profiles")
    version_no = models.PositiveIntegerField()
    stop_behaviour = models.TextField(null=True, blank=True)
    lift_crew = models.PositiveSmallIntegerField(null=True, blank=True)
    transport_posture = models.TextField(null=True, blank=True)
    max_runtime_min = models.PositiveIntegerField(null=True, blank=True)
    min_departure_soc_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    charge_turnaround_min = models.PositiveIntegerField(null=True, blank=True)
    # Planning-only physical fact (Slice 2): minutes to load OR unload the robot. Unknown blocks planning.
    load_unload_min = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=30, default="synthetic_demo")  # engineering_doc | synthetic_demo
    published_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pattern", "version_no"], name="uniq_profile_version"),
            *[
                models.CheckConstraint(
                    condition=Q(**{f"{f}__isnull": True}) | Q(**{f"{f}__gte": 1}),
                    name=f"profile_{f}_positive",
                )
                for f in ("lift_crew", "max_runtime_min", "charge_turnaround_min", "load_unload_min")
            ],
        ]

    def __str__(self) -> str:
        return f"{self.pattern_id} v{self.version_no}"

    def unknown_fields(self) -> list[str]:
        return [f for f in self.REQUIRED_FOR_PLANNING if getattr(self, f) in (None, "")]


class CapabilityApproval(models.Model):
    SLOTS = [("engineering", "Engineering lead"), ("safety", "Safety reviewer")]
    profile = models.ForeignKey(CapabilityProfile, on_delete=models.PROTECT, related_name="approvals")
    slot = models.CharField(max_length=20, choices=SLOTS)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["profile", "slot"], name="uniq_profile_slot"),
            models.UniqueConstraint(fields=["profile", "approver"], name="uniq_profile_approver"),
        ]

    def __str__(self) -> str:
        return f"{self.profile_id}:{self.slot}"


class Robot(models.Model):
    LIFECYCLE = [
        ("commissioning", "Commissioning"),
        ("validation", "Engineering validation"),
        ("in_service", "In service"),
        ("retired", "Retired"),
    ]
    code = models.CharField(max_length=10, unique=True)
    serial = models.CharField(max_length=60, unique=True)
    lifecycle = models.CharField(max_length=20, choices=LIFECYCLE, default="commissioning")
    current_manifest = models.ForeignKey(
        "ConfigurationManifest", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    version = models.PositiveIntegerField(default=1)  # guard version, bumped by triggers
    registered_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    registered_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.code


class ConfigurationManifest(models.Model):
    PROVENANCE = [
        ("manual", "Entered manually (not verified against the robot)"),
        ("robot_reported", "Reported by the robot"),
    ]
    robot = models.ForeignKey(Robot, on_delete=models.PROTECT, related_name="manifests")
    pattern = models.ForeignKey(ManifestPattern, on_delete=models.PROTECT, related_name="manifests")
    autonomy_version = models.CharField(max_length=40)
    model_versions = models.JSONField(default=dict)
    safety_config_hash = models.CharField(max_length=64)
    calibration_ref = models.CharField(max_length=60, blank=True)
    provenance = models.CharField(max_length=20, choices=PROVENANCE, default="manual")
    content_hash = models.CharField(max_length=64)
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.robot_id}@{self.content_hash[:8]}"


class BaseAuthorization(models.Model):
    """Engineering's home-use authorization for a robot on a specific pattern."""

    robot = models.ForeignKey(Robot, on_delete=models.PROTECT, related_name="base_authorizations")
    pattern = models.ForeignKey(ManifestPattern, on_delete=models.PROTECT, related_name="+")
    granted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["robot", "pattern"],
                condition=Q(revoked_at__isnull=True),
                name="uniq_active_base_auth",
            )
        ]

    def __str__(self) -> str:
        return f"{self.robot_id}/{self.pattern_id}"


class HoldPolicy(models.Model):
    """Versioned release rules. rules = {source_type: {"slots": [{"slot", "roles": [...]}],
    "distinct_approvers": bool, "independent_of_placer": bool, "requires_newer_pass_inspection": bool}}"""

    version = models.PositiveIntegerField(unique=True)
    rules = models.JSONField()
    # Slice 7 (G-01): approved_procedure needs a procedure_ref, the floor and three approvals (incl. founder).
    source = models.CharField(max_length=30, choices=HOLD_POLICY_SOURCES, default="synthetic_demo")
    procedure_ref = models.TextField(blank=True, default="", db_default="")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=_in("source", HOLD_POLICY_SOURCES), name="hold_policy_source")
        ]

    def __str__(self) -> str:
        return f"policy v{self.version}"


class HoldPolicyApproval(models.Model):
    SLOTS = [("engineering", "Engineering lead"), ("safety", "Safety reviewer"), ("founder", "Founder")]
    policy = models.ForeignKey(HoldPolicy, on_delete=models.PROTECT, related_name="approvals")
    slot = models.CharField(max_length=20, choices=SLOTS)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["policy", "slot"], name="uniq_policy_slot"),
            models.UniqueConstraint(fields=["policy", "approver"], name="uniq_policy_approver"),
        ]

    def __str__(self) -> str:
        return f"{self.policy_id}:{self.slot}"


class HoldPolicyWithdrawal(models.Model):
    """Slice 7 (G-01). Append-only; a withdrawn policy is never active and has no fallback."""

    policy = models.OneToOneField(HoldPolicy, on_delete=models.PROTECT, related_name="withdrawal")
    withdrawn_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    withdrawn_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"withdrawn policy {self.policy_id}"


class HoldPolicyRegistry(models.Model):
    """Slice 7 (G-01, P0-3): the singleton serialization point (id = 1, inserted by fleet/0008). No data.
    Policy create / approve / withdraw lock it FOR UPDATE; placement, release and rebasing lock it FOR SHARE.
    Last in the
    global lock order (platform_core.locks.ORDER). Append-only: row locks only."""

    id = models.PositiveSmallIntegerField(primary_key=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="hold_policy_registry_singleton")]

    def __str__(self) -> str:
        return "hold-policy registry"


class Hold(models.Model):
    SCOPES = [("robot", "Robot"), ("manifest_pattern", "Configuration pattern")]
    SOURCES = [
        ("inspection", "Failed inspection"),
        ("incident", "Incident"),
        ("maintenance", "Maintenance"),
        ("engineering", "Engineering restriction"),
        ("release_withdrawn", "Release withdrawn"),
        ("manual", "Manual"),
    ]
    code = models.CharField(max_length=12, unique=True)
    scope = models.CharField(max_length=20, choices=SCOPES)
    robot = models.ForeignKey(Robot, null=True, blank=True, on_delete=models.PROTECT, related_name="holds")
    pattern = models.ForeignKey(
        ManifestPattern, null=True, blank=True, on_delete=models.PROTECT, related_name="holds"
    )
    blocking = models.BooleanField(default=True)
    source_type = models.CharField(max_length=20, choices=SOURCES)
    source_ref = models.CharField(max_length=40, blank=True)
    reason = models.TextField()
    # The release policy in force when placed (may be a draft, or absent). Placing a hold NEVER depends on
    # policy approval; an absent/unapproved policy only makes the hold unreleasable.
    policy = models.ForeignKey(HoldPolicy, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    required_slots = models.JSONField(default=list)  # copied from the policy at creation; fixed for life
    manifest_at_placement = models.ForeignKey(
        ConfigurationManifest, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    placed_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    placed_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scope="robot", robot__isnull=False, pattern__isnull=True)
                    | Q(scope="manifest_pattern", pattern__isnull=False, robot__isnull=True)
                ),
                name="hold_scope_target",
            ),
        ]

    def __str__(self) -> str:
        return self.code


class ReleaseApproval(models.Model):
    hold = models.ForeignKey(Hold, on_delete=models.PROTECT, related_name="approvals")
    slot = models.CharField(max_length=40)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    role_used = models.CharField(max_length=40)
    manifest = models.ForeignKey(
        ConfigurationManifest, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    evidence_refs = models.JSONField(default=list)
    note = models.TextField(blank=True)
    approved_at = models.DateTimeField(auto_now_add=True)
    # Slice 7: forced by the trigger to the hold's effective rebase; only approvals with that rebase fill
    # slots.
    rebase = models.ForeignKey(
        "HoldRebase", null=True, blank=True, on_delete=models.PROTECT, related_name="release_approvals"
    )

    def __str__(self) -> str:
        return f"{self.hold_id}:{self.slot}"


class ReleaseApprovalWithdrawal(models.Model):
    approval = models.OneToOneField(ReleaseApproval, on_delete=models.PROTECT, related_name="withdrawal")
    withdrawn_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    withdrawn_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"withdrawn {self.approval_id}"


class HoldRebase(models.Model):
    """Slice 7 (G-01, EM decision 11): a named two-person decision to re-base an unreleasable hold onto the
    active production policy. Effective only through a HoldRebaseActivation (inserted by the final-approval
    trigger)."""

    code = models.CharField(max_length=12, unique=True)  # RB-0001, fleet_hold_rebase_code_seq
    hold = models.ForeignKey(Hold, on_delete=models.PROTECT, related_name="rebases")
    from_policy = models.ForeignKey(
        HoldPolicy, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    to_policy = models.ForeignKey(HoldPolicy, on_delete=models.PROTECT, related_name="+")
    based_on_rebase = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    required_slots = models.JSONField(default=list)
    reason = models.TextField()
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return self.code


class HoldRebaseApproval(models.Model):
    rebase = models.ForeignKey(HoldRebase, on_delete=models.PROTECT, related_name="approvals")
    slot = models.CharField(max_length=20, choices=APPROVAL_SLOTS)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rebase", "slot"], name="uniq_rebase_slot"),
            models.UniqueConstraint(fields=["rebase", "approver"], name="uniq_rebase_approver"),
            models.CheckConstraint(condition=_in("slot", APPROVAL_SLOTS), name="rebase_slot_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.rebase_id}:{self.slot}"


class HoldRebaseActivation(models.Model):
    """Inserted ONLY by the final rebase-approval trigger (ops_app has no INSERT). One activation per base."""

    hold = models.ForeignKey(Hold, on_delete=models.PROTECT, related_name="rebase_activations")
    rebase = models.OneToOneField(HoldRebase, on_delete=models.PROTECT, related_name="activation")
    based_on_rebase = models.ForeignKey(
        HoldRebase, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    activated_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["hold", "based_on_rebase"], nulls_distinct=False, name="uniq_rebase_activation_base"
            )
        ]

    def __str__(self) -> str:
        return f"activation {self.rebase_id}"


class InspectionTemplate(models.Model):
    """items = [{"id": "I1", "label": "...", "robot_condition": true}, ...]. SYNTHETIC until engineering
    supplies the real checklist for the pattern."""

    pattern = models.ForeignKey(ManifestPattern, on_delete=models.PROTECT, related_name="templates")
    version = models.CharField(max_length=20)
    items = models.JSONField()
    source = models.CharField(max_length=30, choices=TEMPLATE_SOURCES, default="synthetic_demo")
    # Slice 7 (X-1): NULL only for rows created before Slice 7 (never approvable); required for new rows.
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    procedure_ref = models.TextField(blank=True, default="", db_default="")  # a reference, never content

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pattern", "version"], name="uniq_template_version"),
            models.CheckConstraint(condition=_in("source", TEMPLATE_SOURCES), name="template_source_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.pattern_id}:{self.version}"


class InspectionTemplateApproval(models.Model):
    """Slice 7 (X-1): per purpose, engineering + safety, distinct people, never the author."""

    template = models.ForeignKey(InspectionTemplate, on_delete=models.PROTECT, related_name="approvals")
    purpose = models.CharField(max_length=20, choices=TEMPLATE_PURPOSES)
    slot = models.CharField(max_length=20, choices=APPROVAL_SLOTS)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["template", "purpose", "slot"], name="uniq_template_purpose_slot"
            ),
            models.UniqueConstraint(
                fields=["template", "purpose", "approver"], name="uniq_template_purpose_approver"
            ),
            models.CheckConstraint(
                condition=_in("purpose", TEMPLATE_PURPOSES), name="template_purpose_valid"
            ),
            models.CheckConstraint(condition=_in("slot", APPROVAL_SLOTS), name="template_slot_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.template_id}:{self.purpose}:{self.slot}"


class InspectionTemplateWithdrawal(models.Model):
    template = models.OneToOneField(InspectionTemplate, on_delete=models.PROTECT, related_name="withdrawal")
    withdrawn_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    withdrawn_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"withdrawn template {self.template_id}"


class PeriodicInspectionRule(models.Model):
    """Slice 7 (X-3): per pattern, versioned and approved. interval_min NULL = UNKNOWN (blocks in both
    modes)."""

    pattern = models.ForeignKey(ManifestPattern, on_delete=models.PROTECT, related_name="periodic_rules")
    version_no = models.PositiveIntegerField()  # consecutive per pattern (trigger)
    interval_min = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=30, choices=RULE_SOURCES)
    procedure_ref = models.TextField(blank=True, default="", db_default="")
    notes = models.TextField(blank=True, default="", db_default="")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pattern", "version_no"], name="uniq_periodic_rule_version"),
            models.CheckConstraint(
                condition=Q(interval_min__isnull=True)
                | Q(interval_min__gte=1, interval_min__lte=MAX_INTERVAL_MIN),
                name="periodic_rule_interval_range",
            ),
            models.CheckConstraint(condition=_in("source", RULE_SOURCES), name="periodic_rule_source_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.pattern_id} periodic v{self.version_no}"


class PeriodicInspectionRuleApproval(models.Model):
    rule = models.ForeignKey(PeriodicInspectionRule, on_delete=models.PROTECT, related_name="approvals")
    slot = models.CharField(max_length=20, choices=APPROVAL_SLOTS)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rule", "slot"], name="uniq_periodic_rule_slot"),
            models.UniqueConstraint(fields=["rule", "approver"], name="uniq_periodic_rule_approver"),
            models.CheckConstraint(condition=_in("slot", APPROVAL_SLOTS), name="periodic_rule_slot_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.rule_id}:{self.slot}"


class PeriodicInspectionRuleWithdrawal(models.Model):
    rule = models.OneToOneField(PeriodicInspectionRule, on_delete=models.PROTECT, related_name="withdrawal")
    withdrawn_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    withdrawn_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"withdrawn periodic rule {self.rule_id}"


class Inspection(models.Model):
    RESULTS = [("PASS", "Pass"), ("FAIL", "Fail"), ("INCOMPLETE", "Incomplete")]
    code = models.CharField(max_length=12, unique=True)
    robot = models.ForeignKey(Robot, on_delete=models.PROTECT, related_name="inspections")
    manifest = models.ForeignKey(ConfigurationManifest, on_delete=models.PROTECT, related_name="+")
    template = models.ForeignKey(InspectionTemplate, on_delete=models.PROTECT, related_name="+")
    visit_ref = models.CharField(max_length=20, blank=True)
    answers = models.JSONField()  # {item_id: "pass" | "fail"}; absent = not answered
    result = models.CharField(max_length=12, choices=RESULTS)
    complete = models.BooleanField()
    failed_robot_items = models.JSONField(default=list)
    failed_visit_items = models.JSONField(default=list)
    performer = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    verifier = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    hold = models.ForeignKey(Hold, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    submitted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.code


class EligibilityCheck(models.Model):
    """A recorded, locked evaluation of fleet eligibility (the primitive later slices' departure
    authorization will build on). Eligible for PLANNING only - never departure clearance."""

    robot = models.ForeignKey(Robot, on_delete=models.PROTECT, related_name="eligibility_checks")
    robot_version_observed = models.PositiveIntegerField()
    pattern_version_observed = models.PositiveIntegerField(null=True)
    manifest = models.ForeignKey(ConfigurationManifest, null=True, on_delete=models.PROTECT, related_name="+")
    eligible_for_planning = models.BooleanField()
    reasons = models.JSONField(default=list)
    checked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    checked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.robot_id}@{self.robot_version_observed}"
