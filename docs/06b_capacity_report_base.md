# Capacity forecast: 5 / 10 / 25 / 50 robots (base assumptions)

**SYNTHETIC DEMO - a forecast from stated assumptions, not a company operating plan.**

Baseline: platform baseline applied (synthetic demo measurement, not real-world performance); 0 input(s) measured, the rest use registry fallbacks. Window: 2026-10-22T03:26:02.444948+00:00 to 2026-11-19T03:26:02.444948+00:00 (start inclusive, end exclusive).

## Founder takeaways

- 5 robots: the current team (2 crews, 2 vehicles, 1 engineer, 1 station, 1 ops lead) completes 8 of 20 demanded visits a week, limited by field crews. Recommended staffing (6 crews, 5 vehicles, 1 engineer, 1 station, 1 ops lead) completes 17 (MET), limited by robots (fleet capacity); cost per completed deployment 1,939.40 -> 1,688.78 SCU; monthly total 67,232.44 -> 124,406.44 SCU.
- 10 robots: the current team (2 crews, 2 vehicles, 1 engineer, 1 station, 1 ops lead) completes 8 of 40 demanded visits a week, limited by field crews. Recommended staffing (11 crews, 9 vehicles, 1 engineer, 1 station, 2 ops leads) completes 35 (MET), limited by robots (fleet capacity); cost per completed deployment 2,083.63 -> 1,395.52 SCU; monthly total 72,232.44 -> 211,654.44 SCU.
- 25 robots: the current team (2 crews, 2 vehicles, 1 engineer, 1 station, 1 ops lead) completes 8 of 100 demanded visits a week, limited by field crews. Recommended staffing (28 crews, 23 vehicles, 1 engineer, 3 stations, 4 ops leads) completes 89 (MET), limited by robots (fleet capacity); cost per completed deployment 2,516.32 -> 1,277.01 SCU; monthly total 87,232.44 -> 492,498.44 SCU.
- 50 robots: the current team (2 crews, 2 vehicles, 1 engineer, 1 station, 1 ops lead) completes 8 of 200 demanded visits a week, limited by field crews. Recommended staffing (56 crews, 45 vehicles, 2 engineers, 6 stations, 7 ops leads) completes 178 (MET), limited by robots (fleet capacity); cost per completed deployment 3,237.47 -> 1,247.45 SCU; monthly total 112,232.44 -> 962,196.89 SCU.
- Hiring and vehicle triggers: field crews from 5 robots (2 -> 6); vehicles from 5 robots (2 -> 5); ops leads from 10 robots (1 -> 2); inspection stations from 25 robots (1 -> 3); recovery engineers from 50 robots (1 -> 2).
- After recommended staffing at 5 robots the limit is robots (fleet capacity); if robot capacity were not the limit, customer demand (every demanded visit completed) would limit at 20/week.
- Even with recommended staffing some demand is not completed: 3 visits/week at 5 robots, 5 visits/week at 10 robots, 11 visits/week at 25 robots, 22 visits/week at 50 robots.
- Biggest single lever at 5 robots with the recommended team: Working days +20% (5 -> 6 days) adds 3 completed deployments a week (17 -> 20).
- Biggest single lever at 5 robots with the current team: Working days +20% (5 -> 6 days) adds 1 completed deployments a week (8 -> 9).
- These results depend on 4 unknown company inputs (illustrative placeholders): charge_between_visits_min, maintenance_hours_per_robot_month, demand_visits_per_robot_week, maintenance_allowance_robot_month.
- This is a forecast from stated assumptions, not a company operating plan; the staffing search is a heuristic, not a global optimum.

## Current team vs recommended staffing

