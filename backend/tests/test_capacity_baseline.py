"""Slice 6 platform baseline (Engineer B): docs/06_scaling_capacity_contract.md section 8 (frozen rev 4).
SYNTHETIC data only.

The world is built through the REAL planning / readiness / execution commands (tests.operations_world). Command
timestamps are forced to clock_timestamp() by the insert triggers, so to get KNOWN durations the test then sets
departed_at / trip and session recorded_at / hold placed_at and released_at to chosen values as the schema OWNER,
with the user triggers of exactly those tables disabled around the UPDATE and re-enabled in `finally` (the same
test-only owner technique as tests/test_operations_metrics.py and the conftest flush). Every expected median is
written out by hand below.

The tests are transactional (`transaction=True`) because `read_snapshot()` must be the outermost transaction."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta

import pytest
from django.db import connection

from capacity import baseline as cb
from capacity import inputs
from execution.models import ExecutionRun, Incident, SessionEvent, TripEvent
from fleet.models import Hold
from operations.snapshot import read_snapshot
from planning import services as ps
from readiness import commands as rc
from tests import operations_world as ow
from tests.planning_world import DAY, TZ
from tests.readiness_world import record_all_evidence

pytestmark = pytest.mark.django_db(transaction=True)

BASE = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # a past, synthetic window origin
H = timedelta(hours=1)
M = timedelta(minutes=1)

# Runs 1..12 reach session_stopped; run 13 only session_active (no stop -> skipped for sessions).
TRAVEL = [31, 44, 25, 38, 52, 29, 41, 36, 47, 33, 40, 27, 35]  # runs 1..13, minutes
SESSION = [60, 95, 70, 120, 85, 110, 75, 90, 100, 65, 130, 80]  # runs 1..12, minutes
# sorted travel: 25 27 29 31 33 35 [36] 38 40 41 44 47 52 -> median 36 (n = 13)
# sorted session: 60 65 70 75 80 [85 90] 95 100 110 120 130 -> median 87.5 (n = 12)
TRAVEL_MEDIAN, SESSION_MEDIAN = 36.0, 87.5


def on_site_minutes(i: int) -> int:
    """Planned on-site length of visit i: 60, 75, 90, 105, 120 repeating."""
    return 60 + 15 * (i % 5)


# in-window visits i = 1..31: (i % 5) counts: 0 -> 6, 1 -> 7, 2 -> 6, 3 -> 6, 4 -> 6; median (16th of 31) = 90
PLANNED_ON_SITE_MEDIAN = 90.0


@contextmanager
def owner_rewrite(*tables: str) -> Iterator[object]:
    """Test-only: the schema OWNER disables the USER triggers of `tables` to set chosen timestamps."""
    with connection.cursor() as cur:
        try:
            for t in tables:
                cur.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
            yield cur
        finally:
            for t in tables:
                cur.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")


def depart(w, robot: str, on_site_min: int) -> ExecutionRun:
    """tests.operations_world.depart_new with a chosen planned on-site length (10:00 local start)."""
    d = DAY + timedelta(days=w.next_day)
    w.next_day += 1
    s = ps.local_to_utc(datetime.combine(d, time(10, 0)), TZ)
    code = ps.request_visit(
        w.ops,
        site_code="S-1",
        on_site_start=s,
        on_site_end=s + timedelta(minutes=on_site_min),
        travel_out_min=30,
        travel_back_min=30,
        tasks=["T-04"],
        required_scopes=["presence"],
    )["entity_id"]
    ps.plan_visit(
        w.ops, visit_code=code, robot_code=robot, lead_code="P-1", second_code="P-2", vehicle_code="VAN-1"
    )
    ps.confirm_visit(w.ops, visit_code=code)
    record_all_evidence(w, code, robot=robot)
    auth = rc.authorize_departure(w.ops, visit_code=code)["code"]
    rc.record_departure(w.ops, authorization_code=auth)
    return ExecutionRun.objects.select_related("visit", "robot").get(visit__code=code)


def set_times(run: ExecutionRun, dep: datetime, travel: int | None, session: int | None) -> None:
    with owner_rewrite("readiness_departureevent", "execution_tripevent", "execution_sessionevent") as cur:
        cur.execute(
            "UPDATE readiness_departureevent SET departed_at = %s WHERE id = %s", [dep, run.departure_id]
        )
        if travel is not None:
            arr = dep + timedelta(minutes=travel)
            cur.execute(
                "UPDATE execution_tripevent SET recorded_at = %s WHERE run_id = %s AND kind = 'arrived_site'",
                [arr, run.pk],
            )
            if session is not None:
                st = arr + 10 * M
                cur.execute(
                    "UPDATE execution_sessionevent SET recorded_at = %s WHERE run_id = %s AND kind = 'start'",
                    [st, run.pk],
                )
                cur.execute(
                    "UPDATE execution_sessionevent SET recorded_at = %s WHERE run_id = %s AND kind = 'stop'",
                    [st + timedelta(minutes=session), run.pk],
                )


def snapshot_measure(start: datetime, end: datetime) -> tuple[cb.Baseline, list[str], list[tuple]]:
    """measure() inside read_snapshot(), capturing every statement, then the backend's locks and xid."""
    seen: list[str] = []

    def wrapper(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    with read_snapshot() as at:
        with connection.execute_wrapper(wrapper):
            b = cb.measure(at, start=start, end=end)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT locktype, mode FROM pg_locks WHERE pid = pg_backend_pid()"
                " AND NOT (locktype = 'relation' AND mode = 'AccessShareLock')"
                " AND NOT (locktype = 'virtualxid' AND mode = 'ExclusiveLock')"
            )
            locks = list(cur.fetchall())
            cur.execute("SELECT txid_current_if_assigned()")
            locks.append(("xid", cur.fetchone()[0]))
    return b, seen, locks


