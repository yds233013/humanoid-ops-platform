# Slice 7: frozen SQL signatures and refusal messages (Engineer A)

| | |
|---|---|
| **Status** | FROZEN for Engineers B and C once merged. A change goes through the integration lead, with the exact reason (contract 07a section 12) |
| **Contract** | `docs/07a_production_guardrails_contract.md` rev 2 (`0a8bdab`) |
| **Migrations** | `fleet/0008_s7_templates_rules_policies` -> `planning/0006_s7_qualification` -> `readiness/0007_s7_guardrails` -> `execution/0005_s7_incident_release_and_deadlines` -> `operations/0003_s7_views` (fleet/0008 depends on the latest Slice 1-6 migration of every app) |
| **Tests** | `backend/tests/test_s7_db.py` (direct `ops_app` SQL, oracle fixtures for the SQL functions) |

Every refusal raises `fleet_fail(text)` (SQLSTATE `check_violation`, mapped to `Conflict` by `run_command`).
Append-only violations raise `reject_mutation()` (`insufficient_privilege`). Missing table privileges raise
`permission denied`. "Prefix" below is the exact start of the message; `%s` parts are filled in at run time.
Python (Engineer B) should mirror the *rule*; the message text of a database refusal is what a direct-SQL caller
sees, and B's explained `Conflict` messages may differ.

## 1. Mode, settings lock, helpers (fleet/0008)

| Signature | Volatility | Meaning |
|---|---|---|
| `platform_settings_lock_held() RETURNS boolean` | VOLATILE | This backend holds advisory key `64942988919` (pg_locks `classid = 15`, `objid = 518479479`, `objsubid = 1`, granted), shared or exclusive. Verified on PostgreSQL 16.16: no lock false; shared true; exclusive true; held by another session false; after commit / rollback false; another key false |
| `fleet_require_settings_lock(decision text) RETURNS void` | VOLATILE | Refuses unless the lock is held. Never acquires it |
| `fleet_production_mode() RETURNS boolean` | STABLE | `coalesce(fleet_setting('demo_mode'), 'false') <> 'true'` |
| `fleet_has_any_role(uid bigint, roles text[]) RETURNS boolean` | STABLE | |
| `fleet_nonblank(t text) RETURNS boolean` | IMMUTABLE | |

Refusal (all asserting triggers): **`%s depends on the deployment mode: take pg_advisory_xact_lock_shared(64942988919) first`**
with `%s` one of `releasing a hold`, `a departure`, `approving an escalation deadline set`.

## 2. Forced database times

| Column | Where |
|---|---|
| `fleet_inspection.submitted_at := clock_timestamp()` | `fleet_inspection_guard()` (fleet/0008, body otherwise = fleet/0004) |
| `fleet_hold.placed_at := clock_timestamp()` on INSERT (unchanged, immutable on UPDATE) | `fleet_hold_guard()` |
| `planning_qualification.granted_at := clock_timestamp()` on INSERT, every kind | `planning_qualification_insert_guard()` |
| `approved_at` / `withdrawn_at` / `created_at` / `requested_at` / `activated_at` of every new Slice 7 table | their insert triggers |

## 3. X-1 inspection templates (fleet/0008)

| Signature | Kind |
|---|---|
| `fleet_template_items_problem(items jsonb) RETURNS text` | IMMUTABLE helper |
| `fleet_template_guard()` | BEFORE INSERT `fleet_inspectiontemplate` |
| `fleet_template_withdrawn(p_template bigint) RETURNS boolean` | STABLE |
| `fleet_template_usable(p_template bigint, p_purpose text, p_production boolean) RETURNS boolean` | STABLE. R1-1: not withdrawn AND (engineering + safety approvals for the purpose OR (NOT production AND source = 'synthetic_demo')). Unknown template: false |
| `fleet_template_approval_guard()` | BEFORE INSERT `fleet_inspectiontemplateapproval`; bumps the pattern (`fleet_bump_pattern`) |
| `fleet_template_withdrawal_guard()` | BEFORE INSERT `fleet_inspectiontemplatewithdrawal`; bumps the pattern |

