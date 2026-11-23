"""The demo workspace (v2 contract section 5, milestone 01D): "Northbay Home Robotics", a FICTIONAL company.

``build_demo_workspace`` (the management command) calls ``check_can_build`` then ``build``. Everything is
written the way the existing seed commands write it: through the guarded service commands (permission check,
audit row, locks, database triggers) where a command exists, and as the schema owner only for the few rows
that have no command (users and role grants as in seed_dev, deployment settings, the workspace demo
flag and the site-check template as in seed_execution). No trigger is disabled while data is written.

What is fictional and what is not:

* The company, the people, the robots, their configuration and every value on them are invented. No value is a
  real robot specification, price or safety approval. Homes are in real Bay Area towns, but the product stores
  no name or street address for a home by design (planning/forms.py: names, addresses and contacts stay in the
  CRM), so a home is a code, its town as the service area and an invented CRM reference.
* Every approval in the workspace is one the existing demo seeds also create (configuration decision,
  capability profile, periodic rule, readiness and incident policy, escalation deadlines, inspector
  qualifications by the safety reviewer, home assessments). Each sits on a ``synthetic_demo`` record or is
  refused in production mode exactly as before: switching the deployment to production shows the same
  production blockers. The hold-release policy stays a DRAFT.
* Past visits (the last 7 days) are booked and confirmed for their planned days and nothing more: the build
  creates no departure authorization, custody transfer, visit inspection, run, close-out or any other
  execution record, because those commands stamp database time and would date a "past" event at the build.
  Screens show such a visit as "Historical scheduled visit", never as completed. The only events stamped
  at build time are current facts: today's periodic robot inspections (planning eligibility needs them), one
  failed inspection that holds a robot, one maintenance hold, one incident reported this morning.
* No visit of today or later is authorized. The visit prepared for journey J4 has everything it needs except
  the checks a crew records on the day; the ops lead's decision is made in the app by a person.

``wipe`` (``--reset``) empties every application table of this database as the schema owner, with the owner's
own user triggers disabled for the TRUNCATE only (the documented owner trust boundary, the same one the test
teardown uses), then restores the rows the migrations insert. It refuses outside a demo workspace.
"""

from __future__ import annotations

import hmac
import os
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django.utils import timezone

from accounts.models import RoleGrant
from execution import escalation_deadlines as ed
from execution import incidents as inc
from execution import policy as ipol
from execution.models import SiteCheckTemplate
from fleet import services as fs
from fleet.models import (
    CapabilityProfile,
    DeploymentSetting,
    PeriodicInspectionRule,
    Robot,
)
from planning import services as ps
from planning.models import ConsentRecord, Site
from platform_core.models import Hub, Workspace
from platform_core.workspace import create_hub, update_workspace
from readiness import evidence as rx

User = get_user_model()

COMPANY = "Northbay Home Robotics"
REGION = "San Francisco Bay Area"
TZ = "America/Los_Angeles"
PASSWORD_ENV = "DEV_SEED_PASSWORD"


class DemoRefused(Exception):  # noqa: N818 - a refusal, shown to the operator
    pass


# ------------------------------------------------------------------ people (all invented)
# (username, role, person code or "", note). The password of every login is DEV_SEED_PASSWORD.
LOGINS: list[tuple[str, str, str, str]] = [
    ("priya.raman", "admin", "", "workspace admin"),
    ("marcus.bell", "founder", "", "founder"),
    ("dana.ortiz", "ops_lead", "P-101", "ops lead (approves departures)"),
    ("kenji.watanabe", "engineering_lead", "P-102", "engineering lead"),
    ("ruth.adeyemi", "engineering_lead", "", "engineering lead (second approver)"),
    ("sam.keller", "engineer", "P-103", "on-call engineer"),
    ("elena.vasquez", "safety_reviewer", "", "safety reviewer"),
    ("lily.tran", "field_operator", "P-110", "hub technician"),
    ("jordan.lee", "field_operator", "P-111", "crew A lead"),
    ("amara.okafor", "field_operator", "P-112", "crew A"),
    ("mateo.silva", "field_operator", "P-121", "crew B lead"),
    ("hannah.kim", "field_operator", "P-122", "crew B"),
    ("tasha.brooks", "field_operator", "P-131", "crew C lead"),
    ("omar.haddad", "field_operator", "P-132", "crew C"),
]
CREWS: list[tuple[str, str, str, str]] = [  # (name, lead person, second person, vehicle)
    ("A", "P-111", "P-112", "VAN-1"),
    ("B", "P-121", "P-122", "VAN-2"),
    ("C", "P-131", "P-132", "VAN-3"),
]
INSPECTORS = ["P-101", "P-110", "P-111", "P-112", "P-121", "P-122", "P-131", "P-132"]
HUB_TECH = "P-110"
ON_CALL, ESCALATION = "P-103", "P-102"

