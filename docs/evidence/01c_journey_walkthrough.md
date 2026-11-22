# 01C closure evidence 1: first-run walkthrough, empty database to Approved to leave

Produced by `backend/tests/test_v2_01c_journey.py` (one test, Django test client, HTTP only, one client per person).
Start: an empty migrated database and the one-time `WORKSPACE_BOOTSTRAP_TOKEN`. End: a confirmed visit that the
ops lead approved to leave. Production rules apply (workspace is NOT in demo mode): the inspection template,
periodic rule and readiness policy are approved records, not synthetic demo items.

Result: **PASS**. 104 steps (every step is a screen, except the one OWNER PROVISIONING step before setup, which is a management command). Final visit: `V-0031` with robot R-J1, approved to leave by the ops lead.

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

- none

## Steps

| # | Role | URL | What was submitted | Result |
|---|---|---|---|---|
| 1 | deployment owner | manage.py provision_settings | consent_form_version=CF-v1, pre_visit_inspection_min=30, --with-defaults (ops_readiness_lookahead_min=2880) | OWNER PROVISIONING (before setup): set consent_form_version = CF-v1 / set pre_visit_inspection_min = 30 / set ops_readiness_lookahead_min = 2880 /  |
| 2 | visitor | GET /setup/ | - | The setup page opens (token configured, workspace not initialized) |
| 3 | visitor | POST /setup/ | a wrong setup token | Refused (HTTP 403): the setup token is not correct; no user created |
| 4 | visitor | POST /setup/ | the one-time token, admin username and password, company name | First admin created; redirected to /login/ |
| 5 | visitor | POST /setup/ | the same token again (replay) | Refused (HTTP 404): setup is permanently closed; no second admin |
| 6 | admin | /login/ | username j_admin, the password the admin set | Signed in |
| 7 | admin | GET /settings/setup/ | (look at the page) | Setup checklist (nothing done yet except the admin) |
| 8 | admin | POST /settings/company/ | company name, region, time zone | Company settings saved. |
| 9 | admin | POST /settings/hubs/new/ | hub SF1 | Hub SF1 added. |
| 10 | admin | POST /settings/users/new/ | invite j_ops (temporary password set by the admin) | Account j_ops created. Now choose their role. |
| 11 | admin | POST /settings/users/879/ | give j_ops the role ops_lead | Role ops lead given to j_ops. |
| 12 | admin | POST /settings/users/new/ | invite j_field1 (temporary password set by the admin) | Account j_field1 created. Now choose their role. |
| 13 | admin | POST /settings/users/880/ | give j_field1 the role field_operator | Role field operator given to j_field1. |
| 14 | admin | POST /settings/users/new/ | invite j_field2 (temporary password set by the admin) | Account j_field2 created. Now choose their role. |
| 15 | admin | POST /settings/users/881/ | give j_field2 the role field_operator | Role field operator given to j_field2. |
| 16 | admin | POST /settings/users/new/ | invite j_eng (temporary password set by the admin) | Account j_eng created. Now choose their role. |
| 17 | admin | POST /settings/users/882/ | give j_eng the role engineer | Role engineer given to j_eng. |
| 18 | admin | POST /settings/users/new/ | invite j_lead (temporary password set by the admin) | Account j_lead created. Now choose their role. |
| 19 | admin | POST /settings/users/883/ | give j_lead the role engineering_lead | Role engineering lead given to j_lead. |
| 20 | admin | POST /settings/users/new/ | invite j_lead2 (temporary password set by the admin) | Account j_lead2 created. Now choose their role. |
| 21 | admin | POST /settings/users/884/ | give j_lead2 the role engineering_lead | Role engineering lead given to j_lead2. |
| 22 | admin | POST /settings/users/new/ | invite j_safety (temporary password set by the admin) | Account j_safety created. Now choose their role. |
| 23 | admin | POST /settings/users/885/ | give j_safety the role safety_reviewer | Role safety reviewer given to j_safety. |
| 24 | admin | POST /settings/users/new/ | invite j_founder (temporary password set by the admin) | Account j_founder created. Now choose their role. |
| 25 | admin | POST /settings/users/886/ | give j_founder the role founder | Role founder given to j_founder. |
| 26 | admin | GET /settings/users/ | (look at the page) | People and roles list shows every invited person |
| 27 | ops_lead | /login/ | username j_ops, the password the admin set | Signed in |
| 28 | ops_lead | POST /account/password/ | first sign-in: choose an own password instead of the temporary one | Password changed. Use your new password next time you sign in. |
| 29 | field_operator | /login/ | username j_field1, the password the admin set | Signed in |
| 30 | field_operator | POST /account/password/ | first sign-in: choose an own password instead of the temporary one | Password changed. Use your new password next time you sign in. |
| 31 | field_operator | /login/ | username j_field2, the password the admin set | Signed in |
| 32 | field_operator | POST /account/password/ | first sign-in: choose an own password instead of the temporary one | Password changed. Use your new password next time you sign in. |
| 33 | engineer | /login/ | username j_eng, the password the admin set | Signed in |
| 34 | engineer | POST /account/password/ | first sign-in: choose an own password instead of the temporary one | Password changed. Use your new password next time you sign in. |
| 35 | engineering_lead | /login/ | username j_lead, the password the admin set | Signed in |
| 36 | engineering_lead | POST /account/password/ | first sign-in: choose an own password instead of the temporary one | Password changed. Use your new password next time you sign in. |
| 37 | engineering_lead | /login/ | username j_lead2, the password the admin set | Signed in |
| 38 | engineering_lead | POST /account/password/ | first sign-in: choose an own password instead of the temporary one | Password changed. Use your new password next time you sign in. |
| 39 | safety_reviewer | /login/ | username j_safety, the password the admin set | Signed in |
| 40 | safety_reviewer | POST /account/password/ | first sign-in: choose an own password instead of the temporary one | Password changed. Use your new password next time you sign in. |
| 41 | founder | /login/ | username j_founder, the password the admin set | Signed in |
| 42 | founder | POST /account/password/ | first sign-in: choose an own password instead of the temporary one | Password changed. Use your new password next time you sign in. |
| 43 | engineering_lead | POST /fleet/patterns/new/ | configuration pattern P-J (a proposal) | Configuration P-J added. Next: publish its capability profile. |
| 44 | engineering_lead | GET /fleet/patterns/P-J/ | (look at the page) | Configuration page: no decision yet, the engineering lead gets the decide button |
| 45 | engineering_lead | POST /fleet/patterns/P-J/decision/ | approve configuration P-J for home use (confirm step) | Decision recorded for configuration P-J: approved for home use. |
| 46 | engineer | POST /fleet/patterns/P-J/profile/ | publish the capability profile (draft) | Capability profile v1 published as a draft. It still needs approval from the engineering lead and the safety reviewer. |
| 47 | engineering_lead | GET /fleet/patterns/P-J/profile/ | (look at the page) | Capability profile page shows the draft with its id and a link to the approval screen |
| 48 | engineering_lead | POST /fleet/patterns/P-J/profile/approve/49/ | capability profile v1: engineering approval (engineering lead) | Approval recorded for capability profile v1 (engineering slot). |
| 49 | safety_reviewer | POST /fleet/patterns/P-J/profile/approve/49/ | capability profile v1: safety approval (safety reviewer) | Approval recorded for capability profile v1 (safety slot). |
| 50 | engineering_lead | GET /fleet/patterns/ | (look at the page) | Configurations list: P-J has an approved profile |
| 51 | engineering_lead | POST /fleet/new/ | add robot R-J1 | Robot R-J1 added. It is not ready for anything yet: record its configuration next. |
| 52 | engineer | POST /fleet/R-J1/manifest/ | record R-J1's configuration manifest | Configuration recorded as a proposal. Engineering still has to approve it before the robot can be used. |
| 53 | engineering_lead | POST /fleet/R-J1/lifecycle/ | lifecycle: validation | Robot R-J1 moved to validation. |
| 54 | engineering_lead | POST /fleet/R-J1/lifecycle/ | lifecycle: in service | Robot R-J1 moved to in service. |
| 55 | engineering_lead | POST /fleet/R-J1/authorize/ | grant R-J1's base home-use authorization (confirm step) | Home-use authorization granted for R-J1. The robot still needs everything else on its readiness list. |
| 56 | ops_lead | POST /people/new/ | register P-1 for j_ops | P-1 registered. They have no qualification yet: that is a separate step. |
| 57 | ops_lead | POST /people/new/ | register P-2 for j_field1 | P-2 registered. They have no qualification yet: that is a separate step. |
| 58 | ops_lead | POST /people/new/ | register P-3 for j_field2 | P-3 registered. They have no qualification yet: that is a separate step. |
| 59 | ops_lead | POST /people/new/ | register P-4 for j_eng | P-4 registered. They have no qualification yet: that is a separate step. |
| 60 | ops_lead | POST /people/new/ | register P-5 for j_lead2 | P-5 registered. They have no qualification yet: that is a separate step. |
| 61 | ops_lead | POST /people/P-1/ | P-1: qualification field_lead | Qualification field lead granted to P-1. |
| 62 | ops_lead | POST /people/P-1/ | P-1: qualification robot_handler | Qualification robot handler granted to P-1. |
| 63 | ops_lead | POST /people/P-2/ | P-2: qualification field_operator | Qualification field operator granted to P-2. |
| 64 | ops_lead | POST /people/P-2/ | P-2: qualification robot_handler | Qualification robot handler granted to P-2. |
| 65 | ops_lead | POST /people/P-3/ | P-3: qualification field_operator | Qualification field operator granted to P-3. |
| 66 | ops_lead | POST /people/P-3/ | P-3: qualification robot_handler | Qualification robot handler granted to P-3. |
| 67 | safety_reviewer | POST /people/P-2/ | P-2: readiness_inspector qualification (safety reviewer) | Qualification readiness inspector granted to P-2. |
| 68 | safety_reviewer | POST /people/P-3/ | P-3: readiness_inspector qualification (safety reviewer) | Qualification readiness inspector granted to P-3. |
| 69 | ops_lead | POST /vehicles/new/ | register vehicle VAN-1 | Vehicle VAN-1 added. |
| 70 | engineer | POST /fleet/patterns/P-J/templates/create/ | inspection template E1 (engineering document reference) | Inspection template recorded; it needs engineering and safety approvals per purpose |
| 71 | engineering_lead | POST /fleet/patterns/P-J/templates/approve/ | template E1 approval: pre_departure / engineering | Template approval recorded |
| 72 | safety_reviewer | POST /fleet/patterns/P-J/templates/approve/ | template E1 approval: pre_departure / safety | Template approval recorded |
| 73 | engineering_lead | POST /fleet/patterns/P-J/templates/approve/ | template E1 approval: periodic / engineering | Template approval recorded |
| 74 | safety_reviewer | POST /fleet/patterns/P-J/templates/approve/ | template E1 approval: periodic / safety | Template approval recorded |
| 75 | engineer | POST /fleet/patterns/P-J/periodic-rules/create/ | periodic inspection rule v1 (90 days) | Periodic inspection rule version recorded; it needs engineering and safety approvals |
| 76 | engineering_lead | POST /fleet/patterns/P-J/periodic-rules/approve/ | periodic rule v1 approval: engineering | Periodic rule approval recorded |
| 77 | safety_reviewer | POST /fleet/patterns/P-J/periodic-rules/approve/ | periodic rule v1 approval: safety | Periodic rule approval recorded |
| 78 | engineering_lead | POST /readiness/policies/create/ | readiness policy v1 (draft) | Policy v1 created as a DRAFT; it needs both approvals |
| 79 | engineering_lead | POST /readiness/policies/1/approve/ | readiness policy v1: engineering approval (a second engineering lead; the author cannot approve) | Policy v1: engineering approval recorded |
| 80 | safety_reviewer | POST /readiness/policies/1/approve/ | readiness policy v1: safety approval | Policy v1: safety approval recorded |
| 81 | admin | GET /settings/operating/ | (look at the page) | Operating settings (read only): the owner's provisioned values are shown |
| 82 | field_operator | POST /readiness/robots/R-J1/inspection/ | periodic inspection of R-J1: all items pass, signed by two inspectors | Inspection RC-0065 recorded: it passed. |
| 83 | ops_lead | POST /planning/sites/new/ | add customer home H-J1 (adults unknown) | Home H-J1 added. Next: record an assessment and consent. |
| 84 | ops_lead | POST /planning/sites/H-J1/adults/ | expected adults: 2 | Number of adults saved. |
| 85 | field_operator | POST /planning/sites/H-J1/assessments/new/ | home assessment: eligible | Assessment recorded. It counts only after someone else approves it. |
| 86 | ops_lead | POST /planning/sites/H-J1/assessments/69/approve/ | approve the assessment (ops lead, not the assessor) | Assessment approved. |
| 87 | field_operator | POST /planning/sites/H-J1/consents/new/ | consent of adult P1 | Consent recorded for P1. |
| 88 | field_operator | POST /planning/sites/H-J1/consents/new/ | consent of adult P2 | Consent recorded for P2. |
| 89 | ops_lead | GET /planning/sites/H-J1/ | (look at the page) | Home reads ready |
| 90 | ops_lead | POST /planning/book/ | book a visit at H-J1 tomorrow 10:00-11:00 | Visit requested. Next: choose a robot, crew and vehicle. |
| 91 | ops_lead | POST /planning/visits/V-0031/plan/ | plan: robot R-J1, lead P-1, second P-2, vehicle VAN-1 | Plan saved. The result of the check is shown below. |
| 92 | ops_lead | POST /planning/visits/V-0031/confirm/ | confirm the visit (ops lead) | Visit confirmed. This is planning only: it is not approval to leave. |
| 93 | ops_lead | GET /readiness/visits/V-0031/ | (look at the page) | Visit is confirmed but not ready |
| 94 | field_operator | POST /readiness/visits/V-0031/inspection/ | pre-departure inspection through the form (two qualified inspectors) | Inspection RC-0066 recorded: it passed. |
| 95 | ops_lead | POST /readiness/visits/V-0031/evidence/ | record pre-departure inspection evidence (RC-0066) | Evidence recorded (pre_departure_inspection) |
| 96 | field_operator | POST /readiness/visits/V-0031/evidence/ | equipment check: restraint kit, recovery kit, charger present | Evidence recorded (equipment_check) |
| 97 | field_operator | POST /readiness/visits/V-0031/evidence/ | battery check: 85% (minimum 60%) | Evidence recorded (battery_check) |
| 98 | ops_lead | POST /readiness/visits/V-0031/recovery-plan/ | recovery plan: on-call P-4, escalation P-5 | Recovery plan recorded |
| 99 | ops_lead | GET /readiness/visits/V-0031/ | (look at the page) | Checklist reads complete, not approved |
| 100 | ops_lead | GET /ops/ | (look at the page) | Today (lookahead provisioned by the owner): 'Checklist complete, waiting for approval' (neutral, not green) |
| 101 | field_operator | GET /readiness/visits/V-0031/ | (look at the page) | A field operator sees no 'approve to leave' button |
| 102 | ops_lead | POST /readiness/visits/V-0031/authorize/ | ops lead requests approval to leave | Approved to leave by j_ops at Sun 22 Nov, 09:20 PST, valid until Sun 22 Nov, 10:50 PST, for visit V-0031 with robot R-J1. This is a time-limited decision; record the dep… |
| 103 | ops_lead | GET /readiness/visits/V-0031/ | (look at the page) | Visit page: Approved to leave, with the decision |
| 104 | ops_lead | GET /ops/ | (look at the page) | Today: the visit is Approved to leave (blue, never green) |

## Findings

- Readiness policy: the author cannot approve it, so a second engineering lead user is needed for the engineering slot (the journey creates two).
- No screen shows or sets demo mode; the journey therefore ran under production rules (approved procedure policy, engineering-document template and rule).
- Today still lists two 'Needs your attention' setup cards for the new workspace (escalation deadlines not set up, closeout time zone not set); both are deployment/owner settings with no screen.
