"""v2 02D fleet health + predictor log (docs/11_v2_phase02_contract.md section 2 "Fleet health" / "Predictive
models: candidate predictors are logged only", 4 milestone 02D, 8.8). SYNTHETIC data only.

Oracles: every health figure is recomputed here in plain Python from the source rows (lifecycle audit events,
holds, incidents, severity decisions) and compared with ``operations.health.fleet_health``. Hand-built worlds with
known answers: a hold spanning the window start, a robot put in service mid-window, a released and a still-open
hold, a non-blocking hold, a configuration-pattern hold, two incidents of the same type (a repeat failure) and of
different types (not a repeat)."""

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.utils import timezone

from execution import incidents as inc
from execution.models import Incident, SeverityDecision
from fleet.models import Hold, Robot
from operations import health as oh
from platform_core.models import AuditEvent
from tests import operations_world as ow

US = timedelta(microseconds=1)


def clock() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def report(w: Any, robot: str, event: str) -> str:
    return str(
        inc.report_incident(
            w.field,
            robot_code=robot,
            event_type=event,
            description="SYNTHETIC incident",
            occurred_at=timezone.now(),
            knowledge_source="witnessed",
        )["code"]
    )


# ------------------------------------------------------------------ the independent Python oracle
Interval = tuple[datetime, datetime]
FAR = datetime.max.replace(tzinfo=timezone.get_current_timezone())


