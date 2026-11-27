# 06 Documentation walkthrough and responsive UI review

Phase 06, engineer 3. Base `0111a9f`, branch `v2/06-docs`. Demo workspace built with `build_demo_workspace` on
2026-11-26 15:23 PST into `humanoid_ops_06d` (a local copy, not the public demo). App: `runserver` as `ops_app`.
Real Chromium (Playwright), driven by a script. Synthetic data only.

**Usability: UNVERIFIED.** Everything here was done by the development team. It is a conformance and taste check,
not a usability study.

## 1. Documentation accuracy

### Walkthrough (`docs/demo_walkthrough.md`), followed step by step

The whole tour ran in one go: ops lead `dana.ortiz` at 1024 px, crew lead `jordan.lee` at 375 px with a persistent
browser profile (the same "phone" from start to end), the network cut with `context.set_offline`, and visit
`V-0037` (the J4 visit named by the build) from readiness to closure (run `EX-0004`).

**94 claims were checked against the running product; 92 matched as written.** The 2 that did not, plus 6 found by
reading, were fixed in the doc:

| Section | Doc said | Product does | Fix |
|---|---|---|---|
| 1.3 | "5 visits at Oakland hub: charge forecast unknown" | the count depends on the day (3 on this build); on a build made after the day's visits started, today's Oakland visits show Blocked by the zone rule | example count made explicit; a "Time of day matters" note under "Before you start" (use **Tomorrow** on a late local build) |
| 5.3 | pill reads "● Online" | "✓ Up to date at HH:MM" once synced ("● Online" only for a moment before) | doc |
| 6.1 | online-only steps "are disabled and read ..." at that point | correct behaviour, but at that point of the tour no online-only step is showing (the session has started) | says where to see it: go offline at **Hand the robot over to the site** (verified: "Needs a connection. Do not start.") |
| 7.1 | "↻ Syncing" then "✓ Up to date" | correct without an offline reload ("↻ Syncing 2"). After an offline reload the shell goes back to the full page first | doc notes both |
| 7.2 | confirmed steps "fold away under Confirmed (n)" | the page reloads with the server records and says "Confirmed by the server: ..."; earlier ones are under "Confirmed (n)" | doc |
| 8.1 | Interruptions card shows "who recorded it" | shows source (Operator) and reason | doc |
| 8.2 | quotes "Resolving does not resume the session." | that sentence appears only for incident-linked interruptions | now a narrator point |
| 9 | banner shows "... must not operate until the session is resumed after a new Go site check." | on the same phone the banner keeps the confirmed-report text ("... The robot stays stopped until the ops lead resolves it") while the session is paused | doc. See finding R-5 |
| 10.2 | Timeline shows "the day's runs and events" | upcoming confirmed visits with readiness, and open runs | doc |

The rest matched exactly: every URL, page title (Today, Operations briefing, Dispatch board, Readiness, V-0037
readiness, Visit dossier), button label (Review options, Record battery levels, Submit a new inspection for this
robot, Record inspection evidence, Record equipment check, Record battery check, Request approval to leave, Record
departure, Resolve, Record return reconciliation, Close trip), the Next step titles in order, role names, login
names, the protective-first order (checked in the ledger: protective at 23:37:33.339, observation at .446), the
offline "Last known at HH:MM" shell, banner texts, and the dossier contents.

### README

- The setup commands work as written: `make db`, `make migrate` (here with `DB_NAME=humanoid_ops_06d`), and the demo
  builder command, which prints the logins (password from `DEV_SEED_PASSWORD`). `/healthz` returned
  `{"status": "ok", "database": "ok", "migrations": "ok"}`. Markers, workflows (`ci.yml`, `perf.yml`), `deploy/`
  contents and the `dev_<role>` seed names were checked against the files.
- Fixed: `docker compose up` runs a **shorter** seed than `make seed` (the README said "the development seed").
- Fixed: "Ops lead" is now "Regional ops lead (covers every hub)".
- Screenshots: added **Today** (there was none). Replaced the board, review and pause shots: they were 800 px wide,
  labelled 1024, and showed an older board. The new ones come from this walkthrough. Replaced the 04 briefing
  shot (4,102 px tall) with a viewport-sized one. The new offline shots are from the persistent-profile run.

### Architecture (`docs/architecture.md`): catalog and grep spot checks

