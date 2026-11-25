"""v2 02D fleet health (docs/11_v2_phase02_contract.md section 2 "Fleet health", 4 milestone 02D).
SYNTHETIC demo data only. Read-only: called inside the caller's ``operations.snapshot.read_snapshot``
(REPEATABLE READ, READ ONLY); no writes, no locks, no evaluator.

``fleet_health(start, end, at)`` over the half-open window [start, min(end, at)): time after the snapshot is
never counted. ONE SQL statement, whatever the fleet size; the per-robot and fleet figures are summed in
Python from its rows.

DEFINITIONS (every metric carries numerator, denominator, window and a "does not establish" note):

* In-service time (the uptime denominator): per robot, the time inside the window during which its lifecycle
  was ``in_service``, reconstructed from the committed ``fleet.lifecycle.*`` audit events (the only path that
  changes a robot's lifecycle). A robot put in service mid-window counts only from that moment (prorated); a
  robot taken out of service stops counting. A robot that is in service now but has no recorded lifecycle
  event has no reconstructable history: its uptime is N/A and it is listed as such, never assumed.
* Held time: the part of the in-service time covered by at least one BLOCKING hold that applies to the robot,
  each hold active over [placed_at, released_at) (released_at later than ``at`` counts as still open).
  Robot-scoped holds apply to their robot; configuration-pattern holds apply to the robots whose CURRENT
  manifest is on that pattern (configuration history is not reconstructed). Overlapping holds count once.
* Uptime share = (in-service time - held time) / in-service time. Zero in-service time gives N/A.
  Maintenance-due status is NOT subtracted: its history is not recorded, so it cannot be reconstructed.
* Holds by source: holds applying to the robot whose active period overlaps the window, by ``source_type``;
  split into released by ``at`` and still open at ``at``. Fleet totals count each hold once.
* Incidents by event type and severity: incidents with ``occurred_at`` in the window and reported by ``at``;
  the severity is the latest severity decision recorded by ``at``, else "unclassified".
* Repeat failure: a robot with two or more of those incidents of the SAME event type inside the window. Only
  recorded incidents count; different event types are not a repeat.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import connection

SYNTHETIC_NOTE = "Synthetic demo data: fictional robots and events, not measurements of any real robot."
DOES_NOT_ESTABLISH = {
    "uptime": (
        "Does not establish how often a robot fails, how it will behave next, or that it may be used: it "
        "only measures recorded hold time against recorded in-service time. Maintenance-due periods are "
        "not subtracted."
    ),
    "holds": "Does not establish why a hold was needed or that a released hold's cause is gone; counts only.",
    "incidents": (
        "Does not establish a cause or a rate: counts of recorded incident reports only. Unreported events "
        "are not here."
    ),
    "repeat": (
        "Does not establish a common cause or predict a next failure: two recorded incidents of the same "
        "type in the window, nothing more."
    ),
}
DENOMINATOR_TEXT = (
    "in-service time inside the window, from recorded lifecycle events; a robot put in service mid-window "
    "counts only from that moment (prorated)"
)
REPEAT_MIN = 2

HEALTH_SQL = r"""
WITH win AS (SELECT %(start)s::timestamptz AS ws, %(end)s::timestamptz AS we),
trans AS (
  -- every committed lifecycle change (fleet.transition_lifecycle is the only writer of fleet_robot.lifecycle)
  SELECT r.id AS robot_id, a.id AS aid, a.occurred_at, a.result ->> 'to' AS to_state
  FROM platform_core_auditevent a
  JOIN fleet_robot r ON r.code = a.entity_id
  WHERE a.entity_type = 'robot' AND a.outcome = 'committed' AND a.command LIKE 'fleet.lifecycle.%%'
    AND a.occurred_at <= %(at)s
),
seg AS (
  SELECT robot_id, to_state, occurred_at AS s,
         coalesce(lead(occurred_at) OVER (PARTITION BY robot_id ORDER BY occurred_at, aid), 'infinity') AS e
  FROM trans
),
svc AS (
  SELECT seg.robot_id, range_agg(tstzrange(greatest(seg.s, win.ws), least(seg.e, win.we), '[)')) AS mr
  FROM seg CROSS JOIN win
  WHERE seg.to_state = 'in_service' AND seg.s < win.we AND seg.e > win.ws
  GROUP BY seg.robot_id
),
hold_rb AS (
  SELECT h.robot_id, h.id AS hold_id FROM fleet_hold h WHERE h.scope = 'robot' AND h.placed_at <= %(at)s
  UNION ALL
  SELECT rb.id, h.id
  FROM fleet_hold h
  JOIN fleet_configurationmanifest m ON m.pattern_id = h.pattern_id
  JOIN fleet_robot rb ON rb.current_manifest_id = m.id
  WHERE h.scope = 'manifest_pattern' AND h.placed_at <= %(at)s
),
hiv AS (
  SELECT x.robot_id, h.code, h.scope, h.source_type, h.blocking, h.placed_at,
         CASE WHEN h.released_at <= %(at)s THEN h.released_at END AS released_at
  FROM hold_rb x JOIN fleet_hold h ON h.id = x.hold_id
),
hwin AS (
  SELECT hiv.* FROM hiv CROSS JOIN win
  WHERE hiv.placed_at < win.we AND (hiv.released_at IS NULL OR hiv.released_at > win.ws)
),
held AS (
  SELECT hwin.robot_id,
         range_agg(tstzrange(greatest(hwin.placed_at, win.ws),
                             least(coalesce(hwin.released_at, 'infinity'), win.we), '[)')) AS mr
  FROM hwin CROSS JOIN win
  WHERE hwin.blocking
  GROUP BY hwin.robot_id
),
inc AS (
  SELECT i.robot_id, i.code, i.event_type, i.occurred_at,
         coalesce((SELECT sd.level FROM execution_severitydecision sd
                   WHERE sd.incident_id = i.id AND sd.recorded_at <= %(at)s
                   ORDER BY sd.recorded_at DESC, sd.id DESC LIMIT 1), 'unclassified') AS severity
  FROM execution_incident i CROSS JOIN win
  WHERE i.occurred_at >= win.ws AND i.occurred_at < win.we AND i.reported_at <= %(at)s
),
robots AS (
  SELECT r.id, r.code, r.lifecycle FROM fleet_robot r
  WHERE r.lifecycle = 'in_service'
     OR EXISTS (SELECT 1 FROM svc WHERE svc.robot_id = r.id)
     OR EXISTS (SELECT 1 FROM hwin WHERE hwin.robot_id = r.id)
     OR EXISTS (SELECT 1 FROM inc WHERE inc.robot_id = r.id)
)
SELECT r.id, r.code, r.lifecycle,
       EXISTS (SELECT 1 FROM trans t WHERE t.robot_id = r.id) AS history_recorded,
       coalesce((SELECT sum(extract(epoch FROM upper(u) - lower(u))) FROM svc, unnest(svc.mr) u
                 WHERE svc.robot_id = r.id), 0) AS service_s,
       coalesce((SELECT sum(extract(epoch FROM upper(u) - lower(u)))
                 FROM svc JOIN held ON held.robot_id = svc.robot_id, unnest(svc.mr * held.mr) u
                 WHERE svc.robot_id = r.id), 0) AS held_s,
       coalesce((SELECT json_agg(json_build_object('code', h.code, 'scope', h.scope, 'source', h.source_type,
                                                   'blocking', h.blocking, 'placed_at', h.placed_at,
                                                   'released_at', h.released_at) ORDER BY h.placed_at, h.code)
                 FROM hwin h WHERE h.robot_id = r.id), '[]') AS holds,
       coalesce((SELECT json_agg(json_build_object('code', i.code, 'event_type', i.event_type,
                                                   'severity', i.severity, 'occurred_at', i.occurred_at)
                                 ORDER BY i.occurred_at, i.code)
                 FROM inc i WHERE i.robot_id = r.id), '[]') AS incidents
