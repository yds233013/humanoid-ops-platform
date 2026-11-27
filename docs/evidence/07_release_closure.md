# v2.0.0 release closure (public demo cutover on `phase02rc`)

Done 2026-11-27 02:44-03:45 UTC by the release engineer, after the EM approved the cutover and the isolated
rehearsal (`07_release_rehearsal.md`). SYNTHETIC data only: the public demo is the fictional "Northbay Home
Robotics". This release does not certify real-world robot operation.

## Release identity

| Item | Value |
|---|---|
| `main` | `52613c0` (fast-forward from the approved candidate; no force push) |
| Tag | annotated `v2.0.0` -> `52613c0` ("Northbay fleet operations platform v2.0.0"); `v2-phase02-release` (48f3298) kept |
| Approved application candidate | `d58f13a`; everything after it is documentation only. The `backend/` git tree of `52613c0` and `d58f13a` is the same object `e315d15` |
| Main CI | run 45930322577: 2,566 passed, 0 failed |
| Public URL | https://web-phase02rc.up.railway.app (Railway project `d94ba44f-...`, environment `phase02rc` `01d9e8fc-ecf1-41a2-a4af-72e5a12a223b`) |

## Public deployments

| Service | Before (kept for rollback) | Now |
|---|---|---|
| web (`f7be5809`) | `bf273754` (Phase 02, `48f3298` tree) | `5c9dd194` (`52613c0`, from a detached worktree) |
| demo-refresh (`3feadf4e`) | `ad9d033c` | `119ce385` (`52613c0`) |
| Postgres (`3e9ec2e2`) | `3605bdbc` (unchanged; server 18.7) | same |

Deployed tree identity: SHA-256 over every file in the web container's `/app` (sorted, no `__pycache__`) =
`79ac46140219bf0b528f1229f1c69c60d97c848d4f512e5299f169dd40b185ec` (521 files), equal to `backend/` of `52613c0`
(and so of `d58f13a`; the same value was deployed in the rehearsal).

Migrations: `start.sh` applied the 34 v2 migrations; `showmigrations` 107 applied, 0 unapplied; `migrate --check`
clean; `/healthz` 200 `{"status":"ok","database":"ok","migrations":"ok"}`.

## Database identity and references

- Database `humanoid_ops`, PostgreSQL system identifier `7712060547390603323` (the phase02rc database was kept; no
  restore). Fingerprint `humanoid_ops:7712060547390603323` = `build_demo_workspace --print-fingerprint` on the
  deployed web, set on web and demo-refresh.
- web: `ADMIN_DATABASE_URL=${{Postgres.DATABASE_URL}}` (already a reference).
- demo-refresh: was all literals; now `ADMIN_DATABASE_URL=${{Postgres.DATABASE_URL}}` and
  `DJANGO_SECRET_KEY`, `DB_APP_PASSWORD`, `DB_OWNER_PASSWORD`, `WORKSPACE_BOOTSTRAP_TOKEN`, `DEV_SEED_PASSWORD`,
  `DB_NAME` as `${{web.*}}` references. Resolved values checked: both services reach phase02rc's own Postgres and
  database `humanoid_ops`.
