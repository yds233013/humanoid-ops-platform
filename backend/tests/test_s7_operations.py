"""Slice 7 Engineer C: read models. docs/07a_production_guardrails_contract.md sections 4.6, 5.6, 6.2, 6.4, 8.

New inbox rules (PERIODIC_INSPECTION_OVERDUE / _DUE_SOON, PERIODIC_INTERVAL_UNCONFIGURED, ESCALATION_OVERDUE,
ESCALATION_DEADLINE_NOT_CONFIGURED, production blockers in production only, THRESHOLD_NOT_CONFIGURED for the two new
keys) firing and not firing at -1 us / = / +1 us; the X-4 oracle (set-based SQL vs the Python reference); the
production-blocker panel in both modes; the X-10 daily close-out (local day, carried over, every section, superset of
the inbox exceptions, missing / invalid time zone, future date, statement budget, no locks or writes, wording).
SYNTHETIC data only: every deadline, interval and time zone below is an arbitrary test value.
"""

import random
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.db import connection, transaction

from execution import escalation as esc
from execution import incidents as inc
from execution.models import EscalationDeadlineSet, EscalationDeadlineSetApproval, IncidentPolicy
from fleet.models import DeploymentSetting, PeriodicInspectionRule, PeriodicInspectionRuleWithdrawal, Robot
from operations import closeout as co
from operations import exceptions as ox
from operations import readmodels as rm
from operations.snapshot import read_snapshot
from readiness.models import DepartureEvent
from tests import operations_world as ow
from tests.fleet_world import set_setting

US = timedelta(microseconds=1)
SETTINGS_KEY = 64942988919
GRACE = {
    "ops_custody_verify_grace_min": "30",
    "ops_return_grace_min": "60",
    "ops_inspection_grace_min": "120",
    "ops_readiness_lookahead_min": "1",  # keep the readiness evaluator out of these tests
    "ops_periodic_due_lookahead_min": "1440",
    "ops_closeout_timezone": "America/Los_Angeles",
}
# SYNTHETIC deadlines (arbitrary test minutes, not company values)
DEADLINES = {
    "unclassified": [{"to_role": "safety_reviewer", "within_min": 30}],
    "low": [],
    "medium": [{"to_role": "ops_lead", "within_min": 10}, {"to_role": "founder", "within_min": 45}],
    "high": [{"to_role": "founder", "within_min": 5}],
}
FORBIDDEN_CLOSEOUT = re.compile(r"\b(signed off|closed out|all clear|complete|OK|cleared)\b")
WRITES_OR_LOCKS = re.compile(
    r"\b(FOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)|nextval|setval|set_config|pg_advisory\w*"
    r"|INSERT|UPDATE|DELETE|TRUNCATE|LOCK)\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ helpers
def configure(**overrides: str | None) -> None:
    for key, value in {**GRACE, **overrides}.items():
        if value is None:
            DeploymentSetting.objects.filter(key=key).delete()
        else:
            set_setting(key, value)


def db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def items(at: datetime) -> list[ox.OpsException]:
    return ox.open_exceptions(at).items


def find(at: datetime, code: str, ref: str | None = None) -> list[ox.OpsException]:
    return [x for x in items(at) if x.code == code and (ref is None or x.entity_ref == ref)]


def fires(at: datetime, code: str, ref: str) -> bool:
    return bool(find(at, code, ref))


def assert_boundary(code: str, ref: str, edge: datetime) -> None:
    assert [fires(edge - US, code, ref), fires(edge, code, ref), fires(edge + US, code, ref)] == [
        False,
        True,
        True,
    ], f"{code} {ref} at {edge}"


def periodic(robot: str, production: bool = False) -> tuple[str, datetime | None]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT state, due_at FROM readiness_periodic_inspection_status(%s, %s, clock_timestamp())",
            [Robot.objects.get(code=robot).pk, production],
        )
        return cur.fetchone()


def make_deadline_set(
    w, deadlines: dict[str, Any] | None = None, source: str = "synthetic_demo", approve=True
):
    pid = IncidentPolicy.objects.get(version=w.incident_policy)
    last = EscalationDeadlineSet.objects.filter(incident_policy=pid).order_by("-version_no").first()
    d = EscalationDeadlineSet.objects.create(
        incident_policy=pid,
        version_no=(last.version_no if last else 0) + 1,
        deadlines=deadlines or DEADLINES,
        source=source,
        notes="SYNTHETIC test deadlines",
        created_by=w.ops,
    )
    if approve:
        approve_deadline_set(w, d)
    return d


