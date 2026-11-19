"""Slice 7 integration, EM Option B (statement budget ruling): the readiness evaluator reads the X-3 periodic status
with ONE call to the authoritative SQL function readiness_periodic_inspection_status; the independent Python
implementation fleet.services.periodic_inspection_status stays the oracle.

1. Bounded: the evaluator's statement count does not grow with a robot's inspection history (0, 1, 10 and 100
   newer candidates, each disqualified by its template or by a signer).
2. Parity: the evaluator with the SQL status equals the evaluator with the Python oracle status (blocker codes and
   messages, readiness label, selected inspection, due date, expiry bound, guard snapshot) across demo/production,
   unknown interval, never inspected, overdue, due before unloading, history and a valid authorization.
SYNTHETIC data only.
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from fleet import services as fs
from fleet.models import Inspection, ManifestPattern, Robot
from planning import services as ps
from planning.models import Qualification, Visit
from readiness import evaluator as ev
from tests import readiness_world
from tests.conftest import make_user
from tests.fleet_world import make_engineering_template, make_periodic_rule, set_setting
from tests.operations_world import _backdate_inspections
from tests.test_s7_readiness import owner_set_submitted_at, production_world

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    world = readiness_world.build_readiness(prefix="ob_")
    world.fo = make_user("ob_fo", "field_operator")  # may sign, never a readiness inspector
    ps.register_person(world.ops, username=world.fo.username, code="P-9")
    return world


def _now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        value: datetime = cur.fetchone()[0]
        return value


def _python_status(
    robot: Robot, pattern: ManifestPattern | None, at: datetime | None, production: bool
) -> fs.PeriodicStatus:
    del pattern
    return fs.periodic_inspection_status(robot, at, production=production)


def _failing_candidates(w: Any, n: int) -> None:
    """n complete PASS inspections of R-01, newer than its qualifying periodic inspection, none qualifying:
    even ones on EPD (approved for pre-departure only), odd ones signed by a non-inspector (w.fo)."""
    if not ManifestPattern.objects.get(code="P-A").templates.filter(version="EPD").exists():
        make_engineering_template(w, "P-A", "EPD", purposes=("pre_departure",))
    for k in range(n):
        fs.submit_inspection(
            w.ops,
            robot_code="R-01",
            template_version="EPD" if k % 2 == 0 else "T1",
            answers=dict(w.all_pass),
            performer=w.field.username if k % 2 == 0 else w.fo.username,
            verifier=w.field2.username,
        )


def test_readiness_statements_do_not_grow_with_inspection_history(w):
    visit = Visit.objects.get(code=w.visit)
    robot = Robot.objects.get(code="R-01")
    qualifying = fs.periodic_inspection_status(robot, production=False)
    assert qualifying.state == "due"
    counts: dict[int, int] = {}
    added = 0
    for n in (0, 1, 10, 100):
        _failing_candidates(w, n - added)
        added = n
        assert Inspection.objects.filter(robot=robot).count() >= n + 1
        at = _now()
        st = fs.periodic_inspection_status(
            robot, at, production=False
        )  # the oracle: still the same inspection
        assert (st.state, st.inspection_id, st.due_at) == ("due", qualifying.inspection_id, qualifying.due_at)
        with CaptureQueriesContext(connection) as q:
            res = ev.evaluate_readiness(visit, at=at)
        counts[n] = len(q.captured_queries)
        assert not any(b["code"].startswith("PERIODIC_") for b in res.blockers), res.blockers
    assert len(set(counts.values())) == 1, counts  # constant: the X-3 status is one statement


def _evaluate(visit: str, at: datetime) -> dict[str, Any]:
    v = Visit.objects.get(code=visit)
    res = ev.evaluate_readiness(v, at=at)
    world = ev._read_world(v, at)
    st = world.periodic
    return {
        "ready": res.ready,
        "label": res.label,
        "blockers": res.blockers,
        "evidence_ids": res.evidence_ids,
        "expires_at": res.expires_at,
        "snapshot": ev._snapshot(world),
        "periodic": None
        if st is None
        else (st.state, st.due_at, st.inspection_id, st.rule_id, st.pattern_code),
    }


def _case_due(w: Any) -> None:
    pass


def _case_valid_authorization(w: Any) -> None:
    readiness_world.record_all_evidence(w, w.visit)


def _case_production_unknown_interval(w: Any) -> None:
    set_setting("demo_mode", "false")  # the SYNTHETIC rule is not usable in production


def _case_production_configured(w: Any) -> None:
    production_world(w)


def _case_never_inspected(w: Any) -> None:
    q = Qualification.objects.get(person__code="P-2", kind="readiness_inspector", revoked_at__isnull=True)
    ps.revoke_qualification(w.safety, qualification_id=q.pk, reason="SYNTHETIC")


def _case_rule_withdrawn(w: Any) -> None:
    rule = fs.active_periodic_rule(ManifestPattern.objects.get(code="P-A").pk)
    assert rule is not None
    fs.withdraw_periodic_rule(w.safety, rule_id=rule.pk, reason="SYNTHETIC")


def _case_overdue(w: Any) -> None:
    make_periodic_rule(w, "P-A", interval_min=60)
    _backdate_inspections("R-01", 2 * 24 * 60)  # the signers' grants move with them (still covered)


def _case_due_before_unloading(w: Any) -> None:
    from planning.services import live_feasibility

    unload_end = ev._unload_end(live_feasibility(Visit.objects.get(code=w.visit)))
    assert unload_end is not None
    interval = int((unload_end - _now()).total_seconds() // 60) - 1
    make_periodic_rule(w, "P-A", interval_min=interval)
    insp = Inspection.objects.get(code=w.inspection)
    owner_set_submitted_at(insp.pk, unload_end - timedelta(minutes=interval))  # due_at == unload_end: blocks


def _case_history(w: Any) -> None:
    readiness_world.record_all_evidence(w, w.visit)
    _failing_candidates(w, 10)


CASES = {
    "due": (_case_due, "due"),
    "valid_authorization": (_case_valid_authorization, "due"),
    "production_unknown_interval": (_case_production_unknown_interval, "unknown_interval"),
    "production_configured": (_case_production_configured, None),
    "never_inspected": (_case_never_inspected, "never_inspected"),
    "rule_withdrawn": (_case_rule_withdrawn, "unknown_interval"),
    "overdue": (_case_overdue, "overdue"),
    "due_before_unloading": (_case_due_before_unloading, "due"),
    "history": (_case_history, "due"),
}


@pytest.mark.parametrize("case", list(CASES))
def test_sql_status_evaluator_equals_python_oracle_evaluator(w, monkeypatch, case):
    build, state = CASES[case]
    build(w)
    at = _now()
    sql = _evaluate(w.visit, at)
    with monkeypatch.context() as m:
        m.setattr(ev, "_periodic_status", _python_status)
        py = _evaluate(w.visit, at)
    assert sql == py
    assert sql["periodic"] is not None
    if state is not None:
        assert sql["periodic"][0] == state
    if case == "valid_authorization":
        assert sql["ready"] and sql["expires_at"] is not None
    if case == "due_before_unloading":
        assert ev.PERIODIC_INSPECTION_DUE_BEFORE_RETURN in [b["code"] for b in sql["blockers"]]