def merge(ivs: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(i for i in ivs if i[0] < i[1]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def clip(ivs: list[Interval], ws: datetime, we: datetime) -> list[Interval]:
    return merge([(max(s, ws), min(e, we)) for s, e in ivs])


def intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    return merge([(max(s1, s2), min(e1, e2)) for s1, e1 in a for s2, e2 in b])


def seconds(ivs: list[Interval]) -> float:
    return sum((e - s).total_seconds() for s, e in ivs)


def oracle(start: datetime, end: datetime, at: datetime) -> dict[str, Any]:
    we = min(end, at)
    robots = {r.id: r for r in Robot.objects.select_related("current_manifest")}
    by_code = {r.code: r for r in robots.values()}
    trans: dict[int, list[tuple[datetime, int, str]]] = {}
    for a in AuditEvent.objects.filter(
        entity_type="robot", outcome="committed", command__startswith="fleet.lifecycle.", occurred_at__lte=at
    ):
        trans.setdefault(by_code[a.entity_id].id, []).append((a.occurred_at, a.id, a.result["to"]))
    service: dict[int, list[Interval]] = {}
    for rid, ts in trans.items():
        ts.sort()
        ivs = []
        for k, (t, _, to) in enumerate(ts):
            nxt = ts[k + 1][0] if k + 1 < len(ts) else FAR
            if to == "in_service":
                ivs.append((t, nxt))
        service[rid] = clip(ivs, start, we)

    def applies(h: Hold) -> list[int]:
        if h.scope == "robot":
            return [h.robot_id]
        return [
            r.id
            for r in robots.values()
            if r.current_manifest and r.current_manifest.pattern_id == h.pattern_id
        ]

    holds: dict[int, list[dict[str, Any]]] = {}
    for h in Hold.objects.filter(placed_at__lte=at):
        rel = h.released_at if h.released_at is not None and h.released_at <= at else None
        if not (h.placed_at < we and (rel is None or rel > start)):
            continue
        for rid in applies(h):
            holds.setdefault(rid, []).append(
                {
                    "code": h.code,
                    "source": h.source_type,
                    "blocking": h.blocking,
                    "placed": h.placed_at,
                    "rel": rel,
                }
            )
    incidents: dict[int, list[tuple[str, str]]] = {}
    for i in Incident.objects.filter(occurred_at__gte=start, occurred_at__lt=we, reported_at__lte=at):
        dec = (
            SeverityDecision.objects.filter(incident=i, recorded_at__lte=at)
            .order_by("-recorded_at", "-id")
            .first()
        )
        incidents.setdefault(i.robot_id, []).append((i.event_type, dec.level if dec else "unclassified"))

    out: dict[str, Any] = {"robots": {}}
    num = den = 0.0
    for rid in (
        {k for k, v in service.items() if v}
        | set(holds)
        | set(incidents)
        | {r.id for r in robots.values() if r.lifecycle == "in_service"}
    ):
        svc = service.get(rid, [])
        blocked = clip(
            [(h["placed"], h["rel"] or FAR) for h in holds.get(rid, []) if h["blocking"]], start, we
        )
        s_s, h_s = seconds(svc), seconds(intersect(svc, blocked))
        rec = rid in trans
        if rec:
            num, den = num + s_s - h_s, den + s_s
        types = Counter(t for t, _ in incidents.get(rid, []))
        out["robots"][robots[rid].code] = {
            "service_s": s_s,
            "held_s": h_s,
            "uptime": (s_s - h_s) / s_s if rec and s_s > 0 else None,
            "holds_by_source": dict(Counter(h["source"] for h in holds.get(rid, []))),
            "holds_open": sum(1 for h in holds.get(rid, []) if h["rel"] is None),
            "incidents_by_type": dict(types),
            "incidents_by_severity": dict(Counter(s for _, s in incidents.get(rid, []))),
            "repeat": {t: n for t, n in types.items() if n >= 2},
        }
    distinct = {h["code"]: h for hs in holds.values() for h in hs}
    out["fleet"] = {
        "uptime": num / den if den > 0 else None,
        "num": num,
        "den": den,
        "holds_by_source": dict(Counter(h["source"] for h in distinct.values())),
        "holds_total": len(distinct),
        "holds_open": sum(1 for h in distinct.values() if h["rel"] is None),
        "incidents_total": sum(len(v) for v in incidents.values()),
    }
    return out


def assert_matches_oracle(fh: oh.FleetHealth) -> dict[str, Any]:
    o = oracle(fh.start, fh.requested_end, fh.at)
    assert {r.code for r in fh.robots} == set(o["robots"])
    for r in fh.robots:
        e = o["robots"][r.code]
        assert r.service_s == pytest.approx(e["service_s"], abs=1e-6), r.code
        assert r.held_s == pytest.approx(e["held_s"], abs=1e-6), r.code
        if e["uptime"] is None:
            assert r.uptime.value is None and r.uptime.display == "N/A", r.code
        else:
            assert r.uptime.value == pytest.approx(e["uptime"], abs=1e-9), r.code
        assert r.holds_by_source == e["holds_by_source"], r.code
        assert r.holds_open == e["holds_open"], r.code
        assert r.incidents_by_type == e["incidents_by_type"], r.code
        assert r.incidents_by_severity == e["incidents_by_severity"], r.code
        assert r.repeat_failures == e["repeat"], r.code
    f = o["fleet"]
    if f["uptime"] is None:
        assert fh.uptime.value is None
    else:
        assert fh.uptime.value == pytest.approx(f["uptime"], abs=1e-9)
        assert fh.uptime.numerator_s == pytest.approx(f["num"], abs=1e-6)
        assert fh.uptime.denominator_s == pytest.approx(f["den"], abs=1e-6)
    assert fh.holds_by_source == f["holds_by_source"]
    assert (fh.holds_total, fh.holds_open) == (f["holds_total"], f["holds_open"])
    assert fh.incidents_total == f["incidents_total"]
    return o


# ------------------------------------------------------------------ the hand-built world
def health_world() -> Any:
    """R-01..R-03 in service before the window. Then, in order:
    H-span: blocking manual hold on R-01, placed BEFORE the window start and never released (spans the start);
    -- window start --
    R-04 registered and put in service (mid-window); R-05 put in service, then back to validation;
    H-rel: blocking engineering hold on R-02, placed and released inside the window;
    H-soft: NON-blocking manual hold on R-03 (counted in holds, never in held time);
    two 'fall' incidents on R-03 (each places a blocking incident hold): a repeat failure;
    'fall' + 'contact' on R-04: not a repeat; a severity decision on one R-03 incident."""
    w = ow.ops_world("hw_", robots=3)
    w.h_span = ow.place_hold(w, "robot", "R-01", source="manual")
    w.start = clock()
    ow.add_robot(w, "R-04")
    w.r04_in_service = AuditEvent.objects.get(
        command="fleet.lifecycle.in_service", entity_id="R-04"
    ).occurred_at
    ow.add_robot(w, "R-05")
    from fleet import services as fs

    fs.transition_lifecycle(w.lead, robot_code="R-05", to="validation", reason="SYNTHETIC")
    w.h_rel = ow.place_hold(w, "robot", "R-02", source="engineering")
    ow.release(w, w.h_rel)
    w.h_soft = ow.place_hold(w, "robot", "R-03", source="manual", blocking=False)
    w.i1 = report(w, "R-03", "fall")
    w.i2 = report(w, "R-03", "fall")
    w.i3 = report(w, "R-04", "fall")
    w.i4 = report(w, "R-04", "contact")
    inc.decide_severity(w.lead, incident_code=w.i1, level="medium", reason="SYNTHETIC")
    w.at = clock()
    return w


@pytest.mark.django_db
def test_health_matches_the_python_oracle_and_known_answers():
    w = health_world()
    fh = oh.fleet_health(w.start, w.at, w.at)
    o = assert_matches_oracle(fh)
    window = (w.at - w.start).total_seconds()

    r1 = fh.robot("R-01")  # in service all window, held all window by H-span (spans the start): uptime 0
    assert r1.service_s == pytest.approx(window) and r1.held_s == pytest.approx(window)
    assert r1.uptime.value == pytest.approx(0.0) and r1.holds_open == 1
    assert r1.holds_by_source == {"manual": 1}

    r2 = fh.robot("R-02")  # released hold: held only between placement and release, then free
    h = Hold.objects.get(code=w.h_rel)
    assert r2.held_s == pytest.approx((h.released_at - h.placed_at).total_seconds())
    assert 0 < r2.uptime.value < 1 and (r2.holds_open, r2.holds_released) == (0, 1)
    assert r2.holds_by_source == {"engineering": 1}

    r3 = fh.robot("R-03")  # non-blocking hold never counts as held time; incident holds do
    first_incident_hold = Hold.objects.filter(source_type="incident", robot__code="R-03").order_by(
        "placed_at"
    )[0]
    assert r3.held_s == pytest.approx((w.at - first_incident_hold.placed_at).total_seconds())
    assert r3.holds_by_source == {"incident": 2, "manual": 1}
    assert r3.repeat_failures == {"fall": 2}
    assert r3.incidents_by_severity == {"medium": 1, "unclassified": 1}

    r4 = fh.robot("R-04")  # commissioned mid-window: prorated from its in_service event
    assert r4.service_s == pytest.approx((w.at - w.r04_in_service).total_seconds())
    assert r4.service_s < window
    assert r4.incidents_by_type == {"contact": 1, "fall": 1} and r4.repeat_failures == {}

    r5 = fh.robot("R-05")  # in service for a moment, then validation: counted only for that span
    assert 0 < r5.service_s < r4.service_s and r5.lifecycle == "validation"

    assert fh.repeat_failures == [("R-03", "fall", 2)] and fh.repeat_failure_robots == 1
    assert fh.incidents_total == 4 and fh.incidents_by_type == {"contact": 1, "fall": 3}
    assert fh.holds_total == 1 + 1 + 1 + 4 and fh.holds_open == 6
    assert o["fleet"]["den"] == pytest.approx(3 * window + r4.service_s + r5.service_s)


@pytest.mark.django_db
def test_window_after_a_release_and_before_the_world():
    w = health_world()
    h = Hold.objects.get(code=w.h_rel)
    # a window that starts after H-rel's release: R-02 is not held at all and the hold is not listed
    fh = oh.fleet_health(h.released_at + US, w.at, w.at)
    assert_matches_oracle(fh)
    assert fh.robot("R-02").held_s == 0 and fh.robot("R-02").holds_by_source == {}
    # an `at` before the release: H-rel still open as of then, and later rows are invisible
    fh2 = oh.fleet_health(w.start, w.at, h.released_at - US)
    assert_matches_oracle(fh2)
    assert fh2.robot("R-02").holds_open == 1 and fh2.incidents_total == 0
    # a window that ends before the world existed: no robot in service, every share N/A
    t0 = w.start - timedelta(days=400)
    fh3 = oh.fleet_health(t0, t0 + timedelta(days=1), w.at)
    assert_matches_oracle(fh3)
    assert fh3.uptime.value is None and fh3.uptime.display == "N/A"
    assert all(r.uptime.value is None for r in fh3.robots)


@pytest.mark.django_db
def test_pattern_hold_applies_to_every_robot_on_the_pattern_and_counts_once():
    w = health_world()
    code = ow.place_hold(w, "manifest_pattern", "P-A", source="engineering")
    at = clock()
    fh = oh.fleet_health(w.start, at, at)
    assert_matches_oracle(fh)
    on_pattern = [
        r.code for r in fh.robots if r.holds_by_source.get("engineering", 0) >= 1 and r.code != "R-02"
    ]
    assert len(on_pattern) >= 2
    assert sum(1 for r in fh.robots for h in r.holds if h["code"] == code) == len(on_pattern) + (
        1 if any(h["code"] == code for h in fh.robot("R-02").holds) else 0
    )
    assert fh.holds_total == 7 + 1  # counted once for the fleet


@pytest.mark.django_db
def test_end_after_at_is_clipped_and_bad_windows_refused():
    w = health_world()
    fh = oh.fleet_health(w.start, w.at + timedelta(days=3), w.at)
    assert fh.end == w.at and fh.end_clipped
    assert_matches_oracle(fh)
    with pytest.raises(ValueError):
        oh.fleet_health(w.at, w.at, w.at)
    with pytest.raises(ValueError):
        oh.fleet_health(w.at + US, w.at + timedelta(days=1), w.at)  # starts after the snapshot
    with pytest.raises(ValueError):
        oh.fleet_health(w.start.replace(tzinfo=None), w.at, w.at)


@pytest.mark.django_db
def test_in_service_robot_without_recorded_lifecycle_history_is_na_not_assumed():
    w = health_world()
    with connection.cursor() as cur:  # TEST-ONLY owner write: a robot in service with no lifecycle event
        cur.execute(
            "INSERT INTO fleet_robot (code, serial, lifecycle, version, registered_by_id, registered_at)"
            " VALUES ('R-09', 'hw_SYN-R-09', 'commissioning', 1, %s, now())",
            [w.ops.pk],
        )
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE fleet_robot DISABLE TRIGGER USER")
        cur.execute("UPDATE fleet_robot SET lifecycle = 'in_service' WHERE code = 'R-09'")
        cur.execute("ALTER TABLE fleet_robot ENABLE TRIGGER USER")
        cur.execute("SET CONSTRAINTS ALL DEFERRED")
    at = clock()
    fh = oh.fleet_health(w.start, at, at)
    assert_matches_oracle(fh)
    r9 = fh.robot("R-09")
    assert not r9.history_recorded and r9.uptime.value is None and fh.no_history == ["R-09"]


@pytest.mark.django_db
def test_every_metric_states_its_parts_and_what_it_does_not_establish():
    w = health_world()
    fh = oh.fleet_health(w.start, w.at, w.at)
    assert fh.note.startswith("Synthetic demo data")
    assert set(fh.does_not_establish) == {"uptime", "holds", "incidents", "repeat"}
    assert "prorated" in fh.denominator_text
    text = " ".join([fh.note, fh.denominator_text, *fh.does_not_establish.values()]).lower()
    for word in ("safe", "cleared", "reliable", "guarantee"):
        assert word not in text


# ================================================================== predictor log (observational only)
def predictor_world(prefix: str = "pl_") -> Any:
    """R-01: maintenance overdue (a 1-visit rule, a record, then one closed visit; the gate is NOT active, so
    nothing blocks), a 72% reading, a non-blocking hold, then departs. R-02: no reading, departs; a reading and an
    incident recorded AFTER its departure must not count. R-03: an incident resolved (not closed) and its hold
    released, then departs: one open incident at departure."""
    from datetime import UTC

    from fleet import maintenance as mnt
    from fleet.models import MaintenanceRecord
    from hub import charging as hc
    from planning import services as ps
    from tests.conftest import make_user

    w = ow.ops_world(prefix, robots=3)
    w.tech = make_user(f"{prefix}tech", "hub_technician")
    ps.register_person(w.ops, username=w.tech.username, code="P-9")
    ps.grant_qualification(
        w.safety,
        person_code="P-9",
        kind="maintenance_technician",
        valid_from=None,
        valid_to=datetime(2027, 6, 1, tzinfo=UTC),
        basis_ref="SYNTHETIC-MNT-TRAINING",
    )
    rid = int(
        mnt.propose_rule(
            w.eng,
            pattern_code="P-A",
            requirement_code="JOINT",
            visit_count=1,
            warning_pct=0,
            source="synthetic_demo",
        )["rule_id"]
    )
    mnt.approve_rule(w.lead, rule_id=rid, slot="engineering")
    mnt.approve_rule(w.safety, rule_id=rid, slot="safety")
    rec = int(mnt.record_maintenance(w.tech, robot_code="R-01", requirement_code="JOINT")["record_id"])
    assert MaintenanceRecord.objects.filter(pk=rec).exists()
    w.run0 = ow.depart_new(w, "R-01")  # one completed visit since the record: the 1-visit rule is now overdue
    ow.advance(w, w.run0, "closed")
    w.rule_id = rid
    w.reading = hc.record_soc(w.ops, robot_code="R-01", soc_pct=72)
    w.soft = ow.place_hold(w, "robot", "R-01", source="manual", blocking=False)
    w.run1 = ow.depart_new(w, "R-01")
    w.run2 = ow.depart_new(w, "R-02")
    hc.record_soc(w.ops, robot_code="R-02", soc_pct=90)  # after R-02 departed: not its battery at start
    w.late_incident = report(w, "R-02", "other")  # after R-02 departed: not open at its departure
    w.i3 = report(w, "R-03", "contact")
    for to in ("triaged", "response_in_progress", "resolved"):  # resolved, NOT closed: still an open issue
        inc.change_incident_status(w.safety, incident_code=w.i3, to_status=to)
    ow.release(w, Incident.objects.get(code=w.i3).hold.code)
    w.run3 = ow.depart_new(w, "R-03")
    return w


def log() -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT operations_log_predictor_signals()")
        return cur.fetchone()[0]


@pytest.mark.django_db
def test_predictor_log_records_each_signal_as_of_the_departure():
    from operations.models import PredictorObservation

    w = predictor_world()
    before = AuditEvent.objects.count()
    assert log() == 4  # R-01 twice, R-02, R-03
    assert log() == 0  # idempotent: one observation per departure
    assert AuditEvent.objects.count() == before + 2
    a = AuditEvent.objects.filter(command="operations.log_predictor_signals").order_by("id")
    assert [e.result for e in a] == [{"logged": 4}, {"logged": 0}] and a[0].actor_id is None

    o0 = PredictorObservation.objects.get(departure_id=w.run0.departure_id)  # before the visit: not overdue
    assert o0.maintenance_overdue is False and o0.battery_state == "unknown" and o0.open_holds == 0
    o1 = PredictorObservation.objects.get(departure_id=w.run1.departure_id)
    assert (o1.battery_state, o1.battery_soc_pct, o1.battery_reading_id) == ("known", 72, w.reading)
    assert o1.battery_source == "manual_reading" and o1.battery_observed_at <= o1.departed_at
    assert o1.open_holds == 1 and o1.source_refs["holds"] == [w.soft]
    assert o1.open_incidents == 0
    assert o1.maintenance_overdue is True
    assert {s["requirement"]: s["state"] for s in o1.maintenance_states} == {"JOINT": "overdue"}
    assert o1.source_refs["maintenance_rules"] == [w.rule_id]
    assert o1.departure_id == w.run1.departure_id and o1.visit_id == w.run1.visit_id
    assert o1.observed_at >= o1.departed_at

    o2 = PredictorObservation.objects.get(robot__code="R-02")
    assert (o2.battery_state, o2.battery_soc_pct, o2.battery_reading_id) == ("unknown", None, None)
    assert o2.open_incidents == 0 and o2.open_holds == 0  # the later incident and its hold are not counted
    assert o2.maintenance_overdue is False  # no record: 'unknown', not overdue
    assert {s["state"] for s in o2.maintenance_states} == {"unknown"}

    o3 = PredictorObservation.objects.get(robot__code="R-03")
    assert o3.open_incidents == 1 and o3.source_refs["incidents"] == [w.i3]
    assert o3.open_holds == 0  # its hold was released before the departure


TABLE_DIGEST = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND c.relname NOT IN ('platform_core_auditevent', 'operations_predictor_observation')
ORDER BY c.relname
"""


def digest() -> dict[str, str]:
    """Every application table's full content (except the audit log and the predictor log), as an md5."""
    out = {}
    with connection.cursor() as cur:
        cur.execute(TABLE_DIGEST)
        for (t,) in cur.fetchall():
            cur.execute(f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM \"{t}\" x")
            out[t] = cur.fetchone()[0]
    return out


def decisions(at: datetime, visit: Any) -> dict[str, Any]:
    from readiness.evaluator import evaluate_readiness

    with connection.cursor() as cur:
        cur.execute("SELECT * FROM operations_robot_eligibility ORDER BY 1")
        elig = cur.fetchall()
        cur.execute(
            "SELECT * FROM fleet_maintenance_status_set(ARRAY(SELECT id FROM fleet_robot), fleet_production_mode(),"
            " %s) ORDER BY robot_id, requirement_code NULLS FIRST",
            [at],
        )
        maint = cur.fetchall()
    holds = list(Hold.objects.order_by("id").values_list("code", "blocking", "released_at"))
    return {
        "eligibility": elig,
        "maintenance": maint,
        "holds": holds,
        "readiness": evaluate_readiness(visit, at),
    }


@pytest.mark.django_db
def test_logging_changes_nothing_else_planning_readiness_holds_maintenance_or_authorization():
    from planning.models import Visit
    from readiness import commands as rc
    from tests.readiness_world import new_confirmed_visit, record_all_evidence

    w = predictor_world("pn_")
    ow.add_robot(w, "R-04")  # R-01..R-03 are out on their runs (or held)
    code = new_confirmed_visit(w, robot="R-04", d=ow.DAY + timedelta(days=w.next_day))
    record_all_evidence(w, code, robot="R-04")
    visit = Visit.objects.get(code=code)
    at = clock()
    tables, before = digest(), decisions(at, visit)
    audit_before = AuditEvent.objects.count()
    assert log() == 4
    assert digest() == tables  # no other table changed, byte for byte
    assert decisions(at, visit) == before  # eligibility, maintenance status, holds, readiness: identical
    assert AuditEvent.objects.count() == audit_before + 1  # only the logger's own audit row
    assert before["readiness"].ready  # and authorization is issued exactly as without the log
    assert rc.authorize_departure(w.ops, visit_code=code)["code"]


PROTECTED_APPS = ("planning", "readiness", "fleet", "hub", "execution", "accounts", "capacity")


@pytest.mark.django_db
def test_no_decision_module_imports_or_reads_the_predictor_log():
    """Grep: nothing in planning, readiness, fleet, hub, execution (code, templates or SQL migrations) refers to
    the predictor module, model or table; inside operations only the page and the logger do."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    needles = ("predictor", "PredictorObservation")
    hits = [
        str(p.relative_to(root))
        for app in PROTECTED_APPS
        for p in (root / app).rglob("*")
        if p.suffix in (".py", ".html", ".sql") and any(n in p.read_text() for n in needles)
    ]
    assert hits == []
    ops_hits = sorted(
        str(p.relative_to(root))
        for p in (root / "operations").rglob("*")
        if p.suffix in (".py", ".html") and "predictor" in p.read_text()
    )
    assert ops_hits == [
        "operations/management/commands/log_predictor_signals.py",
        "operations/migrations/0008_predictor_log.py",
        "operations/models.py",
        "operations/predictors.py",
        "operations/templates/operations/health.html",
        "operations/views.py",
    ]
    with connection.cursor() as cur:  # no view or function other than the logger mentions the table
        cur.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'public' AND p.prosrc LIKE '%%predictor_observation%%'"
        )
        assert [r[0] for r in cur.fetchall()] == ["operations_log_predictor_signals"]
        cur.execute(
            "SELECT viewname FROM pg_views WHERE schemaname = 'public'"
            " AND definition LIKE '%%predictor_observation%%'"
        )
        assert cur.fetchall() == []
