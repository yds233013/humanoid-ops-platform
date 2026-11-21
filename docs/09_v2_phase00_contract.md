# Fleet Operations Platform v2 — Phase 00 contract

Status: **draft for EM review** · 2026-11-21 · Phase 00 only (no UI rewrite, no agents yet).

Inputs: the EM's v2 command (2026-11-21), the current-state audit (`docs/09b_current_state_audit.md`), the robot-fleet
operations research matrix (`docs/09a_robot_fleet_ops_research.md`), the v1 contracts (`docs/03`–`docs/07a`) and the
v1 closure note (`docs/07c`).

## 0. The product test

> Can a newly hired operations lead open the system, understand today's fleet, resolve routine problems, and
> coordinate deployments without needing to understand the underlying engineering?

Every requirement below serves that test. v1's safety core stays; v2 changes who the product is designed for.

## 1. Users

| User | Device | What they need first |
|---|---|---|
| **Operations lead** (primary) | Desktop, sometimes phone | Today's fleet at a glance; what is at risk; the next decision; one-click routine actions |
| Field crew (lead + second) | Phone, poor connectivity | My visits today; one checklist at a time; depart → arrive → site check → finish → return |
| Hub technician | Tablet / desktop at the hub | Charging queue, battery swaps, inspections due, kit and parts shortages |
| Engineering | Desktop | Issues with the facts attached; robots on the bench; configuration approvals |
| Safety reviewer / founder | Desktop, phone | Approvals waiting for them; escalations; what blocks production |
| Customer operations (later the ops lead) | Desktop | Customer profiles, visit changes, messages sent and pending |

## 2. Core journeys (v2 must make each work without instructions)

Each journey lists the screens in the v2 navigation (**Today · Dispatch · Fleet · Customers · Hub · Issues**) and
the v1 rule it keeps.

| # | Journey | Steps in v2 | v1 rule kept |
|---|---|---|---|
| J1 | **First-run setup** | Create workspace → name the company and region → add hubs → add robots (or load the demo workspace) → invite people with roles → set operating hours and zones | Roles and permissions; settings owned by admins |
| J2 | **Morning check** | Open Today → see visits, ready / at risk / blocked counts and "Needs your attention" → open the top item → act | Read models from one snapshot; never a departure decision |
| J3 | **Book a visit** | Customers → pick customer → "Book visit" → system proposes slot, robot, crew, vehicle → confirm | Feasibility, conflicts, eligibility, consent |
| J4 | **Get a robot ready and send it** | Today → visit card → readiness checklist in plain words → missing items become tasks → "Request authorization" (ops lead) → crew taps Depart | Readiness gates, qualified inspectors, authorization function, custody |
| J5 | **Run the visit (field, phone)** | My visits → Checklist → Depart → Arrive → Site check (Go / Not today) → Start / pause / finish → Return | Custody handoffs, site check, sessions, dispositions |
| J6 | **Something goes wrong** | Report issue (phone or desktop) → robot held automatically → incident coordinator prepares the engineering ticket and escalations → reviewer classifies | Every incident holds; reporter describes, engineering/safety classify |
| J7 | **Bring a robot back to service** | Issues → robot → approvals listed by name → approvers approve → ops lead records release | Approvals fixed at placement; production needs an approved policy |
| J8 | **Hub day** | Hub → charging queue, swaps, inspections due, parts low → assign tasks → done | (new) charging, batteries, inventory, work orders |
| J9 | **End of day** | Today → "Close the day" → open runs with reasons → resolve or note → summary | Close-out read-only, not a sign-off |
| J10 | **Plan growth** | Reports → capacity: robots, crews, vans, hubs by fleet size → what breaks first | Capacity model, assumptions shown |

Acceptance for each journey is in section 8.

## 3. Language

The UI never leads with internal codes, gate numbers or database terms. v1 identifiers stay inside the code and the
API; the screens use these words:

| v1 internal | v2 screen wording |
|---|---|
| G1 Home Eligible | **Home ready** / Home needs a visit |
| G2 Robot Eligible | **Robot ready** / Robot unavailable (reason) |
| G3 Visit Ready | **Checklist complete** / Checklist: n items left |
| Departure authorization | **Approved to leave** (until HH:MM) |
| G4 Site check Go / No-Go | **Go** / **Not today** |
| G5 Return to service | **Back in service** / Waiting for approval from … |
| Hold | **On hold** (why) |
| Exception and refusal codes (e.g. `ESCALATION_OVERDUE`, `VISIT_NOT_CONFIRMED`, `EVIDENCE_MISSING`) | A sentence: what happened, why it matters, the action, the owner; refusals as a list of plain reasons, each with its fix |
| `synthetic_demo`, "SYNTHETIC" banners | Removed from screens; one persistent **Demo workspace** badge; demo data is fictional and never counts as a real approval |

## 4. Scale assumptions (test fixtures, not staffing claims)

