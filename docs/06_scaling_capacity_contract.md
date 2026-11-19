# Slice 6 Contract: Scaling and Capacity Model

Status: DRAFT rev 4 for engineering review · 2026-11-19 · baseline `slice-5-closed` (`de21a1d`). Planning only;
no implementation until the EM freezes this document. Rev 2 applied the EM review of rev 1 (`6d96302`); rev 3 applies
the review of rev 2 (`316aebc`); rev 4 makes the recovery cohorts explicit (review of `0b1845b`); section 10 maps
every item. Companion: `docs/06a_workbook_inventory.md` (read-only inventory of `~/humanoid-fleet-ops`).

Everything is SYNTHETIC. The model is a **forecast built on explicit assumptions**, never a validated company
operating plan. Unknown robot specifications, deployment economics and customer demand stay visible as unresolved
inputs; nothing in this slice invents a real robot spec, price or safety figure.

## 0. Purpose

How the operation runs at 5 robots and what must change to reach 10, 25 and 50: completed deployments per week, the
binding constraint at each step, when to hire field crews, recovery engineers and ops leads, when another vehicle or
inspection station is needed, cost per completed deployment, and which operational improvement adds the most
completed deployments.

## 1. Architecture (D1 approved)

```
Slices 2-5 records (synthetic demo) -> capacity.baseline -> measured inputs (labelled synthetic, with n and window)
input registry (provenance)         -> capacity.model (pure Python, authoritative) -> ScenarioResult
ScenarioResult                      -> capacity.workbook (generated .xlsx, live formulas, validated projection)
                                    -> capacity.report (founder tables) + read-only /ops/capacity/ (D5 approved)
```
New Django app `capacity`; no data models, no writes. `model.py` imports nothing from Django. The old
`05_Capacity_Cost_Model.xlsx` stays untouched; its layout and wording may be reused, its formulas are not.

## 2. Input registry (`capacity/inputs.py`) - exact list, 38 inputs + 1 scenario axis

`Input(key, label, unit, time_basis, provenance, source, conservative, base, optimistic, capacity_direction, notes)`.
`provenance` ∈ {`platform_measured`, `synthetic_assumption`, `unknown_company_data`} (rev 1 meanings). Every
`unknown_company_data` value is an **illustrative placeholder**; every output that depends on one lists it.
`capacity_direction` ∈ {`higher_is_better`, `lower_is_better`, `none`} says how the input moves capacity, used only
by the ordering test. Canonical units: minutes for durations inside a day, working days for hold latency, hours for
engineer and maintenance work, km/h for speed, synthetic currency units (SCU) for money, weeks for the simulation step.

**Scenario axis:** `fleet_size` ∈ {5, 10, 25, 50} robots.

