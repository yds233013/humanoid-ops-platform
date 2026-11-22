"""v2 01D: the demo workspace (docs/10_v2_phase01_contract.md section 5 "01D", sections 3, 4 and 12).

One expensive test builds the demo from an empty database, checks it, rebuilds it with --reset and checks that the
rebuild is identical (deterministic for the date). The other tests are cheap refusal and badge checks.
"""

import io
import os
import re
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client
from django.utils import timezone

from accounts.models import RoleGrant
from execution.models import (
    CustodyEvent,
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
    Inspection,
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
from readiness.models import (
    DepartureAuthorization,
    DepartureEvent,
    ReadinessEvidence,
    ReadinessPolicyApproval,
)
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
    assert len(past) >= 15 and {v.status for v in past} == {"confirmed"}

    # ---- ruling B: no fabricated execution history. The build creates no execution record at all ...
    assert ExecutionRun.objects.count() == 0 and TripClosure.objects.count() == 0
    assert DepartureAuthorization.objects.count() == 0 and DepartureEvent.objects.count() == 0
    assert CustodyEvent.objects.count() == 0 and ReadinessEvidence.objects.count() == 0
    # ... and no inspection tied to a visit; the only inspections are today's robot inspections (periodic
    # eligibility needs them) and the one failed inspection that holds a robot, all stamped now.
    assert not Inspection.objects.exclude(visit_ref="").exists()
    assert {_local_day(i.submitted_at) for i in Inspection.objects.all()} <= {
        frozen_today,
        demo.workspace_today(),
    }
    assert Inspection.objects.count() == 49 + 1  # 50 robots minus the never-inspected one, plus the failure
    # past visits are shown as historical scheduled visits, never as completed or back
    ops = User.objects.get(username="dana.ortiz")
    c = Client()
    c.force_login(ops)
    old = past[0]
    for url in (
        f"/planning/visits/{old.code}/",
        f"/readiness/visits/{old.code}/",
        f"/planning/sites/{old.site.code}/",
    ):
        page = c.get(url).content.decode()
        assert "Historical scheduled visit" in page, url
        assert not re.search(r"\b(Completed|Back at the hub)\b", _visible_text(page)), url
    week = c.get(
        f"/planning/?week={old.current_revision.on_site_start.astimezone(LA).date()}"
    ).content.decode()
    assert old.code in week and "Historical scheduled visit" in week
    # ruling A: homes read "H-101 · Fremont · CRM reference ..." on v2 screens
    site = old.site
    label = f"{site.code} · {site.zone} · CRM reference {site.crm_ref}"
    assert label in c.get(f"/planning/visits/{old.code}/").content.decode()
    assert label in c.get("/planning/book/").content.decode()

    # ---- realistic exceptions
    active_holds = Hold.objects.filter(released_at__isnull=True)
    assert set(active_holds.values_list("source_type", flat=True)) == {
        "incident",
        "inspection",
        "maintenance",
    }
    assert Incident.objects.count() == 1
    codes = {e.code for e in open_exceptions(timezone.now(), include_readiness=False).items}
    # truthful attention items only (recorded now): a held robot still booked, a robot never inspected on its
    # configuration, an incident without a severity decision; no overdue return (it needed a fabricated run)
    assert {"HELD_ROBOT_ASSIGNED", "PERIODIC_INSPECTION_OVERDUE", "SEVERITY_UNCLASSIFIED"} <= codes, codes
    assert "RETURN_OVERDUE" not in codes
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
    assert ops.check_password(PASSWORD) and RoleGrant.objects.filter(user=ops, role="ops_lead").exists()
    assert PASSWORD not in out
    for username in ("priya.raman", "dana.ortiz", "jordan.lee", "elena.vasquez", "kenji.watanabe"):
        assert username in out
    html = c.get("/ops/").content.decode()
    assert html.count("Demo workspace") == 1
    assert not re.search(r"(?i)synthetic", _visible_text(html))

    # ---- ruling C: demo values and demo approvals are labelled precisely, without SYNTHETIC per row
    lead_c = Client()
    lead_c.force_login(User.objects.get(username="kenji.watanabe"))
    expect = {
        "/fleet/patterns/HX-430/": ["Demo value", "Not approved for production", "Demo approval",
                                    "Demo checklist \u2014 not approved for production", "What this means"],
        "/fleet/hold-policies/": ["Draft \u2014 not approved", "Not approved for production"],
        "/readiness/policies/": ["Demo approval", "Not approved for production"],
        "/execution/policies/": ["Demo approval", "Not approved for production"],
        "/execution/escalation-deadlines/": ["Not approved for production"],
        "/fleet/NB-001/": ["Demo value", "not approved for production"],
        "/fleet/": ["Demo value"],
        "/ops/robots/": ["Demo value"],
    }  # fmt: skip
    for url, needles in expect.items():
        r = lead_c.get(url)
        assert r.status_code == 200, url
        page = r.content.decode()
        for needle in needles:
            assert needle in page, (url, needle)
        assert not re.search(r"(?i)synthetic", _visible_text(page)), url
        if "Demo value" in page:  # never a neutral chip without its explanation
            assert "This interval is fictional demonstration data" in page, url

    # ---- rebuild with --reset: everything destroyed and rebuilt identically for the date
    before = _fingerprint()
    counts = (User.objects.count(), Robot.objects.count(), Site.objects.count(), Visit.objects.count())
    with pytest.raises(CommandError, match="--reset"):
        _build()  # non-empty demo workspace without --reset
    # ruling D: --reset needs the deployment's database fingerprint, even with both demo markers on
    with pytest.raises(CommandError, match="DEMO_DATABASE_FINGERPRINT is not set"):
        _build("--reset")
    os.environ[demo.FINGERPRINT_ENV] = "humanoid_ops_demo_b:1234567890"
    try:
        with pytest.raises(CommandError, match="does not match this database"):
            _build("--reset")
    finally:
        del os.environ[demo.FINGERPRINT_ENV]
    assert _fingerprint() == before and User.objects.count() == counts[0]  # nothing destroyed
    os.environ[demo.FINGERPRINT_ENV] = demo.database_fingerprint()
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
    assert DepartureAuthorization.objects.count() == 0 and ExecutionRun.objects.count() == 0
    del os.environ[demo.FINGERPRINT_ENV]
    # the wipe never restarts the sequences of the tables it keeps (a later migrate must not collide)
    with connection.cursor() as cur:
        cur.execute(
            "SELECT last_value >= (SELECT max(id) FROM django_migrations) FROM django_migrations_id_seq"
        )
        assert cur.fetchone()[0]

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


def test_reset_refuses_a_wrong_fingerprint_even_with_both_demo_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    _company_workspace()
    Workspace.objects.filter(pk=1).update(demo=True)
    set_setting("demo_mode", "true")
    assert demo.is_demo_workspace()
    monkeypatch.setenv(demo.FINGERPRINT_ENV, demo.database_fingerprint() + "0")
    with pytest.raises(CommandError, match="does not match this database"):
        _build("--reset")
    with pytest.raises(demo.DemoRefused, match="does not match"):
        demo.wipe()
    assert User.objects.filter(username="acme_admin").exists()
    out = io.StringIO()
    call_command("build_demo_workspace", "--print-fingerprint", stdout=out)
    assert out.getvalue().strip() == demo.database_fingerprint()
    assert out.getvalue().strip().startswith(connection.settings_dict["NAME"] + ":")


def test_a_second_build_while_one_holds_the_lock_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(demo.PASSWORD_ENV, PASSWORD)
    s = connection.settings_dict
    other = psycopg.connect(
        host=s["HOST"],
        port=s["PORT"],
        dbname=s["NAME"],
        user=s["USER"],
        password=s["PASSWORD"],
        autocommit=True,
    )
    try:
        other.execute("SELECT pg_advisory_lock(%s)", [demo.LOCK_KEY])
        with pytest.raises(CommandError, match="Another demo build or reset is running"):
            _build()
        assert User.objects.count() == 0 and Robot.objects.count() == 0
    finally:
        other.execute("SELECT pg_advisory_unlock(%s)", [demo.LOCK_KEY])
        other.close()


def test_start_script_exits_non_zero_when_the_initial_demo_build_fails(tmp_path: Path) -> None:
    """start.sh with DEMO_WORKSPACE=1: a failing build_demo_workspace stops the start; gunicorn never runs."""
    backend = Path(__file__).resolve().parents[1]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "gunicorn-started"
    (bin_dir / "python").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *build_demo_workspace*) echo 'refused' >&2; exit 1 ;;\n"
        "  *needs_seed.py*) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gunicorn").write_text(f"#!/bin/sh\ntouch {marker}\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "DEMO_WORKSPACE": "1"}
    done = subprocess.run(["sh", "deploy/start.sh"], cwd=backend, env=env, capture_output=True, text=True)
    assert done.returncode != 0, done.stdout + done.stderr
    assert "not starting the app" in done.stderr and not marker.exists()
    # the same script with a successful build starts the app (the stub gunicorn)
    (bin_dir / "python").write_text("#!/bin/sh\nexit 0\n")
    ok = subprocess.run(["sh", "deploy/start.sh"], cwd=backend, env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and marker.exists(), ok.stderr
