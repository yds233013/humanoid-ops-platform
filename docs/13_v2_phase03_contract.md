# Fleet Operations Platform v2: Phase 03 contract (multiple hubs, regional dispatch, offline field use)

Status: **draft for EM review**, 2026-11-25. This draft authorizes nothing by itself. Phase 02 closed and was released
for the fictional demo at tag `v2-phase02-release` (`48f3298`, application code `c03fa53`).

**Goal:** a regional ops lead can run three hubs from one place, and a crew can keep recording a visit when the phone
loses signal, without the platform ever deciding something safety-relevant from data it has not received.

- Journey J3 and the new dispatch board work across hubs and zones (Phase 00 rows 16 and 17; Phase 01 item P2-6
  "suggest the next free time", moved here by the EM).
- Journey J5 (field visit on a phone) becomes complete: it works offline for the steps allowed offline, queues them,
  shows what is waiting, and never shows success before the server confirms (Phase 00 section 8, Field row).

**Rules carried over:**

- Fictional data only. No real robot specifications, intervals, prices, travel times or safety approvals are invented;
  every threshold is a company decision through the existing approval workflow or a value labelled as demo data.
- No automatic safety approvals. No live customer messaging. No claim of real-world validation.
- No 500-robot claim before Phase 05. Usability stays UNVERIFIED without independent participants.
- Every existing safety suite stays green and none is weakened.

**EM direction (2026-11-25):** do not build offline support and multiple hubs at the same time. Fix the architecture
and invariants first (this document), then build in milestones that can each be tested on their own.

## 1. What exists today (facts from the code, 2026-11-25)

| Area | Present | Missing for Phase 03 |
|---|---|---|
| Hubs | A `Hub` table (code, name, area, time zone) from Phase 01 setup. Only `Charger.hub` and `StockItem.hub` reference it, and both are nullable. | Robots, vehicles, people, visits, work orders, charging sessions, runs and custody have no hub. Two places pick "the first hub" (`hub/charging.py`, `hub/0008`). |
| Custody | An append-only chain (hub, vehicle, site, unaccounted) enforced by triggers; "hub" is a place kind, not a specific hub. | Which hub a robot is at. Transfers between hubs. |
| Settings | One global `DeploymentSetting` table (stock minimums, maintenance gate, freshness, ...). One workspace time zone. | Per-hub values where a hub differs (stock minimums, opening hours). |
| Field commands | Depart, custody verify and transfer, trip events, site check, session start/pause/resume/stop, interruptions, incidents, post-visit inspection. Every `recorded_at` is database time. A trigger re-checks every rule on insert. | Offline use. Most field commands take no idempotency key, and a one-time form token is issued only when the page renders online. |
| Idempotency | `IdempotencyKey` (actor + key, stored response) in `run_command`; accepted by incident report, departure, authorization, inspection submit, recovery. | The same for every field command, plus a client sequence per run. |
| Client time | Only incident `occurred_at` and readiness evidence `observed_at` (bounded) are accepted from the client. | A rule for device time on queued commands. |
| Front end | Server-rendered templates, one small inline script, no service worker, no manifest, no JavaScript files. | An offline-capable field screen. |
| Planning | Suggest picks a free robot, crew and vehicle for one visit; travel minutes are stored per visit. | Zones, a travel-time matrix, hub-aware suggestions, the next free time (P2-6), a dispatch board, a regional pause. |

## 2. Scope

