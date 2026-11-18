# Slice 3 Contract: Deployment Readiness and Departure Authorization

Frozen by the integration lead before parallel work. Engineers do not change names, signatures, lock order or
file ownership here. Need a change? Stop and report it to the integration lead with the exact reason.

Everything is SYNTHETIC. Nothing here grants real-world clearance. A demo policy can exercise the workflow in
demo mode only; production refuses departure unless a genuinely approved procedure exists.

## 0. Principles

1. **Authorization is not departure.** A `DepartureAuthorization` is a time-limited decision. A
   `DepartureEvent` is an immutable operational fact. Never infer one from the other.
2. **Single use.** One authorization -> at most one departure, enforced by a PostgreSQL unique constraint.
3. **Version-bound, not a boolean.** An authorization stores the exact guard versions it was decided on. It is
   usable only while every one of them is unchanged, it is unexpired, unrevoked and unused.
4. **Unknown blocks.** Missing, stale, unapproved or unknown safety-relevant data is a blocker.
5. **Fail closed in production.** `demo_mode != 'true'` -> only a policy with `source = 'approved_procedure'`
   can authorize, and synthetic capability profiles already make the robot ineligible (Slice 1).
6. **No background workers for correctness.** Expiry and invalidation are computed at read time and
   re-checked under locks at departure.

## 1. File ownership

| Owner | Files (exclusive) |
|---|---|
| Integration lead | `backend/config/**`, `backend/platform_core/**`, `backend/accounts/**`, `backend/fleet/**`, `backend/planning/**`, `backend/templates/**`, `backend/tests/conftest.py`, `backend/tests/fleet_world.py`, `backend/tests/planning_world.py`, `docs/**`, `.github/**`, `docker-compose.yml`, `Makefile`, `README.md`, `backend/readiness/__init__.py`, `backend/readiness/management/**` (seed) |
| Engineer A, database and domain | `backend/readiness/models.py`, `backend/readiness/migrations/**`, `backend/readiness/evidence.py` (policy, evidence and recovery-plan commands), `backend/tests/readiness_world.py`, `backend/tests/test_readiness_db.py`, `backend/tests/test_readiness_evidence.py` |
| Engineer B, readiness engine | `backend/readiness/evaluator.py`, `backend/readiness/gates/**`, `backend/tests/test_readiness_engine.py` |
| Engineer C, authorization and concurrency | `backend/readiness/authorization.py`, `backend/readiness/commands.py`, `backend/tests/test_readiness_authorization.py`, `backend/tests/test_readiness_concurrency.py` |
| Engineer D, operator UI | `backend/readiness/views.py`, `backend/readiness/urls.py`, `backend/readiness/templates/**`, `backend/tests/test_readiness_ui.py` |

Deviation from the EM plan, approved by the lead for speed: the thin policy/evidence/recovery-plan *write
commands* live with Engineer A (`evidence.py`) because they are straight writers over A's schema. Engineer C
owns everything that authorizes, revokes or records departure.

## 2. Already done by the lead (Phase 0, on `main`)

- App `readiness` registered in `INSTALLED_APPS`; `readiness_` tables are flushed by test teardown.
- `planning.Person.version` (bumped by every qualification insert/revoke) and `planning.Visit.version`
  (bumped by EVERY update of the visit row). Migration `planning/0005_guard_versions`.
- Lock order extended (`platform_core/locks.py`):

```
role grants -> sites -> manifest patterns -> robots -> persons -> vehicles -> visits
  -> readiness policies (readiness_readinesspolicy) -> departure authorizations (readiness_departureauthorization)
```
  `lock_guards(..., update_policies=, share_policies=, update_authorizations=, share_authorizations=)`.
- Permissions (`accounts/roles.py`):

| Permission | Roles |
|---|---|
| `readiness.read` | ops_lead, field_operator, engineer, engineering_lead, safety_reviewer, founder |
| `readiness.manage_policy` | engineering_lead |
| `readiness.approve_policy` | engineering_lead, safety_reviewer |
| `readiness.withdraw_policy` | engineering_lead, safety_reviewer |
| `readiness.record_evidence` | ops_lead, field_operator, engineer |
| `readiness.record_recovery_plan` | ops_lead, engineering_lead |
| `readiness.authorize_departure` | ops_lead |
| `readiness.revoke_authorization` | ops_lead, engineering_lead, safety_reviewer, founder |
| `readiness.record_departure` | ops_lead, field_operator |

