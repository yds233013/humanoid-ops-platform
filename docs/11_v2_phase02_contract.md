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
- Privileges: the runtime role gets INSERT and SELECT only, through the commands. Approvals go through
  SECURITY DEFINER functions, as v1 does.

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
