"""v2 03B-2: the regional dispatch board (S1), Review options and reassign (S2) and the message polish (S6).

docs/14_v2_03b_design_spec.md and docs/13_v2_phase03_contract.md 13.4 item 5 and 13.5. Proven here:

* Needs attention is ordered blocked, then paused, then at risk; then by departure proximity; then by visit code.
* The status words are exactly "Available for planning", "At risk", "Blocked", "Paused", each with its shape.
* Rows lead with the home's name and the crew's names; codes come after, in a quieter style.
* Hub scoping: a hub A viewer sees no hub B rows (and the reverse); a regional viewer sees both.
* The board reads a FIXED number of statements, whatever the number of hubs and visits.
* The review panel lists its checks and never writes; a save goes ONLY through the guarded plan command, whose own
  checks (and, behind them, the database triggers) refuse an infeasible choice.
* After a save the page says "What changed" and "Planning feasible: not cleared to depart".
* No template hard-codes a zone abbreviation such as "PST".

SYNTHETIC data only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client

from fleet import services as fs
from operations import pauses as op_pauses
from planning import board as pb
from planning import services as ps
from planning.models import Visit, VisitRevision
from platform_core.models import AuditEvent, Hub
from tests.planning_world import DAY, at, build_planning, plan
from tests.test_v2_03a_transfers import _hubs

pytestmark = pytest.mark.django_db(transaction=True)
BACKEND = Path(__file__).resolve().parent.parent
BOARD = f"/planning/board/?day={DAY.isoformat()}"
User = get_user_model()


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _html(r: Any) -> str:
    assert r.status_code == 200, (r.status_code, r.content[:300])
    return str(r.content.decode())


def _names(w: SimpleNamespace) -> None:
    for user, first, last in (
        (w.ops, "Jordan", "Lee"),
        (w.field, "Amara", "Okafor"),
        (w.field2, "Mateo", "Silva"),
    ):
        User.objects.filter(pk=user.pk).update(first_name=first, last_name=last)


def _row(html: str, code: str) -> str:
    m = re.search(rf'<a class="vrow"[^>]*data-row="{code}".*?</a>', html, re.S)
    assert m, f"no row for {code}"
    return m.group(0)


def _attention(html: str) -> list[str]:
    section = html.split('id="attention"', 1)[1].split('id="hubs"', 1)[0]
    return re.findall(r'<article class="bcard" data-status="[^"]+" data-visit="([^"]+)"', section)


def _visit(w: SimpleNamespace, h0: int, hub: str = "HUB-1") -> str:
    return str(
        ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(DAY, h0), on_site_end=at(DAY, h0 + 1), travel_out_min=30,
            travel_back_min=30, tasks=["T-04"], required_scopes=["presence"], hub_code=hub,
        )["entity_id"]
    )  # fmt: skip


@pytest.fixture
def w() -> SimpleNamespace:
    w = build_planning(robots=3)
    _names(w)
    return w


# ================================================================== ordering and words
def _fake_row(code: str, status: str, depart_h: int) -> pb.Row:
    t = datetime(2026, 12, 8, depart_h, tzinfo=UTC)
    return pb.Row(
        visit=None,  # type: ignore[arg-type]
        code=code, hub_id=1, hub_name="Hub", hub_code="H", home="Home", home_code="S", start=t, end=t,
        depart=t, crew="", robot="", vehicle="", zone="", status=status,
    )  # fmt: skip


def test_attention_order_is_severity_then_departure_then_code():
    rows = [
        _fake_row("V-9", "at_risk", 7),
        _fake_row("V-5", "paused", 9),
        _fake_row("V-2", "blocked", 12),
        _fake_row("V-1", "blocked", 12),
        _fake_row("V-3", "blocked", 8),
        _fake_row("V-4", "at_risk", 6),
        _fake_row("V-6", "paused", 7),
    ]
    ordered = [r.code for r in sorted(rows, key=pb.attention_key)]
    # blocked first (earliest departure first, then visit code), then paused, then at risk; never newest first
    assert ordered == ["V-3", "V-1", "V-2", "V-6", "V-5", "V-4", "V-9"]


def test_attention_on_the_page_puts_blocked_before_paused(w, monkeypatch):
    v_paused_late = _visit(w, 14)
    plan(w, v_paused_late, robot="R-02", vehicle="VAN-2")
    v_paused_early = _visit(w, 10)
    plan(w, v_paused_early)
    v_blocked_late = _visit(w, 12)  # requested: no robot, crew or vehicle -> Blocked
    v_blocked_early = _visit(w, 8)
    hub = Hub.objects.get()
    pause = op_pauses.PauseInfo(
        pause_id=1,
        scope="hub",
        reason="Storm warning",
        since=datetime(2026, 12, 8, 17, tzinfo=UTC),
        hub_id=hub.pk,
        zone_id=None,
        hub_name=hub.name,
        hub_code=hub.code,
    )
    monkeypatch.setattr(op_pauses, "board_summary", lambda ids: {hub.pk: [pause]})
    html = _html(_client(w.ops).get(BOARD))
    # blocked cards first; the two paused visits share one cause at one hub, so they are ONE card (D-B1)
    assert _attention(html) == [v_blocked_early, v_blocked_late]
    group = re.search(
        r'<article class="bcard group" data-status="paused" data-group="PAUSE".*?</article>', html, re.S
    )
    assert group and re.findall(r'data-group-visit="([^"]+)"', group.group(0)) == [
        v_paused_early,
        v_paused_late,
    ]
    assert html.index('data-visit="' + v_blocked_late) < html.index('data-group="PAUSE"')
    # the pause banner: what is paused since when, why, and what is still possible
    banner = re.search(r'<p class="pause"[^>]*>(.*?)</p>', html, re.S)
    assert banner and "Departures from" in banner.group(1) and "Storm warning" in banner.group(1)
    assert "Visits can still be planned; none can be authorized to leave." in banner.group(1)
    assert 'data-count="paused"><span class="n">1</span>' in html


def test_status_words_and_shapes(w):
    assert {k: v[0] for k, v in pb.STATUS.items() if k != "departed"} == {
        "blocked": "Blocked",
        "paused": "Paused",
        "at_risk": "At risk",
        "available": "Available for planning",
    }
    assert {pb.STATUS[k][1] for k in ("blocked", "paused", "at_risk", "available")} == {"■", "◆", "▲", "●"}
    ok = _visit(w, 10)
    plan(w, ok)
    blocked = _visit(w, 13)
    html = _html(_client(w.ops).get(BOARD))
    row_ok, row_blocked = _row(html, ok), _row(html, blocked)
    assert 'data-status="available"' in row_ok and "●</span>Available for planning" in row_ok
    assert 'data-status="blocked"' in row_blocked and "■</span>Blocked" in row_blocked
    # status is never colour alone, and nothing on the board reads as permission to leave
    for words in ("Approved to leave", "Cleared to depart", "Ready to go", "Ready to leave"):
        assert words not in html
    assert "Planning is not departure:" in html


def test_rows_lead_with_names_and_quiet_codes(w):
    code = _visit(w, 10)
    plan(w, code)
    row = _row(_html(_client(w.ops).get(BOARD)), code)
    text = re.sub(r"<[^>]+>", " ", row)
    assert text.index("Zone S home") < text.index("S-1") < text.index(code)
    assert '<span class="code">S-1 · ' in row  # the codes are in the quieter style
    assert "Jordan Lee and Amara Okafor" in text and "P-1" not in text and "P-2" not in text
    assert "Robot R-01" in text and "VAN-1" in text
    assert re.search(r'<time datetime="2026-12-08T10:00-08:00">10:00</time>', row)


# ================================================================== hub scope
def test_hub_scoping(w):
    _hubs(w, "bs_")
    a = _visit(w, 10)
    b = _visit(w, 11, hub="HB")
    html_a = _html(_client(w.field).get(BOARD))  # field operator granted at the first hub only
    assert f'data-row="{a}"' in html_a and f'data-row="{b}"' not in html_a and 'data-hub="HB"' not in html_a
    html_b = _html(_client(w.field_b).get(BOARD))
    assert f'data-row="{b}"' in html_b and f'data-row="{a}"' not in html_b
    assert 'data-hub="HB"' in html_b and 'data-hub="HUB-1"' not in html_b
    html_all = _html(_client(w.ops).get(BOARD))  # regional ops lead
    assert f'data-row="{a}"' in html_all and f'data-row="{b}"' in html_all
    assert (
        'data-count="visits"><span class="n">2</span><span class="what">Visits Tue 8 Dec</span>' in html_all
    )
    assert '<span class="scope">all hubs</span>' in html_all  # global counts say "all hubs"
    # ?hub= can only narrow: a hub the viewer holds no role at shows nothing, and says so
    refused = _html(_client(w.field).get(f"{BOARD}&hub=HB"))
    assert (
        "You hold no role at hub HB. Nothing from it is shown." in refused
        and f'data-row="{b}"' not in refused
    )
    narrowed = _html(_client(w.ops).get(f"{BOARD}&hub=HB"))
    assert f'data-row="{b}"' in narrowed and f'data-row="{a}"' not in narrowed
    assert '<span class="scope">Hub B</span>' in narrowed


def _count(c: Client, url: str) -> int:
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        r = c.get(url)
    assert r.status_code == 200
    return len(seen)


def test_statement_count_is_constant_in_hubs_and_visits(w):
    c = _client(w.ops)
    plan(w, _visit(w, 10))
    _visit(w, 13)
    c.get(BOARD)  # warm-up
    small = _count(c, BOARD)
    _hubs(w, "bc_")
    ws_admin = w.admin
    from platform_core import workspace as ws

    ws.create_hub(ws_admin, code="HC", name="Hub C", area="", time_zone="America/Los_Angeles")
    for h0, hub in ((8, "HB"), (9, "HB"), (12, "HC"), (15, "HUB-1"), (16, "HUB-1")):
        _visit(w, h0, hub=hub)
    plan(w, _visit(w, 14), robot="R-02", vehicle="VAN-2")
    html = _html(c.get(BOARD))
    assert len(re.findall(r'<section class="hub"', html)) == 3
    assert len(set(re.findall(r'data-row="([^"]+)"', html))) == 8
    big = _count(c, BOARD)
    assert small == big, (small, big)
    assert big <= 20, big
    print(f"board statements: {big} (1 hub, 2 visits and 3 hubs, 8 visits)")


# ================================================================== review options and reassign
def _save_form(html: str) -> dict[str, str]:
    m = re.search(
        r'<form method="post" action="([^"]+)" class="save" data-save-form="1">(.*?)</form>', html, re.S
    )
    assert m, "no save form"
    assert m.group(1).endswith("/plan/")  # the panel saves through the ONE guarded plan endpoint
    fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', m.group(2)))
    fields["_action"] = m.group(1)
    return fields


WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.I)


def test_review_panel_lists_the_checks_and_never_writes(w):
    code = _visit(w, 10)
    plan(w, code)
    fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="noise in hip")
    c = _client(w.ops)
    c.get(f"/planning/visits/{code}/?review=1")  # warm-up
    before = (AuditEvent.objects.count(), VisitRevision.objects.count())
    seen: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        html = _html(c.get(f"/planning/visits/{code}/?review=1"))
    writes = [
        s for s in seen if WRITE.match(s) and "platform_core_formtoken" not in s and "django_session" not in s
    ]
    assert not writes, writes
    assert (AuditEvent.objects.count(), VisitRevision.objects.count()) == before
    # why: the held robot; the held robot is not possible (with its reason), another one is proposed
    assert "Why this visit needs attention" in html and "Robot R-01 is not ready for planning" in html
    assert re.search(r'data-not-possible="robot">.*?Robot R-01 · not ready: on hold', html, re.S)
    checks = dict(re.findall(r'data-check="([a-z]+)" data-result="([a-z]+)"', html))
    assert set(checks) == {
        "location", "overlap", "crew", "vehicle", "maintenance", "holds", "travel", "pause", "other",
    }  # fmt: skip
    assert checks["holds"] == "pass" and checks["pause"] == "pass"
    # a home with no zone keeps the minutes recorded on the visit (03B-1 rule); never an invented matrix value.
    # v2 03B-3 (EM 03B closure A): it is labelled "Legacy travel estimate" in the at-risk style
    assert checks["travel"] == "risk" and "has no zone yet" in html
    assert "If you save: Planning feasible: not cleared to depart." in html
    assert _save_form(html)["robot"] in ("R-02", "R-03")
    # what the checks say about a choice the plan command would refuse
    bad = _html(
        c.get(f"/planning/visits/{code}/?review=1&robot=R-01&lead=P-1&second=P-2&vehicle=VAN-1&start=&end=")
    )
    assert 'data-proposal="none"' in bad  # the current plan is not a change
    bad = _html(c.get(f"/planning/visits/{code}/?robot=R-01&lead=P-3&second=P-2&vehicle=VAN-1"))
    checks = dict(re.findall(r'data-check="([a-z]+)" data-result="([a-z]+)"', bad))
    assert checks["holds"] == "fail" and 'data-verdict="blocked"' in bad


def test_save_goes_through_the_guarded_plan_command_and_says_not_cleared(w, monkeypatch):
    code = _visit(w, 10)
    plan(w, code)
    fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="noise in hip")
    c = _client(w.ops)
    form = _save_form(_html(c.get(f"/planning/visits/{code}/?review=1")))
    calls: list[dict[str, Any]] = []
    real = ps.plan_visit

    def spy(actor: Any, **kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return real(actor, **kw)

    monkeypatch.setattr(ps, "plan_visit", spy)
    r = c.post(form.pop("_action"), form, follow=True)
    html = _html(r)
    assert len(calls) == 1 and calls[0]["robot_code"] == form["robot"]
    v = Visit.objects.get(code=code)
    assert v.current_revision.robot.code == form["robot"] and v.current_revision.revision_no == 3
    assert r.redirect_chain[-1][0].endswith(f"/planning/visits/{code}/?changed=3#changed")
    assert "Plan saved. Planning feasible: not cleared to depart." in html
    changed = html.split('id="changed"', 1)[1].split("</section>", 1)[0]
    assert "What changed" in changed and "Robot R-01" in changed and f"Robot {form['robot']}" in changed
    assert "Planning feasible: not cleared to depart." in changed


def test_an_infeasible_choice_is_refused_by_the_plan_command(w):
    code = _visit(w, 10)
    plan(w, code)
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="maintenance", reason="noise in hip")
    c = _client(w.ops)
    html = _html(c.get(f"/planning/visits/{code}/?robot=R-02&lead=P-1&second=P-2&vehicle=VAN-1"))
    form = _save_form(html)  # the panel still offers to save it; the command decides
    assert form["robot"] == "R-02" and 'data-verdict="blocked"' in html
    r = c.post(form.pop("_action"), form, follow=True)
    out = _html(r)
    assert Visit.objects.get(code=code).current_revision.robot.code == "R-01"  # unchanged
    assert (
        "Refused (not possible right now)" in out
        and f"The new plan for visit {code} can&#x27;t be saved." in out
    )
    assert "Robot R-02 is not ready for planning" in out and "Nothing was changed." in out
    assert "UTC" not in re.sub(r"<details.*?</details>", "", out, flags=re.S)


def test_the_database_refuses_even_when_the_python_check_is_wrong(w, monkeypatch):
    """The panel's checks and the plan command's own evaluate() are advisory to the database: with evaluate()
    forced to say "feasible", the hub trigger still refuses another hub's vehicle and nothing changes."""
    _hubs(w, "bt_")
    ps.register_vehicle(w.ops, code="VAN-B", robot_capacity=1, restraint_kit="SYN kit", hub_code="HB")
    code = _visit(w, 10)
    c = _client(w.ops)
    html = _html(c.get(f"/planning/visits/{code}/?robot=R-01&lead=P-1&second=P-2&vehicle=VAN-B"))
    assert re.search(r'data-check="vehicle" data-result="fail"', html)
    assert "VAN-B belongs to another hub, so the plan would be refused." in html
    real = ps.evaluate

    def lenient(**kw: Any) -> ps.Plan:
        out = real(**kw)
        out.feasibility, out.blockers = "feasible", []
        return out

    monkeypatch.setattr(ps, "evaluate", lenient)
    form = _save_form(html)
    revisions = VisitRevision.objects.filter(visit__code=code).count()
    out = _html(c.post(form.pop("_action"), form, follow=True))
    assert VisitRevision.objects.filter(visit__code=code).count() == revisions
    assert Visit.objects.get(code=code).status == "requested"
    assert "Refused (not possible right now)" in out and "belongs to another hub" in out


