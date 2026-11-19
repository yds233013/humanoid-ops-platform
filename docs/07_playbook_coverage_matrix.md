# Playbook Coverage Matrix: Field Operations Playbook v1.0 vs Platform Slices 1-6

| | |
|---|---|
| **Status** | DRAFT for EM review |
| **Date** | 2026-11-19 |
| **Platform baseline** | `slice-6-closed` (`cfb8d69`, branch `s7/matrix`) |
| **Source playbook** | `~/humanoid-fleet-ops/source/02_playbook.md` v1.0 (sections 1-19), read with `01_data_model.md` (wins on conflicts) and `03_forms.md` (F1-F5) |
| **Scope** | Review only. The playbook is not rewritten here and no code is changed. Everything is SYNTHETIC. |

**Summary.** The playbook was cut into 114 procedures (P-01 to P-114). Each was checked against the code of Slices 1-6,
not only against the contracts. **29 are implemented platform controls**: the platform refuses the wrong action,
and for most of them PostgreSQL refuses it too, even for direct SQL. **47 are partial**: software enforces part of
the procedure and people do the rest. **23 are human procedures**: the platform records them at most, and often
does nothing. **15 are company-specific requirements** that need validation before any real deployment. The
departure chain is the strongest part. Home assessment, consent, two-person crew, fleet eligibility, readiness
evidence, single-use authorization and departure are all re-proven in the database. Custody, the site check,
"every incident holds the robot" and return reconciliation are also strong. The weak areas are these. (a) Hold
release after an incident has no production path, and its approvers do not match the playbook's severity-based G5.
(b) Periodic inspection, fleet-wide task approval and notification timing exist only in the playbook's
spreadsheet logic, and nothing in the platform enforces them. (c) Customer communication and end-of-day reporting
are fully human, with no record. 24 divergences and 16 ranked gaps are listed below.

---

## 1. Legend

| Category | Meaning |
|---|---|
| **IMPL** (Implemented platform control) | Software refuses the wrong action. The mechanism is cited. **[DB]** = enforced by PostgreSQL (trigger, constraint, privilege or SECURITY DEFINER function), so it holds even for direct `ops_app` SQL. **[APP]** = enforced only by the Python command layer (`run_command` permission check, service code). |
| **PART** (Partial) | Software enforces part of the procedure. People do the rest, and the row says which part. |
| **HUMAN** (Proposed human procedure) | People must do it. The platform at most records it (**[REC]**) or does nothing (**[NONE]**). |
| **VAL** (Company requirement / validation) | A value, procedure or policy the company must supply or approve before real use (playbook **[A]**, **[ENG]**, **[LEGAL/SAFETY]**, decision-rights items). The platform's behaviour until it is supplied is stated. |
| **[READ]** | A display or exception in the Slice 5 command center or the Slice 6 capacity report. It is visible but never blocks anything. |

Validation owners: **ENG** engineering, **SAFETY** safety reviewer, **LEGAL** counsel, **OPS** ops lead, **FOUNDER**.

Platform role names used below (`backend/accounts/roles.py`): `ops_lead`, `field_operator`, `engineer`,
`engineering_lead`, `safety_reviewer`, `founder`, `admin`. Every state change goes through
`platform_core.commands.run_command`. It checks the permission inside the transaction and writes an audit row
that the runtime role can only INSERT.

---

## 2. Coverage matrix

### Section 1: The rule, principles, decision rights

| ID | Playbook step | Cat. | What the platform enforces (mechanism, where) | What stays human | Validation needed | Gap / note |
|---|---|---|---|---|---|---|
| P-01 | §1 Rule: no departure unless G1, G2, G3 recorded as passed | IMPL | [DB] Runtime role has no INSERT on `readiness_departureauthorization` (`readiness/migrations/0005_authorization_write_boundary.py`). The only way in is `readiness_issue_authorization(...)` (SECURITY DEFINER). It re-proves the assessment, consent, crew, eligibility, evidence, battery, equipment and recovery plan (0005 + `0006_issue_function_planning_gates.py`). `readiness_departure_guard` (0003) recomputes the live guards. `uniq_departure_per_visit` (0004) allows one departure per visit. [APP] `readiness.commands.authorize_departure` / `record_departure` re-run `evaluate_readiness` under locks. | Whether the recorded evidence is true | ENG/SAFETY: real readiness policy (`approved_procedure`) | Platform adds a fourth step the playbook lacks: an explicit ops-lead **authorization** (see D-02) |
| P-02 | §1 Rule: no operating in a home until G4 | PART | [DB] `execution_session_guard` (`execution/migrations/0002_db_enforcement.py`): a session `start`/`resume` needs the run's latest SiteCheck = `go`, custody at this site and verified, no open interruption, no blocking hold, and consents ≥ adults present | The platform records sessions. It cannot stop a robot from moving ("does not control robots", README) | ENG: E3 | Enforcement is on the record, not on the robot |
| P-03 | §1 Rule: a held robot comes back only through G5 | PART | [DB] `fleet_hold_guard` + `fleet_hold_release_problem` (`fleet/migrations/0004_...`): release needs every slot of the policy that was fixed when the hold was placed, filled on the robot's current manifest. Approvals are checked by `fleet_release_approval_guard` (`0002`): slot role, active grant, distinct approvers, independent of placer when the rule says so | Root cause, fix verification, regression check (free text `note` / `evidence_refs` only) | ENG/SAFETY/FOUNDER: approved HoldPolicy (B-07) | Seed policy is a DRAFT, usable only with `allow_draft_policies=true` (`fleet_policy_usable`). See G-01 |
| P-04 | §1 Rule: anyone can stop work, never penalized | PART | [APP] `fleet.place_hold` and `execution.report_incident` are open to all six operational roles. `field_operator` can `open_interruption` and record session `stop`. A new hold bumps the robot version and invalidates any authorization (guard snapshot, `readiness/authorization.py`) | "Never penalized" is culture. The physical stop is human | OPS | `field_operator` and `engineer` cannot `revoke_authorization` (`readiness.revoke_authorization` = ops_lead, engineering_lead, safety_reviewer, founder). Placing a hold has the same effect |
| P-05 | §1 Principle 2: if it isn't recorded it didn't happen; approvals are a name and a time | IMPL | [DB] audit table INSERT-only (grant + trigger, README "Database security boundary"). Every Slice 3-4 table is append-only (`*_no_update_delete` triggers, `execution/migrations/0002`). `recorded_at` / `issued_at` are forced to `clock_timestamp()` | — | — | — |
| P-06 | §1 Principles 1, 3, 4, 5 (safe first, every home new, ops makes engineering faster, no hidden problems) | HUMAN | [NONE] Principle 3 is partly backed by P-36/P-37 (assessment validity, live re-check at confirm and departure) | All | — | — |
| P-07 | §1 Principle 6: "Seven records, 15-min sync, 30-min review" | HUMAN | [NONE] | Cadence | OPS | The platform has far more than seven records (D-24) |
| P-08 | §1.1 Book, move or cancel a visit: ops lead decides | IMPL | [APP] `planning.plan` and `planning.confirm` = ops_lead only (`roles.py`). [DB] after departure, cancel/re-plan/release is refused for everyone (`readiness_departed_visit_guard`, `execution_departed_reservation_guard`, `execution_departed_trip_guard`) | Field operator proposes verbally | — | — |
| P-09 | §1.1 Mark readiness passed: check + verify | IMPL | See P-46 (two distinct signers, [DB] `fleet_inspection_guard`) | — | — | Signer role check is [APP] only (`INSPECTION_SIGNER_ROLES`) |
| P-10 | §1.1 Home eligible: ops lead (standard), engineering (borderline), founder + safety (vulnerable) | PART | [DB] `planning_approval_guard` (`planning/migrations/0004_assessment_independence.py`): approver holds ops_lead or engineering_lead and is not the assessor | Deciding what is borderline or vulnerable, and routing it | FOUNDER/SAFETY/LEGAL: L6 | No borderline or vulnerable flag. An ops lead can approve any home |
| P-11 | §1.1 Which software versions may go into homes: engineering | IMPL | [APP] `fleet.decide_pattern` = engineering_lead. [DB] `pattern_decision_guard`. Eligibility needs pattern `approved_home`, re-proven in `readiness_issue_authorization` | — | ENG: release process (B-06) | The platform approves a configuration pattern (hardware + release + firmware), not a software version (D-20) |
| P-12 | §1.1 Which tasks are allowed in homes: engineering | HUMAN | [NONE] No task catalog. `VisitRevision.tasks` is free JSON. Only per-home `excluded_tasks` blocks (P-33) | Keeping the approved task list | ENG | G-06. Task hold scope "not built" (README) |
| P-13 | §1.1 Restrict a robot, task or version; ops can restrict, only engineering lifts | PART | [APP] `fleet.place_hold` (robot or `manifest_pattern` scope) is open to all operational roles. [DB] placing a hold never needs a policy (`fleet_hold_guard`). Lifting = release slots from the HoldPolicy (seed: `engineering_lead` for manual / engineering / maintenance) | Task restriction | ENG/SAFETY: approve the HoldPolicy | `fleet.release_hold` can be executed by ops_lead once the slots are filled (D-09) |
| P-14 | §1.1 G5 after S2/S3: engineering. After S0/S1: engineering + safety + founder | PART | See P-105 | — | ENG/SAFETY/FOUNDER | D-07 |
| P-15 | §1.1 Regulator reporting, disclosure, data requests: founder decides, legal advises | VAL | [NONE] | All | LEGAL/FOUNDER: L2 | — |
| P-16 | §1.1 Pay minor customer damage up to $[X] | VAL | [NONE] | All | FOUNDER: L5 | No amount is set or invented here |

