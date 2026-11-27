# v2.0.0 isolated hosted release rehearsal (Railway environment `v2rc`)

EM ruling 2026-11-26: `d58f13a` approved as the v2.0.0 release candidate; only documentation changed after it
(`4afc898`, the `v2/phase03` head). Before anything public changes, the release needs an isolated hosted rehearsal:
migrations, environment, `ops_app` privileges, HTTPS, a 16-item hosted checklist and backup + rollback.

Done 2026-11-27 02:05-02:40 UTC by the release engineer. The public environments (`production`, `staging`,
`northbay`, `phase02rc`) were not changed: no deploy, restart or variable change; their newest deployments are still
from 2026-11-21 to 2026-11-25 and both public demos answered `/healthz` 200 at the end. SYNTHETIC data only; this is
not real-world validation.

## Environment

| Item | Value |
|---|---|
| Railway environment | `v2rc`, id `fc856346-ae1e-47f8-a34a-009e6175dccf`, created with `railway environment new v2rc --duplicate phase02rc` |
| URL | https://web-v2rc.up.railway.app |
| Services | Postgres (`3e9ec2e2`), web (`f7be5809`), demo-refresh cron (`3feadf4e`, `0 12 * * *`, `sh deploy/refresh_demo.sh`) |
| Postgres | image `postgres-ssl:18`, server **18.7** (see issues: the docs say 16), own new volume |
| Database identity | system identifier `7712666685955682363`; `phase02rc` is `7712060547390603323`, `northbay` `7711205319182180411`. Checked before the first deploy: the new cluster held only `postgres` and `railway` |
| Fingerprint | first `humanoid_ops:7712666685955682363`; after the rollback rehearsal the app runs on the restored database: `humanoid_ops_p02:7712666685955682363` (web and demo-refresh) |
| Logins | owner-held file outside the repository; `DEV_SEED_PASSWORD` generated fresh for `v2rc` |

