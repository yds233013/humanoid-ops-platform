# Slice 7 Contract: Production Guardrail Hardening

| | |
|---|---|
| **Status** | DRAFT rev 1 for engineering review. Not frozen until the EM approves it |
| **Date** | 2026-11-19 |
| **Baseline** | `main` `187c19c` (`slice-6-closed` = `cfb8d69` + the approved coverage matrix) |
| **Authority** | `docs/07_playbook_coverage_matrix.md` section 9 (EM decisions, Slice 7 table). Items X-1, X-3, G-01, X-4, X-8, X-10 only |
| **Rules after freeze** | Same as Slices 3-6: names, signatures, lock order and file ownership change only through the integration lead, with the exact reason |

Everything is SYNTHETIC. Slice 7 adds **guardrails that refuse or expose**. It adds no real procedure, interval,
deadline, checklist or approval. Every company input it needs starts out **unknown**. In production an unknown
input blocks (X-1, X-3, G-01) or is shown as unconfigured (X-4, X-10). Demo mode keeps working on inputs that are
clearly labelled synthetic. Where this contract says "the company decides" (B-07, E7, interval values, deadline
values), the platform only records the decision and its approvers, and never supplies the value.

---

## 0. Principles

1. **Recording never depends on approval. Relying on a record does.** An inspection, an incident and its hold, a
   failed evidence row: all of these are always accepted, whatever the template, the policy or the qualification.
   Only the steps that *rely* on a record (passing evidence, authorization, departure, hold release, periodic due
   date) check its provenance. A refused guardrail can never stop a protective hold from being placed.
2. **Provenance is a record, never a string.** A template, an interval rule or a hold policy is usable in
   production only because of append-only approval rows. Those rows are by distinct people holding the right role
   at approval time, and none of them is the author. A free-text `source` field is descriptive only. No production
   gate reads it as proof of approval. It is read only to *refuse* (synthetic content can never be approved).
3. **Production fails closed, demo is labelled.** "Production" means `fleet_setting('demo_mode') <> 'true'`,
   which includes a missing setting (the existing rule, readiness/0003). Every new gate has one definition
   with an explicit `production` flag, the same in Python and SQL. `allow_draft_policies` (or any demo
   switch) never has an effect in production.
4. **Python explains, PostgreSQL decides.** Python gives the explained blocker (evaluator, eligibility, hold status).
   The database re-proves the same rule for direct `ops_app` SQL: in `readiness_issue_authorization`
   (SECURITY DEFINER, the only INSERT path), in insert triggers, and through guard versions that the departure
   trigger compares. Where a rule exists in both, an oracle test proves they agree.
5. **Time is database time.** Every new rule compares against PostgreSQL time. Two existing app-clock timestamps
   that Slice 7 rules depend on are forced to `clock_timestamp()` by their insert triggers: `fleet_inspection.submitted_at` and
   `fleet_hold.placed_at` (section 2.3).
6. **One global lock order.** Slice 7 appends exactly one table to the order (`fleet_holdpolicy`, at the end). Every
   other new decision reuses guard rows that already exist (pattern, robot, person) in their existing positions
   (section 9).
7. **Read models stay read models.** X-4, X-10 and the production-blocker panel are derived at read time inside the
   Slice 5 `REPEATABLE READ READ ONLY` snapshot. They take no locks, store no status, send nothing, and never claim
   a sign-off or a clearance.

## 1. Scope

| In (EM section 9) | Out |
|---|---|
| X-1 (P0) production inspection-template guard: approval records, evaluator + issue function + evidence insert | Real checklist content (E1), real templates, real approvals |
| X-3 (P0) periodic inspection expiry: approved per-configuration interval, qualifying inspection, SQL-authoritative gate, eligibility + command-center visibility | The interval value (E7, B-08). The playbook's illustrative 14 days is never used |
| G-01 (P0) production hold-release policy lifecycle (create, review, approve, withdraw), binding, re-basing of holds placed under unusable policies, incident-hold release, visible production blocker | Severity-based re-slotting (X-2). The content of the incident release rule (B-07) |
| X-4 (P1) overdue escalation visibility, owner-approved deadlines, "unconfigured" exception | Paging, notifications, email, timers, background workers |
| X-8 (P1) `readiness_inspector` qualification, checked at evidence and at authorization, revocation blocks departure | Training content and records (only a reference is stored) |
| X-10 (P1) daily close-out view, snapshot-based, read-only | A close-out or sign-off record (G-12 stays a human procedure) |
| | X-2, X-5, X-6, X-7, X-9 (deferred by the EM) |

## 2. Shared definitions

### 2.1 The approval pattern (used by X-1, X-3, G-01, X-4)

Each new approvable record follows the readiness-policy pattern that already exists (`readiness/0002`,
`readiness_policy_approval_guard`):

- The record is append-only (`reject_mutation`). A change is a new version. `version_no` is consecutive per parent,
  checked by the insert trigger, and unique.
- Approvals are separate append-only rows, unique per `(record, slot)` and unique per `(record, approver)`. The insert
  trigger checks: (a) the approver holds the slot's role **now** (`has_active_role`); (b) the approver is not the
  record's `created_by`; (c) the record is not withdrawn; (d) the record's source is not `synthetic_demo` when the
  slot set is the production one (where a source rule applies, stated per item).
- Withdrawal is a one-to-one append-only row with a non-empty reason.
- **Active** = the highest version that was ever fully approved. If that version is withdrawn, there is none.
  There is never a fallback to an older version. This is the same rule as `readiness_active_policy_id` and
  `execution_active_incident_policy_id`.

### 2.2 Mode

`production := coalesce(fleet_setting('demo_mode'), 'false') <> 'true'`. A command that *decides* on mode takes the
settings advisory lock SHARED (existing key `64942988919`, existing position, `docs/02_lock_graph.md`). Read models
read the setting inside their snapshot.

### 2.3 Forced database times (prerequisite for X-1, X-3, X-8, G-01)

| Column | Today | Slice 7 |
|---|---|---|
| `fleet_inspection.submitted_at` | `auto_now_add` (app clock), not forced. Direct SQL can backdate it or set it in the future | Forced to `clock_timestamp()` in `fleet_inspection_guard`. A periodic due date and the "covered by a qualification" test (X-8) depend on it |
| `fleet_hold.placed_at` | `auto_now_add`, not forced | Forced to `clock_timestamp()` on INSERT in `fleet_hold_guard` (still immutable on UPDATE). The "newer PASS" release rule compares against it |
| `planning_qualification.granted_at` | `auto_now_add` | Forced to `clock_timestamp()` on INSERT (X-8 forbids backdating relative to it) |

---

## 3. X-1 (P0): production inspection-template guard

### 3.1 Problem (code)

- `fleet.models.InspectionTemplate` has `pattern, version, items, source` (default `"synthetic_demo"`). It has no
  author and no approval, and no command creates it. Seeds and test worlds insert it through the ORM
  (`seed_fleet.py`, `tests/fleet_world.py`, `tests/operations_world.py`).
- No gate reads `source`. `fleet_inspection_guard` (fleet/0004) recomputes the result from the template items.
  `readiness_evidence_guard` (readiness/0002) needs a complete PASS for `pass` evidence.
  `readiness_issue_authorization` (readiness/0006) needs the latest evidence per policy kind to pass. None of them
  checks the template. A PASS on the synthetic 12-item template can therefore back a **production**
  authorization (matrix G-03).
- Even a check on `source` would only check a typed string. The EM requires approval records.

### 3.2 Rule

- **R1-1.** A template is *usable for purpose P in mode M* when it is not withdrawn and:
  - **production:** it has an `engineering` approval and a `safety` approval for purpose P (section 2.1);
  - **demo:** the same, **or** its `source = 'synthetic_demo'`.

  The purposes are `pre_departure` and `periodic` (X-3). One template may be approved for both. Each purpose is
  approved separately.
- **R1-2.** In production, pre-departure inspection evidence may rely only on an inspection whose template is
  usable for `pre_departure`. Three places enforce this:
  - the Python evaluator: new blocker `INSPECTION_TEMPLATE_NOT_APPROVED`, level `blocked`;
  - `readiness_issue_authorization`, under the settings lock (authoritative);
  - the evidence insert, for `result = 'pass'` only (section 3.4).
- **R1-3.** A template whose `source = 'synthetic_demo'` can never be approved (refused by the approval trigger).
  A legacy template with no `created_by` can never be approved either.
