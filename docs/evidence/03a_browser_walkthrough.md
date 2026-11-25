# 03A browser walkthrough: hub-scoped permissions and transfers

Local server of `v2/phase03` (launch config `ops-app-accept`, database `humanoid_ops_accept`, runtime role `ops_app`).
The Northbay demo (one hub, OAK) was rebuilt on the 03A code. The second hub and its technician were then added
through the real setup screens. The walk used a real interactive browser (Claude desktop browser pane) at 1024 x 768,
with the transfer and fleet pages also checked at 768 x 1024. All data is fictional; the new user's password is a
local test value.

| # | Who | What happened on the page |
|---|---|---|
| 1 | priya.raman (admin) | Settings > Hubs > Add a hub: `SJC` "South Bay hub (fictional)" -> "Hub SJC added." |
| 2 | priya.raman | People and roles > Invite: `ravi.patel` with a temporary password -> "Account ravi.patel created. Now choose their role." |
| 3 | priya.raman | Give role **Hub technician** with hub "Every hub (regional roles)" -> **refused**: "Not done. Choose the hub where this person works as hub technician" (HTTP 422). |
| 4 | priya.raman | Give role Hub technician at **SJC** -> "Role hub technician given to ravi.patel at hub SJC." |
| 5 | lily.tran (hub technician + field operator at OAK) | Hub `?hub=SJC` -> "You hold no role at hub SJC, so nothing from it is shown." Fleet list: only OAK robots. |
| 6 | lily.tran | Robot NB-020 (no upcoming visit): "At hub OAK · home hub OAK" -> Move to another hub SJC, with a reason -> "On its way. It can be planned at neither hub until it is received at the other hub." Planning eligibility: "Not eligible for planning: In transfer TR-0001 from OAK to SJC". |
| 7 | lily.tran | Hub > Transfers > "Receive at SJC" -> **refused**: "Refused (permission denied) Only the ops lead or the hub technician can do this. Nothing was changed." (defect W1 below) |
| 8 | ravi.patel | First sign-in forced the password change ("Choose your own password ... Nothing else opens until you do"). Hub > Transfers > Receive at SJC -> "Received. It is now verified at this hub, which is its home hub. Holds were not changed." TR-0001 shows "Received at SJC". |
| 9 | ravi.patel | Robot page NB-020 -> 403 "No fleet access": a hub technician alone has no fleet pages, by the Phase 02 role matrix. lily.tran can see them only because she is also a field operator. |
| 10 | lily.tran | Robot page NB-020 -> 403 "This record belongs to a hub you do not work at, so you cannot open it. Ask your ops lead if you need it." Fleet list: 49 robots, NB-020 gone. |
| 11 | dana.ortiz (ops lead, regional) | Fleet `?hub=SJC`: NB-020 "At hub SJC, Eligible for planning, not cleared to depart". Robot page: "At hub SJC · home hub SJC". OAK visit V-0081 plan options: "NB-020 · at another hub". |
| 12 | dana.ortiz | NB-021 Move to SJC -> **refused**: "Robot NB-021 cannot move to hub SJC: it is booked for visit V-0071 until 2026-12-08 03:35 UTC. Nothing was changed." |
| 13 | dana.ortiz | Transfers page: NB-040 to SJC -> TR-0003 on its way; dana (regional) is offered Receive at SJC, Refuse receipt and Abandon (send back). |
| 14 | lily.tran (after fix W1) | Transfers page, TR-0003: only "Refuse receipt" (OAK is the source); no "Receive at SJC". |
| 15 | dana.ortiz, 768 px | Transfers and Fleet pages: document width 768, no sideways scroll; the hub filter shows All hubs, OAK, SJC. |

## Defects found

| Id | Sev | What happened | Fix |
|---|---|---|---|
| W1 | Low | The transfers page offered "Receive at SJC" to an OAK-only technician. The server refused it correctly, but the refusal message does not name the hub. | The page now offers receive and refuse only at hubs where the viewer holds a role (`e406c34`). The message wording is unchanged and is listed as an open item. |

## Not shown in this walk

- The planning "At risk: robot away until verified back at hub" case: the demo has no robot out on an open run right
  now. It is covered by `tests/test_v2_03a_planning.py`.
- Stock transfers: covered by tests, not walked.
- Times in refusal messages are UTC ("until 2026-12-08 03:35 UTC"), not the workspace time zone. This is listed as
  an open item.
- Usability with independent people: UNVERIFIED.
