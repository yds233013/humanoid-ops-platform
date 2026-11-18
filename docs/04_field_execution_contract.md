# Slice 4 Contract: Field Execution and Incident Response

Status: DRAFT rev 2 for engineering review · 2026-11-18 · baseline `9228ae8` (529 tests). Rev 2 applies the EM's
required amendments on rev 1 (`1a0ded5`); section 11 lists each one. Not frozen until the EM approves. After
freeze, engineers do not change names, signatures, lock order or file ownership; a needed change is reported to
the integration lead with the exact reason.

Everything is SYNTHETIC. The system records what authorized people report. It does not move robots, stop them,
operate them remotely, or describe how to recover one physically. No household names, addresses or contact
details: sites stay opaque codes, participants stay pseudonymous refs (`P1`).

## 0. Principles

1. **Custody, visit execution and incident consequences are separate records.** None is inferred from another.
2. **Append-only.** Every Slice 4 table is insert-only (`reject_mutation` on UPDATE/DELETE/TRUNCATE). State is
   derived from the latest row(s), never stored in an editable status column.
3. **Every incident holds the robot, in the same transaction.** No policy decides whether to hold; a policy can
   only classify severity. Release stays with the existing fleet hold-release workflow.
4. **Unknown blocks.** Missing site check, unknown adult count, unknown or unverified custody, unresolved
   reconciliation: all block the next step. A No-Go or block never prevents reporting an incident or documenting
   a return.
5. **A departed visit has happened.** It is never cancelled, rescheduled or released; it ends through execution
   records (reconciliation, trip closure).
6. **The database enforces the invariants** for direct `ops_app` SQL (triggers + privileges); Python commands give
   the explained answer. Every append-only stream is serialized by a named parent row that both the command and
   the insert trigger lock `FOR UPDATE` (section 5); a unique sequence index is a second line, not the mechanism.
7. **No background workers for correctness.**

## 1. The four decisions (answered from the code)

### D1. Custody authority (EM: approved)
**Finding:** there is no custody table. `fleet.CustodyEvent` appears in the approval package model (section 3) but
was never built (`grep -ri custody backend/` finds nothing outside docs). `planning.TripCargo` is the *planned*
load of a trip; it is not custody.

**Decision:** one new append-only table `execution.CustodyEvent` is THE custody history. `TripCargo` stays the plan
and is used by reconciliation ("cargo matches"). Custody of a robot (`execution.custody.current_custody(robot)`,
mirrored by SQL `execution_current_custody(robot_id)`):

| Robot's history | Custody |
|---|---|
| no custody events AND no execution run ever | `hub` (pre-Slice 4 baseline) |
| has runs or events | the latest event's `to`, with its verification state |
| has runs but no events, or a sequence gap (cannot happen through the triggers; checked anyway) | `unknown` → fails closed everywhere |

### D2. Incident hold fallback (EM: approved)
**Finding:** `fleet._create_hold` already places holds with no policy dependency: it copies the newest
`HoldPolicy`'s rule for the source type if one exists, otherwise the hold has no release slots and is
**unreleasable** until an approved policy provides a rule (fleet migrations 0003/0004). `Hold.SOURCES` already
contains `incident`.

**Decision:** every incident creates a robot-scope blocking `Hold(source_type='incident', source_ref=<incident
code>)` through `_create_hold`, in the same transaction, always; `Incident.hold` is NOT NULL and validated by the
database. There is no significance flag or event-type list that can skip the hold: a matter that does not concern
the robot is a session *observation*, not an incident. The versioned `IncidentPolicy` only supplies the severity
scale, escalation requirement and downgrade roles; with no active policy, severity stays `unclassified` and the
hold is still placed. `report_incident` does not read the incident policy at all. Release rules for incident holds
stay in the existing `HoldPolicy`; Slice 4 invents none.

### D3. Trip state reuse (EM: approved)
**Finding:** `planning_trip_guard` forbids every change to a `VehicleTrip` except setting `released_at` once, and
`planning.services._release_active` (cancel / re-plan) releases trips and reservations. `released_at` means "no
longer planned/reserved", not "back at base".