| Robots | Staffing | Crews | Vehicles | Engineers | Stations | Ops leads | Completed/wk | Demand/wk | Unmet/wk | Binding | Flags | Monthly total (SCU) | Cost per completed (SCU) | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5 | current | 2 | 2 | 1 | 1 | 1 | 8 | 20 | 12 | crew | - | 67,232.44 | 1,939.40 | CURRENT TEAM |
| 5 | recommended | 6 | 5 | 1 | 1 | 1 | 17 | 20 | 3 | robot | - | 124,406.44 | 1,688.78 | MET |
| 10 | current | 2 | 2 | 1 | 1 | 1 | 8 | 40 | 32 | crew | - | 72,232.44 | 2,083.63 | CURRENT TEAM |
| 10 | recommended | 11 | 9 | 1 | 1 | 2 | 35 | 40 | 5 | robot | - | 211,654.44 | 1,395.52 | MET |
| 25 | current | 2 | 2 | 1 | 1 | 1 | 8 | 100 | 92 | crew | - | 87,232.44 | 2,516.32 | CURRENT TEAM |
| 25 | recommended | 28 | 23 | 1 | 3 | 4 | 89 | 100 | 11 | robot | - | 492,498.44 | 1,277.01 | MET |
| 50 | current | 2 | 2 | 1 | 1 | 1 | 8 | 200 | 192 | crew | - | 112,232.44 | 3,237.47 | CURRENT TEAM |
| 50 | recommended | 56 | 45 | 2 | 6 | 7 | 178 | 200 | 22 | robot | - | 962,196.89 | 1,247.45 | MET |

Status reasons:

- 5 robots: current - configured team as entered; recommended - done 17 >= goal 17, crew utilization within trigger
- 10 robots: current - configured team as entered; recommended - done 35 >= goal 35, crew utilization within trigger
- 25 robots: current - configured team as entered; recommended - done 89 >= goal 89, crew utilization within trigger
- 50 robots: current - configured team as entered; recommended - done 178 >= goal 178, crew utilization within trigger

## Hiring and vehicle triggers

The fleet size at which each recommended count first rises (the first step is compared with the current team).

| Resource | Current team | At 5 | At 10 | At 25 | At 50 | First rises at |
|---|---|---|---|---|---|---|
| field crews | 2 | 6 | 11 | 28 | 56 | 5 robots |
| vehicles | 2 | 5 | 9 | 23 | 45 | 5 robots |
| recovery engineers | 1 | 1 | 1 | 1 | 2 | 50 robots |
| inspection stations | 1 | 1 | 1 | 3 | 6 | 25 robots |
| ops leads | 1 | 1 | 2 | 4 | 7 | 10 robots |

## Next constraint after relieving the current one

| Robots | Staffing | Constraint now | Relieved by | Next constraint | Completed now | Completed after |
|---|---|---|---|---|---|---|
| 5 | current | field crews | more field crews (up to the search bound of 20) | vehicles | 8 | 8 |
| 5 | recommended | robots (fleet capacity) | more robot capacity (reliability, turnaround or fleet size) | customer demand (every demanded visit completed) | 17 | 20 |
| 10 | current | field crews | more field crews (up to the search bound of 40) | vehicles | 8 | 8 |
| 10 | recommended | robots (fleet capacity) | more robot capacity (reliability, turnaround or fleet size) | inspection stations | 35 | 35 |
| 25 | current | field crews | more field crews (up to the search bound of 100) | vehicles | 8 | 8 |
| 25 | recommended | robots (fleet capacity) | more robot capacity (reliability, turnaround or fleet size) | vehicles | 89 | 92 |
| 50 | current | field crews | more field crews (up to the search bound of 200) | vehicles | 8 | 8 |
| 50 | recommended | robots (fleet capacity) | more robot capacity (reliability, turnaround or fleet size) | vehicles | 178 | 180 |

## Top 5 levers by incremental completed deployments

Each input moved -20% / +20% (feasible values only); only changes that add completed deployments are listed. Not offered as levers: demand_visits_per_robot_week (customer demand is a market input, not an operational improvement); horizon_weeks (the simulation horizon is a modelling choice); holds_per_incident (a platform rule (Slice 4: every incident holds the robot), not a choice).

