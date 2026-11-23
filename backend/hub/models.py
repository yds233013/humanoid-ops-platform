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
