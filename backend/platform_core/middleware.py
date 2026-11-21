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
