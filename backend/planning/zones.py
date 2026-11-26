"""Zones of a hub (v2 03B-1, docs/13_v2_phase03_contract.md 13.2, 13.4 item 3, 13.5).

FROZEN read accessor: ``zone_of(visit) -> Zone | None`` (the visit's destination zone; None = unknown).

Setup commands (run_command, admin or ops lead; the database enforces the rules in platform_core/0006 and
planning/0013):
  * ``create_zone`` / ``retire_zone``: a retired zone keeps its history and cannot be used for new visits;
  * ``set_hub_base_zone``: the zone the hub sits in (trips start and end there);
  * ``set_site_zone``: the zone of a home. A visit takes its home's zone when it is booked or re-planned.
"""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction

from planning.models import Site, Visit
from platform_core.commands import Conflict, Invalid, Outcome, run_command
from platform_core.hubscope import owner_hub
from platform_core.models import Hub, Zone

ZONE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,11}$")


def zone_of(visit: Visit) -> Zone | None:
    """The visit's destination zone (its home's zone at plan time), or None when unknown."""
    if visit.zone_id is None:
        return None
    return Zone.objects.filter(pk=visit.zone_id).first()


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


def set_hub_base_zone(actor: Any, *, hub_code: str, zone_code: str | None) -> dict[str, Any]:
    hub = _hub(hub_code)

    def handler() -> Outcome:
        z = _zone(hub, zone_code) if zone_code else None
        Hub.objects.filter(pk=hub.pk).update(base_zone=z)
        return Outcome("hub", hub.code, {"base_zone": z.code if z else None})

    return run_command(
        actor,
        "planning.set_hub_base_zone",
        permission="planning.manage_zones",
        payload={"hub": hub.code, "zone": zone_code},
        handler=handler,
        hub=hub,
    )


def set_site_zone(
    actor: Any, *, site_code: str, hub_code: str | None, zone_code: str | None
) -> dict[str, Any]:
    """The zone of a home (None = unknown). Visits booked or re-planned afterwards take it."""

    def handler() -> Outcome:
        z = _zone(_hub(hub_code or ""), zone_code) if zone_code else None
        site = Site.objects.select_for_update().filter(code=site_code).first()
        if site is None:
            raise Invalid(f"No home {site_code}")
        site.zone_ref = z
        site.save(update_fields=["zone_ref"])
        return Outcome("site", site.code, {"zone": z.code if z else None, "hub": z.hub.code if z else None})

    return run_command(
        actor,
        "planning.set_site_zone",
        permission="planning.manage_sites",
        payload={"site": site_code, "hub": hub_code, "zone": zone_code},
        handler=handler,
    )
