"""Operational metrics (Slice 5 Engineer C). docs/05_operations_command_center_contract.md section 4 (metrics
table, "Historical custody at vehicle arrival"), R2, R3.

``metrics(start, end, at)`` over the half-open window [start, end), read inside the caller's snapshot. Every
value is derived from immutable rows recorded no later than ``at``; numerator and denominator are always
returned, and a zero denominator gives ``value=None`` / ``display="N/A"``, never 0. All values are
SYNTHETIC-demo measures, not targets.

Statements: one for every metric computed here, plus two for the open operational exception count
(``operations.exceptions.count_operational_exceptions(at)``: thresholds + a count over the inbox's rule
relation, equal to ``len(open_exceptions(at, include_readiness=False).items)``). The readiness evaluator
never runs on this page (EM decision).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import connection

from operations.exceptions import count_operational_exceptions

SYNTHETIC_NOTE = "Synthetic-demo measures, not targets."
CURRENT_FLEET_NOTE = "current fleet denominator: lifecycle history before `at` is not reconstructed"

KIND_COHORT = "historical cohort"
KIND_EVENT = "event count"
KIND_EVENT_ORDER = "event count, historical state by event order"
KIND_UTILIZATION = "event over window, current-fleet denominator"
KIND_CURRENT = "current state at at"

ARRIVAL_CLASSES = ("with_robot", "robot_unaccounted", "robot_at_site", "robot_already_at_hub", "ambiguous")


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    kind: str  # one of the KIND_* labels (R3)
    definition: str
    numerator: float | None
    denominator: float | None  # None for plain counts
    value: float | None  # None = N/A (zero denominator or source unavailable)
    unit: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        if self.value is None:
            return "N/A"
        if self.unit == "ratio":
            return f"{self.value:.1%}"
        if self.unit == "count":
            return str(int(self.value))
        return f"{self.value:.1f} {self.unit}"


@dataclass(frozen=True)
class Metrics:
    start: datetime
    end: datetime
    at: datetime
    window_min: float
    items: list[Metric]
    note: str = SYNTHETIC_NOTE

    def get(self, key: str) -> Metric:
        return next(m for m in self.items if m.key == key)


def _ratio(num: float, den: float, scale: float = 1.0) -> float | None:
    return None if not den else num * scale / den


# One statement. Window [start, end); every event row must exist by `at`.
METRICS_SQL = """
WITH cohort AS (
  SELECT d.id AS departure_id, d.robot_id, d.departed_at, c.recorded_at AS closed_at
  FROM readiness_departureevent d
  LEFT JOIN execution_executionrun x ON x.departure_id = d.id
  LEFT JOIN execution_tripclosure c ON c.run_id = x.id AND c.recorded_at <= %(at)s
  WHERE d.departed_at >= %(start)s AND d.departed_at < %(end)s AND d.departed_at <= %(at)s
),
arrivals AS (
  SELECT CASE
           WHEN t.custody_seq_at_event IS NULL THEN 'ambiguous'
           WHEN ce.id IS NULL THEN 'ambiguous'
           WHEN ce.to_kind = 'vehicle' AND ce.trip_id = t.trip_id THEN 'with_robot'
           WHEN ce.to_kind = 'unaccounted' THEN 'robot_unaccounted'
           WHEN ce.to_kind = 'site' THEN 'robot_at_site'
           WHEN ce.to_kind = 'hub' THEN 'robot_already_at_hub'
           ELSE 'ambiguous'
         END AS cls
  FROM execution_tripevent t
  JOIN execution_executionrun x ON x.id = t.run_id
  LEFT JOIN execution_custodyevent ce ON ce.robot_id = x.robot_id AND ce.seq = t.custody_seq_at_event
  WHERE t.kind = 'arrived_hub' AND t.recorded_at >= %(start)s AND t.recorded_at < %(end)s
    AND t.recorded_at <= %(at)s
),
busy AS (
  -- per in-service robot: union of its reservation intervals (confirmed or departed visits, current
  -- revision, unreleased), clipped to the window
  SELECT g.robot_id, (SELECT coalesce(sum(extract(epoch FROM upper(u) - lower(u))), 0) FROM unnest(g.mr) u)
           AS seconds
  FROM (
    SELECT res.robot_id,
           range_agg(tstzrange(greatest(rs.starts_at, %(start)s), least(rs.ends_at, %(end)s))) AS mr
    FROM planning_reservation rs
    JOIN planning_resource res ON res.id = rs.resource_id
    JOIN fleet_robot rb ON rb.id = res.robot_id AND rb.lifecycle = 'in_service'
    JOIN planning_visit v ON v.current_revision_id = rs.revision_id
    WHERE rs.released_at IS NULL AND rs.starts_at < %(end)s AND rs.ends_at > %(start)s
      AND (v.status = 'confirmed'
           OR EXISTS (SELECT 1 FROM readiness_departureevent d
                      WHERE d.visit_id = v.id AND d.departed_at <= %(at)s))
    GROUP BY res.robot_id
  ) g
)
SELECT
  (SELECT count(*) FROM cohort) AS departures,
  (SELECT count(*) FROM cohort WHERE closed_at IS NOT NULL) AS closed,
  (SELECT count(DISTINCT robot_id) FROM cohort) AS departed_robots,
  (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM closed_at - departed_at))
   FROM cohort WHERE closed_at IS NOT NULL) AS turnaround_median_s,
  (SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM closed_at - departed_at))
   FROM cohort WHERE closed_at IS NOT NULL) AS turnaround_p90_s,
  (SELECT count(*) FROM arrivals WHERE cls = 'with_robot') AS arrivals_with_robot,
  (SELECT count(*) FROM arrivals WHERE cls = 'robot_unaccounted') AS arrivals_robot_unaccounted,
  (SELECT count(*) FROM arrivals WHERE cls = 'robot_at_site') AS arrivals_robot_at_site,
  (SELECT count(*) FROM arrivals WHERE cls = 'robot_already_at_hub') AS arrivals_robot_already_at_hub,
  (SELECT count(*) FROM arrivals WHERE cls = 'ambiguous') AS arrivals_ambiguous,
  (SELECT count(*) FROM execution_incident i
   WHERE i.occurred_at >= %(start)s AND i.occurred_at < %(end)s AND i.reported_at <= %(at)s) AS incidents,
  (SELECT count(*) FROM fleet_robot WHERE lifecycle = 'in_service') AS in_service_robots,
  (SELECT coalesce(sum(seconds), 0) FROM busy) AS busy_seconds,
  (SELECT count(*) FROM operations_incident_queue) AS open_incidents