### Section 2: Roles

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-17 | §2 Roles OPS-A, OPS-B, ENG-1, SAF-1, FND, LEG-1 | PART | [APP] Time-bounded `RoleGrant`s, `has_active_role` (also used inside DB triggers). Seven platform roles | Legal acts outside the platform | OPS | D-04: no legal role; extra `engineer` and `admin` roles |
| P-18 | §2 Two-person rule, no solo visits | IMPL | [APP] `planning.services.evaluate`: `CREW_INCOMPLETE`, `CREW_SAME_PERSON`. [DB] re-proven in `readiness_issue_authorization` (0006 item D) | — | — | Capacity model fixes `crew_size = 2` (`capacity/inputs.py`) |
| P-19 | §2 Training gate (shadow 3, run 3 watched, ops lead signs off; checkers signed off separately) | PART | [DB] `Qualification` rows (`field_lead`, `field_operator`, `robot_handler`) with validity windows, checked for the whole activity (`readiness_qualified_window`, 0006). [APP] granted by ops_lead (`planning.manage_people`) | Counting shadow and watched visits | OPS | No "readiness checker" qualification. Any active ops_lead/field_operator can sign inspections (G-10) |

### Section 3: Customer onboarding

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-20 | §3.1 Intake call, *What to expect* sheet, never promise an unapproved task | HUMAN | [NONE] | All | FOUNDER/LEGAL: the sheet | — |
| P-21 | §3.2-3.3 Ask only what we need; no names, medical details or door codes; contact details in CRM | PART | By schema: `planning.Site` holds code, zone, timezone, `crm_ref`, `expected_adults`. Consent uses pseudonymous `participant_ref` (`planning/models.py`) | Free-text fields (`restrictions`, `reason`, incident `description`, notes) can still hold personal data | LEGAL: L1 retention | No free-text screening |
| P-22 | §3.4 Explain recording and data with the legal script | VAL | [REC] consent `scope` ⊆ {presence, recording, remote_view} per participant; [APP] visit `required_consent_scopes` must be covered | Script | LEGAL: L1 | Retention and deletion are not modelled |
| P-23 | §3.5 Book the assessment; no visit before G1 | IMPL | [APP] `SITE_NOT_ASSESSED` blocks plan and confirm. [DB] re-proven at authorization (0006 item B) | — | — | — |
| P-24 | §3 Escalate to founder: infant/toddler, dementia, limited mobility, medical device, public figure | VAL | [NONE] No field or flag | Escalation | FOUNDER/SAFETY/LEGAL: L6 | — |

### Section 4: Home suitability (G1)

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-25 | §4.1-4.3 Video walk-through, two-person in-person survey, F1, no-go items | HUMAN | [REC] `SiteAssessment` + `AssessmentApproval` (second person, P-10) | Survey, measurements, photos | ENG: E8 thresholds | F1 Part B items are not stored |
| P-26 | §4.4 Decide Eligible / with restrictions / Not eligible / Pending | PART | [APP] decisions `eligible`, `eligible_restricted`, `not_eligible`. "Pending" = no approved, current assessment (`SITE_NOT_ASSESSED`, blocks) | The judgement | OPS/ENG | D-10: `eligible_restricted` gives `SITE_RESTRICTED` (review), so the visit cannot be confirmed, and [DB] 0006 refuses authorization |
| P-27 | §4.5 Every adult signs the current consent form; partial = not eligible | IMPL | [APP] `planning.services.consent_status`: valid, unwithdrawn consents on setting `consent_form_version`, covering the visit's scopes, ≥ `Site.expected_adults`. Unknown adults = review, no form version = blocked. [DB] re-proven in `readiness_issue_authorization`. Withdrawal blocks session start at once (`execution_session_guard`) | Truth of `expected_adults` and the evidence reference | LEGAL: L1 form | Count-based, not identity-based |
| P-28 | §4.6 Re-assess-by date, 90 days [A] | PART | [APP]+[DB] `SiteAssessment.valid_until` is checked against the site-local visit date (`current_assessment`, 0006) | Choosing the date. The 90 days is not a platform rule | OPS/ENG | — |
| P-29 | §4 Decision table (path width, rugs, Wi-Fi, pets, children) | VAL | [NONE] | Applying the table | ENG: E8; FOUNDER/SAFETY: L6 | — |

