"""Operations command center pages (Slice 5, Engineer D). Contract: docs/05, sections 2-5.

READ ONLY. Every page and API response is built inside ONE `operations.snapshot.snapshot_view` snapshot
(REPEATABLE READ READ ONLY, permission checked inside it) and shows its "as of" database time. The read
models (operations.readmodels / operations.exceptions / operations.metrics, Engineers B and C) are imported
inside the data builders, and the same builders feed the HTML pages and the JSON API (operations.api), so both
always show the same rows in the same order with the same `as_of` and the same `readiness_coverage`.

There are no write forms and no write endpoints: every row links to the existing page where the work is done.
Wording: nothing here says a robot is cleared, safe or ready to deploy; no status is a departure decision.

Transactions: the views opt out of ATOMIC_REQUESTS (`non_atomic_requests`) so the snapshot is always the
outermost transaction; if a caller still wraps the view in a transaction, read_snapshot refuses
(SnapshotNotOutermost) and the view fails closed with a 503 and no data, never a page built outside the
snapshot.
"""

import importlib
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import wraps
from typing import Any

from django.core import signing
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from accounts.permissions import active_roles
from accounts.roles import PERMISSIONS
from fleet.services import demo_mode
from operations.snapshot import SnapshotNotOutermost, snapshot_view

DEMO_BANNER = "SYNTHETIC DEMO - not real robots and not a departure decision"
PAGE_SIZE = 50
TIMELINE_DEFAULT_MIN = 1440
TIMELINE_MAX_MIN = 90 * 1440
METRICS_DEFAULT_DAYS = 7
METRICS_MAX_DAYS = 366
UNAVAILABLE = "The operations snapshot could not be opened as the outermost transaction; nothing was read."


class BadParam(ValueError):
    """An invalid query parameter (400, after the permission check)."""


