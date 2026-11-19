"""Slice 7 Python/SQL parity oracles (contract 07a section 11.6). Where a rule exists in both Python (explains)
and PostgreSQL (decides), random SYNTHETIC worlds plus an edge-case table prove they agree:

    template usability        fleet.services.template_usable         vs fleet_template_usable
    periodic status           fleet.services.periodic_inspection_status vs readiness_periodic_inspection_status
    covered signer            planning.services.inspection_signer_problem vs planning_inspection_signer_problem
    hold release status       fleet.services.hold_release_problem    vs fleet_hold_release_problem
    binding / active policy   binding_policy / active_hold_policy    vs fleet_hold_binding_policy_id / ..._active_...
    floor, template items, escalation deadline rules (validation mirrors)
    periodic eligibility reason vs the same reason derived in SQL (the proposed operations view expression, CR-1)
"""

import random
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection, transaction
from psycopg.types.json import Jsonb

from execution import escalation_deadlines as ed
from execution import policy as pol
from execution.models import IncidentPolicy
from fleet import services as fs
from fleet.management.commands.seed_fleet import DRAFT_POLICY_RULES, TEMPLATE_ITEMS
from fleet.models import (
    Hold,
    HoldPolicy,
    HoldPolicyApproval,
    HoldPolicyWithdrawal,
    Inspection,
    InspectionTemplate,
    InspectionTemplateApproval,
    InspectionTemplateWithdrawal,
    PeriodicInspectionRule,
    PeriodicInspectionRuleApproval,
    PeriodicInspectionRuleWithdrawal,
    Robot,
)
from planning import services as ps
from planning.models import Qualification
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.test_s7_fleet import floor_rules
from tests.test_s7_readiness import build_b7

pytestmark = pytest.mark.django_db
SEED = 20261119


class _Rollback(Exception):  # noqa: N818 - control flow: undo one random world
    pass


@pytest.fixture
def w(monkeypatch):
    world = build_b7(monkeypatch, "o7_")
    world.fo = make_user("o7_fo", "field_operator")  # may sign, never an inspector
    ps.register_person(world.ops, username=world.fo.username, code="P-9")
    world.ghost = make_user("o7_ghost", "field_operator")  # may sign, has no Person at all
    return world


def _row(sql: str, params: list[Any]) -> tuple[Any, ...]:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return tuple(cur.fetchone())


def _one(sql: str, params: list[Any] | None = None) -> Any:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()[0]


def _template(w: Any, version: str, rng: random.Random) -> InspectionTemplate:
    t = InspectionTemplate.objects.create(
        pattern_id=Robot.objects.get(code="R-01").current_manifest.pattern_id,
        version=version,
        items=TEMPLATE_ITEMS,
        source="engineering_doc",
        procedure_ref="DOC-SYNTHETIC-TEST",
        created_by=w.eng,
    )
    for purpose in ("pre_departure", "periodic"):
        for slot, who in (("engineering", w.lead), ("safety", w.safety)):
            if rng.random() < 0.6:
                InspectionTemplateApproval.objects.create(
                    template=t, purpose=purpose, slot=slot, approver=who
                )
    if rng.random() < 0.15:
        InspectionTemplateWithdrawal.objects.create(template=t, withdrawn_by=w.safety, reason="SYNTHETIC")
    return t


# ================================================================== template usability
def test_template_usability_oracle(w):
    rng = random.Random(SEED)
    templates = [InspectionTemplate.objects.get(pattern__code="P-A", version="T1")]
    templates += [_template(w, f"O{n}", rng) for n in range(30)]
    for t in templates:
        for purpose in ("pre_departure", "periodic", "unknown"):
            for production in (True, False):
                py = fs.template_usable(t, purpose, production=production)
                sql = _one("SELECT fleet_template_usable(%s, %s, %s)", [t.pk, purpose, production])
                assert py is sql, (t.version, purpose, production)
    assert _one("SELECT fleet_template_usable(-1, 'periodic', false)") is False
    assert fs.template_usable(None, "periodic", production=False) is False


# ================================================================== periodic status + covered signer
def _sql_status(robot: Robot, production: bool, at: Any) -> tuple[Any, ...]:
    return _row(
        "SELECT state, due_at, inspection_id, rule_id FROM readiness_periodic_inspection_status(%s, %s, %s)",
        [robot.pk, production, at],
    )