### Section 5: Scheduling and assignment (G2)

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-30 | §5 Confirm to the customer only when not BLOCKED | PART | [APP] `planning.confirm_visit` refuses unless live feasibility = `feasible` | The customer call itself | — | No "confirmed to customer" record |
| P-31 | §5 Visit block = drive + setup 30 + task + pack 30 + notes 15 + drive back | PART | [APP]+[DB] timeline: inspection (`pre_visit_inspection_min` setting) + load + travel out + on site + travel back + unload + charging (`planning.services.evaluate`, same formula in 0006). Unknown durations block | Setup, pack and notes must fit inside the on-site window | ENG: load/unload, charge; OPS: inspection minutes | D-16 |
| P-32 | §5 F1 robot double-booked | IMPL | [DB] `EXCLUDE USING gist` on reservations (`planning/migrations/0002_db_enforcement.py`) | — | — | — |
| P-33 | §5 F6 task not approved, restricted or excluded for this home | PART | [APP] `TASK_EXCLUDED`; [DB] 0006 item B | Fleet-wide task approval and restriction | ENG | G-06 |
| P-34 | §5 F2 operator double-booked incl. 15-min buffer | PART | [DB] same exclusion constraint on person `field_shift` [inspection start, unloading end) | The 15-minute buffer: back-to-back bookings are allowed | OPS: buffer value | D-15 |
| P-35 | §5 F3 robot not assignable | IMPL | [APP] `ROBOT_NOT_ELIGIBLE` from `fleet.services.evaluate_eligibility`. [DB] eligibility re-proven in `readiness_issue_authorization` (0005) | — | — | — |
| P-36 | §5 F4 home not eligible on that date | IMPL | P-26, P-28 | — | — | — |
| P-37 | §5 F5 consent missing or on an old form | IMPL | P-27 | — | — | — |
| P-38 | §5 F7 charge / turnaround between visits | IMPL | [DB] a `charging` reservation of `charge_turnaround_min` (approved capability profile) inside the exclusion constraint. Unknown → `UNKNOWN_CHARGE_TIME` | — | ENG: E7 value (B-04) | — |
| P-39 | §5 F8 two trained operators | IMPL | P-18, P-19 | — | — | — |
| P-40 | §5 F9 readiness failed or stale | IMPL | [APP] `EVIDENCE_FAILED` / `EVIDENCE_STALE` (`readiness/evaluator.py`). [DB] latest evidence passing and fresh re-proven in `readiness_issue_authorization` | — | ENG: E7 window | — |
| P-41 | §5 F10 periodic inspection due before the visit | VAL | [NONE] Maintenance is not built (README). No inspection interval or due date anywhere | Tracking due dates by hand | ENG: E7 interval (B-08) | G-02 |
| P-42 | §5 F11 outside shift window (advisory) | HUMAN | [NONE] | Ops lead's call | OPS | Capacity model assumes a 09:00-16:30 working day (`working_day_min`) for forecasting only |
| P-43 | §5 Priorities when demand exceeds capacity; record turned-away requests | HUMAN | [READ] Slice 6 forecast (`capacity/model.py`) reports unmet demand from an assumed demand input, not from recorded requests | Prioritising; recording refusals | FOUNDER: demand data | — |
| P-44 | §5 48-h confirmation, 24-h re-check call; a change sends the home back to G1 | HUMAN | [NONE] Synthetic inspection items 8-12 are visit items, but their content is not defined | Calls; sending the home back to review | OPS | G-09 |

### Section 6: Pre-deployment readiness (G3) - EM focus "readiness"

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-45 | §6 Freshness: no more than 6 h [A] before departure | PART | [APP]+[DB] per-kind `max_age_min` in the active ReadinessPolicy (demo 240/240/120 min), authorization TTL (demo 90 min), `expires_at <= least(now+ttl, evidence observed_at+max_age)` (0005). `observed_at` ≤ 24 h old at insert (`readiness_evidence_guard`) | — | ENG: E7 values | D-12 |
| P-46 | §6.2-6.5 Run the 12 checks; both sign; same person can't check and verify; nobody types the result | IMPL | [DB] `fleet_inspection_guard` (`fleet/migrations/0004`) recomputes `result`/`complete` from the answers. PASS needs every item answered and performer ≠ verifier. [APP] signers must hold ops_lead or field_operator | Doing the checks truthfully | ENG/SAFETY: E1 content | The template is SYNTHETIC (`seed_fleet.TEMPLATE_ITEMS`), and **its source is not checked in production** (G-03) |
| P-47 | §6.3 Any Fail = the robot does not go | IMPL | [DB] `readiness_evidence_guard`: pre-departure evidence `pass` needs inspection `PASS` and complete. The evaluator and the issue function need the latest evidence per kind to pass | — | — | — |
| P-48 | §6 Fail on items 1-7 puts the robot on HOLD at once; 8-12 block only the visit | IMPL | [DB] `fleet_inspection_guard` + `fleet_hold_guard`: a robot-condition failure must reference a blocking robot hold created in the **same transaction** (`created_txid`). One hold per inspection (`uniq_inspection_hold_per_inspection`). [APP] `fleet.services.submit_inspection` | — | ENG: which items are robot-condition | Matches the playbook exactly (INV-1) |
| P-49 | §6.6 Visits row reads CLEARED TO DEPART | IMPL | [APP] evaluator label "Ready to request authorization" (`readiness/evaluator.py` `LABEL_READY`), then an ops-lead `DepartureAuthorization` (time-limited, version-bound) and a single-use `DepartureEvent` | — | — | D-02: never green, never "cleared" |
| P-50 | §6 F2 items 9-10: transport kit and field kit complete | PART | [APP]+[DB] `equipment_check` evidence must list every `policy.required_equipment` + recovery-plan equipment as `present` (`EQUIPMENT_MISSING`, re-proven in 0005) | Checking the kit physically | SAFETY: field-kit list; ENG: transport kit | — |
| P-51 | §6 F2 item 3: battery charge level in range | IMPL | [APP]+[DB] `battery_check` `soc_pct` ≥ approved profile `min_departure_soc_pct`. Unknown minimum blocks (`BATTERY_MIN_UNKNOWN`, `BATTERY_LOW`) | Reading the value truthfully; visual battery check | ENG: E1 minimum (B-04) | — |
| P-52 | (platform only) Recovery plan per visit: on-call engineer, escalation contact, escalation path | IMPL | [DB] `readiness_recovery_plan_guard`; the authorization requires it when the policy says so | Whether the on-call person is really an engineer (`RECOVERY_ONCALL_NOT_ENGINEER` is review only) | ENG: E5 | Not in the playbook (G-15 note) |
| P-53 | §6 table: open an INC the same hour on a readiness fail; tell engineering with a photo | HUMAN | [REC] the inspection hold is the record. A platform incident would add a second hold under the incident release rule | Telling engineering; the ticket | — | D-14 |
| P-54 | §6 table: swap to a spare robot, full new check | IMPL | [DB] evidence is bound to the current revision, robot and manifest (`readiness_evidence_guard`). A swap is a new revision (`plan_visit`), so the old evidence no longer counts (`EVIDENCE_MISSING`) | — | — | — |
| P-55 | §6 table: no spare: call the customer, cancel (ops), rebook pointing back | PART | [APP] `cancel_visit` (ops_lead, reason required). A new visit can be requested | The call; the "rebooking of" link (not modelled) | — | — |
| P-56 | §6 table: same check failed twice in two weeks → weekly review | HUMAN | [NONE] No repeat-failure detection | Review | — | — |
| P-57 | §6 table: battery problem → S0, move people, battery procedure | VAL | [REC] `report_incident` `heat_or_smoke` → hold | Everything physical | ENG/SAFETY: E1, E6; LEGAL: L3 | — |

