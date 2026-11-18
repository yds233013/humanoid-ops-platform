"""Slice 5 time and statement budgets on the scale fixture (Engineer C). docs/05_operations_command_center_contract.md
sections 4a, 6.4, 6.5 and the EM perf decision. SYNTHETIC data only.

    pytest tests/test_operations_perf.py            statement budgets + a non-flaky 2 s bound per call
    pytest tests/test_operations_perf.py -m perf    p95 over 20 runs per read model (local Postgres)

Budgets (EM decision):
  * fleet_overview, incident_queue, metrics, inbox WITHOUT readiness: p95 <= 400 ms
  * timeline and inbox WITH readiness (they run the authoritative evaluator, <= 10 visits): p95 <= 2,000 ms
    (a separate release gate)
  * SQL: overview / queue / metrics constant (1 / 1 / 1 + inbox-without-readiness); readiness pages
    <= 12 + 10 x E with E pinned at 70 statements per evaluated visit
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
import time
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection

from operations import metrics as om
from operations import readmodels as rm
from tests import operations_world as ow
from tests.fleet_world import set_setting

RUNS = 20
E_PIN = 70
FAST_MS = 400  # read models without the evaluator
READINESS_MS = 2000  # timeline and inbox with readiness (separate release gate)
UPPER_S = 2.0  # normal runs: non-flaky bound on each read model's own time
WINDOW = 60 * 24 * 90
THRESHOLDS = {
    rm.CUSTODY_VERIFY_GRACE: "30",
    rm.RETURN_GRACE: "60",
    rm.INSPECTION_GRACE: "120",
    rm.READINESS_LOOKAHEAD: str(60 * 24 * 60),  # every scale visit qualifies: the cap of 10 is hit
}
WITH_READINESS = {"timeline", "inbox_with_readiness"}


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
    }
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
    es = res["timeline"]["evaluator_statements"]
    assert len(es) == 10, es  # the cap is hit on the scale fixture
    assert max(es) <= E_PIN, f"evaluator statements per visit grew past the pinned E = {E_PIN}: {es}"
    assert res["timeline"]["statements"] == 4 + sum(es) <= 12 + 10 * E_PIN
    if "inbox_without_readiness" in res:
        assert res["inbox_without_readiness"]["evaluator_statements"] == []
        assert res["inbox_without_readiness"]["statements"] <= 12
        assert res["metrics"]["statements"] == 1 + res["inbox_without_readiness"]["statements"]
    else:
        assert res["metrics"]["statements"] == 1
    if "inbox_with_readiness" in res:
        assert res["inbox_with_readiness"]["statements"] <= 12 + 10 * E_PIN


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
    clock = EvaluatorClock(monkeypatch)
    res = measure(cases, clock, runs=RUNS)
    check_statements(res)
    no_jit_plans(at)
    s = summary(res)
    print(f"p95 over {RUNS} runs:", json.dumps(s, indent=1))
    if "inbox_with_readiness" not in cases:
        print("open_exceptions: not available yet (Engineer B) - inbox p95 cases skipped")
    elif "inbox_without_readiness" not in cases:
        print("open_exceptions(include_readiness=False) not available yet - that p95 case skipped")
    over = {
        k: v["p95_ms"]
        for k, v in s.items()
        if v["p95_ms"] > (READINESS_MS if k in WITH_READINESS else FAST_MS)
    }
    assert not over, f"p95 over budget (ms): {over}; full: {s}"
