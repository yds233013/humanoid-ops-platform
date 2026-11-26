"""The versioned travel matrix (v2 03B-1, docs/13_v2_phase03_contract.md 10.H, 13.4 item 3, 13.5).

FROZEN read accessor (03B-2 and 03B-3 build on it):

    minutes(from_zone, to_zone, at) -> TravelValue(minutes | None, version_id | None, source)

``source`` is ``company`` or ``demo`` for a matrix value, ``unknown`` when the ordered pair has no version
in force at ``at`` (or a zone is unknown or retired). A missing pair is never filled in: minutes None reads
"Travel time unknown", and a new dispatch that needs that route is blocked (planning.services.evaluate).

A trip from hub H to a visit in zone Z uses two ordered pairs: out = (H's base zone -> Z) at the on-site
start, back = (Z -> H's base zone) at the on-site end (``route``). A visit with no zone (a pre-03B visit, or
a home whose zone is unknown) keeps its recorded minutes, as before 03B.

Writes go only through ``planning_set_travel_time`` (planning/0013, SECURITY DEFINER; admin or ops lead):
every value is a new version, effective now or later, so a saved plan's minutes and version are never
rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection

from platform_core.commands import Invalid, Outcome, guarded_function, run_command
from platform_core.middleware import current_request_id

UNKNOWN = "unknown"
UNKNOWN_TEXT = "Travel time unknown"
SOURCE_WORDS = {"company": "company value", "demo": "demo value", UNKNOWN: "unknown"}


@dataclass(frozen=True)
class TravelValue:
    minutes: int | None
    version_id: int | None
    source: str

    @property
    def known(self) -> bool:
        return self.minutes is not None

    @property
    def label(self) -> str:
        if self.minutes is None:
            return UNKNOWN_TEXT
        return f"{self.minutes} min ({SOURCE_WORDS.get(self.source, self.source)})"


NONE = TravelValue(None, None, UNKNOWN)


def _id(value: Any) -> int | None:
    if value is None:
        return None
    return int(getattr(value, "pk", value))


def _value(minutes: Any, version_id: Any, source: Any) -> TravelValue:
    if minutes is None:
        return NONE
    return TravelValue(int(minutes), int(version_id), str(source))


def minutes(from_zone: Any, to_zone: Any, at: datetime) -> TravelValue:
    """The version of the ORDERED pair in force at ``at``. One statement (none when a zone is unknown)."""
    a, b = _id(from_zone), _id(to_zone)
    if a is None or b is None:
        return NONE
    with connection.cursor() as cur:
        cur.execute("SELECT minutes, version_id, source FROM planning_travel_minutes(%s, %s, %s)", [a, b, at])
        row = cur.fetchone()
    return _value(*row) if row else NONE


@dataclass(frozen=True)
class Route:
    """Travel for one visit: out (hub base zone -> visit zone) and back (the reverse pair)."""

    out: TravelValue
    back: TravelValue
    hub_zone_id: int | None
    zone_id: int | None
    zone_active: bool

    @property
    def known(self) -> bool:
        return self.out.known and self.back.known


ROUTE_SQL = """
SELECT h.base_zone_id, z.active, o.minutes, o.version_id, o.source, b.minutes, b.version_id, b.source
FROM platform_core_hub h
LEFT JOIN platform_core_zone z ON z.id = %(zone)s
LEFT JOIN LATERAL planning_travel_minutes(h.base_zone_id, %(zone)s, %(out_at)s) o ON true
LEFT JOIN LATERAL planning_travel_minutes(%(zone)s, h.base_zone_id, %(back_at)s) b ON true
WHERE h.id = %(hub)s
"""


def route(hub: Any, zone: Any, start: datetime, end: datetime) -> Route:
    """Out at ``start`` and back at ``end`` for a visit dispatched from ``hub`` to ``zone``. One statement."""
    hub_id, zone_id = _id(hub), _id(zone)
    if hub_id is None or zone_id is None:
        return Route(NONE, NONE, None, zone_id, True)
    with connection.cursor() as cur:
        cur.execute(ROUTE_SQL, {"hub": hub_id, "zone": zone_id, "out_at": start, "back_at": end})
        row = cur.fetchone()
    if row is None:
        return Route(NONE, NONE, None, zone_id, True)
    base, active, *vals = row
    return Route(_value(*vals[0:3]), _value(*vals[3:6]), base, zone_id, bool(active))


# ------------------------------------------------------------------ the matrix page (S5)
@dataclass
class MatrixCell:
    from_zone_id: int
    to_zone_id: int
    current: TravelValue
    version: int | None = None  # the version number of the value in force
    pending: int = 0  # versions that take effect later


MATRIX_SQL = """
SELECT f.id, t.id, cur.minutes, cur.id, cur.source, cur.version,
       (SELECT count(*) FROM planning_travel_time x WHERE x.from_zone_id = f.id AND x.to_zone_id = t.id
          AND x.effective_from > clock_timestamp())
