# 02E closure note: Phase 02 final acceptance

Branch `v2/phase02`. `main` and the public Northbay demo are unchanged. All data is fictional. 02E is integration and
acceptance only: no new features beyond the fixes below.

## Candidate

| Item | Value |
|---|---|
| Code SHA | `c03fa53` (this note and the release-doc evidence lines are docs-only commits on top) |
| 02E diff | `474c785...c03fa53` (21 files) |
| Phase 02 diff | `a213730...c03fa53` (Phase 01 release to candidate) |
| 02E migrations (additive, reversible) | `planning/0009_maintenance_gate_settings_lock`, `fleet/0011_maintenance_open_sessions` |
| Full CI | `c03fa53`: 45592470559 success, 2044 passed, 15 skipped |
| Perf | `c03fa53`: 45592473454, 45592487625, 45592503381, all success |

Perf p95 (ms), three runs on `c03fa53`, 2 s ceiling:

| Page | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| `/hub/` (scale fixture) | 95 | 174 | 149 |
| `/hub/` with charging tasks | 130 | 269 | 171 |
| `/hub/stock/` | 26 | 54 | 38 |
| `/hub/charging/` | 19 | 26 | 25 |
| `/ops/` Today with charging | 271 | 515 | 382 |
| `/ops/health/` | 38 | 72 | 66 |
| `/readiness/` (scale fixture) | 959 | 1,341 | 1,280 |
| `/readiness/` (Northbay demo) | 648 | 891 | 880 |

`/readiness/` on the scale fixture was 972 / 1,280 / 1,204 on 02D (`260abf7`): no measurable change from `fleet/0011`.
It stays above the 400 ms target and under the 2 s ceiling, as before.

## EM 02E scope

1. **Carried tests** (`tests/test_v2_02e_carried.py`), two real gaps found and fixed:
   - (a) Direct runtime SQL racing maintenance-gate activation. **Gap:** an `ops_app` write could read the gate as
     off while the activation was uncommitted, then commit after it. **Fix** `planning/0009`: the three planning
     maintenance triggers take the settings lock in shared mode, then read the gate in a new statement. Both orders
     tested, the waiter seen in `pg_locks`: activation first -> the write waits and is refused; write first -> the
     activation waits, the booking is then shown blocked with its reason (no silent re-plan) and a later confirm is
     refused. A mutation test restores the 0008 bodies and shows the bypass, then restores 0009.
   - (b) A new maintenance record must not hide an unresolved session or required inspection. **Gap:** hours only
     counted runs that left after the record, so a record turned an open session from Unknown to Not due. **Fix**
     `fleet/0011`: runs not yet closed at record time stay in the window; an open session keeps hours Unknown. Tested in
     the eligibility view, Python eligibility, the plan command and `readiness_issue_authorization` (direct SQL and
     command). Required inspection: no gap; a record makes the robot "inspection missing" until a passing inspection
     submitted after it.
   - (c) Issue, consume and reserve for a visit whose run is closed: refused through the service and direct SQL as
     `ops_app`; ledger unchanged (no gap).
   - (d) Cancelled-visit reservations: flagged, one-click release (hub technician, ops lead; others 403) using
     `hub_release_stock`, and each item shows "(N held for cancelled visits)" inside the existing statement; statement
     count constant for 1 and 6 such rows.
2. **J8 end to end** in the extended demo, real browser: see `docs/evidence/02e_acceptance_walkthrough.md`. Charging,
   maintenance due and overdue, inspection due, kit shortage, restock, reservation, assignment and work-order
   completion; completing a task changed no stock or safety state.
3. **J2, J3, J4 re-run** with battery, maintenance and inventory facts: Today shows needs-charge, maintenance-overdue
   Blocked and Kit short; J3 Suggest -> plan saved, Planning feasible; J4 -> DA-0003 approved to leave; a held robot
   gets no approval action.
4. **Desktop and tablet**: 1024 x 768 and 768 x 1024 in a real browser, including refusals (99 gloves: "Nothing was
   changed"; engineer POST 403; held robot not approvable) and role-specific actions (hub technician, engineer, ops
   lead).
5. **Release readiness**: CI and 3 perf runs above; migrations additive with reverse bodies; privilege tests in CI
   (`test_db_security.py`, 02C/02D/02E boundary tests); demo rebuilt twice identically; original safety suites green.
6. **Docs**: `docs/12_v2_phase02_release.md` (limitations, migrations, activation order, backup, rollback),
   runbook section 8d, contract 8.9 (EM ruling on demo practice runs), README.

## Demo (EM ruling 8.9)

Exactly two labelled practice runs per rebuild (home "Fictional practice visit"), through the real guarded commands at
real timestamps; two demo maintenance rules labelled "Demo value, not an engineering requirement"; predictor rows only
through the owner-only logger; refused in production mode. Demo battery-reading freshness changed from 240 to 1,440
minutes (demo value) so readings from the 04:00 rebuild do not all go stale by 08:00.

## Walk defects (all fixed)

- D3 (low): Today "Readiness checked for 10 of 13" next to "Readiness assessed: 12" -> notice reconciles (`62ff1e3`).
- D5 (med): tablet nav hid "Issues"; planner robot select made the page 1,174 px wide -> nav wraps at 701-900 px,
  form labels shrink (`62ff1e3`).
- D6 (high): J3 Suggest returned "No suggestion" for every open request (crews busy) -> demo requests in a free
  evening window, demo test asserts a full suggestion (`a4f2e4c`).
- Health page: the current-configuration caveat was only in code; now on the page, with "Recorded availability is not
  measured uptime" (`b856051`).

## Open items and limits

- Phase 02 -> Phase 01 rollback is documented (turn the gate off first) but **not rehearsed**; it is the first step on
  the isolated deploy.
- `confirm_visit` does not take the settings lock up front; the trigger refuses it fail-closed.
- An old run never stopped and never closed keeps that robot's hours Unknown until reviewed (contract behaviour; may
  surface on old data).
- Health metrics are descriptive; pattern-hold matching and the predictor use current configuration; the predictor
  is observational; no stock API or purchasing; cancelling a visit does not auto-release kits (flagged, one click).
- Walk times are by someone who knows the product. **Usability with independent people: UNVERIFIED.** No real-world
  validation; no offline claim (Phase 03); no 500-robot claim (Phase 05).
