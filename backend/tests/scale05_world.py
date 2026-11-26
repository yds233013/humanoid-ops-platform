"""Phase 05 (docs/13_v2_phase03_contract.md 15.11): the 500-robot synthetic scale fixture. SYNTHETIC data only.

    scale_world_500()   500 robots, 5 hubs, 5,000 visits (10 per robot), 1,500 runs, 500 incidents, plus
                        transfers, holds, regional pauses, chargers / readings / charging sessions

Built on ``tests.operations_world.scale_world`` (hubs=5, crews=100: crew c, its people, its van and the 5 robots
it serves belong to hub c // 20, so every hub has 100 robots and 1,000 visits). That builder already gives every
robot 2 closed runs and a third run left at a varied stage (departed .. closed), visits in mixed states
(confirmed / planned / requested / cancelled) and incidents through the real command (each holds its robot).
This module layers on, through the real commands where they exist:

  * holds: a manual or maintenance robot hold on 40 more robots (8 per hub), 10 of them released again;
  * transfers: 2 spare vans per hub (fictional, no trips); 5 sent on (in transit), 2 received, 1 refused;
  * regional pauses: HUB-5 paused (active), HUB-4 paused and lifted (history);
  * charging: 4 chargers per hub, a fresh state-of-charge reading for every third robot (owner rows, as the
    Slice 5 perf fixture writes them), and charging sessions started for up to 2 robots per hub whose last run
    is closed.

Returns the scale world with ``w.cardinalities`` (row counts of the tables that matter) and ``w.timings``.
"""

from __future__ import annotations

import random
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from django.db import connection
from django.utils import timezone

from tests import operations_world as ow

ROBOTS, HUBS, CREWS, VISITS_PER_ROBOT, INCIDENTS = 500, 5, 100, 10, 500

CARDINALITY_TABLES = (
    "platform_core_hub",
    "fleet_robot",
    "planning_vehicle",
    "planning_person",
    "planning_visit",
    "planning_reservation",
    "execution_executionrun",
    "execution_custodyevent",
    "execution_incident",
    "fleet_hold",
    "fleet_hub_transfer",
    "operations_region_pause",
    "hub_charger",
    "hub_charging_session",
    "robot_soc_reading",
    "readiness_departureevent",
)


def _count(table: str) -> int | None:
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", [table])
        if cur.fetchone()[0] is None:
            return None
        cur.execute(f"SELECT count(*) FROM {table}")
        return int(cur.fetchone()[0])


def cardinalities() -> dict[str, Any]:
    out: dict[str, Any] = {t: _count(t) for t in CARDINALITY_TABLES}
    with connection.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM planning_visit GROUP BY status ORDER BY status")
        out["visits_by_status"] = dict(cur.fetchall())
        cur.execute(
            "SELECT h.code, count(r.id) FROM platform_core_hub h LEFT JOIN fleet_robot r ON r.hub_id = h.id"
            " GROUP BY h.code ORDER BY h.code"
        )
        out["robots_by_hub"] = dict(cur.fetchall())
        cur.execute(
            "SELECT h.code, count(v.id) FROM platform_core_hub h LEFT JOIN planning_visit v ON v.hub_id = h.id"
            " GROUP BY h.code ORDER BY h.code"
        )
        out["visits_by_hub"] = dict(cur.fetchall())
        cur.execute("SELECT count(*) FROM fleet_hold WHERE released_at IS NULL")
        out["holds_open"] = int(cur.fetchone()[0])
    return out


