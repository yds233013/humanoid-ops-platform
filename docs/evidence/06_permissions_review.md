# 06 Permissions review: roles x pages and APIs, with hub scope

Phase 06, engineer 3. Base `0111a9f`, branch `v2/06-docs`. Synthetic data only. Checked by the development team
only.

**Verified by** `backend/tests/test_v2_06_permissions_matrix.py` (12 tests, all passing on `humanoid_ops_06d`). The
matrix test makes one real request per cell (Django test client, real PostgreSQL) and pins the HTTP status. On
every page a hub-scoped viewer can open (status 200), it also checks that the other hub's robot, incident and run
codes are absent. Regional viewers are checked to see both hubs on the regional pages.

## World

The execution world at hub `HUB-1` (robot `R-01` departed on a run; crew P-1 is the ops lead and P-2 a field
operator; an incident on that run) plus hub `HB` with robot `RB-1` and an incident on it.

| Row | Who | Grant |
|---|---|---|
| ops | Regional ops lead | `ops_lead`, no hub (ops leads are always regional: the database refuses a hub on this role) |
| tech A / tech B | Hub technician | `hub_technician` at HUB-1 / at HB |
| crew A | Field operator on the run's crew | `field_operator` at HUB-1 |
| field A | Field operator, not on this crew | `field_operator` at HUB-1 |
| field B | Field operator at the other hub | `field_operator` at HB |
| safety | Safety reviewer | `safety_reviewer`, regional |
| pause A | Safety pause holder | `safety_pause` at HUB-1 only |
| admin | Workspace admin | `admin` |
| no role | Signed in, no grant | none |
| anon | Signed out | none |

## Matrix (HTTP status; "HUB-1" and "HB" pages are records of that hub)

`302` for anon means "sent to sign in". `-` means the cell was not requested. `S` marks a page that is filtered to the viewer's hubs.

| Page or API | ops | tech A | tech B | crew A | field A | field B | safety | pause A | admin | no role | anon |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Today `/ops/` | 200 | 403 | 403 | 200 S | 200 S | 200 S | 200 | 403 | 403 | 403 | 302 |
| Operations briefing `/ops/briefing/` | 200 | 403 | 403 | 200 S | 200 S | 200 S | 200 | 403 | 403 | 403 | 302 |
| Issues `/ops/exceptions/` | 200 | 403 | 403 | 200 S | 200 S | 200 S | 200 | 403 | 403 | 403 | 302 |
| Dispatch board `/planning/board/` | 200 | 403 | 403 | 200 S | 200 S | 200 S | 200 | 403 | 403 | 403 | 302 |
| Departure readiness `/readiness/` | 200 | 403 | 403 | 200 S | 200 S | 200 S | 200 | 403 | 403 | 403 | 302 |
| Readiness of a HUB-1 visit | 200 | 403 | 403 | 200 | 200 | 403 | 200 | 403 | 403 | 403 | 302 |
| Fleet list `/fleet/` | 200 | 403 | 403 | 200 S | 200 S | 200 S | 200 | 403 | 403 | 403 | 302 |
| Robot at HUB-1 / at HB | 200 / 200 | 403 | 403 | 200 / 403 | 200 / 403 | 403 / 200 | 200 / 200 | 403 | 403 | 403 | 302 |
| Hub work queue `/hub/` | 200 | 200 S | 200 S | 200 S | 200 S | 200 S | 200 | 403 | 403 | 403 | 302 |
| Book a visit (form) | 200 | 403 | 403 | 200 (no form: "Only the ops lead can request, plan or cancel visits.") | 200 (same) | 200 (same) | 200 (same) | 403 | 403 | 403 | 302 |
| Visit plan and Review options (HUB-1) | 200 | 403 | 403 | 200 | 200 | 403 | 200 | 403 | 403 | 403 | 302 |
| Save a plan (POST) | ops only; anyone else: refused by the guarded command, no revision written | | | | | | | | | | |
| Field run page (HUB-1) | 200 | 403 | 403 | 200 | 200 | 403 | 200 | 403 | 403 | 403 | 302 |
| Field runs list, My visits | 200 | 403 | 403 | 200 S | 200 S | 200 S | 200 | 403 | 403 | 403 | 302 |
| Incident at HUB-1 / at HB | 200 / 200 | 403 | 403 | 200 / 403 | 200 / 403 | 403 / 200 | 200 / 200 | 403 | 403 | 403 | 302 |
| Visit dossier (HUB-1) | 200 | 403 | 403 | 200 | 200 | 403 | 200 | 403 | 403 | 403 | 302 |
| Field snapshot `/api/field/runs/<run>/snapshot` | 200 | 403 | 403 | 200 | 403 | 403 | 403 | 403 | 403 | 403 | 401 |
| Field sync `/api/field/sync` | 200 | 403 | 403 | 200 | 403 | 403 | 403 | 403 | 403 | 403 | 401 |
| Field abandon `/api/field/abandon` | 409 (passes access; stale generation) | 403 | 403 | 409 (same) | 403 | 403 | 403 | 403 | 403 | 403 | 401 |
| Audit log API `/api/audit-events` | 200 | 403 | 403 | 403 | 403 | 403 | 200 | 403 | 200 | 403 | 403 |
| Settings: zones of HUB-1 / HB, travel times | 200 | 403 | 403 | 403 | 403 | 403 | 403 | 403 | 200 | 403 | 302 |
| Settings: workspace setup | 403 | 403 | 403 | 403 | 403 | 403 | 403 | 403 | 200 | 403 | 302 |
| Regional pause page `/planning/pause/` | 200 | 403 | 403 | 200 S (no pause buttons) | 200 S | 200 S | 200 | 200 S | 403 | 403 | 302 |
| Pause confirm, HUB-1 / HB | 200 / 200 | 403 | 403 | 403 | 403 | 403 | 403 | 200 / 403 | 403 | 403 | 302 |
| Lift a pause (GET of the lift page) | 200 | - | - | 403 | - | - | - | 403 (never lifts) | - | - | - |

