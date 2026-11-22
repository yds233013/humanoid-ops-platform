"""Forced first-login password change (v2 01D, P0).

While the signed-in user's PasswordState.must_change is set, every request is turned away except the
change-password page, signing out, the health checks and static files: pages get a redirect to the change
page, the JSON API (/api/...) gets a 403 with a plain reason. No operational page or API is reachable before
the change. The redirect carries no query string (nothing about the password can leak into a URL).
"""

from collections.abc import Callable
from urllib.parse import unquote

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse

from accounts.models import must_change_password

CHANGE_URL = "/account/password/"
# Exact paths only: a look-alike such as /account/passwordX, /login/../ops/ or /healthzX is gated like any
# other page. The one prefix is STATIC_URL (static files carry no data and run no view).
ALLOWED_PATHS = frozenset(
    {
        CHANGE_URL,
        "/logout/",
        "/login/",  # signing in as someone else; the new session is checked on its next request
        "/api/auth/logout",
        "/healthz",
        "/api/health",
    }
)


def _static_prefix() -> str | None:
    url = getattr(settings, "STATIC_URL", None)
    if not url or not str(url).startswith("/"):
        return None  # unset or on another host: nothing here is static
    url = str(url)
    return url if url.endswith("/") else url + "/"


def is_allowed(path: str) -> bool:
    if path in ALLOWED_PATHS:
        return True
    prefix = _static_prefix()
    path = unquote(path)  # request.path_info is already decoded; decode again so %2f tricks cannot hide ".."
    return (
        prefix is not None
        and prefix != "/"
        and path.startswith(prefix)
        and ".." not in path.split("/")
        and "\\" not in path
    )


API_REASON = "Change your temporary password first: sign in on the web and open /account/password/."


class ForcedPasswordChangeMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path_info
        if not is_allowed(path) and must_change_password(getattr(request, "user", None)):
            if path.startswith("/api/"):
                return JsonResponse({"error": "password_change_required", "reason": API_REASON}, status=403)
            return HttpResponseRedirect(CHANGE_URL)
        return self.get_response(request)