| In Phase 03 | Not in Phase 03 |
|---|---|
| Hubs as owners: every robot, vehicle, person (home hub), charger, stock item, work order and visit belongs to one hub | Hubs in different companies or tenants |
| Audited transfer of a robot, vehicle or stock between hubs (custody follows) | Automatic rebalancing between hubs |
| Zones per hub and a fixed travel-time matrix between zones, owner-provisioned, labelled demo values in the demo | A routing API or live traffic |
| Hub-aware Suggest and "next free time" (P2-6); a dispatch board by hub and zone where reassigning re-runs every check | Optimisation across the whole region |
| A regional pause: stop new departures for a hub or zone, with reasons, audited (Phase 00 row "area-wide events") | Wave recall orchestration beyond listing what is out |
| Offline field use for J5: the visit is cached on the phone, allowed steps are queued with a sync state, the server re-checks each one | Native mobile apps; photos and signatures (no file storage exists yet; decision 6) |
| A server sync endpoint with idempotency keys, per-run order and explicit rejection | Offline use for anything other than J5 field steps |
| Hub-scoped roles for hub technicians and crews (decision 3) | Agents (Phase 04); 500-robot performance (Phase 05) |
| Demo: three fictional hubs | Customer messaging, remote assistance consent and incident tiers (proposed for a later phase; decision 7) |

## 3. Safety invariants (binding for every milestone)

**I-1. The server is the only authority for safety state.** Holds, maintenance status, eligibility, custody,
readiness and departure authorization are decided only by the database functions and triggers that decide them
today. Nothing cached on a phone or computed from a queued command changes them.

**I-2. Offline actions may only record a fact or make things safer.** Three classes:

| Class | Steps | Offline? |
|---|---|---|
| **A. Unlocks robot movement or work** | Depart; start a session; resume a session; hand the robot over to the site; recover a robot | **Never.** These need a live server answer. Offline, the screen says "Needs a connection. Do not start." and nothing is queued. |
| **B. Makes things safer** | Pause; stop; report an incident or a problem; open an interruption | **Yes, queued.** The phone immediately shows "Robot must not operate" for its own user until the server confirms. When the server applies an incident it places the hold as today. |
| **C. Records what happened** | Arrived at site; site check result; left site; arrived at hub; back to vehicle; custody verification; observation notes | **Yes, queued.** Applied in order, each re-checked when it arrives; a step that fails its check is rejected, not bent. |

A site check recorded offline can never by itself let a session start: start is class A and re-checks the latest site
check on the server.

**I-3. A queued command is re-checked when it is applied, not when it was entered.** It is applied by the same
command and trigger as an online submission, with the server's state at that moment. If anything changed (a hold was
placed, the authorization was revoked, another crew member already recorded the step), it is **rejected with a
reason** and shown to the crew and the ops lead. It is never merged or silently adjusted.

**I-4. Exactly once.** Every queued command carries an idempotency key made on the phone and a per-run sequence
number. The server applies a key at most once; a repeat returns the stored result. A gap in the sequence holds later
commands for that run until the missing one arrives or is abandoned by the crew (then they are rejected in order).

**I-5. Two times, both kept, only one trusted.** `recorded_at` stays database time and is the time used by every gate.
The phone's time is stored as `device_observed_at`, shown as "phone time", bounded (not in the future beyond a set
skew, not before the run started) and never used for a safety decision, a counter or an expiry.

**I-6. Never claim success early.** Every queued step shows one of: waiting, sending, confirmed, rejected (with the
reason). The visit shows "N steps waiting to send". Only confirmed steps count anywhere on the server.

**I-7. A hub never changes safety state.** Holds, maintenance, inspections, incidents and authorizations belong to the
robot, not to its hub. A transfer never releases, copies or hides any of them. A transfer changes the robot's version,
so any authorization issued before it stops matching (the Slice 7 guard pattern).

**I-8. Hubs do not leak.** A hub technician or crew member sees and changes only their hub's robots, stock, chargers and
work, unless they hold a regional role. The database enforces the scope for writes (the trusted write boundary of
Phase 02 section 8.8), not only the views.

**I-9. Regional pause is a hard stop.** While a hub or zone is paused, no new departure authorization is issued and no
departure is recorded there (database gate). Robots already out continue to be recorded; nothing is auto-recalled.

## 4. Architecture choices

