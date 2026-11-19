"""Slice 6 baseline from the platform (frozen contract docs/06_scaling_capacity_contract.md section 8, rev 4).

`measure(at, start=, end=)` reads Slices 2-5 records over the window [start, end) and returns one `Metric`
per measured quantity, each with its sample size, kind (planned / observed / observed_censored) and whether it
meets the section 8 minimum sample. `apply(values, baseline)` replaces the registry's platform_measured inputs
ONLY where the metric is sufficient and returns a provenance row for every platform_measured input.

Everything here is a SYNTHETIC demo measurement, not real-world performance.

Read-only contract: `measure` must run inside `operations.snapshot.read_snapshot()` (REPEATABLE READ, READ
ONLY, outermost). It checks that it is inside a read-only transaction and refuses otherwise; it issues plain
SELECTs only (no FOR UPDATE / FOR SHARE, no advisory locks, no set_config, no nextval) and writes nothing.
`at` is the snapshot time the caller got from `read_snapshot()`.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from django.db import connection

from capacity.inputs import REGISTRY

Kind = Literal["planned", "observed", "observed_censored"]

SYNTHETIC = "synthetic demo measurement, not real-world performance"

# Section 8 minimum samples.
MIN_RESERVATIONS = 10
MIN_SESSIONS = 10
MIN_TRIPS = 10
MIN_DEPARTURES = 30
MIN_HOLDS = 10
MIN_HOLDS_RELEASED = 5

# Planned metrics: metric key -> planning_reservation.purpose (robot-resource rows).
PLANNED = {
    "planned_inspect_min": "inspection",
    "planned_load_min": "loading",
    "planned_unload_min": "unloading",
    "planned_on_site_min": "on_site",
}
OBSERVED_ON_SITE = "observed_on_site_min"
OBSERVED_TRAVEL = "observed_travel_one_way_min"
INCIDENTS = "incidents_per_visit"
VEHICLE_CAPACITY = "vehicle_robot_capacity"
HOLD_DAYS = "observed_hold_duration_days"  # informational only: never mapped onto hold_min_days

# Structural platform rules (registry rows 12, 14, 16): reported as rules, value = the rule.
RULES: dict[str, tuple[float, str]] = {
    "robots_per_visit": (1, "platform rule (Slice 2): one robot per visit"),
    "crew_size": (2, "platform rule (Slice 2): lead + second per visit"),
    "holds_per_incident": (1, "platform rule (Slice 4): every incident holds the robot"),
}


@dataclass(frozen=True)
class Metric:
    key: str
    value: float | None
    n: int
    kind: Kind
    window_start: datetime
    window_end: datetime
    sufficient: bool
    note: str


@dataclass
class Baseline:
    at: datetime
    window_start: datetime
    window_end: datetime
    metrics: dict[str, Metric] = field(default_factory=dict)


class NotInReadOnlySnapshot(RuntimeError):
    pass


# ------------------------------------------------------------------ pure helpers
def _minutes(d: timedelta) -> float:
    return d.total_seconds() / 60


def median(xs: Sequence[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def km_median(samples: Sequence[tuple[float, bool]]) -> float | None:
    """Kaplan-Meier (product-limit) median of (duration, event) pairs; event=False means right-censored.

    S(t) = prod over distinct event times t_i <= t of (1 - d_i / n_i), n_i = samples with duration >= t_i
    (a censoring at the same time as an event is still at risk at that time). The median is the smallest event
    time with S(t) <= 0.5; None when the curve never reaches 0.5 (median not reached)."""
    times = sorted({t for t, ev in samples if ev})
    s = 1.0
    for t in times:
        at_risk = sum(1 for d, _ in samples if d >= t)
        deaths = sum(1 for d, ev in samples if ev and d == t)
        s *= 1 - deaths / at_risk
        if s <= 0.5:
            return t
    return None


def _note(n: int, minimum: int, what: str, extra: str = "") -> tuple[bool, str]:
    if n == 0:
        return False, f"no platform data (n=0 {what}; need >= {minimum}); {SYNTHETIC}"
    if n < minimum:
        return False, f"insufficient platform data (n={n} {what} < {minimum}); {SYNTHETIC}"
    return True, f"n={n} {what}{extra}; {SYNTHETIC}"


# ------------------------------------------------------------------ reads (plain SELECTs, in the snapshot)
def _rows(sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


DEPARTED = """
    SELECT d.id AS departure_id, d.visit_id, d.departed_at
    FROM readiness_departureevent d
    WHERE d.departed_at >= %(start)s AND d.departed_at < %(end)s
"""

SQL_PLANNED = f"""
    SELECT r.purpose, r.starts_at, r.ends_at
    FROM ({DEPARTED}) d
    JOIN planning_visit v ON v.id = d.visit_id
    JOIN planning_reservation r ON r.revision_id = v.current_revision_id
    JOIN planning_resource res ON res.id = r.resource_id AND res.kind = 'robot'
    WHERE r.released_at IS NULL AND r.purpose = ANY(%(purposes)s)
