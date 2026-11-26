"""Slice 4: field execution and incident response. See docs/04_field_execution_contract.md section 2.

Owned by Engineer A (models + migrations). SYNTHETIC data only. Nothing here moves, stops or operates a robot;
the system records what authorized people report.

Every table is append-only (UPDATE/DELETE/TRUNCATE rejected by triggers). State is derived from the latest
row(s), never stored in an editable column. The single permitted mutation is IncidentPolicy.lock_version,
bumped by the database when an approval or a withdrawal is inserted. PostgreSQL enforces the invariants
(migrations 0002+), so direct SQL from the runtime role cannot bypass them:

- ExecutionRun rows are created ONLY by the AFTER INSERT trigger on readiness_departureevent; the runtime
  role has no INSERT on the table.
- Every `recorded_at` is forced to clock_timestamp() by an insert trigger (Incident.reported_at too).
- Codes come from sequences: execution_run_code_seq (EX-0001), execution_incident_code_seq (IC-0001),
  execution_interruption_code_seq (IN-0001).
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

USER = settings.AUTH_USER_MODEL

CUSTODY_KINDS = [("hub", "Hub"), ("vehicle", "Vehicle"), ("site", "Site"), ("unaccounted", "Unaccounted")]
CUSTODY_BASES = [("administrative", "Administrative"), ("handoff", "Handoff"), ("recovered", "Recovered")]
SOURCES = [
    ("synthetic_demo", "Synthetic demo (usable only in demo mode)"),
    ("approved_procedure", "Approved procedure"),
]
POLICY_SLOTS = [("engineering", "Engineering lead"), ("safety", "Safety reviewer")]
TRIP_EVENT_KINDS = [
    ("arrived_site", "Arrived at site"),
    ("departed_site", "Departed site"),
    ("arrived_hub", "Arrived at hub"),
]
SESSION_EVENT_KINDS = [
    ("start", "Start"),
    ("pause", "Pause"),
    ("resume", "Resume"),
    ("stop", "Stop"),
    ("observation", "Observation"),
]
INCIDENT_EVENT_TYPES = [
    ("fall", "Fall"),
    ("contact", "Contact"),
    ("unexpected_motion", "Unexpected motion"),
    ("stopped_unexpectedly", "Stopped unexpectedly"),
    ("damage", "Damage"),
    ("heat_or_smoke", "Heat or smoke"),
    ("robot_unaccounted", "Robot unaccounted for"),
    ("other", "Other"),
]
KNOWLEDGE_SOURCES = [
    ("witnessed", "Witnessed"),
    ("told_by_crew", "Told by crew"),
    ("found_later", "Found later"),
]
INCIDENT_STATUSES = [
    ("triaged", "Triaged"),
    ("response_in_progress", "Response in progress"),
    ("resolved", "Resolved"),
    ("closed", "Closed"),
]
DISPOSITIONS = [
    ("continue_visit", "Continue visit"),
    ("end_visit_return", "End visit and return"),
    ("robot_left_on_site", "Robot left on site"),
    ("robot_unaccounted", "Robot unaccounted for"),
]


def _keys(choices: list[tuple[str, str]]) -> list[str]:
    return [k for k, _ in choices]


def _in(field: str, choices: list[tuple[str, str]]) -> Q:
    return Q(**{f"{field}__in": _keys(choices)})


class ExecutionRun(models.Model):
    """Opened by the departure trigger (same transaction). Open until a TripClosure exists for its trip."""

    code = models.CharField(max_length=12, unique=True)  # EX-0001, execution_run_code_seq
    departure = models.OneToOneField(
        "readiness.DepartureEvent", on_delete=models.PROTECT, related_name="execution_run"
    )
    visit = models.OneToOneField("planning.Visit", on_delete=models.PROTECT, related_name="execution_run")
    revision = models.ForeignKey("planning.VisitRevision", on_delete=models.PROTECT, related_name="+")
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="execution_runs")
    manifest = models.ForeignKey("fleet.ConfigurationManifest", on_delete=models.PROTECT, related_name="+")
    trip = models.OneToOneField(
        "planning.VehicleTrip", on_delete=models.PROTECT, related_name="execution_run"
    )
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())  # forced to clock_timestamp() by trigger

    def __str__(self) -> str:
        return self.code


class IncidentPolicy(models.Model):
    """Versioned severity scale. Same lifecycle as readiness.ReadinessPolicy (status derived)."""

    version = models.PositiveIntegerField(unique=True)  # consecutive from 1 (trigger)
    rules = (
        models.JSONField()
    )  # {"severity_scale": [...], "escalation_required_from": l, "downgrade_roles": [...]}
    source = models.CharField(max_length=30, choices=SOURCES)
    notes = models.TextField(blank=True)
    lock_version = models.PositiveIntegerField(default=1)  # bumped by approval / withdrawal triggers only
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(condition=_in("source", SOURCES), name="incident_policy_source")
        ]

    def __str__(self) -> str:
        return f"incident policy v{self.version} ({self.source})"


class IncidentPolicyApproval(models.Model):
    policy = models.ForeignKey(IncidentPolicy, on_delete=models.PROTECT, related_name="approvals")
    slot = models.CharField(max_length=20, choices=POLICY_SLOTS)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["policy", "slot"], name="uniq_incident_policy_slot"),
            models.UniqueConstraint(fields=["policy", "approver"], name="uniq_incident_policy_approver"),
            models.CheckConstraint(condition=_in("slot", POLICY_SLOTS), name="incident_policy_slot_valid"),
        ]

    def __str__(self) -> str:
        return f"incident policy {self.policy_id}:{self.slot}"


class IncidentPolicyWithdrawal(models.Model):
    policy = models.OneToOneField(IncidentPolicy, on_delete=models.PROTECT, related_name="withdrawal")
    withdrawn_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    recorded_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"withdrawn incident policy {self.policy_id}"


class Incident(models.Model):
    """Every incident holds the robot in the same transaction (hold NOT NULL, proven by the insert trigger)"""

    code = models.CharField(max_length=12, unique=True)  # IC-0001, execution_incident_code_seq
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="incidents")
    manifest = models.ForeignKey(
        "fleet.ConfigurationManifest", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # must equal the robot's current manifest at insert
    run = models.ForeignKey(
        ExecutionRun, null=True, blank=True, on_delete=models.PROTECT, related_name="incidents"
    )  # must equal the robot's open run (NULL when none)
    visit = models.ForeignKey(
        "planning.Visit", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # = run.visit
    event_type = models.CharField(max_length=24, choices=INCIDENT_EVENT_TYPES)
    description = models.TextField()
    occurred_at = models.DateTimeField()
    reported_at = models.DateTimeField(db_default=Now())  # forced to clock_timestamp() by trigger
    knowledge_source = models.CharField(max_length=16, choices=KNOWLEDGE_SOURCES)
    hold = models.OneToOneField("fleet.Hold", on_delete=models.PROTECT, related_name="incident")
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=_in("event_type", INCIDENT_EVENT_TYPES), name="incident_event_type_valid"
            ),
            models.CheckConstraint(
                condition=_in("knowledge_source", KNOWLEDGE_SOURCES), name="incident_knowledge_source_valid"
            ),
            models.CheckConstraint(
                condition=Q(occurred_at__lte=models.F("reported_at")), name="incident_occurred_before_report"
            ),
            models.CheckConstraint(
                condition=Q(run__isnull=True, visit__isnull=True) | Q(run__isnull=False, visit__isnull=False),
                name="incident_visit_with_run",
            ),
        ]

    def __str__(self) -> str:
        return self.code


class CustodyEvent(models.Model):
    """THE custody history of a robot (contract D1). Sequence per robot, from = previous to."""

    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="custody_events")
    seq = models.PositiveIntegerField()  # must be previous + 1 (trigger)
    from_kind = models.CharField(max_length=12, choices=CUSTODY_KINDS)
    to_kind = models.CharField(max_length=12, choices=CUSTODY_KINDS)
    run = models.ForeignKey(
        ExecutionRun, null=True, blank=True, on_delete=models.PROTECT, related_name="custody_events"
    )  # the robot's open run; NULL only for a no-run unaccounted report or a no-run recovery to hub (0003)
    trip = models.ForeignKey(
        "planning.VehicleTrip", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # = run.trip whenever run is set
    visit = models.ForeignKey(
        "planning.Visit", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # = run.visit whenever run is set
    custodian = models.ForeignKey(
        "planning.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # responsible after the move; NULL only when to_kind = unaccounted
    received_by = models.ForeignKey(
        "planning.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    basis = models.CharField(max_length=16, choices=CUSTODY_BASES)
    incident = models.ForeignKey(
        Incident, null=True, blank=True, on_delete=models.PROTECT, related_name="custody_events"
    )
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())
    # v2 03A-2 (contract 11.4): the hub at the hub end of the move (to_kind or from_kind = hub), set by the
    # database (trigger execution_custody_hub, never the caller): the run's visit hub, or for a recovery
    # without a run the robot's home hub. NULL for a move with no hub end.
    hub = models.ForeignKey(
        "platform_core.Hub", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(from_kind="hub") | Q(to_kind="hub") | Q(hub__isnull=True),
                name="custody_hub_only_at_hub_end",
            ),
            models.UniqueConstraint(fields=["robot", "seq"], name="uniq_custody_robot_seq"),
            models.CheckConstraint(condition=_in("from_kind", CUSTODY_KINDS), name="custody_from_valid"),
            models.CheckConstraint(condition=_in("to_kind", CUSTODY_KINDS), name="custody_to_valid"),
            models.CheckConstraint(condition=_in("basis", CUSTODY_BASES), name="custody_basis_valid"),
            models.CheckConstraint(condition=Q(seq__gte=1), name="custody_seq_positive"),
            models.CheckConstraint(condition=~Q(from_kind=models.F("to_kind")), name="custody_moves"),
            models.CheckConstraint(
                condition=(
                    ((Q(to_kind="unaccounted") | Q(from_kind="unaccounted")) & Q(incident__isnull=False))
                    | (~Q(to_kind="unaccounted") & ~Q(from_kind="unaccounted") & Q(incident__isnull=True))
                ),
                name="custody_incident_iff_unaccounted",
            ),
            models.CheckConstraint(
                condition=(
                    Q(run__isnull=False)
                    | Q(to_kind="unaccounted")
                    | Q(from_kind="unaccounted", to_kind="hub", basis="recovered")
                ),
                name="custody_run_required",
            ),
        ]

    def __str__(self) -> str:
        return f"custody {self.robot_id}#{self.seq} {self.from_kind}->{self.to_kind}"


class CustodyVerification(models.Model):
    """Crew verification of an administrative assignment (turns it into a verified handoff)."""

    custody_event = models.OneToOneField(CustodyEvent, on_delete=models.PROTECT, related_name="verification")
    verified_by = models.ForeignKey("planning.Person", on_delete=models.PROTECT, related_name="+")
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"verification of custody {self.custody_event_id}"


class TripEvent(models.Model):
    """Physical trip progress: arrived_site (1) -> departed_site (2) -> arrived_hub (3)."""

    trip = models.ForeignKey("planning.VehicleTrip", on_delete=models.PROTECT, related_name="trip_events")
    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="trip_events")
    seq = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=TRIP_EVENT_KINDS)
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["trip", "kind"], name="uniq_trip_event_kind"),
            models.UniqueConstraint(fields=["trip", "seq"], name="uniq_trip_event_seq"),
            models.CheckConstraint(condition=_in("kind", TRIP_EVENT_KINDS), name="trip_event_kind_valid"),
            models.CheckConstraint(
                condition=Q(kind="arrived_site", seq=1)
                | Q(kind="departed_site", seq=2)
                | Q(kind="arrived_hub", seq=3),
                name="trip_event_order",
            ),
        ]

    def __str__(self) -> str:
        return f"trip {self.trip_id} {self.kind}"


class SiteCheckTemplate(models.Model):
    """Immutable checklist. A synthetic_demo template is usable only while demo_mode is on."""

    version = models.CharField(max_length=20, unique=True)
    items = models.JSONField()  # [{"id": "...", "label": "..."}], non-empty, distinct ids
    source = models.CharField(max_length=30, choices=SOURCES)
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(condition=_in("source", SOURCES), name="site_check_template_source")
        ]

    def __str__(self) -> str:
        return f"site check template {self.version}"


class SiteCheck(models.Model):
    """consent_count and result are recomputed by the insert trigger; a mismatch is refused."""

    RESULTS = [("go", "Go"), ("no_go", "No-Go")]
    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="site_checks")
    template = models.ForeignKey(SiteCheckTemplate, on_delete=models.PROTECT, related_name="+")
    adults_present = models.PositiveSmallIntegerField()
    conditions = models.JSONField()  # {item_id: "ok" | "not_ok"}, keys exactly the template's items
    consent_count = models.PositiveSmallIntegerField()
    result = models.CharField(max_length=8, choices=RESULTS)
    verifier = models.ForeignKey("planning.Person", on_delete=models.PROTECT, related_name="+")
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(result__in=["go", "no_go"]), name="site_check_result_valid"),
        ]

    def __str__(self) -> str:
        return f"site check {self.pk} ({self.result})"


class Interruption(models.Model):
    """Several may be open; each closes only by its own InterruptionResolution."""

    SOURCES = [("incident", "Incident"), ("operator", "Operator")]
    code = models.CharField(max_length=12, unique=True)  # IN-0001, execution_interruption_code_seq
    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="interruptions")
    source = models.CharField(max_length=12, choices=SOURCES)
    incident = models.OneToOneField(
        Incident, null=True, blank=True, on_delete=models.PROTECT, related_name="interruption"
    )
    reason = models.TextField()
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(source="incident", incident__isnull=False)
                | Q(source="operator", incident__isnull=True),
                name="interruption_source_incident",
            ),
        ]

    def __str__(self) -> str:
        return self.code


class InterruptionResolution(models.Model):
    interruption = models.OneToOneField(Interruption, on_delete=models.PROTECT, related_name="resolution")
    note = models.TextField()
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"resolution of interruption {self.interruption_id}"


class SessionEvent(models.Model):
    """Session state machine: not_started -> active <-> paused -> stopped (section 3)."""

    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="session_events")
    seq = models.PositiveIntegerField()  # previous + 1 within the run (trigger)
    kind = models.CharField(max_length=12, choices=SESSION_EVENT_KINDS)
    site_check = models.ForeignKey(
        SiteCheck, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # start / resume only (required there)
    interruption = models.ForeignKey(
        Interruption, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # pause only (incident-driven pause)
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "seq"], name="uniq_session_run_seq"),
            models.CheckConstraint(condition=_in("kind", SESSION_EVENT_KINDS), name="session_kind_valid"),
            models.CheckConstraint(
                condition=Q(kind__in=["start", "resume"], site_check__isnull=False)
                | (~Q(kind__in=["start", "resume"]) & Q(site_check__isnull=True)),
                name="session_site_check_for_start_resume",
            ),
            models.CheckConstraint(
                condition=Q(kind="pause") | Q(interruption__isnull=True), name="session_interruption_on_pause"
            ),
        ]

    def __str__(self) -> str:
        return f"session {self.run_id}#{self.seq} {self.kind}"


class FaultObservation(models.Model):
    STATUSES = [("suspected", "Suspected"), ("confirmed", "Confirmed")]
    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="fault_observations")
    component = models.CharField(max_length=80)
    status = models.CharField(max_length=12, choices=STATUSES)
    evidence_ref = models.CharField(max_length=80, blank=True)
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["suspected", "confirmed"]), name="fault_observation_status_valid"
            )
        ]

    def __str__(self) -> str:
        return f"fault {self.component} ({self.status})"


class SeverityDecision(models.Model):
    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="severity_decisions")
    level = models.CharField(max_length=40)
    policy = models.ForeignKey(IncidentPolicy, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"severity {self.level} for incident {self.incident_id}"


class IncidentStatusChange(models.Model):
    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="status_changes")
    to_status = models.CharField(max_length=24, choices=INCIDENT_STATUSES)
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["incident", "to_status"], name="uniq_incident_status_step"),
            models.CheckConstraint(
                condition=_in("to_status", INCIDENT_STATUSES), name="incident_status_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"incident {self.incident_id} -> {self.to_status}"


class IncidentEscalation(models.Model):
    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="escalations")
    to_role = models.CharField(max_length=40)
    to_person = models.ForeignKey(
        "planning.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    requested_action = models.TextField()
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"escalation of incident {self.incident_id} to {self.to_role}"


class EscalationAck(models.Model):
    escalation = models.OneToOneField(IncidentEscalation, on_delete=models.PROTECT, related_name="ack")
    acknowledged_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"ack of escalation {self.escalation_id}"


class RecoveryDisposition(models.Model):
    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="dispositions")
    decision = models.CharField(max_length=24, choices=DISPOSITIONS)
    reason = models.TextField()
    incident = models.ForeignKey(Incident, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=_in("decision", DISPOSITIONS), name="disposition_decision_valid"
            ),
            models.CheckConstraint(
                condition=~Q(decision__in=["robot_left_on_site", "robot_unaccounted"])
                | Q(incident__isnull=False),
                name="disposition_incident_required",
            ),
        ]

    def __str__(self) -> str:
        return f"disposition {self.decision} for run {self.run_id}"


class ReturnReconciliation(models.Model):
    OUTCOMES = [("reconciled", "Reconciled"), ("unresolved", "Unresolved")]
    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="reconciliations")
    hub_receipt = models.ForeignKey(
        CustodyEvent, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # the custody event -> hub; required for 'reconciled'
    cargo_matches = models.BooleanField()  # recomputed by the trigger; a mismatch is refused
    post_visit_inspection = models.ForeignKey(
        "fleet.Inspection", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    discrepancies = models.JSONField(default=list)  # list of non-empty strings
    outcome = models.CharField(max_length=12, choices=OUTCOMES)
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(outcome__in=["reconciled", "unresolved"]), name="reconciliation_outcome_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"reconciliation {self.pk} ({self.outcome})"


class TripClosure(models.Model):
    """Closes the run. Needs trip arrived_hub and the run's latest reconciliation 'reconciled'."""

    trip = models.OneToOneField("planning.VehicleTrip", on_delete=models.PROTECT, related_name="closure")
    run = models.OneToOneField(ExecutionRun, on_delete=models.PROTECT, related_name="closure")
    reconciliation = models.OneToOneField(ReturnReconciliation, on_delete=models.PROTECT, related_name="+")
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"closure of trip {self.trip_id}"


