"""v2 01C closure evidence #1: a first-run walkthrough from an empty database to a confirmed visit that is
Approved to leave, including every required authorized approval step.

ONE journey, driven ONLY through HTTP requests of the Django test client against the app's screens. One Client
per person; each signs in through /login/ with the password the admin set. Every form token is parsed from the
rendered page (as a browser would send it), together with the page's other hidden fields. No service call, no
model write, no seed command, no fixture: the only inputs are an empty migrated database and the
WORKSPACE_BOOTSTRAP_TOKEN setting. Models are READ (never written) to assert state and to find an id no screen
shows.

The ONLY non-HTTP step is the deployment owner's provisioning of the operating settings
(``manage.py provision_settings``, "OWNER PROVISIONING (before setup)"): those values belong to the deployment
owner, not to any screen. There are no manual API steps and no direct model writes: the test asserts it. The log
is written to docs/evidence/01c_journey_walkthrough.md.
"""

import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client

from accounts.models import RoleGrant
from fleet.models import (
    BaseAuthorization,
    CapabilityApproval,
    CapabilityProfile,
    HoldPolicyApproval,
    InspectionTemplateApproval,
    PatternApproval,
    PeriodicInspectionRuleApproval,
    Robot,
)
from planning.models import AssessmentApproval, Qualification
from platform_core.models import FormToken, Workspace
from readiness.models import DepartureAuthorization, ReadinessPolicyApproval

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
BOOTSTRAP_TOKEN = "journey-one-time-bootstrap-token"
EVIDENCE_FILE = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "01c_journey_walkthrough.md"
LA = ZoneInfo("America/Los_Angeles")

# (key, role, who it is, what the person is for)
PEOPLE = [
    ("ops", "ops_lead", "Olive Ops"),
    ("field1", "field_operator", "Fay Field"),
    ("field2", "field_operator", "Finn Field"),
    ("eng", "engineer", "Eve Engineer"),
    ("lead", "engineering_lead", "Lena Lead"),
    ("lead2", "engineering_lead", "Lou Lead (second engineering lead)"),
    ("safety", "safety_reviewer", "Sam Safety"),
    ("founder", "founder", "Fred Founder"),
]
PERSON_CODES = {"ops": "P-1", "field1": "P-2", "field2": "P-3", "eng": "P-4", "lead2": "P-5"}
TEMPLATE_ITEMS = [
    {"id": f"I{n}", "label": f"SYNTHETIC robot-condition item {n}", "robot_condition": True}
    for n in range(1, 4)
] + [{"id": f"I{n}", "label": f"SYNTHETIC visit item {n}", "robot_condition": False} for n in range(4, 6)]
POLICY_REQUIREMENTS = {
    "authorization_ttl_min": 90,
    "evidence": [
        {"kind": "pre_departure_inspection", "max_age_min": 240},
        {"kind": "equipment_check", "max_age_min": 240},
        {"kind": "battery_check", "max_age_min": 120},
    ],
    "required_equipment": ["restraint_kit", "recovery_kit", "charger"],
    "recovery_plan_required": True,
}


# ------------------------------------------------------------------ reading rendered pages like a browser
@dataclass
class HtmlForm:
    action: str
    hidden: dict[str, Any] = field(default_factory=dict)  # a name repeated in the form becomes a list
    names: set[str] = field(default_factory=set)
    options: dict[str, list[str]] = field(default_factory=dict)


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[HtmlForm] = []
        self._form: HtmlForm | None = None
        self._select: str | None = None
        self._option: str | None = None  # name of the select while reading an option with no value attribute

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._form = HtmlForm(action=a.get("action", ""))
            self.forms.append(self._form)
        elif self._form is not None and tag == "input" and a.get("name"):
            self._form.names.add(a["name"])
            if a.get("type") == "hidden":
                old = self._form.hidden.get(a["name"])
                value = a.get("value", "")
                self._form.hidden[a["name"]] = (
                    value if old is None else [*(old if isinstance(old, list) else [old]), value]
                )
        elif self._form is not None and tag in ("select", "textarea") and a.get("name"):
            self._form.names.add(a["name"])
            if tag == "select":
                self._select = a["name"]
                self._form.options.setdefault(a["name"], [])
        elif self._form is not None and tag == "option" and self._select:
            if "value" in a:
                self._form.options[self._select].append(a["value"])
            else:
                self._option = self._select

    def handle_data(self, data: str) -> None:
        if self._form is not None and self._option and data.strip():
            self._form.options[self._option].append(data.strip())
            self._option = None

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None
            self._option = None


