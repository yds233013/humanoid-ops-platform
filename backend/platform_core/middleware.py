import uuid
from collections.abc import Callable
from contextvars import ContextVar
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.utils.cache import patch_cache_control

current_request_id: ContextVar[str] = ContextVar("current_request_id", default="")


class RequestIdMiddleware:
    """Attach a request id (from X-Request-ID or generated) so audit rows and logs can be correlated."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = current_request_id.set(rid[:64])
        try:
            response = self.get_response(request)
        finally:
            current_request_id.reset(token)
        response["X-Request-ID"] = rid[:64]
        return response


def _signed_in(request: HttpRequest) -> bool:
    # DRF views set request.user to None for anonymous callers (UNAUTHENTICATED_USER = None)
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated)


class AnonymousHtmlRedirectMiddleware:
    """Anonymous browser users who hit a page that refuses them get the login page, not a bare 403.

    Only GET/HEAD on non-API paths whose view answered 403 are redirected (to /login/?next=<path>). JSON APIs
    (/api/...) keep their 401/403 JSON; POSTs are untouched (a CSRF failure must stay visible). Permissions
    are not changed: the view still refused; only the presentation to a signed-out visitor changes.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if (
            response.status_code == 403
            and request.method in ("GET", "HEAD")
            and not request.path.startswith("/api/")
            and not _signed_in(request)
        ):
            return HttpResponseRedirect(
                f"{settings.LOGIN_URL}?{urlencode({'next': request.get_full_path()})}"
            )
        return response


class SecurityHeadersMiddleware:
    """Content-Security-Policy (when settings.CSP_ENABLED) and Cache-Control: no-store for signed-in users."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if settings.CSP_ENABLED and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = settings.CONTENT_SECURITY_POLICY
        if _signed_in(request):
            patch_cache_control(response, no_store=True, private=True)
        return response


class FriendlyMethodNotAllowedMiddleware:
    """A browser GET of a form-only (POST) address gets a readable page, not Django's bare 405 (01C D5).

    Only non-API GET/HEAD requests answered with an empty 405 are replaced; the status (405) and the Allow
    header are kept, so nothing about what the address accepts changes. Listed after the security-header and
    display-zone middleware so the replacement page still gets the CSP, no-store and the workspace zone.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if (
            response.status_code == 405
            and request.method in ("GET", "HEAD")
            and not request.path.startswith("/api/")
            and not response.content
        ):
            from django.shortcuts import render

            page = render(request, "405.html", status=405)
            if "Allow" in response:
                page["Allow"] = response["Allow"]
            return page
        return response


# ------------------------------------------------------------------ v2 01D: demo refresh maintenance page
DEMO_BUILD_MARKER = "demo_build_completed_at"  # written LAST by a successful build_demo_workspace
DEMO_REFRESH_SKIP = ("/healthz", "/static/")
DEMO_REFRESHING_TEXT = "The demo is being refreshed. Please try again in a minute."
DEMO_REFRESHING_PAGE = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Demo being refreshed · Ops Platform</title>"
    '<style>body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;'
    "color:#14181F;background:#F5F6F8}main{max-width:560px;margin:15vh auto;padding:0 16px}"
    "h1{font-size:1.375rem}</style></head>"
    f"<body><main><h1>One moment</h1><p>{DEMO_REFRESHING_TEXT}</p><p>The demo starts fresh each day: changes "
    "made by visitors are not kept, and you will need to sign in again.</p></main></body></html>"
)


def demo_build_complete() -> bool:
    """True when the demo workspace's completion marker exists (one indexed read). Any database error counts
    as "not complete": the maintenance page is the safe answer while a refresh runs."""
    from django.db import connection

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM fleet_deploymentsetting WHERE key = %s", [DEMO_BUILD_MARKER])
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 - a table being truncated or a lock timeout: still refreshing
        return False


class DemoRefreshMiddleware:
    """Only when DEMO_WORKSPACE=1: while the demo build's completion marker is absent (a refresh is running,
    or the last one failed), every request except /healthz and /static/ gets 503 with a short page
    (Retry-After: 60; JSON for /api/). It never touches /healthz, which keeps reporting the database and
    migrations, so the platform does not restart the service during a refresh.
    Without DEMO_WORKSPACE=1 it does nothing and reads nothing."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        import os

        if os.environ.get("DEMO_WORKSPACE") != "1" or request.path.startswith(DEMO_REFRESH_SKIP):
            return self.get_response(request)
        if demo_build_complete():
            return self.get_response(request)
        if request.path.startswith("/api/"):
            from django.http import JsonResponse

            response: HttpResponse = JsonResponse(
                {"error": "demo_refreshing", "detail": DEMO_REFRESHING_TEXT}, status=503
            )
        else:
            response = HttpResponse(DEMO_REFRESHING_PAGE, status=503, content_type="text/html; charset=utf-8")
        response["Retry-After"] = "60"
        response["Cache-Control"] = "no-store"
        if settings.CSP_ENABLED:
            response["Content-Security-Policy"] = settings.CONTENT_SECURITY_POLICY
        return response
