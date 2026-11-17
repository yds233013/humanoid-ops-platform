# Implementation Approval Package: Reconciliation and First Build Gate

Status: for review · 2026-11-16 · Synthetic context only. Nothing here describes a real company's robot.

**Inputs being reconciled:**
- **S:** Spreadsheet Operating System v1.0 (`~/humanoid-fleet-ops`, 84/84 scenario checks).
- **E:** Engineering Design Review v0.1 + v0.2 (architecture, locking).
- **D:** Domain Engineering Review v1.0 (robotics evidence).

**Confidence labels:**
- **V:** verified in a cited source.
- **I:** inferred.
- **A:** an assumption pending company input.

---

## 1. Contradictions and their resolutions

| # | Contradiction | Sources | Resolution |
|---|---|---|---|
| C1 | A robot hold can only come from an incident | S says only incidents; E and D allow holds from any source | **Hold is its own entity.** Its source can be an inspection, an incident, a work order, a release, or manual (engineering). Incidents create holds; they are not the only way. The spreadsheet is being fixed to match (in progress). |
| C2 | A failed readiness check blocks the visit but leaves the robot ASSIGNABLE until someone files an incident (fail-open interval) | S Scenario A | **In the same transaction**, a blocking inspection FAIL creates a robot-scope blocking Hold with source = that inspection. No second step. Release follows policy (default: a newer PASS inspection on the current manifest **and** engineering sign-off when the failed item is engineering-owned). INV-1. |
| C3 | Status vocabulary: v0.1 "Robot status" vs S "Gate status" vs D "4 lifecycle states" | E, S, D | D wins. Stored: `lifecycle` (commissioning, validation, in_service, retired) and custody. Calculated: authorization (with reasons). "ASSIGNABLE / HOLD / QUARANTINE" become display labels for the calculated result. |
| C4 | Configuration = one software version string | S has `sw`; E has a release + config hash; D has a full manifest | **D wins:** an immutable `ConfigurationManifest`. S keeps a single version column as a simplification. |
| C5 | Incident category | S has one category; D has event type + fault category + severity | **D wins:** three separate fields; fault observations are a child table. |
| C6 | Vehicle | S has none; E has a reservation; D has one robot per trip | `VehicleTrip` with N cargo rows; policy `max_robots_per_trip` = 1 (setting). |
| C7 | Severity rules | S and E hard-coded S0–S3; D says policy | **Policy as versioned data.** S0–S3 ships only as a *draft* policy for the fixtures. |
| C8 | Return-to-service approvers | S fixed them per severity; E/D use a policy fixed at hold creation | E/D wins. S's rules become the draft policy contents. |

---

## 2. Requirements matrix

**Key:**
- **Pri:** P0 = Slice 0/1 · P1 = Slices 2–4 · P2 = later.
- **Abbreviations:** M = manifest · RTS = return-to-service · CMD = command.