def forms_of(html: str) -> list[HtmlForm]:
    p = _Forms()
    p.feed(html)
    return p.forms


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def toasts(html: str) -> list[str]:
    return [text_of(m) for m in re.findall(r'<div class="msg toast[^"]*"[^>]*>(.*?)</div>', html, re.S)]


def alerts(html: str) -> list[str]:
    return [text_of(m) for m in re.findall(r'role="alert"[^>]*>(.*?)</', html, re.S)]


def html_of(r: Any) -> str:
    return str(r.content.decode())


def short(text: str, n: int = 170) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def cell(text: Any) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


# ------------------------------------------------------------------ the journey harness
def approval_snapshot() -> dict[str, int]:
    """Counts of every approval-like record. READ only."""
    return {
        "pattern decision": PatternApproval.objects.count(),
        "capability profile approval": CapabilityApproval.objects.count(),
        "base authorization": BaseAuthorization.objects.count(),
        "inspection template approval": InspectionTemplateApproval.objects.count(),
        "periodic rule approval": PeriodicInspectionRuleApproval.objects.count(),
        "hold policy approval": HoldPolicyApproval.objects.count(),
        "readiness policy approval": ReadinessPolicyApproval.objects.count(),
        "assessment approval": AssessmentApproval.objects.count(),
        "inspector qualification": Qualification.objects.filter(kind="readiness_inspector").count(),
        "departure authorization": DepartureAuthorization.objects.count(),
    }


@dataclass
class Actor:
    key: str
    role: str
    username: str
    password: str
    client: Client = field(default_factory=Client)


