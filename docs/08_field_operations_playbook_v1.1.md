---
title: "Home Humanoid Field Operations Playbook"
subtitle: "Version 1.1 · for a small pilot fleet (about 5 robots, 1–2 field teams, one hub) · Bay Area"
---

::: {.note}
**How to read this playbook.** Each procedure is short on purpose. Boxes tell you how far each step can be trusted:

- **[OPS] Operations proposal.** A process we can reasonably run from day one. The ops lead owns it and changes it as we learn.
- **[ENG] Engineering validation required.** A robot-specific step, such as the e-stop, lifting, charging, fall recovery, software limits or battery checks. **This playbook does not write these procedures.** It marks where they go. Until engineering supplies and signs one, the step is *not done*, and the robot does not go.
- **[LEGAL/SAFETY] Legal or safety review required.** Consent, privacy, regulator reporting, battery transport, insurance and incident disclosure.

Two more marks are new in v1.1. They say who stops a mistake:

- **[PLATFORM]** The operations platform refuses the action until the condition holds. Nobody can click past it, and direct database access by the app cannot skip it.
- **[MANUAL]** The platform does not check this. A person does, and the record is only as good as that person's care.

Records and gates are defined in *01 Data Model and Gate Logic*. The forms are in *03 Field Forms*. The working records now live in the operations platform (`README.md`); the *04 Ops Tracker* spreadsheet stays a training aid.
:::

::: {.legal}
**SYNTHETIC. Not an approved operating procedure.** Every robot, home, person, interval and approval in the platform today is synthetic demo data. The platform has been verified as software (Slice 7 closure note, `docs/07c_slice7_closure_note.md`), and in production mode it refuses to authorize a departure or release a hold until real company decisions are recorded. **No production deployment is approved.** Section 20 lists what is simulated and what a real launch still needs.
:::

# 0. What changed from v1.0

v1.1 keeps every v1.0 procedure. It fixes wording where v1.0 and the platform disagreed (review `docs/07_playbook_coverage_matrix.md`, EM decisions in its section 9). It also adds the custody, recovery-plan and close-out steps the platform now requires, and the Slice 7 guardrails.

| Change | Where | Source |
|---|---|---|
| Gates G1-G5 mapped to platform screens and records | 1.2 | D-01 |
| "CLEARED TO DEPART" replaced by an explicit, time-limited authorization issued by the ops lead. Nothing is ever shown green | 1, 6 | D-02 |
| Robot status names follow the platform | 1.2, 17 | D-03 |
| Role codes mapped to platform roles | 2 | D-04 |
| The reporter describes an incident; engineering or safety classify it. Complaints are not incidents | 11 | D-05 |
| Every incident holds the robot | 11 | D-06 |
| Hold release: approvals come from the approved hold-release policy, fixed when the hold is placed. The severity table is a company policy proposal, not yet approved | 15 | D-07 |
| Readiness-fail release needs engineering approval **and** a newer PASS | 14, 15 | D-08 |
| Approving a release is separate from recording it | 1.1, 15 | D-09 |
| Homes *eligible with restrictions* cannot be booked yet | 4 | D-10 |
| *Pending* means "no approved current assessment" | 4 | D-11 |
| Readiness freshness comes from the approved readiness policy; four evidence kinds | 6 | D-12, D-13 |
| A readiness fail raises an engineering ticket, not an incident | 6 | D-14 |
| The 15-minute crew buffer is not enforced by the platform | 5 | D-15 |
| Visit block sizing follows the platform timeline | 5 | D-16 |
| A departed visit cannot be cancelled; it ends | 8, 18 | D-17 |
| G4 is Go / No-Go; the ops lead records how a visit ends | 8 | D-18 |
| Location inside the hub stays a physical note; custody is hub / vehicle / site / unaccounted | 7, 13 | D-19 |
| "Software version" becomes the configuration (pattern + manifest) | 14 | D-20 |
| Notification timings kept; escalations are recorded, and overdue ones are shown (never paged) | 11 | D-21 |
| Visit statuses mapped | 5 | D-22 |
| Readiness inspectors need a `readiness_inspector` qualification | 2, 6 | D-23 (built in Slice 7, X-8) |
| "Seven records" becomes "a few screens" | 1 | D-24 |
| Custody procedure (verify assignment, two-name handoffs, hub receipt) | 7, 13 | G-07 |
| Road events | 7 | G-08 |
| Recovery plan before departure | 6 | G-15 |
| Daily close-out view | 16 | G-12 (built in Slice 7, X-10) |
| Periodic inspection expiry, approved inspection templates, production hold-release policy, escalation deadlines | 6, 11, 14, 15 | Slice 7 (X-3, X-1, G-01, X-4) |
| Validation register says whether the platform blocks until each item is supplied | 19 | Review section 7 |

# 1. The rule and the principles

**The rule.** No robot departs for a home unless the ops lead has issued a **departure authorization** for that visit, and it has not expired. The platform issues one only when **G1 Home Eligible**, **G2 Robot Eligible** and **G3 Visit Ready** all hold at that moment. No robot operates in a home until **G4 Site Check** reads **Go**. A held robot comes back only through **G5 Return to Service**. **Anyone can stop work at any time, and is never penalized for it.**

Authorization is a named, time-limited decision ("Authorization issued (expires HH:MM)"), not a colour. The platform never shows a visit as green or "cleared". *Ready to request authorization* means the checks pass now; it is not permission to leave. **[PLATFORM]**

**Principles**

1. **Safe first, then useful, then fast.** A cancelled visit costs a day. An injury or a broken trust costs the company.
2. **If it isn't recorded, it didn't happen.** Approvals are a name and a time, not a nod.
3. **Every home is a new deployment.** Homes change between visits; we re-check every time.
4. **Ops makes engineering faster.** Every failure comes back with the facts, logs and conditions engineering needs to reproduce it.
5. **No hidden problems.** Bad news goes up the same day. Near misses count.
6. **Keep it light.** A few screens, one 15-minute daily sync, one 30-minute weekly review. Anything more must earn its place.

