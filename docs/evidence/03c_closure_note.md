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
- **Endpoint** (`tests/test_v2_03c_sync.py`, 50 tests, all over HTTP):
  - Section 6 items 1 to 9 and every 14.8 item-5 failure injection.
  - Authentication is checked first: one fixed 401 body that does not reveal whether a run exists, then CSRF.
  - Ended sessions (password reset, sign-out, deactivation) are refused.
  - Request limits return 413 and 400, and each command runs in its own transaction.
  - An unclassified error returns 500 with nothing stored; a failure during commit returns 503 `OUTCOME_UNKNOWN`.
  - Response schema snapshot, workspace-zone times, and the ops lead's "last reported unsent count" fragment.

## Decisions for the EM

1. **Lock order.** The ledger locks the run row first (14.5), before the domain's robot and visit locks.
   - An online command that holds a robot or visit row, then waits for the run, can deadlock with a field claim.
     PostgreSQL resolves the deadlock, no partial state is possible, and the field side answers `retry`.
   - The alternative is a ledger-only lock row taken first, which removes the inversion. Keep the current order, or
     switch?
2. **Who can sync a run.** Only a crew member of the run, or an ops lead with a grant at the run's hub. This is
   narrower than 14.4's "regional": a regional engineer gets `NOT_YOUR_RUN`.
3. **Protective causal reference.** It holds `last_decided_seq` and `generation`. The outcome of that step as the device
   saw it (14.8 item 1) has no request field yet; adding one is a schema decision.
4. **`NOT_YOUR_RUN` response.** It is answered as a whole-request HTTP 403, with no run details, rather than as
   per-command results.
5. **Field incident reports.** These are permission-checked at the run's hub, which is stricter than the online
   report. A protective stop that cannot apply opens an interruption only. Holds come only from incident reports.
6. **Not stored durably.** `KEY_REUSED`, `NOT_YOUR_RUN` and `NOT_PERMITTED` are not stored, because the writer refuses
   actors without access. A resend after the grant is restored is checked again.
7. **Same key on two runs at once.** The second claim fails on the unique key (23505) and answers `retry`. A resend
   then gets `KEY_REUSED`.

## Open items

- **Offline phone screen.** None exists yet; that is 03D.
- **Perf margin.** It is comfortable on CI (409–510 ms of a 1,000 ms budget). About 20 ms of each command is the
  domain command itself.
- **Carry-forward items** from the 03B closure (contract 14.7, P1 to P3) are being built in parallel on
  `v2/03-carry`. They will be submitted separately.
- **Usability** with independent people: UNVERIFIED.