- Fix: `RoleGrant.valid_from` now defaults to database time (`db_default=Now()`); a host clock a few ms ahead
  of PostgreSQL made brand-new grants briefly inactive (flaky test).

## 3. Schema (Engineer A) - app label `readiness`

All tables append-only unless stated. Withdrawals/voids/revocations are separate one-to-one rows.
DB helper functions available: `has_active_role(uid, role)`, `fleet_setting(key)`, `fleet_fail(msg)`
(raises `check_violation`), `reject_mutation()`.

### ReadinessPolicy
| field | notes |
|---|---|
| `version` | int, unique, consecutive from 1 (trigger) |
| `requirements` | JSON, schema below; validated by a DB trigger (unknown/missing keys rejected) |
| `source` | `synthetic_demo` or `approved_procedure` |
| `notes` | text |
| `created_by`, `created_at` | |
| `lock_version` | int default 1, the guard version; bumped by trigger on approval/withdrawal insert |

`requirements` (exact keys):
```json
{
  "authorization_ttl_min": 90,
  "evidence": [
    {"kind": "pre_departure_inspection", "max_age_min": 240},
    {"kind": "equipment_check", "max_age_min": 240},
    {"kind": "battery_check", "max_age_min": 120}
  ],
  "required_equipment": ["restraint_kit", "recovery_kit", "charger"],
  "recovery_plan_required": true
}
```
`kind` is one of `pre_departure_inspection`, `equipment_check`, `battery_check`. All integers > 0.

**Status** (derived, never stored): `withdrawn` if a withdrawal row exists; else `approved` if both slots
`engineering` and `safety` are approved; else `draft`. **Active policy** (EM decision, migration 0003) = the
highest-version policy that has EVER been fully approved; if that policy is withdrawn there is NO active
policy until a newer version is fully approved (approving its second slot is the explicit activation). There
is never a fallback to an older approved policy. Implemented once in Python (`evidence.current_policy()`) and
once in SQL (`readiness_active_policy_id()`), and tested to agree.

### ReadinessPolicyApproval
`policy`, `slot` (`engineering` needs role engineering_lead, `safety` needs safety_reviewer), `approver`,
`approved_at`. Unique (policy, slot) and (policy, approver). DB trigger rejects: approver without the slot's
active role; approver = policy creator (no self-approval); approval of a withdrawn policy.

### ReadinessPolicyWithdrawal
One-to-one `policy`, `withdrawn_by`, `reason` (non-empty), `withdrawn_at`.

### ReadinessEvidence
| field | notes |
|---|---|
| `code` | `EV-0001` style, from a sequence |
| `visit`, `revision` (planning.VisitRevision), `robot`, `manifest` (fleet.ConfigurationManifest) | DB-checked: `revision.visit = visit`, `revision.robot = robot`, `manifest.robot = robot`, `manifest = robot.current_manifest` at insert, `revision = visit.current_revision` at insert, visit not cancelled |
| `kind` | `pre_departure_inspection` / `equipment_check` / `battery_check` |
| `result` | `pass` / `fail` |
| `inspection` | FK fleet.Inspection, REQUIRED for `pre_departure_inspection` and forbidden otherwise; DB-checked: same robot and manifest, `visit_ref = visit.code`; `result='pass'` requires inspection.result = 'PASS' and complete |
| `details` | JSON. equipment_check: `{"items": {"restraint_kit": "present", ...}}`; battery_check: `{"soc_pct": 85}` (1..100) |
| `observed_at` | when it was observed; must be <= now() and not more than 24h ago at insert |
| `recorded_by`, `recorded_at` | |

Insert bumps `planning_visit.version` (the command holds the visit FOR UPDATE).

### ReadinessEvidenceVoid
One-to-one `evidence`, `voided_by`, `reason`, `voided_at`. Insert bumps the visit version.