Refusal prefixes:
- `invalid inspection template: ` + `items must be a non-empty list` / `every item needs a non-blank id` / `duplicate item id %s` / `item %s needs a non-blank label` / `item %s needs a boolean robot_condition`
- `a new inspection template needs an author (created_by)`
- `creating an inspection template requires engineer or engineering_lead`
- `an engineering_doc inspection template needs a procedure_ref`
- `unknown inspection template`
- `a synthetic_demo inspection template can never be approved`
- `a legacy inspection template (no author) can never be approved`
- `a withdrawn inspection template cannot be approved`
- `slot %s requires role %s` (shared with every engineering/safety slot)
- `the author of an inspection template cannot approve it`
- `the same person cannot fill two approval slots for one purpose`
- `a withdrawal needs a reason` (shared)
- `withdrawing an inspection template requires engineering_lead or safety_reviewer`
- CHECK constraints: `template_source_valid`, `template_purpose_valid`, `template_slot_valid`; UNIQUE `uniq_template_purpose_slot`, `uniq_template_purpose_approver`

## 4. X-3 periodic inspection rules and status

| Signature | Migration |
|---|---|
| `fleet_periodic_rule_withdrawn(p_rule bigint) RETURNS boolean` | fleet/0008 |
| `fleet_periodic_rule_guard()` (BEFORE INSERT rule), `fleet_periodic_rule_approval_guard()`, `fleet_periodic_rule_withdrawal_guard()` (both bump the pattern) | fleet/0008 |
| `fleet_active_periodic_rule_id(p_pattern bigint) RETURNS bigint` | fleet/0008. Highest version_no ever approved (engineering + safety); NULL if withdrawn; no fallback |
| `fleet_usable_periodic_rule_id(p_pattern bigint, p_production boolean) RETURNS bigint` | fleet/0008. The active rule when `interval_min IS NOT NULL` and (source = 'engineering_doc' OR NOT production), else NULL |
| `readiness_periodic_inspection_status(p_robot bigint, p_production boolean, p_at timestamptz) RETURNS TABLE (state text, due_at timestamptz, inspection_id bigint, rule_id bigint)` | readiness/0007 |
| `readiness_periodic_dependency_problem(p_inspection bigint, p_robot bigint, p_production boolean, p_until timestamptz) RETURNS text` | readiness/0007 |

