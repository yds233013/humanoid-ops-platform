"""v2 Phase 04 (EM ruling, contract 13 section 15.11): the read-only Operations briefing (/ops/briefing/).

ONE page for ops leads that answers four questions per visible hub, from the existing read models only:
which robots are unavailable and why, which visits are at risk today, which unresolved incidents need
attention, and what to prioritise (fixed, written rules). It is not an AI and not an agent.

Evidence here (EM minimum exit evidence):

* 12 fixed scenarios, each with expected facts AND prohibited claims (never "ready" when readiness fails,
  never another hub's record for a hub-scoped viewer, never an authorize / start / dispatch action, an explicit
  "Not enough data to say" when the data is missing);
* grounding: the briefing's answers equal the authoritative read models for the same data (dispatch board,
  Today's readiness rows, robot status, incident queue);
* permissions: no cross-hub leak; a user without operations read gets the same 403 as the other ops pages;
* write boundary: GET only (POST, PUT, PATCH and DELETE answer 405) and no form in the page;
* statement budget: bounded and the same whatever the fleet size.

Every test places its visits on the fixed test day (tests.planning_world.DAY) and makes that day "today" by
patching operations.today.local_day, so results never depend on the wall clock. SYNTHETIC data only.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import connection
from django.test import Client

from operations import briefing as bf
from operations import pauses as op_pauses
from operations import readmodels as rm
from operations import scope as opscope
from operations import today
from operations.snapshot import read_snapshot
from planning import board as pboard
from platform_core import interaction as ia
from platform_core.hubscope import HubScope
from platform_core.models import Hub
from tests import operations_world as ow
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.planning_world import DAY, new_visit, plan

pytestmark = pytest.mark.django_db(transaction=True)

URL = "/ops/briefing/"
FOOTER = "This briefing summarises operational records. It cannot authorize, dispatch or control robots."
THRESHOLDS = {
    "ops_custody_verify_grace_min": "30",
    "ops_return_grace_min": "60",
    "ops_inspection_grace_min": "120",
    "ops_readiness_lookahead_min": str(90 * 1440),  # the world's visits are weeks ahead
}
# Claims the briefing may NEVER make, on any page (prohibited claims common to every scenario).
NEVER = (
    "Ready to leave",
    "Ready to go",
    "ready to deploy",
    "Cleared to depart",
    "cleared to leave",
    "safe to deploy",
    "Approved to leave",
    "AI agent",
    "assistant",
)
ACTION_WORDS = re.compile(
    r">\s*(Authorize|Authorise|Start the run|Dispatch now|Start|Depart|Send)\b[^<]*<", re.I
)


@pytest.fixture(autouse=True)
def _setup(transactional_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    set_setting("demo_mode", "true")
    monkeypatch.setattr(today, "local_day", lambda at: DAY)
    monkeypatch.setattr(ia.secrets, "randbelow", lambda n: 1)  # no opportunistic token clean-up in the counts


def _thresholds() -> None:
    for key, value in THRESHOLDS.items():
        set_setting(key, value)


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def get(user: Any, url: str = URL) -> str:
    r = _client(user).get(url)
    assert r.status_code == 200, (url, r.status_code, r.content[:300])
    return r.content.decode()


def main(body: str) -> str:
    """The page's own content (the site header carries the sign-out form, which is not the briefing's)."""
    return body.split("<main>", 1)[1].split("</main>", 1)[0]


def text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&#x27;", "'").replace("&amp;", "&")


def hub_html(body: str, code: str) -> str:
    m = re.search(rf'<section class="bf-hub" id="hub-{re.escape(code)}".*?</section>', body, re.S)
    assert m, f"no hub section {code}"
    return m.group(0)


def question(body: str, n: int, hub: str = "HUB-1") -> str:
    h = hub_html(body, hub)
    m = re.search(rf'<div class="bf-q[^"]*" data-question="{n}">.*?(?=<div class="bf-q|</section>)', h, re.S)
    assert m, f"no question {n}"
    return m.group(0)


def items(html: str, kind: str) -> dict[str, str]:
    """ref -> data-level of every item of a kind."""
    return dict(re.findall(rf'<li data-kind="{kind}" data-ref="([^"]+)" data-level="([^"]+)">', html))


def item_text(html: str, kind: str, ref: str) -> str:
    m = re.search(rf'<li data-kind="{kind}" data-ref="{re.escape(ref)}".*?<!--/item-->', html, re.S)
    assert m, (kind, ref)
    return text(m.group(0))


def priorities(html: str) -> list[tuple[str, str]]:
    return re.findall(r'<li data-priority-rule="(\d+)" data-ref="([^"]+)">', html)


def check_common(body: str) -> None:
    """The prohibited claims and the write boundary, on every scenario page."""
    m = main(body)
    plain = text(m)
    for words in NEVER:
        assert words.lower() not in plain.lower(), words
    assert "<form" not in m and 'method="post"' not in m.lower()
    assert not ACTION_WORDS.search(m), ACTION_WORDS.search(m)
    assert "/authorize" not in m and "/depart" not in m
    assert FOOTER in plain
    assert "Operations briefing" in plain


def build(robots: int = 5, thresholds: bool = True) -> SimpleNamespace:
    """The readiness world (R-01 on w.visit, DAY 10:00, every check recorded) + the incident policy."""
    w = ow.ops_world("bw_", robots=robots)
    if thresholds:
        _thresholds()
    return w


def confirmed(w: Any, robot: str, h0: int, lead: str = "P-3", vehicle: str = "VAN-2", d: Any = DAY) -> str:
    code = new_visit(w, d=d, h0=h0, h1=h0 + 1)
    plan(w, code, robot=robot, lead=lead, second="P-2", vehicle=vehicle)
    w.p.confirm_visit(w.ops, visit_code=code)
    return code


def _sql_count(user: Any, url: str = URL) -> int:
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    c = _client(user)
    with connection.execute_wrapper(wrapper):
        r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    writes = [
        x
        for x in seen
        if re.search(
            r"\b(INSERT|UPDATE|DELETE|FOR UPDATE|FOR SHARE|nextval|set_config|pg_advisory)\b", x, re.I
        )
    ]
    assert not writes, writes[:3]
    return len(seen)


def execution_world(prefix: str) -> SimpleNamespace:
    from tests.execution_world import build_execution

    w = build_execution(prefix=prefix)
    _thresholds()
    return w


# ================================================================== the 12 fixed scenarios
# Each scenario: the world, the EXPECTED facts, and the PROHIBITED claims (plus check_common on every page).


def test_s01_complete_checklist_is_never_called_ready_and_charge_unknown_says_so():
    """S1. R-01's visit has every check recorded (readiness: checklist complete, waiting for approval).
    Expected: it is not listed at risk; it is counted among visits with no open problem, with "That is not
    permission to leave"; the charge forecast is off, so the briefing says there is not enough data to say.
    Prohibited: "ready" / "approved" / "cleared" wording; the visit in the at-risk list."""
    w = build()
    body = get(w.ops)
    check_common(body)
    q2 = question(body, 2)
    assert w.visit not in items(q2, "visit")
    assert 'data-clear="1"' in q2 and "That is not permission to leave." in text(q2)
    q1 = text(question(body, 1))
    assert "No robot at Main hub is unavailable" in q1
    assert "Not enough data to say whether robots at Main hub have enough charge" in q1
    assert "Checklist complete" not in text(main(body))


def test_s02_readiness_fail_is_at_risk_with_the_checklist_reason():
    """S2. A confirmed visit with no checks recorded: the readiness evaluator does not pass it.
    Expected: "At risk" with the checklist's reason and its source; it is a priority (rule 6).
    Prohibited: the visit counted as having no open problem; any line saying it is ready."""
    w = build()
    v = confirmed(w, "R-03", 14)
    body = get(w.ops)
    check_common(body)
    q2 = question(body, 2)
    assert items(q2, "visit")[v] == "at_risk"
    line = item_text(q2, "visit", v)
    assert "A pre-departure check has not been recorded." in line
    assert "Source: Departure readiness checklist" in line
    assert f"/planning/visits/{v}/" in q2
    assert 'data-clear="1"' in q2  # only R-01's visit is clear, never this one
    assert ("6", v) in priorities(question(body, 4))
    assert "ready" not in line.lower()


def test_s03_held_robot_booked_today_is_unavailable_and_its_visit_blocked():
    """S3. Robot R-04 is booked today with every check recorded, then put on hold.
    Expected: R-04 "Unavailable — On hold" with the hold code and "Robot record · Holds"; its visit is Blocked
    and is priority rule 4. Prohibited: R-04's visit called at risk only, or clear."""
    w = build()
    v = confirmed(w, "R-04", 14)
    w.record_all_evidence(v, robot="R-04")
    hold = ow.place_hold(w, "robot", "R-04")
    body = get(w.ops)
    check_common(body)
    q1 = question(body, 1)
    assert items(q1, "robot")["R-04"] == "unavailable"
    line = item_text(q1, "robot", "R-04")
    assert "On hold. It cannot be planned or leave until the hold is released." in line
    assert "Source: Robot record · Holds" in line and hold in line
    q2 = question(body, 2)
    assert items(q2, "visit")[v] == "blocked"
    assert ("4", v) in priorities(question(body, 4))
    assert 'data-clear="1"' in q2  # R-01's visit only


