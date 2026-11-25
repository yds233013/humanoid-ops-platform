"""Template helpers for the shared design system (milestone 01A).

* Time: the database stores UTC; every screen shows the workspace display zone (the workspace row's
  time zone, see platform_core.timezones; settings.TIME_ZONE only when no row exists) with an explicit zone
  label and ONE format (``Sat 21 Nov, 10:48 PST``). Use ``local_dt`` / ``local_time``; never the ``date``
  filter on an instant.
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
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString

from accounts.permissions import active_roles
from accounts.roles import PERMISSIONS
from platform_core import timezones

register = template.Library()

DT_FORMAT = "%a %-d %b, %H:%M %Z"  # Sat 21 Nov, 10:48 PST
DT_FORMAT_YEAR = "%a %-d %b %Y, %H:%M %Z"  # used when the year is not the current local year
DATE_FORMAT = "%a %-d %b"
DATE_FORMAT_YEAR = "%a %-d %b %Y"


def workspace_zone() -> ZoneInfo:
    """The workspace DISPLAY zone (activated per request by DisplayTimeZoneMiddleware)."""
    return timezones.workspace_zone()


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


def home_text(site: Any) -> str:
    """How a home is named on v2 screens (01D ruling A): ``H-101 · Fremont · CRM reference NB-1042``. The
    product stores no customer name or street address (planning/forms.py); the town (service area) and the
    opaque CRM reference are what tell homes apart."""
    if site is None:
        return ""
    parts = [str(site.code)]
    if getattr(site, "zone", ""):
        parts.append(str(site.zone))
    if getattr(site, "crm_ref", ""):
        parts.append(f"CRM reference {site.crm_ref}")
    return " · ".join(parts)


@register.filter
def home_label(site: Any) -> str:
    return home_text(site)


# ------------------------------------------------------------ demo values and demo approvals (01D ruling C)
DEMO_INTERVAL_TEXT = (
    "This interval is fictional demonstration data. It is not an approved engineering requirement for a real "
    "robot."
)
DEMO_CHECKLIST_TEXT = (
    "This checklist is fictional demonstration data. It is not an approved procedure for a real robot and "
    "can never be approved for production."
)
DEMO_SOURCE_TEXT = (
    "This record is fictional demonstration data. Its approvals count in this demo workspace only; "
    "production refuses it."
)
# Approval states shown with their precise meaning (never a neutral chip alone for anything that affects
# whether something is approved).
APPROVAL_WORDS = {
    "synthetic approved": "Demo approval",
    "synthetic draft": "Draft \u2014 not approved",
    "synthetic, demo only": "Demo checklist \u2014 not approved for production",
    "draft": "Draft \u2014 not approved",
}


def _fold(text: str) -> SafeString:
    return format_html(
        '<details class="fold demo-fold"><summary>What this means</summary><p class="muted">{}</p></details>',
        text,
    )


@register.simple_tag
def demo_value(text: str = DEMO_INTERVAL_TEXT) -> SafeString:
    """A neutral "Demo value" chip with its explanation in a fold (values that approve nothing)."""
    return format_html('<span class="chip chip-neutral" data-demo-value="1">Demo value</span>{}', _fold(text))


@register.simple_tag
def demo_checklist() -> SafeString:
    return demo_value(DEMO_CHECKLIST_TEXT)


@register.simple_tag
def source_label(source: str, display: str = "") -> SafeString:
    """The source of an approvable record. A synthetic_demo one is labelled precisely ("Not approved for
    production") with the explanation in a fold; any other source shows its own name."""
    if source == "synthetic_demo":
        return format_html(
            '<span class="chip chip-warn" data-demo-source="1">Not approved for production</span>{}',
            _fold(DEMO_SOURCE_TEXT),
        )
    return format_html("{}", display or source)


@register.filter
def source_option(label: str) -> str:
    """A source choice in a form, in plain words: "Synthetic demo (...)" reads "Demo, fictional (...)"."""
    text = str(label)
    if text.lower().startswith("synthetic demo"):
        rest = text[len("synthetic demo") :].strip()
        return (
            f"Demo, fictional {rest}".strip() if rest else "Demo, fictional (never approvable for production)"
        )
    return text


@register.filter
def approval_text(status: str) -> str:
    return APPROVAL_WORDS.get(str(status), str(status))


HISTORICAL_VISIT = "Historical scheduled visit"


@register.filter
def visit_status(visit: Any) -> str:
    """The visit's status in plain words. A planned or confirmed visit whose on-site time ended before the
    workspace's local today and that never departed is a historical scheduled visit: it is never shown as
    completed, and as nothing else either (display only; no rule reads this)."""
    status = getattr(visit, "status", "")
    rev = getattr(visit, "current_revision", None)
    if status in ("planned", "confirmed") and rev is not None and rev.on_site_end is not None:
        zone = workspace_zone()
        today_start = datetime.combine(datetime.now(zone).date(), datetime.min.time(), tzinfo=zone)
        if rev.on_site_end < today_start:
            from readiness.models import DepartureEvent  # noqa: PLC0415 - readiness depends on platform_core

            if not DepartureEvent.objects.filter(visit_id=visit.pk).exists():
                return HISTORICAL_VISIT
    return str(visit.get_status_display()) if hasattr(visit, "get_status_display") else str(status)


@register.filter
def local_dt(value: Any) -> str:
    """An instant in the workspace time zone with its zone label: ``Sat 21 Nov, 10:48 PST``."""
    dt = _as_datetime(value)
    if dt is None:
        return ""
    zone = workspace_zone()
    local = dt.astimezone(zone)
    fmt = DT_FORMAT if local.year == datetime.now(zone).year else DT_FORMAT_YEAR
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
    zone = workspace_zone()
    if isinstance(value, datetime) or isinstance(value, str):
        dt = _as_datetime(value)
        if dt is None:
            return ""
        d: date = dt.astimezone(zone).date()
    elif isinstance(value, date):
        d = value
    else:
        return ""
    fmt = DATE_FORMAT if d.year == datetime.now(zone).year else DATE_FORMAT_YEAR
    return d.strftime(fmt).replace("  ", " ")


_UTC_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})? ?UTC")
_UTC_CLOCK = re.compile(r"\b(\d{2}:\d{2}) UTC\b")
# ISO-8601 instants with an offset (e.g. "2026-11-20T06:29:07.123+00:00") in model-produced reasons
_ISO_INSTANT = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2}))")


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

    def iso(m: re.Match[str]) -> str:
        return local_dt(datetime.fromisoformat(m.group(1).replace("Z", "+00:00")))

    out = _ISO_INSTANT.sub(iso, out)
    return _UTC_CLOCK.sub(clock, _UTC_STAMP.sub(stamp, out))


