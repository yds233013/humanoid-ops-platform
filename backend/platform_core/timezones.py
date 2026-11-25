"""The ONE display time zone of the workspace (v2 01D; contract section 5, 01A item 3).

Which zone is for what:

* **Display zone = the workspace time zone** (``platform_core_workspace.time_zone``, edited on
  Settings > Company). Every screen shows instants in this zone with its label (``ui.local_dt``,
  ``ui.workspace_tz_label``), Today's "today" and "tomorrow" boundaries use it (``operations.today``), and
  times typed into forms that are labelled with it are read in it. ``settings.TIME_ZONE`` is only the
  fallback when no workspace row exists yet (a fresh database) or its value is unusable.
* **Business-rule zones are separate and unchanged**: the daily close-out uses the deployment owner's
  ``ops_closeout_timezone`` operating setting (``operations.closeout``); visit booking reads wall times in
  the home's own zone (``planning.Site.timezone``); hubs record their own zone. Those zones decide rules,
  never how an instant is labelled on screen.
* **Storage is UTC.** Audit timestamps and every stored instant stay UTC; only presentation changes.

``DisplayTimeZoneMiddleware`` reads the workspace row once per request (one primary-key SELECT), activates it
with ``django.utils.timezone.activate`` (so ``timezone.localdate()`` / ``localtime()`` follow it) and keeps it
for the template helpers, so a page that renders hundreds of times reads the zone once.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

log = logging.getLogger(__name__)

_active_zone: ContextVar[str | None] = ContextVar("workspace_display_zone", default=None)
# The workspace row's demo flag, read in the same SELECT as the zone (v2 01D: the header badge needs it on
# every page; reading it here keeps the page's statement count unchanged).
_active_demo: ContextVar[bool | None] = ContextVar("workspace_demo_flag", default=None)

# Paths that must not touch the database just to pick a display zone (the health check reports a database
# outage itself; static files never render times).
SKIP_PREFIXES = ("/healthz", "/static/")


def _usable(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def _read_workspace_row() -> tuple[str, bool]:
    """(zone name, demo flag) of the workspace row in one SELECT; (settings.TIME_ZONE, False) if none."""
    from platform_core.models import Workspace

    try:
        row = Workspace.objects.filter(pk=1).values_list("time_zone", "demo").first()
    except Exception:  # noqa: BLE001 - no database (or none allowed here): fall back, never fail a render
        log.debug("display zone: workspace row not readable, using settings.TIME_ZONE", exc_info=True)
        row = None
    name, demo = row if row is not None else (None, False)
    return _usable(name) or settings.TIME_ZONE, bool(demo)


def read_workspace_zone_name() -> str:
    """The workspace zone from the database, or ``settings.TIME_ZONE`` when there is no usable row."""
    return _read_workspace_row()[0]


def workspace_demo_flag() -> bool:
    """The workspace row's demo flag: the one read for this request, else read now."""
    cached = _active_demo.get()
    return cached if cached is not None else _read_workspace_row()[1]


def workspace_zone_name() -> str:
    """The display zone name: the one activated for this request, else read from the workspace row."""
    return _active_zone.get() or read_workspace_zone_name()


def workspace_zone() -> ZoneInfo:
    return ZoneInfo(workspace_zone_name())


@contextmanager
def display_zone(name: str | None = None) -> Iterator[str]:
    """Activate the display zone (read from the workspace when ``name`` is None) for a block of code: what the
    middleware does per request, usable from management commands and tests."""
    read_name, demo = _read_workspace_row()
    zone_name = _usable(name) or read_name
    token = _active_zone.set(zone_name)
    demo_token = _active_demo.set(demo)
    timezone.activate(ZoneInfo(zone_name))
    try:
        yield zone_name
    finally:
        timezone.deactivate()
        _active_zone.reset(token)
        _active_demo.reset(demo_token)


class DisplayTimeZoneMiddleware:
    """Activate the workspace display zone for the whole request (see the module docstring)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith(SKIP_PREFIXES):
            return self.get_response(request)
        with display_zone():
            return self.get_response(request)


_UTC_TEXT = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC")


def localize_utc_text(text: str) -> str:
    """v2 03A walk: a database refusal names times as ``2026-12-08 03:35 UTC``; show them in the workspace
    zone with its label, as the rest of the UI does (``Tue 08 Dec, 19:35 PST``)."""
    from datetime import UTC, datetime

    zone = workspace_zone()

    def local(m: re.Match[str]) -> str:
        at = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").replace(tzinfo=UTC).astimezone(zone)
        return at.strftime("%a %d %b %Y, %H:%M %Z")

    return _UTC_TEXT.sub(local, text)
