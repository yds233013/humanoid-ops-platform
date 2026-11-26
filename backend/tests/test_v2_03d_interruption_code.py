"""v2 03D (contract 15.7, EM first-batch item): ROBOT_INTERRUPTION_UNRESOLVED.

A robot whose earlier run left an unresolved operator interruption (the 03C protective fallback) cannot leave
on a new run. Readiness, Today, the API and the run page used to call that ROBOT_NOT_AT_HUB, which is
misleading: the robot IS at the hub. They now say "Robot R-01 has an unresolved interruption from an earlier
run. Resolve it before requesting departure authorization." The database gate
(execution_robot_departure_problem) is unchanged and still refuses a direct authorization.

SYNTHETIC data only.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client

from execution import sessions as ses
from execution.custody import ROBOT_INTERRUPTION_UNRESOLVED, departure_problem, unresolved_interruption
from execution.models import Interruption
from operations import today
from planning.models import Visit
from platform_core.commands import Conflict
from readiness import commands as rc
from readiness.evaluator import ROBOT_NOT_AT_HUB, evaluate_readiness
from readiness.models import DepartureAuthorization
from tests import execution_world as ew
from tests.fleet_world import set_setting
from tests.test_execution_db import NEXT_DAY, _owner_conn, _raw_authorization_sql
from tests.test_v2_03c_protective import _bring_back_and_close, at_site_with_go_check, fallback_stop

pytestmark = pytest.mark.django_db(transaction=True)


def text(robot: str) -> str:
    return (
        f"Robot {robot} has an unresolved interruption from an earlier run. "
        "Resolve it before requesting departure authorization."
    )


@pytest.fixture
def w(transactional_db: None) -> Any:
    """Run 1 is stopped by the protective fallback (an operator interruption), brought back and closed with the
    interruption still open; v2 is a confirmed visit of the same robot with every check recorded."""
    w = ew.build_execution(prefix="ic_")
    at_site_with_go_check(w)
    w.interruption = Interruption.objects.get(code=fallback_stop(w)["interruption"])
    _bring_back_and_close(w)
    w.v2 = w.new_confirmed_visit(robot=w.robot.code, d=NEXT_DAY)
    w.record_all_evidence(w.v2, robot=w.robot.code)
    return w


def client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def plain(body: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).replace("&#x27;", "'")


def test_the_classifier_reads_only_the_interruption_case():
    raw = "robot NB-014 has an unresolved interruption (IN-0001 on run EX-0001); it must be resolved first"
    found = unresolved_interruption(raw)
    assert found is not None and found.robot_code == "NB-014"
    assert found.message == text("NB-014")
    assert "IN-0001 on run EX-0001" in found.action
    for other in (
        None,
        "",
        "robot NB-014 is not at hub (custody: vehicle)",
        "robot NB-014 has an open run (EX-0002); it must be reconciled and its trip closed first",
    ):
        assert unresolved_interruption(other) is None


def test_readiness_gives_the_new_code_instead_of_robot_not_at_hub(w):
    res = evaluate_readiness(Visit.objects.get(code=w.v2))
    by_code = {b["code"]: b for b in res.blockers}
    assert ROBOT_INTERRUPTION_UNRESOLVED in by_code and ROBOT_NOT_AT_HUB not in by_code
    assert by_code[ROBOT_INTERRUPTION_UNRESOLVED]["message"] == text(w.robot.code)
    assert w.interruption.code in by_code[ROBOT_INTERRUPTION_UNRESOLVED]["action"]
    assert not res.ready

    body = plain(client(w.ops).get(f"/readiness/visits/{w.v2}/").content.decode())
    assert text(w.robot.code) in body
    assert "is not at hub" not in body and "not at the hub" not in body


def test_authorization_is_refused_with_the_new_code(w):
    with pytest.raises(Conflict) as refused:
        rc.authorize_departure(w.ops, visit_code=w.v2)
    assert f"{ROBOT_INTERRUPTION_UNRESOLVED}: {text(w.robot.code)}" in str(refused.value)
    assert ROBOT_NOT_AT_HUB not in str(refused.value)


def test_today_names_the_interruption(w, monkeypatch):
    set_setting("ops_readiness_lookahead_min", str(90 * 1440))
    monkeypatch.setattr(today, "local_day", lambda at: NEXT_DAY)
    body = plain(client(w.ops).get("/ops/").content.decode())
    assert text(w.robot.code).rstrip(".") in body
    assert "The robot is not at the hub" not in body


def test_today_words_without_a_robot_code():
    assert today.blocker_words(ROBOT_INTERRUPTION_UNRESOLVED, "R-07").startswith("Robot R-07 has")
    assert today.blocker_words(ROBOT_INTERRUPTION_UNRESOLVED).startswith("The robot has an unresolved")


def test_the_api_carries_the_new_code(w):
    set_setting("ops_readiness_lookahead_min", str(90 * 1440))
    items = client(w.ops).get("/api/ops/exceptions").json()["items"]
    mine = [x for x in items if x["code"] == "READINESS_INCOMPLETE_SOON" and x["visit"] == w.v2]
    assert mine, items
    assert ROBOT_INTERRUPTION_UNRESOLVED in mine[0]["message"]
    assert ROBOT_NOT_AT_HUB not in mine[0]["message"]


def test_the_run_page_says_why_the_robot_cannot_leave_again(w):
    resp = client(w.field).get(f"/execution/runs/{w.run.code}/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert f'data-readiness-code="{ROBOT_INTERRUPTION_UNRESOLVED}"' in body
    assert text(w.robot.code) in plain(body)

    ses.resolve_interruption(w.ops, code=w.interruption.code, note="SYNTHETIC: reviewed after the return")
    body = client(w.field).get(f"/execution/runs/{w.run.code}/").content.decode()
    assert ROBOT_INTERRUPTION_UNRESOLVED not in body
    codes = {b["code"] for b in evaluate_readiness(Visit.objects.get(code=w.v2)).blockers}
    assert ROBOT_INTERRUPTION_UNRESOLVED not in codes and ROBOT_NOT_AT_HUB not in codes


def test_an_open_run_never_shows_the_notice(transactional_db):
    w = ew.build_execution(prefix="ib_")
    at_site_with_go_check(w)
    fallback_stop(w)
    body = client(w.field).get(f"/execution/runs/{w.run.code}/").content.decode()
    assert ROBOT_INTERRUPTION_UNRESOLVED not in body  # the open-run problem comes first; nothing new here


def test_the_database_gate_is_unchanged_and_still_refuses(w):
    """The SQL text is exactly the 03C one, and a direct authorization insert by the schema owner (the only way
    to a departure) is refused by the trigger."""
    with connection.cursor() as cur:
        cur.execute("SELECT execution_robot_departure_problem(%s)", [w.robot.pk])
        raw = cur.fetchone()[0]
    assert raw == departure_problem(w.robot.pk)
    assert raw == (
        f"robot {w.robot.code} has an unresolved interruption ({w.interruption.code} on run {w.run.code}); "
        "it must be resolved first"
    )
    sql, args = _raw_authorization_sql(w, w.v2)
    with _owner_conn() as owner, owner.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="unresolved interruption"):
            cur.execute(sql, args)
    assert not DepartureAuthorization.objects.filter(visit__code=w.v2).exists()