def approve_deadline_set(w, d: EscalationDeadlineSet) -> None:
    """safety + founder approvals; the approval trigger asserts the settings lock (contract 9.1), so both run in
    one transaction that took it first (a savepoint inside a test transaction)."""
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", [SETTINGS_KEY])
        EscalationDeadlineSetApproval.objects.create(deadline_set=d, slot="safety", approver=w.safety)
        EscalationDeadlineSetApproval.objects.create(deadline_set=d, slot="founder", approver=w.founder)


def reported_at(code: str) -> datetime:
    from execution.models import Incident

    return Incident.objects.get(code=code).reported_at


def sql_escalation(at: datetime) -> set[tuple[str, str, datetime, str]]:
    return {
        (x.code, x.entity_ref, x.since, x.owner_role)
        for x in items(at)
        if x.code in ("ESCALATION_OVERDUE", "ESCALATION_DEADLINE_NOT_CONFIGURED")
    }


def capture() -> tuple[list[str], Any]:
    seen: list[str] = []

    def wrapper(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    return seen, wrapper


# ================================================================== X-3 periodic inspection exceptions
@pytest.mark.django_db
def test_periodic_overdue_due_soon_and_boundaries():
    w = ow.ops_world("s7p_", robots=2, incident_policy=False)
    configure()
    ow.add_robot(w, "R-09", periodic=False)  # the shared worlds inspect every robot (X-3); this one never
    state, due = periodic("R-01")  # R-01 has the world's PASS inspection on T1 (synthetic, usable in demo)
    assert state == "due" and due is not None
    assert periodic("R-09") == ("never_inspected", None)
    # never inspected: overdue since the snapshot time, owner engineering_lead, priority 2
    at = db_now()
    (x,) = find(at, "PERIODIC_INSPECTION_OVERDUE", "R-09")
    assert (x.since, x.owner_role, x.priority, x.action_url, x.robot) == (
        at,
        "engineering_lead",
        2,
        "/fleet/R-09/",
        "R-09",
    )
    assert "no qualifying periodic inspection" in x.message
    # due soon: inclusive at due_at - lookahead; overdue from due_at (and due-soon stops there)
    assert_boundary("PERIODIC_INSPECTION_DUE_SOON", "R-01", due - timedelta(minutes=1440))
    (soon,) = find(due - US, "PERIODIC_INSPECTION_DUE_SOON", "R-01")
    assert (soon.since, soon.priority) == (due - timedelta(minutes=1440), 3)
    assert_boundary("PERIODIC_INSPECTION_OVERDUE", "R-01", due)
    assert not fires(due, "PERIODIC_INSPECTION_DUE_SOON", "R-01")
    (late,) = find(due, "PERIODIC_INSPECTION_OVERDUE", "R-01")
    assert late.since == due and "overdue since" in late.message
    # the lookahead is read from the settings, not hard-coded; missing -> its rule is off + one THRESHOLD item
    configure(ops_periodic_due_lookahead_min="60")
    assert not fires(due - timedelta(minutes=61), "PERIODIC_INSPECTION_DUE_SOON", "R-01")
    assert fires(due - timedelta(minutes=60), "PERIODIC_INSPECTION_DUE_SOON", "R-01")
    configure(ops_periodic_due_lookahead_min=None)
    assert not find(due - US, "PERIODIC_INSPECTION_DUE_SOON")
    (t,) = find(at, "THRESHOLD_NOT_CONFIGURED", "ops_periodic_due_lookahead_min")
    assert "PERIODIC_INSPECTION_DUE_SOON" in t.message and "missing" in t.message
    configure(ops_periodic_due_lookahead_min="0")
    assert (
        "not a whole number"
        in find(at, "THRESHOLD_NOT_CONFIGURED", "ops_periodic_due_lookahead_min")[0].message
    )
    # no interval rule is ever invented: nothing here is never-inspected-or-due without the SQL status
    assert not find(at, "PERIODIC_INTERVAL_UNCONFIGURED")
    assert w


@pytest.mark.django_db
def test_periodic_interval_unconfigured_withdrawn_rule_and_production():
    w = ow.ops_world("s7u_", robots=2, incident_policy=False)
    configure()
    at = db_now()
    assert not find(at, "PERIODIC_INTERVAL_UNCONFIGURED")
    rule = PeriodicInspectionRule.objects.filter(pattern__code="P-A").order_by("-version_no").first()
    PeriodicInspectionRuleWithdrawal.objects.create(rule=rule, withdrawn_by=w.lead, reason="SYNTHETIC test")
    at = db_now()
    (x,) = find(at, "PERIODIC_INTERVAL_UNCONFIGURED", "P-A")  # one per pattern in use, not one per robot
    assert (x.since, x.priority, x.owner_role, x.entity_type, x.action_url) == (
        at,
        1,
        "engineering_lead",
        "pattern",
        "/fleet/patterns/P-A/",
    )
    assert "demo mode" in x.message
    # unknown interval replaces the robot items (no fallback to an older rule, no invented due date)
    assert not find(at, "PERIODIC_INSPECTION_OVERDUE") and not find(at, "PERIODIC_INSPECTION_DUE_SOON")
    assert periodic("R-01")[0] == "unknown_interval"
    # a fresh synthetic rule: usable in demo again; refused in production (the source rule)
    from tests.fleet_world import make_periodic_rule

    make_periodic_rule(w, "P-A")
    assert not find(db_now(), "PERIODIC_INTERVAL_UNCONFIGURED")
    set_setting("demo_mode", "false")
    (p,) = find(db_now(), "PERIODIC_INTERVAL_UNCONFIGURED", "P-A")
    assert "production mode" in p.message


def _set_based(production: bool, at: datetime) -> dict[int, tuple[Any, ...]]:
    sql = (
        "WITH mode AS MATERIALIZED (SELECT %(prod)s::boolean AS production),\n"
        + ox.PERIODIC_STATUS_CTES.rstrip().rstrip(",")
        + "\nSELECT robot_id, state, due_at, inspection_id, rule_id FROM pstat"
    )
    with connection.cursor() as cur:
        cur.execute(sql, {"prod": production, "at": at})
        return {r[0]: tuple(r[1:]) for r in cur.fetchall()}


def _function(production: bool, at: datetime) -> dict[int, tuple[Any, ...]]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT r.id, f.state, f.due_at, f.inspection_id, f.rule_id FROM fleet_robot r "
            "CROSS JOIN LATERAL readiness_periodic_inspection_status(r.id, %s, %s) f WHERE r.lifecycle <> 'retired'",
            [production, at],
        )
        return {r[0]: tuple(r[1:]) for r in cur.fetchall()}


