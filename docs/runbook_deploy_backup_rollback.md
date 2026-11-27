# Runbook: deploy, health, backup, restore, rollback (v2 Phase 01A)

Status: written 2026-11-21 for 01A (audit D-17, contract section 12.4). Items marked **OWNER ACTION** need someone with
access to the Railway dashboard; they cannot be done from the repository. Nothing here has been applied to Railway by
the engineering branch.

## 1. What runs where

| Piece | Where | Notes |
|---|---|---|
| Web app (`web` service) | Railway, built from `backend/Dockerfile`, started by `backend/deploy/start.sh` | gunicorn, 2 workers, runs as the restricted `ops_app` database role |
| PostgreSQL (16 in CI and local Docker; 18.7 on the hosted Railway demos) | Railway Postgres service (one per workspace) | schema owner `ops_owner` (migrations only), runtime role `ops_app` (no DDL, no audit UPDATE/DELETE) |
| Staging | a separate Railway environment `staging` | its own database, secret key, cookie names and domain, no outbound integrations (contract 12.3) |

Start sequence (`start.sh`): `deploy/bootstrap.py` (roles and database) -> `migrate` as owner -> demo seed **only when
the database has no robots** (with `DEMO_WORKSPACE=1`: `build_demo_workspace` instead, see section 8b) -> gunicorn as
`ops_app`. A new, empty, non-demo deployment is not seeded; its first admin is
created through `/setup/` or `manage.py bootstrap_admin` with `WORKSPACE_BOOTSTRAP_TOKEN` (see section 8).

## 2. Health check

`GET /healthz` (no login, no secrets): `200 {"status":"ok","database":"ok","migrations":"ok"}` only when the database
answers and no migration is unapplied; otherwise `503` with `database: "unreachable"` or `migrations: "pending"`.
(`/api/health` is the older database-ping only check and stays.)

**OWNER ACTION (Railway UI):** service `web` -> Settings -> Deploy -> Healthcheck Path = `/healthz`, timeout 60 s. Railway
then keeps the previous deployment serving until the new one is healthy, and marks a deploy failed if it never is. Migrations
run before gunicorn starts, so a deploy whose migration fails never becomes healthy and never receives traffic.

**Status (2026-11-22).** Staging: Healthcheck Path `/healthz` set (timeout 300 s) and `DJANGO_ALLOWED_HOSTS` extended
with `healthcheck.railway.app` (Railway probes with that Host header; without it Django answers 400 and the deploy fails);
redeployed, "Deployment successful", `/healthz` 200. Production: **not set yet on purpose**: production still runs the v1
code, which has no `/healthz` (404), so a health check now would fail its next deploy.

**Production transition at the 01D release (in this order):**
1. Take a manual Postgres backup (Postgres -> Backups -> New backup) and note its time.
2. Add `healthcheck.railway.app` to production `DJANGO_ALLOWED_HOSTS` (variables only, `--skip-deploys`).
3. Deploy the approved v2 commit to production with the health check still off; confirm `curl /healthz` = 200
   `{"status":"ok",...}` and the login page loads.
4. Set Healthcheck Path `/healthz` on production `web` and redeploy the same commit; confirm "Deployment successful".
5. If step 3 or 4 fails: Rollback to the previous deployment (Deployments -> previous -> Rollback); if a migration ran, follow
   section 5 (additive migrations only, so the v1 code keeps working on the migrated schema) and, only if data is damaged,
   restore the step-1 backup (section 4).

## 3. Daily backup

Two layers; both are needed because a platform backup you have never restored is not a backup.

1. **Railway volume backups (preferred, no code): DONE 2026-11-22** (workspace on Railway Pro): production Postgres Daily
   (kept 6 days) + Weekly (kept 27 days), staging Postgres Daily (kept 6 days); a manual production backup was taken
   2026-11-22 10:59 (174 MB). Point-in-time recovery is off (enabling it restarts the database). Original instruction: In the Railway dashboard open the Postgres service ->
   Backups -> enable scheduled backups, Daily, and (if the plan allows) Weekly. Railway backups are restored from the same
   Backups tab into a new volume; they are not downloadable. Record the plan limits and retention in the owner's notes.
