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
    assert _attention(html) == [v_blocked_early, v_blocked_late, v_paused_early, v_paused_late]
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
    assert "Planning is not departure." in html


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
    # a home with no zone keeps the minutes recorded on the visit (03B-1 rule); never an invented matrix value
    assert checks["travel"] == "pass" and "has no zone yet" in html
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
