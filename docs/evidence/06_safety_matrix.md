# Phase 06: safety boundaries and offline failure — test matrix

Branch `v2/06-e2e` (base `0111a9f`). This maps the EM Phase 06 safety gates to the tests that prove them. The
counts are test functions (`def test_`) per file at this branch. A parametrized test counts once here; it runs
more cases than that. Files marked **SQL** open their own connection as the runtime role `ops_app` and try to
get past a rule with direct SQL. All data is synthetic. Every check was done by the development team.

New in Phase 06 (this branch): `tests/test_v2_06_zone_regressions.py` (7 cases), `tests/test_v2_06_briefing_scope.py`
(3), `tests/browser/test_v2_06_lifecycle.py` (1 continuous real-browser scenario, see `06_lifecycle.md`).

## Gate: negative authorization and safety tests

| Test file | Tests | What it proves |
|---|---:|---|
| `test_commands.py` | 12 | A refused command writes only a `denied` audit row; any failure rolls back the change and its audit row; audit read needs `audit.read` |
| `test_readiness_authorization.py` | 36 | Departure authorization refused for wrong role, holds, stale or expired evidence, changed plan or manifest |
| `test_readiness_authz_boundary.py` **SQL** | 15 | Authorize, depart and replay at the boundary, also as `ops_app` |
| `test_readiness_hardening.py` **SQL** | 18 | Hardened readiness refusals |
| `test_v2_03c_protective.py` **SQL** | 11 | Start, resume and a new departure refused while an interruption is open; holds never changed by a protective step |
| `test_v2_03b_pause.py` **SQL** | 13 | Authorization and departure refused under a hub or zone pause; hub roles cannot lift; forged actors write nothing |
| `test_v2_03a_authority.py`, `_ownership.py`, `_transfers.py`, `_creation.py` **SQL** | 11 + 13 + 22 + 7 | Wrong role and wrong hub refused, transfers included |
| `test_execution_incidents.py` **SQL** | 13 | An incident commits its hold, custody, interruption and audit together |
| `browser/test_v2_03d_run_page.py` | 1 of 16 | Online-only (class A) steps disabled offline and never queued |
| `browser/test_v2_06_lifecycle.py` (new) | 1 | In the continuous scenario: the robot with an unfinished booking is refused a transfer; nothing reaches the server while offline |

## Gate: direct-SQL enforcement as `ops_app`

Files that connect as `ops_app`: `test_db_security` (5), `test_execution_db` (57), `test_fleet_db_enforcement` (10),
`test_planning_db` (13), `test_readiness_db` (25), `test_s7_db` (42), `test_operations_sql` (23),
`test_v2_03a_definer_audit` (8: every SECURITY DEFINER function called as `ops_app` with null, zero, unknown,
role-less and wrong-role actors), `test_v2_03b_site_zone` (8), `test_v2_03b_zones_travel` (12), `test_v2_03c_ledger`
(22), `test_v2_03c_sync` (43), plus the SQL-marked files above and the 02A to 02E files. New: 
`test_v2_06_zone_regressions.py` issues the authorization function and a raw departure INSERT as `ops_app` for
every zone change (7 cases).

## Gate: role, account and hub isolation

| Test file | Tests | What it proves |
|---|---:|---|
| `test_v2_03a_read_scope.py` **SQL** | 8 | Each hub's field operator and the regional lead see only their rows on robots, overview, incidents, issues, timeline, close-out, people; another hub's record is 403 |
| `test_v2_03b_board.py` | 1 (`test_hub_scoping`) | Board rows per hub; `?hub=` only narrows |
| `test_v2_04_briefing.py` | 2 | S10 (hub B viewer sees only hub B codes); 403 for a hub technician and a user with no role |
| `test_v2_03c_sync.py`, `test_v2_03c_ledger.py`, `test_v2_03d_snapshot.py` | (see offline) | `NOT_YOUR_RUN` with no run details; signed-out sync and snapshot get 401 |
| `browser/test_v2_03d_run_page.py` | 3 | Unsent steps stay with their account; locked offline shell after sign-out; a snapshot is never shown to another account |
| `test_v2_01a_platform.py` | 2 | Signed-out pages redirect; the signed-out API answers 401/403 JSON |
| **`test_v2_06_briefing_scope.py` (new)** | 2 | See gaps 1 and 2 below |

## Gate: offline durability and protective failure injection

