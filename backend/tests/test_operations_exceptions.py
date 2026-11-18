"""Slice 5 exception inbox (Engineer B): every code firing and not firing, inclusive time boundaries at -1 us / =
/ +1 us, missing and invalid thresholds, determinism, identity (uniqueness) with repeated escalations,
interruptions and visits, keyset pagination, readiness coverage, and the statement budget on a small world and
on the scale fixture. docs/05_operations_command_center_contract.md sections 3, 4a, 6.2, 6.4. SYNTHETIC data only.
"""

import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection

from execution import escalation as esc
from execution import incidents as inc
from execution.models import CustodyEvent, ExecutionRun, Incident
from fleet.models import DeploymentSetting, Hold
from operations import exceptions as ox
from operations.exceptions import ExceptionPage, OpsException, open_exceptions
from operations.snapshot import read_snapshot
from planning.models import Visit
from readiness.evaluator import evaluate_readiness
from readiness.models import DepartureEvent
from tests import operations_world as ow
from tests.fleet_world import set_setting
from tests.planning_world import DAY
from tests.readiness_world import new_confirmed_visit

US = timedelta(microseconds=1)
GRACE = {
    "ops_custody_verify_grace_min": 30,
    "ops_return_grace_min": 60,
    "ops_inspection_grace_min": 120,
    "ops_readiness_lookahead_min": 1440,
}
FAR = datetime(2028, 1, 1, tzinfo=UTC)  # a snapshot time after every planned visit of the small worlds
FORBIDDEN = re.compile(
    r"\b(FOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)|nextval|setval|set_config|pg_advisory\w*"
    r"|INSERT|UPDATE|DELETE|TRUNCATE|LOCK)\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ helpers
def configure(**overrides: int | str | None) -> None:
    """All four thresholds valid (GRACE), with overrides; None deletes the key."""
    values: dict[str, int | str | None] = {**GRACE, **overrides}
    for key, value in values.items():
        if value is None:
            DeploymentSetting.objects.filter(key=key).delete()
        else:
            set_setting(key, str(value))


def db_clock():
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def items(at, **kw) -> list[OpsException]:
    return open_exceptions(at, **kw).items


def find(at, code: str, ref: str | None = None) -> list[OpsException]:
    return [x for x in items(at) if x.code == code and (ref is None or x.entity_ref == ref)]


def fires(at, code: str, ref: str) -> bool:
    return bool(find(at, code, ref))


def assert_boundary(code: str, ref: str, edge) -> None:
    """Inclusive threshold: absent 1 us before, present exactly at and 1 us after."""
    assert [fires(edge - US, code, ref), fires(edge, code, ref), fires(edge + US, code, ref)] == [
        False,
        True,
        True,
    ], f"{code} {ref} boundary at {edge}"


def departed_at(run: ExecutionRun):
    return DepartureEvent.objects.get(visit_id=run.visit_id).departed_at


def planned_unloading_end(run: ExecutionRun):
    with connection.cursor() as cur:
        cur.execute("SELECT planned_unloading_end FROM operations_open_run_stage WHERE run_id = %s", [run.pk])
        return cur.fetchone()[0]


def hub_receipt_at(run: ExecutionRun):
    return CustodyEvent.objects.filter(run=run, to_kind="hub").order_by("-seq").first().recorded_at


def capture():
    seen: list[str] = []

    def wrapper(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    return seen, wrapper


def check_page_invariants(page: ExceptionPage) -> None:
    keys = [x.sort_key for x in page.items]
    assert keys == sorted(keys), "not in (priority, since, code, entity_ref) order"
    ids = [(x.code, x.entity_ref) for x in page.items]
    assert len(ids) == len(set(ids)), "(code, entity_ref) is not unique"
    for x in page.items:
        assert x.priority == ox.PRIORITY[x.code] and x.resolvable == ox.RESOLVABLE[x.code]
        assert x.since is not None and x.message and x.owner_role
        for word in ("cleared", "safe", "ready to deploy", "available to deploy"):
            assert word not in x.message.lower()


# ------------------------------------------------------------------ thresholds
@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", 1),
        ("30", 30),
        ("030", 30),
        (str(ox.MAX_THRESHOLD_MIN), ox.MAX_THRESHOLD_MIN),
        (None, None),
        ("", None),
        ("0", None),
        ("00", None),
        ("-5", None),
        ("+5", None),
        ("1.5", None),
        ("1e3", None),
        (" 30", None),
        ("30 ", None),
        ("abc", None),
        ("٣", None),  # a non-ASCII digit is not a configured number
        (str(ox.MAX_THRESHOLD_MIN + 1), None),
        ("9" * 40, None),
    ],
)
def test_parse_threshold(value, expected):
    assert ox.parse_threshold(value) == expected


