# Fleet Operations Platform v2: Phase 02 contract (hub, charging, maintenance)

Status: **draft for EM review**, 2026-11-23. This draft authorizes nothing by itself. Phase 01 closed at `13dd04e`
and was released for fictional demos at `a213730`.

**Goal:** one person can run a hub's day from one screen (journey J8, "Hub day"):

- which robots need charging before their next visit;
- which inspections and maintenance are due or overdue;
- which kits and parts are short;
- who is doing each job, and what is done.

Charge state and maintenance due become part of whether a robot may be booked or leave. They are not only displayed
(Phase 00 research rows 14 and 15).

**Rules carried over:**

- Fictional data only.
- No real robot specifications, intervals, prices or safety approvals are invented. Every interval or threshold is
  either a company decision that goes through the v1 approval workflow, or a value marked as demo data.
- No automatic safety approvals.
- One hub only. Multiple hubs come in Phase 03.

## 1. What already exists (v1 and Phase 01)

| Area | Present today | Missing |
|---|---|---|
| Battery | Battery-check evidence (state of charge %) before departure. `min_departure_soc_pct` and `charge_turnaround_min` on the approved capability profile. Planning reserves the charge turnaround. | A charger as a resource; recorded charging sessions; a robot's state of charge between visits; a charge plan for tomorrow |
| Inspections | Periodic inspection rules (approved), periodic status, inspection templates, post-visit inspection | One hub list of what is due or overdue, with owners |
| Maintenance | Holds with approval-based release (J7); incidents | Maintenance rules (by hours, visits or calendar); work records; maintenance due as a readiness fact |
| Kits | Equipment check per departure (restraint kit, recovery kit, charger) | Counts per hub, kits reserved for visits, low-stock alerts, restocking tasks |
| Hub screen | Close-out page under "Hub" | Hub work queue (J8) |

## 2. Scope

| In Phase 02 | Not in Phase 02 |
|---|---|
| Hub work queue: tasks with type, robot or kit, due time, owner, status, created from facts or by hand | Purchasing, suppliers, costs |
| Chargers (bays) as hub resources; charging sessions (start and end time, start and end %); robot state of charge as last known, with its time and source | Live telemetry from robots: none exists, so all values are entered by hand or come from evidence |
| Charge plan: for each visit today and tomorrow, the robot's projected % at departure compared with the approved minimum, and the charging slot needed | Optimizing across hubs (Phase 03 or 05) |
| Maintenance rules (proposals, approved like periodic inspection rules), maintenance work records, maintenance due and overdue status per robot | Spare-part tracking at serial level |
| Kit inventory per hub (counts, reserved for visits, minimum level as an operating setting), restocking tasks | Customer-facing changes |
| Fleet health: per robot, its uptime share, its holds and incidents by category, and its repeat failures | Predictive models: candidate predictors are logged only |
| A **Hub technician** role (or field operator rights at the hub; see decision 4) | Agents (Phase 04) |
| Demo workspace extended: chargers, charge history from today only, kits, maintenance rules marked as demo, work orders | 500-robot performance (Phase 05) |

## 3. Safety and approval rules

1. **Maintenance rules are company decisions.**
   - A rule is a proposal until engineering and safety approve it, the same pattern as periodic inspection rules.
   - In production, an unapproved or missing rule is a production blocker.
   - The demo uses rules labelled "Demo value, not an engineering requirement".
2. **Maintenance overdue is enforced by the database** (decision 1):
   - an overdue robot cannot be planned for a new visit;
   - it cannot be authorized to leave;
   - existing bookings show "Blocked: maintenance overdue" and name who can clear it.
   - It is cleared only by a maintenance work record signed by a qualified person, and, where the rule says so, a
     post-maintenance inspection.
3. **The charge plan is planning information, not a departure decision.**
   - Departure still needs the battery-check evidence at or above the approved minimum.
   - A projected shortfall marks the visit **At risk** and creates a charging task.
   - It never silently re-plans.
4. **Charging sessions record facts entered by people.** The platform never claims a robot is charged without a
   recorded session end or battery-check evidence.
5. **Kits:**
   - Reserving a kit for a visit uses the existing reservation conflict rules.
   - A shortfall makes the visit **At risk** and creates a restocking task.
   - The departure equipment check stays the gate.
6. **Setup never approves.** Hub setup records chargers, kits and proposals only.
7. **Unchanged v1 safety suites:** all v1 readiness, custody, hold and incident safety tests stay green and none is
   weakened. New gates come with tests that try to get around them with direct SQL as the runtime role, the same
   approach as Slice 7.

## 4. Milestones

