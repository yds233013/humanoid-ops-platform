"""v2 03D-2: the offline field screen's scripts, served same-origin (contract 15.1, 15.10; 03d_modules.md).

    GET /field/queue.js     the queue module (03D-1)
    GET /field/run.js       the run page module (03D-3)
    GET /execution/sw.js    the service worker (03D-3), scope /execution/

The project has no staticfiles app (settings.INSTALLED_APPS), so this tiny view serves exactly these three
files from ``execution/field_static/``. Each URL is bound to one fixed file name: the request path never
names a file, so nothing outside the whitelist can be reached (no path traversal). Same origin, so the CSP
``script-src 'self'`` allows them with no new inline script.

Headers: ``Content-Type: text/javascript; charset=utf-8`` and ``Cache-Control: no-cache`` (the browser checks
for a new version every time). The service worker also gets ``Service-Worker-Allowed: /execution/``. The
platform's SecurityHeadersMiddleware adds ``no-store, private`` for a signed-in user, as on every response;
the files hold no run data either way (15.10 item 1).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed

ROOT = Path(__file__).resolve().parent / "field_static"
FILES = frozenset({"queue.js", "run.js", "sw.js"})
CONTENT_TYPE = "text/javascript; charset=utf-8"
SW_SCOPE = "/execution/"


def _read(name: str) -> bytes:
    if name not in FILES:
        raise Http404("Not found")
    path = (ROOT / name).resolve()
    if path.parent != ROOT or not path.is_file():
        raise Http404("Not found")
    return path.read_bytes()


def asset(name: str, *, service_worker: bool = False) -> Callable[[HttpRequest], HttpResponse]:
    if name not in FILES:
        raise ValueError(f"{name} is not a field asset")

    def view(request: HttpRequest) -> HttpResponse:
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])
        resp = HttpResponse(_read(name), content_type=CONTENT_TYPE)
        resp["Cache-Control"] = "no-cache"
        resp["X-Content-Type-Options"] = "nosniff"
        if service_worker:
            resp["Service-Worker-Allowed"] = SW_SCOPE
        return resp

    view.__name__ = f"field_asset_{name.replace('.', '_')}"
    return view