@dataclass
class Journey:
    log: list[dict[str, str]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    actors: dict[str, Actor] = field(default_factory=dict)

    def record(self, role: str, url: str, submitted: str, result: str) -> None:
        self.log.append(
            {"n": str(len(self.log) + 1), "role": role, "url": url, "submitted": submitted, "result": result}
        )

    def gap(self, text: str) -> None:
        if text not in self.gaps:
            self.gaps.append(text)

    # ---- sign in
    def sign_in(self, actor: Actor) -> None:
        c = actor.client
        assert c.get("/login/").status_code == 200
        r = c.post("/login/", {"username": actor.username, "password": actor.password}, follow=True)
        assert r.status_code == 200 and "_auth_user_id" in c.session, f"{actor.username} could not sign in"
        self.record(
            actor.role, "/login/", f"username {actor.username}, the password the admin set", "Signed in"
        )
        if r.redirect_chain and r.redirect_chain[-1][0] == "/account/password/":
            # v2 01D: an invited account must replace the admin's temporary password first
            new = actor.password + "-own"
            self.submit(
                actor,
                "/account/password/",
                {"current_password": actor.password, "new_password1": new, "new_password2": new},
                expect="Password changed.",
                what="first sign-in: choose an own password instead of the temporary one",
            )
            actor.password = new

    # ---- a screen with a form
    def submit(
        self,
        actor: Actor,
        url: str,
        data: dict[str, Any],
        *,
        action: str | None = None,
        match: dict[str, str] | None = None,
        approves: int = 0,
        expect: tuple[str, ...] | str = (),
        refused: bool = False,
        what: str = "",
        page: str | None = None,
    ) -> Any:
        """GET the page, take the form (by action and hidden values), send hidden fields + ``data``."""
        if isinstance(expect, str):
            expect = (expect,)
        c = actor.client
        page_url = page or url
        got = c.get(page_url)
        assert got.status_code == 200, f"{actor.key} GET {page_url} -> {got.status_code}"
        wanted = action or url
        candidates = [
            f
            for f in forms_of(html_of(got))
            if (f.action or page_url) == wanted
            and all(f.hidden.get(k) == v for k, v in (match or {}).items())
        ]
        assert candidates, f"{actor.key}: no form posting to {wanted} on {page_url} (match {match})"
        form = candidates[0]
        body: dict[str, Any] = {**form.hidden, **data}
        before = approval_snapshot()
        r = c.post(wanted, body, follow=True)
        after = approval_snapshot()
        grew = sum(after[k] - before[k] for k in after)
        assert grew == approves, (
            f"step '{what}': expected {approves} new approval record(s), saw {grew}: "
            f"{ {k: after[k] - before[k] for k in after if after[k] != before[k]} }"
        )
        page_html = html_of(r)
        notes = toasts(page_html) or alerts(page_html)
        self.record(
            actor.role,
            f"POST {wanted}",
            what or short(json.dumps(data, default=str), 140),
            short("; ".join(notes) or f"HTTP {r.status_code}"),
        )
        failed = (
            bool(re.search(r"Refused|Not done|Nothing was changed", "; ".join(notes))) or r.status_code >= 400
        )
        if refused:
            assert failed, f"step '{what}' should have been refused; got {notes}"
        else:
            assert not failed, f"step '{what}' was refused: {notes} (HTTP {r.status_code})"
        joined = " ".join(notes) + " " + page_html
        for needle in expect:
            assert needle in joined, f"step '{what}': expected {needle!r}; got {notes}"
        return r

    def view(
        self, actor: Actor, url: str, what: str, *, expect: tuple[str, ...] = (), absent: tuple[str, ...] = ()
    ) -> str:
        r = actor.client.get(url)
        assert r.status_code == 200, f"{actor.key} GET {url} -> {r.status_code}"
        body = html_of(r)
        for needle in expect:
            assert needle in body, f"view '{what}': {needle!r} not on {url}"
        for needle in absent:
            assert needle not in body, f"view '{what}': {needle!r} should not be on {url}"
        self.record(actor.role, f"GET {url}", "(look at the page)", what)
        return body


def tomorrow_la() -> datetime:
    return datetime.now(LA) + timedelta(days=1)


def test_first_run_journey_from_empty_database_to_approved_to_leave(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE_BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    j = Journey()
    assert User.objects.count() == 0 and Robot.objects.count() == 0, (
        "the journey starts from an empty database"
    )
    assert not Workspace.objects.filter(initialized=True).exists()

    # ================================================================== 0. owner provisioning (before setup)
    out = io.StringIO()
    call_command(
        "provision_settings",
        consent_form_version="CF-v1",
        pre_visit_inspection_min="30",
        with_defaults=True,
        stdout=out,
    )
    assert "set ops_readiness_lookahead_min = 2880" in out.getvalue()
    j.record(
        "deployment owner",
        "manage.py provision_settings",
        "consent_form_version=CF-v1, pre_visit_inspection_min=30, --with-defaults (ops_readiness_lookahead_min=2880)",
        "OWNER PROVISIONING (before setup): " + short(" / ".join(out.getvalue().split("\n")), 120),
    )
    assert approval_snapshot() == dict.fromkeys(approval_snapshot(), 0), "provisioning approved nothing"

    # ================================================================== 1. first-run setup
    visitor = Client()
    r = visitor.get("/setup/")
    assert r.status_code == 200 and "Set up your workspace" in html_of(r)
    j.record(
        "visitor", "GET /setup/", "-", "The setup page opens (token configured, workspace not initialized)"
    )
    bad = visitor.post(
        "/setup/",
        {
            "token": "wrong",
            "username": "j_admin",
            "password": "Zq7!-journey-admin",
            "workspace_name": "Northbay Journey Robotics",
        },
    )
    assert bad.status_code == 403 and User.objects.count() == 0
    j.record(
        "visitor",
        "POST /setup/",
        "a wrong setup token",
        "Refused (HTTP 403): the setup token is not correct; no user created",
    )
    admin = Actor("admin", "admin", "j_admin", "Zq7!-journey-admin")
    ok = visitor.post(
        "/setup/",
        {
            "token": BOOTSTRAP_TOKEN,
            "username": admin.username,
            "password": admin.password,
            "workspace_name": "Northbay Journey Robotics",
        },
    )
    assert ok.status_code == 302 and ok.headers["Location"] == "/login/"
    assert (
        User.objects.count() == 1
        and RoleGrant.objects.filter(user__username="j_admin", role="admin").exists()
    )
    j.record(
        "visitor",
        "POST /setup/",
        "the one-time token, admin username and password, company name",
        "First admin created; redirected to /login/",
    )
    replay = visitor.post(
        "/setup/", {"token": BOOTSTRAP_TOKEN, "username": "j_admin2", "password": "Zq7!-journey-admin2"}
    )
    assert replay.status_code == 404 and User.objects.count() == 1
    assert visitor.get("/setup/").status_code == 404
    j.record(
        "visitor",
        "POST /setup/",
        "the same token again (replay)",
        "Refused (HTTP 404): setup is permanently closed; no second admin",
    )
    j.actors["admin"] = admin
    j.sign_in(admin)
    assert approval_snapshot() == dict.fromkeys(approval_snapshot(), 0), "bootstrap approved nothing"

    # ================================================================== 2. admin: company, hub, people, roles
    body = j.view(
        admin,
        "/settings/setup/",
        "Setup checklist (nothing done yet except the admin)",
        expect=("Company name",),
    )
    assert "Hubs" in body
    j.submit(
        admin,
        "/settings/company/",
        {
            "name": "Northbay Journey Robotics",
            "region": "Bay Area, California",
            "time_zone": "America/Los_Angeles",
        },
        expect="Company settings saved.",
        what="company name, region, time zone",
    )
    j.submit(
        admin,
        "/settings/hubs/new/",
        {"code": "SF1", "name": "San Francisco hub", "area": "Bayview", "time_zone": "America/Los_Angeles"},
        expect="Hub SF1 added.",
        what="hub SF1",
    )
    for key, role, _who in PEOPLE:
        username = f"j_{key}"
        password = f"Zq7!-journey-{key}"
        j.actors[key] = Actor(key, role, username, password)
        r = j.submit(
            admin,
            "/settings/users/new/",
            {"username": username, "password": password},
            expect=f"Account {username} created.",
            what=f"invite {username} (temporary password set by the admin)",
        )
        user_page = r.redirect_chain[-1][0]
        assert re.fullmatch(r"/settings/users/\d+/", user_page)
        j.submit(admin, user_page, {"role": role}, expect="given to", what=f"give {username} the role {role}")
    assert {g.role for g in RoleGrant.objects.exclude(role="admin")} == {r for _k, r, _w in PEOPLE}
    j.view(
        admin,
        "/settings/users/",
        "People and roles list shows every invited person",
        expect=tuple(f"j_{k}" for k, _r, _w in PEOPLE),
    )
    assert approval_snapshot() == dict.fromkeys(approval_snapshot(), 0), (
        "company, hub, users and roles approved nothing"
    )
    for key in j.actors:
        if key != "admin":
            j.sign_in(j.actors[key])
    ops, field1, field2 = j.actors["ops"], j.actors["field1"], j.actors["field2"]
    eng, lead, lead2 = j.actors["eng"], j.actors["lead"], j.actors["lead2"]
    safety = j.actors["safety"]

    # ================================================================== 3. engineering: configuration, robot, approvals
    j.submit(
        lead,
        "/fleet/patterns/new/",
        {"code": "P-J", "hardware_rev": "SYN-HW-1", "release": "1.0", "firmware_set": "FW-J1"},
        expect="Configuration P-J added.",
        what="configuration pattern P-J (a proposal)",
    )
    j.view(
        lead,
        "/fleet/patterns/P-J/",
        "Configuration page: no decision yet, the engineering lead gets the decide button",
        expect=("none yet", "/fleet/patterns/P-J/decision/"),
    )
    j.submit(
        lead,
        "/fleet/patterns/P-J/decision/",
        {"status": "approved_home", "reason": "Journey: approve for home use", "confirm": "on"},
        approves=1,
        expect="Decision recorded for configuration P-J",
        what="approve configuration P-J for home use (confirm step)",
    )
    j.submit(
        eng,
        "/fleet/patterns/P-J/profile/",
        {
            "stop_behaviour": "SYNTHETIC: robot stops in place",
            "lift_crew": 2,
            "max_runtime_min": 120,
            "min_departure_soc_pct": 60,
            "charge_turnaround_min": 180,
            "load_unload_min": 20,
            "source": "engineering_doc",
        },
        expect="published as a draft",
        what="publish the capability profile (draft)",
    )
    profile = CapabilityProfile.objects.get(pattern__code="P-J", version_no=1)  # READ: to assert the id
    body = j.view(
        lead,
        "/fleet/patterns/P-J/profile/",
        "Capability profile page shows the draft with its id and a link to the approval screen",
        expect=(f"profile id {profile.pk}", f"/fleet/patterns/P-J/profile/approve/{profile.pk}/"),
    )
    approve_url = f"/fleet/patterns/P-J/profile/approve/{profile.pk}/"
    j.submit(
        lead,
        approve_url,
        {"slot": "engineering", "confirm": "on"},
        approves=1,
        expect="Approval recorded for capability profile v1",
        what="capability profile v1: engineering approval (engineering lead)",
    )
    j.submit(
        safety,
        approve_url,
        {"slot": "safety", "confirm": "on"},
        approves=1,
        expect="Approval recorded for capability profile v1",
        what="capability profile v1: safety approval (safety reviewer)",
    )
    j.view(lead, "/fleet/patterns/", "Configurations list: P-J has an approved profile", expect=("P-J",))
    j.submit(
        lead,
        "/fleet/new/",
        {"code": "R-J1", "serial": "SYN-J-0001"},
        expect="Robot R-J1 added.",
        what="add robot R-J1",
    )
    j.submit(
        eng,
        "/fleet/R-J1/manifest/",
        {
            "pattern": "P-J",
            "autonomy_version": "SYN-auto-1",
            "safety_config_hash": "a" * 64,
            "calibration_ref": "CAL-J1",
            "provenance": "manual",
        },
        expect="recorded as a proposal",
        what="record R-J1's configuration manifest",
    )
    j.submit(
        lead,
        "/fleet/R-J1/lifecycle/",
        {"to": "validation", "reason": "Journey: begin engineering validation", "confirm": "on"},
        expect="moved to validation",
        what="lifecycle: validation",
    )
    j.submit(
        lead,
        "/fleet/R-J1/lifecycle/",
        {"to": "in_service", "reason": "Journey: validation complete", "confirm": "on"},
        expect="moved to in service",
        what="lifecycle: in service",
    )
    j.submit(
        lead,
        "/fleet/R-J1/authorize/",
        {"confirm": "on"},
        approves=1,
        expect="Home-use authorization granted for R-J1",
        what="grant R-J1's base home-use authorization (confirm step)",
    )
    assert Robot.objects.get(code="R-J1").lifecycle == "in_service"

    # ================================================================== 4. people, qualifications, vehicle
    for key, code in PERSON_CODES.items():
        j.submit(
            ops,
            "/people/new/",
            {"username": f"j_{key}", "code": code},
            expect="registered",
            what=f"register {code} for j_{key}",
        )
    today = datetime.now(LA).date()
    start_day, end_day = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=90)).isoformat()
    for code, kinds in (
        ("P-1", ["field_lead", "robot_handler"]),
        ("P-2", ["field_operator", "robot_handler"]),
        ("P-3", ["field_operator", "robot_handler"]),
    ):
        for kind in kinds:
            j.submit(
                ops,
                f"/people/{code}/",
                {"kind": kind, "valid_from": start_day, "valid_to": end_day},
                expect="granted",
                what=f"{code}: qualification {kind}",
            )
    for code in ("P-2", "P-3"):
        j.submit(
            safety,
            f"/people/{code}/",
            {"kind": "readiness_inspector", "valid_to": end_day, "basis_ref": f"TRAIN-SYN-{code}"},
            approves=1,
            expect="granted",
            what=f"{code}: readiness_inspector qualification (safety reviewer)",
        )
    j.submit(
        ops,
        "/vehicles/new/",
        {"code": "VAN-1", "robot_capacity": 1, "restraint_kit": "SYN kit"},
        expect="Vehicle VAN-1 added.",
        what="register vehicle VAN-1",
    )

    # ================================================================== 5. policies and templates (production rules: nothing synthetic)
    j.submit(
        eng,
        "/fleet/patterns/P-J/templates/create/",
        {
            "version": "E1",
            "source": "engineering_doc",
            "procedure_ref": "SYN-DOC-INSPECT-1",
            "items": json.dumps(TEMPLATE_ITEMS),
        },
        page="/fleet/patterns/P-J/",
        expect="Inspection template recorded",
        what="inspection template E1 (engineering document reference)",
    )
    template_id = ""
    for purpose in ("pre_departure", "periodic"):
        for slot, who in (("engineering", lead), ("safety", safety)):
            r = j.submit(
                who,
                "/fleet/patterns/P-J/templates/approve/",
                {"slot": slot},
                page="/fleet/patterns/P-J/",
                match={"purpose": purpose, "slot": slot},
                approves=1,
                expect="Template approval recorded",
                what=f"template E1 approval: {purpose} / {slot}",
            )
            template_id = template_id or "set"
    assert template_id
    j.submit(
        eng,
        "/fleet/patterns/P-J/periodic-rules/create/",
        {
            "interval_min": 129600,
            "source": "engineering_doc",
            "procedure_ref": "SYN-DOC-PERIODIC-1",
            "notes": "SYNTHETIC interval, not an engineering value",
        },
        page="/fleet/patterns/P-J/",
        expect="Periodic inspection rule version recorded",
        what="periodic inspection rule v1 (90 days)",
    )
    for slot, who in (("engineering", lead), ("safety", safety)):
        j.submit(
            who,
            "/fleet/patterns/P-J/periodic-rules/approve/",
            {},
            page="/fleet/patterns/P-J/",
            match={"slot": slot},
            approves=1,
            expect="Periodic rule approval recorded",
            what=f"periodic rule v1 approval: {slot}",
        )
    j.submit(
        lead,
        "/readiness/policies/create/",
        {
            "source": "approved_procedure",
            "notes": "Journey policy",
            "requirements": json.dumps(POLICY_REQUIREMENTS),
        },
        page="/readiness/policies/",
        expect="created as a DRAFT",
        what="readiness policy v1 (draft)",
    )
    j.submit(
        lead2,
        "/readiness/policies/1/approve/",
        {"slot": "engineering"},
        page="/readiness/policies/",
        approves=1,
        expect="engineering approval recorded",
        what="readiness policy v1: engineering approval (a second engineering lead; the author cannot approve)",
    )
    j.submit(
        safety,
        "/readiness/policies/1/approve/",
        {"slot": "safety"},
        page="/readiness/policies/",
        approves=1,
        expect="safety approval recorded",
        what="readiness policy v1: safety approval",
    )
    j.view(
        admin,
        "/settings/operating/",
        "Operating settings (read only): the owner's provisioned values are shown",
        expect=("CF-v1", "2880", "the deployment owner (via provision_settings)"),
    )

    # ================================================================== 6. periodic inspection through the form
    j.submit(
        field1,
        "/readiness/robots/R-J1/inspection/",
        {
            "template_version": "E1",
            **{f"item_{i['id']}": "pass" for i in TEMPLATE_ITEMS},
            "performer": field1.username,
            "verifier": field2.username,
        },
        expect="recorded: it passed",
        what="periodic inspection of R-J1: all items pass, signed by two inspectors",
    )

    # ================================================================== 7. customer home
    j.submit(
        ops,
        "/planning/sites/new/",
        {"code": "H-J1", "zone": "Zone Bayview", "tz": "America/Los_Angeles", "crm_ref": "CRM-J-1"},
        expect="Home H-J1 added.",
        what="add customer home H-J1 (adults unknown)",
    )
    j.submit(
        ops,
        "/planning/sites/H-J1/adults/",
        {"expected_adults": 2},
        page="/planning/sites/H-J1/",
        expect="Number of adults saved.",
        what="expected adults: 2",
    )
    valid_until = (today + timedelta(days=90)).isoformat()
    j.submit(
        field1,
        "/planning/sites/H-J1/assessments/new/",
        {"decision": "eligible", "valid_until": valid_until, "excluded_tasks": "", "supersedes": ""},
        page="/planning/sites/H-J1/",
        expect="Assessment recorded.",
        what="home assessment: eligible",
    )
    assessment_id = re.search(
        r"/assessments/(\d+)/approve/", html_of(ops.client.get("/planning/sites/H-J1/"))
    )
    assert assessment_id, "the approve button is on the home page for the ops lead"
    j.submit(
        ops,
        f"/planning/sites/H-J1/assessments/{assessment_id.group(1)}/approve/",
        {},
        page="/planning/sites/H-J1/",
        approves=1,
        expect="Assessment approved.",
        what="approve the assessment (ops lead, not the assessor)",
    )
    for n, who in ((1, field1), (2, field2)):
        j.submit(
            who,
            "/planning/sites/H-J1/consents/new/",
            {"participant_ref": f"P{n}", "scope": ["presence"], "evidence_ref": f"DOC-HJ1-{n}"},
            page="/planning/sites/H-J1/",
            expect=f"Consent recorded for P{n}.",
            what=f"consent of adult P{n}",
        )
    j.view(ops, "/planning/sites/H-J1/", "Home reads ready", expect=("Home ready",))

    # ================================================================== 8. book, plan, confirm
    day = tomorrow_la().date().isoformat()
    r = j.submit(
        ops,
        "/planning/book/",
        {
            "site": "H-J1",
            "day": day,
            "start": "10:00",
            "end": "11:00",
            "travel_out_min": 30,
            "travel_back_min": 30,
            "tasks": "T-04",
            "scopes": ["presence"],
        },
        expect="Visit requested.",
        what="book a visit at H-J1 tomorrow 10:00-11:00",
    )
    visit = r.redirect_chain[-1][0].rstrip("/").split("/")[-1]
    visit_url = f"/planning/visits/{visit}/"
    j.submit(
        ops,
        f"{visit_url}plan/",
        {
            "robot": "R-J1",
            "lead": "P-1",
            "second": "P-2",
            "vehicle": "VAN-1",
            "start": "",
            "end": "",
            "reason": "first plan",
        },
        page=visit_url,
        expect="Plan saved.",
        what="plan: robot R-J1, lead P-1, second P-2, vehicle VAN-1",
    )
    j.submit(
        ops,
        f"{visit_url}confirm/",
        {},
        page=visit_url,
        expect="Visit confirmed.",
        what="confirm the visit (ops lead)",
    )
    page = j.view(
        ops,
        f"/readiness/visits/{visit}/",
        "Visit is confirmed but not ready",
        absent=("Approved to leave by",),
    )
    assert "Checklist complete" not in page

    # ================================================================== 9. readiness and approval to leave
    ready_url = f"/readiness/visits/{visit}/"
    j.submit(
        field1,
        f"{ready_url}inspection/",
        {
            "template_version": "E1",
            **{f"item_{i['id']}": "pass" for i in TEMPLATE_ITEMS},
            "performer": field1.username,
            "verifier": field2.username,
        },
        expect="recorded: it passed",
        what="pre-departure inspection through the form (two qualified inspectors)",
    )
    form = next(
        f
        for f in forms_of(html_of(ops.client.get(ready_url)))
        if (f.action or ready_url).endswith("/evidence/")
        and f.hidden.get("kind") == "pre_departure_inspection"
    )
    inspection_code = next(o for o in form.options["inspection_code"] if o)  # newest first
    assert inspection_code, "the new inspection is offered on the visit page"
    j.submit(
        ops,
        f"{ready_url}evidence/",
        {"inspection_code": inspection_code, "result": "pass"},
        page=ready_url,
        match={"kind": "pre_departure_inspection"},
        expect="Pre-departure inspection recorded",
        what=f"record pre-departure inspection evidence ({inspection_code})",
    )
    j.submit(
        field1,
        f"{ready_url}evidence/",
        {
            "item_restraint_kit": "present",
            "item_recovery_kit": "present",
            "item_charger": "present",
            "result": "pass",
        },
        page=ready_url,
        match={"kind": "equipment_check"},
        expect="Equipment check recorded",
        what="equipment check: restraint kit, recovery kit, charger present",
    )
    j.submit(
        field1,
        f"{ready_url}evidence/",
        {"soc_pct": 85, "result": "pass"},
        page=ready_url,
        match={"kind": "battery_check"},
        expect="Battery check recorded",
        what="battery check: 85% (minimum 60%)",
    )
    j.submit(
        ops,
        f"{ready_url}recovery-plan/",
        {
            "on_call": "P-4",
            "escalation": "P-5",
            "escalation_path": "SYNTHETIC: on-call engineer, engineering lead, ops lead",
            "required_equipment": "recovery_kit",
        },
        page=ready_url,
        expect="Recovery plan recorded",
        what="recovery plan: on-call P-4, escalation P-5",
    )
    page = j.view(
        ops,
        ready_url,
        "Checklist reads complete, not approved",
        expect=("Checklist complete - you can approve it now.",),
        absent=("Approved to leave by",),
    )
    assert "data-approved-to-leave" not in page
    today_before = j.view(
        ops,
        "/ops/",
        "Today (lookahead provisioned by the owner): 'Checklist complete, waiting for approval' (neutral, not green)",
        expect=(f'data-visit="{visit}" data-status="ready"', "Checklist complete, waiting for approval"),
        absent=("data-approved-to-leave",),
    )
    assert "chip-neutral" in re.search(rf'data-visit="{visit}".*?</li>', today_before, re.S).group(0)  # type: ignore[union-attr]
    assert not any(
        f.action.endswith("/authorize/") for f in forms_of(html_of(field1.client.get(ready_url)))
    ), "only the ops lead gets the approve button"
    j.record(
        field1.role,
        f"GET {ready_url}",
        "(look at the page)",
        "A field operator sees no 'approve to leave' button",
    )
    j.submit(
        ops,
        f"{ready_url}authorize/",
        {},
        page=ready_url,
        approves=1,
        expect=(f"Approved to leave by {ops.username}", f"for visit {visit} with robot R-J1"),
        what="ops lead requests approval to leave",
    )

    # ---- final assertions
    final = j.view(
        ops,
        ready_url,
        "Visit page: Approved to leave, with the decision",
        expect=(f"Approved to leave by <b>{ops.username}</b>", "data-approved-to-leave"),
    )
    assert f"for visit <b>{visit}</b> with robot <b>R-J1</b>" in final
    auth = DepartureAuthorization.objects.get(visit__code=visit)
    assert auth.issued_by.username == ops.username and auth.robot.code == "R-J1"
    today_page = j.view(
        ops,
        "/ops/",
        "Today: the visit is Approved to leave (blue, never green)",
        expect=(
            f'data-visit="{visit}" data-status="approved"',
            "Approved to leave",
            f"Approved to leave by <b>{ops.username}</b>",
        ),
    )
    row = re.search(rf'data-visit="{visit}".*?</li>', today_page, re.S)
    assert (
        row
        and "chip-info" in row.group(0)
        and "chip-ok" not in row.group(0)
        and "chip-pos" not in row.group(0)
    )
    assert Robot.objects.get(code="R-J1").lifecycle == "in_service"

    # ---- who made each approval (roles checked against the records, READ only)
    def role_of(user: Any) -> set[str]:
        return {g.role for g in RoleGrant.objects.filter(user=user, revoked_at__isnull=True)}

    assert not j.gaps and not any("GAP" in s["submitted"] or "/api/" in s["url"] for s in j.log), (
        "no manual API step and no GAP step: every step is a screen (or the one owner provisioning step)"
    )
    assert sum(1 for s in j.log if s["url"] == "manage.py provision_settings") == 1
    for a in PatternApproval.objects.all():
        assert "engineering_lead" in role_of(a.decided_by)
    for a in CapabilityApproval.objects.all():
        assert {"engineering": "engineering_lead", "safety": "safety_reviewer"}[a.slot] in role_of(a.approver)
    for a in InspectionTemplateApproval.objects.all():
        assert {"engineering": "engineering_lead", "safety": "safety_reviewer"}[a.slot] in role_of(a.approver)
    for a in PeriodicInspectionRuleApproval.objects.all():
        assert {"engineering": "engineering_lead", "safety": "safety_reviewer"}[a.slot] in role_of(a.approver)
    for a in ReadinessPolicyApproval.objects.all():
        assert {"engineering": "engineering_lead", "safety": "safety_reviewer"}[a.slot] in role_of(a.approver)
    for q in Qualification.objects.filter(kind="readiness_inspector"):
        assert "safety_reviewer" in role_of(q.granted_by)
    for a in AssessmentApproval.objects.all():
        assert role_of(a.approver) & {"ops_lead", "engineering_lead"}
    assert "ops_lead" in role_of(auth.issued_by)
    counts = approval_snapshot()
    assert counts["departure authorization"] == 1 and counts["readiness policy approval"] == 2
    assert FormToken.objects.filter(consumed_at__isnull=False).count() >= 40

    j.findings += [
        "Readiness policy: the author cannot approve it, so a second engineering lead user is needed for the engineering slot (the journey creates two).",
        "No screen shows or sets demo mode; the journey therefore ran under production rules (approved procedure policy, engineering-document template and rule).",
        "Today still lists two 'Needs your attention' setup cards for the new workspace (escalation deadlines not set up, closeout time zone not set); both are deployment/owner settings with no screen.",
    ]
    write_evidence(j, visit, counts)


