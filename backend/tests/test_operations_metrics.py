"""Slice 5 metrics (Engineer C). docs/05_operations_command_center_contract.md section 4 (metrics table and
"Historical custody at vehicle arrival"), R2, R3. SYNTHETIC data only.

Expected values are computed by hand from the scenario (reservation layout of tests.planning_world visits:
robot block [S - 80 min, S + 290 min) around on-site start S, on-site 1 h, travel 30 min; DAY is in PST, so
10:00 local = 18:00 UTC)."""

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection

from execution import incidents as inc
from execution.models import CustodyEvent, ExecutionRun, TripEvent
from operations import metrics as om
from readiness.models import DepartureEvent
from tests import operations_world as ow
from tests.planning_world import DAY, plan
from tests.planning_world import new_visit as plan_new_visit
from tests.readiness_world import new_confirmed_visit

US = timedelta(microseconds=1)


def clock():
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def departed_at(run: ExecutionRun):
    return DepartureEvent.objects.get(visit_id=run.visit_id).departed_at


def closed_at(run: ExecutionRun):
    run.refresh_from_db()
    return run.closure.recorded_at


def utc(day_offset: int, hh: int, mm: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=UTC) + timedelta(days=day_offset)


# ================================================================== cohort, turnaround, incident rate
@pytest.mark.django_db
def test_departure_cohort_completion_turnaround_and_incident_rate():
    w = ow.ops_world("mc_", robots=4)
    a = ow.depart_new(w, "R-02")  # departed BEFORE the window, closed inside it: not in the cohort
    start = departed_at(a) + US
    b = ow.depart_new(w, "R-03")
    ow.advance(w, a, "closed")
    ow.advance(w, b, "closed")
    d = ow.depart_new(w, "R-04")
    ow.advance(w, d, "closed")
    c = ow.depart_new(w, "R-02")  # open
    t_inside = start + timedelta(microseconds=5)
    for occurred in (start - US, t_inside):  # one before the window, one inside
        inc.report_incident(
            w.field,
            robot_code="R-01",
            event_type="other",
            description="SYNTHETIC",
            occurred_at=occurred,
            knowledge_source="witnessed",
        )
    end = clock() + US
    at = end

    m = om.metrics(start, end, at)
    comp = m.get("departure_cohort_completion")
    assert (comp.numerator, comp.denominator, comp.value) == (2, 3, 2 / 3)
    assert comp.kind == "historical cohort" and comp.display == "66.7%"

    d1 = (closed_at(b) - departed_at(b)).total_seconds() / 60
    d2 = (closed_at(d) - departed_at(d)).total_seconds() / 60
    lo, hi = sorted([d1, d2])
    med, p90 = m.get("turnaround_median"), m.get("turnaround_p90")
    assert med.denominator == 2 and med.value == pytest.approx((lo + hi) / 2)
    assert p90.value == pytest.approx(lo + 0.9 * (hi - lo))
    assert med.kind == p90.kind == "historical cohort"

    rate = m.get("incident_rate_per_10_departures")
    assert (rate.numerator, rate.denominator, rate.value) == (1, 3, pytest.approx(10 / 3))
    per_robot = m.get("incident_rate_per_departed_robot")
    assert (per_robot.numerator, per_robot.denominator, per_robot.value) == (1, 3, pytest.approx(1 / 3))
    assert rate.kind == per_robot.kind == "event count"
    assert m.get("open_incidents").value == 2 and m.get("open_incidents").kind == "current state at at"
    assert not hasattr(c, "_inspection")  # c stays open

    # `at` before b's closure: b is in the cohort but not yet closed; d and c had not departed yet
    m2 = om.metrics(start, end, closed_at(b) - US)
    comp2 = m2.get("departure_cohort_completion")
    assert (comp2.numerator, comp2.denominator) == (0, 1)
    assert m2.get("turnaround_median").value is None and m2.get("turnaround_median").display == "N/A"
    # closure exactly at `at` counts ("by at")
    assert om.metrics(start, end, closed_at(b)).get("departure_cohort_completion").numerator == 1
    # half-open window: a departure exactly at `end` is outside, exactly at `start` inside
    assert om.metrics(departed_at(b), departed_at(d), at).get("departure_cohort_completion").denominator == 1
    # one closed run: median = p90 = its duration
    m3 = om.metrics(start, departed_at(d), at)
    assert m3.get("turnaround_median").value == pytest.approx(d1)
    assert m3.get("turnaround_p90").value == pytest.approx(d1)


