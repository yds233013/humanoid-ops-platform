# 03B design specification: regional dispatch

Status: **draft for EM review**, 2026-11-25. Written under the EM's approval of the product owner's direction:
*"make it as real and nice as possible with a good taste"*. This spec is part of the 03B acceptance bar (contract 13). It
comes before any 03B code. Every number and name in it is fictional.

## 0. What is wrong today (the starting point)

The Dispatch page (`/planning/`) is a week-long list of visits grouped by day. Each card leads with codes:
"V-0017 · H-121 · Piedmont · CRM reference CRM-48470", then a status chip and "NB-013 · VAN-1".

It answers "what is booked" but not the ops lead's real questions:
- what needs me now;
- which visits are at risk, and why;
- who can fix it;
- what changed after I acted.

Nothing says which hub or zone a visit belongs to. Crew names are missing, and status depends on reading chips.

## 1. Principles

1. **Decisions before inventory.** Every screen opens on what needs a decision. The full list comes second.
2. **Names first, codes second.** For example "Oakland hub (OAK)", "Piedmont home, H-121", "Jordan Lee and Amara
   Okafor", "Robot NB-024". Codes stay visible for traceability, in a quieter style.
3. **One obvious next action per problem.** A problem card names the action and who can take it. If the viewer
   cannot take it, it says who can, without revealing another hub's details.
4. **Planning is never departure.** Every planning result reads "Planning feasible: not cleared to depart" or names
   what blocks it. Nothing on the dispatch board looks like an approval to leave.
5. **Status is words plus shape, never colour alone.** Each status is a text label with an icon (● feasible, ▲ at
   risk, ■ blocked, ◆ paused) and a colour from the existing tokens, which are tested at 4.5:1.
6. **Local time, with the zone.** For example "Thu 26 Nov, 14:00 PST". No UTC anywhere a person reads, including
   refusal messages.
7. **Calm density.** Whitespace and alignment follow the existing design system (`docs/ui_components.md`, the
   `base.html` tokens). There are no new colours, no decorative badges and no animation. One accent: the primary
   action.
8. **Honest emptiness.** Every list has a written empty state ("No visits need attention at Oakland hub today"). An
   unknown value is shown as Unknown, with what would make it known.

## 2. Screens

Each screen lists:
- the primary user and the decision it supports;
- the actions;
- the states: empty, loading (server-rendered, so "loading" means a slow page, covered by the perf budget), blocked,
  refused and error;
- the layout at 1024, 768 and 375 px.

### S1. Dispatch board (`/planning/board/`, the new default under Dispatch)

- **User:** the regional ops lead; a hub-scoped viewer sees only their hubs.
- **Decision:** what to fix first today and tomorrow.

**Layout at 1024 px, top to bottom:**
1. **Header:**
   - "Dispatch", with the day picker (Today, Tomorrow, then a date) and the hub filter (All hubs, Oakland, South
     Bay, Marin), kept on every link;
   - one line stating the planning/departure distinction.
2. **Summary strip:** three numbers, each a link: "Visits today 24", "Need attention 3", "Hubs paused 0".
3. **Needs attention:** at most 5 cards, newest risk first, with "Show all (N)". Each card shows:
   - visit time;
   - home name and area;
   - hub;
   - the one reason in plain words, for example "Robot still on an earlier visit; expected back at Oakland hub by
     12:40";
   - one primary action ("Review options");
   - who can act.
4. **By hub:** one section per hub, with:
   - a header: hub name, zones, robots available / away / held, crews on shift, a pause banner if paused;
   - a time-ordered list of visit rows: time, home and area, crew names, robot, vehicle, status (words + shape),
     zone chip.

   Rows are compact and the whole row is the link.

**At 768 px:** the same order in one column. Hub sections collapse to their header plus the attention rows; "Show all
visits" expands them.

**At 375 px:**
- the summary strip and Needs attention only;
- each hub is a link to its own list;
- no horizontal scroll, and tap targets of at least 44 px.

**States:**
- **Empty:** "No visits planned for {day} at {hub}." with a "Book a visit" action if allowed.
- **Refused hub:** "You hold no role at {hub name}. Nothing from it is shown." (exists today).
- **Paused hub:** a banner, "Departures from Oakland hub are paused since 09:10 PST: {reason}. Visits can still be
  planned; none can be authorized to leave."

The week view stays as "Week list", one click away.

### S2. Review options and reassign (a panel on the visit page, opened from the board)

- **User:** the ops lead.
- **Decision:** a change to the robot, crew, vehicle or time that makes this visit plannable.

**Content:**
1. **"Why this visit needs attention":** the blocking and at-risk reasons, in plain words.
2. **Options, best first.** Each option is one line, for example "Robot NB-031 · at Oakland hub · charge
   enough".
   - Free options come first, then at risk, then not possible (collapsed).
   - Not-possible options show the reason; they are never hidden silently.
