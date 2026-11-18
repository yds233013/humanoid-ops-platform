# Slice 4 Contract: Field Execution and Incident Response

Status: DRAFT for engineering review · 2026-11-18 · baseline `9228ae8` (529 tests). Not frozen until the EM
approves. Engineers do not change names, signatures, lock order or file ownership after freeze; a needed change
is reported to the integration lead with the exact reason.

Everything is SYNTHETIC. The system records what authorized people report. It does not move robots, stop them,
operate them remotely, or describe how to recover one physically. No household names, addresses or contact
details: sites stay opaque codes, participants stay pseudonymous refs (`P1`).

## 0. Principles

1. **Custody, visit execution and incident consequences are separate records.** None is inferred from another.
2. **Append-only.** Every Slice 4 table is insert-only (`reject_mutation` on UPDATE/DELETE/TRUNCATE). State is
   derived from the latest row(s), never stored in an editable status column.
3. **Every incident holds the robot, in the same transaction.** No policy decides whether to hold; a policy can
   only classify severity. Release stays with the existing fleet hold-release workflow.
4. **Unknown blocks.** Missing site check, unknown adult count, unknown custody, unresolved reconciliation: all block
   the next step. A No-Go or block never prevents reporting an incident or documenting a return.
5. **The database enforces the invariants** for direct `ops_app` SQL (triggers + privileges); Python commands give
   the explained answer.
6. **No background workers for correctness.**

## 1. The four decisions (answered from the code)

### D1. Custody authority
**Finding:** there is no custody table. `fleet.CustodyEvent` appears in the approval package model (section 3) but
was never built (`grep -ri custody backend/` finds nothing outside docs). `planning.TripCargo` is the *planned*
load of a trip (one row per trip+robot, checked against `max_robots_per_trip` and capacity); it is not custody.

**Decision:** one new append-only table `execution.CustodyEvent` is THE custody history. A robot's custody is its
latest custody event; a robot with no event is at the hub (pre-Slice 4 baseline). `TripCargo` stays the plan and
is used by reconciliation ("cargo matches"): the robot that left on the trip must be the trip's cargo robot.
No second custody history exists anywhere.

### D2. Incident policy fallback
**Finding:** `fleet._create_hold` already places holds with no policy dependency: it copies the newest
`HoldPolicy`'s rule for the source type if one exists, otherwise the hold has no release slots and is
**unreleasable** until an approved policy provides a rule (fleet migrations 0003/0004). `Hold.SOURCES` already
contains `incident`.

**Decision:** every incident creates a robot-scope blocking `Hold(source_type='incident', source_ref=<incident
code>)` through `_create_hold`, in the same transaction, always. There is no "safety-significant" judgement and no
event-type list that can skip the hold (a non-robot matter is a session *observation*, not an incident). The
`IncidentPolicy` (new, versioned) only supplies the severity scale and escalation requirements. With no active
approved incident policy, severity stays `unclassified` and the hold is still placed. Release rules for incident
holds stay in the existing `HoldPolicy`; Slice 4 invents none.

### D3. Trip state reuse
**Finding:** `planning_trip_guard` forbids every change to a `VehicleTrip` except setting `released_at` once, and
`planning.services._release_active` (used by cancel/re-plan) releases trips and reservations. `released_at` means
"no longer planned/reserved", not "back at base".

**Decision:** `VehicleTrip` and `TripCargo` are reused unchanged (no new columns). Physical trip progress is an
append-only `execution.TripEvent` sequence on the trip; outbound departure is the existing
`readiness.DepartureEvent` (no second departure). Closure is `execution.TripClosure` (one-to-one with the trip).
To stop a departed visit's plan from being released out from under a robot in the field: a departed visit with an
open execution run cannot be cancelled (see C1 in section 9).

### D4. Lock compatibility
**Finding:** `platform_core.locks.ORDER` covers site → pattern → robot → person → vehicle → visit → readiness
policy → authorization; `planning_vehicletrip` is locked only by the cargo trigger (last). Hold insert/release
triggers `UPDATE fleet_robot` (needs robot U held). `record_departure` holds robot **S**.