| ID | Operational scenario | Source · conf | Subsystem | Required data | Command / transition | Invariant | Actor | Approval | Failure behaviour | Acceptance test | Pri | Unknowns |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| R-01 | Register a robot | S, E · I | Fleet | serial, hardware rev | CMD `register_robot` → lifecycle=commissioning | ID unique; never deleted | Ops lead | — | duplicate serial → 409 | register twice → second refused, one audit event | P0 | — |
| R-02 | Record the exact configuration | D · V (Figure, Agility) / I | Config | hw rev, firmware set, autonomy ver, model vers, safety-config hash, calibration ref, provenance (manual \| robot-reported) | CMD `record_manifest` → new immutable M; robot.current_manifest | Rows immutable; exactly one current per robot | Engineer | — | missing required fields → 422 | update an M row → DB error; second current → constraint error | P0 | Can the robot report it? (B-02) |
| R-03 | Approve a configuration for homes | E, D · I | Config | pattern (hw rev, release, firmware set), scope (home \| engineering_only) | CMD `approve_manifest_pattern`; `withdraw_pattern_approval` | Only scope=home counts for authorization | Engineering lead | role = engineering_lead | non-engineer → 403 | engineering_only pattern → authorization reason "pattern not approved" | P0 | Release process (B-06) |
| R-04 | Know the robot's capabilities and limits | D · V (G1 stop→fall, Stretch runstop) | Capability | CapabilityProfile per pattern: stop_behaviour, lift method/crew, transport posture, max runtime, min departure SoC, charge turnaround, approved tasks; each field can be `unknown` | CMD `publish_capability_profile` (versioned) | A field needed for an operation that is `unknown` → authorization reason "capability unknown: X" | Engineering + safety | two named approvers | partial profile allowed; it blocks only what depends on it | stop_behaviour unknown → robot not authorized for visits | P0 | All values (B-01, B-03, B-04) |
| R-05 | Put a robot on hold for any reason | E, D · V/I | Holds | scope (robot \| pattern \| task \| site \| vehicle), blocking/limiting, source type+ref, reason, policy version | CMD `place_hold` | Active blocking hold ⇒ not authorized, at once (same transaction) | Ops, engineering, system | placing never needs approval | — | hold → authorization false within the same transaction; under concurrency see INV-2 | P0 | — |
| R-06 | A failed check blocks the robot immediately | S Scenario A gap · I | Readiness/Holds | inspection id, failed items | CMD `submit_inspection` (FAIL) ⇒ `place_hold(source=inspection)` in the same transaction | INV-1 | System | — | if hold creation fails, the inspection rolls back too | FAIL → robot unauthorized before any incident exists | P0 (hold side) / P1 (inspection UI) | Checklist content (B-05) |
| R-07 | Release a hold | E, D · I | Holds/RTS | approvals: role, person, time, manifest_id, evidence refs | CMD `approve_release`, `release_hold` | Released only if the policy fixed at hold creation is satisfied by approvals on the **current** M; distinct people; append-only | Per policy | per policy | unsatisfied → 409 naming what's missing | approve, change M, release → refused (stale approval) | P0 | Who approves (B-07) |
| R-08 | Authorization is calculated, never typed | S, E · I | Fleet | — | query `authorization(robot, at)` → {ok, reasons[]} | No writable "status" column | — | — | — | setting any display field never changes authorization | P0 | — |
| R-09 | Every change is traceable | E · I | Audit | actor, command, entity, before/after, request id, reason | every CMD writes an `audit_event` in its transaction | audit rows INSERT-only (DB grant + trigger) | System | — | audit write fails → command fails | UPDATE/DELETE on audit → error | P0 | — |
| R-10 | Authenticated, role-checked commands | E · I | Platform | user, role grants with validity | CMD layer checks the policy before executing | No command without an actor | — | — | 401/403 + audit of denial | role matrix test over every command | P0 | IdP (B-09) |
| R-11 | No double-booking of robots, people, vehicles | S F1/F2, E · I | Scheduling | reservation tstzrange incl. turnaround | CMD `plan_visit` | INV-3 (exclusion constraint) | Ops | — | 409 with the conflicting reservation | 50 parallel plans → 1 succeeds | P1 | Turnaround time (B-04) |
| R-12 | Home eligible + consent | S G1, E · I/⚠legal | Sites | assessment (valid_until), consents | gate check at plan/authorize/depart | — | Ops / Customer Ops | legal owns forms | — | withdraw consent → depart refused | P1 | Consent form (B-10) |
| R-13 | Departure re-checks everything | E v0.2 · I | Visits | authorization snapshot + version stamps | CMD `authorize_departure`, `depart` | INV-2 | Ops lead / field lead | — | stale → 409 REAUTHORIZE | interleaving tests | P1 | — |
| R-14 | Vehicle trip with custody | D · I | Logistics | trip, cargo rows, custody events | CMD `load_robot`, `depart_trip`, `arrive`, `return`, `check_in` | cargo ≤ max_robots_per_trip; custody single-valued | Field | — | — | load 2 with max=1 → refused | P1 | Transport limits (B-03) |
| R-15 | On-site check before work | S G4, D · I | Visits | site check, adults present vs consented | CMD `start_active` | needs SiteCheck=Go | Field lead | — | refused | start without check → 409 | P1 | — |
| R-16 | Incident = event + fault + severity | D · I | Incidents | event type, fault observations (suspected/confirmed), severity, M ref | CMD `report_incident` (idempotent) | M frozen at report; policy signals → automatic holds | Anyone | severity downgrade per policy | duplicate key → original returned | report "fall" → robot hold in the same transaction | P1 | Policy v1 (B-07) |
| R-17 | History preserved per visit | E, D · I | Audit/history | visit ↔ M, custody, inspections, incidents, approvals | query `robot_history`, `visit_dossier` | INV-5 | — | — | — | dossier rebuilds exactly after manifest changes | P0 (robot) / P1 (visit) | — |
| R-18 | Recovery plan per visit | D · V (Waymo) | Visits | recovery plan ref | authorize requires it | — | Ops lead | — | refused | missing → refused | P1 | B-01 |
| R-19 | Maintenance counters and work orders | D · I | Maintenance | hours, work orders, part serials | CMD `open_work_order` (blocking → hold) | completing a work order never releases a hold | Engineer | — | — | complete WO → hold still active | P2 | Intervals (B-08) |
| R-20 | Remote sessions recorded | D · V (1X, Starship, Waymo) | Remote | session, operator, mode, consent ref | CMD `start_remote_session` | needs consent scope | Remote operator | — | refused | no consent → refused | P2 | Is there teleop? (B-11) |

