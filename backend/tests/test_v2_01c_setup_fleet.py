"""v2 01C (Engineer A): workspace setup, fleet and resource screens, shared interaction components.

Per form: success, refusal in plain words, server-side denial for every role without the permission, replayed
POST refused (double submit), and a spy that the view calls the intended EXISTING service command.
Approved exceptions to "views call existing guarded services": the workspace settings and Hub (new setup data,
platform_core.workspace) and the user-invite command (accounts.create_user). All three still run through
run_command (permission, audit, one transaction).
"""

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone

from accounts import services as account_services
from accounts.models import RoleGrant
from accounts.roles import ALL_ROLES, PERMISSIONS
from fleet import services as fleet_services
from fleet.models import (
    BaseAuthorization,
    CapabilityApproval,
    CapabilityProfile,
    ConfigurationManifest,
    HoldPolicy,
    HoldPolicyApproval,
    InspectionTemplateApproval,
    ManifestPattern,
    PatternApproval,
    Robot,
)
from planning import services as planning_services
from planning.models import AssessmentApproval, Person, Qualification, Vehicle
from platform_core import interaction
from platform_core import workspace as ws_commands
from platform_core.models import AuditEvent, FormToken, Hub, Workspace
from readiness.models import ReadinessPolicy, ReadinessPolicyApproval
from tests.conftest import make_user

User = get_user_model()
TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')
BACKEND = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ helpers
def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _token(client: Client, url: str) -> str:
    r = client.get(url)
    assert r.status_code == 200, (url, r.status_code)
    m = TOKEN_RE.search(r.content.decode())
    assert m, f"no form token on {url}"
    return m.group(1)


def _post(client: Client, url: str, data: dict[str, Any], token: str | None = None, page: str | None = None):
    token = token or _token(client, page or url)
    return client.post(url, {**data, "_form_token": token})


def _html(r) -> str:
    return r.content.decode()


@pytest.fixture
def w(db) -> SimpleNamespace:
    """One user per role plus a small base fleet built through the real commands."""
    Workspace.ensure()
    w = SimpleNamespace()
    w.users = {role: make_user(f"u_{role}", role) for role in ALL_ROLES}
    w.admin, w.ops, w.lead = w.users["admin"], w.users["ops_lead"], w.users["engineering_lead"]
    w.safety, w.founder, w.field = w.users["safety_reviewer"], w.users["founder"], w.users["field_operator"]
    w.engineer = w.users["engineer"]
    w.newbie = make_user("newbie")  # an account with no role
    w.spare = make_user("spare")  # an account that can still be registered as a person
    fleet_services.create_pattern(w.lead, code="P-1", hardware_rev="H1", release="1.0", firmware_set="F1")
    fleet_services.register_robot(w.ops, code="R-01", serial="SN-01")
    planning_services.register_person(w.ops, username=w.field.username, code="P-1")
    planning_services.grant_qualification(
        w.ops,
        person_code="P-1",
        kind="field_lead",
        valid_from=timezone.now() - timedelta(days=1),
        valid_to=timezone.now() + timedelta(days=30),
    )
    w.qual_id = Qualification.objects.get(person__code="P-1", kind="field_lead").pk
    account_services.grant_role(w.admin, user_id=w.newbie.pk, role="field_operator")
    w.grant_id = RoleGrant.objects.get(user=w.newbie, role="field_operator").pk
    return w


# ------------------------------------------------------------------ the form cases
@dataclass
class Case:
    name: str
    url: Callable[[SimpleNamespace], str]
    gate: tuple[str, ...]  # permissions: a role holding none of them gets 403
    actor: str  # a role that can do it
    data: Callable[[SimpleNamespace], dict[str, Any]]
    snapshot: Callable[[], Any]
    ok_text: str
    service: tuple[Any, str]  # (module, name) of the EXISTING command the view must call once
    refusal: Callable[[SimpleNamespace], tuple[SimpleNamespace | None, str, dict[str, Any], str]] | None = (
        None
    )
    # refusal returns (actor override or None, role actor name, data, expected plain text)
    confirm: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _roles_without(*perms: str) -> list[str]:
    allowed: set[str] = set()
    for p in perms:
        allowed |= PERMISSIONS[p]
    return [r for r in ALL_ROLES if r not in allowed]


