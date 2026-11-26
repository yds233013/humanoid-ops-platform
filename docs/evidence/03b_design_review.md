# 03B design review (lead), against the EM design bar (contract 13.1, 13.4 item 5)

This is a review of the 03B screens on the three-hub fictional demo, in a real browser (the Claude desktop browser
pane) at 1024, 768 and 375 px. The ops lead (dana.ortiz) is regional. Screenshots are in
`docs/evidence/03b_design_review/`: board today and tomorrow, review V-0105 and V-0106, next free time, pause and
travel times, each at three widths. They are viewport captures, retaken after the fixes.

**This is not a usability study.** Independent usability is still UNVERIFIED.

## Before and after: Dispatch

**Before (Phase 02):**
- A week-long list grouped by day.
- Each card led with codes: "V-0017 · H-121 · Piedmont · CRM reference CRM-48470", then a status chip and
  "NB-013 · VAN-1".
- No hub, no crew names, nothing showing what needs attention.

**After (03B):**
- **Header:** the Dispatch board opens on a one-line "Planning is not departure" note. Next come three
  scope-labelled numbers (visits, need attention, hubs paused).
- **Needs attention:** problems are grouped by cause and hub, for example "5 visits at Oakland hub: charge forecast
  unknown · Record battery levels (Hub › Charging)". They are ordered blocked, then paused, then at risk, then by
  departure.
- **Hub sections:** each hub has zones, robot and crew counts, and an optional pause banner. Below them are visit
  rows that lead with the home and crew names. Codes stay quieter, and status is words plus shape.
- **Week list:** one click away.

## Defects found by the lead review and fixed

The first review was done on the merged three-hub demo, before the fixes. The fixes are in `14ec776` and `62ed698`;
the tests are in `tests/test_v2_03b_board.py`.

| Id | Found | Fix |
|---|---|---|
| D-B1 | Five identical "Charge forecast unknown" cards | One card per cause and hub, with a count, one action where to fix it, and the visits folded underneath |
| D-B2 | "Fictional practice visit (Emeryville) home" | Place names are used as they are; no doubled "home" |
| D-B3 | Travel "version" showed a database row id | Shows the pair's own version, out and back |
| D-B4 | An unknown-travel visit showed "leaves the hub 16:30", built from a missing value | "Leave time unknown (travel time unknown)" |
| D-B5 | "At risk:" written twice (chip and sentence) | The chip says it once |
| D-B6 | About 4 interactions to fix; the best option was not preselected | Options ranked by fewer and less severe risks. The best complete option is preselected with its checks shown, so the fix is board card → review → Save plan (2 clicks, through the guarded plan command) |
| D-B7 | The reservations table led with codes ("Person P-212") | Names first ("Grace Chen (P-212)"), using the board's own words |
| D-B8 | The blocked-by-travel panel said "these times work", then "no time can work..", and still offered options | One message: what, why, who can fix it, with a link. Options are folded under "none can fix this" |
| D-B9 | "travel unknown min out" | "Travel time unknown (out), 35 min back" |
| D-B10 | At 375 px, Needs attention was below the fold | The order is now title, summary, Needs attention, then the filters, with a compact "Today · All hubs ▾" control |

Earlier rounds, found by the engineers' own browser checks:
- codes wrapping in the middle of a code;
- a time column overflowing at 768 px;
- tap targets under 44 px;
- the subnav highlighting the wrong page;
- the travel page calling Oakland's own zone unknown;
- underlined buttons;
- a crowded zone chip;
- misaligned hub stats at 375 px.

## Taste checklist (spec section 5)

| Item | Result |
|---|---|
| Aligned to the grid; spacing from the tokens only | Checked by eye at three widths; raw px spacing replaced by tokens |
| One primary action per card or panel | Yes |
| No raw code leads a line | Yes (tested) |
| Local times with an IANA-derived zone label | Yes (tested: no hard-coded zone abbreviation) |
| Every list has an empty state | Yes (tested) |
| Every refusal follows what, why, who | Yes for the 03B paths |
| Status is words plus shape | Yes (tested) |
| No horizontal scroll at 375 px; tap targets at least 44 px | Yes, measured |
| Keyboard path | DOM order checked; tabbed only on the pause page |

## Still open (honest)

- At 375 px the site-wide navigation is about 290 px tall, so the first attention card's button sits just below the
  fold. Fixing that needs a shared header change, which is not part of 03B.
- "Charge forecast unknown" dominates the demo's Oakland hub until battery levels are recorded. This is existing 02B
  behaviour, now shown as one grouped card.
- Next free time often shows three consecutive half-hour slots with the same crew (earliest-first ranking).
- The wording is "Vehicle VAN-5" everywhere, not "Van".