"""

SQL_SESSIONS = f"""
    SELECT x.id,
      (SELECT min(s.recorded_at) FROM execution_sessionevent s WHERE s.run_id = x.id AND s.kind = 'start'),
      (SELECT max(s.recorded_at) FROM execution_sessionevent s WHERE s.run_id = x.id AND s.kind = 'stop')
    FROM ({DEPARTED}) d
    JOIN execution_executionrun x ON x.departure_id = d.departure_id
"""

# Aborted-outbound runs (arrived_hub straight after departure) have no arrived_site event, so the inner join
# excludes them; the NOT EXISTS states it explicitly (no arrived_hub before the arrival).
SQL_TRIPS = f"""
    SELECT x.id, d.departed_at, te.recorded_at
    FROM ({DEPARTED}) d
    JOIN execution_executionrun x ON x.departure_id = d.departure_id
    JOIN execution_tripevent te ON te.run_id = x.id AND te.kind = 'arrived_site'
    WHERE NOT EXISTS (
      SELECT 1 FROM execution_tripevent h
      WHERE h.run_id = x.id AND h.kind = 'arrived_hub' AND h.recorded_at < te.recorded_at
    )
"""

SQL_INCIDENTS = f"""
    SELECT count(*),
      count(DISTINCT x.id) FILTER (WHERE EXISTS (SELECT 1 FROM execution_incident i WHERE i.run_id = x.id))
    FROM ({DEPARTED}) d
    LEFT JOIN execution_executionrun x ON x.departure_id = d.departure_id
"""

SQL_HOLDS = """
    SELECT h.placed_at, h.released_at
    FROM fleet_hold h
    WHERE h.source_type = 'incident' AND h.placed_at >= %(start)s AND h.placed_at < %(end)s