# ================================================================== the measured world
def test_known_world_medians_n_windows_planned_vs_observed_and_read_only():
    n_runs = 33  # run 0 before the window, 1..31 inside, 32 exactly at the (exclusive) end
    w = ow.ops_world("cb_", robots=3)
    robots = list(w.robots) + [ow.add_robot(w, f"R-{k:02d}") for k in range(len(w.robots) + 1, n_runs + 2)]
    runs = [depart(w, robots[i], on_site_minutes(i)) for i in range(n_runs)]
    spare = robots[n_runs]  # never departs

    stopped = list(range(1, 13)) + [0, 32]  # 0 and 32 are outside the window with extreme durations
    for i in stopped:
        ow.advance(w, runs[i], "session_stopped")
    ow.advance(w, runs[13], "session_active")  # start without stop: a trip, not a session
    ow.advance(w, runs[14], "verified")
    w.cmd.trip(runs[14], "arrived_hub")  # aborted outbound: never a travel sample

    # incidents: runs 2 and 3 (run 3 twice -> counted once), run 0 (outside window), a robot with no run
    for i in (2, 3, 3, 0):
        ow.report(w, runs[i].robot.code)
    ow.report(w, spare)

    dep = {i: BASE + i * H for i in range(n_runs)}
    dep[0] = BASE + H - M  # one minute before the window start
    for i in range(n_runs):
        travel = TRAVEL[i - 1] if 1 <= i <= 13 else (500 if i in (0, 32) else None)
        session = SESSION[i - 1] if 1 <= i <= 12 else (900 if i in (0, 32) else None)
        set_times(runs[i], dep[i], travel, session)
    start, end = dep[1], dep[32]  # inclusive start (run 1 counts), exclusive end (run 32 does not)

    b, seen, locks = snapshot_measure(start, end)
    m = b.metrics

    # --- window and read-only guarantees
    assert (b.window_start, b.window_end) == (start, end)
    assert all(x.window_start == start and x.window_end == end for x in m.values())
    assert locks == [("xid", None)], locks  # no row/advisory/tuple locks and no transaction id (zero writes)
    body = "\n".join(seen).upper()
    for banned in (
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "FOR UPDATE",
        "FOR SHARE",
        "ADVISORY",
        "SET_CONFIG",
        "NEXTVAL",
    ):
        assert banned not in body, banned
    assert all(s.lstrip().upper().startswith(("SELECT", "SHOW")) for s in seen)

    # --- planned durations (robot reservations of the departed revision), 31 departures in the window
    for key, minutes in (
        ("planned_inspect_min", 30.0),
        ("planned_load_min", 20.0),
        ("planned_unload_min", 20.0),
    ):
        assert (m[key].value, m[key].n, m[key].kind, m[key].sufficient) == (minutes, 31, "planned", True)
    assert sorted(on_site_minutes(i) for i in range(1, 32))[15] == PLANNED_ON_SITE_MEDIAN
    pos = m["planned_on_site_min"]
    assert (pos.value, pos.n, pos.kind, pos.sufficient) == (PLANNED_ON_SITE_MEDIAN, 31, "planned", True)
    assert "planned, not observed" in pos.note

    # --- observed (sessions 1..12; trips 1..13, aborted run 14 and outside runs 0/32 excluded)
    s = m["observed_on_site_min"]
    assert (s.value, s.n, s.kind, s.sufficient) == (SESSION_MEDIAN, 12, "observed", True)
    t = m["observed_travel_one_way_min"]
    assert (t.value, t.n, t.kind, t.sufficient) == (TRAVEL_MEDIAN, 13, "observed", True)
    assert TripEvent.objects.filter(run=runs[14], kind="arrived_hub").exists()
    assert not TripEvent.objects.filter(run=runs[14], kind="arrived_site").exists()
    assert SessionEvent.objects.filter(run=runs[13], kind="start").exists()

    # --- incidents per visit: runs 2 and 3 of 31 departures
    ipv = m["incidents_per_visit"]
    assert (ipv.value, ipv.n, ipv.kind, ipv.sufficient) == (2 / 31, 31, "observed", True)
    assert "2 runs with >= 1 incident" in ipv.note

    # --- vehicles VAN-1 / VAN-2 capacity 1; VAN-X unknown is not a sample
    v = m["vehicle_robot_capacity"]
    assert (v.value, v.n, v.sufficient) == (1.0, 2, True)

    # --- incident holds were placed now, i.e. after the window: no hold sample
    h = m["observed_hold_duration_days"]
    assert (h.value, h.n, h.kind, h.sufficient) == (None, 0, "observed_censored", False)
    assert "no platform data" in h.note and "never sets hold_min_days" in h.note
    assert all("synthetic demo measurement, not real-world performance" in x.note for x in m.values())

    # --- apply: every sufficient metric replaces its input; observed on_site preferred
    vals, prov = cb.apply(inputs.values("base"), b)
    assert vals["inspect_min"] == 30.0 and vals["load_min"] == 20.0 and vals["unload_min"] == 20.0
    assert vals["on_site_min"] == SESSION_MEDIAN and vals["travel_one_way_min"] == TRAVEL_MEDIAN
    assert vals["incidents_per_visit"] == 2 / 31 and vals["vehicle_robot_capacity"] == 1.0
    rows = {r["key"]: r for r in prov}
    assert (
        rows["on_site_min"]["kind"] == "observed"
        and "observed session preferred" in rows["on_site_min"]["reason"]
    )
    assert all(r["used"] == "platform" for r in prov)

    # --- shrinking the window: start moves one hour on (run 1 drops out) and the end stays exclusive
    b2, _, _ = snapshot_measure(dep[2], dep[31])  # runs 2..30 = 29 departures
    assert b2.metrics["incidents_per_visit"].n == 29
    assert b2.metrics["incidents_per_visit"].sufficient is False  # 29 < 30
    assert "insufficient platform data (n=29 departures < 30)" in b2.metrics["incidents_per_visit"].note
    assert b2.metrics["observed_travel_one_way_min"].n == 12  # runs 2..13
    # runs 2..13 travel sorted: 25 27 29 33 35 [36 38] 40 41 44 47 52 -> 37
    assert b2.metrics["observed_travel_one_way_min"].value == 37.0
    # runs 2..12 session sorted: 65 70 75 80 85 [90] 95 100 110 120 130 -> 90
    assert (b2.metrics["observed_on_site_min"].n, b2.metrics["observed_on_site_min"].value) == (11, 90.0)
    assert b2.metrics["observed_on_site_min"].sufficient is True
    vals2, prov2 = cb.apply(inputs.values("base"), b2)
    rows2 = {r["key"]: r for r in prov2}
    assert vals2["incidents_per_visit"] == inputs.BY_KEY["incidents_per_visit"].base
    assert rows2["incidents_per_visit"]["used"] == "fallback"
    assert "registry fallback used" in rows2["incidents_per_visit"]["reason"]

    # an exact-boundary window: [dep[32], dep[32] + 1 us) holds exactly run 32
    b3, _, _ = snapshot_measure(dep[32], dep[32] + timedelta(microseconds=1))
    assert b3.metrics["incidents_per_visit"].n == 1
    assert b3.metrics["observed_on_site_min"].value == 900.0
    assert b3.metrics["observed_travel_one_way_min"].value == 500.0


