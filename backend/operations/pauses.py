"""The regional pause (v2 03B-1, docs/13_v2_phase03_contract.md I-9, 10.G, 13.4 items 1 and 2, 13.5).

FROZEN read accessors (03B-2's board uses them):

    active_for(hub_id, zone_id) -> PauseInfo | None        operations_active_pause, one statement
    board_summary(hub_ids) -> dict[hub_id, list[PauseInfo]]  every active pause of those hubs, one statement

A pause stops new departure authorizations and departures (database gates, operations/0011). Planning stays
allowed and shows PAUSED_TEXT. Robots already out keep recording everything; nothing is recalled.

Commands: ``pause_region`` (the regional ops lead, or a safety_pause grant at the hub) and ``lift_pause``
(the regional ops lead only), each through run_command(hub=<the hub of the record>) and the SECURITY DEFINER
function that re-checks the actor, the hub and the lock order. ``affected_visits`` is the read-only preview
shown before confirming a pause.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection

from platform_core.commands import Invalid, Outcome, guarded_function, run_command
from platform_core.middleware import current_request_id
from platform_core.models import Hub, Zone

PAUSED_TEXT = "Paused: cannot leave until lifted"


@dataclass(frozen=True)
class PauseInfo:
    pause_id: int
    scope: str  # "hub" or "zone"
    reason: str
    since: datetime
    hub_id: int
    zone_id: int | None
    hub_name: str = ""
    hub_code: str = ""
    zone_name: str | None = None  # None for a hub pause
    zone_code: str | None = None
    time_zone: str = ""  # the hub's IANA zone, for local times

    @property
    def where(self) -> str:
        """``Oakland hub (OAK)`` or ``zone Downtown (DT) of Oakland hub (OAK)``."""
        hub = f"{self.hub_name} ({self.hub_code})"
        return hub if self.zone_id is None else f"zone {self.zone_name} ({self.zone_code}) of {hub}"


_COLUMNS = (
    "p.id, CASE WHEN p.zone_id IS NULL THEN 'hub' ELSE 'zone' END, p.reason, p.created_at, p.hub_id, "
    "p.zone_id, "
    "h.name, h.code, z.name, z.code, h.time_zone"
)
_FROM = (
    "FROM operations_region_pause p JOIN platform_core_hub h ON h.id = p.hub_id "
    "LEFT JOIN platform_core_zone z ON z.id = p.zone_id"
)
_ACTIVE = (
    "p.kind = 'pause' AND NOT EXISTS (SELECT 1 FROM operations_region_pause l "
    "WHERE l.kind = 'lift' AND l.pause_id = p.id)"
)


def _info(row: Any) -> PauseInfo:
    pid, scope, reason, since, hub_id, zone_id, hname, hcode, zname, zcode, tz = row
    return PauseInfo(
        int(pid),
        str(scope),
        str(reason),
        since,
        int(hub_id),
        int(zone_id) if zone_id is not None else None,
        hname,
        hcode,
        zname,
        zcode,
        tz,
    )


def active_for(hub_id: int | None, zone_id: int | None) -> PauseInfo | None:
    """The pause covering a visit dispatched from ``hub_id`` to ``zone_id`` (a hub pause first), or None.
    The same rule as the database gates (operations_active_pause). One statement."""
    if hub_id is None:
        return None
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM operations_active_pause(%s, %s) a "
            "JOIN operations_region_pause p ON p.id = a.pause_id "
            "JOIN platform_core_hub h ON h.id = p.hub_id LEFT JOIN platform_core_zone z ON z.id = p.zone_id "
            "ORDER BY a.since, a.pause_id LIMIT 1",
            [hub_id, zone_id],
        )
        row = cur.fetchone()
    return _info(row) if row else None


def board_summary(hub_ids: list[int] | set[int] | None) -> dict[int, list[PauseInfo]]:
    """Every active pause of ``hub_ids`` (None = every hub), hub pauses first, then by time. One statement."""
    sql = f"SELECT {_COLUMNS} {_FROM} WHERE {_ACTIVE}"
    params: list[Any] = []
    if hub_ids is not None:
        sql += " AND p.hub_id = ANY(%s)"
        params.append(sorted(hub_ids))
    sql += " ORDER BY p.hub_id, p.zone_id IS NOT NULL, p.created_at, p.id"
    out: dict[int, list[PauseInfo]] = {int(h): [] for h in (hub_ids or [])}
    with connection.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            info = _info(row)
            out.setdefault(info.hub_id, []).append(info)
    return out


def history(hub_ids: list[int] | set[int] | None, limit: int = 20) -> list[dict[str, Any]]:
    """Recent pause and lift rows of ``hub_ids`` (None = every hub), newest first. One statement."""
    sql = (
        "SELECT p.id, p.kind, p.pause_id, p.reason, p.created_at, h.name, h.code, coalesce(z.name, ''), "
        "coalesce(z.code, ''), h.time_zone, trim(concat_ws(' ', u.first_name, u.last_name)), u.username "
        f"{_FROM} JOIN auth_user u ON u.id = p.actor_id"
    )
    params: list[Any] = []
    if hub_ids is not None:
        sql += " WHERE p.hub_id = ANY(%s)"
        params.append(sorted(hub_ids))
    sql += " ORDER BY p.id DESC LIMIT %s"
    params.append(limit)
    with connection.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    keys = ("id", "kind", "pause_id", "reason", "at", "hub_name", "hub_code", "zone_name", "zone_code", "tz")
    out = []
    for row in rows:
        d = dict(zip(keys, row[:10], strict=True))
        d["actor"] = row[10] or row[11]
        out.append(d)
    return out


# ------------------------------------------------------------------ the preview shown before a pause
AFFECTED_SQL = """
SELECT v.code, s.code, s.timezone, r.on_site_start, v.status, h.code
FROM planning_visit v
JOIN planning_visitrevision r ON r.id = v.current_revision_id
JOIN planning_site s ON s.id = v.site_id
JOIN platform_core_hub h ON h.id = v.hub_id
WHERE v.status IN ('requested', 'planned', 'confirmed')
  AND r.on_site_end > clock_timestamp()
  AND NOT EXISTS (SELECT 1 FROM readiness_departureevent d WHERE d.visit_id = v.id)
  AND CASE WHEN %(zone)s::bigint IS NULL
           THEN v.hub_id = %(hub)s
                OR v.zone_id IN (SELECT z.id FROM platform_core_zone z WHERE z.hub_id = %(hub)s)
           ELSE v.zone_id = %(zone)s OR (v.zone_id IS NULL AND v.hub_id = %(hub)s) END
