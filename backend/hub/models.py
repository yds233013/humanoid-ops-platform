"""Hub work orders (v2 Phase 02, milestone 02A; docs/11_v2_phase02_contract.md sections 4, 8.1, 8.6, 8.8).

A work order is OPERATIONAL ONLY, never safety evidence (8.1): completing, cancelling or reassigning one never
releases a hold, records an inspection, changes custody, readiness or planning eligibility. A task may LINK to
the real workflow (the inspection form, the robot's hold page, the visit's readiness page); only those guarded
domain commands change the facts.

``hub_workorder`` is the one Phase 02 table the runtime role writes directly, through the command layer
(hub.services): SELECT, INSERT and UPDATE; never DELETE or TRUNCATE (migration 0002).

Derived tasks carry a stable ``(source_kind, source_ref)``; a partial unique index allows at most one ACTIVE
(not done, not cancelled) task per source, so repeated or concurrent refreshes never duplicate one. When the
source fact disappears, ``source_cleared_at`` is set by the refresh and the task says "Resolved by the
underlying record"; its status is NOT changed (nobody did the work), a person closes it.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

USER = settings.AUTH_USER_MODEL

TYPES = [
    ("charging", "Charging"),
    ("inspection_due", "Inspection due"),
    ("maintenance_due", "Maintenance due"),
    ("kit_low", "Kits low"),
    ("hold_work", "Robot on hold"),
    ("equipment_failure", "Equipment check failed"),
    ("other", "Other"),
]
STATUSES = [
    ("open", "Open"),
    ("assigned", "Assigned"),
    ("in_progress", "In progress"),
    ("done", "Done"),
    ("cancelled", "Cancelled"),
]
FINAL = ("done", "cancelled")
SOURCES = [
    ("manual", "Created by a person"),
    ("periodic_inspection", "Periodic inspection status"),
    ("hold", "Active hold"),
    ("equipment_check", "Failed equipment check"),
    ("charge_forecast", "Charge forecast"),  # v2 02B: a visit's robot needs charge before departure
    ("maintenance_status", "Maintenance status"),  # v2 02C: maintenance due soon, overdue or unknown
]


class WorkOrder(models.Model):
    type = models.CharField(max_length=20, choices=TYPES)
    title = models.CharField(max_length=160)
    robot = models.ForeignKey(
        "fleet.Robot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    visit = models.ForeignKey(
        "planning.Visit", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    due_at = models.DateTimeField(null=True, blank=True)
    owner = models.ForeignKey(
        "planning.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=12, choices=STATUSES, default="open")
    source_kind = models.CharField(max_length=24, choices=SOURCES, default="manual")
    source_ref = models.CharField(max_length=80, null=True, blank=True)  # noqa: DJ001 - NULL for manual tasks
    source_cleared_at = models.DateTimeField(null=True, blank=True)
    link_url = models.CharField(max_length=200, blank=True, default="")
    note = models.CharField(max_length=300, blank=True, default="")
    close_reason = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)  # bumped by every transition (stale form refusal)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(type__in=[t for t, _ in TYPES]), name="hub_workorder_type_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s for s, _ in STATUSES]), name="hub_workorder_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(source_kind__in=[s for s, _ in SOURCES]), name="hub_workorder_source_valid"
            ),
            models.CheckConstraint(
                condition=(Q(source_kind="manual", source_ref__isnull=True))
                | (~Q(source_kind="manual") & Q(source_ref__isnull=False)),
                name="hub_workorder_source_ref_iff_derived",
            ),
            models.CheckConstraint(
                condition=~Q(status="assigned") & ~Q(status="in_progress") | Q(owner__isnull=False),
                name="hub_workorder_assigned_has_owner",
            ),
            models.UniqueConstraint(
                fields=["source_kind", "source_ref"],
                condition=~Q(status__in=list(FINAL)),
                name="hub_workorder_one_active_per_source",
            ),
        ]
        indexes = [models.Index(fields=["status", "due_at"]), models.Index(fields=["owner", "status"])]

    def __str__(self) -> str:
        return f"WO-{self.pk} {self.title}"

    @property
    def code(self) -> str:
        return f"WO-{self.pk:04d}" if self.pk else "WO-new"

    @property
    def active(self) -> bool:
        return self.status not in FINAL


# ------------------------------------------------ v2 02B: charging (contract 11, 8.3, 8.4, 8.8)
class Charger(models.Model):
    """A charging bay at the hub. Configuration (like a hub), not evidence: written by the guarded command
    hub.charging.create_charger / set_charger_in_service (run_command, audited). The runtime role has SELECT,
    INSERT and UPDATE on it, never DELETE; flipping ``in_service`` cannot book anything, because every
    reservation and session is written by a SECURITY DEFINER function that re-checks it under the charger's
    row lock."""

    code = models.CharField(max_length=12, unique=True)
    name = models.CharField(max_length=80)
    hub = models.ForeignKey(
        "platform_core.Hub", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    in_service = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(USER, null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "hub_charger"
        ordering = ["code"]

    def __str__(self) -> str:
        return self.code


class ChargerReservation(models.Model):
    """A charger booked for a robot for a period. Written ONLY by hub_reserve_charger() and released only by
    hub_release_charger_reservation() (ops_app has SELECT). Exclusion constraints: no two active reservations
    overlap on one charger or on one robot. A reservation is never evidence that charging happened."""

    charger = models.ForeignKey(Charger, on_delete=models.PROTECT, related_name="reservations")
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="+")
    visit = models.ForeignKey(
        "planning.Visit", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    released_reason = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        db_table = "hub_charger_reservation"
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="hub_charger_res_window"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.charger_id}:{self.robot_id} {self.starts_at}-{self.ends_at}"


class ChargingSession(models.Model):
    """A recorded charging session: facts entered by a person (start, end, start %, end %). Written ONLY by
    hub_start_charging() / hub_end_charging(). At most one open session per charger and per robot (exclusion
    on tstzrange(started_at, coalesce(ended_at, 'infinity'))). Never departure battery evidence."""

    charger = models.ForeignKey(Charger, on_delete=models.PROTECT, related_name="sessions")
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="+")
    reservation = models.ForeignKey(
        ChargerReservation, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    started_at = models.DateTimeField()
    # the expected end given when it starts: other robots' bookings are checked against [started_at,
    # planned_until) while it is open; the exclusion constraints treat an open session as lasting forever.
    planned_until = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    start_soc_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    end_soc_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    started_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    ended_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "hub_charging_session"
        constraints = [
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gt=models.F("started_at")),
                name="hub_charging_session_window",
            ),
            models.CheckConstraint(
                condition=(Q(start_soc_pct__isnull=True) | Q(start_soc_pct__lte=100))
                & (Q(end_soc_pct__isnull=True) | Q(end_soc_pct__lte=100)),
                name="hub_charging_session_pct",
            ),
        ]

    def __str__(self) -> str:
        return f"session {self.pk} {self.robot_id}@{self.charger_id}"


SOC_SOURCES = [
    ("battery_check", "Battery check"),
    ("charging_session_end", "End of a charging session"),
    ("manual_reading", "Reading entered by hand"),
]


class RobotSocReading(models.Model):
    """A recorded state of charge: value, when it was observed, where it came from, who recorded it.
    Append-only (trigger) and written ONLY by record_soc_reading() / hub_end_charging(). Battery-check
    readiness evidence is read alongside these rows (source battery_check) and is never copied here."""

    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="+")
    soc_pct = models.PositiveSmallIntegerField()
    observed_at = models.DateTimeField()
    source = models.CharField(max_length=24, choices=SOC_SOURCES)
    session = models.ForeignKey(
        ChargingSession, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField()

    class Meta:
        db_table = "robot_soc_reading"
        constraints = [
            models.CheckConstraint(condition=Q(soc_pct__lte=100), name="robot_soc_reading_pct"),
            models.CheckConstraint(
                condition=Q(source__in=[s for s, _ in SOC_SOURCES]), name="robot_soc_reading_source"
            ),
            models.CheckConstraint(
                condition=Q(observed_at__lte=models.F("recorded_at")), name="robot_soc_reading_not_future"
            ),
        ]
        indexes = [models.Index(fields=["robot", "-observed_at"])]

    def __str__(self) -> str:
        return f"{self.robot_id}: {self.soc_pct}% at {self.observed_at}"
