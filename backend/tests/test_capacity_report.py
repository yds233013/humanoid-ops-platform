"""Slice 6 Engineer D: founder report, management command and the read-only /ops/capacity/ page.
Contract docs/06_scaling_capacity_contract.md rev 5, sections 0, 5-7 and 9. SYNTHETIC data only.

Report tests are pure Python; the page and command tests use the database (the page runs inside the operations
snapshot, which must be the outermost transaction, so those tests are transactional). Baseline tests skip until
capacity.baseline (Engineer B) exists.
"""

import csv
import importlib.util
import io
import re
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command
from django.test import Client

from accounts.roles import PERMISSIONS
from capacity import model as M
from capacity import report as R
from capacity.inputs import ASSUMPTIONS, BY_KEY, FLEET_SIZES, REGISTRY, unknown_keys, values
from tests.conftest import make_user

HAS_BASELINE = importlib.util.find_spec("capacity.baseline") is not None
needs_baseline = pytest.mark.skipif(not HAS_BASELINE, reason="capacity.baseline (Engineer B) not merged yet")
FORBIDDEN = re.compile(r"\b(cleared|safe|ready to deploy|guaranteed)\b", re.IGNORECASE)
METRICS_ROLES = sorted(PERMISSIONS["operations.metrics"])
READ_ONLY_ROLES = sorted(PERMISSIONS["operations.read"] - PERMISSIONS["operations.metrics"])
URL = "/ops/capacity/"
approx = pytest.approx


@pytest.fixture(scope="module")
def reports():
    return {a: R.founder_report(a) for a in ASSUMPTIONS}


def base(**overrides):
    v = values("base")
    v.update(overrides)
    return v


def step(report, section_prefix, label):
    hits = [s for s in report.worked_example if s.section.startswith(section_prefix) and s.label == label]
    assert len(hits) == 1, (section_prefix, label, hits)
    return hits[0]


# ------------------------------------------------------------------------------------------- comparison
@pytest.mark.parametrize("assumption", ASSUMPTIONS)
def test_report_numbers_equal_model_compare(reports, assumption):
    report = reports[assumption]
    expected = M.compare(values(assumption))
    assert [row.fleet_size for row in report.comparison] == list(FLEET_SIZES)
    for row, exp in zip(report.comparison, expected, strict=True):
        assert (row.fleet_size, row.demand, row.target) == (exp["fleet_size"], exp["demand"], exp["target"])
        for side, key in ((row.current, "current"), (row.recommended, "recommended")):
            e = exp[key]
            assert {
                "crews": side.crews,
                "vehicles": side.vehicles,
                "engineers": side.engineers,
                "stations": side.stations,
                "ops_leads": side.ops_leads,
            } == e["team"]
            assert (side.done, side.binding, side.unmet, list(side.flags)) == (
                e["done"],
                e["binding"],
                e["unmet"],
                e["flags"],
            )
            assert side.total_cost == e["total_cost"]
            assert side.cost_per_deployment == e["cost_per_deployment"]
            assert side.supervisor_shortfall == e["supervisor_shortfall"]
        assert (row.recommended.status, row.recommended.reason) == (
            exp["recommended"]["status"],
            exp["recommended"]["reason"],
        )
        assert row.current.status == "CURRENT TEAM"


def test_five_robot_base_row_equals_contract_worked_example(reports):
    row = reports["base"].comparison[0]
    c, r = row.current, row.recommended
    assert row.fleet_size == 5 and row.demand == 20 and row.target == 17
    assert (c.crews, c.vehicles, c.engineers, c.stations, c.ops_leads) == (2, 2, 1, 1, 1)
    assert (c.done, c.binding, c.unmet, c.flags) == (8, "crew", 12, ())
    assert round(c.total_cost, 2) == 67232.44 and round(c.cost_per_deployment, 2) == 1939.40
    assert (r.crews, r.vehicles, r.engineers, r.stations, r.ops_leads) == (6, 5, 1, 1, 1)
    assert (r.done, r.binding, r.unmet, r.status) == (17, "robot", 3, "MET")
    assert round(r.total_cost, 2) == 124406.44 and round(r.cost_per_deployment, 2) == 1688.78