"""

SQL_VEHICLES = "SELECT robot_capacity FROM planning_vehicle WHERE robot_capacity IS NOT NULL"


def _require_read_only_snapshot() -> None:
    if not connection.in_atomic_block:
        raise NotInReadOnlySnapshot(
            "capacity.baseline.measure must run inside operations.snapshot.read_snapshot()"
        )
    with connection.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        row = cur.fetchone()
    if row is None or row[0] != "on":
        raise NotInReadOnlySnapshot(
            "capacity.baseline.measure must run inside a READ ONLY transaction "
            "(operations.snapshot.read_snapshot())"
        )


# ------------------------------------------------------------------ measure
def measure(at: datetime, *, start: datetime, end: datetime) -> Baseline:
    """Measure the platform baseline over departures (and incident holds placed) in [start, end).

    Must be called inside `read_snapshot()`; `at` is its time. Zero writes, no locks beyond plain SELECTs."""
    if not start < end:
        raise ValueError("the baseline window needs start < end")
    _require_read_only_snapshot()
    p: dict[str, Any] = {"start": start, "end": end}
    b = Baseline(at=at, window_start=start, window_end=end)

    def put(key: str, value: float | None, n: int, kind: Kind, ok: bool, note: str) -> None:
        b.metrics[key] = Metric(key, value, n, kind, start, end, ok, note)

    # Planned reservation lengths per purpose (robot resource, active rows of the departed revision).
    planned: dict[str, list[float]] = {purpose: [] for purpose in PLANNED.values()}
    for purpose, s0, s1 in _rows(SQL_PLANNED, {**p, "purposes": list(PLANNED.values())}):
        planned[purpose].append(_minutes(s1 - s0))
    for key, purpose in PLANNED.items():
        xs = planned[purpose]
        ok, note = _note(len(xs), MIN_RESERVATIONS, f"{purpose} reservations", "; planned, not observed")
        put(key, median(xs), len(xs), "planned", ok, f"median planned minutes; {note}")

    # Observed session: first start -> last stop per run; runs without both events are skipped.
    sess = [
        _minutes(stop - st) for _, st, stop in _rows(SQL_SESSIONS, p) if st is not None and stop is not None
    ]
    ok, note = _note(len(sess), MIN_SESSIONS, "sessions")
    put(
        OBSERVED_ON_SITE,
        median(sess),
        len(sess),
        "observed",
        ok,
        f"median session start -> stop minutes; {note}",
    )

    # Observed travel: departure -> arrived_site (aborted outbound excluded).
    trips = [_minutes(arr - dep) for _, dep, arr in _rows(SQL_TRIPS, p)]
    ok, note = _note(len(trips), MIN_TRIPS, "trips")
    put(
        OBSERVED_TRAVEL,
        median(trips),
        len(trips),
        "observed",
        ok,
        f"median departure -> arrived_site minutes; {note}",
    )

    # Incidents per visit: runs with >= 1 incident / departures.
    (row,) = _rows(SQL_INCIDENTS, p)
    deps, with_incident = int(row[0]), int(row[1])
    ok, note = _note(deps, MIN_DEPARTURES, "departures", f"; {with_incident} runs with >= 1 incident")
    put(INCIDENTS, with_incident / deps if deps else None, deps, "observed", ok, f"observed rate; {note}")

    # Incident-hold duration (calendar days), Kaplan-Meier with right-censoring at the window end (or `at`, if
    # the window ends in the future: nothing after the snapshot time can have been observed).
    censor_at = min(end, at)
    samples: list[tuple[float, bool]] = []
    for placed, released in _rows(SQL_HOLDS, p):
        if released is not None and released < censor_at:
            samples.append(((released - placed).total_seconds() / 86400, True))
        else:
            samples.append((max((censor_at - placed).total_seconds(), 0.0) / 86400, False))
    n_rel = sum(1 for _, ev in samples if ev)
    km = km_median(samples)
    if not samples:
        ok, note = (
            False,
            f"no platform data (n=0 incident holds; need >= {MIN_HOLDS} "
            f"with >= {MIN_HOLDS_RELEASED} released)",
        )
    elif len(samples) < MIN_HOLDS or n_rel < MIN_HOLDS_RELEASED:
        ok = False
        note = (
            f"insufficient platform data (n={len(samples)} incident holds, {n_rel} released; "
            f"need >= {MIN_HOLDS} "
            f"with >= {MIN_HOLDS_RELEASED} released)"
        )
    else:
        ok = km is not None
        note = f"n={len(samples)} incident holds, {n_rel} released, {len(samples) - n_rel} right-censored"
        if km is None:
            note += "; Kaplan-Meier median not reached"
    put(
        HOLD_DAYS,
        km,
        len(samples),
        "observed_censored",
        ok,
        f"Kaplan-Meier median calendar days; {note}; informational only, never sets hold_min_days "
        f"(a synthetic policy minimum); {SYNTHETIC}",
    )

    # Vehicle robot capacity: recorded on planning vehicles (current, not windowed); the smallest is used.
    caps = [float(c) for (c,) in _rows(SQL_VEHICLES, {})]
    put(
        VEHICLE_CAPACITY,
        min(caps) if caps else None,
        len(caps),
        "observed",
        bool(caps),
        (
            f"smallest recorded planning_vehicle.robot_capacity over {len(caps)} vehicles "
            "(current, not windowed)"
            if caps
            else "no platform data (no vehicle with a recorded robot capacity)"
        )
        + f"; {SYNTHETIC}",
    )
    return b


# ------------------------------------------------------------------ apply
def _provenance(key: str, used: str, value: Any, m: Metric | None, kind: str, reason: str) -> dict[str, Any]:
    return {"key": key, "used": used, "value": value, "n": m.n if m else None, "kind": kind, "reason": reason}


def _from_metric(key: str, m: Metric | None, fallback: Any) -> tuple[Any, dict[str, Any]]:
    if m is not None and m.sufficient:
        return m.value, _provenance(key, "platform", m.value, m, m.kind, f"platform {m.key}: {m.note}")
    why = m.note if m is not None else "no platform data (metric not measured)"
    return fallback, _provenance(
        key, "fallback", fallback, m, m.kind if m else "none", f"{why}; registry fallback used"
    )


def apply(values: dict[str, Any], baseline: Baseline) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Registry values with platform_measured inputs replaced ONLY where the metric is sufficient, plus one
    provenance row {key, used, value, n, kind, reason} for EVERY platform_measured input of the registry.
    Platform rules (robots_per_visit, crew_size, holds_per_incident) report kind "platform_rule" and n None.
    observed_hold_duration_days is never mapped."""
    out = dict(values)
    prov: list[dict[str, Any]] = []
    ms = baseline.metrics
    mapping = {
        "inspect_min": "planned_inspect_min",
        "load_min": "planned_load_min",
        "unload_min": "planned_unload_min",
        "travel_one_way_min": OBSERVED_TRAVEL,
        "incidents_per_visit": INCIDENTS,
        "vehicle_robot_capacity": VEHICLE_CAPACITY,
    }
    for inp in REGISTRY:
        if inp.provenance != "platform_measured":
            continue
        key = inp.key
        fallback = values.get(key)
        if key in RULES:
            rule, why = RULES[key]
            out[key] = rule
            prov.append(_provenance(key, "platform", rule, None, "platform_rule", f"{why}; {SYNTHETIC}"))
        elif key == "on_site_min":
            obs, pl = ms.get(OBSERVED_ON_SITE), ms.get("planned_on_site_min")
            if obs is not None and obs.sufficient:
                out[key], row = _from_metric(key, obs, fallback)
                row["reason"] = f"observed session preferred (sufficient); {row['reason']}"
            elif pl is not None and pl.sufficient:
                out[key], row = _from_metric(key, pl, fallback)
                obs_note = obs.note if obs is not None else "not measured"
                row["reason"] = (
                    f"planned on_site used: observed session not sufficient ({obs_note}); {row['reason']}"
                )
            else:
                out[key], row = _from_metric(key, pl, fallback)
                obs_note = obs.note if obs is not None else "not measured"
                row["reason"] = f"observed session: {obs_note}; planned on_site: {row['reason']}"
            prov.append(row)
        elif key in mapping:
            out[key], row = _from_metric(key, ms.get(mapping[key]), fallback)
            prov.append(row)
        else:  # a platform_measured input without a measurement: never silently dropped
            prov.append(
                _provenance(
                    key,
                    "fallback",
                    fallback,
                    None,
                    "none",
                    "no platform measurement defined; registry fallback used",
                )
            )
    return out, prov
