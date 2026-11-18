"""Slice 5 Engineer D: command center pages (/ops/...) and JSON API (/api/ops/...). Contract docs/05, sections
2-6. SYNTHETIC data only.

Pages/API that need the read models of Engineers B (operations.exceptions) and C (operations.readmodels,
operations.metrics) skip themselves until those modules exist in this build; the permission, fail-closed,
transaction and wording tests run without them.
"""

import importlib
import importlib.util
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.conf import settings
from django.db import connection, transaction
from django.test import Client, RequestFactory

from accounts.roles import PERMISSIONS
from operations import api, views
from operations.snapshot import SET_SNAPSHOT
from tests.conftest import make_user
from tests.fleet_world import set_setting

pytestmark = pytest.mark.django_db(transaction=True)

HAS_B = importlib.util.find_spec("operations.exceptions") is not None
HAS_C = (
    importlib.util.find_spec("operations.readmodels") is not None
    and importlib.util.find_spec("operations.metrics") is not None
)
needs_b = pytest.mark.skipif(not HAS_B, reason="operations.exceptions (Engineer B) not merged yet")
needs_c = pytest.mark.skipif(not HAS_C, reason="operations.readmodels / metrics (Engineer C) not merged yet")
needs_bc = pytest.mark.skipif(not (HAS_B and HAS_C), reason="Engineers B and C not merged yet")

READ_ROLES = sorted(PERMISSIONS["operations.read"])
METRICS_ROLES = sorted(PERMISSIONS["operations.metrics"])
READ_ONLY_ROLES = sorted(PERMISSIONS["operations.read"] - PERMISSIONS["operations.metrics"])
PAGES = ["/ops/", "/ops/exceptions/", "/ops/incidents/", "/ops/timeline/"]
APIS = ["/api/ops/overview", "/api/ops/exceptions", "/api/ops/incidents", "/api/ops/timeline"]
ALL_ENDPOINTS = [*PAGES, "/ops/metrics/", *APIS, "/api/ops/metrics"]
FORBIDDEN_WORDS = re.compile(r"\b(cleared|safe|ready to deploy|available to deploy)\b", re.IGNORECASE)
# Requests that are fully handled before any read model runs (invalid parameter -> 400 inside the snapshot).
BAD_PARAM_PAGES = [
    "/ops/timeline/?window_min=abc",
    "/ops/metrics/?start=nope",
    "/ops/exceptions/?cursor=bm9wZQ",
]


@pytest.fixture(autouse=True)
def _demo_mode(transactional_db: None) -> None:
    set_setting("demo_mode", "true")


def client(*roles: str, name: str | None = None, expired: bool = False) -> Client:
    c = Client()
    c.force_login(make_user(name or "u_" + "_".join(roles or ("none",)), *roles, expired=expired))
    return c


@contextmanager
def captured() -> Iterator[list[str]]:
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        yield seen


# ------------------------------------------------------------------ permissions (run now)
@pytest.mark.parametrize("url", ALL_ENDPOINTS)
@pytest.mark.parametrize("roles", [(), ("admin",)], ids=["no-role", "admin"])
def test_no_read_permission_gets_403_and_no_data(url, roles):
    c = client(*roles)
    with captured() as seen:
        r = c.get(url)
    assert r.status_code == 403
    body = r.content.decode()
    assert "<table" not in body and "items" not in body and "as_of" not in body
    if url.startswith("/api/"):
        assert r.json() == {"detail": "forbidden"}
    # the permission is checked inside the snapshot; only session, user and role grants were read
    i = seen.index(SET_SNAPSHOT)
    assert seen[i + 1] == "SELECT transaction_timestamp()"
    for sql in seen[i + 2 :]:
        assert re.search(r'FROM "(django_session|auth_user|accounts_rolegrant)"', sql), sql


@pytest.mark.parametrize("url", ALL_ENDPOINTS)
def test_expired_grant_and_anonymous_get_403(url):
    assert client("ops_lead", name="u_expired", expired=True).get(url).status_code == 403
    r = Client().get(url)
    assert r.status_code == 403 and "<table" not in r.content.decode()