# ------------------------------------------------------------------ view decorator
def ops_view(permission: str, *, json_api: bool = False) -> Callable[..., Any]:
    """GET only, outside ATOMIC_REQUESTS, inside one snapshot (permission checked inside it); fail closed."""

    def decorate(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
        inner = snapshot_view(permission, json=json_api)(view)

        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            try:
                return inner(request, *args, **kwargs)
            except SnapshotNotOutermost:
                if json_api:
                    return JsonResponse({"detail": "unavailable"}, status=503)
                return HttpResponse(UNAVAILABLE, status=503, content_type="text/plain")

        return require_GET(transaction.non_atomic_requests(wrapper))

    return decorate


# ------------------------------------------------------------------ formatting helpers
def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def as_of_text(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def jsonable(value: Any) -> Any:
    """Dataclasses / named tuples / dicts / lists / datetimes -> plain JSON values (keys and order kept)."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if hasattr(value, "_asdict"):
        return {k: jsonable(v) for k, v in value._asdict().items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    return value


def field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ------------------------------------------------------------------ cursor (contract 5, R4)
CURSOR_SALT = "operations.exceptions.cursor"
CURSOR_MAX_LEN = 1024
CURSOR_KEYS = {"after", "page", "first_at", "prev_at"}


def encode_cursor(after: Any, page: int, first_at: datetime, prev_at: datetime) -> str:
    """Continuation token: the read model's cursor (last sort key), page number, prior snapshot times.
    Signed with Django's signer (EM review: the snapshot metadata it carries must not be forgeable).
    Each continuation page is a NEW snapshot; the token only records which snapshots came before."""
    payload = {"after": jsonable(after), "page": page, "first_at": iso(first_at), "prev_at": iso(prev_at)}
    return signing.dumps(payload, salt=CURSOR_SALT, compress=True)


def _aware(raw: Any) -> datetime:
    if not isinstance(raw, str) or len(raw) > 64:
        raise TypeError("timestamp")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError("naive timestamp")
    return dt


def decode_cursor(token: str) -> dict[str, Any]:
    """Validate the WHOLE token: signature, size, exact keys and every type. Anything else is a 400."""
    try:
        if not isinstance(token, str) or not token or len(token) > CURSOR_MAX_LEN:
            raise ValueError("length")
        payload = signing.loads(token, salt=CURSOR_SALT)
        if not isinstance(payload, dict) or set(payload) != CURSOR_KEYS:
            raise ValueError("shape")
        page = payload["page"]
        if not isinstance(page, int) or isinstance(page, bool) or not 2 <= page <= 1_000_000:
            raise ValueError("page")
        _after_for_read_model(payload["after"])  # the sort key must be well-formed too
        return {
            "after": payload["after"],
            "page": page,
            "first_at": _aware(payload["first_at"]),
            "prev_at": _aware(payload["prev_at"]),
        }
    except (
        BadParam,
        signing.BadSignature,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
    ) as exc:
        raise BadParam("cursor is not a valid continuation token") from exc


def _after_for_read_model(after: Any) -> tuple[int, datetime, str, str]:
    """The exceptions sort key (priority, since, code, entity_ref) back from its JSON form."""
    try:
        if not isinstance(after, list | tuple) or len(after) != 4:
            raise TypeError("sort key")
        prio, since, code, ref = after
        if not isinstance(since, str) or len(since) > 64:
            raise TypeError("sort key")
        if not isinstance(prio, int) or isinstance(prio, bool) or not isinstance(code, str):
            raise TypeError("sort key")
        if not isinstance(ref, str):
            raise TypeError("sort key")
        when = datetime.fromisoformat(since)
        if when.tzinfo is None:
            raise ValueError("naive since")
        return (prio, when, code, ref)
    except (ValueError, TypeError) as exc:
        raise BadParam("cursor is not a valid continuation token") from exc


# ------------------------------------------------------------------ data builders (shared by pages and API)
def _read_model(name: str) -> Any:
    """operations.readmodels / exceptions / metrics (Engineers B and C), imported when a page is built."""
    return importlib.import_module(f"operations.{name}")


def overview_data(at: datetime) -> dict[str, Any]:
    readmodels = _read_model("readmodels")

    return {
        "as_of": at,
        "robots": list(readmodels.fleet_overview(at)),
        # Slice 7 (contract 07a 5.6): the production-blocker panel, in BOTH modes
        "production_blockers": readmodels.production_blockers(at),
    }


def production_blockers_data(at: datetime) -> dict[str, Any]:
    readmodels = _read_model("readmodels")

    panel = readmodels.production_blockers(at)
    return {
        "as_of": at,
        "production": panel.production,
        "heading": panel.heading,
        "note": panel.note,
        "items": list(panel.items),
    }


def closeout_data(at: datetime, date_raw: str | None) -> dict[str, Any]:
    """X-10 (contract 07a section 8). A bad or future date is a 400 (BadParam) when the view is dated."""
    closeout = _read_model("closeout")

    try:
        result = closeout.closeout(at, date_raw)
    except closeout.CloseoutDateError as exc:
        raise BadParam(str(exc)) from exc
    data: dict[str, Any] = {
        "as_of": at,
        "not_a_sign_off": result.not_a_sign_off,
        "as_of_note": result.as_of_note,
        "summary": result.summary,
        "timezone": result.timezone,
        "timezone_state": result.timezone_state,
        "dated": result.dated,
        "custody_unverified": [r.run for r in result.custody_unverified],
        "post_visit_outstanding": [r.run for r in result.post_visit_outstanding],
        "reconciliation_unresolved": [r.run for r in result.reconciliation_unresolved],
        "nothing_outstanding": result.nothing_outstanding,
    }
    if result.dated:
        data.update(
            {
                "date": result.day,
                "departed_open": list(result.departed_open),
                "carried_over": list(result.carried_over),
                "counts": {
                    "departed": result.departed_count,
                    "departed_closed": result.departed_closed_count,
                },
            }
        )
    else:
        # P1-5: no local day can be computed; one undated list, no carried-over label, no local-day counts
        data.update(
            {
                "date_unclassified": closeout.UNDATED_HEADING,
                "date_ignored": result.date_ignored,
                "open_runs": list(result.undated_open),
            }
        )
    return {"result": result, "data": data}


def exceptions_data(at: datetime, cursor: str | None) -> dict[str, Any]:
    cont = decode_cursor(cursor) if cursor else None  # a bad token is refused before anything is read
    after = _after_for_read_model(cont["after"]) if cont else None
    exceptions = _read_model("exceptions")

    page_no = cont["page"] if cont else 1
    first_at = cont["first_at"] if cont else at
    # snapshot-dated items keep page 1's time as 'since', so they do not reappear on every new snapshot
    result = exceptions.open_exceptions(at, limit=PAGE_SIZE, after=after, since_at=first_at)
    next_cursor = field(result, "next_cursor")
    return {
        "as_of": at,
        "page": page_no,
        "first_page_as_of": first_at,
        "previous_page_as_of": cont["prev_at"] if cont else None,
        "new_snapshot": cont is not None,
        "items": list(field(result, "items", [])),
        "readiness_coverage": field(result, "readiness_coverage"),
        # EM B2: runs whose RETURN_OVERDUE cannot be computed (no planned unloading end) are shown, not hidden
        "unknown_basis": list(field(result, "unknown_basis", []) or []),
        "next_cursor": encode_cursor(next_cursor, page_no + 1, first_at, at) if next_cursor else None,
    }


def incidents_data(at: datetime) -> dict[str, Any]:
    readmodels = _read_model("readmodels")

    return {"as_of": at, "incidents": list(readmodels.incident_queue(at))}


def parse_window(raw: str | None) -> int:
    if not raw:
        return TIMELINE_DEFAULT_MIN
    try:
        window = int(raw)
    except ValueError as exc:
        raise BadParam("window_min must be a whole number of minutes") from exc
    if not 1 <= window <= TIMELINE_MAX_MIN:
        raise BadParam(f"window_min must be between 1 and {TIMELINE_MAX_MIN}")
    return window


def timeline_data(at: datetime, window_min: int) -> dict[str, Any]:
    readmodels = _read_model("readmodels")

    result = readmodels.timeline(at, window_min=window_min)
    return {
        "as_of": at,
        "window_min": window_min,
        "timeline": result,
        "readiness_coverage": field(result, "readiness_coverage"),
        # EM review: the timeline's coverage counts only qualifying visits inside this window; the inbox's
        # counts every qualifying visit in the readiness lookahead. Label it so the two are never confused.
        "readiness_coverage_scope": f"visits inside this {window_min}-minute timeline window only",
    }


def _parse_instant(raw: str, name: str) -> datetime:
    try:
        if len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), time(0), tzinfo=UTC)
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BadParam(f"{name} must be an ISO date or date-time") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def parse_metrics_window(
    at: datetime, start_raw: str | None, end_raw: str | None
) -> tuple[datetime, datetime]:
    end = _parse_instant(end_raw, "end") if end_raw else at
    start = _parse_instant(start_raw, "start") if start_raw else end - timedelta(days=METRICS_DEFAULT_DAYS)
    if start >= end:
        raise BadParam("start must be before end")
    if end - start > timedelta(days=METRICS_MAX_DAYS):
        raise BadParam(f"the window may span at most {METRICS_MAX_DAYS} days")
    return start, end


def metrics_data(at: datetime, start: datetime, end: datetime) -> dict[str, Any]:
    metrics = _read_model("metrics")

    return {"as_of": at, "start": start, "end": end, "metrics": metrics.metrics(start, end, at)}


# ------------------------------------------------------------------ page helpers
def _base(request: HttpRequest, at: datetime, section: str) -> dict[str, Any]:
    demo = demo_mode()
    roles = active_roles(request.user)
    return {
        "roles": sorted(roles),
        "demo_mode": demo,
        "demo_banner": DEMO_BANNER if demo else "",
        "can_metrics": bool(roles & PERMISSIONS["operations.metrics"]),
        "section": section,
        "as_of_text": as_of_text(at),
        "as_of_iso": iso(at),
    }


def _bad(request: HttpRequest, at: datetime, section: str, exc: BadParam) -> HttpResponse:
    ctx = {**_base(request, at, section), "error": str(exc)}
    return render(request, "operations/bad_request.html", ctx, status=400)


def coverage_view(coverage: Any) -> dict[str, Any] | None:
    """Banner data when readiness coverage is partial (contract 4a); None when complete or absent."""
    if not coverage or field(coverage, "complete", True):
        return None
    return {
        "qualifying": field(coverage, "qualifying"),
        "evaluated": field(coverage, "evaluated"),
        "not_evaluated": field(coverage, "not_evaluated"),
    }


# ------------------------------------------------------------------ pages
@ops_view("operations.read")
def overview(request: HttpRequest, at: datetime) -> HttpResponse:
    data = overview_data(at)
    counts: dict[str, int] = {}
    for r in data["robots"]:
        label = str(field(r, "status_label", ""))
        counts[label] = counts.get(label, 0) + 1
    ctx = {**_base(request, at, "overview"), **data, "label_counts": sorted(counts.items())}
    return render(request, "operations/overview.html", ctx)


@ops_view("operations.read")
def closeout_page(request: HttpRequest, at: datetime) -> HttpResponse:
    try:
        built = closeout_data(at, request.GET.get("date"))
    except BadParam as exc:
        return _bad(request, at, "closeout", exc)
    result = built["result"]
    ctx = {
        **_base(request, at, "closeout"),
        "c": result,
        "undated_heading": _read_model("closeout").UNDATED_HEADING,
    }
    return render(request, "operations/closeout.html", ctx)


@ops_view("operations.read")
def exceptions_page(request: HttpRequest, at: datetime) -> HttpResponse:
    try:
        data = exceptions_data(at, request.GET.get("cursor"))
    except BadParam as exc:
        return _bad(request, at, "exceptions", exc)
    ctx = {
        **_base(request, at, "exceptions"),
        **data,
        "coverage": coverage_view(data["readiness_coverage"]),
        "first_page_as_of_text": as_of_text(data["first_page_as_of"]),
        "previous_page_as_of_text": as_of_text(data["previous_page_as_of"])
        if data["previous_page_as_of"]
        else "",
    }
    return render(request, "operations/exceptions.html", ctx)


@ops_view("operations.read")
def incidents_page(request: HttpRequest, at: datetime) -> HttpResponse:
    ctx = {**_base(request, at, "incidents"), **incidents_data(at)}
    return render(request, "operations/incidents.html", ctx)


@ops_view("operations.read")
def timeline_page(request: HttpRequest, at: datetime) -> HttpResponse:
    try:
        data = timeline_data(at, parse_window(request.GET.get("window_min")))
    except BadParam as exc:
        return _bad(request, at, "timeline", exc)
    ctx = {**_base(request, at, "timeline"), **data, "coverage": coverage_view(data["readiness_coverage"])}
    return render(request, "operations/timeline.html", ctx)


@ops_view("operations.metrics")
def metrics_page(request: HttpRequest, at: datetime) -> HttpResponse:
    try:
        start, end = parse_metrics_window(at, request.GET.get("start"), request.GET.get("end"))
    except BadParam as exc:
        return _bad(request, at, "metrics", exc)
    data = metrics_data(at, start, end)
    ctx = {
        **_base(request, at, "metrics"),
        **data,
        "start_text": as_of_text(start),
        "end_text": as_of_text(end),
    }
    return render(request, "operations/metrics.html", ctx)
