"""Slice 5 time and statement budgets on the scale fixture (Engineer C). docs/05_operations_command_center_contract.md
sections 4a, 6.4, 6.5 and the EM perf decision. SYNTHETIC data only.

    pytest tests/test_operations_perf.py            statement budgets + a non-flaky 2 s bound per call
    pytest tests/test_operations_perf.py -m perf    p95 over 20 runs per read model (local Postgres)

Budgets (EM decision):
  * fleet_overview, incident_queue, metrics, inbox WITHOUT readiness: p95 <= 400 ms
  * timeline and inbox WITH readiness (they run the authoritative evaluator, <= 10 visits): p95 <= 2,000 ms
    (a separate release gate)
  * SQL: overview / queue / metrics constant (1 / 1 / 1 + inbox-without-readiness); readiness pages
    <= 12 + 10 x E with E pinned at 72 statements per evaluated visit (EM, 2026-11-19; was 70)
The evaluator's time is measured and reported separately from each read model's own work.

Times are server-side function times (no HTTP), measured inside the test transaction that holds the fixture:
read_snapshot must be the outermost transaction, so it cannot wrap a non-transactional test; the statements and
plans are the same. Engineer B's inbox cases are skipped until operations.exceptions.open_exceptions exists.
"""

import importlib
import importlib.util
import inspect
import json
import math
import re
import time
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection

from operations import exceptions as ox
from operations import health as oh
from operations import metrics as om
from operations import readmodels as rm
from tests import operations_world as ow
from tests.fleet_world import set_setting

RUNS = 20
E_PIN = ox.READINESS_EVALUATOR_STATEMENTS  # 72: EM Option B, 2026-11-19 (was 70)
FAST_MS = 400  # read models without the evaluator
READINESS_MS = 2000  # timeline and inbox with readiness (separate release gate)
UPPER_S = 2.0  # normal runs: non-flaky bound on each read model's own time
WINDOW = 60 * 24 * 90
THRESHOLDS = {
    rm.CUSTODY_VERIFY_GRACE: "30",
    rm.RETURN_GRACE: "60",
    rm.INSPECTION_GRACE: "120",
    rm.READINESS_LOOKAHEAD: str(60 * 24 * 60),  # every scale visit qualifies: the cap of 10 is hit
    # Slice 7: the due-soon rule on, and a valid close-out zone so the close-out page is dated (SYNTHETIC values)
    "ops_periodic_due_lookahead_min": "1440",
    ox.CLOSEOUT_TIMEZONE_KEY: "America/Los_Angeles",
}
WITH_READINESS = {"timeline", "inbox_with_readiness"}
# v2 02D: fleet health has the 2 s p95 ceiling (contract 11, 8.7)
TWO_SECOND = WITH_READINESS | {"fleet_health"}


def _perf_selected(config: Any) -> bool:
    return "perf" in (config.getoption("markexpr") or "")


def _inbox() -> Any:
    if importlib.util.find_spec("operations.exceptions") is None:
        return None
    return getattr(importlib.import_module("operations.exceptions"), "open_exceptions", None)


class Counter:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, execute, sql, params, many, context):
        self.n += 1
        return execute(sql, params, many, context)


class EvaluatorClock:
    """Wraps readiness.evaluator.evaluate_readiness: time and statements spent inside it."""

    def __init__(self, monkeypatch: Any) -> None:
        import readiness.evaluator as rev

        self.real = rev.evaluate_readiness
        self.secs = 0.0
        self.calls: list[int] = []  # statements per call
        monkeypatch.setattr(rev, "evaluate_readiness", self)
        # the inbox binds the name at import (from readiness.evaluator import evaluate_readiness): patch that
        # reference too, or the inbox's evaluator time would be counted as its own work
        monkeypatch.setattr("operations.exceptions.evaluate_readiness", self)

    def reset(self) -> None:
        self.secs, self.calls = 0.0, []

    def __call__(self, *a: Any, **k: Any) -> Any:
        c = Counter()
        t0 = time.perf_counter()
        try:
            with connection.execute_wrapper(c):
                return self.real(*a, **k)
        finally:
            self.secs += time.perf_counter() - t0
            self.calls.append(c.n)


def counted(fn) -> tuple[Any, int, float]:
    c = Counter()
    t0 = time.perf_counter()
    with connection.execute_wrapper(c):
        out = fn()
    return out, c.n, time.perf_counter() - t0


def db_now():
    with connection.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


def build() -> tuple[Any, Any, dict[str, Any]]:
    w = ow.scale_world()
    for k, v in THRESHOLDS.items():
        set_setting(k, v)
    with connection.cursor() as cur:
        cur.execute("ANALYZE fleet_deploymentsetting")
    at = db_now()
    cases: dict[str, Any] = {
        "fleet_overview": lambda: rm.fleet_overview(at),
        "incident_queue": lambda: rm.incident_queue(at),
        "timeline": lambda: rm.timeline(at, window_min=WINDOW),
        "metrics": lambda: om.metrics(at - timedelta(days=1), at + timedelta(days=60), at),
        # v2 02D: fleet health over the default 28-day window ending at `at` (one statement)
        "fleet_health": lambda: oh.fleet_health(at - timedelta(days=28), at, at),
    }
    if hasattr(ox, "count_operational_exceptions"):  # the metrics count path
        cases["open_exceptions_count"] = lambda: ox.count_operational_exceptions(at)
    inbox = _inbox()
    if inbox is not None:
        cases["inbox_with_readiness"] = lambda: inbox(at)
        if "include_readiness" in inspect.signature(inbox).parameters:
            cases["inbox_without_readiness"] = lambda: inbox(at, include_readiness=False)
    return w, at, cases