@pytest.mark.django_db
def test_zero_denominators_are_na_never_zero():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    m = om.metrics(t0, t0 + timedelta(days=1), clock())
    na = {x.key for x in m.items if x.value is None}
    assert na >= {
        "departure_cohort_completion",
        "turnaround_median",
        "turnaround_p90",
        "incident_rate_per_10_departures",
        "incident_rate_per_departed_robot",
        "scheduled_robot_utilization",  # no robot in service
    }
    for key in na:
        assert m.get(key).display == "N/A"
    rwr = m.get("return_without_robot")
    assert (rwr.value, rwr.denominator, rwr.details["share"]) == (0, 0, None)
    assert m.get("open_incidents").value == 0
    assert m.get("departure_cohort_completion").denominator == 0


@pytest.mark.django_db
def test_window_validation():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        om.metrics(t0, t0, t0)
    with pytest.raises(ValueError):
        om.metrics(t0 + US, t0, t0)
    with pytest.raises(ValueError):
        om.metrics(t0.replace(tzinfo=None), t0 + US, t0)


@pytest.mark.django_db
def test_every_metric_states_kind_definition_and_synthetic_label():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    m = om.metrics(t0, t0 + timedelta(days=1), clock())
    assert m.note == "Synthetic-demo measures, not targets."
    assert [x.key for x in m.items] == [
        "departure_cohort_completion",
        "turnaround_median",
        "turnaround_p90",
        "return_without_robot",
        "incident_rate_per_10_departures",
        "incident_rate_per_departed_robot",
        "scheduled_robot_utilization",
        "open_incidents",
        "open_exceptions",
    ]
    for x in m.items:
        assert x.kind in {
            om.KIND_COHORT,
            om.KIND_EVENT,
            om.KIND_EVENT_ORDER,
            om.KIND_UTILIZATION,
            om.KIND_CURRENT,
        }
        assert x.definition
    assert m.window_min == 1440


# ================================================================== scheduled utilization
@pytest.mark.django_db
def test_scheduled_utilization_union_clipping_status_and_current_fleet_denominator():
    w = ow.ops_world("mu_", robots=3, incident_policy=False)
    # window [Dec 8 18:00Z, Dec 11 18:00Z) = 4320 min
    start, end = utc(0, 18), utc(3, 18)
    # R-01: w.visit, S = Dec 8 18:00Z, block [16:40, 22:50) -> clipped at the start: 290 min
    # R-02: confirmed Dec 9 10:00 local (S 18:00Z): whole block 370 min
    new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=1))
    # R-02: confirmed Dec 11 07:00 local (S 15:00Z): block [13:40, 20:50) clipped at the end: 260 min
    new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=3), h0=7, h1=8)
    # R-03: DEPARTED visit Dec 10 10:00 local: whole block 370 min
    w.next_day = 2
    ow.depart_new(w, "R-03")
    # R-03: planned (not confirmed) Dec 11 11:00 local (S 19:00Z): would add [17:40, 18:00) = 20 min: excluded
    code = plan_new_visit(w, d=DAY + timedelta(days=3), h0=11, h1=12)
    plan(w, code, robot="R-03")
    # R-01: cancelled Dec 10 16:00 local (S Dec 11 00:00Z): released, excluded
    cancelled = new_confirmed_visit(w, robot="R-01", d=DAY + timedelta(days=2), h0=16, h1=17)
    w.p.cancel_visit(w.ops, visit_code=cancelled, reason="SYNTHETIC")
    # R-05: not in service, no visits: not in the denominator
    ow.add_robot(w, "R-05", lifecycle="validation")

    m = om.metrics(start, end, clock())
    u = m.get("scheduled_robot_utilization")
    busy = 290 + 370 + 260 + 370
    assert (u.numerator, u.denominator) == (pytest.approx(busy), pytest.approx(3 * 4320))
    assert u.value == pytest.approx(busy / (3 * 4320))
    assert u.kind == "event over window, current-fleet denominator"
    assert u.details == {"denominator_label": om.CURRENT_FLEET_NOTE, "in_service_robots": 3}
    assert "lifecycle history before `at` is not reconstructed" in u.definition

    # Overlapping reservations of one robot count once (per-robot union, not a sum). Planning's exclusion
    # constraint forbids the overlap, so the owner drops it inside this test transaction only.
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE planning_reservation DROP CONSTRAINT reservation_no_overlap")
        cur.execute("ALTER TABLE planning_reservation DISABLE TRIGGER USER")
        cur.execute(
            "INSERT INTO planning_reservation (purpose, starts_at, ends_at, released_reason, resource_id,"
            " revision_id) SELECT 'on_site', %s, %s, '', resource_id, revision_id FROM planning_reservation"
            " WHERE purpose = 'on_site' AND revision_id = (SELECT current_revision_id FROM planning_visit"
            " WHERE code = %s)",
            [utc(0, 17, 30), utc(0, 23, 20), w.visit],
        )
        cur.execute("ALTER TABLE planning_reservation ENABLE TRIGGER USER")
    u2 = om.metrics(start, end, clock()).get("scheduled_robot_utilization")
    # R-01: [16:40, 22:50) U [17:30, 23:20) clipped to [18:00, ...) = [18:00, 23:20) = 320 min (a sum: 610)
    assert u2.numerator == pytest.approx(busy - 290 + 320)