## 1.1 Who decides what

| Decision | Ops lead | Field operator | Engineering | Safety reviewer | Founder | Legal |
|---|---|---|---|---|---|---|
| Book, move or cancel a visit (before departure) | **Decides** | Proposes | | | | |
| Stop a visit; refuse to depart; call *No-Go* | **Anyone** | **Anyone** | **Anyone** | **Anyone** | **Anyone** | |
| Record readiness evidence (G3) | Records; may sign as inspector if qualified | Records; may sign as inspector if qualified | Writes the check procedure and approves the inspection template | Approves the inspection template and reviews battery and field-kit items | | |
| Issue the departure authorization | **Decides** | | | | | |
| Home eligible (G1) | **Decides** standard cases | Assesses | **Decides** borderline cases | Vulnerable residents, with founder | Vulnerable residents | Owns consent form |
| Which configurations (hardware, release, firmware) may go into homes | | | **Decides** | Approves | Informed | |
| Which tasks are allowed in homes | | | **Decides** | Reviews | Informed | |
| Restrict a robot, task or configuration | **Can restrict** | Can raise | **Can restrict and lift** | Can restrict | Can restrict | |
| Classify an incident's severity | | | **Engineering lead decides** | **Decides** | | |
| Approve a hold release (G5) | | | Approves the slots the hold-release policy gives engineering | Approves the safety slots | Approves the founder slots | |
| Record a hold release once every approval is in | **Records** | | Engineering lead may record | | | |
| Approve the hold-release policy, periodic intervals, escalation deadlines | | | **Proposes** (intervals, policy) | **Approves** | **Approves** (deadlines) | |
| Regulator reporting, disclosure, data requests | Collects facts | | | | **Decides** | **Advises** |
| Pay for minor customer damage up to $[X] **[A]** | **Decides** | | | | Above $[X] | |

**Ops can restrict. Only engineering can lift.** That covers robot holds, task restrictions and configuration approvals. Approving a release and recording it are two steps by different people. **[PLATFORM]** for holds and configurations; **[MANUAL]** for task restrictions (section 15).

## 1.2 Gates and where they live in the platform

| Gate | Platform check | Where you see it | Who records the inputs |
|---|---|---|---|
| G1 Home Eligible | An approved, current home assessment; every adult consented on the current form version; the visit's tasks allowed in that home | Planner (`/planning/`); visit blocked with the reason | Field operator assesses; ops lead approves |
| G2 Robot Eligible | Robot *in service*; configuration approved for home use; base home-use authorization; approved capability profile; no blocking hold; a **qualifying periodic inspection** that is not overdue and stays valid past the planned unloading end | Fleet (`/fleet/`), overview "Planning eligibility" and "Periodic inspection" columns | Engineering (configuration, profile, interval); ops (inspections) |
| G3 Visit Ready | Fresh passing evidence of every kind the readiness policy requires, on the robot's current configuration; battery above the policy minimum; equipment present; recovery plan; inspectors qualified; production: approved policy and approved inspection template | Readiness page of the visit (`/readiness/visits/<code>/`) | Field team; ops lead issues the authorization |
| G4 Site Check | Custody at the site; consent covers every adult present; every site condition OK → **Go**, else **No-Go** | Execution run (`/execution/`) | Lead operator |
| G5 Return to Service | Every approval slot of the hold's policy filled (and for an inspection hold, a newer PASS), then the release recorded | Robot page (`/fleet/<code>/`) | Approvers named by the policy; ops lead or engineering lead records |

Robot status in the command center (`/ops/`) is one of: *Unaccounted*, *Custody unknown*, *Deployed*, *Returned - awaiting inspection / reconciliation / trip closure*, *Held*, *Assigned*, *At hub - not eligible*, *At hub - eligible for planning*. "Eligible for planning" only means the planner may schedule the robot. It is never a departure decision.

# 2. Roles

| Role | v1.0 code | Platform role | Owns |
|---|---|---|---|
| Operations lead | OPS-A | `ops_lead` | The schedule, G1 standard decisions, departure authorizations, recording hold releases, daily report and close-out, weekly review, customer relationship during visits, first call on any incident |
| Field operator | OPS-B | `field_operator` | Readiness evidence, transport and custody, on-site monitoring, run records, incident reports |
| Engineer | (part of ENG-1) | `engineer` | Configuration records, capability profiles, inspection templates and interval proposals; may fill engineering review slots where a policy allows |
| Engineering lead | ENG-1 | `engineering_lead` | Robot procedures (e-stop, lifting, charging, recovery, battery limits), configuration and task approvals, severity classification, root cause, engineering approvals for G5 |
| Safety reviewer | SAF-1 | `safety_reviewer` | Review of safety procedures, field kit, charging area; safety approvals (templates, intervals, policies, G5 slots); severity classification; acknowledging escalations |
| Founder | FND | `founder` | Risk acceptance, founder approvals (G5 slots, escalation deadlines), external communication, regulator decisions with legal |
| Legal counsel | LEG-1 | none (outside the platform) | Consent form, recording and privacy rules, reporting duties, insurance terms |

**Two-person rule [OPS].** Every visit has a lead operator and a second operator, both trained. Solo visits are not allowed. We revisit only with evidence (a run of safe visits) and engineering and founder approval. The planner checks that the crew is assigned. **[PLATFORM]** for assignment; **[MANUAL]** for training.

**Training gate [OPS].** A new operator shadows 3 visits, runs 3 visits while watched, then is signed off by the ops lead (*Field trained = Yes*). **[MANUAL]**

