"""JSON API of the hub work queue (v2 02A): the same commands as the screens; refusals are JSON."""

from typing import Any

from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.permissions import has_permission
from hub import services as hs
from hub.models import FINAL, WorkOrder


def _task(t: WorkOrder) -> dict[str, Any]:
    return {
        "id": t.pk,
        "code": t.code,
        "type": t.type,
        "title": t.title,
        "robot": t.robot.code if t.robot_id and t.robot else None,
        "owner": t.owner.code if t.owner_id and t.owner else None,
        "status": t.status,
        "due_at": t.due_at.isoformat() if t.due_at else None,
        "source_kind": t.source_kind,
        "source_ref": t.source_ref,
        "source_cleared": t.source_cleared_at is not None,
        "link_url": t.link_url,
        "version": t.version,
    }


@api_view(["GET", "POST"])
def tasks(request: Request) -> Response:
    if request.method == "POST":
        d = request.data
        raw_due = d.get("due_at")
        result = hs.create_task(
            request.user,
            type=str(d.get("type", "")),
            title=str(d.get("title", "")),
            robot_code=d.get("robot") or None,
            due_at=parse_datetime(raw_due) if raw_due else None,
            owner_code=d.get("owner") or None,
            note=str(d.get("note", "")),
        )
        return Response(result, status=201)
    if not has_permission(request.user, "hub.view"):
        return Response({"error": "permission_denied", "detail": "Not allowed: hub.view"}, status=403)
    qs = WorkOrder.objects.select_related("robot", "owner").exclude(status__in=FINAL).order_by("due_at", "id")
    if not has_permission(request.user, "hub.view_all"):
        me = hs.person_of(request.user)
        qs = qs.filter(owner=me) if me is not None else qs.none()
    return Response({"tasks": [_task(t) for t in qs]})


@api_view(["POST"])
def task_action(request: Request, task_id: int, action: str) -> Response:
    d = request.data
    raw = d.get("version")
    version = int(str(raw)) if raw not in (None, "") else None
    if action == "assign":
        result = hs.assign_task(
            request.user, task_id=task_id, owner_code=str(d.get("owner", "")), version=version
        )
    elif action == "start":
        result = hs.start_task(request.user, task_id=task_id, version=version)
    elif action == "complete":
        result = hs.complete_task(request.user, task_id=task_id, version=version, note=str(d.get("note", "")))
    elif action == "cancel":
        result = hs.cancel_task(
            request.user, task_id=task_id, version=version, reason=str(d.get("reason", ""))
        )
    else:
        return Response({"error": "not_found"}, status=404)
    return Response(result)
