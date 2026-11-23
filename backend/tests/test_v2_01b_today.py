"""v2 milestone 01B: the "Today" dashboard (/ops/), the robot-status page (/ops/robots/) and Settings >
Production readiness (/ops/production-readiness/). docs/10_v2_phase01_contract.md sections 3, 5 "01B", 7.

Every test places its visits on the fixed test day (tests.planning_world.DAY) and makes that day "today" by
patching operations.today.local_day, so the results never depend on the wall clock. SYNTHETIC data only.
"""

import dataclasses
import logging
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import connection
from django.test import Client

from accounts.roles import PERMISSIONS
from operations import exceptions as ox
from operations import readmodels as rm
from operations import today
from operations.snapshot import SET_SNAPSHOT, read_snapshot
from readiness import commands as rc
from tests import operations_world as ow
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.planning_world import DAY, new_visit, plan
from tests.readiness_world import record_all_evidence

pytestmark = pytest.mark.django_db(transaction=True)

E = ox.READINESS_EVALUATOR_STATEMENTS
THRESHOLDS = {
    "ops_custody_verify_grace_min": "30",
    "ops_return_grace_min": "60",
    "ops_inspection_grace_min": "120",
    "ops_readiness_lookahead_min": str(90 * 1440),  # the world's visits are weeks ahead
}


@pytest.fixture(autouse=True)
def _setup(transactional_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    set_setting("demo_mode", "true")
    monkeypatch.setattr(today, "local_day", lambda at: DAY)


def client(*roles: str, name: str | None = None) -> Client:
    from django.contrib.auth import get_user_model

    username = name or "t_" + "_".join(roles or ("none",))
    user = get_user_model().objects.filter(username=username).first() or make_user(username, *roles)
    c = Client()
    c.force_login(user)
    return c


def _confirmed(w: Any, robot: str, d: Any, h0: int, h1: int, lead: str, vehicle: str) -> str:
    code = new_visit(w, d=d, h0=h0, h1=h1)
    plan(w, code, robot=robot, lead=lead, second="P-2", vehicle=vehicle)
    w.p.confirm_visit(w.ops, visit_code=code)
    return code


def build(robots: int = 5, *, thresholds: bool = True) -> SimpleNamespace:
    """A day with one visit of every status.

    today   V_READY    R-01 10:00  all evidence, no authorization   -> Ready (checklist complete)
            V_RISK     R-03 14:00  no evidence at all              -> At risk
    tomorrow V_APPROVED R-02 10:00 all evidence + authorization    -> Approved to leave
            V_BLOCKED  R-04 14:00  all evidence, then a hold       -> Blocked
    """
    w = ow.ops_world("tw_", robots=robots)
    if thresholds:
        for key, value in THRESHOLDS.items():
            set_setting(key, value)
    w.v_ready = w.visit  # built by build_readiness: R-01 on DAY 10:00-11:00 with all evidence
    w.v_risk = _confirmed(w, "R-03", DAY, 14, 15, "P-3", "VAN-2")
    w.v_approved = _confirmed(w, "R-02", DAY + timedelta(days=1), 10, 11, "P-1", "VAN-1")
    record_all_evidence(w, w.v_approved, robot="R-02")
    w.auth = rc.authorize_departure(w.ops, visit_code=w.v_approved)["code"]
    w.v_blocked = _confirmed(w, "R-04", DAY + timedelta(days=1), 14, 15, "P-3", "VAN-2")
    record_all_evidence(w, w.v_blocked, robot="R-04")
    w.hold = ow.place_hold(w, "robot", "R-04")
    return w


def page(c: Client, url: str = "/ops/") -> str:
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    return r.content.decode()


def rows(body: str) -> dict[str, str]:
    """visit code -> data-status of every visit row."""
    return dict(re.findall(r'<li class="vrow" data-visit="([^"]+)" data-status="([^"]+)"', body))


def row_html(body: str, code: str) -> str:
    m = re.search(rf'<li class="vrow" data-visit="{re.escape(code)}".*?</li>', body, re.S)
    assert m, code
    return m.group(0)


def counts(body: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r'data-count="([^"]+)"><b class="n">(\d+)\+?</b>', body)}


