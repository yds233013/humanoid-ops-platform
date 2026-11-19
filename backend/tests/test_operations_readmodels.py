"""Slice 5 read models (Engineer C): fleet overview, incident queue, timeline.
docs/05_operations_command_center_contract.md sections 2, 3, 4, 4a, 6.4. SYNTHETIC data only.

Expected values are written out by hand from the scenario each test builds (not recomputed from the views)."""

from datetime import timedelta

import pytest
from django.db import connection

from execution import escalation as esc
from execution import incidents as inc
from execution.models import CustodyEvent, ExecutionRun, Incident
from fleet import services as fs
from fleet.models import DeploymentSetting, Hold
from operations import readmodels as rm
from operations.snapshot import read_snapshot
from planning.models import Reservation, Visit
from readiness.models import DepartureEvent
from tests import operations_world as ow
from tests.fleet_world import set_setting
from tests.planning_world import DAY
from tests.readiness_world import new_confirmed_visit, record_all_evidence

US = timedelta(microseconds=1)
THRESHOLDS = {
    rm.CUSTODY_VERIFY_GRACE: "30",
    rm.RETURN_GRACE: "60",
    rm.INSPECTION_GRACE: "120",
    rm.READINESS_LOOKAHEAD: str(60 * 24 * 60),  # 60 days: every test visit (DAY ~ 3 weeks ahead) qualifies
}
WINDOW = 60 * 24 * 90  # 90 days


def db_now():
    with connection.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


def set_thresholds(**override: str | None) -> None:
    for k, v in {**THRESHOLDS, **override}.items():
        if v is None:
            DeploymentSetting.objects.filter(key=k).delete()
        else:
            set_setting(k, v)