3. **Proposed change and "What we checked",** listed before saving with a result per line (✓ / ▲ / ✕):
   - robot location (at hub, away on a same-hub run with the expected return, transfer, other hub);
   - time overlap;
   - crew qualification;
   - vehicle;
   - maintenance;
   - holds;
   - travel time (from the approved matrix, version shown; "Travel time unknown" if the pair is missing);
   - regional pause.
4. **Save plan.** Afterwards the panel shows **"What changed"** (before → after, per field) and the result:
   "Planning feasible: not cleared to depart" or the blocking reasons. Saving runs the guarded command again.
   The preview is advisory only.

**States:**
- **No option works:** "No free robot, crew and vehicle for this time." plus the **next free times** (S3).
- **Refused:** "You can't change visits at South Bay hub. Ask a regional ops lead."

### S3. Suggest and next free time (P2-6)

- **Decision:** when to book a request so it can actually be served.
- **Where:** in "Book a visit" and in S2 when nothing fits.
- **Shows:** up to three next free slots at the visit's hub, each with its robot, crew and vehicle and a one-line why,
  for example "Tue 1 Dec, 18:30 PST · Jordan Lee and Amara Okafor · NB-001 · VAN-1".
- **Search window:** a configurable look-ahead. A missing travel pair makes a slot "Travel time unknown"; it is
  never proposed as free.
- **Empty state:** "No free time in the next 7 days at Oakland hub. Widen the search or add crew time."

### S4. Regional pause (`/planning/pause/`)

- **User:** the ops lead.
- **Decision:** stop new departures for a hub or zone (weather, an incident nearby).
- **Pause form:** a hub or a zone, and a reason (required).
  - Before confirming it shows the affected visits: "This blocks authorization and departure for 6 visits today".
  - It then asks for confirmation.
- **Lift form:** a reason (required). Both are audited.
- **Banner:** a paused hub shows the banner on the board, the visit pages and the readiness pages.
- **Out of scope:** recall, and anything automatic.

### S5. Zones and travel times (Settings > Hubs > {hub} > Zones; Settings > Travel times)

- **User:** an admin or ops lead (setup).
- **Zones:** a list per hub, with name and code.
- **Travel matrix:** a zone × zone grid of minutes.
  - Missing cells read "Unknown", in the at-risk style, not blank.
  - Every value carries its source (company value, or demo value) and the version.
  - Editing creates a new version; the history is one click away.

### S6. Messages (applies everywhere)

**Refusal formula:** what was refused, why in one clause, what to do or who can.
- "You can't receive this transfer at South Bay hub (SJC). Ask a regional ops lead or a South Bay hub technician."
- "Robot NB-021 can't move to South Bay hub: it is booked for Piedmont home on Tue 8 Dec, 19:35 PST. Cancel or move
  that visit first."

**Rules:**
- Never name another hub's people, homes or visits to someone without a role there. Say "a visit at another hub".
- Success messages say what is now true, and what is still needed.

## 3. Accessibility

- Status is never conveyed by colour alone (shape and words). The existing contrast test (`test_ui_foundation`)
  extends to every new colour pair.
- Every action is reachable by keyboard in a sensible order. Focus is visible, using the existing focus ring.
- Every form field has a visible label; the panels are real headings and lists.
- Times use `<time datetime>` with the local text.

## 4. Demo realism (three fictional hubs)

All three hubs are clearly fictional.

| Hub | Character | Robots | Crews | Zones | Notes |
|---|---|---|---|---|---|
| Oakland hub (OAK) | Dense, short drives, the busiest | 50 (existing Northbay fleet) | 3 | Downtown, East Bay hills, Lake Merritt | Today's demo data, unchanged in substance |
| South Bay hub (SJC) | Suburban, longer drives, morning-heavy | 30 | 2 | Willow Glen, Campbell, Santa Clara | One robot received by transfer from Oakland yesterday |
| Marin hub (SRF) | Small, hilly, long travel | 20 | 1 | San Rafael, Mill Valley, Novato | One zone pair is deliberately Unknown, to show the fail-closed travel rule |

Schedules look like a real week: a morning and an afternoon wave per crew, an evening slot at two hubs, one at-risk
visit per hub for a different reason each (robot away, charge, travel unknown), and one paused zone, lifted, in the
history. Every value is labelled as demo data.

## 5. Review process and evidence (acceptance)

1. Screen specs (this document) are approved before code.
2. Each screen is built against the spec. A real-browser review at 1024, 768 and 375 px captures screenshots into
   `docs/evidence/03b_design_review/`.
3. **Taste checklist** for each screen:
   - aligned to the grid;
   - spacing from the tokens only;
   - one primary action;
   - no raw code leads a line;
   - local times with the zone;
   - every list has an empty state;
   - every refusal follows the formula;
   - status is words plus shape;
   - no horizontal scroll at 375 px;
   - keyboard path checked.
4. A defect log with fixes and re-check screenshots.
5. A final design review section, with before and after for Dispatch.
6. Independent usability: still UNVERIFIED unless outside participants are available. The design review is not a
   usability study.
