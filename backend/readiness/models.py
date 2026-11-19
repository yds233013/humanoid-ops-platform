"""Slice 3: deployment readiness and departure authorization. See docs/03_readiness_contract.md.

Owned by Engineer A (models + migrations). SYNTHETIC data only. Nothing here grants real-world clearance.

Every table is append-only. Withdrawals, voids and revocations are separate one-to-one rows. The single
permitted mutation is ReadinessPolicy.lock_version, bumped by a database trigger when an approval or a
withdrawal is inserted. PostgreSQL enforces the invariants (migration 0002), so direct SQL from the runtime
role cannot bypass them.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

USER = settings.AUTH_USER_MODEL

EVIDENCE_KINDS = [
    ("pre_departure_inspection", "Pre-departure inspection"),
    ("equipment_check", "Equipment check"),
    ("battery_check", "Battery check"),
]
POLICY_SLOTS = [("engineering", "Engineering lead"), ("safety", "Safety reviewer")]


class ReadinessPolicy(models.Model):
    """Versioned readiness requirements. Status is derived (see readiness.evidence.policy_status)."""

    SOURCES = [
        ("synthetic_demo", "Synthetic demo (never authorizes in production)"),
        ("approved_procedure", "Approved procedure"),
    ]
    version = models.PositiveIntegerField(unique=True)  # consecutive from 1 (trigger)
    requirements = models.JSONField()  # exact schema validated by a trigger
    source = models.CharField(max_length=30, choices=SOURCES)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    # Guard version: bumped by the database on every approval / withdrawal insert. Otherwise immutable.
    lock_version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(source__in=["synthetic_demo", "approved_procedure"]), name="policy_source_valid"
            )
        ]

    def __str__(self) -> str:
        return f"readiness policy v{self.version} ({self.source})"


class ReadinessPolicyApproval(models.Model):
    policy = models.ForeignKey(ReadinessPolicy, on_delete=models.PROTECT, related_name="approvals")
    slot = models.CharField(max_length=20, choices=POLICY_SLOTS)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["policy", "slot"], name="uniq_readiness_policy_slot"),
            models.UniqueConstraint(fields=["policy", "approver"], name="uniq_readiness_policy_approver"),
            models.CheckConstraint(
                condition=Q(slot__in=["engineering", "safety"]), name="readiness_policy_slot_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"policy {self.policy_id}:{self.slot}"


class ReadinessPolicyWithdrawal(models.Model):
    policy = models.OneToOneField(ReadinessPolicy, on_delete=models.PROTECT, related_name="withdrawal")
    withdrawn_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    withdrawn_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"withdrawn policy {self.policy_id}"


class ReadinessEvidence(models.Model):
    RESULTS = [("pass", "Pass"), ("fail", "Fail")]
    code = models.CharField(max_length=12, unique=True)  # EV-0001, sequence readiness_evidence_code_seq
    visit = models.ForeignKey("planning.Visit", on_delete=models.PROTECT, related_name="readiness_evidence")
    revision = models.ForeignKey("planning.VisitRevision", on_delete=models.PROTECT, related_name="+")
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="+")
    manifest = models.ForeignKey("fleet.ConfigurationManifest", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=30, choices=EVIDENCE_KINDS)
    result = models.CharField(max_length=10, choices=RESULTS)
    inspection = models.ForeignKey(
        "fleet.Inspection", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    details = models.JSONField(default=dict)
    observed_at = models.DateTimeField(db_default=Now())
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=[k for k, _ in EVIDENCE_KINDS]), name="evidence_kind_valid"
            ),
            models.CheckConstraint(condition=Q(result__in=["pass", "fail"]), name="evidence_result_valid"),
            models.CheckConstraint(
                condition=(
                    Q(kind="pre_departure_inspection", inspection__isnull=False)
                    | (~Q(kind="pre_departure_inspection") & Q(inspection__isnull=True))
                ),
                name="evidence_inspection_only_for_inspection_kind",
            ),
        ]
        indexes = [models.Index(fields=["visit", "kind"])]

    def __str__(self) -> str:
        return f"{self.code} {self.kind}:{self.result}"


class ReadinessEvidenceVoid(models.Model):
    evidence = models.OneToOneField(ReadinessEvidence, on_delete=models.PROTECT, related_name="void")
    voided_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    voided_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"void of evidence {self.evidence_id}"


class RecoveryPlan(models.Model):
    """Who responds if the robot needs recovery. Only person codes, never household contact details."""

    visit = models.ForeignKey("planning.Visit", on_delete=models.PROTECT, related_name="recovery_plans")
    revision = models.ForeignKey("planning.VisitRevision", on_delete=models.PROTECT, related_name="+")
    manifest = models.ForeignKey("fleet.ConfigurationManifest", on_delete=models.PROTECT, related_name="+")
    on_call_engineer = models.ForeignKey("planning.Person", on_delete=models.PROTECT, related_name="+")
    escalation_contact = models.ForeignKey("planning.Person", on_delete=models.PROTECT, related_name="+")
    escalation_path = models.TextField()
    required_equipment = models.JSONField(default=list)  # list of non-empty strings
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(on_call_engineer=models.F("escalation_contact")),
                name="recovery_plan_distinct_people",
            )
        ]

    def __str__(self) -> str:
        return f"recovery plan {self.pk} for visit {self.visit_id}"


class DepartureAuthorization(models.Model):
    """A time-limited DECISION bound to exact guard versions. Not a departure (see DepartureEvent)."""

    MODES = [("demo", "Demo (synthetic)"), ("production", "Production")]
    code = models.CharField(max_length=12, unique=True)  # DA-0001, sequence readiness_authorization_code_seq
    visit = models.ForeignKey("planning.Visit", on_delete=models.PROTECT, related_name="authorizations")
    revision = models.ForeignKey("planning.VisitRevision", on_delete=models.PROTECT, related_name="+")
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="+")
    manifest = models.ForeignKey("fleet.ConfigurationManifest", on_delete=models.PROTECT, related_name="+")
    policy = models.ForeignKey(ReadinessPolicy, on_delete=models.PROTECT, related_name="authorizations")
    mode = models.CharField(max_length=12, choices=MODES)
    guard_snapshot = models.JSONField()
    evidence_ids = models.JSONField(default=list)
    recovery_plan = models.ForeignKey(
        RecoveryPlan, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    issued_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    issued_at = models.DateTimeField(db_default=Now())  # forced to clock_timestamp() by the trigger
    expires_at = models.DateTimeField()
    # Slice 7 (X-3, P0-1): set by readiness_issue_authorization, never by the caller. NULL only before
    # Slice 7.
    periodic_inspection = models.ForeignKey(
        "fleet.Inspection", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    planned_unload_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(mode__in=["demo", "production"]), name="authorization_mode_valid"
            ),
            models.CheckConstraint(
                condition=Q(expires_at__gt=models.F("issued_at")), name="authorization_expiry_after_issue"
            ),
        ]

    def __str__(self) -> str:
        return self.code


class AuthorizationRevocation(models.Model):
    authorization = models.OneToOneField(
        DepartureAuthorization, on_delete=models.PROTECT, related_name="revocation"
    )
    revoked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    revoked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"revocation of authorization {self.authorization_id}"


class DepartureEvent(models.Model):
    """Immutable operational fact. One per authorization (unique): single use."""

    authorization = models.OneToOneField(
        DepartureAuthorization, on_delete=models.PROTECT, related_name="departure"
    )
    visit = models.ForeignKey("planning.Visit", on_delete=models.PROTECT, related_name="departures")
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="+")
    departed_at = models.DateTimeField(db_default=Now())  # forced to clock_timestamp() by the trigger
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    guard_snapshot = models.JSONField()

    def __str__(self) -> str:
        return f"departure on authorization {self.authorization_id}"