### Section 7: Transportation and loading - EM focus "transport custody"

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-58 | §7.1 Lift and load only with engineering's procedure; two people | PART | [DB] both crew need `robot_handler` for [loading start, unloading end] (0006 item D). Capability `lift_crew` must be known (eligibility) | The lift itself | ENG: E2 | `lift_crew` is never compared with crew size, and `transport_posture` is not required (`CapabilityProfile.REQUIRED_FOR_PLANNING`) |
| P-59 | §7.2 Secure the robot as specified | PART | [APP]+[DB] vehicle `robot_capacity` and `restraint_kit` must be known (`VEHICLE_*_UNKNOWN`, 0006 item C). [DB] `planning_cargo_guard` keeps cargo ≤ capacity | Physical securing | ENG: E2 | — |
| P-60 | §7.4 Never leave the robot alone in the vehicle | HUMAN | [REC] custody names a custodian (`execution.CustodyEvent.custodian`) | Presence | — | — |
| P-61 | §7.5 Vehicle temperature limit for the battery | VAL | [NONE] | All | ENG: E2 | — |
| P-62 | §7.6 Record *Departed hub* | IMPL | [DB] `DepartureEvent` (single use, live guard re-check). The AFTER INSERT trigger `execution_open_run` opens the `ExecutionRun` and an *administrative* hub→vehicle custody event in the same transaction. Runtime role cannot insert runs | — | — | — |
| P-63 | (platform only) Custody chain: verify departure assignment, vehicle→site, site→vehicle, vehicle→hub receipt | IMPL | [DB] `execution_custody_guard` (0002/0003/0004): seq continuity, `from` = previous `to`, vehicle→site only after `arrived_site` and verification, custodian and receiver = field crew, hub receipt by a field user, no move on a closed run. `execution_custody_verification_guard` | Telling the truth about the handoff | — | The playbook has no custody procedure (G-07) |
| P-64 | §7.7 Anything unusual on the road: check; if the robot moved or was hit, new readiness check before use | PART | [APP]+[DB] any role can `report_incident` → hold + interruption; session start then refused | Pulling over and checking | ENG | No re-inspection inside an open run. An incident ends the robot's visit in practice (G-08) |
| P-65 | §7 legal box: UN3481 / 49 CFR 173.185, auto insurance | VAL | [NONE] | All | LEGAL: L3, L4 | — |

### Section 8: Arrival and household setup (G4)

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-66 | §8 5-minute walk-through checks before the robot leaves the vehicle | PART | [DB] `execution_site_check_guard` recomputes `go`/`no_go`: run open, trip `arrived_site`, custody at this site and verified, every template condition `ok`, `expected_adults` known, adults present ≤ valid consents, template usable in this mode (synthetic → no `go` in production). Verifier = field crew | Doing the walk-through | SAFETY/ENG: real site-check template (B-05, E3) | Template is SYNTHETIC (`seed_execution.TEMPLATE_ITEMS`). No command creates templates |
| P-67 | §8 Every adult present has signed; visitors and household staff | PART | [DB] `adults_present` ≤ consent count (site check), re-checked at session start | Counting visitors and staff truthfully; children are not counted | LEGAL: L1 | Count, not identity (G-11) |
| P-68 | §8 Household safety briefing (2 min) | HUMAN | [NONE] (could be a template item) | Briefing | SAFETY | — |
| P-69 | §8 Record G4 result Go / Go with restrictions / Not today, who decided, before photos | PART | [DB] `go`/`no_go`, `recorded_by`, `verifier`. A No-Go is recorded, never hidden | "Go with restrictions" wording; photos | — | D-18 |
| P-70 | §8 *Not today* | PART | [DB] `no_go` blocks sessions. [APP]+[DB] `RecoveryDisposition` `end_visit_return` (ops_lead only, `execution.record_disposition`) | Calling it on site | — | D-18: the lead operator cannot record the disposition |

### Section 9: Safe deployment and monitoring

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-71 | §9.1 Only approved tasks in planned areas | HUMAN | [NONE] beyond P-33 | All | ENG | G-06 |
| P-72 | §9.2 Lead stays in the room, within reach of the e-stop | VAL | [NONE] The platform implements no e-stop (README) | All | ENG: E3 | Capability `stop_behaviour` must be documented to go in service (`fleet.services.transition_lifecycle`) |
| P-73 | §9.3 Second operator keeps people and pets out | HUMAN | [NONE] | All | — | — |
| P-74 | §9.4 Count interventions | PART | [REC] `SessionEvent` `pause`/`resume`/`observation` with notes | The count. There is no intervention field | — | G-16 |
| P-75 | §9.5 Remote sessions: on-site stop wins | VAL | [NONE] Teleoperation not built (B-11). Consent scope `remote_view` exists | All | ENG: E4 | — |
| P-76 | §9.6 Connection drop: hands off, follow engineering's link-loss behaviour | VAL | [NONE] | All | ENG: E4 (B-01) | — |

### Section 10: Stop-work conditions

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-77 | §10 STOP immediately (contact, fall, smoke, unconsented person, link loss, damage, customer asks, anyone unsafe) | HUMAN | [REC] session `stop`/`pause`, `open_interruption` (field_operator, ops_lead), `report_incident` (all roles) | The physical stop | ENG: E3 | — |
| P-78 | §10 Never catch, hold or push a moving robot | HUMAN | [NONE] | All | ENG/SAFETY | — |
| P-79 | §10 Restart only after a non-safety stop; the first four reasons end the visit | PART | [DB] `stopped` is terminal. `resume` needs a newer `go` check and no open interruption (`execution_session_guard`). An incident interruption cannot be resolved while its hold is unreleased (`execution_interruption_resolution_guard`). `continue_visit` is refused while held (`execution_disposition_guard`) | Deciding that a stop is non-safety (operator interruption) | — | Stricter than the playbook: every incident ends the robot's visit |

