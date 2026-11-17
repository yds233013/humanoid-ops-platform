# Slice 1 Technical Addendum: Responses to the Six Review Comments

Status: for review · 2026-11-16 · Applies on top of `00_approval_package.md`. Slice 0 mechanisms referenced here are implemented and tested.

## 1. INV-2: serialization behaviour and how it is proven

**The lock rule.** Every command that can change whether a robot may depart takes `FOR UPDATE` on that robot's row. Commands that check departure take `FOR SHARE` on the same row. Each command acquires all of its locks **before** reading any prerequisite.

- **Commands that take `FOR UPDATE` (writers):** `place_hold`, `record_manifest`, `release_hold`, `submit_inspection(FAIL)`, `withdraw_pattern_approval`.
  - The last one locks the pattern row, then each affected robot.
- **Commands that take `FOR SHARE` (checkers):** `authorize_departure`, `depart`.

**Global lock order:** `manifest_pattern` (by id) → `robot` (by id). A future slice extends this to `site` → `person` → `vehicle` → `visit`.

**Why this serializes.** `FOR SHARE` and `FOR UPDATE` conflict. So any writer and any checker on the same robot form a total order, decided by who gets the lock first. This works in READ COMMITTED because each statement takes a fresh snapshot, and the checker's reads run after it holds the lock. So a checker sees every writer that committed before it.

**Required outcomes:**

| Order | Required result |
|---|---|
| Writer gets the lock first and commits; the checker waits, then runs | The checker is **refused** (409, with the reason), with no exceptions |
| Checker gets the lock first and commits; the writer waits, then runs | The departure **stands** (it was valid when it committed). The writer commits, and the hold or manifest records `effective_after_departure = true` and alerts the ops lead |

**How it is proven:**

- **Deterministic tests.** Test-only pause hooks sit inside the command, right after lock acquisition. Both orders are forced explicitly, using two real Postgres connections. One test per writer type × checker type × order.
- **Oracle test, 500 randomized runs.** Each writer stamps `robot.version` (incremented under its lock). Each checker records `robot_version_observed` under its lock. Because the lock totally orders them, version order equals commit order on the robot row. The test asserts: for every **accepted** departure D and every blocking hold H on the same robot, `H.robot_version_after ≤ D.version_observed` ⇒ H had been released at a version ≤ D's. In other words, no departure was accepted after an unreleased blocking hold became effective.
- **Lock order deadlock test.** One command touches robots {A, B} while a concurrent one touches {B, A}. Expect no deadlock (both take locks in id order). A build where the ordering is deliberately broken must surface `40P01`, which proves the test can detect a deadlock.
- **Background worker.** Every test runs with the background worker switched off, which proves enforcement doesn't depend on it.

## 2. Approval requirements as policy-defined slots

```
hold_policy(id, version, status: draft|approved, approved_by[], scope_rules jsonb, created_at)
  requirements per hold source/severity: [{slot: "eng_review_1", roles: ["engineer","engineering_lead"]},
                                           {slot: "eng_review_2", roles: ["engineer","engineering_lead"]},
                                           {slot: "safety", roles: ["safety_reviewer"]}],
  distinct_approvers: true, independent_of_placer: true
hold.policy_id + hold.required_slots (copied at creation; fixed for the life of the hold)
release_approval(id, hold_id, slot, approver_id, role_used, manifest_id, approved_at, evidence_refs[])
  UNIQUE (hold_id, slot)
  UNIQUE (hold_id, approver_id)            -- when distinct_approvers (enforced by trigger reading the policy)
```

- Two independent engineering reviewers means two slots with the same roles. There is no one-per-role limit.
- A trigger on `release_approval` INSERT checks three things:
  - the slot exists on the hold;
  - `role_used` is in the slot's allowed roles and the approver holds an active `RoleGrant` for it at `now()`;
  - if the policy requires independence, the approver is not the hold's placer.
- Approval rows are append-only (the same `reject_mutation` trigger as the audit table). A mistaken approval is not deleted; it's superseded by a `withdrawn_approval` row.

## 3. Hold scopes in Slice 1