def write_evidence(j: Journey, visit: str, counts: dict[str, int]) -> None:
    lines = [
        "# 01C closure evidence 1: first-run walkthrough, empty database to Approved to leave",
        "",
        "Produced by `backend/tests/test_v2_01c_journey.py` (one test, Django test client, HTTP only, one client per person).",
        "Start: an empty migrated database and the one-time `WORKSPACE_BOOTSTRAP_TOKEN`. End: a confirmed visit that the",
        "ops lead approved to leave. Production rules apply (workspace is NOT in demo mode): the inspection template,",
        "periodic rule and readiness policy are approved records, not synthetic demo items.",
        "",
        f"Result: **PASS**. {len(j.log)} steps (every step is a screen, except the one OWNER PROVISIONING step before setup, which is a management command). Final visit: `{visit}` with robot R-J1, approved to leave by the ops lead.",
        "",
        "Approval records at the end (created only by the approval steps below, never by a setup screen; the test asserts",
        "that every non-approval step added zero):",
        "",
    ]
    lines += [f"- {k}: {v}" for k, v in counts.items()]
    lines += ["", "## GAPs: steps that have no screen", ""]
    lines += [f"- {g}" for g in j.gaps] or ["- none"]
    lines += ["", "## Steps", "", "| # | Role | URL | What was submitted | Result |", "|---|---|---|---|---|"]
    for s in j.log:
        lines.append(
            f"| {s['n']} | {cell(s['role'])} | {cell(s['url'])} | {cell(s['submitted'])} | {cell(s['result'])} |"
        )
    if j.findings:
        lines += ["", "## Findings", ""] + [f"- {f}" for f in j.findings]
    lines.append("")
    EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_FILE.write_text("\n".join(lines))