def _assert_periodic_oracle(label: str) -> int:
    n = 0
    now = db_now()
    _, due = periodic("R-01")
    points = [now, now + timedelta(days=400)] + ([due - US, due, due + US] if due else [])
    for production in (False, True):
        for at in points:
            assert _set_based(production, at) == _function(production, at), (label, production, at)
            n += 1
    return n


@pytest.mark.django_db
def test_set_based_periodic_status_equals_the_sql_function():
    """Oracle: PERIODIC_STATUS_CTES (inbox + overview) == A's readiness_periodic_inspection_status for every
    non-retired robot, both modes, at -1 us / = / +1 us of a due date, through every state change."""
    from fleet.models import InspectionTemplate, InspectionTemplateWithdrawal
    from planning import services as ps
    from planning.models import Qualification
    from tests.fleet_world import make_engineering_template, make_periodic_rule

    w = ow.ops_world("s7q_", robots=4, incident_policy=False)
    configure()
    checks = _assert_periodic_oracle("world")  # due (R-01) and never inspected (others)
    ow.depart_new(w, "R-02")  # a second robot with qualifying inspections
    checks += _assert_periodic_oracle("second robot")
    make_periodic_rule(w, "P-A", interval_min=60)  # a newer approved, shorter interval
    checks += _assert_periodic_oracle("shorter interval")
    # an engineering template approved for periodic use (production branch), and an inspection on it
    make_engineering_template(w, "P-A", version="E1", purposes=("periodic",))
    checks += _assert_periodic_oracle("engineering template")
    make_periodic_rule(w, "P-A", source="engineering_doc")
    checks += _assert_periodic_oracle("engineering rule")
    # a signer revoked: the inspection stops qualifying
    insp = (
        __import__("fleet.models", fromlist=["Inspection"])
        .Inspection.objects.filter(robot__code="R-01")
        .first()
    )
    q = Qualification.objects.filter(person__user=insp.performer, kind="readiness_inspector").first()
    ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    checks += _assert_periodic_oracle("revoked signer")
    # the synthetic template withdrawn
    t1 = InspectionTemplate.objects.get(pattern__code="P-A", version="T1")
    InspectionTemplateWithdrawal.objects.create(template=t1, withdrawn_by=w.lead, reason="SYNTHETIC")
    checks += _assert_periodic_oracle("template withdrawn")
    # the active rule withdrawn: unknown, no fallback
    rule = PeriodicInspectionRule.objects.filter(pattern__code="P-A").order_by("-version_no").first()
    PeriodicInspectionRuleWithdrawal.objects.create(rule=rule, withdrawn_by=w.lead, reason="SYNTHETIC")
    checks += _assert_periodic_oracle("rule withdrawn")
    assert checks >= 50


