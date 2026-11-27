# Phase 06: briefing benchmark, clean install, upgrade from Phase 02, backup and restore

Phase 06 engineer 2, branch `v2/06-perf` (base `0111a9f`), 2026-11-26. All data is synthetic and fictional. Local
PostgreSQL 16.16 in Docker (`localhost:5440`) on the shared development Mac, with other engineers' work running at
the same time (load average 13 to 66 during this work), so every local time below is indicative only. No password is
written here; the commands use the development values from `.env.example` (`set -a; source ../.env.example; set +a`).
Usability with independent people stays UNVERIFIED; nothing here is real-world validation.

## Gates

| Gate | Result |
| --- | --- |
| A. Briefing in the 500-robot benchmark (all hubs and one hub, p95 <= 2,000 ms) | Added, plus one fix (Today sections the briefing never shows are no longer read). **Not established locally**: the shared host gave no clean run (see A). Release numbers: the lead's three CI rounds |
| B. Clean install from an empty database (README path) | **PASS** |
| C. Upgrade from `v2-phase02-release` | **PASS** (data preserved, default hub backfill, pages render, reverse to Phase 02 and forward again) |
| D. Backup and restore rehearsal | **PASS** (runbook script RESULT PASSED, 26 table counts and 4 fingerprints equal, 1,618 effective privileges of `ops_app` equal, guards refuse, a definer function works) |

## A. Operations briefing in the 500-robot benchmark

`backend/tests/test_v2_05_scale.py::test_500_robot_p95` now also samples, as the same regional ops lead:

- `/ops/briefing/` (all 5 hubs) and `/ops/briefing/?hub=<first hub by code>` (one hub), budget p95 2,000 ms each,
  same method as the other pages (one warm-up, 20 timed requests, nearest-rank p95, statements per request recorded).
- Every sample must return 200, show the expected number of hubs (5, or 1 with `?hub=`) and carry no `error-*`
  notice. The briefing wraps each read in a savepoint and turns a failure into a "Not enough data" notice, so without
  this check a failing read would be timed as a fast page.

The existing pages, the sync-20 step and their statement recording are unchanged.

### Local runs (indicative only, shared host)

Six local attempts, same command
(`DB_NAME=humanoid_ops_06p_perf DJANGO_DB_IDENTITY=owner uv run pytest -m perf -s -q tests/test_v2_05_scale.py`):

| # | Code | Load (1 min) | Outcome |
| --- | --- | --- | --- |
| 1 | before the fix | 32 to 66 | fixture build stopped: 10 s statement timeout on the bulk `planning_reservation` insert (`tests/operations_world.py::scale_world`), nothing timed |
| 2 | before the fix | 9 to 23 | `/ops/briefing/?hub=HUB-1`: one sample had `error-robots` (its `fleet_overview` hit the 10 s statement timeout); the check stopped the run |
| 3 | before the fix | 6 to 11 | completed; over budget: Today 2,104 and briefing (all hubs) 2,089 (table below) |
| 4 | after the fix | 13 to 16 | fixture build stopped (same reservation insert timeout) |
| 5 | after the fix | 12 to 33 | `/ops/briefing/?hub=HUB-1`: `error-robots` again (10 s timeout on `fleet_overview`) |
| 6 | after the fix | 17 to 30 | stopped on an **existing** page before the briefing: the board's robot-status count statement hit the 10 s timeout |

Run 3, the only complete run (p95 / median ms over 20 requests, statements per request):