# ================================================================== Kaplan-Meier hold durations
# (placed offset in days from BASE, duration in days, released?) ; open holds are censored at `end`.
HOLDS = [
    (0, 1.0, True),
    (0, 2.0, True),
    (0, 2.0, True),
    (1, 3.0, True),
    (1, 4.0, True),
    (2, None, False),  # open: censored at end = BASE + 10 d -> 8 days
    (3, None, False),  # open: censored -> 7 days
    (4, 9.0, True),  # released AFTER end (BASE + 13 d): censored at end -> 6 days
    (5, None, False),  # open: censored -> 5 days
    (8, None, False),  # open: censored -> 2 days (tie with two releases at 2.0)
    (6, 3.5, True),
]
# by hand, samples (days, event): 1E 2E 2E 2C 3E 3.5E 4E 5C 6C 7C 8C  (n = 11, 6 released)
# t=1:   at risk 11, 1 event -> S = 10/11            = 0.9091
# t=2:   at risk 10 (the censored 2.0 is still at risk), 2 events -> S = 10/11 * 8/10 = 0.7273
# t=3:   at risk 7, 1 event -> S = 0.7273 * 6/7       = 0.6234
# t=3.5: at risk 6, 1 event -> S = 0.6234 * 5/6       = 0.5195
# t=4:   at risk 5, 1 event -> S = 0.5195 * 4/5       = 0.4156 <= 0.5 -> KM median = 4 days
KM_MEDIAN = 4.0
NAIVE_RELEASED_MEDIAN = 2.5  # median of released durations only (1, 2, 2, 3, 3.5, 4) would understate it