Every field-API refusal is the same `NOT_YOUR_RUN` body with no robot, visit or run details.

## Audit and history (coordinator item from 06-e2e)

- `/api/audit-events` returns the latest 200 audit rows **without a hub filter**. It is safe only because every
  `audit.read` role (admin, ops lead, engineering lead, safety reviewer, founder) is regional, and the database
  check constraint `role_grant_hub_scope` refuses a hub-qualified grant of any of them. The test pins both facts:
  it tries to create each audit role at a hub and expects an `IntegrityError`, and every hub-scoped row and the
  no-role user get 403 with no robot, run or hub codes in the body. **No leak today.**
- Risk to keep in mind: if a hub-scoped role ever gains `audit.read`, this API leaks across hubs at once. The
  test fails first (`test_the_audit_log_is_regional_only_and_no_hub_scoped_grant_can_read_it`).
- History pages a hub-scoped viewer can open keep hub scope: the regional pause history (a HUB-1 pause reason is
  shown to crew A and hidden from field B), the visit dossier and incident details (403 to the other hub).

## Landing after sign-in

Crews land on My visits, hub technicians on the hub work queue, readers on Today, the admin on workspace setup.
Each landing page opens (200) for that role. A person with no role lands on Fleet and is refused (they hold no
role).

## Wording: "regional ops leads" (EM condition, 03D ruling)

The offline run page listed contacts under "Ops leads for this hub", but ops leads are regional (no hub). Changed in
`backend/execution/field_static/run.js` to "Regional ops leads (they cover every hub, this one included)", and the
empty state to "No regional ops lead is listed on this phone. Call your hub." The browser test
`test_protective_final_rejection_keeps_the_banner_and_names_the_ops_lead` now also asserts the wording. The
architecture doc's sync access rule now says "a regional ops lead". Other run-page sentences ("ask your ops lead",
"Contact your ops lead") are generic and stay.

## Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| P-1 | low | The operations pages (Today, briefing, Issues) refuse a role without `operations.read` with a bare sentence ("You do not have access to the operations command center.") and no page frame or navigation. No data is shown. A hub technician never lands there, and the nav does not link to it. | Open (not fixed: feature freeze, cosmetic) |
| P-2 | low | A no-role user lands on Fleet and sees a refusal. Correct, but there is no "ask your admin for a role" hint. | Open |
| P-3 | info | The plan POST has no page gate. The guarded plan command refuses, and the page says "Only the ops lead ...". No revision is written. | By design |

No cross-hub data was found in any cell.
