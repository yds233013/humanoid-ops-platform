# 03A closure note: hub ownership, authority, location and transfers

Branch `v2/phase03`. `main`, tag `v2-phase02-release` and both public demos are unchanged. All data is fictional.
Contract: `docs/13_v2_phase03_contract.md` sections 10 and 11 (frozen 03A interface at `6616adc`).

## Candidate

| Item | Value |
|---|---|
| Code SHA | `d589d93` (later commits docs only) |
| Built as | 03A-1 ownership and authority (merged `b35d380`), 03A-2 location and transfers (merged `881749a`), 03A-3 role-grant write boundary and read scope (merged `d589d93`) |
| Migrations (additive, each reverse tested) | platform_core/0005; accounts/0005-0006; fleet/0012-0016; planning/0010-0011; hub/0010-0013; execution/0006-0008; operations/0009 |
| Full CI | CI_LINE |
| Perf | PERF_LINE |

## Exit gates (contract 10.I and 11.6)

1. **Backfill.** A database built with the Phase 02 demo is migrated back below 03A, then forward again: no null hub
   on any record that needs one, row counts and robot/visit/person version sums unchanged (the backfill runs with
   triggers off, so no issued authorization stops matching), scoped grants at the hub, regional grants without one.
   A database with rows but no hub gets `HUB-1` "Main hub", audited.
2. **Default hub.**
   - With no hub, one is created.
   - With exactly one, it is used.
   - With two or more, the write is refused with "hub required", through the ORM, a command and direct SQL as `ops_app`.
3. **Authority at the database boundary (10.B).**
   - **Grants:** role grants are explicit, audited and revocable, per (user, role, hub). `ops_app` has SELECT only on
     `accounts_rolegrant`. Grants and revokes go only through `accounts_grant_role` and `accounts_revoke_grant`,
     which mirror the existing rules: admin only, no self-grant, hub-scoped roles held at a hub, one active grant per
     user, role and hub, revoke needs a reason. Bootstrap of the first admin works once.
   - **Hub-owned writes:** every guarded write on a hub-owned record (stock, charging, maintenance records,
     transfers, field-command triggers) checks the actor's grant at the hub of the locked target row.
   - **Refused callers:** direct SQL as `ops_app` with a forged, NULL, 0 or empty actor, or as the wrong hub's
     technician, writes nothing.
   - **Home hub:** a technician who changes their own home hub gains nothing.
   - **Revoke vs write race:** in both orders, the waiter is seen in `pg_locks`. If the write comes first it commits,
     then the revoke. If the revoke comes first, the write waits and is then refused.
4. **Location (10.A).** `fleet_robot_location` returns one of:
   - hub(H), verified or not;
   - vehicle;
   - site;
   - transfer_in_transit;
   - unaccounted;
   - unknown.

   Only hub(H), verified, where H is the visit's hub, is dispatchable or authorizable. A robot in transit, at a site,
   unaccounted or at the other hub is refused everywhere: the eligibility view, Python eligibility, options, Suggest,
   the planner, direct-SQL bookings, authorization and departure.
5. **Transfers (10.C).**
   - **State machine:** robot and vehicle transfers move through in_transit, received, receipt_refused and abandoned.
   - **Concurrency:** a transfer racing a booking or an authorization was tested in both orders, with the waiter
     seen. Two simultaneous receipts give exactly one winner.
   - **Holds and authorizations:** holds survive every stage and a receipt never clears one. An authorization issued
     before a transfer fails at departure (robot version bumped).
   - **Abandoned transfers:** an abandoned transfer can never be received at the destination.
   - **Stock:** stock transfers reconcile, and stock in transit is available at neither hub.
   - **Home hub changes:** a robot's or vehicle's home hub changes only through a received transfer (`fleet/0016`).
6. **Field commands at the run's hub.** An operator granted only at the other hub is refused on the run: custody
   verification, trip events, handoffs, site check, observations, departure. This holds through services and
   direct SQL. The regional ops lead is allowed.
