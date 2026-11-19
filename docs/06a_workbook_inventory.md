# Workbook inventory: humanoid-fleet-ops (read-only)

Source: `/Users/yashshah2311/humanoid-fleet-ops` (not modified). Inspected with openpyxl (formulas + `data_only=True`), plus `build/`, `tests/`, `source/`.
Note: the generator scripts live in `build/` (not `source/`; `source/` holds the Markdown for the docs). The folder is not its own git repo.

## 0. Headline facts

- Both workbooks are **written by Python (openpyxl)**: `build/build_model.py` -> `05_Capacity_Cost_Model.xlsx`, `build/build_tracker.py` + `build/data.py` -> `04_Ops_Tracker.xlsx`.
- **Shipped files have zero cached values** (every formula cell is `None` under `data_only=True`). They were never opened/recalculated in Excel/Sheets.
- Computed values exist only in **LibreOffice-recalculated copies** in `tests/out/calc/` (`model.xlsx`, `base.xlsx`, step files). Scans of `calc/model.xlsx` and `calc/base.xlsx` found **no error values** (#REF!/#DIV/0!/#NAME?/Err:5xx), i.e. no circular refs surfaced in LibreOffice. Not verified in Excel or Google Sheets.
- No external links in either workbook. No volatile functions (tracker uses a fixed `AsOf` setting, not `NOW()`).
- Everything is labelled synthetic/illustrative. No real robot model, spec, or company fact found.

---

## 1. 05_Capacity_Cost_Model.xlsx

### 1.1 Sheets

| Sheet | Dims | Formulas | Role | Purpose |
|---|---|---|---|---|
| README | A1:B9 | 0 | doc | How to use, "Not in the model" list, placeholders note |
| Inputs | A1:H26 | 0 | **inputs** | 22 parameters x 4 scenario columns (C:F) + note col G; scenario selector C26 (data validation list) |
| Scenarios | A1:H22 | 60 | output | 15 outputs x 4 scenarios, all `=Engine!..` |
| Staffing | A1:I9 | 33 | output | 4 team/vehicle options for the selected scenario |
| Sensitivity | A1:H11 | 43 | output | cost per completed visit: drive (15..90 min) x completion (50..90%) + capacity/constraint per drive row; colour scale |
| Demand ramp | A1:I11 | 49 | output | demand 5..30/wk for 1T1V and 2T2V: booked, unmet, constraint, cost/completed |
| Engine | A1:AY52 | 2346 | **calculation** | 50 case columns (B:AY): 4 scenarios + 4 staffing + 30 sensitivity + 12 ramp. Rows 3-24 inputs (INDEX from Inputs or typed override), rows 26-52 calc |

Defined names: `Selected = Inputs!$C$26` (default "Higher-demand pilot"), `SelIdx = MATCH(Inputs!$C$26,Inputs!$C$2:$F$2,0)`.
Engine formula count check: 996 `INDEX(Inputs!...)` input cells (1100 minus 104 typed overrides) + 1350 calc cells (27 rows x 50).

### 1.2 Inputs (Inputs!C3:F24; Custom col F = copy of Expected)

Banner A1: "ILLUSTRATIVE ASSUMPTIONS [A] · not company data". Per-row tags only where shown in Note.

| Row | Input | Unit | Cons. | Expected | Higher-demand | Time basis | Tag / note |
|---|---|---|---|---|---|---|---|
| 3 | Robots in fleet | # | 5 | 5 | 5 | static | none (banner only) |
| 4 | Robot availability (not on hold/repair) | % | 60% | 75% | 75% | fraction of fleet | "use measured value after 4 weeks" |
| 5 | Charge/turnaround between visits per robot | h | 3 | 3 | 3 | per visit | **[ENG] placeholder** |
| 6 | Scheduled maintenance per robot | h | 4 | 4 | 4 | per week | **[ENG] placeholder** |
| 7 | Field teams | # | 1 | 1 | 1 | static | - |
| 8 | Operators per team | # | 2 | 2 | 2 | static | "Two-person rule" |
| 9 | Vehicles | # | 1 | 1 | 1 | static | - |
| 10 | Operating days | #/wk | 4 | 5 | 5 | per week | - |
| 11 | Shift length | h | 9 | 9 | 9 | per day | - |
| 12 | Hub time (checks + close-out) | min | 90 | 90 | 90 | per team-day | - |
| 13 | Drive hub->home one way | min | 45 | 30 | 30 | per visit | no chaining |
| 14 | Setup on site incl. G4 | min | 30 | 30 | 30 | per visit | - |
| 15 | Robot task time on site | min | 90 | 60 | 60 | per visit | - |
| 16 | Pack + notes | min | 45 | 45 | 45 | per visit | - |
| 17 | Team buffer between visits | min | 15 | 15 | 15 | per visit | - |
| 18 | Visit completion rate | % | 70% | 85% | 85% | completed/booked | - |
| 19 | Labor per operator hour, fully loaded | $ | 85 | 85 | 85 | per hour | [A] |
| 20 | Vehicle cost per vehicle | $ | 120 | 120 | 120 | per day | [A] |
| 21 | Driving cost per hour driven | $ | 30 | 30 | 30 | per hour | [A] |
| 22 | Consumables per visit | $ | 25 | 25 | 25 | per visit | [A] |
| 23 | Robot cost per robot (optional) | $ | 0 | 0 | 0 | per week | "hardware cost unknown", excluded |
| 24 | Customer demand (visit requests) | # | 8 | 10 | 18 | per week | - |

Real-spec flag review: nothing is a named real robot or vendor figure. Robot-behaviour values (availability, charge 3 h, maintenance 4 h/wk, task 60-90 min, 1 visit per robot-day implied) must be treated as **unknown/synthetic**; only charge and maintenance carry an explicit [ENG] tag, the rest rely on the banner. "Bay Area hub" (README) is fictional framing.

Consistency with tracker Settings: field window 9*60-90 = 450 min = 09:00-16:30 (EarliestDep/LatestRet) ✓; pack+notes 45 = DefPack 30 + DefNotes 15 ✓; charge 3 h = TurnH ✓; buffer 15 ✓; 5 robots ✓. Tracker eligible-home drive times 25-40 min (mean ~32) vs model 30/45.

### 1.3 Calculation chain (Engine rows 26-52, per case column)

```
teams_eff = MIN(teams, vehicles)
window    = shift_h*60 - hub_min
block     = 2*drive + setup + task + pack                     (hub->home->hub)
team_n    = FLOOR((window+buffer)/(block+buffer))             visits/team-day
robots_usable = FLOOR(robots*availability)
robot_n   = FLOOR((window+charge*60)/(block+charge*60))        visits/robot-day
team_cap  = teams_eff*team_n*days
robot_cap = FLOOR(robots_usable*(robot_n*days - maint_h*60/(block+charge*60)))
cap       = MIN(team_cap, robot_cap);  math_cap = same with averages (no FLOOR)
booked    = MIN(demand, cap); completed = booked*completion; unmet = MAX(0,demand-cap)
op_util   = booked*block/(teams*days*window)       robot_util = booked*block/(robots*days*shift_h*60)
drive_share = 2*drive/block
labor = teams*ops*days*shift_h*rate; vehicle = vehicles*days*vehicle_day
driving = booked*2*drive/60*drive_cost_h; cons = booked*consumables; robotcost = robots*robot_week
total = sum; cpv = total/completed; lpv = labor/completed; bottleneck = nested IF text
```

- Cross-sheet refs: Engine -> Inputs (996, via INDEX on scenario index or `SelIdx`); Scenarios/Staffing/Sensitivity/Demand ramp -> Engine (182) and `=Selected`. No other directions; no cycles.
- Hard-coded numbers in formulas: 60 (min/h), 2 (round trip), 0/1 (MAX floor, FLOOR significance). Also **typed constants in Engine override cells** (yellow): staffing teams/vehicles, sensitivity drive/completion, ramp teams/vehicles/demand. The row/column labels on Sensitivity/Demand ramp/Staffing are separate typed constants, **not linked** to those overrides (editing a label changes nothing).
- Errors/guards: `IFERROR` on utilization and cpv (returns 0 or text "no visits", so cpv can be a string). No #REF!/#NAME?. Empty-cell refs: none (Inputs rows 3-24 all populated).
- Cached values: none in shipped file. LibreOffice values (tests/out/calc/model.xlsx):

| Output | Conservative | Expected | Higher-demand |
|---|---|---|---|
| Capacity/wk schedulable (math est.) | 4 (7.1) | 10 (11.5) | 10 (11.5) |
| Team cap / robot cap | 4 / 10 | 10 / 13 | 10 / 13 |
| Booked / completed / unmet | 4 / 2.8 / 4 | 10 / 8.5 / 0 | 10 / 8.5 / 8 |
| Constraint | Field teams | Field teams | Field teams |
| Op util / robot util | 57% / 9% | 87% / 14% | 87% / 14% |
| Total $/wk / cost per completed | 6,880 / 2,457 | 8,800 / 1,035 | 8,800 / 1,035 |

Staffing (Higher-demand): 2T1V = cap 10, "Vehicles", $1,935/visit; 2T2V = cap 13, "Robots available", $1,558; 3T3V = cap 13, $2,305.

### 1.4 Coverage vs the requested scaling model

| Requested item | Today | Gap |
|---|---|---|
| Field staffing | Yes: teams x 2 operators, staffing grid (1-3 teams) | No supervisors/ops lead/engineering/on-call; no hiring lead time |
| Deployments per day | Partly: visits/team-day and visits/robot-day (whole visits); outputs are per week | No explicit per-day output |
| Transport capacity | Partly: vehicles cap teams (`MIN(teams,vehicles)`), 1 robot per trip, no chaining | No robots-per-vehicle, loading time, vehicle downtime |
| Inspection turnaround | No (only fixed charge/turnaround h and maint h/wk) | No post-visit inspection time, 14-day periodic inspection, G3 check time |
| Failure rates and holds | Only as a single availability % and completion % | No failure/hold rate per visit, hold duration, quarantine/G5 time |
| Recovery capacity | No | No spare robots, engineering G5 throughput, recovery trips |
| Customer scheduling | Only weekly demand count | No time windows, zones, cancellations timing, rebooking |
| Utilization | Yes: operator and robot | Formula issues (see 1.5) |
| Costs: staffing / transport | Yes: labor (full shifts), vehicle/day, driving/h, consumables | - |
| Maintenance allowance | No (robot cost/wk optional, 0) | missing |
| Overhead | No (README: excludes hardware, engineering, insurance, hub rent) | missing |
| Total, cost per completed deployment | Yes: total/wk, cost and labor per completed visit | - |
| Scenarios 5/10/25/50 robots | **No**: robots = 5 in every scenario | fleet size never varies |
| Conservative/base/optimistic | Partly: Conservative / Expected / "Higher-demand" (= Expected inputs with demand 18, not an optimistic case) + Custom | no optimistic case |

### 1.5 Issues found in the model logic

- `op_util` divides by `teams`, not `teams_eff` (2T1V shows 43% though one team can't go out); `robot_util` divides by all robots and full shift, ignoring availability and charging.
- `robot_n` with charge 3 h gives **1 visit per robot per day** in all scenarios; this single placeholder drives the robot constraint.
- Maintenance hours are converted to "visit-equivalents" and subtracted (can be fractional, then floored for the fleet).
- `cpv` returns text "no visits" when completed = 0 (mixed types downstream).
- Bottleneck logic: `demand < cap` -> "Demand"; demand == cap reports a resource constraint.
- No units on outputs beyond labels; % stored as fractions.

---

## 2. 04_Ops_Tracker.xlsx (secondary)

All sheets carry banner "SYNTHETIC DEMONSTRATION DATA · fictional, not company data". Data sheets are protected (no password). Pre-built formula rows to 202/302/102 etc., each guarded by `IF(A3="","",...)` (so references to empty rows are by design).

| Sheet | Dims | Formulas | Role | Purpose / records (synthetic rows) |
|---|---|---|---|---|
| README | A1:H16 | 0 | doc | rule, colours, append-only, "not a safety system" |
| Scorecard | A1:H32 | 44 | output | Fleet now (robot status counts) + period KPIs (completion, on-time, readiness pass, S0/S1 per 100, interventions, cancellations, rating, people-hours per completed visit); `MinN` small-sample warning |
| Checks | A1:H30 | 53 | output | 26 integrity checks (dup IDs, orphan refs, departed without PASS, invalid releases/approvals), OK/FAIL |
| Visits | A1:AI202 | 4000 | input + calc | visit plan (8): DEPARTURE CLEARANCE, flags F1-F11, G3 status, drive, depart/back times |
| Robots | A1:W22 | 260 | input + calc | register (5): G2 gate status, why, software, inspection due, holds |
| Homes | A1:X52 | 150 | input + calc | home profile/consent (7): G1, drive from hub (min) |
| Readiness | A1:AA302 | 1500 | input + calc | G3 12-item checks (6), RESULT |
| VisitLog | A1:Y202 | 1000 | input + calc | departed visits (5): G4, outcome, on-time (<=15 min), hours out |
| Incidents | A1:AL202 | 2000 | input + calc | 4 incidents, G5 approvals, blocking, founder notice |
| Daily | A1:P122 | 960 | input + calc | 4 daily reports with computed counts |
| Holds | A1:N102 | 400 | input + calc | 1 hold, valid release check |
| Software | A1:H32 | 60 | input + calc | 3 versions |
| Tasks | A1:H42 | 80 | input + calc | 5 tasks |
| People | A1:H32 | 0 | input | 6 role codes |
| Settings | A1:H14 | 0 | **inputs** | 12 named settings |

Settings (named cells, Settings!B3:B14): AsOf 2026-11-12 18:00 (fixed; "replace with =NOW()" in live use); ConsentVer CF-v1.0 [LEGAL]; InspDays 14 [A][ENG]; G3FreshH 6 [A][ENG]; TurnH 3 [A][ENG]; BufferMin 15 [A][OPS]; EarliestDep 09:00 [A][OPS]; LatestRet 16:30 [A][OPS]; DefSetup 30 [A]; DefPack 30 [A]; DefNotes 15 [A]; MinN 20 [OPS].

Formula notes: sheet-level dependency graph is acyclic (Robots <- Readiness/Holds/Incidents/Software; Visits <- Homes/Robots/Readiness/Tasks/People; VisitLog <- Visits; Scorecard/Checks/Daily are leaves). Uses `MAXIFS` (stored `_xlfn.MAXIFS`; needs Excel 2019/365 or Sheets), SUMPRODUCT array checks, INDEX/MATCH. Hard-coded numbers: 1440/24 (time conversion), 15 (on-time threshold, not linked to a setting), 12 (readiness item count), 100 (per-100 rate), 0.0001/0.00001 (time tolerances), 8 in `LEFT(W3,8)`. No cached values in the shipped file.

Useful for the scaling model: Settings timing values, Homes drive minutes, VisitLog hours out, Scorecard completion/readiness/S0-S1 rates as future calibration inputs. Synthetic week (11-09..11-12) has n<20 everywhere, so no rate is usable as a parameter.

---

## 3. Generation and tests

- Rebuild: `python3 build/build_tracker.py && python3 build/build_model.py`; `build/recalc.py` runs LibreOffice headless (`/Applications/LibreOffice.app`) to compute a copy into `.../calc/`; `build/read_calc.py` reads it back; `build/render_docs.sh` renders `source/*.md` via pandoc + Chrome to HTML/PDF/DOCX.
- `tests/test_model.py`: rebuilds the model, recalcs, and compares **all 50 Engine columns** against an independent Python `ref()` (cap, team_cap, robot_cap, booked, unmet, total, cpv, bottleneck) plus hand anchors (Expected cap 10, cpv 8800/8.5; Conservative cap 4, cpv 6880/2.8; 2T1V "Vehicles"; 2T2V cap 13 "Robots available"). Does not check utilization, math_cap, drive_share, lpv.
- `tests/scenarios.py` (96 checks, report `06_Scenario_Test_Report`, 96/96 pass on 2026-11-16): each step mutates a copy of `data.py`, rebuilds the tracker, recalcs in LibreOffice, reads computed cells.

| Scenario | Checks | Covers |
|---|---|---|
| Baseline | 14 | robot gate statuses, visit clearances/flags, RC-004 FAIL, G5 incomplete, integrity |
| A Readiness failure | 30 | FAIL -> HOLD, flags on other visits, two-person rule (INCOMPLETE), swap, release by newer PASS vs ENG release, invalid ops release, maintenance hold |
| B Fall incident | 22 | S1 -> QUARANTINE, founder notice, duplicate report, typed "Closed", wrong signer, G5 ENG+SAF+FND |
| C Scheduling conflict | 11 | overlapping bookings, turnaround/two-person conflicts, rebooking |
| D Repeated failure | 10 | linked parent incident, task restriction, only ENG may lift |
| E Scaling | 9 | **only capacity-model checks**: Higher-demand constraint + unmet 8, 2T1V "Vehicles", 2T2V "Robots available" cap 13, drive share 60/195, Custom 2T2V@95% availability cap 17 / unmet 1 |

## 4. 02_Field_Operations_Playbook_v1.0 headings

(From `source/02_playbook.md`, the pandoc source of the .docx; python-docx not installed in system python.)
1 The rule and the principles (1.1 Who decides what) · 2 Roles · 3 Customer onboarding · 4 Home suitability assessment (Gate 1) · 5 Scheduling and robot assignment (Gate 2) · 6 Pre-deployment readiness (Gate 3) · 7 Transportation and loading · 8 Arrival and household setup (Gate 4) · 9 Safe deployment and monitoring · 10 Stop-work conditions · 11 Incident response and escalation · 12 Robot recovery and return to hub · 13 Post-deployment inspection · 14 Maintenance and engineering handoff · 15 Engineering feedback, corrective actions and return to service (Gate 5) · 16 End-of-day reporting · 17 Daily and weekly cadence · 18 Customer communication: short scripts · 19 Validation register: what must be approved before real use

## 5. Reuse assessment

| Reuse as-is | Fix | Replace with Python model |
|---|---|---|
| Visit-block / field-window arithmetic and whole-visit FLOOR logic (and `ref()` in test_model.py as a seed) | `op_util` (use teams_eff), `robot_util` (availability, charging) | Fleet-size dimension 5/10/25/50 (currently fixed at 5) |
| Input list + units + [A]/[ENG] tagging convention; Settings timings consistent with tracker | Link grid labels to override cells; numeric cpv with explicit no-visit flag | Conservative/base/optimistic scenario matrix (replace "Higher-demand") |
| Cost structure: labor, vehicle/day, driving/h, consumables, per-completed-visit | Bottleneck tie at demand == cap | Failure/hold rates, hold duration, inspection turnaround, recovery capacity, maintenance allowance, overhead |
| Tracker Settings + Scorecard KPIs as future calibration sources | Name the hard-coded 15-min on-time threshold | Workbook should become an output/reconciliation target generated from the Python model, recalculated (LibreOffice/Excel) with cached values checked in |

## 6. Top risks of trusting this workbook

1. **Never calculated in Excel/Sheets**: shipped files have no cached values; all numbers come from LibreOffice copies in `tests/out/calc/`. Excel needs 2019+/365 for `MAXIFS` (tracker).
2. **Fleet size never varies** (5 robots everywhere); any 10/25/50 conclusion is extrapolation the model does not make.
3. **Robot capacity hinges on one [ENG] placeholder** (3 h charge -> 1 visit/robot/day) and a single availability %, with no failure, hold or recovery dynamics.
4. **Costs omit maintenance, overhead, hardware, engineering, insurance, hub rent**, so cost per completed visit is a floor, not a total.
5. **Utilization formulas are misleading** (wrong denominators), and grid labels are not linked to the values actually computed.
6. "Higher-demand" is not an optimistic case, and all inputs are synthetic; tests prove spreadsheet = Python re-implementation, not that the assumptions are right.