| Milestone | Delivers | Exit evidence |
|---|---|---|
| **02A Hub work queue (J8 shell)** | Work-order table and commands (create, assign, start, done, cancel; audited); "Hub" screen with queues by type (charging, inspection due, maintenance due, kits low, other) and by owner; tasks created from existing facts (periodic inspection due, holds needing work, equipment-check failures) | J8 walk with existing facts; role tests; statement budget for the Hub page |
| **02B Charging** | Chargers as resources; charging-session commands; last known state of charge per robot; charge plan for today and tomorrow; "At risk: needs charge" on Today, Dispatch and Hub; charging tasks; Suggest (J3) skips robots that cannot be charged in time | Charge-plan tests (shortfall, enough, unknown); no departure without battery evidence (unchanged); perf |
| **02C Maintenance** | Maintenance rules (propose, approve, withdraw); counters (visits, active hours from sessions, calendar); due and overdue status; work records with qualification check; database gate for overdue robots in planning and authorization; production blocker for a missing rule | Direct-SQL bypass tests; production-mode refusal; J7 interplay test; demo rule labelled |
| **02D Kits and fleet health** | Kit inventory per hub with counts and minimum levels (owner-provisioned setting); kit reservation for visits; restocking tasks; fleet health view (uptime share, holds and incidents by category, repeat failures); predictor log (battery at start, open issues, overdue maintenance) | Inventory conflict tests; health view correctness against the read models; J8 walk |
| **02E Acceptance** | Demo extended; J8 in the browser at desktop and tablet width; J2, J3 and J4 re-run with charging and maintenance facts; full CI plus 3 perf runs | Same closure bar as 01D |

## 5. Database changes (all additive and reversible)

- New tables: `hub_charger`, `hub_charging_session`, `robot_charge_state` (a view or last-value table), `hub_workorder`,
  `fleet_maintenance_rule` with its approval and withdrawal tables, `fleet_maintenance_record`, `hub_kit_stock`,
  `planning_kit_reservation`.
- New guard functions:
  - `fleet_maintenance_status(robot)`, set-based in the eligibility view, as in Slice 7 0005;
  - planning eligibility gains a reason "maintenance overdue";
  - the readiness evaluator gains one gate, "Maintenance up to date", backed by the same function used by the
    `readiness_issue_authorization` guard.
- Privileges: table by table, see section 8.8. The runtime role never writes a safety-relevant table directly.

## 6. Performance budgets