def test_s04_unresolved_interruption_has_its_own_reason_never_not_at_hub():
    """S4. Run 1 was stopped by the protective fallback and closed with the interruption still open (03D).
    Expected: "R-01 — Unresolved interruption. Departure authorization is unavailable until the interruption
    is resolved." Source: Robot record · Interruption history; priority rule 3.
    Prohibited: the misleading "not at the hub" wording for that robot."""
    from tests.test_v2_03c_protective import _bring_back_and_close, at_site_with_go_check, fallback_stop

    w = execution_world("b4_")
    at_site_with_go_check(w)
    fallback_stop(w)
    _bring_back_and_close(w)
    body = get(w.ops)
    check_common(body)
    q1 = question(body, 1)
    robot = w.robot.code
    assert items(q1, "robot")[robot] == "unavailable"
    line = item_text(q1, "robot", robot)
    assert (
        f"{robot} — Unresolved interruption. Departure authorization is unavailable until the interruption is "
        "resolved." in line
    )
    assert "Source: Robot record · Interruption history" in line
    assert "not at the hub" not in line.lower()
    assert ("3", robot) in priorities(question(body, 4))


def test_s05_unknown_travel_time_puts_the_visit_at_risk_with_the_travel_source():
    """S5. A visit planned without travel times (no travel pair is known for it).
    Expected: listed with the board's travel reason and "Visit plan · Travel times".
    Prohibited: the visit counted as clear; an estimated travel time."""
    w = build()
    v = new_visit(w, h0=15, h1=16, travel=None)
    plan(w, v, robot="R-03", lead="P-3", second="P-2", vehicle="VAN-2")
    body = get(w.ops)
    check_common(body)
    q2 = question(body, 2)
    assert items(q2, "visit")[v] in ("blocked", "at_risk")
    line = item_text(q2, "visit", v)
    assert "Travel time" in line and "Source: Visit plan · Travel times" in line
    assert " min out" not in line  # nothing is estimated


