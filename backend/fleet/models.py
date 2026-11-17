"""Fleet core (Slice 1). Every table here is append-only or changes only through commands; the
database (migration 0002) enforces immutability, single-transition release and approval validity
against the runtime role, so the Python layer is not the only line of defence.

All demo values are SYNTHETIC. Nothing here describes a real robot.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

USER = settings.AUTH_USER_MODEL


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
    source = models.CharField(max_length=30, default="synthetic_demo")  # engineering_doc | synthetic_demo
    published_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["pattern", "version_no"], name="uniq_profile_version")]

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
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"policy v{self.version}"


class HoldPolicyApproval(models.Model):
    SLOTS = [("engineering", "Engineering lead"), ("safety", "Safety reviewer")]
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

    def __str__(self) -> str:
        return f"{self.hold_id}:{self.slot}"


class ReleaseApprovalWithdrawal(models.Model):
    approval = models.OneToOneField(ReleaseApproval, on_delete=models.PROTECT, related_name="withdrawal")
    withdrawn_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    withdrawn_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"withdrawn {self.approval_id}"


class InspectionTemplate(models.Model):
    """items = [{"id": "I1", "label": "...", "robot_condition": true}, ...]. SYNTHETIC until engineering
    supplies the real checklist for the pattern."""

    pattern = models.ForeignKey(ManifestPattern, on_delete=models.PROTECT, related_name="templates")
    version = models.CharField(max_length=20)
    items = models.JSONField()
    source = models.CharField(max_length=30, default="synthetic_demo")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["pattern", "version"], name="uniq_template_version")]

    def __str__(self) -> str:
        return f"{self.pattern_id}:{self.version}"


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
