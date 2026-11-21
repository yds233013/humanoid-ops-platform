"""v2 01C approval screens: pattern decision, capability profile approval, base home-use authorization.

Per screen: success by the right role, server-side denial for every other role (403, nothing written), a plain
refusal naming the role, a replayed POST refused, the button shown only to roles that may act, and viewing the
screens never creates an approval.
"""

import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client

from accounts.roles import ALL_ROLES, PERMISSIONS
from fleet import services as fs
from fleet.models import BaseAuthorization, CapabilityApproval, CapabilityProfile, PatternApproval
from platform_core.models import Workspace
from tests.conftest import make_user

TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')


def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _html(r: Any) -> str:
    return str(r.content.decode())


def _post(client: Client, url: str, data: dict[str, Any], page: str | None = None, token: str | None = None):
    if token is None:
        got = client.get(page or url)
        assert got.status_code == 200, (url, got.status_code)
        m = TOKEN_RE.search(_html(got))
        assert m, f"no token on {page or url}"
        token = m.group(1)
    return client.post(url, {**data, "_form_token": token}, follow=True)


def _counts() -> tuple[int, int, int]:
    return (
        PatternApproval.objects.count(),
        CapabilityApproval.objects.count(),
        BaseAuthorization.objects.count(),
    )


def _others(*perms: str) -> list[str]:
    allowed: set[str] = set()
    for p in perms:
        allowed |= PERMISSIONS[p]
    return [r for r in ALL_ROLES if r not in allowed]


@pytest.fixture
def w(db: Any) -> SimpleNamespace:
    Workspace.ensure()
    w = SimpleNamespace()
    w.users = {role: make_user(f"u_{role}", role) for role in ALL_ROLES}
    w.lead, w.safety, w.eng, w.ops = (
        w.users[r] for r in ("engineering_lead", "safety_reviewer", "engineer", "ops_lead")
    )
    fs.create_pattern(w.lead, code="P-A", hardware_rev="H1", release="1.0", firmware_set="F1")
    fs.register_robot(w.ops, code="R-A", serial="SN-A")
    fs.record_manifest(
        w.eng, robot_code="R-A", pattern_code="P-A", autonomy_version="a1", safety_config_hash="b" * 64
    )
    fs.publish_capability_profile(
        w.eng,
        pattern_code="P-A",
        source="engineering_doc",
        stop_behaviour="stops in place",
        lift_crew=2,
        max_runtime_min=60,
        charge_turnaround_min=90,
    )
    w.profile = CapabilityProfile.objects.get(pattern__code="P-A", version_no=1)
    w.decision_url = "/fleet/patterns/P-A/decision/"
    w.approve_url = f"/fleet/patterns/P-A/profile/approve/{w.profile.pk}/"
    w.auth_url = "/fleet/R-A/authorize/"
    w.revoke_url = "/fleet/R-A/authorize/revoke/"
    return w


# ------------------------------------------------------------------ 1. pattern decision
def test_decision_success_by_engineering_lead_needs_confirm(w: SimpleNamespace) -> None:
    c = _client(w.lead)
    r = _post(c, w.decision_url, {"status": "approved_home", "reason": "ok"})  # no tick
    assert "Tick the box to confirm." in _html(r) and PatternApproval.objects.count() == 0
    r = _post(c, w.decision_url, {"status": "approved_home", "reason": "Reviewed", "confirm": "on"})
    assert "Decision recorded for configuration P-A" in _html(r)
    a = PatternApproval.objects.get()
    assert a.status == "approved_home" and a.decided_by == w.lead and a.reason == "Reviewed"


@pytest.mark.parametrize("role", _others("fleet.decide_pattern"))
def test_decision_denied_for_every_other_role(w: SimpleNamespace, role: str) -> None:
    c = _client(w.users[role])
    got = c.get(w.decision_url)
    assert got.status_code == 403 and "Only the engineering lead can do this." in _html(got)
    r = c.post(
        w.decision_url, {"status": "approved_home", "reason": "x", "confirm": "on", "_form_token": "t"}
    )
    assert r.status_code == 403 and PatternApproval.objects.count() == 0


def test_decision_replay_refused(w: SimpleNamespace) -> None:
    c = _client(w.lead)
    token = TOKEN_RE.search(_html(c.get(w.decision_url))).group(1)  # type: ignore[union-attr]
    data = {"status": "approved_home", "reason": "r", "confirm": "on"}
    _post(c, w.decision_url, data, token=token)
    r = _post(c, w.decision_url, data, token=token)
    assert "already sent" in _html(r) and PatternApproval.objects.count() == 1


def test_decision_service_refusal_is_plain_and_names_the_role(w: SimpleNamespace, monkeypatch: Any) -> None:
    # the page gate passes but the command refuses: the refusal names the role from the permission table
    from platform_core.commands import PermissionDenied

    def refuse(*a: Any, **k: Any) -> Any:
        raise PermissionDenied("Not allowed: fleet.decide_pattern")

    monkeypatch.setattr(fs, "decide_pattern", refuse)
    r = _post(_client(w.lead), w.decision_url, {"status": "withdrawn", "reason": "r", "confirm": "on"})
    assert r.status_code == 403 and "Only the engineering lead can do this." in _html(r)


