# Fleet Operations Platform v2 — Phase 01 contract: product foundation and onboarding

Status: **draft for EM review** · 2026-11-21 · authorizes nothing by itself. Phase 00 closed at 5de7e49.

Goal: a person joining a home-robot company on day one can set the product up, understand today's fleet, book a
visit and get a robot ready, without coaching and without the API or seed commands. Phase 01 makes the **existing**
workflows usable. It adds no agents, routing, charging or 500-robot work (Phases 02–05).

## 1. Scope

| In Phase 01 | Not in Phase 01 (later phase) |
|---|---|
| Design system, navigation, time-zone fix, permission-aware actions, readable refusals, security headers, error pages, health check, backup and rollback (01A) | Charging, batteries, hub inventory (02) |
| Today dashboard (01B) | Multi-hub dispatch board, routing, offline field app, customer messaging beyond an outbox (03) |
| First-run setup; customer, site, consent, people, vehicles, robots; visit request and booking; inspection submission (01C) | Agents (04) |
| Responsive screens for J1–J5 at 375 px; current-date demo workspace; journey tests; usability sessions (01D) | 500/5,000-robot optimization (05) |

J5 (field visit on a phone) is made **usable online** in Phase 01. It is **not** claimed complete until offline
queueing and the field app are tested in Phase 03 (EM clarification A).

## 2. Workspace isolation (EM clarification B)

**Decision proposed: one workspace = one deployment with its own PostgreSQL database.** A company that "buys" the
product gets its own Railway project (or database); the demo is its own workspace. No tenant column is added in
Phase 01, so every v1 database rule, function and test stays exactly as it is, and data of two workspaces can never
meet in one query.

- "Create a workspace" in Phase 01 means the **first-run setup** of a fresh, empty database: the first user becomes
  the workspace admin and names the company, region, time zone and hubs.
- A workspace record (singleton row: name, region, time zone, created at, demo flag) is added. The database refuses a
  second row.
- Isolation test: two databases with different data; a session cookie, user or API call from one workspace is
  refused on the other (different secret keys; users exist only in their own database).
- Shared-database multi-tenancy (tenant keys on every table, tenant-safe guard functions, row-level security) is a
  separate, later contract. Nothing in Phase 01 blocks it: no global singletons are added outside the workspace row.

## 3. Language and status rules (EM clarification C)

- Screens use the plain words of the Phase 00 contract section 3; internal codes appear only in a "Details" fold.
- **"Approved to leave" is always shown with its decision:** "Approved to leave by `<person>` at `<time>`, valid until
  `<time>`, for visit `<code>` with robot `<code>`." Before that, readiness shows "Checklist complete — ask the ops
  lead to approve" in a neutral color, never green, and never the word "approved".
- Every refusal is a list of plain reasons, each with what to do and who can do it. The raw reasons stay in the
  audit log.
