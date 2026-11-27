"""v2 Phase 06: ONE continuous operational lifecycle in a real browser (EM gate "complete real-browser
operational lifecycle": one continuous scenario, not disconnected screenshots from individually seeded states).

Chromium through Playwright against the live server and the real database. The test starts from a minimal base
(``_base``: two hubs with zones, home zones and the demo travel matrix, people with their role grants, the
approved fleet and readiness configuration, one van and one customer home with its assessment and consent) and
then drives the product's own screens as the people who do each job, in order:

  robot commissioning -> hub readiness -> visit booking -> resource planning -> departure authorization ->
  custody transfer -> site arrival -> session start -> network loss (``context.set_offline``) -> offline
  ordinary step + protective interruption -> reconnection -> reconciliation of the phone's queue -> triage of the
  interruption -> return -> post-visit inspection -> closeout -> inter-hub transfer with receipt.

Every step is a real form or button in the UI (no command API call is needed). At key points the Operations
briefing (/ops/briefing/), Today (/ops/), the dispatch board (/planning/board/), the visit dossier and the audit
log (/api/audit-events) are read in the browser and must reflect the change. The identifiers and the audit row
ids of each step are written to docs/evidence/06_lifecycle.md, and screenshots of key moments to
docs/evidence/06_lifecycle/ (375 px for the field crew, 1024 px for ops).

People (all fictional): Dana Ortiz (regional ops lead), Kenji Watanabe (engineering lead), Sam Keller (engineer),
Ruth Adeyemi (second engineering lead, policy approver and escalation contact), Elena Vasquez (safety reviewer:
triage), Lily Tran (hub technician at Oakland), Nora Castillo (hub technician at South Bay), Jordan Lee (crew lead,
field operator at Oakland), Amara Okafor (crew second, field operator at Oakland). SYNTHETIC data only.

Run: ``uv run pytest tests/browser/test_v2_06_lifecycle.py -m browser``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from playwright.sync_api import BrowserContext, Locator, Page, expect

from accounts.services import grant_role
from execution.models import ExecutionRun, FieldCommand, Interruption, SiteCheckTemplate
from fleet import services as fs
from fleet.management.commands.seed_fleet import DRAFT_POLICY_RULES, TEMPLATE_ITEMS
from fleet.models import (
    CapabilityProfile,
    DeploymentSetting,
    HubTransfer,
    Inspection,
    PeriodicInspectionRule,
    Robot,
)
from hub.models import WorkOrder
from operations.demo_workspace import DEMO_TRAVEL, HUB_HOME_ZONE, HUB_ZONES
from planning import services as ps
from planning import travel, zones
from planning.models import Visit
from platform_core.models import AuditEvent, Workspace, Zone
from platform_core.workspace import create_hub, update_workspace
from readiness import evidence as rx
from readiness.models import DepartureAuthorization, DepartureEvent
from tests.readiness_world import REQUIREMENTS

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

User = get_user_model()
TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)
REPO = Path(__file__).resolve().parents[3]
EVIDENCE = REPO / "docs" / "evidence" / "06_lifecycle.md"
SHOTS = REPO / "docs" / "evidence" / "06_lifecycle"
FIELD = {"width": 375, "height": 812}
OPS = {"width": 1024, "height": 900}
ROBOT, SPARE, HOME, VAN = "R-61", "R-62", "H-601", "VAN-1"
PASS_ALL = {
    **{f"item_{i['id']}": "pass" for i in TEMPLATE_ITEMS},
    "performer": "jordan.lee",
    "verifier": "amara.okafor",
}
SITE_ITEMS = [
    {"id": "c1", "label": "SYNTHETIC: walkway and work area clear"},
    {"id": "c2", "label": "SYNTHETIC: adults briefed on the stop words"},
]
# username -> (first, last, role, hub of a hub-scoped grant, person code)
PEOPLE = {
    "dana.ortiz": ("Dana", "Ortiz", "ops_lead", None, "P-10"),
    "kenji.watanabe": ("Kenji", "Watanabe", "engineering_lead", None, None),
    "ruth.adeyemi": ("Ruth", "Adeyemi", "engineering_lead", None, "P-15"),
    "sam.keller": ("Sam", "Keller", "engineer", None, "P-14"),
    "elena.vasquez": ("Elena", "Vasquez", "safety_reviewer", None, None),
    "lily.tran": ("Lily", "Tran", "hub_technician", "OAK", "P-13"),
    "nora.castillo": ("Nora", "Castillo", "hub_technician", "SJC", "P-20"),
    "jordan.lee": ("Jordan", "Lee", "field_operator", "OAK", "P-11"),
    "amara.okafor": ("Amara", "Okafor", "field_operator", "OAK", "P-12"),
}


# ================================================================== the minimal base (no robot, no visit)
def _base() -> SimpleNamespace:
    """Hubs, zones, travel matrix, users with roles, the approved configuration, a van and one customer home.
    Built through the real commands (as the demo builder does); nothing about a robot or a visit exists yet."""
    w = SimpleNamespace(u={})
    admin = User.objects.create_user(username="ada.admin", first_name="Ada", last_name="Admin")
    Workspace.ensure()
    with connection.cursor() as cur:
        cur.execute("SELECT accounts_bootstrap_admin(%s, %s)", [admin.pk, "06-lifecycle"])
    update_workspace(admin, name="Northbay Home Robotics (fictional)", region="Bay Area", time_zone=TZ)
    Workspace.objects.filter(pk=1).update(initialized=True)
    create_hub(admin, code="OAK", name="Oakland hub", area="West Oakland", time_zone=TZ)
    create_hub(admin, code="SJC", name="South Bay hub", area="San Jose", time_zone=TZ)
    for key, value in {
        "demo_mode": "true",
        "allow_draft_policies": "true",
        "consent_form_version": "CF-2026-1",
        "pre_visit_inspection_min": "30",
        "ops_custody_verify_grace_min": "30",
        "ops_return_grace_min": "60",
        "ops_inspection_grace_min": "120",
        "ops_readiness_lookahead_min": "2880",
        "ops_closeout_timezone": TZ,
        "ops_periodic_due_lookahead_min": "1440",
        "ops_soc_fresh_min": "1440",
        "field_clock_skew_min": "2",
    }.items():
        DeploymentSetting.objects.update_or_create(key=key, defaults={"value": value})
    for username, (first, last, role, hub, _code) in PEOPLE.items():
        w.u[username] = User.objects.create_user(username=username, first_name=first, last_name=last)
        grant_role(admin, user_id=w.u[username].pk, role=role, hub_code=hub)
    u = w.u
    ops, lead, lead2, eng, safety = (
        u["dana.ortiz"],
        u["kenji.watanabe"],
        u["ruth.adeyemi"],
        u["sam.keller"],
        u["elena.vasquez"],
    )
    # approved configuration (patterns, profile, checklist, periodic rule, readiness policy, site checklist)
    fs.create_hold_policy(lead, rules=DRAFT_POLICY_RULES)
    fs.create_pattern(lead, code="P-A", hardware_rev="SYN-H2", release="4.3.0", firmware_set="FW-21")
    fs.decide_pattern(lead, pattern_code="P-A", status="approved_home", reason="SYN: release for homes")
    fs.publish_capability_profile(
        eng, pattern_code="P-A", stop_behaviour="SYNTHETIC: stops and sits down", lift_crew=2,
        max_runtime_min=120, min_departure_soc_pct=60, charge_turnaround_min=180, load_unload_min=20,
    )  # fmt: skip
    prof = CapabilityProfile.objects.get(pattern__code="P-A", version_no=1)
    fs.approve_capability_profile(lead, profile_id=prof.pk, slot="engineering")
    fs.approve_capability_profile(safety, profile_id=prof.pk, slot="safety")
    fs.create_inspection_template(
        eng, pattern_code="P-A", version="T1", items=TEMPLATE_ITEMS, source="synthetic_demo"
    )
    fs.create_periodic_rule(eng, pattern_code="P-A", interval_min=129_600, source="synthetic_demo")
    rule = PeriodicInspectionRule.objects.get(pattern__code="P-A", version_no=1)
    fs.approve_periodic_rule(lead, rule_id=rule.pk, slot="engineering")
    fs.approve_periodic_rule(safety, rule_id=rule.pk, slot="safety")
    version = int(
        rx.create_readiness_policy(lead, requirements=REQUIREMENTS, source="synthetic_demo")["entity_id"]
    )
    rx.approve_readiness_policy(lead2, version=version, slot="engineering")
    rx.approve_readiness_policy(safety, version=version, slot="safety")
    SiteCheckTemplate.objects.create(  # no command records site checklists (engineering content), as the demo
        version="SC-1", items=SITE_ITEMS, source="synthetic_demo", recorded_by=ops
    )
    # people, qualifications, the van
    lo, hi = datetime.now(LA) - timedelta(days=30), datetime.now(LA) + timedelta(days=200)
    for username, (_f, _l, _role, hub, code) in PEOPLE.items():
        if code:
            ps.register_person(ops, username=username, code=code, hub_code=hub or "OAK")
    for code, kinds in (
        ("P-11", ["field_lead", "robot_handler"]),
        ("P-12", ["field_operator", "robot_handler"]),
    ):
        for kind in kinds:
            ps.grant_qualification(ops, person_code=code, kind=kind, valid_from=lo, valid_to=hi)
        ps.grant_qualification(
            safety, person_code=code, kind="readiness_inspector", valid_from=None, valid_to=hi,
            basis_ref=f"TRN-SYN-{code}",
        )  # fmt: skip
    ps.register_vehicle(ops, code=VAN, robot_capacity=1, restraint_kit="SYN seat rail kit", hub_code="OAK")
    # zones, the hubs' own zones and the demo travel matrix (source 'demo')
    for hub in ("OAK", "SJC"):
        for code, name in HUB_ZONES[hub]:
            zones.create_zone(ops, hub_code=hub, code=code, name=name)
        zones.set_hub_home_zone(ops, hub_code=hub, zone_code=HUB_HOME_ZONE[hub])
        ids = dict(Zone.objects.filter(hub__code=hub).values_list("code", "pk"))
        for (a, b), minutes in DEMO_TRAVEL[hub].items():
            travel.set_travel_time(
                ops, from_zone_id=ids[a], to_zone_id=ids[b], minutes=minutes, source="demo"
            )
    # the customer and their home (zone Lake Merritt), assessed, approved, two adults consented
    ps.create_site(ops, code=HOME, zone="Piedmont", tz=TZ, crm_ref="CRM-SYN-601", expected_adults=2)
    zones.set_site_zone(ops, site_code=HOME, hub_code="OAK", zone_code="LM", reason="SYN: the home's area")
    a = ps.record_assessment(
        u["jordan.lee"], site_code=HOME, decision="eligible", valid_until=date.today() + timedelta(days=120)
    )
    ps.approve_assessment(ops, assessment_id=int(a["entity_id"]))
    for n in (1, 2):
        ps.record_consent(
            u["amara.okafor"], site_code=HOME, participant_ref=f"A{n}", form_version="CF-2026-1",
            scope=["presence", "recording"], evidence_ref=f"SIGN-SYN-601-{n}",
        )  # fmt: skip
    return w


# ================================================================== trace (the evidence table)
@dataclass
class Step:
    n: int
    what: str
    who: str
    how: str
    ids: str
    audit: list[tuple[int, str]]
    checks: list[str] = field(default_factory=list)
    shots: list[str] = field(default_factory=list)


class Trace:
    """Each step's identifiers, the committed audit rows written since the previous step, and what the other
    screens showed afterwards. Written to docs/evidence/06_lifecycle.md at the end."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.mark = self.last()

    @staticmethod
    def last() -> int:
        return int(AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0)

    def step(self, what: str, who: str, how: str, ids: str, *, empty: bool = False) -> Step:
        rows = list(
            AuditEvent.objects.filter(id__gt=self.mark, outcome="committed")
            .order_by("id")
            .values_list("id", "command")
        )
        if empty:
            assert not rows, f"step '{what}' must write nothing on the server: {rows}"
        else:
            assert rows, f"step '{what}' wrote no audit row"
            self.mark = rows[-1][0]
        s = Step(len(self.steps) + 1, what, who, how, ids, rows)
        self.steps.append(s)
        return s

    def check(self, text: str) -> None:
        self.steps[-1].checks.append(text.replace("|", "/"))

    def write(self, header: dict[str, str]) -> None:
        lines = [
            "# Phase 06: one continuous real-browser operational lifecycle",
            "",
            "Generated by `backend/tests/browser/test_v2_06_lifecycle.py` (Chromium via Playwright against the live",
            "server and a real PostgreSQL 16). One scenario, from a minimal base to an inter-hub transfer, driven",
            "through the product's own screens by the people who do each job, each in their own browser. Every",
            '"Reflected on" line was asserted by the test in the browser; the run fails if any of them is false.',
            "All data is fictional (SYNTHETIC). Usability is UNVERIFIED: this run was made by the development team,",
            "not by independent users. No passwords are used or stored: each browser gets a test session cookie.",
            "",
            "## Run",
            "",
            *[f"- {k}: {v}" for k, v in header.items()],
            "",
            "## Base (before step 1)",
            "",
            "Built through the real commands, with no robot and no visit: hubs `OAK` (Oakland) and `SJC` (South",
            "Bay) with their zones, their own zones and the demo travel matrix; ten people with their role grants",
            "(hub technicians and field operators granted at one hub, the others regional) and the crew's",
            "qualifications; the approved configuration P-A (capability profile, checklist T1, periodic rule,",
            "readiness policy, site checklist SC-1); van `VAN-1` at Oakland; customer home `H-601` in zone Lake",
            "Merritt (LM), assessed eligible, approved, two adults consented.",
            "",
            "## Steps",
            "",
            "| # | Step | Who (role, width) | How | Identifiers | Audit rows (id `command`) | Reflected on |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in self.steps:
            audit = "<br>".join(f"{i} `{c}`" for i, c in s.audit) or "none (nothing reached the server)"
            checks = "<br>".join(s.checks + [f"Screenshot `{x}`" for x in s.shots])
            lines.append(f"| {s.n} | {s.what} | {s.who} | {s.how} | {s.ids} | {audit} | {checks} |")
        lines += [
            "",
            "## Notes",
            "",
            "- **UI only.** Every step used a screen's own form or button; the test called no command API. Steps",
            '  marked "offline queue" were saved on the phone (IndexedDB) by the run page and sent by its own sync',
            "  (`/api/field/sync`), as on a real phone; online-only steps (class A) were normal form posts.",
            "- **Audit rows** are the committed rows of the append-only `platform_core_auditevent` written between",
            "  the previous step and this one. At the end the audit log was read in the browser as the ops lead",
            "  (`/api/audit-events`) and contained every one of these ids with the same command; the visit dossier",
            "  rebuilt the visit from plan to closed trip.",
            "- **Reconciliation** has two meanings here: step 10 is the phone's queue reconciled with the server",
            "  (each saved step confirmed once, in order, protective first); step 14 is the return reconciliation",
            "  of the trip before it is closed.",
            f"- **Transfer.** Robot {ROBOT} itself cannot be sent right after its visit: its booking (the visit plus",
            "  the charge turnaround) has not ended, and the transfer is refused with that reason (step 15). The",
            f"  spare robot {SPARE}, commissioned and inspected in steps 1 and 2, is sent and received instead.",
            "- **Defect found by this run** (fixed, with a regression test): with an open interruption on a robot",
            '  out on a visit, the briefing said "Nothing to prioritise" while Today said "A visit session was',
            '  interrupted and is still stopped". See docs/evidence/06_safety_matrix.md.',
            "",
        ]
        EVIDENCE.write_text("\n".join(lines))


# ================================================================== browser helpers
def _ctx(browser: Any, args: dict[str, Any], sign_in: Callable, user: Any, viewport: dict) -> BrowserContext:
    ctx = browser.new_context(**{**args, "viewport": viewport})
    sign_in(ctx, user)
    return ctx


def _form(page: Page, *, action: str | None = None, has: str | None = None) -> Locator:
    if action:
        return page.locator(f"form[action='{action}']").first
    assert has
    return page.locator("main form[method='post']", has=page.locator(f"[name='{has}']")).first


def _fill(form: Locator, values: dict[str, Any]) -> None:
    for name, value in values.items():
        el = form.locator(f"[name='{name}']")
        kind = el.first.evaluate("e => e.tagName.toLowerCase() + ':' + (e.type || '')")
        if kind.startswith("select"):
            el.first.select_option(value)
        elif kind in ("input:checkbox", "input:radio"):
            for v in value if isinstance(value, list) else [value]:
                if v is True:
                    el.first.check()
                else:
                    form.locator(f"[name='{name}'][value='{v}']").check()
        else:
            el.first.fill(str(value))


def _submit(page: Page, form: Locator, values: dict[str, Any], button: str | None = None) -> None:
    _fill(form, values)
    target = (
        form.locator(f"button:has-text('{button}')")
        if button
        else form.locator("button[type=submit], button.primary")
    )
    with page.expect_navigation(wait_until="load"):
        target.first.click()


def _main(page: Page) -> str:
    return page.locator("main").inner_text()


def _ok(page: Page, text: str) -> None:
    expect(page.locator("main")).to_contain_text(text, timeout=15000)


def _shot(page: Page, name: str, step: Step) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    w = page.viewport_size["width"] if page.viewport_size else 0
    fname = f"{step.n:02d}_{name}_{w}.png"
    page.screenshot(path=str(SHOTS / fname), full_page=True)
    step.shots.append(f"06_lifecycle/{fname}")


def _next(page: Page, key: str, timeout_s: int = 60) -> None:
    """Wait until the run page's Next step card shows ``key`` (the page reloads itself after a confirmation)."""
    for i in range(timeout_s * 2):
        if page.locator(f"[data-next-step='{key}']").count():
            return
        page.wait_for_timeout(500)
        if i % 10 == 9:
            page.reload()
    raise AssertionError(
        f"next step never became {key}: {page.locator('[data-next-step]').get_attribute('data-next-step')}"
    )


def _open_run(page: Page, url: str) -> None:
    page.goto(url)
    page.wait_for_selector("[data-field-pill='uptodate']", timeout=30000)
    page.wait_for_function("!!navigator.serviceWorker.controller", timeout=30000)


def _seen(page: Page, url: str, label: str) -> str:
    """Open ``url`` and return the page's main text (also written to $LIFECYCLE_DUMP when set, for review)."""
    page.goto(url)
    text = _main(page)
    dump = os.environ.get("LIFECYCLE_DUMP")
    if dump:
        Path(dump).mkdir(parents=True, exist_ok=True)
        Path(dump, f"seen_{label}.txt").write_text(text)
    return text


def _ledger(run: ExecutionRun) -> list[tuple[str, int | None, str, str]]:
    return list(
        FieldCommand.objects.filter(run=run)
        .order_by("id")
        .values_list("channel", "seq", "command", "outcome")
    )


# ================================================================== the lifecycle
def test_one_continuous_operational_lifecycle(live_server, browser, browser_context_args, sign_in):
    pages: dict[str, Page] = {}
    try:
        _lifecycle(live_server.url, browser, browser_context_args, sign_in, pages)
    except BaseException:
        dump = os.environ.get("LIFECYCLE_DUMP")  # a debugging aid: every open page's HTML and a screenshot
        if dump:
            Path(dump).mkdir(parents=True, exist_ok=True)
            for name, pg in pages.items():
                try:
                    Path(dump, f"{name}.html").write_text(pg.content())
                    pg.screenshot(path=str(Path(dump, f"{name}.png")), full_page=True)
                except Exception:  # noqa: BLE001, S112 - best effort only
                    continue
        raise


def _lifecycle(
    base: str, browser: Any, args: dict[str, Any], sign_in: Callable, pages: dict[str, Page]
) -> None:
    w = _base()
    u = w.u
    trace = Trace()
    now = datetime.now(LA)
    start = (now + timedelta(minutes=150)).replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 5)
    end = start + timedelta(minutes=60)
    day = start.date().isoformat()
    board = f"{base}/planning/board/?day={day}"
    briefing = f"{base}/ops/briefing/?hub=OAK"
    contexts = []

    def person(name: str, username: str, viewport: dict) -> tuple[BrowserContext, Page]:
        ctx = _ctx(browser, args, sign_in, u[username], viewport)
        contexts.append(ctx)
        pages[name] = ctx.new_page()
        return ctx, pages[name]

    _, ops = person("ops", "dana.ortiz", OPS)
    _, eng = person("eng", "kenji.watanabe", OPS)
    _, sam = person("sam", "sam.keller", OPS)
    _, lily = person("lily", "lily.tran", OPS)
    crew_ctx, crew = person("crew", "jordan.lee", FIELD)
    _, safety = person("safety", "elena.vasquez", OPS)
    _, nora = person("nora", "nora.castillo", OPS)

    # ---------------------------------------------------------------- 1. robot commissioning (and the spare)
    for code, n in ((ROBOT, 1), (SPARE, 2)):
        eng.goto(f"{base}/fleet/new/")
        _submit(eng, _form(eng, has="serial"), {"code": code, "serial": f"SYN-H2-060{n}", "hub": "OAK"})
        _ok(eng, f"Robot {code} added.")
        sam.goto(f"{base}/fleet/{code}/manifest/")
        _submit(
            sam,
            _form(sam, has="autonomy_version"),
            {
                "pattern": "P-A",
                "autonomy_version": "SYN-nav-7.2",
                "safety_config_hash": f"{n:02x}" * 32,
                "calibration_ref": f"CAL-SYN-60{n}",
                "provenance": "manual",
            },
        )
        _ok(sam, "recorded as a proposal")
        for to, why in (
            ("validation", "SYN: commissioning finished"),
            ("in_service", "SYN: validation passed"),
        ):
            eng.goto(f"{base}/fleet/{code}/lifecycle/")
            _submit(eng, _form(eng, has="to"), {"to": to, "reason": why, "confirm": True})
            _ok(eng, "moved to")
        eng.goto(f"{base}/fleet/{code}/authorize/")
        _submit(eng, _form(eng, has="confirm"), {"confirm": True})
        _ok(eng, f"Home-use authorization granted for {code}")
    r61 = Robot.objects.select_related("hub").get(code=ROBOT)
    assert r61.lifecycle == "in_service" and r61.hub.code == "OAK"
    s = trace.step(
        f"Robot commissioning: {ROBOT} registered at Oakland, configuration manifest, validation, in service, "
        f"home-use authorization (spare {SPARE} the same way)",
        "Kenji Watanabe (engineering lead, 1024); Sam Keller (engineer, 1024)",
        "UI forms /fleet/new/, /fleet/&lt;code&gt;/manifest/, /lifecycle/ (twice), /authorize/",
        f"robots `{ROBOT}` (serial SYN-H2-0601) and `{SPARE}` (SYN-H2-0602), home hub OAK",
    )
    eng.goto(f"{base}/fleet/{ROBOT}/")
    expect(eng.locator("main")).to_contain_text("in service", ignore_case=True)
    trace.check(f"Robot page /fleet/{ROBOT}/: in service")
    _shot(eng, "robot_commissioned", s)

    # ---------------------------------------------------------------- 2. hub readiness
    lily.goto(f"{base}/hub/")
    _ok(lily, f"Robot {ROBOT} has no periodic inspection on its configuration")  # the hub's work order
    for code in (ROBOT, SPARE):
        crew.goto(f"{base}/readiness/robots/{code}/inspection/")
        _submit(crew, crew.locator("main form[method='post']").first, PASS_ALL)
        _ok(crew, "it passed")
    lily.goto(f"{base}/hub/charging/")
    _submit(lily, _form(lily, action="/hub/charging/soc/"), {"robot": ROBOT, "soc": 92})
    _ok(lily, "Battery level recorded.")
    tasks = list(
        WorkOrder.objects.filter(robot__code__in=[ROBOT, SPARE], source_kind="periodic_inspection")
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert len(tasks) == 2, tasks
    for task in tasks:
        _close_task(lily, base, task)
    assert set(WorkOrder.objects.filter(id__in=tasks).values_list("status", flat=True)) <= {
        "done",
        "cancelled",
    }
    lily.goto(f"{base}/hub/")
    expect(lily.locator("main")).to_contain_text(re.compile(r"\b0\s+All open tasks"))
    periodic = list(Inspection.objects.order_by("id").values_list("code", flat=True))
    s = trace.step(
        "Hub readiness: the hub queue's inspection work orders; periodic inspection of both robots (two "
        f"qualified inspectors sign); a battery level for {ROBOT}; the work orders closed",
        "Jordan Lee (field operator @OAK, 375); Lily Tran (hub technician @OAK, 1024)",
        "UI /hub/, /readiness/robots/&lt;code&gt;/inspection/, /hub/charging/ (Record a battery level), "
        "/hub/tasks/&lt;id&gt;/ (Take this task, Close task)",
        f"inspections {', '.join(periodic)}; battery reading 92 % ({ROBOT})",
    )
    trace.check(
        "Hub /hub/: the two 'has no periodic inspection' work orders showed 'Resolved by the underlying record' "
        "and were closed by the hub technician: '0 All open tasks'"
    )
    _shot(lily, "hub_ready", s)
    text = _seen(ops, briefing, "02_briefing")
    assert "No robot at Oakland hub is unavailable" in text, text
    trace.check(
        f"Briefing: 'No robot at Oakland hub is unavailable'; {ROBOT} has a battery level, {SPARE} not"
    )

    # ---------------------------------------------------------------- 3. visit booking
    ops.goto(f"{base}/planning/book/")
    _submit(
        ops,
        _form(ops, has="travel_out_min"),
        {
            "site": HOME,
            "hub": "OAK",
            "day": day,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "travel_out_min": 20,
            "travel_back_min": 20,
            "tasks": "T-04",
            "scopes": ["presence"],
        },
        button="Request visit",
    )
    _ok(ops, "Visit requested.")
    visit = Visit.objects.get(site__code=HOME).code
    trace.step(
        f"Visit booking at home {HOME}, {day} {start:%H:%M}-{end:%H:%M} ({TZ})",
        "Dana Ortiz (ops lead, 1024)",
        "UI form /planning/book/ (Request visit)",
        f"visit `{visit}` (requested)",
    )
    ops.goto(board)
    row = ops.locator(f"#hub-OAK a.vrow[data-row='{visit}']").first
    expect(row).to_have_attribute("data-status", "blocked")
    trace.check(f"Board /planning/board/?day={day}: {visit} at Oakland, Blocked (no robot, crew or van yet)")

    # ---------------------------------------------------------------- 4. resource planning
    ops.goto(f"{base}/planning/visits/{visit}/?review=1")
    pick = ops.locator(f"form[data-preview-form] input[name='robot'][value='{ROBOT}']")
    if pick.count() and not pick.is_checked():
        pick.check()
        with ops.expect_navigation():
            ops.locator("form[data-preview-form] button:has-text('Check this change')").click()
    expect(ops.locator("main")).to_contain_text("If you save: Planning feasible")
    review_shot = ops.screenshot(full_page=True)
    _submit(ops, ops.locator("form[data-save-form]"), {"reason": "SYN: first plan from Review options"})
    _ok(ops, "Plan saved")
    _submit(ops, _form(ops, action=f"/planning/visits/{visit}/confirm/"), {})
    _ok(ops, "Visit confirmed")
    v = Visit.objects.select_related("current_revision__robot", "zone").get(code=visit)
    rev = v.current_revision
    assert rev.robot.code == ROBOT and v.zone.code == "LM" and v.status == "confirmed"
    s = trace.step(
        f"Resource planning: Review options, save the best option ({ROBOT}, crew P-11 Jordan Lee + P-12 Amara "
        f"Okafor, {VAN}), confirm the visit",
        "Dana Ortiz (ops lead, 1024)",
        f"UI /planning/visits/{visit}/?review=1 (Save plan), Confirm visit",
        f"visit `{visit}` revision id {rev.pk}, zone LM, travel {rev.travel_out_min}/{rev.travel_back_min} min "
        "(matrix DT-LM)",
    )
    _save_shot(review_shot, "plan_review_1024.png", s)
    ops.goto(board)
    row = ops.locator(f"#hub-OAK a.vrow[data-row='{visit}']").first
    status = row.get_attribute("data-status")
    assert status not in ("blocked", "paused"), status
    trace.check(f"Board: {visit} status {status} with {ROBOT}, Jordan Lee and Amara Okafor, {VAN}")
    text = _seen(ops, f"{base}/ops/", "04_today")
    assert visit in text and ROBOT in text
    trace.check(f"Today /ops/: {visit} robot {ROBOT} listed for today")

    # ---------------------------------------------------------------- 5. departure authorization
    ready = f"{base}/readiness/visits/{visit}/"
    crew.goto(f"{ready}inspection/")
    _submit(crew, crew.locator("main form[method='post']").first, PASS_ALL)
    pre = Inspection.objects.filter(robot__code=ROBOT).latest("id").code
    crew.goto(ready)
    _submit(
        crew, crew.locator("#evidence form"), {"inspection_code": pre}, button="Record inspection evidence"
    )
    _submit(
        crew,
        crew.locator("form[data-evidence-form='equipment_check']"),
        {
            "item_restraint_kit": "present",
            "item_recovery_kit": "present",
            "item_charger": "present",
            "result": "pass",
        },
    )
    _submit(crew, crew.locator("form[data-evidence-form='battery_check']"), {"soc_pct": 90, "result": "pass"})
    ops.goto(ready)
    _submit(
        ops,
        _form(ops, action=f"/readiness/visits/{visit}/recovery-plan/"),
        {
            "on_call": "P-14",
            "escalation": "P-15",
            "escalation_path": "SYN: on-call engineer -> engineering lead -> ops lead",
            "required_equipment": "recovery_kit",
        },
    )
    _ok(ops, "Checklist complete")
    _submit(ops, _form(ops, action=f"/readiness/visits/{visit}/authorize/"), {})
    _ok(ops, "Approved to leave by dana.ortiz")
    auth = DepartureAuthorization.objects.get(visit__code=visit)
    s = trace.step(
        "Departure authorization: pre-departure inspection, equipment and battery checks (crew); recovery plan "
        "and Request approval to leave (ops lead)",
        "Jordan Lee (field operator @OAK, 375); Dana Ortiz (ops lead, 1024)",
        f"UI forms /readiness/visits/{visit}/inspection/ and /readiness/visits/{visit}/",
        f"pre-departure inspection {pre}; authorization `{auth.code}`, valid until "
        f"{auth.expires_at.astimezone(LA):%H:%M}",
    )
    _shot(ops, "approved_to_leave", s)
    text = _seen(ops, f"{base}/ops/", "05_today")
    assert "Approved to leave by dana.ortiz" in text and visit in text, text
    trace.check(f"Today: '{visit} ... Approved to leave by dana.ortiz ... valid until ...'")
    text = _seen(ops, briefing, "05_briefing")
    assert "1 other visit today has no open problem" in text and "That is not permission to leave." in text
    trace.check("Briefing: '1 other visit today has no open problem ... That is not permission to leave.'")

    # ---------------------------------------------------------------- 6. custody transfer: departure + verification
    crew.goto(f"{base}/execution/mine/")
    expect(crew.locator("main")).to_contain_text(visit)
    crew.goto(ready)
    _submit(crew, _form(crew, action=f"/readiness/authorizations/{auth.code}/depart/"), {"confirm": True})
    _ok(crew, "Departed")
    run = ExecutionRun.objects.get(visit__code=visit)
    run_url = f"{base}/execution/runs/{run.code}/"
    _open_run(crew, run_url)
    _next(crew, "verify")
    crew.locator("[data-next-step] button:has-text('Record crew verification')").click()
    _next(crew, "arrive_site")
    dep = DepartureEvent.objects.get(visit__code=visit)
    trace.step(
        "Custody transfer: departure recorded (custody #1 hub -> vehicle, administrative), then verified by the "
        "crew second",
        "Jordan Lee (crew lead, 375)",
        "UI: My visits, Record departure (readiness page); run page Next step verification (offline queue)",
        f"departure event id {dep.pk}; run `{run.code}`; ledger {_ledger(run)}",
    )
    text = _seen(ops, briefing, "06_briefing")
    assert "1 robot out on a visit" in text, text
    trace.check(f"Briefing: '1 robot out on a visit' ({ROBOT})")
    ops.goto(board)
    expect(ops.locator(f"#hub-OAK a.vrow[data-row='{visit}']").first).to_contain_text("Departed")
    trace.check(f"Board: {visit} Departed")

    # ---------------------------------------------------------------- 7. site arrival and handover to the site
    crew.locator("[data-next-step] button:has-text('Vehicle arrived at site')").click()
    _next(crew, "to_site")
    _submit(crew, crew.locator("[data-next-step] form[data-custody-form]"), {"to_kind": "site"})
    _next(crew, "site_check")
    s = trace.step(
        "Site arrival, then custody handover vehicle -> site (receiver from the crew)",
        "Jordan Lee (crew lead, 375)",
        "UI run page Next step: arrival (offline queue), handover (online-only form)",
        f"run `{run.code}`",
    )
    _shot(crew, "on_site", s)

    # ---------------------------------------------------------------- 8. site check and session start
    check = crew.locator("[data-next-step] form[data-site-check-form]")
    _fill(check, {"cond_c1": "ok", "cond_c2": "ok", "adults_present": 2})
    check.locator("button.primary").click()
    _next(crew, "start")
    _submit(crew, crew.locator("[data-next-step] form[data-field-form='session']"), {"kind": "start"})
    _next(crew, "stop")
    trace.step(
        "Site check (Go: 2 adults, both items ok, 2 valid consents) and session start",
        "Jordan Lee (crew lead, 375)",
        "UI run page Next step: site check (offline queue), Start the session (online-only form)",
        f"run `{run.code}`",
    )
    expect(crew.locator("[data-field-pill]")).to_have_attribute("data-field-pill", "uptodate")
    trace.check("Phone: connection pill '✓ Up to date'; Next step 'Stop the session when the work is done'")

    # ---------------------------------------------------------------- 9. network loss: offline steps
    crew_ctx.set_offline(True)
    crew.wait_for_selector("[data-field-pill='offline']")
    expect(crew.locator("[data-next-step] .f-needs")).to_have_count(0)  # stop is protective: allowed offline
    session_form = crew.locator("form[data-field-form='session']").first
    _fill(session_form, {"kind": "observation", "note": "SYN: robot folding laundry, household watching"})
    session_form.locator("button.primary").click()
    crew.fill(
        "form[data-field-command='interruption.open'] input[name='reason']",
        "SYN: a child entered the work area",
    )
    crew.click("form[data-field-command='interruption.open'] button")
    banner = crew.locator("[data-field-banner]")
    expect(banner).to_have_attribute("data-field-banner", "saved")
    expect(banner).to_contain_text("Robot must not operate")
    expect(banner).to_contain_text("saved on this phone, not yet confirmed")
    expect(crew.locator("[data-field-row][data-state='local']")).to_have_count(2)
    expect(crew.locator("[data-field-steps]")).to_contain_text("Saved on this phone")
    assert not Interruption.objects.filter(run=run).exists()
    s = trace.step(
        "Network loss (context.set_offline): an observation note (ordinary) and an interruption (protective) "
        "recorded on the phone",
        "Jordan Lee (crew lead, 375)",
        "UI offline: session form (Observation), Open interruption",
        "2 steps saved on the phone (IndexedDB), none on the server",
        empty=True,
    )
    trace.check(
        "Phone: pill '○ Offline'; banner 'Robot must not operate ... saved on this phone, not yet confirmed'"
    )
    trace.check("Server: no interruption, no ledger row, no audit row while offline")
    _shot(crew, "offline_protective_saved", s)

    # ---------------------------------------------------------------- 10. reconnection and queue reconciliation
    bodies: list[dict] = []
    crew.on(
        "request",
        lambda r: (
            bodies.append(json.loads(r.post_data))
            if r.url.endswith("/api/field/sync") and r.post_data
            else None
        ),
    )
    crew_ctx.set_offline(False)
    crew.wait_for_selector("[data-field-banner='accepted']", timeout=45000)
    expect(crew.locator("[data-field-banner]")).to_contain_text("confirmed by the server")
    expect(crew.locator("[data-field-banner]")).to_contain_text("stays stopped")
    queued = [x for x in _ledger(run) if x[2] in ("interruption.open", "session.observation")]
    assert [(x[0], x[2], x[3]) for x in queued] == [
        ("protective", "interruption.open", "confirmed"),
        ("ordinary", "session.observation", "confirmed"),
    ], queued
    sent = next(b for b in bodies if b.get("commands"))
    assert sent["commands"][0]["channel"] == "protective"
    intr = Interruption.objects.get(run=run)
    s = trace.step(
        "Reconnection: the phone's sync sends the protective step first; the server confirms each step once, "
        "in order (queue reconciled); the session is paused by the interruption",
        "Jordan Lee (crew lead, 375)",
        "UI: the run page's own sync on reconnect (POST /api/field/sync)",
        f"interruption `{intr.code}`; ledger {queued}",
    )
    trace.check(
        "Phone: banner 'confirmed by the server ... stays stopped'; first sync batch starts protective"
    )
    crew.reload()
    _next(crew, "resume")
    trace.check("Phone: Next step 'Resume or stop the session'")
    _shot(crew, "reconnected_paused", s)
    text = _seen(ops, f"{base}/ops/", "10_today")
    assert "A visit session was interrupted and is still stopped." in text, text
    trace.check("Today: 'A visit session was interrupted and is still stopped.' (Resolve the interruption)")
    text = _seen(ops, briefing, "10_briefing")
    assert (
        "Unresolved interruption" in text and f"Review the unresolved interruption on robot {ROBOT}." in text
    ), text
    trace.check(
        f"Briefing: {ROBOT} 'Unresolved interruption ... must not operate'; priority 'Review the "
        f"unresolved interruption on robot {ROBOT}.'"
    )
    text = _seen(ops, run_url, "10_run_ops")
    assert intr.code in text and "Open" in text
    trace.check(f"Run page (ops lead): interruption {intr.code} Open")

    # ---------------------------------------------------------------- 11. triage of the interruption
    safety.goto(run_url)
    _submit(
        safety,
        _form(safety, action=f"/execution/runs/{run.code}/interruptions/resolve/"),
        {"note": "SYN: called the crew; the area is clear, the child is with a parent"},
    )
    expect(safety.locator("main")).to_contain_text("Resolved")
    s = trace.step(
        f"Triage: interruption {intr.code} resolved with a note (resolving does not resume the session)",
        "Elena Vasquez (safety reviewer, 1024)",
        f"UI run page /execution/runs/{run.code}/, Interruptions card (Resolve)",
        f"interruption `{intr.code}`",
    )
    _shot(safety, "interruption_resolved", s)
    text = _seen(ops, briefing, "11_briefing")
    assert "Unresolved interruption" not in text and "1 robot out on a visit" in text, text
    trace.check(f"Briefing: no unresolved interruption; {ROBOT} out on a visit again")
    text = _seen(ops, f"{base}/ops/", "11_today")
    assert "A visit session was interrupted and is still stopped." not in text
    trace.check("Today: the interruption card is gone")

    # ---------------------------------------------------------------- 12. return
    crew.reload()
    _next(crew, "resume")
    form = crew.locator("[data-next-step] form[data-field-form='session']")
    _fill(form, {"kind": "stop", "note": "SYN: work stopped after the interruption"})
    form.locator("button.primary").click()
    _next(crew, "to_vehicle")
    form = crew.locator("[data-next-step] form[data-custody-form]")
    _fill(form, {"to_kind": "vehicle"})
    form.locator("button.primary").click()
    _next(crew, "depart_site")
    crew.locator("[data-next-step] button:has-text('Vehicle departed site')").click()
    _next(crew, "arrive_hub")
    crew.locator("[data-next-step] button:has-text('Vehicle arrived at hub')").click()
    _next(crew, "to_hub")
    _submit(crew, crew.locator("[data-next-step] form[data-custody-form]"), {"to_kind": "hub"})
    _next(crew, "inspect")
    trace.step(
        "Return: session stopped, robot back in the vehicle, left the home, back at the hub, robot received at "
        "the hub (custody #4 vehicle -> hub)",
        "Jordan Lee (crew lead, 375)",
        "UI run page Next step: stop, back to vehicle, trip events (offline queue); hub receipt (online form)",
        f"run `{run.code}`; ledger {[x for x in _ledger(run) if x[2] not in ('interruption.open',)][-4:]}",
    )

    # ---------------------------------------------------------------- 13. post-visit inspection
    crew.goto(f"{ready}inspection/")
    _submit(crew, crew.locator("main form[method='post']").first, PASS_ALL)
    _ok(crew, "it passed")
    post = Inspection.objects.filter(robot__code=ROBOT).latest("id").code
    s = trace.step(
        "Post-visit inspection (after the hub receipt)",
        "Jordan Lee (crew lead, 375)",
        f"UI form /readiness/visits/{visit}/inspection/",
        f"inspection {post}",
    )
    _open_run(crew, run_url)
    _next(crew, "reconcile")
    trace.check("Phone: Next step 'Reconcile the return' (the ops lead's step)")
    _shot(crew, "returned_inspected", s)

    # ---------------------------------------------------------------- 14. closeout
    ops.goto(run_url)
    _submit(ops, _form(ops, action=f"/execution/runs/{run.code}/reconcile/"), {"inspection_code": post})
    _submit(ops, _form(ops, action=f"/execution/runs/{run.code}/close/"), {})
    expect(ops.locator("[data-next-step='done']")).to_have_count(1)
    s = trace.step(
        "Closeout: return reconciliation with the post-visit inspection (reconciled), Close trip",
        "Dana Ortiz (ops lead, 1024)",
        f"UI run page /execution/runs/{run.code}/ (Record return reconciliation, Close trip)",
        f"run `{run.code}` closed",
    )
    trace.check("Run page: 'Next step: Nothing left to record'")
    _shot(ops, "trip_closed", s)
    text = _seen(ops, f"{base}/execution/visits/{visit}/dossier/", "14_dossier")
    for needle in (
        "Plan revision r2 (feasible)",
        f"Departure authorization {auth.code}",
        f"Departure recorded on {auth.code} by jordan.lee",
        "Custody #1: hub -> vehicle",
        "Custody #1 verified by P-12",
        "Custody #2: vehicle -> site",
        "Site check: go",
        "Session start",
        f"Interruption {intr.code} (operator)",
        f"Session pause: Paused by interruption {intr.code}",
        "Session observation",
        f"Interruption {intr.code} resolved",
        "Session stop",
        "Custody #4: vehicle -> hub",
        f"Inspection {post}: PASS",
        "Return reconciliation: reconciled",
        "Trip closed after reconciliation",
    ):
        assert needle in text, needle
    assert "+00:00" not in text, "the dossier shows a raw UTC time"
    trace.check(
        "Visit dossier: plan r2, evidence, the authorization (local expiry), departure, custody #1-#4 with "
        f"verification, site check, session, {intr.code} + resolution, return, reconciliation, closed"
    )
    text = _seen(ops, f"{base}/ops/", "14_today")
    assert "No visits today" in text or visit not in text, text
    trace.check(f"Today: {visit} no longer open; fleet 2 available, 0 out")

    # ---------------------------------------------------------------- 15. inter-hub transfer with receipt
    lily.goto(f"{base}/hub/transfers/")
    reason = {"kind": "robot", "to_hub": "SJC", "reason": "SYN: South Bay needs a robot"}
    _submit(lily, _form(lily, action="/hub/transfers/start/"), {**reason, "code": ROBOT})
    refusal = f"Robot {ROBOT} cannot move to hub SJC: it is booked for visit {visit} until"
    _ok(lily, refusal)
    assert not HubTransfer.objects.filter(robot__code=ROBOT).exists()
    lily.goto(f"{base}/hub/transfers/")
    _submit(lily, _form(lily, action="/hub/transfers/start/"), {**reason, "code": SPARE})
    t = HubTransfer.objects.get(robot__code=SPARE, seq=1)
    nora.goto(f"{base}/hub/transfers/")
    with nora.expect_navigation():
        nora.locator(f"button[data-receive='{t.pk}']").click()
    assert Robot.objects.get(code=SPARE).hub.code == "SJC"
    assert list(HubTransfer.objects.filter(transfer=t).order_by("seq").values_list("state", flat=True)) == [
        "in_transit",
        "received",
    ]
    s = trace.step(
        f"Inter-hub transfer: {ROBOT} refused (its booking has not ended); spare {SPARE} sent from Oakland and "
        "received at South Bay",
        "Lily Tran (hub technician @OAK, 1024); Nora Castillo (hub technician @SJC, 1024)",
        "UI /hub/transfers/ (Send; Receive at SJC)",
        f"transfer `{t.code}` (in transit -> received); {SPARE} home hub SJC",
    )
    trace.check(f"Transfers page (Lily): 'Refused (conflict) {refusal} ...'")
    _shot(nora, "transfer_received", s)
    text = _seen(ops, f"{base}/ops/briefing/", "15_briefing")
    sjc = text.split("South Bay hub SJC", 1)[-1]
    assert "1 robot" in sjc and SPARE in sjc, sjc
    trace.check(f"Briefing (all hubs): South Bay hub has 1 robot ({SPARE}, no battery level yet)")

    # ---------------------------------------------------------------- the audit log, read in the browser
    r = ops.request.get(f"{base}/api/audit-events")
    assert r.status == 200
    api = {int(x["id"]): x["command"] for x in r.json()}
    for st in trace.steps:
        for aid, command in st.audit:
            assert api.get(aid) == command, (st.n, aid, command)
    n = sum(len(st.audit) for st in trace.steps)
    trace.check(f"Audit log /api/audit-events (ops lead): all {n} audit rows of steps 1-15, same commands")

    trace.write(
        {
            "Date": datetime.now(LA).strftime("%Y-%m-%d %H:%M %Z"),
            "Code": f"branch v2/06-e2e at {_sha()} plus the files of the commit that adds this evidence",
            "Visit": f"{visit} at {HOME}, {day} {start:%H:%M}-{end:%H:%M} ({TZ})",
            "Browser": f"Chromium {browser.version} (Playwright), field crew at 375 px, everyone else at 1024 px",
        }
    )
    for c in contexts:
        c.close()


def _close_task(page: Page, base: str, task: int) -> None:
    """The hub technician works one task to its end with the task page's own buttons."""
    for _ in range(4):
        page.goto(f"{base}/hub/tasks/{task}/")
        for action in ("cancel", "complete", "start", "take"):
            form = page.locator(f"form[action='/hub/tasks/{task}/{action}/']")
            if form.count():
                _submit(page, form, {})
                break
        else:
            return


def _save_shot(png: bytes, name: str, step: Step) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    fname = f"{step.n:02d}_{name}"
    (SHOTS / fname).write_bytes(png)
    step.shots.append(f"06_lifecycle/{fname}")


def _sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
