"""Health check, error pages and first-run setup (v2 01A)."""

import logging
from typing import Any

from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from platform_core.bootstrap import BootstrapRefused, bootstrap_first_admin, setup_open

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ health
@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    """200 only if the database answers and all migrations are applied, else 503. No auth, no secrets."""
    database = "ok"
    migrations = "ok"
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            migrations = "pending"
    except DatabaseError:
        log.exception("healthz: database check failed")
        database = "unreachable"
        migrations = "unknown"
    ok = database == "ok" and migrations == "ok"
    response = JsonResponse(
        {"status": "ok" if ok else "unavailable", "database": database, "migrations": migrations},
        status=200 if ok else 503,
    )
    response["Cache-Control"] = "no-store"
    return response


# ------------------------------------------------------------------ error pages
def _wants_json(request: HttpRequest) -> bool:
    return request.path.startswith("/api/")


def _render_error(request: HttpRequest, template: str, status: int, code: str) -> HttpResponse:
    if _wants_json(request):
        return JsonResponse({"error": code}, status=status)
    return render(request, template, status=status)


def permission_denied(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    return _render_error(request, "403.html", 403, "permission_denied")


def page_not_found(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    return _render_error(request, "404.html", 404, "not_found")


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    return _render_error(request, "403_csrf.html", 403, "csrf_failed")


def server_error(request: HttpRequest) -> HttpResponse:
    """500 page. If rendering the full page fails too (e.g. the database is down), fall back to plain HTML."""
    if _wants_json(request):
        return JsonResponse({"error": "server_error"}, status=500)
    try:
        return render(request, "500.html", status=500)
    except Exception:  # noqa: BLE001 - last resort, must never raise
        return HttpResponse(
            "<!doctype html><title>Something went wrong</title><h1>Something went wrong</h1>"
            "<p>We could not complete that request. Please try again in a minute.</p>"
            '<p><a href="/">Back to the start page</a></p>',
            status=500,
            content_type="text/html",
        )


# ------------------------------------------------------------------ first-run setup
@csrf_protect
@require_http_methods(["GET", "HEAD", "POST"])
def setup(request: HttpRequest) -> HttpResponse:
    """Create the first admin with the one-time bootstrap token. 404 when disabled or already initialized."""
    if not setup_open():
        return page_not_found(request)
    ctx: dict[str, Any] = {"error": "", "username": "", "workspace_name": ""}
    if request.method == "POST":
        ctx["username"] = request.POST.get("username", "")
        ctx["workspace_name"] = request.POST.get("workspace_name", "")
        try:
            bootstrap_first_admin(
                token=request.POST.get("token", ""),
                username=request.POST.get("username", ""),
                password=request.POST.get("password", ""),
                workspace_name=request.POST.get("workspace_name", ""),
            )
        except BootstrapRefused as refusal:
            if refusal.reason in ("initialized", "disabled"):
                return page_not_found(request)
            ctx["error"] = refusal.message
            return render(request, "setup.html", ctx, status=403 if refusal.reason == "bad_token" else 400)
        return redirect("/login/")
    return render(request, "setup.html", ctx)