# ================================================================== X-4 escalation deadlines
@pytest.mark.django_db
def test_deadline_not_configured_then_overdue_boundaries_and_clearing():
    w = ow.ops_world("s7e_", robots=2)
    configure()
    code = ow.report(w, "R-01")
    at = db_now()
    (nc,) = find(at, "ESCALATION_DEADLINE_NOT_CONFIGURED")
    assert (nc.entity_ref, nc.since, nc.owner_role, nc.priority, nc.action_url) == (
        "deadline-set",
        at,
        "safety_reviewer",
        1,
        "/execution/escalation-deadlines/",
    )
    assert not find(at, "ESCALATION_OVERDUE")
    # an unapproved set is not usable
    d = make_deadline_set(w, approve=False)
    assert find(db_now(), "ESCALATION_DEADLINE_NOT_CONFIGURED", "deadline-set")
    approve_deadline_set(w, d)
    assert not find(db_now(), "ESCALATION_DEADLINE_NOT_CONFIGURED")
    # unclassified -> the "unclassified" key: safety_reviewer within 30 min of the report (basis reported_at)
    ref = f"{code}/safety_reviewer"
    edge = reported_at(code) + timedelta(minutes=30)
    assert_boundary("ESCALATION_OVERDUE", ref, edge)
    (x,) = find(edge, "ESCALATION_OVERDUE", ref)
    assert (x.since, x.owner_role, x.priority, x.entity_type, x.action_url, x.robot) == (
        edge,
        "safety_reviewer",
        1,
        "incident_role",
        f"/execution/incidents/{code}/",
        "R-01",
    )
    assert "no escalation to safety_reviewer recorded" in x.message
    assert x.context["within_min"] == 30 and x.context["level"] == "unclassified"
    # escalated to the wrong role: still overdue; to the right role: message changes, still overdue until ack
    esc.escalate_incident(w.ops, incident_code=code, to_role="founder", requested_action="SYNTHETIC")
    assert fires(edge, "ESCALATION_OVERDUE", ref)
    e = esc.escalate_incident(
        w.ops, incident_code=code, to_role="safety_reviewer", requested_action="SYNTHETIC"
    )
    (x,) = find(edge, "ESCALATION_OVERDUE", ref)
    assert "escalated to safety_reviewer at" in x.message and "not acknowledged" in x.message
    # an acknowledgment (even after the deadline) clears it; lateness that was resolved is history
    esc.acknowledge_escalation(w.safety, escalation_id=e["id"])
    assert not find(edge + timedelta(days=30), "ESCALATION_OVERDUE", ref)


@pytest.mark.django_db
def test_deadline_levels_empty_list_old_policy_and_production():
    w = ow.ops_world("s7l_", robots=3)
    configure()
    make_deadline_set(w)
    a, b = ow.report(w, "R-01"), ow.report(w, "R-02")
    inc.decide_severity(w.lead, incident_code=a, level="medium", reason="SYNTHETIC")
    inc.decide_severity(w.lead, incident_code=b, level="low", reason="SYNTHETIC")
    far = reported_at(b) + timedelta(days=365)
    refs = {x.entity_ref for x in find(far, "ESCALATION_OVERDUE")}
    # a raised severity keeps the original basis (reported_at): medium = ops_lead 10 min, founder 45 min
    assert refs == {f"{a}/ops_lead", f"{a}/founder"}, refs  # low = [] never fires
    assert_boundary("ESCALATION_OVERDUE", f"{a}/ops_lead", reported_at(a) + timedelta(minutes=10))
    assert find(far, "ESCALATION_OVERDUE", f"{a}/founder")[0].owner_role == "founder"
    # a decision made under an older incident policy version: that incident is unconfigured
    w.incident_policy = ow.make_incident_policy(w)  # v2 becomes the active policy
    assert [x.entity_ref for x in find(far, "ESCALATION_DEADLINE_NOT_CONFIGURED")] == ["deadline-set"]
    make_deadline_set(w)  # a set for v2
    c = ow.report(w, "R-03")
    nc = {x.entity_ref for x in find(far, "ESCALATION_DEADLINE_NOT_CONFIGURED")}
    assert nc == {a, b}, nc  # decided under v1; c is unclassified and covered
    assert {x.entity_ref for x in find(far, "ESCALATION_OVERDUE")} == {f"{c}/safety_reviewer"}
    # production: a synthetic set is not usable (owner_approved only)
    set_setting("demo_mode", "false")
    assert [x.entity_ref for x in find(far, "ESCALATION_DEADLINE_NOT_CONFIGURED")] == ["deadline-set"]
    assert not find(far, "ESCALATION_OVERDUE")
    assert "owner approved" in find(far, "ESCALATION_DEADLINE_NOT_CONFIGURED")[0].message