**What the duplicate copied (the known trap, confirmed).** Railway gave the new Postgres a fresh
`POSTGRES_PASSWORD`, but every other secret on `web` and `demo-refresh` was a literal copy of `phase02rc`'s:
`DJANGO_SECRET_KEY`, `DEV_SEED_PASSWORD`, `DB_APP_PASSWORD`, `DB_OWNER_PASSWORD`, `WORKSPACE_BOOTSTRAP_TOKEN`,
`DEMO_DATABASE_FINGERPRINT`, and on `demo-refresh` also the literal `ADMIN_DATABASE_URL`
(`postgres.railway.internal` with `phase02rc`'s superuser password). Fixed before any deploy:

- `web`: the five secrets regenerated; `DEMO_DATABASE_FINGERPRINT` removed until the first build, then set to this
  database's value; `ADMIN_DATABASE_URL` stays the reference `${{Postgres.DATABASE_URL}}`.
- `demo-refresh`: `ADMIN_DATABASE_URL=${{Postgres.DATABASE_URL}}` and the five secrets as references to `web`
  (`${{web.DJANGO_SECRET_KEY}}` etc.), so the two can never drift (both run `bootstrap.py`, which resets the role
  passwords).
- Checked by comparing resolved values: every secret and database URL of `v2rc` differs from every value in
  `phase02rc` and `northbay`; both services' `ADMIN_DATABASE_URL` equal `v2rc` Postgres' own `DATABASE_URL`.

## Deploys (all from clean detached worktrees, `railway up --project ... --environment v2rc --service ...`)

| # | Deployment | Service | Code | What happened |
|---|---|---|---|---|
| 1 | `4ce86108` | web | `48f3298` (tag `v2-phase02-release`) | migrate to Phase 02, `build_demo_workspace` (18.7 s), gunicorn as `ops_app` |
| 2 | `6004b353` | web | `4afc898` | 34 v2 migrations applied by `start.sh`; no seed (database not empty) |
| 3 | `98ddaa92` | demo-refresh | `4afc898` | cron service; did not run on deploy; next run 12:00 UTC against `v2rc` only |
| 4 | `be96054a` | web | `48f3298` | rollback: Phase 02 code on the database restored from the backup |
| 5 | `70bae1b5` | web | `4afc898` | roll forward: 34 v2 migrations on the restored database; **current** |

Deployed code identity: the app has no version endpoint, so the deployed tree was compared file by file. SHA-256 over
every file in `/app` (sorted, without `__pycache__`) = `79ac4614...85ec` for deploys 2 and 5, equal to the
`backend/` tree of `4afc898` (521 files); deploy 4 = `e9263912...3156` = `backend/` of `48f3298` (399 files). The
`backend/` git tree of `4afc898` and `d58f13a` is the same object (`e315d15`); only `README.md` and `docs/` differ.

## Backup (step 2, before the v2 migration)

| Item | Value |
|---|---|
| When | 2026-11-27 02:09:43 UTC, Phase 02 demo built, before deploy 2 |
| How | `pg_dump --format=custom --no-owner` run inside the `v2rc` Postgres container (client = server 18.7; the host has 15.13, which refuses a newer server) |
| File | `/var/lib/postgresql/data/backups/v2rc-phase02-20261127.dump` on the Postgres volume (outside `PGDATA`), 1,075,364 bytes, SHA-256 `e81e332b...16f5`, 101 table-data entries; database 21 MB |
| Time | dump 0.17 s; restore 2.3 s |
| State at backup | 50 robots, 86 visits, 16 stock movements, 9 reservations, 189 maintenance records, 2 runs, 2 authorizations, 3 holds, 1,117 audit rows, gate on |

## Hosted acceptance checklist (EM, 16 items)

Scripted; no password typed into a hosted page. HTTP checks log in through the real `/login/` form
(`07_release_rehearsal/hosted_http_checks.py`); browser checks log in by HTTP and put the session and CSRF cookies
into a Chromium context (Playwright, `07_release_rehearsal/hosted_browser_checks.py`). The full set ran twice on
`4afc898`: after deploy 2 (run "A") and again after the rollback and roll forward on the restored database (run
"B", deploy 5, demo rebuilt). Both runs passed every item.

| # | Item | Result | Evidence (run B unless noted) |
|---|---|---|---|
| 1 | Health 200 | PASS | `/healthz` 200 `{"status":"ok","database":"ok","migrations":"ok"}` after every deploy and throughout the demo rebuilds |
| 2 | Deployment SHA matches `4afc898` (app tree = `d58f13a`) | PASS | file-tree hash above |
| 3 | All migrations applied | PASS | `showmigrations`: 107 applied, 0 unapplied; `migrate --check` clean; latest `planning.0015_crew_same_hub` |
| 4 | `ops_app` privileges | PASS | runtime connection is `ops_app` (not superuser, no CREATEROLE/CREATEDB/BYPASSRLS); refused: UPDATE and DELETE on the audit log, DELETE on stock movements, maintenance records, predictor log (permission denied), DELETE on holds (trigger "holds cannot be deleted"), CREATE TABLE, TRUNCATE robots |
| 5 | Synthetic multi-hub demo loads | PASS | v2 builder: Oakland 49 robots, South Bay 30, Marin 20 (99), 100 visits; Fleet lists OAK, SJC, SRF; fictional labels shown |
| 6 | Sign-in + role switching | PASS | 9 accounts / 8 roles (ops lead, admin, engineering lead, safety reviewer, engineer, founder, hub technician, field operator, Marin technician with safety pause) each land on their page and `/api/auth/me` returns their roles; audit log 200 for the ops lead, 403 for a field operator; account switching on one phone in item 14 |
| 7 | Today and Operations briefing render | PASS | `/ops/` and `/ops/briefing/` 200 for ops lead and engineering lead, no error notice; Today shows the hub filter and "Needs your attention"; the briefing covers Oakland, South Bay, Marin; hub-filtered views 200 |
| 8 | Dispatch board + planning review | PASS | `/planning/board/` 200 with "Planning is not departure"; first "Review options" link (V-0031) 200 with "Why this visit needs attention" and "Options, best first" |
| 9 | Fleet, Hub, Readiness render | PASS | `/fleet/`, `/hub/`, `/hub/charging/`, `/readiness/`, `/execution/` 200 as ops lead; `/hub/` and `/hub/stock/` as hub technician |
| 10 | Field snapshot + sync (a real crew run) | PASS | crew D lead `luis.romero` on the demo's away run EX-0003 (South Bay, NB-201): snapshot 200 in 0.18 s, another hub's technician 403; on the run page "Vehicle arrived at site" -> `POST /api/field/sync` 200, outcome `confirmed`, seq 1; then handover to site (online), site check (queued, confirmed), session start (online) |
| 11 | Service worker over HTTPS | PASS | `navigator.serviceWorker.ready`: scope `https://web-v2rc.up.railway.app/execution/`, script `/execution/sw.js`, `isSecureContext` true, page controlled |
| 12 | Offline recording survives refresh and reconnect | PASS | `context.set_offline(True)`: interruption + observation saved on the phone (2 rows "Saved on this phone"), server unchanged; reload while offline: "Last known at ..." with both rows; reconnect: both confirmed (ordinary seq 3 -> 4, protective interruption IN-0001 open on the server) |
| 13 | Protective banner persists correctly | PASS | offline: "Robot must not operate - Interruption: report saved on this phone, not yet confirmed"; after offline reload: same (state `saved`); after reconnect: "report confirmed by the server ... the session was paused"; after an online reload: still shown |
| 14 | Cross-account offline data isolated | PASS | with sync blocked, Luis records an observation, signs out (dialog: "1 step is not yet confirmed. They stay on this phone for Luis Romero; another person cannot send them."); `derek.osei` (another South Bay crew lead) on the same browser, offline: "This run is not saved on this phone", no robot code; `dana.ortiz` online on the run page: 0 queued rows; server ledger unchanged. Luis signs in again: his kept step is sent once, by him (seq 2 -> 3) |
| 15 | Mobile layout at 375 px | PASS | `scrollWidth` 375 of 375 on Today, Operations briefing, the run page and My visits |
| 16 | Backup + rollback verified | PASS | next section |

Server ledger of EX-0003 after run A (read in the container): 1 `trip.arrived_site`, 2 `site_check.record`,
3 `session.observation` (the step kept across sign-out), protective `interruption.open`, 4 `session.observation`;
the reconnect batch carried the protective command first. Screenshots (375 px, run B):
`07_release_rehearsal/run_375_open.png`, `offline_saved_375.png`, `offline_reloaded_375.png`,
`reconnected_375.png`, `other_account_offline_375.png`, `today_375.png`, `briefing_375.png`.

**Demo reset protection (also checked).** On `v2rc` web, `refresh_demo.sh` with `phase02rc`'s fingerprint:
"Reset refused: DEMO_DATABASE_FINGERPRINT does not match this database (humanoid_ops:7712666685955682363); nothing
done", counts unchanged; with no fingerprint the script refuses before connecting. With the right fingerprint the
v2 rebuild took 63.8 s (first) / 62.9 s / 97.7 s; the site answered the 503 maintenance page for the length of the
rebuild while `/healthz` stayed 200.

## Rollback 4afc898 -> Phase 02 -> 4afc898 (step 5)

No reverse migration was run. The step-2 backup was restored into a **new, empty database** in the same `v2rc`
cluster (runbook section 4: never on top of the live database); the v2 database `humanoid_ops` was left as it was.

1. `createdb -O ops_owner humanoid_ops_p02`, then `pg_restore --role=ops_owner --no-owner --exit-on-error` (2.3 s).
   Restored: 73 migrations (the Phase 02 set), 50 robots, 1,117 audit rows, all 101 tables owned by `ops_owner`,
   221 triggers; `ops_app` has no DELETE on the audit log.
2. `web` and `demo-refresh`: `DB_NAME=humanoid_ops_p02`, `DEMO_DATABASE_FINGERPRINT=humanoid_ops_p02:...363`
   (a restored database has a new fingerprint; set on purpose).
3. Deploy 4 (`48f3298`): "No migrations to apply"; `/healthz` 200; as `dana.ortiz`: Today, Fleet, NB-024, Readiness,
   Planner, Hub, Stock, Charging, Execution all 200 (0.2-1.6 s); `lily.tran`: Hub, Stock 200. All 15 counted tables
   equal the backup state exactly (robots 50, visits 86, stock items 4 / movements 16 / reservations 9, chargers 4,
   charging sessions 1, maintenance records 189, rules 8, work orders 28, predictor 2, authorizations 2, runs 2,
   holds 3, audit 1,117; maintenance gate on). `ops_app` DELETE refusals as before.
4. Deploy 5 (`4afc898`): 34 migrations applied, 0 unapplied, all 15 counts unchanged (no Phase 02 record lost);
   `/healthz` 200; Today, briefing, Fleet, Readiness, board, Hub, Stock, Execution 200.
5. Demo rebuilt with the v2 builder on the restored database (fingerprint check passed), then the full 16-item
   checklist ran again (run B): all PASS.

Result: **PASS**. A failed v2 release can be backed out by restoring the pre-migration dump into a fresh database
and redeploying `48f3298`, then rolled forward again with no data loss in the restored records. Data written after
the backup is lost in that path (expected; the runbook says so).

## Timings (hosted, single requests from the release engineer's laptop; not a load test)

| Page | Run A (database `humanoid_ops`) | Run B (restored `humanoid_ops_p02`) |
|---|---|---|
| Today `/ops/` | 1.77 s (HTTP); browser 2.09 s first in a new context, then 2.02-2.09 s (TTFB 1.77 s) | first request after deploy 5: 1.94 s; then 2.5-2.7 s; browser 3.49 s first, 2.91-3.07 s (TTFB 2.58 s) |
| Operations briefing | 1.88 s; browser 2.14 s first, 2.10-2.19 s (TTFB 1.90 s) | 2.28 s first after deploy; then 2.7-3.0 s; browser 3.12 s first, 3.17-3.35 s (TTFB 3.02 s) |
| Fleet `/fleet/` (99 robots) | 4.44 s | 6.7-6.9 s |
| Readiness | 1.70 s | 2.5-2.6 s |
| Board / Hub / Execution | 0.19-0.25 s | 0.23-0.34 s |

"Cold" here means the first request after a fresh deploy or in a new browser context; the app has no cache, so
cold and warm differ little. Run B was about 45 % slower than run A on the same data shape. `ANALYZE` on the restored
database changed nothing; one statement round trip from `web` to Postgres measured 2.4 ms (`humanoid_ops`) and
3.2 ms (`humanoid_ops_p02`) at the same moment, and these pages issue hundreds of statements, so the hosted time
follows the private-network latency of the moment. CI performance rounds remain the release measure (Today p95
1.2 s, briefing all-hubs 1.98 s at 500 robots); hosted Today ~2-3 s is the known Phase 05 backlog item, and Fleet
at 4-7 s hosted is the slowest page seen here.

## Issues found

| # | Kind | Issue | Action |
|---|---|---|---|
| 1 | Config (fixed in `v2rc`) | `--duplicate` copies every service secret and `demo-refresh`'s literal `ADMIN_DATABASE_URL` from the source environment; only the Postgres password is new | Regenerated; `demo-refresh` now references `web` and `Postgres` instead of literals. Same procedure needed for any future duplicate |
| 2 | Docs / environment | The Railway Postgres image is `postgres-ssl:18` (server 18.7), also in `phase02rc`; the runbook (sections 1 and 4) says the Railway service is PostgreSQL 16 and to use 16 client tools. CI and local Docker run 16, so hosted and tested major versions differ | Runbook wording to fix in the release docs (use the server's major version; the in-container `pg_dump` is the easy path). No code impact seen: migrations, triggers, grants, dump and restore all worked on 18. Whether to pin hosted Postgres to 16 or run CI on 18 is the lead's call |
| 3 | Test harness (not product) | On the hosted CSP (`script-src 'self'`, no `unsafe-eval`) Playwright's `wait_for_function` is refused; at 375 px the user menu is behind the header's Menu button | Script polls with `page.evaluate` and presses Menu first. Run A's first attempt failed on these two only |
| 4 | Observation | Hosted page times vary with network latency between `web` and Postgres (above); Fleet 4-7 s | Recorded; perf backlog (Phase 05 / v2.1), not a release blocker under the EM ruling |
| 5 | Observation (by design) | Offline, a different account on the same phone sees the protective buttons for a run it has not saved (not the run's data) | As designed (a protective report can always be saved; the server decides); no data of the first user is shown |

No application code change was needed. No critical or high issue.

## State left behind

- `v2rc` keeps running (the lead decides whether to delete it): web on `4afc898` (deploy `70bae1b5`) using database
  `humanoid_ops_p02` with the v2 demo; `demo-refresh` on `4afc898`, daily 12:00 UTC, fingerprint for
  `humanoid_ops_p02` only. The earlier v2 database `humanoid_ops` and the backup file stay in the `v2rc` Postgres
  volume (both synthetic; can be dropped with the environment).
- The demo state includes the rehearsal's run EX-0003 at "interruption open"; the next 12:00 UTC refresh rebuilds it.
- The local `backend/` checkout's Railway link now points at `v2rc` (it pointed at `northbay`); every command in
  this rehearsal passed `--project/--environment/--service` explicitly.
- Detached worktrees used for the deploys were removed.
