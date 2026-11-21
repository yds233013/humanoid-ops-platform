# 01C closure evidence 1: first-run walkthrough, empty database to Approved to leave

Produced by `backend/tests/test_v2_01c_journey.py` (one test, Django test client, HTTP only, one client per person).
Start: an empty migrated database and the one-time `WORKSPACE_BOOTSTRAP_TOKEN`. End: a confirmed visit that the
ops lead approved to leave. Production rules apply (workspace is NOT in demo mode): the inspection template,
periodic rule and readiness policy are approved records, not synthetic demo items.

Result: **PASS**. 95 steps (the GAP steps marked in the log are the only ones not done through a screen; two of them are not even HTTP). Final visit: `V-0001` with robot R-J1, approved to leave by the ops lead.

Approval records at the end (created only by the approval steps below, never by a setup screen; the test asserts
that every non-approval step added zero):

- pattern decision: 1
- capability profile approval: 2
- base authorization: 1
- inspection template approval: 4
- periodic rule approval: 2
- hold policy approval: 0
- readiness policy approval: 2
- assessment approval: 1
- inspector qualification: 2
- departure authorization: 1

## GAPs: steps that have no screen

- Approve a configuration for home use (engineering lead): no screen, only POST /api/fleet/decide-pattern
- The capability profile page shows a draft but no approve button and no profile id; approving is API only
- Approve a capability profile (engineering lead / safety reviewer): no screen, only POST /api/fleet/approve-capability-profile
- Base home-use authorization of a robot (engineering lead): no screen, only POST /api/fleet/grant-base-authorization
- No screen sets deployment settings: consent_form_version and pre_visit_inspection_min (needed for consent and planning)
- No screen sets the Today thresholds (ops_readiness_lookahead_min, ops_closeout_timezone, ...): until set, Today shows 'Checklist not checked yet'

## Steps

