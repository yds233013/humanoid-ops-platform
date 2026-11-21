"""01C.3 / 01C.4: customers, homes, assessments, consent and visit booking screens.

Every state-changing form is tested for: success, a refusal in plain words, a server-side denial for each role
that lacks the permission, and a replayed POST. Concurrency (two confirms, two bookings for one robot slot),
"each view calls the intended service command and never writes planning tables itself", and privacy (no field
can hold a name or a medical detail) are covered below. Targeted runs only.
"""

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pytest
from django.conf import settings
from django.db import connection, models
from django.test import Client

from accounts.roles import ALL_ROLES, PERMISSIONS
from planning import forms as pforms
from planning import services as p
from planning.models import ConsentRecord, Reservation, Site, SiteAssessment, Visit
from planning.plain import plain_blocker, role_words
from platform_core.models import FormToken
from tests.conftest import make_user
from tests.planning_world import DAY, TZ, add_site, build_planning, new_visit, plan

pytestmark = pytest.mark.django_db

TAG = re.compile(r"<[^>]+>")


# ------------------------------------------------------------------ helpers
@pytest.fixture
def w():
    return build_planning(prefix="uc_")


def client_as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


_SEEDED = [0]


def seed_token(c: Client, token: str | None = None) -> str:
    """Issue a shared (database) form token for the signed-in user of this client."""
    if token is None:
        _SEEDED[0] += 1
        token = f"seed-{_SEEDED[0]}"
    FormToken.objects.create(token=token, user_id=int(c.session["_auth_user_id"]))
    return token


def post(c: Client, url: str, data: dict[str, Any], *, token: str | None = None, follow: bool = True):
    data = {**data, "_form_token": token or seed_token(c)}
    return c.post(url, data, follow=follow)


def text(resp) -> str:
    return resp.content.decode()


