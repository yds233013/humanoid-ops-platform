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