| Claim | Check | Result |
|---|---|---|
| 44 SECURITY DEFINER functions; owner `ops_owner`; fixed `search_path`; no PUBLIC execute | `pg_proc` on the demo DB | 44, 0 bad owners, 0 without search_path, 0 PUBLIC: matches |
| triggers on 96 tables | `pg_trigger` (non-internal) | 96: matches |
| "28 tables" SELECT-only for `ops_app` | `has_table_privilege` | **21 application tables** (+ `django_migrations`). The definer audit test pins 18. Fixed in the doc, with the setup tables written only by owner-only commands |
| `ops_app` owns nothing | `pg_class.relowner` | 0: matches |
| hub-scoped roles = field_operator, hub_technician, safety_pause; others regional | `role_grant_hub_scope` definition | matches |
| `accounts_lock_grants`, `fleet_robot_location`, `accounts_grant_role`, `accounts_revoke_grant`, `operations_pause_region`, `operations_lift_pause` exist | `pg_proc` | all present |
| field-sync lock table first | `execution_field_sync_lock` exists, SELECT-only | matches |
| batch of 20; SHA-256 of 9 identity fields; IndexedDB `ops-field` with `queues`, `commands`, `device`, `snapshots`; sync every 30 s; service worker scope `/execution/` | `field_sync.py` (MAX_COMMANDS = 20), `field_ledger.Identity` (9 fields), `queue.js`, `run.js` (SYNC_EVERY_MS = 30000), `field_assets.py` | matches |
| sync access: "an ops lead with a grant at the run's hub" | ops leads cannot hold a hub grant | reworded to "a regional ops lead" |
| `jit=off`, `lock_timeout=3000`, `statement_timeout=10000` | `config/settings.py` | matches |

### Release notes

No inaccurate feature claim was found. `FINAL_CI` placeholders are left as they are.

## 2. Responsive acceptance (Task C)

Screens at 1024, 768 and 375 px: `docs/evidence/06_screens/<screen>_<width>.png`. Full-page shots are clipped at
3,200 px tall. At 375 the browser emulates a touch phone. The walkthrough shots are `wt*.png`.

For each screen, the script checked:

- **no horizontal page scroll:** `documentElement.scrollWidth <= clientWidth`, listing any element past the edge
  that no scroll container clips;
- **visible keyboard focus:** after three Tab presses (153 samples), every focused element showed the 3 px focus
  ring (`rgb(43,123,214)`);
- **touch targets:** visible links, buttons, inputs and summaries; inline links in running text are exempt;
- **smallest text:** the smallest font size of any visible text.

| Screen | 1024 | 768 | 375 | Smallest text | Targets under 24 px at 375 (before R-2 fix) |
|---|---|---|---|---|---|
| landing (sign-in) | no h-scroll | no h-scroll | no h-scroll | 14 px | 0 |
| today | no h-scroll | no h-scroll | no h-scroll | 13 px | 13 |
| briefing | no h-scroll | no h-scroll | no h-scroll | 12 px | 0 |
| board (today) | no h-scroll | no h-scroll | no h-scroll | 12 px | 0 |
| board (tomorrow) | no h-scroll | no h-scroll | no h-scroll | 12 px | 0 |
| departure readiness list | no h-scroll | no h-scroll | no h-scroll | 13 px | 56 |
| readiness of a visit (V-0039, J4) | no h-scroll | no h-scroll | no h-scroll (**was 399 > 375**, fixed R-1) | 13 px | 5 / 4 |
| visit plan, review options | no h-scroll | no h-scroll | no h-scroll | 13 px | 39 (18 px radio buttons inside larger labels) |
| fleet | no h-scroll | no h-scroll | no h-scroll (**was 389 > 375**, fixed R-1) | 13 px | 199 |
| hub (technician) | no h-scroll | no h-scroll | no h-scroll (**was 385 > 375**, fixed R-1) | 13 px | 28 |
| incident / triage | no h-scroll | no h-scroll | no h-scroll | 13 px | 2 |
| regional pause | no h-scroll | no h-scroll | no h-scroll | 12 px | 1 |
| my visits (crew) | no h-scroll | no h-scroll | no h-scroll | 13 px | 0 |
| run page online (crew) | no h-scroll | no h-scroll | no h-scroll | 12.8 px | 4 |
| run page, ops triage | no h-scroll | no h-scroll | no h-scroll | 12.8 px | 7 |
| audit (visit dossier) | no h-scroll | no h-scroll | no h-scroll | 13 px | 1 |