**Enforced in Slice 1:**
- `robot`
- `manifest_pattern`

The schema is `CHECK (scope IN ('robot','manifest_pattern'))`. `task`, `site` and `vehicle` scopes arrive with their own domains. The API and UI don't offer them before then.

| Scope | How a robot is checked | Locking | How it invalidates |
|---|---|---|---|
| robot | `hold.robot_id = robot.id AND released_at IS NULL AND blocking` | writer `FOR UPDATE robot` | robot.version bump (trigger) |
| manifest_pattern | `hold.pattern_id = robot.current_manifest.pattern_id …` | writer `FOR UPDATE pattern`, then `FOR UPDATE` on every robot currently on that pattern (id order); checker `FOR SHARE pattern` then `FOR SHARE robot` | bumps each affected robot.version in the same transaction; a robot switched onto a held pattern later is caught because the eligibility query joins on its current pattern |

**Manifest change on a robot:** lock the old pattern and the new pattern `FOR SHARE` (id order), then the robot `FOR UPDATE`.

## 4. Inspection failure: a draft release policy, and incomplete inspections

**How the result is set.** `submit_inspection` computes the result on the server. The submitted result field is ignored.

| Result | When | Effect |
|---|---|---|
| **PASS** | every item on the referenced template version is present and passed; two distinct signers, each with an active qualifying role | none |
| **FAIL** | any blocking item failed | creates a robot hold in the same transaction (INV-1) |
| **INCOMPLETE** | anything else: missing items, a missing signer, the same person twice | no hold, and the check **never** counts as PASS |
| **422, nothing stored** | malformed input: unknown template version, unknown item IDs, the robot's manifest ≠ the template's pattern | the request is rejected |

**The release rule for inspection-sourced holds** (a newer PASS on the current manifest, plus an engineering slot for engineering-owned items) ships as a **draft** `hold_policy`.

- Production setting: `ALLOW_DRAFT_POLICIES=false`. A hold created under a draft policy can't be released until an approved policy version exists and an engineering lead re-baselines the hold.
- Dev/demo: the setting is `true`, and every screen shows a "Draft policy, not approved" banner.

## 5. Runtime database privileges, audit and approval integrity

Already in place and tested in Slice 0:

- separate owner and runtime roles;
- the runtime role owns nothing;
- audit rows are append-only, enforced by grant and trigger;
- the runtime role cannot UPDATE, DELETE or TRUNCATE the audit table, ALTER or DROP tables, DISABLE or DROP triggers, or CREATE tables (7 tests);
- the runtime role isn't a superuser;
- every app table is covered by grants.

Added in Slice 1, with tests run as `ops_app` directly against SQL:

- `hold.released_at` can only be set by a deferred constraint trigger that checks all of the hold's required slots are filled by valid, non-withdrawn approvals on the robot's **current** manifest. Setting it with raw SQL without them → rejected.
- Hold, approval, manifest and pattern-approval rows are immutable apart from the single `released_at` / `released_by` transition. They can't be deleted.
- Inserting an approval for a role the approver doesn't hold → rejected by the trigger.
- **Trust boundary, unchanged:** a superuser or the schema owner can disable triggers. That's for migrations only, and gets an operational control (separate credentials, logged use) when deployed.

## 6. Fleet eligibility vs departure authorization

- Slice 1 exposes `fleet_eligibility(robot) → {eligible: bool, reasons: [...], evaluated_at, manifest_id}`.
  - It means only: **this robot may be considered when planning a visit.**
  - It checks lifecycle = in_service, a base home-use grant for the current pattern, the pattern approved for scope=home, no active blocking hold at robot or pattern scope, and no `unknown` capability fields required for home visits.
- `departure_authorization(visit)` is a separate entity, arriving in a later slice. It adds site, consent, people, vehicle, inspection and recovery plan.
- **UI and API wording:** "Eligible for planning, not cleared to depart". The fleet UI never shows green; eligibility is shown as a neutral badge. A test asserts the API field name and label, and that no fleet endpoint returns a field called `ready`, `cleared` or `authorized_to_depart`.