# ---------------------------------------------------------------- Slice 7 (X-4): escalation deadline sets
DEADLINE_SOURCES = [
    ("synthetic_demo", "Synthetic demo (usable only in demo mode)"),
    ("owner_approved", "Approved by the owners (founder and safety)"),
]
DEADLINE_SLOTS = [("safety", "Safety reviewer"), ("founder", "Founder")]


class EscalationDeadlineSet(models.Model):
    """Owner-approved escalation deadlines for ONE incident-policy version (contract 07a section 6).
    deadlines = {level | "unclassified": [{"to_role": role, "within_min": 1..5256000}, ...]}; an empty list
    is a stated "no
    clocked notification at this level". Append-only; a change is a new version_no (consecutive per policy).
    The platform never supplies the values: it records them and their approvers."""

    incident_policy = models.ForeignKey(
        IncidentPolicy, on_delete=models.PROTECT, related_name="deadline_sets"
    )
    version_no = models.PositiveIntegerField()
    deadlines = models.JSONField()
    source = models.CharField(max_length=30, choices=DEADLINE_SOURCES)
    notes = models.TextField(blank=True, default="", db_default="")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["incident_policy", "version_no"], name="uniq_deadline_set_version"
            ),
            models.CheckConstraint(
                condition=_in("source", DEADLINE_SOURCES), name="deadline_set_source_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"deadline set {self.incident_policy_id} v{self.version_no}"


class EscalationDeadlineSetApproval(models.Model):
    deadline_set = models.ForeignKey(
        EscalationDeadlineSet, on_delete=models.PROTECT, related_name="approvals"
    )
    slot = models.CharField(max_length=20, choices=DEADLINE_SLOTS)
    approver = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["deadline_set", "slot"], name="uniq_deadline_set_slot"),
            models.UniqueConstraint(fields=["deadline_set", "approver"], name="uniq_deadline_set_approver"),
            models.CheckConstraint(condition=_in("slot", DEADLINE_SLOTS), name="deadline_set_slot_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.deadline_set_id}:{self.slot}"


class EscalationDeadlineSetWithdrawal(models.Model):
    deadline_set = models.OneToOneField(
        EscalationDeadlineSet, on_delete=models.PROTECT, related_name="withdrawal"
    )
    withdrawn_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    withdrawn_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"withdrawn deadline set {self.deadline_set_id}"


# ------------------------------------------------------------------------------------------------------------
# v2 03C-1: the field-command ledger (docs/13_v2_phase03_contract.md 14.3, 14.5, 14.8; field_ledger.py).
# All three tables are append-only, ops_app has SELECT only, and they are written only by the SECURITY
# DEFINER functions of execution/0009 (execution_field_record, _abandon and _report_device).

FIELD_CHANNELS = [("ordinary", "Ordinary"), ("protective", "Protective")]
FIELD_OUTCOMES = [("confirmed", "Confirmed"), ("rejected", "Rejected"), ("abandoned", "Abandoned")]
FIELD_TIME_FLAGS = [
    ("ok", "Phone time within bounds"),
    ("missing", "No phone time"),
    ("out_of_range", "Phone clock out of range"),
    ("", "Not applicable (abandonment)"),
]


class FieldCommand(models.Model):
    """One durable decision about one field command (or an abandonment barrier).

    ``key`` and ``identity_digest`` are the command's immutable identity (14.8 item 2). ``sent_seq`` is the
    sequence the device sent; ``seq`` is the sequence this row DECIDES (null when it decides none: protective
    commands and rejections that do not take a sequence). Unique (run, generation, seq) where seq is set, so
    one sequence of one generation is decided once. An ``abandoned`` row is the barrier of its generation (one
    per generation) and opens the next generation (FieldGeneration)."""

    key = models.UUIDField(unique=True)
    # NULL only on an abandonment barrier (it has no device identity)
    identity_digest = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001
    actor = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    device_id = models.CharField(max_length=64, blank=True)
    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="field_commands")
    channel = models.CharField(max_length=12, choices=FIELD_CHANNELS)
    generation = models.PositiveIntegerField(null=True, blank=True)
    seq = models.PositiveIntegerField(null=True, blank=True)
    sent_seq = models.PositiveIntegerField(null=True, blank=True)
    command = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    device_observed_at = models.DateTimeField(null=True, blank=True)
    device_time_flag = models.CharField(max_length=16, choices=FIELD_TIME_FLAGS, blank=True)
    causal = models.JSONField(null=True, blank=True)
    received_at = models.DateTimeField()
    decided_at = models.DateTimeField()
    outcome = models.CharField(max_length=10, choices=FIELD_OUTCOMES)
    reason_code = models.CharField(max_length=40, blank=True)
    message = models.TextField(blank=True)
    result = models.JSONField(default=dict)

    class Meta:
        db_table = "execution_field_command"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "generation", "seq"],
                condition=Q(seq__isnull=False),
                name="uniq_field_command_run_generation_seq",
            ),
            models.UniqueConstraint(
                fields=["run", "generation"],
                condition=Q(outcome="abandoned"),
                name="uniq_field_command_one_barrier_per_generation",
            ),
            models.CheckConstraint(condition=_in("channel", FIELD_CHANNELS), name="field_command_channel"),
            models.CheckConstraint(condition=_in("outcome", FIELD_OUTCOMES), name="field_command_outcome"),
            models.CheckConstraint(
                condition=_in("device_time_flag", FIELD_TIME_FLAGS), name="field_command_time_flag"
            ),
            # protective: no sequence; ordinary: a generation and a sent sequence, both from 1
            models.CheckConstraint(
                condition=(
                    Q(channel="protective", seq__isnull=True, sent_seq__isnull=True)
                    | Q(channel="ordinary", generation__gte=1, sent_seq__gte=1)
                ),
                name="field_command_channel_sequence",
            ),
            models.CheckConstraint(
                condition=Q(seq__isnull=True) | Q(seq=models.F("sent_seq")), name="field_command_seq_is_sent"
            ),
            # an ordinary confirmation always decides its sequence; an abandonment is a barrier at its seq
            models.CheckConstraint(
                condition=~Q(outcome="confirmed", channel="ordinary") | Q(seq__isnull=False),
                name="field_command_confirmed_decides_seq",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        outcome="abandoned",
                        channel="ordinary",
                        seq__isnull=False,
                        identity_digest__isnull=True,
                    )
                    | (~Q(outcome="abandoned") & Q(identity_digest__regex=r"^[0-9a-f]{64}$"))
                ),
                name="field_command_identity",
            ),
            models.CheckConstraint(
                condition=Q(outcome="confirmed", reason_code="")
                | (~Q(outcome="confirmed") & ~Q(reason_code="")),
                name="field_command_reason_code",
            ),
            models.CheckConstraint(
                condition=Q(decided_at__gte=models.F("received_at")),
                name="field_command_decided_after_received",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key} {self.outcome}"