@pytest.mark.django_db
def test_threshold_not_configured_one_per_missing_or_invalid_key_and_only_its_rule_disabled():
    w = ow.ops_world("tn_", robots=3)
    run = ow.depart_new(w, "R-02")  # unverified departure (CUSTODY_UNVERIFIED input)
    at = FAR
    for key in ox.THRESHOLD_RULES:
        DeploymentSetting.objects.filter(key=key).delete()
    page = open_exceptions(at)
    missing = [x for x in page.items if x.code == "THRESHOLD_NOT_CONFIGURED"]
    assert sorted(x.entity_ref for x in missing) == sorted(ox.THRESHOLD_RULES)
    for x in missing:
        assert (x.priority, x.since, x.owner_role, x.entity_type) == (1, at, "engineering_lead", "setting")
        assert (x.action_url, x.resolvable, x.robot, x.visit) == (None, False, None, None)
        assert "missing" in x.message and ox.THRESHOLD_RULES[x.entity_ref] in x.message
    codes = {x.code for x in page.items}
    assert not codes & {"CUSTODY_UNVERIFIED", "RETURN_OVERDUE", "POST_VISIT_INSPECTION_MISSING"}
    assert "READINESS_INCOMPLETE_SOON" not in codes
    assert page.readiness_coverage == {
        "qualifying": 0,
        "evaluated": 0,
        "not_evaluated": 0,
        "complete": False,
        "skipped": False,
    }
    # non-time rules are untouched by missing thresholds
    assert "HELD_ROBOT_ASSIGNED" not in codes and "INCIDENT_UNTRIAGED" not in codes
    ow.report(w, "R-03")
    assert find(at, "INCIDENT_UNTRIAGED")

    # one invalid key: only its rule is off, the others fire; exactly one exception for it
    configure(ops_return_grace_min="0")
    found = [x for x in items(at) if x.code == "THRESHOLD_NOT_CONFIGURED"]
    assert [x.entity_ref for x in found] == ["ops_return_grace_min"]
    assert "not a whole number" in found[0].message
    assert fires(at, "CUSTODY_UNVERIFIED", run.code) and not find(at, "RETURN_OVERDUE")
    configure(ops_return_grace_min="0", ops_custody_verify_grace_min="abc")
    assert sorted(x.entity_ref for x in find(at, "THRESHOLD_NOT_CONFIGURED")) == [
        "ops_custody_verify_grace_min",
        "ops_return_grace_min",
    ]
    assert not find(at, "CUSTODY_UNVERIFIED")
    configure()
    assert not find(at, "THRESHOLD_NOT_CONFIGURED")
    assert fires(at, "CUSTODY_UNVERIFIED", run.code) and fires(at, "RETURN_OVERDUE", run.code)


@pytest.mark.django_db
def test_threshold_since_is_the_snapshot_time():
    ow.ops_world("ts_", robots=1, incident_policy=False)
    configure(ops_inspection_grace_min=None)
    t1, t2 = db_clock(), db_clock() + timedelta(hours=5)
    assert [x.since for x in find(t1, "THRESHOLD_NOT_CONFIGURED")] == [t1]
    assert [x.since for x in find(t2, "THRESHOLD_NOT_CONFIGURED")] == [t2]