def test_worked_example_trace_matches_contract_section_7(reports):
    rep = reports["base"]
    assert step(rep, "1.", "Robot busy per visit Tr (min)").value == 300
    assert step(rep, "1.", "Robot busy per visit Tr (min)").formula == "30+20+2*40+120+20+30"
    assert step(rep, "1.", "Robot visits per day").value == 1
    assert step(rep, "1.", "Crew visits per day").value == 1
    assert step(rep, "1.", "Vehicle busy per visit Tv (min)").value == 240
    assert step(rep, "1.", "Vehicle visits per day").value == 1
    assert step(rep, "1.", "Inspection per robot Ti (min)").value == 60
    assert step(rep, "1.", "Robot-inspections per station per day").value == 7
    assert step(rep, "2.", "Working hours per robot-month").value == approx(162.5)
    assert step(rep, "2.", "Maintenance fraction m").display == "0.036923"
    assert step(rep, "3.", "Available robots A_1").display == "4.815385"
    caps = {
        k: step(rep, "3.", f"{k[0]} ({n} capacity, visits/week)").value
        for k, n in (("R", "robot"), ("C", "crew"), ("V", "vehicle"), ("I", "inspection"))
    }
    assert caps == {"R": 19, "C": 8, "V": 8, "I": 35}
    assert step(rep, "3.", "Binding constraint").value == "crew"
    assert (step(rep, "3.", "Completed").value, step(rep, "3.", "Unmet").value) == (8, 12)
    assert step(rep, "4.", "Holds per week").value == approx(0.4)
    assert step(rep, "4.", "Recovery jobs per week").value == approx(6.4)
    assert step(rep, "4.", "Recovery backlog after week 1").value == 0
    assert step(rep, "4.", "Unavailable robots next week U_1").value == approx(0.16)
    assert step(rep, "5. Week 26", "Available robots").display == "4.661292"
    assert step(rep, "5. Week 26", "R (robot capacity, visits/week)").value == 18
    assert step(rep, "5. Week 26", "Completed per week").value == 8
    assert step(rep, "6.", "Labor").display == "46,500.00"
    assert step(rep, "6.", "Km per visit").display == "46.667"
    assert step(rep, "6.", "Vehicles").display == "3,732.44"
    assert step(rep, "6.", "Maintenance allowance").display == "5,000.00"
    assert step(rep, "6.", "Overhead").display == "12,000.00"
    assert step(rep, "6.", "Total per month").display == "67,232.44"
    assert step(rep, "6.", "Completed per month").display == "34.67"
    assert step(rep, "6.", "Cost per completed deployment").display == "1,939.40"
    assert step(rep, "7.", "Target (completed/week)").value == 17
    greedy = [
        s.value for s in rep.worked_example if s.section.startswith("8.") and s.label.startswith("Step ")
    ]
    assert [g.rsplit("-> ", 1)[1] for g in greedy] == [
        "crews 3",
        "vehicles 3",
        "crews 4",
        "vehicles 4",
        "crews 5",
        "vehicles 5",
        "crews 6",
    ]
    assert step(rep, "8.", "Recommended team").value == (
        "6 crews, 5 vehicles, 1 engineer, 1 station, 1 ops lead"
    )
    assert step(rep, "8.", "C (crew capacity, visits/week)").value == 24
    assert step(rep, "8.", "V (vehicle capacity, visits/week)").value == 20
    assert step(rep, "8.", "R (robot capacity, visits/week)").value == 17
    assert step(rep, "8.", "Binding constraint").value == "robot"
    assert step(rep, "8.", "Total per month (SCU)").display == "124,406.44"
    assert step(rep, "8.", "Cost per completed deployment (SCU)").display == "1,688.78"


# ------------------------------------------------------------------------------------------- triggers
def T(crews, vehicles, engineers, stations, ops_leads):
    return M.Team(crews, vehicles, engineers, stations, ops_leads)


