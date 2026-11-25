# 02D closure note: kit inventory and fleet health

Branch `v2/phase02`. `main` and the Northbay demo are unchanged. All data is fictional.

## Candidate

| Item | Value |
|---|---|
| Final SHA | `bacdce6` |
| What it contains | 02D-b fleet health + predictor log (6 commits `192704a`..`056a604`, merged as `8ece61a`), 02D-a kit inventory (7 commits `f3d0ddc`..`770856f`, merged as `d11b713`), walk fixes `62a96c4` (merged as `260abf7`), CI flake fix `bacdce6` |
| Diff | `b94bbdf...bacdce6` |
| Migrations (additive) | `operations/0008_predictor_log` · `hub/0007_stock` · `hub/0008_stock_write_boundary` · `hub/0009_stock_visit_and_units` |
| Perf on `d11b713` | 3/3 green (45573674233, 45573678045, 45573682504) |
| Perf on `260abf7` | 3/3 green (45574680379, 45574684727, 45574688527) |
| Full CI | `260abf7`: 1 failed, 2029 passed (pre-existing oracle ordering flake, below); fixed in `bacdce6`, CI on `bacdce6` success (45580403925, 2030 passed) |

Perf p95 on the 200-robot fixture, three runs on `260abf7`: `/hub/` 76 / 164 / 149 ms; `/hub/` with charging tasks 203 / 260 / 250 ms; `/hub/stock/` 56 / 43 / 44 ms (19-21 statements); `/hub/charging/` 48 / 31 / 26 ms; `/ops/health/` 51 / 77 / 64 ms; `/readiness/` 972 / 1,280 / 1,204 ms. All under the 2 s ceiling.

## EM acceptance for 02D

1. **Audited inventory ledger.** `hub_stock_item` (kind `reusable` or `consumable`, created only through a function so the kind never changes), `hub_stock_reservation` (one active reservation per item and visit, stable id; reserving again sets the quantity and writes the difference), append-only `hub_stock_movement` (receive, adjust with a reason, reserve, release, issue, return reusable only, consume consumable only). Every movement writes an audit row. Updates, deletes and truncates of movements are refused for every role; items and reservations cannot be deleted.
2. **Atomic quantities.** On hand, reserved and available are always sums over the ledger; there is no counter column. Every write function takes the item row lock, re-reads the totals and refuses when on hand or available would go below 0. Two simultaneous reservations with too little stock: exactly one succeeds, both orders, the waiter is seen in `pg_locks`. Also tested: reservation against issue and against a write-off.
3. **Trusted write boundary.** Eight SECURITY DEFINER entry functions (owner `ops_owner`, fixed search_path, EXECUTE revoked from PUBLIC, granted to `ops_app` only): `hub_create_stock_item` (admin, ops_lead) and receive, adjust, issue, consume, return, `hub_reserve_stock`, `hub_release_stock` (hub_technician, ops_lead, per contract 8.6). Helper functions are not SECURITY DEFINER and not executable by `ops_app` or PUBLIC. `ops_app` has SELECT only on the three tables and no sequence use. Direct SQL as `ops_app` with a forged, NULL or empty actor writes nothing (tests).
4. **Visit integration.** Owner settings `stock_minimums` and `stock_per_visit` (via `provision_settings`; absent = "No minimum set" / "not checked per visit"). A visit is "Kit short" when stock cannot cover it, filling visits in departure order over the next two days; shown as At risk on Today and the visit page. Reserving, issuing and returning kits leave readiness evidence, authorizations, holds, custody and eligibility unchanged (test); every success message says "The departure equipment check is still required."
5. **Hub workflow.** Below minimum (on available stock) derives a restock task with a stable source ref (`item:movement`), owner, due time (next departure that takes the item) and a link to the Stock page; refreshing never duplicates it. Completing or cancelling a work order never changes stock (test).
6. **Fleet health** (`/ops/health/`, permission `operations.metrics`, one read-only snapshot, 7 / 28 / 90 days or explicit dates). Uptime share = (in-service time - time under a blocking hold) / in-service time, from recorded lifecycle changes, prorated for robots put in service mid-window, overlapping holds counted once; holds by source (open vs released); incidents by event type and latest severity ("unclassified" if none); repeat failure = same robot, two or more incidents of the same event type in the window. Each metric shows numerator, denominator, window and a "Does not establish" note; zero denominators show N/A. Oracle tests recompute every figure in plain Python on hand-built worlds (hold across the window start, robot in service mid-window and taken out, released, open, non-blocking and pattern holds, repeat vs not repeat). 11 statements per request, constant in fleet size.
7. **Predictor log only.** Append-only `operations_predictor_observation`, written only by the owner-only `operations_log_predictor_signals()` (command `log_predictor_signals`; one row per departure; battery at start or Unknown, open holds, open incidents, maintenance overdue, with source refs). Fingerprints of every other table are identical before and after logging; eligibility, maintenance status, holds and readiness evaluation are identical and authorization is then issued normally; no planning, readiness, fleet, hub, execution, accounts or capacity code references the log (tests). The page shows "Observational only; not used for any decision."