# ------------------------------------------------------------------ time-based rules: firing, non-firing, boundary
@pytest.mark.django_db
def test_custody_unverified_boundary_and_cleared_by_verification():
    w = ow.ops_world("cu_", robots=3, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    edge = departed_at(run) + timedelta(minutes=30)
    assert_boundary("CUSTODY_UNVERIFIED", run.code, edge)
    (x,) = find(edge, "CUSTODY_UNVERIFIED", run.code)
    assert (x.since, x.robot, x.visit, x.owner_role, x.priority) == (
        departed_at(run),
        "R-02",
        run.visit.code,
        "field_operator",
        3,
    )
    assert (x.entity_type, x.action_url, x.resolvable) == ("run", f"/execution/runs/{run.code}/", True)
    configure(ops_custody_verify_grace_min=45)  # the threshold is read from the settings, not hard-coded
    assert not fires(edge, "CUSTODY_UNVERIFIED", run.code)
    assert_boundary("CUSTODY_UNVERIFIED", run.code, departed_at(run) + timedelta(minutes=45))
    ow.advance(w, run, "verified")
    assert not find(FAR, "CUSTODY_UNVERIFIED")


@pytest.mark.django_db
def test_return_overdue_boundary_and_not_after_arrival():
    w = ow.ops_world("ro_", robots=3, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "returning")
    end = planned_unloading_end(run)
    assert end is not None
    assert_boundary("RETURN_OVERDUE", run.code, end + timedelta(minutes=60))
    (x,) = find(FAR, "RETURN_OVERDUE", run.code)
    assert (x.since, x.priority, x.owner_role, x.resolvable) == (end, 2, "ops_lead", True)
    assert x.action_url == f"/execution/runs/{run.code}/"
    w.cmd.trip(run, "arrived_hub")
    assert not find(FAR, "RETURN_OVERDUE")
    # a run that never left the outbound leg is overdue too
    other = ow.depart_new(w, "R-03")
    assert fires(planned_unloading_end(other) + timedelta(minutes=60), "RETURN_OVERDUE", other.code)


@pytest.mark.django_db
def test_post_visit_inspection_missing_boundary_and_cleared_by_inspection():
    w = ow.ops_world("pi_", robots=3, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "arrived_hub")
    assert not find(FAR, "POST_VISIT_INSPECTION_MISSING")  # robot still in the vehicle: no hub receipt
    w.cmd.move(run, "hub", receiver="P-3")
    receipt = hub_receipt_at(run)
    assert_boundary("POST_VISIT_INSPECTION_MISSING", run.code, receipt + timedelta(minutes=120))
    (x,) = find(FAR, "POST_VISIT_INSPECTION_MISSING", run.code)
    assert (x.since, x.owner_role, x.priority, x.resolvable) == (receipt, "field_operator", 3, True)
    assert x.action_url == "/fleet/R-02/"
    w.cmd.inspect(run)
    assert not find(FAR, "POST_VISIT_INSPECTION_MISSING")


@pytest.mark.django_db
def test_readiness_incomplete_soon_boundary_not_ready_and_ready():
    w = ow.ops_world("rs_", robots=3, incident_policy=False)
    configure()
    start = Visit.objects.select_related("current_revision").get(code=w.visit).current_revision.on_site_start
    edge = start - timedelta(minutes=1440)  # the visit enters the lookahead
    before = open_exceptions(edge - US)
    assert before.readiness_coverage == {
        "qualifying": 0,
        "evaluated": 0,
        "not_evaluated": 0,
        "complete": True,
        "skipped": False,
    }
    assert not [x for x in before.items if x.code == "READINESS_INCOMPLETE_SOON"]
    for at in (edge, edge + US):
        page = open_exceptions(at)
        assert page.readiness_coverage == {
            "qualifying": 1,
            "evaluated": 1,
            "not_evaluated": 0,
            "complete": True,
            "skipped": False,
        }
        (x,) = [x for x in page.items if x.code == "READINESS_INCOMPLETE_SOON"]
        assert (x.entity_ref, x.visit, x.robot, x.since) == (w.visit, w.visit, "R-01", edge)
        assert (x.owner_role, x.priority, x.resolvable) == ("ops_lead", 3, False)
        assert x.action_url == f"/readiness/visits/{w.visit}/"
        assert "EVIDENCE_STALE" in x.message  # the evaluator's own verdict at `at`, never inferred
        assert not evaluate_readiness(Visit.objects.get(code=w.visit), at=at).ready
    # Ready at `at` (fresh evidence, lookahead covering the visit): evaluated, no exception.
    now = db_clock()
    configure(ops_readiness_lookahead_min=int((start - now).total_seconds() // 60) + 60)
    assert evaluate_readiness(Visit.objects.get(code=w.visit), at=now).ready
    page = open_exceptions(now)
    assert page.readiness_coverage == {
        "qualifying": 1,
        "evaluated": 1,
        "not_evaluated": 0,
        "complete": True,
        "skipped": False,
    }
    assert not [x for x in page.items if x.code == "READINESS_INCOMPLETE_SOON"]
    # A visit that ended, or departed, or is not confirmed does not qualify.
    assert open_exceptions(start + timedelta(hours=2)).readiness_coverage["qualifying"] == 0
    w.p.cancel_visit(w.ops, visit_code=w.visit, reason="SYNTHETIC")
    assert open_exceptions(edge).readiness_coverage["qualifying"] == 0


@pytest.mark.django_db
def test_readiness_coverage_is_partial_beyond_ten_and_evaluates_the_first_ten_by_start_then_id():
    w = ow.ops_world("rc_", robots=3, incident_policy=False)
    configure(ops_readiness_lookahead_min=60 * 24 * 60)
    codes = [w.visit]
    for i in range(12):
        codes.append(new_confirmed_visit(w, robot=f"R-0{1 + i % 3}", d=DAY + timedelta(days=1 + i)))
    visits = Visit.objects.filter(code__in=codes).select_related("current_revision")
    order = [v.code for v in sorted(visits, key=lambda v: (v.current_revision.on_site_start, v.pk))]
    at = db_clock()
    seen, wrapper = capture()
    with connection.execute_wrapper(wrapper):
        page = open_exceptions(at)
    assert page.readiness_coverage == {
        "qualifying": 13,
        "evaluated": 10,
        "not_evaluated": 3,
        "complete": False,
        "skipped": False,
    }
    flagged = [x.entity_ref for x in page.items if x.code == "READINESS_INCOMPLETE_SOON"]
    expected = [c for c in order[:10] if not evaluate_readiness(Visit.objects.get(code=c), at=at).ready]
    assert sorted(flagged) == sorted(expected) and len(expected) >= 9  # only w.visit has evidence
    assert not set(flagged) & set(order[10:])  # never inferred for the unevaluated visits
    assert len(seen) <= 12 + 10 * ox.READINESS_EVALUATOR_STATEMENTS
    assert not [s for s in seen if FORBIDDEN.search(s)]


# ------------------------------------------------------------------ state rules: firing and non-firing
@pytest.mark.django_db
def test_robot_unaccounted_on_a_run_and_without_a_run():
    w = ow.ops_world("ua_", robots=3)
    configure()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "verified")
    assert not find(FAR, "ROBOT_UNACCOUNTED")
    icode = ow.report(w, "R-02", event="robot_unaccounted")
    (x,) = find(FAR, "ROBOT_UNACCOUNTED")
    event = CustodyEvent.objects.filter(robot__code="R-02").order_by("-seq").first()
    assert (x.entity_ref, x.robot, x.visit, x.since, x.priority) == (
        "R-02",
        "R-02",
        run.visit.code,
        event.recorded_at,
        1,
    )
    assert (x.action_url, x.owner_role, x.resolvable) == (f"/execution/runs/{run.code}/", "ops_lead", False)
    assert icode in x.message
    w.cmd.trip(run, "arrived_hub")
    w.cmd.move(run, "hub", note="SYNTHETIC: found and received")
    assert not find(FAR, "ROBOT_UNACCOUNTED")
    # no open run: the incident page is where it is addressed
    icode2 = ow.report(w, "R-03", event="robot_unaccounted")
    (y,) = find(FAR, "ROBOT_UNACCOUNTED")
    assert (y.entity_ref, y.visit, y.action_url) == ("R-03", None, f"/execution/incidents/{icode2}/")
    w.cmd.recover("R-03")
    assert not find(FAR, "ROBOT_UNACCOUNTED")


@pytest.mark.django_db
def test_custody_unknown_fires_only_for_a_broken_history():
    w = ow.ops_world("ck_", robots=2, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    assert not find(FAR, "CUSTODY_UNKNOWN")
    with connection.cursor() as cur:  # as in test_operations_sql: a gap the triggers would refuse
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE execution_custodyevent DISABLE TRIGGER USER")
        cur.execute(
            "INSERT INTO execution_custodyevent (robot_id, seq, from_kind, to_kind, run_id, trip_id, visit_id,"
            " custodian_id, basis, note, recorded_by_id, recorded_at) SELECT robot_id, 5, 'site', 'hub', run_id,"
            " trip_id, visit_id, custodian_id, 'handoff', 'SYNTHETIC gap', recorded_by_id, clock_timestamp()"
            " FROM execution_custodyevent WHERE run_id = %s",
            [run.pk],
        )
        cur.execute("ALTER TABLE execution_custodyevent ENABLE TRIGGER USER")
    (x,) = find(FAR, "CUSTODY_UNKNOWN")
    latest = CustodyEvent.objects.filter(robot__code="R-02").order_by("-recorded_at").first().recorded_at
    assert (x.entity_ref, x.since, x.priority, x.owner_role, x.resolvable) == (
        "R-02",
        latest,
        1,
        "ops_lead",
        False,
    )
    assert x.action_url == "/fleet/R-02/"


@pytest.mark.django_db
def test_incident_untriaged_and_severity_unclassified():
    w = ow.ops_world("iu_", robots=3, incident_policy=False)
    configure()
    icode = ow.report(w, "R-03")
    (x,) = find(FAR, "INCIDENT_UNTRIAGED")
    reported = Incident.objects.get(code=icode).reported_at
    assert (x.entity_ref, x.robot, x.since, x.priority, x.owner_role) == (
        icode,
        "R-03",
        reported,
        2,
        "ops_lead",
    )
    assert (x.action_url, x.resolvable, x.entity_type) == (f"/execution/incidents/{icode}/", True, "incident")
    assert not find(FAR, "SEVERITY_UNCLASSIFIED")  # no active incident policy
    ow.make_incident_policy(w)
    (y,) = find(FAR, "SEVERITY_UNCLASSIFIED")
    assert (y.entity_ref, y.since, y.priority, y.owner_role) == (icode, reported, 3, "safety_reviewer")
    inc.change_incident_status(w.safety, incident_code=icode, to_status="triaged")
    assert not find(FAR, "INCIDENT_UNTRIAGED") and find(FAR, "SEVERITY_UNCLASSIFIED")
    inc.decide_severity(w.lead, incident_code=icode, level="low", reason="SYNTHETIC")
    assert not find(FAR, "SEVERITY_UNCLASSIFIED")


@pytest.mark.django_db
def test_escalations_one_exception_each_until_acknowledged():
    w = ow.ops_world("eu_", robots=3)
    configure()
    icode = ow.report(w, "R-03")
    e1 = esc.escalate_incident(
        w.ops, incident_code=icode, to_role="engineering_lead", requested_action="SYN", to_person_code="P-5"
    )
    e2 = esc.escalate_incident(w.ops, incident_code=icode, to_role="founder", requested_action="SYN")
    e3 = esc.escalate_incident(w.ops, incident_code=icode, to_role="engineering_lead", requested_action="SYN")
    found = find(FAR, "ESCALATION_UNACKNOWLEDGED")
    assert sorted(x.entity_ref for x in found) == sorted(f"ESC-{e['id']}" for e in (e1, e2, e3))
    owners = {x.entity_ref: x.owner_role for x in found}
    assert owners == {
        f"ESC-{e1['id']}": "engineering_lead",
        f"ESC-{e2['id']}": "founder",
        f"ESC-{e3['id']}": "engineering_lead",
    }
    assert all(x.action_url == f"/execution/incidents/{icode}/" and x.priority == 2 for x in found)
    assert "P-5" in [x for x in found if x.entity_ref == f"ESC-{e1['id']}"][0].message
    esc.acknowledge_escalation(w.lead2, escalation_id=e1["id"])
    assert sorted(x.entity_ref for x in find(FAR, "ESCALATION_UNACKNOWLEDGED")) == sorted(
        f"ESC-{e['id']}" for e in (e2, e3)
    )


@pytest.mark.django_db
def test_interruption_open_one_per_interruption_on_open_runs_only():
    w = ow.ops_world("io_", robots=3, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "session_paused")
    first = run._interruption
    second = w.cmd.interrupt(run)["code"]
    found = find(FAR, "INTERRUPTION_OPEN")
    assert sorted(x.entity_ref for x in found) == sorted([first, second])
    assert all(
        (x.robot, x.visit, x.priority, x.owner_role, x.action_url, x.resolvable)
        == ("R-02", run.visit.code, 2, "ops_lead", f"/execution/runs/{run.code}/", False)
        for x in found
    )
    w.cmd.resolve(first)
    assert [x.entity_ref for x in find(FAR, "INTERRUPTION_OPEN")] == [second]
    w.cmd.resolve(second)
    assert not find(FAR, "INTERRUPTION_OPEN")


@pytest.mark.django_db
def test_site_no_go_uses_the_latest_site_check():
    w = ow.ops_world("sg_", robots=3, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "arrived_site")
    w.cmd.site_check(run)  # the robot is still in the vehicle: the database records No-Go
    assert [x.entity_ref for x in find(FAR, "SITE_NO_GO")] == [run.code]
    w.cmd.move(run, "site")
    w.cmd.site_check(run)
    assert not find(FAR, "SITE_NO_GO")
    w.cmd.site_check(run, conditions={"c1": "ok", "c2": "not_ok"})
    (x,) = find(FAR, "SITE_NO_GO")
    assert (x.entity_ref, x.robot, x.priority, x.owner_role, x.resolvable) == (
        run.code,
        "R-02",
        3,
        "ops_lead",
        False,
    )
    w.cmd.site_check(run)
    assert not find(FAR, "SITE_NO_GO")


@pytest.mark.django_db
def test_reconciliation_unresolved_until_reconciled():
    w = ow.ops_world("ru_", robots=3, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "inspected")
    assert not find(FAR, "RECONCILIATION_UNRESOLVED")
    w.cmd.reconcile(run, inspection=run._inspection, discrepancies=["SYNTHETIC: strap frayed"])
    (x,) = find(FAR, "RECONCILIATION_UNRESOLVED")
    assert (x.entity_ref, x.priority, x.owner_role, x.resolvable) == (run.code, 3, "ops_lead", False)
    w.cmd.reconcile(run, inspection=run._inspection)
    assert not find(FAR, "RECONCILIATION_UNRESOLVED")


@pytest.mark.django_db
def test_held_robot_assigned_one_per_visit_robot_and_pattern_holds():
    w = ow.ops_world("ha_", robots=3, incident_policy=False)
    configure()
    at = db_clock()
    assert not find(at, "HELD_ROBOT_ASSIGNED")
    later = new_confirmed_visit(w, robot="R-01", d=DAY + timedelta(days=3))
    ow.place_hold(w, "robot", "R-02", blocking=False)  # non-blocking: never "held"
    hold = ow.place_hold(w, "robot", "R-01")
    placed = Hold.objects.get(code=hold).placed_at
    found = find(at, "HELD_ROBOT_ASSIGNED")
    assert sorted(x.entity_ref for x in found) == sorted([f"R-01/{w.visit}", f"R-01/{later}"])
    assert all(
        (x.since, x.robot, x.priority, x.owner_role, x.action_url, x.resolvable)
        == (placed, "R-01", 2, "ops_lead", "/fleet/R-01/", False)
        and hold in x.message
        for x in found
    )
    assert {x.visit for x in found} == {w.visit, later}
    # an ended visit is not upcoming
    end = Visit.objects.select_related("current_revision").get(code=w.visit).current_revision.on_site_end
    assert [x.entity_ref for x in find(end, "HELD_ROBOT_ASSIGNED")] == [f"R-01/{later}"]
    ow.release(w, hold)
    assert not find(at, "HELD_ROBOT_ASSIGNED")
    ow.place_hold(w, "manifest_pattern", "P-A")  # pattern scope holds every robot on the pattern
    assert sorted(x.entity_ref for x in find(at, "HELD_ROBOT_ASSIGNED")) == sorted(
        [f"R-01/{w.visit}", f"R-01/{later}"]
    )
    w.p.cancel_visit(w.ops, visit_code=later, reason="SYNTHETIC")
    assert [x.entity_ref for x in find(at, "HELD_ROBOT_ASSIGNED")] == [f"R-01/{w.visit}"]


# ------------------------------------------------------------------ identity, determinism, ordering, pagination
@pytest.mark.django_db
def test_identity_is_unique_with_repeated_escalations_interruptions_and_visits():
    w = ow.ops_world("id_", robots=3)
    configure()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "session_paused")
    for _ in range(3):
        w.cmd.interrupt(run)
    icode = ow.report(w, "R-02")
    for role in ("engineering_lead", "engineering_lead", "founder"):
        esc.escalate_incident(w.ops, incident_code=icode, to_role=role, requested_action="SYN")
    icode2 = ow.report(w, "R-02")
    esc.escalate_incident(w.ops, incident_code=icode2, to_role="founder", requested_action="SYN")
    for d in (3, 4, 5):
        new_confirmed_visit(w, robot="R-01", d=DAY + timedelta(days=d))
    ow.place_hold(w, "robot", "R-01")
    ow.place_hold(w, "robot", "R-01")
    page = open_exceptions(db_clock())  # before the visits: they are upcoming
    check_page_invariants(page)
    count = {c: sum(x.code == c for x in page.items) for c in ox.CODES}
    # 1 + 3 operator interruptions + 1 per incident reported on the open run
    assert count["INTERRUPTION_OPEN"] == 6 and count["ESCALATION_UNACKNOWLEDGED"] == 4
    assert count["INCIDENT_UNTRIAGED"] == 2 and count["SEVERITY_UNCLASSIFIED"] == 2
    held = [x.entity_ref for x in page.items if x.code == "HELD_ROBOT_ASSIGNED"]
    assert len(held) == 4 and all(r.startswith("R-01/") for r in held)  # w.visit + 3 (two holds: one each)


SEEDS = [1, 2, 3, 4, 5, 6]
COVERED: set[str] = set()


@pytest.mark.django_db
@pytest.mark.parametrize("seed", SEEDS)
def test_random_world_deterministic_ordered_unique_and_paginates(seed):
    w = ow.random_world(seed)
    configure(ops_readiness_lookahead_min=60 * 24 * 30)
    if seed % 2:
        configure(ops_return_grace_min="x", ops_readiness_lookahead_min=60 * 24 * 30)
    at = db_clock() + timedelta(days=1)
    first, second = open_exceptions(at), open_exceptions(at)
    assert first == second, w.story
    check_page_invariants(first)
    full = first.items
    # the SQL rules come back already in the Python order (code-point text comparison; COLLATE "C" in SQL)
    sql_keys = [x.sort_key for x in ox._sql_rules(at, ox.read_thresholds()[0], None, None)]
    assert sql_keys == sorted(sql_keys) and set(sql_keys) <= {x.sort_key for x in full}
    COVERED.update(x.code for x in full)
    for size in (1, 2, 5):
        got: list[OpsException] = []
        cursor = None
        while True:
            page = open_exceptions(at, limit=size, after=cursor)
            assert len(page.items) <= size and page.readiness_coverage == first.readiness_coverage
            got += page.items
            if page.next_cursor is None:
                break
            assert page.next_cursor == page.items[-1].sort_key and len(page.items) == size
            cursor = page.next_cursor
        assert got == full, (size, w.story)


@pytest.mark.django_db
def test_ordering_ties_break_on_code_then_entity_ref_by_code_point():
    """Every time-free tie lands on (code, entity_ref): THRESHOLD_NOT_CONFIGURED items all have since = at, and
    escalation refs ESC-<id> sort as text by code point (ESC-10 before ESC-9), in SQL exactly as in Python."""
    w = ow.ops_world("ot_", robots=3)
    for key in ox.THRESHOLD_RULES:
        DeploymentSetting.objects.filter(key=key).delete()
    icode = ow.report(w, "R-03")
    ids = [
        esc.escalate_incident(w.ops, incident_code=icode, to_role="founder", requested_action="SYN")["id"]
        for _ in range(11)
    ]
    at = db_clock()
    page = open_exceptions(at)
    check_page_invariants(page)
    top = page.items[:4]
    assert [x.entity_ref for x in top] == sorted(ox.THRESHOLD_RULES)  # priority 1, same since: by entity_ref
    refs = [x.entity_ref for x in page.items if x.code == "ESCALATION_UNACKNOWLEDGED"]
    assert refs == [f"ESC-{i}" for i in ids]  # distinct escalation times order them first


@pytest.mark.django_db
def test_keyset_cursor_and_limit_validation():
    w = ow.ops_world("kc_", robots=3)
    for key in ox.THRESHOLD_RULES:
        DeploymentSetting.objects.filter(key=key).delete()
    ow.report(w, "R-03")
    at = db_clock()
    full = open_exceptions(at).items
    assert len(full) >= 6  # 4 thresholds (Python) + untriaged + unclassified + held (SQL)
    for i in range(len(full)):
        rest = open_exceptions(at, after=full[i].sort_key).items
        assert rest == full[i + 1 :]
    last = open_exceptions(at, limit=len(full))
    assert last.items == full and last.next_cursor is None
    with pytest.raises(ValueError):
        open_exceptions(at, limit=0)


# ------------------------------------------------------------------ snapshot, statements, scale
@pytest.mark.django_db(transaction=True)
def test_runs_inside_the_read_only_snapshot_without_writes_or_locks():
    w = ow.ops_world("sn_", robots=3)
    configure(ops_readiness_lookahead_min=60 * 24 * 60)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "session_paused")
    new_confirmed_visit(w, robot="R-03", d=DAY + timedelta(days=2))  # no evidence: readiness incomplete
    ow.report(w, "R-03")
    seen, wrapper = capture()
    with connection.execute_wrapper(wrapper), read_snapshot() as at:
        page = open_exceptions(at)
    assert {"INTERRUPTION_OPEN", "INCIDENT_UNTRIAGED", "READINESS_INCOMPLETE_SOON"} <= {
        x.code for x in page.items
    }
    assert page.readiness_coverage["evaluated"] >= 1
    assert not [s for s in seen[1:] if FORBIDDEN.search(s)], [s for s in seen if FORBIDDEN.search(s)]


def _count_statements(at) -> tuple[int, ExceptionPage, list[str]]:
    seen, wrapper = capture()
    with connection.execute_wrapper(wrapper):
        page = open_exceptions(at)
    return len(seen), page, seen


def _evaluator_statements(page_at, visits: list[Visit]) -> list[int]:
    out = []
    for v in visits:
        seen, wrapper = capture()
        with connection.execute_wrapper(wrapper):
            evaluate_readiness(v, at=page_at)
        out.append(len(seen))
    return out


# thresholds + every SQL rule + unknown basis; + qualifying visits + Visit rows when readiness is evaluated
BASE_STATEMENTS = 3


@pytest.mark.django_db
def test_statement_budget_small_world():
    w = ow.ops_world("sb_", robots=3)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "session_paused")
    ow.report(w, "R-03")
    configure(ops_readiness_lookahead_min=None)
    at = db_clock() + timedelta(days=1)
    small_off, _, _ = _count_statements(at)
    configure(ops_readiness_lookahead_min=60 * 24 * 60)
    small_on, small_page, seen = _count_statements(at)
    n_eval = small_page.readiness_coverage["evaluated"]
    assert small_off == BASE_STATEMENTS
    assert small_on <= 12 + 10 * ox.READINESS_EVALUATOR_STATEMENTS
    assert small_on - (BASE_STATEMENTS + 2) <= n_eval * ox.READINESS_EVALUATOR_STATEMENTS
    assert not [s for s in seen if FORBIDDEN.search(s)]


