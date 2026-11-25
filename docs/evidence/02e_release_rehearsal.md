# Phase 02 release rehearsal (isolated Railway deployment)

EM ruling 2026-11-25: Phase 02 engineering closed at `c03fa53`; isolated deploy, rollback rehearsal and hosted walk
required before any public cutover. Public Northbay (`web-northbay.up.railway.app`, Phase 01 `a213730`) untouched.

## Isolated deployment

| Item | Value |
|---|---|
| Railway environment | `phase02rc` (id 01d9e8fc-ecf1-41a2-a4af-72e5a12a223b), duplicated from northbay: own Postgres volume, web, demo-refresh cron |
| URL | https://web-phase02rc.up.railway.app |
| Code | `8b1352c` (= tested `c03fa53` + docs-only commits), from a clean detached worktree |
| Secrets | DJANGO_SECRET_KEY, DEV_SEED_PASSWORD, DB_APP_PASSWORD, DB_OWNER_PASSWORD, WORKSPACE_BOOTSTRAP_TOKEN regenerated; DEMO_DATABASE_FINGERPRINT set to this database only; northbay variables unchanged (hash-checked) |
| Database identity | `humanoid_ops:7712060547390603323` (northbay: `humanoid_ops:7711205319182180411`) |
| Backup | Railway manual volume backup 2026-11-25 05:58 EST, 879 MB, before the rollback |
| Deploys | 82054a0c (Phase 02) -> 8ce8868c (Phase 01 rollback) -> bf273754 (Phase 02 forward) |

## Checks

- `/healthz` 200 `{"status":"ok","database":"ok","migrations":"ok"}` on every deploy.
- Demo after first build: 50 robots, 85 visits, exactly 2 practice runs, 2 departure authorizations, 2 predictor rows,
  26 stock movements, 3 holds. Runtime role `ops_app`: DELETE refused on `hub_stock_movement`,
  `operations_predictor_observation`, `fleet_maintenancerecord` (permission denied) and `fleet_hold` (trigger).
- Outbound messaging off (`OUTBOUND_MESSAGES_ENABLED=false`), `DEMO_WORKSPACE=1`.

## Rollback 02 -> 01 -> 02

1. Gate off through the audited owner command (`maintenance_gate_active = false (was true)`).
2. Phase 01 `a213730` deployed on the same additive schema (rebuilt from the tagged source, not a stored image):
   login (dana.ortiz) and Today, Fleet, NB-024, planner, V-0031, readiness list and V-0031, execution: all 200
   (planner 2.2 s, others under 1.1 s); `/hub/` 404 as expected.
3. Phase 02 `8b1352c` deployed again: all 15 counted tables identical to before the rollback; audit +1 (the gate change).
4. Demo rules (8, synthetic, 2 approvals each, none withdrawn) and baselines (only NB-044, NB-049, NB-050 without a
   record, as designed) verified, then the gate re-activated. Hub, Stock, Charging, Today and NB-042 maintenance 200.
5. No reverse migrations were run.

## Hosted journeys (scripted HTTP client through the real forms, CSRF and one-time tokens; no password typed into a browser)

25 checks, 25 s, all passed, run twice (before and after a demo refresh): J8 take/start task, Today shows
maintenance-overdue Blocked (NB-042), needs charge and kit short; V-0034 kit short; receive 2 RK; reserve for V-0034
("departure equipment check is still required"); 99 gloves refused ("Nothing was changed"); charging started
("not a battery check"); SENSOR-CLEAN recorded on NB-042 ("never releases a hold"); task done, stock unchanged
(20 / 19 / 1); engineer sees no stock forms and receive POST 403; J3 Suggest NB-001 / P-111 / P-112 / VAN-1 -> saved,
Planning feasible; J4 inspection RC-0055 -> evidence, equipment, battery 94 % -> DA-0003 approved to leave; V-0078
(NB-019 on hold HD-0001) offers no approval. Layout at 1024 and 768 px was checked in a real browser on the same code
locally (`02e_acceptance_walkthrough.md`); hosted pages were not opened in a browser because hosted sign-in in a
browser would need a typed password.

## Daily demo reset

"Run now" on phase02rc demo-refresh. First run failed safely: the duplicated cron service still had northbay's literal
`ADMIN_DATABASE_URL` password and was refused by phase02rc's own Postgres; nothing was changed anywhere. Fixed by
copying the web service's value. Second run: rebuilt in about 25 s; counts identical to the first build (audit 1,099);
same codes (V-0084 / V-0085 practice runs, NB-003 due soon, NB-042 overdue, V-0034 kit short); hosted walk passed again.
Northbay before and after: 14 users, 50 robots, 83 visits, 787 audit rows, same max id.

A second, mistaken back-to-back walk run on the refreshed data stopped at its first check (NB-042 was already
maintained by the first run) after taking and starting one hub task; the next refresh removes it.

## Not done / limits

- Public cutover (link, `main`, northbay deploy) waits for EM approval of this report.
- Hosted Today p95 not measured here (single requests: Today 1.0-2.6 s cold); perf numbers are from CI.
- Usability with independent people: UNVERIFIED. No real-world validation.