@pytest.mark.django_db
def test_escalation_oracle_sql_equals_python_reference():
    """Set-based SQL (RULES_RELATION_SQL) == ox.reference_escalation_overdue, over randomized incidents and times,
    before and after the deadline set exists, in both modes."""
    w = ow.ops_world("s7o_", robots=4)
    configure()
    rng = random.Random(7)
    codes = [ow.report(w, f"R-0{n}") for n in (1, 2, 3, 4)]
    now = db_now()
    times = [now, now + timedelta(minutes=7), now + timedelta(minutes=31), now + timedelta(days=2)]
    for at in times:
        assert sql_escalation(at) == ox.reference_escalation_overdue(at)
    make_deadline_set(w)
    for code in codes:
        ow.evolve_incident(w, code, rng)
    deadlines = sorted({reported_at(c) + timedelta(minutes=m) for c in codes for m in (5, 10, 30, 45)})
    checked = 0
    for mode in ("true", "false"):
        set_setting("demo_mode", mode)
        for t in [*times, *(d + off for d in deadlines for off in (-US, timedelta(0)))]:
            assert sql_escalation(t) == ox.reference_escalation_overdue(t), (mode, t)
            checked += 1
    assert checked >= 40
    # uniqueness of (code, entity_ref) and the total order
    page = ox.open_exceptions(now + timedelta(days=2))
    keys = [(x.code, x.entity_ref) for x in page.items]
    assert len(keys) == len(set(keys))
    assert [x.sort_key for x in page.items] == sorted(x.sort_key for x in page.items)


def test_no_notification_code_path_exists():
    """X-4 is read-only: the rules and the reference read rows and never send, page, mail or queue anything."""
    import inspect

    src = inspect.getsource(ox) + inspect.getsource(rm) + inspect.getsource(co)
    for word in ("send_mail", "smtplib", "outbox", "notify(", "webhook", "requests.", "urllib"):
        assert word not in src, word


# ================================================================== production blockers (contract 5.6)
@pytest.mark.django_db
def test_production_blockers_panel_both_modes_and_inbox_in_production_only():
    w = ow.ops_world("s7b_", robots=2)
    configure()
    make_deadline_set(w)
    at = db_now()
    panel = rm.production_blockers(at)
    assert not panel.production and panel.heading == "Would block in production"
    codes = {(b.code, b.entity_ref) for b in panel.items}
    with connection.cursor() as cur:
        cur.execute("SELECT code, entity_ref FROM operations_production_blockers")
        assert codes == set(cur.fetchall())
    assert {
        ("HOLD_POLICY_NOT_APPROVED", "hold-policy"),
        ("INSPECTION_TEMPLATE_NOT_APPROVED", "P-A"),
        ("PERIODIC_INTERVAL_NOT_APPROVED", "P-A"),
        ("ESCALATION_DEADLINES_NOT_APPROVED", "deadline-set"),
    } <= codes
    for b in panel.items:
        assert b.url.startswith("/") and b.label
    # demo: not inbox items (labelled on the panel only)
    assert not [x for x in items(at) if x.code in ox.BLOCKER_CODES]
    # an unreleasable hold appears in both modes
    hold = ow.place_hold(w, "robot", "R-02", "manual")
    panel = rm.production_blockers(db_now())
    assert ("HOLD_UNRELEASABLE", hold) in {(b.code, b.entity_ref) for b in panel.items}
    assert [b.url for b in panel.items if b.entity_ref == hold] == ["/fleet/R-02/"]
    # production: the same rows are also priority-1 inbox items, owner and link per code
    set_setting("demo_mode", "false")
    at = db_now()
    panel = rm.production_blockers(at)
    assert panel.production and panel.heading == "Production blockers"
    inbox = {(x.code, x.entity_ref): x for x in items(at) if x.code in ox.BLOCKER_CODES}
    assert set(inbox) == {(b.code, b.entity_ref) for b in panel.items}
    for (code, _), x in inbox.items():
        assert x.priority == 1 and x.since == at and not x.resolvable
        assert x.owner_role == (
            "safety_reviewer" if code == "ESCALATION_DEADLINES_NOT_APPROVED" else "engineering_lead"
        )
        assert x.message.startswith("Production blocker: ")
    assert inbox[("HOLD_UNRELEASABLE", hold)].action_url == "/fleet/R-02/"
    assert inbox[("HOLD_POLICY_NOT_APPROVED", "hold-policy")].action_url == "/fleet/hold-policies/"
    # the metrics count reads the same relation (no drift between pages)
    assert ox.count_operational_exceptions(at) == len(ox.open_exceptions(at, include_readiness=False).items)