def test_s06_crew_and_robot_gaps_block_a_requested_visit():
    """S6. A visit requested with no robot, crew or vehicle yet.
    Expected: Blocked, with the plan's own reasons (no robot chosen, crew incomplete); priority rule 4.
    Prohibited: "Available for planning" for it; the visit counted as clear."""
    w = build()
    v = new_visit(w, h0=12, h1=13)
    body = get(w.ops)
    check_common(body)
    q2 = question(body, 2)
    assert items(q2, "visit")[v] == "blocked"
    line = item_text(q2, "visit", v)
    assert "No robot is chosen yet." in line
    assert "Two people are needed" in line and "Source: Visit plan · Crew" in line
    assert "Available for planning" not in line
    assert ("4", v) in priorities(question(body, 4))


def test_s07_regional_pause_is_said_once_per_hub_and_pauses_the_visits():
    """S7. Departures from the hub are paused.
    Expected: the pause line under robots ("none can be authorized to leave"), the visits Paused, and
    priority rule 5 naming how many visits it covers. Prohibited: any visit at that hub counted as clear."""
    w = build()
    op_pauses.pause_region(w.ops, hub_code="HUB-1", zone_code=None, reason="SYNTHETIC storm warning")
    body = get(w.ops)
    check_common(body)
    q1 = question(body, 1)
    pause = text(re.search(r'<li data-kind="pause".*?</li>', q1, re.S).group(0))
    assert "storm warning" in pause and "none can be authorized to leave" in pause
    assert "Source: Regional pause record" in pause
    q2 = question(body, 2)
    assert items(q2, "visit")[w.visit] == "paused"
    assert "data-clear" not in q2
    prio = question(body, 4)
    assert [r for r, _ in priorities(prio)][:1] == ["5"]
    assert "It covers 1 visit today." in text(prio)


