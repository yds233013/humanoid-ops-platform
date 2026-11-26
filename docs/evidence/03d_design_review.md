# 03D-3 design review: the field run page on a phone

Contract 15.8 (design bar), with 15.10. Screens: `/execution/runs/<code>/` with JavaScript, and the offline shell
that the service worker serves when there is no connection. Real Chromium (Playwright) at 1024, 768 and 375 px.
Screenshots are in `docs/evidence/03d_design_review/`, named `NN_state_WIDTH.png`. They are produced by
`HOP_03D_SHOTS=1 uv run pytest tests/browser/test_v2_03d_design_review.py -m browser`. The harness also checks
that no state scrolls sideways at any width.

All data is synthetic. **Usability with real field crews is UNVERIFIED.** This is a design review by the
engineer, not a usability study.

## What the crew sees

The page reads top to bottom in the order a crew member needs it.

1. **"Robot must not operate"** comes first, above everything else, whenever a protective report is open on this
   phone. It never turns green. A rejection does not remove it.
2. **The connection pill**: ● Online, ○ Offline, ↻ Syncing n, or ✓ Up to date at 14:02 PST. Each state has a
   shape and a word as well as a colour.
3. **Notices that name the next action**: Sign in again, Update now, Reload, Skip step n…, Open the full page.
4. **"Saved on this phone"**: each step this phone holds and its state. Confirmed steps fold away under
   "Confirmed (n)".
5. The server page, unchanged.

Without JavaScript, the page is the server page exactly as before. The mount is empty and hidden, and every form
posts as it did.

## States captured

| # | State | Notes |
|---|---|---|
| 01 | Online, up to date | Only the pill is added. |
| 02 | Offline: a class C step saved, class A disabled | Shows "Saved on this phone, not yet confirmed" under the button. The handover (class A) button is disabled with "■ Needs a connection. Do not start." right under it. |
| 03 | Offline shell after a refresh | Shows "Last known at 12:47 PST. Saved on this phone; this is not current authorization." Has one primary action (the first step to record); class A steps are disabled. |
| 04 | Offline site-check form (dialog) | Built from inert form data the page saved; the household's details are not saved. |
| 05 | Protective report saved offline | The banner is first on the page. |
| 06 | Syncing | ↻ Syncing 2. The server answer is slowed in the harness so this state stays long enough to capture. |
| 07 | Confirmed; protective report accepted | The banner stays: "This only means the report is recorded. The robot stays stopped until the ops lead resolves it." |
| 08 | Protective report not accepted, ops lead names shown | "Not accepted by the server: the robot must still not operate. Contact your ops lead." **The rejection is injected.** |
| 09 | Not accepted, and a fresh snapshot shows the robot safe | The secondary action "Confirm with ops lead: robot is safe" appears. **The snapshot is injected**, because the real interruption is still open. |
| 10 | "Robot is safe" confirmation | Names the report and asks for a note. Keeps the row (EM clarification). |
| 11 | Waiting for access | "▲ Waiting for access: ask an ops lead" with Try again. Uses a real revoked grant. |
| 12 | Sign-out with unsent steps | "1 step is not yet confirmed. They stay on this phone for Ravi Menon; another person cannot send them." |
| 13 | Update ready | "Update ready: reload when it suits you. Saved steps are kept on this phone." |
| 14 | Sign in again | The session ended while the page was open; the steps stay saved. |
| 15 | Already recorded by Ravi Menon | A second phone of the crew, both real devices. Offers "Record again as a new step". |
| 16 | Waiting for an earlier step | A gap in the steps. **The held step is created by an injected first snapshot.** |
| 17 | Skip step confirmation | Names the step. Says "This cannot be undone." Needs a reason. |
| 18 | Reconcile after the skip | On the left, the steps not sent from this phone. On the right, the server's current state. Each row offers "Record again". |
| 19 | Update required | This phone meets a queue written by a newer client (schema raised in IndexedDB). |
| 20 | Offline shell after sign-out | "Sign in again to see this run". Shows no run data. |
| 21 | Not your run | A field operator who is not on the crew. |

## Taste checklist (docs/14_v2_03b_design_spec.md section 5)

| Item | Result |
|---|---|
| Aligned to the grid | Yes. New blocks use the existing card, notice and status components; the offline shell reuses the page header rhythm. |
| Spacing from the tokens only | Yes. `_field_css.html` uses only `--s1`…`--s6`, `--radius` and the existing colour tokens. No new colours. |
| One primary action | Yes, per card. The shell gives the primary style only to the first step to record. The banner's primary action is "Contact your ops lead"; "robot is safe" is secondary. Dialogs have one confirm button, and notices have one button. |
| No raw code leads a line | Mostly. Saved rows lead with names ("Arrived at the home"), with "Step 3" in quieter text. The run code is the page title, as on the server page. The server's own messages can still contain codes (for example "IN-0001"). |
| Local times with the zone | Yes. "12:47 PST" from the site zone through `Intl`; snapshot times are `<time datetime>` elements. |
| Every list has an empty state | Yes. "Nothing saved on this phone for this run yet…", "Every step saved on this phone is confirmed…", "This run is not saved on this phone", and "Nothing on this phone was recorded after the skipped step." On the online page, the saved-steps card is left out when it would be empty, because the pill already says the state. |
| Every refusal follows the formula | Yes. For example: "Not accepted by the server: the robot must still not operate. Contact your ops lead." / "This run is not yours; ask your ops lead." / "Needs a connection. Do not start." |
| Status is words plus shape | Yes. Each state has its own shape: ● ○ ↻ ✓ ◐ ▲ ✕ ◆ ■ —. |
| No horizontal scroll at 375 px | Checked by the harness on every state at every width. |
| Keyboard path checked | Yes, by `test_keyboard_reaches_the_field_actions_with_a_visible_focus_ring`: Tab reaches every field action, the existing 3 px focus ring shows, targets are at least 44 px, dialogs open on Enter with focus in the first field, and Escape closes them. |

Reduced motion: the field screen uses no animation. Scrolling to the banner or to the Reconcile view does not
use smooth scrolling when the reader asks for reduced motion.

## Known limits and defects (honest list)

- Full-page captures turn off the sticky header (harness only). Otherwise Playwright's stitching paints it in
  the middle of the page.
- The server page underneath is unchanged. At 375 px its long custody table (stacked rows) is still dense. That
  is outside 03D-3 and is noted for the integrated review.
- States 08, 09 and 16 use injected server answers, because the demo cannot produce them on demand. The
  behaviour they show is also covered by tests against the real endpoint where that is possible
  (tests/browser/test_v2_03d_run_page.py).
- The usability of the wording and order with real crews is UNVERIFIED.