| Endpoint | p95 | Median | Max | Statements | Budget |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/hub/` | 417 | 261 | 457 | 12 | 2,000 |
| Today `/ops/` | **2,104** | 1,654 | 2,250 | 27 | 2,000 |
| `/planning/board/` | 1,005 | 587 | 1,426 | 15 | 2,000 |
| `/readiness/` | 631 | 546 | 644 | 18 | 2,000 |
| `/ops/briefing/` (5 hubs) | **2,089** | 1,911 | 2,115 | 55 | 2,000 |
| `/ops/briefing/?hub=HUB-1` | 1,986 | 1,789 | 2,157 | 57 | 2,000 |
| sync-20 | 591 | 512 | 684 | 631 | 1,000 |

On the same host Today itself was over budget (CI: 738 to 905 ms in the Phase 05 rounds), so the local numbers say
more about the host than about the code. Statement 10 s timeouts in runs 2, 5 and 6 hit three different statements,
including one on a page this phase did not touch; the database container is shared with other engineers' suites.

### The one fix (query batching)

Profile of the cause: `build_briefing` called `today.build_today(...)`, which runs all four Today sections. Two of
them the briefing never uses: the fleet section (`readmodels.fleet_overview`, the heaviest read at 500 robots, about
half of Today's SQL time in `05_scale_validation.md`, plus its hub filter) and the production-blocker panel. The
briefing then ran `fleet_overview` again for question 1. So each briefing request read the whole fleet twice.

- `operations/today.py`: `build_today` takes an optional keyword `sections` (default `None` = all four, so the Today
  page is unchanged; it is the only other caller).
- `operations/briefing.py`: `build_briefing` asks for `("visits", "issues")` only, the two sections it reads
  (`t.visits*`, `t.unconfigured`, `t.coverage`, `t.issues_ok`, `t.issue_count_exact`, `t.inbox_cards`). No read
  model, rule, wording or page output changes.
- Effect: one `fleet_overview` and no `production_blockers` per briefing request. On the small world of
  `test_statement_count_is_constant_in_fleet_size_and_within_budget` the briefing goes from 120 to 114 statements
  (budget 1,022, still constant in fleet size).
- Regression test: `tests/test_v2_04_briefing.py::test_the_fleet_is_read_once_per_briefing` (all hubs and `?hub=`:
  `fleet_overview` once, `production_blockers` never, no error notice, question 1 still answered; Today still reads
  both). It fails with the old call (`production_blockers: 1`) and passes with the fix.

No complete local run of the fixed code was possible (runs 4 to 6), so whether the briefing meets its 2,000 ms
budget is decided by the lead's three CI rounds on the final candidate.

## B. Clean installation (README / Makefile path)

The shared Docker cluster on 5440 already has the `ops_owner` / `ops_app` roles from
`docker/postgres/init/01-roles.sh`, so `make db` was not re-run (it would start a second compose project on the same
port from this worktree). Roles checked: `ops_owner` LOGIN CREATEDB, `ops_app` LOGIN NOCREATEDB NOCREATEROLE, neither
superuser, as the init script creates them. The database was created with the init script's own statement and the
two Makefile/README steps were run as written, with `DB_NAME` pointed at the scratch database:

```
psql -U postgres -c "CREATE DATABASE humanoid_ops_06p_clean OWNER ops_owner"      # 01-roles.sh, last line
cd backend && set -a; source ../.env.example; set +a; export DB_NAME=humanoid_ops_06p_clean
DJANGO_DB_IDENTITY=owner uv run python manage.py migrate --noinput                 # make migrate
DJANGO_DB_IDENTITY=owner uv run python manage.py build_demo_workspace              # README option A
```

| Step | Time | Result |
| --- | ---: | --- |
| `migrate` (107 migrations) | 92 s (load ~30) | exit 0 |
| `build_demo_workspace` | 321 s (load 32 to 66; the runbook says about 25 s on a quiet laptop, and the Phase 02 build below took 34 s at load ~24) | exit 0: 3 hubs (OAK 49 robots, SJC 30, SRF 20), 22 logins printed, J4 visit V-0037, practice runs EX-0001 / EX-0002 |

Resulting rows: 99 robots, 100 visits, 950 reservations, 3 runs, 3 departure authorizations, 1 incident, 3 holds,
22 users, 26 role grants, 1,882 audit events. No owner table has a row without a hub.

**Smoke check** (`manage.py shell` with the Django test client, connected as the runtime role `ops_app`; the ops lead
`dana.ortiz`, the crew lead of EX-0001 for the field snapshot). Single cold requests, ms:

| Page | Clean install | Upgraded (C) | Restored (D) |
| --- | --- | --- | --- |
| `/healthz` (`migrations: ok`) | 200, 1,332 | 200, 793 | 200, 750 |
| `/` anonymous / signed in | 302 / 302 to `/ops/` | 302 / 302 to `/ops/` | 302 / 302 to `/ops/` |
| `/login/` | 200, 29 | 200, 25 | 200, 16 |
| Today `/ops/` | 200, 1,948 | 200, 1,024 | 200, 854 |
| `/ops/briefing/` (all hubs) | 200, 3,686 (hubs OAK, SJC, SRF) | 200, 1,267 (OAK) | 200, 948 (OAK) |
| `/ops/briefing/?hub=OAK` | 200, 2,605 | 200, 1,016 | 200, 1,116 |
| `/planning/board/` | 200, 208 | 200, 79 | 200, 91 |
| `/readiness/` | 200, 2,200 | 200, 621 | 200, 580 |
| `/hub/` | 200, 91 | 200, 48 | 200, 37 |
| `/fleet/` | 200, 4,507 | 200, 1,860 | 200, 1,266 |
| run page `/execution/runs/EX-0001/` | 200, 105 | 200, 144 | 200, 146 |
| `/api/field/runs/EX-0001/snapshot?device=<uuid>` | 200, 29 | 200, 23 | 200, 58 |

No briefing rendered an `error-*` notice. The clean-install times were taken at a load average of about 30.

## C. Upgrade from `v2-phase02-release`

```
git worktree add --detach /Users/yashshah2311/hop-06-perf-p02 v2-phase02-release     # 48f3298
psql -U postgres -c "CREATE DATABASE humanoid_ops_06p_upgrade OWNER ops_owner"
cd /Users/yashshah2311/hop-06-perf-p02/backend && export DB_NAME=humanoid_ops_06p_upgrade
DJANGO_DB_IDENTITY=owner uv run python manage.py migrate --noinput                   # Phase 02 code, 73 migrations
DJANGO_DB_IDENTITY=owner uv run python manage.py build_demo_workspace                # Phase 02 demo
pg_dump ... --format=custom --no-owner   (pre-upgrade dump, runbook 8d step 2: 1.07 MB, 3 s)
cd /Users/yashshah2311/hop-06-perf/backend
DJANGO_DB_IDENTITY=owner uv run python manage.py migrate --noinput                   # this branch: 34 migrations
DJANGO_DB_IDENTITY=owner uv run python manage.py migrate --check                    # clean
DJANGO_DB_IDENTITY=owner uv run python manage.py makemigrations --check --dry-run   # "No changes detected"
```

| Step | Time | Result |
| --- | ---: | --- |
| Phase 02 `migrate` (73) | 136 s (load ~50) | exit 0 |
| Phase 02 `build_demo_workspace` | 34 s | exit 0: 1 hub (OAK), 50 robots, practice runs EX-0001 / EX-0002 |
| Upgrade `migrate` with this branch (34: accounts 0005-0008, execution 0006-0011, fleet 0012-0018, hub 0010-0015, operations 0009-0011, planning 0010-0015, platform_core 0005-0006) | 12 s | exit 0, every migration OK, no warning |

**Data preserved** (owner connection, before and right after the upgrade, before any page was opened):

| Table | Before (Phase 02) | After upgrade |
| --- | ---: | ---: |
| django_migrations | 73 | 107 |
| auth_user | 14 | 14 |
| accounts_rolegrant | 15 | 15 |
| platform_core_hub | 1 | 1 |
| platform_core_auditevent | 1,092 | 1,092 |
| fleet_robot | 50 | 50 |
| fleet_hold | 3 | 3 |
| fleet_inspection | 54 | 54 |
| fleet_maintenancerecord | 189 | 189 |
| planning_person / planning_vehicle | 10 / 4 | 10 / 4 |
| planning_visit (visits) | 86 | 86 |
| planning_reservation | 820 | 820 |
| readiness_departureauthorization / departureevent | 2 / 2 | 2 / 2 |
| execution_executionrun (runs) | 2 | 2 |
| execution_custodyevent | 8 | 8 |
| execution_incident | 1 | 1 |
| hub_workorder / charger / charging_session | 3 / 4 / 1 | 3 / 4 / 1 |
| hub_stock_item / stock_movement | 4 / 16 | 4 / 16 |
| operations_predictor_observation | 2 | 2 |
| fleet_deploymentsetting | 16 | 16 |

Fingerprints equal before and after: audit max id 1,092 and md5 over (id, command, entity) of every audit row; md5
over (code, version) of every robot; md5 over (code, status, version) of every visit. So the backfill bumped no
version and wrote no audit row (correct: the database already had hub OAK, so `platform_core_default_hub` created no
HUB-1).

**Default hub backfill:** after the upgrade `fleet_robot`, `planning_vehicle`, `planning_person`, `planning_visit`,
`hub_workorder`, `hub_charging_session`, `hub_charger_reservation`, `hub_charger` and `hub_stock_item` each have 0
rows without a hub and exactly one distinct hub (OAK). The 8 `field_operator` / `hub_technician` grants are now at
OAK; every other grant stayed regional (hub NULL). The no-hub case (HUB-1 created and audited) is covered by
`tests/test_v2_03a_ownership.py::test_backfill_creates_hub_1_when_a_database_has_rows_but_no_hub` (run below).

**Pages render** on the upgraded database as `ops_app`: see the smoke table in B (all 200).

**Reverse check.** The project already tests reversal (`tests/test_v2_03a_ownership.py` migrates
`platform_core 0004_hub_formtoken` and back), so the same was done on a copy
(`CREATE DATABASE humanoid_ops_06p_reverse TEMPLATE humanoid_ops_06p_upgrade`):

| Step | Time | Result |
| --- | ---: | --- |
| `migrate platform_core 0004_hub_formtoken` | 28 s | exit 0: 34 unapplied, 73 left (the exact Phase 02 set), `hub_id` columns gone except `hub_charger` / `hub_stock_item` (Phase 02 had them) |
| `migrate` (forward again) | 8 s | exit 0: 34 applied |

All 26 counted tables and the three fingerprints are equal at every step (upgraded, reversed, forward); the backfill
is again complete after the forward run. (This copy was taken after the smoke check, whose `/hub/` request derived
25 hub tasks with 25 `hub.derive_task` audit rows; that is the hub queue's existing behaviour on view, not the
migration.)

## D. Backup and restore rehearsal

Host `pg_dump` is 15.13 and refuses a 16 server (runbook section 4), so the runbook script ran inside the database
container with its PostgreSQL 16.16 client tools:

```
psql -U postgres -c "CREATE DATABASE humanoid_ops_06p_restore OWNER ops_owner"      # new, empty
docker cp backend/deploy/backup_restore_check.sh <db container>:/tmp/
docker exec <db container> sh /tmp/backup_restore_check.sh \
  postgresql://ops_owner:...@localhost:5432/humanoid_ops_06p_upgrade \
  postgresql://ops_owner:...@localhost:5432/humanoid_ops_06p_restore /tmp/p06_upgraded.dump