| Robots | Staffing | # | Input | Change | From | To | Completed/wk | Added | Cost per completed (SCU) | Unknown input |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | current | 1 | Working days (`working_days_week`) | +20% | 5 days | 6 days | 8 -> 9 | +1 | 1,727.54 |  |
| 5 | current | 2 | Bookable share of slots within customer time windows (`scheduling_efficiency`) | +20% | 0.8 fraction | 0.96 fraction | 8 -> 9 | +1 | 1,727.54 |  |
| 5 | recommended | 1 | Working days (`working_days_week`) | +20% | 5 days | 6 days | 17 -> 20 | +3 | 1,440.36 |  |
| 5 | recommended | 2 | Bookable share of slots within customer time windows (`scheduling_efficiency`) | +20% | 0.8 fraction | 0.96 fraction | 17 -> 20 | +3 | 1,440.36 |  |
| 5 | recommended | 3 | Visits with at least one incident (`incidents_per_visit`) | -20% | 0.05 probability | 0.04 probability | 17 -> 18 | +1 | 1,596.77 |  |
| 5 | recommended | 4 | Policy minimum hold (`hold_min_days`) | -20% | 2 working days | 1.6 working days | 17 -> 18 | +1 | 1,596.77 |  |
| 5 | recommended | 5 | Planned maintenance (`maintenance_hours_per_robot_month`) | -20% | 6 hours | 4.8 hours | 17 -> 18 | +1 | 1,596.77 | UNKNOWN |
| 10 | current | 1 | Working days (`working_days_week`) | +20% | 5 days | 6 days | 8 -> 9 | +1 | 1,855.74 |  |
| 10 | current | 2 | Bookable share of slots within customer time windows (`scheduling_efficiency`) | +20% | 0.8 fraction | 0.96 fraction | 8 -> 9 | +1 | 1,855.74 |  |
| 10 | recommended | 1 | Working days (`working_days_week`) | +20% | 5 days | 6 days | 35 -> 40 | +5 | 1,225.17 |  |
| 25 | current | 1 | Working days (`working_days_week`) | +20% | 5 days | 6 days | 8 -> 9 | +1 | 2,240.36 |  |
| 25 | current | 2 | Bookable share of slots within customer time windows (`scheduling_efficiency`) | +20% | 0.8 fraction | 0.96 fraction | 8 -> 9 | +1 | 2,240.36 |  |
| 25 | recommended | 1 | Working days (`working_days_week`) | +20% | 5 days | 6 days | 89 -> 100 | +11 | 1,140.13 |  |
| 25 | recommended | 2 | Bookable share of slots within customer time windows (`scheduling_efficiency`) | +20% | 0.8 fraction | 0.96 fraction | 89 -> 100 | +11 | 1,140.13 |  |
| 25 | recommended | 3 | Visits with at least one incident (`incidents_per_visit`) | -20% | 0.05 probability | 0.04 probability | 89 -> 90 | +1 | 1,263.18 |  |
| 25 | recommended | 4 | Policy minimum hold (`hold_min_days`) | -20% | 2 working days | 1.6 working days | 89 -> 90 | +1 | 1,263.18 |  |
| 25 | recommended | 5 | Planned maintenance (`maintenance_hours_per_robot_month`) | -20% | 6 hours | 4.8 hours | 89 -> 90 | +1 | 1,263.18 | UNKNOWN |
| 50 | current | 1 | Working days (`working_days_week`) | +20% | 5 days | 6 days | 8 -> 9 | +1 | 2,881.38 |  |
| 50 | current | 2 | Bookable share of slots within customer time windows (`scheduling_efficiency`) | +20% | 0.8 fraction | 0.96 fraction | 8 -> 9 | +1 | 2,881.38 |  |
| 50 | recommended | 1 | Working days (`working_days_week`) | +20% | 5 days | 6 days | 178 -> 200 | +22 | 1,113.82 |  |
| 50 | recommended | 2 | Bookable share of slots within customer time windows (`scheduling_efficiency`) | +20% | 0.8 fraction | 0.96 fraction | 178 -> 200 | +22 | 1,113.82 |  |
| 50 | recommended | 3 | Visits with at least one incident (`incidents_per_visit`) | -20% | 0.05 probability | 0.04 probability | 178 -> 180 | +2 | 1,233.95 |  |
| 50 | recommended | 4 | Policy minimum hold (`hold_min_days`) | -20% | 2 working days | 1.6 working days | 178 -> 180 | +2 | 1,233.95 |  |
| 50 | recommended | 5 | Planned maintenance (`maintenance_hours_per_robot_month`) | -20% | 6 hours | 4.8 hours | 178 -> 180 | +2 | 1,233.95 | UNKNOWN |

## Worked example: 5 robots