- Actions appear only for roles that may perform them; others see who can act ("Waiting for the founder's
  approval"). The server still refuses every forbidden action (tests unchanged).

## 4. Setup never approves (EM clarification D)

Setup records facts: company, hubs, people and roles, vehicles, robots, customers, sites, configurations as
**proposals**. It never creates or fills an approval: configuration decisions, capability profile approvals,
inspection-template approvals, periodic-rule approvals, qualifications, hold-release policies, deadline sets and
readiness policies keep their v1 approval workflows and their production refusals. A test asserts that completing
setup leaves every production blocker in place.

## 5. Milestones

### 01A — Foundation

1. **Design system**: one stylesheet and component set (page header, navigation, status chip, card, table with a
   mobile list layout, form field with help and errors, confirmation dialog, empty state, toast). Contrast 4.5:1.
2. **Navigation**: Today · Dispatch · Fleet · Customers · Hub · Issues, plus a user menu (Settings, Reports,
   Sign out). Existing pages are placed under these; nothing is unreachable (fixes D-03). Dispatch and Hub show
   the current planner and fleet hub views until Phases 02–03.
3. **Time**: store UTC; show the workspace time zone with its label everywhere, one date-time format (fixes D-01,
   D-02). A template check fails the build on a "(UTC)" label next to a local time.
4. **Mobile**: remove the rule that hides the third table column; tables become stacked rows under 700 px
   (fixes D-04, D-14).
5. **Permission-aware actions and readable refusals** (fixes D-07, D-10, D-15).
6. **Errors and security**: friendly 403/404/500/CSRF pages with navigation; anonymous users redirected to login;
   HSTS, X-Frame-Options DENY, X-Content-Type-Options, Referrer-Policy and a CSP; Cache-Control: no-store on
   logged-in pages (fixes D-05, D-12, D-16).
7. **Operations**: `/healthz` (database reachable, migrations current) used by Railway; daily database backup and a
   rehearsed restore; keep previous deployments for rollback; written runbook (fixes D-17).

### 01B — Today

The landing page after login. It answers, for the workspace's local today:

- **Visits today and tomorrow** with one status each: Ready, At risk (with why), Blocked (with why), Approved to
  leave (with decision and expiry), Out, Back.
- **Counts**: visits, ready, at risk, blocked; robots available, on hold, out; open issues.
- **Needs your attention**: the exceptions inbox rewritten as cards: what happened, why it matters, the action,
  the owner, a button when the viewer may act.
- **Fleet strip**: robots by status (available, assigned, out, on hold, not in service).
- Built from the existing snapshot read models; within the existing p95 budgets on the 200-robot fixture.

### 01C — Setup and workflows

Screens over the **existing guarded commands** (no new safety logic):

| Screen | Existing command(s) |
|---|---|
| First-run setup wizard: workspace, hubs, operating hours, invite people and roles | new workspace row; `grant_role`; user creation (new, admin only) |
| People and qualifications list | `register_person`; `grant_qualification` / `revoke_qualification` (safety role) |
| Vehicles | `register_vehicle` |
| Robots | `register_robot`, `record_manifest`, `transition_lifecycle` (approvals stay in their own screens) |
| Customers and sites | `create_site`, `set_expected_adults` |
| Home assessment | `record_assessment`, `approve_assessment` |
| Consent | `record_consent`, `withdraw_consent` |
| Book a visit | `request_visit`, `plan_visit` (shows feasibility in plain words), `confirm_visit`, `cancel_visit` |
| Inspection | `submit_inspection` on an approved or demo template |
| Readiness | existing evidence, recovery plan and authorization commands, with section 3 wording |

### 01D — Acceptance

- Responsive layouts for J1–J5 at 375 px; J5 usable online.
- **Demo workspace**: fictional company (e.g. "Northbay Home Robotics"), 50 robots (fixture S), 1 hub, 3 crews,
  realistic customers in Bay Area towns with fictional names and addresses, visits spread over the past week and the
  next two weeks **relative to the current date**, a few holds, an incident and an overdue item. One persistent
  "Demo workspace" badge; no other demo banners. A management command rebuilds it; a daily job keeps dates current.
- Journey tests (section 7) and usability sessions (section 8).
- Deploy to the live demo only after 01D passes (EM: no incomplete workflow in the live demo).

## 6. Database changes

| Change | Notes |
|---|---|
| `platform_core_workspace` singleton (name, region, time_zone, demo, created_at) | Check constraint allows one row; time zone validated by PostgreSQL and Python |
| Hubs (code, name, address area, time zone) | Robots, vehicles and people gain an optional hub reference; existing rows default to the first hub |
| User creation by an admin command | Uses Django auth; role grants stay through `grant_role` |
| No change to readiness, execution, fleet guard functions or their privileges | Proven by the unchanged v1 safety suites |

Every migration is additive and reversible; `makemigrations --check` stays clean.

## 7. Test plan

- **Regression baseline**: the 1,440 passing v1 tests stay green (new tests are added, none weakened).
- **Write-flow tests** per 01C screen: a successful submit, each refusal shown in plain words, permission denied
  server-side for every other role, double-submit protection.
- **Journey tests** (Django test client, one per journey J1–J5): complete the journey through the screens only.
- **Language check**: no internal code, gate number or "(UTC)" mislabel as primary text in any v2 template.
- **Mobile check**: a headless browser at 375 px confirms no critical column is hidden on J1–J5 screens.
- **Isolation test**: two workspace databases; cross-workspace session and API use refused.
- **Setup-never-approves test** (section 4).
- **Security test**: headers present; anonymous redirect; error pages.
- **Performance**: existing perf workflow plus Today on the 200-robot fixture; no unexplained regression.

## 8. Usability sessions (01D)

Five people who have never seen the app (non-engineers preferred), screen-recorded, no coaching. Tasks and targets:
J1 setup < 10 min; J2 "what needs attention today" < 1 min; J3 book a visit < 3 min; J4 get a visit to "Approved to
leave" < 5 min (demo data prepared so it is possible); J5 walk a visit on a phone < 5 min. Success: at least 4 of 5
complete each task. Findings and fixes are reported. (Sessions need people; until the candidate supplies them, the
report says so and uses scripted walkthroughs instead.)

## 9. Deployment strategy

- Work on a `v2/phase01` branch; CI and perf on every push; deploy previews to a separate Railway environment
  (`staging`) with its own database; production demo updated only at 01D closure.
- Railway: health check on `/healthz`, previous deployments retained, daily backup, restore rehearsed into the
  staging database and recorded.

## 10. Acceptance gates (EM closure bar)

| Gate | Evidence |
|---|---|
| First-day usability | Section 8 sessions (or scripted walkthroughs if no participants) for J1–J4, J5 online |
| Core functionality | Write-flow and journey tests green |
| Mobile | 375 px check green; screenshots of J1–J5 |
| Safety | v1 safety suites green; no bypass |
| Permissions | Role matrix tests green |
| Workspace isolation | Isolation test green |
| Demo | Fictional, badge-labelled, current-date, rebuilt by one command |
| Deployment | CI green; headers verified on staging; backup and restore rehearsed; rollback rehearsed |
| Performance | Perf workflow green; Today within budget |

Each milestone (01A → 01B → 01C → 01D) is reviewed by the EM before the next starts.

## 11. Decisions requested

1. Workspace isolation as one deployment and database per workspace for v2 (section 2).
2. A Railway `staging` environment for previews (section 9).
3. Demo company size for 01D: fixture S (50 robots, 1 hub); the 500-robot demo comes with Phase 05.

## 12. EM review (2026-11-21): approved with corrections — 01A authorized

Rulings: one database per workspace **approved** (document the cost: provisioning, migrations, backups,
monitoring and recovery per customer database; no claim of proven multi-customer provisioning); Railway staging
**approved and required**; 50-robot Phase 01 demo **approved** (500 with Phase 05).

Corrections, binding for implementation:

1. **Singleton workspace**: enforced by the database, not a CHECK alone — a fixed primary key (`id = 1`) with a
   constraint on its value (or an equivalent database-enforced design). Test: a second row is rejected.
2. **First admin**: setup cannot be claimed by an arbitrary visitor. A one-time bootstrap token from the deployment
   environment is required; after initialization bootstrap is permanently disabled for that deployment (database
   flag). Tests: wrong token, replay after success, two concurrent attempts (exactly one wins).
3. **Staging isolation**: its own database, secret key, cookie names and domain, its own background jobs, and no
   outbound integrations (messages stay in the outbox). Nothing in staging can reach production data or send real
   messages.
4. **Backup and rollback**: a documented restore into an isolated database, tested; a migration compatibility
   strategy (additive migrations first, code tolerant of both schemas, destructive steps only in a later release with
   a separately reviewed recovery plan); a rollback rehearsal proving the app starts and works afterwards.

Usability: J4 timing counts only with valid approvals already in the fixture and the final decision made by an
authorized person; time to prepare the request and time waiting for the approver are recorded separately. No
automatic safety approval to meet a benchmark. Without the five-person study, that criterion is **unverified**,
never "passed"; scripted walkthroughs are engineering evidence only.

01A closure evidence: implementation commit, migration diff, CI results, screenshots at desktop and 375 px,
role-permission tests, staging URL, security-header checks, backup / restore / rollback rehearsal results, original
safety regression tests green, live demo unchanged.