def test_decision_calls_exactly_one_command(w: SimpleNamespace, monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []
    real = fs.decide_pattern

    def spy(actor: Any, **kw: Any) -> Any:
        calls.append(kw)
        return real(actor, **kw)

    monkeypatch.setattr(fs, "decide_pattern", spy)
    _post(_client(w.lead), w.decision_url, {"status": "engineering_only", "reason": "r", "confirm": "on"})
    assert calls == [{"pattern_code": "P-A", "status": "engineering_only", "reason": "r"}]


# ------------------------------------------------------------------ 2. capability profile approval
def test_profile_approval_by_each_slot_role(w: SimpleNamespace) -> None:
    got = _client(w.lead).get(w.approve_url)
    html = _html(got)
    assert f"Profile id {w.profile.pk}" in html and "version 1" in html and 'data-slot="engineering"' in html
    r = _post(_client(w.lead), w.approve_url, {"slot": "engineering", "confirm": "on"})
    assert "Approval recorded for capability profile v1" in _html(r)
    r = _post(_client(w.safety), w.approve_url, {"slot": "safety", "confirm": "on"})
    assert "Approval recorded" in _html(r)
    assert {a.slot: a.approver for a in CapabilityApproval.objects.all()} == {
        "engineering": w.lead,
        "safety": w.safety,
    }
    assert "Both approvals are recorded" in _html(_client(w.lead).get(w.approve_url))


@pytest.mark.parametrize("role", _others("fleet.approve_capability"))
def test_profile_approval_denied_for_every_other_role(w: SimpleNamespace, role: str) -> None:
    c = _client(w.users[role])
    got = c.get(w.approve_url)
    assert got.status_code == 403 and "engineering lead or the safety reviewer" in _html(got)
    r = c.post(w.approve_url, {"slot": "engineering", "confirm": "on", "_form_token": "t"})
    assert r.status_code == 403 and CapabilityApproval.objects.count() == 0


@pytest.mark.parametrize(
    ("actor", "slot", "named"),
    [
        ("engineering_lead", "safety", "the safety reviewer"),
        ("safety_reviewer", "engineering", "the engineering lead"),
    ],
)
def test_profile_wrong_slot_refused_in_plain_words(
    w: SimpleNamespace, actor: str, slot: str, named: str
) -> None:
    r = _post(_client(w.users[actor]), w.approve_url, {"slot": slot, "confirm": "on"}, page=w.approve_url)
    assert r.status_code == 409 and f"Only {named} can approve the" in _html(r)
    assert CapabilityApproval.objects.count() == 0


def test_profile_same_person_cannot_fill_both_slots(w: SimpleNamespace) -> None:
    both = make_user("both", "engineering_lead", "safety_reviewer")
    c = _client(both)
    _post(c, w.approve_url, {"slot": "engineering", "confirm": "on"})
    r = _post(c, w.approve_url, {"slot": "safety", "confirm": "on"})
    assert r.status_code == 409 and "already approved" in _html(r) and CapabilityApproval.objects.count() == 1


def test_profile_replay_refused(w: SimpleNamespace) -> None:
    c = _client(w.lead)
    token = TOKEN_RE.search(_html(c.get(w.approve_url))).group(1)  # type: ignore[union-attr]
    data = {"slot": "engineering", "confirm": "on"}
    _post(c, w.approve_url, data, token=token)
    r = _post(c, w.approve_url, data, token=token)
    assert "already sent" in _html(r) and CapabilityApproval.objects.count() == 1


def test_profile_slot_already_approved_is_refused(w: SimpleNamespace) -> None:
    lead2 = make_user("lead2", "engineering_lead")
    c2 = _client(lead2)
    token = TOKEN_RE.search(_html(c2.get(w.approve_url))).group(1)  # type: ignore[union-attr]  # page opened early
    _post(_client(w.lead), w.approve_url, {"slot": "engineering", "confirm": "on"})
    r = _post(c2, w.approve_url, {"slot": "engineering", "confirm": "on"}, token=token)
    assert "already approved" in _html(r) and CapabilityApproval.objects.count() == 1


def test_profile_page_of_other_pattern_is_404(w: SimpleNamespace) -> None:
    fs.create_pattern(w.lead, code="P-B", hardware_rev="H", release="1", firmware_set="F")
    assert _client(w.lead).get(f"/fleet/patterns/P-B/profile/approve/{w.profile.pk}/").status_code == 404


# ------------------------------------------------------------------ 3. base authorization
def test_grant_and_revoke_by_engineering_lead(w: SimpleNamespace) -> None:
    c = _client(w.lead)
    r = _post(c, w.auth_url, {})  # no tick
    assert "Tick the box to confirm." in _html(r) and BaseAuthorization.objects.count() == 0
    r = _post(c, w.auth_url, {"confirm": "on"})
    assert "Home-use authorization granted for R-A" in _html(r)
    a = BaseAuthorization.objects.get()
    assert a.granted_by == w.lead and a.revoked_at is None
    r = _post(c, w.auth_url, {"confirm": "on"})
    assert r.status_code == 409 and "Already authorized" in _html(r)
    r = _post(c, w.revoke_url, {"reason": "Sensor fault", "confirm": "on"})
    assert "authorization revoked for R-A" in _html(r)
    a.refresh_from_db()
    assert a.revoked_at is not None and a.revoked_by == w.lead


@pytest.mark.parametrize("role", _others("fleet.base_authorization"))
def test_authorization_denied_for_every_other_role(w: SimpleNamespace, role: str) -> None:
    c = _client(w.users[role])
    for url in (w.auth_url, w.revoke_url):
        got = c.get(url)
        assert got.status_code == 403 and "Only the engineering lead can do this." in _html(got)
        r = c.post(url, {"confirm": "on", "reason": "x", "_form_token": "t"})
        assert r.status_code == 403
    assert BaseAuthorization.objects.count() == 0


def test_authorization_replay_refused(w: SimpleNamespace) -> None:
    c = _client(w.lead)
    token = TOKEN_RE.search(_html(c.get(w.auth_url))).group(1)  # type: ignore[union-attr]
    _post(c, w.auth_url, {"confirm": "on"}, token=token)
    r = _post(c, w.auth_url, {"confirm": "on"}, token=token)
    assert "already sent" in _html(r) and BaseAuthorization.objects.count() == 1


def test_authorization_without_manifest_is_refused(w: SimpleNamespace) -> None:
    fs.register_robot(w.ops, code="R-B", serial="SN-B")
    r = _post(_client(w.lead), "/fleet/R-B/authorize/", {"confirm": "on"})
    assert r.status_code == 409 and "no configuration manifest" in _html(r).lower()


def test_revoke_without_active_authorization_is_refused(w: SimpleNamespace) -> None:
    r = _post(_client(w.lead), w.revoke_url, {"reason": "x", "confirm": "on"})
    assert r.status_code == 409 and "No active base authorization" in _html(r)


# ------------------------------------------------------------------ buttons per role
@pytest.mark.parametrize("role", ALL_ROLES)
def test_buttons_only_for_roles_that_may_act(w: SimpleNamespace, role: str) -> None:
    c = _client(w.users[role])
    if "fleet.read" not in PERMISSIONS or role not in PERMISSIONS["fleet.read"]:
        return  # admin has no fleet access at all
    pattern = _html(c.get("/fleet/patterns/P-A/"))
    robot = _html(c.get("/fleet/R-A/"))
    profile_page = (
        _html(c.get("/fleet/patterns/P-A/profile/"))
        if role in PERMISSIONS["fleet.publish_capability"]
        else ""
    )
    if role in PERMISSIONS["fleet.decide_pattern"]:
        assert w.decision_url in pattern
    else:
        assert w.decision_url not in pattern and "Waiting for the engineering lead" in pattern
    if role in PERMISSIONS["fleet.base_authorization"]:
        assert w.auth_url in robot
    else:
        assert w.auth_url not in robot and "Waiting for the engineering lead" in robot
    if profile_page:
        assert w.approve_url in profile_page  # the link is shown; the screen itself says who may act
    approve_page = c.get(w.approve_url)
    if role in PERMISSIONS["fleet.approve_capability"]:
        body = _html(approve_page)
        assert "Approve</button>" in body
        other = "safety" if role == "engineering_lead" else "engineering"
        mine = "engineering" if role == "engineering_lead" else "safety"
        assert f'value="{mine}"' in body and f'value="{other}"' not in body
    else:
        assert approve_page.status_code == 403


def test_revoke_button_shown_only_to_lead_once_authorized(w: SimpleNamespace) -> None:
    fs.grant_base_authorization(w.lead, robot_code="R-A")
    assert w.revoke_url in _html(_client(w.lead).get("/fleet/R-A/"))
    assert w.revoke_url not in _html(_client(w.ops).get("/fleet/R-A/"))
    assert f'href="{w.auth_url}"' not in _html(
        _client(w.lead).get("/fleet/R-A/")
    )  # already authorized: no grant button


def test_viewing_the_screens_creates_no_approval(w: SimpleNamespace) -> None:
    before = _counts()
    for role in ALL_ROLES:
        c = _client(w.users[role])
        for url in (
            "/fleet/patterns/P-A/",
            "/fleet/patterns/",
            "/fleet/R-A/",
            "/fleet/patterns/P-A/profile/",
            w.decision_url,
            w.approve_url,
            w.auth_url,
            w.revoke_url,
        ):
            assert c.get(url).status_code in (200, 403)
    assert _counts() == before == (0, 0, 0)


def test_setup_profile_text_links_to_the_real_screen(w: SimpleNamespace) -> None:
    html = _html(_client(w.eng).get("/fleet/patterns/P-A/profile/"))
    assert "on the approval screen" not in html
    assert w.approve_url in html and f"profile id {w.profile.pk}" in html
