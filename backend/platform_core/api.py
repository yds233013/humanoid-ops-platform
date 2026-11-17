from typing import Any

from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from accounts.permissions import has_permission
from platform_core.commands import CommandError
from platform_core.models import AuditEvent


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, CommandError):
        return Response({"error": exc.code, "detail": str(exc)}, status=exc.status)
    return drf_exception_handler(exc, context)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request: Request) -> Response:
    with connection.cursor() as cur:
        cur.execute("select 1")
    return Response({"status": "ok"})


@api_view(["GET"])
def audit_events(request: Request) -> Response:
    if not has_permission(request.user, "audit.read"):
        return Response({"error": "permission_denied"}, status=status.HTTP_403_FORBIDDEN)
    qs = AuditEvent.objects.order_by("-id")[:200]
    return Response(
        [
            {
                "id": e.id,
                "occurred_at": e.occurred_at,
                "actor_id": e.actor_id,
                "command": e.command,
                "outcome": e.outcome,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "payload": e.payload,
                "result": e.result,
                "reason": e.reason,
                "request_id": e.request_id,
            }
            for e in qs
        ]
    )
