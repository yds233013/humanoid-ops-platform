# 03A lock graph: settings writers, grant revocation, planning, transfers, authorization

Contract: `docs/13_v2_phase03_contract.md` section 12 ("Lock order ... approved only once the lock graph is
documented and both concurrency orders are demonstrated"). Base graph: `docs/02_lock_graph.md`.

Notation: **U** = `FOR UPDATE` (row), **S** = `FOR SHARE` (row), **X** / **Sh** = the settings advisory lock
`64942988919` (`platform_core.locks.SETTINGS_LOCK_KEY`) exclusive / shared, transaction-scoped only.

## 1. What each participant takes, in order

| Participant | Locks, in the order taken | Defined in |
|---|---|---|
| **Settings writer** (`provision_settings`, any DML on `fleet_deploymentsetting`, schema owner only) | **X** (statement trigger), nothing else | fleet/0007 |
| **Grant revocation** `accounts_revoke_grant` | the grant row **U**; nothing else | accounts/0006 |
| **Grant check of a command** `run_command` -> `accounts_lock_grants` | the actor's qualifying grants **S**; then the command's own locks (rows below) | accounts/0006, platform_core/commands.py |
| **Grant check inside a guarded function** `hub_actor_has_at` | the actor's grants of the roles **S** (after the function's target row) | accounts/0005 |
| **Planning** `plan_visit` (Python) | grants **S** -> **Sh** -> site S -> pattern S -> robot U -> persons U -> vehicle U -> visit U; then its INSERT triggers | planning/services.py |
| Planning triggers (revision, reservation, visit) | robot **S** -> vehicle **S** (location gate, planning/0012); **Sh** (maintenance gate, planning/0009) | planning/0009, 0012 |
| **Authorization** `readiness_issue_authorization` | **Sh** -> snapshot guards (site, pattern, robot S, persons, vehicle) -> visit S -> policies S | readiness/0007 |
| **Departure** | **Sh** (asserted) -> site S -> pattern S -> robot U -> snapshot guards -> visit S -> trip U | execution/0007, readiness/0007 |
| **Transfer start / receive / refuse / abandon** `fleet_transfer_*` | asset row **U** (robot or vehicle) -> **Sh** -> actor's grants **S** (`hub_actor_has_at`); receive then UPDATEs the asset row it already holds | fleet/0015 |
| **Stock transfer / receipt** `hub_transfer_stock`, `hub_receive_transfer` | stock item **U** (source, resp. destination) -> grants **S** | hub/0013 |

## 2. Why there is no cycle

A deadlock needs a cycle in the waits-for graph. Each pair that shares a resource:

1. **Settings writer vs anything.** The writer takes only **X** and holds no row, so it never waits for a row a
   lock holder has. A holder of **Sh** waits for the writer at most, and the writer waits only for **Sh**
   holders to finish. No cycle. The transfer taking **Sh** *after* its robot row (contract 11.5 order) cannot
   close a cycle either: the writer holds no robot row.
2. **Shared holders vs each other.** **Sh** never conflicts with **Sh**, so planning or authorization
   (Sh -> robot) and a transfer (robot U -> Sh) never wait for each other on the advisory lock. They are ordered
   only on the robot row: one waits for the other's robot lock, and neither holds anything the other needs next.
3. **Grant revocation vs a guarded write.** The revocation takes only the grant row **U** and holds nothing else.
   A write takes the grant **S** last (`hub_actor_has_at`) or first (`run_command`), and never needs anything the
   revocation holds except that grant. No cycle.
4. **Transfer vs planning, authorization or departure.** All of them lock the robot row before any later guard
   row except the settings lock (case 1) and the grants (case 3). The transfer holds nothing after the robot
   row except **Sh** and grants **S**. No cycle.
5. **Planning triggers.** They take robot **S** after `plan_visit` already holds robot **U** (re-entrant in the
   same transaction), or, for direct SQL, as their first row lock. The vehicle is taken after the robot, as in the
   global order.

## 3. Both concurrency orders, demonstrated (waiter seen in `pg_locks`, runtime role `ops_app` unless noted)

