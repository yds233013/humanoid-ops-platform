"""v2 03B-3: the measurable design review (docs/13_v2_phase03_contract.md 13.4 item 5) and the EM's 03B closure
requirements, as automated checks where they can be automated.

* Attention ordering: blocked, then paused, then at risk; then the departure; then the visit code.
* Status words: only "Available for planning", "At risk", "Blocked", "Paused" (plus the facts "Departed" and
  "Lifted"); never a word that reads as physical availability or clearance.
* No hard-coded time-zone abbreviation in the new templates and modules.
* Names before codes: no line of the new pages starts with a raw code.
* Every new list has a written empty state.
* EM closure A: a home with no zone shows "Legacy travel estimate: ..." in the at-risk style on the board, the
  review panel and the visit page, and never escapes a regional pause (unknown zone = covered).
* EM closure B: an attention item is resolved in two clicks from the board: Review options (the best option is
  preselected) and Save plan.

SYNTHETIC data only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.test import Client

from fleet import services as fs
from operations import pauses
from planning import board as pb
from planning import zones
from planning.models import Visit
from platform_core.models import Hub
from tests.planning_world import DAY, at, new_visit, plan
from tests.test_v2_03b_zones_travel import _set, _zoned

pytestmark = pytest.mark.django_db(transaction=True)
BACKEND = Path(__file__).resolve().parent.parent
BOARD = f"/planning/board/?day={DAY.isoformat()}"
TZ_NAME = "America/Los_Angeles"
NEW_TEMPLATES = [
    "planning/templates/planning/_next_free.html",
    "planning/templates/planning/book.html",
    "planning/templates/planning/zones.html",
    "planning/templates/planning/travel.html",
    "planning/templates/planning/travel_pair.html",
    "operations/templates/operations/pause.html",
    "operations/templates/operations/pause_confirm.html",
    "operations/templates/operations/pause_lift.html",
]
NEW_MODULES = ["planning/next_free.py", "operations/pause_views.py", "planning/zone_views.py"]
CODE_LEAD = re.compile(r"^(?:[A-Z]{1,4}-\d{2,4}|VAN-\d|EX-\d+|TR-\d+)\b")
FORBIDDEN = re.compile(
    r"(?i)\b(ready to (?:go|leave|depart)|free to (?:go|leave)|available to (?:go|leave|depart)|"
    r"approved to (?:go|leave|depart)|ok to (?:go|leave|depart)|clear(?:ed)? for departure)\b"
)


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _html(r) -> str:
    assert r.status_code == 200, (r.status_code, r.content[:300])
    return str(r.content.decode())


def _lines(html: str) -> list[str]:
    """The text of every line-like element (headings, paragraphs, list items, buttons), tags stripped."""
    html = re.sub(r"<(style|script|head)\b.*?</\1>", "", html, flags=re.S)
    out = []
    for m in re.finditer(r"<(h1|h2|h3|p|li|a class=\"btn[^\"]*\"|button)\b[^>]*>(.*?)</(?:h1|h2|h3|p|li|a|button)>",
                         html, re.S):  # fmt: skip
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        if text:
            out.append(text)
    return out


def _cleared_ok(html: str) -> None:
    text = re.sub(r"<[^>]+>", " ", html)
    assert not FORBIDDEN.search(text), FORBIDDEN.search(text)
    for m in re.finditer(r"(?i)cleared to (depart|leave)", text):
        assert text[max(0, m.start() - 4) : m.start()].lower().endswith("not "), text[
            m.start() - 40 : m.end()
        ]


def _world(prefix: str):
    """Hub HUB-1 with zones ZA (the hub's) and ZB; S-1 in ZB, S-3 in ZA, S-2 with no zone (legacy). On DAY:
    X at S-2 07:00 (R-01; At risk: restricted home; legacy travel), Y at S-1 14:00 (R-02, then held:
    Blocked), Z at S-3 19:00 (R-03; At risk: adults unknown)."""
    w = _zoned(prefix)
    _set(w, w.za, w.za, 15)
    zones.set_site_zone(w.ops, site_code="S-3", hub_code=w.hub.code, zone_code="ZA")
    w.x = new_visit(w, site="S-2", h0=7, h1=8)
    plan(w, w.x, robot="R-01")
    w.y = new_visit(w, site="S-1", h0=14, h1=15, travel=None)
    plan(w, w.y, robot="R-02")
    w.z = new_visit(w, site="S-3", h0=19, h1=20, travel=None)
    plan(w, w.z, robot="R-03")
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="manual", reason="SYN: wrist check")
    return w


# ================================================================== ordering, words, legacy, pause
def test_attention_order_status_words_legacy_label_and_the_unknown_zone_under_a_pause():
    w = _world("dz_")
    c = _client(w.ops)
    html = _html(c.get(BOARD))
    order = re.findall(r'<article class="bcard" data-status="([a-z_]+)" data-visit="([^"]+)"', html)
    assert order == [("blocked", w.y), ("at_risk", w.x), ("at_risk", w.z)], order
    # status words + shapes, and nothing that reads as clearance
    for words, shape in (("Blocked", "■"), ("At risk", "▲")):
        assert re.search(rf'<span class="shape" aria-hidden="true">{shape}</span>{words}</span>', html)
    _cleared_ok(html)
    # EM closure A: the legacy home says so, in the at-risk style (words plus shape), on the board ...
    row = re.search(rf'<a class="vrow"[^>]*data-row="{w.x}".*?</a>', html, re.S).group(0)
    assert (
        '<span class="legacy" data-legacy="1"><span aria-hidden="true">▲</span> Legacy travel estimate: '
        in row
    )
    assert "Legacy travel estimate: 30 min out, 30 min back (this home has no zone yet)" in row
    zoned_row = re.search(rf'<a class="vrow"[^>]*data-row="{w.y}".*?</a>', html, re.S).group(0)
    assert "Legacy travel estimate" not in zoned_row  # a zoned home never falls back to recorded minutes
    # ... and on the visit page
    page = _html(c.get(f"/planning/visits/{w.x}/"))
    assert 'class="pb at_risk" data-legacy="1"' in page and "Legacy travel estimate: 30 min out" in page

    # a pause of zone ZA covers Z (its zone) AND X (no zone: fail closed), never Y (zone ZB)
    pauses.pause_region(w.ops, hub_code=w.hub.code, zone_code="ZA", reason="SYN: road closed")
    html = _html(c.get(BOARD))
    order = re.findall(r'<article class="bcard" data-status="([a-z_]+)" data-visit="([^"]+)"', html)
    assert order == [("blocked", w.y), ("paused", w.x), ("paused", w.z)], order
    assert re.search(r'<span class="shape" aria-hidden="true">◆</span>Paused</span>', html)
    review = _html(c.get(f"/planning/visits/{w.x}/?review=1"))
    why = review.split("Why this visit needs attention", 1)[1].split("</ul>", 1)[0]
    assert 'class="lv-paused"' in why and "are paused" in why
    # the review panel's travel line for the legacy home, when a change is checked
    checked = _html(c.get(f"/planning/visits/{w.x}/?review=1&robot=R-01&lead=P-1&second=P-3&vehicle=VAN-2"))
    travel = re.search(r'<li class="ck-([a-z]+)" data-check="travel".*?</li>', checked, re.S)
    assert travel and travel.group(1) == "risk" and "Legacy travel estimate: 30 min out" in travel.group(0)
    # the attention key is the contract's: severity, departure, code
    rows = pb.build_board(at(DAY, 0), _scope(), DAY, DAY.isoformat()).attention
    assert [pb.attention_key(r) for r in rows] == sorted(pb.attention_key(r) for r in rows)


def _scope():
    from platform_core.hubscope import HubScope

    return HubScope(hubs=list(Hub.objects.all()), regional=True, roles={"ops_lead"})


# ================================================================== two clicks
def test_an_attention_item_is_resolved_in_two_clicks_from_the_board():
    w = _world("d2_")
    c = _client(w.ops)
    html = _html(c.get(BOARD))
    card = re.search(
        rf'<article class="bcard" data-status="blocked" data-visit="{w.y}">.*?</article>', html, re.S
    )
    link = re.search(r'<a class="btn primary" href="([^"]+)" data-action="review">', card.group(0)).group(1)
    page = _html(c.get(link.replace("&amp;", "&")))  # click 1: Review options
    form = re.search(
        r'<form method="post" action="([^"]+)" class="save" data-save-form="1">(.*?)</form>', page, re.S
    )
    assert form, "the best option is preselected, so Save plan is on the first page"
    fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form.group(2)))
    assert fields["robot"] == "R-01"  # the held robot is replaced by the best free one
    r = c.post(form.group(1), fields)  # click 2: Save plan
    assert r.status_code == 302
    rev = Visit.objects.get(code=w.y).current_revision
    assert rev.robot.code == "R-01" and rev.feasibility == "feasible"


# ================================================================== static checks
def test_no_hard_coded_zone_abbreviation_in_the_new_pages_and_modules():
    abbrev = re.compile(r"\b(PST|PDT|EST|EDT|CST|CDT|MST|MDT|GMT|CET|UTC)\b")
    for rel in NEW_TEMPLATES + NEW_MODULES:
        text = (BACKEND / rel).read_text()
        if rel.endswith(".py"):  # string literals only (docstrings and comments are not shown to anyone)
            text = re.sub(r'"""(?:.|\n)*?"""', "", text)
            text = " ".join(re.findall(r"\"[^\"\n]*\"|'[^'\n]*'", text))
        assert not abbrev.search(text), (rel, abbrev.search(text))


def test_every_new_list_has_a_written_empty_state():
    for rel, needle in (
        ("planning/templates/planning/_next_free.html", "nf.empty_text"),
        ("planning/templates/planning/zones.html", "No zones yet"),
        ("planning/templates/planning/travel.html", "No zones yet"),
        ("planning/templates/planning/travel.html", "No hubs to show"),
        ("planning/templates/planning/travel_pair.html", "No value yet"),
        ("operations/templates/operations/pause.html", "Nothing is paused"),
        ("operations/templates/operations/pause.html", "No pauses yet"),
        ("operations/templates/operations/pause.html", "No hubs to show"),
        ("operations/templates/operations/pause_confirm.html", "No visit is waiting to leave there"),
    ):
        assert needle in (BACKEND / rel).read_text(), (rel, needle)
    from planning.next_free import NextFreeResult

    empty = NextFreeResult([], False, days=7, hub_name="Oakland hub")
    assert (
        empty.empty_text
        == "No free time in the next 7 days at Oakland hub. Widen the search or add crew time."
    )


def test_the_new_pages_lead_with_names_and_render_their_empty_states():
    w = _zoned("de_", pair=False)
    c = _client(w.ops)
    pages = {
        "/planning/pause/": ["Nothing is paused", "No pauses yet"],
        f"/settings/hubs/{w.hub.code}/zones/": [],
        "/settings/travel-times/": ["▲ Unknown"],
        f"/settings/travel-times/{w.za.pk}/{w.zb.pk}/": ["No value yet"],
        f"/planning/pause/{w.hub.code}/ZB/": ["No visit is waiting to leave there"],
    }
    for url, needles in pages.items():
        html = _html(c.get(url))
        for needle in needles:
            assert needle in html, (url, needle)
        bad = [line for line in _lines(html) if CODE_LEAD.match(line)]
        assert not bad, (url, bad)
        _cleared_ok(html)
    # the zones page of a hub without zones
    from platform_core import workspace as ws
    from tests.conftest import make_user

    ws.create_hub(make_user("de_admin", "admin"), code="HE", name="Empty hub", area="", time_zone=TZ_NAME)
    assert "No zones yet" in _html(c.get("/settings/hubs/HE/zones/"))