@pytest.mark.django_db
def test_statement_budget_scale_fixture_and_evaluator_pinned():
    """Same constant count as the small world without readiness; the capped 12 + 10 x E total with it; E pinned
    on every evaluated visit."""
    sw = ow.scale_world()
    configure(ops_readiness_lookahead_min=None)
    at = db_clock() + timedelta(days=1)
    scale_off, off_page, _ = _count_statements(at)
    assert scale_off == BASE_STATEMENTS
    assert len(off_page.items) > 100
    configure(ops_readiness_lookahead_min=60 * 24 * 30)
    scale_on, page, seen = _count_statements(at)
    cov = page.readiness_coverage
    assert cov["qualifying"] > 10 and cov["evaluated"] == 10 and cov["complete"] is False
    assert cov["not_evaluated"] == cov["qualifying"] - 10
    assert scale_on <= 12 + 10 * ox.READINESS_EVALUATOR_STATEMENTS
    assert not [s for s in seen if FORBIDDEN.search(s)]
    check_page_invariants(page)
    # E pinned: every evaluated visit stays within READINESS_EVALUATOR_STATEMENTS
    with connection.cursor() as cur:
        cur.execute(ox.READINESS_SQL, {"look": 60 * 24 * 30, "at": at, "cap": 10})
        ids = [r[0] for r in cur.fetchall()]
    per_visit = _evaluator_statements(at, [Visit.objects.get(pk=i) for i in ids])
    print(f"\nscale: off={scale_off} on={scale_on} evaluator per visit={per_visit} items={len(page.items)}")
    assert max(per_visit) <= ox.READINESS_EVALUATOR_STATEMENTS
    # every code the scale fixture can produce is present, with unique identity
    codes = {x.code for x in page.items}
    assert {"INCIDENT_UNTRIAGED", "HELD_ROBOT_ASSIGNED", "READINESS_INCOMPLETE_SOON"} <= codes
    assert sw.robot_codes

    # Non-perf guards only. The EM budgets (p95 <= 400 ms without readiness, <= 2,000 ms with it) are measured
    # by Engineer C's `-m perf` test on a quiet host; here: the agreed 2 s non-perf bound without readiness and
    # a loose 5 s guard with 10 evaluator runs (measured ~1.1 s quiet, ~2.2 s at load average 16), best of 3.
    def best(**kw) -> float:
        runs = []
        for _ in range(3):
            t0 = time.perf_counter()
            open_exceptions(at, **kw)
            runs.append(time.perf_counter() - t0)
        return min(runs)

    rules_s = best(include_readiness=False)
    full_s = best()
    with connection.cursor() as cur:
        t0 = time.perf_counter()
        cur.execute(ox.UNKNOWN_BASIS_SQL)
        basis_s = time.perf_counter() - t0
    print(
        f"scale timing (best of 3): without readiness {rules_s * 1000:.0f} ms (unknown-basis query "
        f"{basis_s * 1000:.0f} ms), with 10 readiness evaluations {full_s * 1000:.0f} ms"
    )
    assert rules_s < 2.0 and full_s < 5.0