@pytest.mark.parametrize("role", READ_ONLY_ROLES)
@pytest.mark.parametrize("url", ["/ops/metrics/", "/api/ops/metrics"])
def test_metrics_need_operations_metrics_not_just_read(role, url):
    c = client(role)
    r = c.get(url)
    assert r.status_code == 403
    body = r.content.decode()
    for word in ("Numerator", "numerator", 'metrics":', "<table", "as_of"):
        assert word not in body
    # the metrics link is hidden for these roles
    page = c.get("/ops/timeline/?window_min=abc").content.decode()
    assert 'href="/ops/metrics/"' not in page


@pytest.mark.parametrize("role", METRICS_ROLES)
def test_metrics_link_shown_to_metrics_roles(role):
    page = client(role).get("/ops/timeline/?window_min=abc").content.decode()
    assert 'href="/ops/metrics/"' in page


@pytest.mark.parametrize("url", [*ALL_ENDPOINTS])
def test_get_only_no_write_endpoints(url):
    c = client("ops_lead")
    assert c.post(url).status_code == 405
    assert c.put(url).status_code == 405
    assert c.delete(url).status_code == 405


# ------------------------------------------------------------------ bad parameters, wording, chrome (run now)
@pytest.mark.parametrize("role", READ_ROLES)
@pytest.mark.parametrize("url", BAD_PARAM_PAGES)
def test_bad_parameters_are_400_inside_the_snapshot_with_as_of_and_banner(role, url):
    if "metrics" in url and role not in METRICS_ROLES:
        pytest.skip("metrics is 403 for this role (tested above)")
    r = client(role).get(url)
    assert r.status_code == 400
    body = r.content.decode()
    assert "As of <b>" in body and "SYNTHETIC DEMO" in body
    assert not FORBIDDEN_WORDS.search(body), FORBIDDEN_WORDS.search(body)


@pytest.mark.parametrize(
    "url",
    [
        "/api/ops/timeline?window_min=0",
        "/api/ops/metrics?start=2026-01-02&end=2026-01-01",
        "/api/ops/exceptions?cursor=eyJ4IjoxfQ",
    ],
)
def test_api_bad_parameters_are_400(url):
    r = client("ops_lead").get(url)
    assert r.status_code == 400 and "detail" in r.json()


def test_templates_never_use_forbidden_wording_or_green_ready_badges():
    import pathlib

    root = pathlib.Path(views.__file__).parent / "templates" / "operations"
    files = sorted(root.glob("*.html"))
    assert len(files) >= 6
    for f in files:
        text = f.read_text()
        assert not FORBIDDEN_WORDS.search(text), (f.name, FORBIDDEN_WORDS.search(text))
        assert "green" not in text.lower() and "badge ready" not in text
        # no write forms: only GET forms (filters) are allowed in the command center templates
        for m in re.finditer(r"<form[^>]*>", text, re.IGNORECASE):
            assert 'method="get"' in m.group(0).lower(), (f.name, m.group(0))
    assert "Suggested follow-up owner" in (root / "incidents.html").read_text()
    assert "Suggested follow-up owner" in (root / "exceptions.html").read_text()
    assert not FORBIDDEN_WORDS.search(views.DEMO_BANNER)


def test_page_has_no_post_form_except_the_global_logout():
    body = client("ops_lead").get("/ops/timeline/?window_min=abc").content.decode()
    posts = re.findall(r"<form[^>]*method=\"post\"[^>]*>", body, re.IGNORECASE)
    assert posts == ['<form method="post" action="/logout/">']


# ------------------------------------------------------------------ transactions (EM requirement 3; run now)
def test_deployed_middleware_snapshot_is_outermost_and_first():
    """settings.MIDDLEWARE as deployed: no transaction or savepoint is open around the snapshot."""
    assert "django.middleware.transaction" not in " ".join(settings.MIDDLEWARE).lower()
    c = client("ops_lead")
    with captured() as seen:
        r = c.get("/ops/timeline/?window_min=abc")
    assert r.status_code == 400
    assert SET_SNAPSHOT in seen
    assert not any("SAVEPOINT" in s.upper() for s in seen)


@pytest.fixture
def atomic_requests() -> Iterator[None]:
    db = connection.settings_dict
    old = db.get("ATOMIC_REQUESTS", False)
    db["ATOMIC_REQUESTS"] = True
    try:
        yield
    finally:
        db["ATOMIC_REQUESTS"] = old


