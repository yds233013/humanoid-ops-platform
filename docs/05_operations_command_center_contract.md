# Slice 5 Contract: Operations Command Center

Status: DRAFT rev 3 for engineering review · 2026-11-18 (rev 2 applied the EM review of rev 1 `b71cf03`; rev 3
applies the review of rev 2 `fd4d3c1`; section 9 is the correction matrix) · baseline `slice-4-closed` (`31419fe`, 677 tests).
Not frozen until the EM approves. Same rules as Slices 3-4: after freeze, names, signatures, file ownership and
budgets change only through the integration lead with the exact reason.

Everything is SYNTHETIC. Slice 5 answers "what needs attention across the fleet right now?". It records nothing
new about robots, visits or incidents.

## 0. Principles

1. **A read model, not a second source of truth.** No table in Slice 5 stores a robot, visit, run, custody or
   incident status. Every status, exception and metric is derived at read time from the existing append-only
   rows and the existing derivations (fleet eligibility, planning feasibility, readiness evaluation,
   `execution_current_custody`).
2. **One definition per fact.** Where a derivation already exists in SQL (custody, open run, departure problem,
   active policies) Slice 5 calls it; where it exists only in Python (fleet eligibility, readiness evaluation)
   Slice 5 either calls it or re-expresses it in set-based SQL **with an oracle test proving equality**.
3. **Consistent snapshot, no blocking.** Each page is built inside one `REPEATABLE READ READ ONLY` transaction and
   shows its "as of" database time. Read models take no row locks, so the command center can never delay a
   departure, an incident report or a custody move.
4. **Actions stay where they are.** Every exception links to the existing page and command that resolves it. The
   command center has no write endpoints.
5. **Unknown is visible.** A missing threshold, unknown custody or unclassified severity is shown as its own
   exception, never silently treated as fine.
6. **Bounded cost.** Every page runs a fixed number of SQL statements, independent of fleet size, within the
   budgets in section 6.

## 1. Scope

| In | Out |
|---|---|
| Fleet overview (one row per robot, derived status + reasons) | Any new robot/visit/run/incident state or write command |
| Operations timeline (upcoming visits, active runs, overdue returns, open reconciliations) | Notifications, paging, email (later; needs the outbox and an owner decision) |
| Exception inbox with stable reason codes, priority and drill-down | Predictive/ML scoring, maintenance forecasting |
| Incident queue (open incidents by severity, age, escalation state) | Teleoperation, telemetry ingestion |
| Operational metrics over a date window, with exact definitions | Billing, customer-facing reporting |
| Drill-down from every item to its immutable records and the visit dossier | Founder presentation |

## 2. Derived robot status (fleet overview)

`operations.readmodels.fleet_overview(at) -> list[RobotStatus]`, one row per non-retired robot:

| field | source |
|---|---|
| `robot`, `lifecycle`, `pattern`, `manifest_id` | fleet rows |
| `custody` (`hub/vehicle/site/unaccounted/unknown`, `verified`) | `execution_current_custody` |
| `open_run` (code, visit, stage) | `execution_open_run_id` + run stream rows |
| `holds` (active blocking hold codes, robot or pattern scope) | `fleet_hold` |
| `eligible_for_planning` + reasons | fleet eligibility (oracle-tested SQL, see D2) |
| `next_visit` (code, on-site start) | planning rows only. **No readiness label on the overview** (D3): readiness is shown only where the authoritative evaluator ran (section 4a) or on the existing visit page |
| `awaiting` | `post_visit_inspection` (hub receipt on an open run with no later inspection), `reconciliation`, `none` |

**Status label** (exactly one, first match wins, no "ready"/"cleared" wording):

| order | label | rule |
|---|---|---|
| 1 | `Unaccounted` | custody unaccounted |
| 2 | `Custody unknown` | custody unknown |
| 3 | `Deployed` | open run and custody vehicle or site |
| 4 | `Returned - awaiting inspection` / `- awaiting reconciliation` / `- awaiting trip closure` | open run and custody hub: no post-visit inspection after the hub receipt / inspected but the latest reconciliation is missing or unresolved / reconciled but no TripClosure yet |
| 5 | `Held` | at hub, no open run, active blocking hold |
| 6 | `Assigned` | at hub, no hold, a confirmed upcoming visit; the row always shows `eligible_for_planning` and its reasons next to the label (an assigned robot can be ineligible; this is a scheduling description, never deployment permission) |
| 7 | `At hub - not eligible` | at hub, eligibility false (reasons shown) |
| 8 | `At hub - eligible for planning` | otherwise (never "available to deploy") |