def _compare_status(robot: Robot, production: bool, at: Any) -> fs.PeriodicStatus:
    py = fs.periodic_inspection_status(robot, at, production=production)
    assert (py.state, py.due_at, py.inspection_id, py.rule_id) == _sql_status(robot, production, at), (
        robot.code,
        production,
        at,
    )
    return py


def _random_world(w: Any, rng: random.Random, n: int) -> None:
    pattern = Robot.objects.get(code="R-01").current_manifest.pattern
    # rules: maybe a new version (synthetic / engineering_doc, known / unknown interval, approved or not)
    if rng.random() < 0.6:
        last = PeriodicInspectionRule.objects.filter(pattern=pattern).order_by("-version_no")[0]
        src = rng.choice(["synthetic_demo", "engineering_doc"])
        r = PeriodicInspectionRule.objects.create(
            pattern=pattern,
            version_no=last.version_no + 1,
            interval_min=rng.choice([None, rng.randint(1, 60), rng.randint(60, 200_000)]),
            source=src,
            procedure_ref="DOC-SYNTHETIC-TEST" if src == "engineering_doc" else "",
            created_by=w.eng,
        )
        for slot, who in (("engineering", w.lead), ("safety", w.safety)):
            if rng.random() < 0.8:
                PeriodicInspectionRuleApproval.objects.create(rule=r, slot=slot, approver=who)
        if rng.random() < 0.2:
            PeriodicInspectionRuleWithdrawal.objects.create(rule=r, withdrawn_by=w.lead, reason="SYNTHETIC")
    # templates and inspections with random signers
    templates = ["T1"]
    if rng.random() < 0.5:
        templates.append(_template(w, f"R{n}", rng).version)
    signers = [w.ops, w.field, w.field2, w.fo, w.ghost]
    for _ in range(rng.randint(0, 3)):
        a, b = rng.sample(signers, 2)
        fs.submit_inspection(
            w.ops,
            robot_code=rng.choice(w.robots),
            template_version=rng.choice(templates),
            answers=dict(w.all_pass) if rng.random() < 0.85 else {**w.all_pass, "I1": "fail"},
            performer=a.username,
            verifier=b.username if rng.random() < 0.9 else None,
        )
    if rng.random() < 0.25:
        q = rng.choice(
            list(Qualification.objects.filter(kind="readiness_inspector", revoked_at__isnull=True))
        )
        ps.revoke_qualification(w.ops, qualification_id=q.pk, reason="SYNTHETIC")
    if rng.random() < 0.1:
        fs.record_manifest(
            w.eng,
            robot_code=rng.choice(w.robots),
            pattern_code="P-A",
            autonomy_version=f"a{n}",
            safety_config_hash=f"{n:064d}",
        )


def test_periodic_status_and_covered_signer_oracle_on_random_worlds(w):
    rng = random.Random(SEED)
    now = _one("SELECT clock_timestamp()")
    compared = 0
    for n in range(50):
        try:
            with transaction.atomic():
                _random_world(w, rng, n)
                for code in w.robots:
                    robot = Robot.objects.get(code=code)
                    for production in (True, False):
                        st = _compare_status(
                            robot, production, now + timedelta(minutes=rng.randint(-60, 300_000))
                        )
                        if st.due_at is not None:  # the boundary table at every due date
                            for us in (-1, 0, 1):
                                _compare_status(robot, production, st.due_at + timedelta(microseconds=us))
                        compared += 1
                for i in Inspection.objects.all():
                    assert ps.inspection_signer_problem(i) == _one(
                        "SELECT planning_inspection_signer_problem(%s)", [i.pk]
                    ), i.code
                raise _Rollback
        except _Rollback:
            pass
    assert compared == 50 * 3 * 2


def test_periodic_status_edge_cases(w):
    robot = Robot.objects.get(code="R-01")
    st = _compare_status(robot, False, _one("SELECT clock_timestamp()"))
    assert st.state == "due"
    assert _compare_status(robot, True, st.due_at).state == "unknown_interval"  # synthetic rule in production
    for us, state in ((-1, "due"), (0, "overdue"), (1, "overdue")):
        assert _compare_status(robot, False, st.due_at + timedelta(microseconds=us)).state == state
    unmanifested = Robot.objects.create(code="R-99", serial="o7-99", registered_by=w.ops)
    assert _compare_status(unmanifested, False, st.due_at).state == "unknown_interval"