**Readiness inspectors.** Both people who sign an inspection (performer and verifier) must hold a current **`readiness_inspector` qualification**, granted by the safety reviewer with a reference to the training record. The platform refuses passing inspection evidence and refuses authorization otherwise, and revoking a qualification blocks departures that relied on that person's inspections. **[PLATFORM]** The training itself, and its content, is **[MANUAL] [LEGAL/SAFETY]**: the platform stores only a reference.

# 3. Customer onboarding

**Goal:** the customer knows what the robot can and can't do, how visits work and what data we keep, before anyone schedules anything.

**Owner:** ops lead. **Output:** the home (site) record and a draft assessment.

1. **Intake call (20 min).** Explain the pilot in plain words. Use the founder-approved *What to expect* sheet. Never promise a task that is not on the approved task list.
2. **Ask only what we need:** number of adults and children (ages, not names), pets, rough area (zone), parking, which tasks they hope for, good times.
3. **Do not ask for or write down** medical details, other people's names, door codes, or anything not needed for the visit. Contact details go in the customer system (CRM), never in the platform.
4. **Explain recording and data** using the legal-approved script: what the robot records, who can see it, how long we keep it, how to ask for deletion.
5. **Book the home assessment** (Section 4). No visit can be confirmed before G1 passes. **[PLATFORM]**

::: {.legal}
**[LEGAL/SAFETY]** The *What to expect* sheet, the recording explanation and the consent form need legal review. California requires all-party consent to record confidential communications (Penal Code 632); how this applies to a robot's cameras and microphones, guests and household staff is a question for counsel, not ops.
:::

**Escalate to the founder before going further if:** the household includes an infant or toddler, a person with dementia or limited mobility, a home medical device, or a public figure.

**Customer commitments and complaints [MANUAL].** Follow-up promises ("we'll tell you by [time]"), compensation talks and complaints have no platform record. Keep them in the CRM with an owner and a date, and review open ones at the daily sync.

# 4. Home suitability assessment (Gate 1)

**Owner:** field operator assesses; ops lead decides standard cases; engineering decides borderline cases. **Form:** F1 Household Suitability Assessment. **Record:** the home's assessment in the planner.

**Steps**

1. **Video walk-through (20 min)** with the customer. Rule out obvious no-goes early.
2. **In-person survey (45–60 min)** by two people. Measure the path from parking to each task area, test Wi-Fi in each task area, photograph floors and hazards (no people in photos).
3. **Fill F1.** Every "No-go" item stops the assessment.
4. **Decide:** *Eligible*, *Eligible with restrictions*, or *Not eligible*. Write the reason. If something is missing, there is simply no approved assessment yet: the platform shows the home as not assessed (v1.0 called this *Pending*).
5. **Consent:** every adult who lives there signs the current consent form version. Record count of adults and count consented. Partial consent = not eligible. **[PLATFORM]** for the counts and the form version; the people behind the counts are **[MANUAL]**.
6. **Set re-assess-by date:** 90 days **[A]**, or sooner if the customer tells us the home changed.

**Decision table**

| Finding | Result |
|---|---|
| Path to task area narrower than engineering's minimum, or has steps | **Not eligible** (stairs are not an approved task) |
| Task area has hazards we can exclude (stove, balcony, pool) | Eligible with restrictions: exclude that area |
| Rugs, thresholds, cords in task areas | Eligible with restrictions: secure or exclude (added after INC-001) |
| Wi-Fi below threshold in a task area | Hotspot needed; if hotspot also fails, not eligible for tasks that need a connection **[ENG]** |
| Pets that can't be kept in a separate room | Not eligible until they can |
| Children under [X] years, or a vulnerable adult **[A]** | Founder + safety reviewer decide |
| Not every adult consents | **Not eligible** |
| Unsure | No decision yet; ask engineering (borderline case) |

**Eligible with restrictions cannot be booked yet.** The platform treats a restricted home as needing review and refuses to confirm or authorize a visit there, because no workflow yet records that the crew acknowledged the restrictions (EM decision on D-10). Until one exists, serve only homes that are eligible without restrictions, or remove the hazard and re-assess. **[PLATFORM]**

**Visitors, staff and children [MANUAL] [LEGAL/SAFETY].** Consent is recorded as counts of adults. A visitor or a child is invisible to the platform; the G4 walk-through (Section 8) is the only check.

::: {.eng}
**[ENG]** Engineering supplies: minimum path width and turning space, floor and threshold limits, slope limit, connectivity requirement, and any task-specific space needs. Until then the survey records measurements and engineering decides.
:::

# 5. Scheduling and robot assignment (Gate 2)

**Owner:** ops lead. **Record:** the visit in the planner. The planner checks conflicts and eligibility; the ops lead resolves them.

**Visit statuses.** *Requested* → *Planned* → *Confirmed*, or *Cancelled* before departure. After departure, the visit's progress is the execution run (departed, at site, returning, at hub, inspected, reconciled, closed). Confirm a visit to a customer only when the planner shows it feasible, never when it shows a blocker.

**How a visit is sized.** The platform builds the visit timeline from the robot's and crew's reservations: pre-departure inspection + loading + travel out + time on site (setup, tasks, pack and notes all live inside it) + travel back + unloading + charging turnaround. Loading, crew size and turnaround come from the approved capability profile; an unknown value blocks planning. **[PLATFORM]** **[ENG]** the values.

**Assignment checks**