- Hub page p95: at most 2 s on the 200-robot fixture, with a 400 ms target, and with the evaluation cap shown as
  counts (Today's pattern).
- Today and the readiness list keep their budgets with the added gates.
- Statement-count tests for every new list page.

## 7. Decisions requested from the EM

1. **Maintenance overdue:** a hard database gate for planning and authorization (proposed), or a warning only in
   Phase 02?
2. **Battery model:**
   - Proposed: robot-level state of charge only (we do not know whether packs can be swapped; a real hardware fact
     is needed).
   - Alternative: swappable packs as assets now.
3. **Inventory scope:**
   - Proposed: kits and consumables counted per hub, with no purchasing.
   - Alternative: parts at serial level.
4. **Hub technician:** a new role "hub_technician" (proposed; least privilege: work orders, charging, kit counts, hub
   inspections), or reuse field operator?
5. **Charging slots:** reserve charger bays through the existing reservation tables (proposed; reuses conflict
   rules), or keep a separate simple queue?
6. **Order:** 02A, then 02B, then 02C, then 02D, then 02E, each reviewed by the EM before the next starts (proposed).

## 8. EM review 2026-11-23: approved with required corrections (binding)

EM rulings:

- Decisions 1-6 are resolved: maintenance gate approved with conditions; robot-level state of charge; hub-level
  counts; a new `hub_technician` role; charger reservations through shared conflict rules but their own table;
  order 02A to 02E.
- Each milestone is reviewed and closed before the next begins.
- No Phase 02 work goes to `main` or to the Northbay demo before Phase 02 release approval.

Phase 02 keeps three kinds of record apart:

- **Recorded facts:** measurements, maintenance work, stock.
- **Planning predictions:** whether a robot is likely to be ready.
- **Safety decisions:** whether it is eligible under approved policy, enforced by the database.

### 8.1 Work orders are not safety evidence

Marking a hub task done never changes:

- readiness or planning eligibility;
- maintenance status;
- custody;
- holds.

Only a separately guarded domain command can change those: a maintenance record, an inspection, battery evidence, an
inventory movement, or a hold release. A work order may *link* to the record that a command creates, but it cannot
create one. Test: completing every work order type leaves eligibility, holds and custody unchanged.

### 8.2 Maintenance rule lifecycle

**Rule versions.** A rule is versioned per configuration (pattern) and requirement code. Each version holds:

- the trigger: calendar days, completed visits or active hours, or any combination, where the first one reached
  counts;
- a warning margin;
- whether a post-maintenance inspection is required, and which approved template it uses;
- an effective-from time.

Approval and withdrawal follow the periodic-inspection-rule pattern (engineering lead plus safety reviewer). A new
version supersedes the old one only from its effective time.

**Counters since the last qualifying maintenance record for that requirement:**

- calendar time from the record's time;
- completed visits, counted from closed runs;
- active hours, summed from recorded session start and stop events.

There is no telemetry. Active hours come only from those events. A run with a session start but no stop makes the
hours **unknown**, never zero.

**Status per robot, requirement and rule version:**

| Status | Planning | Departure authorization |
|---|---|---|
| Not due | Existing checks | Existing checks |
| Due soon (inside the warning margin) | Warning plus a hub task | Existing checks |
| Overdue | **Blocked** | **Blocked** |
| Unknown (missing or incomplete counter data) | **Blocked** until reviewed (fail closed); the reason names the missing data | **Blocked** |
| No approved rule, production mode | Not eligible (production blocker) | **Blocked** |
| No approved rule, demo mode | Uses the demo rule, labelled "Demo value" | Demo rule |
| Maintenance recorded, required inspection missing | **Blocked** | **Blocked** |

**Clearing a status.** A maintenance record clears a status only when it:

- is written by `fleet_record_maintenance()` (SECURITY DEFINER);
- names the robot, the rule version and the requirement;
- is signed by a person holding a valid `maintenance_technician` qualification, granted by the safety reviewer;
- is followed by a passing post-maintenance inspection, when the rule version requires one.

The record stores the rule version it satisfied.

**Holds are unchanged.** A maintenance record never releases an incident or maintenance hold; J7 stays the only path
to release.

**Gates.** Planning eligibility (view and plan command) and `readiness_issue_authorization` both call
`fleet_maintenance_status()`. Direct-SQL bypass tests as the runtime role cover both paths.

### 8.3 Battery confidence

**State-of-charge records.** Each record holds a value, an observed-at time, a source (`battery_check`,
`charging_session_end` or `manual_reading`) and the person who recorded it. Records are append-only.

The screens show three separate things:

- **Last recorded:** the value, its age and its source.
- **Estimated at departure:** Enough, Needs charge or **Unknown**.
- **Departure evidence required:** the existing battery check, unchanged.

**Freshness.** A reading is fresh while it is younger than `ops_soc_fresh_min`, an owner-provisioned setting. If that
setting is missing, the forecast is Unknown.

**When the forecast is Unknown.** It is Unknown when any of these is true:

- the reading is stale;
- the charge rate is unknown;
- the approved profile has no charging characteristics.

No percentage is invented.

**Charging sessions are not departure evidence.** A charging session never counts as departure battery evidence unless
an approved readiness policy explicitly lists that source. The current policies do not.

**Suggest (J3).** A robot is excluded only when an approved constraint proves it cannot charge in time. A robot with an
Unknown forecast stays selectable, labelled "Charge forecast unknown", and remains subject to the departure gates.

### 8.4 Charger concurrency

**Tables.**

- `hub_charger`: code, hub, and in-service flag.
- `hub_charger_reservation`: charger, robot, period, optional visit, status.
- `hub_charging_session`: charger, robot, start time, end time (null while open), start % and end %.

**Constraints in PostgreSQL** (`btree_gist`, as for `planning_reservation`):

- No two active reservations for the same charger overlap:
  `EXCLUDE USING gist (charger_id WITH =, period WITH &&) WHERE (released_at IS NULL)`.
- No robot has overlapping active charger reservations: the same constraint on `robot_id`.
- On `hub_charging_session`, the same two exclusions on `tstzrange(started_at, coalesce(ended_at, 'infinity'))`.
  This means at most one open session per charger and per robot.

**Writes.** Only `hub_reserve_charger()` and `hub_start_charging()` (SECURITY DEFINER) write these tables. Each one:

1. checks that the charger is in service;
2. checks the robot is not reserved by a visit (`planning_reservation` purposes other than charging) for an
   overlapping period, under the robot's advisory lock (the existing per-robot planning lock key);
3. inserts.

**Simultaneous attempts.** Two attempts on the same charger or robot: one wins, and the other gets exclusion error
23P01, mapped to a plain refusal ("Charger C-2 is already booked 14:00-16:00"). Concurrency tests run both orders.

**Releasing.** Cancelling or finishing sets `released_at` or `ended_at` and frees the capacity. A reservation is never
evidence that charging happened.

### 8.5 Inventory invariants

**Items.** Each item per hub has a kind: `reusable` (a kit, which comes back) or `consumable` (used up).

**Stock is a ledger.** `hub_stock_movement` rows are append-only:

- receive;
- adjust (needs a reason);
- reserve;
- release;
- issue;
- return (reusable only);
- consume (consumable only).

The derived quantities are:

- **On hand** = received + adjustments + returned - issued(consumable) - written off;
- **Reserved** = active reservations;
- **Available** = on hand - reserved.

**Database invariants:**

- On hand never goes below 0, and available never goes below 0. Each movement function takes a per-item row lock and
  checks this before inserting; there is no updatable counter column.
- `hub_reserve_stock()` refuses over-reservation atomically.

**The minimum level** is an owner-provisioned operating threshold. A shortage creates a restocking task and an
"At risk" condition on affected visits. The departure equipment check stays authoritative.

### 8.6 Roles and approvals

The permission matrix is enforced in `accounts.roles`, with negative tests for each "no", including direct API calls.

| Action | hub_technician | field_operator | ops_lead | engineer | engineering_lead | safety_reviewer | admin |
|---|---|---|---|---|---|---|---|
| View Hub queue | yes | own tasks | yes | yes | yes | yes | no |
| Create, assign or complete work orders | own and assigned | own | yes | yes | yes | no | no |
| Record charging session or state-of-charge reading | yes | yes (field) | yes | yes | no | no | no |
| Reserve a charger | yes | no | yes | no | no | no | no |
| Stock receive, issue or return | yes | no | yes | no | no | no | no |
| Stock adjustment (with reason) | yes (audited) | no | yes | no | no | no | no |
| Record maintenance (also needs a `maintenance_technician` qualification) | if qualified | no | no | if qualified | if qualified | no | no |
| Submit an inspection (also needs a `readiness_inspector` qualification) | if qualified | if qualified | if qualified | if qualified | no | no | no |
| Propose a maintenance rule | no | no | no | yes | yes | no | no |
| Approve or withdraw a maintenance rule | no | no | no | no | yes | yes | no |
| Grant qualifications | no | no | no | no | no | yes | no |
| Release holds | no | no | J7 as today | no | J7 | J7 | no |
| Authorize departure | no | no | yes | no | no | no | no |
| Change thresholds or settings | no | no | no | no | no | no | owner command only |
| Edit or delete historic measurements | no | no | no | no | no | no | no |

Role membership never stands in for a qualification.

### 8.7 Performance and release

- Hub page: a 2 s p95 ceiling with a 400 ms target, and statement-count tests constant in fleet size.
- Today, the readiness list and Suggest keep their budgets with the new gates.
- The hosted `/readiness/` 2.1 s exception (Phase 01 release) stays open in the performance backlog for Phase 05. It
  is not counted as resolved.

### 8.8 Database privileges and the trusted write boundary

The runtime role `ops_app` gets SELECT on the new tables and **no** INSERT, UPDATE or DELETE on these tables:

- `fleet_maintenance_rule*`;
- `fleet_maintenance_record`;
- `robot_soc_reading`;
- `hub_charger_reservation`;
- `hub_charging_session`;
- `hub_stock_movement`;
- `hub_stock_reservation`.

All writes to them go through SECURITY DEFINER functions owned by `ops_owner`. Each function validates the actor's role
and qualification (passed and checked as in v1), the invariants, and the settings lock where it applies, and writes
an audit row.

`ops_app` may INSERT and UPDATE `hub_workorder` directly through the command layer, because it is operational only
and not evidence.

Direct-SQL tests as `ops_app` prove it cannot:

- manufacture a maintenance record, a rule approval, a state-of-charge reading or a stock movement;
- reserve outside the functions;
- update or delete any of them.

### 8.9 EM ruling 2026-11-25: demo practice runs

The EM approved two practice runs in the Northbay demo. This revises 01D ruling B ("no execution records in the
demo") for Phase 02. Conditions (binding):

- Exactly two completed practice runs after each rebuild. Both visits are clearly labelled as fictional practice
  visits, and the label is visible on the visit and run pages. (The demo uses its own home `PRACTICE`, whose town
  field reads "Fictional practice visit (Emeryville)".)
- All normal readiness, authorization, custody, execution and reconciliation guards stay active.
- Real command timestamps. No backdating and no clock advancement. The 00:30 visit time works with build-time
  execution under the normal scheduling guards; the records say when they were really made.
- Maintenance rules are labelled demo values.
- Predictor rows come only from the owner-only logger.
- No demo-specific bypasses. Practice-run generation lives only in the demo builder, which refuses it in production
  mode.
- Rebuilds never accumulate history: `--reset` empties the database before each build.

Two practice runs show that the pipeline works. They establish no predictive accuracy and no fleet reliability.

