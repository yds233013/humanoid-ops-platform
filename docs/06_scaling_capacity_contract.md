# Slice 6 Contract: Scaling and Capacity Model

Status: DRAFT rev 1 for engineering review · 2026-11-19 · baseline `slice-5-closed` (`de21a1d`). Planning only;
no implementation until the EM freezes this document. Companion: `docs/06a_workbook_inventory.md` (read-only
inventory of the existing `~/humanoid-fleet-ops` workbooks).

Everything is SYNTHETIC. The model is a **forecast built on explicit assumptions**, never a validated company
operating plan. Unknown robot specifications, deployment economics and customer demand stay visible as
unresolved inputs; nothing in this slice invents a real robot spec, a real price or a real safety figure.

## 0. Purpose (what the founder sees)

How the operation runs at 5 robots and what has to change to reach 10, 25 and 50: deployments per week, the
binding constraint at each step, when to hire field crews, recovery engineers and ops leads, when another vehicle
is needed, what it costs per completed deployment, and which operational improvement buys the most capacity.

## 1. Architecture (EM decision)

```
Slices 2-5 records (synthetic demo)  ->  capacity.baseline  ->  measured inputs (labelled synthetic)
input registry (provenance)          ->  capacity.model (pure Python, tested)  ->  scenario results
capacity.model results               ->  capacity.workbook (generated .xlsx with live formulas)
                                     ->  reconciliation test (LibreOffice recalculation == Python)
capacity.report                      ->  founder tables (Markdown/CSV) + read-only page /ops/capacity/
```

- New Django app `capacity`. **No data models, no writes.** `model.py` imports nothing from Django.
- The Python model is the calculation engine of record. The workbook is generated from the same input registry and
  carries live formulas so people can explore scenarios in Excel; a reconciliation test proves its recalculated
  outputs equal the Python outputs for every scenario.
- The existing `05_Capacity_Cost_Model.xlsx` is an **unverified starting asset** (inventory: never calculated in
  Excel, fleet fixed at 5, no failure/hold or recovery model, no maintenance/overhead, two misleading utilization
  formulas, a 3 h charging placeholder that forces one visit per robot per day). Its sheet layout and input
  wording are reused; its formulas are not. It is not modified; the new workbook is generated into this repo.

## 2. Inputs and provenance

Every input lives in one registry `capacity/inputs.py` as `Input(key, label, unit, time_basis, provenance,
source, conservative, base, optimistic, notes)`. `provenance` is exactly one of:

| class | meaning | value rule |
|---|---|---|
| `platform_measured` | derived from Slice 2-5 records by `capacity.baseline` | value comes from the snapshot; shown as "synthetic demo measurement"; the registry holds the fallback used when the platform has too few records (`min_sample`) and says so |
| `synthetic_assumption` | a stated planning assumption for the demo | all three scenario values required, each with a one-line rationale |
| `unknown_company_data` | needs the company (robot specs, demand, prices) | a placeholder value is allowed only so the model runs; it is flagged everywhere it is used and every output that depends on it is marked "depends on unknown input: <key>" |

Initial registry (units and basis are binding; values are proposals for EM review, all synthetic):

