# Release notes: Fleet Operations Platform v2

**v2.0.0, approved for release by the EM on 2026-11-26** (validated application SHA `d58f13a`). This approval covers a
synthetic robotics operations platform; it is not certification for real-world robot operation.

All data in this project is fictional. No value in the product or the demo is a real robot specification,
maintenance interval, travel time, price or safety approval. Nothing has been used with real robots, real homes or
real crews. Usability with independent people is **UNVERIFIED**: every walkthrough was done by the development
team.

**A 500-robot synthetic benchmark does not establish production capacity for 500 physical robots.**

## Summary

v1 built a safety core: holds, inspections, planning, readiness, departure authorization, custody and incidents,
each enforced in the database. v2 turns it into a product an operations lead can use day to day: a Today page and
briefing, a dispatch board across several hubs, hub work (charging, maintenance, kits, transfers), and a field
screen that keeps working when the phone loses signal, without ever deciding something safety-relevant from data the
server has not checked.

| | |
|---|---|
| Version | v2.0.0 |
| Validated application SHA | `d58f13a` (later commits are documentation only) |
| Demo | https://web-phase02rc.up.railway.app runs v2.0.0 (deployed 2026-11-27; `docs/evidence/07_release_closure.md`) |
| Full CI | Run 45920094281 at `d58f13a`: **2,566 passed, 20 skipped, 0 failed** (skips explained below) |
| Verified synthetic scale | 500 robots, 5 hubs, 5,001 visits; three performance rounds at `d58f13a`, all within budget (45920137597, 45920148983, 45920287628) |
| Integrated acceptance (Phase 06) | 16/16 gates (15 with evidence + EM release approval); 15-step real-browser lifecycle; `docs/evidence/06_integrated_acceptance.md` |
| Known unresolved critical/high defects | 0 |
| Independent operator usability | Unverified |
| Real-robot deployment | Not validated |

## What each phase added

### Phase 00: product contract (documents only)

- Who the product is for (ops lead first; field crew, hub technician, engineering, safety reviewer, founder), the
  core journeys J1 to J8, plain language rules, safety invariants that must never regress, and the phase plan.
- A current-state audit of v1 and a research summary of robot-fleet operations.
- `docs/09_v2_phase00_contract.md`, `docs/09a_robot_fleet_ops_research.md`, `docs/09b_current_state_audit.md`.

### Phase 01: product foundation and onboarding

- **01A:** one design system and navigation (Today, Dispatch, Fleet, Customers, Hub, Issues), correct time zones,
  actions shown only to people who may use them, readable refusals that say what to do and who can do it,
  security headers, error pages, the `/healthz` health check, and the deploy, backup and rollback runbook.
- **01B:** the Today page: what needs attention, readiness coverage, robots and issues.
- **01C:** first-run setup in the browser (bootstrap token, invite, forced password change), and screens for
  customers, homes, consent, people, vans, robots, visit booking and inspections. No seed command or API needed.
- **01D:** every journey usable at 375 px, **My visits** for crews, the **Next step** card on the run page, and the
  current-date demo workspace, rebuilt daily.
- Closure: `docs/evidence/01d_closure_note.md` (CI 1,854 passed at `13dd04e`).

### Phase 02: hub day (released for the demo at tag `v2-phase02-release`)

- **Hub work queue:** tasks with type, due time, owner and status; some derived from records (inspections due,
  holds, charging needs, maintenance, low stock). Completing a task never changes safety state.
- **Charging:** chargers as hub resources, non-overlapping bookings and sessions, battery levels typed in by people,
  a charge forecast per visit ("Enough", "Needs charge", "Unknown"), and At-risk visits.
- **Maintenance gate:** maintenance rules approved by the engineering lead and a safety reviewer; counters from
  records; Overdue, Unknown and missing rules block planning and departure in the database once the gate is on.
- **Kits and stock:** an append-only stock ledger per hub, reservations per visit, "Kit short" visits and restock
  tasks.
- **Fleet health** (descriptive figures with their numerator, denominator and window) and an observational
  **predictor log**.
- Release document: `docs/12_v2_phase02_release.md`. Closure: `docs/evidence/02e_closure_note.md` (CI 2,044 passed
  at `c03fa53`).

### Phase 03: multiple hubs, regional dispatch, offline field use

- **03A, hub ownership and authority:** every robot, van, person, charger, stock item, work order and visit belongs
  to a hub. Hub-scoped roles for crews and hub technicians, enforced in the database for writes and in every page
  for reads. Robot location derived from custody. Audited transfers of robots, vans and stock between hubs, which
  never clear a hold and invalidate earlier approvals. Role grants writable only through guarded functions. A
  catalog-wide audit test of every SECURITY DEFINER function. Closure: `docs/evidence/03a_closure_note.md` (CI
  2,185 passed at `c8fd239`).
- **03B, regional dispatch:** zones per hub and a versioned travel-time matrix (a missing pair blocks the visit);
  hub-aware Suggest and next free time; the **dispatch board** with grouped attention cards and a two-click fix
  through the guarded plan command; **regional pause** of a hub or zone, enforced by the authorization and
  departure gates; a three-hub demo. Closure: `docs/evidence/03b_closure_note.md` (CI 2,282 passed at `5449acd`).