1. **Multiple hubs:**
   - An additive `hub_id` column (not null after backfill) on the owner tables.
   - A migration puts every existing row in the one existing hub, so a Phase 02 database keeps working unchanged.
   - Custody events to or from a hub name the hub. A robot's current hub is derived from custody, not stored twice.
2. **Transfers** are commands with SECURITY DEFINER functions:
   - The robot must have no open run, be verified at the sending hub and have no pending authorization.
   - The transfer is recorded as "in transit to hub X" and completed by a receipt at the receiving hub (the same custody pattern as a visit).
   - Stock transfers are movements out and in, as the Phase 02 ledger already supports.
3. **Zones and travel:**
   - Zones belong to a hub. A zone-to-zone travel matrix is an owner-provisioned setting.
   - A visit's travel time comes from the matrix when one exists, otherwise from the value stored on the visit (today's behaviour).
4. **Offline field screen:**
   - A progressive enhancement of the existing server-rendered field pages: a service worker caches the run page and its static assets, and IndexedDB holds the queue.
   - No separate front-end application (Phase 00 decision 1 stands).
   - Without JavaScript or a service worker, the pages work online exactly as today.
5. **Sync endpoint:**
   - `POST /api/field/sync` takes a batch of commands, each with: run, sequence, idempotency key, command name, payload and phone time.
   - It applies them one by one through `run_command` with the existing permission checks.
   - It returns a per-command result: confirmed, rejected with a reason, or held for a missing sequence.
   - Authentication is the user's session. A queue is bound to the user and the run, and the server refuses a command whose run is not the crew's.
6. **One-time form tokens** stay for online forms. Queued commands use the idempotency key instead, because a token
   cannot be issued while offline. The key replaces the token only on the sync endpoint.
7. **Lost or shared phones:**
   - Signing out, or an expired session, clears the queue after warning about unsent steps.
   - The ops lead can see each run's unsent-step count, reported at the last contact.
   - An admin can end a user's sessions; queued commands from an ended session are rejected.

## 5. Milestones (sequential; each reviewed by the EM before the next starts)

| Milestone | Delivers | Exit evidence |
|---|---|---|
| **03A Hub ownership** | `hub_id` on owner tables with backfill; hub-scoped roles and database write scope (I-8); robot, vehicle and stock transfers with custody (I-7); per-hub settings where needed (stock minimums, opening hours); hub filter on Today, Hub, Fleet, Dispatch; the "first hub" shortcuts removed | Backfill test on a Phase 02 database copy; transfer tests (open run refused, holds kept, authorization invalidated, both orders against a concurrent booking); cross-hub write refused by direct SQL as the runtime role; all safety suites green |
| **03B Regional dispatch** | Zones, travel matrix, hub-aware Suggest, "next free time" (P2-6), a dispatch board by hub and zone with reassign that re-runs every check, regional pause (I-9) | Suggest and next-free-time oracle tests; reassign cannot bypass eligibility (direct SQL); pause gate tests in planning, authorization and departure; perf on a 3-hub fixture |
| **03C Field command protocol (server only)** | Idempotency keys on every field command; `device_observed_at` with bounds (I-5); the sync endpoint with per-run sequence (I-4); class A refusal on the endpoint (I-2); rejection reasons; unsent-count reporting | Failure-injection tests (section 6) run against the endpoint without any front end |
| **03D Offline field screen** | Service worker, cached run page, IndexedDB queue, sync state on every step and on the visit (I-6), "Needs a connection" for class A, local "must not operate" after a class B step | Browser tests with the network switched off and on at 375 px; the same failure cases seen from the phone |
| **03E Acceptance** | Three-hub demo; J3 and the dispatch board across hubs; J5 walked offline in a real browser at 375 px with the network cut; J8 per hub; J2 to J4 re-run; full CI and three perf runs; release rehearsal as in Phase 02 | Same closure bar as 02E |

## 6. Required failure-injection and security tests (03C and 03D)