@pytest.mark.parametrize("url", [*BAD_PARAM_PAGES, "/api/ops/timeline?window_min=0"])
def test_atomic_requests_true_snapshot_still_outermost(atomic_requests, url):
    """With ATOMIC_REQUESTS=True the views opt out (non_atomic_requests), so the snapshot is the outermost
    transaction and the request is served normally from it."""
    assert connection.settings_dict["ATOMIC_REQUESTS"] is True
    with captured() as seen:
        r = client("ops_lead").get(url)
    assert (
        r.status_code == 400
    )  # the view ran inside the snapshot (parameter error after the permission check)
    assert SET_SNAPSHOT in seen and not any("SAVEPOINT" in s.upper() for s in seen)


@pytest.mark.parametrize("url", ALL_ENDPOINTS)
def test_atomic_requests_true_still_403_without_permission(atomic_requests, url):
    assert client().get(url).status_code == 403


@pytest.mark.parametrize(
    "view",
    [
        views.overview,
        views.exceptions_page,
        views.incidents_page,
        views.timeline_page,
        views.metrics_page,
        api.overview,
        api.exceptions,
        api.incidents,
        api.timeline,
        api.metrics,
    ],
)
def test_nested_transaction_fails_closed_with_503_and_no_data(view):
    """If something still wraps the view in a transaction, read_snapshot refuses and the view returns 503
    without reading anything (never a page built outside the snapshot)."""
    request = RequestFactory().get("/")
    request.user = make_user("u_nested", "ops_lead")
    with captured() as seen, transaction.atomic():
        r = view(request)
    assert r.status_code == 503
    assert "<table" not in r.content.decode() and "as_of" not in r.content.decode()
    assert SET_SNAPSHOT not in seen and seen == []


def test_views_are_marked_non_atomic():
    for v in [
        views.overview,
        views.exceptions_page,
        views.incidents_page,
        views.timeline_page,
        views.metrics_page,
        api.overview,
        api.exceptions,
        api.incidents,
        api.timeline,
        api.metrics,
    ]:
        assert getattr(v, "_non_atomic_requests", None), v


# ------------------------------------------------------------------ cursor
def test_cursor_round_trip_and_tamper():
    from datetime import UTC, datetime

    t1 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 18, 9, 1, tzinfo=UTC)
    tok = views.encode_cursor(["2", "x"], 2, t1, t2)
    d = views.decode_cursor(tok)
    assert d == {"after": ["2", "x"], "page": 2, "first_at": t1, "prev_at": t2}
    page1 = views.encode_cursor(None, 1, t1, t2)
    for bad in ["", "abc", "!!!!", tok[:-3], page1, "eyJ4IjoxfQ"]:
        with pytest.raises(views.BadParam):
            views.decode_cursor(bad)


# ------------------------------------------------------------------ pages with data (need Engineers B and C)
def world(prefix: str = "ou_", robots: int = 3) -> Any:
    """A small synthetic world with several open exceptions: a departed run with an open interruption, an
    untriaged incident with an unacknowledged escalation, upcoming confirmed visits inside the readiness
    lookahead, and one missing threshold (THRESHOLD_NOT_CONFIGURED)."""
    from execution import escalation as esc
    from operations.management.commands.seed_operations import THRESHOLDS
    from tests.operations_world import advance, depart_new, ops_world, report

    w = ops_world(prefix, robots=robots)
    for key, value in THRESHOLDS.items():
        set_setting(key, value)
    set_setting("ops_readiness_lookahead_min", str(90 * 1440))  # the world's visits are weeks ahead
    from fleet.models import DeploymentSetting

    DeploymentSetting.objects.filter(key="ops_inspection_grace_min").delete()
    w.run = depart_new(w, "R-02")
    advance(w, w.run, "session_paused")
    w.incident = report(w, "R-03")
    esc.escalate_incident(
        w.ops, incident_code=w.incident, to_role="engineering_lead", requested_action="SYNTHETIC look"
    )
    return w


def _rows(body: str, attr: str) -> list[str]:
    return re.findall(rf'<tr {attr}="([^"]*)"', body)


def _exception_rows(body: str) -> list[tuple[str, str]]:
    return re.findall(r'<tr data-code="([^"]*)" data-ref="([^"]*)"', body)