def test_a_viewer_who_cannot_plan_is_told_who_can(w):
    code = _visit(w, 10)
    html = _html(_client(w.field).get(f"/planning/visits/{code}/?review=1"))
    assert "You can&#x27;t change visits at" in html and "Ask an ops lead." in html
    assert 'data-save-form="1"' not in html
    board = _html(_client(w.field).get(BOARD))
    assert "Only an ops lead can change it." in board


def test_empty_states_are_written(w):
    html = _html(_client(w.ops).get("/planning/board/?day=2026-12-09"))
    assert "No visits need attention at all hubs on Wed 9 Dec." in html
    assert "No visits planned for Wed 9 Dec at" in html


def test_day_picker_and_hub_filter_keep_each_other(w):
    _hubs(w, "bd_")
    html = _html(_client(w.ops).get("/planning/board/?day=tomorrow&hub=HB"))
    assert 'href="?day=tomorrow&amp;hub=HB"' in html or 'href="?day=tomorrow&hub=HB"' in html
    assert 'href="?hub=HB"' in html  # Today keeps the hub
    assert '<input type="hidden" name="hub" value="HB">' in html  # the date form keeps the hub
    assert re.search(r'href="\?hub=HUB-1&(amp;)?day=tomorrow"', html)  # a hub link keeps the day
    assert ">Hub B</a>" in html  # hubs are named, not coded


