# Slice 7 closure note: production guardrail hardening

Evidence for closing Slice 7 against the closure bar in `docs/07a_production_guardrails_contract.md` section 14.
SYNTHETIC data only.

## 0. What this note does and does not say

**Verified (software):** the Slice 7 guardrails are built, tested and enforced. In production mode the platform
refuses every authorization, release and approval that depends on a missing company decision, it says why, and
direct SQL as the runtime role cannot get around it.

**Not approved (deployment):** none of this approves a real production deployment. The platform supplies no
company decision. A production deployment still needs every prerequisite in section 7, recorded by the people who
own it, with their approvals. The scratch-database walkthrough shows that the safeguards refuse unsafe actions; it
does not show that the company inputs exist.

## 1. Release

| Item | Value |
|---|---|
| main | `9bb6e96` (fast-forwarded with the EM's approval, from `94861fa`, which was approved earlier) |
| Integration branch | `s7/int` = `9bb6e96` |
| Contract | `docs/07a_production_guardrails_contract.md` (frozen at `0a8bdab`) |
| SQL signatures and change log | `docs/07b_s7_sql_signatures.md` (CR-1, sections 13 and 13.1) |
| CI on main `9bb6e96` | run 44683571156: backend and xlsx green; 1440 passed, 11 skipped, 0 failed |
| Perf on main `9bb6e96` | run 44683585124: every endpoint within budget (close-out 159 / 187 ms) |

## 2. Tests

- Full suite, CI on `9bb6e96`: 1440 passed, 11 skipped, 0 failed (run 44680451881, branch `s7/closeout`, same
  commit). `ruff check`, `ruff format --check`, `mypy .` (89 files) and `makemigrations --check` all pass.
- The `xlsx` job (Slice 6 reconciliation) is green in the same run.
- Slice 7 test files: `test_s7_db.py` (direct `ops_app` SQL bypass tests), `test_s7_concurrency.py` (both-order
  races), `test_s7_readiness.py`, `test_s7_fleet.py`, `test_s7_operations.py`, `test_s7_ui.py`,
  `test_s7_integration.py`, `test_s7_oracles.py`, `test_s7_option_b.py`, `test_s7_perf_oracles.py`.

## 3. Migrations

Slice 7 adds nine migrations. All are additive; no earlier migration was edited.

| Migration | Purpose |
|---|---|
| `fleet/0008_s7_templates_rules_policies` | Inspection-template approvals, periodic inspection rules, hold-policy lifecycle and rebasing |
| `planning/0006_s7_qualification` | `readiness_inspector` qualifications |
| `execution/0005_s7_incident_release_and_deadlines` | Incident-hold release, escalation deadline sets |
| `readiness/0007_s7_guardrails` | Authoritative periodic status, issue-function and departure guards |
| `readiness/0008_s7_evidence_time_source` | Evidence time source fix |
| `operations/0003_s7_views` | Production-blocker and close-out views |
| `operations/0004_s7_eligibility_periodic` | CR-1: periodic reason in planning eligibility |
| `operations/0005_s7_eligibility_setbased` | Performance: set-based periodic status in the eligibility view (EM decision A) |
| `operations/0006_s7_closeout_stage_once` | Performance: close-out computes the open run stage once per statement |

Checked on a fresh database: every migration applies; `operations` migrates back to `0003` (unapplying 0006, 0005,
0004) and forward again cleanly; `makemigrations --check` reports no changes.

## 4. Performance

Perf workflow on GitHub runners (scale fixture: 200 robots, 2000 visits, 600 runs, 300 incidents). p95 in ms,
three runs on `9bb6e96` (44680451831 / 44680463053 / 44680471348). The latency budgets are unchanged.

| Path | Budget | p95 (three runs) |
|---|---|---|
| `/api/ops/overview` | 400 | 176 / 165 / 166 |
| `/ops/` | 400 | 234 / 227 / 230 |
| `/api/ops/incidents` | 400 | 36 / 34 / 39 |
| `/api/ops/metrics` | 400 | 108 / 106 / 106 |
| `/ops/metrics/` | 400 | 125 / 107 / 108 |
| `/api/ops/closeout` | 400 | 118 / 117 / 108 |
| `/ops/closeout/` | 400 | 128 / 127 / 132 |
| `/api/ops/exceptions` | 2000 | 617 / 618 / 612 |
| `/api/ops/timeline` | 2000 | 635 / 657 / 662 |
| `/ops/timeline/` | 2000 | 666 / 675 / 692 |

Readiness evaluator: E = 72 statements per evaluated visit (EM ruling, Option B, 2026-11-19), constant in inspection
history (`test_s7_option_b.py`).

The release blockers found and fixed during integration (EM-approved; details in the commit messages):

| Finding | Before | After |
|---|---|---|
| Eligibility view called the periodic plpgsql function once per robot (0004) | overview 703 ms | 66-104 ms (0005) |
| Rule relation read the full robot status view (metrics, count, inbox) | metrics endpoint up to 411 ms | 106-108 ms (B2) |
| Close-out joined the open run stage per run | close-out ~2600 ms | 108-132 ms (0006) |

Each optimization is held to an oracle against the implementation it replaced (`test_s7_perf_oracles.py`): the
0004 view body with the authoritative function, the previous rule relation, and the 0003 close-out view.

The overview endpoints remain slower than Slice 6 (it now renders the production-blocker panel and the periodic
column) but are within budget. The EM accepted this; no further optimization unless a release gate fails.

## 5. Defects found at the closure stage

- **Exceptions inbox pagination never ended** (latent since Slice 6): items with no start time of their own used
  each page's snapshot time and came back on every continuation page. They now keep page 1's time (`c9ac016`;
  regression test `test_continuation_pages_end_when_snapshot_dated_items_tie`).
- **The escalation deadline page could not record, approve or withdraw a set**: it called command names this build
  never had (`8c31801`; `test_deadline_set_lifecycle_through_the_page`). All 11 fleet Slice 7 page commands were
  checked against `fleet.services` (names and keyword arguments): no other mismatch.

## 6. Walkthroughs

### 6.1 Demo (fresh `make reset` + `make seed`, demo mode)

- **Blockers panel:** "Would block in production" lists 8 items (5 codes: escalation deadlines, hold policy, two
  unreleasable holds, inspection templates for P-A and P-B, periodic intervals for P-A and P-B).
- **Periodic states:** R-01 to R-04 "due" (rule v1, labelled a SYNTHETIC demo interval); R-05 "no qualifying
  inspection on this configuration", which also blocks its planning eligibility.
- **Deadline overdue:** a SYNTHETIC deadline set v2 recorded through the deadline page (unclassified: safety_reviewer
  within 1 minute), approved by the safety reviewer and the founder. The inbox then showed P1 `ESCALATION_OVERDUE`
  for incident IC-0001: "no escalation to safety_reviewer recorded; required within 1 min of the report".
- **Close-out for today:** dated by the synthetic zone America/Los_Angeles, states it is not a sign-off, lists no
  outstanding runs.
- **V-0001 still authorizable:** every readiness gate clear; authorization DA-0001 issued (demo, synthetic).

### 6.2 Production mode (scratch database `humanoid_ops_prod_scratch`)

Seeded in demo mode (the seeds refuse to run otherwise), then `demo_mode` set to `false` by the schema owner.

- **Blockers:** the panel is headed "Production blockers", lists all 5 codes, and says what is missing for each.
  The same items are P1 in the exceptions inbox.
- **Authorization refused:** "Departure not authorized: Readiness incomplete": robot R-01 not eligible (synthetic
  capability profile, periodic inspection interval unknown for P-A), readiness policy v1 is synthetic, inspection
  template T1 not approved for pre-departure use in production.
- **Hold release refused:** "Cannot release HD-0001: hold policy v1 is not usable in production (it must be a fully
  approved approved_procedure policy); a re-basing decision to the active approved policy is needed".
