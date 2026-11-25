"""Hub > Charging (v2 02B): chargers, reservations today and tomorrow, open sessions, the charge forecast of
the visits today and tomorrow, and the forms to start or end a session, reserve a charger and record a
battery level. Every write is one guarded function call (hub.charging); a refusal is shown in plain words.
Nothing here is departure evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.permissions import active_roles
from accounts.roles import PERMISSIONS
from fleet.models import Robot
from hub import charging as ch
from hub.models import Charger, ChargerReservation, ChargingSession
from planning.models import Visit
from platform_core.commands import CommandError
from platform_core.hubscope import HubScope, add_owner_hub_field, hub_scope
from platform_core.interaction import UIForm, command_page, denied, explain, spend_form_token
from platform_core.timezones import workspace_zone

LOCAL_INPUT = ["%Y-%m-%dT%H:%M"]


def _allowed(roles: set[str], perm: str) -> bool:
    return bool(roles & PERMISSIONS.get(perm, set()))


def _local(value: datetime | None) -> datetime | None:
    return None if value is None else value.replace(tzinfo=None).replace(tzinfo=workspace_zone())


class StartForm(forms.Form):
    charger = forms.CharField(max_length=12)
    robot = forms.CharField(max_length=12)
    until = forms.DateTimeField(input_formats=LOCAL_INPUT)
    start_soc = forms.IntegerField(min_value=0, max_value=100, required=False)


class EndForm(forms.Form):
    end_soc = forms.IntegerField(min_value=0, max_value=100, required=False)


class ReserveForm(forms.Form):
    charger = forms.CharField(max_length=12)
    robot = forms.CharField(max_length=12)
    starts = forms.DateTimeField(input_formats=LOCAL_INPUT)
    ends = forms.DateTimeField(input_formats=LOCAL_INPUT)
    visit = forms.CharField(max_length=12, required=False)


class SocForm(forms.Form):
    robot = forms.CharField(max_length=12)
    soc = forms.IntegerField(min_value=0, max_value=100)
    observed = forms.DateTimeField(input_formats=LOCAL_INPUT, required=False)


class ReleaseForm(UIForm):
    reason = forms.CharField(max_length=200, label="Reason", help_text="Kept in the audit log.")


@login_required
@require_GET
def charging_page(request: HttpRequest) -> HttpResponse:
    roles = active_roles(request.user)
    if not _allowed(roles, "hub.view"):
        return denied(request, "hub.view")
    now = timezone.now()
    horizon = now + timedelta(days=2)
    scope = hub_scope(request)  # v2 03A-1: the viewer's hubs; ?hub=CODE narrows
    chargers = list(scope.filter(Charger.objects.order_by("code")))
    sessions = list(
        scope.filter(ChargingSession.objects.filter(ended_at__isnull=True))
        .select_related("charger", "robot")
        .order_by("started_at")
    )
    reservations = list(
        scope.filter(ChargerReservation.objects)
        .filter(released_at__isnull=True, ends_at__gt=now, starts_at__lt=horizon)
        .select_related("charger", "robot", "visit")
        .order_by("starts_at")
    )
    visits = list(
        scope.filter(Visit.objects)
        .filter(
            current_revision__on_site_start__gte=now,
            current_revision__on_site_start__lt=horizon,
            current_revision__robot__isnull=False,
        )
        .exclude(status="cancelled")
        .select_related("current_revision__robot")
        .order_by("current_revision__on_site_start")
    )
    forecasts = ch.visit_forecasts([v.pk for v in visits], now)
    rows: list[dict[str, Any]] = [{"visit": v, "f": forecasts.get(v.pk)} for v in visits]
    far = timezone.now() + timedelta(days=365)
    rows.sort(key=lambda r: (r["f"].departure_at if r["f"] else far, r["visit"].code))  # by departure time
    states = [r["f"].state for r in rows if r["f"] is not None]
    by_charger = {s.charger_id: s for s in sessions}
    next_res: dict[int, ChargerReservation] = {}
    for r in reservations:
        next_res.setdefault(r.charger_id, r)
    return render(
        request,
        "hub/charging.html",
        {
            "chargers": [
                {"c": c, "session": by_charger.get(c.pk), "next": next_res.get(c.pk)} for c in chargers
            ],
            "sessions": sessions,
            "reservations": reservations,
            "rows": rows,
            "needs": states.count(ch.NEEDS),
            "unknown": states.count(ch.UNKNOWN),
            "configured": ch.forecast_configured(),
            "can_record": _allowed(roles, ch.RECORD),
            "can_reserve": _allowed(roles, ch.RESERVE),
            "can_manage": _allowed(roles, ch.MANAGE_CHARGERS),
            "in_service": [c for c in chargers if c.in_service],
            "robot_options": _robot_options(scope),
            "scope": scope,
            "default_until": (now + timedelta(hours=3))
            .astimezone(workspace_zone())
            .strftime("%Y-%m-%dT%H:%M"),
        },
    )


def _robot_options(scope: HubScope | None = None) -> list[tuple[str, str]]:
    """Named robots for the robot selects: in service first, then the others with their state (the commands
    still validate the code)."""
    qs = Robot.objects.exclude(lifecycle="retired").order_by("code")
    robots = list(scope.filter(qs) if scope is not None else qs)
    robots.sort(key=lambda r: (r.lifecycle != "in_service", r.code))
    return [
        (r.code, f"{r.code} · in service" if r.lifecycle == "in_service"
         else f"{r.code} · not in service ({r.get_lifecycle_display()})")
        for r in robots
    ]  # fmt: skip


def _do(request: HttpRequest, perm: str, fn: Any, ok: str, back: str = "/hub/charging/") -> HttpResponse:
    roles = active_roles(request.user)
    if not _allowed(roles, perm):
        return denied(request, perm)
    if not spend_form_token(request):
        return redirect(back)
    try:
        fn()
    except forms.ValidationError as exc:
        messages.error(request, f"Refused (check the form): {' '.join(exc.messages)} Nothing was changed.")
        return redirect(back)
    except CommandError as exc:
        text = explain(exc, perm) if str(exc).startswith("Not allowed:") else str(exc).rstrip(".") + "."
        messages.error(request, f"Refused ({exc.code.replace('_', ' ')}): {text} Nothing was changed.")
        return redirect(back)
    messages.success(request, ok)
    return redirect(back)


def _valid(form: forms.Form) -> dict[str, Any]:
    if not form.is_valid():
        raise forms.ValidationError(
            [f"{name}: {' '.join(str(e) for e in errs)}" for name, errs in form.errors.items()]
        )
    return form.cleaned_data


@login_required
@require_POST
def start_view(request: HttpRequest) -> HttpResponse:
    def go() -> None:
        d = _valid(StartForm(request.POST))
        ch.start_charging(request.user, charger_code=d["charger"], robot_code=d["robot"],
                          planned_until=_local(d["until"]), start_soc_pct=d["start_soc"])  # type: ignore[arg-type]  # fmt: skip

    return _do(
        request, ch.RECORD, go, "Charging started. This records the session only; it is not a battery check."
    )


@login_required
@require_POST
def end_view(request: HttpRequest, session_id: int) -> HttpResponse:
    def go() -> None:
        d = _valid(EndForm(request.POST))
        ch.end_charging(request.user, session_id=session_id, end_soc_pct=d["end_soc"])

    return _do(
        request, ch.RECORD, go,
        "Charging ended. The end level is kept as a reading; the departure battery check is still required.",
    )  # fmt: skip


@login_required
@require_POST
def reserve_view(request: HttpRequest) -> HttpResponse:
    def go() -> None:
        d = _valid(ReserveForm(request.POST))
        ch.reserve_charger(request.user, charger_code=d["charger"], robot_code=d["robot"],
                           starts_at=_local(d["starts"]), ends_at=_local(d["ends"]),  # type: ignore[arg-type]
                           visit_code=d["visit"].strip().upper() or None)  # fmt: skip

    return _do(request, ch.RESERVE, go, "Charger reserved.")


@login_required
@require_POST
def soc_view(request: HttpRequest) -> HttpResponse:
    def go() -> None:
        d = _valid(SocForm(request.POST))
        ch.record_soc(
            request.user, robot_code=d["robot"], soc_pct=d["soc"], observed_at=_local(d["observed"])
        )

    return _do(
        request, ch.RECORD, go, "Battery level recorded. It is a reading, not a departure battery check."
    )


@login_required
def release_view(request: HttpRequest, reservation_id: int) -> HttpResponse:
    r = get_object_or_404(ChargerReservation.objects.select_related("charger", "robot"), pk=reservation_id)
    return command_page(
        request,
        permission=ch.RESERVE,
        form_class=ReleaseForm,
        template="hub/release.html",
        call=lambda actor, d: _release(actor, r.pk, d["reason"]),
        ok=f"Reservation of {r.charger.code} for {r.robot.code} released.",
        success_url="/hub/charging/",
        context={"r": r},
    )


def _release(actor: Any, reservation_id: int, reason: str) -> dict[str, Any]:
    ch.release_reservation(actor, reservation_id=reservation_id, reason=reason)
    return {"entity_id": str(reservation_id)}


# ------------------------------------------------------------------ charger setup (configuration)
class ChargerForm(UIForm):
    code = forms.CharField(
        max_length=12, label="Charger code", help_text="Short and unique, for example C-1."
    )
    name = forms.CharField(
        max_length=80, label="Name", help_text="Where it is, for example Bay 1 by the door."
    )
    note = forms.CharField(max_length=200, required=False, label="Note (optional)")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        add_owner_hub_field(self, "The hub where this charger is.")


class ServiceForm(UIForm):
    in_service = forms.ChoiceField(label="State", choices=[("1", "In service"), ("0", "Out of service")])
    reason = forms.CharField(max_length=200, label="Reason", help_text="Kept in the audit log.")


@login_required
def chargers_page(request: HttpRequest) -> HttpResponse:
    return command_page(
        request,
        permission=ch.MANAGE_CHARGERS,
        form_class=ChargerForm,
        template="hub/chargers.html",
        call=lambda actor, d: ch.create_charger(
            actor, code=d["code"], name=d["name"], note=d["note"], hub_code=d.get("hub") or None
        ),
        ok=lambda r: f"Charger {r['entity_id']} added.",
        success_url="/hub/charging/chargers/",
        context={"chargers": Charger.objects.order_by("code")},
    )


@login_required
def charger_service(request: HttpRequest, code: str) -> HttpResponse:
    c = get_object_or_404(Charger, code=code)
    return command_page(
        request,
        permission=ch.MANAGE_CHARGERS,
        form_class=ServiceForm,
        template="hub/charger_service.html",
        initial={"in_service": "1" if c.in_service else "0"},
        call=lambda actor, d: ch.set_charger_in_service(
            actor, code=c.code, in_service=d["in_service"] == "1", reason=d["reason"]
        ),
        ok=f"Charger {c.code} updated.",
        success_url="/hub/charging/chargers/",
        context={"c": c},
    )