`readiness_periodic_inspection_status` exactly (B's Python oracle must match):
- `state = 'unknown_interval'` when `fleet_usable_periodic_rule_id(pattern of the current manifest, p_production)` is NULL (no manifest, no rule, withdrawn active rule, NULL interval, synthetic rule in production). `due_at`, `inspection_id` NULL; `rule_id` = `fleet_active_periodic_rule_id(pattern)` (may be NULL).
- Qualifying inspection: `robot_id = p_robot`, `manifest_id = current_manifest_id`, `result = 'PASS' AND complete`, `fleet_template_usable(template_id, 'periodic', p_production)`, `planning_inspection_signer_problem(id) IS NULL`. The latest by `(submitted_at DESC, id DESC)`. No filter on `submitted_at <= p_at`.
- None: `state = 'never_inspected'`, `rule_id` = the usable rule.
- Else `due_at = submitted_at + interval_min minutes`; `state = 'overdue'` when `p_at >= due_at`, else `'due'`.

`readiness_periodic_dependency_problem` returns NULL, or the first of:
`there is no relied periodic inspection` /
`periodic inspection %s is not on the robot's current configuration` /
`periodic inspection %s is not a complete PASS` /
`periodic inspection %s uses template %s, which is not usable for periodic inspection in %s mode` /
`periodic inspection %s: ` + signer problem /
`periodic inspection interval unknown for configuration %s` /
`periodic inspection %s is due at %s, not after the planned unloading end %s` (strict: NULL only when `due_at > p_until`).

Rule refusals: `periodic inspection rule versions must be consecutive per pattern`,
`creating a periodic inspection rule requires engineer or engineering_lead`,
`an engineering_doc periodic inspection rule needs a procedure_ref`, `unknown periodic inspection rule`,
`a withdrawn periodic inspection rule cannot be approved`, `slot %s requires role %s`,
`the author of a periodic inspection rule cannot approve it`,
`the same person cannot fill two approval slots on one periodic inspection rule`, `a withdrawal needs a reason`,
`withdrawing a periodic inspection rule requires engineering_lead or safety_reviewer`;
CHECK `periodic_rule_interval_range` (NULL or 1..5,256,000), `periodic_rule_source_valid`, `periodic_rule_slot_valid`.

## 5. G-01 hold policies, binding, release, rebasing (fleet/0008, execution/0005)

| Signature | Notes |
|---|---|
| `fleet_lock_hold_policy_registry(p_update boolean) RETURNS void` | Locks the registry row FOR UPDATE (true) or FOR SHARE (false); refuses `the hold-policy registry row (id 1) is missing` (fail closed). Every trigger takes the registry through it |
| `fleet_hold_policy_slot_role(slot text) RETURNS text` | IMMUTABLE. engineering -> engineering_lead, safety -> safety_reviewer, founder -> founder. `fleet_slot_role` is unchanged (capabilities, templates, rules, rebases never accept `founder`) |
| `fleet_hold_policy_withdrawn(pid bigint) RETURNS boolean` | STABLE |
| `fleet_hold_policy_approved(pid bigint) RETURNS boolean` | STABLE. 2 distinct slots (synthetic_demo) or 3 (approved_procedure) |
| `fleet_policy_production_usable(pid bigint) RETURNS boolean` | STABLE. approved_procedure, fully approved, not withdrawn; mode-independent (used by rebasing and the blockers view) |
| `fleet_policy_usable(pid bigint) RETURNS boolean` (replaced) | STABLE. Section 5.2 formula; `allow_draft_policies` only in demo |
| `fleet_active_hold_policy_id() RETURNS bigint` | STABLE |
| `fleet_hold_binding_policy_id() RETURNS bigint` | STABLE. active, else newest, else NULL. Reads no setting |
| `fleet_hold_policy_floor_problem(rules jsonb) RETURNS text` | IMMUTABLE. Floor of section 5.2 |
| `fleet_hold_policy_guard()` | BEFORE INSERT `fleet_holdpolicy`. Registry U |
| `fleet_policy_approval_guard()` (replaced) | Registry U |
| `fleet_hold_policy_withdrawal_guard()` | BEFORE INSERT `fleet_holdpolicywithdrawal`. Registry U |
| `fleet_hold_effective_rule(h fleet_hold) RETURNS TABLE (policy_id bigint, rebase_id bigint, slots jsonb, rule jsonb)` | STABLE. Latest activation (highest activation id) of the hold, else the placement values |
| `fleet_hold_source_problem(h fleet_hold) RETURNS text` | stub NULL in fleet/0008; execution/0005 body: referencing Incident must be `resolved` or `closed`; in production a hold with `source_type = 'incident'` and no Incident row is unreleasable |
| `fleet_hold_lock_source(h fleet_hold) RETURNS void` | stub in fleet/0008; execution/0005 locks the referencing `execution_incident` FOR SHARE |
| `fleet_hold_release_problem(h fleet_hold) RETURNS text` (replaced) | STABLE |
| `fleet_hold_guard()` (replaced) | see below |
| `fleet_release_approval_guard()` (replaced) | Registry S; forces `rebase_id` = effective rebase |
| `fleet_lock_hold_target(h fleet_hold) RETURNS void` | robot U (or pattern U + its robots U, ascending), then registry S |
| `fleet_hold_rebase_guard()` | BEFORE INSERT `fleet_holdrebase` |
| `fleet_hold_rebase_approval_guard()` | BEFORE INSERT `fleet_holdrebaseapproval`, SECURITY DEFINER (`search_path = public, pg_temp`); the final slot re-checks 1-4 and inserts `fleet_holdrebaseactivation` |
| `fleet_hold_rebase_activation_guard()` | BEFORE INSERT activation: only from inside the approval trigger |
| sequence `fleet_hold_rebase_code_seq` | `RB-0001` codes are drawn by the caller (B's command) |

`fleet_hold_guard()` order (locks follow the global order):
- DELETE: `holds cannot be deleted; release them`.
- UPDATE: structural checks first (`hold already released`, `the only allowed change to a hold is its release`), then
  **asserts the settings lock** (`releasing a hold depends on ...`), then bumps robot / pattern+robots, locks the
  incident S (`fleet_hold_lock_source`), registry S, then `cannot release: ` + `fleet_hold_release_problem(NEW)`.
- INSERT: bumps robot / pattern+robots, registry S, forces `placed_at`, enforces the binding:
  `a new hold binds to policy %s (the active hold policy, else the newest), not %s` (`vN` or `none`),
  `required slots must be copied exactly from the policy`, `a hold cannot be created released`. Reads no setting.

`fleet_hold_release_problem` returns NULL or the first of:
`no release rule exists for this hold; an approved policy must provide one` /
`hold policy v%s was withdrawn; a re-basing decision to the active approved policy is needed` /
`hold policy v%s is not usable in production (it must be a fully approved approved_procedure policy); a re-basing decision to the active approved policy is needed` /
`the hold policy is a draft and drafts are not allowed in this deployment` (demo) /
`slot %s has no valid approval on the current configuration` (approvals with the effective `rebase_id` only) /
`needs a newer PASS inspection on the current configuration` (demo) or
`needs a newer PASS inspection on the current configuration, on a template approved for pre-departure or periodic use (production)` (R1-5) /
`incident %s is %s; its hold can be released only once the incident is resolved or closed` /
`an incident hold with no incident record cannot be released in production`.

Policy refusals: `hold policy versions must be consecutive`, `creating a hold policy requires engineer or engineering_lead`,
`an approved_procedure hold policy needs a procedure_ref`, `hold policy floor: ` + (`rules must be an object keyed by hold source` /
`unknown hold source %s` / `an approved_procedure policy needs a rule for every hold source (missing %s)` /
`rule %s needs at least one slot` / `rule %s: every slot needs a name` / `rule %s: duplicate slot %s` /
`rule %s: slot %s needs roles` / `rule %s: role %s may not approve a release` /
`the incident rule must require distinct approvers independent of the placer`),
`unknown hold policy slot %s`, `a withdrawn hold policy cannot be approved`,
`the founder slot exists only on an approved_procedure hold policy`, `slot %s requires role %s`,
`the author of a hold policy cannot approve it`, `the same person cannot fill two approval slots on one hold policy`,
`a withdrawal needs a reason`, `withdrawing a hold policy requires engineering_lead or safety_reviewer`.

Release-approval refusals (unchanged text): `hold already released`, `slot %s is not required for this hold`,
`role %s may not fill slot %s`, `approver does not hold role %s`, `slot %s is already filled`,
`the same person cannot fill two slots on one hold`, `the person who placed the hold cannot approve its release`,
`approval must be bound to the robot's current configuration`.

Rebase refusals: `unknown hold`, `requesting a hold re-basing requires engineering_lead or safety_reviewer`,
`a re-basing request needs a reason`, `rebase refused: hold %s is already released`,
`rebase refused: from_policy and based_on_rebase must be the hold's current effective policy and rebase`,
`rebase refused: hold %s is already under a production-usable policy; it is released under its own rules`,
`rebase refused: the destination must be the active, production-usable (approved_procedure) policy`,
`rebase refused: required slots must be copied exactly from the destination policy's rule`;
approvals: `unknown rebase`, `slot %s requires role %s`, `the person who placed the hold cannot approve its re-basing`,
`the same person cannot fill both approval slots of a re-basing`, `rebase %s is already effective`; final slot:
`rebase %s refused: hold %s is already released`,
**`rebase %s refused: policy v%s is no longer the active production policy (active: %s)`** (`vM` or `none`),
**`rebase %s refused: superseded by rebase %s, which became effective first`**,
`rebase %s refused: hold %s is already under a production-usable policy`;
activation: `a rebase becomes effective only through its final approval`; UNIQUE `uniq_rebase_activation_base`
(`(hold, based_on_rebase)` NULLS NOT DISTINCT) and the one-to-one `rebase_id`.

## 6. X-8 readiness inspectors (planning/0006, readiness/0007)

| Signature | Notes |
|---|---|
| `planning_qualification_insert_guard()` | BEFORE INSERT `planning_qualification` (`v_now := clock_timestamp()`, granted_at, immediate valid_from) |
| `planning_inspector_covered(p_user bigint, p_at timestamptz) RETURNS boolean` | STABLE. Unrevoked `readiness_inspector` grant of the user's Person with `valid_from <= p_at <= valid_to` |
| `planning_inspection_signer_problem(p_inspection bigint) RETURNS text` | STABLE. Both signers covered at the inspection's `submitted_at` |
| `readiness_relied_predeparture_inspections(p_evidence_ids jsonb) RETURNS SETOF bigint` | STABLE. Inspections behind the given `pre_departure_inspection` evidence ids |
| `readiness_dependency_persons(p_evidence_ids jsonb, p_periodic_inspection bigint) RETURNS SETOF bigint` | STABLE. Signer persons of both relied inspections; a signer without a Person yields NULL |
| `readiness_signer_versions_problem(p_snapshot jsonb, p_evidence_ids jsonb, p_periodic_inspection bigint) RETURNS text` | STABLE |

Refusals: `a readiness_inspector qualification can be granted only by a safety_reviewer`,
`a readiness_inspector qualification cannot be granted to oneself`,
`a readiness_inspector qualification cannot be backdated (valid_from before granted_at)`,
`a readiness_inspector qualification needs a training record reference (basis_ref)`; CHECK `qualification_kind_valid`.
Signer problem: `inspection %s does not exist` / `inspection %s is missing a signer` /
**`inspector %s is not a qualified readiness inspector for inspection %s`** (`%s` = person code, else username).
Signer versions problem: the signer problem (prefixed `periodic inspection %s: ` for the periodic one) /
`an inspection signer has no person record` / `person %s (inspection signer) is missing from person_versions`.

## 7. Readiness evidence, authorization, departure (readiness/0007, replaced bodies)

`readiness_evidence_guard()` adds: signer persons S before the visit UPDATE; for `pre_departure_inspection` +
`pass` only: **`passing pre-departure evidence refused: inspection %s uses template %s, which is not approved for pre-departure use in production`**
(production read WITHOUT asserting the lock, by design), then **`passing pre-departure evidence refused: `** + signer problem (both modes).

`readiness_issue_authorization(p_visit_id bigint, p_issued_by bigint, p_expires_at timestamptz, p_evidence_ids jsonb, p_recovery_plan_id bigint, p_guard_snapshot jsonb, p_mode text) RETURNS TABLE (id bigint, code text)` —
**signature, SECURITY DEFINER, search_path and grants unchanged.** New refusals, in evaluation order:
1. after policy/mode: `authorization refused: the active readiness policy does not require pre_departure_inspection evidence (required in production)` (R1-6)
2. after the crew block: `authorization refused: periodic inspection interval unknown for configuration %s` /
   `authorization refused: periodic inspection none on the current configuration` /
   `authorization refused: periodic inspection overdue since %s` /
   `authorization refused: periodic inspection due at %s, not after the planned unloading end %s` /
   `authorization refused: ` + `readiness_periodic_dependency_problem(...)`
3. in the evidence loop (pre_departure_inspection): **`authorization refused: inspection %s uses template %s, which is not approved for pre-departure use in production`**, then `authorization refused: ` + signer problem
4. `bound := least(bound, due_at)`; the expiry message now reads `authorization refused: expires_at is later than allowed (%s: policy TTL, evidence ages and periodic due date)`
5. after the evidence_ids check: `authorization refused: ` + `readiness_signer_versions_problem(snapshot, evidence_ids, periodic_inspection_id)`
6. inserts `periodic_inspection_id` (the status selection) and `planned_unload_end` (the computed `unload_end`).

`readiness_authorization_guard()` adds: `an authorization must record its relied periodic inspection and planned unloading end`,
`the relied periodic inspection must be the one the periodic inspection status selects`, then the dependency problem
and the signer-versions problem (unprefixed).

`readiness_departure_guard()`: **first** `a departure depends on the deployment mode: ...` (EM ruling 1); existing checks
unchanged; then `the authorization records no relied periodic inspection (issued before Slice 7); authorize again`,
`the relied periodic inspection no longer qualifies: ` + dependency problem,
`inspection signers changed since authorization: ` + signer-versions problem, and in production
`inspection %s uses template %s, which is not approved for pre-departure use in production`.
A revoked signer present in `person_versions` is refused earlier by the existing
`guards changed since authorization: person %s version ...` (two independent layers, mutation-tested).

## 8. X-4 escalation deadline sets (execution/0005)

| Signature | Notes |
|---|---|
| `execution_deadline_rules_problem(p_policy bigint, j jsonb) RETURNS text` | STABLE |
| `execution_deadline_set_withdrawn(p_set bigint) RETURNS boolean` | STABLE |
| `execution_deadline_slot_role(slot text) RETURNS text` | IMMUTABLE. safety -> safety_reviewer, founder -> founder |
| `execution_deadline_set_guard()`, `execution_deadline_set_approval_guard()`, `execution_deadline_set_withdrawal_guard()` | BEFORE INSERT |
| `execution_active_deadline_set_id() RETURNS bigint` | STABLE. For `execution_active_incident_policy_id()`: highest version_no ever approved (safety + founder); NULL if withdrawn. Production usability additionally needs `source = 'owner_approved'` (read models) |

Refusals: `escalation deadline set versions must be consecutive per incident policy`,
`creating an escalation deadline set requires ops_lead or safety_reviewer`, `invalid escalation deadlines: ` +
(`unknown incident policy` / `deadlines must be an object keyed by severity level` /
`deadlines must have exactly the policy's severity levels plus "unclassified" as keys` / `deadlines for %s must be a list` /
`each deadline for %s must have exactly to_role and within_min` / `unknown deadline role %s for %s` /
`role %s appears twice for %s` / `within_min for %s / %s must be a whole number of minutes` /
`within_min for %s / %s must be a whole number from 1 to 5256000`);
approval: `approving an escalation deadline set depends on the deployment mode: ...` (asserted first),
`unknown escalation deadline set`, `a withdrawn escalation deadline set cannot be approved`, `slot %s requires role %s`,
`the author of an escalation deadline set cannot approve it`,
`the same person cannot fill two approval slots on one escalation deadline set`,
`a synthetic_demo escalation deadline set cannot be approved in production`;
withdrawal: `a withdrawal needs a reason`, `withdrawing an escalation deadline set requires safety_reviewer or founder`.

## 9. Views (operations/0003; SECURITY INVOKER, ops_app SELECT only)

`operations_production_blockers (code text, entity_ref text, detail text)` — evaluated as production in both modes:
`HOLD_POLICY_NOT_APPROVED` / `hold-policy`; `HOLD_UNRELEASABLE` / hold code; `INSPECTION_TEMPLATE_NOT_APPROVED` /
pattern code; `PERIODIC_INTERVAL_NOT_APPROVED` / pattern code; `ESCALATION_DEADLINES_NOT_APPROVED` / `deadline-set`.
"In use" = the pattern of a non-retired robot's current manifest.

`operations_closeout_runs (run_id, run_code, robot_id, robot_code, visit_id, visit_code, departed_at, closed, closed_at,
stage, custody_kind, custody_verified, assignment_verified, hub_receipt_at, post_visit_inspection_code,
latest_reconciliation_outcome)` — one row per run; raw state, no grace, no time zone (section 8 rules are C's).

## 10. Privileges

ops_app: no INSERT / UPDATE / DELETE / TRUNCATE on `fleet_holdrebaseactivation` (SELECT only);
SELECT + UPDATE (row locks only; real UPDATEs rejected by `reject_mutation`) on `fleet_holdpolicyregistry`;
no UPDATE / DELETE / TRUNCATE on every other new table. The registry row `id = 1` is the only row any Slice 7
migration inserts.

## 11. Settings keys (lead, phase 0)

`ops_closeout_timezone` (IANA name; X-10) and `ops_periodic_due_lookahead_min` (minutes; X-3 DUE_SOON visibility).
Neither is a gate. `seed_guardrails` writes SYNTHETIC demo values (`America/Los_Angeles`, `1440`) only when missing,
in demo mode only. Constants: `fleet.management.commands.seed_guardrails.CLOSEOUT_TIMEZONE_KEY` / `PERIODIC_DUE_LOOKAHEAD_KEY`.

## 12. Change requests and open items (Engineer A)

- **CR-1 (operations_robot_eligibility).** Contract 4.4 places the periodic reasons in the replaced
  `operations_robot_eligibility` view in operations/0003. Not shipped by A: the Slice 5 eligibility oracle and the
  EDGE_CASES table (`tests/test_operations_sql.py`) compare the view row by row with
  `fleet.services.evaluate_eligibility`, whose periodic reasons are Engineer B's (4.5). Shipping the SQL half alone
  breaks those tests. Proposal: B ships the view replacement in a new `operations/0004` together with the Python
  mirror (or the lead moves it), evaluated at `transaction_timestamp()`, using `readiness_periodic_inspection_status`.
- **Minimal shims in B's files (strictly required for existing tests and the demo).** `readiness/evaluator.py`
  `_read_world` adds the signer persons of both relied inspections via the SQL functions above (otherwise every
  authorization whose inspection signers are not crew is refused by `readiness_signer_versions_problem`).
  `fleet/services.py`: `_create_hold` binds through `fleet_hold_binding_policy_id()` after `share_hold_policy_registry`;
  `release_hold` takes `share_settings=True` first. B replaces them with the full implementations.
- **Seed order.** Since passing pre-departure evidence now needs qualified signers in both modes, `seed_readiness`
  would be refused unless the inspector grants exist first. `make seed` therefore runs `seed_guardrails` after
  `seed_planning` and again at the end (idempotent; the deadline set needs the incident policy). Contract 10 says
  "after the existing seeds"; the end state is the same.
- **`fleet.place_hold` refusing `incident` (5.2)** is B's Python change; the seeded R-03 hold uses that path today,
  so `seed_fleet` must move to a non-`place_hold` path (or `report_incident`) when B lands it.

## 13. Integration change log (lead, branch `s7/int`)

Signatures and refusal prefixes in sections 1-11 are unchanged. Two new migrations, both body-only changes:

| Migration | Change | EM ruling |
|---|---|---|
| `operations/0004_s7_eligibility_periodic` | **CR-1 shipped.** `CREATE OR REPLACE VIEW operations_robot_eligibility`: same columns (names, order, types), still `security_invoker = true`, owner-owned, ops_app SELECT only (re-asserted). New element in the "has a manifest" branch, after the configuration holds and before the robot holds (the Python order): `readiness_periodic_inspection_status(r.id, fleet_production_mode(), transaction_timestamp())` -> `Periodic inspection interval unknown for configuration <P>` / `No qualifying periodic inspection on the current configuration` / `Periodic inspection overdue since <YYYY-MM-DD HH24:MI:SS> UTC` / nothing for `due`. `at` = `transaction_timestamp()` (the page snapshot time). Dependent `operations_robot_status` is not re-created. Reverse restores the Slice 5 body | CR-1 approved as operations/0004; operations/0003 untouched |
| `readiness/0008_s7_evidence_time_source` | `readiness_evidence_guard()` body: one time bound, `now_ts := greatest(clock_timestamp(), statement_timestamp())`, for both `observed_at cannot be in the future` and `observed_at is more than 24 hours ago` (texts unchanged). Root cause of the random-world-29 failure: the DEFAULT (`statement_timestamp()`) was compared with a second, later `clock_timestamp()` read, which is lower than the first after a backward step of the database wall clock. `readiness.evidence.record_evidence` stamps the inspection-derived default as `LEAST(submitted_at, statement_timestamp())` inside the INSERT | Release blocker: deterministic time source + explicit boundary test |

Test evidence (real PostgreSQL):

- CR-1 oracle: `tests/test_operations_sql.py::test_edge_case_eligibility` (view == Python `evaluate_eligibility(robot, now())`
  row by row) with new cases `periodic_never_inspected`, `periodic_signer_revoked`, `periodic_no_rule`,
  `periodic_interval_null`, `periodic_rule_withdrawn`, `periodic_synthetic_rule_production`,
  `periodic_synthetic_template_production`, `periodic_overdue`, `engineering_profile_production_with_production_periodic`
  and the production variants of the synthetic-profile cases; `eligible_baseline` is the normal `due` case;
  `combined_order` pins the position. `test_views_equal_python_on_random_world[0..49]` (random worlds now include
  robots without a periodic inspection and production endings), `test_random_worlds_cover_the_variety` requires
  the interval-unknown and never-inspected reasons. B's `test_s7_oracles::test_periodic_eligibility_reason_matches_a_sql_expression` uses the same SQL expression.
- Migrations: fresh database (`migrate` from zero, `makemigrations --check`: no changes; full `make seed` order) and an
  upgraded Slice 6 database (`0a8bdab` tree migrated and seeded, then migrated forward to `s7/int`): after the
  upgrade every robot reads `interval unknown` (fail-closed, section 4.8); after `seed_guardrails` the view equals
  Python for every robot in demo and in production; `operations 0003` / `readiness 0007` reverse and re-apply cleanly.
- Evidence time: `tests/test_s7_integration.py` steps the database clock 1 us behind the statement start (session-local
  `public.clock_timestamp()` in the test transaction): the 0007 guard body refuses the equipment check exactly as in
  the failure; the 0008 body accepts it; boundary `statement_timestamp()` -1 us / = accepted, +1 us refused; a
  caller-supplied future `observed_at` is still refused.