# ================================================================== periodic eligibility reason (CR-1 support)
SQL_REASON = """
SELECT CASE s.state
  WHEN 'unknown_interval' THEN 'Periodic inspection interval unknown for configuration ' || p.code
  WHEN 'never_inspected' THEN 'No qualifying periodic inspection on the current configuration'
  WHEN 'overdue' THEN 'Periodic inspection overdue since '
                      || to_char(s.due_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' UTC'
END
FROM fleet_robot r
JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
JOIN fleet_manifestpattern p ON p.id = m.pattern_id
CROSS JOIN LATERAL readiness_periodic_inspection_status(r.id, %s, %s) s
WHERE r.id = %s
"""


def test_periodic_eligibility_reason_matches_a_sql_expression(w):
    rng = random.Random(SEED + 1)
    now = _one("SELECT clock_timestamp()")
    for n in range(12):
        try:
            with transaction.atomic():
                _random_world(w, rng, 100 + n)
                for code in w.robots:
                    robot = Robot.objects.get(code=code)
                    for production in (True, False):
                        at = now + timedelta(minutes=rng.randint(-60, 300_000))
                        py = fs.periodic_eligibility_reason(
                            fs.periodic_inspection_status(robot, at, production=production)
                        )
                        assert py == _one(SQL_REASON, [production, at, robot.pk]), (code, production)
                raise _Rollback
        except _Rollback:
            pass


# ================================================================== hold policies: binding, active, release
def _random_policy_state(w: Any, rng: random.Random) -> None:
    for _ in range(rng.randint(0, 3)):
        nxt = HoldPolicy.objects.order_by("-version")[0].version + 1
        procedure = rng.random() < 0.5
        p = HoldPolicy.objects.create(
            version=nxt,
            rules=floor_rules() if procedure else DRAFT_POLICY_RULES,
            source="approved_procedure" if procedure else "synthetic_demo",
            procedure_ref="DOC-SYNTHETIC-TEST" if procedure else "",
            created_by=w.eng,
        )
        slots = [("engineering", w.lead), ("safety", w.safety)] + (
            [("founder", w.founder)] if procedure else []
        )
        for slot, who in slots:
            if rng.random() < 0.75:
                HoldPolicyApproval.objects.create(policy=p, slot=slot, approver=who)
        if rng.random() < 0.2:
            HoldPolicyWithdrawal.objects.create(policy=p, withdrawn_by=w.lead, reason="SYNTHETIC")


def test_binding_and_active_policy_oracle(w):
    rng = random.Random(SEED + 2)
    for _ in range(15):
        try:
            with transaction.atomic():
                _random_policy_state(w, rng)
                active = fs.active_hold_policy()
                assert (active.pk if active else None) == _one("SELECT fleet_active_hold_policy_id()")
                binding = fs.binding_policy()
                assert (binding.pk if binding else None) == _one("SELECT fleet_hold_binding_policy_id()")
                for p in HoldPolicy.objects.all():
                    assert fs.policy_production_usable(p) is _one(
                        "SELECT fleet_policy_production_usable(%s)", [p.pk]
                    )
                    assert fs.hold_policy_approved(p) is _one("SELECT fleet_hold_policy_approved(%s)", [p.pk])
                    for demo, drafts in (("true", "true"), ("true", "false"), ("false", "true")):
                        set_setting("demo_mode", demo)
                        set_setting("allow_draft_policies", drafts)
                        assert fs.policy_usable(p) is _one("SELECT fleet_policy_usable(%s)", [p.pk])
                raise _Rollback
        except _Rollback:
            pass


def _sql_problem(h: Hold) -> Any:
    return _one("SELECT fleet_hold_release_problem(h) FROM fleet_hold h WHERE h.id = %s", [h.pk])