- **R1-4.** Approving or withdrawing a template bumps the pattern's guard version (`fleet_bump_pattern`). That bump
  invalidates any authorization whose snapshot holds the old version, so a withdrawal between authorization
  and departure blocks the departure (`readiness_guard_problem`, pattern version).
- **R1-5 (EM decision 2).** In production the "newer PASS inspection" release condition of an inspection hold
  (`fleet_hold_release_problem`, `requires_newer_pass_inspection`) counts only a PASS on a template usable for
  `pre_departure` or `periodic`.
- **R1-6 (EM decision 3).** In production the active readiness policy must list `pre_departure_inspection` among
  its evidence kinds. Without it the X-1 guard has nothing to check. Evaluator code
  `POLICY_LACKS_INSPECTION`. The issue function refuses with the same reason.

**Evidence insert decision (asked by the EM): refuse `pass`, accept `fail`.**
- In production, `readiness_evidence_guard` refuses `kind = 'pre_departure_inspection'`, `result = 'pass'` when the
  inspection's template is not usable for `pre_departure`.
- A `fail` evidence row is always accepted. Recording a negative observation is never blocked (principle 1).
- Why also refuse at insert: (a) a production `pass` row on a synthetic checklist is a misleading record even if
  nothing ever relies on it; (b) the operator learns at once, not at authorization; (c) it removes a direct-SQL
  path that would leave such rows behind.
- Why it is not enough on its own: the evidence insert does not take the settings lock. A demo-to-production
  switch, or a template withdrawal, can commit after the insert. The issue function re-proves R1-2 under the
  settings lock and the pattern share lock, and remains the authoritative boundary.

### 3.3 Data model

| Table / column | Definition |
|---|---|
| `fleet_inspectiontemplate.created_by_id` | New, nullable FK user. NULL only for rows created before Slice 7. The insert trigger requires it for new rows |
| `fleet_inspectiontemplate.procedure_ref` | New text, default `''`. Required non-blank when `source = 'engineering_doc'` (a document reference, never content) |
| `fleet_inspectiontemplate.source` | CHECK `source IN ('synthetic_demo', 'engineering_doc')`. Descriptive only (principle 2) |
| `fleet_inspectiontemplateapproval` | `template`, `purpose` ∈ {`pre_departure`, `periodic`}, `slot` ∈ {`engineering`, `safety`}, `approver`, `approved_at` (forced DB time). UNIQUE `(template, purpose, slot)`, UNIQUE `(template, purpose, approver)`. Append-only |
| `fleet_inspectiontemplatewithdrawal` | One-to-one `template`, `withdrawn_by`, `reason` (non-blank), `withdrawn_at`. A withdrawal covers every purpose. Append-only |

### 3.4 Database enforcement (Engineer A, `fleet/0008_s7_templates_rules_policies`, `readiness/0007_s7_guardrails`)

| Object | Kind | Rule |
|---|---|---|
| `fleet_template_guard()` | BEFORE INSERT on `fleet_inspectiontemplate` | items: a non-empty array, unique non-blank `id`, non-blank `label`, boolean `robot_condition`. `created_by_id` not null and holds `engineer` or `engineering_lead`. `engineering_doc` needs a `procedure_ref` |
| `fleet_template_approval_guard()` | BEFORE INSERT on the approval table | Section 2.1 (a)-(c). Slot role via `fleet_slot_role(slot)`. `source = 'engineering_doc'` and `created_by_id IS NOT NULL` (R1-3). Then `fleet_bump_pattern(pattern)` (R1-4) |
| `fleet_template_withdrawal_guard()` | BEFORE INSERT on the withdrawal table | Withdrawer holds `engineering_lead` or `safety_reviewer`. Bump the pattern |
| `fleet_template_usable(p_template bigint, p_purpose text, p_production boolean) RETURNS boolean` | STABLE | R1-1 exactly |
| `readiness_evidence_guard()` (replaced) | trigger | Adds the R1-2 insert refusal (`pass` only, production only) and the X-8 checks (section 7) |
| `readiness_issue_authorization()` (replaced, same signature, grants and SECURITY DEFINER) | function | In the evidence loop, for `pre_departure_inspection`: refuse `'authorization refused: inspection %s uses template %s, which is not approved for pre-departure use in production'` when `NOT fleet_template_usable(i.template_id, 'pre_departure', expected_mode = 'production')`. Add R1-6 |
| `fleet_hold_release_problem()` (replaced, section 5) | function | R1-5 inside the newer-PASS clause |

### 3.5 App changes (Engineer B)

- `fleet.services`:
  - `template_usable(template, purpose, *, production) -> bool`: the Python mirror of the SQL function, with
    an oracle test.
  - `create_inspection_template(actor, *, pattern_code, version, items, source, procedure_ref)`. Permission
    `fleet.manage_inspection_template` (engineer, engineering_lead). Locks pattern S.
  - `approve_inspection_template(actor, *, template_id, purpose, slot)` and
    `withdraw_inspection_template(actor, *, template_id, reason)`. Permissions
    `fleet.approve_inspection_template` and `fleet.withdraw_inspection_template` (engineering_lead,
    safety_reviewer). Both lock pattern U and every robot on the pattern U, the same as `decide_pattern`.
  - `submit_inspection` is unchanged. It never refuses on template grounds (principle 1).
- `readiness.evidence.record_evidence`: the same refusal as the trigger, as an explained `Conflict`.
- `readiness.evaluator`:
  - new codes `INSPECTION_TEMPLATE_NOT_APPROVED` and `POLICY_LACKS_INSPECTION`, added to `BLOCKER_CODES`;
  - new gate 5b after `_gate_evidence`. It reads the latest pre-departure evidence's inspection template and
    takes the mode from `fleet_services.demo_mode()`.

### 3.6 UI / read models (Engineer C)

- The fleet pattern page lists its templates with a usability state per purpose: "approved", "synthetic, demo
  only", "awaiting approval", "withdrawn". It also offers the create, approve and withdraw forms.
- The inspection form shows the chosen template's state. In production, an unusable template shows "This
  inspection will be recorded, but it cannot be used as passing pre-departure evidence."
- Production-blocker panel (section 5.6): `INSPECTION_TEMPLATE_NOT_APPROVED` for each pattern in use by a non-retired
  robot that has no template usable for `pre_departure` in production.

### 3.7 Inputs

The template content and its `procedure_ref` come from engineering and safety (E1). None exists, so production
stays blocked. That is correct.

### 3.8 Migration and backfill

- Existing templates get `created_by = NULL` and stay `synthetic_demo`. They remain usable in demo and can never be
  approved.
- No approval rows are created by any migration or seed. The demo keeps working through the synthetic branch of
  R1-1.

### 3.9 Acceptance tests (in addition to section 11)

| Area | Tests |
|---|---|
| Unit | R1-1 truth table: purpose × mode × {synthetic, unapproved doc, one slot, both slots, withdrawn} |
| Approval trigger refusals | Author approves; same person in both slots; role not held; approving a synthetic template; approving a legacy template; approving after withdrawal |
| Evaluator | Production blocks with `INSPECTION_TEMPLATE_NOT_APPROVED` on a synthetic PASS, demo does not; `POLICY_LACKS_INSPECTION` |
| Direct `ops_app` SQL | Call `readiness_issue_authorization` with synthetic-template evidence in production: refused. Insert `pass` evidence in production: refused. Insert `fail` evidence: accepted |
| Races, both orders | Template withdrawal vs authorize (pattern U vs S). Withdrawal after authorization: departure refused (pattern version). Demo-to-production switch after demo evidence: production authorization refused |
| Release | R1-5 newer-PASS on a synthetic template: production refuses, demo allows |

---

## 4. X-3 (P0): periodic inspection expiry

### 4.1 Problem (code)

- Nothing in the platform models a periodic inspection, an interval or a due date (matrix P-99, G-02). An overdue
  robot is eligible (`fleet.services.evaluate_eligibility`) and can be authorized (`readiness_issue_authorization`).
- `Inspection` has no kind. Pre-departure and post-visit inspections are told apart only by `visit_ref` and by
  which record refers to them.

### 4.2 Rule

- **Interval rule.** It is per configuration **pattern**, versioned and approved (section 2.1): one
  `PeriodicInspectionRule` with `interval_min`, where NULL means unknown.
  - Production uses it only when it is active, `source = 'engineering_doc'` and `interval_min IS NOT NULL`.
  - Demo also accepts an active `synthetic_demo` rule.
  - In both modes, a missing rule or a NULL interval is **unknown** and blocks. Demo works only because the seed
    supplies a clearly synthetic rule (section 4.8).
