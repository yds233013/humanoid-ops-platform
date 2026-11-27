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
from accounts.roles import PERMISSIONS
from fleet.models import Robot
from hub import charging
from planning import services as p
from planning import zones
from planning.board import home_name, legacy_travel_text, person_name, travel_text
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
from planning.options import plan_options, restrict_to_hub, suggest
from planning.plain import ALREADY_DONE, plain_blocker, plain_blockers, refusal_text, role_words, who_can
from platform_core.commands import CommandError, PermissionDenied
from platform_core.hubscope import HubScope, hub_scope, other_hub_refusal
from platform_core.interaction import consume_form_token
from platform_core.templatetags.ui import localize_utc_text
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


def _board_view() -> Callable[..., Any]:
    from operations.views import ops_view  # noqa: PLC0415 - the operations snapshot decorator (GET, one snapshot)

    return ops_view("planning.read")


@_board_view()
def board(request: HttpRequest, at: datetime) -> HttpResponse:
    """The regional dispatch board (v2 03B-2, spec S1): decisions first, then one section per hub. Read only,
    built inside one snapshot with a fixed number of statements (planning/board.py)."""
    from planning import board as b  # noqa: PLC0415

    scope = hub_scope(request)  # the viewer's hubs; ?hub=CODE narrows
    day, day_key = b.parse_day(request.GET.get("day"), at)
    data = b.build_board(at, scope, day, day_key)
    show_all = request.GET.get("attention") == "all"
    today = at.astimezone(ZoneInfo(workspace_zone_name())).date()
    return render(
        request,
        "planning/board.html",
        {
            "b": data,
            "scope": scope,
            "attention": data.attention if show_all else data.attention[: b.ATTENTION_LIMIT],
            "cards": data.cards if show_all else data.cards[: b.ATTENTION_LIMIT],
            "show_all": show_all,
            "day_label": _day_label(day_key, day),
            "day_iso": day.isoformat(),
            "today_iso": today.isoformat(),
            "scope_label": _scope_label(scope),
            "q": _board_query(scope, day_key),
            "q_hub": f"hub={scope.code}" if scope.code else "",
            "roles": sorted(scope.roles),
            "can_book": bool(scope.roles & PERMISSIONS["planning.plan"]),
        },
    )


def _day_label(day_key: str, day: date) -> str:
    if day_key in ("today", "tomorrow"):
        return day_key.capitalize()
    return day.strftime("%a %-d %b")


def _scope_label(scope: HubScope) -> str:
    """Counts are always labelled with what they cover (EM condition: global counts say "all hubs")."""
    if scope.selected is not None:
        return scope.selected.name
    if scope.regional:
        return "all hubs"
    return "your hubs" if len(scope.hubs) != 1 else scope.hubs[0].name


def _board_query(scope: HubScope, day_key: str) -> str:
    parts = [f"day={day_key}"] if day_key != "today" else []
    if scope.code:
        parts.append(f"hub={scope.code}")
    return "&".join(parts)


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
    scope = hub_scope(request)  # v2 03A-1: visits dispatched from the viewer's hubs; ?hub=CODE narrows
    visits = (
        scope.filter(Visit.objects)
        .select_related("site", "current_revision")
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
            "unplanned": [
                _card(v)
                for v in scope.filter(Visit.objects).filter(status="requested").select_related("site")
            ],
            "scope": scope,
        },
    )