| Pair | First | Second waits, then | Test |
|---|---|---|---|
| transfer vs settings writer | transfer (robot U, Sh held) | writer (owner, X) waits, then commits | `tests/test_v2_03a_transfers.py::test_a_transfer_and_an_exclusive_settings_writer_are_ordered_both_ways[transfer]` |
| | settings writer (X held) | transfer waits on Sh, then commits | `...[settings]` |
| transfer vs grant revocation | transfer (grant S held) | `accounts_revoke_grant` waits, then commits | `test_a_transfer_and_a_grant_revocation_are_ordered_both_ways[transfer]` |
| | revocation (grant U held) | transfer waits, then is refused (insufficient_privilege), nothing written | `...[revocation]` |
| transfer vs booking | either | the other waits on the robot row; booking refused "In transfer", or transfer refused listing the booking | `test_a_transfer_racing_a_booking_is_ordered_both_ways` |
| transfer vs authorization | either | the other waits on the robot row; refused accordingly | `test_a_transfer_racing_an_authorization_is_ordered_both_ways` |
| two receipts | either | the second waits on the asset row, then is refused "already received" | `test_two_simultaneous_receipts_exactly_one_wins`, `test_two_simultaneous_stock_receipts_exactly_one_wins` |
| guarded write vs grant revocation (03A-1) | either | ordered on the grant row | `tests/test_v2_03a_ownership.py::test_a_grant_revoked_during_a_guarded_write_is_ordered_both_ways` |
| settings activation vs planning triggers (02E) | either | ordered on the settings lock | `tests/test_v2_02e_carried.py` (activation races) |

In the transfer-vs-authorization "transfer first" order the transfer side is the transfer's own lock step
(`fleet_transfer_lock_asset`) and internal start step, held by the owner: the public `fleet_transfer_start`
refuses a booked robot by design, and an authorization needs a booking.

## 4. 03B-1: regional pause, travel matrix and visit scope changes (contract 13.4 item 2, 13.5)

Binding order (13.4 item 2): **the settings lock first, then the hub row** (`platform_core_hub`, by ascending id
when there are two). Gates take the hub **S**; pause and lift take it **U**. 13.2 said "hub row, then the settings
lock"; 13.4 wins and is what is built (operations/0011). **H** = a `platform_core_hub` row.

| Participant | Locks, in the order taken | Defined in |
|---|---|---|
| **Pause** `operations_pause_region` | actor pre-check (no lock) -> **Sh** -> hub **U** -> the actor's grants **S** (`hub_actor_has_at`) -> INSERT (FK key-share on hub, zone, user) + audit | operations/0011 |
| **Lift** `operations_lift_pause` | actor pre-check -> **Sh** -> the pause row read (append-only, no lock) -> its hub **U** -> grants **S** -> INSERT + audit | operations/0011 |
| **Authorization gate** (trigger `readiness_pause_gate` on `readiness_departureauthorization`, fired by `readiness_issue_authorization`) | the function's own order (**Sh** -> snapshot guards -> visit S -> policies S) -> **Sh** again (re-entrant) -> visit **S** (re-entrant) -> dispatch hub and destination-zone hub **S**, ascending -> check in a fresh statement | readiness/0007, operations/0011 |
| **Departure gate** (trigger `readiness_pause_gate` on `readiness_departureevent`) | `record_departure`'s order (grants S -> **Sh** -> site S -> pattern S -> robot U -> persons S -> vehicle S -> visit U -> trip U, then the C3 trigger re-locks them) -> **Sh** -> visit **S** -> hubs **S** -> check | execution/0007, operations/0011 |
| **Visit hub or zone change** (trigger `visit_pause_scope_lock`, BEFORE UPDATE OF hub_id, zone_id when either changes) | the updater's locks (plan_visit: grants S -> **Sh** -> site S -> pattern S -> robot U -> persons U -> vehicle U -> visit U) -> **Sh** -> old and new hub, old and new zone's hub **S**, ascending | operations/0011 |
| **Travel time** `planning_set_travel_time` | actor pre-check -> from zone **S** -> to zone **S** -> grants **S** -> pair advisory lock (transaction, `hashtextextended('planning_travel_time:<from>:<to>')`) -> INSERT + audit | planning/0013 |
| **Home zone change** `planning_set_site_zone` (03B-4) | actor pre-check (no lock) -> **Sh** -> the home (`planning_site`) **U** -> hubs of the old zone, the new zone and the open visits' dispatch hubs **S**, ascending -> grants **S** (`hub_actor_has_at`) -> pause check in a fresh statement -> UPDATE (bumps the home's version) + audit | planning/0014 |

### 4.1 Reconciliation with sections 1 and 2

- **Settings first.** Every new participant takes **Sh** before any hub row. Shared holders never conflict, and
  the exclusive settings writer holds no row (section 2 case 1), so the writer can only make a pause, lift or gate
  wait at its start, never in the middle while it holds a hub row the writer needs.
- **Pause and lift hold nothing a gate waits for except the hub row.** They take no robot, person, vehicle,
  visit, trip or policy row. A gate holds those rows and then waits for the hub (**S** vs **U**); the pause never
  waits for any of them. The only thing a pause takes after the hub is the actor's grants **S**, which a gate
  holds only **S** (no conflict); a grant revocation holds only the grant row (section 2 case 3). No cycle.
- **Hub row after guard rows in the gates.** In the gates the hub comes after the existing guard rows (it is the
  last lock before the check), which keeps every 03A order unchanged: nothing that holds a hub row ever waits for
  a guard row. The 03A hub **S** holders (`fleet_transfer_start` destination hub, `hub_create_stock_item`) hold
  it **S**, compatible with the gates; a pause waits for them and they never wait for a pause.