- **Qualifying inspection** for robot R. All of these must hold:
  1. `i.robot_id = R` and `i.manifest_id = R.current_manifest_id`. The robot's current configuration: a new
     manifest restarts the clock with nothing qualifying.
  2. `i.result = 'PASS' AND i.complete`.
  3. `fleet_template_usable(i.template_id, 'periodic', production)`. Only templates approved for the `periodic`
     purpose count. A pre-departure or post-visit inspection counts only if its template is also approved for
     `periodic`. That choice is engineering's, recorded in the approval data, not the platform's.
  4. Both signers are qualified readiness inspectors for that inspection (X-8, `planning_inspection_signer_problem`
     returns NULL). If a signer's qualification is later revoked, the inspection stops qualifying.
- **Due date.** `due_at = max(qualifying i.submitted_at) + interval_min`, using the rule that is active **now** for
  the robot's **current** pattern. A newer approved interval changes `due_at` for every robot on the pattern. A
  later FAIL does not move `due_at`: a robot-condition FAIL already holds the robot (fleet/0004), and a FAIL on a
  visit item does not.
- **States:** `unknown_interval`, `never_inspected` (no qualifying inspection on the current manifest),
  `overdue` (`at >= due_at`), `due` (otherwise).
- **Authorization gate (authoritative, PostgreSQL; EM decision 6).** The issue function refuses unless the state is
  `due` and `due_at >= unload_end`, where `unload_end` is the planned unloading end computed in readiness/0006 (A).
  The robot must stay within its interval for the whole activity, as crew qualifications must (`readiness_qualified_window`).
  It also bounds `expires_at <= due_at` (defence in depth).
- **Eligibility (EM decision 7).** `evaluate_eligibility` adds reasons: `Periodic inspection interval unknown for
  configuration P`, `No qualifying periodic inspection on the current configuration`, `Periodic inspection overdue
  since <due_at>`. Overdue is evaluated at `at`. Eligibility therefore becomes time-dependent:
  `evaluate_eligibility(robot, at=None)`, where None means database time. Planning, which calls eligibility,
  inherits the block.
- **Evaluator.** Codes `PERIODIC_INSPECTION_INTERVAL_UNKNOWN`, `PERIODIC_INSPECTION_MISSING` and
  `PERIODIC_INSPECTION_DUE_BEFORE_RETURN` (`due_at < unload_end`). The overdue state already arrives through
  `PLANNING_*` from eligibility, and the gate does not repeat it.
- **Departure.** It needs no new trigger clause. `expires_at <= due_at`, together with the existing
  `clock_timestamp() >= a.expires_at` refusal, covers time. Rule approvals and withdrawals bump the pattern
  version, which covers rule changes. A new inspection only moves `due_at` later.

### 4.3 Data model

| Table | Definition |
|---|---|
| `fleet_periodicinspectionrule` | `pattern`, `version_no` (consecutive per pattern), `interval_min` int NULL (CHECK NULL or 1..5,256,000), `source` ∈ {`synthetic_demo`, `engineering_doc`}, `procedure_ref` (required for `engineering_doc`), `notes`, `created_by`, `created_at` (DB time). UNIQUE `(pattern, version_no)`. Append-only |
| `fleet_periodicinspectionruleapproval` | `rule`, `slot` ∈ {`engineering`, `safety`}, `approver`, `approved_at`. UNIQUE slot, UNIQUE approver. Append-only |
| `fleet_periodicinspectionrulewithdrawal` | One-to-one `rule`, `withdrawn_by`, `reason` |

A synthetic rule can be approved, but only for demo use. A production gate never accepts `synthetic_demo`. This
mirrors readiness policies, where demo approvals by dev users exist and production refuses the source.

### 4.4 Database enforcement

| Object | Migration | Rule |
|---|---|---|
| `fleet_periodic_rule_guard()` | fleet/0008 | Consecutive version, author holds engineer or engineering_lead, source/ref consistency |
| `fleet_periodic_rule_approval_guard()`, `..._withdrawal_guard()` | fleet/0008 | Section 2.1. Bump the pattern |
| `fleet_active_periodic_rule_id(p_pattern bigint) RETURNS bigint` | fleet/0008 | Section 2.1 "active" |
| `fleet_inspection_guard()` (replaced) | fleet/0008 | Forces `submitted_at := clock_timestamp()`. Otherwise unchanged |
| `readiness_periodic_inspection_status(p_robot bigint, p_production boolean, p_at timestamptz) RETURNS TABLE (state text, due_at timestamptz, inspection_id bigint, rule_id bigint)` | readiness/0007 (needs the planning qualification helper) | Section 4.2 exactly |
| `readiness_issue_authorization()` (replaced) | readiness/0007 | After the crew block (D): refuse `'authorization refused: periodic inspection %s'` (interval unknown / none on the current configuration / overdue / due at %s, before the planned unloading end %s). Then `bound := least(bound, due_at)` |
| `operations_robot_eligibility` (replaced) | operations/0003 | Adds the three reasons in the Python order, evaluated at `transaction_timestamp()` (= the snapshot `at`) |

### 4.5 App changes

- `fleet.services`:
  - `periodic_inspection_status(robot, at, *, production) -> PeriodicStatus`. An independent Python
    implementation, with an oracle test against the SQL function.
  - `evaluate_eligibility(robot, at=None)` gains the reasons.
  - `create_periodic_rule`, `approve_periodic_rule`, `withdraw_periodic_rule`. Permissions
    `fleet.manage_periodic_rule` (engineer, engineering_lead), `fleet.approve_periodic_rule` (engineering_lead,
    safety_reviewer) and `fleet.withdraw_periodic_rule` (engineering_lead, safety_reviewer). They lock pattern U
    + robots U.
- `readiness.evaluator`: gate 1c, `_gate_periodic_inspection`, using the plan timeline from `live_feasibility`.
- `planning.services`: callers pass `at` explicitly where they already have one.

### 4.6 UI / read models

- The robot page shows "Periodic inspection: due <time> (rule vN, synthetic|approved)" / "overdue since" / "interval
  unknown" / "no qualifying inspection on this configuration". The pattern page shows the rule history and approval
  forms.
- Fleet overview: a new field `periodic_inspection` (`state`, `due_at`).
- New exceptions (Slice 5 conventions; `since` and the inclusive boundary rule are as in contract 05 section 3):

| code | fires when | since | owner | priority | entity_ref |
|---|---|---|---|---|---|
| `PERIODIC_INSPECTION_OVERDUE` | Non-retired robot, state `overdue` or `never_inspected` | `due_at` (or the snapshot time for never) | engineering_lead | 2 | robot code |
| `PERIODIC_INSPECTION_DUE_SOON` | State `due` and `at >= due_at - ops_periodic_due_lookahead_min` | `due_at - lookahead` | engineering_lead | 3 | robot code |
| `PERIODIC_INTERVAL_UNCONFIGURED` | A pattern in use has no rule usable in the current mode | the snapshot time | engineering_lead | 1 | pattern code |

  `ops_periodic_due_lookahead_min` is a new threshold key handled by the existing `THRESHOLD_NOT_CONFIGURED`
  mechanism. It is a visibility setting only and never a gate.

### 4.7 Inputs

The interval comes from engineering (E7, B-08), per pattern, with a procedure reference. Unknown blocks production
authorization and eligibility. The playbook's 14 days is illustrative and is not used anywhere: not in a seed, a
test, a default or the UI.

### 4.8 Migration and backfill

- The migration creates no rule. Every robot becomes `unknown_interval`, which is fail-closed in both modes until a
  rule exists.
- The `seed_guardrails` command (section 10) runs only in demo mode. It creates one `synthetic_demo` rule per
  seeded pattern with `interval_min = 4320`. That is a deliberately arbitrary SYNTHETIC value, shown in the UI as
  "SYNTHETIC demo interval, not an engineering value". The rule is approved by `dev_engineering_lead` and
  `dev_safety_reviewer`, with `dev_engineer` as author.
- The command then submits one fresh synthetic periodic inspection for each in-service seeded robot, through
  `submit_inspection`, signed by the dev users that the same command qualifies (X-8). Nothing is backdated.

### 4.9 Tests

