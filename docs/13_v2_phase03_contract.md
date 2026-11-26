# Fleet Operations Platform v2: Phase 03 contract (multiple hubs, regional dispatch, offline field use)

Status: **approved by the EM 2026-11-25** (sections 10 and 12 binding). **03A CLOSED at `c8fd239`.** The 03B interface is frozen in 13.5. Phase 02 closed and was released
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

## 10. EM review 2026-11-25: conditionally approved at `92d0db5`, binding corrections

All eight decisions in section 9 are approved as proposed. Travel times fail closed (H). The existing incident reporting
stays. Scope is one company with three hubs, not multiple tenants. Phase 03 adds no new authority for field devices and
weakens no existing authorization, custody, maintenance or hold gate. These corrections are binding. Where they differ
from sections 3 to 8, they win.

### 10.A Home hub, current location and dispatch eligibility are three separate things

- **Home hub:** administrative ownership. It changes only through an authorized, audited reassignment.
- **Current location:** a typed state derived from custody and transfers. The states are:
  - `hub(H)`
  - `vehicle(V)`
  - `site(S)`
  - `transfer_in_transit(H1, H2)`
  - `unaccounted`
  - `unknown`

  A robot at a site or in a vehicle is never given a made-up current hub from its home hub.
- **Dispatch eligibility:** decided on the server from the current location, the home hub and the existing safety and
  availability rules. Only `hub(H)`, verified, can make a robot physically available at hub H. A robot in transit,
  at a site, unaccounted or unknown is not dispatchable at either hub.

### 10.B Hub authority at the database boundary

- **Grants:** hub scope comes from explicit, audited, revocable **hub-qualified role grants** (person, role, hub).
  A home hub is not proof of authority. The ops lead acts regionally through a regional grant. A hub technician needs
  an active grant for the affected hub.
- **Checks in every guarded write:** each guarded database operation checks the actor, the grant, the hub and the
  target record together, under transaction-safe locking. This covers `run_command` permission checks and the Phase 02
  stock SECURITY DEFINER functions, which gain the hub dimension.
- **Negative tests:** an authenticated technician
  - supplies another hub's record id;
  - forges an actor id;
  - changes their own home hub (gains nothing);
  - loses their grant while a write is in progress (both orders, with the waiter seen in `pg_locks`).

### 10.C Transfer state machine

- **States:** `at_source → in_transit → received_at_destination`, plus two terminal states:
  - `abandoned`: an audited decision by the ops lead; the robot returns to `at_source` by an explicit receipt;
  - `receipt_refused`: it stays `in_transit` with a reason until resolved.
- **Incidents in transit:** an incident in transit places the hold as today. The transfer stays in transit, and the
  receipt still needs a verified custody step.
- **Concurrent receipts:** of two simultaneous receipts, exactly one wins.
- **Locking:** robot and vehicle transfers take locks in the same order as planning, authorization and custody.
  A transfer cannot start while the robot has a booking or authorization that the transfer would invalidate without
  saying so; such bookings are shown as blocked.
- **Holds:** an existing hold survives every stage. A receipt never clears a hold.
- **Stock:** a transfer is a ledger movement out at the source and in at the destination. While in transit, the stock
  counts at neither hub as available. Reconciliation holds at every stage.

### 10.D Safety reports are never stuck behind a sequence gap

- **Separate channel:** class B incidents, interruptions and stop requests use a safety-report channel. It keeps
  idempotency, audit and causality (each report names the last confirmed sequence it saw). The server applies a
  protective hold without waiting for ordinary sequence gaps.
- **Stop when the session state does not allow it:** a queued stop still obeys the execution state machine. If the
  stop cannot be applied as recorded, the server keeps or creates a protective safety state (a hold and an
  interruption). It never claims the session stopped.
- **Required test:** sequence 2 is missing and sequence 3 reports an incident. On reconnect the hold is placed
  without waiting for 2.

### 10.E At-most-once application, not exactly-once delivery

The network cannot deliver exactly once. **I-4 is replaced by:** each idempotency key is applied at most once, and
committed. A retry returns the same stored response. Also:

- Same key, different command or payload: conflict, nothing runs.
- Same run and sequence, different key: conflict, nothing runs.
- Two simultaneous sync requests claiming the same sequence: one transactional outcome.
- A failed or rejected command keeps a durable final result with its reason.
- An explicit abandonment is an audited resolution that an old, delayed batch can never overwrite.
- The key claim, the sequence claim and the domain command commit in **one transaction**. A crash cannot commit the
  operation without its receipt, or the receipt without the operation.

### 10.F Offline client trust and storage

- **Cached data:** only previously authorized, minimal run data is cached. Never credentials, session cookies or
  unnecessary personal information.
- **Queue ownership:** a queue belongs to one user, one run and one device identity. Another account on the same
  device cannot inherit it.
- **Session expiry or remote revocation:** blocks sync until the user signs in again and is authorized again.
- **Updates and restarts:** a service-worker update or a browser restart never silently drops unsent steps.
- **Deleting the queue:** deleting a queue or signing out needs a clear warning and, where possible, an auditable
  reconciliation path.
- **What the ops lead sees:** the server cannot know the live number of unsent steps, so the ops lead's view says
  "last reported unsent count" with its time.

### 10.G Regional pause semantics