**Decision:** `VehicleTrip` and `TripCargo` are reused unchanged (no new columns). Physical trip progress is an
append-only `execution.TripEvent` sequence; outbound departure is the existing `readiness.DepartureEvent` (no second
departure). Closure is `execution.TripClosure` (one-to-one with the trip). After departure, the trip and its
reservations are never released (C1).

### D4. Lock compatibility (EM: provisionally approved; matrix verified in section 5)
**Finding:** `platform_core.locks.ORDER` covers site → pattern → robot → person → vehicle → visit → readiness
policy → authorization; `planning_vehicletrip` is locked only by the cargo trigger. Hold insert/release triggers
`UPDATE fleet_robot` (robot U must be held). `record_departure` holds robot **S**. `planning.cancel_visit` locks
the visit U, then `_release_active` updates reservations and trips (both later than the visit: in order).

**Decision:** extend the order with `planning_vehicletrip` after visits and the execution tables after
authorizations (section 5). Writers that change custody, place holds or change the run lock the robot **U**;
checkers take robot **S**. No command or trigger locks an earlier table after a later one.

## 2. Domain model (`execution` app)

All rows: `recorded_by` (user), `recorded_at` (DB time, forced by trigger to `clock_timestamp()`), immutable.
"Field crew" of a run = the revision's `lead` and `second` persons. "Field user" = a user with an active
`ops_lead` or `field_operator` role grant (`has_active_role`).