After the R-2 fix, these screens at 375 have **0** targets under 24 px (re-measured): readiness visit, readiness
list, fleet, hub, run page. Today keeps 5: the robot-count links "49 available" and similar, which are 21 px tall
but spaced apart. Most targets stay under 44 px (the design spec's goal for the board, which meets it: 0). That
is noted, not fixed.

The smallest text is 12 px (secondary labels on the board, briefing and pause page). It is readable at 375 but
small.

## 3. Taste checklist (from `docs/14_v2_03b_design_spec.md` section 5)

| Item | Board, review, pause, briefing | Today, readiness, run page, dossier, hub, fleet |
|---|---|---|
| Aligned to the grid | yes | yes; stacked tables at 375 wrap the extra blocks under the label (after R-1) |
| Spacing from tokens only | yes | the R-1 and R-2 fixes use tokens and 2 px padding |
| One primary action | yes (Review options, Record battery levels, Save plan) | run page: the Next step card has one primary action; readiness has several forms (by design) |
| No raw code leads a line | yes (names first: "Alameda home H-135 · V-0042") | **no**: Today items ("Visit V-0032 starts soon"), My visits, the run header ("EX-0004 / V-0037 · H-120") and the hub queue lead with codes |
| Local times with the zone | yes | yes. The dossier printed raw UTC (fixed, R-3) |
| Every list has an empty state | yes | yes, where seen ("No interruptions.", "No dispositions.") |
| Refusals follow the formula | yes ("You can't pause departures at Oakland hub. Ask a regional ops lead ...") | the operations-page refusal is a bare sentence with no frame (P-1 in the permissions review) |
| Status is words plus shape | yes (■ Blocked, ▲ At risk, ● ...) | yes on the run page (pill and banner), readiness uses words |
| No horizontal scroll at 375 | yes | yes, after R-1 |
| Keyboard path | focus ring visible on every sampled element | same |

What reads well: the board's attention cards, the review panel's "What we checked" list with the result line "If
you save: Planning feasible: not cleared to depart.", the offline pill and the red "Robot must not operate"
banner at phone width, and the briefing's sourced priorities.

What does not yet read well (honest, not fixed under the feature freeze):

- After departure, the readiness page shows "Planning: Blocked" with "A departure is already recorded for V-0037"
  and "NB-004: Away on run EX-0004 ... overlaps". That is correct, but alarming right after a successful departure.
- The run page's custody form offers raw lower-case targets ("site", "hub"). Its custodian and receiver lists
  include people from every hub.
- Today: "Visit V-0032 starts soon" for a 14:00 visit shown at 16:05. "1 Visits" and "of 0 assessed" read
  awkwardly.
- The inspection signer list on a visit lists people from all three hubs for an Oakland robot (it is the regional
  ops lead's view).
- The dossier kinds are machine labels ("plan_revision", "pre_departure_inspection").

## 4. Defects found and fixed

| # | Severity | Defect | Fix | Test |
|---|---|---|---|---|
| R-1 | medium | At 375 px, stacked table cells with several blocks (readiness blockers, fleet reasons, hub task actions) laid them side by side and pushed the page 10 to 24 px wide: horizontal scroll on readiness, fleet and hub. | `templates/base.html`: stacked cells wrap (`flex-wrap`), and each block is right-aligned within the width | real-browser re-measure (this doc). No unit test: it is layout |
| R-2 | low | At 375 px, back links, "Details"/"Other records" summaries and code links in stacked tables were 21 px tall (under 24 px). | `templates/base.html`: 2 px vertical padding on those at phone width | re-measure: 0 under 24 px on the five screens listed |
| R-3 | low | The visit dossier printed raw UTC ISO times ("expires 2026-11-27T01:01:38.261952+00:00", "occurred ...+00:00"). | `execution/dossier.py`: `local_dt` (the same format as every page) | `tests/test_v2_06_docs_defects.py` (fails before the fix, passes after) |
| R-4 | low (EM condition) | The offline contact list said "Ops leads for this hub", but ops leads are regional. | `execution/field_static/run.js` wording | assertion added to `tests/browser/test_v2_03d_run_page.py` |

`base.html` still has exactly one inline script (plus the `run.js` module tag).

## 5. Open findings (not fixed)

| # | Severity | Finding |
|---|---|---|
| R-5 | low | After the server confirms a protective report and the ops lead resolves the interruption, the same phone's banner still says "The robot stays stopped until the ops lead resolves it" while the session is paused. It errs on the safe side; it does not switch to the server's paused-session message until the session stops. |
| R-6 | low | A local demo built in the afternoon shows today's Oakland visits as Blocked ("was put in zone ... after this visit was planned"), because the builder zones homes after booking visits that already started. The public 04:00 rebuild avoids it. Documented in the walkthrough. |
| R-7 | info | Touch targets under 44 px are common outside the board. Today's robot-count links are 21 px tall. |

## 6. Environment notes

The shared Docker host was heavily loaded (load average about 136). The first sign-in hit the app's 10-second
statement timeout once; retried later, everything ran within budget. Times on these screens are local to the build
(2026-11-26, PST).
