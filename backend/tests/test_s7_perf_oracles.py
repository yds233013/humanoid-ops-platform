"""Slice 7 performance changes (EM decision on s7/perf, 2026-11-19), each held to an independent oracle.

1. operations/0005: operations_robot_eligibility takes its periodic reason from the set-based periodic status.
   Oracle: operations/0004's view body, which calls the authoritative readiness_periodic_inspection_status
   once per robot, installed as a temporary view. Every robot's (eligible, reasons) must be equal in both
   modes, on every hand-written eligibility edge case (signer revoked, template / rule withdrawn, NULL
   interval, overdue, production fixtures, ...) and at -1 us / = / +1 us of a due date.
2. EM B2: the rule relation's rs reads the base relations instead of operations_robot_status. Oracle: the same
   relation with the previous rs (ox.RS_FROM_STATUS_VIEW). Every inbox row (code, entity, priority, since,
   message, link, ...) and the count must be equal on random worlds, the edge cases and missing settings, in
   both modes (production blockers are inbox items in production only).
SYNTHETIC data only.
"""

import importlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import connection

from fleet.models import DeploymentSetting, Inspection
from operations import exceptions as ox
from tests import operations_world as ow
from tests.fleet_world import make_periodic_rule, set_setting
from tests.test_s7_readiness import owner_set_submitted_at

pytestmark = pytest.mark.django_db

US = timedelta(microseconds=1)
M4 = importlib.import_module("operations.migrations.0004_s7_eligibility_periodic")
ORACLE_VIEW = "s7_eligibility_function_oracle"


def _install_function_view() -> None:
    head = "CREATE OR REPLACE VIEW operations_robot_eligibility WITH (security_invoker = true) AS"
    assert M4.VIEW_SQL.count(head) == 1
    with connection.cursor() as cur:
        cur.execute(M4.VIEW_SQL.replace(head, f"CREATE OR REPLACE TEMP VIEW {ORACLE_VIEW} AS"))


def _rows(view: str) -> list[tuple[Any, ...]]:
    with connection.cursor() as cur:
        cur.execute(f"SELECT robot_id, robot_code, eligible, reasons FROM {view} ORDER BY robot_id")
        return cur.fetchall()


def _assert_views_equal(label: str) -> int:
    """Both modes; restores the demo_mode setting it found."""
    before = DeploymentSetting.objects.filter(key="demo_mode").values_list("value", flat=True).first()
    n = 0
    for mode in ("true", "false"):
        set_setting("demo_mode", mode)
        new, oracle = _rows("operations_robot_eligibility"), _rows(ORACLE_VIEW)
        assert new == oracle, (label, mode)
        n += len(new)
    if before is None:
        DeploymentSetting.objects.filter(key="demo_mode").delete()
    else:
        set_setting("demo_mode", before)
    return n


def _periodic_reason(robot_code: str) -> list[str]:
    with connection.cursor() as cur:
        cur.execute("SELECT reasons FROM operations_robot_eligibility WHERE robot_code = %s", [robot_code])
        reasons: list[str] = cur.fetchone()[0]
    return [r for r in reasons if "eriodic inspection" in r]


@pytest.mark.parametrize("name,build,expected", ow.EDGE_CASES, ids=[c[0] for c in ow.EDGE_CASES])
def test_set_based_eligibility_view_equals_the_function_view(name, build, expected):
    w = ow.edge_world()
    build(w)
    _install_function_view()
    assert _assert_views_equal(name) >= 2


def test_set_based_eligibility_view_due_boundary_and_state_changes():
    from planning import services as ps
    from planning.models import Qualification

    w = ow.edge_world()
    _install_function_view()
    with connection.cursor() as cur:
        cur.execute("SELECT transaction_timestamp()")
        t: datetime = cur.fetchone()[0]
    rule = make_periodic_rule(w, "P-A", interval_min=60)
    ow._owner_backdate(  # the signers' grants cover the moved inspections
        "planning_qualification", "valid_from", "kind = 'readiness_inspector'", [], 3 * 24 * 60
    )
    checks = 0
    for delta, state in ((-US, "overdue"), (timedelta(0), "overdue"), (US, None)):
        due_at = t + delta
        for insp in Inspection.objects.filter(robot__code="R-01"):
            owner_set_submitted_at(insp.pk, due_at - timedelta(minutes=rule.interval_min or 0))
        checks += _assert_views_equal(f"due_at = now {delta}")
        reason = _periodic_reason("R-01")
        if state == "overdue":
            assert reason and reason[0].startswith("Periodic inspection overdue since"), (delta, reason)
        else:
            assert reason == [], (delta, reason)  # still due: no periodic reason
    # a signer revoked, then the template and the rule withdrawn: every step equal to the function view
    q = Qualification.objects.filter(kind="readiness_inspector", revoked_at__isnull=True).first()
    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    checks += _assert_views_equal("signer revoked")
    from fleet.models import (
        InspectionTemplate,
        InspectionTemplateWithdrawal,
        PeriodicInspectionRule,
        PeriodicInspectionRuleWithdrawal,
    )

    InspectionTemplateWithdrawal.objects.create(
        template=InspectionTemplate.objects.get(pattern__code="P-A", version="T1"),
        withdrawn_by=w.lead,
        reason="SYNTHETIC",
    )
    checks += _assert_views_equal("template withdrawn")
    PeriodicInspectionRuleWithdrawal.objects.create(
        rule=PeriodicInspectionRule.objects.filter(pattern__code="P-A").order_by("-version_no").first(),
        withdrawn_by=w.lead,
        reason="SYNTHETIC",
    )
    checks += _assert_views_equal("rule withdrawn")
    assert _periodic_reason("R-01") == ["Periodic inspection interval unknown for configuration P-A"]
    assert checks >= 10