# ------------------------------------------------------------------ homes (real towns, invented references)
# (town, one-way travel minutes from the Oakland hub). Invented estimates.
TOWNS: list[tuple[str, int]] = [
    ("Oakland", 20),
    ("Berkeley", 25),
    ("Alameda", 25),
    ("Emeryville", 15),
    ("Piedmont", 20),
    ("Albany", 30),
    ("El Cerrito", 30),
    ("San Leandro", 25),
    ("Castro Valley", 35),
    ("Hayward", 35),
    ("Richmond", 35),
    ("Orinda", 30),
    ("Lafayette", 35),
    ("Walnut Creek", 40),
    ("San Ramon", 45),
    ("Fremont", 50),
]
HOMES = 40
WITHDRAWN_HOME, RESTRICTED_HOME, CONSENT_GAP_HOME = "H-102", "H-106", "H-110"
SPECIAL_HOMES = (WITHDRAWN_HOME, RESTRICTED_HOME, CONSENT_GAP_HOME)

# ------------------------------------------------------------------ fleet (invented configuration)
PATTERN_HOME, PATTERN_ENG = "HX-430", "HX-440"
ROBOTS = 50
IN_SERVICE = 45  # NB-001 .. NB-045 on HX-430; NB-046..048 HX-440 in validation; NB-049..050 commissioning
INCIDENT_ROBOT, INSPECTION_HOLD_ROBOT, MAINTENANCE_ROBOT = "NB-007", "NB-012", "NB-019"
NEVER_INSPECTED = "NB-048"  # beta robot in validation, no periodic inspection on its configuration yet

CHECKLIST: list[dict[str, Any]] = [
    {"id": "R1", "label": "Body shell, joints and covers undamaged", "robot_condition": True},
    {"id": "R2", "label": "Both hands open and close fully", "robot_condition": True},
    {"id": "R3", "label": "Stop button tested: robot stops and settles", "robot_condition": True},
    {"id": "R4", "label": "Cameras and depth sensors clean", "robot_condition": True},
    {"id": "R5", "label": "Wheels and feet free of debris", "robot_condition": True},
    {"id": "R6", "label": "No warning lights after start-up", "robot_condition": True},
    {"id": "V1", "label": "Transport case and straps packed", "robot_condition": False},
    {"id": "V2", "label": "Household consent confirmed for the visit", "robot_condition": False},
    {"id": "V3", "label": "Home notes and access instructions read", "robot_condition": False},
]
ALL_PASS: dict[str, str] = {i["id"]: "pass" for i in CHECKLIST}
SITE_CHECK_ITEMS = [
    {"id": "c1", "label": "Walkways and the work area are clear"},
    {"id": "c2", "label": "Everyone at home was briefed on the visit"},
    {"id": "c3", "label": "Only people who consented are present"},
]
READINESS_REQUIREMENTS = {
    "authorization_ttl_min": 90,
    "evidence": [
        {"kind": "pre_departure_inspection", "max_age_min": 240},
        {"kind": "equipment_check", "max_age_min": 240},
        {"kind": "battery_check", "max_age_min": 120},
    ],
    "required_equipment": ["restraint_kit", "recovery_kit", "charger"],
    "recovery_plan_required": True,
}
INCIDENT_RULES = {
    "severity_scale": ["S0", "S1", "S2", "S3"],
    "escalation_required_from": "S2",
    "downgrade_roles": ["safety_reviewer"],
}
HOLD_RULES = {  # the same draft rules as seed_fleet (stays a DRAFT: no safety approval is faked)
    "inspection": {
        "slots": [{"slot": "engineering_review", "roles": ["engineer", "engineering_lead"]}],
        "distinct_approvers": True,
        "independent_of_placer": False,
        "requires_newer_pass_inspection": True,
    },
    "incident": {
        "slots": [
            {"slot": "eng_review_1", "roles": ["engineer", "engineering_lead"]},
            {"slot": "eng_review_2", "roles": ["engineer", "engineering_lead"]},
            {"slot": "safety", "roles": ["safety_reviewer"]},
            {"slot": "founder", "roles": ["founder"]},
        ],
        "distinct_approvers": True,
        "independent_of_placer": True,
    },
    **{
        src: {"slots": [{"slot": "engineering", "roles": ["engineering_lead"]}], "distinct_approvers": True}
        for src in ("maintenance", "engineering", "release_withdrawn", "manual")
    },
}
EQUIPMENT = {"items": {"restraint_kit": "present", "recovery_kit": "present", "charger": "present"}}
SETTINGS = {
    "demo_mode": "true",
    "allow_draft_policies": "true",
    "consent_form_version": "CF-2026-1",
    "pre_visit_inspection_min": "30",
    "ops_custody_verify_grace_min": "30",
    "ops_return_grace_min": "60",
    "ops_inspection_grace_min": "120",
    "ops_readiness_lookahead_min": "2880",  # today and tomorrow (provision_settings --with-defaults)
    "ops_closeout_timezone": TZ,
    "ops_periodic_due_lookahead_min": "1440",
}
# Visit slots (local on-site start, minutes on site). Two per crew per day never overlap (crew shift: from the
# inspection 50 min + travel before arrival to the unloading 20 min + travel after leaving).
SLOTS = [(dtime(9, 30), 90), (dtime(14, 0), 90)]
PAST_DAYS, FUTURE_DAYS = 7, 14