@register.simple_tag
def workspace_tz_label() -> str:
    """The workspace display zone name and today's abbreviation, e.g. ``America/Los_Angeles (PST)``."""
    name = timezones.workspace_zone_name()
    return f"{name} ({datetime.now(ZoneInfo(name)).strftime('%Z')})"


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
    ("dispatch", "Dispatch", "/planning/", "planning.read", ("/planning/", "/readiness/", "/execution/")),
    ("fleet", "Fleet", "/fleet/", "fleet.read", ("/fleet/",)),
    ("customers", "Customers", "/planning/sites/", "planning.read", ("/planning/sites/",)),
    ("hub", "Hub", "/hub/", "hub.nav", ("/hub/", "/ops/closeout/")),
    ("issues", "Issues", "/ops/exceptions/", "operations.read", ("/ops/exceptions/", "/ops/incidents/")),
]

# Section links shown under the header (the "pages" of each section).
SECTIONS: dict[str, list[tuple[str, str, str | None]]] = {
    "today": [
        ("Today", "/ops/", "operations.read"),
        ("Timeline", "/ops/timeline/", "operations.read"),
    ],
    "dispatch": [
        ("Planner", "/planning/", "planning.read"),
        ("Departure readiness", "/readiness/", "readiness.read"),
        ("Field runs", "/execution/", "execution.read"),
        ("My visits", "/execution/mine/", "execution.read"),
    ],
    "fleet": [
        ("Robots", "/fleet/", "fleet.read"),
        ("Robot status", "/ops/robots/", "operations.read"),
        ("Configurations", "/fleet/patterns/", "fleet.read"),
        ("People", "/people/", "planning.read"),
        ("Crews", "/crews/", "planning.read"),
        ("Vehicles", "/vehicles/", "planning.read"),
    ],
    "customers": [("Sites", "/planning/sites/", "planning.read")],
    "hub": [
        ("Work queue", "/hub/", "hub.view"),
        ("Charging", "/hub/charging/", "hub.view"),
        ("Daily close-out", "/ops/closeout/", "operations.read"),
        ("Field runs", "/execution/", "execution.read"),
    ],
    "issues": [
        ("Exceptions", "/ops/exceptions/", "operations.read"),
        ("Incident queue", "/ops/incidents/", "operations.read"),
        ("Incident reports", "/execution/incidents/", "execution.read"),
    ],
    "settings": [
        ("Workspace setup", "/settings/setup/", "accounts.grant_role"),
        ("Company", "/settings/company/", "accounts.grant_role"),
        ("Hubs", "/settings/hubs/", "accounts.grant_role"),
        ("Chargers", "/hub/charging/chargers/", "hub.manage_chargers"),
        ("People and roles", "/settings/users/", "accounts.grant_role"),
        ("Operating settings", "/settings/operating/", "platform_core.read_operating_settings"),
        ("Hold release policies", "/fleet/hold-policies/", "fleet.read"),
        ("Readiness policies", "/readiness/policies/", "readiness.read"),
        ("Incident policies", "/execution/policies/", "execution.read"),
        ("Escalation deadlines", "/execution/escalation-deadlines/", "execution.read"),
        ("Production readiness", "/ops/production-readiness/", "operations.read"),
    ],
    "reports": [
        ("Metrics", "/ops/metrics/", "operations.metrics"),
        ("Fleet health", "/ops/health/", "operations.metrics"),
        ("Capacity forecast", "/ops/capacity/", "operations.metrics"),
    ],
}

