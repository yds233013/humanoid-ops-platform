# 01D acceptance walkthrough, pass 1 (engineering evidence, not a usability study)

Date 2026-11-22. Candidate `21d86a9` on a local server (`ops-app-accept`, port 8006, database
`humanoid_ops_accept` built from empty with `build_demo_workspace` in 18.6 s; runtime role `ops_app`). A real
browser tab at 1280 x 800 and at 375 x 812. Logged in as the demo users (local dev password); Claude does not
type passwords into hosted sites, and staging runs the same code.

The walker knows the product, so the times below are a lower bound, not a first-time user's time. The five-person
usability study has no participants yet: that criterion is **UNVERIFIED**.

## Results

| Journey | Role, width | Completed? | Time (scripted) | Verdict |
|---|---|---|---|---|
| J2 What needs attention today | ops lead, desktop + 375 | yes, first screen | < 10 s | pass with 2 low frictions |
| J4 Get a visit to "Approved to leave" | ops lead, desktop | yes (V-0034, DA-0001) | 79 s | pass, but 1 high friction (wrong inspection offered) |
| J3 Book a visit | ops lead, desktop | **no**: request made (V-0086), plan blocked after 3 guesses | 44 s, abandoned | **fail**: the plan step gives no availability guidance |
| J5 Field visit on a phone | crew A lead, 375 | departure -> custody verified -> arrived -> custody to site -> Go check -> session active; return leg not walked | 59 s to session start | usable online, 2 high frictions |
| J1 Setup from empty | - | not re-walked in pass 1 (01C gate + journey test cover it); re-walk after the fixes, with the new forced password change | - | pending |

All pages measured fit 375 px (scrollWidth = 375): Today, readiness visit, run page, field runs.

## Defects

| Id | Sev | Journey | What happened | Fix |
|---|---|---|---|---|
| J3-1 | High | J3 | Plan step: robots are 45 bare codes, people are P-codes without names, nothing says who or what is free, qualified or ready for the window. Three attempts were all blocked (unqualified lead, crew busy, vehicle busy, robot not ready). | Option labels with names and status for the visit window (free / busy until / not qualified / not ready), available first; a read-only "Suggest a free robot, crew and vehicle" that fills the form. Saving still goes through the guarded command. |
| J3-2 | Med | J3 | "Robot NB-012 is not ready for planning" gives no reason. | Name the reason (on hold, inspection overdue, not in service) and who can fix it. |
| J3-3 | Med | J3 | Tasks refuses "laundry folding" (spaces); the user must invent a code. | Accept words: turn spaces into underscores before validation, help text with examples from the workspace. |
| J4-1 | High | J4 | The pre-departure inspection list offers a routine inspection (RC-0043); recording it is refused with a raw "Refused (conflict)". | Offer only inspections usable for this visit; if none, make "Submit a new inspection" the primary next step. |
| J4-2 | Med | J4 | After submitting the visit inspection, it still has to be picked again and recorded as evidence; nothing says so. | Return to the visit with it preselected and a "Next: record it as evidence" message. |
| J4-3 | Low | J4 | Raw codes: "Evidence recorded (pre_departure_inspection)", item names restraint_kit / recovery_kit / charger, "Record a equipment check", mode "Demo (synthetic)". | Plain labels; "an equipment check"; mode "Demo approval". |
| J4-4 | Low | J4 | "Checklist complete - ask the ops lead to approve" is shown to the ops lead. | "You can approve it now" when the viewer may approve. |
| J5-1 | High | J5 | A crew member lands on the company-wide Today; there is no "my visits" view, and Field runs is not in the navigation. | "Your visits today and tomorrow" at the top of Today for field roles (links to readiness / run); Field runs under Dispatch. |
| J5-2 | High | J5 | The run page has no next-step guide; it let the crew record a site check before the robot was in site custody, which recorded a permanent No-Go. | A "Next step" card derived from the run state (verify custody -> arrived -> hand over to site -> site check -> start session -> ...); forms that cannot succeed yet are folded with the reason. Rules unchanged. |
| J5-3 | Med | J3, J4, J5 | People everywhere are P-codes; jordan.lee cannot tell he is P-111. | `Person.label` ("P-111 · Jordan Lee") in every person select and crew line; fictional names for demo users. |
| J5-4 | Med | J5 | "Record departure" has no confirmation; afterwards the readiness page says "Readiness incomplete ... Blocked". | Confirm step; after departure show "Departed, run EX-0001" with a link instead of the checklist verdict. |
| J5-5 | Low | J5 | Custody move to site refused "A handoff needs a receiver"; the form does not say Receiver is required. | Mark it required for a handoff and explain. |
| J2-1 | Low | J2 | "Only 10 of 10 visits have a readiness result" when all are assessed. | "All 10 visits have a readiness result." |
| J2-2 | Low | J2 | On a phone, "Needs your attention" is far below the counts. | A jump link "N items need your attention" at the top. |