| # | key | unit / basis | provenance | cons / base / opt | dir |
|---|---|---|---|---|---|
| 1 | `inspect_min` | min per visit (pre-departure inspection) | platform_measured* | 45 / 30 / 20 | lower |
| 2 | `load_min` | min per visit | platform_measured* | 30 / 20 / 15 | lower |
| 3 | `travel_one_way_min` | min per visit leg | platform_measured* | 60 / 40 / 25 | lower |
| 4 | `on_site_min` | min per visit (customer session) | platform_measured* | 120 / 120 / 120 | none |
| 5 | `unload_min` | min per visit | platform_measured* | 30 / 20 / 15 | lower |
| 6 | `post_inspect_min` | min per visit (post-visit inspection, Slice 4) | synthetic_assumption | 45 / 30 / 20 | lower |
| 7 | `charge_between_visits_min` | min robot-only gap between two visits in one day | unknown_company_data (B-04) | 180 / 120 / 60 | lower |
| 8 | `runtime_limit_min` | min on site per charge | unknown_company_data (B-04) | unconstrained in this illustrative calculation (not a claim about the robot) | none |
| 9 | `working_day_min` | min per working day (09:00-16:30) | synthetic_assumption | 450 / 450 / 450 | higher |
| 10 | `working_days_week` | days per week | synthetic_assumption | 5 / 5 / 5 | higher |
| 11 | `scheduling_efficiency` | fraction of theoretical customer slots bookable within customer time windows | synthetic_assumption | 0.70 / 0.80 / 0.90 | higher |
| 12 | `robots_per_visit` | robots | platform_measured (Slice 2: one robot per visit) | 1 / 1 / 1 | none |
| 13 | `vehicle_robot_capacity` | robots per vehicle | platform_measured (vehicle capacity) | 1 / 1 / 1 | none |
| 14 | `crew_size` | people per visit | platform_measured (lead + second) | 2 / 2 / 2 | none |
| 15 | `incidents_per_visit` | probability a completed visit has ≥ 1 incident (counted once per visit) | platform_measured* | 0.10 / 0.05 / 0.02 | lower |
| 16 | `holds_per_incident` | holds per incident | platform rule (Slice 4: every incident holds) | 1 / 1 / 1 | none |
| 17 | `hold_min_days` | policy minimum working days a held robot is out regardless of engineers (approvals, re-inspection); must be ≤ `working_days_week` | synthetic_assumption (a policy minimum, not calibrated from observed durations) | 3 / 2 / 1 | lower |
| 18 | `recovery_hours_per_hold` | engineer hours per hold | synthetic_assumption | 8 / 5 / 3 | lower |
| 19 | `engineer_hours_per_week` | recovery hours per engineer per week | synthetic_assumption | 30 / 32 / 35 | higher |
| 20 | `maintenance_hours_per_robot_month` | planned maintenance hours | unknown_company_data (B-08) | 8 / 6 / 4 | lower |
| 21 | `demand_visits_per_robot_week` | customer visits demanded per robot per week | unknown_company_data | 3 / 4 / 5 | none (excluded from ordering) |
| 22 | `configured_crews` | crews in the current team | synthetic_assumption | 2 / 2 / 2 | higher |
| 23 | `configured_vehicles` | vehicles in the current team | synthetic_assumption | 2 / 2 / 2 | higher |
| 24 | `configured_engineers` | recovery engineers in the current team | synthetic_assumption | 1 / 1 / 1 | higher |
| 25 | `configured_inspection_stations` | parallel inspection stations | synthetic_assumption | 1 / 1 / 1 | higher |
| 26 | `configured_ops_leads` | ops leads | synthetic_assumption | 1 / 1 / 1 | none |
| 27 | `crew_hire_trigger` | max planned crew utilization | synthetic_assumption | 0.80 / 0.80 / 0.85 | none |
| 28 | `ops_lead_span` | crews per ops lead | synthetic_assumption | 6 / 8 / 10 | none |
| 29 | `staffing_bound_per_robot` | max units of each resource per robot in the staffing search | synthetic_assumption | 4 / 4 / 4 | none |
| 30 | `field_operator_cost_month` | SCU per operator-month | synthetic_assumption | 7000 / 6500 / 6000 | none |
| 31 | `engineer_cost_month` | SCU per engineer-month | synthetic_assumption | 12000 / 11000 / 10000 | none |
| 32 | `ops_lead_cost_month` | SCU per lead-month | synthetic_assumption | 10000 / 9500 / 9000 | none |
| 33 | `vehicle_fixed_cost_month` | SCU per vehicle-month | synthetic_assumption | 1500 / 1300 / 1100 | none |
| 34 | `vehicle_cost_per_km` | SCU per km | synthetic_assumption | 0.9 / 0.7 / 0.6 | none |
| 35 | `avg_speed_kmh` | km/h door to door (converts travel minutes to km) | synthetic_assumption | 30 / 35 / 40 | none |
| 36 | `maintenance_allowance_robot_month` | SCU per robot-month | unknown_company_data | 1500 / 1000 / 700 | none |
| 37 | `overhead_month` | SCU per month | synthetic_assumption | 15000 / 12000 / 10000 | none |
| 38 | `horizon_weeks` | weeks simulated | synthetic_assumption | 26 / 26 / 26 | none |

