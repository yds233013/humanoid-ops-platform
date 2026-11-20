# Humanoid Robotics Operations Platform

An internal operations platform for a small fleet of home humanoid robots. It coordinates and records operations. It does **not** control robots, implement emergency stops, or certify physical safety.

**Status:** Slices 0-7 built, tested and closed: foundation, fleet core, visit planning and scheduling, deployment readiness and departure authorization, field execution and incident response, operations command center, scaling and capacity model, production guardrail hardening (closure note `docs/07c_slice7_closure_note.md`). Field Operations Playbook v1.1: `docs/08_field_operations_playbook_v1.1.md`. Data is synthetic only; no production deployment is approved.

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

## Slice 3: deployment readiness and departure authorization

Open http://localhost:8000/readiness/ for upcoming visits, the per-visit checklist and readiness policies.
Contract: `docs/03_readiness_contract.md`. Lock order: `docs/02_lock_graph.md`.

- **Readiness policy:** versioned requirements (evidence kinds and maximum age, required equipment, recovery
  plan, authorization lifetime). It needs an engineering-lead and a safety-reviewer approval, and its author
  can't approve it. **One active policy:** the newest fully approved version. If it's withdrawn there is
  none until a newer one is approved. There's never a fallback to an older one.
- **Evidence:** pre-departure inspection (a real fleet inspection), equipment check, battery check, plus a
  recovery plan. Each is bound to the visit's current plan revision and the robot's current configuration.
  Evidence can be voided, never edited.
- **Readiness check:** read-only. It re-checks planning live (consent, holds, crew qualifications, van,
  bookings), the policy, evidence freshness, battery against the robot's minimum, equipment and the recovery
  plan. It gives explained blockers. Unknown values block.
- **Departure authorization:** a separate, time-limited decision. It records the exact versions it was
  decided on (site, robot, configuration, crew, visit, policy). Any later change invalidates it: a hold,
  withdrawn consent, a configuration change, a qualification change, a reschedule, new evidence, or a policy
  change.
- **Departure:** its own immutable record. Under locks it re-checks every version and re-runs the full
  readiness check. One authorization, one departure. One visit, one departure. The database also enforces
  both for direct SQL. A departed visit can't be re-planned.
- **Demo vs production:** a `synthetic_demo` policy only works while `demo_mode` is on. Production refuses it.
  Settings changes are serialized against authorization and departure by a database-enforced advisory lock.
- **Never shown as clearance:** the UI never says "cleared to depart". Demo pages carry a "SYNTHETIC DEMO,
  not real-world clearance" banner.

Demo: V-0001 has an approved synthetic policy, fresh evidence and a recovery plan, so it is "Ready to request
authorization". Evidence goes stale on the policy's clock (battery after 2 h).

## Slice 4: field execution and incident response

Open http://localhost:8000/execution/ for execution runs, incidents and incident policies; each visit has a dossier
at `/execution/visits/<code>/dossier/`. Contract: `docs/04_field_execution_contract.md`.

The platform records what authorized people report after departure. It does not move, stop or operate robots.

- **Execution run:** opened by the database in the same transaction as the departure. It can't be created any
  other way, even by direct SQL.
- **Custody:** one append-only history per robot (hub, vehicle, site, unaccounted). The departure creates an
  *administrative assignment, not verified* until the crew verify the physical handoff. Every move names a
  custodian and an identified receiver. A robot can't be in two places.
- **Site check:** Go / No-Go is computed (custody at the site, consents cover the adults present, every condition
  ok); a No-Go is recorded, never hidden. Work sessions start and resume only on a current Go.
- **Incidents:** every incident places a blocking robot hold **in the same transaction**, with or without an
  approved incident policy, also when reported late, after the visit, or with no run. Severity, status and
  escalation are separate, append-only decisions. Resolving or closing an incident never releases the hold; that
  stays with the fleet hold-release workflow.
- **Return:** the vehicle coming back is not the robot coming back. A trip closes only after a hub receipt, a
  post-visit inspection and a reconciled return. A robot left on site or unaccounted keeps the trip open.
- **A departed visit has happened:** it can't be cancelled, rescheduled or released afterwards. A robot can't be
  authorized again until its run is closed and it is back at the hub.

Demo: `make seed` adds a synthetic site-check template and a synthetic_demo incident policy (demo mode only).
Authorize and record V-0001's departure on the readiness page, then work the run on `/execution/`.

## Slice 7: production guardrails

Contract: `docs/07a_production_guardrails_contract.md` (SQL signatures and change log:
`docs/07b_s7_sql_signatures.md`). Slice 7 makes the gates that matter in production refuse anything that is
only demo data. Every rule is enforced in the database too, so direct SQL can't skip it.

- **Approved inspection checklists (X-1):** in production a pre-departure inspection counts only on a checklist
  template that engineering and safety both approved for that purpose. A synthetic template never authorizes a
  production departure.
- **Periodic inspection expiry (X-3):** each configuration needs one approved inspection interval. A robot whose
  periodic inspection is overdue, missing, or whose interval is unknown can't be planned or authorized, and the
  inspection must still be valid at the planned unloading end. No interval value is built in.
- **Qualified inspectors (X-8):** both signers of an inspection must hold a current `readiness_inspector`
  qualification. Revoking one blocks departures that relied on that inspection.
- **Hold release policy (G-01):** releasing a hold in production needs an approved hold policy, bound when the
  hold is placed. Holds placed under a policy that can no longer be used are re-based through an approval flow.
- **Escalation deadlines (X-4):** owner-approved deadlines per incident level make overdue escalations visible
  in the command center. Nothing is paged or emailed.
- **Daily close-out (X-10):** `/ops/closeout/` lists, read-only, the runs still open for the day (custody not
  verified, post-visit inspection outstanding, return not reconciled). It is not a sign-off.

**Production blockers.** The overview (`/ops/`) shows this panel in both modes ("Would block in production" in
demo). In production the same rows are also top-priority items in the exceptions inbox:

| Code | Meaning |
|---|---|
| `HOLD_POLICY_NOT_APPROVED` | No approved hold-release policy, so no hold can be released in production |
| `HOLD_UNRELEASABLE` | A hold whose bound policy can't release it (needs re-basing) |
| `INSPECTION_TEMPLATE_NOT_APPROVED` | A configuration in use has no checklist template approved for production |
| `PERIODIC_INTERVAL_NOT_APPROVED` | A configuration in use has no approved periodic inspection interval |
| `ESCALATION_DEADLINES_NOT_APPROVED` | No owner-approved escalation deadline set for the active incident policy |

Demo: `make seed` also runs `seed_guardrails`, which refuses to run outside demo mode and creates only
`synthetic_demo` rules, templates and synthetic-labelled grants. A production-mode check after seeding still shows
every blocker above. No migration or seed creates a production approval.

## Not built yet (by design)

- Maintenance, teleoperation, analytics. Real procedures and hardware-specific safety evidence (production policies need `approved_procedure` sources that don't exist yet).
- Task, site and vehicle hold scopes. They arrive with their domains.
- SSO: local accounts for now. The identity provider is unknown (blocker B-09).
- Deployment: no target environment yet.
