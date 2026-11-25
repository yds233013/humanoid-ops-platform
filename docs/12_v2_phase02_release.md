# Fleet Operations Platform v2: Phase 02 release (hub, charging, maintenance, kits)

Status: **release candidate documentation**, written in 02E (final acceptance), 2026-11-25. It authorizes nothing by
itself. Phase 02 goes to `main` and to the Northbay demo only after the Phase 02 release approval (contract 11,
section 8).

All data in this project is fictional. No value in the product or the demo is a real robot specification,
maintenance interval, price or safety approval. Demo values are labelled "Demo value" on screen.

Related documents:

- Contract: `docs/11_v2_phase02_contract.md`.
- Closure notes: `docs/evidence/02a_closure_note.md` to `docs/evidence/02d_closure_note.md`.
- Deploy, backup, restore and rollback: `docs/runbook_deploy_backup_rollback.md`, sections 3 to 6, 8a-1 and 8d.

## 1. What Phase 02 adds

Goal: one person can run a hub's day from one screen (journey J8, "Hub day").

**Hub work queue (02A).**

- The Hub page lists tasks with a type, a robot or kit, a due time, an owner and a status.
- Some tasks are made by hand. Others are derived from records: periodic inspections due, holds, failed equipment
  checks, charging needs, maintenance status and low stock.
- A task is a to-do only. Marking it done never changes eligibility, holds, custody, readiness evidence,
  authorizations, maintenance status or stock.
- A new role, `hub_technician`, can work the queue, charge robots and count stock.

**Charging (02B).**

- Chargers are hub resources. Bookings and sessions cannot overlap, per charger and per robot.
- Battery levels are entered by people. The platform has no telemetry.
- Screens show three separate things: the last recorded level, the estimate at departure (Enough, Needs charge or
  Unknown), and the departure battery check that is still required.
- A visit whose robot needs charge is marked At risk, and a charging task appears. Nothing is re-planned.

**Maintenance gate (02C).**

- Maintenance rules are company decisions. Engineering proposes them. The engineering lead and a safety reviewer
  approve them.
- Counters come from records: calendar time, closed visits and recorded session hours.
- Overdue, Unknown, no approved rule, and a missing required inspection all block planning and departure, in the
  database, once the owner turns the gate on.
- Due soon only warns and creates a Hub task.
- Only a qualified person can record maintenance. A maintenance record never releases a hold.

**Kits (02D).**

- Kits and consumables are counted per hub, in an append-only ledger.
- On hand, reserved and available are always sums over the ledger. They can never go below zero.
- Kits can be reserved for a visit. A visit that stock cannot cover is At risk ("Kit short").
- An item below its minimum creates a restock task.
- The departure equipment check stays the gate.

**Fleet health (02D).** `/ops/health/` shows, per robot and for the fleet: recorded availability (from hold and
lifecycle records, not measured uptime), holds by source, incidents
by type and severity, and repeat failures. Every figure shows its numerator, denominator, window and what it does not
establish.

**Predictor log (02D).** The owner-only command `log_predictor_signals` records, for each departure: the battery
level at start (or Unknown), open holds, open incidents and maintenance overdue. Nothing reads these rows to make a
decision.

**Demo (02E).** The Northbay demo now shows a full Hub day. See section 7.

## 2. Limitations

Read these before you show or sell Phase 02.

- **Health metrics are descriptive, not certified uptime.** The page calls the figure "Recorded availability (from
  hold and lifecycle records), not measured uptime". It counts recorded hold time against recorded in-service time. They do not measure availability, reliability or failure rates. Maintenance-due periods are not
  subtracted.
