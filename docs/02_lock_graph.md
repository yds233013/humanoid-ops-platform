# Lock Graph

Every command acquires row locks in one global order. Triggers may only touch rows the calling command already holds, or rows later in the order.

```
role grants (run_command, FOR SHARE)  ->  deployment settings (advisory, SHARED)  ->  planning_site  ->  fleet_manifestpattern  ->  fleet_robot
  ->  planning_person  ->  planning_vehicle  ->  planning_visit  ->  planning_vehicletrip (cargo trigger)
  ->  readiness_readinesspolicy  ->  readiness_departureauthorization        (Slice 3)
```

Slice 4 (field execution, `docs/04_field_execution_contract.md` section 5) appends:

```
  ->  execution_incidentpolicy  ->  execution_executionrun  ->  execution_incident
```
and `planning_vehicletrip` becomes an explicit `lock_guards` target at its existing position (after visits).

Slice 7 (production guardrails, `docs/07a_production_guardrails_contract.md` section 9) appends exactly one row:

```
  ->  fleet_holdpolicyregistry   (singleton, id = 1; lock_guards(update_hold_policy_registry= / share_hold_policy_registry=))
```
- Every read or change of hold-policy state is serialized by this row: policy create / approve / withdraw take it
  U (and hold nothing else); hold placement, release, release approval, rebase request / approval take it S, after
  their robot (or pattern + robots) locks. No command locks `fleet_holdpolicy` rows any more.
- Because the registry is last and its U holders hold no other guard row, a trigger (`fleet_hold_guard`, the
  policy and rebase guards) may take it after its statement's row locks without a deadlock cycle.
- The settings lock stays FIRST. A mode-dependent decision takes it SHARED before any guard row; triggers never
  request it late, they only assert it with `platform_settings_lock_held()` (fleet/0008) and refuse
  `'<decision> depends on the deployment mode: take pg_advisory_xact_lock_shared(64942988919) first'`.
  Asserted by: `fleet_hold_guard` on UPDATE (release), `readiness_departure_guard` (departure),
  the escalation deadline-set approval trigger.

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
| **Slice 7** | |
| fleet.create_inspection_template | pattern S |
| fleet.approve / withdraw inspection template; create / approve / withdraw periodic rule | pattern U, robots on the pattern U (the trigger bumps the pattern and robots) |
| fleet.submit_inspection | robot U, then registry S (only when a hold is placed) |
| fleet.place_hold (robot) / (pattern) | robot U / pattern U + robots U, then registry S (binding resolved after it) |
| execution.report_incident | robot U, visit S, run U (unchanged), then registry S inside `_create_hold`; no settings lock |
| fleet.approve_release / withdraw_release_approval | robot U (or pattern U + robots U), registry S |
| fleet.release_hold | settings S, robot U (or pattern U + robots U), incident S (incident holds), registry S |
| fleet.request / approve hold rebase | robot U (or pattern U + robots U), registry S |
| fleet.create / approve / withdraw hold policy | registry U only |
| planning.grant / revoke qualification (incl. readiness_inspector) | person U (unchanged) |
| readiness.record_evidence | settings S, robot S, signer persons S, visit U (the trigger locks persons before the visit) |
| readiness.authorize_departure / record_departure | unchanged list; `person_versions` also holds the signers of both relied inspections, locked S in the persons position |
| execution escalation deadline set create / approve / withdraw | settings S only (uniqueness constraints serialize) |
| close-out, production blockers, new exceptions | none (snapshot read) |

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
| `readiness_issue_authorization()` (readiness/0005, the only INSERT path for ops_app) | settings S (advisory), site S, pattern S, robot S, persons S, vehicle S, visit S, policies >= S; then the insert above | Yes: authorize_departure holds all of them (visit as U) |
| authorization revocation insert | authorization U | Yes: visit U, authorization U |
| departure event insert | robot S, visit S, policy S, authorization S | Yes: command holds them |
| trip cargo insert | locks `planning_vehicletrip` U | The trip was created in the same transaction; last in the order |
| hold insert (Slice 7) | registry S, then robot / pattern version bumps | Robot U / pattern U + robots U held; registry is last |
| hold release (Slice 7) | asserts settings S held; registry S | Yes: release_hold takes settings S first, registry last |
| hold policy insert / approval / withdrawal (Slice 7) | registry U | Yes: the command holds the registry U, nothing else |
| template / periodic-rule approval or withdrawal (Slice 7) | pattern + its robots (`fleet_bump_pattern`) | Yes: pattern U + robots U |
| release approval insert (Slice 7) | registry S | Robot U (or pattern U + robots U) held; registry is last |
| rebase request / approval insert (Slice 7) | robot U (or pattern U + robots U), registry S; the final approval inserts the activation row | Yes: the command holds them |
| evidence insert (Slice 7) | robot S, signer persons S, then visit version | Yes: record_evidence holds settings S, robot S, persons S, visit U |
| qualification insert (Slice 7) | forces `granted_at` (and `valid_from` for an immediate grant), then person version (unchanged) | Yes: person U |

## Tests that prove it

- `test_fleet_concurrency.py`: both orders of hold vs check, manifest vs check, role revocation vs command; a deadlock that ignores the order is detected; `lock_guards` with opposite inputs never deadlocks.
- `test_planning_concurrency.py`: two planners on the same robot; app locks bypassed (the exclusion constraint alone prevents double booking); concurrent reschedules; hold vs confirm in both orders; consent withdrawal vs confirm.
- `test_readiness_settings_race.py`: demo -> production switch vs authorize and vs depart, both orders, the
  second transaction observed waiting on the advisory lock in `pg_stat_activity`; mutation checks with the shared
  lock disabled show the race; lock placement; the runtime role cannot write settings.
- `test_planning_db.py`: overlaps rejected for direct SQL and for two raw concurrent transactions.

**Known gap:** `fleet.approve_capability_profile` bumps robots through a trigger while holding only the pattern lock. That stays in order (pattern before robots), but the robots aren't locked first. It's safe because robots come later in the order. A command that locks a robot and then a pattern would conflict, and no command does that.
