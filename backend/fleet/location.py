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

from django.db import connection

KIND_LABELS = {
    "hub": "At hub",
    "vehicle": "In a vehicle",
    "site": "At a site",
    "transfer_in_transit": "On its way to another hub",
    "unaccounted": "Unaccounted for",
    "unknown": "Unknown",
}


@dataclass(frozen=True)
class Location:
    kind: str
    hub_id: int | None
    ref: str
    verified: bool
    problem: str | None  # None = a verified hub; else the eligibility reason (same wording as the SQL view)

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


SQL = (
    "SELECT r.id, l.kind, l.hub_id, l.ref, l.verified, fleet_location_problem(l.kind, l.ref, l.verified) "
    "FROM fleet_robot r CROSS JOIN LATERAL fleet_robot_location(r.id) l WHERE r.id = ANY(%s)"
)


def robot_locations(robot_ids: Iterable[int]) -> dict[int, Location]:
    """The location of each robot (one statement, whatever the number of robots)."""
    ids = sorted({int(i) for i in robot_ids})
    if not ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(SQL, [ids])
        return {r[0]: Location(r[1], r[2], r[3], bool(r[4]), r[5]) for r in cur.fetchall()}


def robot_location(robot_id: int) -> Location:
    return robot_locations([robot_id]).get(robot_id) or Location(
        "unknown", None, "", False, "Location unknown"
    )
