"""S5 Zones and travel times (v2 03B-1, docs/14_v2_03b_design_spec.md S5; contract 13.4 item 3).

Plain but complete setup pages for an admin or the ops lead (wave 2 polishes them):
  * ``/settings/hubs/<hub>/zones/``: the hub's zones (name first, code, active or retired, the hub's own
    zone), with add, retire (confirm page) and "the hub's own zone";
  * ``/settings/travel-times/``: a zone x zone grid of the minutes in force (from row, to column: ordered
    pairs). A missing cell reads "Unknown" in the at-risk style, never blank; every value shows its source
    and version;
  * ``/settings/travel-times/<from>/<to>/``: the pair's history and a form that adds the next version
    (effective now or later, never earlier).
Each change is ONE command (planning.zones / planning.travel); the database enforces the rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

from planning import travel, zones
from platform_core.hubscope import hub_scope
from platform_core.interaction import ConfirmForm, UIForm, command_page, denied, holds_any
from platform_core.models import Hub, Zone
from platform_core.timezones import workspace_zone_name

SETUP_PERMS = ("planning.manage_zones", "planning.set_travel_time")


class ZoneForm(UIForm):
    name = forms.CharField(max_length=80, label="Name", help_text="The area as people say it, e.g. Downtown.")
    code = forms.CharField(max_length=12, label="Code", help_text="Capital letters, digits and dashes.")


class BaseZoneForm(UIForm):
    zone = forms.ChoiceField(label="The hub's own zone", required=False)

    def __init__(self, *args: Any, choices: list[tuple[str, str]] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["zone"].choices = [("", "Unknown (no trips can be timed)")] + (choices or [])  # type: ignore[attr-defined]
        self.fields[
            "zone"
        ].help_text = "Trips start and end here: travel out and back are timed from this zone."


class RetireForm(ConfirmForm):
    pass


class TravelForm(UIForm):
    minutes = forms.IntegerField(min_value=1, max_value=1440, label="Minutes")
    source = forms.ChoiceField(
        choices=[("company", "Company value"), ("demo", "Demo value")],
        label="Where the value comes from",
        help_text="A demo value is shown as demo data everywhere.",
    )
    effective_from = forms.DateTimeField(
        required=False,
        label="Takes effect",
        help_text="Leave empty for now. Never earlier than now: past plans keep the value they used.",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )


def _hub(code: str) -> Hub:
    hub = Hub.objects.filter(code=code.upper()).first()
    if hub is None:
        raise Http404("No such hub")
    return hub


@login_required
def zone_list(request: HttpRequest, hub_code: str) -> HttpResponse:
    if not holds_any(request, SETUP_PERMS):
        return denied(request, SETUP_PERMS)
    hub = _hub(hub_code)
    return render(
        request,
        "planning/zones.html",
        {
            "hub": hub,
            "zones": list(Zone.objects.filter(hub=hub).order_by("-active", "name")),
            "page_title": f"Zones of {hub.name}",
        },
    )


@login_required
def zone_new(request: HttpRequest, hub_code: str) -> HttpResponse:
    hub = _hub(hub_code)
    return command_page(
        request,
        permission="planning.manage_zones",
        form_class=ZoneForm,
        template="planning/zone_form.html",
        call=lambda actor, d: zones.create_zone(actor, hub_code=hub.code, code=d["code"], name=d["name"]),
        ok=lambda r: f"Zone {r['name']} ({r['code']}) added to {hub.name}.",
        success_url=f"/settings/hubs/{hub.code}/zones/",
        context={
            "hub": hub,
            "title": f"Add a zone to {hub.name}",
            "submit": "Add zone",
            "zones_url": f"/settings/hubs/{hub.code}/zones/",
        },
    )


@login_required
def zone_base(request: HttpRequest, hub_code: str) -> HttpResponse:
    hub = _hub(hub_code)
    choices = [
        (z.code, f"{z.name} ({z.code})") for z in Zone.objects.filter(hub=hub, active=True).order_by("name")
    ]
    current = (
        Zone.objects.filter(pk=hub.base_zone_id).values_list("code", flat=True).first() or ""
        if hub.base_zone_id is not None
        else ""
    )
    return command_page(
        request,
        permission="planning.manage_zones",
        form_class=BaseZoneForm,
        template="planning/zone_form.html",
        call=lambda actor, d: zones.set_hub_base_zone(actor, hub_code=hub.code, zone_code=d["zone"] or None),
        ok=f"{hub.name}'s own zone is saved.",
        success_url=f"/settings/hubs/{hub.code}/zones/",
        initial={"zone": current},
        form_kwargs={"choices": choices},
        context={
            "hub": hub,
            "title": f"{hub.name}: the hub's own zone",
            "submit": "Save",
            "zones_url": f"/settings/hubs/{hub.code}/zones/",
        },
    )


@login_required
def zone_retire(request: HttpRequest, hub_code: str, zone_code: str) -> HttpResponse:
    hub = _hub(hub_code)
    zone = Zone.objects.filter(hub=hub, code=zone_code.upper()).first()
    if zone is None:
        raise Http404("No such zone")
    return command_page(
        request,
        permission="planning.manage_zones",
        form_class=RetireForm,
        template="planning/zone_retire.html",
        call=lambda actor, d: zones.retire_zone(actor, hub_code=hub.code, code=zone.code),
        ok=f"Zone {zone.name} ({zone.code}) is retired. Its history is kept.",
        success_url=f"/settings/hubs/{hub.code}/zones/",
        context={
            "hub": hub,
            "zone": zone,
            "page_title": f"Retire zone {zone.name}",
            "zones_url": f"/settings/hubs/{hub.code}/zones/",
        },
    )


# ------------------------------------------------------------------ travel matrix
@login_required
def travel_matrix(request: HttpRequest) -> HttpResponse:
    if not holds_any(request, SETUP_PERMS):
        return denied(request, SETUP_PERMS)
    scope = hub_scope(request)
    zs = list(Zone.objects.filter(active=True).select_related("hub").order_by("hub__code", "name"))
    if scope.selected is not None:
        zs = [z for z in zs if z.hub_id == scope.selected.pk]
    cells = travel.matrix([z.pk for z in zs])
    grid = [{"from": f, "cells": [{"to": t, "cell": cells.get((f.pk, t.pk))} for t in zs]} for f in zs]
    unknown = sum(1 for c in cells.values() if not c.current.known)
    return render(
        request,
        "planning/travel.html",
        {"zones": zs, "grid": grid, "unknown": unknown, "scope": scope, "unknown_text": travel.UNKNOWN_TEXT},
    )


@login_required
def travel_pair(request: HttpRequest, from_id: int, to_id: int) -> HttpResponse:
    if not holds_any(request, SETUP_PERMS):
        return denied(request, SETUP_PERMS)
    a = Zone.objects.select_related("hub").filter(pk=from_id).first()
    b = Zone.objects.select_related("hub").filter(pk=to_id).first()
    if a is None or b is None:
        raise Http404("No such zone")
    tz = workspace_zone_name()

    def call(actor: Any, d: dict[str, Any]) -> dict[str, Any]:
        eff = d.get("effective_from")
        if isinstance(eff, datetime) and eff.tzinfo is None:
            eff = eff.replace(tzinfo=ZoneInfo(tz))
        return travel.set_travel_time(
            actor,
            from_zone_id=a.pk,
            to_zone_id=b.pk,
            minutes=d["minutes"],
            source=d["source"],
            effective_from=eff,
        )

    return command_page(
        request,
        permission="planning.set_travel_time",
        form_class=TravelForm,
        template="planning/travel_pair.html",
        call=call,
        ok=lambda r: f"Version {r['version']} saved: {r['minutes']} min from {a.name} to {b.name}.",
        success_url=f"/settings/travel-times/{a.pk}/{b.pk}/",
        context={
            "a": a,
            "b": b,
            "page_title": f"From {a.name} to {b.name}",
            "history": travel.history(a, b),
            "reverse_url": f"/settings/travel-times/{b.pk}/{a.pk}/",
            "tz": tz,
            "now_value": travel.minutes(a, b, datetime.now(ZoneInfo(tz))),
        },
    )