@pytest.mark.django_db
def test_sql_text_order_is_code_point_order_not_the_database_collation():
    """The database collation (en_US) orders 'R-a1' before 'R-B1' and 'R_1' before 'R-2'; Python and the
    contract order by code point. Same code and same since (one pattern hold), so only entity_ref decides."""
    w = ow.ops_world("co_", robots=1, incident_policy=False)
    codes = ["R-B1", "R-a1", "R-2", "R_1"]
    for n, code in enumerate(codes):
        ow.add_robot(w, code)
        new_confirmed_visit(w, robot=code, d=DAY + timedelta(days=2 + n))
    ow.place_hold(w, "manifest_pattern", "P-A")
    at = db_clock()
    configure()
    sql = [
        x for x in ox._sql_rules(at, ox.read_thresholds()[0], None, None) if x.code == "HELD_ROBOT_ASSIGNED"
    ]
    refs = [x.entity_ref.split("/")[0] for x in sql]
    assert len({x.since for x in sql}) == 1
    assert refs == sorted(refs) == ["R-01", "R-2", "R-B1", "R-a1", "R_1"]
    with connection.cursor() as cur:
        cur.execute("SELECT x FROM unnest(%s::text[]) x ORDER BY x", [codes])
        assert [r[0] for r in cur.fetchall()] != sorted(codes)  # the database collation really differs
    # keyset continues in code-point order from every position (SQL WHERE uses the same collation)
    for i, x in enumerate(sql):
        rest = [
            y
            for y in ox._sql_rules(at, ox.read_thresholds()[0], None, x.sort_key)
            if y.code == "HELD_ROBOT_ASSIGNED"
        ]
        assert rest == sql[i + 1 :]


