"""v2 01D: the demo workspace (docs/10_v2_phase01_contract.md section 5 "01D", sections 3, 4 and 12).

One expensive test builds the demo from an empty database, checks it, rebuilds it with --reset and checks that the
rebuild is identical (deterministic for the date). The other tests are cheap refusal and badge checks.
"""

import io
import re
import time
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone

from accounts.models import RoleGrant
from execution.models import (
    EscalationDeadlineSetApproval,
    ExecutionRun,
    Incident,
    IncidentPolicyApproval,
    TripClosure,
)
from fleet import services as fs
from fleet.models import (
    CapabilityApproval,
    Hold,
    HoldPolicy,
    HoldPolicyApproval,
    InspectionTemplateApproval,
    PeriodicInspectionRuleApproval,
    Robot,
)
from operations import demo_workspace as demo
from operations.exceptions import open_exceptions
from operations.readmodels import production_blockers
from planning.models import Qualification, Site, Visit
from platform_core.models import Hub, Workspace
from readiness import commands as rc
from readiness import evidence as rx
from readiness.evaluator import evaluate_readiness
from readiness.models import DepartureAuthorization, ReadinessPolicyApproval
from tests.conftest import make_user
from tests.fleet_world import set_setting

pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()
LA = ZoneInfo("America/Los_Angeles")
PASSWORD = "demo-test-password-4821"
BUILD_BUDGET_S = 60


def _build(*args: str) -> str:
    out = io.StringIO()
    call_command("build_demo_workspace", *args, stdout=out)
    return out.getvalue()


def _local_day(dt) -> date:  # noqa: ANN001
    return dt.astimezone(LA).date()


def _fingerprint() -> list[tuple]:
    rows = []
    for v in Visit.objects.select_related(
        "site", "current_revision__robot", "current_revision__lead", "current_revision__vehicle"
    ).order_by("code"):
        r = v.current_revision
        rows.append(
            (
                v.code,
                v.site.code,
                v.status,
                r.on_site_start,
                r.robot.code if r.robot else None,
                r.lead.code if r.lead else None,
                r.vehicle.code if r.vehicle else None,
            )
        )
    return rows