### Section 11: Incident response and escalation - EM focus "incident escalation"

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-80 | §11 Severity table S0-S3 and C | PART | [DB] `execution_severity_guard`: level must be in the active `IncidentPolicy` scale. Production needs `approved_procedure`. Downgrade needs a `downgrade_roles` role and decider ≠ reporter. No active policy → `unclassified` | Choosing the level | SAFETY/ENG: scale and owners (B-07) | D-05: seed scale is a SYNTHETIC S0-S3 with no C. Only engineering_lead/safety_reviewer decide |
| P-81 | §11 S0/S1 always block (QUARANTINE); S2/S3 block when said | IMPL | [DB] `execution_incident_guard`: `Incident.hold` NOT NULL, a robot-scope blocking hold from the same transaction, one incident per hold. [APP] `execution.incidents.report_incident` never reads the policy | — | — | D-06: every incident holds; no QUARANTINE label |
| P-82 | §11 Notify: founder within 15 min (S0), others same hour; S1 same day; S2 next sync | PART | [DB] `IncidentEscalation` to a role (optionally a person), `EscalationAck` only by a holder of that role. Closing needs every ack and, at or above `escalation_required_from`, at least one escalation (`execution_status_change_guard`). [READ] `ESCALATION_UNACKNOWLEDGED`, `INCIDENT_UNTRIAGED` | Phone calls; the clock | FOUNDER/SAFETY: deadlines | G-04: no deadlines, no paging (notifications are out of scope in Slice 5) |
| P-83 | §11.1-11.2 People first; make safe | HUMAN | [NONE] | All | ENG/SAFETY: E5 | — |
| P-84 | §11.3 Preserve evidence | PART | [DB] the incident freezes the robot's manifest at report. `FaultObservation.evidence_ref`. Manifests are immutable | Not power-cycling; collecting logs | ENG | — |
| P-85 | §11.5 Customer: facts only, no cause, no compensation promise | HUMAN | [NONE] | All | LEGAL | G-05 |
| P-86 | §11.6 Write F4 within the hour (S0/S1) or by end of day | PART | [DB] `occurred_at` ≤ `reported_at` (DB time) and `knowledge_source` are recorded. Late reports still hold | The deadline (not checked) | — | — |
| P-87 | §11.7 Engineering ticket ID in the incident row | HUMAN | [NONE] no ticket field (issue tracker unknown, B-09) | Linking | ENG | G-14 |
| P-88 | §11.8 Same day: cancel this robot's remaining visits; engineering may pause a task or version fleet-wide | PART | [APP]+[DB] a held robot cannot be authorized. [READ] `HELD_ROBOT_ASSIGNED`. Version pause = pattern-scope hold | Cancelling visits (not automatic); task pause | ENG | — |
| P-89 | §11 Duplicate reports: keep both, link, never delete | PART | [DB] append-only, never deleted | Linking (no field) | — | Each duplicate places its own hold, and both need release (G-13) |
| P-90 | §11 legal box: outside reporting (CPSC 24 h, workplace injury, insurer), no media | VAL | [NONE] | All | LEGAL/FOUNDER: L2, L4 | — |

### Sections 12-13: Recovery, return to hub, post-deployment inspection

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-91 | §12 No recovery procedure: don't lift a fallen robot; close off the area; robot may stay until engineering arrives | PART | [DB] `RecoveryDisposition` `robot_left_on_site` / `robot_unaccounted` (incident required) keeps the run open and the trip unclosable (`execution_closure_guard`). `recovered` moves need ops_lead + receiver + note and never release the hold (0003) | The physical recovery; staying with the robot | ENG/SAFETY: E5 | G-05 (robot left in a customer's home) |
| P-92 | §12 After a visit with no incident: pack, after photos, customer debrief, visit log before driving off | PART | [DB] session `stop`, site→vehicle handoff, `departed_site` needs vehicle custody (`execution_trip_event_guard`) | Photos; rating and comment (not modelled) | — | — |
| P-93 | §12 After an incident: travel only as engineering allows; quarantine bay or bench; update location | PART | [DB] the hold stays. Custody = hub/vehicle/site/unaccounted | Physical isolation; bench vs quarantine bay | ENG/SAFETY | D-19 |
| P-94 | §13.1 Unload; visual check against morning photos | IMPL | [DB] vehicle→hub handoff with a field-user receiver. `ReturnReconciliation` `reconciled` needs a post-visit inspection (`visit_ref` = visit) submitted after the hub receipt (`execution_reconciliation_guard`). A FAIL holds the robot. [READ] `POST_VISIT_INSPECTION_MISSING` | The visual check | ENG: post-visit checklist (B-05) | — |
| P-95 | §13.2 Charge with the approved charger and position | VAL | [NONE] | All | ENG/SAFETY: E6 | — |
| P-96 | §13.3 Upload logs and media; *Logs uploaded = Yes* | HUMAN | [NONE] | All | ENG | G-14 |
| P-97 | §13.4 Anything new = an issue now | PART | [APP]+[DB] `report_incident` is allowed after stop, after trip closure, with no run, and still holds | Noticing | — | — |
| P-98 | §13.5 Update Location; gate status updates by itself | IMPL | [DB] custody is derived (`execution_current_custody`). Eligibility and Slice 5 status are derived, never typed | — | — | — |

### Section 14: Maintenance and engineering handoff - EM focus "hold release"

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-99 | §14 Periodic inspection every 14 days [A][ENG]; overdue → NOT AUTHORIZED | VAL | [NONE] not built | Tracking | ENG: E7 (B-08) | G-02 |
| P-100 | §14 Software updates only to approved versions; a test build is NOT AUTHORIZED | IMPL | [APP] `record_manifest` (engineer, engineering_lead). Eligibility needs pattern `approved_home`. [DB] re-proven at authorization. Release approvals are bound to the current manifest and go stale on change (`fleet_hold_release_problem`) | — | ENG | — |
| P-101 | §14 Handoff to engineering: bench or quarantine bay, tag, logs, receipt confirmed | HUMAN | [NONE] | All | ENG | — |
| P-102 | §14 Holds without an incident: anyone places; engineering releases with name + time; nobody else | PART | [APP] `fleet.place_hold` open to all operational roles. [DB] release per policy slot roles (seed: engineering_lead), approvals carry approver + DB time | — | ENG/SAFETY: approve the HoldPolicy | D-09: `release_hold` can be executed by ops_lead or engineering_lead once the slots are filled |
| P-103 | §14-15 Readiness-fail hold: a newer two-person PASS, **or** an engineering release | PART | [DB] seed rule `inspection`: `engineering_review` slot (engineer or engineering_lead) **and** `requires_newer_pass_inspection` on the current manifest (`fleet_hold_release_problem`) | — | ENG | D-08 |

### Section 15: Corrective action and return to service (G5) - EM focus "hold release"

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-104 | §15 Loop: report → triage → reproduce → root cause → fix → verify → regression → G5 → ASSIGNABLE, still needs a fresh G3 | PART | [DB] incident status one step at a time: reported → triaged → response_in_progress → resolved → closed (`execution_status_change_guard`). A fresh G3 is forced: evidence is per revision and manifest (P-54) | Steps 3-7 | ENG | No structured root-cause, fix or regression fields |
| P-105 | §15 G5 approvals: S2/S3 → engineering; S0/S1 → engineering + safety + founder | PART | [DB] release slots come from the HoldPolicy rule for `source_type='incident'`, fixed when the hold is placed (`fleet_hold_guard`). Seed DRAFT rule: 2 engineering reviews + safety + founder for **every** incident, distinct, independent of the placer (`seed_fleet.DRAFT_POLICY_RULES`) | Root cause, fix and regression evidence | ENG/SAFETY/FOUNDER: approve a HoldPolicy | D-07, G-01: not severity-aware, and no production path |
| P-106 | §15 Typing "Closed" does not release the robot | IMPL | [DB]+[APP] no execution code writes `fleet_hold` except the insert in `report_incident`. Closing never releases (contract §3). Interruption resolution is refused while the hold is unreleased | — | — | — |
| P-107 | §15 Patterns: link, parent at the third occurrence, restrict fleet-wide, tell customers, engineering lifts after fix + 2 weeks | PART | [APP] pattern-scope hold restricts a configuration fleet-wide | Linking, parent issues, task restrictions, the 2-week watch, telling customers | ENG/OPS | G-06, G-13 |

