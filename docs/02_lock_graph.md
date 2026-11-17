# Lock Graph

Every command acquires row locks in one global order. Triggers may only touch rows the calling command already holds, or rows later in the order.

```
role grants (run_command, FOR SHARE)  ->  deployment settings (advisory, SHARED)  ->  planning_site  ->  fleet_manifestpattern  ->  fleet_robot
  ->  planning_person  ->  planning_vehicle  ->  planning_visit  ->  planning_vehicletrip (cargo trigger)
  ->  readiness_readinesspolicy  ->  readiness_departureauthorization        (Slice 3)
```

## Deployment settings lock (Slice 3, Engineer F)

`fleet_deploymentsetting` is writable only by the schema owner; the runtime role has SELECT only, so it cannot
row-lock settings. Instead a fixed, dedicated PostgreSQL advisory key guards them:

- Key `64942988919` (`0x0F1EE75E77`, `platform_core.locks.SETTINGS_LOCK_KEY`). Used for nothing else.
- Every INSERT / UPDATE / DELETE / TRUNCATE statement on `fleet_deploymentsetting` takes
  `pg_advisory_xact_lock(key)` EXCLUSIVE in a BEFORE statement trigger (fleet migration 0007). Enforced by the
  database, not Python.
- A command whose decision depends on settings calls `lock_guards(share_settings=True)`, which takes
  `pg_advisory_xact_lock_shared(key)` FIRST (right after run_command's role grants, before any guard row), and
  reads settings only after that. Only transaction-scoped advisory functions are used; both are released at
  commit/rollback, never held across transactions.
- A settings change must run as a plain DML statement in its own transaction (it takes no guard rows), so it
  can only ever wait on the settings lock, never hold a guard row while waiting: no deadlock.
- The schema owner and superusers are trusted administrators (they can disable the trigger). The runtime role
  cannot write settings at all, so it cannot bypass the trigger.

Takes it: `readiness.authorize_departure`, `readiness.record_departure`. A demo -> production switch either
waits until an in-flight demo authorization/departure commits (which is then recorded as mode `demo`), or
commits first and is seen by the command, which refuses (synthetic policy / demo authorization in production).

Not (yet) taken by fleet/planning commands that read settings (`allow_draft_policies` in hold triggers,
`consent_form_version`, `pre_visit_inspection_min`, `max_robots_per_trip`, synthetic-profile eligibility via
`demo_mode`). Those decisions are re-checked at authorize/depart under the lock, so a production switch cannot
let a synthetic authorization depart; adding `share_settings=True` to them is a cheap follow-up if a
settings change must also be atomic with planning.

Within a table, ids are locked in ascending order (`platform_core.locks.lock_guards`). Revocation locks the role-grant row `FOR UPDATE`, so it conflicts with a command's `FOR SHARE` check.

## Commands

| Command | Locks, in order (U = FOR UPDATE, S = FOR SHARE) |
|---|---|
| fleet.place_hold (robot) | robot U |
| fleet.place_hold (pattern) | pattern U, every robot on the pattern U |
| fleet.record_manifest | old + new pattern S, robot U |
| fleet.decide_pattern | pattern U, robots on it U |
| fleet.submit_inspection | robot U |
| fleet.check_eligibility | pattern S, robot S |
| fleet.approve_release / release_hold | robot U (or pattern U + robots U) |
| planning.record_assessment / approve / consent / withdraw / set_expected_adults | site U |
| planning.request_visit | site S |
| planning.plan_visit (and reschedule) | site S, pattern S, robot U, persons U, vehicle U, visit U |
| planning.confirm_visit | site S, pattern S, robot S, persons S, vehicle S, visit U |
| planning.cancel_visit | visit U |
| planning.grant/revoke_qualification | person U |
| readiness.authorize_departure | settings S (advisory), site S, pattern S, robot S, persons S, vehicle S, visit U, policy S |
| readiness.record_departure | settings S (advisory), same rows as authorize, then authorization U |
| readiness.revoke_authorization | visit U, authorization U (no settings lock) |
| deployment setting change (owner DML) | settings X (advisory, statement trigger) only |

## Triggers that write to other rows

| Trigger | Writes | Already held by the caller? |
|---|---|---|
| hold insert/release | `fleet_robot` version (or pattern + its robots) | Yes: robot U / pattern U + robots U |
| pattern decision, capability profile/approval | pattern + robots on it | Pattern U (decide/publish/approve). Robots are later in the order |
| base authorization insert/revoke | `fleet_robot` version | Yes: robot U |
| consent insert/withdraw, assessment insert, assessment approval | `planning_site` version | Yes: site U |
| qualification insert/revoke | `planning_person` version | Yes: person U |
| any visit update | its own `version` (BEFORE UPDATE) | Yes: visit U |
| readiness policy approval / withdrawal | policy `lock_version` | Yes: policy U |
| evidence insert / void, recovery plan insert | robot S (and persons S), then visit version | Yes: robot S, persons S, visit U |
| departure authorization insert | locks robot S, visit S, policy S | Yes: command holds them |
| authorization revocation insert | authorization U | Yes: visit U, authorization U |
| departure event insert | robot S, visit S, policy S, authorization S | Yes: command holds them |
| trip cargo insert | locks `planning_vehicletrip` U | The trip was created in the same transaction; last in the order |

## Tests that prove it

- `test_fleet_concurrency.py`: both orders of hold vs check, manifest vs check, role revocation vs command; a deadlock that ignores the order is detected; `lock_guards` with opposite inputs never deadlocks.
- `test_planning_concurrency.py`: two planners on the same robot; app locks bypassed (the exclusion constraint alone prevents double booking); concurrent reschedules; hold vs confirm in both orders; consent withdrawal vs confirm.
- `test_readiness_settings_race.py`: demo -> production switch vs authorize and vs depart, both orders, the
  second transaction observed waiting on the advisory lock in `pg_stat_activity`; mutation checks with the shared
  lock disabled show the race; lock placement; the runtime role cannot write settings.
- `test_planning_db.py`: overlaps rejected for direct SQL and for two raw concurrent transactions.

**Known gap:** `fleet.approve_capability_profile` bumps robots through a trigger while holding only the pattern lock. That stays in order (pattern before robots), but the robots aren't locked first. It's safe because robots come later in the order. A command that locks a robot and then a pattern would conflict, and no command does that.