def cards(body: str) -> dict[tuple[str, str], str]:
    return {
        (code, ref): html
        for code, ref, html in re.findall(
            r'<article class="acard[^"]*" data-card="([^"]*)" data-ref="([^"]*)">(.*?)</article>', body, re.S
        )
    }


def visible_text(body: str) -> str:
    """What a person reads: the page text outside every Details fold, with all tags (and so every data-
    attribute) removed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", strip_folds(body)))


def strip_folds(body: str) -> str:
    """The page text outside every Details fold, script and style: what a person reads first."""
    main = body.split("<main>", 1)[1]
    main = re.sub(r"<details.*?</details>", "", main, flags=re.S)
    main = re.sub(r"<(script|style).*?</\1>", "", main, flags=re.S)
    return main


# ------------------------------------------------------------------ counts and statuses equal the records
def test_statuses_and_counts_equal_the_underlying_records():
    w = build()
    body = page(client("ops_lead"))
    assert rows(body) == {
        w.v_ready: "ready",
        w.v_risk: "at_risk",
        w.v_approved: "approved",
        w.v_blocked: "blocked",
    }
    # today's counts: two of the four visits are today (ready, at risk); tomorrow's are listed, not counted
    c = counts(body)
    assert (c["visits"], c["ready"], c["approved"], c["at_risk"], c["blocked"]) == (2, 1, 0, 1, 0)
    # the same statuses straight from the read model the page is built on
    at = _db_now()
    tl = rm.timeline(at, window_min=today.window_minutes(at, DAY))
    by_visit = {v.visit: v for v in tl.visits}
    assert by_visit[w.v_ready].readiness_blockers == [] and by_visit[w.v_ready].readiness_evaluated
    assert by_visit[w.v_risk].readiness_blockers and not by_visit[w.v_risk].hold_codes
    assert by_visit[w.v_blocked].hold_codes == [w.hold]
    # why text is plain words
    risk = row_html(body, w.v_risk)
    assert "A pre-departure check has not been recorded" in risk
    assert "robot R-03" in risk
    blocked = row_html(body, w.v_blocked)
    assert "Robot R-04 is on hold" in blocked
    # every row links to its readiness page
    for code in rows(body):
        assert f'href="/readiness/visits/{code}/"' in row_html(body, code)


def test_robot_counts_equal_the_fleet_overview_and_link_to_the_filtered_page():
    w = build()
    ow.place_hold(w, "robot", "R-05")
    c = client("ops_lead")
    body = page(c)
    groups = fleet_groups()
    expected = {k: 0 for k, _ in today.ROBOT_GROUPS}
    for g in groups.values():
        expected[g] += 1
    assert sum(expected.values()) == 5 and expected["on_hold"] >= 2
    got = counts(body)
    for key, n in expected.items():
        assert got[f"robots-{key}"] == n, (key, got)
        assert f'href="/ops/robots/?group={key}"' in body
    # the filtered detail page shows exactly those robots
    for key in ("on_hold", "available"):
        listed = re.findall(r'<tr data-robot="([^"]*)"', page(c, f"/ops/robots/?group={key}"))
        want = [code for code, g in groups.items() if g == key]
        assert listed == want, key
    # the open-issue count equals the inbox the Issues page and API show
    items = c.get("/api/ops/exceptions").json()["items"]
    assert got["issues"] == len(items)
    assert 'href="/ops/exceptions/"' in body


def test_out_and_back_runs_are_listed_with_their_own_status():
    w = ow.ops_world("tr_", robots=4)
    for key, value in THRESHOLDS.items():
        set_setting(key, value)
    out = ow.depart_new(w, "R-02")
    ow.advance(w, out, "at_site")
    back = ow.depart_new(w, "R-03")
    ow.advance(w, back, "at_hub")
    body = page(client("ops_lead"))
    assert rows(body)[out.visit.code] == "out" and rows(body)[back.visit.code] == "back"
    assert "Out" in row_html(body, out.visit.code) and "Back at the hub" in row_html(body, back.visit.code)
    assert f'href="/execution/runs/{out.code}/"' in row_html(body, out.visit.code)
    got = counts(body)
    assert got["robots-out"] == 2 and got["robots-available"] >= 1


# ------------------------------------------------------------------ approved to leave vs checklist complete
def test_approved_to_leave_shows_the_decision_and_a_complete_checklist_never_says_approved():
    w = build()
    body = page(client("ops_lead"))
    ready = row_html(body, w.v_ready)
    assert "Checklist complete, waiting for approval" in ready
    assert "pproved" not in ready and "data-approved-to-leave" not in ready
    assert "chip-ok" not in ready
    approved = row_html(body, w.v_approved)
    assert 'data-approved-to-leave="1"' in approved
    assert re.search(
        r"Approved to leave by <b>[^<]+</b> at <b>[^<]+</b>, "
        r"valid until <b>[^<]+</b>, for visit <b>" + w.v_approved + r"</b> with robot <b>R-02</b>",
        approved,
    )
    from readiness.models import DepartureAuthorization

    a = DepartureAuthorization.objects.get(code=w.auth)
    assert a.issued_by.username in approved
    from platform_core.templatetags.ui import local_dt

    assert local_dt(a.expires_at) in approved and local_dt(a.issued_at) in approved
    # never green: no state on this page uses the green chip or the "ok" tone
    main = body.split("<main>", 1)[1]
    assert "chip-ok" not in main and "chip-pos" not in main
    assert "chip-info" in approved


def test_an_expired_or_revoked_approval_is_not_shown_as_approved():
    w = build()
    c = client("ops_lead")
    assert rows(page(c))[w.v_approved] == "approved"
    rc.revoke_authorization(w.ops, authorization_code=w.auth, reason="SYNTHETIC test revoke")
    body = page(c)
    assert rows(body)[w.v_approved] == "ready"
    assert "Approved to leave" not in row_html(body, w.v_approved)
    assert counts(body)["approved"] == 0


# ------------------------------------------------------------------ attention cards and role visibility
def _attention_world() -> SimpleNamespace:
    w = build()
    w.incident = ow.report(w, "R-05")  # an untriaged incident (the robot is held)
    return w


def test_every_attention_card_has_a_reason_an_owner_and_a_next_action():
    _attention_world()
    body = page(client("ops_lead"))
    found = cards(body)
    assert found, "no attention cards"
    codes = {code for code, _ in found}
    assert {"INCIDENT_UNTRIAGED", "READINESS_INCOMPLETE_SOON", "HELD_ROBOT_ASSIGNED"} <= codes
    for (code, ref), html in found.items():
        assert "<h3>" in html and "Why it matters:" in html, (code, ref)
        assert "Owner:" in html, (code, ref)
        # a next action: a link to the existing page, or who is waited for
        assert 'data-action="1"' in html or 'data-waiting="1"' in html, (code, ref)
        assert "<summary>Details</summary>" in html, (code, ref)
    triage = next(h for (c, _), h in found.items() if c == "INCIDENT_UNTRIAGED")
    assert "has not been looked at yet" in triage and "Owner:</b> ops lead" in triage
    assert re.search(r'href="/execution/incidents/[^"]+/" data-action="1">Review the incident<', triage)
    # the held-robot card names the visit and the action link opens the robot page
    held = next(h for (c, _), h in found.items() if c == "HELD_ROBOT_ASSIGNED")
    assert "Robot R-04 is on hold but is booked for visit" in held


@pytest.mark.parametrize(
    ("role", "can_triage"),
    [
        ("ops_lead", True),
        ("safety_reviewer", True),
        ("engineering_lead", True),
        ("engineer", False),
        ("founder", False),
        ("field_operator", False),
    ],
)
def test_actions_are_shown_only_to_roles_that_may_take_them(role, can_triage):
    _attention_world()
    assert (role in PERMISSIONS["execution.triage_incident"]) == can_triage
    body = page(client(role))
    triage = next(h for (c, _), h in cards(body).items() if c == "INCIDENT_UNTRIAGED")
    if can_triage:
        assert 'data-action="1"' in triage and "Waiting for" not in triage
    else:
        assert 'data-action="1"' not in triage
        assert "Waiting for the ops lead" in triage
    # the readiness card needs readiness.record_evidence: ops_lead, field_operator and engineer have it
    ready_card = next(h for (c, _), h in cards(body).items() if c == "READINESS_INCOMPLETE_SOON")
    may_record = role in PERMISSIONS["readiness.record_evidence"]
    assert ('data-action="1"' in ready_card) == may_record


def test_the_server_still_refuses_what_the_page_hides():
    w = _attention_world()
    founder = client("founder")
    from django.contrib.auth import get_user_model

    from platform_core.models import FormToken

    FormToken.objects.create(
        token="tk-founder-today", user=get_user_model().objects.get(username="t_founder")
    )
    r = founder.post(
        f"/execution/incidents/{w.incident}/status/",
        {"to_status": "triaged", "_form_token": "tk-founder-today"},
        follow=True,
    )
    assert "Refused" in r.content.decode() or r.status_code == 403
    # nothing changed: the incident is still untriaged on the next Today
    body = page(client("ops_lead"))
    assert any(c == "INCIDENT_UNTRIAGED" for c, _ in cards(body))
    # and the exception API agrees
    items = client("ops_lead").get("/api/ops/exceptions").json()["items"]
    assert any(i["code"] == "INCIDENT_UNTRIAGED" for i in items)


def test_no_raw_codes_as_primary_text_and_no_utc_mislabel():
    _attention_world()
    for role in ("ops_lead", "founder", "engineer"):
        body = page(client(role, name=f"t_raw_{role}"))
        visible = visible_text(body)
        for code in ox.CODES:
            assert code not in visible, (role, code)
        for raw in (
            "READINESS_INCOMPLETE_SOON",
            "EVIDENCE_MISSING",
            "PLANNING_",
            "HOLD_POLICY_NOT_APPROVED",
        ):
            assert raw not in visible, raw
        assert "(UTC)" not in body and " UTC" not in visible
        # the codes are available, but only inside Details folds
        assert "INCIDENT_UNTRIAGED" in body
        assert "SYNTHETIC demo deployment" not in body


def test_cards_are_capped_and_the_rest_is_one_click_away():
    _attention_world()
    body = page(client("ops_lead"))
    assert len(cards(body)) <= today.CARD_LIMIT
    n = counts(body)["issues"]
    shown = len(cards(body))
    if n > shown:
        assert "more on the" in body and 'href="/ops/exceptions/"' in body


# ------------------------------------------------------------------ production readiness line and page
def test_production_blockers_moved_off_today_to_a_settings_page():
    build()
    c = client("ops_lead")
    body = page(c)
    m = re.search(r'data-production-readiness="(\d+)"', body)
    assert m and int(m.group(1)) > 0
    assert re.search(
        rf'<a href="/ops/production-readiness/">Production readiness: {m.group(1)} items? needs? a company '
        r"decision</a>",
        body,
    )
    assert (
        "data-blocker=" not in body and "op-blockers" not in body.split("</style>", 1)[1].split("<main>")[1]
    )
    assert "SYNTHETIC demo deployment" not in body and "SYNTHETIC" not in strip_folds(body)
    # the panel page: same table, new words, reachable from the user menu Settings group
    panel = page(c, "/ops/production-readiness/")
    assert "data-blocker=" in panel and 'data-mode="demo"' in panel
    assert "SYNTHETIC demo deployment" not in panel and "These items block real production use" in panel
    assert "Would block in production" in panel
    assert len(re.findall(r'<tr data-blocker="', panel)) == int(m.group(1))
    menu = re.search(r'<div class="grp">Settings</div>(.*?)<hr>', body, re.S)
    assert menu and 'href="/ops/production-readiness/"' in menu.group(1)
    # the JSON API is unchanged
    api = c.get("/api/ops/production-blockers").json()
    assert len(api["items"]) == int(m.group(1))


def test_robot_status_page_keeps_the_detailed_table_and_is_in_the_fleet_section():
    build()
    c = client("ops_lead")
    body = page(c, "/ops/robots/")
    assert {"R-01", "R-02", "R-03", "R-04", "R-05"} <= set(re.findall(r'<tr data-robot="([^"]*)"', body))
    for head in ("Custody", "Open run", "Holds", "Planning eligibility", "Periodic inspection", "Waiting on"):
        assert f"<th>{head}</th>" in body
    assert "data-blocker=" not in body
    assert re.search(r'<a href="/ops/robots/" aria-current="page">Robot status</a>', body)
    assert 'href="/ops/robots/"' in page(c, "/fleet/")
    assert "Robot status" in body and "As of <b>" in body


# ------------------------------------------------------------------ empty and degraded states
def test_empty_state_says_so_and_links_to_the_planner(monkeypatch):
    ow.ops_world("te_", robots=3)
    set_setting("ops_readiness_lookahead_min", "1")
    monkeypatch.setattr(today, "local_day", lambda at: DAY - timedelta(days=30))
    body = page(client("ops_lead"))
    assert 'data-empty="today"' in body and "No visits today" in body
    assert re.search(r'<a href="/planning/">Open the planner to book one</a>', body)
    assert rows(body) == {}
    assert counts(body)["visits"] == 0  # a real zero: the section loaded and there is nothing today
    assert not re.search(r'data-notice="error', body)


def test_a_failing_read_model_shows_a_notice_never_a_healthy_zero(monkeypatch, caplog):
    build()

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("synthetic read model failure")

    c = client("ops_lead")
    for target, notice, unavailable in (
        ("operations.readmodels.fleet_overview", "Could not load the fleet", "robots"),
        ("operations.readmodels.timeline", "Could not load today&#x27;s visits", "visits"),
        ("operations.today.open_exceptions", "Could not load the list of issues", "issues"),
        ("operations.readmodels.production_blockers", "Could not load production readiness", None),
    ):
        with monkeypatch.context() as m, caplog.at_level(logging.ERROR, logger="operations.today"):
            m.setattr(target, boom)
            caplog.clear()
            r = c.get("/ops/")
            body = r.content.decode()
        assert r.status_code == 200, target  # one failure never blanks the page
        assert notice in body.replace("&#39;", "&#x27;").replace("'", "&#x27;"), (target, notice)
        assert 'data-notice="error-' in body
        assert any("could not load" in x.getMessage() for x in caplog.records), target  # logged
        if unavailable:
            assert f'data-count-unavailable="{unavailable}"' in body, target
        if target.endswith("timeline"):
            assert rows(body) == {} and 'data-count="visits"' not in body
            assert "No visits today" not in body  # not "nothing today": we do not know
        if target.endswith("fleet_overview"):
            assert not [k for k in counts(body) if k.startswith("robots-")]
        if target.endswith("production_blockers"):
            assert "data-production-readiness" not in body
        # the other sections still render
        assert 'data-section="attention"' in body
    # and the page still works for the view that did not fail
    assert page(c)


def test_partial_readiness_coverage_is_announced(monkeypatch):
    build()
    real = rm.timeline

    def partial(at: Any, *, window_min: int) -> Any:
        tl = real(at, window_min=window_min)
        cov = {"qualifying": 14, "evaluated": 10, "not_evaluated": 4, "complete": False, "skipped": False}
        return dataclasses.replace(tl, readiness_coverage=cov)

    monkeypatch.setattr(rm, "timeline", partial)
    body = page(client("ops_lead"))
    assert 'data-notice="coverage"' in body
    assert "Readiness checked for 10 of 14 visits" in body


def test_missing_lookahead_setting_is_announced_not_hidden():
    w = ow.ops_world("tl_", robots=3)
    for key in THRESHOLDS:
        if key != "ops_readiness_lookahead_min":
            set_setting(key, THRESHOLDS[key])
    from fleet.models import DeploymentSetting

    DeploymentSetting.objects.filter(key="ops_readiness_lookahead_min").delete()
    body = page(client("ops_lead"))
    assert 'data-notice="lookahead"' in body
    assert rows(body)[w.visit] == "scheduled"  # not checked: never shown as ready


def test_scheduled_visits_beyond_the_evaluator_cap_are_not_called_ready():
    w = build()
    # nothing evaluated at all (look-ahead of one minute): every visit is Scheduled, none Ready or At risk
    set_setting("ops_readiness_lookahead_min", "1")
    body = page(client("ops_lead"))
    got = rows(body)
    assert got[w.v_ready] == "scheduled" and got[w.v_risk] == "scheduled"
    assert got[w.v_blocked] == "blocked"  # a hold is known without the evaluator
    assert "Checklist not checked yet" in row_html(body, w.v_ready)


# ------------------------------------------------------------------ roles and chrome
@pytest.mark.parametrize("role", sorted(PERMISSIONS["operations.read"]))
def test_every_read_role_renders_today_with_header_as_of_and_no_clearance_words(role):
    build()
    body = page(client(role, name=f"t_read_{role}"))
    assert re.search(r"<h1>Today, [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}</h1>", body)
    assert 'data-as-of="' in body and "As of <b>" in body
    assert "Demo workspace" in body
    main = strip_folds(body)
    assert not re.search(r"\b(cleared|safe|ready to deploy|available to deploy)\b", main, re.I)
    assert "chip-ok" not in body.split("<main>", 1)[1]


def test_header_date_is_the_workspace_local_day():
    build()
    body = page(client("ops_lead"))
    assert f"Today, {DAY.strftime('%a %-d %b')}" in body


def test_roles_without_operations_read_are_refused_or_sent_to_fleet():
    assert client("admin", name="t_admin_only").get("/ops/").status_code == 403
    assert client(name="t_norole").get("/ops/").status_code == 403
    field = client("field_operator", name="t_field_land")
    # 01D J5-1: a login whose only role is field operator lands on its own visits (it still may open /ops/)
    assert field.get("/")["Location"] == "/execution/mine/"
    assert field.get("/ops/").status_code != 403
    # 01C ruling: an admin (no operations.read) lands on workspace setup, not on the fleet list
    assert client("admin", name="t_admin_land").get("/")["Location"] == "/settings/setup/"


def test_layout_is_built_for_a_375px_phone():
    build()
    body = page(client("ops_lead"))
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in body
    css = body.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "@media (max-width:700px)" in css
    mobile = css.split("@media (max-width:700px)")[-2:]
    assert any(".cards{grid-template-columns:1fr}" in m for m in mobile)
    assert any(".vrow{grid-template-columns:1fr}" in m for m in mobile)
    # no fixed pixel widths on the Today components (min-width only on tiles, which wrap)
    assert not re.search(r"\.(vrow|acard|cards|tilegroup)\{[^}]*[^-]width:\d+px", css)
    # tables on the page (none expected) would use the stacked layout; the visit list is a list, not a table
    assert "<table" not in strip_folds(body)


# ------------------------------------------------------------------ statements
def fleet_groups() -> dict[str, str]:
    """robot code -> Today group, straight from the read model inside a real snapshot."""
    with read_snapshot() as at:
        return {r.robot: today.robot_group(r.status_rule) for r in rm.fleet_overview(at)}


def _db_now() -> Any:
    with connection.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


def _count(c: Client, url: str = "/ops/") -> int:
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
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


# Today = timeline (3 + up to 10 evaluations of E) + inbox without readiness (3) + fleet (1) + blockers (1)
# + the approval lookup (1 + up to 10 advisory checks of A statements) + savepoints and page chrome.
A = today.AUTHORIZATION_STATEMENTS
TODAY_BUDGET = 12 + 10 * E + 10 * A


def test_statement_count_is_constant_in_fleet_size_and_within_budget():
    w = build(robots=5)
    c = client("ops_lead")
    small = _count(c)
    assert small <= TODAY_BUDGET, small
    for n in range(6, 10):
        ow.add_robot(w, f"R-0{n}")
    assert len(fleet_groups()) == 9
    big = _count(c)
    assert small == big, (small, big)
    print(f"TODAY statements: {small} (budget {TODAY_BUDGET}, E={E}, A={A})")


def test_today_does_not_run_the_evaluator_twice(monkeypatch):
    build()
    import readiness.evaluator as rev

    calls: list[int] = []
    real = rev.evaluate_readiness

    def spy(visit: Any, at: Any = None) -> Any:
        calls.append(visit.pk)
        return real(visit, at=at)

    import operations.exceptions as oxm

    monkeypatch.setattr(rev, "evaluate_readiness", spy)
    monkeypatch.setattr(oxm, "evaluate_readiness", spy)
    page(client("ops_lead"))
    assert len(calls) == len(set(calls)) <= 10, calls


def test_authorization_status_statement_pin():
    """The Today budget assumes one advisory authorization check costs at most AUTHORIZATION_STATEMENTS."""
    from readiness.authorization import authorization_status
    from readiness.models import DepartureAuthorization

    w = build()
    a = DepartureAuthorization.objects.get(code=w.auth)
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        assert authorization_status(a)["state"] == "usable"
    assert len(seen) <= today.AUTHORIZATION_STATEMENTS, len(seen)