| Fixture | Robots | Hubs | Crews | Vehicles | Visits / day | Purpose |
|---|---|---|---|---|---|---|
| S | 50 | 1 | 3 | 3 | 20 | Usability, onboarding |
| M (release) | 500 | 3 | 25 | 30 | 250 | Main performance and acceptance target |
| L (stress) | 5,000 | 20 | 250 | 300 | 2,500 | Find bottlenecks; documented, not certified |

Assumptions to replace with measurements before any staffing claim: visit duration, travel time, charging time,
turnaround, inspection time, visits demanded per robot. 100,000+ robots is out of scope for v2; the data model must
not block it (tenant and region keys, no global locks on hot paths, queue-based agents).

**Latency targets (to validate, not yet measured), p95 at fixture M:** interactive reads < 500 ms; complex
operational views (Today, Dispatch board) < 2 s; dispatch proposals < 3 s excluding an external routing provider.

## 5. Safety invariants (must never regress)

1. No departure without an authorization issued by the database function, unexpired, for the current visit,
   robot, configuration and crew.
2. Every incident places a blocking hold in the same transaction.
3. Hold release only through the approvals fixed at placement; production needs an approved procedure policy.
4. Closing an incident never releases a hold.
5. Custody: one place at a time; every move names who hands over and who receives; a trip closes only after hub
   receipt, post-visit inspection and reconciliation.
6. Readiness: approved template, qualified inspectors, fresh evidence, periodic inspection valid past unloading.
7. The runtime database role cannot bypass any of the above (direct-SQL tests stay green).
8. Demo data never counts as a real approval; production mode refuses synthetic inputs.
9. **Agents** act only through the same audited application commands as people, under their own named service
   identity, never as a database owner. They can never: issue an authorization, sign off safety, classify
   severity, release a hold, change a safety policy, or command a robot.

All 1,440 v1 tests stay green in every phase; new tests are added, none is weakened.

## 6. Agents (Phase 04; boundaries fixed now)

Flow for every agent action: **Observe → Propose → Validate (same rules as a person) → Execute (only if allowed) →
Audit**. A global pause switch stops all agents. Deterministic rules decide safety and scheduling; a language model
may explain, summarize and draft, never decide safety.

| Agent | May do on its own | Needs a person |
|---|---|---|
| Dispatch | Detect conflicts, prepare assignment and route proposals | Any change to a confirmed visit |
| Readiness | Find missing inspections, expiring qualifications, low batteries, missing kit; create tasks and reminders | Certifying readiness |
| Customer | Send approved message templates (reminder, on our way, reschedule offer) within consent and preferences | New wording, compensation, complaints |
| Hub | Watch charging queue, swaps, parts; create replenishment and charging tasks | Physical work, inspection sign-off |
| Incident coordinator | Collect reports, prepare engineering tickets, route notifications, track escalations | Severity, hold release |
| Assistant | Answer questions read-only ("what threatens tomorrow?") | — |

Required: idempotency keys, retry limits, an action log a person can replay, per-workspace permissions,
prompt-injection defenses for any text from customers or tickets.

## 7. Engineering approach

- Keep the Django + PostgreSQL core and its database rules. v2 screens are new routes; v1 routes stay until each
  replacement passes its journey test, then redirect.
- One design system (type scale, colors, spacing, components) applied to every screen; desktop and phone layouts.
- Background work (agents, notifications, reminders) runs in a worker with a durable job table, not in requests.
- Deploy: Railway, reproducible migrations, health check endpoint, daily database backup, documented rollback.
- Demo and production separated by workspace; the demo workspace is fictional and labelled once, persistently.

## 8. Acceptance criteria (Phase 01 onward)

| Area | Criterion |
|---|---|
| Usability | A person who has never seen the app completes J1–J5 and J9 without help, each within a stated time (J2 < 1 min, J3 < 3 min, J4 < 5 min); measured in recorded sessions |
| Language | No internal code or gate number as primary text on any v2 screen (automated check over templates) |
| Field | J5 works on a phone at 375 px width; offline submissions queue and never claim success before the server confirms; no duplicate submissions |
| Safety | All v1 safety tests green; new tests prove every agent forbidden action is impossible |
| Performance | Fixture M meets section 4 targets on the perf workflow; fixture L bottlenecks documented |
| Reliability | No 500 errors on any route in the route crawl; health check, backup and rollback rehearsed |
| Accessibility | Keyboard navigation and 4.5:1 contrast on all v2 screens |

## 9. Current-state findings (from Phase 00 audit)

Full audit: `docs/09b_current_state_audit.md` (54 GET routes × 5 roles, 810 requests, live Railway site).

**Healthy:** no 500 errors, no broken linked pages, nothing slower than 1 s (median 264 ms, slowest median
492 ms); every 403 matches the role table; DEBUG off; Secure session and CSRF cookies; no tracebacks in logs.
G1–G5 codes do not appear on screens; the jargon problem is raw refusal and exception codes.

**19 defects (4 High, 11 Medium, 4 Low).** The ones that decide Phase 01:

| ID | Severity | Finding | v2 answer |
|---|---|---|---|
| D-01 | High | Pacific times labelled "UTC" (planner, incident form); the same instant shown in different zones elsewhere (D-02) | One time rule: show the workspace's local time with its zone label everywhere; store UTC |
| D-06 | High | No screens to request a visit, verify consent, add sites, people or vehicles, or submit an inspection (seed or API only); a new lead cannot schedule | Phase 01/03 screens for J1, J3, J4 |
| D-03 | High | Today's status (/ops/) and capacity are not reachable from the header; several pages unlinked | One navigation: Today · Dispatch · Fleet · Customers · Hub · Issues |
| D-04 | High | Phones lose the 3rd column of every table (Result, Status, When) | Mobile layouts designed per screen; no hidden data |
| D-05 | Medium | No HSTS, X-Frame-Options or CSP | Security headers in Phase 01 |
| D-07, D-08 | Medium | Refusals are one line of raw codes; internal terms on pages | Section 3 language rules; every refusal as a list of plain reasons with the fix |
| D-10, D-11 | Medium | Buttons shown to roles that will be refused; a lone ops lead cannot finish a release with no guidance | Show only allowed actions; show who must act next and notify them |
| D-12 | Medium | Bare 403/404/CSRF pages; anonymous pages 403 instead of a login redirect | Friendly error pages with navigation; redirect to login |
| D-13 | Medium | /ops/ opens with 8 rows of production-blocker codes above the fleet | Today leads with visits and what needs attention; production readiness moves to Settings |
| D-17 | Medium | Only one Railway deployment kept; backups unknown | Backups, health check and rollback in Phase 01 |
| D-09, D-15, D-18 | Medium/Low | Demo authorization expires before the demo visit; authorize button shown when blocked; planner shows only this week | Demo data refreshes itself relative to today; actions only when possible |

**Usability baseline:** a new lead cannot complete J1 (setup) or J3 (book a visit) at all today, and J2 (morning
check) requires knowing /ops/ exists. These are the Phase 01 measures to beat.

## 10. Research evidence (from Phase 00 research)

Full matrix (19 sources, with limits per row): `docs/09a_robot_fleet_ops_research.md`. Most evidence is vendor or
press material; public evidence on home humanoids is thin. What it changes in v2:

| Practice seen in real fleets | Sources (rows) | v2 requirement it adds |
|---|---|---|
| Fleet and per-robot health with uptime / failure tags | 2, 4 | Fleet: health view per robot with failure categories (Phase 02) |
| Charge state and maintenance due count as job readiness | 14, 15 | Readiness includes charge plan and maintenance due, not only a display (Phase 02) |
| Separate work queues by role at each hub | 10, 11, 13 | Hub and Issues screens filter by role and hub (Phases 01–03) |
| Robot or crew help requests as a queue, with load per supervisor | 8, 10, 12 | Issues: a help-request queue distinct from alerts, with queue depth (Phase 03) |
| Remote assistance in homes is consent-based, scheduled and logged | 5, 6 | Customer profile: remote-assistance consent and restricted areas (Phase 03) |
| Tiered incident response: remote advice → recovery crew → emergency services | 10, 11 | Incident workflow tiers and third-party involvement recorded (Phase 03) |
| Area-wide events: bulk pause and wave recall | 12 | Dispatch: region pause and recall mode (Phase 03) |
| Offline-first field apps with sync state | 16, 17 | Field flow pre-caches the visit, queues photos and signatures, shows sync state (Phase 03) |
| Dispatch console with territories and in-day re-planning | 16, 17 | Dispatch board by hub/zone with drag-to-reassign that re-runs all checks (Phase 03) |
| Alert routing by severity and role; API and SSO | 1, 2 | Notification preferences per role; API kept; SSO noted as a production prerequisite |
| Calibration and inspection record before returning to service | 4, 18, 19 | Return-to-service requires a post-repair inspection record (keeps v1 rule) |
| Missed-job predictors are measured, not borrowed | 2, 4, 12, 15 | Log candidate predictors (battery at start, open issues, overdue maintenance, repeat failures); never copy another company's thresholds |

Industrial and warehouse practice is not proof that the same approach is safe in homes; every borrowed practice
above is a design input, not a safety argument.

## 11. Phase plan (EM command)

00 audit and research (this document) → 01 product foundation and onboarding (Today, navigation, setup, demo
workspace) → 02 hub, charging, maintenance → 03 regional dispatch and customer operations (3 hubs) → 04 agents →
05 500-robot performance and reliability → 06 product acceptance → 07 documentation and demo. Each phase: frozen
contract, implementation, tests, CI, perf, walkthrough, EM closure.

## 12. Open decisions for the EM

1. Phase 01 front-end approach: server-rendered Django templates with a design system and small progressive
   enhancement (keeps one codebase, fastest to ship) vs a separate React front end (richer, slower to ship).
   Proposed: server-rendered + HTMX-style partial updates for v2; revisit after Phase 06.
2. Routing provider for Phase 03: a fixed matrix of travel times between zones first (no external dependency),
   a real routing API later.
3. Customer messaging channel for the demo: messages are recorded and shown in an outbox, not actually sent, until
   a real provider and consent text are approved.