A robot with an active blocking hold and a confirmed visit shows `Held` (rule 5 precedes rule 6) and the
`HELD_ROBOT_ASSIGNED` exception stays listed. No status, label or colour on any Slice 5 page means departure
authorization; the existing readiness and authorization pages remain the only place that decides that.

## 3. Exception rules

`operations.exceptions.open_exceptions(at) -> list[OpsException]`; `OpsException(code, priority, entity_type,
entity_ref, robot, visit, since, owner_role, message, action_url)`. Deterministic: same rows + same `at` ⇒ same
list, ordered by `(priority, since, code, entity_ref)`. Priority is an **operational** triage order, not a safety
severity.

| code | fires when | since | owner role | priority |
|---|---|---|---|---|
| `ROBOT_UNACCOUNTED` | custody unaccounted | the custody event | ops_lead | 1 |
| `CUSTODY_UNKNOWN` | custody unknown | latest custody/run row | ops_lead | 1 |
| `INCIDENT_UNTRIAGED` | incident still `reported` | reported_at | ops_lead | 2 |
| `ESCALATION_UNACKNOWLEDGED` | escalation without ack | escalation time | its `to_role` | 2 |
| `SEVERITY_UNCLASSIFIED` | open incident, no severity decision while an active incident policy exists | reported_at | safety_reviewer | 3 |
| `INTERRUPTION_OPEN` | interruption without resolution on an open run | interruption time | ops_lead | 2 |
| `SITE_NO_GO` | latest site check of an open run is no_go | check time | ops_lead | 3 |
| `CUSTODY_UNVERIFIED` | departure assignment not verified after `ops_custody_verify_grace_min` | departure | field crew | 3 |
| `RETURN_OVERDUE` | open run, no `arrived_hub`, now > planned unloading end + `ops_return_grace_min` | planned end | ops_lead | 2 |
| `RECONCILIATION_UNRESOLVED` | latest reconciliation unresolved | its time | ops_lead | 3 |
| `POST_VISIT_INSPECTION_MISSING` | hub receipt on an open run, no inspection after it, after `ops_inspection_grace_min` | hub receipt | field crew | 3 |
| `HELD_ROBOT_ASSIGNED` | robot with an active blocking hold is on a confirmed upcoming visit | hold placed | ops_lead | 2 |
| `READINESS_INCOMPLETE_SOON` | confirmed visit starting within `ops_readiness_lookahead_min` not "Ready to request authorization" | now - lookahead | ops_lead | 3 |
| `THRESHOLD_NOT_CONFIGURED` | a time-based rule's setting is missing or not a positive integer; exactly one per setting key, `entity_ref` = the key | the snapshot time (not when it became invalid) | engineering_lead | 1 |

Time thresholds are `fleet_deploymentsetting` keys (`ops_custody_verify_grace_min`, `ops_return_grace_min`,
`ops_inspection_grace_min`, `ops_readiness_lookahead_min`): schema-owner-written like every setting; the seed
writes SYNTHETIC demo values. A missing threshold disables only its rule and raises `THRESHOLD_NOT_CONFIGURED`
(fail visible, not fail silent); it never disables another rule or any fleet status. Boundary rule for every
time-based code: it fires when `at >= since + threshold` (inclusive), tested at 1 µs before, exactly at and after.
Settings are read inside the page snapshot and are read-only from the command center; no settings lock is
needed because nothing is decided or written.

**Total ordering** (identical in SQL, Python, API and pages): `(priority ASC, since ASC, code ASC, entity_ref ASC)`;
`(code, entity_ref)` is unique per exception, so the order is total. `entity_ref` is always the identifier of the
single underlying row the exception is about, so repeated events never collide:

| code | entity_ref |
|---|---|
| ROBOT_UNACCOUNTED, CUSTODY_UNKNOWN | robot code |
| HELD_ROBOT_ASSIGNED | `<robot code>/<visit code>` (one per held robot and confirmed visit) |
| INCIDENT_UNTRIAGED, SEVERITY_UNCLASSIFIED | incident code |
| ESCALATION_UNACKNOWLEDGED | `ESC-<escalation id>` (one per unacknowledged escalation; several per incident allowed) |
| INTERRUPTION_OPEN | interruption code |
| SITE_NO_GO, CUSTODY_UNVERIFIED, RETURN_OVERDUE, RECONCILIATION_UNRESOLVED, POST_VISIT_INSPECTION_MISSING | run code |
| READINESS_INCOMPLETE_SOON | visit code |
| THRESHOLD_NOT_CONFIGURED | setting key |