---

## 3. Initial domain model

Entities marked **(Slice 1)** are built first. The others are specified now and built in their own slices.

```
User ─< RoleGrant
Robot (Slice 1) ─< ConfigurationManifest (Slice 1) >─ ManifestPattern (Slice 1) ─< PatternApproval (Slice 1)
ManifestPattern ─< CapabilityProfile (Slice 1, versioned)
Robot ─< Hold (Slice 1; scope robot|pattern|task|site|vehicle) ─< ReleaseApproval (Slice 1)
HoldPolicy (Slice 1, versioned data) ─< Hold.policy_version
Robot ─< CustodyEvent (Slice 1: hub-only events; full in P1)
Robot ─< RobotAuthorizationGrant (Slice 1: base home-use grant, bound to pattern)
AuditEvent (Slice 0) · IdempotencyKey (Slice 0) · Outbox (Slice 0, notifications only)
--- P1 ---
Site ─< SiteAssessment ; Site ─< Consent ; Visit >─ Site ; Visit ─< Reservation >─ Resource(robot|person|vehicle)
Vehicle ─< VehicleTrip ─< TripCargo >─ Robot ; Person ─< Qualification
Visit ─< ReadinessInspection (manifest_id) ; Visit ─< DepartureAuthorization ; Visit ─ SiteCheck ; Visit ─ VisitReport
Incident ─< FaultObservation ; Incident >─ Investigation ; Incident.manifest_id ; MaintenanceWorkOrder ; BatteryPack* ; RemoteSession*
```

`*` = modelled only if B-04 / B-11 confirm removable packs or teleoperation.

### Key constraints (Slice 1)

| Table | Constraint |
|---|---|
| robot | `serial` unique; `lifecycle` CHECK in 4 values; no DELETE grant |
| configuration_manifest | immutable (trigger blocks UPDATE/DELETE); `content_hash` unique per robot sequence; partial unique `(robot_id) WHERE is_current` |
| pattern_approval | append-only; the current status is the latest row |
| capability_profile | immutable versions; partial unique current per pattern |
| hold | `CHECK num_nonnulls(robot_id, pattern_id, task_code, site_id, vehicle_id) = 1`; `CHECK (source_type='manual') = (source_ref IS NULL)`; `released_at` set once (trigger); deferred constraint trigger: release satisfies policy |
| release_approval | unique `(hold_id, role)`; approver ≠ other approvers on the same hold; stores `manifest_id` |
| audit_event | INSERT only (REVOKE UPDATE, DELETE + trigger) |
| robot.version | bumped by triggers on manifest, hold, authorization-grant, pattern-approval changes (pattern changes bump `fleet_policy.version`) |

