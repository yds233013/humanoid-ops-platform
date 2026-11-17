"""Guard-row locking in ONE global order (see docs/02_lock_graph.md):

    role grants (run_command) -> sites -> manifest patterns -> robots -> persons -> vehicles -> visits
      -> readiness policies -> departure authorizations

Writers that change whether something may be planned lock FOR UPDATE; checkers lock FOR SHARE. FOR SHARE
conflicts with FOR UPDATE, so a writer and a checker on the same guard row are totally ordered. Reads that
depend on the guards happen AFTER the locks are held (READ COMMITTED gives each statement a fresh snapshot).
Trips are created inside the visit's transaction and their cargo trigger locks the trip row last.
"""

from collections.abc import Iterable

from django.db import connection

from platform_core.testhooks import checkpoint

ORDER = [
    ("planning_site", "sites"),
    ("fleet_manifestpattern", "patterns"),
    ("fleet_robot", "robots"),
    ("planning_person", "persons"),
    ("planning_vehicle", "vehicles"),
    ("planning_visit", "visits"),
    ("readiness_readinesspolicy", "policies"),
    ("readiness_departureauthorization", "authorizations"),
]


def lock_guards(
    *,
    update_sites: Iterable[int] = (),
    share_sites: Iterable[int] = (),
    update_patterns: Iterable[int] = (),
    share_patterns: Iterable[int] = (),
    update_robots: Iterable[int] = (),
    share_robots: Iterable[int] = (),
    update_persons: Iterable[int] = (),
    share_persons: Iterable[int] = (),
    update_vehicles: Iterable[int] = (),
    share_vehicles: Iterable[int] = (),
    update_visits: Iterable[int] = (),
    share_visits: Iterable[int] = (),
    update_policies: Iterable[int] = (),
    share_policies: Iterable[int] = (),
    update_authorizations: Iterable[int] = (),
    share_authorizations: Iterable[int] = (),
    label: str = "",
) -> None:
    requested = {
        "sites": (update_sites, share_sites),
        "patterns": (update_patterns, share_patterns),
        "robots": (update_robots, share_robots),
        "persons": (update_persons, share_persons),
        "vehicles": (update_vehicles, share_vehicles),
        "visits": (update_visits, share_visits),
        "policies": (update_policies, share_policies),
        "authorizations": (update_authorizations, share_authorizations),
    }
    with connection.cursor() as cur:
        for table, key in ORDER:
            upd_raw, shr_raw = requested[key]
            upd = {i for i in upd_raw if i is not None}
            shr = {i for i in shr_raw if i is not None} - upd
            for rid in sorted(upd | shr):  # ascending id within a table
                mode = "UPDATE" if rid in upd else "SHARE"
                cur.execute(f"SELECT id FROM {table} WHERE id = %s FOR {mode}", [rid])
    if label:
        checkpoint(f"locked:{label}")
