"""Forced first-login password change (v2 01D, P0).

While the signed-in user's PasswordState.must_change is set, every request is turned away except the
change-password page, signing out, the health checks and static files: pages get a redirect to the change
page, the JSON API (/api/...) gets a 403 with a plain reason. No operational page or API is reachable before
the change. The redirect carries no query string (nothing about the password can leak into a URL).
"""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse

from accounts.models import must_change_password

CHANGE_URL = "/account/password/"
ALLOWED_PREFIXES = (
    CHANGE_URL,
    "/logout/",
    "/login/",  # signing in as someone else; the new session is checked on its next request
    "/api/auth/logout",
    "/healthz",
    "/api/health",
    "/static/",
)
API_REASON = "Change your temporary password first: sign in on the web and open /account/password/."


class ForcedPasswordChangeMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path
        if not path.startswith(ALLOWED_PREFIXES) and must_change_password(getattr(request, "user", None)):
            if path.startswith("/api/"):
                return JsonResponse({"error": "password_change_required", "reason": API_REASON}, status=403)
            return HttpResponseRedirect(CHANGE_URL)
        return self.get_response(request)