- **Which pause applies:** the affected hub and zone come from the authoritative visit, destination and departure
  context, never from a client-supplied hub id.
- **Database checks:** new authorizations and departures check active hub and zone pauses at the database boundary.
- **Lock order:** pause activation, authorization and departure use one common lock order. Neither can get past an
  uncommitted pause.
- **What a pause does not do:**
  - it never erases recorded departures;
  - robots already out keep reporting incidents, custody and protective actions.
- **No bypass:** a cross-hub reassignment cannot get around a paused hub or zone.
- **Tests:** both sides of the race, with the blocked transaction observed waiting.

### 10.H Missing travel times are explicit

- **Existing visits:** existing Phase 02 single-hub visits keep their recorded travel time through the migration.
- **New dispatch:** for new cross-hub dispatch, a missing zone pair gives "Travel time unknown". It never gives an
  invented estimate, and never the old per-visit fallback.
- **Auditability:** travel values are versioned. Dispatch can say which approved matrix value produced a suggestion.
  Demo values stay labelled synthetic.

### 10.I Added 03A exit gates

1. Every ownership migration backfills existing Phase 02 data correctly. No record that needs a hub is left with none.
2. Cross-hub visibility and writes are tested through ordinary pages, services and direct SQL. A hub technician cannot
   gain regional access by changing their home hub.
3. A robot's transfer state is distinguishable from custody at a known hub. A transfer cannot create a second
   dispatchable location.
4. Robot, vehicle and stock transfers keep their safety, custody and inventory invariants in both concurrency orders.

### 10.J Authorization status

- 03A (hub ownership, custody and transfers): authorized once these amendments are committed.
- 03B, 03C, 03D and 03E: each needs its own closure review of the previous milestone first.
- `main`, the `v2-phase02-release` tag and both deployed Northbay demos stay unchanged. All Phase 03 work stays on
  `v2/phase03` or its milestone branches.

## 11. 03A interface (frozen by the lead before code, 2026-11-25)

03A is built in two steps on milestone branches off `v2/phase03`, merged in order: **03A-1 ownership and authority**,
then **03A-2 location and transfers**. Names below are binding. Bodies are the engineers'.

### 11.1 Hub ownership (03A-1)

- **New `hub` FK, NOT NULL after backfill, on these tables:**
  - `fleet_robot.hub_id` (home hub)
  - `planning_vehicle.hub_id`
  - `planning_person.hub_id` (home hub)
  - `planning_visit.hub_id` (dispatch hub)
  - `hub_workorder.hub_id`
  - `hub_chargingsession.hub_id`
  - `hub_chargerreservation.hub_id`

  `hub_charger.hub_id` and `hub_stockitem.hub_id` become NOT NULL. Stock item codes become unique per hub.
- **Backfill:** every existing row goes to the existing hub. If a database has rows but no hub, the migration creates
  one hub, `HUB-1` "Main hub".
- **Default:** a DB default `platform_core_default_hub()` (SECURITY DEFINER, fixed search_path) applies when a row is
  inserted without a hub:
  - no hub exists: it creates `HUB-1`;
  - exactly one hub exists: it returns that hub;
  - two or more hubs exist: it **raises** "hub required".

  So single-hub databases and the existing tests keep working, and a multi-hub database never guesses.
- **Removed shortcuts:** "first hub" lookups (`hub/charging.py`, `hub/0008`) are removed in favour of the record's own
  hub.
- **Integrity:** a charger reservation, charging session or work order must use a charger or robot of the same hub.
  A visit's robot and vehicle must be dispatchable from the visit's hub. Enforced by triggers.

### 11.2 Hub-qualified grants (03A-1, correction B)

- **Grant column:** `accounts_rolegrant.hub_id` is NULL for a regional grant and set for a hub grant.
  - Hub-scoped roles (`hub_technician`, `field_operator`) **must** have a hub (check constraint).
  - The other roles stay regional (NULL).
  - The active-grant uniqueness becomes (user, role, coalesce(hub, 0)).
- **Backfill:** existing hub-scoped grants get the existing hub.
- **Audit:** grant and revoke stay audited commands. A person's home hub never confers a grant.
- **SQL:** `has_active_role_at(uid bigint, role text, hub bigint) RETURNS boolean` is true for an active grant of that
  role whose hub is `hub`, or is NULL (regional). `has_active_role(uid, role)` keeps its meaning: any hub.
- **Python:**
  - `accounts.permissions.has_permission(user, permission, hub=None)`. With a hub, it counts only grants that apply
    at that hub.
  - `run_command(..., hub=<Hub or id>)`. Each command whose target belongs to a hub resolves the target's hub from the
    **record** (never from client input) and passes it.
- **Guarded writes:** every SECURITY DEFINER write function on a hub-owned record (stock, charging, work orders, and
  the 03A-2 transfer functions) checks `has_active_role_at(actor, role, <hub of the locked target row>)` after taking
  the row lock.
- **Reads:** pages show only the hubs the user has a grant for (regional = all), with a hub filter on Today, Hub,
  Fleet and Dispatch.

### 11.3 Per-hub settings (03A-1)

- `fleet_deploymentsetting` gains a nullable `hub_id`. A hub row overrides the global row for that hub.
- Only `stock_minimums` and `stock_per_visit` are per hub in 03A.
- `provision_settings --hub CODE` writes a hub row. Safety settings, including `maintenance_gate_active`, stay global.

