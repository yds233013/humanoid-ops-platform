"""Slice 6 capacity workbook (contract docs/06_scaling_capacity_contract.md rev 4, section 9).

Two layers:
* generator unit tests (no LibreOffice): sheets, formulas, provenance colours, allowed functions, labelled values;
* reconciliation (marker `xlsx`, skipped when soffice is missing): recalculate with LibreOffice headless and check
  every founder-facing output against the authoritative Python model for all 12 cases.

Everything is SYNTHETIC.
"""

import math
import re
import time

import openpyxl
import pytest

from capacity import model as M
from capacity import workbook as W
from capacity.inputs import ASSUMPTIONS, FLEET_SIZES, REGISTRY, values

SOFFICE = W.find_soffice()
needs_soffice = pytest.mark.skipif(SOFFICE is None, reason="LibreOffice soffice not installed")
ERROR_VALUES = {"#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#NULL!", "#SPILL!", "#CALC!"}
CASES = [(n, a) for n in FLEET_SIZES for a in ASSUMPTIONS]
INT_OUTPUTS = {"demand", "done", "unmet", "robot", "crew", "vehicle", "inspection", "supervisor_shortfall"}
TEXT_OUTPUTS = {"case", "assumption", "binding", "flags", "depends_on_unknown"}
STRESS = {  # exercises the branches the registry values do not: unstable recovery, infeasible vehicle, k = 2
    "conservative": {"configured_engineers": 0},
    "base": {"robots_per_visit": 2, "vehicle_robot_capacity": 1},
    "optimistic": {"working_day_min": 600, "charge_between_visits_min": 30, "runtime_limit_min": 200},
}


# ------------------------------------------------------------------------------------------------ helpers


def _resolved(overrides=None):
    out = {}
    for a in ASSUMPTIONS:
        v = values(a)
        if overrides and a in overrides:
            v.update(overrides[a])
        out[a] = v
    return out


def _table(ws, columns):
    """Rows of a Costs/Scenarios/Staffing/Sensitivity-style sheet keyed by the column keys."""
    keys = [k for k, _ in columns]
    headers = [c.value for c in ws[W.TABLE_HEADER_ROW]][: len(keys)]
    assert headers == [h for _, h in columns]
    rows = []
    for r in ws.iter_rows(min_row=W.TABLE_HEADER_ROW + 1, values_only=True):
        if r[0] is None:
            break
        rows.append(dict(zip(keys, r, strict=False)))
    return rows


def _engine_blocks(ws):
    """{case: {"cells": {key: value}, "weeks": [{col key: value}]}} parsed from the Engine sheet's key column."""
    blocks: dict[str, dict] = {}
    cur = None
    week_keys = [k for k, _ in W.WEEK_COLUMNS]
    for r in ws.iter_rows(min_row=4, values_only=True):
        a = r[0]
        if a == "case":
            cur = {"cells": {}, "weeks": []}
            blocks[r[2]] = cur
        elif cur is None or a is None or a in ("key", "week t"):
            continue
        elif isinstance(a, int | float) and not isinstance(a, bool):
            cur["weeks"].append(dict(zip(week_keys, r, strict=False)))
        else:
            cur["cells"][a] = r[2]
    return blocks


def _close(got, want, *, integer=False):
    """Contract 9: integers exactly; money/rates relative 1e-9 (absolute 1e-6 near zero); N/A for None."""
    if want is None:
        return got == W.NA, 0.0, 0.0
    if isinstance(want, bool) or isinstance(want, str):
        return got == want, 0.0, 0.0
    if not isinstance(got, int | float) or isinstance(got, bool):
        return False, math.inf, math.inf
    if integer:
        return float(got) == float(want) and float(got).is_integer(), 0.0, 0.0
    diff = abs(float(got) - float(want))
    rel = diff / abs(want) if want else (0.0 if diff == 0 else math.inf)
    ok = diff <= 1e-6 if abs(want) < 1e-3 else rel <= 1e-9
    return ok, diff, rel