- **Direct SQL as `ops_app`:**
  - releasing a hold without the settings lock: "releasing a hold depends on the deployment mode: take
    pg_advisory_xact_lock_shared(64942988919) first";
  - with the lock taken first: refused by the same hold-policy rule;
  - switching `demo_mode` back: permission denied;
  - inserting a departure authorization row: permission denied.
- **State afterwards:** both holds still held, production mode still on, no authorization exists.

## 7. Production prerequisites (company decisions; none supplied by the platform)

| Needed | Owner | Blocker it clears |
|---|---|---|
| Real inspection checklist content, as templates approved for pre-departure and periodic use (E1) | Engineering + safety | `INSPECTION_TEMPLATE_NOT_APPROVED` |
| Periodic inspection interval per configuration, with a procedure reference (E7, B-08) | Engineering + safety | `PERIODIC_INTERVAL_NOT_APPROVED` |
| Hold-release policy from an approved procedure (B-07), and re-basing of existing holds | Engineering lead + safety | `HOLD_POLICY_NOT_APPROVED`, `HOLD_UNRELEASABLE` |
| Escalation deadlines, owner approved, for the active incident policy | Founder + safety | `ESCALATION_DEADLINES_NOT_APPROVED` |
| Readiness policy from an approved procedure | Engineering lead + safety | readiness `POLICY_NOT_APPROVED_PROCEDURE` |
| Engineering-supplied capability profiles | Engineering | planning eligibility |
| `readiness_inspector` qualifications from real training records | Safety | inspection evidence and authorization |
| Close-out time zone, readiness look-ahead and grace thresholds | Ops | `THRESHOLD_NOT_CONFIGURED` |

Out of scope for Slice 7 and deferred by the EM: X-2, X-5, X-6, X-7, X-9. Paging and notifications are not built:
overdue escalations are only shown.