2. **Independent logical dump (belt and braces): OWNER ACTION to schedule.** A Railway cron service (or any scheduler
   outside Railway) runs, once a day, with the Postgres service's connection string as `SOURCE_DATABASE_URL`:

   ```sh
   pg_dump "$SOURCE_DATABASE_URL" --format=custom --no-owner --file "backup-$(date -u +%Y%m%d).dump"
   ```

   and copies the file to storage outside Railway (an S3-compatible bucket with 30 days retention). Keep the dump files
   encrypted; they contain every user's password hash and the audit log. Credentials for the bucket are the owner's.

The existing database rules (audit append-only trigger, singleton constraint, role grants) are part of the dump, so a
restore needs no extra scripting beyond having the roles in the target cluster (`deploy/bootstrap.py` creates them).

## 4. Restore into an isolated database (rehearsal and real recovery)

Never restore on top of the live database. Restore into a **new, empty database** (a new Railway Postgres service, or
another database in a scratch cluster), prove it, then point the app at it.

Script: `backend/deploy/backup_restore_check.sh SOURCE_URL TARGET_URL [FILE]` dumps, restores into an empty target, and
compares row counts (`django_migrations`, `auth_user`, `accounts_rolegrant`, `platform_core_auditevent`,
`platform_core_workspace`, `fleet_robot`, `planning_visit`) plus checks that the audit triggers and the workspace
singleton constraint came across. It refuses an identical or non-empty target and exits non-zero on any mismatch.

Exact steps for a real restore from a dump file `backup-20261121.dump`:

```sh
# 1. a new empty database with the platform roles (target cluster superuser URL in ADMIN_DATABASE_URL)
export ADMIN_DATABASE_URL=postgresql://postgres:...@HOST:PORT/postgres
export DB_NAME=ops_restore DB_OWNER_USER=ops_owner DB_OWNER_PASSWORD=... DB_APP_USER=ops_app DB_APP_PASSWORD=...
eval "$(python backend/deploy/bootstrap.py)"          # creates roles and the empty database $DB_NAME

# 2. restore (privileges are in the dump; roles from step 1 must exist)
pg_restore --dbname "postgresql://ops_owner:...@HOST:PORT/ops_restore" --no-owner --exit-on-error backup-20261121.dump

# 3. verify: migrations current, rows present, app starts as the restricted role
DB_NAME=ops_restore DJANGO_DB_IDENTITY=owner python backend/manage.py migrate --check
curl -s https://<restored-app>/healthz
```

Use client tools of the **same major version as the server** (PostgreSQL 16 locally and in CI; 18.7 on the hosted
Railway demos, so run `pg_dump` inside the Postgres container there): an older `pg_dump` refuses to dump a newer
server. Cut over by changing the `web` service's `DB_HOST`/`DB_PORT`/`DB_NAME` variables (or the Postgres reference
variable) to the restored database and redeploying. **Rehearse quarterly and before every destructive migration**, and
record the date and the script's RESULT line in section 9.

## 5. Migration compatibility strategy

Goal: any release can be rolled back one step without touching the database.

1. **Additive first.** A release only adds tables, nullable columns, columns with defaults, new indexes and constraints
   that existing rows satisfy. Every migration is reversible and `makemigrations --check` stays clean.
2. **Code tolerant of both schemas.** The release that adds a column must work if the column is absent for a moment (old
   workers still running) and the release before it must ignore the new column. No code reads a new column as required
   until the following release.
3. **Destructive steps later.** Dropping or renaming columns/tables, tightening NOT NULL, or removing a constraint
   ships in a later release than the code that stopped using it, with a separately reviewed recovery plan (a fresh
   backup taken and rehearsed-restored just before; the exact reverse steps written down; an owner sign-off).
4. **Data migrations** are idempotent and bounded in time (statement timeout is 10 s for the app role; migrations run
   as the owner but must still be quick; large backfills run as a command, not a migration).
5. `migrate` runs in `start.sh` before the new code serves. A failing migration fails the deploy and leaves the previous
   deployment serving (with the Railway health check enabled).

## 6. Rollback

**OWNER ACTION (Railway UI):** keep previous deployments: Project -> Settings -> retain deployment history (do not purge);
note that the audit of 2026-11-21 found only one deployment listed, so history retention must be confirmed in the dashboard.

Decision table:

| Situation | Action |
|---|---|
| Bad code, migrations were additive (normal case) | Railway -> `web` -> Deployments -> pick the previous successful deployment -> Redeploy. The older code runs on the newer (superset) schema. `migrate` on start is a no-op for it. Check `/healthz` and log in. |
| A migration is **not** backward compatible, or data was damaged | Restore into an isolated database (section 4), verify, then point `web` at it and redeploy the previous image. Data written after the backup is lost; say so to users. |
| Database unreachable | `/healthz` returns 503; Railway keeps the last healthy deployment. Fix or fail over the database; nothing to redeploy. |

Do not run `migrate <app> <older>` on a live database as a rollback; it is for development.

**Rollback rehearsal (record in section 9):** deploy the new release to staging, redeploy the previous image against the
same database, confirm `/healthz` 200, sign in, open Fleet, Planner, Readiness, Ops; then redeploy the new one again.

## 7. One database per workspace: what it costs to operate

Decision (contract section 2): a customer workspace is its own deployment and its own PostgreSQL database. This keeps every
v1 database rule untouched and makes cross-customer data leaks structurally impossible, but the work scales with the number of
customers. Nothing here claims multi-customer provisioning has been proven; today there is one live demo workspace and one
staging environment.

| Task | Per workspace, every time |
|---|---|
| Provisioning | new Railway project/service + Postgres, set env (`DJANGO_SECRET_KEY` unique, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, DB credentials, `WORKSPACE_BOOTSTRAP_TOKEN`), custom domain, health check path; first admin via `/setup/`. Manual today (about an hour); scripting it is future work. |
| Migrations | every release must be rolled out N times; a failure in one workspace must not block the others; order canaries (demo, staging) first. |
| Backups | one schedule and one retention per database; one quarterly restore rehearsal per database (or per cohort on the same plan). |
| Monitoring | one health check, log stream and alert per workspace; uptime pings to each `/healthz`. |
| Recovery | each database is restored separately, so an incident is N independent recoveries, not one. |
| Cost | one Postgres (always-on) plus one web instance per customer, plus the backup storage. |
| Secrets | one secret key, one DB owner and app password and one bootstrap token per workspace; rotate independently. |
| Support | no cross-workspace reporting; fleet-wide analytics would need a separate pipeline. |

Shared-database multi-tenancy is a separate, later decision and not blocked by anything in Phase 01.

## 8. First admin (bootstrap)

`WORKSPACE_BOOTSTRAP_TOKEN` is set once in the deployment environment (long random string, treated as a secret). The first
visitor with the token opens `/setup/`, or an operator runs `python manage.py bootstrap_admin --username NAME --token TOKEN`
(password from `BOOTSTRAP_ADMIN_PASSWORD` or a prompt). It succeeds once: the admin, the admin role and the workspace's
`initialized` flag commit in one transaction under a row lock; after that `/setup/` returns 404 forever, even with the
right token, and the token can be removed from the environment. Setup creates no approvals of any kind. The demo seed
(`seed_dev`) marks the workspace initialized, and the migration marks any existing database that already has users as
initialized, so the live demo can never be claimed.

## 8a. Provision a new workspace (operating settings)

Some settings are not on any screen: only the deployment owner sets them, with a management command (never an
HTTP endpoint). Run it as the schema owner, before the first admin signs in:

```
DJANGO_DB_IDENTITY=owner python manage.py provision_settings \
  --consent-form-version CF-v1 --pre-visit-inspection-min 30 --with-defaults
```

- Allowlist: `consent_form_version`, `pre_visit_inspection_min`, `ops_readiness_lookahead_min` (these three are
  required), `ops_closeout_timezone`, `ops_custody_verify_grace_min`, `ops_return_grace_min`,
  `ops_inspection_grace_min`, `ops_periodic_due_lookahead_min`, `ops_soc_fresh_min`, `maintenance_gate_active`
  (true or false; see 8a-1), `stock_minimums` and `stock_per_visit` (`CODE=N,CODE=N`; see 8d). Anything else is
  refused; `demo_mode` and
  `allow_draft_policies` can never be set this way.
- Values come from options or `PROVISION_<KEY>` environment variables (`PROVISION_CONSENT_FORM_VERSION=CF-v1`).
  `deploy/start.sh` runs the command after migrations when any `PROVISION_*` variable is present.
- `--with-defaults` offers `ops_readiness_lookahead_min = 2880` (today and tomorrow) when it is not given or set.
- It is idempotent: the same value is reported as unchanged. Replacing a different existing value needs
  `--allow-change`. If a required value is missing the command lists it and changes nothing.