A test builds repeated escalations, interruptions and visits for one robot/incident and asserts uniqueness.

### Resolution path per code (R6)

| code | authoritative fact | where it is addressed (existing page / command) | permission | always resolvable? |
|---|---|---|---|---|
| ROBOT_UNACCOUNTED | latest custody event to unaccounted | `/execution/runs/<run>/` transfer (recovered) or incident page `recover_robot` | record_field_event + ops_lead | no: only when the robot is genuinely recovered |
| CUSTODY_UNKNOWN | custody function returns unknown | none in the UI: data integrity issue, escalate to engineering | — | no |
| INCIDENT_UNTRIAGED | no status change after `reported` | `/execution/incidents/<code>/` status -> triaged | triage_incident | yes |
| ESCALATION_UNACKNOWLEDGED | escalation without ack | incident page, acknowledge | holder of `to_role` | yes |
| SEVERITY_UNCLASSIFIED | no severity decision, active policy exists | incident page, severity | change_severity | yes while a usable policy exists |
| INTERRUPTION_OPEN | interruption without resolution | run page, resolve interruption | triage_incident | no while its incident hold is unreleased (fleet hold workflow; may need a policy that does not exist yet) |
| SITE_NO_GO | latest site check no_go | run page, new site check, or disposition | record_field_event / record_disposition | no: the site may stay No-Go; end the visit instead |
| CUSTODY_UNVERIFIED | administrative assignment without verification | run page, verify custody | record_field_event (crew) | yes |
| RETURN_OVERDUE | no arrived_hub past planned unloading end + grace | run page, trip event | record_field_event | yes, when the vehicle is back |
| RECONCILIATION_UNRESOLVED | latest reconciliation unresolved | run page, reconcile | reconcile | no while the robot is not back |
| POST_VISIT_INSPECTION_MISSING | no inspection after hub receipt | fleet inspection command | submit_inspection | yes |
| HELD_ROBOT_ASSIGNED | blocking hold + confirmed visit | `/fleet/<robot>/` hold release, or re-plan on `/planning/` | fleet.release_hold / planning.plan | no while the hold's release policy is missing |
| READINESS_INCOMPLETE_SOON | evaluator says not ready (section 4a) | `/readiness/visits/<code>/` | readiness permissions | not always |
| THRESHOLD_NOT_CONFIGURED | deployment setting missing/invalid | schema owner (outside the app) | — | outside the app |

The command center never claims that every exception can be cleared.

## 4. Other read models

- **Timeline** `timeline(at, window)`: confirmed visits in the window with readiness label; active runs with stage
  (departed / at site / session state / returning / at hub / awaiting reconciliation); overdue and unresolved
  items flagged with the exception codes above.
- **Incident queue** `incident_queue(at)`: open incidents (status not closed) ordered by severity rank in the
  latest decision's policy (unclassified ranks first), then age, then code; shows status, hold active/released,
  escalation state, and a **"Suggested follow-up owner"** = role/person of the latest unacknowledged escalation,
  else ops_lead for `reported`, else safety_reviewer (D1). It is a suggestion, not a formal assignment.

### 4a. Readiness inside the command center (D3, revised)

- The overview shows **no** readiness label.
- `READINESS_INCOMPLETE_SOON` and the timeline evaluate, with the authoritative `evaluate_readiness`, only
  confirmed visits whose on-site start is within `ops_readiness_lookahead_min`, earliest first, **at most 10**.
  Visits beyond the cap show **"Not evaluated"** and are counted ("N more not evaluated"); nothing is ever shown as
  ready without the evaluator having run in this snapshot.
- Selection is deterministic: qualifying visits ordered by `(on_site_start, visit id)`, the first 10 evaluated.
- When qualifying visits remain unevaluated, the readiness part of the inbox is **partial**: the inbox and the API
  carry `readiness_coverage = {"qualifying": N, "evaluated": 10, "not_evaluated": N - 10, "complete": false}`, the
  page shows a prominent banner "Readiness exceptions are incomplete: N - 10 upcoming visits were not evaluated",
  and links to `/readiness/` (the existing upcoming-visits list) for those visits. The inbox is never presented as
  exhaustive in that case; `complete` is true only when every qualifying visit was evaluated.