def test_hold_release_status_oracle(w):
    """Random policies, holds, approvals (some on an old manifest, some withdrawn), rebases, newer PASS
    inspections on synthetic / approved templates, in both modes with and without allow_draft_policies."""
    rng = random.Random(SEED + 3)
    eng_doc = _template(w, "HE", random.Random(1))
    for slot, who in (("engineering", w.lead), ("safety", w.safety)):
        if not InspectionTemplateApproval.objects.filter(
            template=eng_doc, purpose="periodic", slot=slot
        ).exists():
            InspectionTemplateApproval.objects.create(
                template=eng_doc, purpose="periodic", slot=slot, approver=who
            )
    approvers = {
        "engineering_lead": w.lead,
        "engineer": w.eng2,
        "safety_reviewer": w.safety,
        "founder": w.founder,
        "ops_lead": w.ops,
    }
    compared = 0
    for n in range(25):
        try:
            with transaction.atomic():
                _random_policy_state(w, rng)
                robot = rng.choice(w.robots)
                source = rng.choice(
                    ["inspection", "maintenance", "engineering", "manual", "release_withdrawn"]
                )
                code = fs.place_hold(
                    w.ops, scope="robot", target=robot, source_type=source, reason="SYNTHETIC"
                )["entity_id"]
                h = Hold.objects.get(code=code)
                if (
                    rng.random() < 0.3
                    and fs.active_hold_policy()
                    and fs.policy_production_usable(fs.active_hold_policy())
                ):
                    if not fs.policy_production_usable(fs.hold_effective_rule(h).policy):
                        rid = fs.request_hold_rebase(w.lead, hold_code=code, reason="SYNTHETIC")["id"]
                        fs.approve_hold_rebase(w.lead, rebase_id=rid, slot="engineering")
                        fs.approve_hold_rebase(w.safety, rebase_id=rid, slot="safety")
                eff = fs.hold_effective_rule(h)
                for s in eff.slots:
                    if rng.random() < 0.8:
                        role = rng.choice(s["roles"])
                        try:
                            fs.approve_release(
                                approvers[role], hold_code=code, slot=s["slot"], role_used=role
                            )
                        except Exception:  # noqa: BLE001 - a refused random approval is part of the world
                            pass
                if rng.random() < 0.5:
                    fs.submit_inspection(
                        w.ops,
                        robot_code=robot,
                        template_version=rng.choice(["T1", eng_doc.version]),
                        answers=dict(w.all_pass),
                        performer=w.field.username,
                        verifier=w.field2.username,
                    )
                if rng.random() < 0.15:
                    fs.record_manifest(
                        w.eng,
                        robot_code=robot,
                        pattern_code="P-A",
                        autonomy_version=f"h{n}",
                        safety_config_hash=f"{n}",
                    )
                for demo, drafts in (
                    ("true", "true"),
                    ("true", "false"),
                    ("false", "true"),
                    ("false", "false"),
                ):
                    set_setting("demo_mode", demo)
                    set_setting("allow_draft_policies", drafts)
                    for hold in Hold.objects.filter(released_at__isnull=True):
                        assert fs.hold_release_problem(hold) == _sql_problem(hold), (hold.code, demo, drafts)
                        compared += 1
                raise _Rollback
        except _Rollback:
            pass
    assert compared >= 100


@pytest.fixture
def ew(monkeypatch):
    from tests import execution_world, readiness_world

    original = readiness_world.build_planning

    def build_planning(robots: int = 3, prefix: str = ""):
        world = original(robots=robots, prefix=prefix)
        for code in world.robots:
            fs.submit_inspection(
                world.ops,
                robot_code=code,
                template_version="T1",
                answers=dict(world.all_pass),
                performer=world.field.username,
                verifier=world.field2.username,
            )
        return world

    monkeypatch.setattr(readiness_world, "build_planning", build_planning)
    return execution_world.build_execution(prefix="o7x_")


def test_incident_release_clause_oracle(ew):
    from execution import incidents as inc

    out = inc.report_incident(
        ew.ops,
        robot_code=ew.robot.code,
        event_type="fall",
        description="SYNTHETIC",
        occurred_at=_one("SELECT now()") - timedelta(minutes=1),
        knowledge_source="witnessed",
    )
    h = Hold.objects.get(pk=inc.Incident.objects.get(code=out["entity_id"]).hold_id)
    for demo in ("true", "false"):
        set_setting("demo_mode", demo)
        assert fs.hold_release_problem(h) == _sql_problem(h)
    incident = inc.Incident.objects.get(hold_id=h.pk)
    for step in ("triaged", "response_in_progress", "resolved"):
        inc.change_incident_status(ew.safety, incident_code=incident.code, to_status=step)
        for demo in ("true", "false"):
            set_setting("demo_mode", demo)
            assert fs.hold_release_problem(h) == _sql_problem(h), (step, demo)
            assert fs.hold_source_problem(h, production=demo == "false") == _one(
                "SELECT fleet_hold_source_problem(h) FROM fleet_hold h WHERE h.id = %s", [h.pk]
            )
    # an incident-source hold with no Incident row: unreleasable in production only
    orphan = Hold.objects.create(
        code="HD-O7",
        scope="robot",
        robot=ew.robot,
        source_type="incident",
        source_ref="",
        reason="SYNTHETIC",
        policy=h.policy,
        required_slots=h.required_slots,
        manifest_at_placement_id=ew.robot.current_manifest_id,
        placed_by=ew.ops,
    )
    for demo in ("true", "false"):
        set_setting("demo_mode", demo)
        assert fs.hold_source_problem(orphan, production=demo == "false") == _one(
            "SELECT fleet_hold_source_problem(h) FROM fleet_hold h WHERE h.id = %s", [orphan.pk]
        )