def _as_of(body: str) -> str:
    m = re.search(r'data-as-of="([^"]+)"', body)
    assert m, "no as-of on the page"
    return m.group(1)


def _main(body: str) -> str:
    return body.split("<main>", 1)[1]


@needs_bc
def test_every_read_role_renders_every_page_with_as_of_banner_and_wording():
    world()
    for role in READ_ROLES:
        c = client(role, name=f"u_{role}")
        urls = [*PAGES, "/ops/metrics/"] if role in METRICS_ROLES else PAGES
        for url in urls:
            r = c.get(url)
            assert r.status_code == 200, (role, url, r.status_code)
            body = r.content.decode()
            assert "As of <b>" in body and _as_of(body)
            assert "SYNTHETIC DEMO" in body
            found = FORBIDDEN_WORDS.search(_main(body))
            assert not found, (role, url, found)
            assert "badge ready" not in body and "green" not in body.lower()
        for url in APIS + (["/api/ops/metrics"] if role in METRICS_ROLES else []):
            r = c.get(url)
            assert r.status_code == 200, (role, url)
            assert r.json()["as_of"] and r.json()["synthetic_demo"] is True


@needs_bc
def test_rows_and_content_present():
    w = world()
    c = client("ops_lead")
    overview = c.get("/ops/").content.decode()
    assert {"R-01", "R-02", "R-03"} <= set(_rows(overview, "data-robot"))
    assert "Ready to deploy" not in overview and "Available to deploy" not in overview
    exc = c.get("/ops/exceptions/").content.decode()
    codes = {code for code, _ in _exception_rows(exc)}
    for code in (
        "INTERRUPTION_OPEN",
        "INCIDENT_UNTRIAGED",
        "ESCALATION_UNACKNOWLEDGED",
        "THRESHOLD_NOT_CONFIGURED",
    ):
        assert code in codes, (code, codes)
    assert "Suggested follow-up owner" in exc
    inc = c.get("/ops/incidents/").content.decode()
    assert _rows(inc, "data-incident") == [w.incident] and "Suggested follow-up owner" in inc
    tl = c.get("/ops/timeline/?window_min=10080").content.decode()
    assert w.run.code in _rows(tl, "data-run")
    met = c.get("/ops/metrics/").content.decode()
    assert len(_rows(met, "data-metric")) >= 5 and "N/A" in met


@needs_bc
def test_every_link_resolves_for_the_ops_lead_and_action_urls_for_their_owner_role():
    world()
    lead = client("ops_lead")
    hrefs: set[str] = set()
    for url in [*PAGES, "/ops/timeline/?window_min=129600", "/ops/metrics/"]:
        body = _main(lead.get(url).content.decode())
        hrefs |= {h for h in re.findall(r'href="([^"]+)"', body) if h.startswith("/")}
    assert any(h.startswith("/execution/incidents/") for h in hrefs)
    assert any(h.startswith("/execution/runs/") for h in hrefs)
    assert any(h.startswith("/readiness/visits/") for h in hrefs)
    for h in sorted(hrefs):
        r = lead.get(h.replace("&amp;", "&"))
        assert r.status_code == 200, (h, r.status_code)
    items = lead.get("/api/ops/exceptions").json()["items"]
    assert items
    owners: dict[str, Client] = {}
    for e in items:
        if e["action_url"] is None:
            assert e["code"] in ("CUSTODY_UNKNOWN", "THRESHOLD_NOT_CONFIGURED"), e
            continue
        role = e["owner_role"]
        if role not in owners:
            owners[role] = client(role, name=f"owner_{role}")
        assert owners[role].get(e["action_url"]).status_code == 200, (role, e["action_url"])