def _visible_text(html: str) -> str:
    html = re.sub(r"<style.*?</style>|<script.*?</script>|<details.*?</details>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


@pytest.fixture
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> date:
    """The anchor date of the build, frozen once for the whole test (no drift across midnight)."""
    today = demo.workspace_today()
    monkeypatch.setattr(demo, "workspace_today", lambda: today)
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    return today


def test_build_from_empty_database_then_reset_rebuilds_the_same_workspace(frozen_today: date) -> None:
    assert demo.is_empty()
    t0 = time.perf_counter()
    out = _build()
    elapsed = time.perf_counter() - t0
    print(f"\nbuild_demo_workspace: {elapsed:.1f} s\n{out}")
    assert elapsed < BUILD_BUDGET_S, f"the demo build took {elapsed:.1f} s (budget {BUILD_BUDGET_S} s)"

    # ---- the company
    ws = Workspace.objects.get(pk=1)
    assert (ws.name, ws.region, ws.time_zone, ws.demo, ws.initialized) == (
        "Northbay Home Robotics",
        "San Francisco Bay Area",
        "America/Los_Angeles",
        True,
        True,
    )
    assert fs.demo_mode()
    assert Hub.objects.count() == 1 and Hub.objects.get().name == "Oakland hub"
    assert Robot.objects.count() == 50  # fixture S
    assert Robot.objects.filter(lifecycle="in_service").count() == 45
    crews = set(
        Visit.objects.exclude(current_revision__lead=None).values_list(
            "current_revision__lead__code",
            "current_revision__second__code",
            "current_revision__vehicle__code",
        )
    )
    assert len(crews) == 3
    for lead, second, _van in crews:
        for code, kinds in (
            (lead, {"field_lead", "robot_handler"}),
            (second, {"field_operator", "robot_handler"}),
        ):
            have = set(Qualification.objects.filter(person__code=code).values_list("kind", flat=True))
            assert kinds | {"readiness_inspector"} <= have
    assert 30 <= Site.objects.count() <= 60
    towns = set(Site.objects.values_list("zone", flat=True))
    assert {"Oakland", "Berkeley", "Alameda"} <= towns
    # fictional data that reads as real: nothing labelled synthetic or test in the demo's own names
    names = [
        *Site.objects.values_list("zone", "crm_ref"),
        *Robot.objects.values_list("code", "serial"),
        *User.objects.values_list("username", flat=True),
        *Hold.objects.values_list("reason", flat=True),
        *Incident.objects.values_list("description", flat=True),
    ]
    assert not re.search(r"(?i)synthetic|\btest\b|\bdemo\b|SYN-", repr(names))

    # ---- visits from 7 days ago to 14 days ahead of the workspace's local today (Sundays off)
    days = sorted(_local_day(v.current_revision.on_site_start) for v in Visit.objects.all())
    assert frozen_today - timedelta(days=7) <= days[0] <= frozen_today - timedelta(days=6)
    assert frozen_today + timedelta(days=13) <= days[-1] <= frozen_today + timedelta(days=14)
    assert frozen_today in days and frozen_today + timedelta(days=1) in days
    past = [v for v in Visit.objects.all() if _local_day(v.current_revision.on_site_start) < frozen_today]
    assert len(past) >= 15
    # every past visit departed through the execution commands; all closed except the one overdue run
    runs = ExecutionRun.objects.filter(visit__in=past)
    assert runs.count() == len(past)
    open_runs = [r for r in runs if not TripClosure.objects.filter(run=r).exists()]
    assert len(open_runs) == 1
    # departures exist only for past visits: nothing of today or later is authorized
    authorized_days = {
        _local_day(a.visit.current_revision.on_site_start) for a in DepartureAuthorization.objects.all()
    }
    assert authorized_days and max(authorized_days) < frozen_today

    # ---- realistic exceptions
    active_holds = Hold.objects.filter(released_at__isnull=True)
    assert set(active_holds.values_list("source_type", flat=True)) == {
        "incident",
        "inspection",
        "maintenance",
    }
    assert Incident.objects.count() == 1
    codes = {e.code for e in open_exceptions(timezone.now(), include_readiness=False).items}
    assert {"RETURN_OVERDUE", "HELD_ROBOT_ASSIGNED"} <= codes, codes
    statuses = {v.status for v in Visit.objects.all()}
    assert {"confirmed", "planned", "requested"} <= statuses
    feas = {v.current_revision.feasibility for v in Visit.objects.all()}
    assert {"feasible", "review", "blocked"} <= feas

    # ---- journey J4: one visit is prepared, NOT authorized; only the day's checks are missing
    j4_code = re.search(r"Journey J4: visit (V-\d+)", out)[1]
    j4 = Visit.objects.get(code=j4_code)
    assert j4.status == "confirmed" and not DepartureAuthorization.objects.filter(visit=j4).exists()
    result = evaluate_readiness(j4)
    assert not result.ready
    assert {b["code"] for b in result.blockers} == {"EVIDENCE_MISSING"}, result.blockers

    # ---- setup never approves (section 4): every approval is a demo-seed kind on a record production refuses
    assert InspectionTemplateApproval.objects.count() == 0
    assert HoldPolicyApproval.objects.count() == 0 and HoldPolicy.objects.get().source == "synthetic_demo"
    assert {a.profile.source for a in CapabilityApproval.objects.all()} == {"synthetic_demo"}
    assert {a.rule.source for a in PeriodicInspectionRuleApproval.objects.all()} == {"synthetic_demo"}
    assert {a.policy.source for a in ReadinessPolicyApproval.objects.all()} == {"synthetic_demo"}
    assert {a.policy.source for a in IncidentPolicyApproval.objects.all()} == {"synthetic_demo"}
    assert {a.deadline_set.source for a in EscalationDeadlineSetApproval.objects.all()} == {"synthetic_demo"}
    # production blockers still hold: evaluated as production, the panel lists blockers ...
    panel = production_blockers(timezone.now())
    assert not panel.production and len(panel.items) >= 3
    # ... and in production mode nothing of the demo can leave or be planned
    set_setting("demo_mode", "false")
    try:
        prod = evaluate_readiness(Visit.objects.get(code=j4_code))
        assert "POLICY_NOT_APPROVED_PROCEDURE" in {b["code"] for b in prod.blockers}
        assert not fs.evaluate_eligibility(Robot.objects.get(code="NB-001")).eligible_for_planning
        assert production_blockers(timezone.now()).production
    finally:
        set_setting("demo_mode", "true")

    # ---- the badge, once, on Today; the logins in the output, never the password
    ops = User.objects.get(username="dana.ortiz")
    assert ops.check_password(PASSWORD) and RoleGrant.objects.filter(user=ops, role="ops_lead").exists()
    assert PASSWORD not in out
    for username in ("priya.raman", "dana.ortiz", "jordan.lee", "elena.vasquez", "kenji.watanabe"):
        assert username in out
    c = Client()
    c.force_login(ops)
    html = c.get("/ops/").content.decode()
    assert html.count("Demo workspace") == 1
    assert not re.search(r"(?i)synthetic", _visible_text(html))

    # ---- rebuild with --reset: everything destroyed and rebuilt identically for the date
    before = _fingerprint()
    counts = (User.objects.count(), Robot.objects.count(), Site.objects.count(), Visit.objects.count())
    with pytest.raises(CommandError, match="--reset"):
        _build()  # non-empty demo workspace without --reset
    t1 = time.perf_counter()
    out2 = _build("--reset")
    print(f"build_demo_workspace --reset: {time.perf_counter() - t1:.1f} s")
    assert "every row of the previous demo workspace was destroyed" in out2
    assert _fingerprint() == before
    assert (
        User.objects.count(),
        Robot.objects.count(),
        Site.objects.count(),
        Visit.objects.count(),
    ) == counts
    assert not DepartureAuthorization.objects.filter(visit__code=j4_code).exists()

    # ---- J4 is possible: once the crew records the day's checks, the ops lead's decision goes through
    j4 = Visit.objects.get(code=j4_code)
    robot = j4.current_revision.robot.code
    lead, second = j4.current_revision.lead, j4.current_revision.second
    insp = fs.submit_inspection(
        ops,
        robot_code=robot,
        template_version="T1",
        answers=dict(demo.ALL_PASS),
        performer=lead.user.username,
        verifier=second.user.username,
        visit_ref=j4_code,
    )["entity_id"]
    rx.record_evidence(
        ops, visit_code=j4_code, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    rx.record_evidence(
        second.user, visit_code=j4_code, kind="equipment_check", result="pass", details=demo.EQUIPMENT
    )
    rx.record_evidence(
        second.user, visit_code=j4_code, kind="battery_check", result="pass", details={"soc_pct": 90}
    )
    assert evaluate_readiness(Visit.objects.get(code=j4_code)).ready
    auth = rc.authorize_departure(ops, visit_code=j4_code)
    assert DepartureAuthorization.objects.filter(code=auth["code"], visit__code=j4_code).exists()


# ------------------------------------------------------------------ refusals (cheap)
def _company_workspace() -> None:
    """A real (non-demo) workspace: an admin exists, the workspace is initialized, demo flag off."""
    make_user("acme_admin", "admin")
    Workspace.ensure()
    Workspace.objects.filter(pk=1).update(name="Acme", initialized=True, demo=False)


def test_refuses_a_non_demo_workspace_with_and_without_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    _company_workspace()
    for args in ((), ("--reset",)):
        with pytest.raises(CommandError, match="not a demo workspace"):
            _build(*args)
    assert Robot.objects.count() == 0 and User.objects.filter(username="acme_admin").exists()
    assert Workspace.objects.get(pk=1).name == "Acme"


def test_reset_refuses_when_the_demo_flag_is_on_but_the_deployment_is_in_production_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    _company_workspace()
    Workspace.objects.filter(pk=1).update(demo=True)
    set_setting("demo_mode", "false")
    with pytest.raises(CommandError, match="not a demo workspace"):
        _build("--reset")
    with pytest.raises(demo.DemoRefused):
        demo.wipe()  # the wipe checks again by itself
    assert User.objects.filter(username="acme_admin").exists()


def test_reset_refuses_when_demo_mode_is_on_but_the_workspace_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    _company_workspace()
    set_setting("demo_mode", "true")
    with pytest.raises(CommandError, match="not a demo workspace"):
        _build("--reset")
    assert User.objects.filter(username="acme_admin").exists()


def test_refuses_a_non_empty_demo_workspace_without_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    _company_workspace()
    Workspace.objects.filter(pk=1).update(demo=True)
    set_setting("demo_mode", "true")
    with pytest.raises(CommandError, match="already has data"):
        _build()
    assert Robot.objects.count() == 0


def test_refuses_an_empty_database_in_production_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    set_setting("demo_mode", "false")
    with pytest.raises(CommandError, match="production mode"):
        _build()
    assert User.objects.count() == 0 and Robot.objects.count() == 0


def test_refuses_without_the_login_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(demo.PASSWORD_ENV, raising=False)
    with pytest.raises(CommandError, match=demo.PASSWORD_ENV):
        _build()
    assert User.objects.count() == 0


# ------------------------------------------------------------------ badge
def test_badge_follows_the_workspace_demo_flag() -> None:
    ops = make_user("badge_ops", "ops_lead")
    c = Client()
    c.force_login(ops)
    Workspace.ensure()
    Workspace.objects.filter(pk=1).update(demo=True)  # demo flag only, no demo_mode setting
    for url in ("/fleet/", "/ops/", "/sites/"):
        html = c.get(url).content.decode()
        assert html.count("Demo workspace") == 1, url
        assert 'data-demo-badge="1"' in html
    Workspace.objects.filter(pk=1).update(demo=False)
    set_setting("demo_mode", "false")
    for url in ("/fleet/", "/ops/", "/sites/"):
        assert "Demo workspace" not in c.get(url).content.decode(), url


# ------------------------------------------------------------------ deployment wiring
def test_start_script_builds_the_demo_only_with_the_flag_and_keeps_the_old_seed_otherwise() -> None:
    start = (Path(__file__).resolve().parents[1] / "deploy" / "start.sh").read_text()
    assert 'if [ "${DEMO_WORKSPACE:-}" = "1" ]; then' in start
    assert "python manage.py build_demo_workspace" in start and "--reset" not in start
    assert "for c in seed_dev seed_fleet seed_planning seed_guardrails" in start  # unchanged without the flag
    refresh = (Path(__file__).resolve().parents[1] / "deploy" / "refresh_demo.sh").read_text()
    assert (
        'if [ "${DEMO_WORKSPACE:-}" != "1" ]; then' in refresh and "build_demo_workspace --reset" in refresh
    )