def _expected_scenario(v, n):
    res = M.current(v, n)
    return {
        "demand": res.demand,
        "done": res.done,
        "unmet": res.unmet,
        "binding": res.binding,
        "flags": ", ".join(res.flags) or "none",
        "robot": res.caps["robot"],
        "crew": res.caps["crew"],
        "vehicle": res.caps["vehicle"],
        "inspection": res.caps["inspection"],
        "util_crew": res.utilization["crew"],
        "util_vehicle": res.utilization["vehicle"],
        "util_robot": res.utilization["robot"],
        "labor": res.costs.labor,
        "vehicles": res.costs.vehicles,
        "maintenance": res.costs.maintenance,
        "overhead": res.costs.overhead,
        "total": res.costs.total,
        "per_deployment": res.costs.per_deployment,
        "supervisor_shortfall": res.supervisor_shortfall,
        "depends_on_unknown": ", ".join(res.depends_on_unknown),
    }


def _all_formulas(wb):
    for ws in wb:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    yield ws.title, c.coordinate, c.value


def _reconcile(calc_path, overrides=None):
    """Compare Scenarios and Engine of a recalculated workbook with the model; returns (failures, max_abs, max_rel)."""
    wb = openpyxl.load_workbook(calc_path, data_only=True)
    resolved = _resolved(overrides)
    scen = {r["case"]: r for r in _table(wb["Scenarios"], W.SCENARIO_COLUMNS)}
    engine = _engine_blocks(wb["Engine"])
    failures, max_abs, max_rel = [], 0.0, 0.0

    def check(where, got, want, integer=False):
        nonlocal max_abs, max_rel
        ok, d, r = _close(got, want, integer=integer)
        if not ok:
            failures.append(f"{where}: workbook {got!r} != python {want!r}")
        elif math.isfinite(d):
            max_abs, max_rel = max(max_abs, d), max(max_rel, r)

    assert set(scen) == {W.case_id(n, a) for n, a in CASES}
    for n, a in CASES:
        v, case = resolved[a], W.case_id(n, a)
        for k, want in _expected_scenario(v, n).items():
            check(f"Scenarios {case} {k}", scen[case][k], want, integer=k in INT_OUTPUTS)
        # Engine intermediates: cycles, maintenance fraction and every week of the recursion
        cells, weeks = engine[case]["cells"], engine[case]["weeks"]
        cyc = M.cycles(v)
        for k in ("tr", "tc", "tv", "ti"):
            check(f"Engine {case} {k}", cells[k], getattr(cyc, k))
        for k in ("v_robot", "v_crew", "v_vehicle", "v_inspection"):
            check(f"Engine {case} {k}", cells[k], getattr(cyc, k), integer=True)
        for k in ("robot", "crew", "vehicle", "inspection"):
            check(f"Engine {case} {k}_infeasible", cells[f"{k}_infeasible"], k in cyc.infeasible)
        check(f"Engine {case} m", cells["m"], M.maintenance_fraction(v))
        res = M.current(v, n)
        assert len(weeks) >= len(res.weeks)
        for wk_py, wk_x in zip(res.weeks, weeks, strict=False):
            t = wk_py.t
            check(f"Engine {case} w{t} t", wk_x["t"], t, integer=True)
            for k in ("available", "holds", "older", "rec_old", "rec_new", "backlog", "unavailable"):
                check(f"Engine {case} w{t} {k}", wk_x[k], getattr(wk_py, k))
            for k in ("robot", "crew", "vehicle", "inspection"):
                check(f"Engine {case} w{t} {k}", wk_x[k], wk_py.caps[k], integer=True)
            for k in ("cap", "done", "unmet"):
                check(f"Engine {case} w{t} {k}", wk_x[k], getattr(wk_py, k), integer=True)
            check(f"Engine {case} w{t} binding", wk_x["binding"], wk_py.binding)
    return failures, max_abs, max_rel