# ------------------------------------------------------------------ guards
def _count(sql: str) -> int:
    with connection.cursor() as cur:
        cur.execute(sql)
        return int(cur.fetchone()[0])


def is_empty() -> bool:
    """No user, robot, home, hub or initialized workspace: a freshly migrated database (owner-only commands
    such as provision_settings may have run; they write settings and an audit row, never workspace data)."""
    return (
        not User.objects.exists()
        and not Robot.objects.exists()
        and not Site.objects.exists()
        and not Hub.objects.exists()
        and not Workspace.objects.filter(initialized=True).exists()
    )


def is_demo_workspace() -> bool:
    """Both demo markers: the workspace row's demo flag AND deployment demo_mode = 'true'."""
    return Workspace.objects.filter(pk=1, demo=True).exists() and fs.demo_mode()


def check_can_build(*, reset: bool) -> None:
    """Refuse a production / non-demo database and a non-empty one without --reset (DemoRefused)."""
    if is_empty():
        if DeploymentSetting.objects.filter(key="demo_mode").exclude(value="true").exists():
            raise DemoRefused(
                "This database is set to production mode (demo_mode is not 'true'); nothing done."
            )
        return
    if not is_demo_workspace():
        raise DemoRefused(
            "This database holds a workspace that is not a demo workspace (demo flag off or production "
            "mode). "
            "The demo builder never writes to or resets it; nothing done."
        )
    if not reset:
        raise DemoRefused(
            "This demo workspace already has data. Run with --reset to destroy it and build it again."
        )
    check_fingerprint()


# ------------------------------------------------------------------ reset
KEEP_TABLES = {"django_migrations", "django_content_type", "auth_permission"}
# Rows the migrations insert (fleet/0002, planning/0002, fleet/0008); restored after a wipe.
MIGRATION_ROWS = [
    "INSERT INTO fleet_deploymentsetting (key, value) VALUES ('allow_draft_policies', 'false') "
    "ON CONFLICT (key) DO NOTHING",
    "INSERT INTO fleet_deploymentsetting (key, value) VALUES ('max_robots_per_trip', '1') "
    "ON CONFLICT (key) DO NOTHING",
    "INSERT INTO fleet_holdpolicyregistry (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
]


# ------------------------------------------------------------------ deployment-level guards (01D ruling D)
FINGERPRINT_ENV = "DEMO_DATABASE_FINGERPRINT"
LOCK_KEY = 7_201_018_301  # pg advisory lock key of the demo builder (one build or reset at a time)


def database_fingerprint() -> str:
    """``<database name>:<PostgreSQL system identifier>`` of the connected database. The system identifier is
    unique per cluster (pg_control_system), so a fingerprint names exactly one database on one server."""
    with connection.cursor() as cur:
        cur.execute("SELECT current_database(), (SELECT system_identifier FROM pg_control_system())")
        name, sysid = cur.fetchone()
    return f"{name}:{sysid}"