| Flag | Problem | Checked by | What the ops lead does |
|---|---|---|---|
| F1 | Robot double-booked | **[PLATFORM]** | Pick another eligible robot or move a visit |
| F2 | Operator double-booked | **[PLATFORM]** for overlaps. The 15-min buffer between visits is **not enforced**: back-to-back reservations are allowed | Move a visit; never split a team across two homes. Keep the buffer by hand **[MANUAL]** |
| F3 | Robot not eligible (hold, lifecycle, configuration, profile, periodic inspection) | **[PLATFORM]**, with the reason | Use another robot. Never ask engineering to "just clear it" for a visit |
| F4 | Home not eligible (not assessed, not eligible, restricted, re-assessment due) | **[PLATFORM]** | Finish the assessment first; don't confirm to customer |
| F5 | Consent missing or on an old form | **[PLATFORM]** | Get consent signed first |
| F6 | A planned task is not approved or is restricted fleet-wide | **[MANUAL]**. Only per-home exclusions are checked; there is no fleet-wide task list in the platform yet | Check the task against engineering's approved list before booking |
| F7 | Same robot needs charge/turnaround time between visits | **[PLATFORM]** (turnaround from the capability profile) **[ENG]** the value | Use another robot or move a visit |
| F8 | Two crew not assigned | **[PLATFORM]** | Assign a second operator or move the visit |
| F9 | Readiness failed or stale | **[PLATFORM]** at authorization | See Section 6 |
| F10 | Robot's periodic inspection overdue, missing, or due before the visit's planned unloading end | **[PLATFORM]** | Schedule the periodic inspection first. Interval unknown = the robot is not eligible |
| F11 | Outside shift window (advisory) | **[MANUAL]** | Ops lead's call. Avoid first visits to a home late in the day |

**Priorities when demand is bigger than capacity**

1. Visits with a safety or learning reason (testing a fix, a follow-up after an incident).
2. Commitments already confirmed.
3. Homes close together on the same day.
4. First come, first served.

**Never** rush a readiness check, skip a buffer, or send one person to make room for a visit. Record turned-away requests in the daily report: unmet demand is the evidence for hiring.

**Customer confirmations [OPS]:** confirm 48 h before; do the **24-hour re-check call** (anything changed in the home? new people, pets, furniture, renovations?). A change sends the home back to the ops lead for a G1 review: record a new assessment that supersedes the current one. The superseded assessment stops counting at once, so the platform blocks the visit until the new one is approved. **[MANUAL]** the call itself is not recorded.

# 6. Pre-deployment readiness (Gate 3)

**Owner:** the two crew who inspect; the ops lead issues the authorization. **Form:** F2 Readiness Inspection. **Record:** readiness evidence on the visit's readiness page.

**When:** before departure, within the freshness window of the **approved readiness policy** (each evidence kind has its own maximum age). A check from yesterday normally does not count. **[PLATFORM]** **[ENG]** the windows.

**The four kinds of readiness record**

| Record | What it is | Notes |
|---|---|---|
| Pre-departure inspection | The robot-condition checks on an **inspection template**, performed and verified by two qualified readiness inspectors (F2 items 1–8, 11–12) | In production the template must be approved by engineering **and** safety for pre-departure use. A synthetic template is refused. **[PLATFORM]** |
| Equipment check | Restraint kit, recovery kit, charger and other kit (F2 items 9–10) | Present / missing per item |
| Battery check | State of charge | Must be at or above the approved capability profile's minimum; unknown minimum blocks **[ENG]** |
| Recovery plan | Who is on call in engineering, a second escalation contact, the escalation path, the recovery equipment | The two people must differ. Prepare it the day before |

**Steps**

1. Open the visit's readiness page. Every gate should read *No blockers* except the evidence you are about to record. If a gate is blocked, stop and fix it first.
2. Run the inspection on the approved template, using engineering's procedure for each [ENG] item.
3. **Any fail = the robot does not go.** No partial passes, no "it's probably fine".
4. Both people sign: one performs, the other verifies. The same person can't do both, and both must be qualified. **[PLATFORM]**
5. Record the equipment check, battery check and recovery plan (if not already done).
6. The page now reads **Ready to request authorization**. The **ops lead** requests the authorization. The platform re-checks everything at that moment and either issues **Authorization issued (expires HH:MM)** or refuses with every reason. **[PLATFORM]**
7. Record the departure when the robot actually leaves, before the expiry. An expired or revoked authorization cannot be used. **[PLATFORM]**

**When a check fails (decision table)**