def test_hold_duration_kaplan_meier_with_right_censoring_by_hand():
    w = ow.ops_world("ck_", robots=3)
    robots = list(w.robots) + [
        ow.add_robot(w, f"R-{k:02d}") for k in range(len(w.robots) + 1, len(HOLDS) + 1)
    ]
    codes = [ow.report(w, robots[i]) for i in range(len(HOLDS))]
    manual = ow.place_hold(w, "robot", robots[0], source="manual")  # not an incident hold: never counted
    end = BASE + timedelta(days=10)
    with owner_rewrite("fleet_hold") as cur:
        for code, (placed, dur, released) in zip(codes, HOLDS, strict=True):
            p0 = BASE + timedelta(days=placed)
            hold_id = Incident.objects.get(code=code).hold_id
            rel = p0 + timedelta(days=dur) if released else None
            cur.execute(
                "UPDATE fleet_hold SET placed_at = %s, released_at = %s WHERE id = %s", [p0, rel, hold_id]
            )
        cur.execute(
            "UPDATE fleet_hold SET placed_at = %s, released_at = %s WHERE code = %s",
            [BASE, BASE + timedelta(hours=1), manual],
        )
    assert Hold.objects.filter(source_type="incident").count() == len(HOLDS)

    b, _, locks = snapshot_measure(BASE, end)
    h = b.metrics["observed_hold_duration_days"]
    assert locks == [("xid", None)]
    assert (h.value, h.n, h.kind, h.sufficient) == (KM_MEDIAN, 11, "observed_censored", True)
    assert "6 released, 5 right-censored" in h.note
    assert cb.median([1, 2, 2, 3, 3.5, 4]) == NAIVE_RELEASED_MEDIAN != KM_MEDIAN

    # informational only: hold_min_days stays the synthetic policy value and is not in the provenance list
    vals, prov = cb.apply(inputs.values("base"), b)
    assert vals["hold_min_days"] == inputs.BY_KEY["hold_min_days"].base
    assert "hold_min_days" not in {r["key"] for r in prov}

    # below threshold: a window holding only the 5 holds placed on days 0-1 (5 holds, all released)
    b2, _, _ = snapshot_measure(BASE, BASE + timedelta(days=2))
    h2 = b2.metrics["observed_hold_duration_days"]
    assert (h2.n, h2.sufficient) == (5, False)
    # censored at day 2 (exclusive end): only the 1-day hold is released before it
    assert "insufficient platform data (n=5 incident holds, 1 released" in h2.note


