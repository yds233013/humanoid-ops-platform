"""01C.1 Workspace setup screens: first-run checklist, company settings, hubs, people and roles.

Admin only. Every state change goes through ``command_page``: role check, one-time token, validation, one
command, feedback. User invitation uses accounts.create_user and role changes use the existing grant_role /
revoke_role. Company settings and hubs are new setup data (platform_core.workspace). Setup records facts only;
nothing on these pages approves, qualifies or sets a policy.
"""

from typing import Any

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts import services as account_services
from accounts.models import RoleGrant
from accounts.permissions import has_permission
from accounts.roles import ADMIN, ALL_ROLES
from platform_core import workspace as ws_commands
from platform_core.commands import Invalid
from platform_core.interaction import ConfirmForm, UIForm, command_page, denied
from platform_core.models import AuditEvent, Hub, Workspace

User = get_user_model()
ADMIN_PERM = "accounts.grant_role"
DEFAULT_NAMES = {"", "Workspace", "Demo workspace"}


def _workspace() -> Workspace:
    Workspace.ensure()
    return Workspace.objects.get(pk=1)


# 01D P2-4: common zones offered as a select; the server still accepts any valid IANA name (a CharField with
# a Select widget: the choices are a convenience, validate_time_zone decides).
COMMON_TIME_ZONES = [
    "America/Los_Angeles",
    "America/Denver",
    "America/Phoenix",
    "America/Chicago",
    "America/New_York",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Kolkata",
    "Asia/Tokyo",
    "Australia/Sydney",
    "UTC",
]


def _tz_field(initial: str = "") -> forms.CharField:
    return forms.CharField(
        max_length=64,
        initial=initial,
        label="Time zone",
        widget=forms.Select(choices=[(z, z) for z in COMMON_TIME_ZONES]),
        help_text="Visit times are shown in this zone. Common zones are listed; ask your administrator if "
        "yours is missing (any IANA zone name such as Europe/Madrid is accepted).",
    )


def _clean_tz(value: str) -> str:
    try:
        return ws_commands.validate_time_zone(value)
    except Invalid as exc:
        raise forms.ValidationError(str(exc)) from None


