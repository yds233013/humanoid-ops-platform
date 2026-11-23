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

# Pass 2 (after the fix batch), candidate `8f52e5b`

Same method: local server of `8f52e5b`, demo rebuilt from empty, real browser at 1280 x 800 and 375 x 812; J1 on a
separate empty database (`humanoid_ops_j1`) with a one-time bootstrap token and owner provisioning (runbook 8a). Times
are scripted by someone who knows the product (lower bound). Five-person usability study: **UNVERIFIED** (no
participants).

| Journey | Role, width | Completed? | Time | Notes |
|---|---|---|---|---|
| J1 Setup from empty | first admin, then invited ops lead, desktop | yes: /setup/ token -> admin -> company, region, time zone -> hub OAK -> invite new.lead -> role ops lead -> new.lead signs in -> forced to "Choose your own password" (/ops/ redirected back) -> changed -> Today | 56 s | robots / configurations / approvals part of J1 covered by the 01C browser gate and `test_v2_01c_journey.py` |
| J2 Attention today | ops lead, desktop + 375 | yes | < 10 s | "N items need your attention" link at the top; "All N visits have a readiness result" |
| J3 Book a visit | ops lead, desktop | **yes**: request (words "laundry folding" accepted) -> Suggest -> Mon 10:00 "No suggestion: nobody qualified as lead is free" (all 3 crews booked; manual choice offered) -> moved to Sun 10:00 -> Suggest picked NB-001, Jordan Lee, Amara Okafor, VAN-1 with reasons -> Save plan -> Confirm | 44 s | no code guessing; labels show names and free/busy |
| J4 Approved to leave | ops lead, desktop | yes (V-0034, DA-0001) | 32 s | no unusable inspection offered; inspection preselected after submit; plain messages; "you can approve it now"; mode "Demo approval" |
| J5 Field visit | crew A lead, 375 | yes, crew part: lands on My visits -> departure (tick required) -> "Departed - run EX-0001" -> Next step card: verify -> arrive -> hand over to site -> site check (only offered after handover) -> start -> stop -> back to vehicle -> leave site -> arrive hub -> receive at hub -> post-visit inspection -> "Reconcile: recorded by the ops lead" | 138 s | online only; offline is Phase 03 |

All pages measured at 375 px: scrollWidth 375 (no sideways scroll).

## Pass 1 defects: status

J3-1, J3-2, J3-3, J4-1, J4-2, J4-3, J4-4, J5-1, J5-2, J5-3, J5-4, J5-5, J2-1, J2-2: **fixed and re-walked**.

## New defects found in pass 2 (none blocks a journey)

| Id | Sev | What | Proposed |
|---|---|---|---|
| P2-1 | Med | After the post-visit inspection the app returns to the readiness page with "Next: record it as pre-departure evidence" | return to the run page with "Post-visit inspection recorded. Next: the ops lead reconciles the return." |
| P2-2 | Med | Custody handover form lists everyone (P-101 Dana Ortiz first); the receiver defaults to the first person, not the run's crew | list the run's crew first and preselect lead -> second |
| P2-3 | Low | Inspection form "Done by / Checked by" show usernames, not Person labels | use Person.label |
| P2-4 | Low | Company / hub time zone is a free-text field | a select of common zones with free text as fallback |
| P2-5 | Low | My visits on a phone: short values break mid-word ("Tod ay", "Lea d") | no `overflow-wrap:anywhere` on short label cells |
| P2-6 | Low | When every crew is busy, Suggest says so but offers no next free time | Phase 03 (dispatch board) |

## Performance on `8f52e5b` (perf runs 45098461493 / 45098467132 / 45098471866, 200-robot fixture)

| Path | Ceiling | Target | p95 (three runs) |
|---|---|---|---|
| Today `/ops/` | 2,000 ms (hard) | 400 ms | 510 / 260 / 362: ceiling met 3/3, target met 2/3 |
| Exceptions inbox with readiness | 2,000 | - | 859 / 660 / 667 |
| Timeline | 2,000 | - | 783 / 623 / 595 |
| Fleet overview | 400 | - | 142 / 83 / 104 |
| Metrics | 400 | - | 198 / 120 / 148 |