def test_km_median_helper_edge_cases():
    assert cb.km_median([]) is None
    assert cb.km_median([(1.0, False)] * 10) is None  # nothing released: median not reached
    assert cb.km_median([(1.0, True), (2.0, False), (3.0, False)]) is None  # S = 2/3 only
    assert cb.km_median([(d, True) for d in (1, 2, 3, 4)]) == 2  # S(2) = 0.5 -> smallest t with S <= 0.5


# ================================================================== fallback paths
def test_zero_rows_and_below_threshold_fall_back_with_reasons():
    w = ow.ops_world("cz_", robots=3)
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        now = cur.fetchone()[0]
    b, _, _ = snapshot_measure(now - timedelta(days=1), now + timedelta(days=1))  # nothing departed yet
    for key in (
        "planned_inspect_min",
        "planned_on_site_min",
        "observed_on_site_min",
        "observed_travel_one_way_min",
    ):
        assert (b.metrics[key].value, b.metrics[key].n, b.metrics[key].sufficient) == (None, 0, False)
        assert "no platform data (n=0" in b.metrics[key].note
    assert b.metrics["incidents_per_visit"].value is None and b.metrics["incidents_per_visit"].n == 0

    vals, prov = cb.apply(inputs.values("conservative"), b)
    base = inputs.values("conservative")
    rows = {r["key"]: r for r in prov}
    measured = [i.key for i in inputs.REGISTRY if i.provenance == "platform_measured"]
    assert sorted(rows) == sorted(measured) and len(prov) == len(measured)
    for key in (
        "inspect_min",
        "load_min",
        "unload_min",
        "on_site_min",
        "travel_one_way_min",
        "incidents_per_visit",
    ):
        assert vals[key] == base[key], key
        assert rows[key]["used"] == "fallback" and rows[key]["value"] == base[key]
        assert "no platform data" in rows[key]["reason"] and "registry fallback used" in rows[key]["reason"]
    for key in ("robots_per_visit", "crew_size", "holds_per_incident"):
        assert rows[key]["used"] == "platform" and rows[key]["kind"] == "platform_rule"
        assert "platform rule" in rows[key]["reason"]
    assert (vals["robots_per_visit"], vals["crew_size"], vals["holds_per_incident"]) == (1, 2, 1)

    # below threshold: 3 departures with sessions and trips (n = 3 < 10)
    runs = [depart(w, r, 60) for r in w.robots]
    for i, run in enumerate(runs):
        ow.advance(w, run, "session_stopped")
        set_times(run, BASE + i * H, 30 + i, 60 + i)
    b2, _, _ = snapshot_measure(BASE, BASE + 3 * H)
    for key, n in (
        ("planned_on_site_min", 3),
        ("observed_on_site_min", 3),
        ("observed_travel_one_way_min", 3),
    ):
        mt = b2.metrics[key]
        assert (mt.n, mt.sufficient) == (n, False) and mt.value is not None
        assert f"insufficient platform data (n={n}" in mt.note
    assert b2.metrics["observed_travel_one_way_min"].value == 31.0  # median of 30, 31, 32: measured, not used
    vals2, prov2 = cb.apply(inputs.values("base"), b2)
    rows2 = {r["key"]: r for r in prov2}
    assert vals2["travel_one_way_min"] == 40 and rows2["travel_one_way_min"]["n"] == 3
    assert "insufficient platform data (n=3 trips < 10)" in rows2["travel_one_way_min"]["reason"]
    assert vals2["on_site_min"] == 120 and rows2["on_site_min"]["used"] == "fallback"