ORDER BY r.on_site_start, v.code
"""


@dataclass
class Affected:
    code: str
    site: str
    site_tz: str
    start: datetime
    status: str
    hub_code: str


def affected_visits(hub_id: int, zone_id: int | None) -> list[Affected]:
    """Visits not yet departed whose authorization and departure a pause of (hub, zone) would block: the same
    coverage rule as operations_active_pause. Read only, one statement."""
    with connection.cursor() as cur:
        cur.execute(AFFECTED_SQL, {"hub": hub_id, "zone": zone_id})
        return [Affected(*row) for row in cur.fetchall()]


# ------------------------------------------------------------------ commands
def _hub_zone(hub_code: str, zone_code: str | None) -> tuple[Hub, Zone | None]:
    hub = Hub.objects.filter(code=(hub_code or "").strip().upper()).first()
    if hub is None:
        raise Invalid("Choose the hub to pause")
    zone = None
    if zone_code:
        zone = Zone.objects.filter(hub=hub, code=zone_code.strip().upper()).first()
        if zone is None:
            raise Invalid(f"{hub.name} ({hub.code}) has no zone {zone_code}")
    return hub, zone


def pause_region(actor: Any, *, hub_code: str, zone_code: str | None, reason: str) -> dict[str, Any]:
    """Stop new authorizations and departures for a hub (zone None) or one of its zones."""
    hub, zone = _hub_zone(hub_code, zone_code)

    def handler() -> Outcome:
        pid = guarded_function(
            "SELECT operations_pause_region(%s, %s, %s, %s, %s)",
            [actor.pk, hub.pk, zone.pk if zone else None, reason, current_request_id.get()],
        )
        return Outcome(
            "region_pause",
            str(pid),
            {"hub": hub.code, "zone": zone.code if zone else None, "scope": "zone" if zone else "hub"},
            audited=True,
        )

    return run_command(
        actor,
        "operations.pause_region",
        permission="operations.pause",
        payload={"hub": hub.code, "zone": zone.code if zone else None, "reason": reason},
        handler=handler,
        hub=hub,
    )


def lift_pause(actor: Any, *, pause_id: int, reason: str) -> dict[str, Any]:
    from operations.models import RegionPause

    def handler() -> Outcome:
        lid = guarded_function(
            "SELECT operations_lift_pause(%s, %s, %s, %s)",
            [actor.pk, pause_id, reason, current_request_id.get()],
        )
        return Outcome("region_pause", str(pause_id), {"lift_id": lid, "lifted": True}, audited=True)

    return run_command(
        actor,
        "operations.lift_pause",
        permission="operations.lift_pause",
        payload={"pause": pause_id, "reason": reason},
        handler=handler,
        hub=lambda: RegionPause.objects.filter(pk=pause_id).values_list("hub_id", flat=True).first(),
    )