# ---------------------------------------------------------------------------------- generator (no soffice)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    return W.build_workbook(tmp_path_factory.mktemp("wb") / "capacity_model.xlsx")


@pytest.fixture(scope="module")
def src_wb(source):
    return openpyxl.load_workbook(source)


def test_sheets_and_default_path(src_wb):
    assert tuple(src_wb.sheetnames) == W.SHEETS
    assert W.DEFAULT_PATH.parts[-2:] == ("build", "capacity_model.xlsx")
    assert src_wb.calculation.fullCalcOnLoad


def test_readme_labels(src_wb):
    text = "\n".join(str(c.value) for row in src_wb["README"].iter_rows() for c in row if c.value)
    for phrase in (
        "SYNTHETIC",
        "FORECAST",
        "NOT a validated company operating plan",
        "Approximations",
        "Excel and Google Sheets compatibility are separate manual checks",
    ):
        assert phrase in text
    for inp in REGISTRY:
        if inp.provenance == "unknown_company_data":
            assert inp.key in text


def test_inputs_rows_values_and_provenance_colours(src_wb):
    ws = src_wb["Inputs"]
    assert [c.value for c in ws[W.TABLE_HEADER_ROW]][: len(W.INPUTS_HEADER)] == list(W.INPUTS_HEADER)
    assert len(set(W.PROVENANCE_FILL.values())) == 3
    for i, inp in enumerate(REGISTRY):
        row = W.INPUTS_FIRST_ROW + i
        assert ws[f"B{row}"].value == inp.key
        assert ws[f"F{row}"].value == inp.provenance
        for a, col in W.VALUE_COLUMN.items():
            want = inp.value(a)
            assert ws[f"{col}{row}"].value == (W.UNCONSTRAINED_TEXT if want is None else want)
        for col in "ABFHIJM":
            assert ws[f"{col}{row}"].fill.fgColor.rgb[-6:] == W.PROVENANCE_FILL[inp.provenance], (
                inp.key,
                col,
            )
    assert ws[f"B{W.INPUTS_FIRST_ROW + len(REGISTRY)}"].value is None  # exactly 38 rows
    assert len(REGISTRY) == 38


def test_formulas_are_live_where_the_contract_says(src_wb):
    counts = {ws.title: 0 for ws in src_wb}
    for sheet, _, _ in _all_formulas(src_wb):
        counts[sheet] += 1
    assert counts["Engine"] > 12 * 26 * 15 and counts["Costs"] >= 12 * 12 and counts["Scenarios"] >= 12 * 20
    for sheet in ("README", "Inputs", "Staffing", "Sensitivity", "Provenance"):
        assert counts[sheet] == 0, sheet
    # every founder-facing Scenarios output is a formula
    ws = src_wb["Scenarios"]
    for r in range(W.TABLE_HEADER_ROW + 1, W.TABLE_HEADER_ROW + 13):
        for i, (k, _) in enumerate(W.SCENARIO_COLUMNS, start=1):
            v = ws.cell(row=r, column=i).value
            if k in ("case", "assumption"):
                assert isinstance(v, str) and not v.startswith("=")
            else:
                assert isinstance(v, str) and v.startswith("="), (r, k)
    # Engine week rows reference the Engine parameter cells which reference Inputs (no literals for inputs)
    engine = [f for s, _, f in _all_formulas(src_wb) if s == "Engine"]
    assert sum("Inputs!" in f for f in engine) == 12 * len(W._PARAMS)
    labels = {c.value for row in src_wb["Staffing"].iter_rows(max_row=2) for c in row}
    assert any(isinstance(x, str) and W.PYTHON_LABEL in x for x in labels)
    labels = {c.value for row in src_wb["Sensitivity"].iter_rows(max_row=2) for c in row}
    assert any(isinstance(x, str) and W.PYTHON_LABEL in x for x in labels)


