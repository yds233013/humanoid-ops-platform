# Humanoid Robotics Operations Platform

An internal operations platform for a small fleet of home humanoid robots. It coordinates and records operations. It does **not** control robots, implement emergency stops, or certify physical safety.

**Status:** Slice 0 (foundation), Slice 1 (fleet core) and Slice 2 (visit planning and scheduling) built and tested. Slice 3 waits for review. Data is synthetic only.

## Quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), make.

```bash
docker compose up --build          # Postgres 16 + migrations + synthetic users + app on :8000
curl localhost:8000/api/health      # {"status":"ok"}
```

For development on the host:

```bash
make db          # Postgres 16 in Docker on localhost:5440
make migrate     # migrations, run as the schema owner
make seed        # synthetic dev users dev_admin, dev_ops_lead, ... (password in .env.example)
make run         # the app, run as the restricted runtime role
make ci          # lint + type check + migration check + tests (what CI runs)
```

## Layout

| Path | What |
|---|---|
| `backend/config` | Django settings (all from environment variables), URLs |
| `backend/platform_core` | `run_command` (the only way to change state), audit events, idempotency keys, database security migration |
| `backend/accounts` | Roles, time-bounded role grants, permission checks, `grant_role` / `revoke_role` commands, login API |
| `backend/tests` | Tests against real Postgres: commands, database privileges, concurrency |
| `docker/postgres/init` | Creates the owner and runtime database roles |
| `docs/` | Approval package, design addendum |

## How a change happens

`run_command(actor, name, permission=..., payload=..., handler=..., idempotency_key=...)` runs these steps in order:

1. Is the actor authenticated? If not: **401**, nothing written.
2. Does the actor hold an active role grant for the permission? If not: **403**, and only a `denied` audit row is written.
3. One transaction:
   1. claim the idempotency key;
   2. run the handler;
   3. write the audit row;
   4. store the response.

Any error rolls back everything, the audit row included. A retry with the same key returns the stored response (`replayed: true`). The same key with a different payload returns **409**.

## Database security boundary

- **`ops_owner`** owns the schema and runs migrations. It's never used by the running app.
- **`ops_app`** is the runtime role:
  - It may read and write application tables.
  - It may only **insert** into `platform_core_auditevent`.
  - It owns nothing, so it can't disable triggers, alter or drop tables, or create tables.
- **A trigger rejects UPDATE, DELETE and TRUNCATE** on the audit table for everyone.
- **The owner can disable that trigger.** Superusers and the schema owner are a separate, documented trust boundary, used only for migrations, and by the test runner's teardown. See `tests/conftest.py`.

## Slice 1: fleet core

Open http://localhost:8000 after `docker compose up --build`. Log in as `dev_ops_lead` (or any `dev_<role>`); the password is `DEV_SEED_PASSWORD` in `.env.example`.

- **Robots:** register, and move through lifecycle (commissioning, validation, in service, retired). Transitions are checked by the database.
- **Configuration:** immutable manifests, approvable patterns (hardware + release + firmware), versioned capability profiles. A blank field means *unknown* and blocks planning.
- **Holds:** robot or pattern scope, from any source. Release needs every policy slot filled by valid approvals on the **current** configuration. Approvals are append-only and can be withdrawn.
- **Inspections:** the result is computed and re-checked by the database. An observed robot-condition failure always holds the robot, even if the inspection is incomplete. Visit items (8–12) block only the visit.
- **Fleet eligibility:** "Eligible for planning, not cleared to depart". Never shown as green. Departure authorization is a later slice.
- **Locking:** one order everywhere: role grants, then patterns, then robots (`platform_core/locks.py`). Permissions are re-checked inside the transaction.

**Implementation constraints worth knowing** (enforced in `fleet/migrations/0002`–`0004`):

- **Placing a hold never needs an approved policy.** A hold with no release rule is created with no slots and can't be released until a policy provides one. Draft policies block *release* only.
- **Every hold row is stamped with `created_txid = pg_current_xact_id()`.** The trigger forces it, and it's immutable. A failed robot-condition inspection must reference a hold whose `source_type = 'inspection'`, whose `source_ref` is that inspection's code, and which was created in the **same transaction**.
- **One hold per inspection:** unique index `uniq_inspection_hold_per_inspection`.
- **Permissions are re-checked on idempotent replays.**

Demo data: R-01 and R-04 eligible; R-02 held by a failed inspection; R-03 held by an incident and waiting on the founder; R-05 on an engineering-only build. The hold policy is deliberately a **draft**: no safety approval is faked.

## Slice 2: visit planning and scheduling

Open http://localhost:8000/planning/ for the week planner, visit pages and sites.

- **Sites:** opaque codes with a coarse zone, IANA timezone and CRM reference. No names or addresses.
  - Assessments are versioned and need a second person to approve.
  - Consent is verified per pseudonymous participant: form version, scope (presence / recording / remote view), evidence reference. Withdrawal takes effect immediately.
  - An unknown adult count means **Requires review**.
- **Crew and vans:** qualifications must cover the **whole** activity. A vehicle with unknown capacity or restraint kit can't carry a robot.
- **Feasibility:** "Planning feasible", "Blocked" or "Requires review", each with explained blockers.
  - Unknown durations block planning: travel, loading, inspection, charging.
  - **Never** permission to depart.
- **Reservations:** a robot is booked separately for inspection, loading, travel, on site, unloading and charging. Crew and van are booked from inspection to unloading.
  - Overlaps are rejected **by PostgreSQL** (a btree_gist exclusion constraint on `[start, end)`), even with app locks bypassed.
  - Back-to-back bookings are fine.
- **Revisions:** plan, reschedule and cancel are atomic. Every change is a new revision. A refused reschedule leaves the confirmed plan untouched. Confirm re-checks everything live.
- **Time:** stored in UTC. Site-local times that don't exist (spring forward) or are ambiguous (fall back) are refused, not guessed.
- **Locks:** see `docs/02_lock_graph.md`.

Demo: V-0001 confirmed; V-0002 blocked (crew and van conflict); V-0003 requires review (restricted home); V-0004 blocked (consent withdrawn).

## Not built yet (by design)

- Departure authorization, field execution, maintenance, teleoperation, analytics.
- Task, site and vehicle hold scopes. They arrive with their domains.
- SSO: local accounts for now. The identity provider is unknown (blocker B-09).
- Deployment: no target environment yet.
