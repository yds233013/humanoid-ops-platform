# ruff: noqa: E501  (spreadsheet formula templates and sheet text read best on one line)
"""Slice 6 capacity workbook (frozen contract docs/06_scaling_capacity_contract.md rev 4, section 9).

Generates `build/capacity_model.xlsx`: a validated PROJECTION of the authoritative Python model
(`capacity.model`). Everything is SYNTHETIC: a forecast built on explicit assumptions, never an operating plan.

* Inputs, Engine, Costs and Scenarios carry LIVE formulas (sections 3, 4 and 6): change an input on the Inputs
  sheet and the current-team results recalculate in Excel, LibreOffice or Google Sheets.
* Staffing and Sensitivity are VALUES computed by the Python model (a search loop is not a plain formula).
* Only functions common to Excel 2016, LibreOffice and Google Sheets are used (`ALLOWED_FUNCTIONS`).

The reconciliation test recalculates the file with LibreOffice headless and checks every founder-facing output
against `capacity.model`. Excel and Google Sheets compatibility are separate manual checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.worksheet import Worksheet

from . import model as M
from .inputs import ASSUMPTIONS, BY_KEY, FLEET_SIZES, REGISTRY, Assumption, unknown_keys
from .inputs import values as registry_values

CellValue = int | float | str | bool | None
ValuesByAssumption = Mapping[Assumption, Mapping[str, float | None]]

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "build" / "capacity_model.xlsx"

SHEETS: tuple[str, ...] = (
    "README",
    "Inputs",
    "Engine",
    "Costs",
    "Scenarios",
    "Staffing",
    "Sensitivity",
    "Provenance",
)
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "MIN",
        "MAX",
        "INT",
        "IF",
        "INDEX",
        "SUM",
        "ROUNDUP",
        "ABS",
        "AND",
        "OR",
        "ISNUMBER",
        "TRIM",
        "SUBSTITUTE",
    }
)
TOL = "1E-9"  # the model's floor / growth tolerance (model.EPS), written into every floor
NA = "N/A"
UNCONSTRAINED_TEXT = "unconstrained"
PYTHON_LABEL = "computed by the Python model (capacity.model) - values, not formulas"

PROVENANCE_FILL: dict[str, str] = {
    "platform_measured": "DDEBF7",  # blue
    "synthetic_assumption": "FFF2CC",  # yellow
    "unknown_company_data": "F8CBAD",  # orange
}
PROVENANCE_MEANING: dict[str, str] = {
    "platform_measured": "measured on the synthetic demo platform when the sample rule is met, else the fallback",
    "synthetic_assumption": "a stated planning assumption (synthetic)",
    "unknown_company_data": "UNKNOWN company data - illustrative placeholder, not a real figure",
}

INPUTS_FIRST_ROW = 5
VALUE_COLUMN: dict[str, str] = {"conservative": "H", "base": "I", "optimistic": "J"}
INPUTS_HEADER = (
    "no.",
    "key",
    "label",
    "unit",
    "time basis",
    "provenance",
    "source",
    "conservative",
    "base",
    "optimistic",
    "capacity direction",
    "integer",
    "notes",
)

# Engine week-table columns (A..Q): key, header
WEEK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("t", "week t"),
    ("u_prev", "U(t-1) unavailable"),
    ("available", "A available robots"),
    ("robot", "R robot cap"),
    ("crew", "C crew cap"),
    ("vehicle", "V vehicle cap"),
    ("inspection", "I inspection cap"),
    ("cap", "cap = min"),
    ("binding", "binding"),
    ("done", "done"),
    ("unmet", "unmet"),
    ("holds", "holds"),
    ("older", "O older backlog"),
    ("rec_old", "rec_old"),
    ("rec_new", "rec_new"),
    ("backlog", "B backlog"),
    ("unavailable", "U unavailable next week"),
)
WEEK_COL: dict[str, str] = {k: get_column_letter(i + 1) for i, (k, _) in enumerate(WEEK_COLUMNS)}

COSTS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("case", "case"),
    ("fleet", "fleet (robots)"),
    ("assumption", "assumption"),
    ("ops_leads", "ops leads (configured)"),
    ("labor", "labor SCU/month"),
    ("km_visit", "km per visit"),
    ("vehicles", "vehicles SCU/month"),
    ("maintenance", "maintenance SCU/month"),
    ("overhead", "overhead SCU/month"),
    ("total", "total SCU/month"),
    ("completed_month", "completed deployments/month"),
    ("per_deployment", "cost per completed deployment SCU"),
    ("span_leads", "leads needed by span"),
    ("supervisor_shortfall", "supervisor shortfall"),
)
SCENARIO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("case", "case"),
    ("fleet", "fleet (robots)"),
    ("assumption", "assumption"),
    ("demand", "demand visits/week"),
    ("done", "completed visits/week"),
    ("unmet", "unmet visits/week"),
    ("binding", "binding constraint"),
    ("flags", "flags"),
    ("robot", "robot capacity/week"),
    ("crew", "crew capacity/week"),
    ("vehicle", "vehicle capacity/week"),
    ("inspection", "inspection capacity/week"),
    ("util_crew", "crew utilization"),
    ("util_vehicle", "vehicle utilization"),
    ("util_robot", "robot utilization"),
    ("labor", "labor SCU/month"),
    ("vehicles", "vehicles SCU/month"),
    ("maintenance", "maintenance SCU/month"),
    ("overhead", "overhead SCU/month"),
    ("total", "total SCU/month"),
    ("per_deployment", "cost per completed deployment SCU"),
    ("supervisor_shortfall", "supervisor shortfall (ops leads)"),
    ("depends_on_unknown", "depends on unknown inputs"),
)
STAFFING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("case", "case"),
    ("fleet", "fleet (robots)"),
    ("assumption", "assumption"),
    ("demand", "demand visits/week"),
    ("target", "robot-limited target/week"),
    ("status", "status"),
    ("reason", "reason"),
    ("crews", "crews"),
    ("vehicles", "vehicles"),
    ("engineers", "engineers"),
    ("stations", "inspection stations"),
    ("ops_leads", "ops leads (span)"),
    ("done", "completed visits/week"),
    ("unmet", "unmet visits/week"),
    ("binding", "binding constraint"),
    ("flags", "flags"),
    ("total", "total SCU/month"),
    ("per_deployment", "cost per completed deployment SCU"),
    ("steps", "search steps"),
)
SENSITIVITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("case", "case"),
    ("fleet", "fleet (robots)"),
    ("assumption", "assumption"),
    ("staffing", "staffing held fixed"),
    ("rank", "rank"),
    ("key", "input"),
    ("unknown", "unknown company data"),
    ("low_value", "value -20%"),
    ("high_value", "value +20%"),
    ("done_low", "completed/week at -20%"),
    ("done_high", "completed/week at +20%"),
    ("delta_done_low", "change at -20%"),
    ("delta_done_high", "change at +20%"),
    ("cost_low", "cost per deployment at -20%"),
    ("cost_high", "cost per deployment at +20%"),
)
TABLE_HEADER_ROW = 4  # header row of Costs / Scenarios / Staffing / Sensitivity; data starts below

# Engine parameter rows: (key, input key, label, rounding) - rounding mirrors model._int (round half up)
_PARAMS: tuple[tuple[str, str, str, str], ...] = (
    ("w", "working_day_min", "W working day (min)", "raw"),
    ("d", "working_days_week", "D working days/week", "raw"),
    ("eff", "scheduling_efficiency", "scheduling efficiency", "raw"),
    ("inspect", "inspect_min", "inspection (min)", "raw"),
    ("load", "load_min", "loading (min)", "raw"),
    ("travel", "travel_one_way_min", "travel one way (min)", "raw"),
    ("on_site", "on_site_min", "on site (min)", "raw"),
    ("unload", "unload_min", "unloading (min)", "raw"),
    ("post", "post_inspect_min", "post-visit inspection (min)", "raw"),
    ("charge", "charge_between_visits_min", "charge between visits (min)", "raw"),
    ("runtime", "runtime_limit_min", "runtime limit on site (min)", "runtime"),
    ("r", "robots_per_visit", "r robots per visit", "int"),
    ("veh_cap", "vehicle_robot_capacity", "robots per vehicle", "int"),
    ("crew_size", "crew_size", "crew size (people)", "raw"),
    ("p_incident", "incidents_per_visit", "incidents per visit", "raw"),
    ("holds_per_incident", "holds_per_incident", "holds per incident", "raw"),
    ("hold_min_days", "hold_min_days", "minimum hold (working days)", "raw"),
    ("rec_hours", "recovery_hours_per_hold", "recovery hours per hold", "raw"),
    ("eng_hours", "engineer_hours_per_week", "engineer hours/week", "raw"),
    ("maint_hours", "maintenance_hours_per_robot_month", "maintenance h/robot-month", "raw"),
    ("demand_per_robot", "demand_visits_per_robot_week", "demand visits/robot/week", "raw"),
    ("crews", "configured_crews", "configured crews", "int"),
    ("vehicles", "configured_vehicles", "configured vehicles", "int"),
    ("engineers", "configured_engineers", "configured engineers", "int"),
    ("stations", "configured_inspection_stations", "configured inspection stations", "int"),
    ("leads", "configured_ops_leads", "configured ops leads", "int"),
    ("span", "ops_lead_span", "ops lead span (crews)", "int"),
    ("op_cost", "field_operator_cost_month", "operator SCU/month", "raw"),
    ("eng_cost", "engineer_cost_month", "engineer SCU/month", "raw"),
    ("lead_cost", "ops_lead_cost_month", "ops lead SCU/month", "raw"),
    ("veh_fixed", "vehicle_fixed_cost_month", "vehicle fixed SCU/month", "raw"),
    ("cost_km", "vehicle_cost_per_km", "vehicle SCU/km", "raw"),
    ("speed", "avg_speed_kmh", "average speed (km/h)", "raw"),
    ("allowance", "maintenance_allowance_robot_month", "maintenance allowance SCU/robot-month", "raw"),
    ("overhead", "overhead_month", "overhead SCU/month", "raw"),
    ("horizon", "horizon_weeks", "H horizon (weeks)", "horizon"),
)

# Engine derived rows: (key, label, formula template using {name} for engine cells, note)
_DERIVED: tuple[tuple[str, str, str, str], ...] = (
    (
        "tr",
        "Tr robot busy per visit (min)",
        "={inspect}+{load}+2*{travel}+{on_site}+{unload}+{post}",
        "section 3",
    ),
    ("tc", "Tc crew busy per visit (min)", "={tr}", "crew does not supervise charging"),
    ("tv", "Tv vehicle busy per visit (min)", "={load}+2*{travel}+{on_site}+{unload}", "one site per trip"),
    ("ti", "Ti inspection per robot (min)", "={inspect}+{post}", "each robot is inspected"),
    (
        "robot_infeasible",
        "robot INFEASIBLE",
        "=OR(IF(ISNUMBER({runtime}),{runtime}<{on_site},FALSE),{tr}<=0,{tr}>{w}+" + TOL + ")",
        "runtime limit below the session, or cycle not in (0, W]",
    ),
    ("crew_infeasible", "crew INFEASIBLE", "=OR({tc}<=0,{tc}>{w}+" + TOL + ")", "cycle not in (0, W]"),
    (
        "vehicle_infeasible",
        "vehicle INFEASIBLE",
        "=OR({tv}<=0,{tv}>{w}+" + TOL + ",{veh_cap}<{r})",
        "cycle not in (0, W] or the vehicle cannot carry r robots",
    ),
    (
        "inspection_infeasible",
        "inspection INFEASIBLE",
        "=OR({ti}<=0,{ti}>{w}+" + TOL + ")",
        "cycle not in (0, W]",
    ),
    (
        "v_robot",
        "robot visits/day",
        "=IF({robot_infeasible},0,IF({tr}+{charge}<=0,1,1+INT(({w}-{tr})/({tr}+{charge})+" + TOL + ")))",
        "largest k with k*Tr+(k-1)*charge <= W (closed form)",
    ),
    ("v_crew", "crew visits/day", "=IF({crew_infeasible},0,INT({w}/{tc}+" + TOL + "))", "floor(W/Tc)"),
    (
        "v_vehicle",
        "vehicle visits/day",
        "=IF({vehicle_infeasible},0,INT({w}/{tv}+" + TOL + "))",
        "floor(W/Tv)",
    ),
    (
        "v_inspection",
        "robot-inspections/day per station",
        "=IF({inspection_infeasible},0,INT({w}/{ti}+" + TOL + "))",
        "floor(W/Ti)",
    ),
    ("hours_month", "working hours per robot-month", "={w}/60*{d}*(52/12)", "weeks per month = 52/12"),
    (
        "m",
        "m maintenance fraction",
        "=IF({hours_month}<=0,1,{maint_hours}/{hours_month})",
        "share of robot time in planned maintenance",
    ),
    ("demand", "demand visits/week", "=INT({demand_per_robot}*{fleet}+" + TOL + ")", "whole visits"),
    ("p_hold", "holds per completed visit", "={p_incident}*{holds_per_incident}", "incident counted once"),
    (
        "jobs_capacity",
        "recovery jobs/week",
        "=IF({rec_hours}>0,{engineers}*{eng_hours}/{rec_hours},1E+300)",
        "engineers x hours / hours per hold",
    ),
    ("f_min", "f_min minimum-hold share of a week", "=IF({d}>0,{hold_min_days}/{d},1)", "hold_min_days / D"),
    ("c_cap", "C crew capacity/week", "=INT({eff}*{crews}*{v_crew}*{d}+" + TOL + ")", "configured crews"),
    (
        "v_cap",
        "V vehicle capacity/week",
        "=INT({eff}*{vehicles}*{v_vehicle}*{d}+" + TOL + ")",
        "one trip/visit",
    ),
    (
        "i_cap",
        "I inspection capacity/week",
        "=IF({r}>0,INT({stations}*{v_inspection}*{d}/{r}+" + TOL + "),0)",
        "not bound by customer windows",
    ),
)


@dataclass
class EngineBlock:
    """Cell addresses (absolute, Engine sheet) of one case block; used by Costs/Scenarios and by the tests."""

    fleet: int
    assumption: Assumption
    first_row: int
    cells: dict[str, str] = field(default_factory=dict)
    week_first_row: int = 0
    week_last_row: int = 0

    @property
    def case(self) -> str:
        return case_id(self.fleet, self.assumption)

    def ref(self, name: str) -> str:
        return f"Engine!{self.cells[name]}"


def case_id(fleet: int, assumption: str) -> str:
    return f"{fleet}-{assumption}"


def _abs(col: str, row: int) -> str:
    return f"${col}${row}"


def _bold(ws: Worksheet, row: int, values: tuple[str, ...] | list[str], col: int = 1) -> None:
    for i, v in enumerate(values):
        cell = ws.cell(row=row, column=col + i, value=v)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _title(ws: Worksheet, title: str, note: str) -> None:
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=note).font = Font(italic=True)


def _widths(ws: Worksheet, widths: Mapping[str, float]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def _resolve(values_by_assumption: ValuesByAssumption | None) -> dict[Assumption, dict[str, float | None]]:
    out: dict[Assumption, dict[str, float | None]] = {}
    for a in ASSUMPTIONS:
        v = registry_values(a)
        if values_by_assumption is not None and a in values_by_assumption:
            v.update(values_by_assumption[a])
        out[a] = v
    return out


def horizon_rows(resolved: Mapping[Assumption, Mapping[str, float | None]]) -> int:
    """Generated week rows per block: the largest horizon of any assumption (the model's max(1, round))."""
    hs = []
    for v in resolved.values():
        h = v["horizon_weeks"]
        hs.append(max(1, int(float(h) + 0.5)) if h is not None else 1)
    return max(hs)


# --------------------------------------------------------------------------------------------------- sheets


def _readme(ws: Worksheet, n_weeks: int) -> None:
    lines = [
        ("Humanoid ops platform - capacity model workbook", True),
        ("SYNTHETIC DATA ONLY. Every number here is synthetic or an illustrative placeholder.", True),
        ("This is a FORECAST built on explicit assumptions, NOT a validated company operating plan.", True),
        ("", False),
        ("What is in this workbook", True),
        ("Inputs: the 38-input registry x conservative / base / optimistic, coloured by provenance.", False),
        (
            "Engine: one block per case (fleet 5, 10, 25, 50 x assumption) with the cycles, maintenance fraction "
            f"and the weekly recursion over {n_weeks} generated week rows - LIVE formulas on Inputs.",
            False,
        ),
        ("Costs: monthly cost per case (configured team) - live formulas on Engine.", False),
        ("Scenarios: founder-facing current-team results per case - live formulas.", False),
        (f"Staffing: recommended staffing - {PYTHON_LABEL}; a search loop is not a formula.", False),
        (f"Sensitivity: -20% / +20% per input - {PYTHON_LABEL}.", False),
        ("Provenance: where each input comes from, and the platform baseline sources.", False),
        ("", False),
        ("Approximations (stated, deliberate)", True),
        ("Whole visits per working day per resource; a visit is an indivisible block (conservative).", False),
        ("Robots start each day charged (overnight charging is an assumption).", False),
        ("One vehicle trip serves one site; multi-site batching is not modelled.", False),
        (
            "Weekly steps with expected values; new holds are treated as placed at the start of their week, so the "
            "remaining minimum hold falls into the next week as hold_min_days / D (an upper bound).",
            False,
        ),
        ("Floors use a 1E-9 tolerance so exact integers are not lost to floating-point error.", False),
        (
            "Results are read at week H (horizon_weeks). If you raise horizon_weeks above the generated rows, "
            "regenerate the workbook (Engine shows a horizon check).",
            False,
        ),
        (
            "The Python model (capacity.model) is authoritative; this workbook is a validated projection.",
            False,
        ),
        ("Reconciled automatically against Python after a LibreOffice headless recalculation.", False),
        (
            "Excel and Google Sheets compatibility are separate manual checks, not claimed from LibreOffice.",
            False,
        ),
        ("", False),
        ("Unknown inputs (unknown_company_data - illustrative placeholders, not real figures)", True),
    ]
    for k in unknown_keys():
        inp = BY_KEY[k]
        lines.append((f"{k}: {inp.label} ({inp.unit}, {inp.time_basis})", False))
    lines.append(
        ("Every result that depends on one of these says so (Scenarios, depends on unknown inputs).", False)
    )
    for i, (text, bold) in enumerate(lines, start=1):
        cell = ws.cell(row=i, column=1, value=text)
        if bold:
            cell.font = Font(bold=True, size=13 if i == 1 else 11)
    _widths(ws, {"A": 140})


def _value_cell(v: float | None) -> float | str:
    return UNCONSTRAINED_TEXT if v is None else v


def _inputs(ws: Worksheet, resolved: Mapping[Assumption, Mapping[str, float | None]]) -> dict[str, int]:
    _title(
        ws,
        "Inputs - the 38-input registry (SYNTHETIC)",
        "Edit the value columns; Engine, Costs and "
        "Scenarios recalculate. Row colour = provenance class (legend below the table).",
    )
    _bold(ws, TABLE_HEADER_ROW, list(INPUTS_HEADER))
    rows: dict[str, int] = {}
    for i, inp in enumerate(REGISTRY):
        row = INPUTS_FIRST_ROW + i
        rows[inp.key] = row
        fill = PatternFill("solid", fgColor=PROVENANCE_FILL[inp.provenance])
        cells: list[CellValue] = [
            i + 1,
            inp.key,
            inp.label,
            inp.unit,
            inp.time_basis,
            inp.provenance,
            inp.source,
            _value_cell(resolved["conservative"][inp.key]),
            _value_cell(resolved["base"][inp.key]),
            _value_cell(resolved["optimistic"][inp.key]),
            inp.capacity_direction,
            "yes" if inp.integer else "no",
            inp.notes,
        ]
        for c, v in enumerate(cells, start=1):
            ws.cell(row=row, column=c, value=v).fill = fill
    legend = INPUTS_FIRST_ROW + len(REGISTRY) + 1
    ws.cell(row=legend, column=2, value="Provenance legend").font = Font(bold=True)
    for j, (prov, colour) in enumerate(PROVENANCE_FILL.items(), start=1):
        ws.cell(row=legend + j, column=2, value=prov).fill = PatternFill("solid", fgColor=colour)
        ws.cell(row=legend + j, column=3, value=PROVENANCE_MEANING[prov])
    ws.cell(
        row=legend + 5,
        column=2,
        value="runtime_limit_min = 'unconstrained': not limited in this illustrative calculation "
        "(not a claim about the robot). Enter minutes to apply a limit.",
    )
    ws.freeze_panes = "C5"
    _widths(
        ws,
        {
            "A": 4,
            "B": 34,
            "C": 34,
            "D": 14,
            "E": 18,
            "F": 22,
            "G": 40,
            "H": 13,
            "I": 13,
            "J": 13,
            "K": 18,
            "L": 8,
            "M": 50,
        },
    )
    return rows


def _engine(ws: Worksheet, input_rows: Mapping[str, int], n_weeks: int) -> list[EngineBlock]:
    _title(
        ws,
        "Engine - live weekly recursion per case (contract sections 3-4; SYNTHETIC)",
        "Column A is a stable key, C holds the live formula. Configured team. Floors use a 1E-9 tolerance.",
    )
    blocks: list[EngineBlock] = []
    row = 4
    for fleet in FLEET_SIZES:
        for a in ASSUMPTIONS:
            block = EngineBlock(fleet=fleet, assumption=a, first_row=row)
            col = VALUE_COLUMN[a]
            _bold(ws, row, ["case", f"{fleet} robots / {a}", case_id(fleet, a)])
            ws.cell(row=row, column=1).fill = PatternFill("solid", fgColor="D9D9D9")
            row += 1
            _bold(ws, row, ["key", "quantity", "value", "note"])
            row += 1

            def put(key: str, label: str, value: CellValue, note: str, *, _b: EngineBlock = block) -> None:
                nonlocal row
                ws.cell(row=row, column=1, value=key)
                ws.cell(row=row, column=2, value=label)
                ws.cell(row=row, column=3, value=value)
                ws.cell(row=row, column=4, value=note)
                _b.cells[key] = _abs("C", row)
                row += 1

            put("fleet", "N fleet size (robots)", fleet, "scenario axis")
            for key, inp_key, label, rounding in _PARAMS:
                src = f"Inputs!{_abs(col, input_rows[inp_key])}"
                if rounding == "int":
                    formula = f"=INT({src}+0.5)"
                elif rounding == "horizon":
                    formula = f"=MAX(1,INT({src}+0.5))"
                elif rounding == "runtime":
                    formula = f'=IF(ISNUMBER({src}),{src},"{UNCONSTRAINED_TEXT}")'
                else:
                    formula = f"={src}"
                put(key, label, formula, f"Inputs {inp_key} ({a})")
            for key, label, template, note in _DERIVED:
                put(key, label, template.format(**block.cells), note)

            # weekly recursion
            row += 1
            _bold(ws, row, [h for _, h in WEEK_COLUMNS])
            row += 1
            c = block.cells
            block.week_first_row = row
            for t in range(1, n_weeks + 1):
                wc = {k: f"{WEEK_COL[k]}{row}" for k in WEEK_COL}
                prev = {k: f"{WEEK_COL[k]}{row - 1}" for k in WEEK_COL}
                formulas: dict[str, CellValue] = {
                    "t": t,
                    "u_prev": 0 if t == 1 else f"={prev['unavailable']}",
                    "available": f"=MAX(0,({c['fleet']}-{wc['u_prev']})*(1-{c['m']}))",
                    "robot": f"=IF({c['r']}>0,INT({c['eff']}*{wc['available']}*{c['v_robot']}*{c['d']}/{c['r']}"
                    f"+{TOL}),0)",
                    "crew": f"={c['c_cap']}",
                    "vehicle": f"={c['v_cap']}",
                    "inspection": f"={c['i_cap']}",
                    "cap": f"=MIN({wc['robot']},{wc['crew']},{wc['vehicle']},{wc['inspection']})",
                    "binding": f'=IF({wc["robot"]}={wc["cap"]},"robot",IF({wc["crew"]}={wc["cap"]},"crew",'
                    f'IF({wc["vehicle"]}={wc["cap"]},"vehicle","inspection")))',
                    "done": f"=MIN({wc['cap']},{c['demand']})",
                    "unmet": f"={c['demand']}-{wc['done']}",
                    "holds": f"={wc['done']}*{c['p_hold']}",
                    "older": 0 if t == 1 else f"={prev['backlog']}",
                    "rec_old": f"=MIN({wc['older']},{c['jobs_capacity']})",
                    "rec_new": f"=MIN({wc['holds']},{c['jobs_capacity']}-{wc['rec_old']})",
                    "backlog": f"=({wc['older']}-{wc['rec_old']})+({wc['holds']}-{wc['rec_new']})",
                    "unavailable": f"={wc['backlog']}+{wc['rec_new']}*{c['f_min']}+{wc['rec_old']}*0",
                }
                for i, (k, _) in enumerate(WEEK_COLUMNS, start=1):
                    ws.cell(row=row, column=i, value=formulas[k])
                row += 1
            block.week_last_row = row - 1

            def rng(k: str, _b: EngineBlock = block) -> str:
                return f"${WEEK_COL[k]}${_b.week_first_row}:${WEEK_COL[k]}${_b.week_last_row}"

            def at(k: str, offset: str = "", _c: dict[str, str] = c) -> str:
                return f"INDEX({rng(k)},{_c['h_row']}{offset})"

            row += 1
            _bold(ws, row, ["key", f"result at week H (generated rows: {n_weeks})", "value", "note"])
            row += 1
            put("h_row", "week reported", f"=MIN({c['horizon']},{n_weeks})", "horizon_weeks")
            put(
                "horizon_check",
                "horizon check",
                f'=IF({c["horizon"]}<={n_weeks},"ok","REGENERATE: horizon exceeds generated week rows")',
                "rows are generated for the largest horizon",
            )
            for k in ("robot", "crew", "vehicle", "inspection", "cap", "done", "unmet", "backlog"):
                put(f"{k}_h", f"{k} at week H", f"={at(k)}", "")
            put(
                "binding_h",
                "binding at week H",
                f"={at('binding')}",
                "ties: robot, crew, vehicle, inspection",
            )
            put(
                "infeasible",
                "INFEASIBLE",
                f"=OR({c['robot_infeasible']},{c['crew_infeasible']},{c['vehicle_infeasible']},"
                f"{c['inspection_infeasible']})",
                "any resource infeasible",
            )
            put(
                "unstable",
                "RECOVERY_UNSTABLE",
                f"=IF({c['h_row']}>1,{at('backlog')}>{at('backlog', '-1')}+{TOL},{at('backlog')}>0+{TOL})",
                "backlog still growing at week H",
            )
            put(
                "not_converged",
                "NOT_CONVERGED",
                f"=IF({c['h_row']}>1,ABS({at('done')}-{at('done', '-1')})>=1,FALSE)",
                "|done_H - done_H-1| >= 1",
            )
            put(
                "flags",
                "flags",
                f'=IF(TRIM(IF({c["infeasible"]},"INFEASIBLE ","")&IF({c["unstable"]},"RECOVERY_UNSTABLE ","")'
                f'&IF({c["not_converged"]},"NOT_CONVERGED",""))="","none",SUBSTITUTE(TRIM(IF({c["infeasible"]},'
                f'"INFEASIBLE ","")&IF({c["unstable"]},"RECOVERY_UNSTABLE ","")&IF({c["not_converged"]},'
                f'"NOT_CONVERGED",""))," ",", "))',
                "",
            )
            put(
                "binding",
                "binding constraint (reported)",
                f'=IF({c["unstable"]},"recovery",{c["binding_h"]})',
                "recovery when the backlog is growing",
            )
            put(
                "util_crew",
                "crew utilization",
                f'=IF({c["crew_h"]}=0,"{NA}",{c["done_h"]}/{c["crew_h"]})',
                "",
            )
            put(
                "util_vehicle",
                "vehicle utilization",
                f'=IF({c["vehicle_h"]}=0,"{NA}",{c["done_h"]}/{c["vehicle_h"]})',
                "",
            )
            put(
                "util_robot",
                "robot utilization",
                f'=IF({c["robot_h"]}=0,"{NA}",{c["done_h"]}/{c["robot_h"]})',
                "",
            )
            blocks.append(block)
            row += 2
    _widths(ws, {get_column_letter(i): 13 for i in range(1, len(WEEK_COLUMNS) + 1)})
    _widths(ws, {"A": 20, "B": 34, "C": 16, "D": 44})
    ws.freeze_panes = "A4"
    return blocks


def _table(ws: Worksheet, columns: tuple[tuple[str, str], ...]) -> dict[str, str]:
    _bold(ws, TABLE_HEADER_ROW, [h for _, h in columns])
    ws.freeze_panes = f"B{TABLE_HEADER_ROW + 1}"
    for i in range(1, len(columns) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 16
    return {k: get_column_letter(i + 1) for i, (k, _) in enumerate(columns)}


def _costs(ws: Worksheet, blocks: list[EngineBlock]) -> dict[str, int]:
    _title(
        ws,
        "Costs - SCU per month, configured team (contract section 6; SYNTHETIC currency)",
        "Live formulas on Engine. Ops leads = configured leads. N/A when nothing is completed.",
    )
    col = _table(ws, COSTS_COLUMNS)
    rows: dict[str, int] = {}
    for i, b in enumerate(blocks):
        row = TABLE_HEADER_ROW + 1 + i
        rows[b.case] = row
        e = b.ref
        x = {k: f"{col[k]}{row}" for k in col}
        cells: dict[str, CellValue] = {
            "case": b.case,
            "fleet": f"={e('fleet')}",
            "assumption": b.assumption,
            "ops_leads": f"={e('leads')}",
            "labor": f"={e('crews')}*{e('crew_size')}*{e('op_cost')}+{e('engineers')}*{e('eng_cost')}"
            f"+{x['ops_leads']}*{e('lead_cost')}",
            "km_visit": f"=2*{e('travel')}/60*{e('speed')}",
            "vehicles": f"={e('vehicles')}*{e('veh_fixed')}+{e('done_h')}*(52/12)*{x['km_visit']}*{e('cost_km')}",
            "maintenance": f"={e('fleet')}*{e('allowance')}",
            "overhead": f"={e('overhead')}",
            "total": f"={x['labor']}+{x['vehicles']}+{x['maintenance']}+{x['overhead']}",
            "completed_month": f"={e('done_h')}*(52/12)",
            "per_deployment": f'=IF({e("done_h")}=0,"{NA}",{x["total"]}/({e("done_h")}*(52/12)))',
            "span_leads": f"=IF({e('span')}>0,ROUNDUP({e('crews')}/{e('span')},0),{e('crews')})",
            "supervisor_shortfall": f"=MAX(0,{x['span_leads']}-{e('leads')})",
        }
        for k, v in cells.items():
            ws[f"{col[k]}{row}"] = v
    return rows


def _scenarios(
    ws: Worksheet,
    blocks: list[EngineBlock],
    cost_rows: Mapping[str, int],
) -> dict[str, int]:
    _title(
        ws,
        "Scenarios - current team, founder view (SYNTHETIC forecast, not an operating plan)",
        "Live formulas. Results at week H with the CONFIGURED team; recommended staffing is on Staffing.",
    )
    col = _table(ws, SCENARIO_COLUMNS)
    cost_col = {k: get_column_letter(i + 1) for i, (k, _) in enumerate(COSTS_COLUMNS)}
    unknown = list(unknown_keys())
    rows: dict[str, int] = {}
    for i, b in enumerate(blocks):
        row = TABLE_HEADER_ROW + 1 + i
        rows[b.case] = row
        e = b.ref

        def cost(k: str, _r: int = cost_rows[b.case]) -> str:
            return f"=Costs!{cost_col[k]}{_r}"

        # depends-on-unknown: runtime_limit_min only when it is set (model.depends_on_unknown)
        parts: list[str] = []
        for j, k in enumerate(unknown):
            sep = ", " if j else ""
            if k == "runtime_limit_min":
                parts.append(f'IF(ISNUMBER({e("runtime")}),"{sep}{k}","")')
            else:
                parts.append(f'"{sep}{k}"')
        cells: dict[str, CellValue] = {
            "case": b.case,
            "fleet": f"={e('fleet')}",
            "assumption": b.assumption,
            "demand": f"={e('demand')}",
            "done": f"={e('done_h')}",
            "unmet": f"={e('unmet_h')}",
            "binding": f"={e('binding')}",
            "flags": f"={e('flags')}",
            "robot": f"={e('robot_h')}",
            "crew": f"={e('crew_h')}",
            "vehicle": f"={e('vehicle_h')}",
            "inspection": f"={e('inspection_h')}",
            "util_crew": f"={e('util_crew')}",
            "util_vehicle": f"={e('util_vehicle')}",
            "util_robot": f"={e('util_robot')}",
            "labor": cost("labor"),
            "vehicles": cost("vehicles"),
            "maintenance": cost("maintenance"),
            "overhead": cost("overhead"),
            "total": cost("total"),
            "per_deployment": cost("per_deployment"),
            "supervisor_shortfall": cost("supervisor_shortfall"),
            "depends_on_unknown": "=" + "&".join(parts),
        }
        for k, v in cells.items():
            ws[f"{col[k]}{row}"] = v
    ws.column_dimensions[col["depends_on_unknown"]].width = 90
    return rows


def _na(v: float | None) -> float | str:
    return NA if v is None else v


def _staffing(ws: Worksheet, resolved: Mapping[Assumption, Mapping[str, float | None]]) -> None:
    _title(
        ws,
        f"Staffing - recommended staffing, {PYTHON_LABEL}",
        "Deterministic bounded greedy search (contract section 5): a heuristic, not a global optimum. "
        "Ops leads = ceil(crews / span). SYNTHETIC.",
    )
    col = _table(ws, STAFFING_COLUMNS)
    plans: dict[tuple[Assumption, int], M.StaffingPlan] = {}
    row = TABLE_HEADER_ROW + 1
    for fleet in FLEET_SIZES:
        for a in ASSUMPTIONS:
            v = resolved[a]
            plan = M.recommend(v, fleet)
            plans[(a, fleet)] = plan
            res = plan.result
            cells: dict[str, CellValue] = {
                "case": case_id(fleet, a),
                "fleet": fleet,
                "assumption": a,
                "demand": res.demand,
                "target": M.robot_limited_target(v, fleet),
                "status": plan.status,
                "reason": plan.reason,
                "crews": plan.team.crews,
                "vehicles": plan.team.vehicles,
                "engineers": plan.team.engineers,
                "stations": plan.team.stations,
                "ops_leads": plan.team.ops_leads,
                "done": res.done,
                "unmet": res.unmet,
                "binding": res.binding,
                "flags": ", ".join(res.flags) or "none",
                "total": res.costs.total,
                "per_deployment": _na(res.costs.per_deployment),
                "steps": " | ".join(plan.steps) or "none (configured team already meets the goal)",
            }
            for k, val in cells.items():
                ws[f"{col[k]}{row}"] = val
            row += 1
    # hiring and vehicle triggers: the fleet step at which each count first increases
    row += 1
    ws.cell(row=row, column=1, value="Hiring and vehicle triggers").font = Font(bold=True)
    ws.cell(
        row=row,
        column=3,
        value="fleet step at which each recommended count first rises above the "
        "configured team (or the previous fleet step); 'none' if it never does in 5-50",
    )
    row += 1
    resources = ("crews", "vehicles", "engineers", "stations", "ops_leads")
    _bold(ws, row, ["assumption", *resources])
    row += 1
    for a in ASSUMPTIONS:
        ws.cell(row=row, column=1, value=a)
        cfg = M.configured_team(resolved[a])
        for j, res_name in enumerate(resources, start=2):
            prev = getattr(cfg, res_name)
            trigger: CellValue = "none"
            for fleet in FLEET_SIZES:
                count = getattr(plans[(a, fleet)].team, res_name)
                if count > prev:
                    trigger = fleet
                    break
                prev = count
            ws.cell(row=row, column=j, value=trigger)
        row += 1
    ws.column_dimensions[col["reason"]].width = 50
    ws.column_dimensions[col["steps"]].width = 120


def _sensitivity(ws: Worksheet, resolved: Mapping[Assumption, Mapping[str, float | None]]) -> None:
    _title(
        ws,
        f"Sensitivity - each input -20% / +20%, {PYTHON_LABEL}",
        "Ranked by incremental completed deployments, then cost per deployment. Feasible perturbations "
        "(probabilities clamped, integers rounded). Unknown inputs flagged. SYNTHETIC.",
    )
    col = _table(ws, SENSITIVITY_COLUMNS)
    row = TABLE_HEADER_ROW + 1
    for fleet in FLEET_SIZES:
        for a in ASSUMPTIONS:
            for staffing in ("configured", "recommended"):
                for rank, s in enumerate(M.sensitivity(resolved[a], fleet, staffing=staffing), start=1):
                    cells: dict[str, CellValue] = {
                        "case": case_id(fleet, a),
                        "fleet": fleet,
                        "assumption": a,
                        "staffing": staffing,
                        "rank": rank,
                        "key": s.key,
                        "unknown": "yes" if s.unknown else "no",
                        "low_value": _value_cell(s.low_value),
                        "high_value": _value_cell(s.high_value),
                        "done_low": s.done_low,
                        "done_high": s.done_high,
                        "delta_done_low": s.delta_done_low,
                        "delta_done_high": s.delta_done_high,
                        "cost_low": _na(s.cost_low),
                        "cost_high": _na(s.cost_high),
                    }
                    for k, val in cells.items():
                        ws[f"{col[k]}{row}"] = val
                    row += 1
    ws.column_dimensions[col["key"]].width = 34


_BASELINE_NOTES: tuple[tuple[str, str], ...] = (
    (
        "inspect / load / unload / on_site (planned)",
        "median planned reservation length per purpose, departed visits in the window; PLANNED, not observed; "
        "n >= 10 reservations",
    ),
    ("on_site (observed)", "median session start -> stop (Slice 4 session events); n >= 10 sessions"),
    ("travel (observed)", "median departure -> arrived_site; aborted-outbound runs excluded; n >= 10 trips"),
    (
        "incidents_per_visit",
        "runs with >= 1 incident / departures in the window (observed rate); n >= 30 departures",
    ),
    (
        "observed hold duration (informational)",
        "Kaplan-Meier median of incident-hold duration, open holds right-censored at the window end; never used "
        "to set hold_min_days (a policy assumption)",
    ),
    (
        "fallback",
        "below the sample rule or without rows: the registry fallback value is used and named in the report",
    ),
    ("label", "synthetic demo measurement, not real-world performance"),
)


def _provenance(ws: Worksheet, resolved: Mapping[Assumption, Mapping[str, float | None]]) -> None:
    _title(
        ws,
        "Provenance - where every input comes from (SYNTHETIC)",
        "Registry: capacity/inputs.py (frozen contract section 2). 'override' = the value used here differs "
        "from the registry (e.g. a platform baseline measurement).",
    )
    header = ["key", "provenance", "source", "notes", "override (assumptions)", "unknown company data"]
    _bold(ws, TABLE_HEADER_ROW, header)
    row = TABLE_HEADER_ROW + 1
    for inp in REGISTRY:
        overrides = [a for a in ASSUMPTIONS if resolved[a][inp.key] != inp.value(a)]
        fill = PatternFill("solid", fgColor=PROVENANCE_FILL[inp.provenance])
        vals = [
            inp.key,
            inp.provenance,
            inp.source,
            inp.notes,
            ", ".join(overrides) or "none (registry value)",
            "yes - illustrative placeholder" if inp.provenance == "unknown_company_data" else "no",
        ]
        for c, v in enumerate(vals, start=1):
            ws.cell(row=row, column=c, value=v).fill = fill
        row += 1
    row += 1
    ws.cell(
        row=row, column=1, value="Platform baseline sources (capacity.baseline, contract section 8)"
    ).font = Font(bold=True)
    row += 1
    for metric, note in _BASELINE_NOTES:
        ws.cell(row=row, column=1, value=metric)
        ws.cell(row=row, column=3, value=note)
        row += 1
    _widths(ws, {"A": 40, "B": 22, "C": 70, "D": 50, "E": 24, "F": 28})


# ---------------------------------------------------------------------------------------------- entry point


def build_workbook(path: str | Path, *, values_by_assumption: ValuesByAssumption | None = None) -> Path:
    """Write the capacity workbook to `path` and return it.

    `values_by_assumption` overrides registry values per assumption (e.g. platform baseline measurements); the
    Inputs sheet shows the values used and Provenance marks every override.
    """
    resolved = _resolve(values_by_assumption)
    n_weeks = horizon_rows(resolved)
    wb = Workbook()
    sheets = {name: (wb.active if i == 0 else wb.create_sheet()) for i, name in enumerate(SHEETS)}
    for name, ws in sheets.items():
        assert ws is not None
        ws.title = name
    ws_of = {name: ws for name, ws in sheets.items() if ws is not None}
    _readme(ws_of["README"], n_weeks)
    input_rows = _inputs(ws_of["Inputs"], resolved)
    blocks = _engine(ws_of["Engine"], input_rows, n_weeks)
    cost_rows = _costs(ws_of["Costs"], blocks)
    _scenarios(ws_of["Scenarios"], blocks, cost_rows)
    _staffing(ws_of["Staffing"], resolved)
    _sensitivity(ws_of["Sensitivity"], resolved)
    _provenance(ws_of["Provenance"], resolved)
    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


SOFFICE_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
)


def find_soffice() -> str | None:
    """LibreOffice's `soffice` (env CAPACITY_SOFFICE, PATH, then the usual install locations), or None."""
    explicit = os.environ.get("CAPACITY_SOFFICE")
    if explicit:
        return explicit if Path(explicit).exists() else None
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    return next((c for c in SOFFICE_CANDIDATES if Path(c).exists()), None)


def recalculate(
    path: str | Path, outdir: str | Path, *, soffice: str | None = None, timeout: int = 300
) -> Path:
    """Recalculate `path` with LibreOffice headless into `outdir` (same file name) and return the copy.

    A throwaway user profile keeps concurrent runs on a shared host from colliding. The copy carries the
    formulas and their freshly computed cached values.
    """
    exe = soffice or find_soffice()
    if exe is None:
        raise FileNotFoundError("LibreOffice soffice not found (set CAPACITY_SOFFICE)")
    src = Path(path).resolve()
    out = Path(outdir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="capacity_lo_")
    try:
        subprocess.run(
            [
                exe,
                f"-env:UserInstallation=file://{profile}",
                "--headless",
                "--norestore",
                "--calc",
                "--convert-to",
                "xlsx:Calc MS Excel 2007 XML",
                "--outdir",
                str(out),
                str(src),
            ],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    result = out / src.name
    if not result.exists():
        raise RuntimeError(f"LibreOffice did not write {result}")
    return result


if __name__ == "__main__":  # pragma: no cover - manual generation: python -m capacity.workbook [path]
    print(build_workbook(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH))