FROM robots r
ORDER BY r.code
"""


@dataclass(frozen=True)
class Share:
    """A ratio with its parts. ``value`` None = N/A (zero denominator or no recorded history)."""

    numerator_s: float
    denominator_s: float
    value: float | None

    @property
    def display(self) -> str:
        return "N/A" if self.value is None else f"{self.value:.1%}"

    @property
    def numerator_h(self) -> float:
        return round(self.numerator_s / 3600, 2)

    @property
    def denominator_h(self) -> float:
        return round(self.denominator_s / 3600, 2)


@dataclass(frozen=True)
class RobotHealth:
    robot_id: int
    code: str
    lifecycle: str
    history_recorded: bool
    service_s: float
    held_s: float
    uptime: Share
    holds: list[dict[str, Any]]
    incidents: list[dict[str, Any]]
    holds_by_source: dict[str, int]
    holds_open: int
    holds_released: int
    incidents_by_type: dict[str, int]
    incidents_by_severity: dict[str, int]
    repeat_failures: dict[str, int]  # event type -> count (>= 2)


@dataclass(frozen=True)
class FleetHealth:
    start: datetime
    end: datetime  # effective end = min(requested end, at)
    requested_end: datetime
    at: datetime
    robots: list[RobotHealth]
    uptime: Share
    holds_by_source: dict[str, int]
    holds_total: int
    holds_open: int
    holds_released: int
    incidents_by_type: dict[str, int]
    incidents_by_severity: dict[str, int]
    incidents_total: int
    repeat_failure_robots: int
    repeat_failures: list[tuple[str, str, int]]  # (robot code, event type, count)
    no_history: list[str]  # robots in service now with no recorded lifecycle event (uptime N/A)
    note: str = SYNTHETIC_NOTE
    does_not_establish: dict[str, str] = field(default_factory=lambda: dict(DOES_NOT_ESTABLISH))
    denominator_text: str = DENOMINATOR_TEXT

    @property
    def window_s(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def end_clipped(self) -> bool:
        return self.end < self.requested_end

    def robot(self, code: str) -> RobotHealth:
        return next(r for r in self.robots if r.code == code)


def share(numerator_s: float, denominator_s: float) -> Share:
    return Share(numerator_s, denominator_s, None if denominator_s <= 0 else numerator_s / denominator_s)


def _rows(raw: Any) -> list[dict[str, Any]]:
    return json.loads(raw) if isinstance(raw, str) else list(raw or [])


def _robot(r: dict[str, Any]) -> RobotHealth:
    holds, incidents = _rows(r["holds"]), _rows(r["incidents"])
    service_s, held_s = float(r["service_s"]), float(r["held_s"])
    by_type = Counter(i["event_type"] for i in incidents)
    uptime = share(service_s - held_s, service_s) if r["history_recorded"] else Share(0.0, 0.0, None)
    return RobotHealth(
        robot_id=r["id"],
        code=r["code"],
        lifecycle=r["lifecycle"],
        history_recorded=bool(r["history_recorded"]),
        service_s=service_s,
        held_s=held_s,
        uptime=uptime,
        holds=holds,
        incidents=incidents,
        holds_by_source=dict(sorted(Counter(h["source"] for h in holds).items())),
        holds_open=sum(1 for h in holds if h["released_at"] is None),
        holds_released=sum(1 for h in holds if h["released_at"] is not None),
        incidents_by_type=dict(sorted(by_type.items())),
        incidents_by_severity=dict(sorted(Counter(i["severity"] for i in incidents).items())),
        repeat_failures={t: n for t, n in sorted(by_type.items()) if n >= REPEAT_MIN},
    )


def fleet_health(start: datetime, end: datetime, at: datetime) -> FleetHealth:
    """Per-robot and fleet health over [start, min(end, at)) as of ``at`` (see the module docstring)."""
    for name, t in (("start", start), ("end", end), ("at", at)):
        if t.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
    eff_end = min(end, at)
    if not start < eff_end:
        raise ValueError("the health window needs start < min(end, as-of time)")

    with connection.cursor() as cur:
        cur.execute(HEALTH_SQL, {"start": start, "end": eff_end, "at": at})
        cols = [d.name for d in cur.description]
        robots = [_robot(dict(zip(cols, row, strict=True))) for row in cur.fetchall()]

    seen: dict[str, dict[str, Any]] = {}
    for rh in robots:
        for h in rh.holds:
            seen.setdefault(h["code"], h)  # a pattern hold applies to several robots: count it once
    by_type: Counter[str] = Counter()
    by_sev: Counter[str] = Counter()
    for rh in robots:
        by_type.update(rh.incidents_by_type)
        by_sev.update(rh.incidents_by_severity)
    counted = [rh for rh in robots if rh.history_recorded]
    num = sum(rh.uptime.numerator_s for rh in counted)
    den = sum(rh.uptime.denominator_s for rh in counted)
    repeats = [(rh.code, t, n) for rh in robots for t, n in rh.repeat_failures.items()]
    return FleetHealth(
        start=start,
        end=eff_end,
        requested_end=end,
        at=at,
        robots=robots,
        uptime=share(num, den),
        holds_by_source=dict(sorted(Counter(h["source"] for h in seen.values()).items())),
        holds_total=len(seen),
        holds_open=sum(1 for h in seen.values() if h["released_at"] is None),
        holds_released=sum(1 for h in seen.values() if h["released_at"] is not None),
        incidents_by_type=dict(sorted(by_type.items())),
        incidents_by_severity=dict(sorted(by_sev.items())),
        incidents_total=sum(by_type.values()),
        repeat_failure_robots=len({c for c, _, _ in repeats}),
        repeat_failures=repeats,
        no_history=[rh.code for rh in robots if not rh.history_recorded and rh.lifecycle == "in_service"],
    )


__all__ = ["HEALTH_SQL", "FleetHealth", "RobotHealth", "Share", "fleet_health", "share"]
