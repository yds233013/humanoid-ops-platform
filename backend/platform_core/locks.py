"""Guard-row locking in ONE global order: manifest patterns (by id) then robots (by id).

Writers that change whether a robot may be planned/deployed lock FOR UPDATE.
Checkers lock FOR SHARE. FOR SHARE conflicts with FOR UPDATE, so a writer and a checker on the same
guard row are totally ordered. Reads that depend on the guard happen AFTER the locks are held, so under
READ COMMITTED each such read sees every writer that committed before the lock was granted.
"""

from collections.abc import Iterable

from django.db import connection

from platform_core.testhooks import checkpoint

_ORDER = [("fleet_manifestpattern", "patterns"), ("fleet_robot", "robots")]


def lock_guards(
    *,
    update_patterns: Iterable[int] = (),
    share_patterns: Iterable[int] = (),
    update_robots: Iterable[int] = (),
    share_robots: Iterable[int] = (),
    label: str = "",
) -> None:
    plan = {
        "fleet_manifestpattern": (
            sorted(set(update_patterns)),
            sorted(set(share_patterns) - set(update_patterns)),
        ),
        "fleet_robot": (sorted(set(update_robots)), sorted(set(share_robots) - set(update_robots))),
    }
    with connection.cursor() as cur:
        for table, _ in _ORDER:
            upd, shr = plan[table]
            # within one table: all ids in ascending order; the mode per id is fixed by the caller
            for rid in sorted(set(upd) | set(shr)):
                mode = "UPDATE" if rid in upd else "SHARE"
                cur.execute(f"SELECT id FROM {table} WHERE id = %s FOR {mode}", [rid])
    if label:
        checkpoint(f"locked:{label}")