FROM platform_core_zone f CROSS JOIN platform_core_zone t
LEFT JOIN LATERAL (
  SELECT x.minutes, x.id, x.source, x.version FROM planning_travel_time x
  WHERE x.from_zone_id = f.id AND x.to_zone_id = t.id AND x.effective_from <= clock_timestamp()
    AND f.active AND t.active
  ORDER BY x.version DESC LIMIT 1) cur ON true
WHERE f.id = ANY(%s) AND t.id = ANY(%s)
"""


def matrix(zone_ids: list[int]) -> dict[tuple[int, int], MatrixCell]:
    """The value in force now for every ordered pair of ``zone_ids``. One statement, whatever the size."""
    if not zone_ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(MATRIX_SQL, [zone_ids, zone_ids])
        rows = cur.fetchall()
    return {
        (f, t): MatrixCell(f, t, _value(m, vid, src), int(ver) if ver is not None else None, int(pending))
        for f, t, m, vid, src, ver, pending in rows
    }


def history(from_zone: Any, to_zone: Any) -> list[Any]:
    from planning.models import TravelTime

    return list(
        TravelTime.objects.filter(from_zone_id=_id(from_zone), to_zone_id=_id(to_zone))
        .select_related("created_by")
        .order_by("-version")
    )


# ------------------------------------------------------------------ the one write
def set_travel_time(
    actor: Any,
    *,
    from_zone_id: int,
    to_zone_id: int,
    minutes: int,
    source: str,
    effective_from: datetime | None = None,
) -> dict[str, Any]:
    """A new version of the ordered pair (from -> to), effective now or at ``effective_from`` (never
    earlier)."""
    payload = {
        "from_zone": from_zone_id,
        "to_zone": to_zone_id,
        "minutes": minutes,
        "source": source,
        "effective_from": effective_from.isoformat() if effective_from else None,
    }

    def handler() -> Outcome:
        if effective_from is not None and effective_from.tzinfo is None:
            raise Invalid("The start time must be timezone-aware")
        new_id = guarded_function(
            "SELECT planning_set_travel_time(%s, %s, %s, %s, %s, %s, %s)",
            [
                actor.pk,
                from_zone_id,
                to_zone_id,
                minutes,
                source,
                effective_from,
                current_request_id.get(),
            ],
        )
        from planning.models import TravelTime

        row = TravelTime.objects.get(pk=new_id)
        return Outcome(
            "travel_time",
            str(new_id),
            {
                "version": row.version,
                "minutes": row.minutes,
                "effective_from": row.effective_from.isoformat(),
            },
            audited=True,
        )

    from platform_core.models import Zone

    return run_command(
        actor,
        "planning.set_travel_time",
        permission="planning.set_travel_time",
        payload=payload,
        handler=handler,
        hub=lambda: Zone.objects.filter(pk=from_zone_id).values_list("hub_id", flat=True).first(),
    )