- **Current configuration only.** Pattern-hold matching on the health page, and the maintenance status in the
  predictor log, use the robot's current configuration. Configuration history is not reconstructed. A robot that
  changed configuration inside the window can be counted against the wrong pattern.
  - The health page says this on screen, next to the hold figures and in the predictor section (added in 02E at
    the EM's request).
- **The predictor is observational only.** It predicts nothing. In the demo, two practice runs show that the
  pipeline works; they establish no predictive accuracy and no fleet reliability (also stated on the page). No planning, readiness, fleet, hub or execution code
  reads it. It is not a model and it is not validated.
- **No stock API and no purchasing.** Stock is changed on the Hub > Stock pages only. There are no suppliers, prices,
  orders or serial-level parts.
- **Cancelling a visit does not release its kits.** The Stock page flags such reservations ("Visit cancelled:
  release this"). A person must release them.
- **Usability with independent people is UNVERIFIED.** Every walk so far was done by someone who knows the product.
  No usability study with independent participants has taken place.
- **No real-world validation.** Nothing in Phase 02 has been used with real robots, real homes or real crews. The
  rules, thresholds and workflows have not been checked against real operations.
- **Fictional demo boundary.** The Northbay demo is fictional. Its rules, intervals, thresholds and approvals are
  demo values on `synthetic_demo` records. Production mode refuses every one of them. The demo's practice runs are
  recorded at build time, not at their planned time (section 7; contract 11, section 8.9).
- **Battery levels are typed in by people.** There is no telemetry. A stale or missing reading gives Unknown, never
  a guessed number.
- **One hub only.** Multiple hubs come in Phase 03.

## 3. Migrations

All Phase 02 migrations are additive. Each one has a reverse. None drops or renames a column or table, and none
tightens an existing constraint on existing rows. The two `AlterField` changes (`accounts/0004`, `hub/0003`) only
add choices.

| App | Migration | What it adds | Milestone |
|---|---|---|---|
| accounts | `0004_hub_technician_role` | the `hub_technician` role choice | 02A |
| hub | `0001_initial` | `hub_workorder` and its checks | 02A |
| hub | `0002_workorder_privileges` | runtime role: SELECT, INSERT, UPDATE only on work orders | 02A |
| hub | `0003_charging` | chargers, charger bookings, charging sessions, battery readings | 02B |
| hub | `0004_charging_write_boundary` | overlap constraints, append-only trigger, SECURITY DEFINER functions | 02B |
| fleet | `0009_maintenance` | maintenance rule, approval, withdrawal and record tables | 02C |
| fleet | `0010_maintenance_sql` | write boundary and maintenance status functions | 02C |
| planning | `0007_maintenance` | the `maintenance_technician` qualification; eligibility view rebuilt | 02C |
| planning | `0008_maintenance_gate` | planning triggers that apply while the gate is active | 02C |
| operations | `0007_maintenance_gate` | eligibility reasons and production blockers | 02C |
| readiness | `0009_maintenance_gate` | maintenance check in authorization and departure | 02C |
| hub | `0006_maintenance_tasks` | maintenance work-order source | 02C |
| operations | `0008_predictor_log` | append-only predictor log and its owner-only function | 02D |
| hub | `0007_stock` | stock items, reservations and movements | 02D |
| hub | `0008_stock_write_boundary` | stock SECURITY DEFINER functions and privileges | 02D |
| hub | `0009_stock_visit_and_units` | visit checks for issue and consume; singular units | 02D |

There is no `hub/0005`. The numbering skips it; `hub/0006` depends on `hub/0004`.

- `planning/0009_maintenance_gate_settings_lock` (02E): the three planning maintenance triggers take the settings lock in shared mode before reading `maintenance_gate_active`, so a direct-SQL write cannot slip past a gate activation in progress. Reverse restores the 0008 bodies.
- `fleet/0011_maintenance_open_sessions` (02E): a maintenance record no longer hides an execution session that was still open when it was recorded; the hours counter stays Unknown until that session is stopped. Reverse restores the 0010 body.

## 4. Before you migrate: backup

Do this for every database you migrate (production, staging and each customer workspace).

1. Take a manual backup in Railway: Postgres > Backups > New backup. Write down its time.
2. Take a logical dump as well (runbook section 3, item 2).
3. Rehearse the restore of that dump into an empty database with `deploy/backup_restore_check.sh` (runbook section 4).
   It must print a passing RESULT line. Record it in runbook section 9.
4. Only then deploy the Phase 02 code. `start.sh` runs `migrate` before the app serves.

A failed migration fails the deploy. The previous deployment keeps serving when the health check is on.

## 5. Production activation

After the migration nothing is blocked yet. Turn the new gates on in this order. Each step is safe to stop after.

**Maintenance (runbook 8a-1):**

1. `migrate`. Production readiness now lists two maintenance blockers.
2. Engineering proposes the maintenance rules for each configuration, with a real procedure reference.
3. The engineering lead and a safety reviewer approve each rule. They must be two different people, and neither may
   be the author.
4. The safety reviewer grants `maintenance_technician` to the people who will sign maintenance.
5. A qualified person records a baseline maintenance record for every robot and requirement. Records are stamped
   when they are made. Do not invent history. Check that no working robot shows Unknown or Overdue.
6. The owner activates the gate:
   `DJANGO_DB_IDENTITY=owner python manage.py provision_settings --maintenance-gate-active true`.

**Stock:**

1. The owner provisions the thresholds:
   `DJANGO_DB_IDENTITY=owner python manage.py provision_settings --stock-minimums "RK=3,RC=2" --stock-per-visit "RK=1"`
   (example values only; use the company's own). Until stock is received, upcoming visits show "Kit short". That is
   a warning; nothing is blocked.
2. An ops lead (or admin) creates the items on Hub > Stock > Items, with the same codes.
3. A hub technician or ops lead receives the initial stock on Hub > Stock (a real count, stamped now).
4. Check Hub > Stock and Today: no unexpected "Kit short", and restock tasks only for items really below minimum.

**Charging:** the owner provisions `ops_soc_fresh_min` (runbook 8a). Without it every forecast is Unknown. Then an ops
lead records the chargers on Hub > Charging > Chargers.

**Predictor log:** optional. If wanted, schedule `DJANGO_DB_IDENTITY=owner python manage.py log_predictor_signals` as
a daily owner job. It only writes the log.

## 6. Rollback

The normal rollback is to redeploy the previous image (runbook section 6). The schema change is additive, so the old
code runs on the new schema.

Before you redeploy the Phase 01 image:

1. **Turn the maintenance gate off first**, while the Phase 02 code still runs:
   `provision_settings --maintenance-gate-active false --allow-change`.
   The gate lives in the database. Phase 01 code has no maintenance screens, so it could not clear an overdue robot,
   and its `provision_settings` does not know this setting.
2. Write down which visits have kits reserved or issued. Phase 01 code does not show stock. The ledger stays in the
   database and comes back with Phase 02.
3. Tell hub technicians that Phase 01 has no Hub page. Their `hub_technician` role grants nothing in Phase 01.
4. Redeploy the previous image. Check `/healthz`, sign in, open Today, the planner and readiness.

Restore the backup from section 4 only if data is damaged. Data written after the backup is lost; say so to users.

Do not run `migrate <app> <older>` on a live database. The reverse migrations exist for development only.

The rollback of Phase 02 to Phase 01 has **not been rehearsed** yet. Rehearse it on staging before the production
release (runbook section 6, "Rollback rehearsal"), and record it in runbook section 9.

## 7. Demo: what Northbay shows for Phase 02

The demo is rebuilt every day by `build_demo_workspace --reset` (runbook 8b). Everything is created through the real
commands; no row is written to get around a guard. Codes below are those of a build on 2026-11-25. Visit and robot
codes depend on the build date, so read the build output: it names the J4 visit and the J8 robots and runs.

| J8 fact | Where to see it |
|---|---|
| Needs charge | Hub > Charging, Today: a robot read by hand at 46% (below the 80% demo minimum) shows "Needs charge before departure" on its upcoming visits, with charging tasks on the Hub |
| Maintenance due soon | Hub: "GRIPPER-PADS ... due soon" on the robot of the first practice run |
| Maintenance overdue (gate active) | Hub, Today: "SENSOR-CLEAN overdue"; its visit tomorrow shows Blocked |
| Maintenance unknown | NB-044 (no baseline), commissioning robots NB-049 and NB-050 |
| Periodic inspection due | Hub: NB-046 and NB-047 "due soon" (beta build inspected every day, demo value); NB-048 never inspected |
| Kit short and restock | Today: V-0034 (tomorrow 14:00, NB-038) is "At risk: Kit short" and nothing else (the build output names it); Hub: restock tasks for restraint kits and gloves, and a hand-made one |
| Work orders to assign and complete | Hub: hand-made and derived tasks, open, assigned and in progress |
| Held robots | NB-019 (maintenance hold, still booked), NB-012 (failed inspection), NB-007 (incident) |
| J3 open requests | V-0081 (H-120), V-0082, V-0083 at 18:30 on days +5 to +7, when every crew is unloaded: Suggest proposes a free robot, crew and van, and saving the plan is "Planning feasible" (V-0080 stays blocked on purpose: missing consent) |
| Health and predictor log | `/ops/health/`: recorded availability, 3 holds, 1 incident, 2 logged departures |

**Practice runs (EM ruling, contract 11 section 8.9).** The demo has exactly two short practice visits at 00:30 local
time today, at their own home `PRACTICE` ("Fictional practice visit (Emeryville)", shown on the visit, readiness and
run pages). They are run end to end through the normal guards and closed. Their records are stamped when the demo is
built, not at 00:30: the build cannot back-date anything. They exist so that the predictor log has departures and the
visit-count demo rules have closed runs. Every other past visit is still a "Historical scheduled visit" with no
execution record. The demo builder refuses to make practice runs in production mode.

**Clean kit-short visit.** One restraint kit is reserved for every visit up to the end of the day after tomorrow,
except one regular visit tomorrow. That visit also has a recovery plan and a hand battery reading above the minimum,
so "Kit short" is its only At-risk cause; readiness misses only the day's checks.

**Demo battery freshness.** The demo's `ops_soc_fresh_min` is 1440 minutes (a day; demo value), so the readings taken
at the 04:00 rebuild still count while the demo is walked during the day.

**Demo maintenance rules.** Four rules on each configuration, all labelled "Demo value, not an engineering
requirement": JOINT-SERVICE, HAND-SERVICE (from 02C), and two short visit-count rules added in 02E, SENSOR-CLEAN
(after every closed visit) and GRIPPER-PADS (due soon after the first). They exist to show the gate. They say nothing
about any real robot.

## 8. Release evidence

Candidate (code) SHA `c03fa53` on `v2/phase02`; closure note `docs/evidence/02e_closure_note.md`.

- Full CI on `c03fa53`: run 45592470559, success, 2044 passed, 15 skipped.
- Three perf runs on `c03fa53`: 45592473454, 45592487625, 45592503381, all success (numbers in the closure note).
- Browser walk J8, J2, J3, J4 at 1024 and 768 px: `docs/evidence/02e_acceptance_walkthrough.md`.
- NOT DONE: Phase 02 to Phase 01 rollback rehearsal on staging (section 6). Do it on the isolated deploy before any link cutover.