# ================================================================== validation mirrors
def _floor_cases() -> list[Any]:
    good = floor_rules()
    cases: list[Any] = [good, [], "x", {"bogus": {}}]
    for src in list(good):
        c = floor_rules()
        del c[src]
        cases.append(c)
    mutations = [
        lambda r: r["manual"].update(slots=[]),
        lambda r: r["manual"].update(slots=[{"slot": "", "roles": ["engineer"]}]),
        lambda r: r["manual"].update(
            slots=[{"slot": "a", "roles": ["engineer"]}, {"slot": "a", "roles": ["engineer"]}]
        ),
        lambda r: r["manual"].update(slots=[{"slot": "a", "roles": []}]),
        lambda r: r["manual"].update(slots=[{"slot": "a", "roles": ["field_operator"]}]),
        lambda r: r["incident"].update(distinct_approvers=False),
        lambda r: r["incident"].pop("independent_of_placer"),
        lambda r: r["manual"].update(slots=["not-an-object"]),
    ]
    for m in mutations:
        c = floor_rules()
        m(c)
        cases.append(c)
    return cases


def test_validation_mirrors_agree_with_sql(w):
    for rules in _floor_cases():
        assert fs.hold_policy_floor_problem(rules) == _one(
            "SELECT fleet_hold_policy_floor_problem(%s)", [Jsonb(rules)]
        )
    items_cases: list[Any] = [
        TEMPLATE_ITEMS,
        [],
        {},
        [{"id": "", "label": "x", "robot_condition": True}],
        [
            {"id": "A", "label": "x", "robot_condition": True},
            {"id": "A", "label": "y", "robot_condition": False},
        ],
        [{"id": "A", "label": " ", "robot_condition": True}],
        [{"id": "A", "label": "x", "robot_condition": "yes"}],
        [{"id": 7, "label": "x", "robot_condition": True}],
    ]
    for items in items_cases:
        assert fs.template_items_problem(items) == _one(
            "SELECT fleet_template_items_problem(%s)", [Jsonb(items)]
        )
    rules = {"severity_scale": ["S3", "S2"], "escalation_required_from": "S2", "downgrade_roles": []}
    v = int(pol.create_incident_policy(w.lead, rules=rules, source="synthetic_demo")["entity_id"])
    policy = IncidentPolicy.objects.get(version=v)
    ok = {"S3": [{"to_role": "ops_lead", "within_min": 60}], "S2": [], "unclassified": []}
    deadline_cases: list[Any] = [
        ok,
        [],
        {"S3": [], "S2": []},
        {**ok, "S3": "x"},
        {**ok, "S3": [{"to_role": "ops_lead"}]},
        {**ok, "S3": [{"to_role": "janitor", "within_min": 5}]},
        {**ok, "S3": [{"to_role": "ops_lead", "within_min": 5}, {"to_role": "ops_lead", "within_min": 6}]},
        {**ok, "S3": [{"to_role": "ops_lead", "within_min": "5"}]},
        {**ok, "S3": [{"to_role": "ops_lead", "within_min": 1.5}]},
        {**ok, "S3": [{"to_role": "ops_lead", "within_min": 0}]},
        {**ok, "S3": [{"to_role": "ops_lead", "within_min": 5_256_001}]},
    ]
    for d in deadline_cases:
        py = ed.deadline_rules_problem(policy, d)
        sql = _one("SELECT execution_deadline_rules_problem(%s, %s)", [policy.pk, Jsonb(d)])
        if py is not None and "unknown deadline role" in py:
            assert sql is not None and sql.startswith("unknown deadline role")  # SQL quotes the JSON value
        else:
            assert py == sql, d
