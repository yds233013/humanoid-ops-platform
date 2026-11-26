# 03C closure note: server field-command protocol

Branch `v2/phase03`. `main`, `v2-phase02-release` and both public demos are unchanged. All data is fictional. 03C is
server-only: the offline phone screen is 03D. Contract: `docs/13_v2_phase03_contract.md` section 14, with 14.8 (the EM
amendments) binding.

## Candidate

| Item | Value |
|---|---|
| SHA | `71d4eab` |
| Diff | `5bab3a2...71d4eab` (frozen interface plus amendments → candidate) |
| Built as | 03C-1 ledger (`f1725a7`), 03C-2 command classes and adapters (`a98d98a`), 03C-3 endpoint and failure injection (03C-3's stand-in ledger replaced by the real one at the merge) |
| Migration | `execution/0009_field_ledger` (additive; reverse tested) |
| Full CI | `71d4eab`: run 45817127392 success, 2389 passed, 0 failed |
| Perf | 45817127900, 45817147880, 45817166082, all success |

Perf p95 (ms), three runs:

| Measurement | Run 1 | Run 2 | Run 3 | Budget |
|---|---|---|---|---|
| `/api/field/sync`, batch of 20 commands | 409 | 483 | 510 | 1,000 |
| Dispatch board | 189 | 214 | 219 | 2,000 |
| Readiness (scale) | 1,143 | 1,418 | 1,414 | 2,000 |

A sync batch of 20 commands runs 631 statements, constant.

## What is proven

- **Ledger** (`tests/test_v2_03c_ledger.py`, 36 tests, run as `ops_app`):
  - Each key is applied at most once. A replay of a confirmed or a rejected key returns the stored outcome, and the
    domain function is not called again.
  - **Identity:** the digest is canonical sorted-key JSON, and changing any one of the 9 identity fields gives
    `KEY_REUSED`, writing nothing.
  - **Sequence:**
    - two keys for one `seq` give `SEQ_TAKEN` ("Already recorded by … at …");
    - a gap is held and stores nothing;
    - abandonment is a barrier that opens a new generation, so a late or held command gets `AFTER_ABANDONED`, and
      `STALE_GENERATION` applies;
    - the protective channel ignores gaps but never duplicates.
  - **Transaction failures:**
    - a crash just before commit stores nothing, and a resend applies once;
    - an exception after the savepoint rollback stores nothing and claims no `seq`;
    - a lost response after commit is followed by a replay that returns the same answer;
    - a competing claim waits (seen in `pg_locks`) and gets exactly one outcome;
    - a committed rejection does not stop the next `seq` from applying.
  - **Direct SQL as `ops_app`:** forged confirmed rows, updates, deletes, TRUNCATE and `nextval` are refused. Forged,
    NULL, 0, missing and role-less actors on all three SECURITY DEFINER writers write nothing.
- **Commands** (`tests/test_v2_03c_commands.py`, 21 tests):
  - Every class A command is refused `ONLINE_ONLY` on both channels.
  - Class C commands run through the existing services and triggers at apply time.
  - Trigger refusals become stable codes. Lock timeout, deadlock, serialization failure, statement timeout and lost
    connection become `retry`, never a durable rejection.
  - A protective stop on a session that is not active gives `protective_state` (an interruption that blocks the
    start), never "stopped".
  - A duplicate incident, pause or interruption creates nothing new.
  - An incident while an earlier ordinary step is missing places the hold at once.
  - A field operator of another hub is refused.
- **Endpoint** (`tests/test_v2_03c_sync.py`, 64 tests, all over HTTP):
  - Section 6 items 1 to 9 and every 14.8 item-5 failure injection.
  - Authentication is checked first: one fixed 401 body that does not reveal whether a run exists, then CSRF.
  - Ended sessions (password reset, sign-out, deactivation) are refused.
  - Request limits return 413 and 400, and each command runs in its own transaction.
  - An unclassified error returns 500 with nothing stored; a failure during commit returns 503 `OUTCOME_UNKNOWN`.
  - Response schema snapshot (with `final`), workspace-zone times, and the ops lead's "last reported unsent
    count" fragment.
  - 14.9: `last_decided_outcome` validation (400), its causal storage, its absence from the digest, and `final`
    on each kind of answer, including `NOT_PERMITTED` re-checked after access returns.

## EM decisions (2026-11-26, recorded in contract 14.9)

1. **Lock order.** The ledger locks the run row first (14.5), before the domain's robot and visit locks, so an
   online command holding a robot or visit row and waiting for the run can deadlock with a field claim
   (PostgreSQL resolves it; the field side answers `retry`). **Approved fix, by 03C-1:** a ledger-only sync lock
   taken first, which removes the inversion. Pending until 03C-1 delivers it.
2. **Who can sync a run. Approved deviation.** A crew member of the run, or an ops lead with a grant at the run's
   hub. Narrower than 14.4's "regional": a regional engineer gets `NOT_YOUR_RUN`.
3. **Protective causal reference. Resolved by a schema addition (03C-3).** Each command may carry
   `last_decided_outcome` (`confirmed`, `rejected`, `abandoned` or null; anything else is HTTP 400): the outcome
   of `last_decided_seq` as the device saw it. It is stored in the protective command's causal reference as
   `outcome` and passed to `field_commands.apply`. It is untrusted metadata and is not part of the 9-field identity
   digest, so a resend that changes only it is a replay. A device that saw a rejection never has that step treated
   as confirmed: the test applies a protective step whose causal reference says "rejected", checks the stored
   reference keeps "rejected", and checks that the dependent ordinary step is still refused by the real state.
4. **`NOT_YOUR_RUN` response. Approved deviation.** A whole-request HTTP 403 with no run details.
5. **Field incident reports and the protective fallback.** Incident reports are permission-checked at the run's
   hub. A protective stop that cannot apply opens an interruption only; holds come only from incident reports.
   **Approved once its boundary is proven, by 03C-2.**
6. **Not stored durably. Approved deviation, with a qualification.** `KEY_REUSED`, `NOT_YOUR_RUN` and
   `NOT_PERMITTED` are not stored. Every result now carries `final`: `NOT_PERMITTED` (like `held` and `retry`)
   has `final=false`, meaning "may change if access changes; keep the step and ask an ops lead", and is re-checked
   on every resend. Tested: after the grant is restored, the same key applies. Confirmations and other rejections
   have `final=true`.
7. **Same key on two runs at once. Approved.** The second claim fails on the unique key and answers `retry`
   (bounded: nothing is claimed); the resend gets `KEY_REUSED`.

## Open items

- **Offline phone screen.** None exists yet; that is 03D.
- **Perf margin.** It is comfortable on CI (409–510 ms of a 1,000 ms budget). About 20 ms of each command is the
  domain command itself.
- **Carry-forward items** from the 03B closure (contract 14.7, P1 to P3) are being built in parallel on
  `v2/03-carry`. They will be submitted separately.
- **Usability** with independent people: UNVERIFIED.

## Narrow closure delta (EM review of 71d4eab, 2026-11-26)

New candidate: `e24c713`, CI run 45834076946 success, 2417 passed, 0 failed. Perf runs 45834078858, 45834096726 and
45834111585, all success.

| Measurement (p95) | Run 1 | Run 2 | Run 3 | Budget |
|---|---|---|---|---|
| Sync, batch of 20 | 561 ms | 381 ms | 318 ms | 1 s |
| Board | 382 ms | 145 ms | 122 ms | 2 s |
| Readiness (scale) | 1,536 ms | 1,121 ms | 960 ms | 2 s |

1. **Lock order, fixed** (`execution/0010_field_sync_lock`, merged from 03C-1).
   - Field claims, abandonment and device reports serialize on `execution_field_sync_lock(run_id)`. That table has no
     foreign key to the run, so taking it never locks the run row. No online path ever takes it (a test checks the
     database catalog and the source code).
   - The domain step then follows the established order: robot, visit, trip, run.
   - Lock graph section 5 shows the full acquisition order has no cycle.
   - Tests, run as `ops_app` with the waiter seen in `pg_locks`:
     - **online-first:** an online transaction holds the robot and the visit, and a field claim waits. Both commit,
       with no deadlock. With the old run-row-first statement restored locally, this test fails with "deadlock
       detected".
     - **field-first:** the online command waits, then completes.
   - The existing same-key and same-seq races give one outcome each, as before.
2. **Protective fallback boundary, proven, and one gap fixed** (`execution/0011`, merged from 03C-2;
   `tests/test_v2_03c_protective.py`, 11 tests).
   - An invalid stop creates or keeps one interruption, without duplicates.
   - START and RESUME are blocked by the service, the field endpoint, the online view and a direct SQL insert as
     `ops_app`. In each case the control succeeds after the interruption is resolved.
   - Site handover, recovery and `continue_visit` are blocked or do not enable work. Resolving needs triage
     permission. Holds are unchanged, and the response is always `protective_state`.
   - **Gap found and fixed:** the robot could be brought back and its run closed, then authorized and sent out on a
     new run while the operator interruption was still unresolved. `execution_robot_departure_problem` now refuses
     authorization and departure until every operator interruption on any of the robot's runs is resolved.
3. **Causal outcome, added** (merged from 03C-3).
   - Each request may carry `last_decided_outcome` (confirmed, rejected or abandoned). It is untrusted, is stored in
     the protective causal reference, and is not part of the identity digest.
   - A device that saw a rejection never has that step treated as confirmed (tested).
   - Every result carries `final`. `NOT_PERMITTED` gives `final=false`, meaning it may change if access changes.
4. **Contract 14.9** records the approved deviations 2, 4, 6 and 7.

Open: readiness reports the new interruption refusal under its existing code, `ROBOT_NOT_AT_HUB`. The text is
correct, but the Today label "The robot is not at the hub" is misleading for this case. Only operator interruptions
block a new departure; an incident interruption relies on its hold.
