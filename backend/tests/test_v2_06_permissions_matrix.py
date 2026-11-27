"""v2 Phase 06 permissions review: roles x key pages and APIs, with hub scope (docs/evidence/06_permissions_review.md).

One request per cell. Each cell pins the HTTP status a role gets on a page or API; every page a hub-scoped
viewer can open is also checked for the other hub's records (robot, incident and run codes), so a 200 never
carries cross-hub data. The regional roles see both hubs where the page is regional.

World: the execution world at HUB-1 (robot R-01 departed on run w.run, crew P-1 = the ops lead and P-2 = the
field operator w.field; an incident on that run) plus hub HB with robot RB-1 and an incident on it.

Rows (who):
  ops      regional ops lead (ops_lead, no hub)
  tech_a   hub technician at HUB-1        tech_b   hub technician at HB
  crew_a   field operator at HUB-1, on the run's crew
  field_a  field operator at HUB-1, not on the run's crew
  field_b  field operator at HB
  safety   safety reviewer (regional)     pause_a  safety_pause at HUB-1 only
  admin    workspace admin                norole   signed in, no role
  anon     not signed in

SYNTHETIC data only.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

import pytest
from django.test import Client

from accounts.models import RoleGrant
from execution.models import Incident
from fleet import services as fs
from operations.models import RegionPause
from planning import services as ps
from platform_core.hubscope import OTHER_HUB_TEXT
from tests.conftest import make_user
from tests.execution_world import raw_incident
from tests.test_v2_03a_transfers import _app_conn, _xworld

pytestmark = pytest.mark.django_db(transaction=True)

ROWS = (
    "ops",
    "tech_a",
    "tech_b",
    "crew_a",
    "field_a",
    "field_b",
    "safety",
    "pause_a",
    "admin",
    "norole",
    "anon",
)
HUB_SCOPED = ("tech_a", "tech_b", "crew_a", "field_a", "field_b", "pause_a")
HUB_OF = {"tech_a": "A", "crew_a": "A", "field_a": "A", "pause_a": "A", "tech_b": "B", "field_b": "B"}


@pytest.fixture
def w() -> SimpleNamespace:
    w = _xworld("pm_")
    fs.register_robot(w.ops, code="RB-1", serial="pm_SN-B1", hub_code="HB")
    ps.register_person(w.ops, username=w.field_b.username, code="P-9", hub_code="HB")
    with _app_conn() as conn:
        raw_incident(conn, w)  # on R-01's run (HUB-1)
        raw_incident(conn, w, robot_id=fs._robot("RB-1").pk)  # RB-1 at HB, no run
    w.inc_a = Incident.objects.get(robot__code="R-01").code
    w.inc_b = Incident.objects.get(robot__code="RB-1").code
    w.pause_a = make_user("pm_pause_a")
    RoleGrant.objects.create(user=w.pause_a, role="safety_pause", hub=w.hub_a)
    w.norole = make_user("pm_norole")
    w.users = {
        "ops": w.ops,
        "tech_a": w.tech_a,
        "tech_b": w.tech_b,
        "crew_a": w.field,
        "field_a": w.field2,
        "field_b": w.field_b,
        "safety": w.safety,
        "pause_a": w.pause_a,
        "admin": w.admin,
        "norole": w.norole,
        "anon": None,
    }
    # The other hub's records, by hub: never on a page of a viewer scoped to the other hub.
    w.markers = {"A": ("R-01", w.inc_a, w.run.code), "B": ("RB-1", w.inc_b)}
    return w


def _client(w: SimpleNamespace, row: str) -> Client:
    c = Client()
    if w.users[row] is not None:
        c.force_login(w.users[row])
    return c


def _expect(default: int, **cells: int) -> dict[str, int]:
    out = dict.fromkeys(ROWS, default)
    out["anon"] = 302  # pages send a signed-out visitor to the sign-in page
    out.update(cells)
    return out


# Readers of operations: ops lead, field operators (any hub), safety reviewer. Not hub technicians, not the
# safety_pause-only holder, not the admin, not a person with no role.
READERS = {"ops": 200, "crew_a": 200, "field_a": 200, "field_b": 200, "safety": 200}
# A record at HUB-1: the readers minus the field operator who holds no grant at HUB-1.
HUB_A_RECORD = {**READERS, "field_b": 403}
HUB_B_RECORD = {**READERS, "crew_a": 403, "field_a": 403}


def _pages(w: SimpleNamespace) -> dict[str, tuple[str, dict[str, int]]]:
    v, r = w.run.visit.code, w.run.code
    return {
        "Today": ("/ops/", _expect(403, **READERS)),
        "Operations briefing": ("/ops/briefing/", _expect(403, **READERS)),
        "Issues": ("/ops/exceptions/", _expect(403, **READERS)),
        "Dispatch board": ("/planning/board/", _expect(403, **READERS)),
        "Departure readiness list": ("/readiness/", _expect(403, **READERS)),
        "Readiness of a HUB-1 visit": (f"/readiness/visits/{v}/", _expect(403, **HUB_A_RECORD)),
        "Fleet list": ("/fleet/", _expect(403, **READERS)),
        "Robot at HUB-1": ("/fleet/R-01/", _expect(403, **HUB_A_RECORD)),
        "Robot at HB": ("/fleet/RB-1/", _expect(403, **HUB_B_RECORD)),
        "Hub work queue": (
            "/hub/",
            _expect(403, **READERS, tech_a=200, tech_b=200),
        ),
        "Book a visit (form)": ("/planning/book/", _expect(403, **READERS)),
        "Visit plan (HUB-1)": (f"/planning/visits/{v}/", _expect(403, **HUB_A_RECORD)),
        "Review options (HUB-1)": (f"/planning/visits/{v}/?review=1", _expect(403, **HUB_A_RECORD)),
        "Field run page (HUB-1)": (f"/execution/runs/{r}/", _expect(403, **HUB_A_RECORD)),
        "Field runs list": ("/execution/", _expect(403, **READERS)),
        "My visits": ("/execution/mine/", _expect(403, **READERS)),
        "Incident at HUB-1": (f"/execution/incidents/{w.inc_a}/", _expect(403, **HUB_A_RECORD)),
        "Incident at HB": (f"/execution/incidents/{w.inc_b}/", _expect(403, **HUB_B_RECORD)),
        "Visit dossier (HUB-1)": (f"/execution/visits/{v}/dossier/", _expect(403, **HUB_A_RECORD)),
        "Regional pause page": ("/planning/pause/", _expect(403, **READERS, pause_a=200)),
        "Pause confirm, HUB-1": ("/planning/pause/HUB-1/", _expect(403, ops=200, pause_a=200)),
        "Pause confirm, HB": ("/planning/pause/HB/", _expect(403, ops=200)),
        "Settings: zones of HUB-1": ("/settings/hubs/HUB-1/zones/", _expect(403, ops=200, admin=200)),
        "Settings: zones of HB": ("/settings/hubs/HB/zones/", _expect(403, ops=200, admin=200)),
        "Settings: travel times": ("/settings/travel-times/", _expect(403, ops=200, admin=200)),
        "Settings: workspace setup": ("/settings/setup/", _expect(403, admin=200)),
        "Audit log API": ("/api/audit-events", _expect(403, ops=200, safety=200, admin=200, anon=403)),
    }


def _get(c: Client, path: str) -> tuple[int, str]:
    resp = c.get(path)
    return resp.status_code, resp.content.decode(errors="replace")


def test_every_page_cell_has_the_expected_status_and_no_cross_hub_data(w):
    misses: list[str] = []
    for name, (path, expected) in _pages(w).items():
        for row in ROWS:
            code, body = _get(_client(w, row), path)
            if code != expected[row]:
                misses.append(f"{name} [{path}] as {row}: {code}, expected {expected[row]}")
                continue
            if code == 200 and row in HUB_SCOPED:
                other = "B" if HUB_OF[row] == "A" else "A"
                leaked = [m for m in w.markers[other] if m in body]
                if leaked:
                    misses.append(f"{name} [{path}] as {row}: shows the other hub's {leaked}")
    assert not misses, "\n".join(misses)


def test_a_refused_page_says_why_in_plain_words_and_a_signed_out_visitor_is_sent_to_sign_in(w):
    code, body = _get(_client(w, "tech_a"), "/ops/")
    # Low finding (06 permissions review): the operations pages refuse with a bare sentence, no page chrome.
    assert code == 403 and "You do not have access" in body and "R-01" not in body and "Traceback" not in body
    code, body = _get(_client(w, "field_b"), f"/execution/runs/{w.run.code}/")
    assert code == 403 and OTHER_HUB_TEXT in body and "R-01" not in body
    resp = _client(w, "anon").get("/ops/")
    assert resp.status_code == 302 and resp["Location"].startswith("/login/")


@pytest.mark.parametrize("path", ["/ops/briefing/", "/ops/exceptions/", "/hub/", "/fleet/"])
def test_regional_viewers_see_both_hubs_and_hub_scoped_viewers_only_their_own(w, path):
    for row in ("ops", "safety"):
        body = _get(_client(w, row), path)[1]
        assert "R-01" in body and "RB-1" in body, (row, path)
    for row in ("crew_a", "field_b", "tech_a", "tech_b"):
        code, body = _get(_client(w, row), path)
        if code != 200:
            continue
        own, other = ("R-01", "RB-1") if HUB_OF[row] == "A" else ("RB-1", "R-01")
        assert other not in body, (row, path)


def test_landing_sends_each_role_to_a_page_it_may_open(w):
    """``/`` after sign-in: crews to My visits, hub technicians to the hub queue, readers to Today, the admin to
    setup. A person with no role lands on Fleet, which refuses them in plain words (they hold no role)."""
    landing = {
        "ops": "/ops/",
        "tech_a": "/hub/",
        "tech_b": "/hub/",
        "crew_a": "/execution/mine/",
        "field_b": "/execution/mine/",
        "safety": "/ops/",
        "admin": "/settings/setup/",
    }
    for row, target in landing.items():
        c = _client(w, row)
        resp = c.get("/")
        assert resp.status_code == 302 and resp["Location"] == target, (row, resp.get("Location"))
        assert c.get(target).status_code == 200, (row, target)


def _sync_body(run: str) -> str:
    return json.dumps(
        {"device_id": str(uuid.uuid4()), "run": run, "generation": 1, "unsent_count": 0, "commands": []}
    )


def test_field_api_cells_only_the_crew_and_ops_leads_reach_a_run(w):
    """Snapshot, sync and abandon of the HUB-1 run: the crew and an ops lead get through (abandon of a stale
    generation then answers 409 STALE, after the access check); everyone else gets the same 403 NOT_YOUR_RUN
    with no run details; a signed-out request gets 401."""
    r = w.run.code
    allowed = {"ops", "crew_a"}
    misses: list[str] = []
    for row in ROWS:
        c = _client(w, row)
        snap = c.get(f"/api/field/runs/{r}/snapshot?device={uuid.uuid4()}")
        sync = c.post("/api/field/sync", _sync_body(r), content_type="application/json")
        ab = c.post(
            "/api/field/abandon",
            json.dumps({"run": r, "generation": 7, "seq": 99, "reason": "SYNTHETIC: matrix"}),
            content_type="application/json",
        )
        if row == "anon":
            want = (401, 401, 401)
        elif row in allowed:
            want = (200, 200, 409)
        else:
            want = (403, 403, 403)
        got = (snap.status_code, sync.status_code, ab.status_code)
        if got != want:
            misses.append(f"{row}: snapshot/sync/abandon {got}, expected {want}")
            continue
        if row not in allowed and row != "anon":
            for resp in (snap, sync, ab):
                body = resp.content.decode()
                assert json.loads(body)["error"] == "NOT_YOUR_RUN", (row, body)
                assert "R-01" not in body and w.run.visit.code not in body, (row, body)
        if row in allowed:
            assert "R-01" in snap.content.decode()
            assert json.loads(ab.content)["error"].startswith("STALE"), ab.content
    assert not misses, "\n".join(misses)


def _token(c: Client, path: str) -> str:
    html = c.get(path).content.decode()
    m = re.search(r'name="_form_token" value="([^"]+)"', html)
    assert m, path
    return m.group(1)


def test_pause_writes_follow_the_matrix(w):
    """Pause: the regional ops lead anywhere, safety_pause only at its own hub; nobody else; lift is the
    regional ops lead's only."""
    before = RegionPause.objects.count()
    for row in ("crew_a", "tech_a", "safety", "norole"):
        resp = _client(w, row).post("/planning/pause/HUB-1/", {"reason": "SYNTHETIC", "confirm": "on"})
        assert resp.status_code == 403, row
    resp = _client(w, "pause_a").post("/planning/pause/HB/", {"reason": "SYNTHETIC", "confirm": "on"})
    assert resp.status_code == 403
    assert RegionPause.objects.count() == before

    c = _client(w, "pause_a")
    resp = c.post(
        "/planning/pause/HUB-1/",
        {"reason": "SYNTHETIC: storm", "confirm": "on", "_form_token": _token(c, "/planning/pause/HUB-1/")},
    )
    assert resp.status_code == 302
    paused = RegionPause.objects.order_by("-id").first()
    assert paused is not None and RegionPause.objects.count() == before + 1
    lift = f"/planning/pause/lift/{paused.pk}/"
    assert c.get(lift).status_code == 403  # safety_pause never lifts
    assert _client(w, "crew_a").get(lift).status_code == 403
    assert _client(w, "ops").get(lift).status_code == 200