### Section 16: End-of-day reporting - EM focus "end-of-day reconciliation"

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-108 | §16 Daily report by 18:00, four written lines | PART | [READ] `operations.metrics` (departure cohort completion, incidents, open exceptions), timeline and incident queue. Counts only, labelled synthetic | The four lines; sending | OPS | No daily-report record (G-12) |
| P-109 | §16 Shift handover: locations current, every departed visit logged, every failure has an INC, checks OK | PART | [DB] a trip closes only after hub receipt + post-visit inspection + `reconciled` reconciliation (`execution_closure_guard`). A robot with an open run cannot be authorized again (`execution_authorization_hub_guard`, C3). [READ] `RETURN_OVERDUE`, `CUSTODY_UNVERIFIED`, `RECONCILIATION_UNRESOLVED`, `ROBOT_UNACCOUNTED`, `POST_VISIT_INSPECTION_MISSING` | The sign-off itself; writing why | OPS | No end-of-day close-out record (G-12). Thresholds come from owner-written settings; missing → `THRESHOLD_NOT_CONFIGURED` |

### Section 17: Daily and weekly cadence

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-110 | §17 Daily and weekly cadence (07:30 checks, 08:45 sync, Friday review) | HUMAN | [READ] the command center is the 07:30 "Fleet now" view | Meetings | OPS | — |
| P-111 | §17 Targets blank for four weeks; small numbers (< 20) shown but not trusted | PART | [READ] metrics show N/A on zero denominators. The capacity baseline needs minimum samples (n ≥ 10 / 30, `capacity/baseline.py`) | Interpreting | OPS | Slice 5 metrics have no small-n flag |

### Section 18: Customer communication scripts - EM focus "customer communication"

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-112 | §18 Customer scripts (24-h re-check, readiness fail, Not today, after a stop, cancel while driving) | HUMAN | [NONE] only `crm_ref` | All | LEGAL: after-incident wording | G-05 |
| P-113 | §18 "Customer cancels while we drive" | PART | [DB] after departure the visit cannot be cancelled (C1). It ends via `end_visit_return` or an aborted-outbound `arrived_hub` (`execution/migrations/0003`) | The call | — | D-17 |

### Section 19: Validation register (rule)

| ID | Playbook step | Cat. | What the platform enforces | What stays human | Validation | Gap / note |
|---|---|---|---|---|---|---|
| P-114 | §19 Until an item is approved, the step is not done and the robot does not go | PART | [DB] production fails closed for: readiness policy (`approved_procedure`), synthetic capability profile, synthetic site-check template, synthetic incident policy (severity only), draft hold policies (release only) | Items with no platform hook (section 7) | All owners | G-03 |

---

## 3. Counts

| Category | Rows |
|---|---|
| IMPL | 29 |
| PART | 47 |
| HUMAN | 23 |
| VAL | 15 |
| **Total** | **114** |

---

## 4. EM focus areas

**Dispatch approval.** *Enforced end-to-end.* Planning confirm needs live feasibility (ops_lead). Then the evaluator
must reach "Ready to request authorization". Then an ops lead issues a time-limited `DepartureAuthorization`, which
only the SECURITY DEFINER `readiness_issue_authorization` can insert and which re-proves every gate in SQL. Then
`DepartureEvent` (ops lead or crew) re-checks the guard snapshot under locks and can happen once per authorization
and once per visit. *Human:* the truth of the evidence entries. *Riskiest gap:* a SYNTHETIC inspection template
can still produce the PASS that backs a production authorization, because nothing checks the template's `source`
(G-03).

**Readiness.** *Enforced:* two-signer inspection with a DB-recomputed result. A same-transaction robot hold on any
robot-condition failure. Equipment, battery (against the profile minimum) and recovery-plan evidence, each bound
to the visit revision and robot manifest, with policy freshness and expiry. *Human:* doing the checks, and the
procedures behind them (E1). *Riskiest gap:* the checklist content and the freshness values are placeholders. The
readiness policy fails closed in production, but the checklist template does not (G-03).

**Transport custody.** *Enforced end-to-end in the database:* departure opens the run and an administrative
hub→vehicle assignment. The crew must verify it before any further move. Every handoff names a field-crew
custodian and receiver. Arrival, site, return and hub receipt are strictly ordered. A robot cannot be in two
places. A vehicle back at the hub is not the robot back at the hub. *Human:* lifting, securing, never leaving the
robot alone, temperature (E2). *Riskiest gap:* the playbook has no custody procedure at all, so field staff are
not told to verify the assignment or to name a receiver (G-07). Incidents in transit have no re-check path short
of ending the visit (G-08).

**Incident escalation.** *Enforced:* any role can report. Every report holds the robot in the same transaction,
with or without a policy. Severity is a separate, policy-bound, append-only decision. Escalations can be
acknowledged only by the target role, and closing needs every ack. *Human:* the phone call and the 15-minute,
same-hour and same-day clocks. *Riskiest gap:* nothing measures or alerts on escalation deadlines, and the
platform has no notifications (G-04).

**Hold release.** *Enforced:* release needs the slots of the policy fixed at placement, filled by distinct,
correctly-roled, active approvers on the robot's current configuration. Closing an incident never releases.
*Human:* root cause, fix verification, regression evidence. *Riskiest gap (P0):* the only hold policy is a seeded
DRAFT. In production every incident hold is unreleasable. In demo, every incident needs 2 engineers + safety +
founder whatever its severity, which disagrees with the playbook's G5 (G-01, D-07).

**Customer communication.** *Enforced:* nothing. The platform stores only an opaque `crm_ref`, by design. *Human:*
every script, confirmation, re-check call, post-incident follow-up and compensation conversation. *Riskiest gap:*
promises made during an incident ("we'll follow up by [time]"), complaints (severity C) and pauses of a task
fleet-wide have no owner record, no deadline and no trace (G-05, G-09).

**End-of-day reconciliation.** *Enforced:* a trip closes only after hub receipt, post-visit inspection and a
`reconciled` reconciliation. A robot left on site or unaccounted keeps the run open and blocks re-authorization.
The command center lists overdue returns, unverified custody, unresolved reconciliations and missing inspections.
*Human:* the 18:00 report and the handover sign-off. *Riskiest gap:* there is no daily close-out record, so "all
OK" at end of day is never recorded and an open run can stay open overnight with only a read-only exception (G-12).

---

## 5. Divergences (playbook vs platform)

