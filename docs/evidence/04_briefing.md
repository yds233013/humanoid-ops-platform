# Phase 04: Operations briefing (`/ops/briefing/`)

Status: built and tested on branch `v2/04-briefing`. Usability: **UNVERIFIED**. No outside participants have used it. The design review below is the builder's own check, not a usability study.

## What it is

The Operations briefing is one read-only page for ops leads. It is linked as **Today › Briefing** in the ops navigation. It is **not an AI and not an agent**: this app has no language model. Every sentence comes from fixed rules in `backend/operations/briefing.py`, applied to the read models the other pages already use. That keeps the briefing's answers in line with those pages.

For each hub the viewer can see, it answers four questions. Hub visibility uses the same `hub_scope` rules as the other pages, and `?hub=` narrows the page to one hub.

| # | Question | Built from (no parallel truth) |
|---|---|---|
| 1 | Which robots are unavailable for deployment, and why | `readmodels.fleet_overview` (the Robot status rule), the eligibility reasons (`planning.plain.robot_reason`: holds, maintenance, transfer, inspections, lifecycle), the departure gate's own SQL function `execution_robot_departure_problem` (its `ROBOT_INTERRUPTION_UNRESOLVED` case), the charge forecast (`hub.charging`), and active regional pauses (the board's banner text) |
| 2 | Which visits are at risk today | `planning.board.build_board` for the local today (plan check, live robot location and eligibility, travel, crew, vehicle, charge, stock, pause), combined with `operations.today.build_today` (Today's row, from the readiness evaluator) |
| 3 | Which unresolved incidents need attention | `readmodels.incident_queue` (queue order: unclassified first, then severity, then age), plus Today's issue cards for escalations that are overdue or not acknowledged |
| 4 | What to prioritise at each hub | The fixed rules below, applied only to the lines that questions 1–3 show |

Every line links to its source record (a robot, visit, incident or pause page) and names its source, for example "Source: Robot record · Interruption history". The page has no forms and offers no actions. It answers GET only, and other methods get a 405. It runs inside the same REPEATABLE READ READ ONLY snapshot as the other ops pages. The footer reads: "This briefing summarises operational records. It cannot authorize, dispatch or control robots."

## Rules for prioritising (shown on the page under "How the priorities are ordered")

Rules are applied lowest number first. Within one rule, the earliest departure or oldest report comes first, then the lowest code. At most 5 lines are shown per hub, followed by "N more below".

1. A robot nobody can locate (unaccounted, or custody unknown).
2. An incident nobody has triaged, or an escalation that is late or not acknowledged.
3. A robot with an unresolved interruption.
4. A visit today that is blocked.
5. A departure pause at the hub, with how many of today's visits it covers.
6. A visit today that is at risk.
7. An incident with no severity decision.
8. Visits today whose checklist was not checked (not enough data).

A visit's level is the worse of the board's status and Today's readiness status, in this order: blocked, then paused, then at risk. A visit with no readiness result on this page load is never called clear. It goes into an explicit "Not enough data to say whether N visits … are at risk" line, which says why and links each visit.

Two more facts come from the visit record itself:
- A visit that is not confirmed yet is shown as at risk.
- A visit whose time has passed with no departure recorded is shown as at risk.

## Insufficient data (said, never guessed)

The page says "Not enough data to say" in these cases:

- **Charge forecast off:** the battery setting is not provisioned, so no charge forecast is made.
- **No recent battery level:** listed per robot, with links.
- **Readiness look-ahead setting missing.**
- **Visits beyond the readiness cap:** at most 10 visits are checked per page load.
- **No escalation deadlines:** without them, late escalations cannot be shown.
- **Issue list over 200 items:** some late escalations may not be marked.
- **A source fails to load:** that read runs in its own savepoint, and the page shows a "Not enough data … could not be loaded" notice instead of a zero.

## Scenarios (`backend/tests/test_v2_04_briefing.py`)

Each scenario checks the expected facts and also checks prohibited claims. The prohibited claims checked on every page:
- no "ready / approved / cleared to leave" wording;
- no AI or agent wording;
- no form and no authorize, start or dispatch action;
- the footer is present.

| # | Scenario | Expected | Prohibited |
|---|---|---|---|
| S1 | Checklist complete, charge forecast off | The visit is counted as having no open problem, "That is not permission to leave"; "Not enough data … charge" | "Checklist complete", "ready" |
| S2 | Readiness fails (no checks recorded) | At risk, with the checklist reason and source; priority rule 6 | Counted as clear |
| S3 | Robot on hold, booked today | Robot "On hold" with the hold code; visit Blocked; rule 4 | Visit clear or only at risk |
| S4 | Unresolved interruption (03D) | "R-01 — Unresolved interruption. Departure authorization is unavailable until the interruption is resolved."; rule 3 | "Not at the hub" |
| S5 | Travel time unknown | Travel reason, "Visit plan · Travel times" | An estimated time |
| S6 | Crew and robot gaps (requested visit) | Blocked: no robot chosen, two people needed; rule 4 | "Available for planning" |
| S7 | Regional pause | Pause line "none can be authorized to leave"; visits Paused; rule 5 first | Any visit counted as clear |
| S8 | Untriaged incident | "Not triaged yet", "No severity decision", the hold; robot on hold; rule 2 first | Shown as being handled |
| S9 | Unaccounted robot | "Cannot be accounted for"; rule 1 first ("Find robot … and record where it is.") | "Out on a visit" |
| S10 | Hub-scoped viewer (two hubs) | Hub B's robot, visit and incident only; regional viewer sees both; `?hub=` narrows | Any hub A record or name; `?hub=HUB-1` leaks nothing |
| S11 | Look-ahead setting missing | One "Not enough data to say whether 2 visits …" line; rule 8 | Either visit called at risk, blocked or clear |
| S12 | Robot out on a run | Listed under "out on a visit" with run and visit codes | Departed visit at risk; robot listed for a fault |

Other tests:
- **Grounding:** in a mixed world, the page equals `build_board` + `build_today` + `fleet_overview` + `incident_queue` read in one snapshot.
- **Priority order and limit.**
- **Permissions:**
  - A hub technician, or a user with no roles, gets 403 on `/ops/briefing/` exactly as on `/ops/`.
  - A signed-out user gets no data.
- **Navigation link.**
- **Write boundary:** POST, PUT, PATCH and DELETE get 405, and the page has no form, button or input.
- **Read-only snapshot.**
- **Statement budget:** 119 statements, the same with 5 robots as with 9 robots plus more incidents, holds and requested visits. The bound is Today's budget + 50 = 1,022.
- **No AI or agent wording.**

## Screenshots (demo workspace, regional ops lead, real Chromium via Playwright)

- `04_briefing/briefing_1024.png`
- `04_briefing/briefing_768.png`
- `04_briefing/briefing_375.png`

`document.scrollWidth` equals the viewport width at all three sizes, so there is no horizontal scroll.

## Limits

- **Readiness is capped:** at most 10 visits are checked per page load, the same as Today. Busy hubs will show a "Not enough data" line for the rest. Opening the visit shows its checklist.
- **Battery state is "if it left now":** the forecast for robots that are otherwise available uses the snapshot time as the departure time. For a visit's own forecast, question 2 uses the board.
- **Field operators can read the page:** they hold `operations.read`, like on every ops page, and see only their hub. Users without operations read (for example a hub technician only) get 403. See deviations.
- **Priorities use only listed facts:** they do not weigh business impact, staffing or customer importance.
- **Usability:** UNVERIFIED.

## Deviations

- **Field-only users:** "field-only user gets 403/404 like other ops pages" was implemented as "the same permission as the other ops pages" (`operations.read`). In this app field operators do hold that permission and see the ops pages scoped to their hub. They are also the only hub-scoped readers, and the cross-hub test (S10) relies on them. The 403 is proven for a hub technician and for a user with no roles.