- The query budget is explicit and total (section 6.4): those pages are budgeted at
  `12 + 10 × E` statements, where `E` = the evaluator's statement count for one visit, pinned by a test on the
  scale fixture (the test fails if `E` grows, so the bound stays honest).
- **Metrics** `metrics(start, end, at)` with exact definitions, all from immutable rows, numerator and denominator
  shown; a zero denominator shows **N/A**, never 0. Each metric states its kind (R3):

| metric | kind | definition |
|---|---|---|
| Departure cohort completion | historical cohort | departures in [start, end) whose run has a TripClosure by `at` / departures in [start, end) |
| Turnaround | historical cohort | median and p90 of (TripClosure time − departed_at) over the same cohort's closed runs |
| Return without robot | event count, historical state by event order | see "Historical custody at vehicle arrival" below |
| Incident rate | event count | incidents with `occurred_at` in the window per 10 departures in the window, and per robot that departed in the window |
| Scheduled robot utilization | event over window, current-fleet denominator | per robot, union of reservation intervals of confirmed or departed visits, clipped to [start, end), summed / (robots currently in_service × window minutes); labelled "current fleet denominator: lifecycle history before `at` is not reconstructed" |
| Open exceptions / open incidents | current state at `at` | counts from section 3 / the incident queue |

  All are labelled synthetic-demo measures, not targets.

**Historical custody at vehicle arrival (event order, not timestamps).** Timestamps alone are not a total order
across the custody and trip-event streams. Slice 5 adds one column, `execution_tripevent.custody_seq_at_event`
(Engineer A; an operations-owned migration touching only this column and the trip-event insert trigger): the
trigger first locks the robot `FOR UPDATE`, then the run (global order: robot before run; the command already holds
both), and records the robot's latest custody `seq` when the trip event is inserted. Every custody insert also
locks the robot, so the two streams are totally ordered per robot by that lock, and the recorded seq is exactly
the custody that existed when the vehicle arrived. Later hub receipts never change it (append-only row).

Each `arrived_hub` event is classified by the custody event with that seq:
- `with_robot`: to_kind vehicle on this trip;
- `robot_unaccounted`: to_kind unaccounted;
- `robot_at_site`: to_kind site;
- `robot_already_at_hub`: to_kind hub (not reachable through the triggers; counted if it appears);
- `ambiguous`: the column is NULL (trip events recorded before this migration); reported as its own count, never
  guessed from timestamps.

"Return without robot" = `robot_unaccounted` + `robot_at_site`. Acceptance tests: vehicle returns with the robot;
after the robot became unaccounted; while the robot is at the site; a hub receipt recorded after the arrival
(including in the same transaction) leaves the classification unchanged; legacy NULL rows are `ambiguous`.

## 5. Architecture

- New app `operations`: `readmodels.py`, `exceptions.py`, `metrics.py`, `views.py`, `urls.py`, `templates/**`.
  **No models with data** (`models.py` empty); migrations add only SQL views/functions and indexes.
- SQL (Engineer A, `operations` migrations): read-only views `operations_robot_status`, `operations_open_run_stage`,
  `operations_incident_queue`, set-based (LATERAL over the existing custody/run functions), `SECURITY INVOKER`,
  ops_app SELECT only; supporting indexes where EXPLAIN on the scale fixture shows sequential scans of growing
  tables.
- `operations.snapshot.read_snapshot()` (lead): view decorator that opens `transaction.atomic()` and issues
  `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` as the **first** statement of the transaction,
  before the permission check (the session/auth middleware reads run earlier in their own autocommit statements and
  are not part of the snapshot). The snapshot time is `transaction_timestamp()`, passed explicitly as `at` to every
  time-dependent calculation. Inside the snapshot: no `FOR UPDATE/SHARE`, no advisory locks, no `run_command`, no
  `nextval`, no `set_config`, no function with hidden writes; a test captures every statement of every endpoint
  and asserts this (READ ONLY also makes PostgreSQL refuse any write). It is a consistent snapshot, not a promise
  of the latest state at response time.
- JSON API mirrors each page (`/api/ops/overview`, `/exceptions`, `/incidents`, `/timeline`, `/metrics`) for tests
  and future clients; keyset pagination on exactly the section 3 ordering, 50 per page. The cursor encodes the last
  sort key, the filters and the snapshot time it came from; **each continuation page is a new snapshot** and says
  so ("page 2 as of T2; page 1 was as of T1"). We do not claim to recreate an earlier MVCC snapshot.