`*` platform_measured inputs use the baseline value when its sample rule (section 6) is met; otherwise the registry
fallback shown, and the report says which. Constants: weeks per month = 52/12.

## 3. Resource cycles (D3 approved with changes)

Each resource is modelled as **whole visits per working day**, with each visit an indivisible block (a stated
conservative approximation; no partial visits, no overlap of two visits for one crew or vehicle).

| resource | busy per visit | between visits | visits per day |
|---|---|---|---|
| robot | `Tr = inspect + load + 2·travel + on_site + unload + post_inspect` | `charge_between_visits_min` (robot only; the crew is free) | largest k with `k·Tr + (k−1)·charge ≤ W` (0 if `Tr > W`) |
| crew | `Tc = Tr` (the crew inspects, loads, travels, supervises, unloads and inspects after; it does **not** supervise charging or maintenance) | none | `floor(W / Tc)` |
| vehicle | `Tv = load + 2·travel + on_site + unload` | none | `floor(W / Tv)` |
| inspection station | `Ti = inspect + post_inspect` **per robot** (each robot is inspected) | none | `floor(W / Ti)` robot-inspections |

The robot starts each day charged (overnight charging is assumed and labelled as an assumption). **Transport mode:
one vehicle trip serves one site**; it carries `robots_per_visit` robots and is INFEASIBLE if
`vehicle_robot_capacity < robots_per_visit`; multi-site batching is out of scope until modelled. **Multi-robot visits
(`robots_per_visit = r > 1`):** one customer visit consumes r robot-visit slots and r robot-inspections, but one crew
slot and one vehicle trip; carrying r robots never creates r customer deployments (section 4 divides robot and
inspection capacity by r). If
`runtime_limit_min` is set and `< on_site_min`, the visit is INFEASIBLE for the robot. Any INFEASIBLE resource has
capacity 0 and the reason is reported; nothing goes negative.

## 4. Weekly simulation (D4, removes the circular dependency)

Deterministic expected-value simulation over `horizon_weeks`, step = one week. Week t uses only state from week t−1,
so there is no circularity. With `D = working_days_week`, `eff = scheduling_efficiency`,
`m = maintenance_hours_per_robot_month / (W/60 · D · 52/12)` (fraction of robot time in planned maintenance):

```
B_0 = 0, U_0 = 0                                    (recovery backlog in jobs; unavailable robots for the next week)
A_t  = max(0, (N − U_{t−1}) · (1 − m))              available robots (held robots do no planned maintenance: no double count)
R_t  = floor(eff · A_t · v_robot · D / r)           robot capacity in customer visits/week (r = robots_per_visit)
C    = floor(eff · crews · v_crew · D)
V    = floor(eff · vehicles · v_vehicle · D)        (one trip per visit; 0 if vehicle_robot_capacity < r)
I    = floor(stations · v_inspection · D / r)       (robot-inspections per week / r; not bound by customer windows)
cap_t = min(R_t, C, V, I);  binding = argmin, ties in the order robot, crew, vehicle, inspection
done_t = min(cap_t, demand),  demand = demand_visits_per_robot_week · N,  unmet_t = demand − done_t
holds_t = done_t · incidents_per_visit · holds_per_incident
jobs_capacity = engineers · engineer_hours_per_week / recovery_hours_per_hold
O_t = B_{t−1}                                        older cohort: holds carried from earlier weeks
rec_old_t = min(O_t, jobs_capacity)                  FIFO: older holds are recovered first
rec_new_t = min(holds_t, jobs_capacity − rec_old_t)  then this week's holds, with what capacity is left
B_t = (O_t − rec_old_t) + (holds_t − rec_new_t)      backlog at week end (older remainder + new remainder)
U_t = B_t + rec_new_t · f_min + rec_old_t · 0,       f_min = hold_min_days / D  (≤ 1, registry-validated)
```
**Cohorts and the minimum hold.** Two cohorts each week: the *older* backlog `O_t` (every such hold was placed at least
one week ago, so its policy minimum of at most one week has already elapsed) and the *new* holds of week t.
Unavailability for next week, in mutually exclusive states: every hold still in the backlog (older or new remainder)
is out the whole week (counted once, in `B_t`); a **new** hold recovered this week is still out for its minimum
`f_min`, because recovering early does not shorten the minimum hold; an **older** hold recovered this week is out
for nothing more (its minimum has passed). Approximation, stated in the report: weekly granularity with expected
values; new holds are treated as if placed at the start of their week, so the remaining minimum falls into the next
week as `f_min` (an upper bound on the true remainder), and `hold_min_days ≤ working_days_week` keeps every minimum
inside one step. Golden cases (with `f_min = 0.4`):

