"""TEST-ONLY URL configuration for the 03D-3 browser tests (never shipped).

It is the real ``config.urls`` plus stand-ins for the pieces other 03D engineers own, added ONLY when the real
route does not resolve in this build:

* ``/field/<name>.js`` and ``/execution/sw.js`` (03D-2 owns the serving view): served from
  ``execution/field_static/``; ``queue.js`` falls back to ``tests/browser/stub_queue.js`` while the real file
  (03D-1) is missing or empty;
* ``GET /api/field/runs/<code>/snapshot`` (03D-2): a small stand-in that follows 03d_modules.md.

After the three branches merge, every real route resolves first and these stand-ins are never reached; the
``FIELD_PIECES`` dict says which ones the current run used.
"""

from __future__ import annotations

from pathlib import Path

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.urls import Resolver404, path, resolve

from config.urls import urlpatterns as real_patterns

BACKEND = Path(__file__).resolve().parents[2]
STATIC = BACKEND / "execution" / "field_static"
STUB_QUEUE = Path(__file__).with_name("stub_queue.js")


def _real(url: str) -> bool:
    try:
        resolve(url, urlconf="config.urls")
        return True
    except Resolver404:
        return False


def _js(body: bytes, *, worker: bool = False) -> HttpResponse:
    resp = HttpResponse(body, content_type="text/javascript; charset=utf-8")
    resp["Cache-Control"] = "no-cache"
    if worker:
        resp["Service-Worker-Allowed"] = "/execution/"
    return resp


def queue_is_stub() -> bool:
    real = STATIC / "queue.js"
    return not real.exists() or not real.read_text().strip()


def field_js(request: HttpRequest, name: str) -> HttpResponse:
    if name == "queue" and queue_is_stub():
        return _js(STUB_QUEUE.read_bytes())
    f = STATIC / f"{name}.js"
    if name not in ("queue", "run") or not f.exists():
        raise Http404
    return _js(f.read_bytes())


# A test may set this to publish a "new version" of the worker (the update tests, 15.10 item 4).
SW_VERSION_SUFFIX = ""


def service_worker(request: HttpRequest) -> HttpResponse:
    body = (STATIC / "sw.js").read_text()
    if SW_VERSION_SUFFIX:
        body = body.replace('const VERSION = "', f'const VERSION = "{SW_VERSION_SUFFIX}-', 1)
    return _js(body.encode(), worker=True)


def snapshot(request: HttpRequest, code: str) -> JsonResponse:
    """Stand-in for the 03D-2 snapshot API (03d_modules.md): same shape, derived from the domain records."""
    from accounts.models import RoleGrant
    from execution import field_ledger, field_sync
    from execution.incidents import session_state
    from execution.models import CustodyEvent, Interruption, SessionEvent, SiteCheck, TripEvent
    from planning.models import Person

    user = request.user
    if not user.is_authenticated:
        return JsonResponse({"error": "NOT_AUTHENTICATED"}, status=401)
    run = field_sync.run_for_sync(user, code)
    if run is None:
        return JsonResponse({"error": "NOT_YOUR_RUN", "message": "This run is not yours."}, status=403)
    trips = {t.kind: t.recorded_at for t in TripEvent.objects.filter(run=run)}
    custody = list(CustodyEvent.objects.filter(run=run).order_by("seq"))
    to_site = next((c.recorded_at for c in custody if c.to_kind == "site"), None)
    back = next((c.recorded_at for c in custody if c.from_kind == "site" and c.to_kind == "vehicle"), None)
    check = SiteCheck.objects.filter(run=run).order_by("-recorded_at").first()
    start = SessionEvent.objects.filter(run=run, kind="start").order_by("seq").first()

    def step(command: str, label: str, klass: str, at) -> dict:
        return {
            "command": command,
            "label": label,
            "class": klass,
            "done": at is not None,
            "recorded_at": field_sync.iso_local(at),
        }

    steps = [
        step("trip.arrived_site", "Arrived at the home", "C", trips.get("arrived_site")),
        step("custody.handover_to_site", "Hand the robot over to the site", "A", to_site),
        step("site_check.record", "Site check", "C", check.recorded_at if check else None),
        step("session.start", "Start the session", "A", start.recorded_at if start else None),
        step("custody.back_to_vehicle", "Robot back in the vehicle", "C", back),
        step("trip.departed_site", "Left the home", "C", trips.get("departed_site")),
        step("trip.arrived_hub", "Back at the hub", "C", trips.get("arrived_hub")),
    ]
    open_int = Interruption.objects.filter(run=run, resolution__isnull=True).order_by("id").first()
    paused = session_state(run.pk) == "paused"
    led = field_ledger.state(run.pk)
    crew = Person.objects.select_related("user").filter(pk__in=[run.revision.lead_id, run.revision.second_id])
    leads = RoleGrant.objects.filter(role="ops_lead").select_related("user").order_by("user__username")
    name = lambda u: u.get_full_name() or u.get_username()  # noqa: E731
    return JsonResponse(
        {
            "snapshot_version": 1,
            "fetched_at": field_sync.iso_local(field_sync._db_now()),
            "user": {"id": user.pk, "name": name(user)},
            "run": {
                "code": run.code,
                "state": "open",
                "hub": "",
                "robot": run.robot.code,
                "home_label": "Home (fictional)",
            },
            "steps": steps,
            "crew": [{"id": p.user_id, "name": p.label} for p in crew],
            "ops_leads": [{"name": name(g.user)} for g in leads],
            "protective": {
                "open": bool(open_int or paused),
                "kind": "interruption" if open_int else ("pause" if paused else None),
                "since": field_sync.iso_local(open_int.recorded_at) if open_int else None,
                "message": open_int.reason if open_int else "",
            },
            "ledger": {
                "device_id": request.GET.get("device", ""),
                "generation": led["generation"],
                "next_expected_seq": led["next_expected_seq"],
                "last_decided_seq": led["last_decided_seq"],
                "last_decided_outcome": None,
            },
            "readiness_code": None,
        },
        headers={"Cache-Control": "no-store"},
    )


FIELD_PIECES = {
    "serving_view": "real" if _real("/field/run.js") else "test stand-in",
    "service_worker_route": "real" if _real("/execution/sw.js") else "test stand-in",
    "snapshot_api": "real" if _real("/api/field/runs/EX-0001/snapshot") else "test stand-in",
    "queue_js": "test stub" if queue_is_stub() else "real",
}

urlpatterns = list(real_patterns)
if FIELD_PIECES["serving_view"] != "real":
    urlpatterns.append(path("field/<str:name>.js", field_js))
if FIELD_PIECES["service_worker_route"] != "real":
    urlpatterns.append(path("execution/sw.js", service_worker))
if FIELD_PIECES["snapshot_api"] != "real":
    urlpatterns.append(path("api/field/runs/<str:code>/snapshot", snapshot))


def publish_new_service_worker(monkeypatch, suffix: str = "next") -> None:
    """Make the server answer /execution/sw.js with a new VERSION (a test of the 15.10 item 4 update path).
    Uses the real 03D-2 serving view when it is in this build, else the stand-in above."""
    try:
        from execution import field_assets  # 03D-2
    except ImportError:
        monkeypatch.setattr(__import__(__name__, fromlist=["x"]), "SW_VERSION_SUFFIX", suffix)
        return
    original = field_assets._read

    def _read(name: str) -> bytes:
        body = original(name)
        if name == "sw.js":
            body = body.replace(b'const VERSION = "', f'const VERSION = "{suffix}-'.encode(), 1)
        return body

    monkeypatch.setattr(field_assets, "_read", _read)