**Decision:** extend the order (section 5) with `planning_vehicletrip` after visits and the new execution tables
after authorizations. Every Slice 4 writer that can change whether work may continue locks the robot **U** (holds
bump the robot version; custody changes are robot-scoped). Checkers take robot **S**. No command locks a later
table then an earlier one; triggers only touch rows the command already holds or later rows. Matrix in section 5.

## 2. Domain model (`execution` app)

All rows: `recorded_by` (user), `recorded_at` (DB time, forced by trigger to `clock_timestamp()`), immutable.

| Entity | Fields | DB rules |
|---|---|---|
| `ExecutionRun` | `code` (EX-0001), `departure` (1:1 readiness.DepartureEvent), `visit`, `revision`, `robot`, `manifest` (robot's manifest at departure), `trip` (planning.VehicleTrip) | Created ONLY by an AFTER INSERT trigger on `readiness_departureevent` (same transaction as the departure). ops_app has no INSERT. Unique per departure and per visit. |
| `CustodyEvent` | `robot`, `seq` (1,2,...), `from_kind`/`to_kind` in `hub, vehicle, site, unaccounted`, `trip` (when vehicle), `visit` (when site), `run`, `custodian` (planning.Person, responsible after the transfer), `received_by` (Person acknowledging receipt; NULL only for `to_kind='unaccounted'`), `incident` (required for `unaccounted`), `note` | Unique (robot, seq). Trigger forces `seq = previous + 1` and requires `from` = previous `to` (no event = hub). Allowed moves below. |
| `TripEvent` | `trip`, `run`, `seq`, `kind` in `arrived_site, departed_site, arrived_hub` | Unique (trip, seq); unique (trip, kind); order arrived_site → departed_site → arrived_hub; trip must have a departure. |
| `SiteCheck` | `run`, `template`, `adults_present` (int ≥ 0, required), `consent_ok` (computed), `conditions` (JSON from the synthetic checklist template, `{item_id: "ok"|"not_ok"}`), `result` `go`/`no_go` (computed), `verifier` (Person, lead or second of the revision) | Trigger recomputes `result`: go only if robot custody = site of this visit, trip `arrived_site`, every condition `ok`, `adults_present ≤ valid consents covering the revision's scopes`, site `expected_adults` known. Mismatch → refused. |
| `SessionEvent` | `run`, `seq`, `kind` in `start, pause, resume, stop, observation`, `site_check` (for start/resume), `interruption` (for an incident-driven pause), `note` | Trigger enforces the state machine and the start/resume gates (section 3). |
| `SiteCheckTemplate` | `version`, `items` JSON `[{"id","label"}]`, `source` `synthetic_demo`/`approved_procedure` | immutable; SiteCheck.conditions keys must equal the template's item ids |
| `Interruption` | `code` (IN-0001), `run`, `source` `incident`/`operator`, `incident` (when source=incident), `reason` | Several may be open at once; each closes only by its own `InterruptionResolution`. |
| `InterruptionResolution` | 1:1 `interruption`, `note` (non-empty) | Refused while the interruption's incident hold is unreleased. |
| `IncidentPolicy` | `version`, `rules` JSON `{"severity_scale": [..low→high], "escalation_required_from": "<level>", "downgrade_roles": ["safety_reviewer"]}`, `source` `synthetic_demo`/`approved_procedure`, `lock_version` | Same lifecycle as ReadinessPolicy: engineering + safety slots, no self-approval, withdrawal; active = highest fully approved, no fallback; synthetic usable only in demo mode. |
| `IncidentPolicyApproval` / `IncidentPolicyWithdrawal` | as readiness | as readiness |
| `Incident` | `code` (IC-0001), `robot`, `manifest` (forced = robot's current manifest), `run` (nullable: incidents can be reported at the hub or after closure), `visit` (nullable, = run.visit when run given), `event_type` (`fall, contact, unexpected_motion, stopped_unexpectedly, damage, heat_or_smoke, robot_unaccounted, other`), `description`, `occurred_at` (reported; ≤ now), `reported_at` (DB time), `knowledge_source` `witnessed`/`told_by_crew`/`found_later`, `hold` (fleet.Hold) | Trigger: `hold` must be robot scope, blocking, unreleased, `source_type='incident'`, `source_ref=code`, robot = incident robot, `created_txid = pg_current_xact_id()` (same pattern as inspection INV-1). Unique index one incident-hold per source_ref. |
| `FaultObservation` | `incident`, `component` (free text), `status` `suspected`/`confirmed`, `evidence_ref` | append-only; a later row supersedes an earlier one for the same component |
| `SeverityDecision` | `incident`, `level`, `policy`, `reason` | `level` must be in the active policy's scale; first decision free for triagers; a level lower than the latest needs a role in `downgrade_roles` and a decider ≠ reporter. Without an active policy: refused (stays `unclassified`). |
| `IncidentStatusChange` | `incident`, `to_status` | reported (implicit) → triaged → response_in_progress → resolved → closed; one row per step, in order. `closed` requires every escalation acknowledged. Never touches the hold. |
| `IncidentEscalation` / `EscalationAck` | `incident`, `to_role`, `to_person` (optional), `requested_action`; ack 1:1 with `acknowledged_by` | ack by a user holding `to_role` (or `to_person`) |
| `RecoveryDisposition` | `run`, `decision` `continue_visit`/`end_visit_return`/`robot_left_on_site`/`robot_unaccounted`, `reason`, `incident` (required for the last two), `countersigned_by` (engineering_lead, required when the robot has an unreleased blocking hold) | operational decision only, no physical instructions |
| `ReturnReconciliation` | 1:1 `run`, `hub_receipt` (CustodyEvent to hub), `cargo_matches` (computed), `post_visit_inspection` (fleet.Inspection, nullable), `discrepancies` JSON list, `outcome` `reconciled`/`unresolved` | Trigger: `reconciled` only if custody = hub via `hub_receipt` (its `received_by` set), cargo_matches, a post-visit inspection for this robot with `visit_ref = visit.code` submitted after the hub receipt (any result: a FAIL already holds the robot), `discrepancies` empty. Otherwise only `unresolved`. A later row may supersede `unresolved` with `reconciled`; nothing supersedes `reconciled`. |
| `TripClosure` | 1:1 `trip`, `reconciliation` | Trigger: trip `arrived_hub`; the reconciliation is `reconciled` and is the latest for the run. |

### Custody moves allowed (trigger)
| From → To | Requires |
|---|---|
| hub → vehicle | only from the ExecutionRun trigger at departure (trip = run.trip) |
| vehicle → site | trip has `arrived_site`; site = run.visit |
| site → vehicle | same trip; trip not yet `departed_site` |
| vehicle → hub | trip has `arrived_hub` |
| any → unaccounted | an Incident `robot_unaccounted` for this robot in the same transaction |
| unaccounted → hub / site / vehicle | `received_by` set; an ops_lead recorder |

`departed_site` needs custody = vehicle on this trip, OR a `RecoveryDisposition` `robot_left_on_site`/
`robot_unaccounted` for the run. A robot can never be in custody of two places: one sequence per robot.

## 3. State machines

**Run / visit execution (derived):** departed → arrived (TripEvent) → checked in (latest SiteCheck go / no_go) →
session (below) → left site → at hub → reconciled → trip closed. A cancelled visit cannot hide a run (section 9 C1).

**Session (derived from SessionEvent):** `not_started → active ⇄ paused → stopped`. `stop` from active or paused;
`stopped` is terminal; `observation` allowed in any state, also after stop.

Gates for `start` and `resume`, re-checked by the command under locks AND by the trigger:
- the referenced SiteCheck is the latest for the run, is `go`, and (for `resume`) was recorded after the most
  recent pause and after every interruption opened on the run;
- no open interruption on the run;
- no unreleased blocking hold on the robot or its current pattern;
- robot custody = site of this visit;
- valid consents covering the revision's scopes ≥ the site check's `adults_present` (consent withdrawn after the
  check blocks);
- recorder is the revision's lead or second, or an ops_lead.

**Incident:** status and severity are independent. reported → triaged → response_in_progress → resolved → closed.
Corrections are new rows. Resolving or closing never releases a hold (no code path writes `fleet_hold` from
execution except the placement in `report_incident`).

## 4. Commands

All through `run_command` (permission, idempotency, audit), locks via `lock_guards` before reads, re-read after
locking, `RetryTransaction` if the run/visit changed between read and lock. Return dicts like existing services.
"Field crew" = revision lead or second (checked in Python and by the trigger via `has_active_role` + person ids).

| Command | Owner | Permission | Locks |
|---|---|---|---|
| (trigger) open run + hub→vehicle custody on departure | A | n/a | inside `record_departure` (robot now **U**, section 9 C2) |
| `record_trip_event(actor, *, run_code, kind)` | B | `execution.record_field_event` | robot U, visit S, trip U, run U |
| `transfer_custody(actor, *, run_code, to_kind, custodian_code, received_by_code, note="")` | B | `execution.record_field_event` | robot U, persons S, visit S, trip U, run U |
| `record_site_check(actor, *, run_code, adults_present, conditions, verifier_code)` | B | `execution.record_field_event` | site S, robot S, persons S, visit S, run U |
| `reconcile_return(actor, *, run_code, inspection_code=None, discrepancies=[])` | B | `execution.reconcile` | robot U, visit S, trip U, run U |
| `close_trip(actor, *, run_code)` | B | `execution.close_trip` | robot S, visit S, trip U, run U |
| `record_session_event(actor, *, run_code, kind, site_check_id=None, note="")` | C | `execution.record_field_event` | site S, pattern S, robot S, persons S, visit S, run U |
| `open_interruption(actor, *, run_code, reason)` / `resolve_interruption(actor, *, code, note)` | C | `execution.record_field_event` / `execution.triage_incident` | robot S, run U |
| `report_incident(actor, *, robot_code, event_type, description, occurred_at, knowledge_source, run_code=None, idempotency_key=None)` | C | `execution.report_incident` | robot U, visit S (if run), incident policy S (active), run U (if run) |
| `decide_severity`, `change_incident_status`, `escalate_incident`, `acknowledge_escalation`, `add_fault_observation` | C | `execution.triage_incident` / `execution.change_severity` / `execution.close_incident` / escalation target role | incident policy S, run S (if any), incident U |
| `record_disposition(actor, *, run_code, decision, reason, incident_code=None)` + `countersign_disposition` | C | `execution.record_disposition` / `execution.countersign` | robot S, visit S, run U |
| `create/approve/withdraw_incident_policy` | C | `execution.manage_incident_policy` / `approve_incident_policy` | incident policy U |

`report_incident`, in ONE transaction: lock → validate → `_create_hold(source_type='incident', source_ref=code)`
→ insert Incident (hold FK) → if a session on the run is `active`: insert Interruption(source=incident) and a
`pause` SessionEvent linked to it; if `paused`/`not_started`: insert the Interruption only → (`robot_unaccounted`:
insert custody → unaccounted) → audit → commit. Any failure rolls everything back. Allowed after stop, after the
trip closed, and with no run at all. Idempotent on key; duplicate key returns the original incident.

New permissions (`accounts/roles.py`, lead):

| Permission | Roles |
|---|---|
| `execution.read` | ops_lead, field_operator, engineer, engineering_lead, safety_reviewer, founder |
| `execution.record_field_event` | ops_lead, field_operator |
| `execution.report_incident` | ops_lead, field_operator, engineer, engineering_lead, safety_reviewer, founder |
| `execution.triage_incident` | ops_lead, engineering_lead, safety_reviewer |
| `execution.change_severity` | engineering_lead, safety_reviewer (downgrade limited further by policy `downgrade_roles`) |
| `execution.close_incident` | engineering_lead, safety_reviewer |
| `execution.record_disposition` | ops_lead |
| `execution.countersign` | engineering_lead |
| `execution.reconcile`, `execution.close_trip` | ops_lead |
| `execution.manage_incident_policy` | engineering_lead |
| `execution.approve_incident_policy` | engineering_lead, safety_reviewer |

## 5. Lock order and matrix

```
role grants -> settings (advisory S) -> site -> pattern -> robot -> person -> vehicle -> visit
  -> vehicle trip -> readiness policy -> departure authorization
  -> incident policy (execution_incidentpolicy) -> execution run (execution_executionrun)
  -> incident (execution_incident)
```
`lock_guards` gains `update_/share_trips`, `_incident_policies`, `_runs`, `_incidents` (lead). Ascending id within a
table. Append-only child rows (custody, trip/session events, checks, decisions) are not lock targets: their parent
(robot / trip / run / incident) is held U by the writer.

| Trigger | Writes / locks | Already held? |
|---|---|---|
| departure insert → run + custody insert | inserts run, custody | robot U (C2), visit S, trip (later, locked U by trigger), authorization S |
| incident insert (hold check) | reads hold | hold inserted by same command; robot U |
| hold insert (existing) | UPDATE fleet_robot version | robot U |
| custody / trip event / session / site check guards | read latest rows, consents, holds | parent U; site/robot S where read |
| trip closure guard | reads reconciliation | trip U, run U |
| severity / status guards | reads policy, roles | incident U, policy S |

Cycles checked: no command locks trip/run/incident and then robot, person, visit or policy. Incident-policy
approval locks only the policy. Pattern-scope hold placement (existing) locks pattern → robots and nothing later.
Settings lock is not taken by Slice 4 commands (they read only `demo_mode` for incident policy usability; the
synthetic policy is refused at decision time, and severity carries no departure consequence) — C4 asks whether to
add it.

## 6. Database bypass protections (direct `ops_app` SQL tests)

- No INSERT on `execution_executionrun` (trigger-created only); no UPDATE/DELETE/TRUNCATE on any execution table.
- Incident without its same-transaction hold, with a hold from an earlier transaction, a released hold, another
  robot's hold, a non-incident hold → refused.
- Custody: skipped seq, wrong `from`, two concurrent "next" events (unique), vehicle→site before arrival,
  unaccounted without incident → refused.
- Session start/resume with: no go check, stale check (before pause/interruption), open interruption, active
  robot or pattern hold, custody not at site, consent withdrawn below adults present → refused.
- Trip closure without reconciled reconciliation / before arrived_hub / with a later unresolved row → refused.
- Reconciliation `reconciled` with missing inspection, inspection before hub receipt, wrong visit_ref, cargo
  mismatch, non-empty discrepancies → refused.
- Severity downgrade by a user without the role, by the reporter, with no active policy → refused.
- Departed visit with an open run cannot be cancelled; its trip cannot be released.
- Recorder role checks use `has_active_role` on the recorded user id; the DB still cannot know the human behind
  `ops_app` (documented, as in Slice 3).

## 7. Acceptance tests (closure bar)

Deterministic barriers (`checkpoint` hooks after first read and after locks), both orders, real Postgres, no sleeps.

1. **Incident atomicity:** incident + hold + interruption + pause + audit commit together; injected failure in hold
   insert, interruption insert and audit write each roll back everything. Late report (after stop, after trip
   closed, no run) still holds.
2. **Incident concurrency:** report vs session start/resume (both orders: start refused if report first; session
   paused by the report if start first); report vs reconcile; report vs close_trip; duplicate idempotent reports.
3. **Custody integrity:** concurrent transfers (one wins), gaps, double custody, premature closure.
4. **Site checks:** no_go blocks start; consent withdrawal (site U) vs start (site S) both orders; unknown expected
   adults blocks; adults present > consents blocks.
5. **Session lifecycle:** every invalid transition; resume needs a newer go check; stopped never restarts.
6. **Severity and policy:** no policy → unclassified + hold still placed; draft/withdrawn/synthetic-in-production
   policy refused; downgrade rules; append-only history.
7. **Recovery and return:** robot left on site / unaccounted keeps run open and trip unclosable; hold survives
   incident resolve/close; failed post-visit inspection holds; trip closes with a held robot (valid).
8. **Bypass:** section 6, as ops_app.
9. **Operator UI:** full synthetic walkthrough (depart → arrive → custody → go → start → incident → pause →
   return → inspect → reconcile → close) plus no-go, interrupted, unresolved-recovery paths; no wording that
   suggests clearance.
10. **Integration:** fresh migrations, full suite one run, ruff/format/mypy/migration check, green Actions, dossier
    (`visit_dossier(visit)` reconstructs every Slice 2-4 row in time order with the manifest at each step).

## 8. File ownership and sequence

| Owner | Files (exclusive) |
|---|---|
| Integration lead | `backend/config/**`, `platform_core/**` (lock order), `accounts/**` (permissions), `fleet/**`, `planning/**`, `readiness/**` (C1-C3 changes), `templates/**` base, `tests/conftest.py`, `tests/execution_world.py`, `execution/__init__.py`, `execution/apps.py`, `execution/management/**` (seed), `execution/dossier.py`, `docs/**`, `.github/**`, `Makefile`, `README.md` |
| Engineer A, database | `execution/models.py`, `execution/migrations/**`, `tests/test_execution_db.py` |
| Engineer B, logistics | `execution/logistics.py` (trip events, custody), `execution/site_checks.py`, `execution/reconciliation.py`, `tests/test_execution_logistics.py`, `tests/test_execution_logistics_concurrency.py` |
| Engineer C, incidents | `execution/incidents.py`, `execution/sessions.py`, `execution/escalation.py`, `execution/policy.py`, `tests/test_execution_incidents.py`, `tests/test_execution_incident_concurrency.py` |
| Engineer D, UI | `execution/views.py`, `execution/urls.py`, `execution/templates/**`, `tests/test_execution_ui.py` |

Sequence: lead phase 0 (permissions, lock order, app shell, C1-C3, world fixture) → A (schema + triggers + bypass
tests) → merge A → B and C in parallel (own worktrees, own DBs `ops_s4_<x>`) → D on frozen signatures → lead merges
B, C, D in order, full suite once, CI, UI walkthrough → EM review.

## 9. Changes to Slice 0-3 behaviour (need approval)

- **C1.** A departed visit whose run is not closed cannot be cancelled (today Slice 3 allows cancel after
  departure, which would release the trip and the robot's reservations while the robot is in the field). After
  trip closure, cancel stays allowed for record-keeping. Trigger on `planning_visit` + on `planning_vehicletrip`
  release.
- **C2.** `readiness.record_departure` locks the robot **U** instead of S (its trigger now writes custody). Stronger
  lock, same order; the Slice 3 race tests are rerun unchanged.
- **C3.** New readiness blocker `ROBOT_NOT_AT_HUB` (evaluator) + matching refusal in `readiness_issue_authorization`:
  a robot whose custody is not hub, or that has an open run, cannot be authorized for another visit.
- **C4 (question).** Should incident-policy-dependent commands take the settings lock (demo→production switch)?
  Proposal: no; severity has no departure consequence, and the synthetic policy is refused at write time.

## 10. Still unresolved (company input; production restrictions)

Physical stop/recovery behaviour (B-01), lift/transport (B-03), real site-check and post-visit checklists (B-05),
severity scale and owners (B-07), incident hold release rules (HoldPolicy `incident` rule), teleoperation (B-11).
Production: incident policy must be `approved_procedure`; site-check conditions template stays synthetic and
production refuses `go` with a synthetic template.