@login_required
def visit_detail(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    v = get_object_or_404(Visit.objects.select_related("site", "site__zone_ref", "hub", "zone"), code=code)
    if (deny := other_hub_refusal(request, v.hub_id)) is not None:  # v2 03A-3: another hub's visit
        return deny
    can_plan = has_permission(request.user, "planning.plan")
    wants_suggestion = request.GET.get("suggest") == "1"
    if wants_suggestion and not can_plan:
        return HttpResponseForbidden(who_can("planning.plan"))
    card = _card(v)
    options = suggestion = review = next_slots = None
    wants_review = request.GET.get("review") == "1" or any(
        k in request.GET for k in ("robot", "lead", "second", "vehicle")
    )
    departed = v.status != "cancelled" and v.departures.exists()
    moved = zones.home_zone_change_blocker(v) if v.status != "cancelled" and not departed else None
    if moved is not None:
        # the home changed zone after this plan (readiness blocks it as PLANNING_HOME_ZONE_CHANGED): the
        # planner says so, Blocked, and never "Nothing is blocking this plan"
        card = {
            **card,
            "state": "blocked",
            "label": p.LABELS["blocked"],
            "plain": [plain_blocker(moved), *card["plain"]],
        }
    if can_plan and v.status != "cancelled":
        # read only, a fixed number of statements (planning/options.py); 03B closure P2: only the visit's own
        # hub's robots, people and vehicles are offered, the rest are counted
        # the windows a NEW plan uses: the home's current zone, as the save (as_replanned)
        options = restrict_to_hub(plan_options(p.as_replanned(v)), v.hub_id)
        if wants_review and not departed:
            from planning.review import build_review  # noqa: PLC0415

            # v2 03B-2 (spec S2): advisory only; saving runs the guarded plan command again
            review = build_review(v, request.GET, live=card["live"], opts=options)
        if (
            review is not None
            and review.blocker is None
            and (review.nothing_fits or request.GET.get("next") == "1")
        ):
            # v2 03B-3 (spec S2 "No option works", S3): the next free times at the visit's hub. Read only;
            # choosing one posts to the guarded plan command.
            from planning.next_free import next_free  # noqa: PLC0415

            next_slots = next_free(v)
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
            "reservations": reservation_rows(v),
            "travel_line": travel_text(card["rev"].travel_out_min, card["rev"].travel_back_min),
            "leave_at": (
                card["rev"].on_site_start - timedelta(minutes=card["rev"].travel_out_min)
                if card["rev"].travel_out_min is not None
                else None
            ),
            "can_fix_blocker": bool(
                review is not None
                and review.blocker is not None
                and has_permission(request.user, review.blocker["permission"])
            ),
            "options": options,
            # v2 02B: the assigned robot's charge forecast (planning information, never a departure decision)
            "charge": charging.visit_forecasts([v.pk]).get(v.pk) if charging.forecast_configured() else None,
            # v2 02D: stock reserved for this visit, and what it still cannot get (planning information; the
            # departure equipment check stays the gate)
            **_stock_context(v),
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
            # v2 03B-2 (spec S2, S6)
            "review": review,
            "wants_review": wants_review,
            "departed": departed,
            "changed": _changed(v, request.GET.get("changed")),
            "hub": v.hub,
            "home_title": home_name(v.site),
            "board_url": "/planning/board/"
            + (f"?hub={request.GET['hub']}" if request.GET.get("hub") else ""),
            "refuse_plan": cannot_change_text(v.hub),
            "nf": next_slots,
            "zone": v.zone if v.zone_id is not None else None,
            "legacy": (
                legacy_travel_text(card["rev"].travel_out_min, card["rev"].travel_back_min, v.site.zone_ref)
                if v.zone_id is None and not departed and v.status != "cancelled"
                else ""
            ),
            "nf_action": "plan",
            "nf_intro": (
                "No free robot, crew and vehicle for this time. Each time below was checked with the same "
                "plan check that runs when you save."
                if review is not None and review.nothing_fits
                else ""
            ),
            "q_hub": f"hub={request.GET['hub']}" if request.GET.get("hub") else "",
        },
    )


# Reservation purposes in the board's words (D-B7)
PURPOSE_WORDS = {
    "inspection": "Pre-visit inspection",
    "loading": "Loading",
    "travel_out": "Travel to the home",
    "on_site": "On site",
    "travel_back": "Travel back to the hub",
    "unloading": "Unloading",
    "charging": "Charging and turnaround",
    "trip": "Vehicle trip",
    "field_shift": "Crew shift",
}


def reservation_rows(v: Visit) -> list[dict[str, Any]]:
    """Names first, codes quieter (D-B7): "Grace Chen (P-212)", "Robot NB-201", "Vehicle VAN-5". One read."""
    out = []
    qs = (
        Reservation.objects.filter(revision__visit=v)
        .select_related("resource__person__user", "resource__robot", "resource__vehicle", "revision")
        .order_by("released_at", "starts_at", "id")
    )
    for r in qs:
        res = r.resource
        if res.kind == "person" and res.person is not None:
            name, code = person_name(res.person), res.person.code
        elif res.kind == "robot" and res.robot is not None:
            name, code = f"Robot {res.robot.code}", ""
        elif res.vehicle is not None:
            name, code = f"Vehicle {res.vehicle.code}", ""
        else:
            name, code = str(res), ""
        out.append(
            {
                "name": name,
                "code": code,
                "purpose": PURPOSE_WORDS.get(r.purpose, r.get_purpose_display()),
                "starts_at": r.starts_at,
                "ends_at": r.ends_at,
                "revision": r.revision.revision_no,
                "released_at": r.released_at,
                "released_reason": r.released_reason,
            }
        )
    return out