# ================================================================== return without robot (event order)
def arrival(run: ExecutionRun) -> TripEvent:
    return TripEvent.objects.get(run=run, kind="arrived_hub")


@pytest.mark.django_db
def test_return_without_robot_classification_four_cases_and_legacy_null():
    w = ow.ops_world("mr_", robots=6)
    t0 = clock()
    # 1. with_robot; a hub receipt after the arrival (same transaction too) leaves it unchanged
    with_robot = ow.depart_new(w, "R-02")
    ow.advance(w, with_robot, "at_hub")
    # 2. robot_unaccounted: the vehicle returns after the robot went unaccounted, then it is recovered
    lost = ow.depart_new(w, "R-03")
    ow.advance(w, lost, "verified")
    ow.report(w, "R-03", event="robot_unaccounted")
    w.cmd.trip(lost, "arrived_hub")
    w.cmd.move(lost, "hub", note="SYNTHETIC: found and received")
    # 3. robot_at_site: robot left on site
    left = ow.depart_new(w, "R-04")
    ow.advance(w, left, "at_site")
    icode = ow.report(w, "R-04", event="other")
    w.cmd.disposition(left, "robot_left_on_site", incident=icode)
    w.cmd.trip(left, "departed_site")
    w.cmd.trip(left, "arrived_hub")
    # 4. robot_already_at_hub (unreachable through the triggers) and 5. legacy NULL: owner inserts with the
    # trip-event guard off, inside the test transaction only
    hub_run = ow.depart_new(w, "R-05")
    ow.advance(w, hub_run, "returning")
    legacy = ow.depart_new(w, "R-06")
    ow.advance(w, legacy, "returning")
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE execution_tripevent DISABLE TRIGGER execution_trip_event_guard")
        # the classification looks up the custody event by (the run's robot, seq): give R-05 a hub event seq
        r5_hub = CustodyEvent.objects.filter(robot__code="R-05").order_by("-seq").first()
        cur.execute(
            "ALTER TABLE execution_custodyevent DISABLE TRIGGER USER; "
            "INSERT INTO execution_custodyevent (robot_id, seq, from_kind, to_kind, run_id, trip_id, visit_id,"
            " custodian_id, received_by_id, basis, note, recorded_by_id, recorded_at) SELECT robot_id, seq + 1,"
            " to_kind, 'hub', run_id, trip_id, visit_id, custodian_id, received_by_id, 'handoff',"
            " 'SYNTHETIC early hub', recorded_by_id, now() FROM execution_custodyevent WHERE id = %s; "
            "ALTER TABLE execution_custodyevent ENABLE TRIGGER USER",
            [r5_hub.pk],
        )
        cur.execute(
            "INSERT INTO execution_tripevent (trip_id, run_id, seq, kind, recorded_by_id, recorded_at,"
            " custody_seq_at_event) VALUES (%s, %s, 3, 'arrived_hub', %s, clock_timestamp(), %s),"
            " (%s, %s, 3, 'arrived_hub', %s, clock_timestamp(), NULL)",
            [hub_run.trip_id, hub_run.pk, w.field.pk, r5_hub.seq + 1, legacy.trip_id, legacy.pk, w.field.pk],
        )
        cur.execute("ALTER TABLE execution_tripevent ENABLE TRIGGER execution_trip_event_guard")
        cur.execute(
            "SELECT custody_seq_at_event FROM execution_tripevent WHERE id = %s", [arrival(legacy).pk]
        )
        assert cur.fetchone() == (None,)

    end = clock() + US
    m = om.metrics(t0, end, end)
    rwr = m.get("return_without_robot")
    assert rwr.details["by_class"] == {
        "with_robot": 1,
        "robot_unaccounted": 1,
        "robot_at_site": 1,
        "robot_already_at_hub": 1,
        "ambiguous": 1,
    }
    assert (rwr.numerator, rwr.denominator, rwr.value) == (2, 5, 2)
    assert rwr.details["share"] == pytest.approx(2 / 5)
    assert rwr.kind == "event count, historical state by event order" and rwr.display == "2"

    # Arrivals are counted in the window by their own time; one exactly at `end` is outside.
    m2 = om.metrics(t0, arrival(lost).recorded_at, end)
    assert m2.get("return_without_robot").details["by_class"]["with_robot"] == 1
    assert sum(m2.get("return_without_robot").details["by_class"].values()) == 1
    # `at` before an arrival hides it
    m3 = om.metrics(t0, end, arrival(left).recorded_at - US)
    assert m3.get("return_without_robot").details["by_class"]["robot_at_site"] == 0