- **Foreign-key key-share.** An INSERT of a row referencing a hub (a visit, a custody event, a pause) takes a
  key-share lock on the hub row, which conflicts with the pause's **U**. Such a transaction only ever adds key-share
  or **S** on that hub afterwards, which PostgreSQL grants without queueing behind the waiting pause (it already
  holds a lock on the tuple), so a pause waits for it and never the reverse.
- **Visit scope change.** It holds the visit **U** and then takes hub **S**; the gates take visit **S** then hub
  **S**; a pause takes no visit. So a change of a visit's hub or zone and a pause are ordered on the hub row, and
  the gates read the visit's hub and zone after the visit lock: a changed visit is always checked against the
  pauses of its current hub and zone (13.4 item 2, "visit changes").
- **Home zone change (03B-4).** Settings first, then the home, then the hub rows. The home is the first guard row of
  the global order and the hubs come after every guard row, as in the gates, so nothing that holds a hub row ever
  waits for a guard row (taking the hubs before the home could deadlock with a gate that holds the home **S** and
  queues for a hub behind a waiting pause). It holds the home **U** and the hubs **S**: a pause or lift (hub **U**)
  waits for it or it waits for them, and re-reads the pauses after its hub locks; an authorization or departure
  (home **S**, then hubs **S**) waits for it on the home row, or it waits for them. The gates also refuse a visit
  whose saved zone differs from its home's current zone (`operations_pause_gate_problem`, planning/0014), and the
  home's new version invalidates every earlier authorization at departure.
- **Travel time.** It locks only zone rows (**S**, ordered against a zone retirement, which takes the zone **U**)
  and its pair lock; it holds no hub or guard row, so it is outside every cycle above.

**Invariant (13.4 item 2):** a committed pause is never bypassed by a later authorization or departure: the gate
waits for an uncommitted pause on the hub row and re-reads in a fresh statement; a pause started after the gate
took the hub row waits for the gate's transaction to finish.

### 4.2 Both race orders, waiter seen in `pg_locks` (ops_app unless noted)

| Pair | First | Second waits, then | Test (`tests/test_v2_03b_pause.py`) |
|---|---|---|---|
| pause vs authorization | pause (hub U held, uncommitted) | `authorize_departure` waits at its gate, then is refused "Paused: cannot leave until lifted", nothing issued | `test_a_pause_and_an_authorization_are_ordered_both_ways[pause]` |
| | authorization (paused before commit, hub S held) | the pause waits, then commits after; that authorization's departure is then refused | `...[authorization]` |
| pause vs departure | pause (hub U held) | `record_departure` waits at its gate, then is refused; nothing departs | `test_a_pause_and_a_departure_are_ordered_both_ways[pause]` |
| | departure (paused before commit) | the pause waits, then commits; the departure stands (a pause never erases one) | `...[departure]` |
| lift vs authorization | lift (hub U held, uncommitted) | `readiness_issue_authorization` (direct SQL) waits at its gate, then is issued | `test_a_lift_and_an_authorization_are_ordered_both_ways[lift]` |
| | the authorization gate step (`operations_pause_gate_problem`, held by the owner, refusing under the active pause) | the lift waits, then commits; a new authorization is then issued | `...[authorization]` |
| home zone change vs pause | pause of the home's zone (hub U held) | `planning_set_site_zone` (ops_app) waits on the hub row, then is refused (it would take the visit out from under the pause) | `tests/test_v2_03b_site_zone.py::test_a_zone_change_and_a_pause_are_ordered_both_ways[pause]` |
| | zone change (home U, hub S held) | a pause of the new zone waits, then commits; the re-planned visit is refused at authorization | `...[zone_change]` |
| home zone change vs authorization | zone change (home U held) | `authorize_departure` waits on the home row, then is refused (plan the visit again); nothing issued | `test_a_zone_change_and_an_authorization_are_ordered_both_ways[zone_change]` |
| | authorization (stopped before commit, home S held) | the zone change waits, then commits; that authorization is refused at departure (home version) | `...[authorization]` |
| home zone change vs departure | zone change (home U held) | `record_departure` waits on the home row, then is refused; nothing departs | `test_a_zone_change_and_a_departure_are_ordered_both_ways[zone_change]` |
| | departure (stopped before commit, home S held) | the zone change waits, then commits; the departure stands | `...[departure]` |

In the lift-vs-authorization "authorization first" order the authorization side is its gate step, held open by
the owner, as in the 03A transfer race: a real authorization under the active pause raises at that step, and
PostgreSQL aborts the transaction and releases its locks at the error (before the client's ROLLBACK; checked in
pg_locks), so the step itself is what holds the hub row while the lift waits.