| # | Role | URL | What was submitted | Result |
|---|---|---|---|---|
| 1 | visitor | GET /setup/ | - | The setup page opens (token configured, workspace not initialized) |
| 2 | visitor | POST /setup/ | a wrong setup token | Refused (HTTP 403): the setup token is not correct; no user created |
| 3 | visitor | POST /setup/ | the one-time token, admin username and password, company name | First admin created; redirected to /login/ |
| 4 | visitor | POST /setup/ | the same token again (replay) | Refused (HTTP 404): setup is permanently closed; no second admin |
| 5 | admin | /login/ | username j_admin, the password the admin set | Signed in |
| 6 | admin | GET /settings/setup/ | (look at the page) | Setup checklist (nothing done yet except the admin) |
| 7 | admin | POST /settings/company/ | company name, region, time zone | Company settings saved. |
| 8 | admin | POST /settings/hubs/new/ | hub SF1 | Hub SF1 added. |
| 9 | admin | POST /settings/users/new/ | invite j_ops (temporary password set by the admin) | Account j_ops created. Now choose their role. |
| 10 | admin | POST /settings/users/2/ | give j_ops the role ops_lead | Role ops lead given to j_ops. |
| 11 | admin | POST /settings/users/new/ | invite j_field1 (temporary password set by the admin) | Account j_field1 created. Now choose their role. |
| 12 | admin | POST /settings/users/3/ | give j_field1 the role field_operator | Role field operator given to j_field1. |
| 13 | admin | POST /settings/users/new/ | invite j_field2 (temporary password set by the admin) | Account j_field2 created. Now choose their role. |
| 14 | admin | POST /settings/users/4/ | give j_field2 the role field_operator | Role field operator given to j_field2. |
| 15 | admin | POST /settings/users/new/ | invite j_eng (temporary password set by the admin) | Account j_eng created. Now choose their role. |
| 16 | admin | POST /settings/users/5/ | give j_eng the role engineer | Role engineer given to j_eng. |
| 17 | admin | POST /settings/users/new/ | invite j_lead (temporary password set by the admin) | Account j_lead created. Now choose their role. |
| 18 | admin | POST /settings/users/6/ | give j_lead the role engineering_lead | Role engineering lead given to j_lead. |
| 19 | admin | POST /settings/users/new/ | invite j_lead2 (temporary password set by the admin) | Account j_lead2 created. Now choose their role. |
| 20 | admin | POST /settings/users/7/ | give j_lead2 the role engineering_lead | Role engineering lead given to j_lead2. |
| 21 | admin | POST /settings/users/new/ | invite j_safety (temporary password set by the admin) | Account j_safety created. Now choose their role. |
| 22 | admin | POST /settings/users/8/ | give j_safety the role safety_reviewer | Role safety reviewer given to j_safety. |
| 23 | admin | POST /settings/users/new/ | invite j_founder (temporary password set by the admin) | Account j_founder created. Now choose their role. |
| 24 | admin | POST /settings/users/9/ | give j_founder the role founder | Role founder given to j_founder. |
| 25 | admin | GET /settings/users/ | (look at the page) | People and roles list shows every invited person |
| 26 | ops_lead | /login/ | username j_ops, the password the admin set | Signed in |
| 27 | field_operator | /login/ | username j_field1, the password the admin set | Signed in |
| 28 | field_operator | /login/ | username j_field2, the password the admin set | Signed in |
| 29 | engineer | /login/ | username j_eng, the password the admin set | Signed in |
| 30 | engineering_lead | /login/ | username j_lead, the password the admin set | Signed in |
| 31 | engineering_lead | /login/ | username j_lead2, the password the admin set | Signed in |
| 32 | safety_reviewer | /login/ | username j_safety, the password the admin set | Signed in |
| 33 | founder | /login/ | username j_founder, the password the admin set | Signed in |
| 34 | engineering_lead | POST /fleet/patterns/new/ | configuration pattern P-J (a proposal) | Configuration P-J added. Next: publish its capability profile. |
| 35 | engineering_lead | POST /api/fleet/decide-pattern | GAP: no screen. {"pattern_code": "P-J", "status": "approved_home", "reason": "Journey: approve for home use"} | HTTP 201 |
| 36 | engineer | POST /fleet/patterns/P-J/profile/ | publish the capability profile (draft) | Capability profile v1 published as a draft. It still needs approval from the engineering lead and the safety reviewer. |
| 37 | engineering_lead | POST /api/fleet/approve-capability-profile | GAP: no screen. {"profile_id": 1, "slot": "engineering"} | HTTP 201 |
| 38 | safety_reviewer | POST /api/fleet/approve-capability-profile | GAP: no screen. {"profile_id": 1, "slot": "safety"} | HTTP 201 |
| 39 | engineering_lead | GET /fleet/patterns/ | (look at the page) | Configurations list: P-J has an approved profile |
| 40 | engineering_lead | POST /fleet/new/ | add robot R-J1 | Robot R-J1 added. It is not ready for anything yet: record its configuration next. |
| 41 | engineer | POST /fleet/R-J1/manifest/ | record R-J1's configuration manifest | Configuration recorded as a proposal. Engineering still has to approve it before the robot can be used. |
| 42 | engineering_lead | POST /fleet/R-J1/lifecycle/ | lifecycle: validation | Robot R-J1 moved to validation. |
| 43 | engineering_lead | POST /fleet/R-J1/lifecycle/ | lifecycle: in service | Robot R-J1 moved to in service. |
| 44 | engineering_lead | POST /api/fleet/grant-base-authorization | GAP: no screen. {"robot_code": "R-J1"} | HTTP 201 |
| 45 | ops_lead | POST /people/new/ | register P-1 for j_ops | P-1 registered. They have no qualification yet: that is a separate step. |
| 46 | ops_lead | POST /people/new/ | register P-2 for j_field1 | P-2 registered. They have no qualification yet: that is a separate step. |
| 47 | ops_lead | POST /people/new/ | register P-3 for j_field2 | P-3 registered. They have no qualification yet: that is a separate step. |
| 48 | ops_lead | POST /people/new/ | register P-4 for j_eng | P-4 registered. They have no qualification yet: that is a separate step. |
| 49 | ops_lead | POST /people/new/ | register P-5 for j_lead2 | P-5 registered. They have no qualification yet: that is a separate step. |
| 50 | ops_lead | POST /people/P-1/ | P-1: qualification field_lead | Qualification field lead granted to P-1. |
| 51 | ops_lead | POST /people/P-1/ | P-1: qualification robot_handler | Qualification robot handler granted to P-1. |
| 52 | ops_lead | POST /people/P-2/ | P-2: qualification field_operator | Qualification field operator granted to P-2. |
| 53 | ops_lead | POST /people/P-2/ | P-2: qualification robot_handler | Qualification robot handler granted to P-2. |
| 54 | ops_lead | POST /people/P-3/ | P-3: qualification field_operator | Qualification field operator granted to P-3. |
| 55 | ops_lead | POST /people/P-3/ | P-3: qualification robot_handler | Qualification robot handler granted to P-3. |
| 56 | safety_reviewer | POST /people/P-2/ | P-2: readiness_inspector qualification (safety reviewer) | Qualification readiness inspector granted to P-2. |
| 57 | safety_reviewer | POST /people/P-3/ | P-3: readiness_inspector qualification (safety reviewer) | Qualification readiness inspector granted to P-3. |
| 58 | ops_lead | POST /vehicles/new/ | register vehicle VAN-1 | Vehicle VAN-1 added. |
| 59 | engineer | POST /fleet/patterns/P-J/templates/create/ | inspection template E1 (engineering document reference) | Inspection template recorded; it needs engineering and safety approvals per purpose |
| 60 | engineering_lead | POST /fleet/patterns/P-J/templates/approve/ | template E1 approval: pre_departure / engineering | Template approval recorded |
| 61 | safety_reviewer | POST /fleet/patterns/P-J/templates/approve/ | template E1 approval: pre_departure / safety | Template approval recorded |
| 62 | engineering_lead | POST /fleet/patterns/P-J/templates/approve/ | template E1 approval: periodic / engineering | Template approval recorded |
| 63 | safety_reviewer | POST /fleet/patterns/P-J/templates/approve/ | template E1 approval: periodic / safety | Template approval recorded |
| 64 | engineer | POST /fleet/patterns/P-J/periodic-rules/create/ | periodic inspection rule v1 (90 days) | Periodic inspection rule version recorded; it needs engineering and safety approvals |
| 65 | engineering_lead | POST /fleet/patterns/P-J/periodic-rules/approve/ | periodic rule v1 approval: engineering | Periodic rule approval recorded |
| 66 | safety_reviewer | POST /fleet/patterns/P-J/periodic-rules/approve/ | periodic rule v1 approval: safety | Periodic rule approval recorded |
| 67 | engineering_lead | POST /readiness/policies/create/ | readiness policy v1 (draft) | Policy v1 created as a DRAFT; it needs both approvals |
| 68 | engineering_lead | POST /readiness/policies/1/approve/ | readiness policy v1: engineering approval (a second engineering lead; the author cannot approve) | Policy v1: engineering approval recorded |
| 69 | safety_reviewer | POST /readiness/policies/1/approve/ | readiness policy v1: safety approval | Policy v1: safety approval recorded |
| 70 | (no screen) | GAP: no screen | deployment settings consent_form_version=CF-v1, pre_visit_inspection_min=30 | Written with one existing setting row each (non-HTTP step, GAP) |
| 71 | field_operator | POST /readiness/robots/R-J1/inspection/ | periodic inspection of R-J1: all items pass, signed by two inspectors | Inspection RC-0001 recorded: it passed. |
| 72 | ops_lead | POST /planning/sites/new/ | add customer home H-J1 (adults unknown) | Home H-J1 added. Next: record an assessment and consent. |
| 73 | ops_lead | POST /planning/sites/H-J1/adults/ | expected adults: 2 | Number of adults saved. |
| 74 | field_operator | POST /planning/sites/H-J1/assessments/new/ | home assessment: eligible | Assessment recorded. It counts only after someone else approves it. |
| 75 | ops_lead | POST /planning/sites/H-J1/assessments/1/approve/ | approve the assessment (ops lead, not the assessor) | Assessment approved. |
| 76 | field_operator | POST /planning/sites/H-J1/consents/new/ | consent of adult P1 | Consent recorded for P1. |
| 77 | field_operator | POST /planning/sites/H-J1/consents/new/ | consent of adult P2 | Consent recorded for P2. |
| 78 | ops_lead | GET /planning/sites/H-J1/ | (look at the page) | Home reads ready |
| 79 | ops_lead | POST /planning/book/ | book a visit at H-J1 tomorrow 10:00-11:00 | Visit requested. Next: choose a robot, crew and vehicle. |
| 80 | ops_lead | POST /planning/visits/V-0001/plan/ | plan: robot R-J1, lead P-1, second P-2, vehicle VAN-1 | Plan saved. The result of the check is shown below. |
| 81 | ops_lead | POST /planning/visits/V-0001/confirm/ | confirm the visit (ops lead) | Visit confirmed. This is planning only: it is not approval to leave. |
| 82 | ops_lead | GET /readiness/visits/V-0001/ | (look at the page) | Visit is confirmed but not ready |
| 83 | field_operator | POST /readiness/visits/V-0001/inspection/ | pre-departure inspection through the form (two qualified inspectors) | Inspection RC-0002 recorded: it passed. |
| 84 | ops_lead | POST /readiness/visits/V-0001/evidence/ | record pre-departure inspection evidence (RC-0002) | Evidence recorded (pre_departure_inspection) |
| 85 | field_operator | POST /readiness/visits/V-0001/evidence/ | equipment check: restraint kit, recovery kit, charger present | Evidence recorded (equipment_check) |
| 86 | field_operator | POST /readiness/visits/V-0001/evidence/ | battery check: 85% (minimum 60%) | Evidence recorded (battery_check) |
| 87 | ops_lead | POST /readiness/visits/V-0001/recovery-plan/ | recovery plan: on-call P-4, escalation P-5 | Recovery plan recorded |
| 88 | ops_lead | GET /readiness/visits/V-0001/ | (look at the page) | Checklist reads complete, not approved |
| 89 | ops_lead | GET /ops/ | (look at the page) | Today, readiness lookahead not configured: the complete checklist is NOT shown (visit stays 'Scheduled / Checklist not checked yet') |
| 90 | (no screen) | GAP: no screen | deployment setting ops_readiness_lookahead_min=2880 | Written with one existing setting row |
| 91 | ops_lead | GET /ops/ | (look at the page) | Today after the lookahead is set: 'Checklist complete, waiting for approval' (neutral, not green) |
| 92 | field_operator | GET /readiness/visits/V-0001/ | (look at the page) | A field operator sees no 'approve to leave' button |
| 93 | ops_lead | POST /readiness/visits/V-0001/authorize/ | ops lead requests approval to leave | Approved to leave by j_ops at Sat 21 Nov, 07:33 PST, valid until Sat 21 Nov, 09:03 PST, for visit V-0001 with robot R-J1. This is a time-limited decision; record the dep… |
| 94 | ops_lead | GET /readiness/visits/V-0001/ | (look at the page) | Visit page: Approved to leave, with the decision |
| 95 | ops_lead | GET /ops/ | (look at the page) | Today: the visit is Approved to leave (blue, never green) |

## Findings

- Today shows a visit whose checklist is complete as 'Scheduled / Checklist not checked yet' until the deployment threshold ops_readiness_lookahead_min is set, and no screen sets it (Today also lists THRESHOLD_NOT_CONFIGURED cards for it). GAP: no screen.
- Readiness policy: the author cannot approve it, so a second engineering lead user is needed for the engineering slot (the journey creates two).
- The capability profile page says approval happens 'on the approval screen', but no such screen exists (approve-capability-profile is API only).
- No screen shows or sets demo mode; the journey therefore ran under production rules (approved procedure policy, engineering-document template and rule).
- Today still lists two 'Needs your attention' setup cards for the new workspace (escalation deadlines not set up, closeout time zone not set); both are deployment/owner settings with no screen.