def measure(cases: dict[str, Any], clock: EvaluatorClock, runs: int) -> dict[str, dict[str, Any]]:
    """Per case: statements (must not vary), evaluator calls/statements, and per-run total / evaluator / own ms."""
    out: dict[str, dict[str, Any]] = {}
    for name, fn in cases.items():
        fn()  # warm-up (plan and catalog caches)
        rows = []
        for _ in range(runs):
            clock.reset()
            _, n, secs = counted(fn)
            rows.append((n, secs * 1000, clock.secs * 1000, list(clock.calls)))
        ns = {r[0] for r in rows}
        assert len(ns) == 1, f"{name}: statement count varies {ns}"
        out[name] = {
            "statements": rows[0][0],
            "evaluator_statements": rows[0][3],
            "total_ms": [r[1] for r in rows],
            "evaluator_ms": [r[2] for r in rows],
            "own_ms": [r[1] - r[2] for r in rows],
        }
    return out


def p95(samples: list[float]) -> float:
    s = sorted(samples)
    return s[math.ceil(0.95 * len(s)) - 1]


def check_statements(res: dict[str, dict[str, Any]]) -> None:
    assert res["fleet_overview"]["statements"] == 1
    assert res["incident_queue"]["statements"] == 1
    assert (
        res["fleet_overview"]["evaluator_statements"] == res["incident_queue"]["evaluator_statements"] == []
    )
    assert res["metrics"]["evaluator_statements"] == []  # EM: metrics never runs the evaluator
    assert res["fleet_health"]["statements"] == 1 and res["fleet_health"]["evaluator_statements"] == []
    es = res["timeline"]["evaluator_statements"]
    assert len(es) == 10, es  # the cap is hit on the scale fixture
    assert max(es) <= E_PIN, f"evaluator statements per visit grew past the pinned E = {E_PIN}: {es}"
    assert res["timeline"]["statements"] == 4 + sum(es) <= 12 + 10 * E_PIN
    if "inbox_without_readiness" in res:
        assert res["inbox_without_readiness"]["evaluator_statements"] == []
        assert res["inbox_without_readiness"]["statements"] <= 12
        # metrics: its own statement + count_operational_exceptions (thresholds + one count over the rules)
        if "open_exceptions_count" in res:
            assert res["metrics"]["statements"] == 1 + res["open_exceptions_count"]["statements"] == 1 + 2
            assert res["open_exceptions_count"]["evaluator_statements"] == []
        else:  # before the count path: metrics ran the whole inbox without readiness
            assert res["metrics"]["statements"] == 1 + res["inbox_without_readiness"]["statements"]
    else:
        assert res["metrics"]["statements"] == 1
    if "inbox_with_readiness" in res:
        assert res["inbox_with_readiness"]["statements"] <= 12 + 10 * E_PIN
        ies = res["inbox_with_readiness"]["evaluator_statements"]
        assert len(ies) == 10 and max(ies) <= E_PIN, ies  # the cap is hit; the evaluator is timed separately


def no_jit_plans(at: Any) -> dict[str, float]:
    """EXPLAIN (ANALYZE) each read-model statement: no JIT compilation; report the plan cost."""
    queries = {
        "fleet_overview": (rm.FLEET_OVERVIEW_SQL, None),
        "incident_queue": (rm.INCIDENT_QUEUE_SQL, None),
        "timeline_visits": (rm.TIMELINE_VISITS_SQL, {"at": at, "window_end": at + timedelta(minutes=WINDOW)}),
        "timeline_runs": (rm.TIMELINE_RUNS_SQL, None),
        "metrics": (
            om.METRICS_SQL,
            {"start": at - timedelta(days=1), "end": at + timedelta(days=60), "at": at},
        ),
    }
    costs = {}
    with connection.cursor() as cur:
        for name, (sql, params) in queries.items():
            cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
            raw = cur.fetchone()[0]
            top = (raw if isinstance(raw, list) else json.loads(raw))[0]
            costs[name] = round(top["Plan"]["Total Cost"])
            assert "JIT" not in top, f"{name}: JIT compiled (estimated cost {costs[name]})"
    print("plan costs", costs)
    return costs


