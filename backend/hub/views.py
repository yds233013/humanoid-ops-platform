"""The Hub screen (v2 02A, journey J8 shell): the hub work queue.

GET /hub/ refreshes the derived tasks (one set-based statement, hub.derive) and shows the queue: counts by
type, overdue and unassigned, "Assigned to me", by owner; a filter picks the list below. Every row says
the next action and links to the real workflow when the viewer may open it. Nothing here changes a
safety fact.
POST endpoints spend the one-time form token first, then call ONE command of hub.services.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.permissions import active_roles
from accounts.roles import PERMISSIONS
from hub import derive
from hub import services as hs
from hub.api import parse_version
from hub.models import FINAL, TYPES, WorkOrder
from planning.models import Person
from platform_core.commands import CommandError
from platform_core.interaction import UIForm, command_page, denied, explain, spend_form_token
from platform_core.timezones import workspace_zone

TYPE_WORDS = dict(TYPES)
MANUAL_TYPES = [(t, w) for t, w in TYPES if t not in ("hold_work", "equipment_failure")]
RECENT_DAYS = 7
RECENT_LIMIT = 30

# What to do, per type, and which page it happens on (the permission needed to open that page).
WORKFLOW: dict[str, tuple[str, str | None, str]] = {
    "inspection_due": ("Record the periodic inspection", "fleet.read", "inspection form"),
    "hold_work": (
        "Do the work; the hold is released only through its approvals on the hold page",
        "fleet.read",
        "hold page",
    ),
    "equipment_failure": (
        "Fix or replace the kit, then record a new equipment check for the visit",
        "readiness.read",
        "visit readiness",
    ),
    "charging": (
        "Charge the robot and record the session; it still needs its battery check before departure",
        "hub.view",
        "charging page",
    ),
    "maintenance_due": ("Do the maintenance", None, ""),
    "kit_low": ("Restock it and record what arrives as received on the Stock page", "hub.view", "stock page"),
    "other": ("Do the work described", None, ""),
}


def _allowed(roles: set[str], perm: str | None) -> bool:
    return perm is None or bool(roles & PERMISSIONS.get(perm, set()))


class Viewer:
    def __init__(self, request: HttpRequest) -> None:
        self.roles = active_roles(request.user)
        self.person = hs.person_of(request.user)
        self.all = _allowed(self.roles, "hub.view_all")
        self.work = _allowed(self.roles, hs.WORK)
        self.manager = _allowed(self.roles, hs.MANAGE)
        self.claim = _allowed(self.roles, hs.CLAIM)
        self.user_id = request.user.pk

    def mine(self, t: WorkOrder) -> bool:
        return self.person is not None and t.owner_id == self.person.pk

    def actions(self, t: WorkOrder) -> list[str]:
        """The transitions this viewer may ask for (the command re-checks everything)."""
        if not self.work or t.status in FINAL:
            return []
        out: list[str] = []
        mine = self.mine(t)
        if t.status == "open" and (self.manager or (self.claim and self.person is not None)):
            out.append("take" if not self.manager else "assign")
        if t.status == "assigned" and self.manager:
            out.append("assign")
        if t.status == "assigned" and (mine or self.manager):
            out.append("start")
        if t.status == "in_progress" and (mine or self.manager):
            out.append("complete")
        own_manual = t.created_by_id == self.user_id and t.source_kind == "manual" and mine
        if self.manager or own_manual or (t.source_cleared_at is not None and mine):
            out.append("cancel")
        return out


def _next_step(t: WorkOrder, mine: bool = False) -> str:
    owner = "You" if mine else (t.owner.label if t.owner_id and t.owner else "")
    if t.source_cleared_at is not None:
        return "Resolved by the underlying record. Close this task."
    what = WORKFLOW[t.type][0]
    if t.status == "open":
        return f"Needs an owner. Then: {what.lower()}."
    if t.status == "assigned":
        return f"{owner} {'start' if mine else 'starts'} it. Then: {what.lower()}."
    if t.status == "in_progress":
        return f"{owner}: {what.lower()}, then mark it done."
    return ""


def _row(t: WorkOrder, v: Viewer, now: datetime) -> dict[str, Any]:
    _what, perm, page = WORKFLOW[t.type]
    link = t.link_url if t.link_url and _allowed(v.roles, perm) else ""
    return {
        "t": t,
        "type": TYPE_WORDS[t.type],
        "overdue": t.due_at is not None and t.due_at < now and t.status not in FINAL,
        "next": _next_step(t, v.mine(t)),
        "link": link,
        "link_text": f"Open the {page}" if page else "",
        "link_hidden": bool(t.link_url and not link),
        "page": page,
        "actions": v.actions(t),
        "mine": v.mine(t),
    }


FILTERS = ("all", "mine", "overdue", "unassigned", "resolved")


@login_required
@require_GET
def hub_page(request: HttpRequest) -> HttpResponse:
    v = Viewer(request)
    if not _allowed(v.roles, "hub.view"):
        if _allowed(v.roles, "operations.read"):
            return redirect("/ops/closeout/")  # the close-out stays under Hub for its readers
        return denied(request, "hub.view")
    derive.refresh()
    now = timezone.now()
    qs = WorkOrder.objects.select_related("robot", "owner__user", "visit").filter(
        Q(status__in=["open", "assigned", "in_progress"])
        | Q(finished_at__gte=now - timedelta(days=RECENT_DAYS))
    )
    if not v.all:  # a field operator sees their own tasks only (contract 8.6)
        qs = qs.filter(owner=v.person) if v.person is not None else qs.none()
    tasks = list(qs.order_by("due_at", "id"))
    active = [t for t in tasks if t.status not in FINAL]
    recent = sorted((t for t in tasks if t.status in FINAL), key=lambda t: t.finished_at or now, reverse=True)
    by_type = Counter(t.type for t in active)
    owners = Counter((t.owner.code, t.owner.label) for t in active if t.owner_id and t.owner)
    view = request.GET.get("view", "all")
    view = view if view in FILTERS or view.startswith(("type:", "owner:")) else "all"
    rows = [_row(t, v, now) for t in active]
    groups = {
        "mine": [r for r in rows if r["mine"]],
        "overdue": [r for r in rows if r["overdue"]],
        "unassigned": [r for r in rows if r["t"].owner_id is None],
        "resolved": [r for r in rows if r["t"].source_cleared_at is not None],
    }
    if view in groups:
        shown = groups[view]
    elif view.startswith("type:"):
        shown = [r for r in rows if r["t"].type == view[5:]]
    elif view.startswith("owner:"):
        shown = [r for r in rows if r["t"].owner_id and r["t"].owner.code == view[6:]]
    else:
        shown = rows
    return render(
        request,
        "hub/queue.html",
        {
            "rows": shown,
            "view": view,
            "counts": {k: len(g) for k, g in groups.items()},
            "total": len(rows),
            "types": [(t, w, by_type.get(t, 0)) for t, w in TYPES],
            "owners": sorted(((code, label, n) for (code, label), n in owners.items()), key=lambda x: x[0]),
            "recent": [_row(t, v, now) for t in recent[:RECENT_LIMIT]],
            "viewer": v,
            "own_only": not v.all,
            "tz_now": now.astimezone(workspace_zone()),
        },
    )


# ------------------------------------------------------------------ create
class TaskForm(UIForm):
    type = forms.ChoiceField(label="Type", choices=MANUAL_TYPES)
    title = forms.CharField(max_length=160, label="What needs doing")
    robot = forms.CharField(
        max_length=12, required=False, label="Robot (optional)", help_text="A robot code, for example NB-011."
    )
    due_at = forms.DateTimeField(
        required=False,
        label="Due (optional)",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="In the workspace time zone.",
    )
    owner = forms.ChoiceField(required=False, label="Owner")
    note = forms.CharField(max_length=300, required=False, label="Note (optional)")

    def __init__(self, *args: Any, owner_choices: list[tuple[str, str]] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.fields["owner"].choices = owner_choices or [("", "Nobody yet")]  # type: ignore[attr-defined]

    def clean_due_at(self) -> datetime | None:
        value: datetime | None = self.cleaned_data.get("due_at")
        if value is None:
            return None
        naive = value.replace(tzinfo=None)
        return naive.replace(tzinfo=workspace_zone())


def _owner_choices(v: Viewer) -> list[tuple[str, str]]:
    if v.manager:
        people = Person.objects.select_related("user").order_by("code")
        return [("", "Nobody yet")] + [(p.code, p.label) for p in people]
    if v.person is None:
        return [("", "Nobody yet")]
    me = [(v.person.code, f"Me ({v.person.label})")]
    return ([("", "Nobody yet")] + me) if v.claim else me


@login_required
def task_new(request: HttpRequest) -> HttpResponse:
    v = Viewer(request)
    return command_page(
        request,
        permission=hs.WORK,
        form_class=TaskForm,
        form_kwargs={"owner_choices": _owner_choices(v)},
        template="hub/task_new.html",
        call=lambda actor, d: hs.create_task(
            actor,
            type=d["type"],
            title=d["title"],
            robot_code=d["robot"].strip().upper() or None,
            due_at=d["due_at"],
            owner_code=d["owner"] or None,
            note=d["note"],
        ),
        ok=lambda r: f"Task WO-{int(r['entity_id']):04d} added.",
        success_url="/hub/",
    )


# ------------------------------------------------------------------ one task
@login_required
@require_GET
def task_detail(request: HttpRequest, task_id: int) -> HttpResponse:
    v = Viewer(request)
    if not _allowed(v.roles, "hub.view"):
        return denied(request, "hub.view")
    t = get_object_or_404(
        WorkOrder.objects.select_related("robot", "owner__user", "visit", "created_by"), pk=task_id
    )
    if not v.all and not v.mine(t):
        return denied(request, "hub.view_all")
    people = (
        [(p.code, p.label) for p in Person.objects.select_related("user").order_by("code")]
        if v.manager
        else []
    )
    return render(
        request,
        "hub/task.html",
        {"r": _row(t, v, timezone.now()), "t": t, "people": people, "viewer": v},
    )


ACTIONS = {"take", "assign", "start", "complete", "cancel"}


@login_required
@require_POST
def task_action(request: HttpRequest, task_id: int, action: str) -> HttpResponse:
    back = request.POST.get("back", "")
    back = back if back.startswith("/hub/") and "//" not in back else "/hub/"
    v = Viewer(request)
    if action not in ACTIONS:
        return redirect(back)
    if not v.work:
        return denied(request, hs.WORK)
    if not spend_form_token(request):
        return redirect(back)
    version = parse_version(request.POST.get("version"))
    if version is None:  # EM 02B hardening: never act on a task without the version the form showed
        messages.error(
            request, "Refused (check the form): the page was out of date. Reload it; nothing was changed."
        )
        return redirect(back)
    try:
        if action == "take":
            if v.person is None:
                raise CommandError("You need a person record to take a task; ask the ops lead")
            hs.assign_task(request.user, task_id=task_id, owner_code=v.person.code, version=version)
            done = "You took the task."
        elif action == "assign":
            hs.assign_task(
                request.user, task_id=task_id, owner_code=request.POST.get("owner", ""), version=version
            )
            done = "Task assigned."
        elif action == "start":
            hs.start_task(request.user, task_id=task_id, version=version)
            done = "Task started."
        elif action == "complete":
            hs.complete_task(
                request.user, task_id=task_id, version=version, note=request.POST.get("note", "")
            )
            done = "Task marked done. This records the task only; the underlying record is unchanged."
        else:
            hs.cancel_task(
                request.user, task_id=task_id, version=version, reason=request.POST.get("reason", "")
            )
            done = "Task cancelled."
    except CommandError as exc:
        text = explain(exc, hs.WORK) if str(exc).startswith("Not allowed:") else str(exc).rstrip(".") + "."
        messages.error(request, f"Refused ({exc.code.replace('_', ' ')}): {text} Nothing was changed.")
        return redirect(back)
    messages.success(request, done)
    return redirect(back)