| Area | Tests |
|---|---|
| States | Each state; manifest change resets; a newer approved rule changes `due_at`; withdrawn active rule means unknown (no fallback); synthetic rule refused in production; boundary at `due_at` −1 µs / = / + |
| Authorization window | `due_at` between now and `unload_end` refused; `expires_at` bounded by `due_at` |
| Direct `ops_app` SQL | Issue function refusals for each state. Inspection inserted with a future `submitted_at` gets DB time |
| Races, both orders | Rule withdrawal vs authorize. Approval of a shorter interval vs authorize. New inspection vs authorize |
| Oracles | Python/SQL oracle on ≥ 50 random worlds plus the edge-case table. Extended eligibility oracle (Slice 5 6.1) including the time argument |

---

## 5. G-01 (P0): incident hold release policy

### 5.1 Problem (code)

- `HoldPolicy` has `version, rules, created_by`, two approval slots (engineering, safety) and **no source, no
  withdrawal and no author-independence check**. `approve_hold_policy` and `fleet_policy_approval_guard` check the
  role only, so the author can fill one slot.
- `fleet_policy_usable(pid)` (fleet/0002) returns true when two slots are filled **or**
  `allow_draft_policies = 'true'`. It never looks at `demo_mode`, so a production deployment with that switch
  left on would release under drafts.
- `_create_hold` (fleet/services.py) binds a new hold to the **newest** policy, approved or not. A newer draft
  therefore captures all new holds, which are unreleasable in production even while an older approved
  policy exists. No database rule says which policy a hold must bind to: direct SQL can pick any policy with a
  rule for the source.
- The only policy is the seeded DRAFT (`seed_fleet.DRAFT_POLICY_RULES`, 4 slots for every incident). In production
  every incident hold is unreleasable and there is no path out (matrix G-01). Required slots are fixed at
  placement (`fleet_hold_guard`), and that stays (EM: no X-2).

### 5.2 Rules

**Lifecycle (create, review, activate, withdraw)**

| Step | Who | Rule |
|---|---|---|
| Create version N+1 | `fleet.manage_hold_policy`: engineer, engineering_lead (EM decision 10) | `source` ∈ {`synthetic_demo`, `approved_procedure`}. `approved_procedure` needs a `procedure_ref` and must pass the **floor** below. Consecutive version. Immutable |
| Review / approve | Slots `engineering` (engineering_lead), `safety` (safety_reviewer), and for `approved_procedure` also `founder` (founder) | Section 2.1: distinct people, none of them the author, role held now. A `synthetic_demo` policy needs engineering + safety only |
| Active | n/a | Highest version fully approved (2 slots synthetic, 3 slots approved_procedure), and none if that version is withdrawn |
| Withdraw | engineering_lead, safety_reviewer (`fleet.withdraw_hold_policy`) | Reason required. Immutable |

**Floor for an `approved_procedure` policy** (platform integrity, not company content; DB-enforced at insert):
- one rule for **every** `Hold.SOURCES` value, so no source can become unreleasable by omission;
- every rule has ≥ 1 slot, distinct slot names, and roles ⊆ the roles of `fleet.approve_release`;
- the `incident` rule has `distinct_approvers = true` and `independent_of_placer = true`.

Which roles fill which incident slots is **B-07 and stays open** (section 5.7).

**Usability (one definition, mode-aware):**
```
fleet_policy_usable(pid) :=
  pid IS NOT NULL AND NOT withdrawn(pid) AND (
      (approved(pid) AND (source = 'approved_procedure' OR demo_mode))
   OR (demo_mode AND allow_draft_policies) )
```
`allow_draft_policies` has no effect outside demo mode (EM decision 14). This is a behaviour change, tested.

**Binding at placement (fixed for life, DB-enforced).** A new hold binds to
`fleet_hold_binding_policy_id()`, which returns:
- the active policy; else,
- in demo with `allow_draft_policies`, the newest policy (today's demo behaviour); else
- NULL (no slots, unreleasable).

`required_slots` is copied from that policy's rule for the source. If the rule is missing, `policy_id` is NULL and
`[]`. `fleet_hold_guard` refuses any other `policy_id`.

**Holds placed under older, draft, withdrawn, synthetic or no policy (EM decision 11).**
- A hold whose bound policy (or effective re-basing) is not production-usable stays **unreleasable in production**.
- Its only way out is an explicit, append-only **re-basing decision**: a `HoldRebase` naming the hold, the
  from-policy, the to-policy and a reason, effective when two distinct people approve it (engineering_lead and
  safety_reviewer), neither of whom placed the hold. Its rules:
  - the to-policy must be the **active** policy, production-usable (`approved_procedure`, all three slots, not
    withdrawn);
  - the slots are copied **exactly** from the to-policy's rule for the hold's source;
  - allowed only while the hold is unreleased and its current effective policy is not production-usable, so a
    hold already under an approved production policy can never be "shopped" to another policy;
  - release approvals given before the rebase do not count. `fleet_releaseapproval.rebase_id` is forced by the
    trigger to the hold's current effective rebase, and only approvals with that `rebase_id` fill slots.
- Holds under an older policy that is still approved and production-usable remain releasable under **their own**
  rules (fixed at placement). They are not re-based.
- Why: automatic re-binding would silently change requirements fixed at placement (forbidden). Leaving such holds
  unreleasable for ever would force retiring robots over a paperwork gap. A named two-person decision keeps the
  fixed-at-placement record intact (the hold row never changes) and leaves an audit trail.

**Incident holds under an approved policy (release).** Release needs all of the following:
- every effective slot filled by a valid approval on the robot's current manifest (existing);
- distinct approvers, independent of the placer (floor);
- the newer-PASS clause where the rule asks for it (R1-5 applies);
- **(EM decision 12)** if an `execution_incident` references the hold, its status is `resolved` or `closed`;
- in production, a hold with `source_type = 'incident'` and no Incident row is unreleasable. In demo, the seeded
  R-03 hold has no Incident row and keeps working.

Closing an incident still never releases (Slice 4 principle 3). `fleet.place_hold` refuses `source_type = 'incident'`
in Python from now on: incidents come only from `report_incident`.

**Visible production blocker, with no demo bypass.**
- `HOLD_POLICY_NOT_APPROVED`: no production-usable active policy, or the active one lacks an incident rule.
- `HOLD_UNRELEASABLE`: one item per unreleased hold whose effective policy is not production-usable.

Both appear in the production-blocker panel in **both** modes. In demo they are labelled "would block release in
production". In production they are also exception-inbox items (priority 1). No setting hides them.

### 5.3 Data model

| Table / column | Definition |
|---|---|
| `fleet_holdpolicy.source` | New, CHECK ∈ {`synthetic_demo`, `approved_procedure`}. Existing rows are backfilled to `synthetic_demo`, which is true: no real policy exists |
| `fleet_holdpolicy.procedure_ref` | New text, default `''` |
| `fleet_holdpolicyapproval.slot` | Adds `founder` |
| `fleet_holdpolicywithdrawal` | One-to-one `policy`, `withdrawn_by`, `reason`, `withdrawn_at` |
| `fleet_holdrebase` | `hold`, `from_policy` (nullable), `to_policy`, `required_slots` jsonb, `reason`, `requested_by`, `requested_at` (DB time). Append-only |
| `fleet_holdrebaseapproval` | `rebase`, `slot` ∈ {`engineering`, `safety`}, `approver`, `approved_at`. UNIQUE slot, UNIQUE approver |
| `fleet_releaseapproval.rebase_id` | New nullable FK, forced by the trigger |

### 5.4 Database enforcement

| Object | Migration | Rule |
|---|---|---|
| `fleet_hold_policy_guard()` (new, BEFORE INSERT) | fleet/0008 | Consecutive version, author role, source/ref, floor for `approved_procedure` |
| `fleet_hold_policy_slot_role(slot)` | fleet/0008 | engineering → engineering_lead, safety → safety_reviewer, founder → founder. Kept separate from `fleet_slot_role`, so capability approvals never accept a `founder` slot (that would break the `{engineering, safety}` equality in `approved_profile`) |
| `fleet_policy_approval_guard()` (replaced) | fleet/0008 | Section 2.1. `founder` slot only on `approved_procedure`. Locks the policy row `FOR UPDATE` |
| `fleet_hold_policy_withdrawal_guard()` | fleet/0008 | Role. Locks the policy U |
| `fleet_policy_usable(pid)` (replaced), `fleet_active_hold_policy_id()`, `fleet_hold_binding_policy_id()` | fleet/0008 | Section 5.2 |
| `fleet_hold_guard()` (replaced) | fleet/0008 | INSERT: forces `placed_at`, `policy_id = fleet_hold_binding_policy_id()` (or NULL when that policy has no rule for the source), slots copied exactly. Takes the bound policy `FOR SHARE`. UPDATE: unchanged plus the new release problem |
| `fleet_hold_effective_rule(h fleet_hold) RETURNS TABLE (policy_id, rebase_id, slots, rule)` | fleet/0008 | The latest effective rebase, else the placement values |
| `fleet_hold_source_problem(h fleet_hold) RETURNS text` | fleet/0008 stub (NULL). Replaced in execution/0005 | Incident-status clause (EM decision 12). Cross-app SQL lives in the execution migration, as C1/C3 did |
| `fleet_hold_release_problem(h)` (replaced) | fleet/0008 | Uses the effective rule, the mode-aware usability, approvals with the current `rebase_id`, R1-5, and the source problem |
| `fleet_release_approval_guard()` (replaced) | fleet/0008 | Validates against the effective slots. Forces `rebase_id` |
| `fleet_hold_rebase_guard()`, `fleet_hold_rebase_approval_guard()` | fleet/0008 | Section 5.2 rebase rules. Lock the robot (or pattern + robots) U, then the to-policy S |

### 5.5 App changes

- `fleet.services`:
  - `_create_hold` binds through the SQL binding rule (Python mirror `binding_policy()`) and locks the candidate
    policy S last (section 9);
  - `create_hold_policy(actor, *, rules, source, procedure_ref)`, `approve_hold_policy` with the author check and
    the `founder` slot, and new `withdraw_hold_policy`;
  - `request_hold_rebase(actor, *, hold_code, reason)` and `approve_hold_rebase(actor, *, rebase_id, slot)`.
    Permissions `fleet.rebase_hold` (engineering_lead, safety_reviewer);
  - `hold_release_status` reports the effective rule, its origin (placement / rebase), production usability
    and the incident-status condition;
  - `place_hold` refuses `incident`.
- `accounts/roles.py` (lead): the permissions above.

### 5.6 UI / read models

- Hold page: "Release requirements fixed at placement under policy vN (synthetic draft | synthetic approved |
  approved procedure | withdrawn | none)". In production: "Releasable when …" or "Unreleasable in production: needs a
  re-basing decision to the active approved policy". The rebase request and approval forms explain the
  two-person rule.