class Counter:
    """Every SQL statement the wrapped block sends (connection.execute_wrapper)."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def __call__(self, execute, sql, params, many, context):
        self.sql.append(sql)
        return execute(sql, params, many, context)

    def __enter__(self):
        self._cm = connection.execute_wrapper(self)
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)

    @property
    def n(self) -> int:
        return len(self.sql)


def by_robot(at=None) -> dict[str, rm.RobotStatus]:
    return {r.robot: r for r in rm.fleet_overview(at or db_now())}


def custody_since(robot_code: str):
    return CustodyEvent.objects.filter(robot__code=robot_code).order_by("-seq").first().recorded_at


# ================================================================== fleet overview (section 2)
@pytest.mark.django_db
def test_fleet_overview_every_status_label_with_hand_written_rows():
    w = ow.ops_world("fo_", robots=9)
    # R-01: Assigned (build_readiness confirmed w.visit for R-01)
    # R-02: At hub - eligible for planning (nothing else)
    # R-03: Held (blocking robot hold)
    held = ow.place_hold(w, "robot", "R-03")
    # R-04: Deployed (departed, unverified administrative assignment)
    run4 = ow.depart_new(w, "R-04")
    # R-05 / R-06 / R-07: Returned - awaiting inspection / reconciliation / trip closure
    run5 = ow.depart_new(w, "R-05")
    ow.advance(w, run5, "at_hub")
    run6 = ow.depart_new(w, "R-06")
    ow.advance(w, run6, "inspected")
    w.cmd.reconcile(run6, inspection=run6._inspection, discrepancies=["SYNTHETIC: strap frayed"])
    run7 = ow.depart_new(w, "R-07")
    ow.advance(w, run7, "reconciled")
    # R-08: Unaccounted (robot_unaccounted incident on a verified run)
    run8 = ow.depart_new(w, "R-08")
    ow.advance(w, run8, "verified")
    lost = ow.report(w, "R-08", event="robot_unaccounted")
    # R-09: At hub - not eligible (base authorization revoked)
    fs.revoke_base_authorization(w.lead, robot_code="R-09", reason="SYNTHETIC")
    # R-10: commissioning, no manifest (not eligible); R-11 retired: absent
    ow.add_robot(w, "R-10", pattern=None, lifecycle="commissioning")
    ow.add_robot(w, "R-11", lifecycle="retired")

    rows = by_robot()
    assert list(rows) == sorted(rows)  # ordered by robot code
    assert "R-11" not in rows
    assert {k: (r.status_rule, r.status_label) for k, r in rows.items()} == {
        "R-01": (6, "Assigned"),
        "R-02": (8, "At hub - eligible for planning"),
        "R-03": (5, "Held"),
        "R-04": (3, "Deployed"),
        "R-05": (4, "Returned - awaiting inspection"),
        "R-06": (4, "Returned - awaiting reconciliation"),
        "R-07": (4, "Returned - awaiting trip closure"),
        "R-08": (1, "Unaccounted"),
        "R-09": (7, "At hub - not eligible"),
        "R-10": (7, "At hub - not eligible"),
    }
    assert rows["R-01"] == rm.RobotStatus(
        robot="R-01",
        robot_id=rows["R-01"].robot_id,
        lifecycle="in_service",
        pattern="P-A",
        manifest_id=rows["R-01"].manifest_id,
        custody=rm.Custody(kind="hub", verified=True, since=None, seq=None),  # pre-Slice-4 baseline
        open_run=None,
        holds=[],
        holds_since=None,
        eligible_for_planning=True,
        eligibility_reasons=[],
        next_visit=rm.NextVisit(
            code=w.visit, on_site_start=Visit.objects.get(code=w.visit).current_revision.on_site_start
        ),
        awaiting="none",
        status_rule=6,
        status_label="Assigned",
        url="/fleet/R-01/",
    )
    assert rows["R-01"].manifest_id is not None
    h = Hold.objects.get(code=held)
    assert (rows["R-03"].holds, rows["R-03"].holds_since) == ([held], h.placed_at)
    r4 = rows["R-04"]
    assert r4.open_run == rm.OpenRun(code=run4.code, visit=run4.visit.code, stage="departed")
    assert (r4.custody.kind, r4.custody.verified, r4.custody.seq) == ("vehicle", False, 1)
    assert r4.custody.since == custody_since("R-04")
    assert r4.next_visit is None  # its only visit departed
    assert (rows["R-05"].awaiting, rows["R-05"].open_run.stage) == ("post_visit_inspection", "at_hub")
    assert (rows["R-06"].awaiting, rows["R-06"].open_run.stage) == (
        "reconciliation",
        "awaiting_reconciliation",
    )
    assert (rows["R-07"].awaiting, rows["R-07"].open_run.stage) == ("trip_closure", "awaiting_closure")
    r8 = rows["R-08"]
    assert (r8.custody.kind, r8.open_run.code) == ("unaccounted", run8.code)
    assert r8.holds == [Hold.objects.get(source_ref=lost).code]
    assert r8.eligible_for_planning is False
    assert rows["R-09"].eligibility_reasons == ["No base home-use authorization for configuration P-A"]
    assert rows["R-10"].eligibility_reasons == [
        "Lifecycle is Commissioning, not In service",
        "No configuration manifest recorded",
    ]
    assert (rows["R-10"].pattern, rows["R-10"].manifest_id) == (None, None)
    for r in rows.values():
        assert "ready" not in r.status_label.lower() and "cleared" not in r.status_label.lower()


@pytest.mark.django_db
def test_held_beats_assigned_and_assigned_shows_eligibility_reasons():
    w = ow.ops_world("fh_", robots=2, incident_policy=False)
    fs.revoke_base_authorization(w.lead, robot_code="R-01", reason="SYNTHETIC")
    r = by_robot()["R-01"]
    assert (r.status_label, r.eligible_for_planning, r.next_visit.code) == ("Assigned", False, w.visit)
    assert r.eligibility_reasons == ["No base home-use authorization for configuration P-A"]
    hold = ow.place_hold(w, "manifest_pattern", "P-A")
    rows = by_robot()
    assert (rows["R-01"].status_label, rows["R-01"].next_visit.code) == ("Held", w.visit)
    assert rows["R-02"].holds == [hold] and rows["R-02"].status_label == "Held"


@pytest.mark.django_db
def test_custody_unknown_label():
    w = ow.ops_world("fk_", robots=2, incident_policy=False)
    run = ow.depart_new(w, "R-02")
    with connection.cursor() as cur:  # broken history, schema owner, test transaction only (as in A's test)
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE execution_custodyevent DISABLE TRIGGER USER")
        cur.execute(
            "INSERT INTO execution_custodyevent (robot_id, seq, from_kind, to_kind, run_id, trip_id, visit_id,"
            " custodian_id, basis, note, recorded_by_id, recorded_at) SELECT robot_id, 5, 'site', 'hub', run_id,"
            " trip_id, visit_id, custodian_id, 'handoff', 'SYNTHETIC gap', recorded_by_id, now()"
            " FROM execution_custodyevent WHERE run_id = %s",
            [run.pk],
        )
        cur.execute("ALTER TABLE execution_custodyevent ENABLE TRIGGER USER")
    r = by_robot()["R-02"]
    assert (r.status_rule, r.status_label, r.custody.kind, r.custody.verified) == (
        2,
        "Custody unknown",
        "unknown",
        False,
    )
    assert r.custody.since is None and r.open_run.code == run.code


@pytest.mark.django_db
def test_fleet_overview_refuses_a_time_that_is_not_the_snapshot_time():
    ow.ops_world("fm_", robots=1, incident_policy=False)
    now = db_now()
    assert len(rm.fleet_overview(now)) == 1
    with pytest.raises(rm.SnapshotTimeMismatch):
        rm.fleet_overview(now + US)


@pytest.mark.django_db
def test_empty_fleet_and_empty_queue():
    now = db_now()
    assert rm.fleet_overview(now) == []
    assert rm.incident_queue(now) == []


@pytest.mark.django_db(transaction=True)
def test_read_models_run_inside_the_read_only_snapshot_in_few_statements():
    w = ow.ops_world("fs_", robots=3)
    set_thresholds()
    run = ow.depart_new(w, "R-02")
    ow.report(w, "R-03")
    with read_snapshot() as at, Counter() as c:
        rows = rm.fleet_overview(at)
        assert c.n == 1
        q = rm.incident_queue(at)
        assert c.n == 2
        t = rm.timeline(at, window_min=WINDOW)
        assert [x.run for x in t.runs] == [run.code]
        assert [v.visit for v in t.visits] == [w.visit]
    assert len(rows) == 3 and len(q) == 1
    joined = "\n".join(c.sql).upper()
    for word in (
        "FOR UPDATE",
        "FOR SHARE",
        "NEXTVAL",
        "SET_CONFIG",
        "ADVISORY",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    ):
        assert word not in joined.replace("FOR UPDATE)", ""), word


# ================================================================== incident queue (section 4, D1)
@pytest.mark.django_db
def test_incident_queue_order_fields_and_suggested_owner():
    w = ow.ops_world("iq_", robots=3)
    a = ow.report(w, "R-01")  # unclassified, oldest
    b = ow.report(w, "R-02")  # high
    c = ow.report(w, "R-03")  # low
    d = ow.report(w, "R-01", event="contact")  # medium
    e = ow.report(w, "R-02", event="other")  # unclassified, newer
    f = ow.report(w, "R-03", event="other")  # high, newer than b
    g = ow.report(w, "R-01", event="other")  # closed: excluded
    for code, level in ((b, "high"), (c, "low"), (d, "medium"), (f, "high"), (g, "low")):
        inc.decide_severity(w.lead, incident_code=code, level=level, reason="SYN")
    for to in ["triaged", "response_in_progress", "resolved", "closed"]:
        inc.change_incident_status(w.safety, incident_code=g, to_status=to)
    inc.change_incident_status(w.safety, incident_code=c, to_status="triaged")
    e1 = esc.escalate_incident(w.ops, incident_code=d, to_role="founder", requested_action="SYN")
    e2 = esc.escalate_incident(
        w.ops, incident_code=d, to_role="engineering_lead", requested_action="SYN", to_person_code="P-5"
    )
    esc.acknowledge_escalation(w.lead2, escalation_id=e2["id"])

    at = db_now()
    q = rm.incident_queue(at)
    # unclassified first (oldest first), then rank high > medium > low, ties by age then code
    assert [x.code for x in q] == [a, e, b, f, d, c]
    by = {x.code: x for x in q}
    ia = Incident.objects.get(code=a)
    assert by[a] == rm.QueuedIncident(
        code=a,
        robot="R-01",
        run=None,
        visit=None,
        event_type="fall",
        occurred_at=ia.occurred_at,
        reported_at=ia.reported_at,
        age=at - ia.reported_at,
        status="reported",
        unclassified=True,
        severity_level=None,
        severity_rank=None,
        hold=Hold.objects.get(source_ref=a).code,
        hold_active=True,
        unacknowledged_escalations=0,
        unacknowledged_escalation_ids=[],
        latest_unacknowledged_at=None,
        suggested_owner_role="ops_lead",
        suggested_owner_person=None,
        url=f"/execution/incidents/{a}/",
    )
    assert (by[b].severity_level, by[b].severity_rank, by[b].unclassified) == ("high", 3, False)
    assert (by[c].status, by[c].severity_rank, by[c].suggested_owner_role) == (
        "triaged",
        1,
        "safety_reviewer",
    )
    # severity decided but still 'reported': ops_lead (status rule), not safety_reviewer
    assert by[b].suggested_owner_role == "ops_lead"
    assert (by[d].unacknowledged_escalations, by[d].unacknowledged_escalation_ids) == (1, [e1["id"]])
    assert (by[d].suggested_owner_role, by[d].suggested_owner_person) == ("founder", None)
    assert g not in by


@pytest.mark.django_db
def test_incident_queue_run_visit_and_released_hold():
    w = ow.ops_world("ir_", robots=2)
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "verified")
    ow.report(w, "R-02")
    x = rm.incident_queue(db_now())[0]
    assert (x.run, x.visit, x.hold_active) == (run.code, run.visit.code, True)


# ================================================================== timeline (sections 4, 4a)
def tl(at=None, window=WINDOW) -> rm.Timeline:
    return rm.timeline(at or db_now(), window_min=window)


@pytest.mark.django_db
def test_timeline_visits_window_order_exclusions_and_readiness_labels():
    w = ow.ops_world("tv_", robots=3)
    set_thresholds()
    later = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=5))
    ready = new_confirmed_visit(w, robot="R-03", d=DAY + timedelta(days=2))
    record_all_evidence(w, ready, robot="R-03")
    cancelled = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=7))
    w.p.cancel_visit(w.ops, visit_code=cancelled, reason="SYNTHETIC")
    departed = ow.depart_new(w, "R-02")  # its visit is departed: an active run, not an upcoming visit
    hold = ow.place_hold(w, "robot", "R-02")

    at = db_now()
    t = tl(at)
    assert [v.visit for v in t.visits] == [w.visit, ready, later]  # by on-site start
    assert departed.visit.code not in {v.visit for v in t.visits}
    by = {v.visit: v for v in t.visits}
    assert by[ready].readiness_evaluated and by[ready].readiness_label == "Ready to request authorization"
    assert by[ready].readiness_blockers == [] and by[ready].flags == []
    assert (
        by[w.visit].readiness_label == "Ready to request authorization"
    )  # build_readiness recorded evidence
    assert by[later].readiness_label == "Readiness incomplete"
    assert "EVIDENCE_MISSING" in by[later].readiness_blockers
    # amendment A1: since = the visit's on_site_start - lookahead (identical to the exception inbox)
    since = by[later].on_site_start - timedelta(minutes=int(THRESHOLDS[rm.READINESS_LOOKAHEAD]))
    h = Hold.objects.get(code=hold)
    assert by[later].flags == [
        rm.Flag("HELD_ROBOT_ASSIGNED", f"R-02/{later}", h.placed_at),
        rm.Flag("READINESS_INCOMPLETE_SOON", later, since),
    ]
    assert (by[later].hold_codes, by[later].robot, by[later].url) == (
        [hold],
        "R-02",
        f"/readiness/visits/{later}/",
    )
    assert t.readiness_coverage == {
        "qualifying": 3,
        "evaluated": 3,
        "not_evaluated": 0,
        "complete": True,
        "skipped": False,
    }
    assert t.unconfigured_thresholds == []

    start = Visit.objects.get(code=w.visit).current_revision.on_site_start
    end = Visit.objects.get(code=w.visit).current_revision.on_site_end
    # a visit whose on-site end has passed at `at` is no longer upcoming; one in progress still is
    in_progress = tl(start + US)
    assert w.visit in {v.visit for v in in_progress.visits}
    assert w.visit not in {v.visit for v in tl(end).visits}


@pytest.mark.django_db
def test_timeline_window_boundary_is_half_open():
    w = ow.ops_world("tw_", robots=2, incident_policy=False)
    set_thresholds()
    start = Visit.objects.get(code=w.visit).current_revision.on_site_start
    at = start - timedelta(minutes=100)
    assert [v.visit for v in rm.timeline(at, window_min=100).visits] == []  # start == at + window: outside
    assert [v.visit for v in rm.timeline(at + US, window_min=100).visits] == [w.visit]


@pytest.mark.django_db
def test_timeline_readiness_lookahead_boundary_is_inclusive():
    w = ow.ops_world("tl_", robots=2, incident_policy=False)
    set_thresholds(**{rm.READINESS_LOOKAHEAD: "60"})
    start = Visit.objects.get(code=w.visit).current_revision.on_site_start
    for at, qualifies in ((start - timedelta(minutes=60) - US, False), (start - timedelta(minutes=60), True)):
        v = rm.timeline(at, window_min=WINDOW).visits[0]
        assert (v.in_lookahead, v.readiness_evaluated) == (qualifies, qualifies)
        assert v.readiness_label == ("Readiness incomplete" if qualifies else "Not evaluated")


@pytest.mark.django_db
def test_timeline_readiness_cap_partial_coverage_and_statement_budget():
    w = ow.ops_world("tc_", robots=3, incident_policy=False)
    set_thresholds()
    codes = [w.visit] + [
        new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=k)) for k in range(1, 12)
    ]
    at = db_now()
    with Counter() as c:
        t = tl(at)
    assert [v.visit for v in t.visits] == codes  # 12 qualifying, in (start, id) order
    assert [v.readiness_evaluated for v in t.visits] == [True] * 10 + [False] * 2
    assert [v.readiness_label for v in t.visits[10:]] == ["Not evaluated"] * 2
    assert all(v.flags == [] for v in t.visits[10:])  # never inferred
    assert t.readiness_coverage == {
        "qualifying": 12,
        "evaluated": 10,
        "not_evaluated": 2,
        "complete": False,
        "skipped": False,
    }

    # budget: thresholds + visits + runs + one Visit load + E per evaluated visit (E measured here)
    from readiness.evaluator import evaluate_readiness

    es = []
    for code in codes[:10]:
        visit = Visit.objects.get(code=code)
        with Counter() as one:
            evaluate_readiness(visit, at=at)
        es.append(one.n)
    e = max(es)
    assert e <= 72  # E pinned at 72: measured worst after EM Option B (2026-11-19; was 70, contract 6.4)
    assert c.n == 4 + sum(es), (c.n, es)  # nothing per visit or per run besides the capped evaluator
    assert c.n <= 12 + 10 * e
    print(f"\ntimeline statements {c.n}, E = {e}")


@pytest.mark.django_db
def test_timeline_lookahead_unconfigured_evaluates_nothing():
    w = ow.ops_world("tu_", robots=2, incident_policy=False)
    for bad in (None, "", "0", "-5", "1.5", " 30", "abc"):
        set_thresholds(**{rm.READINESS_LOOKAHEAD: bad})
        t = tl()
        assert [(v.visit, v.readiness_label, v.in_lookahead) for v in t.visits] == [
            (w.visit, "Not evaluated", False)
        ], bad
        assert t.readiness_coverage == {
            "qualifying": 0,
            "evaluated": 0,
            "not_evaluated": 0,
            "complete": False,
            "skipped": False,
        }
        assert t.unconfigured_thresholds == [rm.READINESS_LOOKAHEAD]


def test_parse_threshold():
    assert [
        rm.parse_threshold(v) for v in ("30", "030", "1", None, "", "0", "-1", "+1", "1.0", " 1", "1 ", "١")
    ] == [
        30,
        30,
        1,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ]


@pytest.mark.django_db
def test_timeline_window_min_must_be_positive_integer():
    for bad in (0, -1, 1.5, True, "10"):
        with pytest.raises(ValueError):
            rm.timeline(db_now(), window_min=bad)


def run_flags(t: rm.Timeline, run: ExecutionRun) -> dict[str, rm.Flag]:
    (r,) = [x for x in t.runs if x.run == run.code]
    return {f.code: f for f in r.flags}


@pytest.mark.django_db
def test_timeline_run_stage_and_custody_unverified_boundary():
    w = ow.ops_world("tr_", robots=2, incident_policy=False)
    set_thresholds()
    run = ow.depart_new(w, "R-02")
    dep = DepartureEvent.objects.get(visit=run.visit).departed_at
    grace = timedelta(minutes=30)
    t = tl(dep + grace - US)
    (r,) = t.runs
    assert (r.run, r.robot, r.visit, r.stage, r.session_state, r.custody_kind, r.custody_verified) == (
        run.code,
        "R-02",
        run.visit.code,
        "departed",
        "not_started",
        "vehicle",
        False,
    )
    assert r.departed_at == dep and r.url == f"/execution/runs/{run.code}/"
    assert "CUSTODY_UNVERIFIED" not in run_flags(t, run)
    for at in (dep + grace, dep + grace + US):
        assert run_flags(tl(at), run)["CUSTODY_UNVERIFIED"] == rm.Flag("CUSTODY_UNVERIFIED", run.code, dep)
    w.cmd.verify(run)
    assert "CUSTODY_UNVERIFIED" not in run_flags(tl(dep + timedelta(days=1)), run)


@pytest.mark.django_db
def test_timeline_custody_unverified_disabled_when_threshold_missing():
    w = ow.ops_world("tm_", robots=2, incident_policy=False)
    set_thresholds(**{rm.CUSTODY_VERIFY_GRACE: "abc"})
    run = ow.depart_new(w, "R-02")
    t = tl(db_now() + timedelta(days=30))
    assert "CUSTODY_UNVERIFIED" not in run_flags(t, run)
    assert t.unconfigured_thresholds == [rm.CUSTODY_VERIFY_GRACE]


@pytest.mark.django_db
def test_timeline_return_overdue_boundary_and_arrival_clears_it():
    w = ow.ops_world("to_", robots=2, incident_policy=False)
    set_thresholds()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "returning")
    planned = (
        Reservation.objects.filter(revision_id=run.revision_id, purpose="unloading")
        .order_by("-ends_at")
        .first()
        .ends_at
    )
    cut = planned + timedelta(minutes=60)
    t = tl(cut - US)
    assert t.runs[0].planned_unloading_end == planned and t.runs[0].stage == "returning"
    assert t.runs[0].return_deadline == cut
    assert "RETURN_OVERDUE" not in run_flags(t, run)
    assert run_flags(tl(cut), run)["RETURN_OVERDUE"] == rm.Flag("RETURN_OVERDUE", run.code, planned)
    assert "RETURN_OVERDUE" in run_flags(tl(cut + US), run)
    w.cmd.trip(run, "arrived_hub")
    t = tl(cut + timedelta(days=1))
    assert "RETURN_OVERDUE" not in run_flags(t, run) and t.runs[0].arrived_hub_at is not None


@pytest.mark.django_db
def test_timeline_unknown_planned_unloading_end_is_an_unknown_deadline_never_invented():
    w = ow.ops_world("td_", robots=2, incident_policy=False)
    set_thresholds()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "returning")
    with connection.cursor() as cur:  # no unloading reservation (owner, test transaction only)
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE planning_reservation DISABLE TRIGGER USER")
        cur.execute(
            "DELETE FROM planning_reservation WHERE revision_id = %s AND purpose = 'unloading'",
            [run.revision_id],
        )
        cur.execute("ALTER TABLE planning_reservation ENABLE TRIGGER USER")
    dep = DepartureEvent.objects.get(visit=run.visit).departed_at
    t = tl(db_now() + timedelta(days=365))
    (r,) = t.runs
    assert (r.planned_unloading_end, r.return_deadline) == (None, None)
    assert [(f.code, f.entity_ref, f.since) for f in r.flags] == [("RETURN_DEADLINE_UNKNOWN", run.code, dep)]
    w.cmd.trip(run, "arrived_hub")  # back at the hub: no deadline is pending any more
    assert "RETURN_DEADLINE_UNKNOWN" not in run_flags(tl(), run)


@pytest.mark.django_db
def test_timeline_return_deadline_unknown_when_grace_unconfigured():
    w = ow.ops_world("tg_", robots=2, incident_policy=False)
    set_thresholds(**{rm.RETURN_GRACE: None})
    run = ow.depart_new(w, "R-02")
    (r,) = tl(db_now() + timedelta(days=365)).runs
    assert r.planned_unloading_end is not None and r.return_deadline is None
    assert "RETURN_OVERDUE" not in run_flags(tl(db_now() + timedelta(days=365)), run)


@pytest.mark.django_db
def test_timeline_post_visit_inspection_missing_boundary_then_reconciliation_unresolved():
    w = ow.ops_world("ti_", robots=2, incident_policy=False)
    set_thresholds()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "at_hub")
    receipt = custody_since("R-02")
    cut = receipt + timedelta(minutes=120)
    assert "POST_VISIT_INSPECTION_MISSING" not in run_flags(tl(cut - US), run)
    assert run_flags(tl(cut), run)["POST_VISIT_INSPECTION_MISSING"] == rm.Flag(
        "POST_VISIT_INSPECTION_MISSING", run.code, receipt
    )
    assert "POST_VISIT_INSPECTION_MISSING" in run_flags(tl(cut + US), run)
    insp = w.cmd.inspect(run)
    w.cmd.reconcile(run, inspection=insp, discrepancies=["SYNTHETIC: strap frayed"])
    flags = run_flags(tl(cut + US), run)
    assert "POST_VISIT_INSPECTION_MISSING" not in flags
    rec_at = run.reconciliations.order_by("-recorded_at").first().recorded_at
    assert flags["RECONCILIATION_UNRESOLVED"] == rm.Flag("RECONCILIATION_UNRESOLVED", run.code, rec_at)
    (r,) = tl().runs
    assert (r.stage, r.awaiting, r.latest_reconciliation) == (
        "awaiting_reconciliation",
        "reconciliation",
        "unresolved",
    )
    w.cmd.reconcile(run, inspection=insp)
    assert "RECONCILIATION_UNRESOLVED" not in run_flags(tl(), run)
    w.cmd.close(run)
    assert tl().runs == []


@pytest.mark.django_db
def test_timeline_site_no_go_interruptions_and_unaccounted():
    w = ow.ops_world("tn_", robots=3)
    set_thresholds()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "at_site")
    w.cmd.site_check(run, conditions={"c1": "ok", "c2": "not_ok"})
    check_at = run.site_checks.order_by("-recorded_at").first().recorded_at
    assert run_flags(tl(), run)["SITE_NO_GO"] == rm.Flag("SITE_NO_GO", run.code, check_at)
    check_id = w.cmd.site_check(run)["id"]
    assert "SITE_NO_GO" not in run_flags(tl(), run)  # the latest check decides
    w.cmd.session(run, "start", site_check_id=check_id)
    i1 = w.cmd.interrupt(run)["code"]
    t = tl()
    (r,) = [x for x in t.runs if x.run == run.code]
    assert r.session_state == "paused"
    assert [(f.code, f.entity_ref) for f in r.flags] == [("INTERRUPTION_OPEN", i1)]
    w.cmd.resolve(i1)
    assert run_flags(tl(), run) == {}

    run3 = ow.depart_new(w, "R-03")
    ow.advance(w, run3, "verified")
    ow.report(w, "R-03", event="robot_unaccounted")
    f = run_flags(tl(), run3)
    assert f["ROBOT_UNACCOUNTED"] == rm.Flag("ROBOT_UNACCOUNTED", "R-03", custody_since("R-03"))


@pytest.mark.django_db
def test_timeline_flags_match_exception_inbox_when_available():
    """Integration check with Engineer B's inbox (skipped until operations.exceptions.open_exceptions exists):
    every run/visit flag on the timeline is an exception of the inbox with the same code, entity_ref and
    since, and the readiness coverage agrees when the window covers the lookahead."""
    ex = pytest.importorskip("operations.exceptions")
    if not hasattr(ex, "open_exceptions"):
        pytest.skip("operations.exceptions.open_exceptions is not available yet (Engineer B)")
    w = ow.ops_world("tx_", robots=4)
    set_thresholds()
    run = ow.depart_new(w, "R-02")
    ow.advance(w, run, "at_hub")
    run3 = ow.depart_new(w, "R-03")
    ow.advance(w, run3, "verified")
    ow.report(w, "R-03", event="robot_unaccounted")
    ow.place_hold(w, "robot", "R-01")
    at = db_now() + timedelta(days=1)
    t = rm.timeline(at, window_min=WINDOW)
    page = ex.open_exceptions(at)
    inbox = {(e.code, e.entity_ref): e.since for e in page.items}
    flags = [f for r in t.runs for f in r.flags] + [f for v in t.visits for f in v.flags]
    assert flags
    for f in flags:
        assert inbox.get((f.code, f.entity_ref)) == f.since, f
    assert page.readiness_coverage == t.readiness_coverage


@pytest.mark.django_db(transaction=True)
def test_two_qualifying_visits_have_identical_readiness_triples_in_timeline_and_inbox():
    """EM review #2: per-visit since = that visit's on_site_start - lookahead, identical in timeline and inbox."""
    from operations.exceptions import open_exceptions

    w = ow.ops_world("tr_", robots=3)
    set_thresholds()
    a = new_confirmed_visit(w, robot="R-02", d=DAY + timedelta(days=3))
    b = new_confirmed_visit(w, robot="R-03", d=DAY + timedelta(days=6))
    with read_snapshot() as at:
        t = rm.timeline(at, window_min=WINDOW)
        page = open_exceptions(at)
    tl_triples = {
        (f.code, f.entity_ref, f.since)
        for v in t.visits
        for f in v.flags
        if f.code == "READINESS_INCOMPLETE_SOON"
    }
    inbox_triples = {
        (x.code, x.entity_ref, x.since) for x in page.items if x.code == "READINESS_INCOMPLETE_SOON"
    }
    mine = {x for x in tl_triples if x[1] in (a, b)}
    assert {x[1] for x in mine} == {a, b}
    sinces = {x[1]: x[2] for x in mine}
    assert sinces[a] != sinces[b]  # each visit has its own since
    assert mine <= inbox_triples
