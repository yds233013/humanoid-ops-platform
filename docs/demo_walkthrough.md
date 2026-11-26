# Demo walkthrough (5 to 7 minutes)

A guided tour of one visit from planning to close, role by role, on the demo workspace "Northbay Home Robotics".
Everything in it is fictional. This is a script for showing the product; it is not evidence of usability, which is
UNVERIFIED.

## Before you start

- **Where:** the public demo, https://web-phase02rc.up.railway.app (the Phase 02 release, one hub; the demo link
  will be updated at release, after the v2 release approval), or a local copy built with `build_demo_workspace`
  (see the README). This script follows the v2 demo, which has three hubs (Oakland `OAK`, South Bay `SJC`,
  Marin `SRF`). On the Phase 02 demo, skip the steps marked **(v2)**.
- **Logins:** one per role. Credentials are shared separately by the project owner. This script uses:
  - `dana.ortiz`, the regional ops lead;
  - `jordan.lee`, crew A lead at Oakland (crew A is Jordan Lee and Amara Okafor, van `VAN-1`);
  - optionally `lily.tran`, the Oakland hub technician.
- **Codes change daily.** The demo is rebuilt every day, and visit, run and incident codes depend on the date. The
  rebuild names a visit prepared for this tour ("Journey J4: visit V-...") that belongs to crew A. Below it is
  called **the J4 visit**. On 2026-11-26 it was `V-0037`.
- **Use two browser windows** (or a normal and a private window): one signed in as the ops lead on a desktop, one
  signed in as the crew lead, ideally on a phone or a browser narrowed to phone width.
- **Going offline:** on a phone, airplane mode. On a desktop browser, the developer tools Network panel, "Offline".
  Open the run page once **with** a connection first, so the phone has the page and the run saved.

## 1. Ops lead: what needs attention (about 1 minute)

Sign in as `dana.ortiz`. You land on **Today** (`/ops/`).

