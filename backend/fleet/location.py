"""v2 03A-2: where a robot is (docs/13_v2_phase03_contract.md 11.4, correction 10.A).

The single definition lives in SQL (fleet migration 0015: ``fleet_robot_location`` and
``fleet_location_problem``); Python only calls it, so pages, the planner and the database gates can never
disagree.

Kinds: hub (with its hub), vehicle, site, transfer_in_transit, unaccounted, unknown. Only a verified hub
location makes a robot available, and only at that hub. The HOME hub is ownership, not location.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection

KIND_LABELS = {
    "hub": "At hub",
    "vehicle": "In a vehicle",
    "site": "At a site",
    "transfer_in_transit": "On its way to another hub",
    "unaccounted": "Unaccounted for",
    "unknown": "Unknown",
}


AT_RISK = "At risk: robot away until verified back at hub"


@dataclass(frozen=True)
class Location:
    kind: str
    hub_id: int | None
    ref: str
    verified: bool
    problem: str | None  # PHYSICAL dispatch: None = a verified hub (authorization and departure)
    # v2 03A-2 section 12: the FUTURE-PLANNING predicate (fleet_planning_location). None = may be planned
    # (subject to the visit's hub and, when away, to the run's reserved window); else the eligibility reason
    # (same wording as the SQL view).
    planning_problem: str | None = None
    away_run: str | None = None  # the open run that has the robot away
    away_hub: int | None = None  # that run's visit hub
    away_from: datetime | None = None  # the run's reserved window [away_from, away_until)
    away_until: datetime | None = None

    @property
    def away(self) -> bool:
        """Away on an open run whose reserved window has not ended: plannable outside it, At risk."""
        return (
            self.kind in ("vehicle", "site") and self.planning_problem is None and self.away_run is not None
        )

    def planning_problem_for(
        self, hub_id: int | None, start: datetime | None, end: datetime | None
    ) -> str | None:
        """The visit-specific answer, as fleet_planning_location_problem (SQL) gives it (no robot code):
        None = allowed. ``start``/``end``: the booking window [start, end)."""
        if self.planning_problem:
            return self.planning_problem
        if self.kind == "hub":
            if hub_id is not None and self.hub_id != hub_id:
                return f"At hub {self.ref}, not at this visit's hub"
            return None
        if hub_id is not None and self.away_hub != hub_id:
            return f"Away on run {self.away_run} of another hub than this visit's"
        if (
            start is not None
            and self.away_from is not None
            and self.away_until is not None
            and start < self.away_until
            and (end if end is not None and end > start else start) >= self.away_from
        ):
            until = f"{self.away_until:%Y-%m-%d %H:%M} UTC"
            return f"Away on run {self.away_run} until {until}; this booking overlaps it"
        return None

    @property
    def at_verified_hub(self) -> bool:
        return self.kind == "hub" and self.verified and self.hub_id is not None

    def dispatchable_at(self, hub_id: int | None) -> bool:
        return self.at_verified_hub and hub_id is not None and self.hub_id == hub_id

    @property
    def label(self) -> str:
        base = KIND_LABELS.get(self.kind, self.kind)
        if self.kind == "hub":
            return f"At hub {self.ref}" + ("" if self.verified else " (not verified)")
        if self.kind == "transfer_in_transit":
            return f"{base}: {self.ref}"
        if self.kind in ("vehicle", "site") and self.ref:
            return f"{base} ({self.ref})" + ("" if self.verified else ", not verified")
        if self.kind == "unaccounted" and self.ref:
            return f"{base} (incident {self.ref})"
        return base


# The columns of fleet_planning_location (fleet/0017), in Location's field order after ``kind``.
COLUMNS = (
    "l.kind, l.hub_id, l.ref, l.verified, l.problem, l.planning_problem, l.away_run, l.away_hub, "
    "l.away_from, l.away_until"
)
SQL = (
    f"SELECT r.id, {COLUMNS} FROM fleet_robot r CROSS JOIN LATERAL fleet_planning_location(r.id) l "
    "WHERE r.id = ANY(%s)"
)


def from_row(row: Any) -> Location:
    """A Location from the COLUMNS of fleet_planning_location."""
    return Location(row[0], row[1], row[2], bool(row[3]), row[4], row[5], row[6], row[7], row[8], row[9])


def robot_locations(robot_ids: Iterable[int]) -> dict[int, Location]:
    """The location of each robot (one statement, whatever the number of robots)."""
    ids = sorted({int(i) for i in robot_ids})
    if not ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(SQL, [ids])
        return {r[0]: from_row(r[1:]) for r in cur.fetchall()}


def robot_location(robot_id: int) -> Location:
    return robot_locations([robot_id]).get(robot_id) or Location(
        "unknown", None, "", False, "Location unknown", "Location unknown"
    )