def test_only_common_functions(src_wb):
    used: set[str] = set()
    for _, _, f in _all_formulas(src_wb):
        body = re.sub(r'"[^"]*"', '""', f)  # ignore string literals
        used |= set(re.findall(r"([A-Z][A-Z0-9.]*)\(", body))
        assert "#" not in body and "{" not in body  # no spill refs, no array constants
    assert used <= W.ALLOWED_FUNCTIONS, used - W.ALLOWED_FUNCTIONS
    forbidden = {
        "MAXIFS",
        "MINIFS",
        "LAMBDA",
        "LET",
        "FILTER",
        "SEQUENCE",
        "XLOOKUP",
        "IFS",
        "SWITCH",
        "CONCAT",
        "TEXTJOIN",
        "UNIQUE",
        "SORT",
        "IFERROR",
    }
    assert not used & forbidden


def test_na_text_for_zero_denominators(src_wb):
    formulas = [f for s, _, f in _all_formulas(src_wb) if s in ("Engine", "Costs")]
    na = [f for f in formulas if f'"{W.NA}"' in f]
    assert len(na) == 12 * 4  # three utilizations + cost per deployment per case


def test_staffing_and_sensitivity_values_match_python(src_wb):
    staffing = {r["case"]: r for r in _table(src_wb["Staffing"], W.STAFFING_COLUMNS)}
    sens = _table(src_wb["Sensitivity"], W.SENSITIVITY_COLUMNS)
    assert len(staffing) == 12 and len(sens) == 12 * 2 * 38
    for n, a in CASES:
        v = values(a)
        plan = M.recommend(v, n)
        row = staffing[W.case_id(n, a)]
        assert (row["crews"], row["vehicles"], row["engineers"], row["stations"], row["ops_leads"]) == (
            plan.team.crews,
            plan.team.vehicles,
            plan.team.engineers,
            plan.team.stations,
            plan.team.ops_leads,
        )
        assert row["status"] == plan.status and row["done"] == plan.result.done
        assert row["target"] == M.robot_limited_target(v, n)
        assert _close(row["total"], plan.result.costs.total)[0]  # openpyxl stores ~16 significant digits
        for staffing_mode in ("configured", "recommended"):
            py = M.sensitivity(v, n, staffing=staffing_mode)
            xl = [r for r in sens if r["case"] == W.case_id(n, a) and r["staffing"] == staffing_mode]
            assert [r["key"] for r in xl] == [s.key for s in py]
            assert [(r["delta_done_low"], r["delta_done_high"]) for r in xl] == [
                (s.delta_done_low, s.delta_done_high) for s in py
            ]


def test_worked_example_staffing_values(src_wb):
    row = {r["case"]: r for r in _table(src_wb["Staffing"], W.STAFFING_COLUMNS)}["5-base"]
    assert (row["crews"], row["vehicles"], row["engineers"], row["stations"], row["ops_leads"]) == (
        6,
        5,
        1,
        1,
        1,
    )
    assert (row["done"], row["unmet"], row["binding"], row["target"]) == (17, 3, "robot", 17)
    assert row["total"] == pytest.approx(124406.44, abs=0.005)
    assert row["per_deployment"] == pytest.approx(1688.78, abs=0.005)


def test_overrides_are_shown_and_marked(tmp_path):
    path = W.build_workbook(tmp_path / "x.xlsx", values_by_assumption={"base": {"inspect_min": 33}})
    wb = openpyxl.load_workbook(path)
    row = W.INPUTS_FIRST_ROW + [i.key for i in REGISTRY].index("inspect_min")
    assert wb["Inputs"][f"I{row}"].value == 33 and wb["Inputs"][f"H{row}"].value == 45
    prov = {
        r[0]: r[4]
        for r in wb["Provenance"].iter_rows(min_row=W.TABLE_HEADER_ROW + 1, values_only=True)
        if r[0]
    }
    assert prov["inspect_min"] == "base" and prov["load_min"] == "none (registry value)"


