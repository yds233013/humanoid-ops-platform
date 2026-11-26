# Phase 05: 500-robot validation

Contract 15.11 (EM, 2026-11-26): 500 robots, 5 hubs, 5,000 visits, mixed states. The p95 budgets are 2 s for Hub,
Today, the dispatch board and Readiness, and 1 s for a 20-command field sync. Release numbers come from three rounds,
and at most one targeted fix is allowed. All data is synthetic and fictional.

**A 500-robot synthetic benchmark does not establish production capacity for 500 physical robots.**

## Fixture

The builder is `backend/tests/scale05_world.py::scale_world_500`. It builds on the Slice 5 builder
`tests/operations_world.py::scale_world`, which gains two parameters, `hubs` and `crews`. Their defaults keep the
200-robot fixture unchanged.

| What | Count |
| --- | --- |
| Hubs | 5 (HUB-1 .. HUB-5), 100 robots and about 1,000 visits each |
| Robots | 500, every one in service with a manifest, a periodic inspection and base authorization |
| Crews | 100: 200 people at their crew's hub, plus the base world's people. 100 crew vans, plus 8 base and spare vans |
| Visits | 5,001: 3,608 confirmed, 873 planned, 344 requested, 176 cancelled. 10 per robot, plus the base world's visit |
| Reservations | 44,810 (robot timeline, crew shifts, van trips) |
| Runs | 1,500: 2 closed per robot, and a third left at a random stage from departed to closed (unresolved included) |
| Custody events | 5,525 |
| Incidents | 500 through the real command, each holding its robot. One in three goes on to severity, escalation or closure |
| Holds | 540 (530 open): the incident holds, plus 40 manual or maintenance holds (8 per hub). 10 of those 40 are released |
| Transfers | 10 rows over 7 vans: 4 in transit, 2 received, 1 refused |
| Regional pauses | HUB-5 paused (active). HUB-4 paused and then lifted (history) |
| Charging | 20 chargers (4 per hub), 167 fresh state-of-charge readings (every third robot), 10 charging sessions (2 per hub) |

Visits, reservations, departures and run streams are inserted in bulk as the schema owner, as the Slice 5 fixture
does. No trigger is disabled. Everything else uses the real commands. Phase 05 issues the authorization and
departure inserts in chunks of 100 robots: one 500-row `readiness_issue_authorization` statement went over the
app's 10 s `statement_timeout`.

Transfers use spare vans only. Every robot has bookings that have not ended, and the transfer command refuses to
move a robot that does.

## Method

- **Test:** `backend/tests/test_v2_05_scale.py::test_500_robot_p95`. It is marked `perf` and skipped unless run
  with `-m perf`.
- **CI:** `.github/workflows/perf.yml` runs it as its own step (25-minute step timeout) and uploads `perf05.log`.
- **What is measured:** whole requests through the Django test client: auth, permission, snapshot, read models
  and rendering.
  - Viewer: a regional ops lead, who sees every hub. This is the widest read.
  - Pages: `/hub/`, Today (`/ops/`), `/planning/board/`, `/readiness/`.
  - Sync: `POST /api/field/sync` with 20 `session.observation` commands per request. The request comes from the
    crew second of a run on site. Each request uses fresh keys and the next 20 sequence numbers, and every
    command must come back `confirmed`.
- **Sampling:** one untimed warm-up request per endpoint, then 20 timed requests. p95 is nearest rank (the 19th
  of 20).
- **Output:** JSON with:
  - the environment before and after (load average, `shared_buffers`, `work_mem`, `jit`, `random_page_cost`,
    `effective_cache_size`, server version, row counts);
  - the fixture cardinalities and build phase timings;
  - per endpoint: p95, median, budget, statements per request and every raw sample.
- **Command:**

  ```
  cd backend && DB_NAME=<db> DJANGO_DB_IDENTITY=owner uv run pytest -m perf -s -q tests/test_v2_05_scale.py
  ```

## Local numbers (indicative only)

These were taken on the shared development Mac: PostgreSQL 16.16 in Docker, `shared_buffers` 128MB, `work_mem`
4MB, other engineers' work running at the same time.

| Endpoint | Statements | First run p95 / median (ms), load 9–23 | After the fix p95 / median (ms), load ~6 | Budget (ms) |
| --- | ---: | ---: | ---: | ---: |
| `/hub/` | 12 | 1,800 / 793 | 190 / 149 | 2,000 |
| Today `/ops/` | 27 | **14,972** / 9,002 | 924 / 861 | 2,000 |
| `/planning/board/` | 14 | **2,602** / 1,655 | 630 / 392 | 2,000 |
| `/readiness/` | 18 | 1,674 / 1,313 | 737 / 537 | 2,000 |
| `/api/field/sync` (20 commands) | 631 | **1,547** / 1,244 | 510 / 457 | 1,000 |

The first run failed three budgets. Its load average was 9–23, against about 6 for the second run, so much of
the difference is host load, not the fix. Only the CI rounds below count.

## The one optimization: JIT off on the app connection

To profile, the fixture was rebuilt once in a throwaway diagnostic (not committed), timing every statement of one
request:

- **Today:** 2.1 s of SQL. Two statements took most of it:
  - the fleet overview (`operations_robot_status`): 1,024 ms;
  - the issue rules: 845 ms.
- **The fleet overview's `EXPLAIN (ANALYZE)`:** estimated cost 163,738, which is past `jit_above_cost` (100,000).
  JIT compilation took 253 ms of its 654 ms execution.
- **The board's slowest statement** (robot status counts per hub): JIT took 144 ms of 482 ms.

The read models already must never JIT compile. `tests/test_operations_sql.py` and
`tests/test_operations_perf.py::no_jit_plans` assert it, and earlier slices kept the row estimates honest with
`LIMIT 1` / `LIMIT 16` for that reason. At 500 robots that is no longer enough: the plan cost is linear in the
number of robots (per-robot lateral subqueries), so it passes the threshold.

**Fix:** `config/settings.py` adds `-c jit=off` to the connection options, next to the existing `lock_timeout` and
`statement_timeout`. This adds no statement, no index and no read-model change, and the snapshot rules (no
`set_config` inside the snapshot) are untouched. Short OLTP statements and the write triggers gain nothing from
JIT. `test_v2_05_scale.py::test_the_app_connection_never_jit_compiles` pins the setting.

## Not optimized (open)

- **Sync-20: 631 statements per request**, about 31 per command, through `execution/field_sync.py` and the ledger
  functions. That file belongs to another engineer, and the budget passed after the fix. It is the endpoint with
  the least margin under load (1,547 ms at a load average of about 9).
- **Today** remains the heaviest page: 27 statements, about 0.9 s locally after the fix. Its two read-model
  statements scale linearly with the fleet.

## CI rounds (release numbers)

Each cell is the p95 in ms from `perf05.log`. Fill in the run link and the runner load.

| Round | Run | Load before | `/hub/` | Today `/ops/` | `/planning/board/` | `/readiness/` | sync-20 | All within budget |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 45895674016 (`cdb0154`) | see log | 278.9 | 905.1 | 432.7 | 590.0 | 533.1 | Yes |
| 2 | 45895683113 (`cdb0154`) | see log | 168.9 | 738.0 | 344.6 | 504.5 | 397.4 | Yes |
| 3 | 45895692044 (`cdb0154`) | see log | 269.2 | 863.8 | 403.1 | 614.9 | 478.8 | Yes |

All three rounds are within budget, so the verified synthetic scale is 500 robots / 5 hubs / 5,000 visits.
If any round fails a budget after the single fix, publish the last verified scale (contract 15.11), not 500.
