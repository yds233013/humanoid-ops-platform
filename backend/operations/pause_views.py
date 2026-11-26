"""S4 Regional pause pages (v2 03B-1, docs/14_v2_03b_design_spec.md S4; contract I-9, 13.4 items 1 and 2).

Plain but complete (wave 2 polishes them):
  * ``/planning/pause/``: what is paused now (hub names first, local times with the zone), where the viewer
    can pause, and the recent history;
  * ``/planning/pause/<hub>/`` and ``/planning/pause/<hub>/<zone>/``: the confirm page. It first shows the
    affected visits ("This blocks authorization and departure for N visits"), then asks for a reason and the
    confirmation tick;
  * ``/planning/pause/lift/<id>/``: lift, with a reason and the confirmation tick.
Every change is ONE command (operations.pauses.pause_region / lift_pause); the database re-checks the actor,
the hub and the lock order. Nothing here recalls a robot or approves anything.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.permissions import has_permission
from operations import pauses
from operations.models import RegionPause
from planning.board import pause_banner
from platform_core.hubscope import hub_scope, other_hub_refusal
from platform_core.interaction import ConfirmForm, command_page, denied
from platform_core.models import Hub, Zone

VIEW_PERMS = ("operations.pause", "operations.lift_pause", "operations.read")


class PauseForm(ConfirmForm):
    reason = forms.CharField(
        max_length=200,
        label="Reason",
        help_text="What is happening, for example weather or an incident nearby. Everyone sees it.",
    )
    field_order = ["reason", "confirm"]


class LiftForm(ConfirmForm):
    reason = forms.CharField(max_length=200, label="Reason", help_text="Why it is safe to leave again.")
    field_order = ["reason", "confirm"]


class _Shown:
    """An active pause as the page shows it: the PauseInfo, the S1 banner and the visits it holds."""

    def __init__(self, info: Any, banner: str, affected: int) -> None:
        self.info, self.banner, self.affected = info, banner, affected

    def __getattr__(self, name: str) -> Any:
        return getattr(self.info, name)


def _may_view(request: HttpRequest) -> bool:
    return any(has_permission(request.user, p) for p in VIEW_PERMS)


@login_required
def pause_page(request: HttpRequest) -> HttpResponse:
    if not _may_view(request):
        return denied(request, VIEW_PERMS)
    scope = hub_scope(request)
    ids = scope.ids
    hubs = [h for h in scope.hubs if ids is None or h.pk in ids]
    active = pauses.board_summary(sorted(h.pk for h in hubs))
    zones_by_hub: dict[int, list[Zone]] = {}
    for z in Zone.objects.filter(hub_id__in=[h.pk for h in hubs], active=True).order_by("name"):
        zones_by_hub.setdefault(z.hub_id, []).append(z)
    rows: list[dict[str, Any]] = []
    for h in hubs:
        mine = active.get(h.pk, [])
        paused_zones = {p.zone_id for p in mine if p.zone_id is not None}
        for z in zones_by_hub.get(h.pk, []):
            z.paused = z.pk in paused_zones  # type: ignore[attr-defined]
        rows.append(
            {
                "hub": h,
                "zones": zones_by_hub.get(h.pk, []),
                # v2 03B-3 (S4, S1 wording): each active pause with its banner and how many visits it holds
                "active": [
                    _Shown(p, pause_banner(h.name, p), len(pauses.affected_visits(h.pk, p.zone_id)))
                    for p in mine
                ],
                "hub_paused": any(p.zone_id is None for p in mine),
                "can_pause": has_permission(request.user, "operations.pause", hub=h.pk),
            }
        )
    return render(
        request,
        "operations/pause.html",
        {
            "rows": rows,
            "scope": scope,
            "any_active": any(r["active"] for r in rows),
            "can_lift": has_permission(request.user, "operations.lift_pause"),
            "history": pauses.history(sorted(h.pk for h in hubs)),
            "paused_text": pauses.PAUSED_TEXT,
        },
    )


@login_required
def pause_confirm(request: HttpRequest, hub_code: str, zone_code: str | None = None) -> HttpResponse:
    hub = Hub.objects.filter(code=hub_code.upper()).first()
    if hub is None:
        raise Http404("No such hub")
    if (refusal := other_hub_refusal(request, hub.pk)) is not None:
        return refusal
    zone = None
    if zone_code:
        zone = Zone.objects.filter(hub=hub, code=zone_code.upper()).first()
        if zone is None:
            raise Http404("No such zone")
    affected = pauses.affected_visits(hub.pk, zone.pk if zone else None)
    today = timezone.localdate()
    today_count = sum(1 for a in affected if timezone.localtime(a.start).date() == today)
    where = (
        f"zone {zone.name} ({zone.code}) of {hub.name} ({hub.code})" if zone else f"{hub.name} ({hub.code})"
    )
    return command_page(
        request,
        permission="operations.pause",
        form_class=PauseForm,
        template="operations/pause_confirm.html",
        call=lambda actor, d: pauses.pause_region(
            actor, hub_code=hub.code, zone_code=zone.code if zone else None, reason=d["reason"]
        ),
        ok=f"Departures for {where} are paused. Visits can still be planned; none can be authorized to leave "
        "until the pause is lifted.",
        success_url="/planning/pause/",
        context={
            "hub": hub,
            "zone": zone,
            "where": where,
            "affected": affected,
            "today_count": today_count,
            "paused_text": pauses.PAUSED_TEXT,
        },
    )


@login_required
def lift_confirm(request: HttpRequest, pause_id: int) -> HttpResponse:
    p = RegionPause.objects.filter(pk=pause_id, kind="pause").select_related("hub", "zone").first()
    if p is None:
        raise Http404("No such pause")
    if (refusal := other_hub_refusal(request, p.hub_id)) is not None:
        return refusal
    where = (
        f"zone {p.zone.name} ({p.zone.code}) of {p.hub.name} ({p.hub.code})"
        if p.zone
        else f"{p.hub.name} ({p.hub.code})"
    )
    return command_page(
        request,
        permission="operations.lift_pause",
        form_class=LiftForm,
        template="operations/pause_lift.html",
        call=lambda actor, d: pauses.lift_pause(actor, pause_id=p.pk, reason=d["reason"]),
        ok=f"The pause of {where} is lifted. Each visit still needs its own departure authorization.",
        success_url="/planning/pause/",
        context={
            "pause": p,
            "where": where,
            "still_active": not p.lifts.exists(),
            "affected": len(pauses.affected_visits(p.hub_id, p.zone_id)),
        },
    )
