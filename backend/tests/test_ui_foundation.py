"""01A foundation: design tokens, time display, navigation, plain-language refusals, permission-aware pages."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.test import Client

from platform_core.templatetags import ui
from tests.conftest import make_user
from tests.fleet_world import set_setting

BACKEND = Path(__file__).resolve().parent.parent
TEMPLATES = sorted(p for p in BACKEND.glob("**/templates/**/*.html") if ".venv" not in p.parts)


# ------------------------------------------------------------------ time
def test_no_template_labels_a_time_with_utc():
    assert TEMPLATES, "template discovery found nothing"
    offenders = [str(p.relative_to(BACKEND)) for p in TEMPLATES if "(UTC)" in p.read_text()]
    assert not offenders, f'"(UTC)" label found in {offenders}: show workspace time with |local_dt instead'


def test_no_template_formats_an_instant_with_the_date_filter():
    """An instant must go through local_dt / local_time (workspace zone, zone label, one format)."""
    pattern = re.compile(r'\|date:"[^"]*[Hi]:?[^"]*"')
    offenders = [str(p.relative_to(BACKEND)) for p in TEMPLATES if pattern.search(p.read_text())]
    assert not offenders, f"use |local_dt / |local_time instead of |date with a clock format: {offenders}"


def test_local_dt_winter_and_summer_zone_labels():
    assert ui.local_dt(datetime(2026, 11, 21, 18, 48, tzinfo=UTC)) == "Sat 21 Nov, 10:48 PST"
    now_year = datetime.now(ui.workspace_zone()).year
    summer = ui.local_dt(datetime(now_year, 7, 4, 18, 48, tzinfo=UTC))
    assert summer == f"{'Sat' if datetime(now_year, 7, 4).weekday() == 5 else summer[:3]} 4 Jul, 11:48 PDT"


def test_local_dt_other_year_shows_year_and_naive_is_utc():
    assert ui.local_dt(datetime(2027, 2, 19, 18, 48, tzinfo=UTC)) == "Fri 19 Feb 2027, 10:48 PST"
    assert ui.local_dt(datetime(2026, 11, 21, 18, 48)) == "Sat 21 Nov, 10:48 PST"  # naive = stored UTC


def test_local_dt_same_instant_same_text_in_any_zone():
    from zoneinfo import ZoneInfo

    base = datetime(2026, 11, 21, 18, 48, tzinfo=UTC)
    assert ui.local_dt(base) == ui.local_dt(base.astimezone(ZoneInfo("Asia/Tokyo")))
    assert ui.local_dt(base) == ui.local_dt(base.astimezone(ZoneInfo("Europe/London")))


def test_local_dt_accepts_iso_strings_and_ignores_empty():
    assert ui.local_dt("2026-11-21T18:48:00+00:00") == "Sat 21 Nov, 10:48 PST"
    assert ui.local_dt(None) == "" and ui.local_dt("") == "" and ui.local_dt("not a time") == ""


def test_local_time_and_local_date():
    t = datetime(2026, 11, 21, 18, 48, tzinfo=UTC)
    assert ui.local_time(t) == "10:48"
    assert ui.local_date(t) == "Sat 21 Nov"
    assert ui.local_date(datetime(2026, 11, 22, 3, 0, tzinfo=UTC)) == "Sat 21 Nov"  # still the 21st in PST
    assert ui.local_date(datetime(2027, 2, 19).date()) == "Fri 19 Feb 2027"


def test_localize_utc_text_rewrites_stamps_in_sentences():
    out = ui.localize_utc_text("Periodic inspection due 2026-11-21 18:48 UTC, not acknowledged")
    assert out == "Periodic inspection due Sat 21 Nov, 10:48 PST, not acknowledged"
    assert "UTC" not in ui.localize_utc_text("As of 2026-11-21 18:48:05 UTC")
    assert ui.localize_utc_text("nothing to change") == "nothing to change"


# ------------------------------------------------------------------ contrast
def _lum(hex_color: str) -> float:
    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_design_tokens_meet_text_contrast():
    css = (BACKEND / "templates" / "base.html").read_text()
    tokens = dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})", css))
    pairs = [
        ("ink", "bg"), ("ink", "card"), ("soft", "bg"), ("soft", "card"), ("soft", "neutralbg"),
        ("blue", "card"), ("blue", "bluebg"), ("amber", "amberbg"), ("red", "redbg"), ("pos", "posbg"),
        ("head-ink", "head"), ("head-soft", "head"), ("ink", "neutralbg"), ("blue", "bg"),
        # v2 03B-2: the check marks of "What we checked" and the review panel's shapes sit on the card colour
        ("pos", "card"), ("amber", "card"), ("red", "card"),
    ]  # fmt: skip
    bad = {
        f"{a} on {b}": round(_ratio(tokens[a], tokens[b]), 2)
        for a, b in pairs
        if _ratio(tokens[a], tokens[b]) < 4.5
    }
    assert not bad, f"contrast below 4.5:1: {bad}"


def test_phone_header_tints_keep_contrast():
    """03B closure: the phone header's open Menu button has a white background tint over --head; the header
    text and the soft nav text on that tint still meet 4.5:1 (borders and dividers are not text grounds)."""
    css = (BACKEND / "templates" / "base.html").read_text()
    tokens = dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})", css))
    tints = re.findall(r"background:#ffffff([0-9a-f]{2})", css.split("03B closure: on phones")[1])
    assert tints, "the phone header uses white background tints over the header colour"
    head = [int(tokens["head"][i : i + 2], 16) for i in (1, 3, 5)]
    for alpha in {int(t, 16) / 255 for t in tints}:
        blended = "#" + "".join(f"{round(c + (255 - c) * alpha):02X}" for c in head)
        for ink in ("head-ink", "head-soft"):
            assert _ratio(tokens[ink], blended) >= 4.5, (ink, blended)


def test_mobile_css_does_not_hide_a_table_column():
    css = (BACKEND / "templates" / "base.html").read_text()
    assert "nth-child" not in css, "D-04: no rule may hide a table column on small screens"
    assert "table.stack" in css


# ------------------------------------------------------------------ refusals
RAW = (
    "Refused (conflict): Departure not authorized: Readiness incomplete - VISIT_NOT_CONFIRMED: Visit is not "
    "confirmed; PLANNING_CONSENT: Consent is missing; EVIDENCE_MISSING: Record a battery check"
)


def test_refusal_becomes_plain_reasons_with_raw_codes_kept():
    r = ui.parse_refusal(RAW)
    assert r is not None
    assert r["kind"] == "conflict" and r["headline"] == "Departure not authorized: Readiness incomplete"
    assert [x["text"] for x in r["reasons"]] == [
        "Visit is not confirmed",
        "Consent is missing",
        "Record a battery check",
    ]
    assert [x["code"] for x in r["reasons"]] == [
        "VISIT_NOT_CONFIRMED",
        "PLANNING_CONSENT",
        "EVIDENCE_MISSING",
    ]
    assert r["raw"] == RAW


def test_sentence_refusal_and_non_refusal():
    r = ui.parse_refusal("Refused (invalid input): A reason is required")
    assert r is not None and r["reasons"] == [{"code": "", "text": "A reason is required"}]
    assert ui.parse_refusal("Hold HD-1 released") is None


# ------------------------------------------------------------------ navigation and pages
NAV_LABELS = ["Today", "Dispatch", "Fleet", "Customers", "Hub", "Issues"]


def _get(user, url):
    c = Client()
    c.force_login(user)
    return c.get(url, follow=True)


def _nav(html: str) -> list[str]:
    m = re.search(r'<nav class="primary-nav".*?</nav>', html, re.S)
    assert m, "primary navigation missing"
    return re.findall(r">([^<]+)</a>", m.group(0))


@pytest.mark.django_db(transaction=True)
def test_every_page_has_the_global_header_with_user_menu():
    ops = make_user("ui_ops", "ops_lead")
    for url in ("/fleet/", "/planning/", "/planning/board/", "/planning/sites/", "/ops/", "/ops/exceptions/", "/readiness/",
                "/execution/", "/execution/incidents/", "/ops/closeout/"):  # fmt: skip
        html = _get(ops, url).content.decode()
        assert _nav(html) == NAV_LABELS, url
        assert "Sign out" in html and "Settings" in html and "Reports" in html, url
    # the pages under Settings and Reports are one click from the user menu
    html = _get(ops, "/fleet/").content.decode()
    for link in ("/fleet/hold-policies/", "/readiness/policies/", "/execution/policies/",
                 "/execution/escalation-deadlines/", "/ops/metrics/", "/ops/capacity/"):  # fmt: skip
        assert f'href="{link}"' in html, link


@pytest.mark.django_db(transaction=True)
def test_navigation_only_lists_what_the_role_may_open():
    field = make_user("ui_field", "field_operator")
    html = _get(field, "/fleet/").content.decode()
    assert "Reports" not in html and "/ops/metrics/" not in html  # metrics are not for field operators
    assert "Today" in _nav(html)


@pytest.mark.django_db(transaction=True)
def test_current_section_is_marked():
    ops = make_user("ui_ops2", "ops_lead")
    html = _get(ops, "/ops/exceptions/").content.decode()
    assert re.search(r'href="/ops/exceptions/"[^>]*aria-current="page"', html)
    assert re.search(r'<a href="/ops/exceptions/" aria-current="page">Issues</a>', html)


@pytest.mark.django_db(transaction=True)
def test_demo_workspace_badge_only_in_demo_mode_and_no_synthetic_banner():
    ops = make_user("ui_ops3", "ops_lead")
    set_setting("demo_mode", "true")
    html = _get(ops, "/fleet/").content.decode()
    assert html.count("Demo workspace") == 1
    assert "SYNTHETIC DEMO" not in html
    set_setting("demo_mode", "false")
    assert "Demo workspace" not in _get(ops, "/fleet/").content.decode()


@pytest.mark.django_db(transaction=True)
def test_landing_after_login_by_role():
    ops = make_user("ui_ops4", "ops_lead")
    admin = make_user("ui_admin", "admin")
    c = Client()
    c.force_login(ops)
    assert c.get("/")["Location"] == "/ops/"
    c.force_login(admin)
    assert c.get("/")["Location"] == "/settings/setup/"  # 01C ruling: an admin lands on workspace setup
    assert Client().get("/")["Location"].startswith("/login/")
    login = Client().post("/login/", {"username": "ui_ops4", "password": "test-pass-123456"})
    assert login["Location"] == "/"


def test_base_template_renders_a_refusal_message_as_a_list_with_details():
    from django.template import engines

    html = (
        engines["django"]
        .from_string(
            "{% load ui %}{% with r=msg|refusal %}{{ r.headline }}|{{ r.reasons|length }}|{{ r.reasons.0.code }}{% endwith %}"
        )
        .render({"msg": RAW})
    )
    assert html == "Departure not authorized: Readiness incomplete|3|VISIT_NOT_CONFIRMED"