def test_s08_untriaged_incident_needs_attention_and_comes_first():
    """S8. A fall is reported on R-02 (it is held by the incident's hold).
    Expected: the incident under question 3 with "Not triaged yet" and "No severity decision", its age, the
    hold; R-02 unavailable (on hold); priority rule 2 first. Prohibited: the incident shown as being handled."""
    w = build()
    code = ow.report(w, "R-02", minutes_ago=90)
    body = get(w.ops)
    check_common(body)
    q3 = question(body, 3)
    assert items(q3, "incident")[code] == "attention"
    line = item_text(q3, "incident", code)
    assert "Not triaged yet." in line and "No severity decision." in line
    assert "Severity not decided" in line and "involving robot R-02" in line
    assert "Robot R-02 stays on hold" in line
    assert "data-open-incidents" not in q3
    assert items(question(body, 1), "robot")["R-02"] == "unavailable"
    assert priorities(question(body, 4))[0] == ("2", code)


def test_s09_unaccounted_robot_is_rule_one():
    """S9. R-01 (out on its run) is reported unaccounted for.
    Expected: "Cannot be accounted for" with "Robot record · Custody history", and the first priority is rule 1
    "Find robot R-01 and record where it is." Prohibited: R-01 described as merely out on a visit."""
    from tests.execution_world import raw_incident
    from tests.test_v2_03a_transfers import _app_conn

    w = execution_world("b9_")
    with _app_conn() as conn:
        raw_incident(conn, w, unaccounted=True)
    body = get(w.ops)
    check_common(body)
    q1 = question(body, 1)
    robot = w.robot.code
    assert items(q1, "robot")[robot] == "unavailable"
    line = item_text(q1, "robot", robot)
    assert "Cannot be accounted for." in line and "Source: Robot record · Custody history" in line
    assert "Out on a visit" not in line
    prio = question(body, 4)
    assert priorities(prio)[0] == ("1", robot)
    assert f"Find robot {robot} and record where it is." in text(prio)