def check_fingerprint() -> None:
    """--reset needs an independent, deployment-level confirmation that THIS is the demo database: the
    environment's DEMO_DATABASE_FINGERPRINT must equal the connected database's fingerprint, and the
    connection must be to the database the settings name. Refuses even when demo_mode and Workspace.demo
    are both on."""
    expected = os.environ.get(FINGERPRINT_ENV, "").strip()
    if not expected:
        raise DemoRefused(
            f"Reset refused: {FINGERPRINT_ENV} is not set for this deployment. Set it to the value printed "
            "by `manage.py build_demo_workspace --print-fingerprint` on the demo database only."
        )
    actual = database_fingerprint()
    configured = str(connection.settings_dict.get("NAME") or "")
    if actual.split(":", 1)[0] != configured:
        raise DemoRefused("Reset refused: the connection is not to the configured database; nothing done.")
    if not hmac.compare_digest(expected, actual):
        raise DemoRefused(
            f"Reset refused: {FINGERPRINT_ENV} does not match this database ({actual}); nothing done."
        )


@contextmanager
def build_lock() -> Iterator[None]:
    """Session advisory lock (try, never wait): a second build or reset at the same time is refused."""
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        if not cur.fetchone()[0]:
            raise DemoRefused("Another demo build or reset is running on this database; nothing done.")
    try:
        yield
    finally:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])


def wipe() -> None:
    """Destroy every row of every application table and restart every sequence (demo workspace only)."""
    if not is_demo_workspace():
        raise DemoRefused("Reset refused: this is not a demo workspace (demo flag off or production mode).")
    check_fingerprint()
    with connection.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename",
        )
        tables = [t for (t,) in cur.fetchall() if t not in KEEP_TABLES]
    q = connection.ops.quote_name
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        for t in tables:
            cur.execute(f"ALTER TABLE {q(t)} DISABLE TRIGGER USER")
        cur.execute("TRUNCATE " + ", ".join(q(t) for t in tables) + " RESTART IDENTITY CASCADE")
        for t in tables:
            cur.execute(f"ALTER TABLE {q(t)} ENABLE TRIGGER USER")
        for sql in MIGRATION_ROWS:
            cur.execute(sql)
    restart_sequences()
    Workspace.ensure()


# Sequences of application data only: never those of the kept tables (django_migrations, content types,
# permissions), whose rows survive a wipe; restarting them would make the next migrate collide.
APP_SEQUENCES_SQL = """
SELECT s.relname
FROM pg_class s
JOIN pg_namespace n ON n.oid = s.relnamespace AND n.nspname = 'public'
LEFT JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = s.oid AND d.deptype IN ('a', 'i')
LEFT JOIN pg_class t ON t.oid = d.refobjid
WHERE s.relkind = 'S' AND (t.relname IS NULL OR NOT (t.relname = ANY(%s)))
ORDER BY s.relname
"""


def restart_sequences() -> None:
    """Application sequences back to their start, so codes (V-0001, ...) are the same for every build of a
    date. Run only on an empty or just-wiped database (every application table is empty)."""
    with connection.cursor() as cur:
        cur.execute(APP_SEQUENCES_SQL, [sorted(KEEP_TABLES)])
        for (seq,) in cur.fetchall():
            cur.execute(f"ALTER SEQUENCE {connection.ops.quote_name(seq)} RESTART")


# ------------------------------------------------------------------ build
@dataclass
class Summary:
    anchor: date
    timings: dict[str, float] = field(default_factory=dict)
    logins: list[tuple[str, str, str]] = field(default_factory=list)
    j4_visit: str = ""
    attention_robot: str = NEVER_INSPECTED
    incident: str = ""
    notes: list[str] = field(default_factory=list)


def workspace_today() -> date:
    return timezone.now().astimezone(ZoneInfo(TZ)).date()


