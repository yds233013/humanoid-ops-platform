"""03B closure carry-forward (contract 14.7, P2): a controlled, REPEATED measurement of the readiness scale
benchmark. It measures; it optimizes nothing and changes no budget. Skipped unless run with ``-m perf``.

The benchmark is ``tests/test_operations_perf.py::test_readiness_list_p95_on_the_scale_fixture``: GET
/readiness/ as an ops lead on the 200-robot scale fixture (about 2,000 visits), with the readiness look-ahead
wide enough that every visit qualifies, so the evaluator cap (10 visits) is always hit.

Procedure (docs/evidence/03b_readiness_perf.md):

* the scale fixture is built ONCE; the same database, user and settings serve every round;
* ``PERF_REPEAT_ROUNDS`` rounds (default 5), run one after another in this one process. Each round is the
  benchmark's own sampler (``_readiness_samples``): one warm-up request, then 20 timed requests;
* a full garbage collection runs before each round, and the host load average is recorded before and after;
* per round: median and p95 (nearest rank, as the benchmark); across rounds: the median of the round p95s,
  their min, max and spread (max - min, and as a share of the median), and the median and p95 of all
  samples pooled;
* the result is printed and, when ``PERF_REPEAT_OUT`` names a file, written there as JSON.

Run, with nothing else using the database or the machine:

    pytest tests/test_v2_03b_readiness_perf_repeat.py -m perf -s

The only assertions are the benchmark's own (the evaluator cap, and every response 200). SYNTHETIC data.
"""

from __future__ import annotations

import gc
import json
import os
import platform
import statistics
import time
from typing import Any

import pytest
from django.db import connection

from operations import readmodels as rm
from tests.fleet_world import set_setting
from tests.test_operations_perf import RUNS, WINDOW, _perf_selected, _readiness_samples, build, p95

ROUNDS = int(os.environ.get("PERF_REPEAT_ROUNDS", "5"))


def summarize(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """Across rounds: median, min, max and spread of the round p95s and medians; pooled median and p95."""
    p95s = [r["p95_ms"] for r in rounds]
    medians = [r["median_ms"] for r in rounds]
    pooled = [x for r in rounds for x in r["samples_ms"]]
    mid = statistics.median(p95s)
    return {
        "rounds": len(rounds),
        "requests_per_round": RUNS,
        "round_p95_ms": p95s,
        "round_median_ms": medians,
        "p95_median_of_rounds_ms": round(mid, 1),
        "p95_min_ms": min(p95s),
        "p95_max_ms": max(p95s),
        "p95_spread_ms": round(max(p95s) - min(p95s), 1),
        "p95_spread_pct_of_median": round(100 * (max(p95s) - min(p95s)) / mid, 1) if mid else None,
        "median_median_of_rounds_ms": round(statistics.median(medians), 1),
        "pooled_median_ms": round(statistics.median(pooled), 1),
        "pooled_p95_ms": round(p95(pooled), 1),
        "pooled_n": len(pooled),
    }


def test_summarize_is_plain_arithmetic():
    """The summary itself, on fixed numbers (runs without -m perf, no database)."""
    rounds = [
        {"p95_ms": 100.0, "median_ms": 50.0, "samples_ms": [50.0] * 19 + [100.0]},
        {"p95_ms": 140.0, "median_ms": 60.0, "samples_ms": [60.0] * 19 + [140.0]},
        {"p95_ms": 120.0, "median_ms": 55.0, "samples_ms": [55.0] * 19 + [120.0]},
    ]
    s = summarize(rounds)
    assert s["p95_median_of_rounds_ms"] == 120.0 and (s["p95_min_ms"], s["p95_max_ms"]) == (100.0, 140.0)
    assert s["p95_spread_ms"] == 40.0 and s["p95_spread_pct_of_median"] == 33.3
    assert s["median_median_of_rounds_ms"] == 55.0 and s["pooled_n"] == 60
    assert s["pooled_median_ms"] == 55.0 and s["pooled_p95_ms"] == 60.0


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)
def test_readiness_scale_benchmark_repeated(request, monkeypatch):
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf")
    from django.db import transaction

    from tests.conftest import make_user

    t0 = time.perf_counter()
    with transaction.atomic():
        build()
    build_s = round(time.perf_counter() - t0, 1)
    set_setting("ops_readiness_lookahead_min", str(WINDOW))  # every visit qualifies: the cap must hold
    user = make_user("perf_rr_ops", "ops_lead")
    with connection.cursor() as cur:
        cur.execute("SHOW server_version")
        pg = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM planning_visit")
        visits = cur.fetchone()[0]
    rounds: list[dict[str, Any]] = []
    for _ in range(ROUNDS):
        gc.collect()
        load_before = os.getloadavg()[0]
        res = _readiness_samples(monkeypatch, user)
        res["load_before"], res["load_after"] = round(load_before, 2), round(os.getloadavg()[0], 2)
        assert res["evaluator_calls_per_request"] <= rm.READINESS_CAP
        rounds.append(res)
    out = {
        "benchmark": "GET /readiness/ on the 200-robot scale fixture (test_readiness_list_p95_on_the_scale_fixture)",
        "environment": {
            "python": platform.python_version(),
            "postgres": pg,
            "machine": platform.machine(),
            "system": platform.system(),
            "cpu_count": os.cpu_count(),
            "visits": visits,
            "fixture_build_s": build_s,
        },
        "summary": summarize(rounds),
        "rounds": [
            {
                k: r[k]
                for k in (
                    "median_ms",
                    "p95_ms",
                    "evaluator_p95_ms",
                    "statements",
                    "load_before",
                    "load_after",
                )
            }
            for r in rounds
        ],
    }
    print("\nrepeated readiness measurement:", json.dumps(out, indent=1))
    if path := os.environ.get("PERF_REPEAT_OUT"):
        with open(path, "w") as fh:
            json.dump({**out, "samples_ms": [r["samples_ms"] for r in rounds]}, fh, indent=1)