@pytest.mark.django_db
def test_fleet_overview_periodic_field():
    w = ow.ops_world("s7f_", robots=2, incident_policy=False)
    configure()
    ow.add_robot(w, "R-09", periodic=False)  # the shared worlds inspect every robot (X-3); this one never
    with connection.cursor() as cur:
        cur.execute("SELECT now()")
        at = cur.fetchone()[0]
    rows = {r.robot: r for r in rm.fleet_overview(at)}
    assert rows["R-01"].periodic_inspection.state == "due"
    assert rows["R-01"].periodic_inspection.due_at == periodic("R-01")[1]
    assert rows["R-01"].periodic_inspection.rule_source == "synthetic_demo"
    assert rows["R-09"].periodic_inspection.state == "never_inspected"
    assert rows["R-09"].periodic_inspection.due_at is None


# ================================================================== time-zone setting
@pytest.mark.django_db
@pytest.mark.parametrize(
    "value,state",
    [
        (None, "missing"),
        ("Mars/Olympus_Mons", "invalid"),
        ("", "invalid"),
        (" America/Los_Angeles", "invalid"),
        ("../etc/passwd", "invalid"),
        ("America/Los_Angeles", "valid"),
        ("UTC", "valid"),
    ],
)
def test_closeout_timezone_threshold_item(value, state):
    configure(ops_closeout_timezone=value)
    _, _, tz = ox.read_settings()
    assert tz.state == state
    at = db_now()
    found = [
        x
        for x in ox._threshold_items(at, *ox.read_thresholds(), tz)
        if x.entity_ref == ox.CLOSEOUT_TIMEZONE_KEY
    ]
    if state == "valid":
        assert not found and tz.name == value
    else:
        (x,) = found
        assert x.since == at and x.action_url == "/ops/closeout/" and tz.name is None
        assert ("missing" if state == "missing" else "not a time zone name") in x.message


# ================================================================== X-10 close-out
def closeout(at: datetime | None = None, day: str | None = None) -> co.Closeout:
    return co.closeout(at or db_now(), day)


def runs(rows: list[co.CloseoutRun]) -> list[str]:
    return [r.run for r in rows]


def local(dt: datetime, tz: str = "America/Los_Angeles") -> date:
    return dt.astimezone(ZoneInfo(tz)).date()


@pytest.mark.django_db
def test_closeout_sections_through_a_run_lifecycle():
    w = ow.ops_world("s7c_", robots=3, incident_policy=False)
    configure()
    r2, r3 = ow.depart_new(w, "R-02"), ow.depart_new(w, "R-03")
    c = closeout()
    assert c.dated and c.day == local(db_now()) and c.timezone == "America/Los_Angeles"
    assert runs(c.departed_open) == [r2.code, r3.code] and not c.carried_over
    assert runs(c.custody_unverified) == [r2.code, r3.code]
    assert (c.departed_count, c.departed_closed_count) == (2, 0)
    assert not c.nothing_outstanding and "not a sign-off" in c.summary
    ow.advance(w, r2, "at_site")  # both include the crew verification of the departure assignment
    ow.advance(w, r3, "at_hub")
    c = closeout()
    assert runs(c.custody_unverified) == []
    by = {r.run: r for r in c.departed_open}
    assert by[r2.code].post_visit == "robot not back at hub (at the site)"
    assert by[r3.code].post_visit == "post-visit inspection outstanding"
    assert by[r3.code].reconciliation == "no reconciliation recorded"
    assert runs(c.reconciliation_unresolved) == [r3.code]
    insp = w.cmd.inspect(r3)
    c = closeout()
    assert r3.code not in runs(c.post_visit_outstanding)
    w.cmd.reconcile(r3, inspection=insp, discrepancies=["SYNTHETIC missing kit"])
    c = closeout()
    by = {r.run: r for r in c.departed_open}
    assert by[r3.code].reconciliation == "reconciliation unresolved"
    w.cmd.reconcile(r3, inspection=insp)
    assert r3.code not in runs(closeout().reconciliation_unresolved)
    w.cmd.close(r3)
    c = closeout()
    assert runs(c.departed_open) == [r2.code] and (c.departed_count, c.departed_closed_count) == (2, 1)


