# 09b. Current-state audit of the live demo

Audited: 2026-11-21 (about 03:55 to 04:10 PST, 11:55 to 12:10 UTC)
Site: https://web-production-3a92454a.up.railway.app (Railway project humanoid-ops-platform, service web, region ams)
Method: curl only, five demo logins, plus read-only reading of the source and `railway status / logs / variables (names only) / volume list / deployment list`. No code was changed, nothing committed, nothing deleted.
All data is SYNTHETIC demo data.

## 0. Summary

- 54 distinct GET routes x 5 roles x 3 tries = 810 requests. Zero 500 errors. Zero 404s on linked pages. Median response time 264 ms, slowest single median 492 ms. Nothing is over 1 s.
- Every 403 matched `accounts/roles.py` (field_operator is refused metrics, capacity, audit events; everything else is open to the 5 tested roles). The engineer role has no demo login and was not tested.
- Broken-link crawl: 27 internal links from the five hubs plus one second-level link, all 200.
- Writes made on the live site: exactly one refused POST (authorize V-0004, refused with 302 and an error message, no state change) and one POST without a CSRF token (403). No robot, visit, hold, incident or authorization was created, changed or deleted. Steps (c) and (d) were NOT performed live; they are described from the forms and source (see section 3 for why).
- Defects: 4 High, 11 Medium, 4 Low (19 total, section 6).

Five worst issues:
1. D-01 Several pages label times "UTC" but show Pacific time (incident page says 02:48 UTC, the command center says the same event is 10:48 UTC). Wrong times in an operations tool.
2. D-06 There is no screen to request/create a visit, verify consent, record an inspection or add a site. A new lead cannot schedule a visit; those exist only as seed commands or the API.
3. D-03 The command center (/ops/) and the capacity page are not linked from the main header or any other section; the home page is the fleet list, not "today's status".
4. D-04 On phones, every table silently loses its 3rd column, including Pass/Fail "Result", approval "Status" and "When".
5. D-05 No X-Frame-Options, HSTS or CSP headers on the live site.

## 1. Route inventory

Cell format: HTTP status / median ms of 3 tries. Roles in order: ops_lead, founder, safety_reviewer, engineering_lead, field_operator. Titles are the `<title>` minus " · Ops Platform". Parameterized codes used: robots R-01..R-05 (R-99 as a negative test), visits V-0001..V-0004, incident IC-0001, patterns P-A and P-B. No execution runs exist (no departure has been recorded), so /execution/runs/<code>/ routes cannot be tested; IC-0002, P-C, V-0005, RUN-0001 return 404 as expected.

