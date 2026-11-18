"""Operations command center JSON API (Slice 5, Engineer D). Mounted at /api/ops/ (config/urls.py).

Mirrors the pages exactly: the same data builders (operations.views), the same snapshot (`as_of`), the same
ordering, the same continuation token (`cursor`) and the same `readiness_coverage`. GET only, read only.
A user without the permission gets 403 and no data; a nested transaction fails closed with 503.
"""

from datetime import datetime
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.urls import path

from fleet.services import demo_mode
from operations import views as v


def _ok(data: dict[str, Any]) -> JsonResponse:
    body = v.jsonable(data)
    body["synthetic_demo"] = demo_mode()
    return JsonResponse(body)


def _bad(exc: v.BadParam) -> JsonResponse:
    return JsonResponse({"detail": str(exc)}, status=400)


@v.ops_view("operations.read", json_api=True)
def overview(request: HttpRequest, at: datetime) -> JsonResponse:
    return _ok(v.overview_data(at))


@v.ops_view("operations.read", json_api=True)
def exceptions(request: HttpRequest, at: datetime) -> JsonResponse:
    try:
        return _ok(v.exceptions_data(at, request.GET.get("cursor")))
    except v.BadParam as exc:
        return _bad(exc)


@v.ops_view("operations.read", json_api=True)
def incidents(request: HttpRequest, at: datetime) -> JsonResponse:
    return _ok(v.incidents_data(at))


@v.ops_view("operations.read", json_api=True)
def timeline(request: HttpRequest, at: datetime) -> JsonResponse:
    try:
        return _ok(v.timeline_data(at, v.parse_window(request.GET.get("window_min"))))
    except v.BadParam as exc:
        return _bad(exc)


@v.ops_view("operations.metrics", json_api=True)
def metrics(request: HttpRequest, at: datetime) -> JsonResponse:
    try:
        start, end = v.parse_metrics_window(at, request.GET.get("start"), request.GET.get("end"))
    except v.BadParam as exc:
        return _bad(exc)
    return _ok(v.metrics_data(at, start, end))


app_name = "operations-api"

urlpatterns = [
    path("overview", overview, name="overview"),
    path("exceptions", exceptions, name="exceptions"),
    path("incidents", incidents, name="incidents"),
    path("timeline", timeline, name="timeline"),
    path("metrics", metrics, name="metrics"),
]