- Policy pages: list, versions, slots, approvals, withdrawal.
- `operations.readmodels.production_blockers(at)`, one statement over the view `operations_production_blockers`:
  `HOLD_POLICY_NOT_APPROVED`, `HOLD_UNRELEASABLE`, `INSPECTION_TEMPLATE_NOT_APPROVED` (X-1),
  `PERIODIC_INTERVAL_NOT_APPROVED` (X-3, evaluated as production), `ESCALATION_DEADLINES_NOT_APPROVED` (X-4).
  The panel is shown on the overview in both modes. In production the items also join the exception inbox.

### 5.7 Open company decision (B-07)

Who must approve the release of an incident hold is a company decision: the content of the `incident` rule (slots and
roles), and whether it depends on severity. The playbook G5 says engineering for S2/S3, and engineering + safety +
founder for S0/S1. The platform keeps fixed-at-placement slots and does **not** implement severity-based slots
(X-2 deferred). The company therefore has to approve **one** rule that is acceptable for every incident hold, or
ask the EM to reopen X-2. Until an `approved_procedure` policy with an incident rule is approved by engineering,
safety and the founder, every production incident hold stays unreleasable, and the platform says so in the
production-blocker panel. Slice 7 ships no such policy.

### 5.8 Migration and backfill

- `source` is backfilled to `synthetic_demo`. Existing holds keep their `policy_id` and slots unchanged, and
  `placed_at` values are not rewritten.
- Demo: the seeded DRAFT stays a draft. `allow_draft_policies` and `demo_mode` keep R-03 releasable after the founder
  approves (unchanged).
- Production: every existing hold is under a synthetic policy, so `HOLD_UNRELEASABLE` (fail closed).
- Tests that set `allow_draft_policies = true` with `demo_mode` false, or that expect binding to the newest draft,
  are changed with the reason "G-01 behaviour change" (lead, phase 0).

### 5.9 Tests

| Area | Tests |
|---|---|
| Lifecycle | Author self-approval refused; founder slot required for `approved_procedure`; floor violations refused at insert; withdrawal leaves no active policy and no fallback |
| Binding | Draft newer than approved: binds approved. Direct SQL binding to another policy: refused |
| `allow_draft_policies` | Ignored in production |
| Rebase | Allowed only from unusable to the active production policy; placer excluded; two distinct people; old approvals do not count; rebase of a hold already under a usable policy refused |
| Incident release | Refused while `reported`/`triaged`/`response_in_progress`; allowed at `resolved`; no Incident row in production refused |
| Direct `ops_app` SQL | Each refusal |
| Races, both orders | Policy withdrawal vs release; withdrawal vs placement (binding re-read after the lock); rebase approval vs release; status change vs release |
| Oracle | Python/SQL oracle of `hold_release_status` vs `fleet_hold_release_problem` |

---

## 6. X-4 (P1): overdue escalation visibility

### 6.1 Problem (code)

`IncidentEscalation` and `EscalationAck` (execution/models.py) record the target role, the action and the ack. There is
no deadline anywhere. Slice 5 shows `ESCALATION_UNACKNOWLEDGED` and `INCIDENT_UNTRIAGED`, but not lateness, and a missing
escalation is invisible until closure (`execution_status_change_guard`) (matrix P-82, G-04).

### 6.2 Rule

- **Deadline set (owner-approved input, EM decision 16).** It is bound to one incident-policy version. `deadlines`
  is a JSON object whose keys are exactly that policy's `severity_scale` levels plus `"unclassified"`. Each value is
  a list of `{"to_role": <known role>, "within_min": 1..5,256,000}` with distinct roles.
  - An **empty list means "no clocked notification at this level"**. That is a stated decision, not missing data.
  - `source` is `synthetic_demo` or `owner_approved`.
  - Approvals: slots `safety` (safety_reviewer) and `founder` (founder), section 2.1.
  - *Usable* = active for the **active** incident policy (`execution_active_incident_policy_id()`), and in production
    `source = 'owner_approved'`.
- **Level of an incident:** the latest `SeverityDecision.level` when that decision's policy is the set's policy.
  `unclassified` when the incident has no decision. If the latest decision was made under another policy version,
  the incident is **unconfigured**.
- **Basis time** (EM decision 17): `incident.reported_at` (DB time, the moment the platform knew).
  `occurred_at` is shown but not used. A severity raised later keeps the original basis, so an escalation that
  becomes required late is immediately overdue. That is intentional: the clock started at the report.
- **Fires `ESCALATION_OVERDUE`** for an incident that is not closed and each entry `(R, N)` of its level when
  `at >= reported_at + N` and no `EscalationAck` exists on any escalation of this incident with `to_role = R`. The
  message says which: "no escalation to R recorded" or "escalated to R at T, not acknowledged". An ack after the
  deadline clears the item. Lateness that has been resolved is history (visible in the dossier), not an open
  exception.
- **Fires `ESCALATION_DEADLINE_NOT_CONFIGURED`:**
  - once per snapshot when no usable deadline set exists (`entity_ref = 'deadline-set'`);
  - once per open incident whose level is unconfigured (`entity_ref` = the incident code).
- No paging, notification, email or timer of any kind. The existing `ESCALATION_UNACKNOWLEDGED` is unchanged.

| code | since | owner | priority | entity_ref |
|---|---|---|---|---|
| `ESCALATION_OVERDUE` | `reported_at + N` | R | 1 | `<incident code>/<role>` |
| `ESCALATION_DEADLINE_NOT_CONFIGURED` | the snapshot time | safety_reviewer | 1 | `deadline-set` or the incident code |

### 6.3 Data model and enforcement

- New `execution_escalationdeadlineset` (`incident_policy`, `version_no`, `deadlines`, `source`, `notes`,
  `created_by`, `created_at`), `..._approval`, `..._withdrawal`. Append-only.