| key | unit / basis | provenance | source / note |
|---|---|---|---|
| `fleet_size` | robots | scenario axis | 5, 10, 25, 50 |
| `inspect_min`, `load_min`, `unload_min` | minutes per visit | platform_measured | median planned reservation of that purpose (Slice 2) |
| `on_site_min` | minutes per visit | platform_measured | median planned on-site reservation |
| `travel_one_way_min` | minutes | synthetic_assumption | depends on geography; cons/base/opt 60/40/25 |
| `charge_turnaround_min` | minutes per visit | unknown_company_data | robot spec (B-04); placeholder 180 (the old model's 3 h) |
| `runtime_limit_min` | minutes on site | unknown_company_data | robot spec (B-04); placeholder = no limit, flagged |
| `crew_size` | people per visit | platform_measured | lead + second (Slice 2 rule) |
| `robots_per_trip` | robots | platform_measured | `max_robots_per_trip` setting (Slice 2) |
| `working_day_min`, `working_days_week` | minutes/day, days/week | synthetic_assumption | 09:00-16:30 field window from the tracker settings; 5 days |
| `incidents_per_departure` | probability | platform_measured | incidents / departures in the window (min_sample 30, else fallback cons/base/opt 0.10/0.05/0.02) |
| `hold_days_per_incident` | days | platform_measured | median hold placed -> released for incident holds (fallback 5/3/1) |
| `recovery_engineer_hours_per_incident` | hours | synthetic_assumption | 8/5/3 |
| `planned_maintenance_hours_per_robot_month` | hours | unknown_company_data | robot spec (B-08); placeholder 8 |
| `demand_visits_per_robot_week` | visits | unknown_company_data | customer demand; placeholder 3/4/5 |
| `crew_utilization_hire_trigger` | fraction | synthetic_assumption | hire when planned crew utilization > 0.80 |
| `ops_lead_span` | field crews per ops lead | synthetic_assumption | 6/8/10 |
| costs: `field_operator_cost_month`, `engineer_cost_month`, `ops_lead_cost_month`, `vehicle_cost_month`, `vehicle_cost_per_km`, `maintenance_allowance_robot_month`, `overhead_month` | currency/month or /km | synthetic_assumption or unknown_company_data | all labelled "synthetic placeholder currency", no real salary or price claims |

The registry is complete when a test proves every input has unit, time basis, provenance, source and all three
values (or the explicit unknown placeholder).

## 3. The model (pure functions, `capacity/model.py`)

`run(inputs: Inputs, fleet_size: int, assumption: str) -> ScenarioResult`, deterministic, no I/O.

**Time per visit (minutes)**
- robot cycle = inspect + load + 2 × travel + on_site + unload + charge_turnaround
- crew cycle = inspect + load + 2 × travel + on_site + unload (crew blocked inspection through unloading, Slice 2)
- vehicle cycle = load + 2 × travel + on_site + unload (Slice 2 trip reservation)
- a cycle longer than the working day is **INFEASIBLE** for that resource (capacity 0, flagged with the reason),
  never a fractional or negative visit.

**Reliability**
- effective robots = fleet × (1 − downtime share), downtime share = holds per robot-week × hold days / working
  days + maintenance hours / available hours; capped to [0, 1].
- recovery demand (engineer hours/week) = expected incidents/week × recovery hours; recovery capacity =
  engineers × available hours; if demand > capacity the excess hold time is added to downtime (backlog) and the
  binding constraint can become `recovery`.

**Capacity (visits/week, integer, floor)**
- robot capacity = effective robots × floor(working day / robot cycle) × days
- crew capacity = field crews × floor(working day / crew cycle) × days
- vehicle capacity = vehicles × floor(working day / vehicle cycle) × robots_per_trip × days
- capacity = min of the three; `binding_constraint` = the argmin (ties named in fixed order robot, crew,
  vehicle, recovery)
- completed = min(capacity, demand); unmet = demand − completed

**Staffing plan (what to hire for the demand)**: smallest field crews, vehicles and engineers such that capacity ≥
demand (or the robot limit, whichever is lower) and planned crew utilization ≤ the hire trigger; ops leads =
ceil(crews / span). The plan is reported per fleet size with the step at which each hire happens.

**Costs (per month, synthetic placeholder currency)**: staffing (crews × crew_size × operator cost + engineers +
ops leads), vehicles (fixed + per km × trips × 2 × travel distance), maintenance allowance × fleet, overhead; total;
cost per completed deployment = total / completed per month, **N/A when completed is 0**.

**Founder decision outputs**
- the binding constraint at 5 / 10 / 25 / 50 and the next constraint after relieving it;
- hiring triggers (when the crew, engineer or ops-lead count increases) and vehicle triggers;
- sensitivity: for each non-scenario input, capacity and cost per deployment at −20% / +20% (tornado), ranked by
  capacity gained, so "which improvement gives the most capacity" is computed, not asserted;
- every output lists the unknown_company_data inputs it depends on.

## 4. Baseline from the platform (`capacity/baseline.py`)

Inside `operations.snapshot.read_snapshot()` (read-only, no locks): median planned durations per reservation
purpose, crew size, robots per trip, incidents per departure and median incident-hold duration over a window, with
the sample size of each. Below `min_sample` the registry fallback is used and the report says "insufficient
platform data (n=…), using assumption". The 5-robot baseline report states plainly that these are **synthetic demo
measurements, not real-world performance**.

## 5. Workbook (`capacity/workbook.py`) and reconciliation

Generated with openpyxl into `build/capacity_model.xlsx` (not committed): Inputs (registry with provenance column
and colour per class), Engine (one column per fleet × assumption case, live formulas mirroring section 3),
Scenarios, Staffing plan, Costs, Sensitivity, Provenance. Reconciliation: recalculate with LibreOffice headless
(available locally at `/opt/homebrew/bin/soffice`; CI job installs `libreoffice-calc`) and assert every output cell
equals the Python result within 1e-9 (integers exactly) for all 12 cases, and that no cell is an error value.
Functions are limited to ones Excel 2016, LibreOffice and Google Sheets all support (no MAXIFS, no dynamic arrays).

## 6. Tests (closure bar)

1. Provenance completeness (section 2), unknown inputs flagged in every dependent output.
2. Conservation: completed ≤ each resource capacity and ≤ demand; used hours ≤ available hours per resource.
3. Impossible schedules: cycle > working day ⇒ INFEASIBLE with reason, capacity 0, no negatives, no exceptions.
4. Zero denominators ⇒ N/A (cost per deployment, utilization), never 0 or an error.
5. Monotonicity: more robots never reduces robot capacity; more crews/vehicles/engineers never reduce capacity;
   a higher incident rate or longer hold never increases capacity; longer travel never increases capacity.
6. Scenario ordering: conservative ≤ base ≤ optimistic capacity for every fleet size (with the registry values).
7. Hand-computed golden cases (at least 4, including one per binding constraint and one recovery backlog).
8. Python == recalculated workbook for all 12 cases; no error cells.
9. Baseline extraction against a synthetic world with known durations/incidents (exact expected medians and n),
   and the fallback path below min_sample; runs inside the snapshot with no writes.
10. Report and page: renders for operations.metrics roles, 403 otherwise, "forecast, not a plan" and synthetic
    labelling present, no forbidden wording.
11. Full suite, static checks, CI.

## 7. Ownership and sequence

| Owner | Files |
|---|---|
| Lead | app shell, `capacity/inputs.py` registry (values reviewed by the EM), docs, permissions reuse (`operations.metrics`), CI job for LibreOffice, integration |
| A, model | `capacity/model.py`, `tests/test_capacity_model.py` (sections 3, 6.2-6.7) |
| B, baseline | `capacity/baseline.py`, `tests/test_capacity_baseline.py` (section 4, 6.9) |
| C, workbook | `capacity/workbook.py`, `tests/test_capacity_workbook.py` (section 5, 6.8) |
| D, report + page | `capacity/report.py`, `capacity/management/commands/capacity_report.py`, `capacity/views.py`, `capacity/urls.py`, `capacity/templates/**`, `tests/test_capacity_report.py` |

Sequence: lead registry first (frozen interface) → A → B, C, D in parallel on A's frozen API → lead merge, CI,
report generation, EM review. Engineers run one full suite at a time (shared host).

## 8. Decisions requested

- D1 Engine of record = Python; workbook generated and reconciled (EM direction) - confirm.
- D2 The registry values in section 2 (synthetic assumptions and unknown placeholders) - approve or change.
- D3 Capacity uses floor(visits per day) per resource (whole visits), and charging blocks the robot only (not
  the crew). Confirm.
- D4 Recovery modelled as engineer hours per incident with backlog folding into downtime. Confirm or simplify.
- D5 A read-only `/ops/capacity/` page in addition to the generated report and workbook. Confirm or drop.
