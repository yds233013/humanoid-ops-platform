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
  failed inspection that holds a robot, one maintenance hold, one incident reported this morning, and (v2 02C)
  the baseline maintenance records of today: demo maintenance rules labelled "Demo value, not an engineering
  requirement", the hub technician qualified as maintenance technician by the safety reviewer, the maintenance
  gate active, and one in-service robot without a baseline (Unknown, so blocked and never booked).
* v2 02E (Hub day, J8): two short PRACTICE visits at 00:30 local today are run end to end through the real
  readiness and execution commands and closed at build time (crews B and C, home H-104). These are the only
  execution records. Every one of them is stamped at the build (database time), never back-dated, so a run
  reads "departed at <build time>" for a visit planned at 00:30. They exist so that the predictor log has
  departures and the visit-count demo maintenance rules have closed runs: one robot is then "Due soon" and one
  "Overdue" (blocked; its visit tomorrow shows the block). The build also runs the owner-only predictor log
  once (the same function as ``log_predictor_signals``).
* No other visit of today or later is authorized. The visit prepared for journey J4 has everything it needs
  except the checks a crew records on the day; the ops lead's decision is made in the app by a person.

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

from accounts.roles import ADMIN, HUB_SCOPED_ROLES
from accounts.services import grant_role
from execution import escalation_deadlines as ed
from execution import incidents as inc
from execution import policy as ipol
from execution.models import SiteCheckTemplate
from fleet import maintenance as fm
from fleet import services as fs
from fleet.models import (
    CapabilityProfile,
    DeploymentSetting,
    InspectionTemplate,
    PeriodicInspectionRule,
    Robot,
)
from planning import services as ps
from planning.models import ConsentRecord, Site, Visit
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
# v2 02A: extra roles. The hub technician keeps field_operator (she signs inspections and records consent in
# this demo) and gains hub_technician (the hub work queue, contract 11 section 8.6).
EXTRA_ROLES: dict[str, list[str]] = {"lily.tran": ["hub_technician"]}
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
# v2 02C: an in-service robot with no baseline maintenance record: its maintenance status is Unknown, so it is
# blocked (the gate is active in the demo) and never booked.
NO_MAINTENANCE_BASELINE = "NB-044"
# v2 02C demo maintenance rules (synthetic_demo, labelled "Demo value, not an engineering requirement"):
# (requirement code, calendar days, completed visits, active hours, warning %, post-maintenance inspection)
MAINTENANCE_RULES: list[tuple[str, int | None, int | None, int | None, int, bool]] = [
    ("JOINT-SERVICE", 30, 40, 60, 20, False),
    ("HAND-SERVICE", None, 25, None, 20, True),
    # v2 02E: two short visit-count demo rules, so the two practice runs below give one robot "Due soon" and
    # one "Overdue" from real closed runs (counters cannot be back-dated). Demo values like the others.
    ("SENSOR-CLEAN", None, 1, None, 0, False),  # after every closed visit
    ("GRIPPER-PADS", None, 4, None, 75, False),  # due soon from the first closed visit
]
# Periodic inspection interval per configuration (demo values). v2 02E: the beta build is inspected every day
# while it is in validation, so its robots inspected today show "due soon" on the Hub within the look-ahead.
PERIODIC_INTERVALS: dict[str, tuple[int, str]] = {
    PATTERN_HOME: (4320, "Every three days"),
    PATTERN_ENG: (1440, "Every day while the beta build is in validation"),
}
# v2 02E practice runs (EM ruling 2026-11-25, contract 11 section 8.9): two short visits at 00:30 local
# today (crews B and C) at a home of their own whose label says "Fictional practice visit" on every visit
# and run page, run through the real readiness and execution commands and closed at build time. They give
# the predictor log its departures, the visit-count maintenance rules their closed runs, and Hub day its
# due-soon / overdue robots. Every record they create is stamped at the build (database time); nothing is
# back-dated.
PRACTICE_HOME, PRACTICE_START, PRACTICE_MIN = "PRACTICE", dtime(0, 30), 60
PRACTICE_ZONE, PRACTICE_CRM, PRACTICE_TRAVEL = "Fictional practice visit (Emeryville)", "PRACTICE-RUNS", 15
PRACTICE_CREWS = ("B", "C")

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
    # v2 02B: demo value (labelled on Settings > Operating settings). v2 02E: a day, not 4 hours, because the
    # demo is rebuilt at 04:00 and walked during the day: the build's hand readings must still count then.
    "ops_soc_fresh_min": "1440",
    # v2 02C: the maintenance gate is ACTIVE in the demo (the build records the rules, the qualification and
    # the baselines before any visit is planned: the rollout order of docs/runbook_deploy_backup_rollback.md)
    "maintenance_gate_active": "true",
    # v2 02D: demo stock thresholds (fictional; labelled on Settings > Operating settings like every setting)
    "stock_minimums": "RK=2,RC=2,WIPE=10,GLOVE=4",
    "stock_per_visit": "RK=1",
    # v2 03B-3: the next free time search (demo values: a week ahead, visits on site 08:00-20:00; every hub
    # also gets its own operating hours row with the same value)
    "dispatch_lookahead_days": "7",
    "hub_operating_hours": "08:00-20:00",
    # v2 03C-3: the phone clock tolerance of the field sync (contract 14.4 demo value)
    "field_clock_skew_min": "2",
}
# ------------------------------------------------------------------ v2 03B-3: three fictional hubs
# Zones per hub (code, name) and the hub's own zone (where its vans leave from and return to).
HUB_ZONES: dict[str, list[tuple[str, str]]] = {
    "OAK": [("DT", "Downtown"), ("HILLS", "East Bay hills"), ("LM", "Lake Merritt")],
    "SJC": [("WG", "Willow Glen"), ("CAM", "Campbell"), ("SC", "Santa Clara")],
    "SRF": [("SR", "San Rafael"), ("MV", "Mill Valley"), ("NOV", "Novato")],
}
HUB_HOME_ZONE = {"OAK": "DT", "SJC": "WG", "SRF": "SR"}
# The travel matrix, ordered pairs (from, to) -> minutes. DEMO VALUES (source 'demo', labelled so on every
# screen), invented, not measured. Marin's San Rafael -> Novato is deliberately missing: every visit to a
# Novato home is "Travel time unknown" (blocked, never estimated: the fail-closed travel rule).
DEMO_TRAVEL: dict[str, dict[tuple[str, str], int]] = {
    "OAK": {("DT", "DT"): 15, ("DT", "LM"): 20, ("DT", "HILLS"): 35, ("LM", "DT"): 20, ("LM", "LM"): 10,
            ("LM", "HILLS"): 30, ("HILLS", "DT"): 30, ("HILLS", "LM"): 30, ("HILLS", "HILLS"): 15},
    "SJC": {("WG", "WG"): 15, ("WG", "CAM"): 25, ("WG", "SC"): 30, ("CAM", "WG"): 25, ("CAM", "CAM"): 15,
            ("CAM", "SC"): 30, ("SC", "WG"): 35, ("SC", "CAM"): 30, ("SC", "SC"): 15},
    "SRF": {("SR", "SR"): 15, ("SR", "MV"): 30, ("MV", "SR"): 30, ("MV", "MV"): 15, ("MV", "NOV"): 45,
            ("NOV", "MV"): 45, ("NOV", "NOV"): 15, ("NOV", "SR"): 35},
}  # fmt: skip
UNKNOWN_PAIR = ("SRF", "SR", "NOV")
# Oakland's towns by zone. San Ramon and Fremont are outside the zoned area for now: their homes keep the
# travel minutes recorded on each visit ("Legacy travel estimate", EM 03B closure A).
OAK_TOWN_ZONE = {
    **dict.fromkeys(
        ("Oakland", "Emeryville", "Alameda", "Berkeley", "Albany", "El Cerrito", "Richmond"), "DT"
    ),
    **dict.fromkeys(("Piedmont", "San Leandro", "Hayward"), "LM"),
    **dict.fromkeys(("Castro Valley", "Orinda", "Lafayette", "Walnut Creek"), "HILLS"),
}
LEGACY_TOWNS = ("San Ramon", "Fremont")