@pytest.mark.django_db
def test_closeout_local_day_carried_over_future_and_zones():
    w = ow.ops_world("s7d_", robots=2, incident_policy=False)
    configure()
    run = ow.depart_new(w, "R-02")
    dep = DepartureEvent.objects.get(visit_id=run.visit_id).departed_at
    now = db_now()
    # carried over: the snapshot two days later, same open run, departed before D
    later = closeout(now + timedelta(days=2))
    assert later.day == local(now + timedelta(days=2))
    assert runs(later.carried_over) == [run.code] and not later.departed_open
    assert later.carried_over[0].carried_over and (later.departed_count, later.departed_closed_count) == (
        0,
        0,
    )
    # an explicit earlier day: the run departed after it is in neither section
    before = closeout(now, (local(dep) - timedelta(days=1)).isoformat())
    assert not before.departed_open and not before.carried_over
    # future day refused; malformed refused
    with pytest.raises(co.CloseoutDateError, match="future"):
        closeout(now, (local(now) + timedelta(days=1)).isoformat())
    for bad in ("2026-13-01", "20261101", "yesterday", "2026-02-30"):
        with pytest.raises(co.CloseoutDateError):
            closeout(now, bad)
    # membership follows the configured zone (+14 and -11 put one instant on different local dates)
    for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "UTC"):
        configure(ops_closeout_timezone=zone)
        c = closeout(now, local(dep, zone).isoformat())
        assert runs(c.departed_open) == [run.code], zone
        assert c.departed_count == 1
        assert not closeout(now, (local(dep, zone) - timedelta(days=1)).isoformat()).departed_open


@pytest.mark.django_db
@pytest.mark.parametrize(
    "instant",
    [
        "2026-03-08T09:59:59.999999+00:00",  # 01:59:59 PST, before the spring-forward gap
        "2026-03-08T10:00:00+00:00",  # 03:00 PDT
        "2026-11-01T08:59:59.999999+00:00",  # 01:59 PDT (first 1 am)
        "2026-11-01T09:00:00+00:00",  # 01:00 PST (repeated hour)
        "2026-11-02T07:59:59.999999+00:00",  # 23:59:59 PST on Nov 1
        "2026-11-02T08:00:00+00:00",  # midnight PST Nov 2
    ],
)
def test_postgres_and_python_agree_on_local_day_across_dst_and_midnight(instant):
    """The day of the snapshot is computed in Python, membership in PostgreSQL: both must agree."""
    t = datetime.fromisoformat(instant)
    with connection.cursor() as cur:
        cur.execute("SELECT (%s::timestamptz AT TIME ZONE 'America/Los_Angeles')::date", [t])
        assert cur.fetchone()[0] == local(t)


@pytest.mark.django_db
@pytest.mark.parametrize("value", [None, "Not/AZone"])
def test_closeout_without_a_valid_timezone_is_one_undated_list(value):
    w = ow.ops_world("s7z_", robots=3, incident_policy=False)
    configure(ops_closeout_timezone=value)
    r2, r3 = ow.depart_new(w, "R-02"), ow.depart_new(w, "R-03")
    ow.advance(w, r3, "at_hub")
    for day in (None, "2001-01-01", "2999-01-01", "garbage"):  # the date is accepted but ignored
        c = closeout(db_now() + timedelta(days=3), day)
        assert not c.dated and c.day is None and c.timezone is None
        assert c.timezone_state == ("missing" if value is None else "invalid")
        assert runs(c.undated_open) == [r2.code, r3.code]
        assert not c.departed_open and not c.carried_over  # no "departed on D", no carried-over label
        assert not any(r.carried_over or r.local_day for r in c.undated_open)
        assert c.departed_count is None and c.departed_closed_count is None  # no local-day counts
        assert c.date_ignored == bool(day)
        assert runs(c.post_visit_outstanding) == [r2.code, r3.code]
        assert [r.post_visit for r in c.post_visit_outstanding] == [
            "robot not back at hub (in the vehicle)",
            "post-visit inspection outstanding",
        ]
        assert runs(c.custody_unverified) == [r2.code]  # r3's assignment was verified on the way
        assert "not a sign-off" in c.summary and "UTC" in c.summary


@pytest.mark.django_db
def test_closeout_is_a_superset_of_the_inbox_run_exceptions():
    w = ow.ops_world("s7s_", robots=4, incident_policy=False)
    configure()
    a, b, c3 = ow.depart_new(w, "R-02"), ow.depart_new(w, "R-03"), ow.depart_new(w, "R-04")
    ow.advance(w, b, "at_hub")
    ow.advance(w, c3, "inspected")
    w.cmd.reconcile(c3, inspection=c3._inspection, discrepancies=["SYNTHETIC"])
    at = db_now() + timedelta(days=1)
    exc = items(at)
    c = closeout(at)
    listed = {
        "CUSTODY_UNVERIFIED": set(runs(c.custody_unverified)),
        "POST_VISIT_INSPECTION_MISSING": set(runs(c.post_visit_outstanding)),
        "RECONCILIATION_UNRESOLVED": set(runs(c.reconciliation_unresolved)),
    }
    seen = 0
    for x in exc:
        if x.code in listed:
            assert x.entity_ref in listed[x.code], (x.code, x.entity_ref, listed)
            seen += 1
    assert seen >= 3
    assert {a.code, b.code, c3.code} <= set(runs(c.open_runs))