# ------------------------------------------------------------------ EM decisions B1, B2, include_readiness
@pytest.mark.django_db
def test_unacknowledged_escalation_stays_listed_after_the_incident_is_closed():
    """EM B1: closure is not acknowledgment. The triggers refuse closing with an unacknowledged escalation and
    refuse escalating a closed incident, so the state is simulated as the schema owner (legacy / repaired data)."""
    w = ow.ops_world("ec_", robots=3)
    configure()
    icode = ow.report(w, "R-03")
    e1 = esc.escalate_incident(w.ops, incident_code=icode, to_role="founder", requested_action="SYN")
    (x,) = find(FAR, "ESCALATION_UNACKNOWLEDGED")
    assert x.context == {"incident": icode, "incident_status": "reported"}
    assert x.message.endswith("(incident reported)")
    esc.acknowledge_escalation(w.founder, escalation_id=e1["id"])
    for to in ("triaged", "response_in_progress", "resolved", "closed"):
        inc.change_incident_status(w.safety, incident_code=icode, to_status=to)
    assert not find(FAR, "ESCALATION_UNACKNOWLEDGED") and not find(FAR, "INCIDENT_UNTRIAGED")
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE execution_incidentescalation DISABLE TRIGGER USER")
        cur.execute(
            "INSERT INTO execution_incidentescalation (incident_id, to_role, to_person_id, requested_action,"
            " recorded_by_id, recorded_at) SELECT id, 'engineering_lead', NULL, 'SYNTHETIC late', %s,"
            " clock_timestamp() FROM execution_incident WHERE code = %s RETURNING id",
            [w.ops.pk, icode],
        )
        eid = cur.fetchone()[0]
        cur.execute("ALTER TABLE execution_incidentescalation ENABLE TRIGGER USER")
    (y,) = find(FAR, "ESCALATION_UNACKNOWLEDGED")
    assert y.entity_ref == f"ESC-{eid}" and y.owner_role == "engineering_lead"
    assert y.context == {"incident": icode, "incident_status": "closed"}
    assert "(incident closed)" in y.message
    assert all(z.context == {} for z in items(FAR) if z.code != "ESCALATION_UNACKNOWLEDGED")