- Permissions (lead): `operations.read` (ops_lead, field_operator, engineer, engineering_lead, safety_reviewer,
  founder); `operations.metrics` (ops_lead, engineering_lead, safety_reviewer, founder). Two layers, both tested
  (R5): PostgreSQL — ops_app can SELECT each view and gains no write privilege through it (views are SECURITY
  INVOKER, owned by the schema owner, no rules/triggers); Django — a user without `operations.read` gets 403 from
  every page and API endpoint and no data in the body.

## 6. Budgets and tests (closure bar)

1. **Oracle equality:** the SQL views equal the per-robot Python derivations (custody helpers,
   `fleet.services.evaluate_eligibility`, open runs) row for row, comparing the full result: eligible flag **and
   every reason**, robot and pattern holds, manifest/pattern approval, base authorization, capability profile
   (approved / unknown fields / synthetic in production), demo vs production mode, several hold-policy and pattern
   versions. Both ≥ 50 seeded random worlds **and** a hand-written edge-case table (one row per eligibility reason,
   each missing/unknown datum, mode switch).
2. **Exception rules:** each code has a firing and a non-firing test, plus boundary tests at exactly the threshold;
   determinism (same input ⇒ same ordered list); `THRESHOLD_NOT_CONFIGURED` when a key is missing or invalid.
3. **Snapshot consistency:** a writer commits between two statements of a page build (checkpoint barrier) and
   the page still reflects one snapshot; and the page never blocks a writer (writer completes while a page
   transaction is open).
4. **Query budget:** each page and API endpoint ≤ 12 SQL statements, constant between a small world and the scale
   fixture (captured with `connection.execute_wrapper`); the two pages that run the readiness evaluator (section 4a)
   ≤ `12 + 10 × E` in total, with `E` pinned by a test.
5. **Time budget:** on the scale fixture (200 robots, 2,000 visits, 600 runs, 300 incidents, all synthetic) each
   endpoint's server time p95 ≤ 400 ms over 20 runs on the local Postgres, measured in a dedicated marked test
   (`-m perf`, run once by the lead and in CI as a non-flaky upper bound of 2 s).