# ================================================================== EM B2: rs from the base relations
def _relation_with_status_view_rs() -> str:
    m = re.search(
        r"-- Slice 7 perf \(EM B2\): rs .*?\n  WHERE r\.lifecycle <> 'retired'\n\),\n", ox.RULES_SQL, re.S
    )
    assert m is not None
    return ox.RULES_SQL[: m.start()] + ox.RS_FROM_STATUS_VIEW + ox.RULES_SQL[m.end() :]


def _all_rows(sql: str, at: datetime) -> list[tuple[Any, ...]]:
    thresholds = ox.read_thresholds()[0]
    params = {
        **ox._relation_params(at, thresholds),
        "prio": json.dumps(ox.PRIORITY),
        "a_prio": None,
        "a_since": None,
        "a_code": None,
        "a_ref": None,
        "limit": None,
    }
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return [
            tuple(json.dumps(v, sort_keys=True) if isinstance(v, dict) else v for v in r)
            for r in cur.fetchall()
        ]


def _assert_rules_equal(label: str) -> int:
    old_sql = _relation_with_status_view_rs()
    assert old_sql != ox.RULES_SQL and "FROM operations_robot_status" in old_sql
    n = 0
    for mode in ("true", "false"):
        set_setting("demo_mode", mode)
        with connection.cursor() as cur:
            cur.execute("SELECT now()")
            at = cur.fetchone()[0] + timedelta(days=1)
        new, old = _all_rows(ox.RULES_SQL, at), _all_rows(old_sql, at)
        assert new == old, (label, mode)
        assert ox.count_operational_exceptions(at) == len(
            ox.open_exceptions(at, include_readiness=False).items
        )
        n += len(new)
    return n


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_base_relation_rs_equals_status_view_rs_on_random_worlds(seed):
    ow.random_world(seed)
    rows = _assert_rules_equal(f"seed {seed}")
    for key in ("ops_return_grace_min", "ops_periodic_due_lookahead_min"):  # missing configuration
        DeploymentSetting.objects.filter(key=key).delete()
    rows += _assert_rules_equal(f"seed {seed}, settings missing")
    assert rows > 0


@pytest.mark.parametrize("name,build,expected", ow.EDGE_CASES, ids=[c[0] for c in ow.EDGE_CASES])
def test_base_relation_rs_equals_status_view_rs_on_edge_cases(name, build, expected):
    w = ow.edge_world()
    build(w)
    ow.report(w, "R-02")  # an open incident (and its hold) on every edge case
    assert _assert_rules_equal(name) > 0


# ================================================================== operations/0006: close-out stage once
M6 = importlib.import_module("operations.migrations.0006_s7_closeout_stage_once")
CLOSEOUT_ORACLE = "s7_closeout_runs_per_run_oracle"
OPEN_RUNS_PER_ROW = (
    "LEFT JOIN operations_open_run_stage s ON s.run_id = c.run_id"  # the previous OPEN_RUNS_SQL join
)


def _closeout_rows(view: str) -> list[tuple[Any, ...]]:
    with connection.cursor() as cur:
        cur.execute(f"SELECT * FROM {view} ORDER BY run_id")
        return cur.fetchall()


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6])
def test_closeout_view_stage_once_equals_the_per_run_view(seed):
    from operations import closeout as co

    if seed == 0:  # a fixed world with runs at several stages (random worlds may have none)
        w = ow.ops_world("co_", robots=4)
        ow.depart_new(w, "R-02")
        ow.advance(w, ow.depart_new(w, "R-03"), "at_hub")
        ow.advance(w, ow.depart_new(w, "R-04"), "reconciled")
    else:
        ow.random_world(seed)
    head = "CREATE OR REPLACE VIEW operations_closeout_runs WITH (security_invoker = true) AS"
    assert M6.REVERSE_SQL.count(head) == 1
    with connection.cursor() as cur:
        cur.execute(M6.REVERSE_SQL.replace(head, f"CREATE OR REPLACE TEMP VIEW {CLOSEOUT_ORACLE} AS"))
    new, old = _closeout_rows("operations_closeout_runs"), _closeout_rows(CLOSEOUT_ORACLE)
    assert new == old, seed
    if seed == 0:
        assert len(new) == 3
    # OPEN_RUNS_SQL with the stage CTE == the previous per-row join (dated and undated)
    m = re.search(r"-- the open run stage once.*?\nWITH s AS MATERIALIZED \(.*?\)\n", co.OPEN_RUNS_SQL, re.S)
    assert m is not None
    previous = co.OPEN_RUNS_SQL[: m.start()] + co.OPEN_RUNS_SQL[m.end() :]
    previous = previous.replace("LEFT JOIN s ON s.run_id = c.run_id", OPEN_RUNS_PER_ROW)
    with connection.cursor() as cur:
        cur.execute("SELECT (now() AT TIME ZONE 'America/Los_Angeles')::date")
        day = cur.fetchone()[0]
        for params in ({"tz": "America/Los_Angeles", "day": day}, {"tz": None, "day": None}):
            cur.execute(co.OPEN_RUNS_SQL, params)
            a = cur.fetchall()
            cur.execute(previous, params)
            assert a == cur.fetchall(), (seed, params)