| Step | Item | Value | How |
|---|---|---|---|
| 1. Cycles | Robot busy per visit Tr (min) | 300 | 30+20+2*40+120+20+30 |
| 1. Cycles | Robot visits per day | 1 | largest k with k*Tr+(k-1)*charge <= W |
| 1. Cycles | Crew visits per day | 1 | floor(W / Tc) |
| 1. Cycles | Vehicle busy per visit Tv (min) | 240 | load + 2*travel + on_site + unload |
| 1. Cycles | Vehicle visits per day | 1 | floor(W / Tv) |
| 1. Cycles | Inspection per robot Ti (min) | 60 | inspect + post_inspect |
| 1. Cycles | Robot-inspections per station per day | 7 | floor(W / Ti) |
| 2. Maintenance | Working hours per robot-month | 162.5 | W/60 * D * 52/12 |
| 2. Maintenance | Maintenance fraction m | 0.036923 | maintenance hours / working hours |
| 3. Week 1 (current team) | Available robots A_1 | 4.815385 | N * (1 - m) |
| 3. Week 1 (current team) | R (robot capacity, visits/week) | 19 |  |
| 3. Week 1 (current team) | C (crew capacity, visits/week) | 8 |  |
| 3. Week 1 (current team) | V (vehicle capacity, visits/week) | 8 |  |
| 3. Week 1 (current team) | I (inspection capacity, visits/week) | 35 |  |
| 3. Week 1 (current team) | Binding constraint | crew | lowest capacity; ties robot, crew, vehicle, inspection |
| 3. Week 1 (current team) | Demand (visits/week) | 20 | demand per robot-week * N |
| 3. Week 1 (current team) | Completed | 8 | min(capacity, demand) |
| 3. Week 1 (current team) | Unmet | 12 |  |
| 4. Reliability | Holds per week | 0.4 | completed * incidents per visit * holds per incident |
| 4. Reliability | Recovery jobs per week | 6.4 | engineers * hours per week / hours per hold |
| 4. Reliability | Recovery backlog after week 1 | 0 |  |
| 4. Reliability | Unavailable robots next week U_1 | 0.16 | B + rec_new * f_min |
| 5. Week 26 (reported, current team) | Available robots | 4.661292 | (N - U) * (1 - m) |
| 5. Week 26 (reported, current team) | R (robot capacity, visits/week) | 18 |  |
| 5. Week 26 (reported, current team) | Completed per week | 8 |  |
| 5. Week 26 (reported, current team) | Binding constraint | crew |  |
| 5. Week 26 (reported, current team) | Unmet per week | 12 |  |
| 5. Week 26 (reported, current team) | Flags | none |  |
| 6. Monthly cost (current team, SCU) | Labor | 46,500.00 | crews*crew size*operator + engineers*engineer + leads*lead |
| 6. Monthly cost (current team, SCU) | Km per visit | 46.667 | 2 * travel/60 * speed |
| 6. Monthly cost (current team, SCU) | Vehicles | 3,732.44 | vehicles*fixed + completed*52/12*km*cost per km |
| 6. Monthly cost (current team, SCU) | Maintenance allowance | 5,000.00 | N * allowance |
| 6. Monthly cost (current team, SCU) | Overhead | 12,000.00 |  |
| 6. Monthly cost (current team, SCU) | Total per month | 67,232.44 |  |
| 6. Monthly cost (current team, SCU) | Completed per month | 34.67 | completed per week * 52/12 |
| 6. Monthly cost (current team, SCU) | Cost per completed deployment | 1,939.40 | total / completed |
| 7. Robot-limited target | Target (completed/week) | 17 | every other resource at the search bound |
| 8. Recommended staffing (greedy) | Step 1 | rule 3: done 8 < goal 17, binding crew -> crews 3 |  |
| 8. Recommended staffing (greedy) | Step 2 | rule 3: done 8 < goal 17, binding vehicle -> vehicles 3 |  |
| 8. Recommended staffing (greedy) | Step 3 | rule 3: done 12 < goal 17, binding crew -> crews 4 |  |
| 8. Recommended staffing (greedy) | Step 4 | rule 3: done 12 < goal 17, binding vehicle -> vehicles 4 |  |
| 8. Recommended staffing (greedy) | Step 5 | rule 3: done 16 < goal 17, binding crew -> crews 5 |  |
| 8. Recommended staffing (greedy) | Step 6 | rule 3: done 16 < goal 17, binding vehicle -> vehicles 5 |  |
| 8. Recommended staffing (greedy) | Step 7 | rule 4: crew utilization 0.8500 > 0.8 -> crews 6 |  |
| 8. Recommended staffing (greedy) | Recommended team | 6 crews, 5 vehicles, 1 engineer, 1 station, 1 ops lead |  |
| 8. Recommended staffing (greedy) | Status | MET: done 17 >= goal 17, crew utilization within trigger |  |
| 8. Recommended staffing (greedy) | R (robot capacity, visits/week) | 17 |  |
| 8. Recommended staffing (greedy) | C (crew capacity, visits/week) | 24 |  |
| 8. Recommended staffing (greedy) | V (vehicle capacity, visits/week) | 20 |  |
| 8. Recommended staffing (greedy) | I (inspection capacity, visits/week) | 35 |  |
| 8. Recommended staffing (greedy) | Completed per week | 17 |  |
| 8. Recommended staffing (greedy) | Binding constraint | robot |  |
| 8. Recommended staffing (greedy) | Unmet per week | 3 |  |
| 8. Recommended staffing (greedy) | Total per month (SCU) | 124,406.44 |  |
| 8. Recommended staffing (greedy) | Cost per completed deployment (SCU) | 1,688.78 |  |