class FieldGeneration(models.Model):
    """Generation g >= 2 of a run's ordinary stream, opened by the abandonment barrier of generation g - 1.
    Generation 1 is implicit (no row). The current generation of a run is the highest row, else 1."""

    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="+")
    generation = models.PositiveIntegerField()
    barrier = models.OneToOneField(FieldCommand, on_delete=models.PROTECT, related_name="+")
    opened_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    opened_at = models.DateTimeField()

    class Meta:
        db_table = "execution_field_generation"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "generation"], name="uniq_field_generation_run_generation"
            ),
            models.CheckConstraint(condition=Q(generation__gte=2), name="field_generation_from_two"),
        ]

    def __str__(self) -> str:
        return f"run {self.run_id} generation {self.generation}"


class FieldDeviceReport(models.Model):
    """The unsent-step count a device last reported for a run (10.F: "last reported unsent count"). The newest
    row wins."""

    device_id = models.CharField(max_length=64)
    run = models.ForeignKey(ExecutionRun, on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    unsent_count = models.PositiveIntegerField()
    reported_at = models.DateTimeField()

    class Meta:
        db_table = "execution_field_device_report"
        ordering = ["id"]
        indexes = [models.Index(fields=["run", "reported_at"], name="field_device_report_run_idx")]

    def __str__(self) -> str:
        return f"{self.device_id} run {self.run_id}: {self.unsent_count}"


class FieldSyncLock(models.Model):
    """The ledger-only serialization point of one run's field claims, abandonments and device reports
    (execution/0010, EM ruling on 03C). One row per run, created on demand by execution_field_sync_lock and
    only ever locked FOR UPDATE. ``run_id`` is deliberately NOT a foreign key: a foreign-key check would take
    KEY SHARE on the run row, which online commands lock FOR UPDATE after their guard rows
    (docs/evidence/03a_lock_graph.md section 5)."""

    run_id = models.BigIntegerField(primary_key=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "execution_field_sync_lock"
        ordering = ["run_id"]

    def __str__(self) -> str:
        return f"field sync lock of run {self.run_id}"