@needs_bc
def test_api_and_page_have_the_same_order_as_of_and_coverage():
    w = world()
    c = client("ops_lead")
    page = c.get("/ops/exceptions/").content.decode()
    data = c.get("/api/ops/exceptions").json()
    assert _exception_rows(page) == [(e["code"], e["entity_ref"]) for e in data["items"]]
    keys = [(e["priority"], e["since"], e["code"], e["entity_ref"]) for e in data["items"]]
    assert keys == sorted(keys)
    assert set(data) >= {"as_of", "items", "readiness_coverage", "next_cursor", "page"}
    assert data["readiness_coverage"]["complete"] is True and "data-coverage" not in page
    pairs = [
        ("/ops/", "/api/ops/overview", "data-robot", lambda d: [r["robot"] for r in d["robots"]]),
        (
            "/ops/incidents/",
            "/api/ops/incidents",
            "data-incident",
            lambda d: [i["code"] for i in d["incidents"]],
        ),
        (
            "/ops/timeline/?window_min=10080",
            "/api/ops/timeline?window_min=10080",
            "data-run",
            lambda d: [x["run"] for x in d["timeline"]["runs"]],
        ),
        (
            "/ops/timeline/?window_min=10080",
            "/api/ops/timeline?window_min=10080",
            "data-visit",
            lambda d: [x["visit"] for x in d["timeline"]["visits"]],
        ),
        (
            "/ops/metrics/",
            "/api/ops/metrics",
            "data-metric",
            lambda d: [m["key"] for m in d["metrics"]["items"]],
        ),
    ]
    for page_url, api_url, attr, extract in pairs:
        body = c.get(page_url).content.decode()
        d = c.get(api_url).json()
        assert _rows(body, attr) == extract(d), page_url
        assert d["as_of"] and _as_of(body)
    assert w.run.code in [
        x["run"] for x in c.get("/api/ops/timeline?window_min=10080").json()["timeline"]["runs"]
    ]


@needs_bc
def test_same_snapshot_time_on_page_and_api_body():
    """`as_of` is the snapshot's transaction_timestamp(), the same field name and format on both."""
    world()
    c = client("ops_lead")
    from datetime import datetime

    for page_url, api_url in zip([*PAGES, "/ops/metrics/"], [*APIS, "/api/ops/metrics"], strict=True):
        p = datetime.fromisoformat(_as_of(c.get(page_url).content.decode()))
        a = datetime.fromisoformat(c.get(api_url).json()["as_of"])
        assert p.tzinfo is not None and a.tzinfo is not None and p <= a  # page first, then a newer snapshot


@needs_bc
def test_pagination_continuity_new_snapshot_note_and_api_parity(monkeypatch):
    world()
    c = client("ops_lead")
    full = [(e["code"], e["entity_ref"]) for e in c.get("/api/ops/exceptions").json()["items"]]
    assert len(full) >= 4
    monkeypatch.setattr(views, "PAGE_SIZE", 2)
    # HTML: follow the next links
    seen: list[tuple[str, str]] = []
    url, page_no, first_as_of = "/ops/exceptions/", 1, None
    while url:
        body = c.get(url).content.decode()
        rows = _exception_rows(body)
        assert 0 < len(rows) <= 2
        seen += rows
        if page_no == 1:
            first_as_of = _as_of(body)
            assert "data-continuation" not in body
        else:
            assert "data-continuation" in body and f"Page {page_no} is a new snapshot" in body
            assert "page 1 was as of" in body
        m = re.search(r'href="(/ops/exceptions/\?cursor=[^"]+)" rel="next"', body)
        url = m.group(1) if m else ""
        page_no += 1
    assert seen == full and first_as_of
    # API: follow next_cursor; same pages, and an API cursor works on the page (one cursor format)
    api_seen: list[tuple[str, str]] = []
    cursor = None
    pages = 0
    while True:
        d = c.get("/api/ops/exceptions", {"cursor": cursor} if cursor else {}).json()
        pages += 1
        api_seen += [(e["code"], e["entity_ref"]) for e in d["items"]]
        assert d["page"] == pages and d["new_snapshot"] is (pages > 1)
        if pages == 2:
            assert d["first_page_as_of"] and d["previous_page_as_of"]
            assert _exception_rows(c.get("/ops/exceptions/", {"cursor": cursor}).content.decode()) == [
                (e["code"], e["entity_ref"]) for e in d["items"]
            ]
        cursor = d["next_cursor"]
        if not cursor:
            break
    assert api_seen == full


