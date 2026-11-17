"""Server-rendered day/week planner. Every action posts to the same commands the tests exercise."""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import active_roles, has_permission
from fleet.models import Robot
from planning import services as p
from planning.models import Person, Reservation, Site, Vehicle, Visit
from platform_core.commands import CommandError

PLANNER_TZ = "America/Los_Angeles"  # display default; each visit also shows its site's own timezone


def _guard(request: HttpRequest) -> HttpResponse | None:
    if not has_permission(request.user, "planning.read"):
        return HttpResponseForbidden("No planning access")
    return None


def _card(v: Visit) -> dict[str, Any]:
    r = v.current_revision
    assert r is not None
    live = p.live_feasibility(v) if v.status != "cancelled" else None
    tz = ZoneInfo(v.site.timezone)
    return {
        "visit": v,
        "rev": r,
        "start": r.on_site_start.astimezone(tz),
        "end": r.on_site_end.astimezone(tz),
        "live": live,
        "label": p.LABELS[live.feasibility] if live else "Cancelled",
        "state": live.feasibility if live else "cancelled",
    }


@login_required
def week(request: HttpRequest) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    tz = ZoneInfo(PLANNER_TZ)
    try:
        anchor = date.fromisoformat(request.GET["week"]) if "week" in request.GET else timezone.localdate()
    except ValueError:
        anchor = timezone.localdate()
    monday = anchor - timedelta(days=anchor.weekday())
    lo = datetime.combine(monday, datetime.min.time(), tz)
    hi = lo + timedelta(days=7)
    days: list[dict[str, Any]] = [{"date": monday + timedelta(days=i), "cards": []} for i in range(7)]
    visits = (
        Visit.objects.select_related("site", "current_revision")
        .filter(current_revision__on_site_start__lt=hi, current_revision__on_site_end__gt=lo)
        .order_by("current_revision__on_site_start")
    )
    for v in visits:
        c = _card(v)
        idx = (c["start"].astimezone(tz).date() - monday).days
        if 0 <= idx < 7:
            days[idx]["cards"].append(c)
    return render(
        request,
        "planning/week.html",
        {
            "days": days,
            "monday": monday,
            "prev": monday - timedelta(days=7),
            "next": monday + timedelta(days=7),
            "tz": PLANNER_TZ,
            "roles": sorted(active_roles(request.user)),
            "demo_mode": True,
            "unplanned": [_card(v) for v in Visit.objects.filter(status="requested").select_related("site")],
        },
    )


@login_required
def visit_detail(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    v = get_object_or_404(Visit.objects.select_related("site"), code=code)
    card = _card(v)
    return render(
        request,
        "planning/visit.html",
        {
            **card,
            "revisions": v.revisions.order_by("-revision_no"),
            "reservations": Reservation.objects.filter(revision__visit=v)
            .select_related("resource", "revision")
            .order_by("released_at", "starts_at"),
            "robots": Robot.objects.filter(lifecycle="in_service").order_by("code"),
            "persons": Person.objects.order_by("code"),
            "vehicles": Vehicle.objects.order_by("code"),
            "site_tz": v.site.timezone,
            "roles": sorted(active_roles(request.user)),
            "demo_mode": True,
        },
    )


@login_required
def sites(request: HttpRequest) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    rows = []
    for s in Site.objects.order_by("code"):
        a = p.current_assessment(s, timezone.localdate())
        level, why = p.consent_status(s, ["presence"])
        rows.append(
            {
                "site": s,
                "assessment": a,
                "consent_level": level,
                "consent": why,
                "consents": s.consents.order_by("participant_ref", "-verified_at"),
            }
        )
    return render(
        request,
        "planning/sites.html",
        {"rows": rows, "roles": sorted(active_roles(request.user)), "demo_mode": True},
    )


def _do(request: HttpRequest, code: str, fn: Any, ok: str, **kw: Any) -> HttpResponse:
    try:
        fn(request.user, **kw)
        messages.success(request, ok)
    except CommandError as exc:
        messages.error(request, f"Refused: {exc}")
    return redirect("planning-visit", code=code)


@login_required
@require_POST
def plan_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    v = get_object_or_404(Visit.objects.select_related("site"), code=code)
    kw: dict[str, Any] = {}
    if f.get("start") and f.get("end"):
        try:
            kw["on_site_start"] = p.local_to_utc(datetime.fromisoformat(f["start"]), v.site.timezone)
            kw["on_site_end"] = p.local_to_utc(datetime.fromisoformat(f["end"]), v.site.timezone)
        except CommandError as exc:
            messages.error(request, f"Refused: {exc}")
            return redirect("planning-visit", code=code)
    return _do(
        request,
        code,
        p.plan_visit,
        "Plan saved",
        visit_code=code,
        robot_code=f.get("robot") or None,
        lead_code=f.get("lead") or None,
        second_code=f.get("second") or None,
        vehicle_code=f.get("vehicle") or None,
        reason=f.get("reason") or "replanned",
        **kw,
    )


@login_required
@require_POST
def confirm_view(request: HttpRequest, code: str) -> HttpResponse:
    return _do(
        request,
        code,
        p.confirm_visit,
        "Visit confirmed (planning only - not cleared to depart)",
        visit_code=code,
    )


@login_required
@require_POST
def cancel_view(request: HttpRequest, code: str) -> HttpResponse:
    return _do(
        request,
        code,
        p.cancel_visit,
        "Visit cancelled",
        visit_code=code,
        reason=request.POST.get("reason", ""),
    )
