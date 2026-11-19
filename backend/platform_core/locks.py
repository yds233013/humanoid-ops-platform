"""Guard-row locking in ONE global order (see docs/02_lock_graph.md):

    role grants (run_command) -> deployment settings (advisory, shared) -> sites -> manifest patterns
      -> robots -> persons -> vehicles -> visits -> vehicle trips -> readiness policies
      -> departure authorizations -> incident policies -> execution runs -> incidents   (Slice 4)
      -> hold-policy registry (fleet_holdpolicyregistry, singleton id 1)                  (Slice 7)

The hold-policy registry is LAST: its FOR UPDATE holders (hold-policy create / approve / withdraw) hold no
other guard row, so a trigger may take it FOR SHARE after its statement's row locks (fleet/0008) without a
cycle.

Deployment settings (fleet_deploymentsetting) are writable only by the schema owner, and the runtime role has
SELECT only, so it cannot row-lock them. Instead every change to that table takes the transaction-level
advisory lock SETTINGS_LOCK_KEY EXCLUSIVE (statement trigger, fleet migration 0007), and a command whose
decision depends on settings takes the same key SHARED (share_settings=True) before any row lock, then reads
the settings. A settings change and such a command are therefore totally ordered: the command either commits
entirely before the change is applied or reads the changed value. A settings change must run in its own
transaction (it takes no row guards), so it can never hold a guard row while waiting for this lock.

Writers that change whether something may be planned lock FOR UPDATE; checkers lock FOR SHARE. FOR SHARE
conflicts with FOR UPDATE, so a writer and a checker on the same guard row are totally ordered. Reads that
depend on the guards happen AFTER the locks are held (READ COMMITTED gives each statement a fresh snapshot).
Trips are created inside the visit's transaction and their cargo trigger locks the trip row last.
"""

from collections.abc import Iterable

from django.db import connection

from platform_core.testhooks import checkpoint

# "0F1EE7 5E77" ~ "FLEET SETT". Must equal the literal in fleet/migrations/0007_settings_advisory_lock.py
# (tests/test_readiness_settings_race.py checks the installed trigger function). Never reuse it elsewhere.
SETTINGS_LOCK_KEY = 0x0F1EE75E77  # 64942988919

ORDER = [
    ("planning_site", "sites"),
    ("fleet_manifestpattern", "patterns"),
    ("fleet_robot", "robots"),
    ("planning_person", "persons"),
    ("planning_vehicle", "vehicles"),
    ("planning_visit", "visits"),
    ("planning_vehicletrip", "trips"),
    ("readiness_readinesspolicy", "policies"),
    ("readiness_departureauthorization", "authorizations"),
    ("execution_incidentpolicy", "incident_policies"),
    ("execution_executionrun", "runs"),
    ("execution_incident", "incidents"),
    ("fleet_holdpolicyregistry", "hold_policy_registry"),  # Slice 7: singleton row id 1, always last
]
HOLD_POLICY_REGISTRY_ID = 1


def lock_settings_shared() -> None:
    """Hold the deployment-settings lock SHARED until this transaction ends. Needs no table privilege."""
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [SETTINGS_LOCK_KEY])


def lock_guards(
    *,
    share_settings: bool = False,
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
    update_trips: Iterable[int] = (),
    share_trips: Iterable[int] = (),
    update_policies: Iterable[int] = (),
    share_policies: Iterable[int] = (),
    update_authorizations: Iterable[int] = (),
    share_authorizations: Iterable[int] = (),
    update_incident_policies: Iterable[int] = (),
    share_incident_policies: Iterable[int] = (),
    update_runs: Iterable[int] = (),
    share_runs: Iterable[int] = (),
    update_incidents: Iterable[int] = (),
    share_incidents: Iterable[int] = (),
    update_hold_policy_registry: bool = False,
    share_hold_policy_registry: bool = False,
    label: str = "",
) -> None:
    requested = {
        "sites": (update_sites, share_sites),
        "patterns": (update_patterns, share_patterns),
        "robots": (update_robots, share_robots),
        "persons": (update_persons, share_persons),
        "vehicles": (update_vehicles, share_vehicles),
        "visits": (update_visits, share_visits),
        "trips": (update_trips, share_trips),
        "policies": (update_policies, share_policies),
        "authorizations": (update_authorizations, share_authorizations),
        "incident_policies": (update_incident_policies, share_incident_policies),
        "runs": (update_runs, share_runs),
        "incidents": (update_incidents, share_incidents),
        "hold_policy_registry": (
            [HOLD_POLICY_REGISTRY_ID] if update_hold_policy_registry else [],
            [HOLD_POLICY_REGISTRY_ID] if share_hold_policy_registry else [],
        ),
    }
    if share_settings:  # first: right after run_command's role grants, before every guard row
        lock_settings_shared()
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