# Longest matching prefix wins; first match order matters for the specific paths.
SECTION_PREFIXES: list[tuple[str, str]] = [
    ("/settings/", "settings"),
    ("/people/", "fleet"),
    ("/crews/", "fleet"),
    ("/vehicles/", "fleet"),
    ("/fleet/hold-policies/", "settings"),
    ("/readiness/policies/", "settings"),
    ("/execution/policies/", "settings"),
    ("/execution/escalation-deadlines/", "settings"),
    ("/ops/production-readiness/", "settings"),
    ("/ops/robots/", "fleet"),
    ("/ops/metrics/", "reports"),
    ("/ops/health/", "reports"),
    ("/ops/capacity/", "reports"),
    ("/planning/sites/", "customers"),
    ("/ops/closeout/", "hub"),
    ("/hub/", "hub"),
    ("/ops/exceptions/", "issues"),
    ("/ops/incidents/", "issues"),
    ("/execution/incidents/", "issues"),
    ("/ops/timeline/", "today"),
    ("/ops/", "today"),
    ("/execution/", "dispatch"),  # field runs and My visits (01D J5-1)
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
    """True in a demo workspace (v2 01D): the workspace row's demo flag is on, or the deployment runs in demo
    mode (demo_mode = 'true', where synthetic approvals are usable). Either one shows the one persistent
    "Demo workspace" badge; a production workspace (flag off, production mode) never shows it."""
    cached: bool | None = getattr(request, "_ui_demo", None)
    if cached is None:
        from fleet.services import demo_mode

        cached = timezones.workspace_demo_flag() or bool(demo_mode())
        request._ui_demo = cached  # type: ignore[attr-defined]
    return cached


FIELD_ONLY_LANDING = "/execution/mine/"


def landing_url(roles: set[str]) -> str:
    """Where a person lands after login: a crew member (field operator and no other operational role) on My
    visits (01D J5-1); otherwise Today when they may read it; an admin (who manages the workspace but reads no
    operations) lands on workspace setup; everyone else on Fleet. v2 02A: a hub technician whose other
    roles are only field operator and/or admin lands on the hub work queue."""
    if "hub_technician" in roles and not (roles - {"hub_technician", "field_operator", "admin"}):
        return "/hub/"
    if "field_operator" in roles and not (roles - {"field_operator", "admin"}):
        return FIELD_ONLY_LANDING
    if _allowed(roles, "operations.read"):
        return "/ops/"
    if _allowed(roles, "platform_core.manage_workspace"):
        return "/settings/setup/"
    return "/fleet/"


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
        # a view that already read demo_mode passes it; the workspace flag comes from the request's one read
        "demo": (bool(context["demo_mode"]) or timezones.workspace_demo_flag())
        if "demo_mode" in context
        else workspace_demo_mode(request),
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


# ------------------------------------------------------------------ 01C interaction components
# Documented in docs/ui_components.md.
@register.simple_tag(takes_context=True)
def form_token(context: template.Context) -> SafeString:
    """Hidden one-time token for double-submit protection. Put it in every state-changing form, next to
    ``{% csrf_token %}``. The first POST spends it; a replay is refused with a friendly message."""
    from platform_core.interaction import TOKEN_FIELD, issue_form_token

    request = context.get("request")
    token = issue_form_token(request) if request is not None else ""
    return format_html('<input type="hidden" name="{}" value="{}">', TOKEN_FIELD, token)


@register.simple_tag
def who_can(permission: str) -> str:
    """``the safety reviewer`` / ``the ops lead or the engineering lead`` for a permission name."""
    from platform_core.interaction import who_can as _who

    return _who(permission)


@register.simple_tag
def refusal_note(permission: str) -> SafeString:
    """A quiet line for a viewer who may not act: ``Only the safety reviewer can do this.``"""
    from platform_core.interaction import refusal_text

    return format_html('<span class="waiting" data-refusal="1">{}</span>', refusal_text(permission))


@register.simple_tag
def waiting_note(permission: str, what: str = "approval") -> SafeString:
    """``Waiting for the founder's approval``: who must act next, for pending items."""
    from platform_core.interaction import waiting_text

    return format_html('<span class="waiting" data-waiting="1">{}</span>', waiting_text(permission, what))


@register.simple_tag(takes_context=True)
def can(context: template.Context, permission: str) -> bool:
    """True when the viewer holds the permission: ``{% can "fleet.register_robot" as ok %}``."""
    request = context.get("request")
    return request is not None and _allowed(request_roles(request), permission)