### 11.4 Location (03A-2, correction A)

- **Custody:** hub-end custody events record `hub_id`.
- **Location function:** `fleet_robot_location(robot bigint) RETURNS TABLE(kind text, hub_id bigint, ref text,
  verified boolean)`.
  - Kinds: `hub`, `vehicle`, `site`, `transfer_in_transit`, `unaccounted`, `unknown`.
  - A robot with no custody history is at `hub(home hub)`, verified: today's rule, now with a hub.
- **Dispatch eligibility:** a robot is dispatchable for a visit only at `hub(H)`, verified, where H = the visit's hub.
  This is enforced in the planning eligibility view and the departure custody guard.

### 11.5 Transfers (03A-2, correction C)

- **Table:** `fleet_hub_transfer` (append-only): asset kind (robot, vehicle), asset, from hub, to hub, state,
  actor, reason, times.
- **States:**
  - `in_transit`, then `received`, at the destination or back at the source after an abandonment;
  - `receipt_refused` (stays in transit, with a reason);
  - `abandoned` (by the ops lead).
- **Functions** (SECURITY DEFINER; EXECUTE granted to `ops_app` only):
  - `fleet_transfer_start(actor, kind, asset, to_hub, reason, request_id)`: ops lead or a grant at the source.
  - `fleet_transfer_receive(actor, transfer, request_id)`: a grant at the destination, or at the source after
    abandonment.
  - `fleet_transfer_refuse(actor, transfer, reason, request_id)`
  - `fleet_transfer_abandon(actor, transfer, reason, request_id)`: ops lead.
- **Robot start requires:** no open run, verified at `hub(source)`, no unused authorization and no future booking. A
  blocking booking is listed in the refusal.
- **On start:** the robot version is bumped, holds are untouched, and the location becomes `transfer_in_transit`.
- **On receipt:** the robot's home hub changes to the destination and custody gets a verified hub event at the
  destination. A receipt never clears a hold.
- **Locks:** robot row lock, then the settings lock, the same order as planning and authorization.
- **Vehicles:** the same functions. A vehicle needs no open trip and no future trip.
- **Stock:** `hub_transfer_stock(actor, item, quantity, to_hub, request_id)` and `hub_receive_transfer(...)` write
  `transfer_out` and `transfer_in` ledger movements. In transit, the quantity counts at neither hub.

### 11.6 03A exit tests (contract sections 5 and 10.I)

- Backfill on a copy of a Phase 02 database: no null hub, counts unchanged.
- Default hub function in all three cases: none, one, many.
- Cross-hub write refused through a page, a service and direct SQL as `ops_app`, with forged, NULL and empty actors.
- A technician who changes their home hub gains nothing.
- A grant revoked during a write, in both orders, with the waiter seen in `pg_locks`.
- Location states. A robot in transit, at a site or unaccounted cannot be proposed at either hub.
- Transfers:
  - a transfer racing a booking and an authorization, both orders;
  - two simultaneous receipts: exactly one wins;
  - holds kept at every stage;
  - an old authorization fails after a transfer;
  - stock reconciliation through transfer out and in.
- Every Phase 02 and earlier suite stays green.

## 12. EM ruling 2026-11-25: future planning is separate from physical dispatch (amends 10.A)

10.A mixed up two separate questions: where a robot can physically be dispatched from, and whether it can be booked
for a future visit. **Option B is approved with these binding conditions.**

| Current robot location | Future planning | Authorization and departure |
|---|---|---|
| Verified at the visit's hub | Allowed (existing checks) | Allowed (existing checks) |
| At a site or in a vehicle on an open run of the **same** hub | Allowed, shown **At risk: robot away until verified back at hub** | **Blocked** |
| At another hub | Blocked | Blocked |
| In an inter-hub transfer | Blocked | Blocked |
| Unaccounted or unknown | Blocked | Blocked |
| At a hub but unverified | Blocked | Blocked |

