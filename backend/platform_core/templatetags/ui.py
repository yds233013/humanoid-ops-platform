"""Template helpers for the shared design system (milestone 01A).

* Time: the database stores UTC; every screen shows the workspace time zone (settings.TIME_ZONE) with an
  explicit zone label and ONE format (``Sat 21 Nov, 10:48 PST``). Use ``local_dt`` / ``local_time``; never the
  ``date`` filter on an instant.
* Navigation: ``{% site_header %}`` renders the one global header (navigation, section links, user menu and
  the single "Demo workspace" badge).
* Plain language: ``refusal`` turns a raw "Refused (...): CODE: text; CODE: text" message into a list of
  plain reasons; the raw codes stay in a "Details" fold.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django import template
from django.conf import settings
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString

from accounts.permissions import active_roles
from accounts.roles import PERMISSIONS

register = template.Library()

DT_FORMAT = "%a %-d %b, %H:%M %Z"  # Sat 21 Nov, 10:48 PST
DT_FORMAT_YEAR = "%a %-d %b %Y, %H:%M %Z"  # used when the year is not the current local year
DATE_FORMAT = "%a %-d %b"
DATE_FORMAT_YEAR = "%a %-d %b %Y"


def workspace_zone() -> ZoneInfo:
    return ZoneInfo(settings.TIME_ZONE)


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt  # a naive value is a stored UTC instant


@register.filter
def local_dt(value: Any) -> str:
    """An instant in the workspace time zone with its zone label: ``Sat 21 Nov, 10:48 PST``."""
    dt = _as_datetime(value)
    if dt is None:
        return ""
    local = dt.astimezone(workspace_zone())
    fmt = DT_FORMAT if local.year == datetime.now(workspace_zone()).year else DT_FORMAT_YEAR
    return local.strftime(fmt)


@register.filter
def local_time(value: Any) -> str:
    """Clock time only, in the workspace zone: ``10:48``. Use it for the end of a range whose start is shown
    with ``local_dt``."""
    dt = _as_datetime(value)
    return "" if dt is None else dt.astimezone(workspace_zone()).strftime("%H:%M")


@register.filter
def local_date(value: Any) -> str:
    """A calendar day: an instant is converted to the workspace zone first; a ``date`` is shown as is."""
    if isinstance(value, datetime) or isinstance(value, str):
        dt = _as_datetime(value)
        if dt is None:
            return ""
        d: date = dt.astimezone(workspace_zone()).date()
    elif isinstance(value, date):
        d = value
    else:
        return ""
    fmt = DATE_FORMAT if d.year == datetime.now(workspace_zone()).year else DATE_FORMAT_YEAR
    return d.strftime(fmt).replace("  ", " ")


_UTC_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})? ?UTC")
_UTC_CLOCK = re.compile(r"\b(\d{2}:\d{2}) UTC\b")


@register.filter
def localize_utc_text(text: Any) -> str:
    """Rewrite "YYYY-MM-DD HH:MM UTC" stamps inside sentences produced by read models into the workspace
    zone and format. A bare "HH:MM UTC" (an as-of clock time) is taken as today's UTC date."""
    out = str(text)

    def stamp(m: re.Match[str]) -> str:
        return local_dt(datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:00+00:00"))

    def clock(m: re.Match[str]) -> str:
        today = datetime.now(UTC).date().isoformat()
        return local_dt(datetime.fromisoformat(f"{today}T{m.group(1)}:00+00:00"))

    return _UTC_CLOCK.sub(clock, _UTC_STAMP.sub(stamp, out))


@register.simple_tag
def workspace_tz_label() -> str:
    """The workspace zone name and today's abbreviation, e.g. ``America/Los_Angeles (PST)``."""
    zone = workspace_zone()
    return f"{settings.TIME_ZONE} ({datetime.now(zone).strftime('%Z')})"


# ------------------------------------------------------------------ status chips
TONES = {"neutral", "ok", "warn", "bad", "info"}


@register.simple_tag
def chip(tone: str, label: Any) -> SafeString:
    """A status chip. Tones: neutral, info, ok, warn, bad. Never use ``ok`` (green) for anything that sounds
    like permission to depart; those states use ``info`` or ``neutral``."""
    tone = tone if tone in TONES else "neutral"
    return format_html('<span class="chip chip-{} badge">{}</span>', tone, label)


# ------------------------------------------------------------------ permission helpers
@register.filter
def intersect(a: Iterable[str] | None, b: Iterable[str] | None) -> list[str]:
    """Roles in both lists, in the order of ``a``."""
    allowed = set(b or [])
    return [x for x in (a or []) if x in allowed]


@register.filter
def role_label(role: str) -> str:
    return str(role).replace("_", " ")


@register.filter
def role_list(roles: Iterable[str] | None) -> str:
    """``engineering_lead, founder`` -> ``engineering lead or founder`` for "Waiting for ..." notes."""
    words = [role_label(r) for r in (roles or [])]
    return " or ".join(words) if len(words) < 3 else ", ".join(words[:-1]) + " or " + words[-1]


@register.filter
def get_item(mapping: Any, key: str) -> Any:
    try:
        return mapping[key]
    except (KeyError, TypeError, IndexError):
        return None


# ------------------------------------------------------------------ plain-language refusals
_REASON_SPLIT = re.compile(r"(?:^|;\s+|\s-\s)([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+):\s")
_HEAD = re.compile(r"^Refused \(([^)]*)\):\s*(.*)$", re.S)


def parse_refusal(text: str) -> dict[str, Any] | None:
    """Split "Refused (conflict): Departure not authorized: Readiness incomplete - CODE: sentence; CODE: ..."
    into a headline and plain reasons (each with its raw code kept for the Details fold).

    Returns None when the text is not a refusal."""
    m = _HEAD.match(text.strip())
    if m is None:
        return None
    kind, body = m.group(1), m.group(2)
    parts = _REASON_SPLIT.split(body)
    reasons: list[dict[str, str]] = []
    headline = body
    if len(parts) >= 3:
        headline = parts[0].strip().rstrip("-: ").strip()
        for code, sentence in zip(parts[1::2], parts[2::2], strict=False):
            reasons.append({"code": code, "text": sentence.strip().rstrip(";").strip()})
    # Sentence-only bodies keep their sentence as the single reason.
    if not reasons:
        reasons = [{"code": "", "text": body.strip()}]
        headline = ""
    return {"kind": kind, "headline": headline, "reasons": reasons, "raw": text.strip()}


@register.filter
def refusal(message: Any) -> dict[str, Any] | None:
    return parse_refusal(str(message))


# ------------------------------------------------------------------ navigation
# (key, label, url, permission or None, url prefixes that make it the active section)
NAV: list[tuple[str, str, str, str | None, tuple[str, ...]]] = [
    ("today", "Today", "/ops/", "operations.read", ("/ops/", "/ops/timeline/")),
    ("dispatch", "Dispatch", "/planning/", "planning.read", ("/planning/", "/readiness/")),
    ("fleet", "Fleet", "/fleet/", "fleet.read", ("/fleet/",)),
    ("customers", "Customers", "/planning/sites/", "planning.read", ("/planning/sites/",)),
    ("hub", "Hub", "/ops/closeout/", "operations.read", ("/ops/closeout/", "/execution/")),
    ("issues", "Issues", "/ops/exceptions/", "operations.read", ("/ops/exceptions/", "/ops/incidents/")),
]

# Section links shown under the header (the "pages" of each section).
SECTIONS: dict[str, list[tuple[str, str, str | None]]] = {
    "today": [
        ("Robot status", "/ops/", "operations.read"),
        ("Timeline", "/ops/timeline/", "operations.read"),
    ],
    "dispatch": [
        ("Planner", "/planning/", "planning.read"),
        ("Departure readiness", "/readiness/", "readiness.read"),
    ],
    "fleet": [("Robots", "/fleet/", "fleet.read")],
    "customers": [("Sites", "/planning/sites/", "planning.read")],
    "hub": [
        ("Daily close-out", "/ops/closeout/", "operations.read"),
        ("Field runs", "/execution/", "execution.read"),
    ],
    "issues": [
        ("Exceptions", "/ops/exceptions/", "operations.read"),
        ("Incident queue", "/ops/incidents/", "operations.read"),
        ("Incident reports", "/execution/incidents/", "execution.read"),
    ],
    "settings": [
        ("Hold release policies", "/fleet/hold-policies/", "fleet.read"),
        ("Readiness policies", "/readiness/policies/", "readiness.read"),
        ("Incident policies", "/execution/policies/", "execution.read"),
        ("Escalation deadlines", "/execution/escalation-deadlines/", "execution.read"),
    ],
    "reports": [
        ("Metrics", "/ops/metrics/", "operations.metrics"),
        ("Capacity forecast", "/ops/capacity/", "operations.metrics"),
    ],
}

# Longest matching prefix wins; first match order matters for the specific paths.
SECTION_PREFIXES: list[tuple[str, str]] = [
    ("/fleet/hold-policies/", "settings"),
    ("/readiness/policies/", "settings"),
    ("/execution/policies/", "settings"),
    ("/execution/escalation-deadlines/", "settings"),
    ("/ops/metrics/", "reports"),
    ("/ops/capacity/", "reports"),
    ("/planning/sites/", "customers"),
    ("/ops/closeout/", "hub"),
    ("/ops/exceptions/", "issues"),
    ("/ops/incidents/", "issues"),
    ("/execution/incidents/", "issues"),
    ("/ops/timeline/", "today"),
    ("/ops/", "today"),
    ("/execution/", "hub"),
    ("/planning/", "dispatch"),
    ("/readiness/", "dispatch"),
    ("/fleet/", "fleet"),
]


def section_for(path: str) -> str:
    for prefix, key in SECTION_PREFIXES:
        if path.startswith(prefix):
            return key
    return ""


def _request(context: template.Context) -> HttpRequest | None:
    return context.get("request")


def request_roles(request: HttpRequest) -> set[str]:
    """Active roles of the viewer, read once per request."""
    cached: set[str] | None = getattr(request, "_ui_roles", None)
    if cached is None:
        cached = active_roles(getattr(request, "user", None))
        request._ui_roles = cached  # type: ignore[attr-defined]
    return cached


def _allowed(roles: set[str], permission: str | None) -> bool:
    return permission is None or bool(roles & PERMISSIONS.get(permission, set()))


def workspace_demo_mode(request: HttpRequest) -> bool:
    cached: bool | None = getattr(request, "_ui_demo", None)
    if cached is None:
        from fleet.services import demo_mode

        cached = bool(demo_mode())
        request._ui_demo = cached  # type: ignore[attr-defined]
    return cached


def landing_url(roles: set[str]) -> str:
    """Where a person lands after login: Today when they may read it, else Fleet."""
    return "/ops/" if _allowed(roles, "operations.read") else "/fleet/"


@register.inclusion_tag("ui/header.html", takes_context=True)
def site_header(context: template.Context) -> dict[str, Any]:
    request = _request(context)
    if request is None or not getattr(getattr(request, "user", None), "is_authenticated", False):
        return {"authenticated": False}
    roles = set(context["roles"]) if "roles" in context else request_roles(request)
    path = request.path
    active = section_for(path)
    nav = [
        {"label": label, "url": url, "active": key == active}
        for key, label, url, perm, _ in NAV
        if _allowed(roles, perm)
    ]
    sub = [
        {"label": label, "url": url, "active": False}
        for label, url, perm in SECTIONS.get(active, [])
        if _allowed(roles, perm)
    ]
    current = max(
        (x for x in sub if path.startswith(str(x["url"]))), key=lambda x: len(str(x["url"])), default=None
    )
    if current is not None:
        current["active"] = True
    settings_links = [x for x in SECTIONS["settings"] if _allowed(roles, x[2])]
    report_links = [x for x in SECTIONS["reports"] if _allowed(roles, x[2])]
    return {
        "authenticated": True,
        "request": request,
        "nav": nav,
        "sub": sub if len(sub) > 1 else [],
        "settings_links": settings_links,
        "report_links": report_links,
        "username": request.user.get_username(),
        "roles": sorted(roles),
        "home_url": landing_url(roles),
        "demo": bool(context["demo_mode"]) if "demo_mode" in context else workspace_demo_mode(request),
        "csrf_token": context.get("csrf_token"),
        "settings_active": active == "settings",
        "reports_active": active == "reports",
    }


# ------------------------------------------------------------------ page header block
class PageHeaderNode(template.Node):
    def __init__(self, nodelist: template.NodeList, kwargs: dict[str, template.Variable | str]) -> None:
        self.nodelist = nodelist
        self.kwargs = kwargs

    def render(self, context: template.Context) -> str:
        from django.template.loader import render_to_string

        values: dict[str, Any] = {}
        for key, val in self.kwargs.items():
            values[key] = val.resolve(context) if isinstance(val, template.Variable) else val
        values["body"] = self.nodelist.render(context)
        return render_to_string("ui/page_header.html", values)


@register.tag("page_header")
def page_header(parser: template.base.Parser, token: template.base.Token) -> PageHeaderNode:
    """``{% page_header title="Fleet" back_url="/fleet/" %}subtitle html{% endpage_header %}``"""
    bits = token.split_contents()[1:]
    kwargs: dict[str, template.Variable | str] = {}
    for bit in bits:
        key, _, raw = bit.partition("=")
        if not raw:
            raise template.TemplateSyntaxError("page_header takes key=value arguments")
        kwargs[key] = template.Variable(raw)
    nodelist = parser.parse(("endpage_header",))
    parser.delete_first_token()
    return PageHeaderNode(nodelist, kwargs)