- **03C, field command protocol (server):** `POST /api/field/sync` and `/api/field/abandon`; command classes
  (online only, protective, records); an append-only ledger that applies each key at most once; per-run sequence
  with generations and abandonment; database time for every gate and phone time stored but never trusted;
  protective steps that are never refused for a clock and never stuck behind a gap. An unresolved operator
  interruption blocks the robot's next departure. Closure: `docs/evidence/03c_closure_note.md` (CI 2,417 passed at
  `e24c713`).
- **03D, offline field screen:** a progressive enhancement of the run page with a service worker, an
  account-bound IndexedDB queue, atomic sequence numbers with one sync coordinator per queue, protective steps sent
  first, a "Robot must not operate" banner that stays until the server shows a safe state, reconciliation after a
  skipped step, and safe service-worker updates with unsent steps. The new readiness code
  `ROBOT_INTERRUPTION_UNRESOLVED` replaces a misleading "not at the hub" message. Design review:
  `docs/evidence/03d_design_review.md` (21 states at 1024, 768 and 375 px, real Chromium).

### Phase 04: operations briefing

- `/ops/briefing/` (Today > Briefing): one read-only page that answers, per hub, which robots are unavailable and
  why, which visits are at risk today, which incidents need attention, and what to prioritise, by eight fixed
  rules. Every line links to its source; missing data is said as "Not enough data to say", never guessed.
- It is not an AI and not an agent. There is no language model in the app. It has no forms and cannot act.
- 12 fixed scenarios with prohibited-claim checks: `docs/evidence/04_briefing.md`.

### Phase 05: 500-robot validation

- A synthetic fixture of 500 robots, 5 hubs, 100 crews, 5,001 visits, 1,500 runs, 500 incidents and mixed states.
- One optimization: JIT compilation is turned off on the app's database connection.
- Two product-debt fixes from the EM's list: Today applies the hub scope before its issue-list limit, and plan-form
  resource labels lead with names, not codes.
- Evidence: `docs/evidence/05_scale_validation.md`.

## Verified scale (synthetic)

p95 in milliseconds, three CI rounds at `cdb0154` (`.github/workflows/perf.yml`), 20 timed requests per endpoint,
viewed by a regional ops lead (the widest read):

| Endpoint | Budget | Round 1 | Round 2 | Round 3 |
|---|---:|---:|---:|---:|
| Hub `/hub/` | 2,000 | 278.9 | 168.9 | 269.2 |
| Today `/ops/` | 2,000 | 905.1 | 738.0 | 863.8 |
| Dispatch board `/planning/board/` | 2,000 | 432.7 | 344.6 | 403.1 |
| Departure readiness `/readiness/` | 2,000 | 590.0 | 504.5 | 614.9 |
| Field sync, 20 commands | 1,000 | 533.1 | 397.4 | 478.8 |

All three rounds are within budget. These are whole requests through the Django test client on a shared CI runner,
not a load test with many users, and not a measurement on hosted infrastructure.

**A 500-robot synthetic benchmark does not establish production capacity for 500 physical robots.**

## Tests

| Run | Result |
|---|---|
| Phase 01 closure, `13dd04e` | 1,854 passed |
| Phase 02 closure, `c03fa53` | 2,044 passed |
| 03A closure, `c8fd239` | 2,185 passed |
| 03B closure, `5449acd` | 2,282 passed |
| 03C closure, `e24c713` | 2,417 passed |
| Latest full CI on `v2/phase03`, `81f9d9c` | 2,533 passed, one browser-test setup error (fixed in `3fa3a18`) |
| Release candidate `d58f13a` (run 45920094281) | 2,566 passed, 20 skipped, 0 failed |

**Why 20 tests are skipped in the main CI job** (none is an unexecuted acceptance gate):

| Count | Tests | Reason | Where they run |
|---|---|---|---|
| 9 | `test_operations_perf.py` p95 budgets | marked `perf` | `perf.yml`, three rounds at `d58f13a` |
| 1 | `test_v2_05_scale.py` 500-robot p95 | marked `perf` | `perf.yml`, three rounds at `d58f13a` |
| 1 | `test_v2_03b_readiness_perf_repeat.py` | marked `perf`; 03B repeat measurement | run for 03B evidence (`03b_readiness_perf.md`); readiness is also measured in both `perf.yml` suites |
| 5 | `test_capacity_workbook.py` LibreOffice checks | need LibreOffice | the separate `xlsx` CI job (installs LibreOffice), success |
| 1 | `test_v2_03d_design_review.py` | screenshot generator, only with `HOP_03D_SHOTS=1` | run to produce `docs/evidence/03d_design_review/` |
| 2 | `test_operations_ui.py` | parametrized roles for which metrics is 403, asserted by the test above them | n/a (covered) |
| 1 | `test_s7_ui.py` | legacy guard for a build without `withdraw_hold_policy` | n/a (the function exists) |

