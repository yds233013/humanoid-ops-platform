"""Server-rendered day/week planner. Every action posts to the same commands the tests exercise."""

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import active_roles, has_permission
from hub import charging
from planning import services as p
from planning.forms import (
    AdultsForm,
    AssessmentForm,
    BookForm,
    CancelForm,
    ConsentForm,
    PlanForm,
    SiteForm,
    WithdrawForm,
)
from planning.models import Reservation, Site, SiteAssessment, Visit
from planning.options import plan_options, suggest
from planning.plain import ALREADY_DONE, plain_blockers, refusal_text, role_words, who_can
from platform_core.commands import CommandError
from platform_core.interaction import consume_form_token
from platform_core.timezones import workspace_zone_name


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
        "plain": plain_blockers(live.blockers) if live else [],
    }


@login_required
def week(request: HttpRequest) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    tz_name = workspace_zone_name()  # the week is laid out in the workspace display zone
    tz = ZoneInfo(tz_name)
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
            "tz": tz_name,
            "roles": sorted(active_roles(request.user)),
            "unplanned": [_card(v) for v in Visit.objects.filter(status="requested").select_related("site")],
        },
    )


@login_required
def visit_detail(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    v = get_object_or_404(Visit.objects.select_related("site"), code=code)
    can_plan = has_permission(request.user, "planning.plan")
    wants_suggestion = request.GET.get("suggest") == "1"
    if wants_suggestion and not can_plan:
        return HttpResponseForbidden(who_can("planning.plan"))
    card = _card(v)
    options = suggestion = None
    if can_plan and v.status != "cancelled":
        options = plan_options(v)  # read only, a fixed number of statements (planning/options.py)
        if wants_suggestion:
            suggestion = suggest(v, options)  # read only: books, reserves and records nothing
            if suggestion.found:
                picks = {
                    "robots": suggestion.robot,
                    "leads": suggestion.lead,
                    "seconds": suggestion.second,
                    "vehicles": suggestion.vehicle,
                }
                for name, pick in picks.items():
                    for o in getattr(options, name):
                        o.selected = pick is not None and o.code == pick.code
    return render(
        request,
        "planning/visit.html",
        {
            **card,
            "revisions": v.revisions.select_related(
                "robot", "lead__user", "second__user", "vehicle"
            ).order_by("-revision_no"),
            "reservations": Reservation.objects.filter(revision__visit=v)
            .select_related("resource", "revision")
            .order_by("released_at", "starts_at"),
            "options": options,
            # v2 02B: the assigned robot's charge forecast (planning information, never a departure decision)
            "charge": charging.visit_forecasts([v.pk]).get(v.pk) if charging.forecast_configured() else None,
            "suggestion": suggestion,
            "site_tz": v.site.timezone,
            "roles": sorted(active_roles(request.user)),
            "can": {
                "plan": can_plan,
                "confirm": has_permission(request.user, "planning.confirm"),
            },
            "who_plan": who_can("planning.plan"),
            "who_confirm": who_can("planning.confirm"),
            "plan_form": PlanForm(),
        },
    )


# ------------------------------------------------------------------ shared plumbing for state-changing forms
def _site_or_404(code: str) -> Site:
    return get_object_or_404(Site, code=code)


def _form_errors(form: Any) -> str:
    parts = []
    for name, errs in form.errors.items():
        label = form.fields[name].label if name in form.fields and form.fields[name].label else None
        parts.append(f"{label + ': ' if label else ''}{' '.join(errs)}")
    return " ".join(parts)


def _run(
    request: HttpRequest,
    fn: Callable[..., Any],
    *,
    permission: str,
    ok: str,
    **kw: Any,
) -> dict[str, Any] | None:
    """The one place a view calls a service command: one-time token first (a replay does nothing and says so),
    then exactly ONE existing command. Returns its result, or None after queueing a plain-words message."""
    if consume_form_token(request) != "ok":
        messages.warning(request, ALREADY_DONE)
        return None
    try:
        result: dict[str, Any] = fn(request.user, **kw)
    except CommandError as exc:
        messages.error(request, refusal_text(exc, permission))
        return None
    messages.success(request, ok)
    return result


def _not_saved(request: HttpRequest, form: Any) -> None:
    messages.error(request, f"Refused (check the form): {_form_errors(form)} Nothing was changed.")


# ------------------------------------------------------------------ customers (sites)
def site_status(site: Site) -> dict[str, Any]:
    """One plain line from the existing checks (current_assessment, consent_status); nothing is re-decided."""
    today = timezone.localdate()
    a = p.current_assessment(site, today)
    level, why = p.consent_status(site, ["presence"])
    base: dict[str, Any] = {"assessment": a, "consent_level": level, "consent_why": why}
    if a is None:
        return {**base, "tone": "warn", "text": "Home needs an assessment", "ready": False}
    if a.decision == "not_eligible":
        return {**base, "tone": "bad", "text": "Home is not eligible for visits", "ready": False}
    if level == "ok":
        note = "Some tasks are excluded at this home." if a.excluded_tasks else ""
        return {**base, "tone": "ok", "text": "Home ready", "detail": note, "ready": True}
    m = re.match(r"(\d+) of (\d+) adults", why)
    if m:
        n = int(m.group(2)) - int(m.group(1))
        text = f"Consent missing for {n} adult{'s' if n != 1 else ''}"
    elif site.expected_adults is None:
        text = "Number of adults not set"
    else:
        text = "Consent missing"
    return {**base, "tone": "warn", "text": text, "detail": why, "ready": False}


@login_required
def sites(request: HttpRequest) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    q = request.GET.get("q", "").strip()
    qs = Site.objects.order_by("code")
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(zone__icontains=q) | Q(crm_ref__icontains=q))
    rows = [{"site": s, "status": site_status(s)} for s in qs]
    return render(
        request,
        "planning/sites.html",
        {
            "rows": rows,
            "q": q,
            "total": Site.objects.count(),
            "can_add": has_permission(request.user, "planning.manage_sites"),
            "who_add": who_can("planning.manage_sites"),
            "roles": sorted(active_roles(request.user)),
        },
    )