6. **Drill-down:** every exception and queue row links to an existing page that renders for its owner role.
7. **No writes:** a test asserts the operations views run no INSERT/UPDATE/DELETE and take no row locks.
8. **UI:** pages render for every read role, 403 otherwise; wording check (no "cleared", "safe", "ready to
   deploy"); live synthetic walkthrough producing at least one exception of each code from the seed + scripted
   events, then resolving them through the existing pages and watching them disappear.
9. **Integration:** fresh migrations, full suite once, ruff/format/mypy/migration check, green Actions.

## 7. File ownership and sequence

| Owner | Files (exclusive) |
|---|---|
| Lead | app shell, `operations/snapshot.py`, permissions, settings keys + `seed_operations`, `config/**`, docs, README, CI, `tests/operations_world.py` (scale fixture shared), integration |
| A, SQL | `operations/migrations/**`, `tests/test_operations_sql.py` (oracle + no-locks + indexes/EXPLAIN) |
| B, exceptions | `operations/exceptions.py`, `tests/test_operations_exceptions.py` |
| C, read models + metrics | `operations/readmodels.py`, `operations/metrics.py`, `tests/test_operations_readmodels.py`, `tests/test_operations_metrics.py`, `tests/test_operations_perf.py` |
| D, UI + API | `operations/views.py`, `operations/urls.py`, `operations/api.py`, `operations/templates/**`, `tests/test_operations_ui.py` |

Sequence: lead phase 0 → A (views + oracle) → merge A → B, C in parallel → D → lead merge, perf run, walkthrough,
CI → EM review.

## 8. Decisions requested

- **D1 Incident ownership:** derived from escalations (proposal) vs a new append-only `IncidentAssignment`
  command. Proposal: derived in Slice 5; explicit assignment only if the EM wants it, as a small Slice 4 addendum.
- **D2 Eligibility in SQL:** re-express fleet eligibility as a set-based SQL view with an oracle test (proposal),
  vs calling the Python per robot (simpler, O(N) queries, breaks budget 4).
- **D3 Readiness (approved as in 4a):** no readiness label on the overview; the authoritative evaluator runs only for
  the first 10 qualifying visits in the lookahead (ordered by on-site start, then visit id); beyond the cap "Not
  evaluated" with a count and a partial-coverage banner; inclusive total budget `12 + 10 × E` with `E` pinned by a
  test (6.4).
- **D4 Thresholds as deployment settings** (owner-written, synthetic demo values) and
  `THRESHOLD_NOT_CONFIGURED` when missing.
- **D5 Snapshot isolation** `REPEATABLE READ READ ONLY` per page (no locks), accepting that a page can be up to one
  request old.

## 9. Rev 2 correction matrix (EM review of `b71cf03`)

| item | change |
|---|---|
| D1 | approved; field renamed "Suggested follow-up owner" (section 4) |
| D2 | oracle compares the full eligibility result incl. every reason, modes and policy/pattern versions, plus an edge-case table (6.1) |
| D3 | overview shows no readiness label; evaluator only in a bounded lookahead (≤ 10 visits), explicit total budget `12 + 10 × E` with `E` pinned; beyond the cap "Not evaluated" (4a, 6.4) |
| D4 | one THRESHOLD_NOT_CONFIGURED per key, since = snapshot time, inclusive `>=` boundary tested at -1 µs / = / +, never disables other rules or statuses, read-only (3) |
| D5 | snapshot set as the first statement before the permission check, `transaction_timestamp()` as `at`, no locks / nextval / set_config / command wrapper, captured-statement test (5) |
| R1 | Held precedes Assigned; Assigned shows eligibility; new `Returned - awaiting trip closure` closes the gap; no status means departure authorization (2) |
| R2 | departure-cohort completion; utilization with clipping and per-robot union, current-fleet denominator labelled; N/A on zero denominators (4) |
| R3 | every metric labelled event count / historical cohort / current state; return-without-robot uses custody at the arrival event's time (4) |
| R4 | one total ordering everywhere; cursor encodes key + filters + snapshot time; each continuation page is a new snapshot and says so (3, 5) |
| R5 | view-privilege tests for ops_app and 403/no-data tests for users without operations.read (5) |
| R6 | resolution-path table per exception code incl. "not always resolvable" cases (3) |
| Rev 3: readiness coverage | partial-coverage flag, count, banner and link when the cap cuts qualifying visits; deterministic (start, id) selection (4a) |
| Rev 3: return-without-robot order | new `custody_seq_at_event` on trip events, set under the robot lock; classification by event order; legacy rows `ambiguous`; four acceptance cases (4) |
| Rev 3: section 8 D3 | replaced with the approved design (8) |
| Rev 3: exception identity | entity_ref per code; escalations keyed by escalation id; uniqueness test (3) |

## 10. EM-approved amendment A1 (performance and semantics, 2026-11-19)

Approved by the EM during integration; it changes only the items below and reopens nothing else.

| item | amendment |
|---|---|
| Latency budgets (6.5) | p95 ≤ 400 ms: fleet overview, incident queue, metrics, exception inbox **without** readiness evaluation. p95 ≤ 2,000 ms (separate release gate, still tested): timeline and exception inbox **with** readiness evaluation. Evaluator time is measured and reported separately from the read-model work. |
| SQL budget (6.4) | unchanged: 12 + 10 × E; E provisionally pinned at 70 (measured 60-69 at scale). |
| Readiness cap (4a) | unchanged: 10 authoritative evaluations per page, partial-coverage disclosure. |
| Metrics (4) | the metrics page never runs the readiness evaluator. It shows "Open operational exceptions - readiness not evaluated" from `open_exceptions(..., include_readiness=False)`, never labelled as the total. |
| READINESS_INCOMPLETE_SOON `since` (3) | `on_site_start - ops_readiness_lookahead_min`; qualifying visit = confirmed, current revision, not departed, `on_site_start - L <= at` and `on_site_end > at`. |
| Thresholds (3) | valid = ASCII digits, 1..5,256,000 minutes; anything else is not configured. |
| Text ordering (3) | text sort keys compared with `COLLATE "C"` so SQL and Python order identically. |
| ESCALATION_UNACKNOWLEDGED (3) | kept after the incident is closed (closure is not acknowledgment); the item shows the incident's status. |
| RETURN_OVERDUE basis (3) | if the planned unloading end is unknown, the exception is not produced and no deadline is invented; the missing basis is exposed as unknown (inbox `unknown_basis`, timeline `RETURN_DEADLINE_UNKNOWN`). |
| Action links (3) | where no dedicated page exists (post-visit inspection, held robot, custody unknown) the link is the robot's fleet page; THRESHOLD_NOT_CONFIGURED has none. |