- It takes the settings advisory lock exclusively, validates everything before writing, and records one audit
  event. Settings > Operating settings (read-only) shows what is set; /settings/setup/ shows
  "Operating settings incomplete" until the required values exist.

## 8a-1. Maintenance gate rollout (v2 02C)

Maintenance overdue is a database gate for planning and departure (contract 11, sections 3.2 and 8.2). Migrating an
existing database must not stop it from working before any maintenance rule exists, so the gate has an owner switch,
`maintenance_gate_active` (allowlisted in `provision_settings`; absent means inactive):

- **Inactive** (absent or `false`): the maintenance status of every robot is computed and shown (robot page, Hub
  tasks), with "Maintenance gate not yet activated". Planning, readiness and `readiness_issue_authorization` are NOT
  blocked by maintenance. In production mode the inactive gate is itself a production blocker
  (`MAINTENANCE_GATE_INACTIVE` on Production readiness), next to `MAINTENANCE_RULE_NOT_APPROVED` for each
  configuration without an approved production rule.
- **Active** (`true`): fail closed. Overdue, Unknown (no baseline record, or a run with a session start but no stop),
  no approved rule (in production also a demo rule) and a missing required post-maintenance inspection all block
  planning (view, plan command, planning triggers) and authorization; the departure re-checks it.

Order (each step is safe to stop after; nothing is blocked until the last one):

1. `migrate` (fleet 0009/0010, planning 0007/0008, operations 0007, readiness 0009, hub 0006). Nothing changes for
   planning or departure; Production readiness now lists the two maintenance blockers.
2. Engineering proposes the maintenance rules on each configuration page (Fleet > configuration > Maintenance rules:
   requirement code, triggers, warning margin, optional post-maintenance inspection template, effective time,
   `engineering_doc` source with its procedure reference).
3. The engineering lead and a safety reviewer approve each version (two different people, neither the author).
4. The safety reviewer grants `maintenance_technician` to the people who sign maintenance (the existing qualification
   grant: never to oneself, not backdated, with a training-record reference). Only hub technicians, engineers and the
   engineering lead can record maintenance, and only while qualified.
5. Record a baseline maintenance record for every robot and requirement (robot page > Maintenance details and
   records), stamped at the time it is recorded (no back-dated history). Check the robot pages and the Hub queue:
   no robot that should keep working may show Unknown or Overdue, and required inspections are recorded.
6. Activate, as the schema owner:

```
DJANGO_DB_IDENTITY=owner python manage.py provision_settings --maintenance-gate-active true
```

   Activation needs no `--allow-change`. It runs under the settings lock (no plan, authorization or departure
   straddles it) and writes one audit event. Do not leave `PROVISION_MAINTENANCE_GATE_ACTIVE` in the deploy
   environment unless the rollout above is complete: `deploy/start.sh` would apply it after migrations.

Deactivation is a deliberate weakening: `provision_settings --maintenance-gate-active false --allow-change` (refused
without `--allow-change`; audited; shown again as a production blocker). Authorizations already issued are re-checked
at departure in the current gate state, as Slice 7 does for periodic inspections: an authorization issued while a
robot was clear cannot depart once the robot is blocked, and `expires_at` is bounded by the earliest calendar due
time.

## 8b. Demo workspace (v2 01D)

