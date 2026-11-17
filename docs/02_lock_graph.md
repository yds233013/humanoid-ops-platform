# Lock Graph

Every command acquires row locks in one global order. Triggers may only touch rows the calling command already holds, or rows later in the order.

```
role grants (run_command, FOR SHARE)  ->  planning_site  ->  fleet_manifestpattern  ->  fleet_robot
  ->  planning_person  ->  planning_vehicle  ->  planning_visit  ->  planning_vehicletrip (cargo trigger)
  ->  readiness_readinesspolicy  ->  readiness_departureauthorization        (Slice 3)
```

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

## Triggers that write to other rows

| Trigger | Writes | Already held by the caller? |
|---|---|---|
| hold insert/release | `fleet_robot` version (or pattern + its robots) | Yes: robot U / pattern U + robots U |
| pattern decision, capability profile/approval | pattern + robots on it | Pattern U (decide/publish/approve). Robots are later in the order |
| base authorization insert/revoke | `fleet_robot` version | Yes: robot U |
| consent insert/withdraw, assessment insert, assessment approval | `planning_site` version | Yes: site U |
| qualification insert/revoke | `planning_person` version | Yes: person U |
| any visit update | its own `version` (BEFORE UPDATE) | Yes: visit U |
| trip cargo insert | locks `planning_vehicletrip` U | The trip was created in the same transaction; last in the order |

## Tests that prove it

- `test_fleet_concurrency.py`: both orders of hold vs check, manifest vs check, role revocation vs command; a deadlock that ignores the order is detected; `lock_guards` with opposite inputs never deadlocks.
- `test_planning_concurrency.py`: two planners on the same robot; app locks bypassed (the exclusion constraint alone prevents double booking); concurrent reschedules; hold vs confirm in both orders; consent withdrawal vs confirm.
- `test_planning_db.py`: overlaps rejected for direct SQL and for two raw concurrent transactions.

**Known gap:** `fleet.approve_capability_profile` bumps robots through a trigger while holding only the pattern lock. That stays in order (pattern before robots), but the robots aren't locked first. It's safe because robots come later in the order. A command that locks a robot and then a pattern would conflict, and no command does that.
