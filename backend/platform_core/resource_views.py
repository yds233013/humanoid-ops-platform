"""01C.2 People, qualifications, vehicles and crews.

State changes go through ``command_page`` and call ONE existing planning command: register_person,
grant_qualification, revoke_qualification, register_vehicle. (This module only CALLS planning.services; it
does not edit the planning app.) Registering a person never qualifies them; the readiness_inspector
qualification stays with the safety reviewer and the service enforces it.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from accounts.models import RoleGrant
from accounts.permissions import has_permission
from planning import services as ps
from planning.models import Person, Qualification, Vehicle
from platform_core.hubscope import add_owner_hub_field
from platform_core.interaction import ConfirmForm, UIForm, command_page, denied, holds_any
from platform_core.timezones import workspace_zone

User = get_user_model()
READ = "planning.read"
MANAGE_PEOPLE = "planning.manage_people"
GRANT_INSPECTOR = "planning.grant_inspector_qualification"
REVOKE_INSPECTOR = "planning.revoke_inspector_qualification"
INSPECTOR = ps.INSPECTOR_KIND


def _active_roles_by_user() -> dict[int, list[str]]:
    now = timezone.now()
    out: dict[int, list[str]] = {}
    for g in RoleGrant.objects.filter(revoked_at__isnull=True, valid_from__lte=now).order_by("role"):
        if g.valid_to is None or g.valid_to > now:
            out.setdefault(g.user_id, []).append(g.role)
    return out


def _qual_state(q: Qualification, now: datetime) -> str:
    if q.revoked_at is not None:
        return "revoked"
    if q.valid_to <= now:
        return "expired"
    if q.valid_from > now:
        return "not started"
    return "active"


def _day_start(d: date) -> datetime:
    # a typed calendar day is read in the workspace display zone (the zone every date on screen is shown in)
    return datetime.combine(d, time.min, tzinfo=workspace_zone()).astimezone(UTC)


# ------------------------------------------------------------------ forms
class PersonForm(UIForm):
    username = forms.ChoiceField(
        label="Account",
        help_text="The person's sign-in account. Invite them first in Settings if it is missing.",
    )
    code = forms.CharField(
        max_length=12, label="Person code", help_text="A short unique code, for example P-4."
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        free = User.objects.filter(is_active=True, person__isnull=True).order_by("username")
        field: Any = self.fields["username"]
        field.choices = [(u.username, u.username) for u in free]
        add_owner_hub_field(
            self, "Their home hub. It gives no permission: roles are given per hub in Settings."
        )


class QualificationForm(UIForm):
    kind = forms.ChoiceField(
        label="Qualification",
        choices=[
            (k, label + (" (requires the safety reviewer)" if k == INSPECTOR else ""))
            for k, label in Qualification.KINDS
        ],
    )
    valid_from = forms.DateField(
        required=False,
        label="Valid from",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Required, except for a readiness inspector, which starts when it is granted.",
    )
    valid_to = forms.DateField(label="Valid until (last day)", widget=forms.DateInput(attrs={"type": "date"}))
    basis_ref = forms.CharField(
        max_length=200,
        required=False,
        label="Training record reference",
        help_text="Required for a readiness inspector. A reference only, no personal details.",
    )

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if data.get("kind") and data["kind"] != INSPECTOR and not data.get("valid_from"):
            self.add_error("valid_from", "Choose the first valid day.")
        if data.get("valid_from") and data.get("valid_to") and data["valid_to"] < data["valid_from"]:
            self.add_error("valid_to", "The last day cannot be before the first day.")
        return data


class RevokeQualificationForm(ConfirmForm):
    reason = forms.CharField(max_length=300, label="Reason", help_text="Kept in the audit log.")


class VehicleForm(UIForm):
    code = forms.CharField(max_length=12, label="Vehicle code", help_text="For example V-1.")
    robot_capacity = forms.IntegerField(
        required=False,
        min_value=1,
        label="Robots it can carry",
        help_text="Leave empty if unknown. An unknown capacity blocks visits that carry a robot.",
    )
    restraint_kit = forms.CharField(
        max_length=60,
        required=False,
        label="Restraint kit",
        help_text="Leave empty if unknown. An unknown kit blocks visits that carry a robot.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        add_owner_hub_field(self, "The hub that owns the vehicle.")


# ------------------------------------------------------------------ people
@login_required
def people_list(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, READ):
        return denied(request, READ)
    now = timezone.now()
    roles = _active_roles_by_user()
    quals: dict[int, list[str]] = {}
    for q in Qualification.objects.filter(revoked_at__isnull=True, valid_from__lte=now, valid_to__gt=now):
        quals.setdefault(q.person_id, []).append(q.kind)
    rows = [
        {
            "person": p,
            "roles": roles.get(p.user_id, []),
            "quals": sorted(quals.get(p.pk, [])),
        }
        for p in Person.objects.select_related("user").order_by("code")
    ]
    return render(request, "resources/people.html", {"rows": rows})


@login_required
def person_new(request: HttpRequest) -> HttpResponse:
    return command_page(
        request,
        permission=MANAGE_PEOPLE,
        form_class=PersonForm,
        template="resources/person_new.html",
        call=lambda actor, d: ps.register_person(
            actor, username=d["username"], code=d["code"].strip(), hub_code=d.get("hub") or None
        ),
        ok=lambda r: f"{r['entity_id']} registered. They have no qualification yet: that is a separate step.",
        success_url=lambda r: f"/people/{r['entity_id']}/",
    )


@login_required
def person_detail(request: HttpRequest, code: str) -> HttpResponse:
    """Qualifications of one person. POST grants one with grant_qualification."""
    person = get_object_or_404(Person.objects.select_related("user"), code=code)
    now = timezone.now()
    quals = [{"q": q, "state": _qual_state(q, now)} for q in person.qualifications.order_by("-granted_at")]
    ctx = {
        "person": person,
        "quals": quals,
        "inspector": INSPECTOR,
        "can_revoke": holds_any(request, (MANAGE_PEOPLE, REVOKE_INSPECTOR)),
    }
    perms = (MANAGE_PEOPLE, GRANT_INSPECTOR)
    if not holds_any(request, perms):
        if request.method != "GET" or not has_permission(request.user, READ):
            return denied(request, perms if request.method == "POST" else READ)
        return render(request, "resources/person.html", {**ctx, "form": None})

    def call(actor: Any, d: dict[str, Any]) -> dict[str, Any]:
        kind = d["kind"]
        return ps.grant_qualification(
            actor,
            person_code=code,
            kind=kind,
            valid_from=None if kind == INSPECTOR else _day_start(d["valid_from"]),
            valid_to=_day_start(d["valid_to"] + timedelta(days=1)),
            basis_ref=(d.get("basis_ref") or "").strip(),
        )

    return command_page(
        request,
        permission=perms,
        form_class=QualificationForm,
        template="resources/person.html",
        call=call,
        ok=lambda r: f"Qualification {r['kind'].replace('_', ' ')} granted to {code}.",
        success_url=request.path,
        context=ctx,
        command_permission=lambda d: GRANT_INSPECTOR if d["kind"] == INSPECTOR else MANAGE_PEOPLE,
    )


@login_required
def qualification_revoke(request: HttpRequest, code: str, qid: int) -> HttpResponse:
    person = get_object_or_404(Person, code=code)
    q = Qualification.objects.filter(pk=qid, person=person).first()
    if q is None:
        raise Http404("No such qualification")
    perm = REVOKE_INSPECTOR if q.kind == INSPECTOR else MANAGE_PEOPLE
    return command_page(
        request,
        permission=perm,
        form_class=RevokeQualificationForm,
        template="resources/qualification_revoke.html",
        call=lambda actor, d: ps.revoke_qualification(
            actor, qualification_id=qid, reason=d["reason"].strip()
        ),
        ok=f"Qualification {q.kind.replace('_', ' ')} of {code} revoked.",
        success_url=f"/people/{code}/",
        context={"person": person, "q": q},
    )


# ------------------------------------------------------------------ vehicles
@login_required
def vehicle_list(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, READ):
        return denied(request, READ)
    return render(request, "resources/vehicles.html", {"vehicles": Vehicle.objects.order_by("code")})


@login_required
def vehicle_new(request: HttpRequest) -> HttpResponse:
    return command_page(
        request,
        permission="planning.manage_vehicles",
        form_class=VehicleForm,
        template="resources/vehicle_new.html",
        call=lambda actor, d: ps.register_vehicle(
            actor,
            code=d["code"].strip(),
            robot_capacity=d.get("robot_capacity"),
            restraint_kit=(d.get("restraint_kit") or "").strip(),
            hub_code=d.get("hub") or None,
        ),
        ok=lambda r: f"Vehicle {r['entity_id']} added.",
        success_url="/vehicles/",
    )


# ------------------------------------------------------------------ crews
@login_required
def crew_list(request: HttpRequest) -> HttpResponse:
    """Crews are the people with a field role: the field operator role or a field qualification."""
    if not has_permission(request.user, READ):
        return denied(request, READ)
    now = timezone.now()
    roles = _active_roles_by_user()
    field_qual: dict[int, list[str]] = {}
    for q in Qualification.objects.filter(
        revoked_at__isnull=True,
        valid_from__lte=now,
        valid_to__gt=now,
        kind__in=["field_lead", "field_operator", "robot_handler"],
    ):
        field_qual.setdefault(q.person_id, []).append(q.kind)
    people = {p.user_id: p for p in Person.objects.select_related("user")}
    rows = []
    for user_id, user_roles in roles.items():
        if "field_operator" in user_roles:
            person = people.get(user_id)
            rows.append(
                {
                    "username": person.user.username if person else User.objects.get(pk=user_id).username,
                    "person": person,
                    "quals": sorted(field_qual.get(person.pk, [])) if person else [],
                }
            )
    seen = {r["username"] for r in rows}
    for p in people.values():
        if p.pk in field_qual and p.user.username not in seen:
            rows.append({"username": p.user.username, "person": p, "quals": sorted(field_qual[p.pk])})
    rows.sort(key=lambda r: str(r["username"]))
    return render(request, "resources/crews.html", {"rows": rows})