| # | Topic | Playbook / data model says | Platform does | Recommendation (which side changes) |
|---|---|---|---|---|
| D-01 | Gate model | Five gates G1-G5 | Layers: assessment + consent (planning), fleet eligibility, readiness evidence + **authorization** + departure, site check, hold release | **Playbook v1.1**: add a G1-G5 → platform mapping table. Keep the gate names for training |
| D-02 | Departure wording | Green **CLEARED TO DEPART**, computed | Never green, never "cleared". "Ready to request authorization", then a named ops-lead authorization (`readiness/evaluator.py`; UI wording tests) | **Playbook**: replace with "Authorization issued (expires HH:MM)" and add the ops-lead step. The platform's never-green rule stays |
| D-03 | Robot status | RETIRED / QUARANTINE / HOLD / NOT AUTHORIZED / ASSIGNABLE | Lifecycle + "Eligible for planning, not cleared to depart" + Slice 5 labels (`Held`, `Assigned`, `At hub - not eligible`, ...) | **Playbook**: adopt the platform labels. Possible platform display-only label for incident holds (no new state) |
| D-04 | Roles | OPS-A, OPS-B, ENG-1, SAF-1, FND, LEG-1 | `ops_lead`, `field_operator`, `engineer`, `engineering_lead`, `safety_reviewer`, `founder`, `admin`; no legal role | **Playbook**: role-code mapping. LEG-1 stays outside the platform. Explain `engineer` vs `engineering_lead` (e.g. engineers may fill engineering review slots in the seed rules) |
| D-05 | Severity | Fixed S0-S3 + C, set by the reporter on F4 | Versioned `IncidentPolicy` scale. Only engineering_lead / safety_reviewer decide. `unclassified` until then. No C | **Both**: playbook says the reporter describes and engineering/safety classify. Company approves the scale (B-07). Complaints (C) get a home outside the incident table (EM decision) |
| D-06 | Which incidents hold | S0/S1 always; S2/S3 only if someone says so | Every incident holds the robot in the same transaction (contract D2) | **Playbook**: adopt "every incident holds". Matters that do not concern the robot are session observations |
| D-07 | G5 approvers | By severity: S2/S3 engineering; S0/S1 engineering + safety + founder | By hold `source_type`, fixed at placement, before severity exists. Seed draft: 4 slots for every incident | **EM decision needed**: (a) platform change, a severity-aware release rule (proposal X-2), or (b) company approves one strict rule and the playbook adopts it |
| D-08 | Readiness-fail release | Newer two-person PASS **or** engineering release | Engineering review slot **and** newer PASS on the current manifest (seed) | **Playbook** adopts AND, unless engineering approves the OR in the HoldPolicy. The policy is data, so no code change is needed |
| D-09 | Who releases holds | "Engineering releases ... nobody else can" | Approvals by the policy's slot roles. `release_hold` executed by ops_lead or engineering_lead once every slot is filled | **Playbook**: "engineering approves; ops lead may record the release" (already matches F5 "ops lead records it") |
| D-10 | Eligible with restrictions | A go, with the restrictions applied | `SITE_RESTRICTED` = review → cannot confirm. [DB] 0006 refuses authorization | **EM decision**: platform proposal X-5 (an explicit restrictions acknowledgement), or playbook v1.1 "restricted homes are not served yet" |
| D-11 | Pending G1 | Explicit *Pending* decision | No Pending. No approved current assessment = `SITE_NOT_ASSESSED` | **Playbook** note only |
| D-12 | Readiness freshness | One window, 6 h [A] | Per evidence kind `max_age_min` + authorization TTL (policy data) | **Playbook**: point to the approved readiness policy, not a fixed number |
| D-13 | Readiness contents | 12 F2 items | Inspection template (robot-condition flag per item) + separate equipment check, battery SoC check and recovery plan | **Playbook**: describe the four evidence kinds. F2 items 9-10 become the equipment check |
| D-14 | Readiness fail → INC | Open an INC the same hour | The inspection hold is the record. A platform incident would add a second hold under the incident rule | **Playbook**: on a readiness fail, raise an engineering ticket, not a platform incident, unless an event occurred |
| D-15 | Operator buffer | 15 min [A] between visits | Back-to-back allowed by the exclusion constraint | **EM decision**: platform setting (proposal X-6) or playbook drops the buffer |
| D-16 | Visit block sizing | setup 30 + pack 30 + notes 15 | inspection + loading + travel + on site + travel + unloading + charging | **Playbook**: adopt the platform timeline; setup, pack and notes live inside on site |
| D-17 | Cancel after departure | *Cancelled (customer/ops)* any time | Never after departure (C1). Ends via disposition / aborted outbound | **Playbook** |
| D-18 | G4 outcome and who records Not today | Go / Go with restrictions / Not today, lead operator | `go`/`no_go`. Ending the visit is a disposition by ops_lead only | **Playbook** for the wording. **EM decision** whether a field lead may record `end_visit_return` (proposal X-7) |
| D-19 | Location list | Hub charging / bench / quarantine bay, vehicle, home, engineering bench, off-site repair | Custody: hub / vehicle / site / unaccounted | **Playbook**: location inside the hub stays a physical note. No platform change now |
| D-20 | Software approval | Software list, one version string | Configuration pattern (hardware + release + firmware), immutable manifests | **Playbook** terminology |
| D-21 | Notification timings | 15 min / same hour / same day / 24 h | No clocks. Escalation + ack records only | **Playbook** keeps the timings. Platform proposal X-4 makes lateness visible |
| D-22 | Visit status values | Scheduled, In progress, Completed, Partial, Stopped, Not today, Cancelled, Rescheduled | Visit requested / planned / confirmed / cancelled; execution run stages derived | **Playbook** mapping |
| D-23 | Readiness checkers | Separately trained checkers | Any active ops_lead / field_operator may sign ([APP]) | **EM decision**: platform proposal X-8 (checker qualification) or playbook drops the separate sign-off |
| D-24 | "Seven records" | Principle 6 | Many append-only records and policies | **Playbook**: rephrase as "a few screens", not "seven records" |

---

## 6. Gaps ranked

Each gap is an operational risk where there is neither a platform control nor a clear human procedure, or where
the existing procedure relies on a control that the platform does not have.

| # | Pri | Gap | Why it matters | Suggested owner |
|---|---|---|---|---|
| G-01 | **P0** | No approved release path for incident holds | The seed HoldPolicy is a DRAFT, usable only while `allow_draft_policies=true`. In production every incident hold is unreleasable. In demo the rule ignores severity (4 approvals for every incident) and disagrees with the playbook G5 | ENG + SAFETY + FOUNDER (B-07); EM for D-07 |
| G-02 | **P0** | Periodic inspection interval not enforced | The playbook says the tracker marks overdue robots NOT AUTHORIZED (F10). The platform has no interval or due date, so an overdue robot can be authorized | ENG (B-08), EM (scope) |
| G-03 | **P0** | Synthetic inspection checklist accepted in production | `InspectionTemplate.source` is never checked. A PASS on the synthetic 12-item template can back a production authorization, even though the readiness policy itself must be `approved_procedure` | ENG (platform, proposal X-1) |
| G-04 | **P1** | Escalation deadlines are untracked and nobody is alerted | The S0 15-minute founder call depends on memory. The platform shows unacknowledged escalations but no lateness, and has no notifications | FOUNDER/SAFETY (deadlines), ENG (X-4) |
| G-05 | **P1** | Customer commitments and complaints have no record | Post-incident "follow up by [time]", compensation talks and severity-C complaints have no owner, deadline or trace. A robot left in a home overnight (`robot_left_on_site`) has no customer-side procedure (who the custodian is, presence consent, insurance) | OPS + LEGAL |
| G-06 | **P1** | No fleet-wide task approval or restriction | Tasks are free text. Only per-home exclusions block. The playbook F6 / §15.3 "Restricted row in Tasks" has no counterpart | ENG |
| G-07 | **P1** | Playbook has no custody procedure | The crew are not told to verify the departure assignment, name a receiver or record hub receipt, and the platform requires all three | OPS (playbook v1.1) |
| G-08 | **P1** | No re-check path after a road event | The playbook says to re-check before use, but a run has no re-inspection. The only safe record is an incident, which holds the robot and ends the visit. Staff may skip recording to save the visit | OPS + ENG |
| G-09 | **P1** | 24-hour re-check results not recorded | A home change found by phone should send the home back to G1. Nothing records the call or blocks the visit | OPS (template item or a site update by the ops lead) |
| G-10 | **P1** | Inspection signer training not enforced | Any ops_lead/field_operator can sign readiness, and the role check is [APP] only | OPS, ENG (X-8) |
| G-11 | **P1** | Consent of visitors, staff and children | Consent is count-based. A visitor who is not counted, or a child, is invisible to the platform | LEGAL (L1) |
| G-12 | **P1** | No end-of-day close-out record | The handover "all OK" is never recorded. An open run overnight is only a read-only exception | OPS |
| G-13 | **P2** | Duplicate incident reports each hold the robot | With no link field, two reports of one event need two full releases. Harmless for safety but costly | ENG |
| G-14 | **P2** | Engineering ticket and log-upload links | Nothing ties an incident to the engineering ticket or to uploaded logs (B-09) | ENG |
| G-15 | **P2** | Recovery plan not in the playbook | The platform requires an on-call engineer and escalation path per visit. The playbook never asks anyone to prepare one | OPS (playbook) |
| G-16 | **P2** | Interventions not counted | §9.4 count exists only as free-text session observations | OPS |