@needs_bc
def test_partial_readiness_coverage_banner_on_page_and_api(monkeypatch):
    """More qualifying visits than the evaluator cap: banner on the inbox and the timeline, the same coverage
    in the API, and a link to the readiness list. (Cap lowered to 1 to keep the world small.)"""
    ex = importlib.import_module("operations.exceptions")
    rm = importlib.import_module("operations.readmodels")

    w = world()
    from datetime import timedelta

    from tests.planning_world import DAY
    from tests.readiness_world import new_confirmed_visit

    new_confirmed_visit(w, robot="R-01", d=DAY + timedelta(days=30))  # a second qualifying visit
    monkeypatch.setattr(ex, "READINESS_CAP", 1)
    monkeypatch.setattr(rm, "READINESS_CAP", 1)
    c = client("ops_lead")
    data = c.get("/api/ops/exceptions").json()
    cov = data["readiness_coverage"]
    assert (
        cov["complete"] is False and cov["evaluated"] == 1 and cov["not_evaluated"] == cov["qualifying"] - 1
    )
    page = c.get("/ops/exceptions/").content.decode()
    n = cov["not_evaluated"]
    assert 'data-coverage="partial"' in page
    assert f"Readiness exceptions are incomplete: {n} upcoming visit" in page
    assert 'href="/readiness/"' in _main(page).split("data-coverage", 1)[1]
    tl = c.get("/ops/timeline/?window_min=129600").content.decode()
    tl_api = c.get("/api/ops/timeline?window_min=129600").json()
    assert tl_api["readiness_coverage"]["complete"] is False and 'data-coverage="partial"' in tl
    assert "Not evaluated" in tl


@needs_bc
def test_partial_banner_from_coverage_dict(monkeypatch):
    """The banner is driven only by readiness_coverage.complete (UI unit check, independent of world size)."""
    ex = importlib.import_module("operations.exceptions")

    real = ex.open_exceptions

    def partial(at, *, limit=None, after=None):
        page = real(at, limit=limit, after=after)
        cov = {"qualifying": 14, "evaluated": 10, "not_evaluated": 4, "complete": False}
        return type(page)(items=page.items, readiness_coverage=cov, next_cursor=page.next_cursor)

    monkeypatch.setattr(ex, "open_exceptions", partial)
    world()
    c = client("field_operator")
    page = c.get("/ops/exceptions/").content.decode()
    assert "Readiness exceptions are incomplete: 4 upcoming visits were not evaluated" in page
    assert c.get("/api/ops/exceptions").json()["readiness_coverage"]["not_evaluated"] == 4


def _count(c: Client, url: str) -> int:
    with captured() as seen:
        r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    assert SET_SNAPSHOT in seen
    assert not [
        s
        for s in seen
        if re.search(
            r"\b(INSERT|UPDATE|DELETE|FOR UPDATE|FOR SHARE|nextval|set_config|pg_advisory)\b", s, re.I
        )
    ]
    return len(seen)


@needs_bc
def test_statement_budget_per_endpoint_constant_in_fleet_size():
    """Every statement of the request (session, user, snapshot, permission, page chrome, read models) counted.
    Plain endpoints <= 12; the readiness-evaluating ones (exceptions, timeline, metrics - metrics counts open
    exceptions) <= 12 + 10 x E with E pinned by Engineer B; all constant between a small and a larger world."""
    E = importlib.import_module("operations.exceptions").READINESS_EVALUATOR_STATEMENTS
    from tests.operations_world import advance, depart_new, report

    w = world()
    c = client("ops_lead")
    plain = ["/ops/", "/ops/incidents/", "/api/ops/overview", "/api/ops/incidents"]
    evaluating = [
        "/ops/exceptions/",
        "/api/ops/exceptions",
        "/ops/timeline/",
        "/api/ops/timeline",
        "/ops/metrics/",
        "/api/ops/metrics",
    ]
    # with the readiness evaluator running (the world's visits are inside the lookahead): 12 + 10 x E
    for u in evaluating:
        n = _count(c, u)
        assert n <= 12 + 10 * E, (u, n)
    # without evaluator runs every endpoint is within the plain budget, and constant in fleet size
    set_setting("ops_readiness_lookahead_min", "1")
    small = {u: _count(c, u) for u in plain + evaluating}
    run = depart_new(w, "R-01")
    advance(w, run, "at_site")
    report(w, "R-01")
    report(w, "R-02")
    big = {u: _count(c, u) for u in plain + evaluating}
    for u, n in small.items():
        assert n <= 12, (u, n)
    assert small == big, (small, big)
