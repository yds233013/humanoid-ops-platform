# Slice 5 Contract: Operations Command Center

Status: DRAFT rev 1 for engineering review · 2026-11-18 · baseline `slice-4-closed` (`31419fe`, 677 tests).
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
| `next_visit` (code, on-site start, readiness label) | planning + readiness evaluator (lazy, see D3) |
| `awaiting` | `post_visit_inspection` (hub receipt on an open run with no later inspection), `reconciliation`, `none` |

**Status label** (exactly one, first match wins, no "ready"/"cleared" wording):

| order | label | rule |
|---|---|---|
| 1 | `Unaccounted` | custody unaccounted |
| 2 | `Custody unknown` | custody unknown |
| 3 | `Deployed` | open run and custody vehicle or site |
| 4 | `Returned - awaiting inspection` / `- awaiting reconciliation` | open run and custody hub |
| 5 | `Held` | at hub, no open run, active blocking hold |
| 6 | `Assigned` | at hub, no hold, a confirmed upcoming visit |
| 7 | `At hub - not eligible` | at hub, eligibility false (reasons shown) |
| 8 | `At hub - eligible for planning` | otherwise (never "available to deploy") |

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
| `THRESHOLD_NOT_CONFIGURED` | a time-based rule's setting is missing or not a positive integer | — | engineering_lead | 1 |

Time thresholds are `fleet_deploymentsetting` keys (`ops_custody_verify_grace_min`, `ops_return_grace_min`,
`ops_inspection_grace_min`, `ops_readiness_lookahead_min`): schema-owner-written like every setting; the seed
writes SYNTHETIC demo values. A missing threshold disables only its rule and raises `THRESHOLD_NOT_CONFIGURED`
(fail visible, not fail silent). Settings are read inside the page snapshot; no settings lock is needed because
nothing is decided or written.

## 4. Other read models

- **Timeline** `timeline(at, window)`: confirmed visits in the window with readiness label; active runs with stage
  (departed / at site / session state / returning / at hub / awaiting reconciliation); overdue and unresolved
  items flagged with the exception codes above.
- **Incident queue** `incident_queue(at)`: open incidents (status not closed) ordered by severity rank in the
  latest decision's policy (unclassified ranks first), then age; shows status, hold active/released, escalation
  state, **derived owner** = role/person of the latest unacknowledged escalation, else ops_lead for `reported`,
  else safety_reviewer (D1).
- **Metrics** `metrics(start, end)` with exact definitions, all from immutable rows, each with numerator and
  denominator shown:
  - visit completion = runs closed in window / departures in window;
  - turnaround = median and p90 of (TripClosure time − departure) for runs closed in window;
  - return-without-robot count = trips with `arrived_hub` while custody was not hub at that time;
  - incident rate = incidents reported in window per 10 departures, and per robot;
  - fleet utilization = robot reserved minutes (all purposes of confirmed, departed visits) / (non-retired
    in-service robots × window minutes). Definitions are labelled synthetic-demo metrics, not targets.

## 5. Architecture

- New app `operations`: `readmodels.py`, `exceptions.py`, `metrics.py`, `views.py`, `urls.py`, `templates/**`.
  **No models with data** (`models.py` empty); migrations add only SQL views/functions and indexes.
- SQL (Engineer A, `operations` migrations): read-only views `operations_robot_status`, `operations_open_run_stage`,
  `operations_incident_queue`, set-based (LATERAL over the existing custody/run functions), `SECURITY INVOKER`,
  ops_app SELECT only; supporting indexes where EXPLAIN on the scale fixture shows sequential scans of growing
  tables.
- `operations.snapshot.read_snapshot()` (lead): context manager opening `REPEATABLE READ READ ONLY` and returning
  DB `now()`; every page and API call runs inside it.
- JSON API mirrors each page (`/api/ops/overview`, `/exceptions`, `/incidents`, `/timeline`, `/metrics`) for tests
  and future clients; paginated with keyset `(priority, since, id)`, 50 per page.
- Permissions (lead): `operations.read` (ops_lead, field_operator, engineer, engineering_lead, safety_reviewer,
  founder); `operations.metrics` (ops_lead, engineering_lead, safety_reviewer, founder).

## 6. Budgets and tests (closure bar)

1. **Oracle equality:** for randomized synthetic worlds (seeded RNG, ≥ 50 worlds) the SQL views equal the
   per-robot Python derivations (custody helpers, `fleet.services.evaluate_eligibility`, open runs) row for row.
2. **Exception rules:** each code has a firing and a non-firing test, plus boundary tests at exactly the threshold;
   determinism (same input ⇒ same ordered list); `THRESHOLD_NOT_CONFIGURED` when a key is missing or invalid.
3. **Snapshot consistency:** a writer commits between two statements of a page build (checkpoint barrier) and
   the page still reflects one snapshot; and the page never blocks a writer (writer completes while a page
   transaction is open).
4. **Query budget:** each page and API endpoint ≤ 12 SQL statements, constant between a small world and the scale
   fixture (captured with `connection.execute_wrapper`).
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
- **D3 Readiness label in overview/timeline:** the readiness evaluator is per-visit Python (many queries). Proposal:
  evaluate only for visits inside the lookahead window, capped at 25 per page, and show "not evaluated" beyond the
  cap; the budget in 6.4 excludes these capped evaluations and they are counted separately (≤ 25 × evaluator).
- **D4 Thresholds as deployment settings** (owner-written, synthetic demo values) and
  `THRESHOLD_NOT_CONFIGURED` when missing.
- **D5 Snapshot isolation** `REPEATABLE READ READ ONLY` per page (no locks), accepting that a page can be up to one
  request old.