Conditions:
- The open run belongs to the same hub as the proposed visit.
- The new booking does not overlap the run's reserved window, including return travel and turnaround.
- An overdue or indeterminate return (the run's reserved window has ended and the robot is still not back) is never
  treated as a reliable availability estimate. Planning is refused.
- Every hold, maintenance, qualification, reservation and eligibility check still applies.
- The planner and Suggest show "At risk: robot away until verified back at hub".
- Authorization and departure keep the strict verified-hub rule, with no exceptions.
- The physical-dispatch predicate is not weakened. A separate **future-planning location predicate** is added, and it
  is enforced the same way in Python and in PostgreSQL.

Deviations approved with conditions:
- **Received transfer row as the location source.** The received transfer row may establish the verified hub
  location, provided the resolver orders it against later custody events. There must be no competing source of
  location, and an older receipt never overrides newer custody evidence.
- **Who may refuse a receipt.** A receipt may be refused at the source or the destination; a third hub is refused. A
  refusal never establishes custody, completes the transfer or makes the asset available. Only an explicit, audited
  receipt resolves it.
- **Lock order.** It is approved only once the lock graph is documented (exclusive settings writers, grant revocation,
  planning, transfers, authorization) and both concurrency orders are demonstrated.

Security fixes found in 03A (direct role-grant writes, hub change by plain UPDATE, grant locking needing UPDATE
privilege) are approved in scope. For closure:
- Every new SECURITY DEFINER function has a fixed search_path, restricted EXECUTE and an actor check, and there is no
  direct-write bypass.
- The permission and concurrency tests run as `ops_app`.

Added 03A exit tests:
- A robot out today with a non-overlapping visit tomorrow: planning is allowed, At risk.
- An overlap with the existing run: refused.
- An overdue or unknown return: refused, no optimistic availability.
- Still away when authorization is requested: refused.
- Verified back at the right hub: normal re-evaluation.
- In transfer or at another hub: planning refused.
- A direct-SQL bypass of the planning location rule: refused.
- An older transfer receipt followed by a newer custody event: the newer wins.
- A refused receipt: the asset stays unavailable.

Perf: the fixture stays representative of real operations. Robots are not moved to the hub just to pass. The perf
run reports the eligible candidate count and benchmarks the revised predicate.

03A closure package: final SHA and diff, migration and backfill evidence, exact runtime privileges, transfer and
concurrency tests, full CI, perf, and a **browser walkthrough showing hub-scoped permissions and transfers**.

## 13. 03B amendments (draft for EM review, 2026-11-25)

### 13.1 Design acceptance bar (EM-approved product owner direction, binding for 03B)

The product owner's direction was "make it as real and nice as possible with a good taste". The EM approved it on
2026-11-25 as an enforceable acceptance requirement, not a finishing pass. The screen specs are in
`docs/14_v2_03b_design_spec.md`.

| Area | Acceptance requirement |
|---|---|
| Screen specification | Primary user, decision, actions, and the empty, loading, blocked and error states are defined before implementation |
| Visual hierarchy | The key action and operational risks are visible immediately; the dispatch board is designed around decisions, not inventory |
| Reassignment | Explains every re-check and its outcome; never implies departure approval |
| Language | Hub names first, local times with the zone, refusals that say what to do without revealing another hub's details |
| Responsive design | Real browser review at 1024, 768 and 375 px |
| Accessibility | Status never by colour alone; keyboard access; readable labels |
| Demo realism | Three distinct fictional hubs, plausible crew and visit schedules |
| Evidence | Screenshots, defect log, fixes, final design review |

### 13.2 03B interface (to be frozen before code)

- **Zones:** `platform_core_zone` (hub, code, name, active), plus `planning_visit.zone_id`. The zone is taken from the
  site's zone when known. Unknown is allowed and shown.
- **Travel times:** `planning_travel_time` holds the from zone, to zone, minutes, version, source (company or demo),
  effective_from and created_by.
  - Append-only; the latest effective version per pair is the one in force.
  - A missing pair means **Travel time unknown** (10.H).
  - A visit planned before 03B keeps its recorded travel minutes.
- **Regional pause:** `operations_region_pause` is append-only. It records pause and lift rows: hub, or hub and zone;
  reason; actor; time.
  - Written only through SECURITY DEFINER `operations_pause_region(actor, hub, zone, reason, request_id)` and
    `operations_lift_pause(actor, pause, reason, request_id)`, for an ops lead or a grant at that hub.
- **Pause gate (I-9, 10.G):** `readiness_issue_authorization` and the departure guard refuse while an active pause
  covers the visit's hub or zone. The hub and zone are taken from the visit, never from client input.
  - Pause and lift take the hub row lock, then the settings lock in shared mode. Authorization and departure take the
    same locks in the same order. The lock graph doc is extended accordingly.
- **Planning under a pause:** planning stays allowed during a pause and shows "Paused: cannot leave until lifted".
  A cross-hub reassignment cannot route around a paused hub, because the gate follows the visit's hub at
  authorization time.
- **Suggest and next free time:** a read-only Python service on top of the existing feasibility checks and the
  future-planning predicate (section 12).
  - It returns up to three slots within a look-ahead set by the owner (`dispatch_lookahead_days`, demo value 7).
  - A slot whose travel time is unknown is never offered as free.
- **Dispatch board:** read-only, built from one snapshot like Today, with a statement-count test that stays constant
  in hubs and visits. p95 at most 2 s on a 3-hub fixture of 200 robots (section 8).

### 13.3 03B exit tests (in addition to section 6, items 10 and 12)

- Pause vs authorization and pause vs departure, both orders, waiter seen.
- A pause blocks authorization through a direct-SQL call as `ops_app`.
- A cross-hub reassignment of a paused visit is still blocked at authorization.
- Lift restores the gate.
- Suggest and next-free-time oracle tests, including a missing travel pair.
- Travel version auditability: a suggestion names its matrix version.
- Design bar evidence per 13.1.

### 13.4 EM review of 03B 2026-11-25: binding corrections before the interface is frozen

The six-screen design and the 13.1 design bar are approved. Section 13.2 is amended as follows; where the two
differ, 13.4 wins.

1. **Pause authority.**
   - A regional ops lead may pause or lift a hub or zone.
   - A hub-scoped role may *pause* only with an explicitly defined `safety_pause` permission, granted separately. A
     general hub grant (hub technician, field operator) is not enough.
   - **Lifting** needs regional ops lead authority, or a separately defined and approved safety authority.
   - Required negative direct-SQL tests: a hub technician cannot pause or lift. A hub-scoped user with the pause
     permission still cannot lift.
2. **Pause synchronization and scope.**
   - **Lock order:** one documented order is used by authorization, departure, pause, lift and any change to a
     visit's hub or zone: **the settings lock first, then the hub row.** It is reconciled with the 03A lock graph,
     and both race orders are proved with the waiting transaction observed.
   - **Zone:** a zone pause applies to the visit's authoritative destination zone.
   - **Visit changes:** changing a visit's hub or zone re-checks every applicable pause before authorization or
     departure.
   - **Hub over zone:** a hub pause overrides zone-level state.
   - **Unknown zone:** an unknown zone never silently gets past a zone pause. A visit with an unknown zone is treated
     as paused while any zone pause is active at its hub.
   - **Protective actions:** robots that have already departed can still submit protective actions.
   - **Invariant:** a committed pause is never bypassed by a later authorization or departure transaction.
3. **Travel matrix.**
   - **Direction:** pairs are ordered (from zone to zone); A→B and B→A are separate unless the owner sets both.
   - **Versions:** each ordered pair has one unique, deterministic effective version.
   - **No retroactive changes:** a new value has effective_from of now or later, and past dispatch decisions are
     never silently rewritten.
   - **Retired zones:** a retired or inactive zone keeps its history. New visits cannot use it, and its pairs are not
     offered.
   - **Saved plans:** each saved planning decision stores the matrix version it used. A changed plan is checked fresh.
   - **No fallback:** a missing pair has no synthetic value. For a new dispatch that needs that route, the option is
     **blocked / unavailable** (not at risk). The board may still show "Travel time unknown" as an attention item.
4. **Next free time search.**
   - **Steps:** candidate starts in 30-minute steps inside the hub's operating hours (an owner setting; demo value
     08:00-20:00 local), in the hub's IANA time zone, with daylight-saving handled by zone conversion.
   - **A valid slot has:** a robot under section 12 planning eligibility, a qualified lead and second, a vehicle, a
     known travel time, and no overlap.
   - **Ranking:** fully feasible slots come first, then clearly labelled "At risk" slots (robot away on a same-hub
     run). Within each group, earliest first, then a deterministic tie-break (robot code, crew codes, vehicle code).
   - **Budget:** at most `dispatch_lookahead_days` (demo 7) days, and a candidate budget of 200 evaluations per
     request. When the budget is reached, the result says so.
   - Neither group is ever shown as a departure approval.