def test_triggers_first_rise_on_hand_made_steps():
    configured = T(2, 2, 1, 1, 1)
    teams = [(5, T(2, 3, 1, 1, 1)), (10, T(4, 3, 1, 1, 1)), (25, T(4, 3, 1, 2, 1)), (50, T(9, 8, 3, 2, 1))]
    got = {t.resource: t.first_rise for t in R.hiring_triggers(configured, teams)}
    assert got == {"crews": 10, "vehicles": 5, "engineers": 50, "stations": 25, "ops_leads": None}
    crews = next(t for t in R.hiring_triggers(configured, teams) if t.resource == "crews")
    assert crews.counts == ((5, 2), (10, 4), (25, 4), (50, 9)) and crews.configured == 2


def test_triggers_compare_with_previous_step_not_only_the_current_team():
    # a count that dips below the current team and then comes back does not "rise" until it exceeds the step
    configured = T(3, 2, 1, 1, 1)
    teams = [(5, T(1, 2, 1, 1, 1)), (10, T(2, 2, 1, 1, 1)), (25, T(2, 2, 1, 1, 1)), (50, T(2, 2, 1, 1, 1))]
    crews = next(t for t in R.hiring_triggers(configured, teams) if t.resource == "crews")
    assert crews.first_rise == 10


def test_base_report_triggers(reports):
    got = {t.resource: t.first_rise for t in reports["base"].triggers}
    assert got == {"crews": 5, "vehicles": 5, "engineers": 50, "stations": 25, "ops_leads": 10}


# ------------------------------------------------------------------------------------------- next constraint
def test_next_constraint_crew_bound_then_vehicle():
    nc = R.next_constraint(base(), 5, T(2, 2, 1, 1, 1))
    assert (nc.constraint, nc.next_constraint, nc.done_before, nc.done_after) == ("crew", "vehicle", 8, 8)


def test_next_constraint_vehicle_bound_then_crew():
    nc = R.next_constraint(base(), 5, T(3, 2, 1, 1, 1))
    assert (nc.constraint, nc.next_constraint, nc.done_before, nc.done_after) == ("vehicle", "crew", 8, 12)


def test_next_constraint_robot_bound_reads_remaining_capacities():
    # recommended 5-robot team: R 17, C 24, V 20, I 35, demand 20 -> demand next, 20 completed
    nc = R.next_constraint(base(), 5, T(6, 5, 1, 1, 1), staffing="recommended")
    assert (nc.constraint, nc.next_constraint, nc.done_before, nc.done_after) == ("robot", "demand", 17, 20)
    # with more demand, vehicles (20) would limit next
    nc = R.next_constraint(base(demand_visits_per_robot_week=6), 5, T(6, 5, 1, 1, 1), staffing="recommended")
    assert (nc.constraint, nc.next_constraint, nc.done_after) == ("robot", "vehicle", 20)


def test_next_constraint_recovery_relieved_by_engineers():
    v = base(horizon_weeks=4)  # no engineers: the backlog is still growing at week 4
    res = M.simulate(v, 5, T(6, 5, 0, 1, 1), leads_mode="span")
    assert res.binding == "recovery"
    nc = R.next_constraint(v, 5, T(6, 5, 0, 1, 1), staffing="recommended")
    assert nc.constraint == "recovery" and "recovery engineers" in nc.relieved_by
    assert nc.next_constraint == "robot" and nc.done_after > nc.done_before


def test_report_reflects_rev5_recovery_classification():
    v = base(configured_engineers=0)
    rows = {r.fleet_size: r for r in R.comparison(v)}
    expected = {e["fleet_size"]: e for e in M.compare(v)}
    for n in (5, 10):
        cur, rec = rows[n].current, rows[n].recommended
        assert cur.binding == expected[n]["current"]["binding"] == "recovery"
        assert "RECOVERY_UNSTABLE" in cur.flags
        assert rec.status == "MET" and rec.engineers >= 1 and "RECOVERY_UNSTABLE" not in rec.flags
        nc = R.next_constraint(v, n, M.configured_team(v))
        assert nc.constraint == "recovery" and "recovery engineers" in nc.relieved_by
    infeasible = {r.fleet_size: r for r in R.comparison(base(on_site_min=500, configured_engineers=0))}[5]
    assert "INFEASIBLE" in infeasible.current.flags and "RECOVERY_UNSTABLE" not in infeasible.current.flags
    assert infeasible.recommended.status == "UNATTAINABLE" and infeasible.recommended.engineers == 0