def test_s10_hub_scoped_viewer_sees_only_their_hub():
    """S10. Two hubs. A field operator granted only at hub B; a regional ops lead sees both.
    Expected: hub B's robot, visit and incident for the hub B viewer, and both hubs for the ops lead.
    Prohibited: any hub A robot, visit, incident or hub name for the hub B viewer; ?hub=HUB-1 for that viewer
    shows nothing from it."""
    from fleet import services as fs
    from tests.test_v2_03a_transfers import _hubs

    w = build()
    _hubs(w, "b10_")
    fs.register_robot(w.ops, code="RB-1", serial="b10_SN-B1", hub_code="HB")
    from tests.planning_world import at as local_at

    vb = str(
        w.p.request_visit(
            w.ops, site_code="S-1", on_site_start=local_at(DAY, 11), on_site_end=local_at(DAY, 12),
            travel_out_min=30, travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code="HB",
        )["entity_id"]
    )  # fmt: skip
    inc_a = ow.report(w, "R-02")
    inc_b = ow.report(w, "RB-1")
    hub_b = get(w.field_b)
    check_common(hub_b)
    m = main(hub_b)
    assert 'data-hub="HB"' in m and 'data-hub="HUB-1"' not in m
    assert "RB-1" in m and vb in m and inc_b in m
    for other in ("R-01", "R-02", "R-03", w.visit, inc_a, "Main hub", "HUB-1"):
        assert other not in m, other
    refused = main(get(w.field_b, f"{URL}?hub=HUB-1"))
    assert "You hold no role at hub HUB-1" in text(refused)
    for other in ("R-01", "R-02", w.visit, inc_a, 'data-hub="'):
        assert other not in refused, other
    regional = main(get(w.ops))
    assert 'data-hub="HB"' in regional and 'data-hub="HUB-1"' in regional
    assert inc_a in regional and inc_b in regional
    narrowed = main(get(w.ops, f"{URL}?hub=HB"))
    assert 'data-hub="HUB-1"' not in narrowed and inc_a not in narrowed


def test_s11_missing_lookahead_says_not_enough_data_instead_of_guessing():
    """S11. The readiness look-ahead setting is missing, so no checklist is checked.
    Expected: one explicit "Not enough data to say whether 2 visits today at Main hub are at risk" line with
    both visits linked, and a rule 8 priority. Prohibited: either visit called at risk, blocked or clear."""
    w = build(thresholds=False)
    v = confirmed(w, "R-03", 14)
    body = get(w.ops)
    check_common(body)
    q2 = question(body, 2)
    assert items(q2, "visit") == {}
    unknown = text(
        re.search(r'<div class="bf-unknown" data-unknown="readiness">.*?</div>', q2, re.S).group(0)
    )
    assert "Not enough data to say whether 2 visits today at Main hub are at risk" in unknown
    assert "the readiness look-ahead setting is missing" in unknown
    assert w.visit in unknown and v in unknown
    assert "data-clear" not in q2
    assert ("8", "readiness") in priorities(question(body, 4))


def test_s12_robot_out_on_a_run_and_its_departed_visit():
    """S12. R-01 has departed on its run.
    Expected: R-01 under "out on a visit" with the run and visit codes; its departed visit is not a visit at
    risk. Prohibited: R-01 listed as unavailable for a fault; the departed visit at risk."""
    w = execution_world("b12_")
    body = get(w.ops)
    check_common(body)
    q1 = question(body, 1)
    robot = w.robot.code
    assert robot not in items(q1.split("data-out=")[0], "robot")
    out = re.search(r'<details class="fold bf-fold" data-out="1">.*?</details>', q1, re.S)
    assert out and items(out.group(0), "robot") == {robot: "out"}
    line = item_text(out.group(0), "robot", robot)
    assert "Out on a visit." in line and w.run.code in line and w.visit in line
    assert w.visit not in items(question(body, 2), "visit")


# ================================================================== grounding
def _scope(user: Any) -> HubScope:
    from django.test import RequestFactory

    from platform_core.hubscope import hub_scope

    req = RequestFactory().get(URL)
    req.user = user
    return hub_scope(req)