| case | O | holds | jobs_capacity | rec_old | rec_new | B | U |
|---|---|---|---|---|---|---|---|
| backlog carried, capacity below the older cohort | 4 | 2 | 3 | 3 | 0 | 3 (1 old + 2 new) | 3 |
| capacity clears the older cohort and part of the new | 1 | 3 | 2 | 1 | 1 | 2 (0 old + 2 new) | 2.4 |
| worked example (section 7), everything recovered | 0 | 0.4 | 6.4 | 0 | 0.4 | 0 | 0.16 |
| no engineers: nothing recovered | 2 | 1 | 0 | 0 | 0 | 3 | 3 |
**One unavailability measure, mutually exclusive states.** Every held robot is in exactly one state at week end:
(a) *in the recovery backlog* - unavailable all of next week (counted once, in `B_t`); or (b) *recovered this week* -
still unavailable for the policy minimum `f_min` of next week, because recovery finishing early does not shorten
the minimum hold. Older backlog robots are past the minimum already. A robot is never counted in both (a) and (b),
so recovery backlog and minimum hold latency cannot double-count.
Reported result = week `horizon_weeks`. Flags: `RECOVERY_UNSTABLE` if `B_H > B_{H−1} + 1e-9` (backlog still
growing), `NOT_CONVERGED` if `|done_H − done_{H−1}| ≥ 1`. When the backlog is growing, the binding constraint
reported is `recovery`. Floors use a 1e-9 tolerance so exact integers are not lost to float error.

## 5. Current capacity vs recommended staffing (P0-3)

- **Current capacity** always uses the configured team (inputs 22-26), never the recommendation.
- **Robot-limited target** = `done_H` of the simulation with crews, vehicles, engineers and stations at their search
  bound (`staffing_bound_per_robot · N`): the most the fleet itself can deliver at its own reliability.
- **Recommended staffing** = deterministic greedy search starting from the configured team, goal
  `g = min(demand, target)`. Each iteration runs the simulation and applies the FIRST matching rule:
  1. any resource INFEASIBLE, or `target = 0` ⇒ stop, `UNATTAINABLE` (reason named);
  2. `RECOVERY_UNSTABLE` ⇒ add one engineer (recovery has priority over every other binding, so a robot-bound or
     already-sufficient team is never "fixed" by adding crews while the queue grows);
  3. `done_H < g` ⇒ add one unit of the binding resource among crew, vehicle, inspection station (robot cannot bind
     here because `g ≤ target`; if it does, stop `UNATTAINABLE`, reason robot capacity);
  4. crew utilization `done_H / C > crew_hire_trigger` ⇒ add one crew;
  5. otherwise stop, `MET`.
  Case "target < demand, the configured team already delivers the target, but recovery is unstable": rule 2 adds
  engineers until stable, then rules 3-5 re-check. Bound: `staffing_bound_per_robot · N` units per resource; reaching
  it on any rule ⇒ `UNATTAINABLE` (bound reached). It is a heuristic, not a global optimum,
  and the report says so. Hiring and vehicle triggers = the fleet step at which each count first increases.

## 6. Costs (per month, SCU)