def scale_world_500(seed: int = 5) -> SimpleNamespace:
    import time

    from fleet import transfers as tr
    from fleet.models import Robot
    from hub import charging as hc
    from hub.models import Charger, RobotSocReading
    from operations import pauses
    from planning import services as ps
    from platform_core.models import Hub

    rng = random.Random(seed)
    w = ow.scale_world(
        robots=ROBOTS,
        visits_per_robot=VISITS_PER_ROBOT,
        incidents=INCIDENTS,
        seed=seed,
        crews=CREWS,
        hubs=HUBS,
    )
    t0 = time.perf_counter()
    hubs = list(Hub.objects.order_by("code"))
    assert len(hubs) == HUBS, [h.code for h in hubs]

    # ---------------------------------------------------------------- more holds (commands)
    by_hub: dict[int, list[str]] = {}
    for code, hub_id in Robot.objects.filter(code__in=w.robot_codes).values_list("code", "hub_id"):
        by_hub.setdefault(hub_id, []).append(code)
    w.extra_holds = []
    for robots in by_hub.values():
        for code in rng.sample(sorted(robots), 8):
            source = rng.choice(["manual", "maintenance"])
            w.extra_holds.append(ow.place_hold(w, "robot", code, source=source))
    for code in w.extra_holds[::4]:
        ow.release(w, code)

    # ---------------------------------------------------------------- transfers (spare vans, commands)
    w.transfers = []
    for i, hub in enumerate(hubs):
        for j in (1, 2):
            ps.register_vehicle(
                w.ops, code=f"VT-{i + 1}{j}", robot_capacity=1, restraint_kit="SYN kit", hub_code=hub.code
            )
    for i in range(len(hubs)):
        dest = hubs[(i + 1) % len(hubs)]
        tid = tr.start_transfer(
            w.ops,
            kind="vehicle",
            code=f"VT-{i + 1}1",
            to_hub_code=dest.code,
            reason="SYNTHETIC: rebalance vans",
        )["id"]
        w.transfers.append(tid)
    tr.receive_transfer(w.ops, transfer_id=w.transfers[0])
    tr.receive_transfer(w.ops, transfer_id=w.transfers[1])
    tr.refuse_transfer(w.ops, transfer_id=w.transfers[2], reason="SYNTHETIC: no parking bay")
    for i in (3, 4):  # two more in transit, from the second van of hubs 4 and 5
        hub, dest = hubs[i], hubs[(i + 2) % len(hubs)]
        w.transfers.append(
            tr.start_transfer(
                w.ops,
                kind="vehicle",
                code=f"VT-{i + 1}2",
                to_hub_code=dest.code,
                reason="SYNTHETIC: cover a shift",
            )["id"]
        )

    # ---------------------------------------------------------------- regional pauses (commands)
    lifted = pauses.pause_region(
        w.ops, hub_code=hubs[3].code, zone_code=None, reason="SYNTHETIC: storm drill"
    )
    pauses.lift_pause(w.ops, pause_id=int(lifted["entity_id"]), reason="SYNTHETIC: drill over")
    pauses.pause_region(w.ops, hub_code=hubs[4].code, zone_code=None, reason="SYNTHETIC: road closure")

    # ---------------------------------------------------------------- charging
    ow.set_setting("ops_soc_fresh_min", "240")
    now = timezone.now()
    Charger.objects.bulk_create(
        [
            Charger(code=f"C-{i + 1}{n}", name=f"Bay {n}", hub=hub)
            for i, hub in enumerate(hubs)
            for n in range(1, 5)
        ]
    )
    RobotSocReading.objects.bulk_create(
        [
            RobotSocReading(
                robot=r,
                soc_pct=30 + (i * 7) % 70,
                observed_at=now,
                source="manual_reading",
                recorded_by=w.ops,
                recorded_at=now,
            )
            for i, r in enumerate(Robot.objects.order_by("id")[::3])
        ]
    )
    with connection.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT r.code FROM fleet_hold h JOIN fleet_robot r ON r.id = h.robot_id"
            " WHERE h.released_at IS NULL AND h.robot_id IS NOT NULL"
        )
        held = {c for (c,) in cur.fetchall()}
    closed = [w.robot_codes[n] for n, stage in sorted(w.scale_open_stages.items()) if stage == "closed"]
    hub_of = dict(Robot.objects.filter(code__in=closed).values_list("code", "hub_id"))
    w.charging_sessions = []
    for i, hub in enumerate(hubs):
        free = [c for c in closed if hub_of.get(c) == hub.id and c not in held][:2]
        for n, code in enumerate(free, start=1):
            w.charging_sessions.append(
                hc.start_charging(
                    w.ops,
                    charger_code=f"C-{i + 1}{n}",
                    robot_code=code,
                    planned_until=now + timedelta(hours=2),
                    start_soc_pct=35,
                )
            )
    with connection.cursor() as cur:
        cur.execute(ow.ANALYZE_SCALE)
        cur.execute(
            "ANALYZE fleet_hub_transfer, hub_charger, hub_charging_session, robot_soc_reading, planning_vehicle,"
            " platform_core_hub"
        )
    w.timings["overlay_500"] = round(time.perf_counter() - t0, 1)
    w.cardinalities = cardinalities()
    return w