def test_next_constraint_when_demand_is_met():
    nc = R.next_constraint(base(demand_visits_per_robot_week=1), 5, T(2, 2, 1, 1, 1))
    assert (nc.constraint, nc.next_constraint, nc.done_before, nc.done_after) == ("demand", "crew", 5, 5)


# ------------------------------------------------------------------------------------------- levers, inputs
def test_levers_follow_model_sensitivity_and_add_completed_deployments(reports):
    rep = reports["base"]
    for n in FLEET_SIZES:
        for staffing, mode in (("current", "configured"), ("recommended", "recommended")):
            levers = [lv for lv in rep.levers if lv.fleet_size == n and lv.staffing == staffing]
            assert 1 <= len(levers) <= R.TOP_LEVERS
            assert [lv.rank for lv in levers] == list(range(1, len(levers) + 1))
            ranked = [
                row.key
                for row in M.sensitivity(values("base"), n, staffing=mode)
                if row.key not in R.NOT_LEVERS and max(row.delta_done_low, row.delta_done_high) > 0
            ]
            assert [lv.key for lv in levers] == ranked[: R.TOP_LEVERS]
            assert all(lv.delta_done > 0 and lv.done_after - lv.done_before == lv.delta_done for lv in levers)
            assert [lv.delta_done for lv in levers] == sorted((lv.delta_done for lv in levers), reverse=True)
    assert not any(lv.key in R.NOT_LEVERS for lv in rep.levers)


@pytest.mark.parametrize("assumption", ASSUMPTIONS)
def test_unknown_inputs_listed(reports, assumption):
    rep = reports[assumption]
    keys = [u.key for u in rep.unknown_inputs]
    assert keys == [
        "charge_between_visits_min",
        "maintenance_hours_per_robot_month",
        "demand_visits_per_robot_week",
        "maintenance_allowance_robot_month",
    ]
    for row in M.compare(values(assumption)):
        for side in ("current", "recommended"):
            assert set(row[side]["depends_on_unknown"]) <= set(keys)
    assert set(keys) <= set(unknown_keys())
    md = R.render_markdown(rep)
    for k in keys:
        assert f"**UNKNOWN** {BY_KEY[k].label} (`{k}`)" in md
    # a runtime limit, once set, is an unknown the results depend on too
    v = values(assumption)
    v["runtime_limit_min"] = 600
    assert "runtime_limit_min" in [u.key for u in R.founder_report(assumption, values=v).unknown_inputs]


def test_provenance_table_covers_every_input(reports):
    rep = reports["base"]
    assert [p.key for p in rep.provenance] == [i.key for i in REGISTRY]
    for p in rep.provenance:
        assert p.value_used == p.registry_value == BY_KEY[p.key].base
        assert p.provenance == BY_KEY[p.key].provenance
    if not HAS_BASELINE:
        assert rep.baseline.status == R.BASELINE_UNAVAILABLE
        pm = [p for p in rep.provenance if p.provenance == "platform_measured"]
        assert pm and all("platform baseline not available" in p.value_source for p in pm)
    override = R.founder_report("base", values=base(inspect_min=25))
    row = next(p for p in override.provenance if p.key == "inspect_min")
    assert row.value_used == 25 and "caller-supplied" in row.value_source


def test_takeaways_come_from_the_numbers(reports):
    text = "\n".join(reports["base"].takeaways)
    assert (
        "5 robots: the current team (2 crews, 2 vehicles, 1 engineer, 1 station, 1 ops lead) completes 8 of 20"
        in text
    )
    assert "cost per completed deployment 1,939.40 -> 1,688.78 SCU" in text
    assert "field crews from 5 robots (2 -> 6)" in text
    assert "recovery engineers from 50 robots (1 -> 2)" in text
    assert "3 visits/week at 5 robots" in text
    assert "4 unknown company inputs" in text
    assert "not a company operating plan" in text