| Situation | Action |
|---|---|
| Fail on a robot-condition item (template items flagged *robot condition*) | Record the failing inspection. The platform puts the robot on an **inspection hold at once**. Raise an **engineering ticket** the same hour with a photo of the result. **Do not open a platform incident** unless an event happened (a fall, damage, smoke): an incident would add a second hold |
| Fail on equipment or battery | Blocks only this visit. Fix it or swap |
| Another robot is eligible and free | Swap the robot on the visit, write the swap in the visit notes, run a **full new inspection** on the new robot. Never skip steps to save time |
| No spare robot | Call the customer (call, don't text): *"Our pre-visit safety check found an issue, so we're not bringing the robot today. Safety checks come first. Can we offer you [two slots]?"* Cancel the visit (it has not departed) and book a new one |
| Same check failed twice in two weeks | Put it on the weekly review |
| The failure looks like a battery problem (swelling, heat, smell, smoke) | Treat as **S0** (Section 11). Move people away. Follow engineering and safety's battery procedure. Do not handle the battery yourself **[ENG][LEGAL/SAFETY]** |

::: {.eng}
**[ENG]** Engineering writes the procedure and pass criteria for: battery visual check, sensor self-test, e-stop test (robot and remote, if any), function test, and the approved-configuration check, and records them as an inspection template that engineering and safety approve. The safety reviewer approves the field-kit list (including which fire extinguisher type, if any, is right for the battery chemistry).
:::

# 7. Transportation, loading and custody

**Owner:** lead operator. **Record:** the execution run (custody, trip events).

The platform keeps one custody history per robot: **hub**, **vehicle**, **site** or **unaccounted**. A robot is never in two places, and every move names who hands over and who receives. **[PLATFORM]**

1. **Lift and load only with engineering's procedure** for weight, lift points and posture. Crew size comes from the capability profile. **[ENG]**
2. Secure the robot in the vehicle as engineering specifies (cradle, straps, power state, charge level for transport). **[ENG]**
3. Load the field kit and transport kit checked in the equipment check.
4. **Record the departure.** The platform then opens the run and records an *administrative* assignment of the robot to the vehicle, **not verified**.
5. **Verify the departure assignment** before driving off: the crew confirm the robot is physically in the vehicle and record the verification. An unverified assignment shows in the exceptions inbox after the grace time. **[PLATFORM]** for the record; **[MANUAL]** for the look.
6. **Never leave the robot alone in the vehicle.** One person stays with the vehicle if both need to step away.
7. Respect the vehicle temperature limit for the battery. On hot days, don't load until departure. **[ENG]**
8. Record *Arrived at site*. At the door, hand the robot from vehicle to site custody with **two crew names** (the custodian and an identified receiver).
9. **Anything unusual on the road** (hard braking, shifting load, a strap failure): pull over safely, check, and record what happened as a session observation on the run. If the robot moved or was hit, **it does not operate on this visit**: there is no on-the-road re-inspection. Report it as an incident if there was any damage or doubt (this holds the robot), and end the visit. Do not skip the record to save the visit. **[MANUAL]**

::: {.legal}
**[LEGAL/SAFETY]** Lithium batteries installed in equipment are regulated in transport (UN3481; 49 CFR 173.185). Whether carrying our own robots in our own vehicle falls under an exception, and what labels or documents are needed, is a question for counsel. Commercial auto insurance must cover carrying robots and visits to customer homes.
:::

# 8. Arrival and household setup (Gate 4)

**Owner:** lead operator; anyone can call *No-Go*. **Record:** the run's site check.

**Before the robot leaves the vehicle**, do a 5-minute walk-through with the customer:

| Check | If not OK |
|---|---|
| Home matches its profile (no new hazards, furniture, renovations) | Exclude the area if the plan allows, or *No-Go* |
| Every adult present has signed consent | Ask them to sign now (current form) or leave the area; otherwise *No-Go* |
| Visitors, guests, household staff present | Same consent rule. If unclear, *No-Go* |
| Children and pets placed per the home plan | Wait or *No-Go* |
| Task area clear; rug edges, cords and thresholds secured or excluded | Fix with the customer or exclude |
| Wi-Fi or hotspot working in the task area | Set up the hotspot; if still failing, only tasks engineering says work offline **[ENG]** |
| Monitoring position agreed: operator in the room, clear path to the e-stop | Change the plan or *No-Go* **[ENG]** e-stop location and use |

**Safety briefing for the household (2 minutes):** where to stand, what the robot will do, that we may stop at any time, and that they can too ("say stop and we stop").

Record the **site check**. The platform computes **Go** only when custody is at the site, consent covers every adult present and every condition is OK; anything else is **No-Go**, and a No-Go is recorded, never hidden. Work sessions start and resume only on a current Go. **[PLATFORM]** In production, a synthetic site-check template cannot produce Go. Take "before" photos of the task area (no people).

**After a No-Go, or when the customer cancels once we have left:** the visit cannot be cancelled any more; it **ends**. The **ops lead** records the disposition *End visit and return* (or *Robot left on site* / *Robot unaccounted for* in Section 12), and the robot comes back through the normal return. **[PLATFORM]** The field lead calls the ops lead to record it; letting the field lead record it directly was deferred (X-7).

# 9. Safe deployment and monitoring

**Owner:** lead operator monitors; second operator watches the room and the household.

1. Run **only approved tasks** in the planned areas. No improvising, no "can it also do…?" requests on the spot.
2. **The lead operator stays in the room**, within reach of the e-stop as engineering specifies, and never between the robot and a wall or furniture. **[ENG]**
3. **The second operator** keeps people and pets out of the task area and watches for changes (a child walking in, a dog getting out).
4. **Count interventions:** every time a person helps, corrects, restarts or stops the robot, record a session observation with the time and why. **[MANUAL]**: the count is free text.
5. **Pause** the session when the work is interrupted and **resume** only on a current Go.
6. **Remote sessions (if the company uses them):** the people in the room can always override. If the remote link and the on-site team disagree, the on-site team's stop wins. **[ENG]** the remote protocol.
7. If the connection drops: hands-off, ready on the e-stop, keep people back. What the robot does on link loss is engineering's to define; follow it. **[ENG]**

# 10. Stop-work conditions

::: {.stop}
**STOP immediately (e-stop if it can be reached safely) when:**

- The robot touches a person or pet, or nearly does.
- The robot falls, starts to fall, or moves in a way nobody expected.
- Smoke, burning smell, heat, swelling or leaking from the robot or battery.
- A child, pet or unconsented adult enters the task area and can't be moved out.
- The connection drops during motion and engineering's link-loss behavior is not confirmed.
- Anything is damaged, or about to be.
- The customer asks to stop.
- **Anyone on the team feels unsafe, for any reason.**

**Never catch, hold or push a moving or falling robot.** Step back. Keep others back.
:::

After a stop: make the area safe, check people first, then follow Section 11. Restart only if the stop was for a non-safety reason (for example, a pet walked in) **and** the lead operator is sure. Any stop for the first four reasons ends that robot's visit.

# 11. Incident response and escalation

**Owner:** whoever sees it reports it; ops lead triages; the engineering lead or safety reviewer classifies. **Form:** F4 Incident Report. **Record:** the incident in the platform.

**Every incident holds the robot.** Reporting an incident places a blocking robot hold in the same step, with or without an approved incident policy, also when reported late, after the visit, or with no run. **[PLATFORM]** Something that does not concern the robot (a remark, a minor customer request) is a session observation, not an incident.

**The reporter describes; engineering or safety classify.** The incident starts *unclassified*. The engineering lead or the safety reviewer records the severity on the company's approved incident policy scale. The table below is the proposed scale **[A]**; the company approves the real one (B-07). Customer complaints are not incidents: they go to the CRM (Section 3).

| Level (proposed) | Examples | First response | Who is told, by when |
|---|---|---|---|
| **S0** | Anyone hurt; battery heat, smoke or fire | Stop. Get people out. Call 911 if needed. Nobody touches the robot unless an approved procedure allows it | **Founder within 15 minutes.** Engineering, safety reviewer, legal the same hour |
| **S1** | Fall; near miss with a person; major damage; unexpected motion | Stop. Make the area safe. Don't move the robot except per procedure | Founder and engineering the same day (aim: within 1 hour) |
| **S2** | Task failure, stuck, minor damage, connection loss | Stop the task. Recover per procedure | Next daily sync; engineering ticket within 24 h |
| **S3** | Small glitch, cosmetic issue | Log it | Weekly review |

**Steps after any incident**

1. **People first:** check everyone, including the team.
2. **Make safe:** robot stopped; area cleared; follow engineering's guidance before moving it.
3. **Preserve evidence:** do not power-cycle, wipe or "test it again" unless engineering asks. Note the time, configuration, task, what you saw. Photos of the scene and damage (not people).
4. **Report** in the platform (this holds the robot), then **notify** per the table. Use the phone for S0/S1; message for S2.
5. **Record each notification as an escalation** in the platform (to the founder, safety reviewer or engineering), and the person told **acknowledges** it there. The phone call is still what reaches them: **the platform pages nobody.**
6. **Customer:** facts only, no guesses about the cause, no promises about compensation beyond the approved limit. *"The robot lost balance. Is everyone OK? We've stopped it and won't move it until it's safe. We'll follow up by [time] with what we know."* Record the promise in the CRM.
7. **Write F4 within the hour** (S0/S1) or before end of day (S2/S3). Facts only.
8. **Engineering ticket** in engineering's own tracker; put its ID in the incident notes. **[MANUAL]**: there is no ticket field yet.
9. **Same day:** cancel this robot's remaining visits that have not departed. Engineering decides if the task or configuration should pause fleet-wide.

**Escalation deadlines.** The founder and the safety reviewer approve an **escalation deadline set**: per severity level (and for unclassified incidents), which roles must be escalated to within how many minutes of the report. The exceptions inbox then shows **ESCALATION_OVERDUE** when an escalation is late or unacknowledged, and the incident page shows each deadline. If no approved set exists, the inbox says so (*deadlines not configured*). In production only an owner-approved set counts. **[PLATFORM]** for visibility; the calls are **[MANUAL]**.

**Duplicate reports.** If two people report the same event, keep both. Each holds the robot, so each needs its own release; note in both that they describe the same event. Never delete. **[MANUAL]** (no link field yet).

::: {.legal}
**[LEGAL/SAFETY]** Whether an event must be reported outside the company (for example, the CPSC duty to report defects that could create a substantial risk of injury, within 24 hours; workplace-injury reporting; insurer notice) is decided by the founder with counsel. Ops collects facts and keeps evidence. Do not speak to media or post anything about an incident.
:::

# 12. Robot recovery and return to hub

::: {.eng}
**[ENG] No recovery procedure is written here.** How to make a fallen or faulted robot safe, whether and how to lift it, how to power it down, and how to move it out of a home must come from engineering for this robot and be reviewed by the safety reviewer. Until those procedures exist and the team is trained on them:

- Do not lift or move a fallen robot. Call the on-call engineer named in the visit's recovery plan, then the escalation contact, and wait.
- If the robot cannot be made safe, keep the area closed off and stay with it.
- Plan with the customer for the robot to stay until engineering arrives, if needed.
:::

**If the robot must stay in the home**, the ops lead records the disposition **Robot left on site**: custody stays at the site with a named custodian, and the trip stays open until the robot is recovered. If nobody knows where the robot is, record **Robot unaccounted for** at once; it shows as a priority-1 item in the exceptions inbox. **[PLATFORM]** for the record. Who is responsible for the robot overnight, the household's consent to its presence and insurance are **[MANUAL] [LEGAL/SAFETY]**.

**After a visit with no incident:** pack per engineering's procedure, take "after" photos, short debrief with the customer (rating, comment, no personal details), record the session stop, hand custody from site to vehicle with two names, record *Departed site*, and finish the run notes before driving off.

**After a visit with an incident:** the robot travels only as engineering allows, goes straight to the quarantine bay or engineering bench, and the physical location inside the hub is written on its tag and in the incident notes (the platform's custody says only *hub*).

# 13. Post-deployment inspection and return

**Owner:** field operator. **Record:** the execution run.

The vehicle coming back is not the robot coming back. A trip closes only after all of these, in order: **[PLATFORM]**

1. Record *Arrived at hub*.
2. **Hub receipt:** hand custody from vehicle to hub, naming the custodian and an identified hub receiver.
3. **Post-visit inspection** of the robot, referencing the visit, after the hub receipt. Visual check for new damage against the morning photos. Anything new = a failing item (which holds the robot) or an incident, now, not tomorrow.
4. Put on charge using the approved charger and charging position. **[ENG][LEGAL/SAFETY]** charging area layout and fire safety.
5. **Upload logs and media** to engineering's store. **[MANUAL]** (no upload link in the platform yet).
6. **Return reconciliation:** confirm the robot, kit and records all came back. *Reconciled* lets the trip close; anything unresolved keeps it open.
7. **Close the trip.** A robot left on site or unaccounted keeps the trip open.

A robot cannot be authorized again until its run is closed and it is back at the hub. **[PLATFORM]**

# 14. Maintenance and engineering handoff

**Owner:** engineering owns the schedule and procedures; ops runs the parts engineering has trained us on.

- **Configurations, not software strings.** A robot's configuration is its pattern (hardware revision, release, firmware set) plus an immutable manifest. Only patterns approved for home use may go out. A new manifest restarts the periodic-inspection clock. **[PLATFORM]**
- **Periodic inspection.** Each configuration pattern needs one **approved periodic inspection rule**: the interval, proposed by engineering with a procedure reference and approved by the engineering lead and the safety reviewer. A robot is not eligible when its periodic inspection is overdue, missing on its current configuration, or when the interval is unknown, and an authorization is refused if the inspection would expire before the planned unloading end. **[PLATFORM]** The interval value is **[ENG]**: it is **unknown until engineering supplies it**. v1.0's illustrative 14 days is not used anywhere. The demo uses a clearly labelled synthetic interval that production refuses.
- **What counts as a periodic inspection:** a complete PASS on the robot's current configuration, on a template approved for periodic use, performed and verified by two qualified readiness inspectors. The overview shows each robot's state (*due*, *overdue*, *no qualifying inspection*, *interval unknown*), and the inbox warns ahead of the due date.
- **Handoff to engineering:** robot on the bench or in the quarantine bay, incident code on a tag, logs uploaded, location noted. Engineering confirms receipt in the ticket.
- **Holds without an incident:** anyone with the right may place a hold (maintenance due, suspect part, engineering wants the robot). Release follows Section 15.
- **Handback from engineering:** only through a recorded hold release (Section 15). A robot back on the charging rack without one is still held, and the platform shows it.

# 15. Engineering feedback, corrective actions and return to service (Gate 5)

**Owner:** engineering lead. **Form:** F5 Return-to-Service Approval. **Record:** the hold on the robot's page.

**The loop**

1. **Report** (F4) → 2. **Triage** at the daily sync: severity classified, owner named → 3. **Reproduce** at the hub mock-home area if possible → 4. **Root cause** written in plain words → 5. **Fix** → 6. **Verify the fix at the hub** (date) → 7. **Regression check** added to the readiness inspection template, home assessment or task rules (or a written reason why not) → 8. **Release approvals** → 9. Release recorded; the robot is eligible again and still needs fresh readiness evidence and a new authorization for its next visit.

**How a hold is released.** **[PLATFORM]**

- Every hold is bound, when it is placed, to the **active approved hold-release policy**. The policy says which approval slots (engineering, safety, founder, ...) each kind of hold needs. These requirements are **fixed at placement**: a later policy or a severity decision does not change them.
- The people in those roles approve their slots. Then the **ops lead or engineering lead records the release**. Approving and recording are separate steps.
- **Readiness-fail (inspection) holds** need the engineering approval **and** a newer PASS inspection on the robot's current configuration. Either alone is not enough.
- **Incident holds** cannot be released while the incident is still being responded to.
- In **production**, only a fully approved policy from an **approved procedure** can release a hold. A hold bound to a draft or synthetic policy shows as **HOLD_UNRELEASABLE** and needs a **re-basing** decision to the active approved policy, approved like any release. Until the company approves a hold-release policy, no hold can be released in production, and the command center shows **HOLD_POLICY_NOT_APPROVED** as a production blocker. That is deliberate: the platform fails closed.

**Approvals by severity (company policy proposal, not approved).** v1.0's rule, kept as the proposal the company may adopt in its hold-release policy (B-07):

| Incident | Proposed approvals |
|---|---|
| S2/S3 that blocked the robot | Engineering |
| S0/S1 | Engineering **and** safety reviewer **and** founder |

The platform cannot apply this today, because severity is decided after the hold is placed and approvals are fixed at placement. Choosing between one strict rule for every incident and a future severity-aware release (X-2) is a separate company decision (EM decision on D-07). Until it is made, follow the approved policy, which may be stricter than this table, never looser.

**Closing an incident does not release the robot.** Resolving or closing an incident never releases its hold; only the release workflow does. **[PLATFORM]**

**Patterns.** When a similar problem shows up again on any robot:

1. Note the earlier incident's code in the new one.
2. At the third occurrence, or sooner if any one was S1+, open a parent issue in engineering's tracker that names the pattern and the shared conditions (task, configuration, home features).
3. **Restrict** the task or configuration fleet-wide. Configuration: engineering withdraws its approval for home use (**[PLATFORM]** blocks planning). Task: there is no fleet-wide task list yet, so the ops lead tells the team and checks every booking by hand (**[MANUAL]**, F6).
4. Tell affected customers the task is paused.
5. Only engineering lifts the restriction, after a verified fix and two weeks of watching **[OPS]**.

# 16. End-of-day reporting and close-out

**Owner:** ops lead. **When:** by 18:00. **Record:** the daily report (written) and the **daily close-out view** (`/ops/closeout/`, read-only).

1. **Open the close-out view.** It lists every run that departed today and is still open, runs carried over from earlier days, custody assignments not verified, post-visit inspections outstanding and reconciliations unresolved, each with its reason. It is computed from the records; it records nothing and **is not a sign-off**. **[PLATFORM]**
2. Work each listed item, or write in the daily report why it stays open (a robot left on site, a reconciliation waiting on engineering).
3. Write four short lines:
   1. **What happened:** visits done, stopped, ended early; new incidents by code.
   2. **Top risk for tomorrow:** one item.
   3. **Decisions needed:** who, by when.
   4. **Escalations:** what went to whom, and whether each was acknowledged.

**Shift handover [OPS].** Before leaving: the close-out view is empty or every item has a written reason, every departed visit has its run records, every failure has its hold and ticket, and no robot is *Unaccounted*. **[MANUAL]** the handover itself is not recorded in the platform.

The close-out day follows the deployment's time zone, set by the schema owner. If it is not set, the view lists every open run without assigning days.

# 17. Daily and weekly cadence

| When | What | Who | Time |
|---|---|---|---|
| 07:30 | Open the command center: fleet overview (robot status, periodic inspection, production blockers panel) and the **exceptions inbox** (P1 first). Any robot held or not eligible for today's visits? | Ops lead | 10 min |
| 08:00–09:00 | Readiness evidence for today's visits; authorization; departure and verified assignment | Field team, ops lead | 30–60 min |
| 08:45 | **Daily sync:** yesterday's issues, today's visits, open incidents and overdue escalations, open customer promises, who decides what | Ops lead, engineering lead (+ founder if S0/S1 open) | **15 min** |
| Visits | Site check on arrival; session records; run notes before leaving the street | Field team | — |
| 17:00 | Hub receipt, post-visit inspection, logs uploaded, reconciliation, trip closure | Field team | 30 min |
| 18:00 | Close-out view and daily report | Ops lead | 10 min |
| Friday | **Weekly review:** metrics page, repeat issues, restrictions, open holds and releases, periodic inspections falling due, next week's plan (timeline), unmet demand | Ops lead, engineering lead, founder | **30 min** |

Targets on the metrics page stay blank until there are four weeks of data. Small numbers (under 20) are shown but not trusted.

# 18. Customer communication: short scripts

| Moment | Say |
|---|---|
| 24-hour re-check | "Has anything changed at home since we last came? New furniture, rugs, guests, pets, anything being repaired?" |
| Readiness failure, no spare | "Our pre-visit safety check found an issue, so we won't bring the robot today. Safety checks come first. Can we offer you [two slots]?" |
| No-Go at the door | "Something in the room isn't how we planned it, and we only run the robot when it is. Let's set a new time." |
| After a stop | "We stopped the robot to be safe. Everyone OK? We'll look into what happened and tell you by [time]." |
| Customer cancels before we leave | "No problem at all. Would [two slots] work instead?" |
| Customer cancels while we drive | "No problem at all. We'll head back, and would [two slots] work instead?" (the visit ends; the ops lead records *End visit and return*) |

# 19. Validation register: what must be approved before real use

The last column says whether the platform refuses to go ahead until the item is supplied (status after Slice 7).

| # | Item | Owner | Section | Platform blocks until supplied? |
|---|---|---|---|---|
| E1 | Readiness check procedures and pass criteria (battery, sensors, e-stop, function, configuration) | Engineering + safety | 6 | **Yes** for the record: production needs an approved readiness policy and an inspection template approved for pre-departure use. The content of the checks is human |
| E2 | Lifting, loading, securing, transport power state and charge level, vehicle temperature limit | Engineering | 7 | **Partly**: crew size, loading and turnaround must be known; power state and temperature are not checked |
| E3 | E-stop location, reach and use; monitoring position | Engineering | 8, 9 | **Partly**: stop behaviour must be documented; a synthetic site-check template cannot give Go in production |
| E4 | Link-loss behavior; remote session protocol | Engineering | 9 | **No** |
| E5 | Fall and fault recovery, power-down, moving a robot out of a home | Engineering + safety | 12 | **Partly**: a recovery plan (people, path, equipment) is required; the physical procedure is human |
| E6 | Charging area, battery storage, fire safety | Engineering + safety (+ fire marshal if needed) | 13 | **No** |
| E7 | Periodic inspection interval, turnaround time, readiness freshness windows | Engineering (+ safety for the interval) | 5, 6, 14 | **Yes**: unknown interval or turnaround blocks; freshness is policy data |
| E8 | Home limits: path width, floors, slopes, connectivity | Engineering | 4 | **No** (the assessment decision is human) |
| L1 | Consent form, recording rules, data retention and deletion | Legal | 3, 4 | **Partly**: a current consent form version must be configured and counts must match; retention is not built |
| L2 | Regulator and insurer reporting duties | Legal + founder | 11 | **No** |
| L3 | Battery transport rules | Legal / hazmat specialist | 7 | **No** |
| L4 | Insurance: product, general liability, auto, workers' comp | Legal + founder | 7, 11 | **No** |
| L5 | Damage payment limit, cancellation policy | Founder | 1, 5 | **No** |
| L6 | Vulnerable residents policy (young children, dementia, medical devices) | Founder + safety + legal | 3, 4 | **No** |
| P1 | Incident severity scale (incident policy) | Engineering + safety + founder | 11 | **Yes**: a synthetic policy is refused in production |
| P2 | Hold-release policy from an approved procedure, and re-basing of existing holds | Engineering lead + safety | 15 | **Yes**: no hold is released in production without it |
| P3 | Escalation deadlines | Founder + safety | 11 | **Visible**: overdue escalations need an owner-approved set; missing deadlines are shown as unconfigured |
| P4 | Readiness inspector qualifications, from real training records | Safety | 2, 6 | **Yes** |
| P5 | Site-check template | Ops + safety | 8 | **Yes**: synthetic gives no Go in production |
| P6 | Close-out time zone, readiness look-ahead and grace thresholds | Ops (set by the schema owner) | 16, 17 | **Visible**: missing settings are shown and their rules switched off |

Until an item is approved, the step that depends on it is **not done**, and the robot does not go. Where the last column says **Yes**, the platform enforces that. Where it says **No** or **Partly**, it is a human promise, and the ops lead checks it.

# 20. Simulated versus real

| Today (simulated) | For a real launch |
|---|---|
| All robots, homes, people, visits and incidents are synthetic | Real records, entered by the people who own them |
| Policies, templates, intervals, deadlines and qualifications are `synthetic_demo`, clearly labelled, and usable only in demo mode | Each is supplied by its owner (Section 19), with its approvals recorded |
| Demo mode lets the workflows run end to end so the team can train | Production mode. In it, the platform refuses every authorization and release whose inputs are synthetic or missing, and lists each gap as a **production blocker** |
| The platform records what people report; it does not move, stop or check a robot | Robot safety functions and physical checks remain engineering's, validated outside the platform |
| Nothing is paged, emailed or sent | The human notification calls in Section 11 stay mandatory |

The platform makes gaps visible and gives every decision an owner and a record. It cannot check that a person did a check properly, and it does not replace robot safety functions or qualified safety review.