5. **Measurable design review.**
   - **Ordering:** Needs attention is ordered by safety/operational severity first (blocked, then paused, then at
     risk), then by how close the departure is, then by a deterministic tie-break. Not "newest first".
   - **Status words:** "Available for planning", "At risk", "Blocked", "Paused". Never words that imply physical
     availability or clearance.
   - **375 px:** every essential dispatch action stays reachable through the hub detail or the review flow. No action
     is desktop-only.
   - **Time zones:** time zones come from real IANA conversion. No zone abbreviation is hard-coded.
- 13.3 exit tests are extended with tests for each of the five corrections.

### 13.5 03B interface (frozen by the lead before code)

This section implements 13.2 as corrected by 13.4. Names are binding; bodies belong to the engineers. 03B is built in
two waves on milestone branches off `v2/phase03`.

**Wave 1 (in parallel)**
- **03B-1 data and gates:** zones, travel matrix, regional pause.
- **03B-2 dispatch board and reassign:** S1, S2, S6. Built against the read accessors below. Each accessor returns
  "unknown/none" until 03B-1 lands.

**Wave 2 (after wave 1 merges)**
- **03B-3:** next free time (S3), the pause and zones/travel screens (S4, S5) and the three-hub demo.
- Then the lead's design review at 1024, 768 and 375 px.

**03B-1 database**
- `platform_core_zone(id, hub_id, code, name, active, created_at)`, unique (hub, code). Retired = `active=false`; the
  history is kept.
- `planning_site.zone_id` (nullable) and `planning_visit.zone_id` (nullable). A visit's zone is its site's zone at
  plan time.
- `planning_travel_time(id, from_zone_id, to_zone_id, minutes, version, source, effective_from, created_by_id,
  created_at)`:
  - append-only;
  - unique (from_zone, to_zone, version);
  - `effective_from >= now()` is enforced on insert;
  - written only by SECURITY DEFINER `planning_set_travel_time(actor, from_zone, to_zone, minutes, source,
    effective_from, request_id)` (admin or ops lead).
- `planning_visit_revision.travel_version_id` (nullable): the matrix row a saved plan used.
- `operations_region_pause(id, hub_id, zone_id null, kind 'pause'|'lift', pause_id null, reason, actor_id,
  created_at)`:
  - append-only;
  - written only by `operations_pause_region(actor, hub, zone, reason, request_id)`, needing `operations.pause` (the
    regional ops lead, or a `safety_pause` grant at that hub);
  - and by `operations_lift_pause(actor, pause, reason, request_id)`, needing `operations.lift_pause` (the regional ops
    lead only).
- `operations_active_pause(hub bigint, zone bigint) RETURNS TABLE(pause_id, scope, reason, since)`:
  - a hub pause overrides;
  - an unknown (NULL) zone is covered by any active zone pause at its hub.