"""


OPEN_EXCEPTIONS_LABEL = "Open operational exceptions - readiness not evaluated"


def _open_exceptions_metric(at: datetime) -> Metric:
    """EM decision: the metrics page never runs the readiness evaluator. It counts the inbox's exceptions
    WITHOUT the readiness rule and says so in its label; it is never presented as the total. The count comes
    from ``count_operational_exceptions(at)`` (Slice 5 perf, EM instruction 1): a ``SELECT count(*)`` over the
    inbox's own rule relation plus THRESHOLD_NOT_CONFIGURED, equal to
    ``len(open_exceptions(at, include_readiness=False).items)`` (oracle-tested) without building the rows."""
    definition = (
        "exceptions of the exception inbox (contract section 3) at `at`, excluding READINESS_INCOMPLETE_SOON "
        "(the readiness evaluator does not run on this page); not the total"
    )
    n = count_operational_exceptions(at)
    details = {"readiness": "not evaluated", "total": False}
    return Metric(
        "open_exceptions", OPEN_EXCEPTIONS_LABEL, KIND_CURRENT, definition, n, None, n, "count", details
    )


def metrics(start: datetime, end: datetime, at: datetime) -> Metrics:
    """Every section 4 metric over [start, end) as of `at` (see the module docstring)."""
    for name, t in (("start", start), ("end", end), ("at", at)):
        if t.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
    if not start < end:
        raise ValueError("the metrics window needs start < end")

    with connection.cursor() as cur:
        cur.execute(METRICS_SQL, {"start": start, "end": end, "at": at})
        cols = [d.name for d in cur.description]
        r = dict(zip(cols, cur.fetchone(), strict=True))

    window_s = (end - start).total_seconds()
    deps, closed, robots = r["departures"], r["closed"], r["departed_robots"]
    arrivals = {k: int(r[f"arrivals_{k}"]) for k in ARRIVAL_CLASSES}
    arrivals_total = sum(arrivals.values())
    without = arrivals["robot_unaccounted"] + arrivals["robot_at_site"]
    in_service = r["in_service_robots"]
    busy_s = float(r["busy_seconds"])
    capacity_s = in_service * window_s
    med = r["turnaround_median_s"]
    p90 = r["turnaround_p90_s"]

    items = [
        Metric(
            "departure_cohort_completion",
            "Departure cohort completion",
            KIND_COHORT,
            "departures in [start, end) whose run has a TripClosure by `at` / departures in [start, end)",
            closed,
            deps,
            _ratio(closed, deps),
            "ratio",
        ),
        Metric(
            "turnaround_median",
            "Turnaround (median)",
            KIND_COHORT,
            "median of (TripClosure time - departed_at) over the cohort's runs closed by `at` "
            "(continuous percentile)",
            None,
            closed,
            None if med is None else float(med) / 60,
            "min",
        ),
        Metric(
            "turnaround_p90",
            "Turnaround (p90)",
            KIND_COHORT,
            "90th percentile of (TripClosure time - departed_at) over the cohort's runs closed by `at` "
            "(continuous percentile)",
            None,
            closed,
            None if p90 is None else float(p90) / 60,
            "min",
        ),
        Metric(
            "return_without_robot",
            "Return without robot",
            KIND_EVENT_ORDER,
            "arrived_hub events in [start, end) classified by the robot's custody when the vehicle arrived "
            "(custody_seq_at_event): robot_unaccounted + robot_at_site; NULL (legacy) rows are ambiguous",
            without,
            arrivals_total,
            without,
            "count",
            {"by_class": arrivals, "share": _ratio(without, arrivals_total)},
        ),
        Metric(
            "incident_rate_per_10_departures",
            "Incident rate per 10 departures",
            KIND_EVENT,
            "incidents with occurred_at in [start, end) x 10 / departures in [start, end)",
            r["incidents"],
            deps,
            _ratio(r["incidents"], deps, 10.0),
            "per 10 departures",
        ),
        Metric(
            "incident_rate_per_departed_robot",
            "Incident rate per departed robot",
            KIND_EVENT,
            "incidents with occurred_at in [start, end) / robots that departed in [start, end)",
            r["incidents"],
            robots,
            _ratio(r["incidents"], robots),
            "per robot",
        ),
        Metric(
            "scheduled_robot_utilization",
            "Scheduled robot utilization",
            KIND_UTILIZATION,
            "per robot currently in service, union of the reservation intervals of confirmed or departed "
            "visits clipped to [start, end), summed / (robots currently in_service x window minutes); "
            + CURRENT_FLEET_NOTE,
            busy_s / 60,
            capacity_s / 60,
            _ratio(busy_s, capacity_s),
            "ratio",
            {"denominator_label": CURRENT_FLEET_NOTE, "in_service_robots": in_service},
        ),
        Metric(
            "open_incidents",
            "Open incidents",
            KIND_CURRENT,
            "incidents in the incident queue (status not closed) at `at`",
            r["open_incidents"],
            None,
            r["open_incidents"],
            "count",
        ),
        _open_exceptions_metric(at),
    ]
    return Metrics(start=start, end=end, at=at, window_min=window_s / 60, items=items)


__all__ = ["ARRIVAL_CLASSES", "OPEN_EXCEPTIONS_LABEL", "Metric", "Metrics", "metrics"]