def cannot_change_text(hub: Any) -> str:
    """Spec S2 "Refused" (S6 formula: what, why, who): names only the visit's own hub."""
    return f"You can't change visits at {hub.name} ({hub.code}). Ask {_an(role_words('planning.plan'))}."


def _an(words: str) -> str:
    return f"an {words}" if words[:1].lower() in "aeiou" else f"a {words}"


def _changed(v: Visit, raw: str | None) -> dict[str, Any] | None:
    if not raw or not raw.isdigit():
        return None
    from planning.review import what_changed  # noqa: PLC0415

    return what_changed(v, int(raw))


def _stock_context(v: Visit) -> dict[str, Any]:
    from hub import stock  # noqa: PLC0415
    from hub.models import StockReservation  # noqa: PLC0415

    if v.status == "cancelled":
        return {"stock_reserved": [], "stock_needs": []}
    return {
        "stock_reserved": list(
            StockReservation.objects.filter(visit=v, released_at__isnull=True)
            .select_related("item")
            .order_by("item__code")
        ),
        "stock_needs": [n for n in stock.visit_needs() if n.visit_id == v.pk],
    }


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
def _book_hub(d: dict[str, Any]) -> Any:
    from platform_core.models import Hub  # noqa: PLC0415

    code = (d.get("hub") or "").strip().upper()
    hubs = list(Hub.objects.order_by("code"))
    return next((h for h in hubs if h.code == code), hubs[0] if len(hubs) == 1 else None)


def _book_slot(request: HttpRequest, target: Site, d: dict[str, Any], start: Any, end: Any) -> Any:
    """v2 03B-3 (S3): book a chosen next free time. Two guarded commands, in order: request the visit, then
    plan it with the slot's robot, crew and vehicle (``plan_visit`` checks everything again under its locks).
    If the plan is refused, the request stays and the visit page says why."""
    result = _run(
        request,
        p.request_visit,
        permission="planning.plan",
        ok="Visit requested.",
        site_code=target.code,
        on_site_start=start,
        on_site_end=end,
        travel_out_min=d["travel_out_min"],
        travel_back_min=d["travel_back_min"],
        tasks=d["tasks"],
        required_scopes=d["scopes"],
        hub_code=d.get("hub") or None,
    )
    if result is None:
        return None
    code = result["entity_id"]
    list(messages.get_messages(request))  # replaced by the one message below
    try:
        saved = p.plan_visit(
            request.user,
            visit_code=code,
            robot_code=request.POST.get("slot_robot") or None,
            lead_code=request.POST.get("slot_lead") or None,
            second_code=request.POST.get("slot_second") or None,
            vehicle_code=request.POST.get("slot_vehicle") or None,
            reason="Booked at a next free time",
        )
    except CommandError as exc:
        messages.error(
            request,
            f"Visit {code} is requested, but the plan for that time was refused: "
            f"{localize_utc_text(str(exc)).rstrip('.')}. Choose a robot, crew and vehicle below.",
        )
        return redirect("planning-visit", code=code)
    messages.success(request, "Visit booked at the chosen time. " + plan_saved_text(saved))
    return redirect(f"/planning/visits/{code}/?changed={int(saved['revision'])}#changed")


@login_required
def book_visit(request: HttpRequest, code: str | None = None) -> HttpResponse:
    if (deny := _guard(request)) is not None:
        return deny
    site = _site_or_404(code) if code else None
    chosen = site or (Site.objects.filter(code=request.GET.get("site", "")).first())
    initial = {"site": chosen.code} if chosen else {}
    form = BookForm(request.POST or None, initial=initial)
    nf_result = None
    book_hidden: dict[str, Any] = {}
    book_scopes: list[str] = []
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
                if request.POST.get("find") and has_permission(request.user, "planning.plan"):
                    # v2 03B-3 (S3): read only; nothing is saved until a time is chosen
                    from planning.next_free import Request as FreeRequest  # noqa: PLC0415
                    from planning.next_free import next_free  # noqa: PLC0415

                    hub = _book_hub(d)
                    if hub is not None:
                        nf_result = next_free(
                            FreeRequest(
                                site=target,
                                hub=hub,
                                minutes=int((end - start).total_seconds() // 60),
                                tasks=d["tasks"],
                                scopes=d["scopes"],
                                travel_out_min=d["travel_out_min"],
                                travel_back_min=d["travel_back_min"],
                            )
                        )
                        book_hidden = {
                            "site": target.code,
                            "travel_out_min": d["travel_out_min"] or "",
                            "travel_back_min": d["travel_back_min"] or "",
                            "tasks": ", ".join(d["tasks"]),
                            **({"hub": d["hub"]} if d.get("hub") else {}),
                        }
                        book_scopes = list(d["scopes"])
                    else:
                        messages.error(request, "Choose the hub first, then find the next free times.")
                elif request.POST.get("slot_robot"):
                    if (done := _book_slot(request, target, d, start, end)) is not None:
                        return done
                elif (
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
                        hub_code=d.get("hub") or None,
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
            "nf": nf_result,
            "nf_action": "book",
            "book_action": request.path,
            "book_hidden": book_hidden,
            "book_scopes": book_scopes,
        },
    )