def _closed_form(tr, w, charge):
    """The Engine v_robot formula, transcribed: IF(Tr>W,0,1+INT((W-Tr)/(Tr+charge))) with the 1e-9 floor."""
    if tr <= 0 or tr > w + 1e-9:
        return 0
    if tr + charge <= 0:
        return 1
    return 1 + math.floor((w - tr) / (tr + charge) + 1e-9)


def _brute_force(tr, w, charge):
    k = 0
    while (k + 1) * tr + k * charge <= w + 1e-9:
        k += 1
    return k


def test_robot_visits_closed_form_equals_model_cycles():
    checked = 0
    for w in (300, 420, 450, 480, 600, 720, 900):
        for charge in (0, 15, 30, 45, 60, 90, 120, 180, 240):
            for on_site in (30, 60, 90, 120, 150, 240):
                for travel in (5, 10, 25, 40, 60):
                    v = values("base")
                    v.update(
                        working_day_min=w,
                        charge_between_visits_min=charge,
                        on_site_min=on_site,
                        travel_one_way_min=travel,
                    )
                    cyc = M.cycles(v)
                    want = _brute_force(cyc.tr, w, charge)
                    assert _closed_form(cyc.tr, w, charge) == cyc.v_robot == want, (w, charge, cyc.tr)
                    checked += 1
    assert checked > 1000


# ------------------------------------------------------------------------------ reconciliation (soffice)


@pytest.fixture(scope="module")
def recalculated(source, tmp_path_factory):
    start = time.perf_counter()
    calc = W.recalculate(source, tmp_path_factory.mktemp("calc"), soffice=SOFFICE)
    elapsed = time.perf_counter() - start
    print(f"\nLibreOffice recalculation: {elapsed:.2f}s")
    return calc


@pytest.mark.xlsx
@needs_soffice
def test_recalculated_file_keeps_formulas_and_has_cached_values(source, recalculated):
    src = list(_all_formulas(openpyxl.load_workbook(source)))
    calc_formulas = openpyxl.load_workbook(recalculated)
    after = {(s, c) for s, c, _ in _all_formulas(calc_formulas)}
    assert {(s, c) for s, c, _ in src} <= after  # formulas preserved through the round trip
    cached = openpyxl.load_workbook(recalculated, data_only=True)
    missing = [(s, c) for s, c, _ in src if cached[s][c].value is None]
    assert not missing, missing[:10]
    print(
        f"formulas: {len(src)} ("
        + ", ".join(f"{name} {sum(1 for s, _, _ in src if s == name)}" for name in W.SHEETS)
        + ")"
    )


@pytest.mark.xlsx
@needs_soffice
def test_no_error_values(recalculated):
    wb = openpyxl.load_workbook(recalculated, data_only=True)
    errors = [
        (ws.title, c.coordinate, c.value)
        for ws in wb
        for row in ws.iter_rows()
        for c in row
        if c.data_type == "e"
        or (isinstance(c.value, str) and (c.value in ERROR_VALUES or c.value.startswith("Err:")))
    ]
    assert not errors, errors[:10]


@pytest.mark.xlsx
@needs_soffice
def test_all_12_cases_reconcile_with_python(recalculated):
    failures, max_abs, max_rel = _reconcile(recalculated)
    print(
        f"\nreconciliation (registry values): 12 cases, max abs diff {max_abs:.3g}, max rel diff {max_rel:.3g}"
    )
    assert not failures, failures[:20]