def summary(res: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        k: {
            "statements": v["statements"],
            "p95_ms": round(p95(v["total_ms"])),
            "own_p95_ms": round(p95(v["own_ms"])),
            "evaluator_p95_ms": round(p95(v["evaluator_ms"])),
            "median_ms": round(sorted(v["total_ms"])[len(v["total_ms"]) // 2]),
            "E": v["evaluator_statements"] or None,
        }
        for k, v in res.items()
    }


@pytest.mark.django_db
def test_scale_statement_budgets_and_upper_bound(request, monkeypatch):
    """Normal runs: exact statement counts, constant in fleet size (the small-world tests assert the same
    constants), E pinned, no JIT, and a non-flaky 2 s bound on each read model's own time (best of 3; the
    evaluator's share is reported, and bounded by the -m perf release gate)."""
    if _perf_selected(request.config):
        pytest.skip("covered by the -m perf test")
    _, at, cases = build()
    clock = EvaluatorClock(monkeypatch)
    res = measure(cases, clock, runs=3)
    check_statements(res)
    print("\n", summary(res))
    for name, v in res.items():
        assert min(v["own_ms"]) < UPPER_S * 1000, f"{name}: {min(v['own_ms']):.0f} ms of its own"
    no_jit_plans(at)
    if "inbox_with_readiness" not in cases:
        print("open_exceptions: not available yet (Engineer B) - inbox cases skipped")


@pytest.mark.perf
@pytest.mark.django_db
def test_scale_p95(request, monkeypatch):
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf (p95 budgets on the scale fixture)")
    w, at, cases = build()
    print(f"\nscale fixture built in {w.timings}")
    print("environment before:", json.dumps(environment()))
    clock = EvaluatorClock(monkeypatch)
    res = measure(cases, clock, runs=RUNS)
    check_statements(res)
    no_jit_plans(at)
    s = summary(res)
    print(f"p95 over {RUNS} runs:", json.dumps(s, indent=1))
    print(
        "individual samples (ms):",
        json.dumps(
            {
                k: {
                    "total": [round(x, 1) for x in v["total_ms"]],
                    "evaluator": [round(x, 1) for x in v["evaluator_ms"]],
                }
                for k, v in res.items()
            },
            indent=1,
        ),
    )
    print("environment after:", json.dumps(environment()))
    if "inbox_with_readiness" not in cases:
        print("open_exceptions: not available yet (Engineer B) - inbox p95 cases skipped")
    elif "inbox_without_readiness" not in cases:
        print("open_exceptions(include_readiness=False) not available yet - that p95 case skipped")
    over = {
        k: v["p95_ms"] for k, v in s.items() if v["p95_ms"] > (READINESS_MS if k in TWO_SECOND else FAST_MS)
    }
    assert not over, f"p95 over budget (ms): {over}; full: {s}"


# ------------------------------------------------------------------ EM review #9: whole HTTP endpoints
HTTP_CASES = {
    # path, budget (ms): the contract's latency targets are endpoint-level, so this includes auth,
    # permission check, snapshot setup, the read model, serialization and template rendering.
    "/api/ops/overview": FAST_MS,
    "/api/ops/incidents": FAST_MS,
    "/api/ops/metrics": FAST_MS,
    "/api/ops/exceptions": READINESS_MS,  # the inbox runs the readiness evaluator
    "/api/ops/timeline": READINESS_MS,
    # v2 01B: Today runs the readiness evaluator for the visits of today and tomorrow (the timeline's cap of 10),
    # so it has the readiness budget (contract docs/09 section 4: complex operational views < 2 s)
    "/ops/": READINESS_MS,
    "/ops/robots/": FAST_MS,  # the old Today page: one row per robot
    "/ops/production-readiness/": FAST_MS,
    "/ops/incidents/": FAST_MS,
    "/ops/metrics/": FAST_MS,
    "/ops/exceptions/": READINESS_MS,
    "/ops/timeline/": READINESS_MS,
    # 01D release fix: /readiness/ lists every confirmed future visit and evaluates at most 10 in the look-ahead
    "/readiness/": READINESS_MS,
    # Slice 7 X-10 (contract 07a section 14): the close-out page, contract 05 A1 budget (no evaluator)
    "/api/ops/closeout": FAST_MS,
    "/ops/closeout/": FAST_MS,
    # v2 02D: fleet health + the predictor log (contract 11, 8.7: 2 s p95 ceiling, 400 ms target)
    "/ops/health/": READINESS_MS,
}


def environment() -> dict[str, Any]:
    """What the timings depend on: host load, the PostgreSQL settings that shape plans, and the dataset."""
    import os
    import subprocess

    env: dict[str, Any] = {}
    try:
        env["loadavg"] = subprocess.run(
            ["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        env["loadavg"] = str(os.getloadavg())
    with connection.cursor() as cur:
        for name in ("shared_buffers", "work_mem", "jit", "random_page_cost", "effective_cache_size"):
            cur.execute(f"SHOW {name}")
            env[name] = cur.fetchone()[0]
        cur.execute("SELECT version()")
        env["server"] = cur.fetchone()[0].split(" on ")[0]
        sizes = {}
        for table in (
            "fleet_robot",
            "planning_visit",
            "execution_executionrun",
            "execution_incident",
            "execution_incidentescalation",
            "execution_custodyevent",
            "readiness_departureevent",
        ):
            cur.execute(f"SELECT count(*) FROM {table}")
            sizes[table] = cur.fetchone()[0]
        env["rows"] = sizes
    return env


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)  # the snapshot must be the outermost transaction
def test_scale_http_endpoints_p95(request, monkeypatch):
    """Whole requests through the Django test client (auth, permission, snapshot, read model, serialization,
    template rendering). Warm-up: one untimed request per endpoint right before its 20 timed samples (plan and
    catalog caches, template loading). Prints the environment before and after, every individual sample, the
    statements per request and the readiness evaluator's time inside each request."""
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf (endpoint-level p95 budgets on the scale fixture)")
    from django.db import transaction
    from django.test import Client

    from tests.conftest import make_user

    with transaction.atomic():  # scale_world uses a temp table that lives for one transaction
        build()
    print("\nenvironment before:", json.dumps(environment()))
    clock = EvaluatorClock(monkeypatch)
    c = Client()
    c.force_login(make_user("perf_ops", "ops_lead"))
    params = {"window_min": str(WINDOW)}
    results: dict[str, dict[str, Any]] = {}
    for path, budget in HTTP_CASES.items():
        q = params if "timeline" in path else {}
        assert c.get(path, q).status_code == 200, path  # warm-up, and the endpoint must work
        samples, evaluator, statements = [], [], set()
        load = environment()["loadavg"]
        for _ in range(RUNS):
            clock.reset()
            counter = Counter()
            t0 = time.perf_counter()
            with connection.execute_wrapper(counter):
                resp = c.get(path, q)
            samples.append((time.perf_counter() - t0) * 1000)
            evaluator.append(clock.secs * 1000)
            statements.add(counter.n)
            assert resp.status_code == 200, path
        results[path] = {
            "p95_ms": round(p95(samples), 1),
            "median_ms": round(sorted(samples)[RUNS // 2], 1),
            "budget_ms": budget,
            "evaluator_p95_ms": round(p95(evaluator), 1),
            "evaluator_median_ms": round(sorted(evaluator)[RUNS // 2], 1),
            "evaluator_calls": len(clock.calls),
            "statements": sorted(statements),
            "loadavg_at_start": load,
            "samples_ms": [round(x, 1) for x in samples],
            "evaluator_samples_ms": [round(x, 1) for x in evaluator],
        }
    print(f"\nendpoint p95 over {RUNS} runs:", json.dumps(results, indent=1))
    print("\nenvironment after:", json.dumps(environment()))
    over = {k: v["p95_ms"] for k, v in results.items() if v["p95_ms"] > v["budget_ms"]}
    assert not over, f"endpoint p95 over budget (ms): {over}; full: {results}"


# ------------------------------------------------------------------ v2 02A: the Hub page
HUB_MS = 2000  # contract 11, 6 and 8.7: 2 s p95 ceiling (400 ms target)


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)
def test_hub_page_p95_on_the_scale_fixture(request):
    """/hub/ (one-statement task refresh + the queue) on the 200-robot scale fixture, as the ops lead."""
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf")
    from django.db import transaction
    from django.test import Client

    from tests.conftest import make_user

    with transaction.atomic():
        build()
    # v2 02B: charging data on the scale fixture (cheap): the forecast on, 4 chargers, a fresh reading for every
    # third robot (rows written as the schema owner, as fixtures do; the app writes them only via functions)
    from django.utils import timezone

    from fleet.models import Robot
    from hub.models import Charger, RobotSocReading

    set_setting("ops_soc_fresh_min", "240")
    perf_user = make_user("perf_hub_ops", "ops_lead")
    Charger.objects.bulk_create([Charger(code=f"C-{n}", name=f"Bay {n}") for n in range(1, 5)])
    now = timezone.now()
    RobotSocReading.objects.bulk_create(
        [
            RobotSocReading(
                robot=r,
                soc_pct=30 + (i * 7) % 70,
                observed_at=now,
                source="manual_reading",
                recorded_by=perf_user,
                recorded_at=now,
            )  # fmt: skip
            for i, r in enumerate(Robot.objects.order_by("id")[::3])
        ]
    )
    # v2 02D: stock on the scale fixture (fictional): 20 items, 10 receipts each, minimums for half of them (some
    # below), one item counted per visit. Rows written as the schema owner, as fixtures do (the app writes them
    # only through the hub_*_stock functions).
    from hub.models import StockItem, StockMovement

    items = StockItem.objects.bulk_create(
        [
            StockItem(
                code=f"K{n}",
                name=f"Kit {n}",
                kind="reusable" if n % 2 else "consumable",
                unit="kits",
                created_by=perf_user,
                created_at=now,
            )  # fmt: skip
            for n in range(20)
        ]
    )
    StockMovement.objects.bulk_create(
        [
            StockMovement(
                item=it,
                kind="receive",
                quantity=1 + n % 3,
                on_hand_delta=1 + n % 3,
                reserved_delta=0,
                actor=perf_user,
                occurred_at=now,
            )  # fmt: skip
            for it in items
            for n in range(10)
        ]
    )
    set_setting("stock_minimums", ",".join(f"K{n}={10 + 3 * (n % 3)}" for n in range(0, 20, 2)))
    set_setting("stock_per_visit", "K1=1")
    c = Client()
    c.force_login(perf_user)
    assert c.get("/hub/").status_code == 200  # warm-up; creates the derived tasks once
    samples, statements = [], set()
    for _ in range(RUNS):
        counter = Counter()
        t0 = time.perf_counter()
        with connection.execute_wrapper(counter):
            resp = c.get("/hub/")
        samples.append((time.perf_counter() - t0) * 1000)
        statements.add(counter.n)
        assert resp.status_code == 200
    from hub.models import WorkOrder

    res = {
        "p95_ms": round(p95(samples), 1),
        "median_ms": round(sorted(samples)[RUNS // 2], 1),
        "budget_ms": HUB_MS,
        "statements": sorted(statements),
        "tasks": WorkOrder.objects.count(),
        "charging_tasks": WorkOrder.objects.filter(source_kind="charge_forecast").count(),
        "samples_ms": [round(x, 1) for x in samples],
    }
    print("\n/hub/ on the scale fixture:", json.dumps(res, indent=1))
    assert res["p95_ms"] <= HUB_MS, res
    # v2 02B: Today and the Charging page with the charge forecast on (Today keeps its readiness budget)
    # v2 02D: the Stock page (2 s p95, the Hub ceiling)
    for path, budget in (("/ops/", READINESS_MS), ("/hub/charging/", HUB_MS), ("/hub/stock/", HUB_MS)):
        assert c.get(path).status_code == 200  # warm-up
        times, stmts = [], set()
        for _ in range(RUNS):
            counter = Counter()
            t0 = time.perf_counter()
            with connection.execute_wrapper(counter):
                assert c.get(path).status_code == 200
            times.append((time.perf_counter() - t0) * 1000)
            stmts.add(counter.n)
        out = {"p95_ms": round(p95(times), 1), "median_ms": round(sorted(times)[RUNS // 2], 1),
               "budget_ms": budget, "statements": sorted(stmts)}  # fmt: skip
        print(f"\n{path} with charging on the scale fixture:", json.dumps(out))
        assert out["p95_ms"] <= budget, (path, out)


CHARGE_VISITS = 40  # confirmed visits in the next two days, one robot each (most scale robots are held)
CHARGE_FEW = 3  # robots with a low reading in the "few tasks" comparison run


def _hub_samples(c: Any, runs: int) -> tuple[list[float], set[int]]:
    assert c.get("/hub/").status_code == 200  # warm-up; derives any new tasks once
    samples, statements = [], set()
    for _ in range(runs):
        counter = Counter()
        t0 = time.perf_counter()
        with connection.execute_wrapper(counter):
            resp = c.get("/hub/")
        samples.append((time.perf_counter() - t0) * 1000)
        statements.add(counter.n)
        assert resp.status_code == 200
    return samples, statements


def _planning_predicate_bench(visit_code: str) -> dict[str, Any]:
    """v2 03A-2 section 12: p95 of the revised planning predicate on the scale fixture: the eligibility view (every
    robot, with fleet_planning_location once per robot), one robot's visit-specific answer
    (fleet_planning_location_problem) and the options list of one visit (view + location + busy windows)."""
    from planning import options as popt
    from planning.models import Visit

    visit = Visit.objects.select_related("site", "current_revision").get(code=visit_code)
    rev = visit.current_revision
    assert rev is not None

    def timed(fn: Any) -> list[float]:
        fn()  # warm-up
        out = []
        for _ in range(RUNS):
            t0 = time.perf_counter()
            fn()
            out.append((time.perf_counter() - t0) * 1000)
        return out

    def view() -> None:
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FILTER (WHERE eligible) FROM operations_robot_eligibility")
            cur.fetchone()

    def one() -> None:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT fleet_planning_location_problem(%s, %s, %s, %s)",
                [rev.robot_id, visit.hub_id, rev.on_site_start, rev.on_site_end],
            )
            cur.fetchone()

    out = {}
    for name, fn in (
        ("eligibility_view", view),
        ("one_robot_visit_answer", one),
        ("options", lambda: popt.plan_options(visit)),
    ):
        samples = timed(fn)
        out[name] = {"p95_ms": round(p95(samples), 1), "median_ms": round(sorted(samples)[RUNS // 2], 1)}
    return out


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)
def test_hub_page_p95_with_charging_tasks_on_the_scale_fixture(request, monkeypatch):
    """02B item C: charge_forecast derivation at the 200-robot scale. Everything charging-related goes through
    the guarded paths (charger command, planning commands, hub_record_soc_reading via hub.charging), no raw
    inserts. /hub/ p95 with the derived charging tasks stays within the Hub budget, and its statement count is
    the same with a few charging tasks as with many (bounded, not per task)."""
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf")
    from django.db import transaction
    from django.test import Client
    from django.utils import timezone

    import platform_core.interaction as ia
    from hub import charging as hc
    from hub.models import WorkOrder
    from tests.conftest import make_user
    from tests.readiness_world import MIN_DEPARTURE_SOC

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the counts
    with transaction.atomic():
        w, _, _ = build()
    set_setting("ops_soc_fresh_min", "240")
    perf_user = make_user("perf_hub_charge_ops", "ops_lead")
    for n in range(1, 5):
        hc.create_charger(w.ops, code=f"C-{n}", name=f"Bay {n}")
    # v2 03A-2 section 12: who the planner may consider. The fixture is left as built (robots on open runs stay
    # out: they are plannable outside their runs' reserved windows, At risk); nothing is moved to the hub.
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE e.eligible), count(*) FILTER (WHERE e.eligible AND l.kind <> 'hub'),"
            " count(*) FILTER (WHERE l.kind <> 'hub'), count(*)"
            " FROM operations_robot_eligibility e CROSS JOIN LATERAL fleet_planning_location(e.robot_id) l"
            " WHERE e.lifecycle <> 'retired'"
        )
        eligible, eligible_away, away, robots_total = cur.fetchone()
        cur.execute(
            "SELECT coalesce(split_part(l.planning_problem, ':', 1), 'none'), count(*)"
            " FROM fleet_robot r CROSS JOIN LATERAL fleet_planning_location(r.id) l"
            " WHERE l.kind <> 'hub' GROUP BY 1 ORDER BY 1"
        )
        away_reasons = {k: n for k, n in cur.fetchall()}
    candidates = {
        "robots": robots_total,
        "eligible_candidates": eligible,
        "eligible_away_at_risk": eligible_away,
        "away_on_open_runs": away,
        "away_location_reasons": away_reasons,  # "none" = plannable outside the run's window
    }
    print("\nplanning candidates on the scale fixture:", json.dumps(candidates))
    # confirmed visits in the next two days on distinct scale robots (their fixture visits start on DAY + 1)
    crews = ow.SCALE_CREWS
    base = timezone.now().replace(minute=0, second=0, microsecond=0)
    confirmed: list[str] = []
    confirmed_visits: list[str] = []
    skipped: dict[str, str] = {}
    slots = [0] * crews  # each crew takes visits 7 hours apart: 20, 27, 34, 41 hours from now
    for n, robot in enumerate(w.robot_codes):
        if len(confirmed) == CHARGE_VISITS:
            break
        crew = n % crews
        if slots[crew] == 4:
            continue
        start = base + timedelta(hours=20 + 7 * slots[crew])
        code = w.p.request_visit(
            w.ops, site_code="S-1", on_site_start=start, on_site_end=start + timedelta(hours=1), travel_out_min=30,
            travel_back_min=30, tasks=["T-04"], required_scopes=["presence"],
        )["entity_id"]  # fmt: skip
        out = w.p.plan_visit(w.ops, visit_code=code, robot_code=robot, lead_code=f"PC-{crew}L",
                             second_code=f"PC-{crew}S", vehicle_code=f"VS-{crew}")  # fmt: skip
        if out["feasibility"] != "feasible":  # e.g. a held robot: not part of this scenario
            skipped[robot] = "; ".join(out["blockers"])[:100]
            w.p.cancel_visit(w.ops, visit_code=code, reason="perf scenario: robot not available")
            continue
        w.p.confirm_visit(w.ops, visit_code=code)
        slots[crew] += 1
        confirmed.append(robot)
        confirmed_visits.append(code)
    assert len(confirmed) == CHARGE_VISITS, (len(confirmed), candidates, skipped)
    bench = _planning_predicate_bench(confirmed_visits[0])
    print("planning predicate benchmark:", json.dumps(bench))
    c = Client()
    c.force_login(perf_user)

    def low(robots: list[str]) -> None:
        for k, robot in enumerate(robots):
            hc.record_soc(perf_user, robot_code=robot, soc_pct=(MIN_DEPARTURE_SOC - 40) + k % 30)

    def charging_tasks() -> int:
        return WorkOrder.objects.filter(source_kind="charge_forecast").count()

    low(confirmed[:CHARGE_FEW])
    few_samples, few_statements = _hub_samples(c, RUNS)
    few_tasks = charging_tasks()
    low(confirmed[CHARGE_FEW:])
    samples, statements = _hub_samples(c, RUNS)
    res = {
        "p95_ms": round(p95(samples), 1),
        "median_ms": round(sorted(samples)[RUNS // 2], 1),
        "budget_ms": HUB_MS,
        "statements": sorted(statements),
        "tasks": WorkOrder.objects.count(),
        "charging_tasks": charging_tasks(),
        "confirmed_visits": len(confirmed),
        "robots_skipped": len(skipped),
        "planning_candidates": candidates,
        "planning_predicate": bench,
        "few": {
            "charging_tasks": few_tasks,
            "statements": sorted(few_statements),
            "p95_ms": round(p95(few_samples), 1),
        },  # fmt: skip
        "samples_ms": [round(x, 1) for x in samples],
    }
    print("\n/hub/ with charging tasks on the scale fixture:", json.dumps(res, indent=1))
    assert 0 < few_tasks < res["charging_tasks"], res
    assert res["charging_tasks"] >= CHARGE_VISITS, res  # every confirmed visit's robot needs charge
    assert res["p95_ms"] <= HUB_MS, res
    assert statements == few_statements, res  # bounded: the same statements whatever the task count


# ------------------------------------------------------------------ 01D release fix: /readiness/
def _readiness_samples(monkeypatch: Any, user: Any) -> dict[str, Any]:
    from django.test import Client

    clock = EvaluatorClock(monkeypatch)
    monkeypatch.setattr("readiness.views.evaluate_readiness", clock)  # the list binds the name at import
    c = Client()
    c.force_login(user)
    assert c.get("/readiness/").status_code == 200  # warm-up
    samples, evaluator, statements = [], [], set()
    for _ in range(RUNS):
        clock.reset()
        counter = Counter()
        t0 = time.perf_counter()
        with connection.execute_wrapper(counter):
            resp = c.get("/readiness/")
        samples.append((time.perf_counter() - t0) * 1000)
        evaluator.append(clock.secs * 1000)
        statements.add(counter.n)
        assert resp.status_code == 200
    body = resp.content.decode()
    counts = body.split('data-readiness-counts="')[1].split('"')[0]
    return {
        "p95_ms": round(p95(samples), 1),
        "median_ms": round(sorted(samples)[RUNS // 2], 1),
        "budget_ms": READINESS_MS,
        "evaluator_calls_per_request": len(clock.calls),  # reset before each request: the last one
        "evaluator_p95_ms": round(p95(evaluator), 1),
        "statements": sorted(statements),
        "counts_all_assessed_not": counts,
        "samples_ms": [round(x, 1) for x in samples],
    }


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)
def test_readiness_list_p95_on_the_scale_fixture(request, monkeypatch):
    """/readiness/ on the 200-robot scale fixture (2,000 visits): every confirmed future visit listed, the
    evaluator for at most 10 inside the look-ahead. Budget: the readiness budget (2,000 ms p95)."""
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf")
    from django.db import transaction

    from tests.conftest import make_user

    with transaction.atomic():
        build()
    set_setting("ops_readiness_lookahead_min", str(WINDOW))  # every visit qualifies: the cap must hold
    res = _readiness_samples(monkeypatch, make_user("perf_rl_ops", "ops_lead"))
    print("\n/readiness/ on the scale fixture:", json.dumps(res, indent=1))
    assert res["evaluator_calls_per_request"] <= rm.READINESS_CAP
    assert res["p95_ms"] <= READINESS_MS, res


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)
def test_readiness_list_p95_on_the_northbay_demo(request, monkeypatch):
    """/readiness/ on the real demo workspace (build_demo_workspace into the empty perf test database:
    50 robots, about 85 visits), as the demo ops lead."""
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf")
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    monkeypatch.setenv("DEV_SEED_PASSWORD", "perf-demo-password-1")
    t0 = time.perf_counter()
    call_command("build_demo_workspace", stdout=__import__("io").StringIO())
    build_s = round(time.perf_counter() - t0, 1)
    res = _readiness_samples(monkeypatch, get_user_model().objects.get(username="dana.ortiz"))
    res["build_s"] = build_s
    print("\n/readiness/ on the Northbay demo:", json.dumps(res, indent=1))
    assert res["evaluator_calls_per_request"] <= rm.READINESS_CAP
    assert res["p95_ms"] <= READINESS_MS, res


# ------------------------------------------------------------------ EM perf instruction 2: inbox call accounting
class InboxSpy:
    """Counts open_exceptions() calls and every evaluate_readiness() call (by visit id), wherever it is reached
    from: the inbox's own reference and the evaluator module's (the timeline imports it from there)."""

    def __init__(self, monkeypatch: Any) -> None:
        import readiness.evaluator as rev
        from operations import exceptions as ox

        self.inbox_calls = 0
        self.evaluated: list[int] = []
        real_inbox, real_eval = ox.open_exceptions, rev.evaluate_readiness

        def inbox(*a: Any, **k: Any) -> Any:
            self.inbox_calls += 1
            return real_inbox(*a, **k)

        def evaluate(visit: Any, *a: Any, **k: Any) -> Any:
            self.evaluated.append(visit.pk)
            return real_eval(visit, *a, **k)

        monkeypatch.setattr(ox, "open_exceptions", inbox)
        monkeypatch.setattr(ox, "evaluate_readiness", evaluate)
        monkeypatch.setattr(rev, "evaluate_readiness", evaluate)

    def reset(self) -> None:
        self.inbox_calls, self.evaluated = 0, []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["/ops/exceptions/", "/api/ops/exceptions"])
def test_one_inbox_call_and_at_most_ten_evaluations_per_exceptions_response(path, monkeypatch):
    """Each exceptions response (first and continuation page) calls open_exceptions() exactly once and
    evaluates at most 10 visits, each once: the coverage banner / template / JSON paths evaluate nothing."""
    from django.test import Client

    from tests.conftest import make_user
    from tests.planning_world import DAY
    from tests.readiness_world import new_confirmed_visit

    w = ow.ops_world("oc_", robots=3, incident_policy=False)
    for i in range(12):  # 13 qualifying visits: the cap of 10 is hit
        new_confirmed_visit(w, robot=f"R-0{1 + i % 3}", d=DAY + timedelta(days=1 + i))
    for k, v in THRESHOLDS.items():
        set_setting(k, v)
    set_setting("ops_inspection_grace_min", "x")  # + one THRESHOLD_NOT_CONFIGURED
    set_setting("demo_mode", "true")
    c = Client()
    c.force_login(make_user("oc_perf_viewer", "ops_lead"))
    spy = InboxSpy(monkeypatch)
    resp = c.get(path)
    assert resp.status_code == 200
    assert spy.inbox_calls == 1
    assert len(spy.evaluated) == 10 and len(set(spy.evaluated)) == 10, spy.evaluated
    if path.startswith("/api/"):
        body = resp.json()
        assert (
            body["readiness_coverage"]["evaluated"] == 10 and body["readiness_coverage"]["qualifying"] == 13
        )
        # a continuation page is its own snapshot: again exactly one inbox call and <= 10 evaluations
        if body["next_cursor"]:
            spy.reset()
            assert c.get(path, {"cursor": body["next_cursor"]}).status_code == 200
            assert spy.inbox_calls == 1 and len(spy.evaluated) == len(set(spy.evaluated)) <= 10
    else:
        assert 'data-coverage="partial"' in resp.content.decode()


def _explain(cur: Any, sql: str, params: Any) -> list[str]:
    cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
    return [r[0] for r in cur.fetchall()]


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)  # the snapshot must be the outermost transaction
def test_inbox_profile(request, monkeypatch):
    """EM perf instruction 2 (diagnostic, prints only): EXPLAIN (ANALYZE, BUFFERS) of the all-rules query, the
    count projection and the unknown-basis query on the 200-robot scale fixture, then one instrumented request
    per exceptions endpoint split into SQL rules / unknown-basis / readiness selection / evaluator / rendering."""
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf (inbox profile on the scale fixture)")
    from django.db import transaction
    from django.test import Client

    from operations import api, views
    from operations.snapshot import read_snapshot
    from tests.conftest import make_user

    with transaction.atomic():
        build()
    with read_snapshot() as at, connection.cursor() as cur:
        th, _ = ox.read_thresholds()
        rel = ox._relation_params(at, th)
        rules_params = {
            **rel,
            "prio": json.dumps(ox.PRIORITY),
            "a_prio": None,
            "a_since": None,
            "a_code": None,
            "a_ref": None,
            "limit": views.PAGE_SIZE + 1,
        }
        for name, sql, params in (
            ("ALL RULES (inbox page 1, limit 51)", ox.RULES_SQL, rules_params),
            ("COUNT PROJECTION", ox.COUNT_SQL, rel),
            ("UNKNOWN BASIS", ox.UNKNOWN_BASIS_SQL, None),
            (
                "READINESS SELECTION",
                ox.READINESS_SQL,
                {"look": th["ops_readiness_lookahead_min"], "at": at, "cap": ox.READINESS_CAP},
            ),
        ):
            print(f"\n===== EXPLAIN (ANALYZE, BUFFERS) {name}")
            print("\n".join(_explain(cur, sql, params)))

    phases: dict[str, float] = {}

    def timed(label: str, fn: Any) -> Any:
        def inner(*a: Any, **k: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                phases[label] = phases.get(label, 0.0) + (time.perf_counter() - t0) * 1000

        return inner

    def sql_clock(execute, sql, params, many, context):
        label = (
            "sql_rules"
            if sql == ox.RULES_SQL
            else "unknown_basis"
            if sql == ox.UNKNOWN_BASIS_SQL
            else "readiness_selection"
            if sql == ox.READINESS_SQL
            else None
        )
        if label is None:
            return execute(sql, params, many, context)
        return timed(label, execute)(sql, params, many, context)

    monkeypatch.setattr(ox, "evaluate_readiness", timed("evaluator", ox.evaluate_readiness))
    monkeypatch.setattr(ox, "open_exceptions", timed("open_exceptions", ox.open_exceptions))
    monkeypatch.setattr(views, "render", timed("render_template", views.render))
    monkeypatch.setattr(api, "_ok", timed("serialize_json", api._ok))
    c = Client()
    c.force_login(make_user("perf_prof", "ops_lead"))
    for path in ("/ops/exceptions/", "/api/ops/exceptions"):
        c.get(path)  # warm-up
        for i in range(3):
            phases.clear()
            t0 = time.perf_counter()
            with connection.execute_wrapper(sql_clock):
                assert c.get(path).status_code == 200
            total = (time.perf_counter() - t0) * 1000
            inbox = phases.get("open_exceptions", 0.0)
            known = sum(
                phases.get(k, 0.0) for k in ("sql_rules", "unknown_basis", "readiness_selection", "evaluator")
            )
            row = {k: round(v, 1) for k, v in sorted(phases.items())}
            row["inbox_other"] = round(inbox - known, 1)
            row["request_total"] = round(total, 1)
            row["outside_inbox_and_render"] = round(
                total - inbox - phases.get("render_template", 0.0) - phases.get("serialize_json", 0.0), 1
            )
            print(f"\nPROFILE {path} run {i + 1}: {json.dumps(row)}")

    # the evaluator's share: its statements' database round trips vs its Python work, and which statements
    # repeat across the 10 evaluated visits (normalized text, top 12 by total time)
    import re as _re

    from operations.snapshot import read_snapshot as _snap

    stats: dict[str, list[float]] = {}
    slowest: dict[str, tuple[float, Any, Any]] = {}
    inside = {"on": False}

    def stmt_clock(execute, sql, params, many, context):
        if not inside["on"]:
            return execute(sql, params, many, context)
        t0 = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            key = _re.sub(r"\s+", " ", sql)[:160]
            ms = (time.perf_counter() - t0) * 1000
            stats.setdefault(key, []).append(ms)
            if ms > slowest.get(key, (0.0, None, None))[0]:
                slowest[key] = (ms, sql, params)

    real_eval = ox.evaluate_readiness

    def eval_on(*a: Any, **k: Any) -> Any:
        inside["on"] = True
        try:
            return real_eval(*a, **k)
        finally:
            inside["on"] = False

    monkeypatch.setattr(ox, "evaluate_readiness", eval_on)
    monkeypatch.setattr(ox, "open_exceptions", ox.open_exceptions)
    phases.clear()
    with _snap() as at2, connection.execute_wrapper(stmt_clock):
        t0 = time.perf_counter()
        ox.open_exceptions(at2, limit=views.PAGE_SIZE)
        total = (time.perf_counter() - t0) * 1000
    db_ms = sum(sum(v) for v in stats.values())
    n = sum(len(v) for v in stats.values())
    print(
        f"\nEVALUATOR SPLIT: inbox {total:.1f} ms; evaluator statements {n}, their DB round trips {db_ms:.1f} ms"
    )
    top = sorted(stats.items(), key=lambda kv: -sum(kv[1]))[:12]
    for sql, times in top:
        print(f"  {len(times):3d}x {sum(times):7.1f} ms  {sql}")
    with _snap(), connection.cursor() as cur:
        for sql, _times in top[:2]:
            _ms, full, params = slowest[sql]
            print(
                f"\n===== EXPLAIN (ANALYZE, BUFFERS) slowest evaluator statement ({_ms:.1f} ms):\n{full}\n{params}"
            )
            print("\n".join(_explain(cur, full, params)))


# ================================================================== v2 03B-3: the dispatch board on three hubs
BOARD_MS = 2000  # contract section 8: p95 at most 2 s (400 ms target) on a 3-hub fixture of 200 robots
BOARD_HUBS = (("HB", "Hub B", 60), ("HC", "Hub C", 40))  # + 100 robots at HUB-1 = 200
BOARD_VISITS_PER_HUB = 8


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)
def test_dispatch_board_p95_on_a_three_hub_fixture(request, monkeypatch):
    """/planning/board/ as the regional ops lead on 3 hubs and 200 robots (the scale fixture's 100 robots and
    1,000 visits at HUB-1, plus 60 and 40 robots with crews, vans and visits at hubs B and C, all through the
    commands). Reports p95 and the statement count, which is the same for one hub and for three."""
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf")
    from django.db import transaction
    from django.test import Client

    import platform_core.interaction as ia
    from planning import services as ps
    from platform_core import workspace as wsp
    from tests.conftest import make_user
    from tests.planning_world import at

    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the counts
    with transaction.atomic():
        w, _at, _cases = build_scale_for_board()
    admin = make_user("perf_board_admin", "admin")
    day = DAY_FOR_BOARD
    lo, hi_dt = day_bounds_for_quals()
    for code, name, n in BOARD_HUBS:
        wsp.create_hub(admin, code=code, name=name, area="", time_zone="America/Los_Angeles")
        robots = [ow.add_robot(w, f"{code}-{i:03d}", hub_code=code) for i in range(n)]
        for c in range(2):
            pair = []
            for role, kinds in (
                ("L", ["field_lead", "robot_handler"]),
                ("S", ["field_operator", "robot_handler"]),
            ):
                u = make_user(f"pb_{code}_{c}{role}")  # a person needs no grant to be on a crew
                pc = f"PB-{code}{c}{role}"
                ps.register_person(w.ops, username=u.username, code=pc, hub_code=code)
                for k in kinds:
                    ps.grant_qualification(w.ops, person_code=pc, kind=k, valid_from=lo, valid_to=hi_dt)
                pair.append(pc)
            ps.register_vehicle(
                w.ops, code=f"VB-{code}{c}", robot_capacity=1, restraint_kit="SYN kit", hub_code=code
            )
            for v in range(BOARD_VISITS_PER_HUB // 2):
                h0 = 8 + v * 3
                visit = ps.request_visit(
                    w.ops, site_code="S-1", on_site_start=at(day, h0), on_site_end=at(day, h0 + 1),
                    travel_out_min=20, travel_back_min=20, tasks=["T-04"], required_scopes=["presence"],
                    hub_code=code,
                )["entity_id"]  # fmt: skip
                ps.plan_visit(w.ops, visit_code=visit, robot_code=robots[c * 20 + v], lead_code=pair[0],
                              second_code=pair[1], vehicle_code=f"VB-{code}{c}")  # fmt: skip
    from fleet.models import Robot

    assert Robot.objects.count() == 200
    perf_user = make_user("perf_board_ops", "ops_lead")
    c = Client()
    c.force_login(perf_user)
    url = f"/planning/board/?day={day.isoformat()}"
    results = {}
    for label, path in (("one hub (HB)", url + "&hub=HB"), ("three hubs", url)):
        assert c.get(path).status_code == 200  # warm-up
        times, stmts = [], set()
        for _ in range(RUNS):
            counter = Counter()
            t0 = time.perf_counter()
            with connection.execute_wrapper(counter):
                resp = c.get(path)
            times.append((time.perf_counter() - t0) * 1000)
            stmts.add(counter.n)
            assert resp.status_code == 200
        results[label] = {
            "p95_ms": round(p95(times), 1),
            "median_ms": round(sorted(times)[RUNS // 2], 1),
            "budget_ms": BOARD_MS,
            "statements": sorted(stmts),
            "visits": len(set(re.findall(r'data-row="([^"]+)"', resp.content.decode()))),
        }
    print("\n/planning/board/ on the 3-hub, 200-robot fixture:", json.dumps(results, indent=1))
    three, one = results["three hubs"], results["one hub (HB)"]
    assert three["p95_ms"] <= BOARD_MS, three
    assert len(three["statements"]) == 1 and three["statements"] == one["statements"], results


def build_scale_for_board() -> tuple[Any, Any, dict[str, Any]]:
    """The scale fixture with 100 robots (1,000 visits) at HUB-1: hubs B and C bring the total to 200."""
    w = ow.scale_world(robots=100)
    for k, v in THRESHOLDS.items():
        set_setting(k, v)
    return w, db_now(), {}


def day_bounds_for_quals() -> tuple[Any, Any]:
    from datetime import UTC, datetime

    from django.utils import timezone

    return timezone.now() - timedelta(days=30), datetime(2027, 6, 1, tzinfo=UTC)


DAY_FOR_BOARD = ow.DAY + timedelta(days=2)  # a scale-fixture day: visit k=1 of every HUB-1 robot