def test_planning_writes_are_the_ops_leads_only(w):
    """The plan form has no page gate: the guarded plan command refuses anyone but the ops lead, the page says
    so, and no revision is written (with or without a valid one-time form token)."""
    from planning.models import VisitRevision

    v = w.run.visit.code
    before = VisitRevision.objects.count()
    c = _client(w, "crew_a")
    token = _token(c, f"/execution/runs/{w.run.code}/")  # a token from a page the crew may open
    resp = c.post(
        f"/planning/visits/{v}/plan/",
        {"robot": "R-02", "_form_token": token, "reason": "SYNTHETIC"},
        follow=True,
    )
    assert resp.status_code == 200 and "Only the ops lead" in resp.content.decode()
    for row in ("field_a", "tech_a", "safety", "norole", "field_b"):
        resp = _client(w, row).post(f"/planning/visits/{v}/plan/", {"robot": "R-02", "reason": "SYNTHETIC"})
        assert resp.status_code in (302, 403), row
    assert VisitRevision.objects.count() == before


def test_the_audit_log_is_regional_only_and_no_hub_scoped_grant_can_read_it(w):
    """Coordinator item (06-e2e): /api/audit-events is unscoped by hub, so it must stay closed to every
    hub-scoped viewer. Every audit.read role is regional, and the database refuses a hub-qualified grant of any
    of them (check constraint role_grant_hub_scope), so no hub-scoped person can ever hold audit access."""
    from django.db import IntegrityError, transaction

    from accounts.roles import HUB_SCOPED_ROLES, PERMISSIONS

    readers = PERMISSIONS["audit.read"]
    assert not readers & set(HUB_SCOPED_ROLES)
    for role in sorted(readers):
        with pytest.raises(IntegrityError), transaction.atomic():
            RoleGrant.objects.create(user=w.field_b, role=role, hub=w.hub_b)
    for row in ("tech_a", "tech_b", "crew_a", "field_a", "field_b", "pause_a", "norole", "anon"):
        resp = _client(w, row).get("/api/audit-events")
        assert resp.status_code == 403, row
        body = resp.content.decode()
        assert "R-01" not in body and "RB-1" not in body and w.run.code not in body, row
    for row in ("ops", "safety", "admin"):
        rows = json.loads(_client(w, row).get("/api/audit-events").content)
        assert isinstance(rows, list) and rows, row


def test_history_pages_keep_hub_scope(w):
    """Audit-like history a hub-scoped viewer can open: the pause history, the visit dossier and the
    incident detail never show another hub's records."""
    c = _client(w, "ops")
    c.post(
        "/planning/pause/HUB-1/",
        {
            "reason": "SYNTHETIC: storm at hub A",
            "confirm": "on",
            "_form_token": _token(c, "/planning/pause/HUB-1/"),
        },
    )
    assert "SYNTHETIC: storm at hub A" in _get(_client(w, "crew_a"), "/planning/pause/")[1]
    code, body = _get(_client(w, "field_b"), "/planning/pause/")
    assert code == 200 and "SYNTHETIC: storm at hub A" not in body
    assert _get(_client(w, "field_b"), f"/execution/visits/{w.run.visit.code}/dossier/")[0] == 403
    assert _get(_client(w, "field_b"), f"/execution/incidents/{w.inc_a}/")[0] == 403
    assert _get(_client(w, "crew_a"), f"/execution/incidents/{w.inc_b}/")[0] == 403