1. Point out the hub filter (**All hubs**, `OAK`, `SJC`, `SRF`) **(v2)**, the readiness coverage ("Readiness checked
   for 10 of N visits"), and **Needs your attention**: each item says why it matters, who owns it and what to do,
   for example a held robot booked for a visit, or a robot overdue for its regular inspection.
2. Open **Briefing** (`/ops/briefing/`, under Today) **(v2)**. For each hub it answers four questions: robots
   unavailable and why, visits at risk today, incidents needing attention, and what to do first. Every line links
   to its source. The footer says it cannot authorize, dispatch or control robots.
3. Open **Dispatch** (`/planning/board/`, the **Board**). The **Needs attention** cards group problems by cause and
   hub, for example "5 visits at Oakland hub: charge forecast unknown" with **Record battery levels**. The banner
   reads "Planning is not departure: nothing here clears a robot to leave."

## 2. Ops lead: plan a visit (about 1 minute)

1. On the board, pick a visit card that needs attention and press **Review options**. The review shows **Why this
   visit needs attention**, the **Best option, ready to save**, and **What we checked** (robot location, time
   overlap, crew qualification, vehicle, maintenance, holds, travel time, regional pause, home and consent).
2. Point out the result line: "If you save: Planning feasible: not cleared to depart." Saving runs every check
   again in the database.
3. Optional: **Book a visit** (`/planning/book/`) shows Suggest and the next free time, labelled "Suggested
   planning time ... This is not departure authorization."
4. Optional **(v2)**: **Regional pause** (`/planning/pause/`) shows that a hub or zone can be paused with a reason.
   The demo's Oakland "Lake Merritt" zone was paused and lifted. Do not leave a pause active during the tour: it
   blocks the next step.

## 3. Readiness: the day's checks (about 1 minute)

1. Open **Dispatch > Departure readiness** (`/readiness/`) and then the J4 visit
   (`/readiness/visits/<J4 visit>/`). The **Status (checked now)** card shows "Planning: Planning feasible",
   "Checklist: Readiness incomplete" and "Leave decision: None issued".
2. **What is still missing** lists three items: a pre-departure inspection, an equipment check and a battery check,
   each with "What to do" and "Who can do it". The recovery plan is already recorded.
3. Record them on the same page (the crew lead or the ops lead may do this):
   - **Pre-departure inspection:** "Submit a new inspection for this robot", complete it, then press **Record
     inspection evidence** (the new inspection comes back preselected);
   - **Equipment check:** mark the restraint kit, recovery kit and charger present, result Pass, **Record equipment
     check**;
   - **Battery check:** enter a battery percentage above the policy's minimum, result Pass, **Record battery
     check**.
4. The status changes to "Checklist complete - you can approve it now."

## 4. Ops lead: departure authorization (30 seconds)

1. On the same page press **Request approval to leave**. It "records a time-limited decision to leave; it does not
   record a departure".
2. The page now reads "Approved to leave by dana.ortiz at ..., valid until ..., for visit ... with robot ...". The
   **Leave approvals** table shows the approval with its expiry.
3. Point out: any change before departure (a hold, a new inspection, a reschedule, a pause) makes this approval stop
   matching, and departure is refused.

## 5. Field crew: depart and work on site (about 1 minute)

Sign in as `jordan.lee` (phone width). You land on **My visits** (`/execution/mine/`).

1. The J4 visit is listed with "Open the readiness checklist". On its readiness page, tick **The robot is leaving
   now** and press **Record departure**. The page says "Departed - run EX-..." with a link to the run.
2. Open the run (`/execution/runs/<run>/`). The **Next step** card guides the crew in order:
   1. **Verify the departure handover**;
   2. **Record the arrival at the site**;
   3. **Hand the robot over to the site** (a custody move to site with a receiver from the crew; online only);
   4. **Record the site check** (every item ok and enough consents give a Go);
   5. **Start the session** (online only).
3. The connection pill at the top reads "● Online".

## 6. Field crew: lose the signal (about 1 minute)

Go offline (airplane mode, or "Offline" in the developer tools).

1. The pill changes to "○ Offline". Online-only steps (start, resume, hand over, recover) are disabled and read
   "■ Needs a connection. Do not start."
2. Record a **protective report**: **Open an interruption** with a short reason (for example "Child entered the
   work area"). At once the top of the page shows **Robot must not operate**: "report saved on this phone, not yet
   confirmed. It is sent first, as soon as there is a connection. Keep the robot stopped."
3. Record an ordinary step, for example an **Observation note**. It shows "Saved on this phone, not yet
   confirmed."
4. Optional: reload the page while still offline. The saved run opens as "Last known at HH:MM. Saved on this phone;
   this is not current authorization." The saved steps are still there.

## 7. Reconnect and confirm (30 seconds)

Go back online.

1. The pill shows "↻ Syncing", then "✓ Up to date at HH:MM". The protective report is sent first, then the
   observation note.
2. Each step changes from "Saved on this phone" to confirmed only when the server confirms that step. Confirmed
   steps fold away under "Confirmed (n)".
3. The **Robot must not operate** banner stays: "This only means the report is recorded. The robot stays stopped
   until the ops lead resolves it." The server paused the session when the interruption was recorded.

## 8. Ops lead: triage the interruption (30 seconds)

In the ops lead's window, open the same run (**Dispatch > Field runs**, `/execution/`, then the run).

1. The **Interruptions** card lists the interruption as **Open**, with its reason and who recorded it.
2. Type a resolution note (for example "Called the crew; area clear") and press **Resolve**. "Resolving does not
   resume the session."
3. Point out: an unresolved interruption would block this robot's next departure authorization, on this or any
   later run.

## 9. Field crew: return and close (about 1 minute)

Back as `jordan.lee`, refresh the run page. The banner is still there: the session is paused, and "Robot ... must
not operate until the session is resumed after a new Go site check."

1. The Next step card now reads **Resume or stop the session** (resuming needs a new Go site check). Stop the
   session. A stopped session is a safe end, so on the next fresh snapshot the banner clears.
2. Continue through the Next step card: **Move the robot back to the vehicle**, **Record leaving the site**, **Record
   the arrival at the hub**, **Receive the robot at the hub**, **Record the post-visit inspection**.

Then as `dana.ortiz`, on the run page:

3. **Record return reconciliation**, then **Close trip**. The Next step card reads **Nothing left to record**.

## 10. Audit (30 seconds)

1. Open the visit's dossier, `/execution/visits/<J4 visit>/dossier/` (the **Visit dossier** link at the top of
   the run page). It rebuilds the
   whole visit from append-only records in time order: plan revisions, evidence, the approval, the departure,
   every custody move, the site check, the session, the interruption and its resolution, the return.
2. **Today > Timeline** (`/ops/timeline/`) shows the day's runs and events across hubs.
3. Every change you made went through one command path that wrote an audit row in the same transaction. Users with
   audit access (admin, ops lead, engineering lead, safety reviewer, founder) can read the raw audit log as JSON
   at `/api/audit-events`.

## If something is refused

That is part of the product. A refusal says what was refused, why, and who can fix it, and nothing was changed.
Common ones in this tour: a step recorded out of order, an approval that no longer matches because something
changed, or a phone that has been signed out (its saved steps stay on the phone for the same user).
