"""Workspace setup commands (v2 01C.1): company settings and hubs.

These are the approved exceptions to "views call existing guarded services": the workspace row and hubs are
new setup data with no earlier command. They still go through ``run_command`` (permission, audit row, one
transaction) and record facts only; nothing here approves, qualifies or sets a policy (contract section 4).
"""

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction

from platform_core.commands import Conflict, Invalid, Outcome, run_command
from platform_core.models import Hub, Workspace

HUB_CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,11}$")


def validate_time_zone(name: str) -> str:
    """The IANA name, or raise Invalid. ZoneInfo rejects paths and unknown names."""
    name = name.strip()
    if not name:
        raise Invalid("Choose a time zone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise Invalid(f"{name} is not a time zone name (use a name like America/Los_Angeles)") from None
    return name


def update_workspace(actor: Any, *, name: str, region: str, time_zone: str) -> dict[str, Any]:
    name, region, time_zone = name.strip(), region.strip(), time_zone.strip()
    payload = {"name": name, "region": region, "time_zone": time_zone}

    def handler() -> Outcome:
        if not name:
            raise Invalid("The company name cannot be empty")
        if len(name) > 120 or len(region) > 80:
            raise Invalid("The company name or region is too long")
        tz = validate_time_zone(time_zone)
        Workspace.ensure()
        ws = Workspace.objects.select_for_update().get(pk=1)
        ws.name, ws.region, ws.time_zone = name, region, tz
        ws.save(update_fields=["name", "region", "time_zone"])
        return Outcome("workspace", "1", {"name": name, "region": region, "time_zone": tz})

    return run_command(
        actor,
        "platform_core.update_workspace",
        permission="platform_core.manage_workspace",
        payload=payload,
        handler=handler,
    )


def create_hub(actor: Any, *, code: str, name: str, area: str, time_zone: str) -> dict[str, Any]:
    code, name, area, time_zone = code.strip().upper(), name.strip(), area.strip(), time_zone.strip()
    payload = {"code": code, "name": name, "area": area, "time_zone": time_zone}

    def handler() -> Outcome:
        if not HUB_CODE.match(code):
            raise Invalid("The hub code uses capital letters, digits and dashes, up to 12 characters")
        if not name or len(name) > 80 or len(area) > 80:
            raise Invalid("Give the hub a name (up to 80 characters)")
        tz = validate_time_zone(time_zone)
        if Hub.objects.filter(code=code).exists():
            raise Conflict(f"A hub with code {code} already exists")
        with transaction.atomic():
            hub = Hub.objects.create(code=code, name=name, area=area, time_zone=tz, created_by=actor)
        return Outcome("hub", hub.code, {"name": name, "time_zone": tz})

    return run_command(
        actor,
        "platform_core.create_hub",
        permission="platform_core.manage_hubs",
        payload=payload,
        handler=handler,
    )