```
ops_leads = configured_ops_leads (current team)  |  ceil(crews / ops_lead_span) (recommended staffing)
labor     = crews · crew_size · field_operator_cost_month + engineers · engineer_cost_month + ops_leads · ops_lead_cost_month
km_visit  = 2 · travel_one_way_min / 60 · avg_speed_kmh
vehicles  = vehicles · vehicle_fixed_cost_month + done_H · 52/12 · km_visit · vehicle_cost_per_km
total     = labor + vehicles + N · maintenance_allowance_robot_month + overhead_month
cost per completed deployment = total / (done_H · 52/12), N/A when done_H = 0
```
Current-team report adds a **supervisor shortfall** = `max(0, ceil(configured_crews / ops_lead_span) −
configured_ops_leads)` when the configured leads are too few for the span. Utilizations: crew = `done_H / C`, vehicle = `done_H / V`, robot = `done_H / R_H`, each N/A when the denominator
is 0. No cost-per-deployment ordering across scenarios is required (P1-5).

## 7. Worked example (5 robots, base, configured team; numbers the golden test will assert)

1. Cycles: `Tr = 30+20+2·40+120+20+30 = 300`; charge 120, W 450: 1·300 ≤ 450, 2·300+120 = 720 > 450 ⇒ `v_robot = 1`.
   `Tc = 300 ⇒ v_crew = 1`; `Tv = 20+80+120+20 = 240 ⇒ v_vehicle = 1`; `Ti = 60 ⇒ v_inspection = 7`.
2. Maintenance: `W/60·D·52/12 = 162.5 h/month`, `m = 6/162.5 = 0.036923`.
3. Week 1: `A_1 = 5·(1−m) = 4.815385`; `R = floor(0.8·4.815385·1·5) = floor(19.26) = 19`;
   `C = floor(0.8·2·1·5) = 8`; `V = 8`; `I = 1·7·5 = 35`; `cap = 8`, binding **crew** (tie with vehicle, crew first);
   demand `4·5 = 20` ⇒ `done = 8`, `unmet = 12`.
4. Reliability: `holds = 8·0.05·1 = 0.4/week`; `jobs_capacity = 1·32/5 = 6.4 ≥ 0.4 ⇒ B = 0` (stable);
   `U = B + (holds − min(holds, B))·f_min = 0 + 0.4·(2/5) = 0.16` unavailable robots next week.
5. Week 2 onward: `A = (5 − 0.16)·(1−m) = 4.661292`; `R = floor(18.645) = 18`; cap stays 8 (crew), steady through
   week 26: **current capacity 8 completed visits/week, binding crew, 12 unmet, recovery stable**.
6. Costs: labor `2·2·6500 + 11000 + 1·9500 = 46,500`; `km_visit = 2·40/60·35 = 46.667`; vehicles
   `2·1300 + 8·52/12·46.667·0.7 = 3,732.44`; maintenance `5,000`; overhead `12,000`; **total 67,232.44 SCU/month**,
   **cost per completed deployment 1,939.40 SCU** (34.67 completed/month).
7. Robot-limited target (all other resources at bound): steady `A = 4.487938`, `R = 17` ⇒ **target 17**/week.
8. Recommended staffing (greedy): crew 3 → vehicle 3 → crew 4 → vehicle 4 → crew 5 → vehicle 5 (now `done = 17`
   = target) → crew 6 (utilization 17/20 = 0.85 > 0.80) ⇒ **6 crews, 5 vehicles, 1 engineer, 1 station, 1 ops lead**:
   `C = 24, V = 20, R = 17` ⇒ binding **robot**, 17 completed/week, 3 unmet (fleet-limited); total **124,406.44
   SCU/month**, **1,688.78 SCU per completed deployment**.
9. Founder reading: at 5 robots the first constraint is field crews, not robots; staffing to the fleet's limit
   roughly doubles completed visits (8 → 17) and lowers cost per deployment (1,939 → 1,689); after that, robots
   are the constraint (demand 20 > 17), so the next lever is robot reliability/turnaround or fleet size.
   Depends on unknown inputs: charge_between_visits_min, maintenance_hours_per_robot_month,
   demand_visits_per_robot_week, maintenance_allowance_robot_month.

