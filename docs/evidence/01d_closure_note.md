# 01D closure note (Phase 01 final acceptance)

Branch `v2/phase01`. `main` and the live demo are unchanged. Demo data is fictional.

## 1. Candidate

| Item | Value |
|---|---|
| Final SHA | `13dd04e` (app code identical to `6594066`; `13dd04e` changes one test only) |
| Diff since `8f52e5b` | 12 files, +201 / -22: P2-1..P2-5 fixes (`0a464b0`), runbook restore rehearsal (`0ca1812`), pass-2 evidence (`b0297ed`), Today landing test (`13dd04e`) |
| Full CI on `13dd04e` | run 45103415316: success, 1854 passed, 11 skipped, 0 failed (lint, format, mypy, makemigrations --check, xlsx included) |
| Previous full CI | `6594066`: 1853 passed, 1 failed (the Today landing test still expected `/ops/` for field operators; fixed in `13dd04e`, an intended J5-1 change) |
| Staging | `6594066` deployed (Railway deploy `8ec1bc5e`, health check `/healthz` passed) |

## 2. Performance (200-robot fixture, p95 ms)

| Path | Ceiling | `13dd04e` runs 45103416521 / 45103422464 / 45103428580 | `6594066` runs 45099815996 / 45099822869 / 45099828247 |
|---|---|---|---|
| Today `/ops/` | 2,000 hard; 400 target | 517 / 523 / 360 | 516 / 516 / 521 |
| Exceptions inbox with readiness | 2,000 | 878 / 895 / 670 | - |
| Timeline | 2,000 | 817 / 854 / 615 | - |
| Fleet overview | 400 | 150 / 152 / 99 | - |
| Metrics | 400 | 205 / 207 / 147 | - |

The Today 2 s ceiling is met in 6 of 6 runs. The 400 ms optimization target is met in 1 of 3 on the final SHA (2 of 3 on `8f52e5b`). Today's
p95 rose about 100-150 ms since 01B: two fixed reads per request (display zone, password flag) and the larger Today
(attention link, coverage). Not optimized in 01D; the target stays open.

## 3. Acceptance (browser, desktop 1280 and phone 375)

Scripted by someone who knows the product (lower bound), on a local server of the candidate (Claude does not type
passwords into hosted sites); `docs/evidence/01d_acceptance_walkthrough.md` has passes 1 and 2.

| Journey | Result |
|---|---|
| J1 setup from empty, bootstrap token, invite, forced first-login password change | 56 s |
| J2 what needs attention today | < 10 s |
| J3 book a visit, Suggest, save, confirm, no code guessing | 44 s |
| J4 get a visit to "Approved to leave" | 32 s |
| J5 field visit on a phone (online), My visits -> departure -> Next-step card -> hub receipt -> post-visit inspection | 138 s |
| J5 ops lead part: reconcile return and close the trip on the website | done ("Run: Closed", "Nothing left to record") |

All measured pages fit 375 px. All 14 pass-1 defects and P2-1..P2-5 fixed and re-checked in the browser (P2-1 and
P2-2 by tests; P2-3 signer names and P2-5 phone wrapping seen in the browser). P2-6 (suggest the next free time) moved
to Phase 03 as an explicit acceptance item (EM ruling).

**Usability study: UNVERIFIED.** No independent participants; no participant results are claimed.

## 4. Safety and regression

Full suite on `13dd04e` (see section 1) includes the unchanged v1 safety suites (authorization, custody, holds,
incidents, Slice 7 guardrails, direct-SQL bypass tests). Changes to safety code in 01D: one wording fix in
`readiness/evaluator.py` ("Record a" -> "Record an"), no gate outcome change. Suggest is read-only and uses
`planning.services.evaluate`; the guarded plan command still decides. No automatic approvals.

## 5. Demo workspace

`build_demo_workspace` (Northbay Home Robotics, 50 robots, 1 hub, 3 crews, current-date visits, no fabricated
execution history). Reset refuses without the database fingerprint, refuses a concurrent refresh, and a failed initial
build stops startup: `tests/test_v2_01d_demo.py` (12 tests) green in the full suite. Staging still holds the earlier
seed data; the demo build runs on a deployment with `DEMO_WORKSPACE=1` (runbook 8b).

## 6. Release operations

| Item | Evidence |
|---|---|
| Scheduled backups | Railway Pro: production Daily (6 d) + Weekly (27 d); staging Daily (6 d); the staging daily backup ran on schedule (2026-11-23 01:39 UTC, 175 MB) |
| Restore | Railway backup restored into staging: a marker user created after the backup disappeared, 8 users and 93 audit rows (backup state), `/healthz` ok (runbook section 9); earlier logical-dump restore rehearsal also passed |
| Health check | staging `/healthz` health check on (deploy passed with it); production switch plan in runbook section 2: manual backup -> allow `healthcheck.railway.app` -> deploy v2 with the check off -> verify -> turn the check on and redeploy; old deployment keeps serving until the new one is healthy, so no planned outage |
| Rollback | previous deployments retained with Rollback; staging rollback rehearsed 2026-11-21 |
| Staging smoke (`6594066`) | `/healthz` ok; CSP, HSTS, X-Frame-Options DENY, nosniff, Referrer-Policy; login 302; `/ops/`, `/planning/`, `/readiness/`, `/execution/`, `/execution/mine/`, `/planning/sites/`, `/fleet/`, `/ops/exceptions/`, `/settings/operating/` all 200 in < 0.6 s |

## 7. Boundaries kept

No live customer messaging; no automatic safety approvals; no physical-safety or real-world claims; J5 online only
(offline is Phase 03); no 500-robot claim (Phase 05); no production release on synthetic data alone.