class _Builder:
    def __init__(self, password: str, anchor: date, log: Callable[[str], None]) -> None:
        self.password = password
        self.anchor = anchor
        self.log = log
        self.rng = random.Random(anchor.toordinal())
        self.u: dict[str, Any] = {}
        self.s = Summary(anchor=anchor)
        self.t0 = time.perf_counter()
        # Robots booked on visits: in service and not one that this build puts on a hold.
        held = (MAINTENANCE_ROBOT, INSPECTION_HOLD_ROBOT, INCIDENT_ROBOT)
        self.pool = [f"NB-{n:03d}" for n in range(1, IN_SERVICE + 1) if f"NB-{n:03d}" not in held]

    def lap(self, name: str) -> None:
        self.s.timings[name] = round(time.perf_counter() - self.t0, 1)

    def at(self, d: date, t: dtime) -> datetime:
        return ps.local_to_utc(datetime.combine(d, t), TZ)

    # .............................................................. company, logins, hub
    def company(self) -> None:
        for key, value in SETTINGS.items():  # owner-only deployment settings, as the seeds write them
            DeploymentSetting.objects.update_or_create(key=key, defaults={"value": value})
        encoded = make_password(self.password)  # hashed once; every demo login shares the env password
        for username, role, _code, note in LOGINS:
            first, _, last = username.partition(".")  # invented people: "jordan.lee" -> Jordan Lee
            user = User.objects.create(
                username=username,
                password=encoded,
                first_name=first.capitalize(),
                last_name=last.capitalize(),
            )
            RoleGrant.objects.create(user=user, role=role)
            self.u[username] = user
            self.s.logins.append((username, role, note))
        Workspace.ensure()
        update_workspace(self.u["priya.raman"], name=COMPANY, region=REGION, time_zone=TZ)
        Workspace.objects.filter(pk=1).update(demo=True, initialized=True, initialized_at=timezone.now())
        create_hub(self.u["priya.raman"], code="OAK", name="Oakland hub", area="West Oakland", time_zone=TZ)
        self.lap("company, logins, hub")

    # .............................................................. configurations, policies
    def configurations(self) -> None:
        lead, lead2, eng = self.u["kenji.watanabe"], self.u["ruth.adeyemi"], self.u["sam.keller"]
        safety, founder, ops = self.u["elena.vasquez"], self.u["marcus.bell"], self.u["dana.ortiz"]
        fs.create_hold_policy(lead, rules=HOLD_RULES)  # stays a DRAFT on purpose
        fs.create_pattern(lead, code=PATTERN_HOME, hardware_rev="H2", release="4.3.0", firmware_set="FW-21")
        fs.create_pattern(
            lead, code=PATTERN_ENG, hardware_rev="H2", release="4.4.0-beta", firmware_set="FW-22"
        )
        fs.decide_pattern(
            lead, pattern_code=PATTERN_HOME, status="approved_home", reason="Release 4.3 for homes"
        )
        fs.decide_pattern(lead, pattern_code=PATTERN_ENG, status="engineering_only", reason="Beta build")
        fs.publish_capability_profile(
            eng,
            pattern_code=PATTERN_HOME,
            stop_behaviour="Stops all motion, lowers both arms and sits down",
            lift_crew=2,
            transport_posture="Seated in its transport case",
            max_runtime_min=120,
            min_departure_soc_pct=80,
            charge_turnaround_min=180,
            load_unload_min=20,
        )
        prof = CapabilityProfile.objects.get(pattern__code=PATTERN_HOME, version_no=1)
        fs.approve_capability_profile(lead, profile_id=prof.pk, slot="engineering")
        fs.approve_capability_profile(safety, profile_id=prof.pk, slot="safety")
        fs.publish_capability_profile(eng, pattern_code=PATTERN_ENG)  # everything unknown: not for homes
        for code in (PATTERN_HOME, PATTERN_ENG):
            fs.create_inspection_template(
                eng, pattern_code=code, version="T1", items=CHECKLIST, source="synthetic_demo"
            )
            fs.create_periodic_rule(
                eng, pattern_code=code, interval_min=4320, source="synthetic_demo", notes="Every three days"
            )
            rule = PeriodicInspectionRule.objects.get(pattern__code=code, version_no=1)
            fs.approve_periodic_rule(lead, rule_id=rule.pk, slot="engineering")
            fs.approve_periodic_rule(safety, rule_id=rule.pk, slot="safety")
        version = int(
            rx.create_readiness_policy(
                lead, requirements=READINESS_REQUIREMENTS, source="synthetic_demo", notes="Departure checks"
            )["entity_id"]
        )
        rx.approve_readiness_policy(lead2, version=version, slot="engineering")
        rx.approve_readiness_policy(safety, version=version, slot="safety")
        iv = int(
            ipol.create_incident_policy(
                lead, rules=INCIDENT_RULES, source="synthetic_demo", notes="Severity"
            )["entity_id"]
        )
        ipol.approve_incident_policy(lead2, version=iv, slot="engineering")
        ipol.approve_incident_policy(safety, version=iv, slot="safety")
        levels = [*INCIDENT_RULES["severity_scale"], "unclassified"]
        ds = ed.create_deadline_set(
            ops,
            incident_policy_version=iv,
            deadlines={lvl: [{"to_role": "safety_reviewer", "within_min": 600}] for lvl in levels},
            source="synthetic_demo",
            notes="Escalate to the safety reviewer within ten hours",
        )
        set_id = int(ds["entity_id"])
        ed.approve_deadline_set(safety, set_id=set_id, slot="safety")
        ed.approve_deadline_set(founder, set_id=set_id, slot="founder")
        # No command creates site-check templates (engineering-supplied content); the owner records one, as
        # seed_execution does.
        SiteCheckTemplate.objects.create(
            version="SC-1", items=SITE_CHECK_ITEMS, source="synthetic_demo", recorded_by=ops
        )
        self.lap("configurations and policies")

    # .............................................................. people, vehicles
    def people(self) -> None:
        ops, safety = self.u["dana.ortiz"], self.u["elena.vasquez"]
        start = self.at(self.anchor - timedelta(days=120), dtime(0, 0))
        end = self.at(self.anchor + timedelta(days=240), dtime(0, 0))
        for username, _role, code, _note in LOGINS:
            if code:
                ps.register_person(ops, username=username, code=code)
        for _name, lead, second, _van in CREWS:
            for kind in ("field_lead", "robot_handler"):
                ps.grant_qualification(ops, person_code=lead, kind=kind, valid_from=start, valid_to=end)
            for kind in ("field_operator", "robot_handler"):
                ps.grant_qualification(ops, person_code=second, kind=kind, valid_from=start, valid_to=end)
        for code in INSPECTORS:
            ps.grant_qualification(
                safety,
                person_code=code,
                kind="readiness_inspector",
                valid_from=None,
                valid_to=end,
                basis_ref=f"TRN-2026-{code[2:]}",
            )
        for _name, _lead, _second, van in CREWS:
            ps.register_vehicle(ops, code=van, robot_capacity=1, restraint_kit="Seat rail and strap kit")
        ps.register_vehicle(ops, code="VAN-4")  # spare van: capacity and restraint not recorded yet
        self.lap("people and vehicles")

    # .............................................................. robots
    def robots(self) -> None:
        ops, lead, eng = self.u["dana.ortiz"], self.u["kenji.watanabe"], self.u["sam.keller"]
        for n in range(1, ROBOTS + 1):
            code = f"NB-{n:03d}"
            pattern = PATTERN_ENG if IN_SERVICE < n <= IN_SERVICE + 3 else PATTERN_HOME
            fs.register_robot(ops, code=code, serial=f"NBH2-{24 + n // 30}-{1000 + n * 7}")
            fs.record_manifest(
                eng,
                robot_code=code,
                pattern_code=pattern,
                autonomy_version="nav-7.2" if pattern == PATTERN_HOME else "nav-7.3b",
                safety_config_hash=f"{n:02x}" * 32,
                calibration_ref=f"CAL-{n:03d}",
            )
            if n <= IN_SERVICE + 3:
                fs.transition_lifecycle(
                    lead, robot_code=code, to="validation", reason="Commissioning finished"
                )
            if n <= IN_SERVICE:
                fs.transition_lifecycle(lead, robot_code=code, to="in_service", reason="Validation passed")
                fs.grant_base_authorization(lead, robot_code=code)
            # Every robot with a configuration gets its periodic inspection today (two qualified inspectors
            # sign; planning eligibility needs it), except NEVER_INSPECTED: the beta robot nobody inspected
            # on its configuration yet, which Today shows as overdue for its regular inspection (true now).
            # NB-046..048 stay in validation (beta build), NB-049..050 in commissioning.
            if code != NEVER_INSPECTED:
                self.inspect(code)
        self.lap("robots")

    def inspect(self, robot: str, answers: dict[str, str] | None = None) -> str:
        """A robot inspection by the hub technician and the ops lead, both qualified inspectors."""
        return str(
            fs.submit_inspection(
                self.u["dana.ortiz"],
                robot_code=robot,
                template_version="T1",
                answers=answers or dict(ALL_PASS),
                performer=self.username(HUB_TECH),
                verifier="dana.ortiz",
            )["entity_id"]
        )

    @staticmethod
    def username(person_code: str) -> str:
        return next(u for u, _r, c, _n in LOGINS if c == person_code)

    # .............................................................. homes
    def homes(self) -> list[tuple[str, int]]:
        ops, field_user = self.u["dana.ortiz"], self.u["lily.tran"]
        until = self.anchor + timedelta(days=180)
        homes: list[tuple[str, int]] = []
        for i in range(HOMES):
            town, travel = TOWNS[i % len(TOWNS)]
            code = f"H-{101 + i}"
            adults = 1 + (i % 3 != 0) + (i % 7 == 0)  # 1, 2 or 3 adults
            restricted = i == 5
            ps.create_site(
                ops, code=code, zone=town, tz=TZ, crm_ref=f"CRM-{48210 + i * 13}", expected_adults=adults
            )
            a = ps.record_assessment(
                field_user,
                site_code=code,
                decision="eligible_restricted" if restricted else "eligible",
                valid_until=until,
                restrictions="Ground floor only; stairs are steep" if restricted else "",
            )
            ps.approve_assessment(ops, assessment_id=int(a["entity_id"]))
            consents = adults - 1 if i == 9 else adults  # H-110: one adult has not consented yet
            for n in range(1, consents + 1):
                ps.record_consent(
                    field_user,
                    site_code=code,
                    participant_ref=f"A{n}",
                    form_version=SETTINGS["consent_form_version"],
                    scope=["presence", "recording"],
                    evidence_ref=f"SIGN-{code}-{n}",
                )
            homes.append((code, travel))
        self.lap("homes, assessments, consent")
        return homes

    # .............................................................. visits
    def schedule(self, homes: list[tuple[str, int]]) -> list[dict[str, Any]]:
        """Deterministic plan for the date: (day offset, crew, slot, home, robot); no visits on Sundays."""
        pool = self.pool
        normal_homes = [h for h in homes if h[0] not in SPECIAL_HOMES]
        rows: list[dict[str, Any]] = []
        for offset in range(-PAST_DAYS, FUTURE_DAYS + 1):
            day = self.anchor + timedelta(days=offset)
            if day.weekday() == 6 and offset not in (0, 1):  # Sundays off, except today and tomorrow
                continue
            day_robots = self.rng.sample(pool, len(CREWS) * len(SLOTS))
            k = 0
            for crew in CREWS:
                for slot_no, (start, minutes) in enumerate(SLOTS):
                    robot = day_robots[k]
                    k += 1
                    keep = 0.8 if offset < 0 else (0.85 if offset <= 3 else 0.6)
                    if day.weekday() >= 5:
                        keep = 0.4  # weekends are lighter
                    if offset in (0, 1) or self.rng.random() < keep:
                        home, travel = self.rng.choice(normal_homes)
                        rows.append(
                            {
                                "offset": offset,
                                "day": day,
                                "crew": crew,
                                "slot": slot_no,
                                "start": start,
                                "minutes": minutes,
                                "home": home,
                                "travel": travel,
                                "robot": robot,
                            }
                        )
        return rows

    def book(self, row: dict[str, Any], *, confirm: bool = True, robot: str | None = None) -> str:
        ops = self.u["dana.ortiz"]
        start = self.at(row["day"], row["start"])
        code = str(
            ps.request_visit(
                ops,
                site_code=row["home"],
                on_site_start=start,
                on_site_end=start + timedelta(minutes=row["minutes"]),
                travel_out_min=row["travel"],
                travel_back_min=row["travel"],
                tasks=["tidy", "laundry"],
                required_scopes=["presence"],
            )["entity_id"]
        )
        _name, lead, second, van = row["crew"]
        out = ps.plan_visit(
            ops,
            visit_code=code,
            robot_code=robot or row["robot"],
            lead_code=lead,
            second_code=second,
            vehicle_code=van,
        )
        if confirm and out["feasibility"] == "feasible":
            ps.confirm_visit(ops, visit_code=code)
        return code

    def recovery_plan(self, visit: str) -> None:
        """A recovery plan is part of the preparation (who is on call); it is not a check of the day."""
        rx.record_recovery_plan(
            self.u["dana.ortiz"],
            visit_code=visit,
            on_call_code=ON_CALL,
            escalation_code=ESCALATION,
            escalation_path="On-call engineer, then the engineering lead, then the ops lead",
            required_equipment=["recovery_kit"],
        )

    def visits(self, homes: list[tuple[str, int]]) -> None:
        rows = self.schedule(homes)
        past = [r for r in rows if r["offset"] < 0]
        future = [r for r in rows if r["offset"] >= 0]
        # Past visits stay what the records can truthfully say: booked and confirmed for their planned days.
        # Nothing is departed, inspected, handed over or closed for them (every execution command stamps
        # database time, so any such record would be dated at the build). Screens call them "Historical
        # scheduled visit".
        for row in past:
            self.book(row)
        self.lap(f"past visits ({len(past)}, booked for their planned days; no execution records)")
        booked: list[tuple[dict[str, Any], str]] = []
        for row in future:
            booked.append((row, self.book(row)))
        tomorrow = [(r, c) for r, c in booked if r["offset"] == 1]
        # J4: crew A's first visit tomorrow has a recovery plan; the day's checks are what is left to do.
        _j4_row, j4 = next((r, c) for r, c in tomorrow if r["crew"][0] == "A" and r["slot"] == 0)
        self.recovery_plan(j4)
        self.s.j4_visit = j4
        self.lap(f"upcoming visits ({len(future)})")
        self.exceptions(homes, booked)

    # .............................................................. exceptions
    def exceptions(self, homes: list[tuple[str, int]], booked: list[tuple[dict[str, Any], str]]) -> None:
        ops, field_user = self.u["dana.ortiz"], self.u["lily.tran"]
        travel = dict(homes)
        crew_c = CREWS[2]
        evening = dtime(18, 30)  # after crew C's afternoon visit is unloaded, whatever the travel time

        def extra(offset: int, home: str, robot: str | None = None) -> str:
            day = self.anchor + timedelta(days=offset)
            busy = {r["robot"] for r, _c in booked if r["day"] == day}
            row = {"day": day, "start": evening, "minutes": 60, "home": home, "travel": travel[home],
                   "crew": crew_c, "robot": robot or next(x for x in self.pool if x not in busy)}  # fmt: skip
            return self.book(row)

        # A visit confirmed at a home whose second adult then withdrew consent: blocked.
        extra(1, WITHDRAWN_HOME)
        consent = ConsentRecord.objects.get(site__code=WITHDRAWN_HOME, participant_ref="A2")
        ps.withdraw_consent(field_user, consent_id=consent.pk, reason="Second adult asked to withdraw")
        # The robot of a confirmed visit in two days goes on a maintenance hold: blocked (held robot booked).
        extra(2, "H-115", robot=MAINTENANCE_ROBOT)
        fs.place_hold(
            ops,
            scope="robot",
            target=MAINTENANCE_ROBOT,
            source_type="maintenance",
            reason="Left wrist actuator is noisy; replacement booked",
        )
        # A restricted home: planned, waiting for the ops lead to review the restriction.
        extra(3, RESTRICTED_HOME)
        # A home where the adult has not consented yet: cannot be planned until consent is recorded.
        extra(4, CONSENT_GAP_HOME)
        # Requests not planned yet (no robot or crew assigned).
        for k, home in enumerate(("H-120", "H-133", "H-138")):
            start = self.at(self.anchor + timedelta(days=5 + k), dtime(10, 30))
            ps.request_visit(
                ops,
                site_code=home,
                on_site_start=start,
                on_site_end=start + timedelta(minutes=90),
                travel_out_min=travel[home],
                travel_back_min=travel[home],
                tasks=["tidy"],
            )
        # A failed robot-condition item puts its robot on an inspection hold.
        self.inspect(INSPECTION_HOLD_ROBOT, answers={**ALL_PASS, "R2": "fail"})
        # An incident reported this morning: the robot is held until the release approvals are in.
        occurred = timezone.now() - timedelta(hours=3)
        reported = inc.report_incident(
            ops,
            robot_code=INCIDENT_ROBOT,
            event_type="contact",
            description="Arm brushed a door frame while carrying a laundry basket during a practice run at "
            "the hub",
            occurred_at=occurred,
            knowledge_source="witnessed",
        )
        inc.change_incident_status(
            self.u["elena.vasquez"], incident_code=reported["code"], to_status="triaged"
        )
        self.s.incident = str(reported["code"])
        self.lap("exceptions (holds, incident, blocked and waiting visits)")

    def build(self) -> Summary:
        self.company()
        self.configurations()
        self.people()
        self.robots()
        homes = self.homes()
        self.visits(homes)
        return self.s


def build(
    *, password: str, anchor: date | None = None, log: Callable[[str], None] = lambda _m: None
) -> Summary:
    """Build the demo workspace into an empty (or just wiped) database. The caller checks first."""
    if not password:
        raise DemoRefused(f"Set {PASSWORD_ENV} (see .env.example): the demo logins use it as their password.")
    restart_sequences()
    return _Builder(password, anchor or workspace_today(), log).build()
