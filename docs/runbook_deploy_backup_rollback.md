# Runbook: deploy, health, backup, restore, rollback (v2 Phase 01A)

Status: written 2026-11-21 for 01A (audit D-17, contract section 12.4). Items marked **OWNER ACTION** need someone with
access to the Railway dashboard; they cannot be done from the repository. Nothing here has been applied to Railway by
the engineering branch.

## 1. What runs where

| Piece | Where | Notes |
|---|---|---|
| Web app (`web` service) | Railway, built from `backend/Dockerfile`, started by `backend/deploy/start.sh` | gunicorn, 2 workers, runs as the restricted `ops_app` database role |
| PostgreSQL 16 | Railway Postgres service (one per workspace) | schema owner `ops_owner` (migrations only), runtime role `ops_app` (no DDL, no audit UPDATE/DELETE) |
| Staging | a separate Railway environment `staging` | its own database, secret key, cookie names and domain, no outbound integrations (contract 12.3) |

Start sequence (`start.sh`): `deploy/bootstrap.py` (roles and database) -> `migrate` as owner -> demo seed **only when
the database has no robots** -> gunicorn as `ops_app`. A new, empty, non-demo deployment is not seeded; its first admin is
created through `/setup/` or `manage.py bootstrap_admin` with `WORKSPACE_BOOTSTRAP_TOKEN` (see section 8).

## 2. Health check

`GET /healthz` (no login, no secrets): `200 {"status":"ok","database":"ok","migrations":"ok"}` only when the database
answers and no migration is unapplied; otherwise `503` with `database: "unreachable"` or `migrations: "pending"`.
(`/api/health` is the older database-ping only check and stays.)

**OWNER ACTION (Railway UI):** service `web` -> Settings -> Deploy -> Healthcheck Path = `/healthz`, timeout 60 s. Railway
then keeps the previous deployment serving until the new one is healthy, and marks a deploy failed if it never is. Migrations
run before gunicorn starts, so a deploy whose migration fails never becomes healthy and never receives traffic.

## 3. Daily backup

Two layers; both are needed because a platform backup you have never restored is not a backup.

1. **Railway volume backups (preferred, no code): OWNER ACTION.** In the Railway dashboard open the Postgres service ->
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

Use client tools of the **same major version as the server** (PostgreSQL 16): an older `pg_dump` refuses to dump a newer
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
  `ops_inspection_grace_min`, `ops_periodic_due_lookahead_min`. Anything else is refused; `demo_mode` and
  `allow_draft_policies` can never be set this way.
- Values come from options or `PROVISION_<KEY>` environment variables (`PROVISION_CONSENT_FORM_VERSION=CF-v1`).
  `deploy/start.sh` runs the command after migrations when any `PROVISION_*` variable is present.
- `--with-defaults` offers `ops_readiness_lookahead_min = 2880` (today and tomorrow) when it is not given or set.
- It is idempotent: the same value is reported as unchanged. Replacing a different existing value needs
  `--allow-change`. If a required value is missing the command lists it and changes nothing.
- It takes the settings advisory lock exclusively, validates everything before writing, and records one audit
  event. Settings > Operating settings (read-only) shows what is set; /settings/setup/ shows
  "Operating settings incomplete" until the required values exist.

## 9. Rehearsal log

| Date | What | Result |
|---|---|---|
| 2026-11-21 | `backup_restore_check.sh` on the local Docker PostgreSQL 16: source database migrated to head with `seed_dev` and `seed_fleet` data (53 migrations, 7 users, 7 role grants, 40 audit events, 5 robots), restored into a second empty database | PASSED: every row count equal, audit append-only triggers and workspace singleton constraint present in the restored copy; refused a second run against the now non-empty target (exit 2); the app started on the restored database as the restricted `ops_app` role: `/healthz` 200 `{"status":"ok","database":"ok","migrations":"ok"}`, login and `/fleet/` 200 |
| (pending) | Restore of a Railway backup into the staging database | not done; needs the owner's Railway access |
| (pending) | Rollback rehearsal on staging (section 6) | not done; needs the staging environment |

## Staging rollback rehearsal (2026-11-21, Railway staging)

| Step | Railway deployment | Code | Login + /ops/, /fleet/R-01/, /readiness/visits/V-0001/, /planning/ | /healthz | 01A CSP header |
|---|---|---|---|---|---|
| Start | e0980a4f (SUCCESS) | v2/phase01 6ed7a2e | all 200 | 200 | present |
| Roll back | d3db9fdb (SUCCESS) | previous release main 5ea5f09, on the database already migrated by 01A (`platform_core.0003`) | all 200 | 404 (old code has none) | absent |
| Roll forward | 657c07b6 (SUCCESS) | v2/phase01 6ed7a2e | all 200 | 200 | present |

Result: the 01A migration is additive and backward compatible: the previous release starts and serves every core
page on the migrated database, and rolling forward again works. Note: `railway up` from a directory that is not
linked to the project deploys nothing; pass `--project <id> --environment <env> --service web` explicitly.