## 8. Baseline from the platform (`capacity/baseline.py`, P1-6)

Inside `operations.snapshot.read_snapshot()`, over an explicit window `[start, end)` passed by the caller:

| metric | definition | kind | min sample |
|---|---|---|---|
| inspect/load/unload/on_site planned | median planned reservation length of that purpose, departed visits in the window | **planned**, not observed | n ≥ 10 reservations |
| on_site observed | median session start → stop (Slice 4 session events) | observed | n ≥ 10 sessions |
| travel observed | median departure → `arrived_site` | observed | n ≥ 10 trips |
| incidents_per_visit | runs with ≥ 1 incident / departures in the window | observed rate | n ≥ 30 departures |
| observed_hold_duration_days (informational only) | Kaplan-Meier median of incident-hold duration; still-open holds right-censored at `end`. Reported next to the `hold_min_days` policy assumption, **never used to set it** | observed, censored | ≥ 10 holds with ≥ 5 released |

Source fields (all read-only, inside the snapshot):

| metric | rows and fields |
|---|---|
| planned durations | `planning_reservation` (`purpose`, `starts_at`, `ends_at`, `released_at IS NULL`) joined to the `revision_id` of visits with a `readiness_departureevent` whose `departed_at` is in the window; robot-resource rows for inspect/load/unload/on_site |
| on_site observed | `execution_sessionevent` per run: first `start` → last `stop` (`recorded_at`), runs whose departure is in the window; runs without both events are skipped |
| travel observed | `execution_tripevent` `arrived_site.recorded_at` − `readiness_departureevent.departed_at` (aborted-outbound runs excluded) |
| incidents_per_visit | numerator: distinct `execution_incident.run_id` among runs departed in the window; denominator: `readiness_departureevent` rows in the window |
| observed hold duration | `fleet_hold` with `source_type='incident'`: `placed_at` → `released_at`, censored at `end` when `released_at IS NULL` |

When the rows are unavailable or below the threshold the metric reports `n`, says "no/insufficient platform data",
and the registry fallback is used and named. The report states for each: value, n, window, planned vs observed,
and "synthetic demo measurement, not real-world performance".

## 9. Workbook and tests

**Workbook** (`capacity/workbook.py`, generated into `build/capacity_model.xlsx`, not committed): Inputs (registry
with provenance colours), Engine (one block per case with the weekly recursion over the horizon as rows, live
formulas), Scenarios, Staffing, Costs, Sensitivity, Provenance. Functions limited to a common subset (no MAXIFS, no
dynamic arrays, no LAMBDA). Reconciliation gate (automated): recalculate with LibreOffice headless; formulas
preserved; cached values present; no error cells; every founder-facing output equals Python - integers exactly,
money and rates within relative 1e-9 (absolute 1e-6 near zero). Excel and Google Sheets compatibility are separate
manual checks, recorded as such, not claimed from LibreOffice. The greedy search and sensitivity tables are computed
in Python and written as values (labelled), because a loop is not expressible as plain formulas.

**Sensitivity** (P1-6): each non-axis input perturbed −20% / +20% with feasibility rules (probabilities clamped to
[0, 1], integer inputs rounded and kept ≥ 1 or ≥ 0 as their meaning requires, durations > 0); unknown flags
preserved; ranked by **incremental completed deployments** at the configured team and at the recommended staffing,
then by cost per deployment.

