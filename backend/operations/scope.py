"""Hub read scope of the operations pages (v2 03A, invariant I-8; EM closure item 2026-11-25).

A hub-scoped viewer (hub technician, field operator: grants at named hubs) sees only the rows of their hubs on
Robot status, Issues, the incident queue, the timeline, the close-out and the production blockers. A regional
viewer (``hub_ids`` None) sees everything, unchanged. The hub of a row is the record's own:

  robot status            the robot's home hub
  incident (queue)        the visit's dispatch hub when the incident has a visit, else the robot's home hub
  issue                   the robot's hub, else the visit's hub; an issue about neither (a missing setting, a
                          configuration) is shown to everyone who can read the page, as on Today
  timeline visit / run    the visit's dispatch hub (the read model filters visits before the readiness cap)

Each filter is at most two set-based statements, whatever the number of rows; nothing runs for a regional
viewer.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from fleet.models import Robot
from planning.models import Visit


def robot_hubs_by_code(codes: Iterable[str]) -> dict[str, int]:
    codes = sorted({c for c in codes if c})
    if not codes:
        return {}
    return dict(Robot.objects.filter(code__in=codes).values_list("code", "hub_id"))


def visit_hubs_by_code(codes: Iterable[str]) -> dict[str, int]:
    codes = sorted({c for c in codes if c})
    if not codes:
        return {}
    return dict(Visit.objects.filter(code__in=codes).values_list("code", "hub_id"))


def keep_robots(rows: list[Any], hub_ids: set[int] | None) -> list[Any]:
    """Robot status rows (``robot_id``) of the viewer's hubs."""
    if hub_ids is None:
        return list(rows)
    hubs = dict(Robot.objects.filter(pk__in=[r.robot_id for r in rows]).values_list("id", "hub_id"))
    return [r for r in rows if hubs.get(r.robot_id) in hub_ids]


def keep_incidents(rows: list[Any], hub_ids: set[int] | None) -> list[Any]:
    """Incident queue rows (``robot``, ``visit`` codes) of the viewer's hubs."""
    if hub_ids is None:
        return list(rows)
    visits = visit_hubs_by_code(r.visit for r in rows if r.visit)
    robots = robot_hubs_by_code(r.robot for r in rows if not r.visit)
    return [r for r in rows if (visits.get(r.visit) if r.visit else robots.get(r.robot)) in hub_ids]


def keep_issues(items: list[Any], hub_ids: set[int] | None) -> list[Any]:
    """Issues (``robot`` / ``visit`` codes) of the viewer's hubs, plus issues about no robot and no visit."""
    if hub_ids is None:
        return list(items)
    robots = robot_hubs_by_code(x.robot for x in items if x.robot)
    visits = visit_hubs_by_code(x.visit for x in items if x.visit and not x.robot)
    out = []
    for x in items:
        if not x.robot and not x.visit:
            out.append(x)
        elif (robots.get(x.robot) if x.robot else visits.get(x.visit)) in hub_ids:
            out.append(x)
    return out


def keep_timeline(tl: Any, hub_ids: set[int] | None) -> Any:
    """The timeline's runs of the viewer's hubs (its visits are filtered by the read model before the cap)."""
    if hub_ids is None:
        return tl
    visits = visit_hubs_by_code(r.visit for r in tl.runs)
    return dataclasses.replace(tl, runs=[r for r in tl.runs if visits.get(r.visit) in hub_ids])


def keep_blockers(panel: Any, hub_ids: set[int] | None) -> Any:
    """Production blockers of the viewer's hubs: a robot hold that cannot be released (HOLD_UNRELEASABLE) is
    kept only for a robot of those hubs; configuration-level blockers (patterns, policies, deadline sets,
    pattern holds) apply to every hub and are kept."""
    if hub_ids is None:
        return panel
    from fleet.models import Hold

    codes = [b.entity_ref for b in panel.items if b.code == "HOLD_UNRELEASABLE"]
    hubs = dict(Hold.objects.filter(code__in=codes, robot__isnull=False).values_list("code", "robot__hub_id"))
    items = [
        b
        for b in panel.items
        if b.code != "HOLD_UNRELEASABLE" or b.entity_ref not in hubs or hubs[b.entity_ref] in hub_ids
    ]
    return dataclasses.replace(panel, items=items)
