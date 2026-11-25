# 02C closure note: maintenance

Branch `v2/phase02`. `main` and the Northbay demo are unchanged. All data is fictional.

## Candidate

| Item | Value |
|---|---|
| Final SHA | `5f87b87` |
| What it contains | 02C (9 commits `01b628b`..`af6a1bb`, merged as `faf41ba`), on top of the 02B follow-ups (`a5be18a`, `5d0c089`), plus a test-only fix `5f87b87` |
| Diff | `b814ac4...5f87b87` |
| Migrations (additive) | `fleet/0009_maintenance` (rule, approval, withdrawal and record tables) · `fleet/0010_maintenance_sql` (SECURITY DEFINER write boundary, status functions) · `planning/0007_maintenance` (adds the `maintenance_technician` qualification; the eligibility view is swapped around the column change) · `planning/0008_maintenance_gate` (planning triggers while the gate is active) · `operations/0007_maintenance_gate` (eligibility reasons; production blockers) · `readiness/0009_maintenance_gate` (authorization and departure guard) · `hub/0006_maintenance_tasks` |
| Full CI on `faf41ba` | 1,981 passed, 1 failed |
| The failure | Flaky test `test_labels_cost_a_fixed_number_of_statements`. A random form-token clean-up runs once in about 25 page views, so the count read 35 or 36. |
| The fix | `5f87b87` turns the random clean-up off in that test, as the perf tests already do. It passed 3/3 locally. |
| Full CI on `5f87b87` | Success |
| Perf on `faf41ba` | 3/3 green (runs 45379643761, 45379652432, 45379662842) |

## EM acceptance for 02C

1. **Approved, versioned rules per configuration and requirement.**
   - A rule can trigger on calendar days, closed visit runs or active hours, whichever is reached first. It also has a
     warning margin, an optional required inspection template and an effective-from time.
   - The engineer or engineering lead proposes. The engineering lead and the safety reviewer approve; they must be two
     different people, and neither can be the author. A rule can be withdrawn.
   - A proposal is never in force. The version in force is the highest approved one whose effective time has passed.
   - If that version is withdrawn, there is no rule, with no fallback to an older version (same as periodic rules).
   - In production, a demo rule counts as no rule.
2. **Counters come from authoritative records.**
   - They count from the last qualifying record. A record stops qualifying if the signer's qualification is revoked.
   - Visits come from closed runs.
   - Active hours come from session start, pause, resume and stop events. A run with a start but no stop makes the hours
     Unknown, never zero.
3. **Fail-closed states.** Each of these blocks planning and departure; "due soon" only warns and creates a Hub task:
   - No rule
   - Unknown
   - Overdue
   - Required inspection missing
4. **Qualified records.** Only `fleet_record_maintenance()` (SECURITY DEFINER, owned by `ops_owner`, fixed search path,
   EXECUTE revoked from PUBLIC) can write a record. It requires an allowed role (hub_technician, engineer or
   engineering_lead) and a valid `maintenance_technician` qualification at record time.
   - The safety reviewer grants that qualification, never to themselves and never backdated, with a training reference.
   - Records are append-only for every role.
   - `ops_app` has SELECT only on the rule, approval and record tables.
   - Direct-SQL tests with forged, NULL and empty actors write nothing.
5. **The database enforces the gate.**
   - `fleet_maintenance_status_set()` drives the status. The single-robot function calls it, so the two always agree.
   - Planning: the eligibility view and the planning triggers refuse a new reservation, requested→planned, or
     any→confirmed for a blocked robot.
   - Authorization: `readiness_issue_authorization` checks maintenance and caps `expires_at` at the earliest calendar
     due time. The departure guard checks again.
   - Direct SQL as `ops_app` cannot get past either gate.
6. **Holds are separate.** Recording maintenance never releases a hold.
   - Tested.
   - Seen in the browser: after maintenance on held robot NB-019, Today still shows 3 on hold, including NB-019.
7. **Regression.** Tests cover:
   - existing bookings, which become blocked with a stated reason and are never silently re-planned;
   - authorizations issued before the gate was activated, before a rule was withdrawn, and before a record was made;
     this follows the Slice 7 periodic pattern, where approvals and records invalidate older guard snapshots;
   - concurrent maintenance against bookings and authorizations, in both orders;
   - attempts to get around privileges.

   The Slice 7, readiness, planning, hub, charging and demo suites are green in CI.
8. **Safe incremental rollout.** The owner setting `maintenance_gate_active` controls the gate:
   - It is set through `provision_settings`; absent means inactive. Turning it off from true needs `--allow-change`.
     Every change is audited and taken under the settings lock.
   - While inactive, the status is computed and shown as "Maintenance gate not yet activated", nothing is blocked, and
     production lists it as a blocker (`MAINTENANCE_GATE_INACTIVE`).
   - Rollout order, in runbook 8a-1: migrate → propose → approve → grant qualifications → baseline records → activate.

## Browser walk

Local server of the same code, Northbay demo rebuilt (gate active), signed in as lily.tran, 1,024 px.

1. The Hub showed 7 "Maintenance due" tasks. Robots with no baseline record (NB-044, and commissioning robots
   NB-049 and NB-050) were listed as unknown.
2. `/fleet/NB-044/maintenance/` showed:
   - the requirements HAND-SERVICE v1 and JOINT-SERVICE v1, labelled "Demo value";
   - status "Unknown: no maintenance record";
   - the limits and where each counter comes from;
   - "Recording maintenance never releases a hold".
3. Recorded JOINT-SERVICE. Message: "Maintenance recorded. It never releases a hold; a required post-maintenance
   inspection is recorded separately."
4. JOINT-SERVICE is now "Not due". HAND-SERVICE stays Unknown, so NB-044 stays blocked.
5. Recorded maintenance on NB-019, a held robot. Today still shows **3 on hold**, including NB-019.

## Open items (engineering)

- **Hub task source references** are `robot:requirement:ruleversion:recordid`. The record id lets a second maintenance
  cycle under the same rule version create a new task.
- **The planning triggers** read the gate setting without taking the settings lock. The plan command takes the lock,
  and authorization still stops a race that comes in through direct SQL.
- **Statement counts:** the robot page has 1 more, the configuration page 3 more, and the plan and eligibility checks
  1 more each. These pages have no budget tests; the existing budgets are unchanged.
- **Usability:** independent usability testing is still UNVERIFIED.