@pytest.mark.django_db
def test_return_overdue_without_planned_unloading_end_is_suppressed_and_listed_as_unknown_basis():
    """EM B2 regression: no unloading reservation -> no RETURN_OVERDUE, but the missing basis is exposed."""
    w = ow.ops_world("ub_", robots=3, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    other = ow.depart_new(w, "R-03")
    edge = planned_unloading_end(run) + timedelta(minutes=60)
    assert fires(edge, "RETURN_OVERDUE", run.code)
    assert open_exceptions(edge).unknown_basis == []
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE planning_reservation DISABLE TRIGGER USER")
        cur.execute(
            "DELETE FROM planning_reservation WHERE purpose = 'unloading' AND revision_id = %s",
            [run.revision_id],
        )
        cur.execute("ALTER TABLE planning_reservation ENABLE TRIGGER USER")
    assert planned_unloading_end(run) is None
    page = open_exceptions(FAR)
    assert not [x for x in page.items if x.code == "RETURN_OVERDUE" and x.entity_ref == run.code]
    assert [x.entity_ref for x in page.items if x.code == "RETURN_OVERDUE"] == [other.code]
    assert page.unknown_basis == [
        {"rule": "RETURN_OVERDUE", "run": run.code, "reason": "planned unloading end unknown"}
    ]
    # page-independent, and gone once the vehicle is back at the hub
    assert open_exceptions(FAR, limit=1).unknown_basis == page.unknown_basis
    ow.advance(w, run, "returning")
    w.cmd.trip(run, "arrived_hub")
    assert open_exceptions(FAR).unknown_basis == []


@pytest.mark.django_db
def test_include_readiness_false_runs_no_evaluator():
    w = ow.ops_world("ir_", robots=3, incident_policy=False)
    configure(ops_readiness_lookahead_min=60 * 24 * 60)
    new_confirmed_visit(w, robot="R-03", d=DAY + timedelta(days=2))  # no evidence: incomplete
    at = db_clock()
    full = open_exceptions(at)
    assert any(x.code == "READINESS_INCOMPLETE_SOON" for x in full.items)
    seen, wrapper = capture()
    with connection.execute_wrapper(wrapper):
        page = open_exceptions(at, include_readiness=False)
    assert len(seen) == BASE_STATEMENTS
    assert page.readiness_coverage == {
        "qualifying": 0,
        "evaluated": 0,
        "not_evaluated": 0,
        "complete": False,
        "skipped": True,
    }
    assert page.items == [x for x in full.items if x.code != "READINESS_INCOMPLETE_SOON"]