def test_grounding_answers_equal_the_authoritative_read_models():
    """For one mixed world, every answer equals what the authoritative read model says for the same data:
    visits = dispatch board status combined with Today's readiness row; robots = the Robot status rule;
    incidents = the incident queue. Read in ONE snapshot, then compared with the page."""
    w = build()
    v_risk = confirmed(w, "R-03", 14)
    v_held = confirmed(w, "R-04", 18)
    w.record_all_evidence(v_held, robot="R-04")
    ow.place_hold(w, "robot", "R-04")
    v_req = new_visit(w, h0=12, h1=13)
    inc = ow.report(w, "R-02")
    body = get(w.ops)

    scope = _scope(w.ops)
    with read_snapshot() as at:
        board = pboard.build_board(at, scope, DAY, "today")
        t = today.build_today(at, w.ops, scope.ids)
        robots = rm.fleet_overview(at)
        queue = opscope.keep_incidents(list(rm.incident_queue(at)), scope.ids)
    trows = {r.code: r for r in t.visits if r.kind == "visit" and r.day == "today"}

    q2 = question(body, 2)
    shown = items(q2, "visit")
    for row in board.rows:
        if row.status == "departed":
            continue
        expected = bf.visit_level(row, trows.get(row.code))
        if expected in ("blocked", "paused", "at_risk"):
            assert shown.get(row.code) == expected, (row.code, row.status, shown)
        else:
            assert row.code not in shown, row.code
        # never "clear" when the board or readiness says blocked / at risk
        if row.status in ("blocked", "at_risk") or getattr(trows.get(row.code), "status", "") in (
            "blocked",
            "at_risk",
        ):
            assert row.code in shown
    assert shown[v_risk] == "at_risk" and shown[v_held] == "blocked" and shown[v_req] == "blocked"

    q1 = question(body, 1)
    listed = items(q1, "robot")
    for r in robots:
        group = today.robot_group(r.status_rule)
        if group in ("available", "assigned"):
            assert r.robot not in listed, (r.robot, listed)  # no interruption or charge fact in this world
        elif group == "out":
            assert r.robot not in listed
        else:
            assert listed.get(r.robot) == "unavailable", (r.robot, r.status_label)
    assert set(listed) == {"R-02", "R-04"}

    q3 = question(body, 3)
    page_incidents = set(items(q3, "incident"))
    assert page_incidents == {i.code for i in queue} == {inc}
    # the priority list only repeats lines shown above it
    refs = {ref for _, ref in priorities(question(body, 4))}
    assert refs <= set(shown) | set(listed) | page_incidents | {"readiness"} | set(
        re.findall(r'data-kind="pause" data-ref="([^"]+)"', q1)
    )


def test_priority_rules_order_and_limit():
    """Question 4 orders by rule number, then time, then code, and shows at most PRIORITY_LIMIT."""
    hb = bf.HubBriefing(1, "H", "Hub")
    base = DAY
    from tests.planning_world import at as local_at

    for i, h in enumerate((15, 9, 12, 10, 11, 8)):
        hb.visits.append(
            bf.Item("visit", f"V-{i}", "at_risk" if i % 2 else "blocked", f"{h}:00 · Home", f"/v/{i}/",
                    [bf.Reason("", "Something.", "Visit plan")], when=local_at(base, h))
        )  # fmt: skip
    hb.robots.append(
        bf.Item("robot", "R-9", "unavailable", "R-9", "/fleet/R-9/", [bf.interruption_reason("x")])
    )
    bf.prioritise(hb)
    got = [(p.rule.number, p.ref) for p in hb.priorities]
    assert got == [(3, "R-9"), (4, "V-4"), (4, "V-2"), (4, "V-0"), (6, "V-5")]
    assert hb.more_priorities == 2
    assert [r.number for r in bf.RULES] == list(range(1, 9))


# ================================================================== permissions
def test_permissions_match_the_other_ops_pages():
    """No operations read (a hub technician only, or no role) -> the same 403 as /ops/; signed out -> no data.
    A hub-scoped field operator gets only their hub (scenario S10)."""
    build()
    tech = make_user("b_tech_only")
    from accounts.models import RoleGrant

    RoleGrant.objects.create(user=tech, role="hub_technician", hub=Hub.objects.get())
    nobody = make_user("b_nobody")
    for user in (tech, nobody):
        for url in (URL, "/ops/"):
            assert _client(user).get(url).status_code == 403, (user.username, url)
    anon = Client().get(URL)
    assert anon.status_code in (302, 403) and b"bf-hub" not in anon.content


def test_navigation_links_to_the_briefing():
    w = build()
    body = get(w.ops, "/ops/")
    assert '<a href="/ops/briefing/">Briefing</a>' in body
    assert '<a href="/ops/briefing/" aria-current="page">Briefing</a>' in get(w.ops)


