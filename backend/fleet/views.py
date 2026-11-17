"""Minimal server-rendered fleet interface. Every action posts to the same commands the API uses."""

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import active_roles, has_permission
from fleet import services as s
from fleet.models import Hold, Robot
from platform_core.commands import CommandError


def _banner() -> dict[str, Any]:
    policy = s.current_policy() if Hold.objects.exists() or s.HoldPolicy.objects.exists() else None
    return {
        "demo_mode": s.demo_mode(),
        "policy_draft": policy is not None and s.policy_status(policy) == "draft",
    }


@login_required
def fleet_list(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, "fleet.read"):
        return HttpResponseForbidden("No fleet access")
    rows = []
    for r in Robot.objects.order_by("code"):
        rows.append(
            {
                "robot": r,
                "e": s.evaluate_eligibility(r),
                "open_holds": list(r.holds.filter(released_at__isnull=True)),
            }
        )
    return render(
        request, "fleet/list.html", {"rows": rows, "roles": sorted(active_roles(request.user)), **_banner()}
    )


@login_required
def robot_detail(request: HttpRequest, code: str) -> HttpResponse:
    if not has_permission(request.user, "fleet.read"):
        return HttpResponseForbidden("No fleet access")
    r = get_object_or_404(Robot, code=code)
    holds = []
    for h in r.holds.select_related("policy").order_by("-placed_at"):
        holds.append({"hold": h, "status": s.hold_release_status(h) if h.released_at is None else None})
    pattern_holds = []
    if r.current_manifest is not None:
        for h in Hold.objects.filter(pattern=r.current_manifest.pattern, released_at__isnull=True):
            pattern_holds.append({"hold": h, "status": s.hold_release_status(h)})
    return render(
        request,
        "fleet/detail.html",
        {
            "robot": r,
            "e": s.evaluate_eligibility(r),
            "manifest": r.current_manifest,
            "holds": holds,
            "pattern_holds": pattern_holds,
            "history": list(reversed(s.robot_history(r))),
            "roles": sorted(active_roles(request.user)),
            **_banner(),
        },
    )


def _run(request: HttpRequest, code: str, fn: Any, ok: str, **kwargs: Any) -> HttpResponse:
    try:
        fn(request.user, **kwargs)
        messages.success(request, ok)
    except CommandError as exc:
        messages.error(request, f"Refused: {exc}")
    return redirect("fleet-detail", code=code)


@login_required
@require_POST
def place_hold_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    return _run(
        request,
        code,
        s.place_hold,
        "Hold placed",
        scope="robot",
        target=code,
        source_type=p.get("source_type", "manual"),
        source_ref=p.get("source_ref", ""),
        reason=p.get("reason", ""),
    )


@login_required
@require_POST
def approve_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    return _run(
        request,
        code,
        s.approve_release,
        "Approval recorded",
        hold_code=p["hold"],
        slot=p["slot"],
        role_used=p["role"],
        note=p.get("note", ""),
    )


@login_required
@require_POST
def release_view(request: HttpRequest, code: str) -> HttpResponse:
    return _run(request, code, s.release_hold, "Hold released", hold_code=request.POST["hold"])


@login_required
@require_POST
def check_view(request: HttpRequest, code: str) -> HttpResponse:
    return _run(request, code, s.check_eligibility, "Eligibility check recorded", robot_code=code)