**Tests (closure bar):** registry completeness and exact size (38), incl. `hold_min_days ≤ working_days_week`;
worked example (section 7) exact; more golden cases: robot-bound; vehicle-bound (`Tv` short, crews plentiful);
**overlapping recovery and minimum hold** (a hold still in the backlog during its minimum-hold period is counted
once; a hold recovered within the week is still out for `f_min`); **multi-robot visit** (`r = 2`: robot and
inspection capacity halve, crew and vehicle do not; vehicle capacity 1 ⇒ INFEASIBLE); recovery-unstable
(engineers 0) where the staffing search adds engineers first; supervisor shortfall; INFEASIBLE cycle;
runtime-limited; conservation (`done ≤` every capacity and `≤ demand`; busy minutes ≤
available minutes per resource); zero denominators ⇒ N/A; monotonicity where guaranteed (more robots never lower
`R`; more crews/vehicles/stations/engineers never lower `done_H`; higher incident rate, longer hold or recovery,
longer travel never raise `done_H`); scenario ordering of current `done_H` (cons ≤ base ≤ opt) only with demand held at
the base value and inputs with `capacity_direction` checked to be consistently ordered first; greedy search
termination and `UNATTAINABLE`; baseline extraction with known synthetic data incl. censoring and the fallback path,
inside the snapshot with no writes; reconciliation; `/ops/capacity/` permissions (operations.metrics), "forecast, not
an operating plan" and synthetic labels, no forbidden wording; full suite and CI (LibreOffice installed in the CI job).

## 10. Rev 2 correction matrix (EM review of `6d96302`)

| item | change |
|---|---|
| P0-1 capacity equations | per-resource cycles; crew does not supervise charging; charging blocks the robot only between same-day visits; overnight charging stated; one site per vehicle trip, multi-robot only when the visit needs it, no batching (3) |
| P0-2 reliability | incidents counted once per visit; incident→hold factor explicit; minimum hold latency and engineer recovery queue modelled separately; maintenance applied only to non-held robots (no double count); weekly discrete-time backlog with end-of-horizon reporting (4) |
| P0-3 circularity and staffing | lagged weekly simulation; current capacity uses the configured team; deterministic bounded greedy search with `UNATTAINABLE` (4, 5) |
| P1-4 registry | exact 38 inputs with values, units, basis; configured team; speed for km; scheduling efficiency; inspection stations; runtime limit expressed as unconstrained for this calculation (2) |
| P1-5 ordering | `capacity_direction` per input; ordering tested only for current completed visits with demand fixed; no cost ordering (2, 9) |
| P1-6 sensitivity and baseline | feasible perturbations; ranked by incremental completed deployments; planned vs observed; explicit windows; per-metric samples; right-censored holds (8, 9) |
| P1-7 workbook | formulas preserved, cached values, no errors, relative tolerance, integers exact; Excel/Sheets as separate manual checks (9) |
| worked example | section 7, from inputs to founder recommendation, computed and asserted by the golden test |
| Rev 3 P0-1 double count | single unavailability `U_t` with mutually exclusive backlog / recovered-this-week states; early recovery does not shorten the minimum hold; golden case (4, 9) |
| Rev 3 P0-2 multi-robot | robot and inspection capacity divided by `robots_per_visit`; inspection per robot; vehicle stays a feasibility constraint; golden case (3, 4, 9) |
| Rev 3 P1-3 staffing | ordered rules with recovery first; the target < demand + unstable case specified (5) |
| Rev 3 P1-4 ops leads | configured leads for current cost, span-based for recommended, supervisor shortfall reported (6) |
| Rev 4 recovery cohorts | explicit older/new cohort accounting with FIFO, minimum hold applied only to new holds recovered in their own week, approximation stated, four golden cohort cases (4) |
| Rev 3 P1-5 baseline | exact source fields per metric; unavailable-data behaviour; `hold_min_days` is a policy assumption, observed hold duration informational only (2, 8) |

## 11. Ownership (unchanged)

Lead: app shell, registry, docs, CI LibreOffice job, integration. A: `model.py` (+ greedy search) and its tests.
B: `baseline.py` and tests. C: `workbook.py` and reconciliation tests. D: `report.py`, management command,
`/ops/capacity/` page and tests. Sequence: lead registry → A → B, C, D in parallel → merge, CI, report, EM review.
One full suite at a time on the shared host.