1. The same batch is sent twice, and the same command is sent in two batches: each key is applied once.
2. Sequence 3 arrives before 2: 3 waits; 2 arrives, then both apply in order; 2 is abandoned, then 3 is rejected.
3. A hold is placed while the crew is offline; a queued "arrived at site" still applies (class C), but a later start
   (class A) is refused online.
4. The authorization expires or is revoked while the phone is offline: nothing queued can depart or start.
5. Two phones of the same crew record the same step offline: one is confirmed, the other is rejected as already recorded.
6. The phone clock is wrong (hours ahead or behind): gates use database time; phone time outside the bounds is refused
   with a reason.
7. A batch for a run that is not the user's, or after the user was signed out by an admin: refused, nothing applied.
8. A class A command posted directly to the sync endpoint: refused.
9. A queued incident: the hold is placed when it arrives, the session is paused if it is still active, and the order
   relative to a queued stop is kept.
10. A transfer racing a booking or an authorization, in both orders, with the waiter observed in `pg_locks`.
11. A cross-hub write by direct SQL as the runtime role, with a forged, NULL or empty actor: refused.
12. A regional pause racing an authorization and a departure, in both orders.

## 7. Database changes (all additive and reversible)

- `hub_id` on robots, vehicles, people (home hub), visits, work orders, charging sessions and charger reservations.
  Backfilled to the existing hub, then made not null.
- Custody events: optional `hub_id` on hub-end events. The current-custody function returns the hub.
- New tables:
  - `platform_core_zone` and `planning_travel_time` (zone pair, minutes, source);
  - `fleet_hub_transfer` (append-only);
  - `operations_region_pause` (append-only, with a lift record);
  - `execution_field_command` (key, run, sequence, phone time, received time, result, reason).
- SECURITY DEFINER functions for transfers, pause and lift, and applying a field command. EXECUTE is granted to the
  runtime role only. Every new safety-relevant table is SELECT-only for the runtime role (Phase 02 section 8.8).

## 8. Performance budgets

- Today, Hub, Dispatch board and readiness list: p95 at most 2 s, with a 400 ms target, on a 3-hub fixture of 200
  robots in total. Statement-count tests stay constant in the number of hubs.
- Sync endpoint: p95 at most 1 s for a batch of 20 commands.
- The hosted cold-request latency of Today and readiness (about 2.3 s at the Phase 02 release) stays in the Phase 05
  backlog. Phase 03 must not make it worse; it is measured on the release rehearsal.

## 9. Decisions requested from the EM

1. **Order:** 03A, then 03B, then 03C, then 03D, then 03E, as proposed. Hubs first, because the offline queue is bound to a
   run and a crew, and both gain a hub. Or offline first?
2. **Offline classes (I-2):** confirm that depart, start, resume, handover to site and recovery are never offline. The
   alternative, a pre-issued offline start permit, is not proposed: it would move a safety decision onto the phone.
3. **Hub scope:**
   - Proposed: hub technicians and crews are scoped to their home hub and the ops lead is regional.
   - Alternative: everyone sees every hub, and only writes are scoped.
4. **Robot location:** derived from custody (proposed), or a separate "home hub" plus "current hub"? Proposed: a home hub
   (ownership, owner setting) plus the current hub from custody, and planning uses the current hub.
5. **Travel matrix:** owner-provisioned fixed minutes per zone pair, labelled demo values in the demo (proposed, as in
   Phase 00 decision 2).
6. **Photos and signatures offline:** out of Phase 03 (proposed; there is no file storage, and adding it is its own
   security decision), or in?
7. **Customer-operations items from the Phase 00 plan** (help-request queue, remote-assistance consent, incident tiers):
   proposed for a later phase, so Phase 03 stays focused on hubs, dispatch and offline. Regional pause stays in 03B
   because it is a safety stop.
8. **Demo:** three fictional hubs of 50, 30 and 20 robots (100 in total), each with its own crews and stock, rebuilt
   daily as today.