def _snap_ws():
    return Workspace.objects.values_list("name", "region", "time_zone").get(pk=1)


CASES: list[Case] = [
    Case(
        "company",
        lambda w: "/settings/company/",
        ("platform_core.manage_workspace",),
        "admin",
        lambda w: {
            "name": "Northbay Home Robotics",
            "region": "Bay Area",
            "time_zone": "America/Los_Angeles",
        },
        _snap_ws,
        "Company settings saved.",
        (ws_commands, "update_workspace"),
        refusal=lambda w: (
            None,
            "admin",
            {"name": "Northbay", "region": "Bay Area", "time_zone": "Mars/Olympus"},
            "is not a time zone name",
        ),
    ),
    Case(
        "hub",
        lambda w: "/settings/hubs/new/",
        ("platform_core.manage_hubs",),
        "admin",
        lambda w: {
            "code": "sf1",
            "name": "San Francisco",
            "area": "SoMa",
            "time_zone": "America/Los_Angeles",
        },
        lambda: list(Hub.objects.values_list("code", flat=True)),
        "Hub SF1 added.",
        (ws_commands, "create_hub"),
        refusal=lambda w: (
            None,
            "admin",
            {"code": "SF1", "name": "Again", "area": "", "time_zone": "America/Los_Angeles"},
            "already exists",
        ),
    ),
    Case(
        "invite",
        lambda w: "/settings/users/new/",
        ("accounts.create_user",),
        "admin",
        lambda w: {"username": "maria", "email": "", "password": "Temp-pass-8842!"},
        lambda: list(User.objects.values_list("username", flat=True).order_by("username")),
        "Account maria created.",
        (account_services, "create_user"),
        refusal=lambda w: (
            None,
            "admin",
            {"username": "newbie", "email": "", "password": "Temp-pass-8842!"},
            "is taken",
        ),
    ),
    Case(
        "grant_role",
        lambda w: f"/settings/users/{w.newbie.pk}/",
        ("accounts.grant_role",),
        "admin",
        lambda w: {"role": "ops_lead"},
        lambda: RoleGrant.objects.count(),
        "given to newbie",
        (account_services, "grant_role"),
        refusal=lambda w: (None, "admin", {"role": "field_operator"}, "already holds an active"),
    ),
    Case(
        "revoke_role",
        lambda w: f"/settings/users/{w.newbie.pk}/revoke/{w.grant_id}/",
        ("accounts.revoke_role",),
        "admin",
        lambda w: {"reason": "left the company", "confirm": "on"},
        lambda: RoleGrant.objects.filter(revoked_at__isnull=True).count(),
        "taken away from newbie",
        (account_services, "revoke_role"),
        confirm=True,
    ),
    Case(
        "robot",
        lambda w: "/fleet/new/",
        ("fleet.register_robot",),
        "ops_lead",
        lambda w: {"code": "R-02", "serial": "SN-02"},
        lambda: list(Robot.objects.values_list("code", flat=True).order_by("code")),
        "Robot R-02 added.",
        (fleet_services, "register_robot"),
        refusal=lambda w: (None, "ops_lead", {"code": "R-01", "serial": "SN-99"}, "already exists"),
    ),
    Case(
        "manifest",
        lambda w: "/fleet/R-01/manifest/",
        ("fleet.record_manifest",),
        "engineer",
        lambda w: {
            "pattern": "P-1",
            "autonomy_version": "a1",
            "safety_config_hash": "h" * 64,
            "calibration_ref": "",
            "provenance": "manual",
        },
        lambda: ConfigurationManifest.objects.count(),
        "Configuration recorded as a proposal",
        (fleet_services, "record_manifest"),
        refusal=None,
    ),
    Case(
        "lifecycle",
        lambda w: "/fleet/R-01/lifecycle/",
        ("fleet.transition_lifecycle", "fleet.retire_robot"),
        "engineering_lead",
        lambda w: {"to": "validation", "reason": "bench checks done", "confirm": "on"},
        lambda: Robot.objects.values_list("lifecycle", flat=True).get(code="R-01"),
        "moved to validation",
        (fleet_services, "transition_lifecycle"),
        refusal=lambda w: (
            None,
            "engineering_lead",
            {"to": "in_service", "reason": "go", "confirm": "on"},
            "Record a configuration manifest before putting a robot in service",
        ),
        confirm=True,
        extra={"command_perms": ("fleet.transition_lifecycle",)},
    ),
    Case(
        "pattern",
        lambda w: "/fleet/patterns/new/",
        ("fleet.manage_patterns",),
        "engineering_lead",
        lambda w: {"code": "P-2", "hardware_rev": "H2", "release": "2.0", "firmware_set": "F2"},
        lambda: list(ManifestPattern.objects.values_list("code", flat=True).order_by("code")),
        "Configuration P-2 added.",
        (fleet_services, "create_pattern"),
        refusal=lambda w: (
            None,
            "engineering_lead",
            {"code": "P-1", "hardware_rev": "H9", "release": "9", "firmware_set": "F9"},
            "exists",
        ),
    ),
    Case(
        "profile",
        lambda w: "/fleet/patterns/P-1/profile/",
        ("fleet.publish_capability",),
        "engineer",
        lambda w: {
            "stop_behaviour": "Holds position",
            "lift_crew": "2",
            "transport_posture": "",
            "max_runtime_min": "120",
            "min_departure_soc_pct": "",
            "charge_turnaround_min": "180",
            "load_unload_min": "",
            "source": "engineering_doc",
        },
        lambda: CapabilityProfile.objects.count(),
        "published as a draft",
        (fleet_services, "publish_capability_profile"),
        refusal=lambda w: (
            None,
            "engineer",
            {"lift_crew": "0", "source": "engineering_doc"},
            "Ensure this value is greater than or equal to 1",
        ),
    ),
    Case(
        "person",
        lambda w: "/people/new/",
        ("planning.manage_people",),
        "ops_lead",
        lambda w: {"username": "spare", "code": "P-9"},
        lambda: list(Person.objects.values_list("code", flat=True).order_by("code")),
        "P-9 registered.",
        (planning_services, "register_person"),
        refusal=lambda w: (None, "ops_lead", {"username": "spare", "code": "P-1"}, "already registered"),
    ),
    Case(
        "qualification",
        lambda w: "/people/P-1/",
        ("planning.manage_people", "planning.grant_inspector_qualification"),
        "ops_lead",
        lambda w: {
            "kind": "field_operator",
            "valid_from": "2026-01-01",
            "valid_to": "2030-01-01",
            "basis_ref": "",
        },
        lambda: Qualification.objects.count(),
        "granted to P-1",
        (planning_services, "grant_qualification"),
        refusal=lambda w: (
            None,
            "ops_lead",
            {"kind": "readiness_inspector", "valid_from": "", "valid_to": "2030-01-01", "basis_ref": "TR-1"},
            "Only the safety reviewer can do this.",
        ),
        extra={"command_perms": ("planning.manage_people",)},
    ),
    Case(
        "qualification_revoke",
        lambda w: f"/people/P-1/qualifications/{w.qual_id}/revoke/",
        ("planning.manage_people",),
        "ops_lead",
        lambda w: {"reason": "retrained", "confirm": "on"},
        lambda: Qualification.objects.filter(revoked_at__isnull=True).count(),
        "of P-1 revoked",
        (planning_services, "revoke_qualification"),
        confirm=True,
    ),
    Case(
        "vehicle",
        lambda w: "/vehicles/new/",
        ("planning.manage_vehicles",),
        "ops_lead",
        lambda w: {"code": "V-1", "robot_capacity": "2", "restraint_kit": "Kit A"},
        lambda: list(Vehicle.objects.values_list("code", flat=True)),
        "Vehicle V-1 added.",
        (planning_services, "register_vehicle"),
        refusal=lambda w: (
            None,
            "ops_lead",
            {"code": "V-1", "robot_capacity": "", "restraint_kit": ""},
            "exists",
        ),
    ),
]
IDS = [c.name for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_success_shows_feedback_and_calls_the_existing_command_once(w, case: Case):
    client = _client(w.users[case.actor])
    url = case.url(w)
    before = case.snapshot()
    module, name = case.service
    with mock.patch.object(module, name, wraps=getattr(module, name)) as spy:
        r = _post(client, url, case.data(w))
        assert r.status_code == 302, _html(r)[:2000]
        assert spy.call_count == 1  # the view called the intended existing command, once
    assert case.snapshot() != before
    page = client.get(r["Location"])
    html = _html(page)
    assert 'class="msg toast success"' in html and case.ok_text in html


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_every_role_without_the_permission_is_denied_server_side(w, case: Case):
    url = case.url(w)
    before = case.snapshot()
    denied_roles = _roles_without(*case.gate)
    assert denied_roles, "a form every role may use needs no denial test"
    for role in denied_roles:
        client = _client(w.users[role])
        if not (
            case.name == "qualification"
            and role in ("field_operator", "engineer", "engineering_lead", "founder")
        ):
            assert client.get(url).status_code == 403, role  # (people may READ a person's page)
        # even with a token someone else rendered, the server refuses and records nothing
        real_token = _token(_client(w.users[case.actor]), url)
        r = client.post(url, {**case.data(w), "_form_token": real_token})
        assert r.status_code == 403, role
        assert "Only" in _html(r) and "can do this" in _html(r)
    assert case.snapshot() == before
    assert Client().post(url, case.data(w)).status_code == 302  # anonymous: sent to sign in
    assert case.snapshot() == before


@pytest.mark.parametrize("case", [c for c in CASES if c.refusal], ids=[c.name for c in CASES if c.refusal])
def test_refusal_is_shown_in_plain_words_and_changes_nothing(w, case: Case):
    client = _client(w.users[case.actor])
    url = case.url(w)
    if case.name in ("hub", "vehicle"):  # first create it, so the second attempt is refused
        assert _post(client, url, case.data(w)).status_code == 302
    before = case.snapshot()
    _, role, data, text = case.refusal(w)
    r = _post(client, url, data)
    assert r.status_code in (400, 403, 409, 422), (r.status_code, _html(r)[:1500])
    html = _html(r)
    assert text in html
    assert "Traceback" not in html
    assert case.snapshot() == before


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_replayed_post_is_refused_and_does_nothing(w, case: Case):
    client = _client(w.users[case.actor])
    url = case.url(w)
    token = _token(client, url)
    assert _post(client, url, case.data(w), token=token).status_code == 302
    after_first = case.snapshot()
    module, name = case.service
    with mock.patch.object(module, name, wraps=getattr(module, name)) as spy:
        r = _post(client, url, case.data(w), token=token)  # the same form submitted again
        assert spy.call_count == 0
    assert r.status_code == 302
    assert case.snapshot() == after_first
    html = _html(client.get(r["Location"]))
    assert "already sent" in html and "nothing was done a second time" in html


@pytest.mark.parametrize("case", [c for c in CASES if c.confirm], ids=[c.name for c in CASES if c.confirm])
def test_destructive_actions_need_the_confirm_box_and_work_without_scripts(w, case: Case):
    client = _client(w.users[case.actor])
    url = case.url(w)
    before = case.snapshot()
    page = _html(client.get(url))
    assert 'name="confirm"' in page and 'class="confirm-box"' in page
    data = {k: v for k, v in case.data(w).items() if k != "confirm"}
    r = _post(client, url, data)
    assert r.status_code == 400 and "Tick the box to confirm." in _html(r)
    assert case.snapshot() == before


# ------------------------------------------------------------------ roles named in refusals
def test_lifecycle_refusals_name_the_responsible_role(w):
    # founder may open the page (retire) but only the engineering lead moves a robot to validation
    r = _post(
        _client(w.founder), "/fleet/R-01/lifecycle/", {"to": "validation", "reason": "x", "confirm": "on"}
    )
    assert r.status_code == 403 and "Only the engineering lead can do this." in _html(r)
    assert Robot.objects.get(code="R-01").lifecycle == "commissioning"
    # the engineering lead cannot retire: that is the founder's decision
    r = _post(_client(w.lead), "/fleet/R-01/lifecycle/", {"to": "retired", "reason": "x", "confirm": "on"})
    assert r.status_code == 403 and "Only the founder can do this." in _html(r)
    assert Robot.objects.get(code="R-01").lifecycle == "commissioning"


def test_readiness_inspector_grant_needs_the_safety_reviewer_and_says_so(w):
    page = _html(_client(w.ops).get("/people/P-1/"))
    assert "requires the safety reviewer" in page and "Only the safety reviewer can do this." in page
    before = Qualification.objects.count()
    data = {"kind": "readiness_inspector", "valid_from": "", "valid_to": "2030-01-01", "basis_ref": "TR-7"}
    assert _post(_client(w.ops), "/people/P-1/", data).status_code == 403
    assert Qualification.objects.count() == before
    # the safety reviewer can (the service still enforces its own rules)
    assert _post(_client(w.safety), "/people/P-1/", data).status_code == 302
    assert Qualification.objects.count() == before + 1
    # the safety reviewer has no right to the other qualifications
    other = {"kind": "field_operator", "valid_from": "2026-01-01", "valid_to": "2030-01-01", "basis_ref": ""}
    r = _post(_client(w.safety), "/people/P-1/", other)
    assert r.status_code == 403 and "Only the ops lead can do this." in _html(r)


def test_refusal_wording_helpers():
    assert interaction.refusal_text("fleet.retire_robot") == "Only the founder can do this."
    assert interaction.waiting_text("fleet.retire_robot") == "Waiting for the founder's approval"
    assert interaction.who_can("fleet.register_robot") == "the ops lead or the engineering lead"
    assert interaction.waiting_text("fleet.approve_capability") == (
        "Waiting for approval from the engineering lead or the safety reviewer"
    )


# ------------------------------------------------------------------ setup flow
def test_first_run_checklist_progress_and_who_approves(w):
    client = _client(w.admin)
    html = _html(client.get("/settings/setup/"))
    # people: the fixture users already hold roles; hub (v2 03A-1): the fixture's first robot was registered before
    # any hub existed, so the database created HUB-1 "Main hub" (platform_core_default_hub)
    assert "2 of 6 steps done" in html
    assert "never approves anything" in html and "Production readiness" in html
    assert "the safety reviewer" in html  # who must approve
    _post(
        client,
        "/settings/company/",
        {"name": "Northbay", "region": "Bay Area", "time_zone": "America/Los_Angeles"},
    )
    _post(
        client,
        "/settings/hubs/new/",
        {"code": "SF1", "name": "SF", "area": "", "time_zone": "America/Los_Angeles"},
    )
    r = _post(
        client, "/settings/users/new/", {"username": "maria", "email": "", "password": "Temp-pass-8842!"}
    )
    maria = User.objects.get(username="maria")
    assert r["Location"] == f"/settings/users/{maria.pk}/"
    assert not RoleGrant.objects.filter(user=maria).exists()  # an invitation gives no role
    _post(client, f"/settings/users/{maria.pk}/", {"role": "ops_lead"})
    html = _html(client.get("/settings/setup/"))
    assert (
        "5 of 6 steps done" in html and "Operating settings incomplete" in html
    )  # owner provisions the rest
    assert maria.check_password("Temp-pass-8842!")
    # the temporary password never reaches the audit log
    assert "Temp-pass" not in str(list(AuditEvent.objects.values_list("payload", "result")))
    # the audit log has one row per command
    cmds = set(AuditEvent.objects.values_list("command", flat=True))
    assert {"platform_core.update_workspace", "platform_core.create_hub", "accounts.create_user"} <= cmds


def test_company_settings_are_admin_only_and_time_zone_is_validated(w):
    for role in _roles_without("platform_core.manage_workspace"):
        assert _client(w.users[role]).get("/settings/company/").status_code == 403, role
        assert _client(w.users[role]).get("/settings/setup/").status_code == 403, role
    r = _post(
        _client(w.admin), "/settings/company/", {"name": "X", "region": "Y", "time_zone": "../etc/passwd"}
    )
    assert r.status_code == 400 and "is not a time zone name" in _html(r)
    assert Workspace.objects.get(pk=1).time_zone == "America/Los_Angeles"
    with pytest.raises(Exception, match="time zone"):
        ws_commands.update_workspace(w.admin, name="X", region="Y", time_zone="Nowhere/City")  # service too


def test_admin_cannot_grant_a_role_to_themselves(w):
    token = FormToken.objects.create(token="t" * 43, user=w.admin).token
    r = _post(_client(w.admin), f"/settings/users/{w.admin.pk}/", {"role": "founder"}, token=token)
    assert RoleGrant.objects.filter(user=w.admin).count() == 1
    html = _html(_client(w.admin).get(f"/settings/users/{w.admin.pk}/"))
    assert "cannot give a role to your own account" in html
    assert r.status_code in (200, 409, 422) and "Separation of duties" in _html(r)


def test_setup_creates_no_approval_qualification_or_policy_rows(w):
    """Contract section 4: setup records facts only."""
    tables = [
        CapabilityApproval,
        PatternApproval,
        BaseAuthorization,
        AssessmentApproval,
        HoldPolicy,
        HoldPolicyApproval,
        InspectionTemplateApproval,
        ReadinessPolicy,
        ReadinessPolicyApproval,
    ]
    before = {t.__name__: t.objects.count() for t in tables}
    quals_before = Qualification.objects.count()
    admin, ops, lead, eng = _client(w.admin), _client(w.ops), _client(w.lead), _client(w.engineer)
    _post(admin, "/settings/company/", {"name": "N", "region": "R", "time_zone": "America/New_York"})
    _post(
        admin,
        "/settings/hubs/new/",
        {"code": "H1", "name": "Hub", "area": "", "time_zone": "America/New_York"},
    )
    _post(admin, "/settings/users/new/", {"username": "zed", "email": "", "password": "Temp-pass-8842!"})
    zed = User.objects.get(username="zed")
    _post(admin, f"/settings/users/{zed.pk}/", {"role": "engineer"})
    # v2 03A-1: two hubs now (HUB-1 from the fixture, H1 above), so every new record names its hub
    _post(ops, "/fleet/new/", {"code": "R-77", "serial": "SN-77", "hub": "H1"})
    _post(
        lead,
        "/fleet/patterns/new/",
        {"code": "P-7", "hardware_rev": "H", "release": "1", "firmware_set": "F"},
    )
    _post(eng, "/fleet/R-77/manifest/", {"pattern": "P-7", "autonomy_version": "a", "safety_config_hash": "b" * 64,
                                         "calibration_ref": "", "provenance": "manual"})  # fmt: skip
    _post(eng, "/fleet/patterns/P-7/profile/", {"stop_behaviour": "stops", "source": "engineering_doc"})
    _post(ops, "/people/new/", {"username": "spare", "code": "P-8", "hub": "H1"})
    _post(ops, "/vehicles/new/", {"code": "V-7", "robot_capacity": "", "restraint_kit": "", "hub": "H1"})
    assert (
        Robot.objects.filter(code="R-77").exists()
        and CapabilityProfile.objects.filter(pattern__code="P-7").exists()
    )
    assert {t.__name__: t.objects.count() for t in tables} == before
    assert Qualification.objects.count() == quals_before
    # and the new robot is still not eligible for anything
    e = fleet_services.evaluate_eligibility(Robot.objects.get(code="R-77"))
    assert e.eligible_for_planning is False


def test_list_pages_have_empty_states_with_the_next_step(db):
    Workspace.ensure()
    ops = _client(make_user("e_ops", "ops_lead"))
    lead = _client(make_user("e_lead", "engineering_lead"))
    admin = _client(make_user("e_admin", "admin"))
    assert "No robots yet" in _html(ops.get("/fleet/")) and "/fleet/new/" in _html(ops.get("/fleet/"))
    assert "No configurations yet" in _html(lead.get("/fleet/patterns/"))
    assert "No people registered yet" in _html(ops.get("/people/")) and "/people/new/" in _html(
        ops.get("/people/")
    )
    assert "No vehicles yet" in _html(ops.get("/vehicles/")) and "/vehicles/new/" in _html(
        ops.get("/vehicles/")
    )
    assert "No crew yet" in _html(ops.get("/crews/"))
    assert "No hubs yet" in _html(admin.get("/settings/hubs/"))
    # a viewer who cannot add is told who can
    field = _client(make_user("e_field", "field_operator"))
    assert "ask the ops lead or the engineering lead" in _html(field.get("/fleet/"))


def test_crews_are_people_with_a_field_role(w):
    html = _html(_client(w.ops).get("/crews/"))
    assert "u_field_operator" in html and "P-1" in html and "field lead" in html
    assert "newbie" in html and "Not registered as a person yet" in html  # role holder, no person record yet


def test_people_and_robot_pages_show_actions_only_to_roles_that_may_act(w):
    assert "Add a robot" in _html(_client(w.ops).get("/fleet/"))
    assert "Add a robot" not in _html(_client(w.field).get("/fleet/"))
    assert "Register a person" not in _html(_client(w.field).get("/people/"))
    detail = _html(_client(w.engineer).get("/fleet/R-01/"))
    assert "Record configuration" in detail and "Change lifecycle" not in detail  # lifecycle is the lead's
    assert "Change lifecycle" in _html(_client(w.lead).get("/fleet/R-01/"))
    assert "Revoke" in _html(_client(w.ops).get("/people/P-1/"))
    assert "Revoke" not in _html(_client(w.field).get("/people/P-1/"))


# ------------------------------------------------------------------ shared components
def test_form_token_is_one_time_and_bound_to_the_user(w):
    client = _client(w.ops)
    token = _token(client, "/vehicles/new/")
    other = _client(w.lead)
    r = other.post("/vehicles/new/", {"_form_token": token, "code": "V-X"})
    assert r.status_code == 403 and not Vehicle.objects.filter(code="V-X").exists()
    assert FormToken.objects.get(token=token).consumed_at is None  # someone else's token was not spent
    assert client.post("/vehicles/new/", {"_form_token": token, "code": "V-X"}).status_code == 302
    assert FormToken.objects.get(token=token).consumed_at is not None
    expired = client.post("/vehicles/new/", {"code": "V-Y"})  # no token at all
    assert expired.status_code == 302 and not Vehicle.objects.filter(code="V-Y").exists()
    assert "had expired" in _html(client.get(expired["Location"]))


def test_old_tokens_expire(w):
    client = _client(w.ops)
    token = _token(client, "/vehicles/new/")
    FormToken.objects.filter(token=token).update(created_at=timezone.now() - timedelta(days=2))
    r = client.post("/vehicles/new/", {"_form_token": token, "code": "V-Z"})
    assert r.status_code == 302 and not Vehicle.objects.filter(code="V-Z").exists()


def test_invalid_form_rerenders_with_inline_errors_and_a_fresh_token(w):
    client = _client(w.ops)
    r = _post(client, "/vehicles/new/", {"code": "", "robot_capacity": "-3"})
    html = _html(r)
    assert r.status_code == 400
    assert html.count('class="err"') >= 2 and "This field is required." in html
    assert 'aria-invalid="true"' in html and 'id="id_robot_capacity-help"' in html
    assert TOKEN_RE.search(html)  # a fresh token for the retry
    assert Vehicle.objects.count() == 0


def test_toast_banner_for_error_and_success_levels(w):
    html = _html(_client(w.ops).get("/vehicles/"))
    assert 'class="toasts"' not in html  # nothing to say: no banner
    client = _client(w.ops)
    r = _post(client, "/fleet/new/", {"code": "R-01", "serial": "other"})
    html = _html(r)
    assert 'class="msg toast error"' in html and "Not done." in html and 'role="alert"' in html


def test_new_templates_use_no_inline_scripts():
    """The CSP allows one hashed script: the components work without JavaScript and add none."""
    new = [
        *BACKEND.glob("templates/settings/*.html"),
        *BACKEND.glob("templates/resources/*.html"),
        *BACKEND.glob("fleet/templates/fleet/setup_*.html"),
        BACKEND / "templates/ui/form.html",
        BACKEND / "templates/ui/form_field.html",
        BACKEND / "templates/ui/denied.html",
        BACKEND / "templates/ui/confirm_box.html",
    ]
    assert len(new) > 15
    assert [str(p) for p in new if "<script" in p.read_text()] == []
    csp = (BACKEND / "config/settings.py").read_text()
    assert "CSP_SCRIPT_HASH" in csp


def test_admin_can_reach_setup_from_the_user_menu(w):
    html = _html(_client(w.admin).get("/settings/setup/"))
    assert 'href="/settings/setup/"' in html and "Workspace setup" in html


# ------------------------------------------------------------------ concurrency
@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_registrations_of_the_same_robot_code_exactly_one_succeeds():
    Workspace.ensure()
    ops = make_user("cc_ops", "ops_lead")
    clients = [_client(ops), _client(ops)]
    tokens = [_token(c, "/fleet/new/") for c in clients]
    barrier = threading.Barrier(2)
    results: dict[int, Any] = {}

    def run(i: int) -> None:
        try:
            barrier.wait(10)
            r = clients[i].post(
                "/fleet/new/", {"code": "R-RACE", "serial": f"SN-{i}", "_form_token": tokens[i]}
            )
            results[i] = (r.status_code, _html(r) if r.status_code != 302 else "")
        except Exception as exc:  # noqa: BLE001
            results[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    codes = sorted(r[0] for r in results.values() if isinstance(r, tuple))
    assert codes == [302, 409], results
    assert Robot.objects.filter(code="R-RACE").count() == 1
    loser = next(r for r in results.values() if r[0] == 409)
    assert "already exists" in loser[1]


@pytest.mark.django_db(transaction=True)
def test_the_same_form_token_posted_twice_at_once_runs_the_command_once():
    Workspace.ensure()
    ops = make_user("cc2_ops", "ops_lead")
    clients = [_client(ops), _client(ops)]
    token = _token(clients[0], "/vehicles/new/")
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def run(i: int) -> None:
        try:
            barrier.wait(10)
            codes.append(
                clients[i].post("/vehicles/new/", {"code": "V-DBL", "_form_token": token}).status_code
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert codes == [302, 302]  # both are answered kindly ...
    assert Vehicle.objects.filter(code="V-DBL").count() == 1  # ... but the command ran once
    assert AuditEvent.objects.filter(command="planning.register_vehicle", outcome="committed").count() == 1