| Route | Page title (ops_lead) | ops_lead | founder | safety_rev | eng_lead | field_op |
|---|---|---|---|---|---|---|
| `/` |  | 302 / 225 | 302 / 216 | 302 / 212 | 302 / 212 | 302 / 216 |
| `/login/` | Log in | 200 / 235 | 200 / 227 | 200 / 234 | 200 / 232 | 200 / 229 |
| `/fleet/` | Fleet | 200 / 329 | 200 / 329 | 200 / 332 | 200 / 341 | 200 / 334 |
| `/fleet/hold-policies/` | Hold release policies | 200 / 258 | 200 / 256 | 200 / 255 | 200 / 264 | 200 / 256 |
| `/fleet/R-01/` | R-01 | 200 / 299 | 200 / 310 | 200 / 314 | 200 / 291 | 200 / 284 |
| `/fleet/R-02/` | R-02 | 200 / 326 | 200 / 298 | 200 / 422 | 200 / 330 | 200 / 311 |
| `/fleet/R-03/` | R-03 | 200 / 301 | 200 / 357 | 200 / 349 | 200 / 322 | 200 / 327 |
| `/fleet/R-04/` | R-04 | 200 / 278 | 200 / 282 | 200 / 317 | 200 / 295 | 200 / 281 |
| `/fleet/R-05/` | R-05 | 200 / 280 | 200 / 282 | 200 / 394 | 200 / 272 | 200 / 281 |
| `/fleet/R-99/` | Not Found | 404 / 217 | 404 / 219 | 404 / 239 | 404 / 235 | 404 / 235 |
| `/fleet/patterns/P-A/` | P-A | 200 / 252 | 200 / 261 | 200 / 267 | 200 / 264 | 200 / 266 |
| `/fleet/patterns/P-B/` | P-B | 200 / 253 | 200 / 269 | 200 / 269 | 200 / 271 | 200 / 275 |
| `/planning/` | Planner | 200 / 403 | 200 / 434 | 200 / 492 | 200 / 439 | 200 / 418 |
| `/planning/sites/` | Sites | 200 / 253 | 200 / 257 | 200 / 257 | 200 / 251 | 200 / 237 |
| `/planning/visits/V-0001/` | V-0001 | 200 / 298 | 200 / 356 | 200 / 317 | 200 / 312 | 200 / 309 |
| `/planning/visits/V-0002/` | V-0002 | 200 / 296 | 200 / 330 | 200 / 315 | 200 / 311 | 200 / 287 |
| `/planning/visits/V-0003/` | V-0003 | 200 / 297 | 200 / 307 | 200 / 303 | 200 / 321 | 200 / 299 |
| `/planning/visits/V-0004/` | V-0004 | 200 / 312 | 200 / 302 | 200 / 299 | 200 / 306 | 200 / 292 |
| `/readiness/` | Readiness | 200 / 391 | 200 / 398 | 200 / 384 | 200 / 396 | 200 / 388 |
| `/readiness/policies/` | Readiness policies | 200 / 243 | 200 / 238 | 200 / 248 | 200 / 256 | 200 / 257 |
| `/readiness/visits/V-0001/` | V-0001 readiness | 200 / 427 | 200 / 434 | 200 / 434 | 200 / 493 | 200 / 401 |
| `/readiness/visits/V-0002/` | V-0002 readiness | 200 / 376 | 200 / 407 | 200 / 433 | 200 / 398 | 200 / 365 |
| `/readiness/visits/V-0003/` | V-0003 readiness | 200 / 399 | 200 / 399 | 200 / 472 | 200 / 412 | 200 / 433 |
| `/readiness/visits/V-0004/` | V-0004 readiness | 200 / 396 | 200 / 381 | 200 / 487 | 200 / 390 | 200 / 397 |
| `/execution/` | Execution runs | 200 / 239 | 200 / 237 | 200 / 243 | 200 / 261 | 200 / 253 |
| `/execution/incidents/` | Incidents | 200 / 260 | 200 / 265 | 200 / 250 | 200 / 248 | 200 / 256 |
| `/execution/incidents/IC-0001/` | IC-0001 incident | 200 / 258 | 200 / 285 | 200 / 269 | 200 / 277 | 200 / 264 |
| `/execution/policies/` | Incident policies | 200 / 246 | 200 / 247 | 200 / 246 | 200 / 292 | 200 / 280 |
| `/execution/escalation-deadlines/` | Escalation deadlines | 200 / 263 | 200 / 254 | 200 / 257 | 200 / 241 | 200 / 244 |
| `/execution/visits/V-0001/dossier/` | V-0001 dossier | 200 / 257 | 200 / 259 | 200 / 271 | 200 / 256 | 200 / 271 |
| `/execution/visits/V-0002/dossier/` | V-0002 dossier | 200 / 262 | 200 / 265 | 200 / 259 | 200 / 261 | 200 / 258 |
| `/execution/visits/V-0003/dossier/` | V-0003 dossier | 200 / 268 | 200 / 263 | 200 / 261 | 200 / 261 | 200 / 254 |
| `/execution/visits/V-0004/dossier/` | V-0004 dossier | 200 / 264 | 200 / 271 | 200 / 261 | 200 / 272 | 200 / 255 |
| `/ops/` | Operations overview | 200 / 271 | 200 / 269 | 200 / 274 | 200 / 275 | 200 / 279 |
| `/ops/exceptions/` | Exceptions | 200 / 271 | 200 / 290 | 200 / 263 | 200 / 287 | 200 / 280 |
| `/ops/incidents/` | Incident queue | 200 / 254 | 200 / 254 | 200 / 241 | 200 / 242 | 200 / 240 |
| `/ops/timeline/` | Operations timeline | 200 / 257 | 200 / 257 | 200 / 253 | 200 / 255 | 200 / 248 |
| `/ops/metrics/` | Operations metrics | 200 / 262 | 200 / 278 | 200 / 267 | 200 / 263 | 403 / 241 |
| `/ops/closeout/` | Daily close-out | 200 / 254 | 200 / 253 | 200 / 264 | 200 / 266 | 200 / 261 |
| `/ops/capacity/` | Capacity forecast | 200 / 332 | 200 / 331 | 200 / 333 | 200 / 430 | 403 / 231 |
| `/api/health` | {"status":"ok"} | 200 / 219 | 200 / 231 | 200 / 235 | 200 / 240 | 200 / 232 |
| `/api/auth/csrf` | {"csrfToken":"7EbelNCVMHqpRRgtYjMm | 200 / 227 | 200 / 230 | 200 / 232 | 200 / 226 | 200 / 236 |
| `/api/auth/me` | {"user_id":2,"username":"dev_ops_l | 200 / 228 | 200 / 222 | 200 / 236 | 200 / 227 | 200 / 225 |
| `/api/audit-events` | [{"id":94,"occurred_at":"2026-11-2 | 200 / 260 | 200 / 270 | 200 / 258 | 200 / 256 | 403 / 228 |
| `/api/fleet/robots` | [{"code":"R-01","serial":"SYN-0001 | 200 / 321 | 200 / 327 | 200 / 330 | 200 / 332 | 200 / 322 |
| `/api/fleet/robots/R-01` | {"code":"R-01","serial":"SYN-0001" | 200 / 264 | 200 / 246 | 200 / 260 | 200 / 259 | 200 / 258 |
| `/api/fleet/robots/R-05` | {"code":"R-05","serial":"SYN-0005" | 200 / 254 | 200 / 247 | 200 / 258 | 200 / 258 | 200 / 256 |
| `/api/ops/overview` | {"as_of": "2026-11-21T11:59:01.155 | 200 / 264 | 200 / 269 | 200 / 270 | 200 / 263 | 200 / 275 |
| `/api/ops/exceptions` | {"as_of": "2026-11-21T11:59:02.047 | 200 / 285 | 200 / 289 | 200 / 273 | 200 / 276 | 200 / 266 |
| `/api/ops/incidents` | {"as_of": "2026-11-21T11:59:02.856 | 200 / 228 | 200 / 246 | 200 / 242 | 200 / 230 | 200 / 242 |
| `/api/ops/timeline` | {"as_of": "2026-11-21T11:59:03.652 | 200 / 245 | 200 / 254 | 200 / 256 | 200 / 237 | 200 / 242 |
| `/api/ops/metrics` | {"as_of": "2026-11-21T11:59:04.516 | 200 / 274 | 200 / 264 | 200 / 268 | 200 / 281 | 403 / 234 |
| `/api/ops/production-blockers` | {"as_of": "2026-11-21T11:59:05.315 | 200 / 240 | 200 / 239 | 200 / 257 | 200 / 247 | 200 / 253 |
| `/api/ops/closeout` | {"as_of": "2026-11-21T11:59:06.149 | 200 / 261 | 200 / 251 | 200 / 256 | 200 / 251 | 200 / 252 |

Notes on the inventory
- Expected 403s, all match roles.py: field_operator on /ops/metrics/, /ops/capacity/, /api/audit-events, /api/ops/metrics (operations.metrics and audit.read exclude field_operator). No role gets a 403 it should not get.
- `/` redirects (302) to /fleet/. Anonymous GET of /fleet/ redirects to /login/, but anonymous /ops/ and all /api/ routes return a bare 403 (see D-12).
- Unknown record codes (R-99, V-9999, IC-9999, P-Z) return a plain 404 page with no navigation.
- POST-only routes (hold, approve, release, check, authorize, depart, revoke, incidents/report, logout and others) return 405 on GET, as intended. Not counted in the 54.
- Timing note: requests ran one role at a time from one machine, so times include about 200 ms of network round trip to the server (Railway region ams, edge lax). Server time is small.
- Query strings: /planning/?week=garbage, /ops/closeout/?day=garbage and /ops/?x=1 all return 200 (bad input is ignored, no 500).

## 2. Broken-link crawl (as dev_ops_lead)

Start pages: /ops/, /fleet/, /planning/, /readiness/, /execution/. Level 1: 27 unique internal links, all 200 (no links returned anything other than 200/302). Level 2: one new link (/execution/incidents/IC-0001/ from /ops/exceptions/), 200.

Link-graph problems found (pages that work but cannot be reached by clicking):
- /ops/ is linked only from other /ops/ pages. Nothing in the global header or in Fleet, Planner, Readiness or Execution points to it.
- /ops/capacity/ is linked from no page at all (not even /ops/). Its own page nav does not show Daily close-out.
- /execution/visits/<V>/dossier/ is linked from no page that exists today (only from run pages, and no run exists).
- /planning/visits/V-0003/ is not on this week's planner (visit is Mon 23 Nov); reachable only through "Next".
- /readiness/ lists only confirmed visits, so /readiness/visits/V-0002, V-0003 and V-0004 are not linked from anywhere (V-0004's page is the one that explains why it is blocked).
Header navigation shows only Fleet, Planner, Readiness on every page. Each section also has its own second nav with a different set of links (Execution hub links Readiness and Fleet but not Ops; Ops links all of them; Planner links only Sites and Fleet).

## 3. Workflow walk (as a new operations lead, dev_ops_lead)

Login is a plain form and lands on /fleet/. Demo banner "SYNTHETIC DEMONSTRATION DATA" is on every page (an amber strip, fine).

### (a) See today's status
Pages: /ops/ (Operations overview). Clicks: 0 if you know the URL; otherwise not discoverable (D-03). From the header the only guess is Fleet, which shows planning eligibility ("Eligible for planning - not cleared to depart"), not status.
On /ops/: first thing shown is a table "Would block in production" with 8 rows of raw codes (ESCALATION_DEADLINES_NOT_APPROVED, HOLD_POLICY_NOT_APPROVED, HOLD_UNRELEASABLE, INSPECTION_TEMPLATE_NOT_APPROVED, PERIODIC_INTERVAL_NOT_APPROVED). The robot status table is below it. Jargon: "derived from the recorded rows", "snapshot", "custody", "rule v1", "guard v8" on robot pages, "manifest". Then /ops/exceptions/ (priority P2/P3, codes PERIODIC_INSPECTION_OVERDUE, SEVERITY_UNCLASSIFIED), /ops/timeline/ ("next 1440 minutes"), /ops/closeout/.
Stuck: finding the page; reading past the production-blocker table; times differ between pages (D-02).

### (b) Schedule a visit
Pages: /planning/ then /planning/visits/<V>/.
There is no way to create a visit in the UI. The planner has zero forms, and /planning/sites/ has none. `request_visit` exists only in services and in the `seed_planning` command (grep of all views/URLs). An ops lead can only open an existing visit (V-0001..V-0004), pick robot / lead / second / vehicle / new start / new end in "Plan or reschedule" (6 fields), click "Save plan" (1 form), then "Confirm" (1 more form). Blocked reasons are shown in plain-ish English (e.g. "Person P-OPS is already booked for V-0001 (Field crew block) 2026-11-22 16:40-20:20 UTC, overlapping field_shift") but people and robots are bare codes (P-OPS, P-FLD, VAN-1).
V-0004 is blocked because "1 of 2 adults have valid CF-v1 consent covering presence"; the readiness page says "Verify consent for every adult" but no screen exists to do it (consent and assessments are seed/API only). Dead end.
Stuck: no "new visit" button; consent codes (CF-v1); two-step save then confirm; the reservation table is mislabelled (D-01).

### (c) Get a robot authorized to depart
Pages: /readiness/ then /readiness/visits/<V>/ (only V-0001 is linked). Clicks: from the list 1 click to the visit; then possibly record evidence (1 form with up to 10 inputs), record recovery plan (1 form, 4 fields), then "Request departure authorization" (1 button, no fields). So 3 forms if starting empty.
Page shows a 9-row checklist "Gate / State / Blockers and what to do". Blockers are printed with raw codes in brackets, e.g. "[blocked · EVIDENCE_MISSING]", plus an action sentence ("Record a pre-departure inspection"). The "Pre-departure inspection" evidence needs an "Inspection code (e.g. INS-0001)", and inspections can only be created through the API command `submit-inspection`; the fleet page has no inspection form (D-08). Passing evidence also needs both signers to be "qualified readiness inspectors", which only a safety reviewer can grant (a different login).
The request button is shown even when the checklist is red. I POSTed it on V-0004 (the only live write) and got "Refused (conflict): Departure not authorized: Readiness incomplete - VISIT_NOT_CONFIRMED: ...; PLANNING_CONSENT: ...; RESERVATIONS_MISSING: ...; EVIDENCE_MISSING: ... (x3); RECOVERY_PLAN_MISSING ..." as one long unbroken red line (D-07).
Live state: V-0001 already has authorization DA-0001, issued Sat 21 Nov 02:54 by dev_ops_lead, "expires 04:24" (PST, today). The visit is Sunday 22 Nov 10:00. It is now past 04:00, so the demo authorization is about to expire and will need re-issuing; the pages show "expires 04:24" with no date or zone on the list (D-09).
Not performed live: I did not re-issue or void anything because it would change the demo state irreversibly (there is no delete) and the instruction was to write only if needed.

### (d) Record a departure and run the visit through return
Pages: /readiness/visits/V-0001/ then /execution/runs/<run>/ then back to the same run page (one long page).
Departure: one "Record departure" button next to DA-0001 (1 click). It opens a run. Then, from the run page source (not confirmed live, no run exists): verify administrative custody with a crew verifier (1 form), record 3 trip events one at a time (arrived site, departed site, arrived hub; 3 forms/buttons), a site check (checklist per template item, adults present, verifier; 1 form), session events (start, pause, resume, stop; each 1 form), optional interruptions, a custody move back to hub with custodian and receiver (1 form), post-visit inspection code and discrepancies in "Record return reconciliation" (1 form; needs an inspection created via API again), then "Close trip" (1 button). Minimum about 9 form submissions across about 10 sections on one page, with order enforced by refusals ("Start and resume are refused unless the latest site check is Go and newer than any pause..."). Jargon: custody, disposition, reconciliation, hub receipt, interruptions, session, trip closure, "Run" vs "Visit" vs "Trip".
Stuck: nothing says what order to do things; the run page is about 10 sections long; the hub return needs two different concepts (vehicle arrived vs robot received); the post-visit inspection code has no UI to create.
Why not run live: no run exists, DA-0001 expires at 04:24 PST, and the run cannot be undone or deleted; doing it would also use up the demo's only authorized visit.

### (e) Handle an incident
Pages: /execution/incidents/ ("Report incident" form: robot, type, knowledge source, occurred-at in UTC, description; 1 form, about 5 fields) then /execution/incidents/<IC>/.
Reporting always puts a blocking hold on the robot. Afterwards: triage and status moves one step at a time (reported, triaged, response_in_progress, resolved, closed; lowercase underscore values shown raw), severity decision (needs engineering lead or safety reviewer and an "approved incident policy"; page says "Severity: unclassified"), escalations with deadlines (e.g. safety_reviewer within 600 min), fault observations. Page text says closing never releases the hold. So handling an incident ends in someone else's queue and then in (f).
Stuck: "Occurred at (UTC)" asks a field person to enter UTC while evidence forms ask for site time; incident page shows UTC-labelled times that are really Pacific (D-01); the status button "Move to closed" is shown to ops_lead, but source (execution/incidents.py line 475) requires the close_incident permission (engineering lead or safety reviewer), so it would be refused; "Severity: unclassified" is the state of every incident until a policy is approved.

### (f) Release a hold
Pages: /fleet/<robot>/ (hold section). R-02 (HD-0001, failed inspection) and R-03 (HD-0002, incident IC-0001).
Page shows "Release problem now: slot founder has no valid approval on the current configuration", then a slot table (Required slot / Allowed roles / Status: eng_review_1, eng_review_2, safety, founder), an "Approve slot" form (slot, acting role, note) and a "Release hold" form. R-03 needs a founder approval and R-02 needs an engineer or engineering lead approval, so a lead cannot finish alone: log out, log in as another role, approve, log back in, release (at least 3 form submissions and 2 logins). The Release button and the role dropdown (a single "ops_lead" option) appear even though the release will be refused. Text mentions "re-basing", "policy v1 (synthetic draft)", "guard v8", "Safety config syn000000000000...". A banner says the policy is a DRAFT and releases are only allowed because it is a demo.
Not performed live (would change the demo's robots).

G1-G5: these codes do not appear in any page or template. They are in documents only.

## 4. UX and usability findings

| Page | Problem | Sev | Suggested fix (plain words) |
|---|---|---|---|
| All tables (base.html line 28) | On screens up to 700 px wide the third column of every table is hidden. That hides Result (Pass/Fail) in evidence history, Status (Missing/Approved) in the hold slot table, "When" in status history, "Configuration" in the fleet list, "On site" in the readiness list. | High | Remove the blanket rule; wrap wide tables in a horizontally scrolling box, or hide only columns marked low-priority. |
| Header, all pages | Header has only Fleet, Planner, Readiness. Ops overview, Execution, Incidents, Capacity are missing; each section has a different second row of links. | High | One shared menu on every page: Today, Fleet, Planner, Readiness, Execution, Incidents, Capacity. Make "Today" the home page after login. |
| /ops/ | The first block is 8 production blockers in raw codes; the robot status table, which answers "what is the status today", is below it. | Medium | Put robot status and exceptions first; put the production blockers in a collapsed section. |
| Planning visit page; incident pages | "(UTC)" labels on Pacific times (D-01). | High | Show real UTC, or label as site time, one rule everywhere. |
| /fleet/, /fleet/R-0x/, readiness list | Times without a zone or without a date: "due 2026-11-24 02:48", "expires 04:24". Date styles differ: "Sat 21 Nov", "2026-11-21 02:48", "Feb. 19, 2027". | Medium | One format with weekday, date, time and zone abbreviation. |
| Readiness checklist, flash messages, /ops/exceptions/ | Internal codes in uppercase (VISIT_NOT_CONFIRMED, EVIDENCE_MISSING, PERIODIC_INSPECTION_DUE_BEFORE_RETURN, HOLD_UNRELEASABLE) and DB-ish words (TripClosure, `at`, "derived from the recorded rows", "snapshot"). | Medium | Show plain sentences; keep codes in small grey text or a tooltip. Rewrite metric definitions without code names. |
| Refusal message (e.g. authorize V-0004) | "Refused (conflict): ..." followed by 8 problems in one run-on line. | Medium | Show a list, one problem per line, each with the fix. |
| Fleet R-0x hold section | Release button and approval form shown although the release will be refused; needs other roles with no explanation of who or how; "re-basing" unexplained. | Medium | Disable the button and say "Needs engineering review by an engineer (not you)"; add a short "how to release" line. |
| Incident page | "Move to closed" shown to a role that cannot close. | Medium | Show buttons only for permissions the user has. |
| Planner | No "new visit" action; people, robots, vans are bare codes (P-OPS, VAN-1, S-001). | High | Add a "Request visit" form; show names or roles beside codes. |
| Readiness visit page | "Request departure authorization" always visible even when red; page is 6 tables long, evidence form has 10 inputs, "Other items" free text "name=present, name=missing". | Medium | Show the request only when all gates are green; split the page into steps. |
| Execution run page | Single page with about 10 forms; no "next step" indicator. | Medium | Show a step list with the current step highlighted. |
| Incident form | "Occurred at (UTC)" while rest uses site time. | Medium | Default to now, in site time. |
| 403, 404, CSRF-failure pages | Plain text or default Django page, no header, no link back. "You do not have access to the operations command center." is the entire page. | Medium | Use the normal page frame with a "Back to home" link and who to ask. |
| Empty states (/execution/, /ops/timeline/, /ops/closeout/) | Present and clear ("No execution runs yet. A run opens when a departure is recorded."). The closeout page has five empty tables in a row. | Low | Collapse empty tables into one line. |
| Mobile | Viewport meta tag is present (base.html). Layout is 1100 px max with tables; only Capacity, Execution and Hold policies have a scrolling wrapper; Planner switches to one column under 800 px. Readiness visit page (34 headers across 6 tables) and /ops/capacity/ (61 headers, 44 KB) are table-heavy. | Medium | Scrolling wrapper on all tables; card layout for the long ones. |
| SYNTHETIC labels | Amber banner on every page, plus "SYNTHETIC" tags inside rows; clear and consistent. Policy banner red "DRAFT". Some pages carry two banners and a second note before real content. | Low | Keep; merge the two banners where both appear. |
| Session | Session lasts a fixed 12 hours; no warning before expiry. | Low | Show "session ends at ..." or extend on activity. |
| Fleet list / robot page | "guard v8", "Safety config syn0000...", "manifest #3", "rule v1" exposed. | Low | Hide behind a "details" link. |
| Capacity page | "SCU" never explained (30 occurrences); 61 headers. | Low | Explain SCU once at the top or say "demo currency units". |

## 5. Deployment health

| Check | Result |
|---|---|
| HTTPS | Yes. http:// returns 301 to https:// (done by the Railway edge; the app itself has no SSL redirect setting). |
| Strict-Transport-Security (HSTS) | Missing. |
| X-Frame-Options | Missing (XFrameOptionsMiddleware is not in MIDDLEWARE). All forms can be framed. |
| Content-Security-Policy | Missing. Pages use inline styles. |
| X-Content-Type-Options | `nosniff` present. |
| Referrer-Policy | `same-origin` present. Cross-Origin-Opener-Policy `same-origin` present. |
| Cache-Control on logged-in pages | Not set (the login page sends no-store, but /fleet/ etc. send nothing). Back button after logout may show data from the browser cache. |
| Session cookie | Secure, HttpOnly, SameSite=Lax, expires in about 11.8 h (12 h setting). |
| CSRF cookie | Secure, SameSite=Lax, not HttpOnly (Django default). CSRF checks work: POST without token returns 403. |
| DEBUG | Off. Unknown URL shows a bare 404; the CSRF failure page says "More information is available with DEBUG=True", which confirms it is off. No stack traces seen. |
| /api/health | 200 {"status":"ok"}, no login needed, no database check shown. |
| /admin/ | 404 (not installed). /static/admin/... 404. /robots.txt 404. No static file serving. |
| Anonymous API | /api/* returns 403 {"error":"permission_denied"} rather than 401. Nothing leaked. |
| Logs | `railway logs --service web`: boot, migrations, many 404/403 warnings from my tests. No tracebacks or 500s. Log timestamps are PST, app pages say UTC. |
| Services | web (online, deployed by CLI upload, no git repo attached) and Postgres (image postgres-ssl:18, volume postgres-volume 5000 MB, status Ready; CLI reports 0 MB used). |
| Rollback | `railway deployment list` shows only one deployment (1b78fcf3, SUCCESS, 2026-11-21 02:47 PST). No earlier deployment to roll back to in the list. |
| Backups | Unknown: not visible through the CLI. Only a volume exists; no backup schedule seen. |
| Start script | Runs migrations and a seed-once check on every boot; 2 gunicorn workers, single instance. |
| Secrets | Variable names only inspected (DJANGO_SECRET_KEY, DJANGO_DEBUG, DEV_SEED_PASSWORD, DB passwords, ...). No values read. The demo password is one shared value for 5 accounts (DEV_SEED_PASSWORD), by design for the demo. |

## 6. Defect inventory

| ID | Area | Description | Sev | Evidence | Repro |
|---|---|---|---|---|---|
| D-01 | Time zones | "UTC" labels on local (Pacific) times. Incident page shows "Occurred Sat 21 Nov 2026 02:48 (UTC)" and deadline "12:48 (UTC)"; /ops/exceptions/ shows the same incident as 10:48 UTC. Planner visit reservations table is headed "From (UTC)" but V-0001 on site shows 10:00, while /ops/ and the conflict message show 18:00 / 16:40 UTC. Cause: TIME_ZONE is America/Los_Angeles and templates use the `date` filter under a UTC header (planning/visit.html line 30). | High | /execution/incidents/IC-0001/, /execution/incidents/, /planning/visits/V-0001/ vs /ops/exceptions/ | yes |
| D-02 | Time zones | Same instant shown in different zones and formats: due 2026-11-24 02:48 (/fleet/, no zone) vs Tue 24 Nov 10:48 (/ops/, UTC); "expires 04:24" with no date; "Feb. 19, 2027". | Medium | /fleet/ vs /ops/; /readiness/ | yes |
| D-03 | Navigation | /ops/ is not linked from the global header or any non-ops page; /ops/capacity/ is linked from no page; dossier pages and readiness pages for V-0002..V-0004 are unlinked; header lacks Execution and Ops; landing page is /fleet/. | High | crawl of 15 pages (section 2) | yes |
| D-04 | Mobile | base.html hides the 3rd column of every table at max-width 700 px, hiding Result, Status, When. | High | templates/base.html line 28 (and the same rule in execution/_chrome.html) | yes (source; not rendered in a browser) |
| D-05 | Security | No X-Frame-Options, HSTS or CSP on any response. | Medium | `curl -D - /login/` | yes |
| D-06 | Workflow | No UI to request a visit, verify consent, record assessments, add sites/people/vehicles, or submit an inspection. Visit V-0004 shows "Verify consent for every adult" with no screen for it. Evidence and reconciliation forms ask for an inspection code that cannot be created in the UI. | High | no routes in config/urls.py or app urls.py; only seed_planning and /api/fleet/submit-inspection | yes |
| D-07 | Messages | Refusals are one long line with raw codes: "Refused (conflict): ... VISIT_NOT_CONFIRMED: ...; EVIDENCE_MISSING: ..." | Medium | POST /readiness/visits/V-0004/authorize/ | yes |
| D-08 | Jargon | Raw codes and internal terms in pages (checklist codes, HOLD_UNRELEASABLE, TripClosure in metric definitions, guard v8, manifest, CF-v1, SCU, "1440 minutes"). | Medium | /ops/, /readiness/visits/V-0004/, /ops/metrics/, /ops/capacity/ | yes |
| D-09 | Demo data | Authorization DA-0001 for V-0001 expires today 04:24 PST, about 30 hours before the visit (Sun 22 Nov 10:00); it will read as expired during the demo. | Medium | /readiness/visits/V-0001/ | yes (until re-issued) |
| D-10 | Permissions UX | Buttons shown to roles who will be refused: "Move to closed" for ops_lead (needs close_incident); "Release hold" on R-03 for ops_lead although a founder approval is missing; approval form lists only "ops_lead" as role. | Medium | /execution/incidents/IC-0001/, /fleet/R-03/; execution/incidents.py line 475 | yes |
| D-11 | Workflow | Hold release needs other people's roles (founder for HD-0002, engineer for HD-0001) with no guidance; a lone ops lead cannot finish. | Medium | /fleet/R-03/ slot table | yes |
| D-12 | Error pages | 403, 404 and CSRF-failure pages are bare, with no nav or way back; anonymous /ops/ and /api/* return 403 instead of redirect to login / 401. | Medium | `curl /nosuch/`, `curl /ops/`, POST without token | yes |
| D-13 | Information density | /ops/ opens with the production-blocker table (8 rows of codes) above the actual robot status. Readiness visit page is 6 tables and 34 headers. | Medium | /ops/, /readiness/visits/V-0001/ | yes |
| D-14 | Mobile | Only Capacity, Execution and Hold policies pages wrap tables in a scroll box; other wide tables can overflow. | Medium | grep for overflow-x in templates | yes (source) |
| D-15 | Readiness UX | "Request departure authorization" button is shown on a blocked visit, and also on a visit that already has an authorization. | Low | /readiness/visits/V-0004/, /readiness/visits/V-0001/ | yes |
| D-16 | Cache | Logged-in pages carry no Cache-Control header (login page does). | Low | `curl -D - -b jar /fleet/` | yes |
| D-17 | Ops | Backups and rollback: only one deployment is kept; backup status unknown (not visible via the CLI). | Medium | railway deployment list, railway volume list | unknown |
| D-18 | Scheduling | /planning/ shows only the current week; V-0003 (Mon 23 Nov) is not on screen until "Next"; "Since" for PERIODIC_INSPECTION_OVERDUE equals the current time on every reload. | Low | /planning/, /ops/exceptions/ | yes |
| D-19 | Session | Fixed 12-hour session with no warning; engineer role has no demo login so its pages could not be checked. | Low | settings.py SESSION_COOKIE_AGE; login file | yes |

Total: High 4 (D-01, D-03, D-04, D-06), Medium 11 (D-02, D-05, D-07, D-08, D-09, D-10, D-11, D-12, D-13, D-14, D-17), Low 4 (D-15, D-16, D-18, D-19) = 19.

## 7. Things that work well (so they are not broken by fixes)
- No 500s, no slow pages, fast (about 0.25 to 0.45 s).
- Role gating is consistent between UI, API and roles.py; the 403s are all intended.
- Clear "not a departure decision" wording; synthetic labelling is consistent.
- Empty states are explained; bad query strings are ignored.
- Cookies Secure/HttpOnly, CSRF enforced, DEBUG off, no admin exposed, no secrets in pages.