- **Gates:** `readiness_issue_authorization` and the departure guard refuse while `operations_active_pause(visit.hub,
  visit.zone)` returns a row.
- **Lock order:** every one of authorization, departure, pause, lift, and a change to a visit's hub or zone takes the
  settings lock first, then the hub row (`platform_core_hub` FOR SHARE for gates, FOR UPDATE for pause and lift). The
  order is documented in `docs/evidence/03a_lock_graph.md`.

**Read accessors (Python; 03B-2 uses these, 03B-1 implements them)**
- `planning.zones.zone_of(visit) -> Zone | None`
- `planning.travel.minutes(from_zone, to_zone, at) -> TravelValue(minutes | None, version_id | None, source)`. A missing
  pair gives minutes None, read as "Travel time unknown".
- `operations.pauses.active_for(hub_id, zone_id) -> PauseInfo | None`
- `operations.pauses.board_summary(hub_ids) -> dict[hub_id, list[PauseInfo]]`. One statement.

**03B-2 board**
- `/planning/board/`: the new Dispatch default, with "Week list" kept.
- Built from one read snapshot (like Today), with a statement-count test that stays constant in hubs and visits.
- **Status words:** "Available for planning", "At risk", "Blocked", "Paused", each with its shape icon.
- **Attention order:** blocked, then paused, then at risk; then departure proximity; then visit code.
- **Reassign panel:** "What we checked" lines come from the same checks the guarded plan command runs. They are
  advisory, and the command checks again on save.

**03B-3**
- `planning.next_free(visit_or_request, *, limit=3) -> NextFreeResult(slots, exhausted: bool)`. The search follows
  13.4 item 4.
- Owner settings: `dispatch_lookahead_days` (demo 7) and `hub_operating_hours` per hub (demo 08:00-20:00).

**EM conditions at the freeze (2026-11-25, 03A closure ruling)**
- 03B-2 may build the UI against these accessors, but every real reassignment calls the final guarded planning path.
  No working reassignment may bypass the pause or travel rules while 03B-1 is still unmerged.
- Wave 2 starts only after the wave 1 integration checks pass. The final 03B acceptance and design review come before
  03C.
- Global counts (production blockers) are labelled "all hubs" wherever they are shown. The dispatch board does not
  depend on Issues paging until Issues paging is fixed.
- Every new 03B guarded write meets the database boundary: SECURITY DEFINER, actor and hub authority, `ops_app` with no
  direct DML, and catalog-audit classification.

## 14. 03C interface: server field-command protocol (frozen by the lead before code, 2026-11-26)

03B closed at `5449acd` (EM 2026-11-26). 03C is authorized for development, not release. This section implements
sections 3 (I-2 to I-6), 4 (items 5 to 7), 6 and 10.D, 10.E and 10.F. Names and meanings are binding. 03C is
server-only: no service worker or offline UI (that is 03D).

### 14.1 Endpoint and schema

`POST /api/field/sync`. JSON. Authentication is the user's session, plus the CSRF header. An ended or revoked
session gets HTTP 401 and nothing is read or stored.

**Request:**
```json
{
  "device_id": "uuid (made once per browser install, stored on the phone)",
  "run": "EX-0007",
  "unsent_count": 3,
  "commands": [
    {
      "key": "uuid (made on the phone, one per command, never reused)",
      "channel": "ordinary | protective",
      "seq": 4,
      "last_confirmed_seq": 3,
      "command": "trip.arrived_site",
      "payload": {},
      "device_observed_at": "2026-11-27T09:41:05-08:00"
    }
  ]
}
```

- **Batch limits:** at most 20 commands per request, with each payload at most 4 KB. A larger request gets HTTP 413,
  and nothing is applied.
- **`seq`:** required on the ordinary channel. The protective channel must send `null`.
- **`unsent_count`:** stored as the device's *last reported* count, with its time. Pages show "last reported unsent
  count", never a live number.

**Response (HTTP 200 unless stated):**
```json
{
  "server_time": "2026-11-27T09:41:07-08:00",
  "run": "EX-0007",
  "last_confirmed_seq": 4,
  "next_expected_seq": 5,
  "results": [
    {"key": "uuid", "outcome": "confirmed | rejected | held | retry", "reason_code": "",
     "message": "plain words", "seq": 4, "recorded_at": "...", "result": {}}
  ]
}
```

There is one result per command, in request order. Outcomes:

| Outcome | Meaning | Final? | What the phone does |
|---|---|---|---|
| `confirmed` | Applied and committed. Also returned for a replay of a confirmed key with the same payload. | Final | Mark the step done |
| `rejected` | Not applied, and never will be. The reason is stored durably; a replay returns the same answer. | Final | Show the reason; do not resend |
| `held` | Valid, but waiting for an earlier ordinary `seq` of this run. Nothing is stored. | No | Resend later with the same key |
| `retry` | A temporary server problem (lock timeout, deadlock, maintenance). Nothing is committed. | No | Resend later with the same key |

### 14.2 Command classes (I-2)