- Triggers (execution/0005): `execution_deadline_set_guard()` validates `deadlines` against the bound policy's scale
  (`execution_deadline_rules_problem(policy_id, j)`); the approval and withdrawal guards follow section 2.1.
- Function `execution_active_deadline_set_id()`.
- Authors: `execution.manage_escalation_deadlines` (ops_lead, safety_reviewer). Approvers:
  `execution.approve_escalation_deadlines` (safety_reviewer, founder). Withdraw: same roles.
- The approval commands take **no guard row**: uniqueness constraints serialize concurrent approvals, and nothing
  else depends on these rows transactionally (read-only use). They take the settings lock S only for the
  production source check, as C4 does for incident policies.

### 6.4 Read model and UI

- The rules join `RULES_RELATION_SQL` (one statement, in the existing statement budget) in `operations/exceptions.py`.
- Python reference implementation `operations.exceptions.reference_escalation_overdue(at)`, used only by the oracle
  test.
- The incident page shows each required `(role, deadline)` with its state. A new page `/execution/escalation-deadlines/`
  shows the sets and approvals.

### 6.5 Inputs, migration, tests

- **Inputs.** The deadline values belong to the founder and safety (playbook D-21). None exist. Demo:
  `seed_guardrails` creates a `synthetic_demo` set for the synthetic incident policy with obviously synthetic
  values, labelled. Production: `ESCALATION_DEADLINE_NOT_CONFIGURED` until an `owner_approved` set exists.
- **Tests:**
  - firing and non-firing at −1 µs / = / +;
  - ack before vs after the deadline; escalation to the wrong role; closed incident excluded;
  - unclassified uses the `"unclassified"` key; decision under an old policy is unconfigured; empty list never
    fires;
  - set validation refusals; synthetic set in production is unconfigured;
  - oracle SQL vs Python reference on random worlds; determinism and uniqueness of `(code, entity_ref)`;
  - a test that no notification code path exists (no outbox row, no mail).

---

## 7. X-8 (P1): readiness-checker qualification

### 7.1 Problem (code)

- `submit_inspection` accepts any signer with an active `ops_lead` or `field_operator` role
  (`accounts.roles.INSPECTION_SIGNER_ROLES`), checked in Python only (matrix D-23, G-10).
- `planning.Qualification.KINDS` has no checker kind, there is no DB CHECK on `kind`, and any `planning.manage_people`
  holder (ops_lead) grants anything.
- **Does the existing person-version guard cover a signer revocation? Verified: no.** `readiness_guard_problem`
  (readiness/0003) compares `person_versions`, but the evaluator fills it only with the revision's lead and
  second and the recovery plan's people (`evaluator._read_world`). Inspection signers are `User`s
  (`Inspection.performer/verifier`) who need not be crew. Revoking a signer's qualification today bumps a person row
  that no authorization snapshot contains, so it would neither invalidate the authorization nor block departure.

### 7.2 Rule

- **New qualification kind `readiness_inspector`.** It expires like every qualification (`valid_from`, `valid_to`).
  - **Grant (EM decision 18):** by a `safety_reviewer` (permission `planning.grant_inspector_qualification`),
    never to themselves (`granted_by ≠ person.user`).
  - No backdating: `valid_from >= granted_at`, with `granted_at` forced to DB time.
  - A non-blank `basis_ref` (training record reference) is required.
  - Revocation: ops_lead, safety_reviewer or engineering_lead. Revoking is the safe direction.
- **Covered signer (EM decision 19).** A signer of inspection *i* is covered when their user has a `Person` and one
  `readiness_inspector` grant row with `valid_from <= i.submitted_at <= valid_to` and `revoked_at IS NULL` at check
  time. Expiry *after* the inspection does not invalidate work done while qualified. Revocation does.
- **Checked when pre-departure evidence is recorded.** `readiness_evidence_guard`, for `kind =
  'pre_departure_inspection'` and `result = 'pass'`, refuses unless the performer and the verifier are both covered.
  This applies in both modes (demo users are given synthetic grants). `fail` evidence is always accepted.
- **Checked when authorization is issued.** `readiness_issue_authorization` re-checks the covered-signer rule for
  the relied pre-departure evidence (`'authorization refused: inspector %s is not a qualified readiness inspector
  for inspection %s'`).
- **Revocation blocks departure through the existing person-version guard, once it is extended:**
  - the evaluator adds the signer persons of the latest pre-departure evidence's inspection to `person_versions`
    (the snapshot keys are unchanged; only the dict grows);
  - the issue function, the authorization guard and the departure guard refuse when a signer person of a relied
    inspection is missing from `person_versions` (new `readiness_signer_versions_problem(p_snapshot,
    p_evidence_ids)`, called next to `readiness_guard_problem`);
  - `revoke_qualification` bumps `planning_person.version` (existing `qualification_bump`). An authorization issued
    earlier then fails `readiness_guard_problem` at departure ("person P version …, qualification changed");
  - a revocation committed before authorization is refused by the re-check above.
- X-3 qualifying inspections use the same covered-signer rule.
- The role check in `submit_inspection` stays (app). An uncovered signer does not stop an inspection from being
  recorded or its hold from being placed (principle 1). The form warns.

### 7.3 Data model and enforcement

| Object | Migration | Rule |
|---|---|---|
| `planning_qualification.kind` | planning/0006 | CHECK ∈ {`field_lead`, `field_operator`, `robot_handler`, `readiness_inspector`} |
| `planning_qualification.basis_ref` | planning/0006 | New text, default `''` |
| `planning_qualification_insert_guard()` (new BEFORE INSERT) | planning/0006 | Forces `granted_at`. For `readiness_inspector`: grantor role, no self-grant, `valid_from >= granted_at`, `basis_ref` non-blank. Other kinds unchanged |
| `planning_inspection_signer_problem(p_inspection bigint) RETURNS text` | planning/0006 | Covered-signer rule for both signers |
| `readiness_evidence_guard()` (replaced) | readiness/0007 | Locks the signer persons `FOR SHARE` **before** the visit UPDATE (order robot → persons → visit), then the rule |
| `readiness_signer_versions_problem(jsonb, jsonb)` | readiness/0007 | Signer persons ⊆ `person_versions` keys |
| issue function, `readiness_authorization_guard`, `readiness_departure_guard` (replaced) | readiness/0007 | Call the two checks. `readiness_lock_snapshot_guards` already share-locks every `person_versions` key in order |

### 7.4 App changes

- `planning.services.grant_qualification` checks the special permission for `readiness_inspector` and takes `basis_ref`.
- `readiness.evidence.record_evidence` share-locks the signer persons through `_lock_visit_and_robot(share_persons=…)`
  and gives an explained `Conflict`.
- `readiness.evaluator._read_world` adds signer persons. New code `INSPECTOR_NOT_QUALIFIED`.
- `readiness.commands` needs no change: it already locks the persons named in the snapshot.
- Contract 03 section 5 (snapshot shape) gets a note: `person_versions` also contains the inspection signers.

### 7.5 Migration and backfill

- No existing qualification becomes `readiness_inspector`, and nothing is backdated.
- Existing pre-departure evidence (demo V-0001) no longer authorizes. `seed_guardrails` grants synthetic
  `readiness_inspector` qualifications to `dev_ops_lead` and `dev_field_operator` (granted by `dev_safety_reviewer`,
  valid from now, `basis_ref = "SYNTHETIC-TRAINING"`). If V-0001 has not departed, it re-records V-0001's
  pre-departure inspection and evidence, so the demo is "Ready to request authorization" again.
- Test worlds (`fleet_world`, `readiness_world`, `operations_world`) do the same (lead).

### 7.6 Tests

| Area | Tests |
|---|---|
| Grant refusals | Ops lead grant; self-grant; backdated `valid_from`; blank `basis_ref`; kind CHECK |
| Coverage | Covered at the inspection time vs expired at the inspection time; expiry after the inspection still valid; revoked invalid |
| Evidence | `pass` refused / `fail` accepted |
| Races, both orders | Revoke vs record_evidence; revoke vs authorize. Revoke after authorization: departure refused by person version |
| Direct `ops_app` SQL | Each of the above |
| Oracle | Python/SQL oracle of the covered-signer rule |
| Regression | Signer equal to the crew lead: one `person_versions` entry, no duplicate |

---

## 8. X-10 (P1): daily close-out view

### 8.1 Problem