# ================================================================== apply() without a database
def _metric(key: str, value: float | None, n: int, ok: bool, kind: str = "planned") -> cb.Metric:
    return cb.Metric(key, value, n, kind, BASE, BASE + H, ok, f"test note {key}")  # type: ignore[arg-type]


def test_apply_replaces_only_sufficient_platform_measured_inputs_and_lists_every_one():
    b = cb.Baseline(BASE, BASE, BASE + H)
    b.metrics = {
        "planned_inspect_min": _metric("planned_inspect_min", 33.0, 12, True),
        "planned_load_min": _metric("planned_load_min", 17.0, 4, False),
        "planned_unload_min": _metric("planned_unload_min", 18.0, 12, True),
        "planned_on_site_min": _metric("planned_on_site_min", 95.0, 12, True),
        "observed_on_site_min": _metric("observed_on_site_min", 80.0, 9, False, "observed"),
        "observed_travel_one_way_min": _metric("observed_travel_one_way_min", 22.0, 10, True, "observed"),
        "incidents_per_visit": _metric("incidents_per_visit", 0.5, 29, False, "observed"),
        "vehicle_robot_capacity": _metric("vehicle_robot_capacity", 2.0, 1, True, "observed"),
        "observed_hold_duration_days": _metric(
            "observed_hold_duration_days", 1.5, 20, True, "observed_censored"
        ),
    }
    before = inputs.values("optimistic")
    vals, prov = cb.apply(before, b)
    assert before == inputs.values("optimistic")  # input dict not mutated
    rows = {r["key"]: r for r in prov}
    measured = [i.key for i in inputs.REGISTRY if i.provenance == "platform_measured"]
    assert [r["key"] for r in prov] == measured  # every platform_measured input, registry order
    assert vals["inspect_min"] == 33.0 and rows["inspect_min"]["used"] == "platform"
    assert vals["load_min"] == 15 and rows["load_min"]["used"] == "fallback" and rows["load_min"]["n"] == 4
    assert vals["unload_min"] == 18.0
    assert vals["on_site_min"] == 95.0 and rows["on_site_min"]["kind"] == "planned"
    assert "planned on_site used: observed session not sufficient" in rows["on_site_min"]["reason"]
    assert vals["travel_one_way_min"] == 22.0 and rows["travel_one_way_min"]["kind"] == "observed"
    assert vals["incidents_per_visit"] == 0.02 and rows["incidents_per_visit"]["used"] == "fallback"
    assert vals["vehicle_robot_capacity"] == 2.0
    assert vals["hold_min_days"] == 1  # never mapped from the observed hold duration
    unchanged = {k for k in before if k not in measured}
    assert all(vals[k] == before[k] for k in unchanged)
    for r in prov:
        assert set(r) == {"key", "used", "value", "n", "kind", "reason"}
        assert r["value"] == vals[r["key"]]


def test_measure_refuses_outside_a_read_only_snapshot():
    with pytest.raises(cb.NotInReadOnlySnapshot):
        cb.measure(BASE, start=BASE, end=BASE + H)
    with pytest.raises(ValueError):
        with read_snapshot() as at:
            cb.measure(at, start=BASE, end=BASE)
    from django.db import transaction

    with transaction.atomic(), pytest.raises(cb.NotInReadOnlySnapshot, match="READ ONLY"):
        cb.measure(BASE, start=BASE, end=BASE + H)