| Class | Commands | Via sync |
|---|---|---|
| **A: online only** | depart (`record_departure`); session start and resume; hand the robot over to the site; recover a robot; anything issuing or using an authorization | Always `rejected` `ONLINE_ONLY`: "Needs a connection. Do not start." |
| **B: protective** (channel `protective`) | session pause; session stop; report an incident or problem; open an interruption | Applied **regardless of sequence gaps** (10.D). Each keeps `last_confirmed_seq` for causality. |
| **C: records** (channel `ordinary`) | trip events (arrived at site, left site, arrived at hub); back to vehicle; custody verification; site check result; observation note | Applied in `seq` order, each through the existing command and trigger at apply time (I-3) |

The server classifies by command name. A class B or C command sent on the wrong channel is `rejected`
`WRONG_CHANNEL`. An unknown command name is `rejected` `UNKNOWN_COMMAND`.

**Protective stop that cannot be applied as recorded** (10.D), for example when the session is not active in the
server's state:
- the server records an interruption, and places a hold where the incident rules call for one;
- it answers `confirmed` with `result.effect = "protective_state"` and a message saying what was recorded instead;
- it never claims the session stopped.

### 14.3 At-most-once, sequence and replay (10.E)

Each key is applied at most once, then committed. The rules:

1. **Same key, same command and payload hash:** returns the stored outcome (`confirmed` or `rejected`). The domain
   command does not run again.
2. **Same key, different command or payload:** `rejected` `KEY_REUSED`. Nothing runs, and the original row is
   unchanged.
3. **Same run and `seq`, different key:** `rejected` `SEQ_TAKEN`. Nothing runs. This covers two phones of one crew:
   one step is confirmed, the other is refused with "Already recorded by {name} at {local time}".
4. **Two simultaneous requests claiming the same `seq` or key:** the run row lock serializes them, so there is one
   transactional outcome. The other request gets rule 1, 2 or 3.
5. **Gaps:** an ordinary command whose `seq` is higher than `next_expected_seq` is `held`, and nothing is stored.
   When the missing `seq` arrives and is decided, either confirmed or rejected, the next one can apply.
6. **Abandonment:** `POST /api/field/abandon` `{run, seq, reason}`, by a crew member of the run or the ops lead.
   - It writes a final `abandoned` row for that `seq`. An old delayed batch can never overwrite it: it is
     `rejected` `ABANDONED`.
   - Later held ordinary commands of that run are then `rejected` `AFTER_ABANDONED`, in order, because they assumed
     the missing step.
   - The crew re-records from the server's current state.
7. **Atomicity:** for each command, one database transaction does all of these:
   - claims the key and the `seq`;
   - runs the domain command through `run_command` (the existing permission checks and triggers);
   - writes the outcome row.

   A domain refusal rolls the domain work back to a savepoint, and the `rejected` row is still committed. A crash
   commits neither the effect without its receipt, nor the receipt without its effect.
8. **Rejected steps still advance the sequence:** a `rejected` ordinary command takes its `seq`, so the run moves on.
   The phone shows the reason and does not resend.

### 14.4 Time, authority and scope (I-5, 10.F)

- `recorded_at` is database time, and is used by every gate. `device_observed_at` is stored and shown as "phone time".
  It is never used for a gate, counter or expiry.
- **Bounds:** the phone time must be no earlier than the run's departure minus 5 minutes, and no later than the
  receipt time plus `field_clock_skew_min` (an owner setting; demo value 2).
  - On the **ordinary** channel, a time outside the bounds is `rejected` `DEVICE_TIME_OUT_OF_BOUNDS`.
  - On the **protective** channel, a protective action is **never refused for a clock**. The phone time is stored as
    unknown and flagged "phone clock out of range".
- **Authority:** checked at apply time against the actor's *current* grants at the run's hub (03A-2 triggers). A
  revoked grant means `rejected` `NOT_PERMITTED`.
- **Wrong run:** a run that is not the actor's crew run, for a non-regional actor, is `rejected` `NOT_YOUR_RUN`, and
  nothing else is read.
- **Device binding:** a queue belongs to (user, run, device). A key first claimed by another user or device is
  `rejected` `KEY_REUSED`.
- **Nothing from the client is trusted:** no cached readiness, old authorization or optimistic phone state is read.
  The server state at apply time decides.

### 14.5 Storage and privileges

- **`execution_field_command`:** append-only. Columns:
  - `key` (uuid, unique), `actor_id`, `device_id`, `run_id`;
  - `channel`, `seq` (null for protective), `command`, `payload_hash`;
  - `device_observed_at`, `device_time_flag`;
  - `received_at`, `decided_at`;
  - `outcome` (confirmed, rejected or abandoned), `reason_code`, `message`, `result` (jsonb).

  Unique (`run_id`, `seq`) where `seq` is not null.
- **`execution_field_device_report`:** (`device_id`, `run_id`, `actor_id`, `unsent_count`, `reported_at`).
  Append-only; the newest row wins.
- **Writes:** only through SECURITY DEFINER `execution_field_record(...)` and `execution_field_abandon(...)`, owned by
  `ops_owner` with a fixed search_path; EXECUTE for `ops_app` only. `ops_app` has SELECT only on both tables, and
  they are classified in the catalog audit.
- **Locks:** the run row FOR UPDATE first, then whatever the domain command takes (03A and 03B lock graph,
  unchanged). The lock graph doc gets a 03C section.

### 14.6 03C exit tests

All of section 6, items 1 to 9, against the endpoint without any front end, plus:
- **Direct SQL as `ops_app`:** inserting or updating field-command rows, or forging a `confirmed` outcome, is
  refused.
