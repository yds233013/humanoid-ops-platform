"""Zones of a hub (v2 03B-1, docs/13_v2_phase03_contract.md 13.2, 13.4 item 3, 13.5).

FROZEN read accessors: ``zone_of(visit) -> Zone | None`` (the visit's destination zone; None = unknown) and
``hub_origin_zone(hub) -> Zone | None`` (where the hub's trips start and end; None = unknown).

Setup commands (run_command, admin or ops lead; the database enforces the rules in platform_core/0006 and
planning/0013):
  * ``create_zone`` / ``retire_zone``: a retired zone keeps its history and cannot be used for new visits;
  * ``set_hub_home_zone``: the zone the hub's vans leave from and return to (``hub_origin_zone``);
  * ``set_site_zone``: the zone of a home, with a reason, through the guarded database function
    planning_set_site_zone (v2 03B-4; the regional ops lead only). A visit takes its home's zone when it is
    booked or re-planned.
"""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction

from planning.models import Site, Visit
from platform_core.commands import Conflict, Invalid, Outcome, guarded_function, run_command
from platform_core.hubscope import owner_hub
from platform_core.middleware import current_request_id
from platform_core.models import Hub, Zone

ZONE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,11}$")


def zone_of(visit: Visit) -> Zone | None:
    """The visit's destination zone (its home's zone at plan time), or None when unknown. No statement when
    ``visit.zone`` is already loaded (``select_related("zone")``); else one."""
    if visit.zone_id is None:
        return None
    return visit.zone


def hub_origin_zone(hub: Hub | int | None) -> Zone | None:
    """The zone the hub's vans leave from and return to (``platform_core_hub.home_zone_id``), or None when
    unknown: then every zoned visit dispatched from the hub has "Travel time unknown". No statement when given
    a Hub whose ``home_zone`` is already loaded (``select_related("home_zone")``)."""
    if hub is None:
        return None
    if isinstance(hub, Hub):
        return hub.home_zone if hub.home_zone_id is not None else None
    return Zone.objects.filter(pk__in=Hub.objects.filter(pk=hub).values("home_zone_id")).first()


def home_zone_changed(visit: Visit) -> bool:
    """True when the visit's home is no longer in the zone the visit was planned with (13.5: a visit's zone is
    its home's zone at plan time; a re-plan takes the home's zone). The ONE predicate behind the readiness
    blocker PLANNING_HOME_ZONE_CHANGED, the readiness page's planning header, the planner and the board. No
    statement when ``visit.site`` is loaded."""
    return visit.zone_id != visit.site.zone_ref_id


def home_zone_change_text(visit: Visit) -> str:
    """What changed, naming the zones: "Home H-1 was put in zone Lake Merritt (LM) after this visit was
    planned". "" when nothing changed. No statement when ``zone`` and ``site__zone_ref`` are loaded."""
    if not home_zone_changed(visit):
        return ""
    site = visit.site
    now = site.zone_ref if site.zone_ref_id is not None else None
    then = visit.zone if visit.zone_id is not None else None
    if then is None:
        return f"Home {site.code} was put in zone {zone_label(now)} after this visit was planned"
    if now is None:
        return (
            f"Home {site.code} no longer has a zone; it was in zone {zone_label(then)} when this visit "
            "was planned"
        )
    return (
        f"Home {site.code} moved from zone {zone_label(then)} to zone {zone_label(now)} after this visit "
        "was planned"
    )


HOME_ZONE_CHANGED_ACTION = "Plan the visit again so its zone, travel time and pauses are checked."


def home_zone_change_blocker(visit: Visit) -> dict[str, str] | None:
    """The home-zone change as a planning blocker (code HOME_ZONE_CHANGED, Blocked) for the planner and the
    board, naming the zones; None when the home is still in the visit's zone. The readiness gate reports the
    same fact as PLANNING_HOME_ZONE_CHANGED."""
    text = home_zone_change_text(visit)
    if not text:
        return None
    return {
        "code": "HOME_ZONE_CHANGED",
        "level": "blocked",
        "message": f"{text}.",
        "action": HOME_ZONE_CHANGED_ACTION,
    }


def zone_label(zone: Zone | None, hub: Hub | None = None) -> str:
    """``Downtown (DT)``, ``Downtown (DT), Oakland hub`` with a hub, or ``Unknown zone``."""
    if zone is None:
        return "Unknown zone"
    text = f"{zone.name} ({zone.code})"
    return f"{text}, {hub.name}" if hub is not None else text