@pytest.mark.django_db(transaction=True)
def test_closeout_budget_snapshot_and_no_writes_or_locks():
    w = ow.ops_world("s7k_", robots=3, incident_policy=False)
    configure()
    ow.depart_new(w, "R-02")
    seen, wrapper = capture()
    with read_snapshot() as at, connection.execute_wrapper(wrapper):
        dated = co.closeout(at)
    assert len(seen) <= 6 and len(seen) == 3, seen  # settings + open runs + counts
    configure(ops_closeout_timezone=None)
    seen2, wrapper = capture()
    with read_snapshot() as at, connection.execute_wrapper(wrapper):
        undated = co.closeout(at)
    assert len(seen2) == 2, seen2  # settings + open runs
    for sql in seen + seen2:
        assert not WRITES_OR_LOCKS.search(sql), sql
    assert dated.dated and not undated.dated
    # snapshot consistency: a departure committed while the snapshot is open is not seen
    with read_snapshot() as at:
        first = runs(co.closeout(at).open_runs)
        import threading

        def depart() -> None:
            try:
                ow.depart_new(w, "R-03")
            finally:
                connection.close()

        t = threading.Thread(target=depart)
        t.start()
        t.join()
        assert runs(co.closeout(at).open_runs) == first
    assert len(runs(co.closeout(db_now()).open_runs)) == len(first) + 1


@pytest.mark.django_db
def test_closeout_wording_never_claims_a_sign_off():
    w = ow.ops_world("s7w_", robots=2, incident_policy=False)
    configure()
    c = closeout()
    assert c.nothing_outstanding
    assert c.summary.startswith(
        f"No outstanding items found for runs departed on {c.day.isoformat()}, as of "
    )
    assert c.summary.endswith("This page is not a sign-off; the platform has no close-out record.")
    ow.depart_new(w, "R-02")
    for text in (c.summary, closeout().summary, co.NOT_A_SIGN_OFF, co.AS_OF_NOTE, co.UNDATED_HEADING):
        assert not FORBIDDEN_CLOSEOUT.search(text), text


@pytest.mark.django_db
def test_maximum_deadline_configuration_is_not_truncated():
    """EM ruling on C's LIMIT 16 (defensive bound on the deadline entries per level): the largest configuration
    the database accepts - every known role (the trigger allows each role once per level) at every level - yields
    one exception per unacknowledged role, no duplicates, and equals the Python reference at every edge. Repeated
    escalations to one role: the first one is reported and any acknowledgment clears the role."""
    from execution.policy import KNOWN_ROLES

    w = ow.ops_world("s7m_", robots=2)
    configure()
    code = ow.report(w, "R-01")
    roles = sorted(KNOWN_ROLES)
    assert len(roles) == 6 < 16  # the LIMIT can never cut a valid level
    full = {
        lvl: [{"to_role": r, "within_min": 5 * (i + 1)} for i, r in enumerate(roles)] for lvl in DEADLINES
    }
    make_deadline_set(w, deadlines=full)
    esc.escalate_incident(w.ops, incident_code=code, to_role="founder", requested_action="SYNTHETIC")
    esc.escalate_incident(w.ops, incident_code=code, to_role="founder", requested_action="SYNTHETIC")
    esc.escalate_incident(w.ops, incident_code=code, to_role="safety_reviewer", requested_action="SYNTHETIC")
    second = esc.escalate_incident(
        w.ops, incident_code=code, to_role="safety_reviewer", requested_action="SYNTHETIC"
    )
    esc.acknowledge_escalation(w.safety, escalation_id=second["id"])  # only the later one is acknowledged
    late = reported_at(code) + timedelta(days=1)
    got = [x for x in find(late, "ESCALATION_OVERDUE") if x.entity_ref.startswith(f"{code}/")]
    refs = [x.entity_ref for x in got]
    assert sorted(refs) == sorted(f"{code}/{r}" for r in roles if r != "safety_reviewer")
    assert len(refs) == len(set(refs))
    (fx,) = [x for x in got if x.entity_ref == f"{code}/founder"]
    assert fx.context["escalated"] is True and "not acknowledged" in fx.message
    edges = [reported_at(code) + timedelta(minutes=5 * (i + 1)) for i in range(len(roles))]
    for t in [*(e + off for e in edges for off in (-US, timedelta(0))), late]:
        assert sql_escalation(t) == ox.reference_escalation_overdue(t), t