@pytest.mark.django_db
def test_hub_receipt_in_the_same_transaction_keeps_with_robot():
    from django.db import transaction

    w = ow.ops_world("ms_", robots=2, incident_policy=False)
    t0 = clock()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "returning")
    with transaction.atomic():
        w.cmd.trip(run, "arrived_hub")
        w.cmd.move(run, "hub", receiver="P-3")
    end = clock() + US
    by = om.metrics(t0, end, end).get("return_without_robot").details["by_class"]
    assert by == {
        "with_robot": 1,
        "robot_unaccounted": 0,
        "robot_at_site": 0,
        "robot_already_at_hub": 0,
        "ambiguous": 0,
    }


# ================================================================== open exceptions (Engineer B)
@pytest.mark.django_db
def test_open_exceptions_metric_never_runs_the_evaluator_and_is_labelled(monkeypatch):
    """EM decision: the metrics page counts the inbox WITHOUT readiness, labels it so, never as the total,
    and never calls the readiness evaluator."""
    import inspect

    import readiness.evaluator as rev

    def boom(*a, **k):
        raise AssertionError("the metrics page must not run the readiness evaluator")

    monkeypatch.setattr(rev, "evaluate_readiness", boom)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    at = clock()
    oe = om.metrics(t0, t0 + timedelta(days=1), at).get("open_exceptions")
    assert oe.label == "Open operational exceptions - readiness not evaluated"
    assert "not the total" in oe.definition and oe.kind == "current state at at"
    spec = importlib.util.find_spec("operations.exceptions")
    ex = importlib.import_module("operations.exceptions") if spec else None
    fn = getattr(ex, "open_exceptions", None)
    if fn is None or "include_readiness" not in inspect.signature(fn).parameters:
        assert (oe.value, oe.display) == (None, "N/A")
        assert oe.details == {
            "unavailable": "the exception inbox without readiness evaluation is not installed"
        }
    else:
        monkeypatch.undo()
        page = fn(at, include_readiness=False)
        assert oe.value == len(page.items)
        assert oe.details == {"readiness": "not evaluated", "total": False}
        assert all(e.code != "READINESS_INCOMPLETE_SOON" for e in page.items)
