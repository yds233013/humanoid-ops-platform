import uuid
from collections.abc import Callable
from contextvars import ContextVar

from django.http import HttpRequest, HttpResponse

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