---

## 4. The five critical invariants

| INV | Statement | DB mechanism | Transaction boundary | Executable tests |
|---|---|---|---|---|
| **INV-1** | A failed blocking inspection makes the robot unavailable for new departures immediately, with no incident needed | `submit_inspection` FAIL inserts the inspection **and** a Hold(source=inspection) in one transaction; robot.version bumped by trigger | lock robot FOR UPDATE → insert inspection → insert hold → audit → commit | (a) FAIL → `authorization(robot).ok=false`, reason cites the inspection; (b) a hold insert failure (injected) → inspection rolled back as well; (c) a concurrent `authorize_departure` with a FOR SHARE lock either completes before (and depart is then refused) or after (refused) |
| **INV-2** | A new hold or configuration change prevents departure under every interleaving | guard-row versions (v0.2): writers FOR UPDATE, checkers FOR SHARE, global lock order; full re-check at depart | as in v0.2 §1.4 | barrier-forced interleavings × {hold, manifest, pattern withdraw}; 1,000 randomized runs; worker disabled; invariant query: no depart with `robot_version_seen` ≥ the version of an unreleased blocking hold |
| **INV-3** | No conflicting visits or vehicle trips | `EXCLUDE USING gist (resource_id WITH =, during WITH &&) WHERE (released_at IS NULL)` on reservations; trip cargo count check | single INSERT, database decides | 50 parallel plans for one robot → 1 commit; cargo > max → refused |
| **INV-4** | No release without current, configuration-specific approvals | deferred constraint trigger checks the policy fixed at hold creation against approvals whose `manifest_id` = current manifest; approvers distinct, role valid at signing | lock robot FOR UPDATE → validate → set released_at → audit | approve then change manifest → release refused; same person twice → refused; expired role grant → refused; raw-SQL release bypassing the service → trigger refuses |
| **INV-5** | History preserved | immutable manifests, append-only holds/approvals/audit/custody; visit and incident store `manifest_id` at the time | — | change manifest after a hold → robot history shows the hold against the old manifest; deletes refused; dossier equals a recorded snapshot |

INV-1, INV-2 (hold and manifest parts), INV-4 and INV-5 (robot scope) are **in Slice 1**. INV-3 and the visit parts land in P1.

---

## 5. Hub-to-home-to-hub logistics model (P1; specified now)

```
Visit: planned → confirmed → authorized ─┐
VehicleTrip: planned → loading → in_transit_out → on_site → in_transit_back → unloading → closed
Robot custody: hub ─load→ vehicle(trip) ─unload→ site(visit) ─load→ vehicle ─unload→ hub ─check_in→ post-visit inspection → turnaround (charge | maintenance)
```

| Step | Command | Preconditions | Unknown physical constraint (blocks only that step) |
|---|---|---|---|
| Transport authorization | `authorize_departure` | gates, qualified crew, vehicle kit, recovery plan | — |
| Loading | `load_robot` | trip planned; cargo ≤ max; crew size ≥ profile.lift_crew | lift method and crew (B-03), restraint (B-03), transport posture and state of charge (B-04) |
| Departure | `depart_trip` | full re-check (INV-2) | — |
| Arrival / site authorization | `arrive`, `submit_site_check` | — | — |
| Deployment | `start_active` / `stop_active` | SiteCheck = Go | stop behaviour (B-01) |
| Return | `load_robot` (return), `depart_trip` | — | — |
| Hub check-in | `check_in` | custody vehicle → hub | — |
| Post-visit inspection | `submit_inspection(kind=post)` | — | checklist (B-05) |
| Turnaround | reservation tail = profile.charge_turnaround | — | charge time (B-04) |