| Test file | Tests | What it proves |
|---|---:|---|
| `test_v2_03c_sync.py` **SQL** | 43 | At most once per key, `KEY_REUSED`, held and retry store nothing, sequence and generation, abandonment |
| `test_v2_03c_ledger.py` **SQL** | 22 | The ledger is append-only and SELECT-only for `ops_app` |
| `test_v2_03c_protective.py` **SQL** | 11 | A protective stop that cannot apply records an interruption, never a false "stopped" |
| `test_v2_03c_commands.py` | 17 | Field command classes and handling |
| `browser/test_v2_03d_run_page.py` | 16 | Network cut, refresh and restart offline; protective first; final rejection keeps the banner; a lost answer (503) resends the same keys and confirms once; HTTP 200 without a result never confirms; two devices; revoked access; skip and reconcile; service-worker update (failures injected with `page.route`) |
| `browser/test_v2_03d_queue.py` | 20 | Queue module: sequence allocation, batching, locks, schema, identity switch |
| `browser/test_v2_03d_design_review.py` | 1 | Slow, rejected and aborted syncs on every screen state |
| **`browser/test_v2_06_lifecycle.py` (new)** | 1 | The same protocol inside the full lifecycle: offline note + interruption, no server write while offline, protective sent first, each confirmed once |

## Gate: zone-consistency regressions

| Test file | Tests | What it proves |
|---|---:|---|
| `test_v2_03b_site_zone.py` **SQL** | 8 | Only the regional lead moves a home; direct SQL refused; a zone change vs pause, authorization and departure, both orders |
| `test_v2_03b_zones_travel.py` **SQL** | 12 | Missing travel pair blocks (no fallback); matrix append-only and versioned |
| `test_v2_06z_zone_truth.py` | 4 | Readiness, planner and board say the same about a zone change |
| `test_v2_06z_review_matches_save.py` | 3 | Review panel = save (matrix pair, missing pair, no zone) |
| **`test_v2_06_zone_regressions.py` (new)** | 3 (7 cases) | For gets a zone / moves / loses its zone: plan stale on readiness, board and both authorization paths (service and SQL as `ops_app`); review of the re-plan = save; re-plan clears the blocker and the visit can leave; an authorization issued before the change is refused at departure (service and raw INSERT as `ops_app`); a change and its undo still void the old authorization, and a re-plan does not revive it |

## Gaps found and filled

1. **The briefing never showed more than the source pages, but nothing proved it.** S10 checked the codes on
   the briefing page only. New test: for a field operator granted only at hub B, every line of the briefing
   links to a source record this viewer can open (200) and that names it; the other hub's field operator gets
   403 on each of those links and their own briefing names none of them.
2. **Field-operator scope over time on the briefing.** New test: a field operator at both hubs sees both and
   `?hub=` narrows; after both grants are revoked, the briefing (with and without `?hub=`) and Today answer 403
   and show no record.
3. **Zone cases not covered before:** a home losing its zone, a change-and-undo, and the review/save agreement
   for a re-plan after each change (see the zone table).

## Defects found (fixed on this branch, each with a regression test)

| Severity | Defect | Fix | Regression test |
|---|---|---|---|
| Medium | The briefing did not name an unresolved interruption on a robot that is out on a visit: it listed the robot as "out on a visit" and said "Nothing to prioritise", while Today said "A visit session was interrupted and is still stopped". The departure gate answers "open run" first for such a robot, so the briefing's interruption rule never fired. Found by the lifecycle test. | `operations/briefing.py`: for a robot out on a visit, Today's own `INTERRUPTION_OPEN` card (same snapshot, no extra statement) adds "Unresolved interruption ... must not operate" and priority rule 3. No DB change. | `test_v2_06_briefing_scope.py::test_an_open_interruption_on_a_robot_out_on_a_visit_is_named_and_prioritised` (fails without the fix); lifecycle step 10 |
| Low | The visit dossier showed the authorization expiry as a raw UTC ISO time (`2026-11-27T01:17:06.833207+00:00`) while every other time is local. | `execution/dossier.py` uses the workspace local time format. | lifecycle step 14 asserts no `+00:00` in the dossier |

`tests/test_v2_04_briefing.py` (20, statement budget included) and `tests/test_execution_dossier.py` still pass.

## Not covered (open)

- `/api/audit-events` is tested only for allowed versus refused (admin versus no role). No test checks each role,
  and no test checks hub scoping inside audit rows. By design, audit read is regional (`audit.read` roles only).
- The briefing is not checked for a user whose grant expired by date. That path is the same as revocation
  (`hubscope`).