@login_required
def site_new(request: HttpRequest) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    allowed = has_permission(request.user, "planning.manage_sites")
    form = SiteForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            d = form.cleaned_data
            if (
                result := _run(
                    request,
                    p.create_site,
                    permission="planning.manage_sites",
                    ok=f"Home {d['code']} added. Next: record an assessment and consent.",
                    code=d["code"],
                    zone=d["zone"],
                    tz=d["tz"],
                    crm_ref=d["crm_ref"],
                    expected_adults=d["expected_adults"],
                )
            ) is not None:
                return redirect("planning-site", code=result["entity_id"])
            form = SiteForm(request.POST)  # keep what was typed; the page shows a fresh token
        else:
            _not_saved(request, form)
    return render(
        request,
        "planning/site_new.html",
        {"form": form, "can_add": allowed, "who_add": who_can("planning.manage_sites")},
    )


@login_required
def site_detail(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    site = _site_or_404(code)
    status = site_status(site)
    superseded = set(
        SiteAssessment.objects.filter(site=site, supersedes__isnull=False).values_list(
            "supersedes_id", flat=True
        )
    )
    assessments = []
    for a in site.assessments.select_related("assessed_by").order_by("-assessed_at"):
        approval = getattr(a, "approval", None)
        assessments.append(
            {
                "a": a,
                "approval": approval,
                "replaced": a.pk in superseded,
                "current": status["assessment"] is not None and status["assessment"].pk == a.pk,
                "excluded": ", ".join(a.excluded_tasks),
            }
        )
    can = {
        "manage": has_permission(request.user, "planning.manage_sites"),
        "assess": has_permission(request.user, "planning.assess"),
        "approve": has_permission(request.user, "planning.approve_assessment"),
        "consent": has_permission(request.user, "planning.consent"),
        "plan": has_permission(request.user, "planning.plan"),
    }
    return render(
        request,
        "planning/site.html",
        {
            "site": site,
            "status": status,
            "assessments": assessments,
            "consents": site.consents.order_by("participant_ref", "-verified_at"),
            "can": can,
            "who": {
                k: who_can(v)
                for k, v in {
                    "manage": "planning.manage_sites",
                    "assess": "planning.assess",
                    "approve": "planning.approve_assessment",
                    "consent": "planning.consent",
                    "plan": "planning.plan",
                }.items()
            },
            "approve_roles": role_words("planning.approve_assessment"),
            "adults_form": AdultsForm(initial={"expected_adults": site.expected_adults}),
            "assess_form": AssessmentForm(site=site),
            "consent_form": ConsentForm(),
            "withdraw_form": WithdrawForm(),
            "visits": site.visits.select_related("current_revision").order_by("-created_at")[:10],
        },
    )


def _back_to_site(code: str) -> HttpResponse:
    return redirect("planning-site", code=code)


@login_required
@require_POST
def site_adults(request: HttpRequest, code: str) -> HttpResponse:
    form = AdultsForm(request.POST)
    if not form.is_valid():
        _not_saved(request, form)
        return _back_to_site(code)
    n = form.cleaned_data["expected_adults"]
    _run(
        request,
        p.set_expected_adults,
        permission="planning.manage_sites",
        ok="Number of adults saved." if n else "Number of adults set to unknown.",
        site_code=code,
        expected_adults=n,
    )
    return _back_to_site(code)


@login_required
@require_POST
def assessment_new(request: HttpRequest, code: str) -> HttpResponse:
    site = _site_or_404(code)
    form = AssessmentForm(request.POST, site=site)
    if not form.is_valid():
        _not_saved(request, form)
        return _back_to_site(code)
    d = form.cleaned_data
    _run(
        request,
        p.record_assessment,
        permission="planning.assess",
        ok="Assessment recorded. It counts only after someone else approves it.",
        site_code=code,
        decision=d["decision"],
        valid_until=d["valid_until"],
        restrictions="Some tasks are not allowed at this home." if d["excluded_tasks"] else "",
        excluded_tasks=d["excluded_tasks"],
        supersedes_id=int(d["supersedes"]) if d["supersedes"] else None,
    )
    return _back_to_site(code)


@login_required
@require_POST
def assessment_approve(request: HttpRequest, code: str, assessment_id: int) -> HttpResponse:
    a = get_object_or_404(SiteAssessment, pk=assessment_id, site__code=code)
    _run(
        request,
        p.approve_assessment,
        permission="planning.approve_assessment",
        ok="Assessment approved.",
        assessment_id=a.pk,
    )
    return _back_to_site(code)


@login_required
@require_POST
def consent_new(request: HttpRequest, code: str) -> HttpResponse:
    _site_or_404(code)
    form = ConsentForm(request.POST)
    if not form.is_valid():
        _not_saved(request, form)
        return _back_to_site(code)
    d = form.cleaned_data
    from fleet import services as fleet_services

    form_version = fleet_services.setting("consent_form_version")
    if not form_version:
        messages.error(
            request,
            "Refused (not possible right now): No current consent form version is set up, so consent cannot "
            "be recorded. Ask whoever manages the workspace settings to set it. Nothing was changed.",
        )
        return _back_to_site(code)
    _run(
        request,
        p.record_consent,
        permission="planning.consent",
        ok=f"Consent recorded for {d['participant_ref']}.",
        site_code=code,
        participant_ref=d["participant_ref"],
        form_version=form_version,
        scope=d["scope"],
        evidence_ref=d["evidence_ref"],
    )
    return _back_to_site(code)


@login_required
@require_POST
def consent_withdraw(request: HttpRequest, code: str, consent_id: int) -> HttpResponse:
    c = get_object_or_404(_site_or_404(code).consents, pk=consent_id)
    form = WithdrawForm(request.POST)
    if not form.is_valid():
        _not_saved(request, form)
        return _back_to_site(code)
    _run(
        request,
        p.withdraw_consent,
        permission="planning.consent",
        ok=f"Consent for {c.participant_ref} withdrawn. It stops counting immediately.",
        consent_id=c.pk,
        reason=form.cleaned_data["reason"],
    )
    return _back_to_site(code)


# ------------------------------------------------------------------ book a visit
@login_required
def book_visit(request: HttpRequest, code: str | None = None) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    site = _site_or_404(code) if code else None
    chosen = site or (Site.objects.filter(code=request.GET.get("site", "")).first())
    initial = {"site": chosen.code} if chosen else {}
    form = BookForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            d = form.cleaned_data
            target = _site_or_404(d["site"])
            try:
                lo, hi = form.local_times()
                start = p.local_to_utc(lo, target.timezone)
                end = p.local_to_utc(hi, target.timezone)
            except CommandError as exc:
                messages.error(request, refusal_text(exc, "planning.plan"))
            else:
                if (
                    result := _run(
                        request,
                        p.request_visit,
                        permission="planning.plan",
                        ok="Visit requested. Next: choose a robot, crew and vehicle.",
                        site_code=target.code,
                        on_site_start=start,
                        on_site_end=end,
                        travel_out_min=d["travel_out_min"],
                        travel_back_min=d["travel_back_min"],
                        tasks=d["tasks"],
                        required_scopes=d["scopes"],
                    )
                ) is not None:
                    return redirect("planning-visit", code=result["entity_id"])
            form = BookForm(request.POST)
        else:
            _not_saved(request, form)
    shown = chosen
    if request.method == "POST" and form.data.get("site"):
        shown = Site.objects.filter(code=form.data["site"]).first() or chosen
    return render(
        request,
        "planning/book.html",
        {
            "form": form,
            "site": shown,
            "status": site_status(shown) if shown else None,
            "can_plan": has_permission(request.user, "planning.plan"),
            "who_plan": who_can("planning.plan"),
            "site_tz": shown.timezone if shown else None,
        },
    )


def _redirect_visit(code: str) -> HttpResponse:
    return redirect("planning-visit", code=code)


@login_required
@require_POST
def plan_view(request: HttpRequest, code: str) -> HttpResponse:
    v = get_object_or_404(Visit.objects.select_related("site"), code=code)
    form = PlanForm(request.POST)
    if not form.is_valid():
        _not_saved(request, form)
        return _redirect_visit(code)
    d = form.cleaned_data
    kw: dict[str, Any] = {}
    if d["start"] and d["end"]:
        try:
            kw["on_site_start"] = p.local_to_utc(d["start"].replace(tzinfo=None), v.site.timezone)
            kw["on_site_end"] = p.local_to_utc(d["end"].replace(tzinfo=None), v.site.timezone)
        except CommandError as exc:
            messages.error(request, refusal_text(exc, "planning.plan"))
            return _redirect_visit(code)
    _run(
        request,
        p.plan_visit,
        permission="planning.plan",
        ok="Plan saved. The result of the check is shown below.",
        visit_code=code,
        robot_code=d["robot"] or None,
        lead_code=d["lead"] or None,
        second_code=d["second"] or None,
        vehicle_code=d["vehicle"] or None,
        reason=d["reason"] or "replanned",
        **kw,
    )
    return _redirect_visit(code)


@login_required
@require_POST
def confirm_view(request: HttpRequest, code: str) -> HttpResponse:
    get_object_or_404(Visit, code=code)
    _run(
        request,
        p.confirm_visit,
        permission="planning.confirm",
        ok="Visit confirmed. This is planning only: it is not approval to leave.",
        visit_code=code,
    )
    return _redirect_visit(code)


@login_required
def cancel_view(request: HttpRequest, code: str) -> HttpResponse:
    """GET shows a confirmation page; POST (with the box ticked, a reason and a fresh token) cancels."""
    v = get_object_or_404(Visit.objects.select_related("site"), code=code)
    if request.method == "POST":
        form = CancelForm(request.POST)
        if not form.is_valid():
            _not_saved(request, form)
            return redirect("planning-visit-cancel", code=code)
        _run(
            request,
            p.cancel_visit,
            permission="planning.plan",
            ok=f"Visit {code} cancelled. Its reservations are released.",
            visit_code=code,
            reason=form.cleaned_data["reason"],
        )
        return _redirect_visit(code)
    if (deny := _guard(request)) is not None:
        return deny
    return render(
        request,
        "planning/cancel.html",
        {
            "visit": v,
            "form": CancelForm(),
            "can_plan": has_permission(request.user, "planning.plan"),
            "who_plan": who_can("planning.plan"),
        },
    )