## J8 walk (scripted, real forms, local Northbay demo, lily.tran)

The browser pane was hidden, so the walk used a scripted HTTP session through the real pages and forms (CSRF and one-time form tokens), as accepted for 02C. On `d11b713`: received 2 RK ("Stock received and added to the ledger."), reserved 1 RK for upcoming visit V-0026, issued it, returned it, used 1 GLOVE; refusals "Only 0 kits ... are out with that visit, so 5 cannot come back. Nothing was changed." and "Only 16 kits ... are on hand. Nothing was changed."; Hub showed "Restock Nitrile gloves (GLOVE): below its minimum of 4"; an engineer can view Stock but a receive POST is refused (403).

Two defects found and fixed in `62a96c4`:
- **D1:** the visit selects offered visits already over; reserve refused such a visit but issue accepted it. Now issue and consume for a visit use the same database check as reserve (not cancelled, not over, run not closed); returns stay allowed for kits actually out with a visit; selects offer upcoming visits only (returns: visits with kits out). 5 new tests.
- **D2:** "1 boxes": counts of 1 use the singular unit on the page and in database refusal messages.

## Tests

`tests/test_v2_02d_inventory.py` (37 tests) and `tests/test_v2_02d_health.py` (11 tests); updated exact-list tests: hub_technician permission sets (02A, 02B), hub function list (02B follow-ups), SELECT-only tables in `test_db_security.py`. Statement counts: `/hub/stock/` 19 (budget 20, constant from 2 to 14 items); `/hub/` 11 (constant from 2 to 12 restock tasks); `/ops/health/` 11 (constant).

## CI history

CI on `260abf7` (run 45574677385): 1 failed, 2029 passed, 15 skipped. The failure was the Slice 6 scale oracle (`test_scale_fixture_oracle_and_explain_without_growing_seq_scans`): robot 1297 had four incident holds, the SQL view lists them `ORDER BY h.id`, but the Python eligibility query (`fleet/services.py`) had no ORDER BY, so it returned heap order. Pre-existing, not 02D code; it can also change the order of reasons shown on pages. Fixed in `bacdce6` (robot and configuration holds `order_by("id")`); CI 45580403925 success, 2030 passed; `tests/test_operations_sql.py` 122 passed locally. `bacdce6` touches only that query, so the perf runs on `260abf7` stand.

## Open items and limits

- There is no stock JSON API (none existed); the role "no" tests cover services and page POSTs.
- Cancelling a visit does not release its stock; the Stock page flags such reservations ("Visit cancelled: release this").
- The closed-run refusal for issue/consume has no dedicated test (building a trip closure in a test is heavy); the check is in `hub/0009`.
- Pattern-hold matching on the health page and the predictor's maintenance status use the robot's current configuration (configuration history is not reconstructed); stated on the page.
- The demo has no departures, so the predictor section shows "Nothing logged yet" until `log_predictor_signals` runs.
- Carried to 02E by the EM: a concurrency test of direct runtime SQL racing maintenance-gate activation, and a test that a new maintenance record does not conceal an unresolved incomplete session or required inspection.
- Usability with independent people: UNVERIFIED.