---

## 6. Slice 0 and Slice 1 plan

### Slice 0: foundation

**Repository layout:** `backend/` (Django 5 + DRF), `frontend/` (React + TS + Vite), `docs/`, `docker-compose.yml`, `.github/workflows/ci.yml`.

| Ticket | Acceptance criteria |
|---|---|
| S0-1 Repo + env | `docker compose up` brings up Postgres 16 + the app; `uv` locks Python dependencies; `make test` works from a fresh clone |
| S0-2 Migrations | Django migrations plus raw-SQL migrations for triggers and grants; `makemigrations --check` passes in CI |
| S0-3 Auth + roles | Session login (local users in dev; OIDC adapter interface for later); roles ops_lead, field_operator, engineer, engineering_lead, safety_reviewer, founder, admin; `RoleGrant` has validity dates |
| S0-4 Audit | `audit_event` INSERT-only, enforced by grant + trigger; a test proves UPDATE/DELETE fail |
| S0-5 Command layer | `@command` wrapper: authenticate → authorize → transaction → lock helper (`lock_guards`, global order) → handler → audit → outbox; idempotency keys |
| S0-6 Test harness | pytest + pytest-django against real Postgres; concurrency helper using two DB connections with barriers |
| S0-7 CI | GitHub Actions: ruff, mypy (domain), tests with a Postgres service, migration check |
| S0-8 Fixtures | Synthetic robots R-01..R-05 matching the spreadsheet dataset |

**Done =** a fresh clone starts, migrations run, tests pass, CI config is valid, and an authenticated command produces an auditable database change.

### Slice 1: fleet core

| Ticket | Acceptance criteria |
|---|---|
| S1-1 Robot registry | register, lifecycle transitions (commission → validation → in_service → retired) with role checks; audit |
| S1-2 Manifests + patterns | record immutable manifests with provenance; approve or withdraw patterns (scope home / engineering_only) |
| S1-3 Capability profiles | versioned, two approvers; `unknown` allowed and reported as an authorization reason |
| S1-4 Holds + policy | place hold (any scope/source); HoldPolicy versions; release only with policy-satisfying, manifest-bound approvals (INV-4) |
| S1-5 Inspection-fail hold | minimal `submit_inspection` (result + failed items) to prove INV-1, without the full checklist UI |
| S1-6 Authorization query | `authorization(robot)` → ok + reasons; never stored |
| S1-7 History | robot timeline from the audit, manifest and hold tables |
| S1-8 Minimal UI | Fleet list (status + reasons), robot detail (timeline, holds, place/release hold, manifest); release shows what's missing. No dashboard |
| S1-9 Tests | invalid transitions, the role matrix, INV-1/2(hold+manifest)/4/5 concurrency tests |

---

## 7. Blockers that need real company information

The Slice 0/1 **code** isn't blocked: unknown values are modelled explicitly. The blockers below stop real operational use.

| ID | Need | Blocks |
|---|---|---|
| B-01 | Stop / power-loss / link-loss physical behaviour | authorization for visits (capability `unknown`) |
| B-02 | Can the robot report its manifest and telemetry? | manifest provenance = robot_reported |
| B-03 | Mass, dimensions, lift crew, restraint, max robots per trip | loading |
| B-04 | Battery: removable?, runtime, minimum state of charge, charge turnaround, storage | turnaround, BatteryPack entity |
| B-05 | Inspection checklist items per configuration | full inspection UI |
| B-06 | Release process and approval roles | pattern approval roles |
| B-07 | Severity and hold-release policy owners and content | HoldPolicy v1 (draft only until then) |
| B-08 | Maintenance intervals | work orders |
| B-09 | Identity provider, cloud, issue tracker | SSO, deployment, ticket sync |
| B-10 | Consent forms, recording position | Sites slice |
| B-11 | Teleoperation existence and model | RemoteSession |
