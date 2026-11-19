"""Slice 2: visit planning and resource scheduling. SYNTHETIC data only.

Planning feasibility is NEVER departure permission. A confirmed visit with every resource reserved is still
not cleared to depart; that decision belongs to a later slice. No address, name or contact detail of a
household is stored here: a site is an opaque code, a coarse zone, a timezone and a CRM reference.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

USER = settings.AUTH_USER_MODEL


class Site(models.Model):
    code = models.CharField(max_length=12, unique=True)
    zone = models.CharField(max_length=60)
    timezone = models.CharField(max_length=60)  # IANA name, validated by the command layer
    crm_ref = models.CharField(max_length=60)  # opaque pointer into the CRM; no personal data here
    expected_adults = models.PositiveSmallIntegerField(null=True, blank=True)  # NULL = unknown
    version = models.PositiveIntegerField(default=1)  # guard version, bumped by triggers
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(expected_adults__isnull=True) | Q(expected_adults__gte=1),
                name="site_expected_adults_positive",
            )
        ]

    def __str__(self) -> str:
        return self.code


class SiteAssessment(models.Model):
    DECISIONS = [
        ("eligible", "Eligible"),
        ("eligible_restricted", "Eligible with restrictions"),
        ("not_eligible", "Not eligible"),
    ]
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="assessments")
    decision = models.CharField(max_length=24, choices=DECISIONS)
    restrictions = models.TextField(blank=True)
    excluded_tasks = models.JSONField(default=list)
    valid_until = models.DateField()
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    assessed_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    assessed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.site_id}:{self.decision}"


class AssessmentApproval(models.Model):
    assessment = models.OneToOneField(SiteAssessment, on_delete=models.PROTECT, related_name="approval")
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"approval {self.assessment_id}"


class ConsentRecord(models.Model):
    """A VERIFIED consent for one pseudonymous participant. The signed form lives in the evidence store /
    CRM; here only its reference, version, scope and verification. Withdrawal is a single transition."""

    SCOPES = ["presence", "recording", "remote_view"]
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="consents")
    participant_ref = models.CharField(max_length=20)  # e.g. "P1" - never a name
    form_version = models.CharField(max_length=20)
    scope = models.JSONField()  # subset of SCOPES
    evidence_ref = models.CharField(max_length=60)
    verified_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    verified_at = models.DateTimeField(auto_now_add=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["site", "participant_ref"],
                condition=Q(withdrawn_at__isnull=True),
                name="uniq_active_consent_per_participant",
            )
        ]

    def __str__(self) -> str:
        return f"{self.site_id}:{self.participant_ref}"


class Person(models.Model):
    user = models.OneToOneField(USER, on_delete=models.PROTECT, related_name="person")
    code = models.CharField(max_length=12, unique=True)
    version = models.PositiveIntegerField(default=1)  # guard version, bumped by qualification triggers

    def __str__(self) -> str:
        return self.code


class Qualification(models.Model):
    KINDS = [
        ("field_lead", "Field lead"),
        ("field_operator", "Field operator"),
        ("robot_handler", "Robot handling / loading"),
        # Slice 7 (X-8): signs inspections that readiness relies on. Granted by a safety reviewer only, never
        # to
        # oneself, never backdated (planning/0006 trigger), with a training-record reference in basis_ref.
        ("readiness_inspector", "Readiness inspector"),
    ]
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="qualifications")
    kind = models.CharField(max_length=20, choices=KINDS)
    # NOT NULL after BEFORE triggers: an immediate readiness_inspector grant inserts NULL and the trigger sets
    # it
    # to the same clock_timestamp() as granted_at (contract 07a section 7.2, P1-6).
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField()
    granted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)  # forced to clock_timestamp() by the insert trigger
    revoked_at = models.DateTimeField(null=True, blank=True)
    basis_ref = models.TextField(blank=True, default="", db_default="")  # training record reference only

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__gt=models.F("valid_from")), name="qualification_window"
            ),
            models.CheckConstraint(
                condition=Q(
                    kind__in=["field_lead", "field_operator", "robot_handler", "readiness_inspector"]
                ),
                name="qualification_kind_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.person_id}:{self.kind}"


class Vehicle(models.Model):
    code = models.CharField(max_length=12, unique=True)
    robot_capacity = models.PositiveSmallIntegerField(null=True, blank=True)  # NULL = unknown -> blocks cargo
    restraint_kit = models.CharField(max_length=60, blank=True)  # empty = unknown -> blocks cargo
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(robot_capacity__isnull=True) | Q(robot_capacity__gte=1),
                name="vehicle_capacity_positive",
            )
        ]

    def __str__(self) -> str:
        return self.code


class Resource(models.Model):
    """One row per bookable thing, so reservations have a real foreign key and one exclusion constraint."""

    KINDS = [("robot", "Robot"), ("person", "Person"), ("vehicle", "Vehicle")]
    kind = models.CharField(max_length=10, choices=KINDS)
    robot = models.OneToOneField(
        "fleet.Robot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    person = models.OneToOneField(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    vehicle = models.OneToOneField(Vehicle, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind="robot", robot__isnull=False, person__isnull=True, vehicle__isnull=True)
                    | Q(kind="person", person__isnull=False, robot__isnull=True, vehicle__isnull=True)
                    | Q(kind="vehicle", vehicle__isnull=False, robot__isnull=True, person__isnull=True)
                ),
                name="resource_one_target",
            )
        ]

    def __str__(self) -> str:
        target = self.robot or self.person or self.vehicle
        return f"{self.get_kind_display()} {target.code if target else '?'}"


class Visit(models.Model):
    STATUS = [
        ("requested", "Requested"),
        ("planned", "Planned"),
        ("confirmed", "Confirmed"),
        ("cancelled", "Cancelled"),
    ]
    code = models.CharField(max_length=12, unique=True)
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="visits")
    status = models.CharField(max_length=12, choices=STATUS, default="requested")
    current_revision = models.ForeignKey(
        "VisitRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_reason = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)  # guard version, bumped on every update (trigger)

    def __str__(self) -> str:
        return self.code


class VisitRevision(models.Model):
    """Append-only planning revision. A reschedule or reassignment creates a new revision; history is kept."""

    FEASIBILITY = [("feasible", "Planning feasible"), ("blocked", "Blocked"), ("review", "Requires review")]
    visit = models.ForeignKey(Visit, on_delete=models.PROTECT, related_name="revisions")
    revision_no = models.PositiveIntegerField()
    on_site_start = models.DateTimeField()  # UTC
    on_site_end = models.DateTimeField()  # UTC; [start, end) half-open
    travel_out_min = models.PositiveIntegerField(null=True, blank=True)  # NULL = unknown -> blocker
    travel_back_min = models.PositiveIntegerField(null=True, blank=True)
    tasks = models.JSONField(default=list)
    required_consent_scopes = models.JSONField(default=list)
    robot = models.ForeignKey(
        "fleet.Robot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    lead = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    second = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    vehicle = models.ForeignKey(Vehicle, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    feasibility = models.CharField(max_length=10, choices=FEASIBILITY)
    blockers = models.JSONField(default=list)  # [{"code","message","action"}]
    snapshot = models.JSONField(default=dict)  # eligibility + guard versions used for this decision
    reason = models.TextField(blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["visit", "revision_no"], name="uniq_visit_revision"),
            models.CheckConstraint(
                condition=Q(on_site_end__gt=models.F("on_site_start")), name="revision_window"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.visit_id} r{self.revision_no}"


class VehicleTrip(models.Model):
    revision = models.OneToOneField(VisitRevision, on_delete=models.PROTECT, related_name="trip")
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="trips")
    depart_at = models.DateTimeField()
    return_at = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(return_at__gt=models.F("depart_at")), name="trip_window")
        ]

    def __str__(self) -> str:
        return f"trip {self.pk}"


class TripCargo(models.Model):
    trip = models.ForeignKey(VehicleTrip, on_delete=models.PROTECT, related_name="cargo")
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["trip", "robot"], name="uniq_trip_robot")]

    def __str__(self) -> str:
        return f"{self.trip_id}:{self.robot_id}"


class Reservation(models.Model):
    """Exclusive occupancy of one resource for [start, end). Overlap is rejected BY THE DATABASE
    (exclusion constraint), independent of any application lock."""

    PURPOSES = [
        ("inspection", "Pre-visit inspection"),
        ("loading", "Loading"),
        ("travel_out", "Travel out"),
        ("on_site", "On site"),
        ("travel_back", "Travel back"),
        ("unloading", "Unloading"),
        ("charging", "Charging / turnaround"),
        ("trip", "Vehicle trip"),
        ("field_shift", "Field crew block"),
    ]
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="reservations")
    revision = models.ForeignKey(VisitRevision, on_delete=models.PROTECT, related_name="reservations")
    purpose = models.CharField(max_length=16, choices=PURPOSES)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)
    released_reason = models.CharField(max_length=60, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="reservation_window")
        ]

    def __str__(self) -> str:
        return f"{self.resource_id} {self.purpose}"