def test_invalid_assumption_is_refused():
    with pytest.raises(ValueError):
        R.founder_report("aggressive")


# ------------------------------------------------------------------------------------------- rendering
def test_markdown_content(reports):
    md = R.render_markdown(reports["base"])
    assert md.startswith("# Capacity forecast: 5 / 10 / 25 / 50 robots (base assumptions)")
    assert R.BANNER in md
    for heading in (
        "## Founder takeaways",
        "## Current team vs recommended staffing",
        "## Hiring and vehicle triggers",
        "## Next constraint after relieving the current one",
        "## Top 5 levers",
        "## Worked example: 5 robots",
        "## Unknown company inputs",
        "## Input provenance",
    ):
        assert heading in md
    assert (
        "| 5 | current | 2 | 2 | 1 | 1 | 1 | 8 | 20 | 12 | crew | - | 67,232.44 | 1,939.40 | CURRENT TEAM |"
        in md
    )
    assert (
        "| 5 | recommended | 6 | 5 | 1 | 1 | 1 | 17 | 20 | 3 | robot | - | 124,406.44 | 1,688.78 | MET |"
        in md
    )
    assert not FORBIDDEN.search(md), FORBIDDEN.search(md)


@pytest.mark.parametrize("assumption", ASSUMPTIONS)
def test_csv_content(reports, assumption):
    rep = reports[assumption]
    files = R.render_csv(rep)
    assert set(files) == {
        "comparison.csv",
        "triggers.csv",
        "next_constraints.csv",
        "levers.csv",
        "worked_example.csv",
        "unknown_inputs.csv",
        "provenance.csv",
    }
    rows = list(csv.DictReader(io.StringIO(files["comparison.csv"])))
    assert len(rows) == 2 * len(FLEET_SIZES)
    for row in rep.comparison:
        for name, side in (("current", row.current), ("recommended", row.recommended)):
            got = next(r for r in rows if r["fleet_size"] == str(row.fleet_size) and r["staffing"] == name)
            assert int(got["completed_per_week"]) == side.done and got["binding"] == side.binding
            assert float(got["monthly_total_scu"]) == approx(side.total_cost, abs=0.005)
            assert float(got["cost_per_completed_deployment_scu"]) == approx(
                side.cost_per_deployment, abs=0.005
            )
    prov = list(csv.DictReader(io.StringIO(files["provenance.csv"])))
    assert len(prov) == len(REGISTRY)
    unknown = list(csv.DictReader(io.StringIO(files["unknown_inputs.csv"])))
    assert [u["key"] for u in unknown] == [u.key for u in rep.unknown_inputs]
    for text in files.values():
        assert not FORBIDDEN.search(text)


def test_base_csv_five_robot_rows(reports):
    rows = list(csv.DictReader(io.StringIO(R.render_csv(reports["base"])["comparison.csv"])))
    cur, rec = rows[0], rows[1]
    assert (cur["monthly_total_scu"], cur["cost_per_completed_deployment_scu"]) == ("67232.44", "1939.40")
    assert (rec["monthly_total_scu"], rec["cost_per_completed_deployment_scu"]) == ("124406.44", "1688.78")


# ------------------------------------------------------------------------------------------- baseline
@needs_baseline
@pytest.mark.django_db(transaction=True)
def test_baseline_section_when_available():
    from operations.snapshot import read_snapshot

    with read_snapshot() as at:
        vals, info = R.load_baseline("base", at, start=at - timedelta(days=28), end=at)
    assert info.status != R.BASELINE_UNAVAILABLE and info.window
    rep = R.founder_report("base", values=vals, baseline=info)
    assert "Baseline: platform baseline applied" in R.render_markdown(rep)


def test_baseline_unavailable_uses_registry(monkeypatch):
    monkeypatch.setattr(R, "baseline_available", lambda: False)
    at = datetime(2026, 11, 18, tzinfo=UTC)
    vals, info = R.load_baseline("base", at, start=at - timedelta(days=28), end=at)
    assert vals == values("base") and info.status == "platform baseline not available"
    md = R.render_markdown(R.founder_report("base", values=vals, baseline=info))
    assert "Baseline: platform baseline not available." in md