@dataclass(frozen=True)
class RegionHub:
    """A hub added in 03B-3: South Bay (SJC) and Marin (SRF)."""

    code: str
    name: str
    area: str
    robots: tuple[str, ...]  # registered here
    # (username, roles at this hub, person code, note)
    logins: tuple[tuple[str, tuple[str, ...], str, str], ...]
    crews: tuple[tuple[str, str, str, str], ...]  # (name, lead, second, vehicle)
    homes: tuple[tuple[str, str, str], ...]  # (code, town, zone code)
    slots: tuple[tuple[dtime, int], ...]  # morning and afternoon waves (local start, minutes on site)


REGION_HUBS: tuple[RegionHub, ...] = (
    RegionHub(
        "SJC", "South Bay hub", "San Jose",
        tuple(f"NB-{n}" for n in range(201, 230)),  # 29 here + 1 received from Oakland = 30
        (("nora.castillo", ("field_operator", "hub_technician"), "P-210", "South Bay hub technician"),
         ("luis.romero", ("field_operator",), "P-211", "South Bay crew D lead"),
         ("grace.chen", ("field_operator",), "P-212", "South Bay crew D"),
         ("derek.osei", ("field_operator",), "P-221", "South Bay crew E lead"),
         ("maya.patel", ("field_operator",), "P-222", "South Bay crew E")),
        (("D", "P-211", "P-212", "VAN-5"), ("E", "P-221", "P-222", "VAN-6")),
        (("H-201", "Willow Glen", "WG"), ("H-202", "Willow Glen", "WG"), ("H-203", "Campbell", "CAM"),
         ("H-204", "Campbell", "CAM"), ("H-205", "Santa Clara", "SC"), ("H-206", "Santa Clara", "SC"),
         ("H-207", "Willow Glen", "WG"), ("H-208", "Campbell", "CAM")),
        ((dtime(8, 30), 120), (dtime(13, 0), 90)),  # suburban and morning-heavy
    ),
    RegionHub(
        "SRF", "Marin hub", "San Rafael",
        tuple(f"NB-{n}" for n in range(301, 321)),
        (("ivan.petrov", ("field_operator", "hub_technician", "safety_pause"), "P-310",
          "Marin hub technician (may pause departures at Marin)"),
         ("sofia.alvarez", ("field_operator",), "P-311", "Marin crew F lead"),
         ("ben.carter", ("field_operator",), "P-312", "Marin crew F")),
        (("F", "P-311", "P-312", "VAN-7"),),
        (("H-301", "San Rafael", "SR"), ("H-302", "San Rafael", "SR"), ("H-303", "Mill Valley", "MV"),
         ("H-304", "Mill Valley", "MV"), ("H-305", "San Rafael", "SR"), ("H-306", "Novato", "NOV")),
        ((dtime(10, 0), 90), (dtime(14, 30), 90)),  # small, hilly, long drives
    ),
)  # fmt: skip
REGION_DAYS = 4  # South Bay and Marin are booked from today to three days ahead
SOUTH_BAY_EVENING = dtime(18, 0)  # crew D's evening visit tomorrow (Oakland has its own evening slot)
AWAY_MINUTES = 180  # the South Bay visit that has its robot out today (afternoon wave)
TRAVEL_UNKNOWN_HOME = "H-306"  # Novato: San Rafael -> Novato has no value
PAUSE_REASON = "Street fair closes the roads around the lake"
LIFT_REASON = "Street fair is over and the roads are open again"

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
    "ON CONFLICT (key) WHERE hub_id IS NULL DO NOTHING",
    "INSERT INTO fleet_deploymentsetting (key, value) VALUES ('max_robots_per_trip', '1') "
    "ON CONFLICT (key) WHERE hub_id IS NULL DO NOTHING",
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


# v2 03A-1: fleet_deploymentsetting got a surrogate id (identity) and keeps the migration rows across a wipe
# (MIGRATION_ROWS), so its sequence is never restarted: its ids are internal and appear in no code.
SEQUENCES_KEPT_FOR = KEEP_TABLES | {"fleet_deploymentsetting"}