@pytest.mark.xlsx
@needs_soffice
def test_worked_example_cells(recalculated):
    wb = openpyxl.load_workbook(recalculated, data_only=True)
    block = _engine_blocks(wb["Engine"])["5-base"]
    c, w1, w2 = block["cells"], block["weeks"][0], block["weeks"][1]
    assert (c["tr"], c["tv"], c["ti"]) == (300, 240, 60)
    assert (c["v_robot"], c["v_crew"], c["v_vehicle"], c["v_inspection"]) == (1, 1, 1, 7)
    assert c["hours_month"] == pytest.approx(162.5)
    assert c["m"] == pytest.approx(0.036923, abs=1e-6)
    assert w1["available"] == pytest.approx(4.815385, abs=1e-6)
    assert (w1["robot"], w1["crew"], w1["vehicle"], w1["inspection"], w1["cap"]) == (19, 8, 8, 35, 8)
    assert (w1["binding"], c["demand"], w1["done"], w1["unmet"]) == ("crew", 20, 8, 12)
    assert w1["holds"] == pytest.approx(0.4) and c["jobs_capacity"] == pytest.approx(6.4)
    assert w1["backlog"] == pytest.approx(0) and w1["unavailable"] == pytest.approx(0.16)
    assert w2["available"] == pytest.approx(4.661292, abs=1e-6) and w2["robot"] == 18
    assert (c["done_h"], c["binding"], c["unmet_h"], c["flags"]) == (8, "crew", 12, "none")
    scen = {r["case"]: r for r in _table(wb["Scenarios"], W.SCENARIO_COLUMNS)}["5-base"]
    assert scen["labor"] == pytest.approx(46500)
    assert scen["vehicles"] == pytest.approx(3732.44, abs=0.005)
    assert (scen["maintenance"], scen["overhead"]) == (5000, 12000)
    assert scen["total"] == pytest.approx(67232.44, abs=0.005)
    assert scen["per_deployment"] == pytest.approx(1939.40, abs=0.005)
    cost = {r["case"]: r for r in _table(wb["Costs"], W.COSTS_COLUMNS)}["5-base"]
    assert cost["km_visit"] == pytest.approx(46.667, abs=0.0005)
    assert cost["completed_month"] == pytest.approx(34.67, abs=0.005)


@pytest.mark.xlsx
@needs_soffice
def test_stress_overrides_reconcile_with_python(tmp_path):
    """Branches the registry does not reach: unstable recovery (engineers 0), INFEASIBLE vehicle with r = 2 and
    N/A costs, two robot visits per day with a runtime limit set."""
    resolved = _resolved(STRESS)
    assert any(M.RECOVERY_UNSTABLE in M.current(resolved["conservative"], n).flags for n in FLEET_SIZES)
    assert M.current(resolved["base"], 5).costs.per_deployment is None
    assert M.cycles(resolved["optimistic"]).v_robot == 2
    src = W.build_workbook(tmp_path / "stress.xlsx", values_by_assumption=STRESS)
    calc = W.recalculate(src, tmp_path / "calc", soffice=SOFFICE)
    failures, max_abs, max_rel = _reconcile(calc, STRESS)
    print(
        f"\nreconciliation (stress overrides): 12 cases, max abs diff {max_abs:.3g}, max rel diff {max_rel:.3g}"
    )
    assert not failures, failures[:20]
    scen = {
        r["case"]: r
        for r in _table(openpyxl.load_workbook(calc, data_only=True)["Scenarios"], W.SCENARIO_COLUMNS)
    }
    unstable = [n for n in FLEET_SIZES if M.RECOVERY_UNSTABLE in M.current(resolved["conservative"], n).flags]
    for n in unstable:
        assert scen[W.case_id(n, "conservative")]["binding"] == "recovery"
        assert "RECOVERY_UNSTABLE" in scen[W.case_id(n, "conservative")]["flags"]
    assert scen["5-base"]["per_deployment"] == W.NA and scen["5-base"]["util_vehicle"] == W.NA
    assert "INFEASIBLE" in scen["5-base"]["flags"]
    assert "runtime_limit_min" in scen["5-optimistic"]["depends_on_unknown"]