```

(`pg_dump --format=custom --no-owner`, then `pg_restore --no-owner --exit-on-error`, as runbook sections 3 and 4.)

| Step | Time |
| --- | ---: |
| 1/4 dump (custom format, 1.30 MB) | 0.4 s |
| 2/4 restore into the empty target | 9.8 s |
| 3/4 row counts (7 tables) | 1.4 s |
| 4/4 audit triggers + workspace singleton constraint | 0.1 s |
| Total | 11.8 s |

Script output: `ok` for django_migrations 107, auth_user 14, accounts_rolegrant 15, platform_core_auditevent 1,117,
platform_core_workspace 1, fleet_robot 50, planning_visit 86; audit append-only triggers present; workspace singleton
constraint present; `RESULT: restore check PASSED`.

Extra checks (source = upgraded database, target = restored):

| Check | Result |
| --- | --- |
| 26 table counts + audit max id / audit md5 / robot md5 / visit md5 / hub list / backfill state | all equal |
| `manage.py migrate --check` on the restored database | clean |
| Page renders | smoke check all 200 (table in B). Run pages EX-0001 / EX-0002, visit V-0037, robot NB-037 rendered on both: identical apart from per-request form tokens, idempotency keys and the "occurred at" default (now); see the two observations below |
| Table, sequence, function owners; SECURITY DEFINER flags and function ACLs; 262 user triggers; constraints; schema ACL; default privileges | identical, except 21 sequences whose ACL text was `{ops_owner=rwU/ops_owner}` in the source and NULL (built-in default) after restore. Both mean "owner only" |
| Effective privileges of `ops_app` (`has_table_privilege` x 7 kinds on every table and view, `has_sequence_privilege` x 3 on every sequence, `has_function_privilege` EXECUTE on every function: 1,618 checks) | identical on source and restored (909 true) |
| 44 SECURITY DEFINER functions | all owned by `ops_owner` on both; `execution_open_run_on_departure`, `fleet_hold_rebase_approval_guard`, `operations_log_predictor_signals` are still not executable by `ops_app` |

**`ops_app` still cannot write guarded tables** (psql as `ops_app`, each in a rolled-back transaction; same output on
the source and the restored database):

| Attempt | Refused by |
| --- | --- |
| UPDATE / DELETE / TRUNCATE `platform_core_auditevent` | permission denied |
| DELETE `fleet_hold` | trigger: "holds cannot be deleted; release them" |
| DELETE `hub_stock_movement`, `fleet_maintenancerecord`, `operations_predictor_observation` | permission denied |
| INSERT `accounts_rolegrant` (an admin grant) | permission denied |
| UPDATE `fleet_robot` SET hub_id = NULL | trigger: "Robot NB-001 has a booking or charging that has not ended; it cannot change hub" |
| INSERT `readiness_departureauthorization` | permission denied |
| UPDATE `fleet_deploymentsetting` (`demo_mode`) | permission denied |
| CREATE TABLE | permission denied for schema public |

**A definer function still works** after restore: as `ops_app`, `hub.charging.record_soc` (lily.tran, NB-010, 77 %)
calls `hub_record_soc_reading` (SECURITY DEFINER, owner `ops_owner`): `robot_soc_reading` 5 -> 6, one audit row
`hub.record_soc_reading` committed (1,117 -> 1,118). The visit page then shows NB-010 "needs charge" instead of
"charge forecast unknown", the only content difference between the source and restored renders.

## Targeted tests

All with `DJANGO_DB_IDENTITY=owner uv run pytest ... -q -p no:cacheprovider` on scratch test databases:

| Tests | Result |
| --- | --- |
| `tests/test_v2_04_briefing.py` (21, with the new test) + `tests/test_v2_01d_timezone.py` (9), after the fix | 30 passed |
| `tests/test_v2_03a_ownership.py -k backfill` (2: a Phase 02 demo database backfilled and reversed; HUB-1 created when rows have no hub) + `tests/test_v2_05_scale.py::test_the_app_connection_never_jit_compiles` | 3 passed |
| The new test against the old briefing call | fails as expected (`production_blockers: 1`) |
| `tests/test_v2_05_scale.py -m perf` | see A (no clean local run after the fix) |
| `ruff check` / `ruff format --check` on the changed files | clean |

## Observations (not fixed)

- **Low: reservation rows on the visit page have no tie-break.** `planning/views.py::reservation_rows` orders by
  `released_at, starts_at`; rows that start at the same time (V-0037: robot inspection and crew shift at 08:25) came
  out in a different order after the restore (physical row order). Content is identical; only the order of equal-time
  rows differs. A fix would add `"id"` to the `order_by`; left to the owner of `planning/views.py` (feature freeze,
  not in this engineer's files).
- **Info: sequence ACL text after restore.** 21 sequences carry an explicit owner-only ACL in a migrated database and
  NULL after `pg_restore`; the effective privileges are identical (checked above). Nothing to change.
- **Low (test fixture): the 500-robot fixture can hit the 10 s statement timeout on a loaded host.** Runs 1 and 4
  failed inside `tests/operations_world.py::scale_world` (the bulk insert of about 44,810 `planning_reservation`
  rows in one statement) before anything was timed. CI has built it three times (`05_scale_validation.md`). If a CI
  round fails there, chunking that insert (as Phase 05 did for authorizations) is the fix.
- `/hub/` derives hub tasks on view (25 `hub.derive_task` audit rows the first time it was opened on the upgraded
  demo). Existing behaviour; noted because it changes counts taken after a page view.

## Clean-up

The detached worktree `/Users/yashshah2311/hop-06-perf-p02` was removed with `git worktree remove`, and the scratch
databases `humanoid_ops_06p_clean`, `_upgrade`, `_restore`, `_reverse` and the test databases were dropped; the dump
inside the container was deleted.
