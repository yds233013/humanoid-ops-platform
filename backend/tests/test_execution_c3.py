"""Slice 4 contract C3 (integration lead): a robot can be authorized and depart only from the hub (baseline: no
custody events and no runs ever; or a verified hub receipt) and never while it has an open execution run. Checked
by the evaluator (ROBOT_NOT_AT_HUB), the authorization insert trigger and the departure trigger; the SQL side is
covered by tests/test_execution_db.py, this file covers the command and evaluator paths."""

import pytest

from execution.custody import current_custody, departure_problem, open_run_id
from execution.models import ExecutionRun
from fleet.models import Robot
from planning.models import Visit
from platform_core.commands import Conflict
from readiness import commands as rc
from readiness.evaluator import ROBOT_NOT_AT_HUB, evaluate_readiness
from tests.execution_world import build_execution, close_run, go_check, to_hub, to_site
from tests.readiness_world import build_readiness
from tests.test_execution_db import NEXT_DAY

pytestmark = pytest.mark.django_db(transaction=True)


def _codes(visit_code: str) -> set[str]:
    return {b["code"] for b in evaluate_readiness(Visit.objects.get(code=visit_code)).blockers}


def _second_visit(w, robot: str = "R-01") -> str:
    code = w.new_confirmed_visit(robot=robot, d=NEXT_DAY)
    w.record_all_evidence(code, robot=robot)
    return code


def test_baseline_robot_with_no_history_is_at_hub():
    w = build_readiness(prefix="c3a_")
    r = Robot.objects.get(code="R-01")
    assert current_custody(r.pk).kind == "hub" and current_custody(r.pk).verified
    assert open_run_id(r.pk) is None and departure_problem(r.pk) is None
    assert ROBOT_NOT_AT_HUB not in _codes(w.visit)


def test_open_run_blocks_evaluator_and_authorization_for_another_visit():
    w = build_execution(prefix="c3b_")
    other = _second_visit(w)
    assert ROBOT_NOT_AT_HUB in _codes(other)
    with pytest.raises(Conflict, match="open run"):
        rc.authorize_departure(w.ops, visit_code=other)


def test_record_departure_revalidates_the_hub_gate():
    """Authorized while at hub, then the robot leaves on another visit: the old authorization cannot depart."""
    w = build_readiness(prefix="c3c_")
    other = _second_visit(w)
    late = rc.authorize_departure(w.ops, visit_code=other)["code"]
    first = rc.authorize_departure(w.ops, visit_code=w.visit)["code"]
    rc.record_departure(w.ops, authorization_code=first)
    with pytest.raises(Conflict):
        rc.record_departure(w.ops, authorization_code=late)
    assert ExecutionRun.objects.count() == 1


def test_robot_is_back_at_hub_only_after_reconciliation_and_trip_closure(app_role_conn):
    w = build_execution(prefix="c3d_")
    other = _second_visit(w)
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
        go_check(cur, w)
        to_hub(cur, w)
    # custody is a verified hub receipt, but the run is still open: still blocked
    assert current_custody(w.robot.pk).kind == "hub"
    assert "open run" in (departure_problem(w.robot.pk) or "")
    assert ROBOT_NOT_AT_HUB in _codes(other)
    with app_role_conn.cursor() as cur:
        close_run(cur, w)
    assert open_run_id(w.robot.pk) is None and departure_problem(w.robot.pk) is None
    assert ROBOT_NOT_AT_HUB not in _codes(other)