- No secret was regenerated: every phase02rc secret was checked unique against production, staging, northbay and
  v2rc (the copies found in the rehearsal were v2rc's, which came from phase02rc). The demo logins are unchanged.

## Cutover window and backup

1. 02:44 UTC demo-refresh suspended (its `DEMO_DATABASE_FINGERPRINT` removed; `refresh_demo.sh` refuses without
   it). 02:45:33 public demo put into the app's own maintenance mode: the build-complete marker
   (`fleet_deploymentsetting` `demo_build_completed_at`, was `2026-11-26T12:03:11.867654+00:00`) deleted, so every
   page and API call answered 503 "The demo is being refreshed" while `/healthz` stayed 200. Audit log unchanged
   (1,098 rows) across the backup.
2. 02:45:47 pre-migration backup `phase02rc_pre_v2_20261127T024545Z.dump` (`pg_dump` 18.7, custom format, inside the
   Postgres container): 1,074,208 bytes, SHA-256
   `3c45cc40276d5d5a8db5e2de808d9ead11883cb4640d78fe8a6eaf2dff6fea6c`, `pg_restore --list` readable (1,836 entries,
   101 table-data). Copies: on the Postgres volume (`/var/lib/postgresql/data/backups/`) and off it in the owner's
   local backup folder (mode 600, directory 700, excluded from git).
3. 03:34 web `52613c0` deployed (site still in maintenance); 03:34:50 v2 demo rebuilt with `refresh_demo.sh`
   (fingerprint check passed, 32 s): Oakland 49, South Bay 30, Marin 20 robots. The build wrote the marker and the
   site reopened. 03:35 demo-refresh fingerprint restored and `52613c0` deployed.
4. After the hosted checks below (which ran a real crew run on the public demo), the demo was rebuilt once more
   (28 s) so visitors start from the clean daily state.

**Rollback reference (EM recipe, not needed):** restore the dump above into a fresh database (runbook section 4;
rehearsed in `07_release_rehearsal.md`: restore 2.3 s, all 15 counts equal), point web and demo-refresh at it with
the new fingerprint, redeploy `bf273754`'s code (`48f3298`). No reverse migrations.

## Hosted checks on the public URL (16 items, same scripts as the rehearsal)

`docs/evidence/07_release_rehearsal/hosted_http_checks.py` and `hosted_browser_checks.py` (Chromium/Playwright
over HTTPS; sign-in by HTTP, cookies injected; no password typed into a page). All 16 PASS.

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | Health 200 | PASS | `/healthz` 200 ok/ok/ok |
| 2 | Deployed tree = `52613c0` (= `d58f13a`) | PASS | tree hash above |
| 3 | All migrations applied | PASS | 107 / 0 pending |
| 4 | `ops_app` privileges | PASS | runtime role `ops_app`, no superuser/createrole/createdb/bypassrls; refused UPDATE/DELETE audit, DELETE stock movements, maintenance records, predictor log, holds (trigger), CREATE TABLE, TRUNCATE |
| 5 | Synthetic multi-hub demo | PASS | 99 robots across OAK/SJC/SRF, 100 visits, fictional labels |
| 6 | Sign-in + role switching | PASS | 9 accounts / 8 roles land on their pages; audit log 200 ops lead / 403 field operator; account switch on one browser in item 14 |
| 7 | Today + Operations briefing | PASS | 200, no error notice, hub filter, all three hubs; 2nd role and hub-filtered views 200 |
| 8 | Dispatch board + planning review | PASS | board with "Planning is not departure"; Review options page with "Why this visit needs attention" |
| 9 | Fleet, Hub, Readiness | PASS | all 200 (ops lead; hub technician for Hub) |
| 10 | Field snapshot + sync, real crew run | PASS | `luis.romero` on EX-0003: snapshot 200 (0.15 s), other hub 403; sync `trip.arrived_site` confirmed seq 1; handover, site check, session start |
| 11 | Service worker over HTTPS | PASS | scope `https://web-phase02rc.up.railway.app/execution/`, secure context, page controlled |
| 12 | Offline survives refresh + reconnect | PASS | 2 steps saved offline, server unchanged; offline reload "Last known at" with both; reconnect confirms both (ordinary seq 3 -> 4, interruption IN-0001 open) |
| 13 | Protective banner | PASS | "Robot must not operate ... saved on this phone, not yet confirmed" offline and after reload; "confirmed by the server" after reconnect and after reload |
| 14 | Cross-account isolation | PASS | sign-out dialog keeps Luis's unsent step for him; `derek.osei` offline: "This run is not saved on this phone"; `dana.ortiz`: 0 queued rows; ledger unchanged; Luis again: step sent once |
| 15 | 375 px layout | PASS | scrollWidth 375/375 on Today, briefing, run page, My visits |
| 16 | Backup + rollback | PASS | backup above; rollback rehearsed in `07_release_rehearsal.md` |

Timings on the public URL (single requests): Today 0.97 s, briefing 1.1 s, Fleet 2.1-2.3 s, Readiness 1.0 s,
board 0.3 s; in the browser Today 1.2-1.3 s and briefing 1.3-1.7 s (TTFB about 1 s).

## Refresh target

demo-refresh runs `sh deploy/refresh_demo.sh` at `0 12 * * *` (12:00 UTC) on deployment `119ce385`, against
phase02rc's own Postgres only (references above), fingerprint `humanoid_ops:7712060547390603323`; a reset with
any other fingerprint is refused and changes nothing (verified in the rehearsal).

## Other demos and environments unchanged

| Environment | Newest deployments (unchanged) | Check now |
|---|---|---|
| northbay (Phase 01 demo, fallback) | web `18c8d611`, demo-refresh `9954def4`, Postgres `958da9dd` (2026-11-23) | `/healthz` 200, `/login/` 200 |
| production (legacy v1 demo) | web `1b78fcf3`, Postgres `04a3c2fb` (2026-11-21) | `/login/` 200 (`/healthz` 404 as before: v1 has none) |
| staging | web `8ec1bc5e`, Postgres `78b65618` (2026-11-23) | `/healthz` 200, `/login/` 200 |
| v2rc (rehearsal, kept) | web `70bae1b5`; demo-refresh redeployed `e856179a` **without** a fingerprint | suspended as the EM asked: its 12:00 UTC run refuses ("not set; nothing done") |

## Known limitations (see `docs/release_notes_v2.md`, "Known limitations")

- Synthetic data only; no real-world robot validation; usability with independent operators UNVERIFIED.
- The daily refresh signs everyone out and deletes visitor changes (maintenance page for ~30-60 s); no continuous
  availability is claimed.
- Hosted Postgres is 18.7 while CI runs 16 (rehearsal and this cutover passed on 18.7).
- Hosted page time follows web-to-database latency; Fleet is the slowest page (2-7 s seen); the all-hubs briefing
  is within 1% of its CI budget at 500 robots (v2.1 follow-up).
- The demo builder's late-day build shows some same-day Oakland visits as Blocked (zone rule; documented).
- The v2rc cron service still has its schedule; it is suspended by the missing fingerprint, not by removing the
  schedule (the CLI did not apply a schedule change). The phase02rc suspension during the window worked the same
  way and is now lifted.

## Final status

v2.0.0 (`52613c0`, application tree `d58f13a`) is the public Northbay demo at https://web-phase02rc.up.railway.app.
All 16 hosted checks passed on the public URL, the pre-cutover backup is verified and kept off the volume, the daily
refresh targets only phase02rc, and the Phase 01 and legacy demos are unchanged. No application code change was
made during the release. Released.