def restart_sequences() -> None:
    """Application sequences back to their start, so codes (V-0001, ...) are the same for every build of a
    date. Run only on an empty or just-wiped database (every application table is empty)."""
    with connection.cursor() as cur:
        cur.execute(APP_SEQUENCES_SQL, [sorted(SEQUENCES_KEPT_FOR)])
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
    # v2 02E: the practice runs (visit, run, robot) and the robot whose maintenance is due soon / overdue
    practice: list[tuple[str, str, str]] = field(default_factory=list)
    due_soon_robot: str = ""
    overdue_robot: str = ""
    predictor_rows: int = 0
    kit_short_visit: str = ""
    # v2 03B-3: the robot moved to South Bay (robot, transfer), the South Bay run out today (visit, run,
    # robot) and the attention visit of each hub
    transferred: tuple[str, str] | None = None
    away: tuple[str, str, str] | None = None
    attention: dict[str, str] = field(default_factory=dict)


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
        held = (MAINTENANCE_ROBOT, INSPECTION_HOLD_ROBOT, INCIDENT_ROBOT, NO_MAINTENANCE_BASELINE)
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
            self.u[username] = user
            self.s.logins.append((username, " + ".join([role, *EXTRA_ROLES.get(username, [])]), note))
        # v2 03A-1: the admin's (regional) grant first, then the hub, then every other grant: hub-scoped
        # roles (hub technician, field operator) are granted AT the Oakland hub, the others are regional. No
        # record is created before the hub, so the database never has to create a default hub.
        # v2 03A-3: grants go through the role-grant functions (accounts/0006), as in a real workspace: the
        # admin's grant is the empty workspace's first-admin bootstrap, and the admin grants every other role.
        admin = self.u["priya.raman"]
        Workspace.ensure()
        with connection.cursor() as cur:
            cur.execute("SELECT accounts_bootstrap_admin(%s, %s)", [admin.pk, "demo-build"])
        update_workspace(admin, name=COMPANY, region=REGION, time_zone=TZ)
        Workspace.objects.filter(pk=1).update(demo=True, initialized=True, initialized_at=timezone.now())
        create_hub(admin, code="OAK", name="Oakland hub", area="West Oakland", time_zone=TZ)
        for username, role, _code, _note in LOGINS:
            for r in [role, *EXTRA_ROLES.get(username, [])]:
                if username == "priya.raman" and r == ADMIN:
                    continue
                grant_role(
                    admin,
                    user_id=self.u[username].pk,
                    role=r,
                    hub_code="OAK" if r in HUB_SCOPED_ROLES else None,
                )
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
            interval, every = PERIODIC_INTERVALS[code]
            fs.create_periodic_rule(
                eng, pattern_code=code, interval_min=interval, source="synthetic_demo", notes=every
            )
            rule = PeriodicInspectionRule.objects.get(pattern__code=code, version_no=1)
            fs.approve_periodic_rule(lead, rule_id=rule.pk, slot="engineering")
            fs.approve_periodic_rule(safety, rule_id=rule.pk, slot="safety")
            # v2 02C: demo maintenance rules, proposed by the engineer, approved by the engineering lead and
            # the safety reviewer (synthetic_demo: never usable in production, a production blocker there)
            template = InspectionTemplate.objects.get(pattern__code=code, version="T1")
            for req, days, visits, hours, warn, inspect in MAINTENANCE_RULES:
                m = fm.propose_rule(
                    eng, pattern_code=code, requirement_code=req, calendar_days=days, visit_count=visits,
                    active_hours=hours, warning_pct=warn,
                    inspection_template_id=template.pk if inspect else None, source="synthetic_demo",
                    notes="Demo value, not an engineering requirement",
                )  # fmt: skip
                fm.approve_rule(lead, rule_id=m["rule_id"], slot="engineering")
                fm.approve_rule(safety, rule_id=m["rule_id"], slot="safety")
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
        # v2 02C: the hub technician signs maintenance records (granted by the safety reviewer, as inspectors)
        ps.grant_qualification(
            safety,
            person_code=HUB_TECH,
            kind="maintenance_technician",
            valid_from=None,
            valid_to=end,
            basis_ref=f"TRN-MNT-2026-{HUB_TECH[2:]}",
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
            # v2 02C: the baseline maintenance records, stamped now (no invented history), BEFORE the
            # day's inspection, which is then also the post-maintenance inspection HAND-SERVICE requires.
            # Every robot with a configuration except NO_MAINTENANCE_BASELINE (Unknown, blocked) and the
            # commissioning ones.
            if n <= IN_SERVICE + 3 and code != NO_MAINTENANCE_BASELINE:
                for req, *_rest in MAINTENANCE_RULES:
                    fm.record_maintenance(
                        self.u["lily.tran"],
                        robot_code=code,
                        requirement_code=req,
                        notes="Baseline at go-live",
                    )
            if code != NEVER_INSPECTED:
                self.inspect(code)
        self.lap("robots")

    def inspect(self, robot: str, answers: dict[str, str] | None = None, performer: str = "") -> str:
        """A robot inspection by the hub technician and the ops lead, both qualified inspectors."""
        return str(
            fs.submit_inspection(
                self.u["dana.ortiz"],
                robot_code=robot,
                template_version="T1",
                answers=answers or dict(ALL_PASS),
                performer=performer or self.username(HUB_TECH),
                verifier="dana.ortiz",
            )["entity_id"]
        )

    @staticmethod
    def username(person_code: str) -> str:
        every = [(u, c) for u, _r, c, _n in LOGINS] + [
            (u, c) for h in REGION_HUBS for u, _r, c, _n in h.logins
        ]
        return next(u for u, c in every if c == person_code)

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
        # v2 03B-3: a travel value never takes effect in the past (13.4 item 3), so the visits that already
        # started when the build runs are booked first, before their homes get a zone: they keep their
        # recorded minutes (legacy). Every later visit of a zoned home uses the demo travel matrix.
        cut = timezone.now() + timedelta(minutes=10)
        started = [r for r in future if self.at(r["day"], r["start"]) <= cut]
        for row in started:
            booked.append((row, self.book(row)))
        self.oakland_zones()
        for row in future:
            if row not in started:
                booked.append((row, self.book(row)))
        booked.sort(key=lambda rc: rc[1])
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
        # Requests not planned yet (no robot or crew assigned). v2 02E (walk D6): in the evening, after every
        # crew's afternoon visit is unloaded (the same window as the exception visits above, never used on
        # these days), so J3's Suggest finds a free crew, van and robot and the plan can be saved.
        for k, home in enumerate(("H-120", "H-133", "H-138")):
            start = self.at(self.anchor + timedelta(days=5 + k), evening)
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

    # .............................................................. practice runs (v2 02E)
    def practice_runs(self) -> None:
        """Two short visits at 00:30 local today, each run end to end through the real commands and closed
        now: readiness evidence, the ops lead's authorization, departure, custody, site check, session,
        return, post-visit inspection, reconciliation and trip closure. Every record is stamped at the
        build (database time); nothing is back-dated. Robot "overdue" is booked tomorrow, so its visit shows
        the maintenance block; robot "due soon" is not booked today or tomorrow. After its run the hub
        technician records SENSOR-CLEAN on the due-soon robot, so only GRIPPER-PADS is due soon there."""
        from execution import logistics as lg  # noqa: PLC0415
        from execution import reconciliation as rcn  # noqa: PLC0415
        from execution import sessions as ses  # noqa: PLC0415
        from execution import site_checks as sc  # noqa: PLC0415
        from execution.models import ExecutionRun  # noqa: PLC0415
        from hub import charging as hc  # noqa: PLC0415
        from readiness import commands as rc  # noqa: PLC0415

        if fs.production_mode():  # never in production: practice runs exist only in the demo workspace
            raise DemoRefused(
                "Practice runs are built only in a demo workspace (demo_mode 'true'); nothing done."
            )
        ops, field_user = self.u["dana.ortiz"], self.u["lily.tran"]
        today, tomorrow = self.anchor, self.anchor + timedelta(days=1)
        # the practice home: its own code, and its town field says "Fictional practice visit" (shown with the
        # home on the visit, readiness and run pages). Assessed and consented like every demo home.
        ps.create_site(
            ops, code=PRACTICE_HOME, zone=PRACTICE_ZONE, tz=TZ, crm_ref=PRACTICE_CRM, expected_adults=1
        )
        a = ps.record_assessment(
            field_user, site_code=PRACTICE_HOME, decision="eligible", valid_until=today + timedelta(days=180)
        )
        ps.approve_assessment(ops, assessment_id=int(a["entity_id"]))
        ps.record_consent(field_user, site_code=PRACTICE_HOME, participant_ref="A1",
                          form_version=SETTINGS["consent_form_version"], scope=["presence", "recording"],
                          evidence_ref=f"SIGN-{PRACTICE_HOME}-1")  # fmt: skip

        def robots_on(day: date, *, regular: bool = False) -> list[str]:
            """Robots booked on the day; ``regular``: only the regular visits (not the special homes')."""
            visits = Visit.objects.exclude(site__code__in=SPECIAL_HOMES) if regular else Visit.objects.all()
            return list(
                visits.filter(
                    current_revision__on_site_start__gte=self.at(day, dtime(0, 0)),
                    current_revision__on_site_start__lt=self.at(day + timedelta(days=1), dtime(0, 0)),
                    current_revision__robot__isnull=False,
                )
                .exclude(status="cancelled")
                .order_by("current_revision__on_site_start", "code")
                .values_list("current_revision__robot__code", flat=True)
            )

        booked_today, booked_tomorrow = set(robots_on(today)), robots_on(tomorrow)
        j4_robot = Visit.objects.filter(code=self.s.j4_visit).values_list(
            "current_revision__robot__code", flat=True
        )[0]
        # The robot of tomorrow's last regular visit (not J4's, not one charging() reads first) becomes
        # overdue. Its practice visit ends hours before any visit of today, so it may also be booked today.
        overdue = next(r for r in reversed(robots_on(tomorrow, regular=True)) if r != j4_robot)
        due_soon = next(r for r in self.pool if r not in booked_today and r not in booked_tomorrow)
        crews = {c[0]: c for c in CREWS}
        travel = PRACTICE_TRAVEL
        for robot, crew_name in ((due_soon, PRACTICE_CREWS[0]), (overdue, PRACTICE_CREWS[1])):
            crew = crews[crew_name]
            _name, lead_code, second_code, _van = crew
            lead, second = self.username(lead_code), self.username(second_code)
            lead_u, second_u = self.u[lead], self.u[second]
            row = {"day": today, "start": PRACTICE_START, "minutes": PRACTICE_MIN, "home": PRACTICE_HOME,
                   "travel": travel, "crew": crew, "robot": robot}  # fmt: skip
            visit = self.book(row)
            self.recovery_plan(visit)
            insp = fs.submit_inspection(
                ops, robot_code=robot, template_version="T1", answers=dict(ALL_PASS), performer=lead,
                verifier=second, visit_ref=visit,
            )["entity_id"]  # fmt: skip
            rx.record_evidence(
                ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
            )
            rx.record_evidence(
                second_u, visit_code=visit, kind="equipment_check", result="pass", details=EQUIPMENT
            )
            # the hub technician reads the battery by hand (the predictor log's "battery at start"), then the
            # crew records the departure battery check (the readiness evidence; a reading is never evidence)
            hc.record_soc(self.u["lily.tran"], robot_code=robot, soc_pct=92)
            rx.record_evidence(
                second_u, visit_code=visit, kind="battery_check", result="pass", details={"soc_pct": 92}
            )
            auth = rc.authorize_departure(ops, visit_code=visit)["code"]
            rc.record_departure(ops, authorization_code=auth)
            run = ExecutionRun.objects.get(visit__code=visit)
            lg.verify_custody(lead_u, run_code=run.code, custody_seq=1, verifier_code=lead_code)
            lg.record_trip_event(lead_u, run_code=run.code, kind="arrived_site")
            lg.transfer_custody(lead_u, run_code=run.code, to_kind="site", custodian_code=lead_code,
                                received_by_code=second_code)  # fmt: skip
            check = sc.record_site_check(lead_u, run_code=run.code, template_version="SC-1", adults_present=1,
                                         conditions={i["id"]: "ok" for i in SITE_CHECK_ITEMS},
                                         verifier_code=second_code)  # fmt: skip
            ses.record_session_event(lead_u, run_code=run.code, kind="start", site_check_id=check["id"])
            ses.record_session_event(lead_u, run_code=run.code, kind="stop")
            lg.transfer_custody(lead_u, run_code=run.code, to_kind="vehicle", custodian_code=second_code,
                                received_by_code=lead_code)  # fmt: skip
            lg.record_trip_event(lead_u, run_code=run.code, kind="departed_site")
            lg.record_trip_event(lead_u, run_code=run.code, kind="arrived_hub")
            lg.transfer_custody(lead_u, run_code=run.code, to_kind="hub", custodian_code=lead_code,
                                received_by_code=HUB_TECH)  # fmt: skip
            post = fs.submit_inspection(
                ops, robot_code=robot, template_version="T1", answers=dict(ALL_PASS),
                performer=self.username(HUB_TECH), verifier="dana.ortiz", visit_ref=visit,
            )["entity_id"]  # fmt: skip
            rcn.reconcile_return(ops, run_code=run.code, inspection_code=post)
            rcn.close_trip(ops, run_code=run.code)
            self.s.practice.append((visit, run.code, robot))
        fm.record_maintenance(self.u["lily.tran"], robot_code=due_soon, requirement_code="SENSOR-CLEAN",
                              notes="Sensors cleaned after the practice run")  # fmt: skip
        self.s.due_soon_robot, self.s.overdue_robot = due_soon, overdue
        self.lap("practice runs (2, recorded now)")

    # .............................................................. hub work queue (v2 02A)
    def hub_tasks(self) -> None:
        """A few hand-made hub tasks; the ones derived from records (inspections due, holds, failed equipment
        checks) are created when the Hub page first opens. To-dos only: none of them changes a record."""
        from hub import services as hs  # noqa: PLC0415

        ops, lily = self.u["dana.ortiz"], self.u["lily.tran"]
        morning = self.at(self.anchor + timedelta(days=1), dtime(8, 0))
        hs.create_task(ops, type="kit_low", title="Restock recovery kits: two left on the shelf",
                       owner_code=HUB_TECH, due_at=morning)  # fmt: skip
        hs.create_task(ops, type="other", title="Tidy charging bay 2 and coil the cables")
        started = hs.create_task(
            lily, type="other", title="Label the spare restraint straps", owner_code=HUB_TECH
        )
        hs.start_task(lily, task_id=int(started["entity_id"]))
        self.lap("hub tasks")

    # .............................................................. charging (v2 02B)
    def charging(self) -> None:
        """Four chargers (one out of service), battery levels read by hand TODAY (stamped now: no invented
        history), one booking and one session. The forecast then shows Enough, Needs charge and Unknown."""
        from hub import charging as hc  # noqa: PLC0415

        ops, lily = self.u["dana.ortiz"], self.u["lily.tran"]
        for code, name in (
            ("C-1", "Bay 1 by the door"),
            ("C-2", "Bay 2"),
            ("C-3", "Bay 3"),
            ("C-4", "Bay 4 (spare)"),
        ):
            hc.create_charger(ops, code=code, name=name)
        hc.set_charger_in_service(
            ops, code="C-4", in_service=False, reason="Cable damaged; replacement ordered"
        )
        now = timezone.now()
        tomorrow = self.anchor + timedelta(days=1)
        robots = list(
            Visit.objects.filter(
                current_revision__on_site_start__gte=self.at(tomorrow, dtime(0, 0)),
                current_revision__on_site_start__lt=self.at(tomorrow + timedelta(days=1), dtime(0, 0)),
                current_revision__robot__isnull=False,
            )
            .exclude(status="cancelled")
            .order_by("current_revision__on_site_start", "code")
            .values_list("current_revision__robot__code", flat=True)
        )
        robots = list(dict.fromkeys(robots))
        if len(robots) >= 3:
            hc.record_soc(lily, robot_code=robots[0], soc_pct=94)  # meets the 80% demo minimum
            hc.record_soc(lily, robot_code=robots[1], soc_pct=46)  # below it: needs charge
            self.s.attention["OAK"] = str(
                Visit.objects.filter(
                    current_revision__robot__code=robots[1],
                    current_revision__on_site_start__gte=self.at(tomorrow, dtime(0, 0)),
                )
                .exclude(status="cancelled")
                .order_by("current_revision__on_site_start")
                .values_list("code", flat=True)
                .first()
            )
            # robots[2] and the rest: nothing recorded today, so the forecast is Unknown
            evening = max(now + timedelta(minutes=10), self.at(self.anchor, dtime(21, 0)))
            hc.reserve_charger(lily, charger_code="C-1", robot_code=robots[1], starts_at=evening,
                               ends_at=evening + timedelta(hours=2, minutes=30))  # fmt: skip
        from platform_core.commands import CommandError  # noqa: PLC0415

        for spare in (code for code in self.pool if code not in robots):  # the first robot free right now
            try:
                until = now + timedelta(hours=3)
                hc.start_charging(
                    lily, charger_code="C-2", robot_code=spare, planned_until=until, start_soc_pct=58
                )
                break
            except CommandError:
                continue
        self.lap("charging")

    # .............................................................. kit inventory (v2 02D)
    def kit_short_visit(self) -> str:
        """v2 02E: the visit that will be "Kit short", chosen so that kits are its ONLY At-risk cause the
        build controls: tomorrow's last regular visit (not at a special home, not J4) whose robot is not
        held, not blocked by maintenance, not due soon and has no battery reading from the build (the
        needs-charge and "Enough" robots), with no other visit before it from today. It gets a recovery plan
        like J4 and a hand battery reading of 88%, so readiness misses only the day's checks and the charge
        forecast says Enough."""
        from hub.models import RobotSocReading  # noqa: PLC0415

        tomorrow = self.anchor + timedelta(days=1)
        skip = {MAINTENANCE_ROBOT, INSPECTION_HOLD_ROBOT, INCIDENT_ROBOT, NO_MAINTENANCE_BASELINE,
                self.s.overdue_robot, self.s.due_soon_robot,
                *RobotSocReading.objects.values_list("robot__code", flat=True)}  # fmt: skip
        visits = (
            Visit.objects.filter(
                status="confirmed",
                current_revision__on_site_start__gte=self.at(tomorrow, dtime(0, 0)),
                current_revision__on_site_start__lt=self.at(tomorrow + timedelta(days=1), dtime(0, 0)),
                current_revision__robot__isnull=False,
            )
            .exclude(site__code__in=(*SPECIAL_HOMES, PRACTICE_HOME))
            .exclude(code=self.s.j4_visit)
            .exclude(current_revision__robot__code__in=skip)
            .order_by("-current_revision__on_site_start", "-code")
        )
        from hub import charging as hc  # noqa: PLC0415

        day_start = self.at(self.anchor, dtime(0, 0))
        for v in visits.select_related("current_revision__robot"):
            rev = v.current_revision
            assert rev is not None and rev.robot is not None
            earlier = Visit.objects.filter(  # another visit first would make the charge forecast Unknown
                current_revision__robot=rev.robot,
                current_revision__on_site_start__gte=day_start,
                current_revision__on_site_start__lt=rev.on_site_start,
            ).exclude(status="cancelled")
            if not earlier.exists():
                break
        else:
            raise DemoRefused("No clean visit tomorrow for the kit shortage; nothing more built.")
        self.recovery_plan(v.code)
        # read by hand today, above the 80% demo minimum: the charge forecast says Enough for this visit
        hc.record_soc(self.u["lily.tran"], robot_code=rev.robot.code, soc_pct=88)
        return v.code

    def stock(self) -> None:
        """Fictional kits and consumables, every quantity recorded TODAY through the stock ledger (no
        invented history). Each visit takes one restraint kit (demo setting). v2 02E: one kit is
        reserved for every visit that departs from now to the end of the day after tomorrow, except ONE
        clean visit tomorrow (``kit_short_visit``); the count received covers exactly those reservations
        after one kit is written off, so that visit, and only it, shows At risk "Kit short" all day (and
        restraint kits fall below their minimum: a restock task). Gloves end below their minimum too.
        None of it is an equipment check."""
        from hub import stock as st  # noqa: PLC0415
        from readiness.models import DepartureEvent  # noqa: PLC0415

        ops, lily = self.u["dana.ortiz"], self.u["lily.tran"]
        for code, name, kind, unit in (
            ("RK", "Restraint kit", "reusable", "kits"),
            ("RC", "Recovery kit", "reusable", "kits"),
            ("WIPE", "Cleaning wipes", "consumable", "packs"),
            ("GLOVE", "Nitrile gloves", "consumable", "boxes"),
        ):
            st.create_item(ops, code=code, name=name, kind=kind, unit=unit)
        short = self.s.kit_short_visit = self.kit_short_visit()
        until = self.at(self.anchor + timedelta(days=3), dtime(0, 0))
        departed = DepartureEvent.objects.values_list("visit_id", flat=True)
        covered = [
            v.code
            for v in Visit.objects.exclude(status="cancelled")
            .exclude(pk__in=departed)
            .exclude(code=short)
            .select_related("current_revision")
            .order_by("current_revision__on_site_start", "code")
            if v.current_revision is not None
            and timezone.now()
            <= v.current_revision.on_site_start - timedelta(minutes=v.current_revision.travel_out_min or 0)
            < until
        ]
        st.receive(lily, item_code="RK", quantity=len(covered) + 1, note="Demo stock count")
        st.adjust(lily, item_code="RK", delta=-1, reason="Strap torn during the count; written off")
        st.receive(lily, item_code="RC", quantity=4, note="Demo stock count")
        st.receive(lily, item_code="WIPE", quantity=30, note="Demo stock count")
        st.consume(lily, item_code="WIPE", quantity=4)
        st.receive(lily, item_code="GLOVE", quantity=6, note="Demo stock count")
        st.consume(lily, item_code="GLOVE", quantity=3)  # 3 left, minimum 4: a restocking task
        for code in covered:
            st.reserve(lily, item_code="RK", visit_code=code, quantity=1)
        if self.s.j4_visit:
            st.reserve(lily, item_code="RC", visit_code=self.s.j4_visit, quantity=1)
        self.lap(f"stock ({len(covered)} restraint kits reserved; {short} is kit short)")

    # .............................................................. v2 03B-3: zones and travel
    def set_site_zone(self, site_code: str, hub_code: str, zone_code: str) -> None:
        """The ONE place the demo sets a home's zone (03B-4 may switch it to their guarded function)."""
        from planning import zones  # noqa: PLC0415

        zones.set_site_zone(
            self.u["dana.ortiz"],
            site_code=site_code,
            hub_code=hub_code,
            zone_code=zone_code,
            reason="Demo set-up: the home's area (fictional)",
        )

    def hub_zones(self, hub_code: str) -> None:
        """The hub's zones, its own zone and its demo travel matrix (source 'demo', effective now: a travel
        value never takes effect in the past)."""
        from planning import travel, zones  # noqa: PLC0415
        from platform_core.models import Zone  # noqa: PLC0415

        ops = self.u["dana.ortiz"]
        for code, name in HUB_ZONES[hub_code]:
            zones.create_zone(ops, hub_code=hub_code, code=code, name=name)
        zones.set_hub_home_zone(ops, hub_code=hub_code, zone_code=HUB_HOME_ZONE[hub_code])
        ids = dict(Zone.objects.filter(hub__code=hub_code).values_list("code", "pk"))
        for (a, b), minutes in DEMO_TRAVEL[hub_code].items():
            travel.set_travel_time(
                ops, from_zone_id=ids[a], to_zone_id=ids[b], minutes=minutes, source="demo"
            )

    def oakland_zones(self) -> None:
        self.hub_zones("OAK")
        for site_code, town in Site.objects.filter(code__startswith="H-").values_list("code", "zone"):
            if town in OAK_TOWN_ZONE:
                self.set_site_zone(site_code, "OAK", OAK_TOWN_ZONE[town])
        self.lap("Oakland zones and demo travel matrix")

    # .............................................................. v2 03B-3: South Bay and Marin
    def regions(self) -> None:
        """South Bay (SJC) and Marin (SRF), through the same guarded commands as Oakland: hubs, logins with
        hub-scoped grants, crews, vans, robots (inspected today, maintenance baselines today), zones and the
        demo travel matrix, homes with an explicit zone, a robot received by transfer from Oakland, a morning
        and an afternoon wave per crew from today to three days ahead, an evening visit, one attention visit
        per hub and a lifted zone pause at Oakland."""
        admin, ops, safety = self.u["priya.raman"], self.u["dana.ortiz"], self.u["elena.vasquez"]
        encoded = make_password(self.password)
        start = self.at(self.anchor - timedelta(days=120), dtime(0, 0))
        end = self.at(self.anchor + timedelta(days=240), dtime(0, 0))
        for h in REGION_HUBS:
            create_hub(admin, code=h.code, name=h.name, area=h.area, time_zone=TZ)
            for username, roles, person, note in h.logins:
                first, _, last = username.partition(".")
                self.u[username] = User.objects.create(
                    username=username,
                    password=encoded,
                    first_name=first.capitalize(),
                    last_name=last.capitalize(),
                )
                for role in roles:
                    grant_role(admin, user_id=self.u[username].pk, role=role, hub_code=h.code)
                self.s.logins.append((username, " + ".join(roles) + f" @{h.code}", note))
                ps.register_person(ops, username=username, code=person, hub_code=h.code)
                ps.grant_qualification(
                    safety,
                    person_code=person,
                    kind="readiness_inspector",
                    valid_from=None,
                    valid_to=end,
                    basis_ref=f"TRN-2026-{person[2:]}",
                )
            tech_user, tech_code = h.logins[0][0], h.logins[0][2]
            ps.grant_qualification(
                safety,
                person_code=tech_code,
                kind="maintenance_technician",
                valid_from=None,
                valid_to=end,
                basis_ref=f"TRN-MNT-2026-{tech_code[2:]}",
            )
            for _name, lead, second, van in h.crews:
                for kind in ("field_lead", "robot_handler"):
                    ps.grant_qualification(ops, person_code=lead, kind=kind, valid_from=start, valid_to=end)
                for kind in ("field_operator", "robot_handler"):
                    ps.grant_qualification(ops, person_code=second, kind=kind, valid_from=start, valid_to=end)
                ps.register_vehicle(
                    ops, code=van, robot_capacity=1, restraint_kit="Seat rail and strap kit", hub_code=h.code
                )
            self.region_robots(h, tech_user)
            self.hub_zones(h.code)
            self.region_homes(h, tech_user)
            # v2 03B-3: the operating hours of every hub (demo value, owner setting, like the other settings)
        for code in ("OAK", *(h.code for h in REGION_HUBS)):
            DeploymentSetting.objects.update_or_create(
                key="hub_operating_hours", hub=Hub.objects.get(code=code), defaults={"value": "08:00-20:00"}
            )
        self.lap("South Bay and Marin: hubs, people, robots, zones, homes")
        self.transfer_to_south_bay()
        self.region_visits()
        self.pause_history()

    def region_robots(self, h: RegionHub, tech: str) -> None:
        ops, lead, eng = self.u["dana.ortiz"], self.u["kenji.watanabe"], self.u["sam.keller"]
        for i, code in enumerate(h.robots):
            n = int(code[3:])
            fs.register_robot(ops, code=code, serial=f"NBH2-{25 + i // 30}-{4000 + n * 7}", hub_code=h.code)
            fs.record_manifest(
                eng,
                robot_code=code,
                pattern_code=PATTERN_HOME,
                autonomy_version="nav-7.2",
                safety_config_hash=f"{n % 256:02x}" * 32,
                calibration_ref=f"CAL-{n:03d}",
            )
            fs.transition_lifecycle(lead, robot_code=code, to="validation", reason="Commissioning finished")
            fs.transition_lifecycle(lead, robot_code=code, to="in_service", reason="Validation passed")
            fs.grant_base_authorization(lead, robot_code=code)
            for req, *_rest in MAINTENANCE_RULES:
                fm.record_maintenance(
                    self.u[tech], robot_code=code, requirement_code=req, notes="Baseline at go-live"
                )
            self.inspect(code, performer=tech)

    def region_homes(self, h: RegionHub, tech: str) -> None:
        ops, field_user = self.u["dana.ortiz"], self.u[tech]
        until = self.anchor + timedelta(days=180)
        for i, (code, town, zone) in enumerate(h.homes):
            adults = 1 + (i % 2)
            ps.create_site(
                ops,
                code=code,
                zone=town,
                tz=TZ,
                crm_ref=f"CRM-{51000 + int(code[2:]) * 7}",
                expected_adults=adults,
            )
            self.set_site_zone(code, h.code, zone)  # every new home has an explicit zone
            a = ps.record_assessment(field_user, site_code=code, decision="eligible", valid_until=until)
            ps.approve_assessment(ops, assessment_id=int(a["entity_id"]))
            for n in range(1, adults + 1):
                ps.record_consent(
                    field_user,
                    site_code=code,
                    participant_ref=f"A{n}",
                    form_version=SETTINGS["consent_form_version"],
                    scope=["presence", "recording"],
                    evidence_ref=f"SIGN-{code}-{n}",
                )

    def transfer_to_south_bay(self) -> None:
        """One Oakland robot moves to South Bay through the real transfer functions: sent by Oakland's hub
        technician, received by South Bay's. Stamped now (a transfer cannot be back-dated to yesterday). The
        robot is one of Oakland's with no booking from now on and no role in the Hub day facts."""
        from fleet import transfers as tr  # noqa: PLC0415
        from planning.models import Reservation  # noqa: PLC0415
        from platform_core.commands import CommandError  # noqa: PLC0415

        busy = set(
            Reservation.objects.filter(released_at__isnull=True, ends_at__gt=timezone.now()).values_list(
                "resource__robot__code", flat=True
            )
        )
        skip = {self.s.due_soon_robot, self.s.overdue_robot, *busy}
        for code in [c for c in reversed(self.pool) if c not in skip] + ["NB-049"]:
            try:
                t = tr.start_transfer(
                    self.u["lily.tran"],
                    kind="robot",
                    code=code,
                    to_hub_code="SJC",
                    reason="South Bay needs one more robot for its morning wave",
                )
            except CommandError:
                continue
            tr.receive_transfer(self.u["nora.castillo"], transfer_id=int(t["id"]))
            self.s.transferred = (code, str(t["code"]))
            break
        self.lap("transfer Oakland -> South Bay")

    def region_book(
        self,
        hub: str,
        home: str,
        day: date,
        start: dtime,
        minutes: int,
        crew: tuple[str, ...],
        robot: str,
        *,
        confirm: bool = True,
    ) -> str:
        ops = self.u["dana.ortiz"]
        on_site = self.at(day, start)
        code = str(
            ps.request_visit(
                ops,
                site_code=home,
                on_site_start=on_site,
                on_site_end=on_site + timedelta(minutes=minutes),
                tasks=["tidy", "laundry"],
                required_scopes=["presence"],
                hub_code=hub,
            )["entity_id"]
        )
        out = ps.plan_visit(
            ops,
            visit_code=code,
            robot_code=robot,
            lead_code=crew[1],
            second_code=crew[2],
            vehicle_code=crew[3],
        )
        if confirm and out["feasibility"] == "feasible":
            ps.confirm_visit(ops, visit_code=code)
        return code

    def region_visits(self) -> None:
        """A morning and an afternoon wave per crew, today to three days ahead (Sundays off except today and
        tomorrow); robots in rotation, never twice a day. South Bay: crew D's evening visit tomorrow; the
        robot out on a run today (At risk tomorrow). Marin: the Novato visit (travel unknown, blocked)."""
        today, tomorrow = self.anchor, self.anchor + timedelta(days=1)
        transferred = self.s.transferred[0] if self.s.transferred else ""
        sjc, srf = REGION_HUBS
        away_robot = sjc.robots[0]
        # the away run is crew D's afternoon wave today, or tomorrow's when the build runs after it started
        away_day = 0 if timezone.now() + timedelta(minutes=40) <= self.at(today, sjc.slots[1][0]) else 1
        for h in REGION_HUBS:
            homes = [x[0] for x in h.homes if x[0] != TRAVEL_UNKNOWN_HOME]
            robots = [r for r in h.robots if r != away_robot]
            if h.code == "SJC" and transferred:
                robots.insert(3, transferred)
            k = 0
            used: dict[date, set[str]] = {}
            for offset in range(REGION_DAYS):
                day = today + timedelta(days=offset)
                if day.weekday() == 6 and offset not in (0, 1):
                    continue
                for c, crew in enumerate(h.crews):
                    for slot, (start, minutes) in enumerate(h.slots):
                        if h.code == "SJC" and offset == away_day and c == 0 and slot == 1:
                            continue  # crew D's afternoon wave of that day is the away run (below)
                        if h.code == "SJC" and offset == away_day + 1 and c == 0 and slot == 0:
                            continue  # crew D's next morning is the At-risk visit (below)
                        if self.at(day, start) <= timezone.now() + timedelta(minutes=10):
                            continue  # already started: the demo travel values only take effect now
                        robot = robots[k % len(robots)]
                        home = homes[(offset * 3 + c * 2 + slot) % len(homes)]
                        k += 1
                        used.setdefault(day, set()).add(robot)
                        self.region_book(h.code, home, day, start, minutes, crew, robot)
            if h.code == "SJC":
                # crew D's evening visit tomorrow (after their afternoon wave is unloaded), a robot not out
                # tomorrow
                evening = next(r for r in robots if r not in used.get(tomorrow, set()))
                self.region_book("SJC", "H-207", tomorrow, SOUTH_BAY_EVENING, 60, h.crews[0], evening)
        crew_d = sjc.crews[0]
        self.away_run(away_robot, crew_d, away_day)
        # Marin: a request for the Novato home; San Rafael -> Novato has no travel value, so the plan check
        # blocks it ("Travel time unknown"), never estimated
        crew_f = srf.crews[0]
        self.s.attention["SRF"] = self.region_book(
            "SRF", TRAVEL_UNKNOWN_HOME, tomorrow, dtime(16, 30), 60, crew_f, srf.robots[-1], confirm=False
        )
        self.region_battery_readings()
        self.lap("South Bay and Marin visits")

    def region_battery_readings(self) -> None:
        """South Bay's and Marin's technicians read the battery of every robot booked from now to the end of
        tomorrow by hand (stamped now; a reading is never departure evidence): the charge forecast says
        Enough, so each new hub's only attention visit is the one built for it."""
        from hub import charging as hc  # noqa: PLC0415

        until = self.at(self.anchor + timedelta(days=2), dtime(0, 0))
        for h in REGION_HUBS:
            booked = (
                Visit.objects.filter(
                    hub__code=h.code,
                    current_revision__on_site_start__gte=timezone.now(),
                    current_revision__on_site_start__lt=until,
                    current_revision__robot__isnull=False,
                )
                .exclude(status="cancelled")
                .exclude(current_revision__robot__code=self.s.away[2] if self.s.away else "")
                .order_by("current_revision__robot__code")
                .values_list("current_revision__robot__code", flat=True)
                .distinct()
            )
            for robot in booked:
                hc.record_soc(self.u[h.logins[0][0]], robot_code=robot, soc_pct=90)

    def away_run(self, robot: str, crew: tuple[str, ...], away_day: int) -> None:
        """The robot of crew D's afternoon visit today departs now and stays out (no return recorded): its
        visit tomorrow morning is planned as "At risk: robot away until verified back at hub" (section 12).
        Every record is stamped at the build (database time), like the practice runs; nothing is back-dated.
        When the build runs after the afternoon wave has started, the run is for tomorrow's afternoon wave and
        the At-risk visit the morning after, so the run's reserved window never ends before the walk (an
        ended window would be an overdue return)."""
        from execution import logistics as lg  # noqa: PLC0415
        from execution.models import ExecutionRun  # noqa: PLC0415
        from hub import charging as hc  # noqa: PLC0415
        from readiness import commands as rc  # noqa: PLC0415

        ops = self.u["dana.ortiz"]
        today = self.anchor
        _name, lead_code, second_code, _van = crew
        lead, second = self.username(lead_code), self.username(second_code)
        run_day = today + timedelta(days=away_day)
        risk_day = run_day + timedelta(days=1)
        on_site = self.at(run_day, REGION_HUBS[0].slots[1][0]).astimezone(ZoneInfo(TZ))
        visit = self.region_book("SJC", "H-203", on_site.date(), on_site.time(), AWAY_MINUTES, crew, robot)
        self.recovery_plan(visit)
        insp = fs.submit_inspection(
            ops,
            robot_code=robot,
            template_version="T1",
            answers=dict(ALL_PASS),
            performer=lead,
            verifier=second,
            visit_ref=visit,
        )["entity_id"]
        rx.record_evidence(
            ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
        )
        rx.record_evidence(
            self.u[second], visit_code=visit, kind="equipment_check", result="pass", details=EQUIPMENT
        )
        hc.record_soc(self.u["nora.castillo"], robot_code=robot, soc_pct=95)
        rx.record_evidence(
            self.u[second], visit_code=visit, kind="battery_check", result="pass", details={"soc_pct": 95}
        )
        auth = rc.authorize_departure(ops, visit_code=visit)["code"]
        rc.record_departure(ops, authorization_code=auth)
        run = ExecutionRun.objects.get(visit__code=visit)
        lg.verify_custody(self.u[lead], run_code=run.code, custody_seq=1, verifier_code=lead_code)
        self.s.away = (visit, run.code, robot)
        self.s.attention["SJC"] = self.region_book(
            "SJC", "H-201", risk_day, REGION_HUBS[0].slots[0][0], REGION_HUBS[0].slots[0][1], crew, robot
        )

    def pause_history(self) -> None:
        """A zone pause at Oakland (Lake Merritt), paused and lifted by the ops lead: it stays in the history
        and the audit log. Stamped now."""
        from operations import pauses  # noqa: PLC0415

        ops = self.u["dana.ortiz"]
        p = pauses.pause_region(ops, hub_code="OAK", zone_code="LM", reason=PAUSE_REASON)
        pauses.lift_pause(ops, pause_id=int(p["entity_id"]), reason=LIFT_REASON)
        self.lap("pause history (Lake Merritt, paused and lifted)")

    def build(self) -> Summary:
        self.company()
        self.configurations()
        self.people()
        self.robots()
        homes = self.homes()
        self.visits(homes)
        self.practice_runs()
        self.hub_tasks()
        self.charging()
        self.stock()
        self.regions()
        self.predictor_log()
        return self.s

    def predictor_log(self) -> None:
        """v2 02D/02E: the owner-only predictor log, run once for the practice departures (the same function
        as ``manage.py log_predictor_signals``). Observational only."""
        from operations.management.commands.log_predictor_signals import log_predictor_signals  # noqa: PLC0415

        self.s.predictor_rows = log_predictor_signals()
        self.lap("predictor log")


def hub_lines(s: Summary) -> list[str]:
    """v2 03B-3: what each hub holds, with the codes a browser walk needs."""
    from planning.models import Person, Vehicle  # noqa: PLC0415
    from platform_core.models import Zone  # noqa: PLC0415

    out = []
    for hub in Hub.objects.order_by("pk"):
        zones = ", ".join(Zone.objects.filter(hub=hub).order_by("pk").values_list("name", flat=True))
        robots = Robot.objects.filter(hub=hub).count()
        vans = Vehicle.objects.filter(hub=hub, robot_capacity__isnull=False).count()
        people = Person.objects.filter(hub=hub).count()
        out.append(
            f"{hub.name} ({hub.code}): {robots} robots, {vans} crew vehicle{'s' if vans != 1 else ''}, "
            f"{people} people; zones {zones}."
        )
    if s.transferred:
        out.append(
            f"  South Bay received robot {s.transferred[0]} from Oakland by transfer {s.transferred[1]} "
            "(today: a transfer is stamped now, never back-dated)."
        )
    if s.away:
        out.append(
            f"  South Bay at risk: robot {s.away[2]} is out on run {s.away[1]} (visit {s.away[0]}); its "
            f"next morning's visit {s.attention.get('SJC', '?')} is At risk (robot away)."
        )
    if s.attention.get("SRF"):
        out.append(
            f"  Marin blocked: visit {s.attention['SRF']} to a Novato home, travel time San Rafael -> "
            "Novato unknown (no demo value on purpose)."
        )
    if s.attention.get("OAK"):
        out.append(f"  Oakland at risk: visit {s.attention['OAK']} needs charge before it leaves.")
    if s.away:  # the South Bay and Marin step ran; it also writes the pause history
        out.append(
            f"  Pause history: zone Lake Merritt of Oakland hub paused and lifted ({PAUSE_REASON.lower()})."
        )
    return out


def build(
    *, password: str, anchor: date | None = None, log: Callable[[str], None] = lambda _m: None
) -> Summary:
    """Build the demo workspace into an empty (or just wiped) database. The caller checks first."""
    if not password:
        raise DemoRefused(f"Set {PASSWORD_ENV} (see .env.example): the demo logins use it as their password.")
    from platform_core.middleware import DEMO_BUILD_MARKER  # noqa: PLC0415

    DeploymentSetting.objects.filter(key=DEMO_BUILD_MARKER).delete()  # maintenance page until we finish
    restart_sequences()
    summary = _Builder(password, anchor or workspace_today(), log).build()
    # The LAST write: the completion marker. Until it exists, a demo deployment (DEMO_WORKSPACE=1) serves
    # the "being refreshed" page, never a half-built one (platform_core.middleware.DemoRefreshMiddleware).
    DeploymentSetting.objects.update_or_create(
        key=DEMO_BUILD_MARKER, defaults={"value": timezone.now().isoformat()}
    )
    return summary