def page_tokens(html: str, action: str) -> list[str]:
    """Tokens inside the form(s) posting to ``action`` on a rendered page."""
    out = []
    for m in re.finditer(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', html, re.S):
        out += re.findall(r'name="_form_token" value="([^"]+)"', m.group(1))
    return out


def primary_text(html: str) -> str:
    """The page with Details folds removed: what a person reads first."""
    return re.sub(r"<details.*?</details>", "", html, flags=re.S)


def roles_without(permission: str) -> list[str]:
    return [r for r in ALL_ROLES if r not in PERMISSIONS[permission]]


def user_with(role: str):
    return make_user(f"u_{role}", role)


# ------------------------------------------------------------------ the form scenarios
@dataclass
class Scenario:
    name: str
    permission: str
    service: str
    url: str
    page: str
    data: dict[str, Any]
    state: Callable[[], Any]
    success: str
    refuse_data: dict[str, Any] | None = None
    refuse_text: str = ""
    expect_change: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def scenarios(w) -> dict[str, Scenario]:
    add_site(w, "S-4", 2, consents=0)  # two adults, no consent yet
    v_req = new_visit(w)
    v_plan = new_visit(w, h0=13, h1=14)
    plan(w, v_plan, robot="R-02", lead="P-3", second="P-2", vehicle="VAN-2")
    v_cancel = new_visit(w, h0=15, h1=16)
    v_new = new_visit(w, h0=8, h1=9)
    s1_consent = ConsentRecord.objects.filter(site__code="S-1", participant_ref="P1").first()
    assert s1_consent is not None
    pending = p.record_assessment(w.field, site_code="S-3", decision="eligible", valid_until=date(2027, 3, 2))
    own = p.record_assessment(w.ops, site_code="S-2", decision="eligible", valid_until=date(2027, 3, 2))
    visit_data = {"robot": "R-03", "lead": "P-1", "second": "P-2", "vehicle": "VAN-1", "reason": "test"}

    def n(model: type[models.Model], **kw: Any) -> Callable[[], int]:
        return lambda: model.objects.filter(**kw).count()

    cancelled = new_visit(w, h0=17, h1=18)
    p.cancel_visit(w.ops, visit_code=cancelled, reason="setup")
    return {
        "create_site": Scenario(
            "create_site",
            "planning.manage_sites",
            "create_site",
            "/planning/sites/new/",
            "/planning/sites/new/",
            {"code": "H-9", "zone": "Zone Q", "tz": TZ, "crm_ref": "CRM-H9", "expected_adults": "2"},
            n(Site, code="H-9"),
            "Home H-9 added",
            {"code": "S-1", "zone": "Z", "tz": TZ, "crm_ref": "CRM-1", "expected_adults": ""},
            "Site S-1 exists",
        ),
        "adults": Scenario(
            "adults",
            "planning.manage_sites",
            "set_expected_adults",
            "/planning/sites/S-3/adults/",
            "/planning/sites/S-3/",
            {"expected_adults": "3"},
            lambda: Site.objects.get(code="S-3").expected_adults,
            "Number of adults saved",
            {"expected_adults": "0"},
            "Nothing was changed",
        ),
        "assessment_new": Scenario(
            "assessment_new",
            "planning.assess",
            "record_assessment",
            "/planning/sites/S-3/assessments/new/",
            "/planning/sites/S-3/",
            {"decision": "eligible", "valid_until": "2027-03-01", "excluded_tasks": "T-09", "supersedes": ""},
            n(SiteAssessment, site__code="S-3"),
            "Assessment recorded",
            {"decision": "eligible", "valid_until": "2001-01-01", "supersedes": ""},
            "valid beyond today",
        ),
        "assessment_approve": Scenario(
            "assessment_approve",
            "planning.approve_assessment",
            "approve_assessment",
            f"/planning/sites/S-3/assessments/{pending['entity_id']}/approve/",
            "/planning/sites/S-3/",
            {},
            lambda: SiteAssessment.objects.filter(site__code="S-3", approval__isnull=False).count(),
            "Assessment approved",
            None,
            "",
            extra={"own_url": f"/planning/sites/S-2/assessments/{own['entity_id']}/approve/"},
        ),
        "consent_new": Scenario(
            "consent_new",
            "planning.consent",
            "record_consent",
            "/planning/sites/S-4/consents/new/",
            "/planning/sites/S-4/",
            {"participant_ref": "P1", "scope": ["presence"], "evidence_ref": "DOC-S4-1"},
            n(ConsentRecord, site__code="S-4"),
            "Consent recorded for P1",
            {"participant_ref": "P1", "scope": ["presence"], "evidence_ref": "DOC-S4-1"},
            "",
        ),
        "consent_withdraw": Scenario(
            "consent_withdraw",
            "planning.consent",
            "withdraw_consent",
            f"/planning/sites/S-1/consents/{s1_consent.pk}/withdraw/",
            "/planning/sites/S-1/",
            {"reason": "customer asked", "confirm": "on"},
            n(ConsentRecord, site__code="S-1", withdrawn_at__isnull=False),
            "withdrawn",
            {"reason": "customer asked"},
            "Tick the box",
        ),
        "book": Scenario(
            "book",
            "planning.plan",
            "request_visit",
            "/planning/book/",
            "/planning/book/",
            {
                "site": "S-1",
                "day": DAY.isoformat(),
                "start": "09:00",
                "end": "10:00",
                "travel_out_min": "30",
                "travel_back_min": "30",
                "tasks": "T-04",
                "scopes": ["presence"],
            },
            lambda: Visit.objects.count(),
            "Visit requested",
            {"site": "S-1", "day": DAY.isoformat(), "start": "10:00", "end": "09:00", "scopes": ["presence"]},
            "must end after it starts",
        ),
        "plan": Scenario(
            "plan",
            "planning.plan",
            "plan_visit",
            f"/planning/visits/{v_new}/plan/",
            f"/planning/visits/{v_new}/",
            {"robot": "R-01", "lead": "P-1", "second": "P-2", "vehicle": "VAN-1"},
            lambda: Reservation.objects.filter(revision__visit__code=v_new, released_at__isnull=True).count(),
            "Plan saved",
            {
                "robot": "R-01",
                "lead": "P-1",
                "second": "P-2",
                "vehicle": "VAN-1",
                "start": "2026-12-08T10:00",
            },
            "both a new start and a new end",
            extra={"cancelled": cancelled, "visit_data": visit_data},
        ),
        "confirm": Scenario(
            "confirm",
            "planning.confirm",
            "confirm_visit",
            f"/planning/visits/{v_plan}/confirm/",
            f"/planning/visits/{v_plan}/",
            {},
            lambda: Visit.objects.get(code=v_plan).status,
            "Visit confirmed",
            None,
            "",
            extra={"requested_url": f"/planning/visits/{v_req}/confirm/"},
        ),
        "cancel": Scenario(
            "cancel",
            "planning.plan",
            "cancel_visit",
            f"/planning/visits/{v_cancel}/cancel/",
            f"/planning/visits/{v_cancel}/cancel/",
            {"reason": "customer moved", "confirm": "on"},
            lambda: Visit.objects.get(code=v_cancel).status,
            f"Visit {v_cancel} cancelled",
            {"reason": "customer moved"},
            "Tick the box",
            extra={"cancelled_url": f"/planning/visits/{cancelled}/cancel/"},
        ),
    }


NAMES = [
    "create_site",
    "adults",
    "assessment_new",
    "assessment_approve",
    "consent_new",
    "consent_withdraw",
    "book",
    "plan",
    "confirm",
    "cancel",
]


@pytest.mark.parametrize("name", NAMES)
def test_success_shows_message_and_changes_state(w, name):
    sc = scenarios(w)[name]
    # approving needs someone other than the assessor (a field operator recorded it): use the ops lead
    user = w.ops if sc.permission != "planning.consent" else w.field
    before = sc.state()
    html = text(post(client_as(user), sc.url, sc.data))
    assert sc.success in html, html[html.find("<main") : html.find("<main") + 1500]
    assert sc.state() != before


@pytest.mark.parametrize("name", NAMES)
def test_denied_server_side_for_every_role_without_the_permission(w, name):
    sc = scenarios(w)[name]
    for role in roles_without(sc.permission):
        before = sc.state()
        html = text(post(client_as(user_with(role)), sc.url, sc.data))
        if "No planning access" in html:  # roles without planning.read are stopped at the page itself (403)
            assert "planning.read" not in html and role not in PERMISSIONS["planning.read"], (role, name)
        else:
            assert "Refused (not allowed)" in html, (role, name)
            assert role_words(sc.permission) in html
        assert sc.state() == before, f"{role} changed state through {name}"


@pytest.mark.parametrize("name", NAMES)
def test_replayed_post_does_nothing_and_says_so(w, name):
    sc = scenarios(w)[name]
    user = w.ops if sc.permission != "planning.consent" else w.field
    c = client_as(user)
    token = seed_token(c, "once")
    first = post(c, sc.url, sc.data, token=token)
    assert sc.success in text(first)
    after_first = sc.state()
    again = post(c, sc.url, sc.data, token=token)
    assert "already submitted" in text(again) and "Nothing was changed" in text(again)
    assert sc.state() == after_first
    forged = post(c, sc.url, sc.data, token="never-issued")
    assert "already submitted" in text(forged)
    assert sc.state() == after_first


@pytest.mark.parametrize("name", [n for n in NAMES if n not in ("assessment_approve", "confirm")])
def test_refusal_is_in_plain_words(w, name):
    sc = scenarios(w)[name]
    user = w.ops if sc.permission != "planning.consent" else w.field
    c = client_as(user)
    if name == "consent_new":  # a second active consent for the same adult
        assert sc.success in text(post(c, sc.url, sc.data))
        sc.refuse_text = "already has an active consent"
    if name == "consent_withdraw":
        pass
    if name == "create_site":
        pass
    before = sc.state()
    html = text(post(c, sc.url, sc.refuse_data or {}))
    assert "Refused" in html and sc.refuse_text in html
    assert "Nothing was changed" in html or sc.refuse_text == "already has an active consent"
    if name != "consent_new":
        assert sc.state() == before
    assert not re.search(r"\b[A-Z]{3,}_[A-Z_]+\b", primary_text(html)), "internal code in primary text"


def test_other_refusals_name_what_to_do(w):
    sc = scenarios(w)
    # the assessor cannot approve their own assessment
    html = text(post(client_as(w.ops), sc["assessment_approve"].extra["own_url"], {}))
    assert "Refused (not allowed)" in html and "cannot approve their own assessment" in html
    # confirming a visit that is only requested
    html = text(post(client_as(w.ops), sc["confirm"].extra["requested_url"], {}))
    assert "Only a planned visit can be confirmed" in html and "Refused (not possible right now)" in html
    # planning a cancelled visit
    cancelled = sc["plan"].extra["cancelled"]
    html = text(post(client_as(w.ops), f"/planning/visits/{cancelled}/plan/", sc["plan"].extra["visit_data"]))
    assert "Visit is cancelled" in html
    # cancelling twice
    html = text(post(client_as(w.ops), sc["cancel"].extra["cancelled_url"], {"reason": "x", "confirm": "on"}))
    assert "Already cancelled" in html


# ------------------------------------------------------------------ pages
def test_customers_list_search_and_empty_state(db):
    ops = make_user("e_ops", "ops_lead")
    c = client_as(ops)
    html = text(c.get("/planning/sites/"))
    assert "No customer homes yet" in html and "Add customer home" in html
    w = build_planning(prefix="lp_")
    c = client_as(w.ops)
    html = text(c.get("/planning/sites/"))
    assert "S-1" in html and "S-2" in html and "S-3" in html
    only = text(c.get("/planning/sites/?q=S-2"))
    assert "S-2" in only and "CRM-S-1" not in only
    none = text(c.get("/planning/sites/?q=zzz-nothing"))
    assert "No homes match your search" in none


def test_site_status_lines_come_from_the_existing_checks(w):
    add_site(w, "S-4", 2, consents=0)
    add_site(w, "S-5", 3, consents=1)
    p.create_site(w.ops, code="S-6", zone="Z", tz=TZ, crm_ref="CRM-6", expected_adults=1)
    c = client_as(w.ops)
    assert "Home ready" in text(c.get("/planning/sites/S-1/"))
    assert "Consent missing for 2 adults" in text(c.get("/planning/sites/S-4/"))
    assert "Consent missing for 2 adults" in text(c.get("/planning/sites/S-5/"))  # 1 of 3 valid
    assert "Number of adults not set" in text(c.get("/planning/sites/S-3/"))
    assert "Home needs an assessment" in text(c.get("/planning/sites/S-6/"))
    lst = text(c.get("/planning/sites/"))
    assert "Home ready" in lst and "Home needs an assessment" in lst
    # withdrawing a consent is reflected at once
    cid = ConsentRecord.objects.get(site__code="S-2").pk
    p.withdraw_consent(w.ops, consent_id=cid, reason="t")
    assert "Consent missing for 1 adult" in text(c.get("/planning/sites/S-2/"))


def test_site_page_shows_history_and_only_actions_the_viewer_may_take(w):
    pending = p.record_assessment(w.field, site_code="S-3", decision="eligible", valid_until=date(2027, 3, 2))
    html = text(client_as(w.ops).get("/planning/sites/S-3/"))
    assert "Approve assessment" in html and "Record assessment" in html and "Record consent" in html
    assert f"/planning/sites/S-3/assessments/{pending['entity_id']}/approve/" in html
    html = text(client_as(w.field).get("/planning/sites/S-3/"))
    assert "Approve assessment" not in html
    assert "Only the" in html and "approve a home assessment" in html
    assert "Record assessment" in html
    html = text(client_as(w.eng).get("/planning/sites/S-3/"))
    assert "Record assessment" in html  # the details fold exists but shows who may act
    assert "Only the field operator or ops lead can record a home assessment." in html
    assert 'action="/planning/sites/S-3/adults/"' not in html


def test_forms_pages_carry_one_token_per_form(w):
    html = text(client_as(w.ops).get("/planning/sites/S-3/"))
    toks = re.findall(r'name="_form_token" value="([^"]+)"', html)
    assert len(toks) == len(set(toks)) >= 3
    assert page_tokens(html, "/planning/sites/S-3/adults/")


def test_booking_flow_request_plan_confirm_plain_english(w):
    c = client_as(w.ops)
    data = {
        "site": "S-1",
        "day": DAY.isoformat(),
        "start": "10:00",
        "end": "11:00",
        "travel_out_min": "30",
        "travel_back_min": "30",
        "tasks": "T-04",
        "scopes": ["presence"],
    }
    resp = post(c, "/planning/sites/S-1/book/", data)
    assert resp.redirect_chain, "success goes to the visit page"
    code = Visit.objects.latest("id").code
    html = text(resp)
    assert "Visit requested" in html and code in html
    assert "No robot is chosen yet." in primary_text(html)
    assert "What to do: Pick a robot below." in primary_text(html)
    assert "ROBOT_MISSING" not in primary_text(html) and "ROBOT_MISSING" in html  # code only in Details
    resp = post(
        c,
        f"/planning/visits/{code}/plan/",
        {"robot": "R-01", "lead": "P-1", "second": "P-2", "vehicle": "VAN-1"},
    )
    html = text(resp)
    assert "Planning feasible" in html and "Nothing is blocking this plan." in html
    resp = post(c, f"/planning/visits/{code}/confirm/", {})
    assert "Visit confirmed" in text(resp) and "not approval to leave" in text(resp)
    assert Visit.objects.get(code=code).status == "confirmed"


def test_conflict_is_explained_in_local_time_with_what_to_do(w):
    v1, v2 = new_visit(w), new_visit(w)
    plan(w, v1)
    c = client_as(w.ops)
    html = text(
        post(
            c,
            f"/planning/visits/{v2}/plan/",
            {"robot": "R-01", "lead": "P-3", "second": "P-2", "vehicle": "VAN-2"},
        )
    )
    prim = primary_text(html)
    assert f"already booked for visit {v1}" in prim and " UTC" not in prim
    assert "PST" in prim or "PDT" in prim
    assert "Pick another time, or choose a different robot, person or vehicle." in prim
    assert "CONFLICT" in html and "CONFLICT" not in prim


def test_plain_blocker_translation_covers_every_service_code():
    import inspect

    from planning.plain import BLOCKERS

    codes = set(re.findall(r'_add\(\s*b,\s*[^,]+,\s*"([A-Z_]+)"', inspect.getsource(p.evaluate)))
    assert len(codes) >= 15, codes
    assert not codes - set(BLOCKERS), codes - set(BLOCKERS)
    assert (
        plain_blocker({"level": "blocked", "code": "NEW_UNKNOWN", "message": "m", "action": "a"})["sentence"]
        == "m"
    )


def test_who_must_act_when_viewer_cannot(w):
    v = new_visit(w)
    plan(w, v)
    html = text(client_as(w.field).get(f"/planning/visits/{v}/"))
    assert "Only the ops lead can confirm visits." in html
    assert "Only the ops lead can request, plan or cancel visits." in html
    assert "Confirm visit</button>" not in html and "Cancel visit..." not in html
    book = text(client_as(w.field).get("/planning/book/"))
    assert "Only the ops lead can request, plan or cancel visits." in book and "Request visit" not in book


def test_cancel_needs_a_confirmation_step_and_releases_reservations(w):
    v = new_visit(w)
    plan(w, v)
    c = client_as(w.ops)
    html = text(c.get(f"/planning/visits/{v}/cancel/"))
    assert "Cancel this visit?" in html and "Keep the visit" in html
    assert Visit.objects.get(code=v).status == "planned"  # a GET never cancels
    assert "Tick the box" in text(post(c, f"/planning/visits/{v}/cancel/", {"reason": "r"}))
    assert Visit.objects.get(code=v).status == "planned"
    assert "cancelled" in text(post(c, f"/planning/visits/{v}/cancel/", {"reason": "r", "confirm": "on"}))
    assert Visit.objects.get(code=v).status == "cancelled"
    assert not Reservation.objects.filter(revision__visit__code=v, released_at__isnull=True).exists()
    assert "Reason" in text(c.get(f"/planning/visits/{new_visit(w)}/cancel/"))


def test_anonymous_post_is_redirected_to_login(w):
    r = Client().post("/planning/visits/V-0001/cancel/", {"reason": "x"})
    assert r.status_code == 302 and "/login/" in r["Location"]
    r = Client().post("/planning/sites/new/", {})
    assert r.status_code == 302 and "/login/" in r["Location"]


def test_rendered_token_works_once_end_to_end(w):
    c = client_as(w.ops)
    html = text(c.get("/planning/sites/S-3/"))
    tok = page_tokens(html, "/planning/sites/S-3/adults/")[0]
    r1 = c.post("/planning/sites/S-3/adults/", {"expected_adults": "4", "_form_token": tok}, follow=True)
    assert "Number of adults saved" in text(r1)
    r2 = c.post("/planning/sites/S-3/adults/", {"expected_adults": "5", "_form_token": tok}, follow=True)
    assert "already submitted" in text(r2)
    assert Site.objects.get(code="S-3").expected_adults == 4


# ------------------------------------------------------------------ views call the commands, nothing else writes
def test_each_view_calls_exactly_its_service_command_and_never_writes_planning_tables(w, monkeypatch):
    all_sc = scenarios(w)
    calls: list[str] = []
    state = threading.local()
    state.inside = False
    command_names = [
        "create_site",
        "set_expected_adults",
        "record_assessment",
        "approve_assessment",
        "record_consent",
        "withdraw_consent",
        "request_visit",
        "plan_visit",
        "confirm_visit",
        "cancel_visit",
    ]
    for nm in command_names:
        real = getattr(p, nm)

        def wrapper(*a: Any, _real=real, _nm=nm, **k: Any):
            calls.append(_nm)
            state.inside = True
            try:
                return _real(*a, **k)
            finally:
                state.inside = False

        monkeypatch.setattr(p, nm, wrapper)

    stray: list[str] = []
    real_save = models.Model.save

    def save(self, *a, **k):
        if self._meta.app_label == "planning" and not state.inside:
            stray.append(f"save {type(self).__name__}")
        return real_save(self, *a, **k)

    monkeypatch.setattr(models.Model, "save", save)
    for op in ("update", "delete", "bulk_create", "bulk_update", "create"):
        real_op = getattr(models.QuerySet, op)

        def guard(self, *a, _op=op, _real=real_op, **k):
            if self.model._meta.app_label == "planning" and not state.inside:
                stray.append(f"{_op} {self.model.__name__}")
            return _real(self, *a, **k)

        monkeypatch.setattr(models.QuerySet, op, guard)

    for name in NAMES:
        sc = all_sc[name]
        calls.clear()
        user = w.ops if sc.permission != "planning.consent" else w.field
        post(client_as(user), sc.url, sc.data)
        assert calls == [sc.service], (name, calls)
    # validation errors and replays call no command at all
    calls.clear()
    post(client_as(w.ops), "/planning/sites/new/", {"code": ""})
    c = client_as(w.ops)
    c.post("/planning/sites/S-3/adults/", {"expected_adults": "2"}, follow=True)  # no token
    assert calls == [] and stray == []


def test_view_module_has_no_direct_model_writes():
    import inspect

    from planning import views

    src = inspect.getsource(views)
    for needle in (
        ".save(",
        ".create(",
        ".delete(",
        ".update(",
        "bulk_create",
        "get_or_create",
        "objects.update",
    ):
        assert needle not in src, f"views.py must go through services, found {needle}"


# ------------------------------------------------------------------ privacy
def test_forms_have_no_fields_for_names_medical_or_contact_details():
    banned = (
        "name",
        "email",
        "phone",
        "address",
        "contact",
        "medical",
        "health",
        "diagnos",
        "note",
        "comment",
        "restriction",
    )
    form_classes = [
        pforms.SiteForm,
        pforms.AdultsForm,
        pforms.ConsentForm,
        pforms.WithdrawForm,
        pforms.CancelForm,
    ]
    names: list[str] = []
    for cls in form_classes:
        names += list(cls.base_fields)
    names += list(pforms.BookForm.base_fields) + list(pforms.PlanForm.base_fields)
    names += list(pforms.AssessmentForm.base_fields)
    assert not [n for n in names if any(b in n for b in banned)], names


def test_free_text_fields_reject_sentences_and_names(w):
    c = client_as(w.ops)
    bad_site = {"code": "H-8", "zone": "Z", "tz": TZ, "crm_ref": "Jane Doe", "expected_adults": ""}
    assert "no spaces, no names" in text(post(c, "/planning/sites/new/", bad_site))
    assert not Site.objects.filter(code="H-8").exists()
    consents_before = ConsentRecord.objects.filter(site__code="S-3").count()
    html = text(
        post(
            c,
            "/planning/sites/S-3/consents/new/",
            {"participant_ref": "Jane", "scope": ["presence"], "evidence_ref": "DOC-1"},
        )
    )
    assert (
        "Never a name" in html and ConsentRecord.objects.filter(site__code="S-3").count() == consents_before
    )
    html = text(
        post(
            c,
            "/planning/sites/S-3/assessments/new/",
            {
                "decision": "eligible",
                "valid_until": "2027-03-01",
                "excluded_tasks": "needs oxygen tank",
                "supersedes": "",
            },
        )
    )
    assert "Nothing was changed" in html
    html = text(
        post(
            c,
            "/planning/book/",
            {
                "site": "S-1",
                "day": DAY.isoformat(),
                "start": "09:00",
                "end": "10:00",
                "tasks": "Mrs Smith is ill",
                "scopes": ["presence"],
            },
        )
    )
    assert "Nothing was changed" in html and Visit.objects.count() == 0


def test_extra_posted_fields_are_ignored_and_stored_text_is_not_echoed(w):
    c = client_as(w.ops)
    data = {
        "code": "H-7",
        "zone": "Zone Q",
        "tz": TZ,
        "crm_ref": "CRM-H7",
        "expected_adults": "1",
        "customer_name": "Jane Doe",
        "medical": "uses oxygen at night",
        "phone": "555-0100",
    }
    post(c, "/planning/sites/new/", data)
    html = text(c.get("/planning/sites/H-7/")) + text(c.get("/planning/sites/"))
    for leak in ("Jane Doe", "oxygen", "555-0100"):
        assert leak not in html
    assert not SiteAssessment.objects.filter(restrictions__icontains="oxygen").exists()


def test_free_text_restrictions_from_other_paths_are_never_displayed(w):
    # an assessment written through the API/service with free-text restrictions must not surface in v2 screens
    p.create_site(w.ops, code="H-6", zone="Z", tz=TZ, crm_ref="CRM-H6", expected_adults=1)
    a = p.record_assessment(
        w.field,
        site_code="H-6",
        decision="eligible_restricted",
        valid_until=date(2027, 3, 1),
        restrictions="resident uses oxygen, call Jane on 555-0100",
    )
    p.approve_assessment(w.ops, assessment_id=int(a["entity_id"]))
    p.record_consent(
        w.field,
        site_code="H-6",
        participant_ref="P1",
        form_version="CF-v1",
        scope=["presence"],
        evidence_ref="D-1",
    )
    c = client_as(w.ops)
    v = p.request_visit(
        w.ops,
        site_code="H-6",
        on_site_start=p.local_to_utc(datetime(2026, 12, 8, 10), TZ),
        on_site_end=p.local_to_utc(datetime(2026, 12, 8, 11), TZ),
        travel_out_min=30,
        travel_back_min=30,
    )["entity_id"]
    pages = [
        text(c.get(u))
        for u in (
            "/planning/sites/H-6/",
            "/planning/sites/",
            f"/planning/visits/{v}/",
            f"/planning/?week={DAY.isoformat()}",
        )
    ]
    for html in pages:
        assert "oxygen" not in html and "555-0100" not in html and "Jane" not in html
    assert (
        "restrictions" in text(c.get(f"/planning/visits/{v}/")).lower()
    )  # the plain sentence still tells people


# ------------------------------------------------------------------ concurrency (real Postgres, separate connections)
def _run_threads(fns: list[Callable[[], Any]]) -> list[Any]:
    barrier, out = threading.Barrier(len(fns)), [None] * len(fns)

    def go(i: int) -> None:
        try:
            barrier.wait(20)
            out[i] = fns[i]()
        except Exception as e:  # noqa: BLE001
            out[i] = e
        finally:
            connection.close()

    ts = [threading.Thread(target=go, args=(i,)) for i in range(len(fns))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return out


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_confirms_exactly_one_wins():
    w = build_planning(prefix="cf_")
    v = new_visit(w)
    plan(w, v)
    ops2 = make_user("cf_ops2", "ops_lead")
    clients = [client_as(w.ops), client_as(ops2)]
    for i, c in enumerate(clients):
        seed_token(c, f"cc{i}")

    def confirm(i: int) -> Callable[[], Any]:
        return lambda: clients[i].post(f"/planning/visits/{v}/confirm/", {"_form_token": f"cc{i}"})

    res = _run_threads([confirm(0), confirm(1)])
    assert all(getattr(r, "status_code", None) == 302 for r in res), res
    pages = [text(c.get(f"/planning/visits/{v}/")) for c in clients]
    wins = sum("Visit confirmed" in h for h in pages)
    refused = sum("Only a planned visit can be confirmed" in h for h in pages)
    assert (wins, refused) == (1, 1), (wins, refused)
    assert Visit.objects.get(code=v).status == "confirmed"


@pytest.mark.django_db(transaction=True)
def test_two_bookings_for_the_same_robot_slot_exactly_one_wins():
    w = build_planning(prefix="cb_")
    v1, v2 = new_visit(w), new_visit(w)
    ops2 = make_user("cb_ops2", "ops_lead")
    clients = [client_as(w.ops), client_as(ops2)]
    for i, c in enumerate(clients):
        seed_token(c, f"bb{i}")
    crews = [("P-1", "P-2", "VAN-1"), ("P-3", "P-2", "VAN-2")]

    def book(i: int, v: str) -> Callable[[], Any]:
        lead, second, van = crews[i]
        return lambda: clients[i].post(
            f"/planning/visits/{v}/plan/",
            {"robot": "R-01", "lead": lead, "second": second, "vehicle": van, "_form_token": f"bb{i}"},
        )

    res = _run_threads([book(0, v1), book(1, v2)])
    assert all(getattr(r, "status_code", None) == 302 for r in res), res
    holders = [
        code
        for code in (v1, v2)
        if Reservation.objects.filter(
            revision__visit__code=code, resource__kind="robot", released_at__isnull=True
        ).exists()
    ]
    assert len(holders) == 1, holders
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM planning_reservation a JOIN planning_reservation b ON a.resource_id=b.resource_id "
            "AND a.id<b.id WHERE a.released_at IS NULL AND b.released_at IS NULL "
            "AND tstzrange(a.starts_at,a.ends_at,'[)') && tstzrange(b.starts_at,b.ends_at,'[)')"
        )
        assert cur.fetchone()[0] == 0
    loser = v2 if holders[0] == v1 else v1
    assert Visit.objects.get(code=loser).status == "requested"
    loser_client = clients[1] if loser == v2 else clients[0]
    assert "already booked for visit" in text(loser_client.get(f"/planning/visits/{loser}/"))


def test_settings_time_zone_is_the_workspace_zone():
    assert settings.TIME_ZONE == "America/Los_Angeles"