def _redirect_visit(code: str) -> HttpResponse:
    return redirect("planning-visit", code=code)


@login_required
@require_POST
def plan_view(request: HttpRequest, code: str) -> HttpResponse:
    """Save a plan through the ONE guarded plan command (planning.services.plan_visit): its own checks under
    its locks, then the database triggers. Used by the plan form and by Review options (v2 03B-2, spec S2)."""
    v = get_object_or_404(Visit.objects.select_related("site", "hub"), code=code)
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
    if consume_form_token(request) != "ok":
        messages.warning(request, ALREADY_DONE)
        return _redirect_visit(code)
    try:
        result = p.plan_visit(
            request.user,
            visit_code=code,
            robot_code=d["robot"] or None,
            lead_code=d["lead"] or None,
            second_code=d["second"] or None,
            vehicle_code=d["vehicle"] or None,
            reason=d["reason"] or "replanned",
            **kw,
        )
    except CommandError as exc:
        messages.error(request, plan_refusal(exc, v, d, kw))
        return _redirect_visit(code)
    messages.success(request, plan_saved_text(result))
    back = f"/planning/visits/{code}/?changed={int(result['revision'])}"
    if request.POST.get("hub"):
        back += f"&hub={str(request.POST['hub'])[:12]}"
    return redirect(f"{back}#changed")


def plan_saved_text(result: dict[str, Any]) -> str:
    """S6: a success message says what is now true and what is still needed."""
    if result.get("feasibility") == "feasible":
        return (
            "Plan saved. Planning feasible: not cleared to depart. "
            "Next: confirm the visit, then complete departure readiness."
        )
    if result.get("feasibility") == "review":
        return "Plan saved. Requires review: not cleared to depart. Check what needs review below."
    n = len(result.get("blockers") or [])
    return (
        f"Plan saved as a request: {n} problem{'s' if n != 1 else ''} still block it. "
        "The result of the check is shown below."
    )


def plan_refusal(exc: CommandError, v: Visit, d: dict[str, Any], kw: dict[str, Any]) -> str:
    """S6 refusal formula for a plan: what was refused, why in plain words, what to do or who can. The reasons
    of a blocked reschedule are re-read with the read-only plan check (the same evaluate()), so they are
    sentences with local times, never codes or UTC."""
    hub = v.hub
    if isinstance(exc, PermissionDenied):
        return f"Refused (not allowed): {cannot_change_text(hub)} Nothing was changed."
    text = str(exc)
    if text.startswith("Rescheduling refused"):
        from planning.models import Person, Vehicle  # noqa: PLC0415

        rev = v.current_revision
        assert rev is not None
        robot = Robot.objects.filter(code=d["robot"]).first() if d["robot"] else None
        plan = p.evaluate(
            site=v.site,
            start=kw.get("on_site_start", rev.on_site_start),
            end=kw.get("on_site_end", rev.on_site_end),
            travel_out=rev.travel_out_min,
            travel_back=rev.travel_back_min,
            tasks=rev.tasks,
            scopes=rev.required_consent_scopes,
            robot=robot,
            lead=Person.objects.filter(code=d["lead"]).first() if d["lead"] else None,
            second=Person.objects.filter(code=d["second"]).first() if d["second"] else None,
            vehicle=Vehicle.objects.filter(code=d["vehicle"]).first() if d["vehicle"] else None,
            visit=v,
        )
        why = [b["sentence"] for b in plain_blockers([x for x in plan.blockers if x["level"] == "blocked"])]
        reasons = " ".join(why) if why else "The plan check found a blocker."
        return (
            f"Refused (not possible right now): The new plan for visit {v.code} can't be saved. {reasons} "
            "Nothing was changed. Pick another option under Review options."
        )
    return f"Refused (not possible right now): {localize_utc_text(text).rstrip('.')}. Nothing was changed."


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
