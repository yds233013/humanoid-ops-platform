# 03B carry-forward: readiness performance, repeated measurement

Contract 14.7 (P2): "repeat readiness performance runs under controlled conditions". This is a measurement only.
Nothing was optimized and no budget changed. All data is fictional (the synthetic scale fixture).

## Why

The 03B closure note reported three single runs of the readiness scale page: 1,185, 1,476 and 1,511 ms p95.
Wave-1 runs gave 962–1,028 ms, and 03A gave 1,298–1,436 ms. Each figure was one p95 over 20 requests, taken
on a shared machine. One run cannot separate a real change from noise, so the EM asked for a repeatable
procedure.

## Method

- **Test:** `backend/tests/test_v2_03b_readiness_perf_repeat.py::test_readiness_scale_benchmark_repeated`.
  - It is marked `perf` and skipped unless run with `-m perf`.
  - It reuses the existing benchmark unchanged: `test_operations_perf._readiness_samples`, the sampler of
    `test_readiness_list_p95_on_the_scale_fixture`.
- **What is measured:** `GET /readiness/` as an ops lead, server side through the Django test client.
  - The data is the 200-robot scale fixture, with 2,001 visits.
  - The readiness look-ahead covers every visit, so the evaluator cap of 10 visits is always hit.
- **Controls:**
  - The fixture is built once, and the same database, user and settings serve every round.
  - The rounds run one after another, in one process.
  - Before each round, the test runs a full garbage collection and records the 1-minute load average. It
    records the load again after the round.
- **Rounds:** `PERF_REPEAT_ROUNDS` rounds (default 5). Each round is one warm-up request, then 20 timed
  requests.
- **Statistics:**
  - Per round: the median and the p95 (nearest rank, as in the benchmark).
  - Across rounds: the median of the round p95s, their min, max and spread, and the median and p95 of all
    100 samples pooled.
  - A unit test checks the arithmetic on fixed numbers. That test runs without `-m perf`.
- **Command:** run it with nothing else using the database:

  ```
  cd backend && set -a && . ../.env.example && set +a && \
    DB_NAME=<db> DJANGO_DB_IDENTITY=owner PERF_REPEAT_ROUNDS=5 PERF_REPEAT_OUT=/tmp/readiness_repeat.json \
    uv run pytest tests/test_v2_03b_readiness_perf_repeat.py -m perf -s
  ```

  `PERF_REPEAT_OUT` keeps the raw samples as JSON.

## Local results (2026-11-26)

**Environment:**
- Apple M2, 8 cores, 16 GB, macOS;
- Python 3.12.10;
- PostgreSQL 16.16 in Docker on localhost:5440;
- branch `v2/03-carry`;
- fixture build: 34.5 s.

**Host not idle.** The 1-minute load average was 4.7 at the start and 2.5–3.6 during the rounds. Another
project's dev server and the Docker database were running. This is a developer laptop, not a dedicated runner.

| Round | Median (ms) | p95 (ms) | Evaluator p95 (ms) | Statements / request | Load before → after |
|---|---|---|---|---|---|
| 1 | 1,194 | 1,299 | 776 | 895 | 3.60 → 3.07 |
| 2 | 1,264 | 1,453 | 914 | 895 | 3.07 → 2.54 |
| 3 | 1,350 | 1,433 | 924 | 895 | 2.54 → 3.32 |
| 4 | 1,335 | 1,523 | 957 | 895 | 3.32 → 2.91 |
| 5 | 1,401 | 1,475 | 989 | 895 | 2.91 → 3.20 |

| Summary | Value |
|---|---|
| p95, median of 5 rounds | **1,453 ms** |
| p95 range (min–max) | 1,299–1,523 ms |
| p95 spread | 223 ms (15.4% of the median) |
| Median, median of 5 rounds | 1,335 ms |
| Pooled (100 samples): median / p95 | 1,318 ms / 1,475 ms |
| Budget (readiness pages) | 2,000 ms p95 |

## Reading (for the EM's decision)

- **Within the budget.** Every round's p95 is under 2,000 ms, and the worst round has a margin of 477 ms
  (24%).
- **The statements are constant:**
  - 895 per request in every round;
  - the evaluator called at most 10 times per request.

  So the variation is time per statement, not more work.
- **The spread is about 15%** of the median p95 on this machine.
  - The earlier single runs (1,185–1,511 ms) fall inside the range seen here.
  - So the 03B closure's run-to-run difference is consistent with noise. It does not show a regression.
- **The times rise across rounds.** The round median rose steadily, from 1,194 to 1,401 ms, and the evaluator
  share of the request time rose with it. The load average did not rise in step. Possible causes:
  - the machine warming up and throttling;
  - Docker I/O;
  - something accumulating in the test database between requests.

  This was not investigated, because the task was to measure, not to optimize. If it matters, the next step is
  to rerun with more rounds (`PERF_REPEAT_ROUNDS=10`) on an idle machine, and check whether the rise continues.
- **The evaluator is most of the time.** Its p95 is 776–989 ms of a 1,299–1,523 ms request p95.
- **Not verified:**
  - an idle or dedicated runner;
  - several independent processes, or fresh fixtures per run;
  - comparison with the 03B closure machine.

  The numbers above are one session on one laptop.

**Suggested decision rule:**
- Optimize when the median of round p95s, measured this way on an idle runner, exceeds about 1,700 ms (85% of
  the budget).
- Or optimize when any round exceeds the 2,000 ms budget.

On these numbers, neither is met.