### RecoveryPlan
`visit`, `revision`, `manifest`, `on_call_engineer` (planning.Person), `escalation_contact`
(planning.Person, must differ from on_call_engineer), `escalation_path` (text, non-empty),
`required_equipment` (JSON list of strings), `recorded_by`, `recorded_at`. Same revision/manifest DB checks as
evidence. Insert bumps the visit version. No household contact details, only person codes.

### DepartureAuthorization
| field | notes |
|---|---|
| `code` | `DA-0001` style |
| `visit`, `revision`, `robot`, `manifest`, `policy` | the exact things decided on |
| `mode` | `demo` / `production` (from `demo_mode` at issue time) |
| `guard_snapshot` | JSON, exact shape in section 5 |
| `evidence_ids` | JSON list of evidence ids relied on; `recovery_plan` FK (nullable only if the policy does not require one) |
| `issued_by`, `issued_at` (DB time), `expires_at` | `expires_at > issued_at` and `expires_at <= issued_at + policy ttl` (trigger) |

DB trigger on insert rejects: policy withdrawn or not approved; `mode='production'` with policy source
`synthetic_demo`; revision != visit.current_revision; visit status != confirmed; manifest != robot's current
manifest; revision.robot != robot.

### AuthorizationRevocation
One-to-one `authorization`, `revoked_by`, `reason` (non-empty), `revoked_at`.