# ------------------------------------------------------------------------------------------- command
@pytest.mark.django_db(transaction=True)
def test_management_command_writes_files(tmp_path):
    out = io.StringIO()
    call_command("capacity_report", "--assumption", "base", "--out", str(tmp_path / "cap"), stdout=out)
    folder = tmp_path / "cap"
    names = {p.name for p in folder.iterdir()}
    assert {
        "report.md",
        "comparison.csv",
        "triggers.csv",
        "next_constraints.csv",
        "levers.csv",
        "worked_example.csv",
        "unknown_inputs.csv",
        "provenance.csv",
    } <= names
    md = (folder / "report.md").read_text()
    assert "1,939.40" in md and "1,688.78" in md and R.BANNER in md
    if importlib.util.find_spec("capacity.workbook") is None:
        assert "workbook skipped" in out.getvalue()
    else:
        assert "capacity_model.xlsx" in names


# ------------------------------------------------------------------------------------------- page
def client(*roles, name=None, expired=False):
    c = Client()
    c.force_login(make_user(name or "u_" + "_".join(roles or ("none",)), *roles, expired=expired))
    return c


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("role", METRICS_ROLES)
def test_page_200_for_metrics_roles(role):
    r = client(role).get(URL)
    assert r.status_code == 200
    body = r.content.decode()
    assert "Capacity forecast" in body and "As of <b>" in body
    assert 'data-fleet="5" data-staffing="current"' in body
    assert "67,232.44" in body and "124,406.44" in body and "1,939.40" in body and "1,688.78" in body
    for key in (
        "charge_between_visits_min",
        "maintenance_hours_per_robot_month",
        "demand_visits_per_robot_week",
        "maintenance_allowance_robot_month",
    ):
        assert f'class="unknown" data-unknown="{key}"' in body
    assert not FORBIDDEN.search(body), FORBIDDEN.search(body)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("assumption", ASSUMPTIONS)
def test_page_assumption_selector_shows_that_report(assumption):
    r = client("founder").get(URL, {"assumption": assumption})
    assert r.status_code == 200
    body = r.content.decode()
    assert f'data-assumption="{assumption}"' in body
    assert f'<option value="{assumption}" selected>' in body
    expected = R.founder_report(assumption).comparison[-1].recommended
    assert R.money(expected.total_cost) in body
    assert "5 robots: the current team (" in body and "not a company operating plan" in body
    assert not FORBIDDEN.search(body)


@pytest.mark.django_db(transaction=True)
def test_page_bad_assumption_is_400_without_data():
    r = client("founder").get(URL, {"assumption": "nope"})
    assert r.status_code == 400
    body = r.content.decode()
    assert "assumption must be one of" in body and "Capacity forecast" in body
    assert 'data-table="comparison"' not in body


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "roles", [(), ("admin",), *[(r,) for r in READ_ONLY_ROLES]], ids=lambda r: "-".join(r) or "none"
)
def test_page_403_and_no_data_without_operations_metrics(roles):
    r = client(*roles).get(URL)
    assert r.status_code == 403
    body = r.content.decode()
    assert "<table" not in body and "SCU" not in body and "As of" not in body


@pytest.mark.django_db(transaction=True)
def test_page_403_for_anonymous_and_expired():
    r = Client().get(URL)  # v2 01A: signed-out browsers are redirected to login instead of a bare 403
    assert r.status_code == 302 and r["Location"].startswith("/login/?next=")
    assert "<table" not in r.content.decode()
    assert client("founder", name="u_expired", expired=True).get(URL).status_code == 403


@pytest.mark.django_db(transaction=True)
def test_page_is_get_only():
    c = client("founder")
    for method in (c.post, c.put, c.delete):
        assert method(URL).status_code == 405


def test_registry_and_report_wording_have_no_forbidden_words(reports):
    for rep in reports.values():
        for text in (*rep.takeaways, *rep.notes, *(s.display for s in rep.worked_example)):
            assert not FORBIDDEN.search(text)
    for inp in REGISTRY:
        assert not FORBIDDEN.search(f"{inp.label} {inp.source} {inp.notes}")