7. **Read scope (I-8).**
   - List and detail pages show only the viewer's hubs; a regional user sees all. Another hub's robot,
     maintenance, inspection, visit, readiness, run, dossier or work-order page returns 403 with a plain sentence.
   - Today applies the hub scope before the readiness cap, so a scoped user's counts describe their own hubs.
   - `?hub=` narrows and never widens.
8. **Per-hub settings.** `stock_minimums` and `stock_per_visit` can be set per hub (`provision_settings --hub`).
   Safety settings stay global, and the database refuses a hub row for any other key.

## Found and fixed during 03A

- **`ops_app` could write role grants directly (pre-existing since v1).** A compromised runtime role could forge a
  grant at any hub. Closed by `accounts/0006`.
- **Latent production failure.** Locking grants FOR SHARE needs UPDATE privilege. As `ops_app`, the new permission
  lock would have failed in production. Tests connect as the owner, so no test saw it. Fixed with the
  `accounts_lock_grants` function. The test suite now includes explicit `ops_app` checks for it.
- **`ops_app` could change a robot's or vehicle's hub with a plain UPDATE.** Closed by `fleet/0016`: only a
  received transfer changes it.

## Existing tests changed (minimal; each listed in the engineer reports)

- **Exact-list tests:** `test_db_security` (SELECT-only tables), `test_v2_02b_followups` and `test_v2_02d_inventory`
  (function lists), `test_v2_02a_hub` and `test_v2_02b_charging` (permissions), `test_v2_02d_health` (migration
  dependency).
- **`test_execution_c3`, `test_execution_db`:** a robot that is out can no longer be planned for another visit, so
  the robot is brought back to the hub first. The open-run refusal is still asserted.
- **`test_v2_02e_carried`:** the reason for a robot still at the site is now "Not at a hub". The stock page
  statement budget went from 20 to 21: one constant statement for the transfer list, constant with 1 and 6 rows.
- **`test_commands`, readiness lock-order tests:** the audit row and the grant lock now come from database
  functions.
- **`test_v2_01c_setup_fleet`:** registering a robot before any hub creates HUB-1, so the checklist starts at
  "2 of 6".

## Decisions for the EM

1. **Strict planning location rule.** Correction 10.A is implemented as written, so a robot that is in a vehicle or
   at a site cannot be planned for a *later* visit until it is verified back at a hub. Before 03A it was refused only
   at authorization. This is safer, but planning tomorrow's visit for a robot that is out today is now impossible.
   - **Keep it**, or
   - **allow future planning while the robot is away**, keeping the hub(H)-verified rule at authorization and
     departure, and showing "away until back at hub" as At risk.
2. **Receipt is the transfer row, not a custody event.** The custody chain has no hub-to-hub move (custody must
   change place kind), so the received transfer row is the verified hub location that `fleet_robot_location`
   reads. This departs from 11.5's "custody gets a verified hub event". Accept it, or add a custody kind?
3. **Refuse at either end.** A receipt may be refused at the source or the destination; a third hub is refused.
4. **Lock order.** Transfers lock the asset row, then the settings lock (shared). Planning and authorization take the
   settings lock first. It cannot deadlock: shared holders never conflict, and the exclusive holder holds no rows.
   Accept, or align?

## Open items and limits

- Work orders keep a Python-only actor check, as designed in Phase 02. The same-hub trigger keeps robot and visit
  consistent.
- Not hub-scoped yet: incidents, people, the timeline, the robot status and Issues pages.
- Pause, stop, incidents, interruptions and hold placement accept a field role at any hub on purpose: they make
  things safer.
- Suggest lists only same-hub, at-hub robots, but hub-aware Suggest with zones and travel is 03B.
- Stock in transit can only be received; it cannot be refused or abandoned.
- A compromised `ops_app` can still pass a real admin's id as the actor. This is the same trust model as every
  guarded function.
- An `ops_app` transaction holding grant locks can delay a revoke. An idle-in-transaction timeout would bound it;
  it is not set.
- No browser walk in 03A: there is no new journey yet. Pages were tested through the Django test client. The
  three-hub demo and the journeys come in 03E.
- Usability with independent people: UNVERIFIED.
