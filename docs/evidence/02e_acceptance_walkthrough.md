# 02E acceptance walkthrough: J8, J2, J3, J4 in a real browser

Branch `v2/phase02`. Local server of the integration branch (launch config `ops-app-accept`, database
`humanoid_ops_accept`, runtime role `ops_app`), Northbay demo rebuilt from empty with `build_demo_workspace --reset`
(build of 2026-11-25, 21.6 s). Real interactive browser (the Claude desktop browser pane): clicks, selects and form
submits on the real pages, at **1024 x 768** (desktop) and **768 x 1024** (tablet). Not a scripted HTTP client.
All data is fictional. Times are from someone who knows the product, so they are not usability evidence:
**independent usability: UNVERIFIED** (no external participants).

## J8 Hub day (lily.tran, hub technician, 1024 px)

| Step | What happened on the page |
|---|---|
| Open Hub | 29 open tasks: 2 charging, 3 inspection due, 16 maintenance due, 3 kits low, 3 robot on hold, 2 other; "A task is a to-do only: it never releases a hold, records an inspection or changes readiness." |
| Assignment | WO-0015 "Restock Restraint kit (RK): below its minimum of 2" -> Take this task -> "You took the task." (owner P-110 Lily Tran) -> Start -> "Task started." |
| Kit shortage | Stock: RK 18 on hand / 18 reserved / 0 available, below minimum; "V-0034 ... Restraint kit, needs 1, At risk: not enough available"; Today showed V-0034 "At risk - Kit short". |
| Restock | Receive 2 RK, note "SYNTHETIC demo delivery" -> "Stock received and added to the ledger." RK 20 / 18 / 2; V-0034 now "Not reserved yet". |
| Reserve | Reserve 1 RK for V-0034 -> "Stock reserved for the visit. The departure equipment check is still required." "Every visit in the next two days has what it needs reserved or issued." |
| Refusal | Record use of 99 GLOVE -> "Refused (conflict) - Only 3 boxes of Nitrile gloves (GLOVE) are on hand. Nothing was changed." |
| Charging | NB-009 at 46% "Needs charge" (80% demo minimum) on V-0026 and V-0033; start charging NB-009 on C-3 at 46% -> "Charging started. This records the session only; it is not a battery check." |
| Maintenance due | /fleet/NB-042/maintenance/: SENSOR-CLEAN v1 (Demo value) Overdue 1 of 1 visits (from the closed practice run), GRIPPER-PADS due soon; Today showed V-0036 (NB-042) Blocked "Maintenance SENSOR-CLEAN overdue". Recorded SENSOR-CLEAN -> "Maintenance recorded. It never releases a hold; a required post-maintenance inspection is recorded separately." V-0036 no longer blocked. |
| Work-order completion | WO-0015 Mark done -> "Task marked done. This records the task only; the underlying record is unchanged." Stock RK unchanged (20 / 19 / 1); because RK is still below minimum the Hub derived a new restock task. |
| Inspection due / holds | Hub lists 3 inspection-due tasks (NB-046, NB-047 due soon, NB-048 never inspected) and 3 held robots (NB-019, NB-012, NB-007). |
| Role refusal | sam.keller (engineer): Stock page readable, no forms rendered; a POST to /hub/stock/receive/ returned 403. |

## J2 Today (lily.tran 1024 px; dana.ortiz 768 px)

Today shows battery, maintenance and inventory facts: "Needs charge before departure" (NB-009), "Maintenance SENSOR-CLEAN
overdue ... Blocked" (V-0036), "Kit short" (V-0034), holds (NB-019 booked on V-0078 while on hold).

## J4 Get a robot ready and send it (dana.ortiz, ops lead, 768 px)

V-0031 (NB-024, crew A, tomorrow 09:30, RK reserved): readiness missing only the day's checks -> Submit a new
inspection (all items Pass, performer P-111 Jordan Lee, verifier P-101 Dana Ortiz) -> "Inspection RC-0055 recorded: it
passed. Next: record it as pre-departure evidence below." -> Record inspection evidence -> equipment check (restraint
kit, recovery kit, charger present) -> battery check 94% -> "Checklist complete - you can approve it now." -> Request
approval to leave -> **DA-0003** "Approved to leave ... valid until 03:36 ... record the departure separately". Negative:
V-0078 (NB-019 on maintenance hold HD-0001) "5 items left", no approval action offered.

## J3 Book a visit

After fix D6 and a fresh rebuild, dana.ortiz at 1024 px: V-0081 (H-120, requested, Mon 30 Nov 18:30) -> "Suggest a free
robot, crew and vehicle" -> "Suggested assignment - Not yet saved ... revalidated when saved": NB-001 (free, charge
forecast unknown: "it must be charged and pass the battery check before it leaves"), lead P-111 Jordan Lee, second
P-112 Amara Okafor, VAN-1 -> Save plan -> "Plan saved." V-0081 Planned, revision 2, **Planning feasible**, "Nothing is
blocking this plan", "Planning only - not cleared to depart". Page width 1024 (no sideways scroll). Before the fix all
three requests showed "No suggestion" (D6).

## Tablet (768 px)

Today, Hub, Stock, Charging, Fleet health, planner and readiness pages: document width 768 (no sideways scroll) after
fix D5; tables fit (734/734); health page shows "Recorded availability is not measured uptime", the
current-configuration note and the practice-run note.

## Defects found in this walk

| Id | Sev | What happened | Fix |
|---|---|---|---|
| D3 | Low | Today said "Readiness checked for 10 of 13 visits" next to the tile "Readiness assessed: 12" (the tile also counts visits whose status comes from holds, maintenance, charge or stock; the full checklist runs for at most 10 per load). | Notice now: "... (at most 10 per page load). 2 more visits have a status from other records (holds, maintenance, charge or stock)." Test added. `62ff1e3` |
| D5 | Med | At 768 px the primary nav scrolled sideways with no cue (Issues hidden), and the planner robot select (long status labels) made the page 1174 px wide (also wider than 1024). | Nav wraps onto its own line at 701-900 px; `.row>label{min-width:0}` and `select{max-width:100%}`. Re-measured: 768. `62ff1e3` |
| D6 | High | J3: "Suggest a free robot, crew and vehicle" returned "No suggestion" for every open request (V-0081, V-0082, V-0083): all three crews booked in those windows. | Demo builder moves the three requests to an 18:30 window where a crew, van and robot are free; a demo test asserts Suggest returns a full plan for all three and that saving V-0081 is Planning feasible. `a4f2e4c` |