The trip-closure chain is enforced (`execution_closure_guard`), and Slice 5 lists `RETURN_OVERDUE`,
`CUSTODY_UNVERIFIED`, `POST_VISIT_INSPECTION_MISSING` and `RECONCILIATION_UNRESOLVED` with grace thresholds. Nothing
shows "what is still open for day D", and no close-out record exists (G-12).

### 8.2 Rule

- **Page and API.** Page `/ops/closeout/?date=YYYY-MM-DD` and API `/api/ops/closeout?date=`. Permission
  `operations.read`. Built inside `operations.snapshot.read_snapshot()` (REPEATABLE READ READ ONLY, no locks; contract
  05 section 5). It never runs the readiness evaluator.
- **Local day.** Uses the deployment setting `ops_closeout_timezone` (IANA). If it is missing or invalid, the page shows
  "Close-out time zone not configured" and lists only the carried-over section (which needs no day), and
  `THRESHOLD_NOT_CONFIGURED` names the key. A run belongs to day D when its `readiness_departureevent.departed_at`,
  converted to that zone, falls on D. A future D is refused (400).
- **Sections** (all items are runs, keyed by run code, ordered by `(departed_at, run code)`):
  1. *Departed on D, still open*: no `TripClosure`. Shows the stage from `operations_open_run_stage`.
  2. *Carried over*: departed before D, still open at the snapshot.
  3. *Custody not verified*: a run in 1 or 2 whose administrative assignment has no `CustodyVerification`.
  4. *Post-visit inspection outstanding*: a hub receipt and no inspection of the robot with `visit_ref` = the visit
     after it, or "robot not back at hub" (custody site / vehicle / unaccounted).
  5. *Reconciliation unresolved*: the latest reconciliation is `unresolved`, or there is a hub receipt and no
     reconciliation.
  6. *Context counts*: runs departed on D, and runs departed on D that are closed.
- **No grace thresholds.** The close-out lists the raw outstanding state. The inbox applies grace.
- **State is "as of" the snapshot, not reconstructed at the end of D,** and the page says so.
- **Wording.**
  - Never "signed off", "closed out", "all clear", "complete", "OK" or "cleared".
  - With nothing outstanding: "No outstanding items found for runs departed on D, as of HH:MM. This page is not a
    sign-off; the platform has no close-out record."
- **Budget.** ≤ 6 SQL statements, constant in fleet size. The latency budgets are those of contract 05 A1 for pages
  without the evaluator (p95 ≤ 400 ms on the scale fixture).

### 8.3 Data model and enforcement

No tables. View `operations_closeout_runs` (operations/0003, SECURITY INVOKER, ops_app SELECT only) over the existing
custody, run, trip-event, inspection and reconciliation rows. New setting key `ops_closeout_timezone`; the seed writes a
synthetic demo value.

### 8.4 Tests

- Day membership across midnight and DST in the configured zone; carried-over runs; each section firing and not
  firing.
- Agreement: every run with a `CUSTODY_UNVERIFIED`, `POST_VISIT_INSPECTION_MISSING` or `RECONCILIATION_UNRESOLVED`
  exception in the same snapshot is listed (superset).
- Snapshot consistency (a writer commits mid-build); no writes and no locks (captured statements); budget.
- 403 without `operations.read`; forbidden-wording test; missing time-zone path.

---

## 9. Lock-order impact

**Global order after Slice 7** (one entry appended; nothing moves):
```
role grants -> settings (advisory S) -> site -> pattern -> robot -> person -> vehicle -> visit -> vehicle trip
  -> readiness policy -> departure authorization -> incident policy -> execution run -> incident
  -> fleet_holdpolicy                                                                        (Slice 7, new)
```
`platform_core.locks.ORDER` gains `("fleet_holdpolicy", "hold_policies")` with `update_hold_policies` and
`share_hold_policies` (lead). Why last: every command that touches a hold policy already holds robots (or a pattern
and its robots), and `report_incident` also holds the visit S and the run U before `_create_hold` runs. Placing
`fleet_holdpolicy` right after robots would invert that order. No command takes anything after a hold policy.

| Command (new or changed) | Locks, in order |
|---|---|
| create_inspection_template | pattern S |
| approve / withdraw inspection template; create / approve / withdraw periodic rule | pattern U, robots on the pattern U (trigger bumps the pattern and robots, as `decide_pattern`) |
| submit_inspection | robot U, then hold policy S (only when a hold is placed) |
| place_hold (robot) / (pattern) | robot U / pattern U + robots U, then hold policy S (candidate and newer ids, re-resolve after the lock, `RetryTransaction` if the binding changed) |
| report_incident | unchanged (robot U, visit S, run U), then hold policy S inside `_create_hold` |
| approve_release / withdraw_release_approval / release_hold | robot U (or pattern U + robots U), incident S (incident holds), hold policy S (effective policy) |
| request / approve hold rebase | robot U (or pattern U + robots U), hold policy S (to-policy) |
| create_hold_policy | none (consecutive version unique) |
| approve / withdraw hold policy | hold policy U |
| grant / revoke qualification (incl. readiness_inspector) | person U (unchanged) |
| record_evidence | robot S, **signer persons S**, visit U (the trigger locks in the same order) |
| authorize_departure / record_departure | unchanged list. `person_versions` now includes the signers, so they are locked S in the persons position |
| escalation deadline set create / approve / withdraw | settings S only (uniqueness constraints serialize) |
| close-out, production blockers, new exceptions | none (snapshot read) |

**Races covered by the lock order** (each tested in both orders, deterministic checkpoints, no sleeps):

| Pair | Serialization point |
|---|---|
| Template withdrawal / periodic-rule change vs authorize | pattern U vs S; after-authorization changes caught by the pattern version at departure |
| Hold-policy withdrawal vs release, vs placement | policy U vs S |
| Rebase approval vs release | robot U |
| Incident status vs release | incident S vs U |
| Qualification revoke vs record_evidence, vs authorize | person U vs S |
| Qualification revoke vs departure | person version |
| Mode switch vs authorize | existing settings lock |

`docs/02_lock_graph.md` is updated by the lead with these rows. The cycle check is re-run in
`test_fleet_concurrency.py` (`lock_guards` with opposite inputs never deadlocks, including `hold_policies`).

## 10. Migrations, backfill and demo vs production

Migration order:
1. fleet/0008 (templates, rules, hold policies, forced times);
2. planning/0006 (qualification);
3. readiness/0007 (evidence, issue function, signer and periodic SQL);
4. execution/0005 (incident release clause, deadline sets);
5. operations/0003 (views).

No migration inserts an approval, a rule, a deadline set or a qualification.

| Item | Production (no company input) | Demo after `make seed` (new idempotent `seed_guardrails`, demo mode only, run after the existing seeds) |
|---|---|---|
| X-1 | Synthetic-template PASS refused at evidence and authorization; panel blocker | Synthetic T1 usable; the panel says it would block in production |
| X-3 | Every robot ineligible and unauthorizable (`interval unknown`) | Synthetic 4,320-min rule per pattern; fresh synthetic periodic inspections |
| G-01 | Every hold unreleasable; panel and inbox blockers | Draft policy unchanged; R-03 flow unchanged; blockers visible as "would block in production" |
| X-4 | `ESCALATION_DEADLINE_NOT_CONFIGURED` | Synthetic deadline set |
| X-8 | No qualified inspectors: no passing pre-departure evidence | Synthetic grants for dev ops lead and field operator; V-0001 evidence re-recorded |
| X-10 | Time zone not configured: carried-over section only | `ops_closeout_timezone` synthetic demo value |

Every synthetic value is labelled "SYNTHETIC demo, not a company value" in the UI.

## 11. Acceptance tests (closure bar items, all real PostgreSQL)

1. **Per-item suites** (sections 3.9, 4.9, 5.9, 6.5, 7.6, 8.4).
2. **Direct `ops_app` SQL bypass** (`tests/test_s7_db.py`). Every new trigger refusal and issue-function refusal. The
   runtime role has no INSERT on authorization, cannot write settings, and cannot UPDATE/DELETE/TRUNCATE any new table.
   Forced times ignore supplied values.
3. **Interleavings, both orders.** Every pair in section 9, plus a demo-to-production switch vs each new
   mode-dependent decision (evidence insert, authorize, template usability, deadline-set approval).
4. **Production vs demo separation.** Each gate tested in both modes. `allow_draft_policies` and synthetic sources
   never usable in production. Panel blockers present in both modes.