The demo is its own workspace (its own deployment and database, section 7): a fictional company, **Northbay Home
Robotics** (Oakland hub, 50 robots, 3 crews, 40 homes in Bay Area towns, visits from 7 days ago to 14 days ahead of
the workspace's local today). One command builds it, as the schema owner:

```
DJANGO_DB_IDENTITY=owner python manage.py build_demo_workspace           # into an empty, migrated database
DJANGO_DB_IDENTITY=owner python manage.py build_demo_workspace --reset   # destroy the demo data and rebuild
```

- **Logins**: the command prints the usernames (one per role, plus the crews and the hub technician). Every demo
  login's password is the `DEV_SEED_PASSWORD` environment variable; the command refuses to run without it. No
  password is in the repository except the development value in `.env.example`; set a different one on Railway.
- **What it refuses**: a database that holds a non-demo workspace (the workspace demo flag is off, or
  `demo_mode` is not `'true'`); a non-empty demo database without `--reset`; an empty database whose `demo_mode`
  is explicitly not `'true'`. `--reset` itself refuses unless the workspace is a demo workspace **and** the
  deployment-level fingerprint matches (below). It never touches a production workspace.
- **Database fingerprint (required for `--reset`)**: `DEMO_DATABASE_FINGERPRINT` must equal
  `<database name>:<PostgreSQL system identifier>` of the connected database (`current_database()` and
  `pg_control_system()`), and the connection must be to the database the settings name (`DB_NAME`). It is an
  independent check from the deployment: a reset is refused on a mismatch even when `demo_mode` and the
  workspace demo flag are both on (for example a demo cron service pointed at a copied or restored database).
  To set it (OWNER ACTION, demo project only): after the first build, open a shell on the demo `web` service
  (`railway run` or the service's shell) and run
  `DJANGO_DB_IDENTITY=owner python manage.py build_demo_workspace --print-fingerprint` (writes nothing); paste
  the printed value into the variable `DEMO_DATABASE_FINGERPRINT` of the demo cron service (and `web` if you
  run resets from there). A restored or recreated database has a new value: set it again on purpose.
- **One at a time**: the build and the reset take a PostgreSQL advisory lock (try-lock). A second build or
  reset started while one runs is refused at once and changes nothing.
- **What `--reset` destroys**: every row of every application table of that database: users and role grants,
  sessions (everyone is signed out), the audit log, robots, holds, homes, visits, readiness evidence, departures,
  runs, incidents, settings, the workspace row. Only the schema, `django_migrations`, content types and permissions
  stay; the rows the migrations insert are restored and every sequence restarts (so codes repeat for a given
  date). The TRUNCATE runs as the schema owner with the owner's own user triggers disabled for that one statement
  (the documented owner trust boundary, the same one the test teardown uses); the runtime role cannot do this.
  Anything a demo visitor did that day is gone. Take no backup of it: it is fictional.
- **How it writes**: through the existing guarded service commands (permission, audit row, locks, triggers), like
  the seed commands; as the owner only for what has no command (users and role grants, deployment settings, the
  workspace demo flag, the site-check template). No trigger is disabled while data is written. Every approval in it
  is one the demo seeds also create, on `synthetic_demo` records that production refuses (Settings > Production
  readiness still lists every blocker). Apart from the two practice runs (below), no visit of today or later is
  authorized: the visit named in the output
  ("Journey J4: visit V-....") has its plan, crew qualifications, periodic inspection and recovery plan; the crew
  records the day's checks and the ops lead approves in the app.
- **No fabricated history**: past visits (the last 7 days) are booked and confirmed for their planned days and
  nothing more. The build creates no departure authorization, custody transfer, visit inspection, run,
  close-out or other execution record (those commands stamp database time, so they would date a "past" event at
  the build). Screens show such a visit as **Historical scheduled visit**, never as completed. The only records
  stamped at build time are current facts: today's periodic robot inspections (planning eligibility needs
  them), one failed inspection that holds a robot, one maintenance hold, an incident reported this morning, and
  the recovery plan of the J4 visit. The attention item is a beta robot (NB-048) with no periodic inspection on
  its configuration yet, which is true at any time; there is no "overdue return" (it needed a fabricated run).
- **Practice runs (v2 02E, EM ruling in contract 11 section 8.9)**: the one exception to the rule above. Two short
  practice visits at 00:30 local today (crews B and C, home `PRACTICE`, labelled "Fictional practice visit" on the
  visit, readiness and run pages) are run end to end through the real readiness and execution commands and closed
  during the build. Their records are stamped at the build time, never back-dated, so a run reads "departed at
  <build time>" for a visit planned at 00:30. They give the predictor log its departures (the build runs the
  owner-only predictor log once) and the visit-count demo maintenance rules their closed runs: one robot is then
  "Due soon" and one "Overdue" (blocked; its visit tomorrow shows the block). The build output names them.