| Entity | Fields | DB rules (insert trigger) |
|---|---|---|
| `ExecutionRun` | `code` (EX-0001), `departure` (1:1 readiness.DepartureEvent), `visit` (unique), `revision`, `robot`, `manifest` (robot's manifest at departure), `trip` (unique, planning.VehicleTrip) | Created ONLY by the AFTER INSERT trigger on `readiness_departureevent`, same transaction. ops_app has no INSERT. **Open** until a `TripClosure` exists for its trip. |
| `CustodyEvent` | `robot`, `seq`, `from_kind`/`to_kind` in `hub, vehicle, site, unaccounted`, `trip` (when vehicle), `visit` (when site), `run` (NULL only for a no-run unaccounted report), `custodian` (Person responsible after the move), `received_by` (Person acknowledging receipt), `basis` `administrative`/`handoff`/`recovered`, `incident` (required when `to_kind` or `from_kind` is `unaccounted`), `note` | Locks `fleet_robot` FOR UPDATE. Unique (robot, seq); `seq = previous + 1` forced; `from` = previous `to` (baseline hub only per D1). Move rules below. Refused when the run is closed. |
| `CustodyVerification` | 1:1 `custody_event` (an `administrative` one), `verified_by` (Person) | Locks robot. The verifier is field crew of the run, holds a field role, and differs from nobody in particular (one verifier). Turns an administrative assignment into a verified handoff. |
| `TripEvent` | `trip`, `run`, `seq`, `kind` in `arrived_site, departed_site, arrived_hub` | Locks the run FOR UPDATE. Unique (trip, kind), order arrived_site → departed_site → arrived_hub; run open. `departed_site` needs custody = vehicle on this trip (verified), OR a `robot_left_on_site`/`robot_unaccounted` disposition on the run. |
| `SiteCheckTemplate` | `version`, `items` JSON `[{"id","label"}]`, `source` `synthetic_demo`/`approved_procedure` | immutable; synthetic usable only while `demo_mode` |
| `SiteCheck` | `run`, `template`, `adults_present` (int ≥ 0), `conditions` `{item_id: "ok"/"not_ok"}`, `consent_count` (computed), `result` `go`/`no_go` (computed), `verifier` (Person, field crew) | Locks the run. Keys = template items. Trigger recomputes: `go` only if run open, trip `arrived_site`, custody = site of this visit and verified, every condition `ok`, site `expected_adults` known, `adults_present ≤ consent_count` (valid consents on the current form covering the revision's scopes), template usable in the current mode. Mismatch → refused. |
| `SessionEvent` | `run`, `seq`, `kind` in `start, pause, resume, stop, observation`, `site_check` (start/resume), `interruption` (incident-driven pause), `note` | Locks the run. State machine and gates in section 3. |
| `Interruption` | `code` (IN-0001), `run`, `source` `incident`/`operator`, `incident` (when source=incident), `reason` | Locks the run. Several may be open; each closes only by its own resolution. |
| `InterruptionResolution` | 1:1 `interruption`, `note` (non-empty) | Locks the run. Refused while the interruption's incident hold is unreleased. |
| `IncidentPolicy` | `version`, `rules` `{"severity_scale": [low→high], "escalation_required_from": "<level>", "downgrade_roles": [roles]}`, `source` `synthetic_demo`/`approved_procedure`, `notes`, `lock_version` | Same lifecycle as ReadinessPolicy: engineering + safety slots, no self-approval, withdrawal; active = highest-version ever fully approved, none if that one is withdrawn (no fallback); synthetic usable only while `demo_mode`. |
| `IncidentPolicyApproval` / `IncidentPolicyWithdrawal` | as readiness | as readiness; bump `lock_version` |
| `Incident` | `code` (IC-0001), `robot`, `manifest` (forced = robot's current manifest), `run` (nullable; = the robot's open run if any, forced), `visit` (= run.visit), `event_type` (`fall, contact, unexpected_motion, stopped_unexpectedly, damage, heat_or_smoke, robot_unaccounted, other`), `description`, `occurred_at` (≤ reported_at), `reported_at` (DB time), `knowledge_source` `witnessed`/`told_by_crew`/`found_later`, `hold` NOT NULL | Locks the robot. `hold`: robot scope, same robot, blocking, unreleased, `source_type='incident'`, `source_ref = code`, `created_txid = pg_current_xact_id()`. Unique index one incident hold per `source_ref` (as inspections). |
| `FaultObservation` | `incident`, `component`, `status` `suspected`/`confirmed`, `evidence_ref` | Locks the incident. |
| `SeverityDecision` | `incident`, `level`, `policy`, `reason` | Locks the incident. `policy` = the active policy (usable in the mode), `level` in its scale; a level lower than the latest needs a role in `downgrade_roles` and a decider ≠ reporter. No active policy → refused (stays `unclassified`). |
| `IncidentStatusChange` | `incident`, `to_status` | Locks the incident. reported (implicit) → triaged → response_in_progress → resolved → closed, one step at a time. `closed` needs every escalation acknowledged and, when the latest severity ≥ `escalation_required_from`, at least one escalation. Never touches the hold. |
| `IncidentEscalation` / `EscalationAck` | `incident`, `to_role`, `to_person` (optional), `requested_action`; ack 1:1 with `acknowledged_by` | Lock the incident. Ack by a user holding `to_role` (and = `to_person` when set). |
| `RecoveryDisposition` | `run`, `decision` `continue_visit`/`end_visit_return`/`robot_left_on_site`/`robot_unaccounted`, `reason`, `incident` (required for the last two) | Locks the run. `continue_visit` refused while the robot or its pattern has an unreleased blocking hold or the run has an open interruption; nothing overrides that. Recorder ops_lead. |
| `ReturnReconciliation` | `run`, `hub_receipt` (CustodyEvent → hub), `cargo_matches` (computed), `post_visit_inspection` (fleet.Inspection, nullable), `discrepancies` JSON list, `outcome` `reconciled`/`unresolved` | Locks the run. Refused once the run is closed. `reconciled` only if: latest custody is `hub_receipt` with `basis='handoff'` and a field-user receiver; the run's robot is the trip's only cargo robot; every custody event of the run is verified or a handoff; a post-visit inspection of this robot with `visit_ref = visit.code` submitted after the hub receipt (any result: a FAIL already holds the robot); `discrepancies` empty. Anything else may only be `unresolved`. |
| `TripClosure` | 1:1 `trip`, `run`, `reconciliation` | Locks the run. Trip `arrived_hub`; `reconciliation` is the run's latest row and `reconciled`. Closes the run. |

### Custody moves (trigger)
| From → To | basis | Requires |
|---|---|---|
| hub → vehicle | `administrative` | only from the departure trigger; `custodian` = revision lead, `received_by` NULL until `CustodyVerification` by field crew |
| vehicle → site | `handoff` | trip `arrived_site`; outbound event verified; custodian and receiver = field crew |
| site → vehicle | `handoff` | same trip, not yet `departed_site`; custodian and receiver = field crew |
| vehicle → hub | `handoff` | trip `arrived_hub`; custodian = field crew; `received_by` = a Person whose user is a field user |
| any → unaccounted | `handoff` | an Incident `robot_unaccounted` on this robot in the same transaction (section 4 order); receiver NULL |
| unaccounted → hub/site/vehicle | `recovered` | ops_lead recorder, receiver set, non-empty note (the recovery acknowledgment); does NOT resolve the incident or release its hold |

No move on a closed run. A vehicle returning without its robot is a `TripEvent arrived_hub` with custody still
`site` or `unaccounted`: the vehicle has returned, the robot has not, and the trip cannot close (D of section 11).

## 3. State machines

**Run (derived):** departed → arrived → checked in (latest SiteCheck) → session → left site → vehicle at hub →
reconciled → closed. Only `TripClosure` closes a run. A missing robot is never a completed return.

**Session (from SessionEvent):** `not_started → active ⇄ paused → stopped`. `stop` from active or paused;
`stopped` is terminal; `observation` in any state, including after stop, while the run is open.

Gates for `start` and `resume`, checked by the command under locks AND by the trigger:
- the referenced SiteCheck is the run's latest, is `go`, and (for `resume`) is newer than the latest `pause` and
  every Interruption of the run;
- no open interruption on the run;
- no unreleased blocking hold on the robot or its current pattern;
- custody = site of this visit, verified;
- valid consents covering the revision's scopes ≥ the check's `adults_present` (withdrawal after the check blocks);
- recorder is a field user who is field crew of the run, or an ops_lead.

**Incident:** status and severity are independent. Corrections are new rows. Resolving or closing never releases
a hold: no execution code writes `fleet_hold` except the insert in `report_incident`.

## 4. Commands

All through `run_command` (permission, idempotency, audit), locks via `lock_guards` before reads, re-read after
locking, `RetryTransaction` if the run/visit changed between read and lock. Return dicts like existing services.
Every lock list below is complete: it names each row the command or any trigger it fires will lock or update.

| Command | Owner | Permission | Locks (in order) |
|---|---|---|---|
| (trigger) open run + hub→vehicle custody, inside `record_departure` | A | — | settings S, site S, pattern S, robot **U** (C2), persons S, vehicle S, visit S→U as today, trip U (new, trigger), policy S, authorization U |
| `verify_custody(actor, *, run_code, custody_seq, verifier_code)` | B | `execution.record_field_event` | robot U, persons S, run U |
| `record_trip_event(actor, *, run_code, kind)` | B | `execution.record_field_event` | robot U, visit S, trip U, run U |
| `transfer_custody(actor, *, run_code, to_kind, custodian_code, received_by_code, note="")` | B | `execution.record_field_event` (recovered: ops_lead only) | robot U, persons S, visit S, trip U, run U |
| `record_site_check(actor, *, run_code, template_version, adults_present, conditions, verifier_code)` | B | `execution.record_field_event` | settings S, site S, robot S, persons S, visit S, run U |
| `reconcile_return(actor, *, run_code, inspection_code=None, discrepancies=[])` | B | `execution.reconcile` | robot U, visit S, trip U, run U |
| `close_trip(actor, *, run_code)` | B | `execution.close_trip` | robot S, visit S, trip U, run U |
| `record_session_event(actor, *, run_code, kind, site_check_id=None, note="")` | C | `execution.record_field_event` | site S, pattern S, robot S, persons S, visit S, run U |
| `open_interruption(actor, *, run_code, reason)` | C | `execution.record_field_event` | robot S, run U |
| `resolve_interruption(actor, *, code, note)` | C | `execution.triage_incident` | robot S, run U |
| `report_incident(actor, *, robot_code, event_type, description, occurred_at, knowledge_source, idempotency_key=None)` | C | `execution.report_incident` | robot U, visit S (open run's), run U (if open run), then fleet hold trigger updates robot (held) |
| `decide_severity(actor, *, incident_code, level, reason)` | C | `execution.change_severity` | settings S, incident policy S, incident U |
| `change_incident_status(...)`, `escalate_incident(...)`, `acknowledge_escalation(...)`, `add_fault_observation(...)` | C | triage / close_incident / escalation target role | incident U (status `closed` also reads policy: settings S, incident policy S first) |
| `record_disposition(actor, *, run_code, decision, reason, incident_code=None)` | C | `execution.record_disposition` | pattern S, robot S, run U |
| `create/approve/withdraw_incident_policy` | C | `execution.manage_incident_policy` / `approve_incident_policy` | settings S, incident policy U |
| `planning.cancel_visit` / `plan_visit` (reschedule) after departure | lead | existing | refused in Python after visit U; refused by triggers (C1) |

**`report_incident`, one transaction, exact order:** robot U (+ visit S, run U when the robot has an open run) →
`_create_hold(source_type='incident', source_ref=<new code>)` (its trigger bumps the held robot) → insert Incident
(hold FK) → if `event_type='robot_unaccounted'` and custody is not already unaccounted: insert CustodyEvent → 
unaccounted (current time; earlier custody is never rewritten; `occurred_at` and `reported_at` stay separate facts)
→ if an open run: insert Interruption(source=incident) and, if the session is `active`, a `pause` SessionEvent
linked to it → audit → commit. Any failure rolls everything back. Allowed after stop, after trip closure, with no
run. Idempotent on key.

New permissions (`accounts/roles.py`, lead):

| Permission | Roles |
|---|---|
| `execution.read` | ops_lead, field_operator, engineer, engineering_lead, safety_reviewer, founder |
| `execution.record_field_event` | ops_lead, field_operator |
| `execution.report_incident` | ops_lead, field_operator, engineer, engineering_lead, safety_reviewer, founder |
| `execution.triage_incident` | ops_lead, engineering_lead, safety_reviewer |
| `execution.change_severity` | engineering_lead, safety_reviewer (downgrade limited further by the policy) |
| `execution.close_incident` | engineering_lead, safety_reviewer |
| `execution.record_disposition`, `execution.reconcile`, `execution.close_trip` | ops_lead |
| `execution.manage_incident_policy` | engineering_lead |
| `execution.approve_incident_policy` | engineering_lead, safety_reviewer |

## 5. Lock order and matrix

```
role grants -> settings (advisory S) -> site -> pattern -> robot -> person -> vehicle -> visit
  -> vehicle trip (planning_vehicletrip) -> readiness policy -> departure authorization
  -> incident policy (execution_incidentpolicy) -> execution run (execution_executionrun)
  -> incident (execution_incident)
```
`lock_guards` gains `update_/share_trips`, `_incident_policies`, `_runs`, `_incidents` (lead). Ascending id within a
table.

**Parent row that serializes each append-only stream** (the command holds it U; the insert trigger also takes it
`FOR UPDATE`, a no-op for the command and the serialization point for direct SQL):

| Stream | Parent |
|---|---|
| CustodyEvent, CustodyVerification, Incident (+ its hold) | `fleet_robot` |
| TripEvent, SiteCheck, SessionEvent, Interruption(+Resolution), RecoveryDisposition, ReturnReconciliation, TripClosure | `execution_executionrun` |
| SeverityDecision, IncidentStatusChange, Escalation(+Ack), FaultObservation | `execution_incident` |
| IncidentPolicyApproval / Withdrawal | `execution_incidentpolicy` |

**Triggers that lock or write other rows:**

| Trigger | Locks / writes | Held by the command already? |
|---|---|---|
| departure insert → run + hub→vehicle custody | robot U (custody), trip U, inserts run, custody | Yes: robot U (C2), trip U added to `record_departure`; trip is after visit, before policies |
| custody insert / verification | robot U | Yes |
| run-child inserts | run U; read robot/pattern holds, consents, custody | run U yes; reads only |
| incident insert | robot U | Yes |
| fleet hold insert (existing) | UPDATE fleet_robot | Yes: robot U |
| incident-child inserts | incident U; read policy | Yes |
| C1 guards on planning_visit / visitrevision / reservation / vehicletrip | read readiness_departureevent | visit U held by every planning writer; departure holds visit (S→U) — the two serialize on the visit |
| C3 departure / issue-function hub check | read custody + runs of the robot | robot S (issue) / U (departure) held |

Checked for cycles: no command takes a trip, run, incident or incident policy before a site, pattern, robot, person,
vehicle, visit or readiness row. Incident-policy approval takes only settings S and the policy. `report_incident`
never takes the incident policy. Pattern-scope hold placement (existing) stops at robots. The departure path adds
only trip U, which sits between visit and readiness policy.

## 6. Database bypass protections (direct `ops_app` SQL tests)

- No INSERT on `execution_executionrun`; no UPDATE/DELETE/TRUNCATE on any execution table.
- Incident with NULL hold, a hold from an earlier transaction, a released, non-blocking, pattern-scope, other-robot
  or non-incident hold, or a second incident on the same hold → refused.
- Custody: wrong `from`, forced seq gap, vehicle→site before arrival or before verification, receiver without a
  field role, move on a closed run, unaccounted without a same-transaction incident, recovered without ops_lead →
  refused.
- Session start/resume with no go check, stale check, open interruption, robot/pattern hold, custody not at site or
  unverified, consent withdrawn below adults present → refused.
- Disposition `continue_visit` with a hold or an open interruption → refused.
- Reconciliation `reconciled` with any missing condition, or any reconciliation after closure → refused.
- Trip closure without the latest reconciliation `reconciled`, or before `arrived_hub` → refused.
- Severity with no active policy, synthetic policy in production, downgrade without the role or by the reporter →
  refused.
- Departed visit: status change, new revision, reservation release, trip release → refused (C1).
- Authorization issue or departure for a robot whose custody is not a verified hub receipt (or baseline hub), or
  that has an open run → refused (C3).
- Role checks use `has_active_role` on the recorded user id; the DB still cannot know the human behind `ops_app`.

## 7. Acceptance tests (closure bar)

Deterministic barriers (`checkpoint` after first read and after locks), both orders, real Postgres, no sleeps.

1. **Incident atomicity:** hold + incident + unaccounted custody + interruption + pause + audit commit together;
   injected failure at each step rolls back everything. Late report (after stop, after closure, no run) still holds.
2. **Races, both orders:** cancel vs departure; report_incident vs session start and vs resume; custody transfer vs
   reconcile; reconcile vs close_trip; two concurrent custody moves; demo→production switch vs severity decision,
   vs incident-policy approval and vs site check (synthetic template); consent withdrawal vs session start.
3. **Custody integrity:** gaps, double custody, administrative vs verified, recovered path, closed-run refusal.
4. **Site checks:** no_go blocks start; unknown expected adults; adults present > consents.
5. **Session lifecycle:** every invalid transition; resume needs a newer go check; stopped never restarts.
6. **Severity and policy:** no policy → unclassified + hold placed; draft/withdrawn/synthetic-in-production refused;
   downgrade rules; append-only history.
7. **Recovery and return:** left-on-site and unaccounted keep the run open and the trip unclosable after the vehicle
   arrives at hub; hold survives incident resolve/close; continue_visit refused while held; failed post-visit
   inspection holds; a held robot's trip can close.
8. **Slice 3 regression:** existing readiness concurrency suite passes unchanged with robot U at departure; tests
   that asserted cancel-after-departure is allowed are changed to assert it is refused (C1, approved change).
9. **Bypass:** section 6, as ops_app.
10. **Operator UI + integration:** full synthetic walkthrough and the no-go, interrupted, unresolved-recovery paths;
    fresh migrations, full suite once, ruff/format/mypy/migration check, green Actions, `visit_dossier(visit)`
    reconstructs every Slice 2-4 row in time order with the manifest and revision at each step.

## 8. File ownership and sequence

| Owner | Files (exclusive) |
|---|---|
| Integration lead | `backend/config/**`, `platform_core/**` (lock order), `accounts/**` (permissions), `fleet/**`, `planning/**`, `readiness/**` (C1-C3), `templates/**` base, `tests/conftest.py`, `tests/execution_world.py`, existing tests changed by C1/C2, `execution/__init__.py`, `execution/apps.py`, `execution/custody.py` (read helpers), `execution/dossier.py`, `execution/management/**` (seed), `docs/**`, `.github/**`, `Makefile`, `README.md` |
| Engineer A, database | `execution/models.py`, `execution/migrations/**` (incl. C1/C3 SQL that touches planning/readiness tables, as execution migrations), `tests/test_execution_db.py` |
| Engineer B, logistics | `execution/logistics.py` (trip events, custody, verification), `execution/site_checks.py`, `execution/reconciliation.py`, `tests/test_execution_logistics.py`, `tests/test_execution_logistics_concurrency.py` |
| Engineer C, incidents | `execution/incidents.py`, `execution/sessions.py`, `execution/escalation.py`, `execution/policy.py`, `tests/test_execution_incidents.py`, `tests/test_execution_incident_concurrency.py` |
| Engineer D, UI | `execution/views.py`, `execution/urls.py`, `execution/templates/**`, `tests/test_execution_ui.py` |

Sequence: lead phase 0 (permissions, lock order, app shell, Python side of C1-C3, world fixture) → A (schema,
triggers, C1/C3 SQL, bypass tests) → merge A → B and C in parallel (own worktrees, own DBs `ops_s4_<x>`) → D on frozen
signatures → lead merges B, C, D in order, full suite once, CI, UI walkthrough → EM review.

## 9. Changes to Slice 0-3 behaviour (EM decisions)

- **C1 (changed per EM).** Once a `DepartureEvent` exists, the visit can never be cancelled, rescheduled or
  re-planned, and its reservations and vehicle trip can never be released, through any path. Python:
  `cancel_visit`/`plan_visit` refuse after the visit lock. SQL: `readiness_departed_visit_guard` no longer allows
  `cancelled`; new guards refuse UPDATE of `planning_reservation.released_at` and `planning_vehicletrip.released_at`
  for a departed visit. Completion is execution state. Administrative corrections are out of Slice 4 (a future
  append-only correction record); none repurposes cancellation.
- **C2 (approved).** `record_departure` locks the robot FOR UPDATE (same position) and the trip FOR UPDATE.
- **C3 (approved with change).** `ROBOT_NOT_AT_HUB` blocker in the evaluator, a refusal in
  `readiness_issue_authorization`, and a refusal in the departure trigger + `record_departure`: custody must be the
  D1 baseline hub or a verified hub receipt, and the robot must have no open run.
- **C4 (changed per EM).** Settings lock S (existing key, existing position) for: incident-policy create/approve/
  withdraw, `decide_severity`, `change_incident_status` to closed, and `record_site_check`. Not for
  `report_incident`: the protective hold never depends on a policy or the mode.

## 10. Still unresolved (company input; production restrictions)

Physical stop/recovery behaviour (B-01), lift/transport (B-03), real site-check and post-visit checklists (B-05),
severity scale and owners (B-07), incident hold release rules (HoldPolicy `incident` rule), teleoperation (B-11).
Production: incident policy must be `approved_procedure`; a synthetic site-check template cannot produce `go`.

## 11. Rev 2 corrections (EM review of `1a0ded5`)

| EM item | Correction |
|---|---|
| C1 | Permanent: no cancel / reschedule / release after departure, Python + SQL, tests through `_release_active` and direct SQL (section 9). |
| C2 | Unchanged, plus trip U at departure (the trigger now locks the trip). |
| C3 | Hub = baseline (no events, no runs ever) or verified hub receipt; open run always blocks; enforced at evaluator, issue function and departure. |
| C4 | Settings lock S for every policy- or mode-dependent decision (section 9). |
| A. Mandatory hold | `Incident.hold` NOT NULL; same-transaction proof in the insert trigger; unique hold per incident; `report_incident` never reads the policy. |
| B. Creation order | robot U → Hold → Incident → unaccounted custody → interruption/pause → audit; late reports never rewrite custody. |
| C. Closure | Reconcile and close both lock the run U and the triggers re-lock it; closure needs the run's latest reconciliation; no reconciliation after closure. |
| D. Stranded robot | Vehicle return ≠ robot return; left-on-site/unaccounted keep the trip unclosable; `continue_visit` refused with a hold or open interruption; no countersign override (countersign removed). |
| E. Custody endpoints | Custodian/receiver must be field crew / field users; departure creates an `administrative` assignment (custodian = revision lead, no receiver) that crew must verify before any further move; recovered moves need ops_lead and never touch the incident or hold. |
| Lock matrix | Complete per command incl. trigger effects; parent row per stream locked by command and trigger; C1 guards and cancel path mapped (section 5). |
| Races | Added: cancel vs departure, custody vs reconcile, reconcile vs close, settings switch vs policy-dependent decisions (section 7.2). |
