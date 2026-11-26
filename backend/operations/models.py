"""Slice 5 had NO data models (contract 05 section 0.1): the command center derives everything at read time.

v2 02D adds ONE append-only table, the predictor log (docs/11_v2_phase02_contract.md section 2: "Predictive
models: candidate predictors are logged only"). It is observational: nothing in planning, readiness, fleet
or hub reads it, and it is written only by the owner-only SQL function
``operations_log_predictor_signals()`` (migration 0008; the runtime role has SELECT only). SYNTHETIC demo
data only.
"""

from django.conf import settings
from django.db import models


class PredictorObservation(models.Model):
    """Candidate signals recorded for one departure, as they stood at the departure time. Never a decision."""

    BATTERY_KNOWN = "known"
    BATTERY_UNKNOWN = "unknown"

    departure = models.OneToOneField(
        "readiness.DepartureEvent", on_delete=models.PROTECT, related_name="+"
    )  # one observation per departure
    visit = models.ForeignKey("planning.Visit", on_delete=models.PROTECT, related_name="+")
    robot = models.ForeignKey("fleet.Robot", on_delete=models.PROTECT, related_name="+")
    departed_at = models.DateTimeField()
    # battery at start: the latest state-of-charge reading observed AND recorded by the departure,
    # else unknown
    battery_state = models.CharField(max_length=8)
    battery_soc_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    battery_reading = models.ForeignKey(
        "hub.RobotSocReading", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    battery_observed_at = models.DateTimeField(null=True, blank=True)
    battery_source = models.CharField(max_length=24, blank=True)
    # open issues at the departure time
    open_holds = models.PositiveIntegerField()
    open_incidents = models.PositiveIntegerField()
    # maintenance status (fleet_maintenance_status_set at the departure time, current mode)
    maintenance_overdue = models.BooleanField()
    maintenance_states = models.JSONField(default=list)
    source_refs = models.JSONField(default=dict)  # hold codes, incident codes, reading id, rule ids
    observed_at = models.DateTimeField()  # when the row was written (clock_timestamp())

    class Meta:
        db_table = "operations_predictor_observation"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        battery_state="known", battery_soc_pct__isnull=False, battery_reading__isnull=False
                    )
                    | models.Q(
                        battery_state="unknown", battery_soc_pct__isnull=True, battery_reading__isnull=True
                    )
                ),
                name="predictor_obs_battery_state",
            ),
            models.CheckConstraint(
                condition=models.Q(battery_soc_pct__isnull=True) | models.Q(battery_soc_pct__lte=100),
                name="predictor_obs_battery_pct",
            ),
        ]

    def __str__(self) -> str:
        return f"predictor observation for departure {self.departure_id}"


class RegionPause(models.Model):
    """A regional pause or its lift (v2 03B-1, docs/13_v2_phase03_contract.md I-9, 10.G, 13.4 item 2, 13.5).

    Append-only. A ``pause`` row stops new departure authorizations and departures for a hub (zone NULL)
    or for one zone of that hub; a ``lift`` row names the pause it ends (``pause``). Written only by the
    SECURITY DEFINER functions ``operations_pause_region`` (the regional ops lead, or a safety_pause grant
    at the hub) and ``operations_lift_pause`` (the regional ops lead only); the runtime role has SELECT
    only. Which pause covers a visit: ``operations_active_pause(hub, zone)``. Nothing here recalls a robot
    or erases a departure."""

    KINDS = [("pause", "Pause"), ("lift", "Lift")]
    hub = models.ForeignKey("platform_core.Hub", on_delete=models.PROTECT, related_name="+")
    zone = models.ForeignKey(
        "platform_core.Zone", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=8, choices=KINDS)
    pause = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="lifts")
    reason = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField()

    class Meta:
        db_table = "operations_region_pause"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="pause", pause__isnull=True) | models.Q(kind="lift", pause__isnull=False)
                ),
                name="region_pause_kind_link",
            ),
            models.UniqueConstraint(
                fields=["pause"], condition=models.Q(kind="lift"), name="uniq_region_pause_one_lift"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.hub_id}/{self.zone_id or '-'}"