---

## 7. Validation register cross-check (playbook §19)

| # | Item | Where the platform blocks until supplied | Blocks in production? |
|---|---|---|---|
| E1 | Readiness procedures and pass criteria | Readiness policy must be `approved_procedure` with engineering + safety approvals (`readiness_issue_authorization`, `POLICY_NOT_APPROVED_PROCEDURE`). Battery minimum unknown → `BATTERY_MIN_UNKNOWN`. **The inspection template content is not bound** | **Partly** (G-03) |
| E2 | Lifting, loading, securing, transport power state and charge, vehicle temperature | `lift_crew`, `charge_turnaround_min`, `load_unload_min` must be known (eligibility / planning). Vehicle capacity and restraint kit must be known. `transport_posture` and temperature: no hook | **Partly**: known-ness only, values never compared |
| E3 | E-stop location and use; monitoring position | `stop_behaviour` documented to go in service and for eligibility. A synthetic site-check template can't produce `go` in production | **Partly**: text presence only |
| E4 | Link-loss behaviour; remote protocol | None (B-01, B-11) | **No** |
| E5 | Fall/fault recovery, power-down, moving out of a home | Recovery plan record (names, path) when the policy requires one. The physical procedure has no hook | **Partly** |
| E6 | Charging area, battery storage, fire safety | None | **No** |
| E7 | Inspection interval, turnaround, freshness window | Turnaround: unknown blocks. Freshness: policy data. Interval: not built | **Partly** (G-02) |
| E8 | Home limits (path, floors, slopes, connectivity) | None; the assessment decision is human | **No** |
| L1 | Consent form, recording rules, retention and deletion | `consent_form_version` setting required (`CONSENT` blocked "No current consent form version is configured"). Scopes per visit. Retention: none | **Partly** |
| L2 | Regulator and insurer reporting | None | **No** |
| L3 | Battery transport rules | None | **No** |
| L4 | Insurance | None | **No** |
| L5 | Damage payment limit, cancellation policy | None | **No** |
| L6 | Vulnerable residents policy | None | **No** |
| (platform only) | Incident severity policy (B-07), hold release policy, site-check template (B-05) | Severity: synthetic refused in production. Hold release: drafts unusable in production. Site check: synthetic → no `go` in production | Yes, as stated |

The playbook's closing sentence ("until an item is approved ... the robot does not go") is true in the platform for
E1 (except the checklist content), parts of E2/E3/E7 and L1. For E4, E6, E8 and L2-L6 it remains a human promise.

---

## 8. Proposed next steps (proposals for EM decision, nothing implemented)

### 8.1 Playbook v1.1: surgical edits (content preserved)

1. §1 and §5.2 of the data model: add a one-table G1-G5 → platform mapping (D-01). Replace "CLEARED TO DEPART" with
   the authorization wording and the ops-lead step (D-02, D-03).
2. §2: add the role-code ↔ platform-role table (D-04). State that inspection signers need an active ops_lead or
   field_operator role.
3. §4: say that *Eligible with restrictions* homes cannot yet be booked, or keep the text and wait for X-5 (D-10).
   Note that "Pending" = no approved assessment (D-11).
4. §5: replace the visit-block formula with the platform timeline (D-16). Mark the 15-min buffer as not enforced
   until X-6 is decided (D-15). Mark F6 (fleet-wide tasks) and F10 (periodic inspection) as **not enforced by the
   platform; manual check** (G-02, G-06).
5. §6: list the four evidence kinds and point the freshness window to the readiness policy (D-12, D-13). On a
   readiness fail, raise an engineering ticket, not an incident (D-14). Add "prepare the recovery plan" (G-15).
6. §7: add a short custody procedure: verify the departure assignment, hand off at site with two crew names,
   name a field receiver at hub receipt (G-07). Say what to record after a road event (G-08).
7. §8: G4 outcomes are Go / No-Go. Who records the end of the visit (D-18).
8. §11: every incident holds the robot (D-06). The reporter describes, engineering or safety classify (D-05).
   Record each notification as a platform escalation, and the founder or safety reviewer acknowledges it (D-21).
9. §14-15: the release wording "engineering approves, ops lead may record" (D-09). The readiness-fail release
   needs AND (D-08). The G5 table follows the EM decision on D-07.
10. §16: add a one-line close-out: every open run is listed with a reason (G-12). §18: "cancel while driving"
    ends the visit; it is not a cancellation (D-17).
11. §19: add a column "platform blocks until supplied: yes / partly / no" from section 7 above.

### 8.2 Small platform changes worth considering (each needs an EM go-ahead and its own slice or addendum)

| # | Proposal | Closes |
|---|---|---|
| X-1 | Refuse `pre_departure_inspection` evidence (or authorization) in production when its inspection's template `source` is `synthetic_demo`, in Python and in `readiness_issue_authorization` | G-03 |
| X-2 | Severity-aware incident release: resolve the required slots at release time from the latest `SeverityDecision`, or allow a stricter-only re-slotting when severity is raised. Needs a contract change (rules are fixed at placement today) | G-01, D-07 |
| X-3 | Periodic inspection due date in fleet eligibility (owner-written interval setting + latest PASS inspection), unknown interval blocks | G-02 |
| X-4 | Read-only `ESCALATION_OVERDUE` exception driven by owner-written deadline settings per severity (same pattern as `THRESHOLD_NOT_CONFIGURED`) | G-04 |
| X-5 | Append-only "restrictions acknowledged" record that lets an `eligible_restricted` assessment become feasible for a named visit | D-10 |
| X-6 | Optional crew buffer setting added to person `field_shift` reservations | D-15 |
| X-7 | Allow the run's field lead (not only ops_lead) to record `end_visit_return` after a `no_go` | D-18 |
| X-8 | A `readiness_checker` qualification required of inspection signers, checked in `fleet_inspection_guard` | G-10, D-23 |
| X-9 | Append-only incident link (`duplicate_of`) and external ticket reference, never touching holds | G-13, G-14 |
| X-10 | A daily close-out view in the command center listing every open run, unverified custody and unresolved reconciliation as of 18:00 (read-only) | G-12 |

Fleet-wide task approval (G-06) is larger: it needs the task hold scope that the README lists as "not built", so
it belongs to a future slice, not a small change.