def _hub(code: str) -> Hub:
    hub = owner_hub(code)
    if hub is None:
        raise Invalid("Choose the hub")
    return hub


def _zone(hub: Hub, code: str) -> Zone:
    z = Zone.objects.filter(hub=hub, code=(code or "").strip().upper()).first()
    if z is None:
        raise Invalid(f"{hub.name} ({hub.code}) has no zone {code}")
    return z


def create_zone(actor: Any, *, hub_code: str, code: str, name: str) -> dict[str, Any]:
    code, name = (code or "").strip().upper(), (name or "").strip()
    hub = _hub(hub_code)

    def handler() -> Outcome:
        if not ZONE_CODE.match(code):
            raise Invalid("The zone code uses capital letters, digits and dashes, up to 12 characters")
        if not name or len(name) > 80:
            raise Invalid("Give the zone a name (up to 80 characters)")
        if Zone.objects.filter(hub=hub, code=code).exists():
            raise Conflict(f"{hub.name} ({hub.code}) already has a zone {code}")
        with transaction.atomic():
            z = Zone.objects.create(hub=hub, code=code, name=name)
        return Outcome("zone", str(z.pk), {"hub": hub.code, "code": code, "name": name})

    return run_command(
        actor,
        "planning.create_zone",
        permission="planning.manage_zones",
        payload={"hub": hub.code, "code": code, "name": name},
        handler=handler,
        hub=hub,
    )


def retire_zone(actor: Any, *, hub_code: str, code: str) -> dict[str, Any]:
    hub = _hub(hub_code)

    def handler() -> Outcome:
        z = Zone.objects.select_for_update().get(pk=_zone(hub, code).pk)
        if not z.active:
            raise Conflict(f"Zone {z.code} is already retired")
        z.active = False
        z.save(update_fields=["active"])
        return Outcome("zone", str(z.pk), {"hub": hub.code, "code": z.code, "active": False})

    return run_command(
        actor,
        "planning.retire_zone",
        permission="planning.manage_zones",
        payload={"hub": hub.code, "code": code},
        handler=handler,
        hub=hub,
    )


def set_hub_home_zone(actor: Any, *, hub_code: str, zone_code: str | None) -> dict[str, Any]:
    hub = _hub(hub_code)

    def handler() -> Outcome:
        z = _zone(hub, zone_code) if zone_code else None
        Hub.objects.filter(pk=hub.pk).update(home_zone=z)
        return Outcome("hub", hub.code, {"home_zone": z.code if z else None})

    return run_command(
        actor,
        "planning.set_hub_home_zone",
        permission="planning.manage_zones",
        payload={"hub": hub.code, "zone": zone_code},
        handler=handler,
        hub=hub,
    )


def set_site_zone(
    actor: Any, *, site_code: str, hub_code: str | None, zone_code: str | None, reason: str
) -> dict[str, Any]:
    """Move a home to a zone (None = unknown), with a reason. Only through the guarded database function
    planning_set_site_zone (v2 03B-4, planning/0014): the regional ops lead, settings lock then the hub rows,
    one audit row, refused while it would take an open visit of the home out from under an active pause. The
    home's guard version moves, so a departure authorization issued before the change no longer departs.
    Visits keep the zone they were planned with until they are planned again; a re-plan takes the new zone,
    and authorization and departure are refused until then."""
    zone = _zone(_hub(hub_code or ""), zone_code) if zone_code else None

    def handler() -> Outcome:
        site = Site.objects.filter(code=site_code).first()
        if site is None:
            raise Invalid(f"No home {site_code}")
        version = guarded_function(
            "SELECT planning_set_site_zone(%s, %s, %s, %s, %s)",
            [actor.pk, site.pk, zone.pk if zone else None, reason, current_request_id.get()],
        )
        return Outcome(
            "site",
            site.code,
            {
                "zone": zone.code if zone else None,
                "hub": zone.hub.code if zone else None,
                "site_version": version,
            },
            audited=True,
        )

    return run_command(
        actor,
        "planning.set_site_zone",
        permission="planning.manage_sites",
        payload={"site": site_code, "hub": hub_code, "zone": zone_code, "reason": reason},
        handler=handler,
        hub=zone.hub_id if zone else None,
    )