## Unknown company inputs these results depend on

Illustrative placeholders, not company data. Replace them with real figures before relying on any number above.

| Input | Unit | Value used | Conservative / base / optimistic | Source |
|---|---|---|---|---|
| **UNKNOWN** Robot charge gap between same-day visits (`charge_between_visits_min`) | min between visits | 120 | 180 / 120 / 60 | robot specification (B-04); placeholder |
| **UNKNOWN** Planned maintenance (`maintenance_hours_per_robot_month`) | hours per robot-month | 6 | 8 / 6 / 4 | maintenance intervals (B-08); placeholder |
| **UNKNOWN** Customer demand (`demand_visits_per_robot_week`) | visits per robot-week | 4 | 3 / 4 / 5 | customer demand; placeholder |
| **UNKNOWN** Maintenance allowance (`maintenance_allowance_robot_month`) | SCU per robot-month | 1,000 | 1,500 / 1,000 / 700 | placeholder |

## Input provenance

| Input | Unit | Provenance | Value used | Value from | Source |
|---|---|---|---|---|---|
| Pre-departure inspection (`inspect_min`) | min per visit | platform_measured | 30 | platform baseline: value 30; n 1; kind planned | planned inspection reservation; fallback |
| Loading (`load_min`) | min per visit | platform_measured | 20 | platform baseline: value 20; n 1; kind planned | planned loading reservation; fallback |
| Travel one way (`travel_one_way_min`) | min per visit leg | platform_measured | 40 | platform baseline: value 40; n 1; kind observed | observed departure -> arrived_site; fallback |
| Customer session on site (`on_site_min`) | min per visit | platform_measured | 120 | platform baseline: value 120; n 1; kind planned | planned on_site reservation / observed session; fallback |
| Unloading (`unload_min`) | min per visit | platform_measured | 20 | platform baseline: value 20; n 1; kind planned | planned unloading reservation; fallback |
| Post-visit inspection (`post_inspect_min`) | min per visit | synthetic_assumption | 30 | registry (base) | Slice 4 requires a post-visit inspection; duration assumed |
| Robot charge gap between same-day visits (`charge_between_visits_min`) | min between visits | **UNKNOWN company data** | 120 | registry (base) | robot specification (B-04); placeholder |
| Robot runtime limit on site (`runtime_limit_min`) | min per charge | **UNKNOWN company data** | unconstrained | registry (base) | robot specification (B-04) (unconstrained in this illustrative calculation; not a claim about robot capability) |
| Field working day (`working_day_min`) | min per working day | synthetic_assumption | 450 | registry (base) | 09:00-16:30 field window (ops tracker settings) |
| Working days (`working_days_week`) | days per week | synthetic_assumption | 5 | registry (base) | planning assumption |
| Bookable share of slots within customer time windows (`scheduling_efficiency`) | fraction per week | synthetic_assumption | 0.8 | registry (base) | planning assumption |
| Robots per customer visit (`robots_per_visit`) | robots per visit | platform_measured | 1 | platform baseline: value 1; kind platform_rule | Slice 2: one robot per visit |
| Robots one vehicle can carry (`vehicle_robot_capacity`) | robots per vehicle | platform_measured | 1 | platform baseline: value 1.0; n 1; kind observed | planning_vehicle.robot_capacity |
| Crew per visit (`crew_size`) | people per visit | platform_measured | 2 | platform baseline: value 2; kind platform_rule | Slice 2: lead + second |
| Visits with at least one incident (`incidents_per_visit`) | probability per completed visit | platform_measured | 0.05 | platform baseline: value 0.05; n 1; kind observed | runs with >= 1 incident / departures; fallback |
| Holds per incident (`holds_per_incident`) | holds per incident | platform_measured | 1 | platform baseline: value 1; kind platform_rule | Slice 4 rule: every incident holds the robot |
| Policy minimum hold (`hold_min_days`) | working days per hold | synthetic_assumption | 2 | registry (base) | policy minimum (approvals, re-inspection); not calibrated from observed holds |
| Recovery work per hold (`recovery_hours_per_hold`) | engineer hours per hold | synthetic_assumption | 5 | registry (base) | planning assumption |
| Recovery hours per engineer (`engineer_hours_per_week`) | hours per engineer-week | synthetic_assumption | 32 | registry (base) | planning assumption |
| Planned maintenance (`maintenance_hours_per_robot_month`) | hours per robot-month | **UNKNOWN company data** | 6 | registry (base) | maintenance intervals (B-08); placeholder |
| Customer demand (`demand_visits_per_robot_week`) | visits per robot-week | **UNKNOWN company data** | 4 | registry (base) | customer demand; placeholder (excluded from the scenario-ordering test) |
| Current crews (`configured_crews`) | crews team | synthetic_assumption | 2 | registry (base) | 5-robot starting team |
| Current vehicles (`configured_vehicles`) | vehicles team | synthetic_assumption | 2 | registry (base) | 5-robot starting team |
| Current recovery engineers (`configured_engineers`) | engineers team | synthetic_assumption | 1 | registry (base) | 5-robot starting team |
| Current inspection stations (`configured_inspection_stations`) | stations team | synthetic_assumption | 1 | registry (base) | 5-robot starting team |
| Current ops leads (`configured_ops_leads`) | leads team | synthetic_assumption | 1 | registry (base) | 5-robot starting team |
| Max planned crew utilization (`crew_hire_trigger`) | fraction per week | synthetic_assumption | 0.8 | registry (base) | staffing policy |
| Crews per ops lead (`ops_lead_span`) | crews per lead | synthetic_assumption | 8 | registry (base) | staffing policy |
| Search bound per resource (`staffing_bound_per_robot`) | units per robot | synthetic_assumption | 4 | registry (base) | keeps the staffing search finite |
| Field operator cost (`field_operator_cost_month`) | SCU per operator-month | synthetic_assumption | 6,500 | registry (base) | synthetic placeholder currency |
| Engineer cost (`engineer_cost_month`) | SCU per engineer-month | synthetic_assumption | 11,000 | registry (base) | synthetic placeholder currency |
| Ops lead cost (`ops_lead_cost_month`) | SCU per lead-month | synthetic_assumption | 9,500 | registry (base) | synthetic placeholder currency |
| Vehicle fixed cost (`vehicle_fixed_cost_month`) | SCU per vehicle-month | synthetic_assumption | 1,300 | registry (base) | synthetic placeholder currency |
| Vehicle running cost (`vehicle_cost_per_km`) | SCU per km | synthetic_assumption | 0.7 | registry (base) | synthetic placeholder currency |
| Average door-to-door speed (`avg_speed_kmh`) | km/h per trip | synthetic_assumption | 35 | registry (base) | converts travel minutes to km |
| Maintenance allowance (`maintenance_allowance_robot_month`) | SCU per robot-month | **UNKNOWN company data** | 1,000 | registry (base) | placeholder |
| Overhead (`overhead_month`) | SCU per month | synthetic_assumption | 12,000 | registry (base) | synthetic placeholder currency |
| Simulation horizon (`horizon_weeks`) | weeks per run | synthetic_assumption | 26 | registry (base) | planning assumption |

## Notes and approximations

- Weekly expected-value simulation; the reported result is the last week of the horizon.
- Each resource does whole visits per working day; no partial visits and no multi-site vehicle trips.
- New holds count as placed at the start of their week (an upper bound on the remaining minimum hold).
- Recommended staffing is a deterministic greedy heuristic from the current team, not a global optimum.
- Unknown company inputs are illustrative placeholders; every result that uses them says so.
- Money is in synthetic currency units (SCU); weeks per month = 52/12.