5. **Permissions.** Each new command 403 for every other role. Author or placer exclusions. Escalation-ack
   behaviour unchanged.
6. **Python/SQL oracles:**
   - template usability;
   - periodic status;
   - covered signer;
   - hold release status;
   - extended eligibility view (Slice 5 6.1, now with `at`);
   - escalation overdue (SQL vs Python reference);
   - close-out vs exceptions (superset).
7. **Slice 3-6 regression.** Existing suites pass. Tests changed only for the stated behaviour changes:
   `allow_draft_policies` outside demo, binding to the active policy, `place_hold` with `incident`, test worlds
   granting inspector qualifications and periodic rules.
   - `tests/test_readiness_authz_boundary.py` gains a mapping for every new evaluator code to its SQL refusal.
   - The Slice 5 `E` pin is re-measured. If the evaluator grows past `E = 70`, the integration lead raises the pin,
     with the measured figure.

## 12. File ownership and merge order

| Owner | Files (exclusive) |
|---|---|
| Lead | `platform_core/locks.py` (`hold_policies`), `accounts/roles.py` (permissions), `config/**`, `tests/conftest.py`, `tests/fleet_world.py`, `tests/readiness_world.py`, `tests/execution_world.py`, `tests/operations_world.py`, `*/management/commands/seed_guardrails.py` (new, in `fleet`) and `make seed`, existing tests changed by stated behaviour changes, `docs/**` (incl. 02 lock graph, 03 snapshot note), `README.md`, `.github/**`, integration |
| A, schema/DB | `fleet/models.py`, `fleet/migrations/0008_*`, `planning/models.py` (kind, `basis_ref`), `planning/migrations/0006_*`, `readiness/migrations/0007_*`, `execution/models.py` (deadline sets), `execution/migrations/0005_*`, `operations/migrations/0003_*`, `tests/test_s7_db.py` (bypass + oracle fixtures for SQL functions) |
| B, app/evaluator | `fleet/services.py`, `planning/services.py` (grant), `readiness/evidence.py`, `readiness/evaluator.py`, `execution/escalation_deadlines.py` (new), `tests/test_s7_fleet.py`, `tests/test_s7_readiness.py`, `tests/test_s7_concurrency.py`, `tests/test_s7_oracles.py` |
| C, read models/UI | `operations/exceptions.py`, `operations/readmodels.py`, `operations/closeout.py` (new), `operations/views.py`, `operations/api.py`, `operations/urls.py`, `operations/templates/**`, `fleet/views.py`, `fleet/api.py`, `fleet/templates/**`, `readiness/views.py` + templates (signer warnings), `execution/views.py` + templates (deadline pages), `tests/test_s7_operations.py`, `tests/test_s7_ui.py` |

**Merge order:**
1. Lead phase 0: permissions, lock order, test worlds, settings keys, seed skeleton.
2. A: schema, triggers, SQL functions, bypass tests. Merge.
3. B: services, evaluator, Python mirrors, oracles, races, in its own worktree and database `ops_s7_b`.
4. C: read models and UI, on frozen B signatures, database `ops_s7_c`.
5. Lead: merge B, then C. Full suite once on the shared host (one suite at a time, as in Slice 6). CI, UI
   walkthrough, EM review.

## 13. What Slice 7 does NOT do

- No real procedures, checklists, intervals, deadlines, release rules or approvals. No approval row is created by
  a migration, and none by a seed outside demo. The playbook's 14 days, 15 minutes and similar figures are not used.
- No notifications, paging, email, outbox, timers or background workers (X-4 is read-only).
- No close-out or sign-off record (X-10 is a view).
- No severity-aware release (X-2). No restricted-home acknowledgement (X-5), crew buffer (X-6), field-lead disposition
  (X-7) or incident links and ticket references (X-9).
- No post-visit template approval requirement on reconciliation. No fleet-wide task restrictions (G-06).
- No change to the Slice 6 capacity model or its workbook.

## 14. Closure bar

- Full backend CI green: `ruff check`, `ruff format --check`, `mypy .`, `makemigrations --check`, full `pytest` (one
  run on the shared host, by the lead).
- The `xlsx` job still green (Slice 6 reconciliation, unchanged).
- The `perf` workflow within the contract 05 A1 budgets, including the close-out page.
- Section 11 complete, including every direct-SQL bypass test and every both-order race.
- UI walkthrough, recorded in the closure note:
  - **demo:** blockers panel visible, periodic due states, deadline overdue, close-out for today, V-0001 still
    authorizable;
  - **production mode on a scratch database:** every P0 blocker visible and every authorization and release
    refused with its explained reason.
- README section "Slice 7" with the production-blocker list.

## 15. Decisions requested from the EM

Each item gives the recommended answer first.

1. **X-1 evidence insert.** *Recommend:* refuse production `pass` pre-departure evidence on a non-approved template,
   accept `fail`. The issue function stays authoritative (section 3.2).
2. **X-1 inspection-hold release.** *Recommend:* in production the "newer PASS" must be on a template approved for
   `pre_departure` or `periodic` (R1-5).
3. **X-1 policy floor.** *Recommend:* in production the active readiness policy must require
   `pre_departure_inspection` (R1-6).
4. **Template approval roles.** *Recommend:* author engineer or engineering_lead. Approvals engineering_lead +
   safety_reviewer, per purpose, author excluded.
5. **X-3 interval scope.** *Recommend:* one versioned, approved rule per configuration pattern. Alternative: per
   robot, or one fleet-wide value (rejected: the interval belongs to the configuration).
6. **X-3 authorization window.** *Recommend:* `due_at >= planned unloading end`. Alternative: not overdue at issue time
   only.
7. **X-3 eligibility.** *Recommend:* overdue / never / unknown are eligibility reasons, which also block planning.
   Alternative: authorization-only, with a planning warning.
8. **X-3 qualifying inspection.** *Recommend:* only templates approved for `periodic`. A pre-departure or post-visit
   inspection counts only if engineering approves its template for `periodic` too. Signers must be covered (X-8).
9. **X-3 demo value.** *Recommend:* 4,320 min, labelled SYNTHETIC, demo only. No production default, ever.
10. **G-01 policy approvals.** *Recommend:* authors engineer or engineering_lead. `approved_procedure` needs engineering_lead
    + safety_reviewer + founder, all distinct, author excluded. Synthetic needs two slots.
11. **G-01 legacy holds.** *Recommend:* unreleasable in production until a two-person (engineering_lead + safety_reviewer,
    not the placer) re-basing to the active production policy, with fresh approvals. Rejected alternatives: automatic
    re-binding (breaks fixed-at-placement); "retire the robot" as the only path.
12. **G-01 incident status.** *Recommend:* releasing an incident hold needs the incident `resolved` or `closed`. Closing
    still never releases.
13. **G-01 floor.** *Recommend:* an `approved_procedure` policy must cover every hold source. Its incident rule must have
    distinct approvers and be independent of the placer.
14. **`allow_draft_policies`.** *Recommend:* no effect outside demo mode (behaviour change).
15. **B-07 (company, open).** The incident release rule content (slots, roles, any severity dependence) is a company
    decision for engineering, safety and the founder. Until it exists, production incident holds stay unreleasable
    and visibly blocked. *Recommend:* the EM asks the company for one strict, severity-independent rule, or reopens
    X-2 explicitly.
16. **X-4 storage.** *Recommend:* versioned deadline sets approved by safety_reviewer + founder and bound to the
    incident-policy version. Alternative: owner-written deployment settings, as in Slice 5 D4 (no approval provenance).
17. **X-4 semantics.** *Recommend:* basis `reported_at`; an explicit `"unclassified"` entry; an empty list means "no
    clock"; a late ack clears the item.
18. **X-8 grant.** *Recommend:* safety_reviewer grants. No self-grant, no backdating, training reference required.
    Revocation by ops_lead, safety_reviewer or engineering_lead.
19. **X-8 coverage.** *Recommend:* the qualification must cover the inspection time and be unrevoked at evidence and
    at authorization. Expiry after the inspection does not invalidate it.
20. **X-10 day basis.** *Recommend:* `ops_closeout_timezone` setting. A run belongs to the local date of its
    departure. Carried-over runs are always listed. The state shown is "as of now", never reconstructed.
21. **Forced DB times.** *Recommend:* force `fleet_inspection.submitted_at`, `fleet_hold.placed_at` and
    `planning_qualification.granted_at` to `clock_timestamp()` on insert (section 2.3).