def test_dispatch_opens_on_the_board_with_the_week_list_one_click_away(w):
    html = _html(_client(w.ops).get("/ops/"))
    assert 'href="/planning/board/">Dispatch</a>' in html
    board = _html(_client(w.ops).get("/planning/board/"))
    assert 'href="/planning/">Week list</a>' in board and 'aria-current="page">Board</a>' in board


# ================================================================== time zones
def test_no_template_hard_codes_a_zone_abbreviation():
    offenders = []
    for path in BACKEND.rglob("*.html"):
        if ".venv" in path.parts:
            continue
        text = path.read_text()
        if re.search(r"\b(PST|PDT|EST|EDT|CST|CDT|MST|MDT)\b", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert not offenders, offenders


def test_times_carry_the_iana_zone_label(w, settings):
    code = _visit(w, 10)
    html = _html(_client(w.ops).get(BOARD))
    row = _row(html, code)
    assert "11:00 PST</time>" in row  # December in America/Los_Angeles, from the zone conversion
    assert "times in America/Los_Angeles (" in html
    summer = _html(_client(w.ops).get("/planning/board/?day=2026-07-08"))
    assert "No visits planned for Wed 8 Jul" in summer
    assert pb.day_bounds(DAY)[0].utcoffset() == timedelta(hours=-8)


# ================================================================== design review 2026-11-26 (D-B1 .. D-B10)
def _visible(html: str) -> str:
    """The text a sighted person reads: tags and screen-reader-only spans removed."""
    html = re.sub(r'<span class="sr">.*?</span>', "", html, flags=re.S)
    html = re.sub(r"<(style|script|head)\b.*?</\1>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_db1_same_cause_same_hub_is_one_card_with_the_count(w):
    from tests.fleet_world import set_setting

    set_setting("ops_soc_fresh_min", "60")  # the forecast is on; no battery level is recorded: unknown
    codes = []
    for h0, robot, vehicle in ((8, "R-01", "VAN-1"), (12, "R-02", "VAN-2"), (16, "R-03", "VAN-1")):
        codes.append(_visit(w, h0))
        plan(w, codes[-1], robot=robot, vehicle=vehicle)
    lone = _visit(w, 18)  # requested, nothing chosen: Blocked, its own card
    html = _html(_client(w.ops).get(BOARD))
    cards = re.findall(
        r'<article class="bcard[^"]*" data-status="([a-z_]+)" data-(?:visit|group)="([^"]+)"', html
    )
    assert cards == [("blocked", lone), ("at_risk", "CHARGE_UNKNOWN")], cards  # severity order kept
    group = re.search(r'<article class="bcard group".*?</article>', html, re.S).group(0)
    assert 'data-count="3"' in group and "3 visits at" in group and "charge forecast unknown" in group
    assert re.findall(r'data-group-visit="([^"]+)"', group) == codes  # departure order inside the card
    # ONE action: the place to fix it, for whoever may do it
    actions = re.findall(r'data-action="([a-z-]+)"', group)
    assert (
        actions == ["fix"] and 'href="/hub/charging/?hub=HUB-1"' in group and "Record battery levels" in group
    )
    assert "Need attention</span>" in html and 'data-count="attention"><span class="n">4</span>' in html


def test_db2_a_place_name_is_never_doubled():
    from types import SimpleNamespace as NS

    assert pb.home_name(NS(zone="Piedmont")) == "Piedmont home"
    assert (
        pb.home_name(NS(zone="Fictional practice visit (Emeryville)"))
        == "Fictional practice visit (Emeryville)"
    )
    assert pb.home_name(NS(zone="Lakeside house")) == "Lakeside house"
    assert pb.home_name(NS(zone="")) == "Home"
    assert "home home" not in pb.home_name(NS(zone="Hill home")).lower()


def test_db3_travel_line_shows_the_pair_version_number():
    from planning.models import TravelTime
    from tests.test_v2_03b_zones_travel import _set, _zoned

    w = _zoned("bv_")
    _set(w, w.za, w.zb, 20)  # version 2 of hub side -> hills
    code = _visit(w, 10)
    plan(w, code)
    html = _html(_client(w.ops).get(f"/planning/visits/{code}/?robot=R-02&lead=P-1&second=P-2&vehicle=VAN-1"))
    line = re.search(r'<li class="ck-([a-z]+)" data-check="travel".*?</li>', html, re.S)
    assert line and line.group(1) == "pass", line
    text = _visible(line.group(0))
    assert "20 min out, 35 min back" in text and "versions 2 out and 1 back (demo value)" in text
    ids = set(TravelTime.objects.values_list("pk", flat=True))
    assert not any(f"version {i} " in text for i in ids if i > 2)  # never a row id


def test_db4_no_leave_time_is_computed_from_an_unknown_travel_time(w):
    code = _visit(w, 10)
    unknown = str(
        ps.request_visit(
            w.ops, site_code="S-1", on_site_start=at(DAY, 16), on_site_end=at(DAY, 17), travel_out_min=None,
            travel_back_min=35, tasks=["T-04"], required_scopes=["presence"], hub_code="HUB-1",
        )["entity_id"]
    )  # fmt: skip
    html = _html(_client(w.ops).get(BOARD))
    card = re.search(
        rf'<article class="bcard" data-status="[a-z_]+" data-visit="{unknown}">.*?</article>', html, re.S
    )
    assert card and "Leave time unknown (travel time unknown)" in card.group(0)
    assert "leaves the hub" not in card.group(0) and "16:00</time>" not in card.group(0).split("where")[1]
    known = re.search(
        rf'<article class="bcard" data-status="[a-z_]+" data-visit="{code}">.*?</article>', html, re.S
    )
    assert known and "leaves the hub" in known.group(0) and ">09:30</time>" in known.group(0)
    page = _visible(_html(_client(w.ops).get(f"/planning/visits/{unknown}/")))
    assert "Leave time unknown (travel time unknown)" in page and "leaves the hub" not in page
    assert "unknown min" not in page


def test_db9_travel_words_never_say_unknown_min():
    assert pb.travel_text(None, 35) == "Travel time unknown (out), 35 min back"
    assert pb.travel_text(30, 30) == "30 min out, 30 min back"
    assert pb.travel_text(None, None) == "Travel time unknown"
    assert "unknown min" not in pb.legacy_travel_text(None, 35)


def test_db7_reservations_lead_with_names_and_board_words(w):
    code = _visit(w, 10)
    plan(w, code)
    html = _html(_client(w.ops).get(f"/planning/visits/{code}/"))
    table = html.split("<h2>Reservations</h2>", 1)[1].split("</table>", 1)[0]
    assert '<b>Jordan Lee</b> <span class="code">(P-1)</span>' in table
    assert "<b>Robot R-01</b>" in table and "<b>Vehicle VAN-1</b>" in table
    assert "Person P-1" not in table and "Vehicle trip" in table and "Travel to the home" in table
    assert "Crew shift" in table


def test_db8_a_blocker_no_option_can_fix_is_one_message():
    from tests.test_v2_03b_zones_travel import _zoned

    w = _zoned("bk_", pair=False)  # the home is in a zone with no approved travel time from the hub
    code = _visit(w, 10)
    html = _html(_client(w.ops).get(f"/planning/visits/{code}/?review=1"))
    assert html.count("data-blocker=") == 1 and 'data-blocker="TRAVEL_UNKNOWN"' in html
    panel = html.split('id="review"', 1)[1].split('id="by-hand"', 1)[0]
    review = _visible(panel)
    assert review.count("Travel time unknown") == 1  # one clear message, not repeated in the list
    assert "Settings › Travel times" in review and 'data-action="fix-blocker"' in html
    assert "<summary>Options (none can fix this)</summary>" in html and 'data-options-folded="1"' in html
    assert "data-next-free=" not in panel and "These times work" not in panel and ".." not in review
    assert 'data-save-form="1"' not in html


def test_db5_db6_away_robot_best_option_preselected_and_saved_in_one_post(app_role_conn, monkeypatch):
    from tests.execution_world import to_site
    from tests.test_v2_03a_planning import NEXT
    from tests.test_v2_03a_planning import _visit as visit_at
    from tests.test_v2_03a_transfers import _xworld

    w = _xworld("bo_")
    with app_role_conn.cursor() as cur:
        to_site(cur, w)  # R-01 is away on a run of this hub
    code = visit_at(w)
    assert plan(w, code, robot="R-01", lead="P-1", second="P-2", vehicle="VAN-2")["feasibility"] == "feasible"
    c = _client(w.ops)
    html = _html(c.get(f"/planning/visits/{code}/?review=1"))
    why = _visible(html.split("Why this visit needs attention", 1)[1].split("</ul>", 1)[0])
    assert "away" in why and not re.search(
        r"(At risk|Blocked|Paused)\s*:", why
    )  # D-B5: the chip says it once
    # D-B6: the robot at the hub beats the away one; it is preselected and already checked
    assert 'data-auto="1"' in html and "Best option, ready to save" in html
    form = _save_form(html)
    assert form["robot"] in ("R-02", "R-03") and 'data-keep="1"' in html
    checks = dict(re.findall(r'data-check="([a-z]+)" data-result="([a-z]+)"', html))
    assert checks["location"] == "pass"
    calls: list[str] = []
    real = ps.plan_visit

    def spy(actor: Any, **kw: Any) -> dict[str, Any]:
        calls.append(kw["robot_code"])
        return real(actor, **kw)

    monkeypatch.setattr(ps, "plan_visit", spy)
    r = c.post(form.pop("_action"), form)  # ONE press of Save plan
    assert r.status_code == 302 and calls == [form["robot"]]
    rev = Visit.objects.get(code=code).current_revision
    assert rev.robot.code == form["robot"] and rev.feasibility == "feasible"
    assert NEXT  # the visit is tomorrow, outside the run's window


def test_db10_narrow_order_puts_summary_and_attention_before_the_filters(w):
    _visit(w, 10)
    html = _html(_client(w.ops).get(BOARD))
    main = html.split("<main>", 1)[1]
    pos = {k: main.index(v) for k, v in {
        "title": "<h1>Dispatch</h1>", "summary": 'data-summary="1"', "attention": 'id="attention"',
        "filters": 'id="filters"', "hubs": 'id="hubs"',
    }.items()}  # fmt: skip
    assert pos["title"] < pos["summary"] < pos["attention"] < pos["filters"] < pos["hubs"], pos
    # the planning note is one line with "More"; the day and hub are one compact control at the top
    note = re.search(r'<details class="planline".*?</summary>', html, re.S).group(0)
    assert "Planning is not departure:" in note and "More" in note
    assert 'href="#filters" data-jump="filters"' in html
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert (
        ".planline summary{" in css
        and "min-height:44px" in css.split(".planline summary{", 1)[1].split("}", 1)[0]
    )