- **Deterministic** for a date (seeded by the workspace's local date); about 25 s on a laptop.
- **First start**: set `DEMO_WORKSPACE=1` and `DEV_SEED_PASSWORD` on the demo `web` service. `start.sh` runs
  `build_demo_workspace` once when the database has no robots. If that build is refused or fails, `start.sh`
  exits non-zero and the app does not start (Railway shows a failed deploy): it never serves a half-built demo.
  Fix the cause (usually a database that is not empty or a missing `DEV_SEED_PASSWORD`) and redeploy.
  Without `DEMO_WORKSPACE=1` the start sequence is unchanged.

**Daily refresh (OWNER ACTION, demo environment only).** Visits are placed relative to the build date, so the demo is
rebuilt every day. On Railway, add a **cron service** to the demo project only:

1. New service from the same repository and `backend/Dockerfile`, same variables as `web` (reference them:
   `ADMIN_DATABASE_URL`, `DB_*`, `DJANGO_SECRET_KEY`, `DEV_SEED_PASSWORD`, `DEMO_WORKSPACE=1`) plus
   `DEMO_DATABASE_FINGERPRINT` (see above).
2. Settings > Deploy > Custom start command: `sh deploy/refresh_demo.sh`.
3. Settings > Cron schedule: `0 12 * * *` (Railway cron is UTC: 04:00 in winter, 05:00 in summer in California).
4. No public domain, no health check (it exits when done). Check its logs after the first run: the last lines are
   the timings and the login list.

`deploy/refresh_demo.sh` refuses unless `DEMO_WORKSPACE=1` and `DEMO_DATABASE_FINGERPRINT` are set, and the command
refuses unless the database is a demo workspace whose fingerprint matches, so a cron service copied by mistake into a production project does nothing. Never add it to a customer
workspace or to staging when staging holds anything but the demo. During the rebuild (about half a minute) pages
may error or show partial data; users are signed out.

**What visitors see during a refresh (no continuous availability is claimed).** With `DEMO_WORKSPACE=1` the
app serves a maintenance page (HTTP 503, `Retry-After: 60`, "The demo is being refreshed. Please try again in a
minute."; `/api/` answers `{"error": "demo_refreshing"}`) whenever the build's completion marker is missing.
`build_demo_workspace` deletes the marker first and writes it as its very last step
(`fleet_deploymentsetting` key `demo_build_completed_at`); `--reset` truncates it with everything else. So:

- A daily refresh signs everyone out, deletes every change visitors made, and shows the maintenance page for
  the length of the rebuild (about 20-40 s), never a half-built workspace.
- A refresh that fails part-way leaves the marker absent: the maintenance page stays until a rebuild
  succeeds. Re-run it from the demo cron service (Railway: the service's "Run now", or redeploy it) or from a
  shell on the demo `web` service: `DJANGO_DB_IDENTITY=owner python manage.py build_demo_workspace --reset`
  (with `DEV_SEED_PASSWORD` and `DEMO_DATABASE_FINGERPRINT` set). Check the cron logs for the cause first.
- `/healthz` is deliberately NOT affected: it keeps reporting the database and the migrations, so Railway does
  not restart or roll back the service while the demo is being rebuilt. A green health check therefore does
  not mean the demo is browsable; the maintenance page is the signal.
- Without `DEMO_WORKSPACE=1` (every customer workspace) the marker is never read and nothing changes.

## 8c. Public demo: Northbay environment (2026-11-23)

- Railway environment `northbay` in the same project (duplicated from staging configuration): its own Postgres,
  its own secrets (secret key, database passwords, bootstrap token, demo password), `DEMO_WORKSPACE=1`,
  `OUTBOUND_MESSAGES_ENABLED=false`, `DJANGO_DEBUG=false`, health check `/healthz`,
  `DEMO_DATABASE_FINGERPRINT` = the value `build_demo_workspace --print-fingerprint` printed on that database.
  URL: https://web-northbay.up.railway.app . Deployed from a detached checkout of the approved commit.
- Cron service `demo-refresh` in the same environment: start command `sh deploy/refresh_demo.sh`, schedule
  `0 12 * * *` (12:00 UTC, 04:00 Pacific). Rehearsed with a temporary `*/5` schedule: the site answered 503
  ("being refreshed") for about 16 s, then 200.
- The legacy demo (`production` environment, https://web-production-3a92454a.up.railway.app) and its database are
  left unchanged.
- **Rollback of the cutover:** point the published demo link back at the legacy URL. Nothing in Railway needs to be
  switched; both deployments keep running. A code rollback of the Northbay service uses section 6.
- Logins: owner-held file, never in git. A refresh recreates the same usernames with the `DEV_SEED_PASSWORD` value.

## 8d. Phase 02 release (v2 02E): migrate, activate, roll back

What Phase 02 adds, its limits and its migration list: `docs/12_v2_phase02_release.md`. This section is the order
of work. Sections 3 to 6 and 8a-1 still apply; nothing here replaces them.

**Before migrating (every database: production, staging, each customer workspace):**

1. Manual Railway backup (Postgres > Backups > New backup); write down its time.
2. Logical dump (section 3, item 2).
3. Restore rehearsal of that dump with `deploy/backup_restore_check.sh` (section 4); record the RESULT line in
   section 9.

**Migrate.** Deploy the approved Phase 02 commit. `start.sh` runs `migrate` (accounts 0004, hub 0001-0004 and
0006-0009, fleet 0009-0011, planning 0007-0009, operations 0007-0008, readiness 0009; all additive; 02E added
`planning/0009_maintenance_gate_settings_lock` and `fleet/0011_maintenance_open_sessions`). Check
`/healthz` = 200 and sign in. Nothing is blocked yet: the maintenance gate is off, and stock settings are absent
("No minimum set", "not checked per visit").

**Activate maintenance:** section 8a-1, steps 2 to 6 (propose rules -> approve -> grant `maintenance_technician` ->
baseline records -> `provision_settings --maintenance-gate-active true`).

**Activate stock (owner, then the hub):**

1. `DJANGO_DB_IDENTITY=owner python manage.py provision_settings --stock-minimums "RK=3,RC=2" --stock-per-visit "RK=1"`
   (example values; use the company's). Upcoming visits show "Kit short" until stock is received. That is a
   warning only.
2. An ops lead or admin creates the items on Hub > Stock > Items, with the same codes.
3. A hub technician or ops lead receives the counted initial stock on Hub > Stock.
4. Check Hub > Stock, the Hub queue and Today.

**Charging:** provision `ops_soc_fresh_min` (8a) and record the chargers (Hub > Charging > Chargers).

**Predictor log (optional):** a daily owner job `DJANGO_DB_IDENTITY=owner python manage.py log_predictor_signals`.

**Roll back to Phase 01 (not rehearsed yet; rehearse on staging first and record it in section 9):**

1. While Phase 02 still runs: `provision_settings --maintenance-gate-active false --allow-change`. The gate is in
   the database; Phase 01 code has no maintenance screens and its `provision_settings` does not know the setting.
2. Note kits reserved or issued for visits (Phase 01 has no stock screens; the ledger stays in the database).
3. Redeploy the previous image (section 6). Check `/healthz`, sign in, open Today, the planner and readiness.
4. Restore the backup only if data is damaged (section 4). Never run `migrate <app> <older>` on a live database.

**Demo:** the Northbay demo takes the Phase 02 code only after the Phase 02 release approval. Deploy it like any
release, then run the demo cron once ("Run now") so the rebuild includes the Phase 02 facts (8b).

## 8e. Public demo: Phase 02 (2026-11-25)

- Railway environment `phase02rc` (duplicated from `northbay`, then every app secret regenerated and each service's
  `ADMIN_DATABASE_URL` checked against its own Postgres): web, Postgres, cron `demo-refresh` (12:00 UTC).
  URL: https://web-phase02rc.up.railway.app . Code: tag `v2-phase02-release` (48f3298; application code c03fa53).
  `DEMO_DATABASE_FINGERPRINT` = `humanoid_ops:7712060547390603323` on web and demo-refresh.
- The Phase 01 demo (`northbay`, https://web-northbay.up.railway.app) and its database, cron and secrets are kept
  unchanged as the fallback.
- **Rollback of the cutover:** point the published demo link back at the Phase 01 URL. Do not change or restore the
  Phase 02 database. Release evidence: `docs/evidence/02e_release_rehearsal.md`.

## 9. Rehearsal log

| Date | What | Result |
|---|---|---|
| 2026-11-21 | `backup_restore_check.sh` on the local Docker PostgreSQL 16: source database migrated to head with `seed_dev` and `seed_fleet` data (53 migrations, 7 users, 7 role grants, 40 audit events, 5 robots), restored into a second empty database | PASSED: every row count equal, audit append-only triggers and workspace singleton constraint present in the restored copy; refused a second run against the now non-empty target (exit 2); the app started on the restored database as the restricted `ops_app` role: `/healthz` 200 `{"status":"ok","database":"ok","migrations":"ok"}`, login and `/fleet/` 200 |
| 2026-11-23 01:4x UTC | Restore of a Railway volume backup into the **staging** database (Postgres -> Backups -> scheduled "Daily" backup of 2026-11-22 20:39 PST / 2026-11-23 01:39 UTC, 175 MB -> Restore -> review staged change: volume swap only, the old volume kept -> Deploy). Before: 93 audit rows, 8 users; after the backup an inactive marker user `restore_marker_after_backup` was created (9 users). | PASSED: after the restore the marker is gone, 8 users, 93 audit rows (exactly the backup state); `/healthz` 200 `{"status":"ok","database":"ok","migrations":"ok"}` with no app redeploy needed. Counts read inside the staging web container (`railway ssh`, runtime role). |
| (pending) | Rollback rehearsal on staging (section 6) | not done; needs the staging environment |
| 2026-11-25 10:5x-11:2x UTC | **Phase 02 isolated release rehearsal** on Railway environment `phase02rc` (duplicated from northbay; own Postgres, web and demo-refresh services; every app secret regenerated; DB fingerprint `humanoid_ops:7712060547390603323`, northbay is `...7711205319182180411`). Manual volume backup 2026-11-25 05:58 EST (879 MB) before the rollback. | PASSED. Phase 02 `8b1352c` (deploy 82054a0c): `/healthz` 200, demo built (50 robots, 85 visits, 2 practice runs, 2 authorizations, 2 predictor rows), `ops_app` refused DELETE on stock ledger, predictor log, maintenance records and holds. Rollback: `provision_settings --maintenance-gate-active false --allow-change` -> Phase 01 `a213730` (deploy 8ce8868c, rebuilt from the tagged source) on the same schema: `/healthz` 200, login, Today, Fleet, robot, planner, visit, readiness list and visit, execution all 200; `/hub/` 404 (Phase 01 has none). Roll forward `8b1352c` (deploy bf273754): all 15 table counts equal (stock 26 movements / 19 reservations, 1 charging session, 189 maintenance records, 2 predictor rows), audit +1 (the gate change only); 8 demo rules synthetic with 2 approvals each, none withdrawn; robots without a baseline exactly NB-044, NB-049, NB-050 (intended) -> gate re-activated. No reverse migrations. |
| 2026-11-25 11:2x UTC | phase02rc demo-refresh cron "Run now" | First run FAILED safely: the duplicated cron service kept northbay's literal `ADMIN_DATABASE_URL` password and was refused by phase02rc's own Postgres (no other database touched). Fixed by copying the web service's value; second run rebuilt in ~25 s with identical counts (audit 1,099 = first build) and the same codes; northbay counts unchanged (14 users, 83 visits, 787 audit rows, same max id). Lesson: after duplicating an environment, check every service's database URL, not only web's. |

## Staging rollback rehearsal (2026-11-21, Railway staging)

| Step | Railway deployment | Code | Login + /ops/, /fleet/R-01/, /readiness/visits/V-0001/, /planning/ | /healthz | 01A CSP header |
|---|---|---|---|---|---|
| Start | e0980a4f (SUCCESS) | v2/phase01 6ed7a2e | all 200 | 200 | present |
| Roll back | d3db9fdb (SUCCESS) | previous release main 5ea5f09, on the database already migrated by 01A (`platform_core.0003`) | all 200 | 404 (old code has none) | absent |
| Roll forward | 657c07b6 (SUCCESS) | v2/phase01 6ed7a2e | all 200 | 200 | present |

Result: the 01A migration is additive and backward compatible: the previous release starts and serves every core
page on the migrated database, and rolling forward again works. Note: `railway up` from a directory that is not
linked to the project deploys nothing; pass `--project <id> --environment <env> --service web` explicitly.

## 10. PostgreSQL versions and hosted performance (v2.0.0)

- **PostgreSQL 16** is the primary automated validation environment (CI and local Docker).
- **PostgreSQL 18.7** is what Railway runs for the hosted demos (`phase02rc`, `v2rc`). It was validated through the
  v2.0.0 deployment rehearsal: installation, 34 migrations, runtime privileges, hosted smoke and real-browser offline
  checks, backup restore, rollback and roll-forward (`docs/evidence/07_release_rehearsal.md`).
- Automated regression coverage on PostgreSQL 18 is planned for v2.1. There is no claim of comprehensive automated
  PostgreSQL 18 compatibility yet.
- **Hosted latency (observed, not a controlled p95 benchmark):** Today 1.8–2.7 s, Operations briefing 1.9–3.0 s,
  Fleet 4.4–6.9 s on `v2rc`. Accepted as known performance debt for v2.0.0; v2.1 investigates database round trips,
  query time and web-to-database latency separately (network latency is a plausible contributor, not an established
  cause).