The suite runs against a real PostgreSQL 16 (the hosted demos run PostgreSQL 18.7, validated by the deployment
rehearsal; automated PostgreSQL 18 coverage is planned for v2.1). It includes direct-SQL bypass attempts as the restricted runtime role,
concurrency tests in both orders with the waiting transaction observed in `pg_locks`, failure injection on the sync
endpoint, and real-browser tests of the offline screen with the network cut.

## Known limitations

- **Hosted latency (observed on the rehearsal environment, not a controlled benchmark):** Fleet 4.4–6.9 s, Operations
  briefing 1.9–3.0 s, Today 1.8–2.7 s. Known performance debt for v2.1 (with the all-hubs briefing headroom: 18 ms
  in its slowest CI round).
- **PostgreSQL:** CI runs 16; the hosted demos run 18.7, validated by the deployment rehearsal only.

**v2.1 backlog (EM, not part of this release):** P1 hosted Fleet latency; P1 PostgreSQL 18 CI; P2 briefing headroom;
P2 hosted Today round trips; P2 independent operator usability testing; P3 touch targets, naming, refusal page.

**Scope and validation**

- Fictional data only. No real-world validation with robots, homes or crews.
- Usability with independent people: UNVERIFIED.
- Not production-ready on its own: production needs approved procedures, inspection templates, maintenance rules,
  inspection intervals, escalation deadlines and a hold-release policy that do not exist yet. Settings > Production
  readiness lists every blocker; production mode refuses every demo approval.
- No telemetry. Battery levels and robot condition are entered by people; a stale or missing reading is "Unknown".
- No live customer messaging, no automatic safety approvals, no AI.

**Offline field use**

- Offline covers the field run page only, and only steps that record facts or make things safer. Depart, start,
  resume, hand over to the site and recover always need a connection.
- The phone must open the run once with a connection before it can record ordinary steps offline (protective
  reports still work).
- Photos and signatures are not supported (there is no file storage).
- Three of the 21 design-review states (a final protective rejection, a "robot is safe" snapshot, a held step)
  were produced with injected server answers; the behaviour is also covered by tests against the real endpoint
  where possible.
- At 375 px the server part of the run page (the custody table) is still dense.

**Planning, dispatch and hubs**

- Next free time does not consider pauses or charge; final checks run when the plan is saved.
- A zoned home with a missing travel pair is Blocked, by design; there is no fallback estimate.
- Stock in transit between hubs can only be received; it cannot be refused or abandoned.
- Hub work orders keep a Python-only actor check (as designed in Phase 02); every other hub-owned write is checked
  in the database.
- Protective field steps (pause, stop, incidents, interruptions, holds) are accepted from a field role at any hub,
  on purpose, because they make things safer.
- A compromised runtime role could pass a real admin's id as the actor. This is the trust model of every guarded
  function.
- A transaction holding grant locks can delay a revocation; no idle-in-transaction timeout is set.
- Some production-blocker counts are regional (global), not per hub.

**Briefing and Today**

- Readiness is fully evaluated for at most 10 visits per page load; the page says so and shows "Not enough data"
  for the rest.
- The briefing's battery forecast for available robots assumes the robot leaves now.
- Priorities use only the facts on the page; they do not weigh business impact, staffing or customer importance.

**Hub, charging, maintenance and stock**

- Fleet health figures are descriptive, not measured uptime. Pattern-hold matching and the predictor log use the
  robot's current configuration only.
- The predictor log is observational. Nothing reads it to make a decision; it predicts nothing.
- No stock API and no purchasing. Cancelling a visit does not release its kits automatically (flagged, one click).
- An old run that was never stopped or closed keeps that robot's maintenance hours Unknown until reviewed.

**Performance and operations**

- The 20-command field sync runs 631 statements per request (about 31 per command); it has the least margin
  under load.
- Today is the heaviest page; two of its read-model statements grow linearly with the fleet.
- Hosted cold-request latency of Today and readiness (about 2.3 s at the Phase 02 release) is not yet improved.
- Rolling back from Phase 02 to Phase 01 is documented but not rehearsed.
- The demo is rebuilt daily; during the rebuild visitors see a maintenance page and are signed out. Demo build time
  is sensitive to host load.

**EM product-debt list (contract 15.10)**

- Today's issue-list pagination filtered after its limit: fixed in Phase 05 (`7539c84`).
- Plan-form resource labels led with codes: fixed in Phase 05 (`7539c84`).

## Upgrading

Every v2 migration is additive and reversible. Before migrating any database, take a backup and rehearse the
restore, then deploy; `start.sh` runs the migrations before the app serves. New gates (maintenance, stock, zones
and travel times) do nothing until the owner provisions them. Order of work, activation and rollback:
`docs/12_v2_phase02_release.md` and `docs/runbook_deploy_backup_rollback.md`. The upgrade from
`v2-phase02-release` was rehearsed in Phase 06 (34 migrations, data counts and fingerprints equal, reverse and
forward again; backup and restore checked): `docs/evidence/06_install_upgrade_backup.md`.