- **Competing devices:** two devices racing for the same `seq`, in both orders, with the waiter seen in `pg_locks`.
- **Protective channel with a gap:** a protective incident with `seq` 2 missing places the hold at once.
- **Protective stop on a non-active session:** gives the protective state, never "stopped".
- **Class A:** every class A command is refused with `ONLINE_ONLY`.
- **Abandonment:** cannot be overwritten by a late batch.
- **Revocation:** a revoked grant or an ended session mid-queue is refused.
- **Performance:** a batch of 20 commands at p95 of 1 s or less.

### 14.7 Carried from the 03B closure (EM), to be done alongside 03C

- **P1:** label next-free results: "Suggested planning time. Final checks, including charging, site requirements and
  regional pauses, run when you save. This is not departure authorization." Show the outstanding checks before
  saving.
- **P2:**
  - limit plan-form resources to the visit's hub;
  - compact the mobile header without hiding safety messages;
  - in Issues, apply the hub scope before the limit;
  - repeat readiness performance runs under controlled conditions.
- **P3:**
  - more varied next-free slots;
  - terminology.

### 14.8 EM review of 03C 2026-11-26: binding amendments (they win over 14.1 to 14.7)

Both choices are approved:
- **A rejected ordinary command consumes its `seq`.** The rejection is a durable, final *sequence decision*. It is not
  a successful execution.
- **A protective action is never refused for clock bounds.** This applies only to clock validation. A protective
  command still needs a valid session, authorized run access and a supported operation.

1. **Decided, not confirmed.**
   - `last_confirmed_seq` is replaced by **`last_decided_seq`**, in the request and the response. The response also
     carries `next_expected_seq`.
   - A rejected `seq` is decided, but it satisfies no operational prerequisite. Every later command re-checks the
     actual server state.
   - A protective command records the last ordinary `seq` the device observed, and that step's outcome as the device
     saw it. It never treats a rejected step as confirmed.
2. **Replay identity.**
   - The immutable identity of a key is the canonical serialization (sorted-key JSON, UTF-8, SHA-256) of: `actor_id`
     (from the session, never the request), `device_id`, `run_id`, `channel`, `seq`, `command`, `payload` and
     `device_observed_at`.
   - Once a key has a durable outcome, a replay with an identical identity returns that outcome **before** any domain
     execution.
   - Any changed field is `KEY_REUSED`. A replay is never reinterpreted with a new device time or `seq`.
3. **Protective idempotency and ordering.**
   - A duplicate protective command never creates a second incident, interruption or hold. It is caught by the key,
     and a second key for the same report is caught by the domain's existing idempotency or state check.
   - Protective reports are ordered by server receipt and commit. The device's causal reference is kept.
   - A protective command is allowed despite ordinary gaps, but is still re-checked against the current server state.
     A stop that cannot stop the session gives the documented protective state.
4. **Abandonment is a durable sequence barrier.**
   - A run's ordinary stream has a **generation** (it starts at 1).
   - Abandoning `seq` N writes a final barrier row for (run, generation, N), and ends that generation for every
     device.
   - Any later command of that generation (held, or arriving later, at or after N) is `rejected` `AFTER_ABANDONED`.
     Nothing is reused silently.
   - The crew reconciles from the current server state, and the server opens **generation g+1** with `seq` starting
     at 1. The request carries `generation`, and a stale generation is `rejected` `STALE_GENERATION`.
   - Uniqueness is (run, generation, seq).
5. **Transactions and errors.**
   - A **durable `rejected`** is written only for a *classified domain refusal*: a permission, state, conflict, class,
     bounds, identity or sequence rule.
   - A database timeout, deadlock, serialization failure or lost connection **never** becomes a durable rejection.
     It is `retry`, or an HTTP 5xx when the server cannot say what happened.
   - `held` and `retry` claim nothing: no key and no `seq`.
   - Required tests:
     - a crash just before commit (nothing claimed, so a resend applies once);
     - a lost HTTP response after a successful commit, then a replay (the same `confirmed`);
     - an exception after the savepoint rollback but before the rejection is written (no partial state; the whole
       command is `retry`);
     - a competing request during an uncommitted key claim (it waits, then gets the replay or `KEY_REUSED`);
     - a committed rejection followed by a dependent command (that command re-checks the real state).
6. **Authentication and receipt.**
   - An invalid, ended or revoked session is refused with HTTP 401 **before** anything else: no device report is
     written, no run is looked up, and nothing reveals whether a run exists.
   - An authenticated user without run access gets `NOT_YOUR_RUN`, with no run details and no command-history write.
   - The times are:
     - `received_at`: database receipt;
     - `decided_at`: database decision;
     - `device_observed_at`: untrusted, from the device;
     - `server_time`: when the server answered.

     All are stored in UTC and shown in the workspace zone.

**Build plan (EM-approved):** three engineers in parallel on separate components, with integration verified end to end
under the real `ops_app` role.

| Engineer | Builds |
|---|---|
| **03C-1** | Ledger schema, immutable identity, generations and barriers, database privileges, transaction semantics |
| **03C-2** | Command classification, domain adapters, protective fallback |
| **03C-3** | Authenticated HTTP endpoint, response contract, device reports, failure-injection tests |