class TimeZoneFormMixin(forms.Form):
    """Keeps the current (or submitted) zone in the select even when it is not a common one."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["time_zone"]
        current = str(self.data.get("time_zone") or self.initial.get("time_zone") or field.initial or "")
        widget = field.widget
        assert isinstance(widget, forms.Select)
        zones = list(COMMON_TIME_ZONES)
        if current and current not in zones:
            zones.insert(0, current)
        widget.choices = [(z, z) for z in zones]

    def clean_time_zone(self) -> str:
        return _clean_tz(self.cleaned_data["time_zone"])


class CompanyForm(TimeZoneFormMixin, UIForm):
    name = forms.CharField(max_length=120, label="Company name")
    region = forms.CharField(
        max_length=80, label="Region", help_text="Where you operate, for example Bay Area, California."
    )
    time_zone = _tz_field()


class HubForm(TimeZoneFormMixin, UIForm):
    code = forms.CharField(
        max_length=12,
        label="Hub code",
        help_text="Short and unique, for example SF1. Capital letters and digits.",
    )
    name = forms.CharField(max_length=80, label="Hub name")
    area = forms.CharField(
        max_length=80, required=False, label="Area", help_text="The town or neighbourhood the hub serves."
    )
    time_zone = _tz_field()


class InviteForm(UIForm):
    username = forms.CharField(max_length=150, label="Username")
    email = forms.EmailField(required=False, label="Email (optional)")
    password = forms.CharField(
        label="Temporary password",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Share it with the person privately. At least 8 characters and not a common password.",
    )


class ResetPasswordForm(ConfirmForm):
    password = forms.CharField(
        label="New temporary password",
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Share it with the person privately. They must change it when they next sign in.",
    )


class GrantForm(UIForm):
    role = forms.ChoiceField(label="Role", choices=[(r, r.replace("_", " ").capitalize()) for r in ALL_ROLES])


class RevokeForm(ConfirmForm):
    reason = forms.CharField(max_length=300, label="Reason", help_text="Kept in the audit log.")


# ------------------------------------------------------------------ checklist
def setup_steps(ws: Workspace) -> list[dict[str, Any]]:
    saved = AuditEvent.objects.filter(
        command="platform_core.update_workspace", outcome=AuditEvent.OUTCOME_COMMITTED
    ).exists()
    invited = (
        RoleGrant.objects.filter(revoked_at__isnull=True)
        .exclude(role=ADMIN)
        .values("user_id")
        .distinct()
        .count()
    )
    hubs = Hub.objects.count()
    named = ws.name.strip() not in DEFAULT_NAMES
    from fleet import provisioning

    operating_ok = provisioning.required_complete()
    return [
        {
            "title": "Company name",
            "done": named,
            "detail": ws.name if named else "Not set yet",
            "url": "/settings/company/",
            "action": "Edit",
        },
        {
            "title": "Region",
            "done": bool(ws.region.strip()),
            "detail": ws.region or "Not set yet",
            "url": "/settings/company/",
            "action": "Edit",
        },
        {
            "title": "Time zone",
            "done": saved,
            "detail": ws.time_zone + ("" if saved else " (default, please confirm)"),
            "url": "/settings/company/",
            "action": "Confirm",
        },
        {
            "title": "Hubs",
            "done": hubs > 0,
            "detail": f"{hubs} added" if hubs else "No hub yet",
            "url": "/settings/hubs/new/",
            "action": "Add a hub",
        },
        {
            "title": "Invite people",
            "done": invited > 0,
            "detail": f"{invited} with a role" if invited else "Nobody has a role yet",
            "url": "/settings/users/new/",
            "action": "Invite someone",
        },
        {
            "title": "Operating settings",
            "done": operating_ok,
            "detail": "All required values are set"
            if operating_ok
            else "Operating settings incomplete: the deployment owner must provision the required values",
            "url": "/settings/operating/",
            "action": "View",
        },
    ]


@login_required
@require_http_methods(["GET", "HEAD"])
def setup_checklist(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, ADMIN_PERM):
        return denied(request, ADMIN_PERM)
    from fleet.models import ManifestPattern, Robot

    ws = _workspace()
    steps = setup_steps(ws)
    return render(
        request,
        "settings/setup.html",
        {
            "steps": steps,
            "done_count": sum(1 for s in steps if s["done"]),
            "robots": Robot.objects.count(),
            "patterns": ManifestPattern.objects.count(),
            "workspace": ws,
            "operating_incomplete": not steps[-1]["done"],
        },
    )


# ------------------------------------------------------------------ operating settings (read only)
@login_required
@require_http_methods(["GET", "HEAD"])
def operating_settings(request: HttpRequest) -> HttpResponse:
    """Read-only. There is no form and no POST handler: only the deployment owner provisions these."""
    perm = "platform_core.read_operating_settings"
    if not has_permission(request.user, perm):
        return denied(request, perm)
    from fleet import provisioning

    return render(
        request,
        "settings/operating.html",
        {"rows": provisioning.rows(), "complete": provisioning.required_complete()},
    )


# ------------------------------------------------------------------ company
@login_required
def company_settings(request: HttpRequest) -> HttpResponse:
    ws = _workspace()
    return command_page(
        request,
        permission="platform_core.manage_workspace",
        form_class=CompanyForm,
        template="settings/company.html",
        initial={
            "name": "" if ws.name in DEFAULT_NAMES else ws.name,
            "region": ws.region,
            "time_zone": ws.time_zone,
        },
        call=lambda actor, d: ws_commands.update_workspace(
            actor, name=d["name"], region=d["region"], time_zone=d["time_zone"]
        ),
        ok="Company settings saved.",
        success_url="/settings/setup/",
        context={"workspace": ws},
    )


# ------------------------------------------------------------------ hubs
@login_required
def hub_list(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, ADMIN_PERM):
        return denied(request, ADMIN_PERM)
    return render(request, "settings/hubs.html", {"hubs": Hub.objects.all()})


@login_required
def hub_new(request: HttpRequest) -> HttpResponse:
    return command_page(
        request,
        permission="platform_core.manage_hubs",
        form_class=HubForm,
        template="settings/hub_new.html",
        initial={"time_zone": _workspace().time_zone},
        call=lambda actor, d: ws_commands.create_hub(
            actor, code=d["code"], name=d["name"], area=d["area"], time_zone=d["time_zone"]
        ),
        ok=lambda r: f"Hub {r['entity_id']} added.",
        success_url="/settings/hubs/",
    )


# ------------------------------------------------------------------ people and roles
def _active_grants() -> dict[int, list[RoleGrant]]:
    now = timezone.now()
    out: dict[int, list[RoleGrant]] = {}
    for g in RoleGrant.objects.filter(revoked_at__isnull=True, valid_from__lte=now).order_by("role"):
        if g.valid_to is None or g.valid_to > now:
            out.setdefault(g.user_id, []).append(g)
    return out


@login_required
def user_list(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, ADMIN_PERM):
        return denied(request, ADMIN_PERM)
    grants = _active_grants()
    rows = [
        {"user": u, "roles": [g.role for g in grants.get(u.pk, [])]}
        for u in User.objects.order_by("username")
    ]
    return render(request, "settings/users.html", {"rows": rows})


@login_required
def user_new(request: HttpRequest) -> HttpResponse:
    return command_page(
        request,
        permission="accounts.create_user",
        form_class=InviteForm,
        template="settings/user_new.html",
        call=lambda actor, d: account_services.create_user(
            actor, username=d["username"], password=d["password"], email=d.get("email", "")
        ),
        ok=lambda r: f"Account {r['username']} created. Now choose their role.",
        success_url=lambda r: f"/settings/users/{r['entity_id']}/",
    )


@login_required
def user_detail(request: HttpRequest, user_id: int) -> HttpResponse:
    """Roles of one person. POST grants a role with the existing grant_role command."""
    if not has_permission(request.user, ADMIN_PERM):
        return denied(request, ADMIN_PERM)
    target = get_object_or_404(User, pk=user_id)
    grants = _active_grants().get(target.pk, [])
    return command_page(
        request,
        permission=ADMIN_PERM,
        form_class=GrantForm,
        template="settings/user_detail.html",
        call=lambda actor, d: account_services.grant_role(actor, user_id=target.pk, role=d["role"]),
        ok=lambda r: f"Role {r['role'].replace('_', ' ')} given to {target.username}.",
        success_url=request.path,
        context={"target": target, "grants": grants, "is_self": target.pk == request.user.pk},
    )


@login_required
def user_revoke(request: HttpRequest, user_id: int, grant_id: int) -> HttpResponse:
    """Confirm page: take a role away (existing revoke_role command)."""
    if not has_permission(request.user, "accounts.revoke_role"):
        return denied(request, "accounts.revoke_role")
    target = get_object_or_404(User, pk=user_id)
    grant = RoleGrant.objects.filter(pk=grant_id, user=target).first()
    if grant is None:
        raise Http404("No such role")
    return command_page(
        request,
        permission="accounts.revoke_role",
        form_class=RevokeForm,
        template="settings/user_revoke.html",
        call=lambda actor, d: account_services.revoke_role(actor, grant_id=grant_id, reason=d["reason"]),
        ok=f"Role {grant.role.replace('_', ' ')} taken away from {target.username}.",
        success_url=f"/settings/users/{target.pk}/",
        context={"target": target, "grant": grant},
    )


@login_required
def user_password(request: HttpRequest, user_id: int) -> HttpResponse:
    """Confirm page: set a new temporary password for someone else (accounts.reset_password). The person must
    change it at their next sign-in. The password goes only into the POST body: never a URL, message or audit
    row."""
    perm = "accounts.reset_password"
    if not has_permission(request.user, perm):
        return denied(request, perm)
    target = get_object_or_404(User, pk=user_id)
    return command_page(
        request,
        permission=perm,
        form_class=ResetPasswordForm,
        template="settings/user_password.html",
        call=lambda actor, d: account_services.reset_password(
            actor, user_id=target.pk, password=d["password"]
        ),
        ok=f"Temporary password set for {target.username}. They must choose their own at their next sign-in.",
        success_url=f"/settings/users/{target.pk}/",
        context={"target": target},
    )