### DepartureEvent
| field | notes |
|---|---|
| `authorization` | **OneToOne (unique)**: single use |
| `visit`, `robot` | must equal the authorization's |
| `departed_at` | DB time |
| `recorded_by` | |
| `guard_snapshot` | the snapshot re-taken under locks at departure (must equal the authorization's) |

DB trigger on insert rejects: authorization revoked; `clock_timestamp() >= expires_at`; revision no longer the
visit's current revision; visit not confirmed; manifest no longer current; policy withdrawn. The trigger checks
**structural** validity only. Live readiness (holds, consent, qualifications...) is re-evaluated by Engineer C's
command under locks. Do not describe the trigger as enforcing live eligibility.

**Updated by migration 0003 (EM direct-SQL requirement):** the departure trigger now also recomputes the live
guard state from the tables (locking it FOR SHARE in the global order) and requires it to equal the
authorization's snapshot field by field; refuses a demo authorization or synthetic policy when `demo_mode` is
off; and requires the recorder to hold ops_lead or field_operator. What only the command checks (and the DB
does not): evidence freshness beyond `expires_at`, time-lapsed qualification/assessment windows, role grants
of the on-call person, settings other than `demo_mode`, and the "visit crew only" departure rule. After a
departure, a visit can no longer be re-planned or rescheduled (it can still be cancelled).

## 4. Commands

All use `run_command`, existing permission/idempotency/audit patterns, `lock_guards` in the global order,
re-read after locking, and raise `RetryTransaction` if the visit's current revision changed between read and
lock. Return dicts like the existing services.

### Engineer A (`readiness/evidence.py`)
```python
create_readiness_policy(actor, *, requirements: dict, source: str, notes: str = "") -> dict   # readiness.manage_policy
approve_readiness_policy(actor, *, version: int, slot: str) -> dict                          # readiness.approve_policy; locks policy U
withdraw_readiness_policy(actor, *, version: int, reason: str) -> dict                       # readiness.withdraw_policy; locks policy U
record_evidence(actor, *, visit_code: str, kind: str, result: str, details: dict | None = None,
                inspection_code: str | None = None, observed_at: datetime | None = None) -> dict
                # readiness.record_evidence; locks robot S, visit U. entity_id = evidence code
void_evidence(actor, *, evidence_code: str, reason: str) -> dict                             # readiness.record_evidence; robot S, visit U
record_recovery_plan(actor, *, visit_code: str, on_call_code: str, escalation_code: str,
                     escalation_path: str, required_equipment: list[str]) -> dict
                # readiness.record_recovery_plan; locks robot S, persons S, visit U
current_policy() -> ReadinessPolicy | None        # read helper: highest-version approved, not withdrawn
policy_status(policy) -> str                       # 'approved' | 'draft' | 'withdrawn'
```

### Engineer B (`readiness/evaluator.py`), pure read, no locks, no writes
```python
@dataclass
class ReadinessResult:
    ready: bool
    label: str                     # LABELS below
    blockers: list[dict[str, str]] # {"level": "blocked"|"review", "code", "message", "action"} stable codes
    policy_id: int | None
    policy_version: int | None
    evidence_ids: list[int]
    recovery_plan_id: int | None
    manifest_id: int | None
    guard_snapshot: dict[str, Any] # section 5
    expires_at: datetime | None    # latest allowed expiry: min(at + ttl, each evidence observed_at + max_age)

evaluate_readiness(visit: Visit, at: datetime | None = None) -> ReadinessResult
take_guard_snapshot(visit: Visit) -> dict[str, Any]    # section 5; reads current rows only
```
Gates, in order (each failure adds a blocker, never stops early):
1. Visit is `confirmed` with a current revision (`VISIT_NOT_CONFIRMED`).
2. Planning is still feasible NOW: `planning.services.live_feasibility(visit)` == feasible; copy its blockers
   (prefix codes `PLANNING_`). This covers site assessment, consent, robot eligibility/holds, qualifications,
   vehicle and conflicts.
3. Every planned reservation for the current revision is still active (`RESERVATIONS_MISSING`).
4. Policy: a current approved policy exists (`POLICY_MISSING`); production mode needs
   `source='approved_procedure'` (`POLICY_NOT_APPROVED_PROCEDURE`).
5. Evidence per policy requirement: latest non-voided evidence of that kind for the current revision AND the
   robot's current manifest, `result='pass'`, `observed_at >= at - max_age_min`
   (`EVIDENCE_MISSING`, `EVIDENCE_FAILED`, `EVIDENCE_STALE`, `EVIDENCE_WRONG_CONFIGURATION`).
6. Battery: `soc_pct >= approved capability profile's min_departure_soc_pct`; unknown minimum blocks
   (`BATTERY_MIN_UNKNOWN`, `BATTERY_LOW`).
7. Equipment: the equipment check covers `policy.required_equipment` + recovery plan's `required_equipment`,
   each `present` (`EQUIPMENT_MISSING`).
8. Recovery plan for the current revision and manifest when required (`RECOVERY_PLAN_MISSING`).
9. Expiry must be in the future (`EXPIRY_NOT_POSITIVE`).

Labels: `"Ready to request authorization"` / `"Readiness incomplete"`. Never "cleared".

### Engineer C (`readiness/commands.py`, `readiness/authorization.py`)
```python
authorize_departure(actor, *, visit_code: str, idempotency_key: str | None = None) -> dict
    # readiness.authorize_departure
    # locks: site S, pattern S, robot S, persons S (lead, second, recovery-plan persons), vehicle S,
    #        visit U, policy S  -> re-read, RetryTransaction if revision changed, evaluate_readiness,
    #        refuse (Conflict, with blockers) unless ready, insert DepartureAuthorization with snapshot.
record_departure(actor, *, authorization_code: str, idempotency_key: str | None = None) -> dict
    # readiness.record_departure; actor must be ops_lead or the revision's lead/second person
    # locks: same as authorize + authorization U. Re-take the guard snapshot: must EQUAL the stored one;
    # re-run evaluate_readiness: must be ready; not expired/revoked/used. Insert DepartureEvent.
revoke_authorization(actor, *, authorization_code: str, reason: str) -> dict
    # readiness.revoke_authorization; locks visit U, authorization U
authorization_status(auth) -> dict   # advisory, unlocked:
    # {"state": "usable"|"expired"|"revoked"|"invalidated"|"used", "reasons": [...]}
    # invalidated = any guard in the snapshot differs from now (names which one) or policy superseded/withdrawn
```
Checkpoints for tests: `lock_guards(label="readiness.authorize_departure")` and
`label="readiness.record_departure"`, plus `checkpoint("read:readiness.authorize_departure")` and
`checkpoint("read:readiness.record_departure")` after the first read, before locks.

### Engineer D (`readiness/views.py`, `urls.py`, templates)
Routes mounted by the lead at `/readiness/`:
- `/readiness/` list of upcoming confirmed visits with readiness label.
- `/readiness/visits/<code>/` checklist: gate results, blockers + actions, evidence history, recovery plan,
  authorizations with live `authorization_status`, departure events.
- POST `/readiness/visits/<code>/evidence/`, `/void/`, `/recovery-plan/`, `/authorize/`;
  `/readiness/authorizations/<code>/depart/`, `/revoke/`.
- `/readiness/policies/` list + create/approve/withdraw.
Views only call the commands above. Labels distinguish: Planning feasible / Readiness incomplete / Ready to
request authorization / Authorization issued (expires HH:MM) / Authorization expired / invalidated /
revoked / Departure recorded. Persistent banner "SYNTHETIC DEMO - not real-world clearance" when demo mode.
No green "cleared to depart" anywhere.

## 5. Guard snapshot (exact shape)

```json
{
  "visit_id": 1, "visit_version": 7, "revision_id": 3, "visit_status": "confirmed",
  "site_id": 1, "site_version": 4,
  "robot_id": 1, "robot_version": 9, "manifest_id": 2,
  "pattern_id": 1, "pattern_version": 5,
  "person_versions": {"3": 2, "4": 2},
  "vehicle_id": 1,
  "policy_id": 2, "policy_lock_version": 3
}
```
`person_versions` keys are person ids as strings: lead, second, and recovery-plan persons. Equality of the
whole dict is the invalidation test. Every change that matters bumps one of these: holds/manifest/lifecycle
(robot), pattern decisions (pattern), consent/assessment/headcount (site), qualifications (person),
reschedule/cancel/evidence/recovery plan (visit), policy approval/withdrawal (policy). A newer approved
policy is detected by `policy_id` != current policy id.

## 6. Test infrastructure

- `tests/readiness_world.py` (Engineer A): `build_readiness(prefix)` on top of `build_planning`: publishes and
  approves capability profile v2 for P-A with `min_departure_soc_pct=60`, creates + approves a
  `synthetic_demo` policy with the JSON above, plans + confirms a visit, records a passing pre-departure
  inspection (fleet `submit_inspection` with `visit_ref`), equipment check, battery check and recovery plan.
  Returns the world with `w.visit` and helpers. B, C, D build on it.
- Each worktree uses its own database: `DB_NAME=ops_s3_<letter>` (test DB `test_ops_s3_<letter>`). Never run
  tests against another engineer's database.
- Direct-SQL tests connect as `ops_app` (`app_role_conn`).
- Gate before handing back: `ruff check`, `ruff format --check`, `mypy .`, `makemigrations --check`, full
  `pytest`.

## 7. As built by Engineer A (commit 473fe38) - B, C, D code against this

Models in `readiness.models` (reverse names in brackets):
- `ReadinessPolicy`: version, requirements, source, notes, created_by, created_at, lock_version
  [approvals, withdrawal, authorizations]
- `ReadinessPolicyApproval`: policy, slot, approver, approved_at
- `ReadinessPolicyWithdrawal`: policy (1:1 `withdrawal`), withdrawn_by, reason, withdrawn_at
- `ReadinessEvidence`: code (EV-0001), visit [`readiness_evidence`], revision, robot, manifest, kind, result,
  inspection, details, observed_at (DB default), recorded_by, recorded_at
- `ReadinessEvidenceVoid`: evidence (1:1 `void`), voided_by, reason, voided_at
- `RecoveryPlan`: visit [`recovery_plans`], revision, manifest, on_call_engineer, escalation_contact,
  escalation_path, required_equipment, recorded_by, recorded_at
- `DepartureAuthorization`: code (`nextval('readiness_authorization_code_seq')`, format DA-0001), visit
  [`authorizations`], revision, robot, manifest, policy, mode, guard_snapshot, evidence_ids, recovery_plan,
  issued_by, issued_at (forced to clock_timestamp() by trigger), expires_at
- `AuthorizationRevocation`: authorization (1:1 `revocation`), revoked_by, reason, revoked_at
- `DepartureEvent`: authorization (1:1 `departure`), visit [`departures`], robot, departed_at (forced to
  clock_timestamp()), recorded_by, guard_snapshot
- Constants `EVIDENCE_KINDS`, `POLICY_SLOTS`. `readiness.evidence` exports the six commands,
  `current_policy()`, `policy_status()`, `validate_requirements()`.

Trigger facts that matter to C:
- The authorization trigger also requires: the recovery plan (when the policy needs one) for the same visit,
  revision and manifest; every `evidence_ids` entry non-voided and for the same visit/revision/manifest;
  `guard_snapshot` keys `visit_id, revision_id, robot_id, manifest_id, policy_id` matching the row; `mode`
  matching the `demo_mode` setting.
- **Take time from PostgreSQL** (`SELECT clock_timestamp()`), not the app clock, when computing `expires_at`;
  the trigger compares against DB time and the TTL check is exact.
- Departure trigger requires `guard_snapshot` equal to the authorization's, and locks robot S -> visit S ->
  policy S -> authorization S (all already held by the command).

Test world `tests/readiness_world.py`: `build_readiness(prefix)` -> `w.visit` (confirmed, all evidence and a
recovery plan), `w.policy_version`, `w.evidence` (kind -> code), `w.inspection`, `w.recovery_plan_id`,
`w.lead2`; helpers `make_policy`, `new_confirmed_visit`, `record_inspection`, `record_all_evidence`,
`requirements(**overrides)`; constants `REQUIREMENTS`, `EQUIPMENT_ALL_PRESENT`, `RECOVERY_EQUIPMENT`,
`MIN_DEPARTURE_SOC = 60`, `BATTERY_SOC = 85`.

Left to B's gates (not enforced in the DB): an equipment check marked `pass` with an item `missing`; whether
the on-call person is actually an engineer (report as `review`, not `blocked`, since roles live on users).

## 8. Authorization write boundary (migration readiness/0005, EM closure condition)

The runtime role has **no INSERT** on `readiness_departureauthorization`. The only way to issue one is
`readiness_issue_authorization(p_visit_id, p_issued_by, p_expires_at, p_evidence_ids, p_recovery_plan_id,
p_guard_snapshot, p_mode) RETURNS TABLE (id, code)`: SECURITY DEFINER (schema owner), `search_path = public,
pg_temp`, EXECUTE for ops_app only. `authorize_departure` calls it after its locks and `evaluate_readiness`;
the Python evaluator remains the primary, explained decision, the function is the database-enforced minimum.
It re-proves in SQL: issuer holds ops_lead; visit confirmed, current revision, not departed; snapshot equals
the live guards (`readiness_guard_problem`, shared with the triggers); active policy, mode from demo_mode,
production needs approved_procedure; robot in service, pattern approved_home, base authorization, no blocking
robot/pattern hold, approved capability profile with known planning fields (synthetic refused in production);
consent count on the current form covering the revision's scopes >= expected adults (unknown blocks); lead
field_lead, second field_operator/field_lead, both robot_handler (window: see 0006 below); latest
recovery plan (required when the policy says so); latest evidence per policy kind passing and fresh; battery
>= profile minimum (unknown blocks); required equipment present; `evidence_ids` exactly the relied set;
`now < expires_at <= least(now + ttl, evidence observed_at + max_age)`.

Migration readiness/0006 (EM follow-up) adds the planning gates: site assessment (approved, non-superseded,
valid on the site-local visit date; not_eligible and eligible_restricted refuse; no excluded task), the live
plan timeline (same formula as planning), every live-plan reservation of the current revision active and
covering its interval, an unreleased vehicle trip carrying the robot (stricter than the evaluator), vehicle
capacity and restraint kit known, lead and second distinct, and qualification windows identical to planning
(inspection start, or loading start for robot_handler, through unloading end) replacing 0005's simplified
window. The only evaluator item not re-proven is the on-call engineer role (a review item); the database also
cannot know the human behind ops_app. `tests/test_readiness_authz_boundary.py` maps every evaluator and
planning blocker code to its SQL refusal.