# ================================================================== write boundary
def test_only_get_is_allowed_and_nothing_posts():
    w = build()
    c = _client(w.ops)
    for method in ("post", "put", "patch", "delete"):
        r = getattr(c, method)(URL)
        assert r.status_code == 405, (method, r.status_code)
    m = main(get(w.ops))
    assert "<form" not in m and "<button" not in m and "<input" not in m


def test_snapshot_is_read_only():
    """The page runs inside the operations snapshot (REPEATABLE READ READ ONLY) and issues no write."""
    w = build()
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        assert _client(w.ops).get(URL).status_code == 200
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY" in seen


# ================================================================== statement budget
E = __import__("operations.exceptions", fromlist=["x"]).READINESS_EVALUATOR_STATEMENTS
TODAY_BUDGET = 12 + 10 * E + 10 * today.AUTHORIZATION_STATEMENTS
BRIEFING_BUDGET = (
    TODAY_BUDGET + 20 + 30
)  # + the board's fixed statements + the briefing's own (and savepoints)


def test_statement_count_is_constant_in_fleet_size_and_within_budget():
    w = build(robots=5)
    ow.report(w, "R-02")  # the same shapes of data in both counts: one open incident without a visit
    _sql_count(w.ops)  # warm-up
    small = _sql_count(w.ops)
    assert small <= BRIEFING_BUDGET, small
    for n in range(6, 10):
        ow.add_robot(w, f"R-0{n}")
    ow.report(w, "R-06")
    ow.report(w, "R-07")
    new_visit(w, h0=12, h1=13)  # requested, not confirmed: on the board, never evaluated
    ow.place_hold(w, "robot", "R-08")
    body = get(w.ops)
    assert len(items(question(body, 1), "robot")) >= 3
    big = _sql_count(w.ops)
    assert small == big, (small, big)
    print(f"BRIEFING statements: {small} (budget {BRIEFING_BUDGET})")


def test_the_fleet_is_read_once_per_briefing(monkeypatch):
    """v2 06 (query batching, 500-robot benchmark): the briefing asks Today for its visit and issue sections
    only. Before, Today's fleet section ran fleet_overview a second time (the heaviest read at 500 robots, for
    counts the page never shows) and its production-blocker panel ran too. The Today page still reads both."""
    w = build(robots=5)
    ow.report(w, "R-02")
    calls = {"fleet_overview": 0, "production_blockers": 0}
    for name in calls:
        real = getattr(rm, name)

        def counted(*a: Any, _name: str = name, _real: Any = real, **k: Any) -> Any:
            calls[_name] += 1
            return _real(*a, **k)

        monkeypatch.setattr(rm, name, counted)
    body = get(w.ops)
    assert calls == {"fleet_overview": 1, "production_blockers": 0}, calls
    assert 'data-unknown="error-' not in body
    assert len(items(question(body, 1), "robot")) >= 1  # the incident hold on R-02: question 1 still answered
    for name in calls:
        calls[name] = 0
    get(w.ops, f"{URL}?hub={Hub.objects.order_by('code').first().code}")
    assert calls == {"fleet_overview": 1, "production_blockers": 0}, calls
    for name in calls:
        calls[name] = 0
    assert _client(w.ops).get("/ops/").status_code == 200
    assert calls["fleet_overview"] >= 1 and calls["production_blockers"] >= 1, calls  # Today: unchanged


def test_no_wording_claims_an_ai_or_an_agent():
    """The EM ruling: a deterministic generator is never presented as an AI or an agent."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in (
        root / "operations/briefing.py",
        root / "operations/templates/operations/briefing.html",
        root / "operations/templates/operations/_briefing_item.html",
    ):
        src = path.read_text()
        visible = src if path.suffix == ".html" else "\n".join(re.findall(r'"([^"\n]*)"', src))
        for word in (" AI ", "agent", "assistant", "LLM", "chatbot"):
            if path.suffix == ".py" and word == "agent":
                continue  # the module docstring says it is NOT an agent; visible strings are checked below
            assert word not in visible, (path.name, word)
    w = build()
    plain = text(main(get(w.ops)))
    assert " AI" not in plain and "agent" not in plain.lower()
