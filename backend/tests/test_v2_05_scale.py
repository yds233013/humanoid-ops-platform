"""Phase 05 (docs/13_v2_phase03_contract.md 15.11): 500-robot validation. SYNTHETIC data only.

    pytest tests/test_v2_05_scale.py -m perf -s     p95 over 20 requests per endpoint on the 500-robot fixture

Fixture: tests.scale05_world.scale_world_500 (500 robots, 5 hubs, 5,000 visits, mixed operational states).
Whole requests through the Django test client as a regional ops lead (every hub visible: the widest read):

  * pages (budget p95 <= 2,000 ms): /hub/, Today (/ops/), the dispatch board (/planning/board/), /readiness/
  * POST /api/field/sync with 20 commands (budget p95 <= 1,000 ms): 20 session observations per request, from
    the crew of a run on site, fresh keys and the next 20 sequence numbers each time (every one applies)

Method as tests/test_operations_perf.py: one untimed warm-up request per endpoint, then 20 timed samples; p95 is
nearest rank (the 19th of 20). Printed as JSON: the environment before and after, the fixture cardinalities and
build timings, and per endpoint p95, median, budget, statements per request and every raw sample.
Local timings are indicative only; the release numbers are the three CI rounds (.github/workflows/perf.yml).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from django.db import connection

from tests.test_operations_perf import RUNS, Counter, _perf_selected, environment, p95

PAGE_MS = 2000
SYNC_MS = 1000
PAGES = ("/hub/", "/ops/", "/planning/board/", "/readiness/")
SYNC = "/api/field/sync"
DEVICE = "0b6f3c9e-0000-4000-8000-000000000505"


def _sample(fn: Any) -> dict[str, Any]:
    fn()  # warm-up (plan and catalog caches, template loading), and the endpoint must work
    samples, statements = [], set()
    for _ in range(RUNS):
        counter = Counter()
        t0 = time.perf_counter()
        with connection.execute_wrapper(counter):
            fn()
        samples.append((time.perf_counter() - t0) * 1000)
        statements.add(counter.n)
    return {
        "p95_ms": round(p95(samples), 1),
        "median_ms": round(sorted(samples)[RUNS // 2], 1),
        "statements": sorted(statements),
        "samples_ms": [round(x, 1) for x in samples],
    }


def _sync_run(w: Any) -> tuple[Any, Any]:
    """An open run on site (custody at the site, not ended) and the crew second's user."""
    from execution.models import ExecutionRun

    n = next(n for n, stage in sorted(w.scale_open_stages.items()) if stage == "at_site")
    run = (
        ExecutionRun.objects.select_related("revision__second__user")
        .filter(robot__code=w.robot_codes[n])
        .order_by("-id")
        .first()
    )
    assert run is not None
    return run, run.revision.second.user


@pytest.mark.perf
@pytest.mark.django_db(transaction=True)  # sync commits per command; the read snapshot is the outermost one
def test_500_robot_p95(request):
    if not _perf_selected(request.config):
        pytest.skip("run with -m perf (500-robot p95 budgets)")
    from django.db import transaction
    from django.test import Client
    from django.utils import timezone

    from tests.conftest import make_user
    from tests.scale05_world import cardinalities, scale_world_500

    t0 = time.perf_counter()
    with transaction.atomic():  # scale_world uses a temp table that lives for one transaction
        w = scale_world_500()
    build_s = round(time.perf_counter() - t0, 1)
    print("\n500-robot fixture built in", build_s, "s; phases:", json.dumps(w.timings))
    print("fixture cardinalities:", json.dumps(w.cardinalities, indent=1))
    print("environment before:", json.dumps(environment()))

    c = Client()
    c.force_login(make_user("perf05_ops", "ops_lead"))
    results: dict[str, dict[str, Any]] = {}
    for path in PAGES:

        def get(path: str = path) -> None:
            resp = c.get(path)
            assert resp.status_code == 200, (path, resp.status_code)

        results[path] = {**_sample(get), "budget_ms": PAGE_MS, "loadavg_at_start": environment()["loadavg"]}

    run, crew = _sync_run(w)
    fc = Client()
    fc.force_login(crew)
    state = {"seq": 1}

    def post20() -> None:
        commands = []
        for i in range(20):
            commands.append(
                {
                    "key": str(uuid.uuid4()),
                    "channel": "ordinary",
                    "seq": state["seq"] + i,
                    "last_decided_seq": None,
                    "command": "session.observation",
                    "payload": {"note": f"SYNTHETIC observation {state['seq'] + i}"},
                    "device_observed_at": timezone.now().isoformat(),
                }
            )
        body = {
            "device_id": DEVICE,
            "run": run.code,
            "generation": 1,
            "unsent_count": 0,
            "commands": commands,
        }
        resp = fc.post(SYNC, json.dumps(body), content_type="application/json")
        assert resp.status_code == 200, resp.content
        out = resp.json()
        assert [r["outcome"] for r in out["results"]] == ["confirmed"] * 20, out["results"][:2]
        state["seq"] += 20

    results[f"{SYNC} (20 commands)"] = {
        **_sample(post20),
        "budget_ms": SYNC_MS,
        "run": run.code,
        "loadavg_at_start": environment()["loadavg"],
    }
    print(f"\n500-robot p95 over {RUNS} requests:", json.dumps(results, indent=1))
    print("environment after:", json.dumps(environment()))
    print("fixture cardinalities after (sync rows added):", json.dumps(cardinalities()))
    over = {k: (v["p95_ms"], v["budget_ms"]) for k, v in results.items() if v["p95_ms"] > v["budget_ms"]}
    assert not over, f"p95 over budget (ms, budget): {over}"


@pytest.mark.django_db
def test_the_app_connection_never_jit_compiles():
    """The one Phase 05 optimization: JIT is off on every app connection (config/settings.py), so a read
    model whose plan cost grows past jit_above_cost with the fleet never pays the compile time."""
    with connection.cursor() as cur:
        cur.execute("SHOW jit")
        assert cur.fetchone()[0] == "off"
