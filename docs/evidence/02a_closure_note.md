# 02A closure note: hub work queue

Branch `v2/phase02`. `main` and the Northbay demo are unchanged. All data is fictional.

## Candidate

| Item | Value |
|---|---|
| Final SHA | `d5f8f71`: 02A `6ea7d3e` plus J8 polish `d5f8f71` |
| Diff | `2c5cf83...d5f8f71` (contract approval -> 02A) |
| Migrations (additive, reversible) | `hub/0001_initial` (`hub_workorder` and its checks; partial unique index on `(source_kind, source_ref)` for active tasks); `hub/0002_workorder_privileges` (see below); `accounts/0004_hub_technician_role` (role choices only) |
| Privileges | Contract 8.8: `ops_app` has exactly SELECT, INSERT and UPDATE on `hub_workorder`, plus use of its sequence. DELETE, TRUNCATE, REFERENCES and TRIGGER are revoked, and tests prove DELETE and TRUNCATE fail. No other privilege changed. |
| Full CI on `d5f8f71` | run 45162549711: success, 1,899 passed, 14 skipped, 0 failed (lint, format, mypy, migrations check) |

## Performance on `d5f8f71` (200-robot fixture, p95 in ms)

Perf runs: 45162549535, 45162558766, 45162568302.

| Path | Ceiling | p95 per run |
|---|---|---|
| Hub `/hub/` (300 tasks) | 2,000; target 400 | 91 / 184 / 139: ceiling met 3/3, target met 3/3 |
| Today `/ops/` | 2,000; target 400 | 240 / 459 / 219 |
| Readiness list | 2,000 | 852 / 1,230 / 830 |

`/hub/` makes 10 statements (budget 15), and that number stays the same as tasks and holds grow (test).

## What 02A delivers

**Work orders.**

- The transitions are fixed: open, then assigned, then in progress, then done. Cancelling needs a reason.
- Every transition writes an audit row; every refusal writes a denied audit row.
- Forms carry the task's version, so an action on a stale page is refused in plain words.
- A one-time form token stops double submits.
- A JSON API at `/api/hub/tasks` follows the same rules.

**Authorization.** It checks the broad `hub.work` permission first, then this specific task's ownership and the
allowed transition. A field operator works only their own tasks. A hub technician can take only unowned tasks, and only
for themselves.

**Derived tasks.** These are refreshed by one set-based upsert on every Hub page load. The same refresh is available
as `manage.py refresh_hub_tasks`. Sources:

- periodic inspections that are overdue, have never been done, or fall due within the look-ahead;
- active robot and configuration holds;
- the latest failed equipment check for a visit that is still upcoming.

Duplicates are prevented:

- Each source has a stable reference, and the partial unique index allows only one active task per source.
- A source makes at most one task, ever. A finished hold task does not reappear while the hold remains.
- When the underlying fact disappears, the task shows "Resolved by the underlying record". It is not marked done,
  because no work was recorded.

**Hub screen.**

- Queue chips: all, assigned to me, overdue, no owner, resolved.
- Counts by type and by owner.
- A table where each row gives the next step and links to the real workflow (inspection form, hold page, visit
  readiness), with the action on its own line.
- Tasks closed in the last 7 days.
- An "Add a task" form.

Everything is worded plainly, and the page says on screen that a task is a to-do only.

**`hub_technician` role.**

- Has only the 02A permissions, as in contract 8.6.
- Is offered in the setup invite list.
- Lands on `/hub/` after login, including when it is held together with field operator.
- In the demo, lily.tran has both roles, and there are three hand-made tasks.

## Tests (all green in CI)

`tests/test_v2_02a_hub.py` has 31 tests. They cover:

- transitions and their audit rows, the fixed graph, and refusal of a stale version;
- the role matrix, and refusal of page and API POSTs for each of the 8 roles (403, nothing written);
- ownership per task, and field operators seeing only their own tasks;
- derivation from all three sources;
- five refreshes leaving one active task, and a finished task not coming back;
- source cleared, with a real hold release through its approvals and a new periodic inspection;
- concurrent refreshes on two `ops_app` connections, in both orders;
- concurrent start of the same task, and concurrent assign against cancel;
- **non-interference:** for every task type, complete, reassign or cancel, then check that holds, inspections,
  custody, readiness evidence, planning eligibility and non-hub audit rows are unchanged;
- the privileges;
- the "You" wording, the chip markup, and the dual-role landing.

Safety suites: v1 and Slice 7 readiness, custody, hold and incident suites pass unchanged in the same CI run.

## J8 browser walk

The walk ran on a local server of the same code, with the Northbay demo rebuilt, signed in as lily.tran
(hub technician and field operator), at 768 px and 1,024 px.

1. The queue showed 7 open tasks: 3 hand-made, 3 robot holds, 1 periodic inspection.
2. WO-0001 (restock kits): start, then done with a note. 4 recovery kits restocked.
3. WO-0004 (robot NB-019 on hold): take, start, done with a note.
4. Reload: WO-0004 was not recreated, and both tasks are listed under "Closed in the last 7 days".
5. Non-interference seen in the browser: Today still shows **3 on hold**, including "Robot NB-019 is on hold but is
   booked for visit V-0084".
6. 52 s in total, with no horizontal scroll at either width.
7. Four polish defects were found and fixed in `d5f8f71`, then checked again in the browser:
   - cramped counts are now spaced chips;
   - an owner sees "You" wording;
   - the action sits on its own line;
   - the dual-role user lands on the Hub.

Usability with independent people is still **UNVERIFIED**, as in Phase 01.

## Scope notes

- **Configuration holds** also produce hold tasks, with no robot set.
- **Periodic interval unknown** stays in the exceptions inbox. It is an engineering configuration problem, so no hub
  task is made for it.
- **02B review item (from the EM's 02A authorization), carried forward:** charging sessions and charger reservations
  must not conflict across tables, with concurrent transaction tests.
