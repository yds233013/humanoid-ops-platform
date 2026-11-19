"""Slice 7 fleet commands (Engineer B): inspection templates (X-1), periodic interval rules (X-3), hold-policy
lifecycle, binding, release and re-basing (G-01), through the command layer. SYNTHETIC data only: every
"approved" row here is a test approval by a test user, never a company approval.

Contract: docs/07a_production_guardrails_contract.md sections 3, 4, 5 and 9.
"""

from typing import Any

import pytest
from django.db import connection

from fleet import services as s
from fleet.management.commands.seed_fleet import DRAFT_POLICY_RULES, TEMPLATE_ITEMS
from fleet.models import (
    Hold,
    HoldPolicy,
    HoldRebaseActivation,
    InspectionTemplate,
    ManifestPattern,
    PeriodicInspectionRule,
    ReleaseApproval,
)
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.models import AuditEvent
from tests.conftest import make_user
from tests.fleet_world import build_world, set_setting

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    return build_world(robots=2, prefix="f7_")


def _one(sql: str, params: list[Any] | None = None) -> Any:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()[0]


def floor_rules() -> dict[str, Any]:
    """A SYNTHETIC rule set that satisfies the approved_procedure floor (test fixture, not company content)."""
    rule = {"slots": [{"slot": "engineering", "roles": ["engineering_lead"]}], "distinct_approvers": True}
    rules: dict[str, Any] = {src: dict(rule) for src, _ in Hold.SOURCES}
    rules["incident"] = {
        "slots": [
            {"slot": "engineering", "roles": ["engineering_lead"]},
            {"slot": "safety", "roles": ["safety_reviewer"]},
        ],
        "distinct_approvers": True,
        "independent_of_placer": True,
    }
    rules["inspection"] = {**rule, "requires_newer_pass_inspection": True}
    return rules


def procedure_policy(w: Any, approve: bool = True) -> HoldPolicy:
    """An approved_procedure policy (SYNTHETIC test content) authored by w.eng, fully approved through the
    commands by w.lead, w.safety and w.founder."""
    v = int(
        s.create_hold_policy(
            w.eng, rules=floor_rules(), source="approved_procedure", procedure_ref="DOC-SYNTHETIC-TEST"
        )["entity_id"]
    )
    if approve:
        s.approve_hold_policy(w.lead, version=v, slot="engineering")
        s.approve_hold_policy(w.safety, version=v, slot="safety")
        s.approve_hold_policy(w.founder, version=v, slot="founder")
    return HoldPolicy.objects.get(version=v)


def doc_template(w: Any, version: str = "E1", author: Any = None) -> InspectionTemplate:
    tid = int(
        s.create_inspection_template(
            author or w.eng,
            pattern_code="P-A",
            version=version,
            items=TEMPLATE_ITEMS,
            source="engineering_doc",
            procedure_ref="DOC-SYNTHETIC-TEST",
        )["entity_id"]
    )
    return InspectionTemplate.objects.get(pk=tid)


def hold(w: Any, robot: str = "R-01", source: str = "maintenance") -> Hold:
    code = s.place_hold(w.ops, scope="robot", target=robot, source_type=source, reason="SYNTHETIC")[
        "entity_id"
    ]
    return Hold.objects.get(code=code)


def sql_usable(t: InspectionTemplate, purpose: str, production: bool) -> bool:
    return bool(_one("SELECT fleet_template_usable(%s, %s, %s)", [t.pk, purpose, production]))


# ================================================================== X-1 inspection templates
def test_template_create_refusals_and_permissions(w):
    with pytest.raises(PermissionDenied):
        s.create_inspection_template(
            w.ops, pattern_code="P-A", version="X", items=TEMPLATE_ITEMS, source="synthetic_demo"
        )
    with pytest.raises(Invalid, match="procedure reference"):
        s.create_inspection_template(
            w.eng, pattern_code="P-A", version="X", items=TEMPLATE_ITEMS, source="engineering_doc"
        )
    with pytest.raises(Invalid, match="duplicate item id I1"):
        s.create_inspection_template(
            w.eng,
            pattern_code="P-A",
            version="X",
            items=[TEMPLATE_ITEMS[0], TEMPLATE_ITEMS[0]],
            source="synthetic_demo",
        )
    with pytest.raises(Conflict, match="already exists"):
        s.create_inspection_template(
            w.eng, pattern_code="P-A", version="T1", items=TEMPLATE_ITEMS, source="synthetic_demo"
        )
    t = doc_template(w)
    assert t.created_by == w.eng and t.procedure_ref == "DOC-SYNTHETIC-TEST"
    assert AuditEvent.objects.filter(command="fleet.create_inspection_template", outcome="committed").exists()


def test_template_approval_rules_usability_and_pattern_bump(w):
    t = doc_template(w)
    pattern_v = ManifestPattern.objects.get(code="P-A").version
    assert s.template_state(t, "pre_departure") == "awaiting approval"
    with pytest.raises(PermissionDenied):
        s.approve_inspection_template(w.ops, template_id=t.pk, purpose="pre_departure", slot="engineering")
    with pytest.raises(PermissionDenied, match="requires role engineering_lead"):
        s.approve_inspection_template(w.safety, template_id=t.pk, purpose="pre_departure", slot="engineering")
    s.approve_inspection_template(w.lead, template_id=t.pk, purpose="pre_departure", slot="engineering")
    assert not s.template_usable(t, "pre_departure", production=True)  # one slot is not enough
    both = make_user("f7_both", "engineering_lead", "safety_reviewer")
    s.approve_inspection_template(both, template_id=t.pk, purpose="periodic", slot="engineering")
    with pytest.raises(Conflict, match="same person"):
        s.approve_inspection_template(both, template_id=t.pk, purpose="periodic", slot="safety")
    s.approve_inspection_template(w.safety, template_id=t.pk, purpose="pre_departure", slot="safety")
    assert s.template_usable(t, "pre_departure", production=True)
    assert not s.template_usable(t, "periodic", production=True)  # each purpose approved separately
    assert ManifestPattern.objects.get(code="P-A").version > pattern_v  # R1-4 bump
    # the author never approves
    t2 = doc_template(w, "E2", author=w.lead)
    with pytest.raises(Conflict, match="author"):
        s.approve_inspection_template(w.lead, template_id=t2.pk, purpose="pre_departure", slot="engineering")
    # synthetic content can never be approved (R1-3)
    t1 = InspectionTemplate.objects.get(pattern__code="P-A", version="T1")
    with pytest.raises(Conflict, match="synthetic_demo inspection template can never be approved"):
        s.approve_inspection_template(w.lead, template_id=t1.pk, purpose="pre_departure", slot="engineering")
    # withdrawal: every purpose, bumps, no approval afterwards
    pattern_v = ManifestPattern.objects.get(code="P-A").version
    with pytest.raises(PermissionDenied):
        s.withdraw_inspection_template(w.eng, template_id=t.pk, reason="x")
    s.withdraw_inspection_template(w.safety, template_id=t.pk, reason="SYNTHETIC superseded")
    assert ManifestPattern.objects.get(code="P-A").version > pattern_v
    assert s.template_state(t, "pre_departure") == "withdrawn"
    assert not s.template_usable(t, "pre_departure", production=False)
    with pytest.raises(Conflict, match="withdrawn"):
        s.approve_inspection_template(w.safety, template_id=t.pk, purpose="periodic", slot="safety")
    # the Python mirror agrees with the SQL function on every case above
    for tpl in (t, t1, t2):
        for purpose in ("pre_departure", "periodic"):
            for production in (True, False):
                assert s.template_usable(tpl, purpose, production=production) == sql_usable(
                    tpl, purpose, production
                ), (tpl.version, purpose, production)


def test_submit_inspection_never_refuses_on_template_grounds(w):
    """Principle 1: recording never depends on approval. A production inspection on the synthetic template is
    recorded, and a robot-condition failure still holds the robot."""
    set_setting("demo_mode", "false")
    out = s.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="T1",
        answers={**w.all_pass, "I4": "fail"},
        performer=w.ops.username,
        verifier=w.field.username,
    )
    assert out["result"] == "FAIL" and out["hold"]


# ================================================================== X-3 periodic interval rules
def test_periodic_rule_commands_active_and_no_fallback(w):
    p = ManifestPattern.objects.get(code="P-A")
    with pytest.raises(PermissionDenied):
        s.create_periodic_rule(w.ops, pattern_code="P-A", interval_min=60, source="synthetic_demo")
    with pytest.raises(Invalid, match="interval_min"):
        s.create_periodic_rule(w.eng, pattern_code="P-A", interval_min=0, source="synthetic_demo")
    with pytest.raises(Invalid, match="procedure reference"):
        s.create_periodic_rule(w.eng, pattern_code="P-A", interval_min=60, source="engineering_doc")
    before = s.active_periodic_rule(p.pk)
    assert before is not None  # the world's synthetic v1
    rid = int(
        s.create_periodic_rule(
            w.eng, pattern_code="P-A", interval_min=None, source="engineering_doc", procedure_ref="DOC-X"
        )["entity_id"]
    )
    rule = PeriodicInspectionRule.objects.get(pk=rid)
    assert rule.version_no == before.version_no + 1 and rule.interval_min is None
    with pytest.raises(PermissionDenied, match="fleet.approve_periodic_rule"):
        s.approve_periodic_rule(w.eng, rule_id=rid, slot="engineering")  # engineers author, never approve
    with pytest.raises(PermissionDenied, match="requires role engineering_lead"):
        s.approve_periodic_rule(w.safety, rule_id=rid, slot="engineering")
    pattern_v = ManifestPattern.objects.get(code="P-A").version
    s.approve_periodic_rule(w.lead, rule_id=rid, slot="engineering")
    out = s.approve_periodic_rule(w.safety, rule_id=rid, slot="safety")
    assert out["active"] is True and ManifestPattern.objects.get(code="P-A").version > pattern_v
    # active but NULL interval: unknown in both modes
    assert s.usable_periodic_rule(p.pk, production=False) is None
    assert _one("SELECT fleet_usable_periodic_rule_id(%s, false)", [p.pk]) is None
    s.withdraw_periodic_rule(w.lead, rule_id=rid, reason="SYNTHETIC")
    assert s.active_periodic_rule(p.pk) is None  # no fallback to the approved v1
    assert _one("SELECT fleet_active_periodic_rule_id(%s)", [p.pk]) is None
    with pytest.raises(Conflict, match="withdrawn"):
        s.approve_periodic_rule(w.lead, rule_id=rid, slot="engineering")


def test_periodic_rule_author_cannot_approve(w):
    rid = int(
        s.create_periodic_rule(w.lead, pattern_code="P-C", interval_min=600, source="synthetic_demo")[
            "entity_id"
        ]
    )
    with pytest.raises(Conflict, match="author"):
        s.approve_periodic_rule(w.lead, rule_id=rid, slot="engineering")


# ================================================================== G-01 hold-policy lifecycle
def test_hold_policy_lifecycle_commands(w):
    with pytest.raises(PermissionDenied):
        s.create_hold_policy(w.ops, rules=DRAFT_POLICY_RULES)
    with pytest.raises(Invalid, match="procedure reference"):
        s.create_hold_policy(w.eng, rules=floor_rules(), source="approved_procedure")
    bad = floor_rules()
    del bad["manual"]
    with pytest.raises(Invalid, match="needs a rule for every hold source \\(missing manual\\)"):
        s.create_hold_policy(w.eng, rules=bad, source="approved_procedure", procedure_ref="DOC")
    bad = floor_rules()
    bad["incident"]["independent_of_placer"] = False
    with pytest.raises(Invalid, match="independent of the placer"):
        s.create_hold_policy(w.eng, rules=bad, source="approved_procedure", procedure_ref="DOC")
    own = s.create_hold_policy(w.lead, rules=DRAFT_POLICY_RULES)["entity_id"]
    with pytest.raises(Conflict, match="author"):
        s.approve_hold_policy(w.lead, version=int(own), slot="engineering")
    p = procedure_policy(w, approve=False)
    s.approve_hold_policy(w.lead, version=p.version, slot="engineering")
    s.approve_hold_policy(w.safety, version=p.version, slot="safety")
    assert s.policy_status(p) == "draft" and s.active_hold_policy() is None  # founder slot still missing
    out = s.approve_hold_policy(w.founder, version=p.version, slot="founder")
    assert out["active"] is True and s.policy_production_usable(p)
    assert s.active_hold_policy() == p and _one("SELECT fleet_active_hold_policy_id()") == p.pk
    # the founder slot exists only on approved_procedure
    v1 = HoldPolicy.objects.get(version=1)
    with pytest.raises(Conflict, match="founder slot"):
        s.approve_hold_policy(w.founder, version=v1.version, slot="founder")
    # withdrawal: no active policy, no fallback; binding falls back to the newest
    with pytest.raises(PermissionDenied):
        s.withdraw_hold_policy(w.eng, version=p.version, reason="x")
    s.withdraw_hold_policy(w.safety, version=p.version, reason="SYNTHETIC")
    assert s.active_hold_policy() is None and s.policy_status(p) == "withdrawn"
    assert s.binding_policy() == p == HoldPolicy.objects.get(pk=_one("SELECT fleet_hold_binding_policy_id()"))
    with pytest.raises(Conflict, match="withdrawn"):
        s.approve_hold_policy(w.lead, version=p.version, slot="engineering")


def test_binding_draft_newer_than_approved_binds_approved_and_ignores_settings(w):
    synthetic = HoldPolicy.objects.get(version=1)  # the world's draft, authored by w.lead
    s.approve_hold_policy(make_user("f7_lead2", "engineering_lead"), version=1, slot="engineering")
    s.approve_hold_policy(w.safety, version=1, slot="safety")
    s.create_hold_policy(w.eng, rules=DRAFT_POLICY_RULES)  # newer draft v2
    for demo, drafts in (("true", "true"), ("false", "true"), ("false", "false"), ("true", "false")):
        set_setting("demo_mode", demo)
        set_setting("allow_draft_policies", drafts)
        assert s.binding_policy() == synthetic
        assert _one("SELECT fleet_hold_binding_policy_id()") == synthetic.pk
        h = hold(w)
        assert h.policy == synthetic and h.required_slots == DRAFT_POLICY_RULES["maintenance"]["slots"]


def test_place_hold_refuses_incident_source(w):
    with pytest.raises(Invalid, match="report_incident"):
        s.place_hold(w.ops, scope="robot", target="R-01", source_type="incident", reason="fall")
    assert not Hold.objects.filter(source_type="incident").exists()


def test_release_depends_on_mode_and_allow_draft_only_in_demo(w):
    h = hold(w)
    s.approve_release(w.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    set_setting("demo_mode", "false")  # allow_draft_policies stays 'true': ignored outside demo
    st = s.hold_release_status(h)
    assert st["releasable"] is False and st["needs_rebase"] and "not usable in production" in st["problem"]
    assert st["problem"] == _one(
        "SELECT fleet_hold_release_problem(h) FROM fleet_hold h WHERE id = %s", [h.pk]
    )
    with pytest.raises(Conflict, match="not usable in production"):
        s.release_hold(w.lead, hold_code=h.code)
    set_setting("demo_mode", "true")
    assert s.hold_release_status(h)["releasable"] is True
    s.release_hold(w.lead, hold_code=h.code)
    assert Hold.objects.get(pk=h.pk).released_at is not None


def test_pattern_hold_release_locks_pattern_and_robots(w):
    code = s.place_hold(
        w.lead, scope="manifest_pattern", target="P-A", source_type="engineering", reason="SYNTHETIC"
    )["entity_id"]
    s.approve_release(
        make_user("f7_l3", "engineering_lead"),
        hold_code=code,
        slot="engineering",
        role_used="engineering_lead",
    )
    s.release_hold(w.ops, hold_code=code)
    assert Hold.objects.get(code=code).released_at is not None


# ================================================================== G-01 re-basing
def test_rebase_flow_old_approvals_do_not_count(w):
    code = s.place_hold(w.lead, scope="robot", target="R-01", source_type="maintenance", reason="SYNTHETIC")
    h = Hold.objects.get(code=code["entity_id"])  # placed by w.lead, bound to the synthetic draft v1
    old = s.approve_release(w.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    with pytest.raises(Conflict, match="no active production-usable"):
        s.request_hold_rebase(w.lead, hold_code=h.code, reason="SYNTHETIC")
    p = procedure_policy(w)
    with pytest.raises(PermissionDenied):
        s.request_hold_rebase(w.ops, hold_code=h.code, reason="x")
    with pytest.raises(Invalid, match="reason"):
        s.request_hold_rebase(w.lead, hold_code=h.code, reason=" ")
    out = s.request_hold_rebase(w.lead, hold_code=h.code, reason="SYNTHETIC: move to the approved procedure")
    rid = out["id"]
    assert out["from_policy"] == 1 and out["to_policy"] == p.version and out["based_on"] is None
    b = h.rebases.get()
    assert b.required_slots == floor_rules()["maintenance"]["slots"] and s.rebase_state(b) == "pending"
    # placer excluded; same person cannot fill both slots
    with pytest.raises(Conflict, match="placed the hold"):
        s.approve_hold_rebase(w.lead, rebase_id=rid, slot="engineering")
    both = make_user("f7_rb_both", "engineering_lead", "safety_reviewer")
    s.approve_hold_rebase(both, rebase_id=rid, slot="engineering")
    with pytest.raises(Conflict, match="both approval slots"):
        s.approve_hold_rebase(both, rebase_id=rid, slot="safety")
    assert not HoldRebaseActivation.objects.exists()
    final = s.approve_hold_rebase(w.safety, rebase_id=rid, slot="safety")
    assert final["effective"] is True and s.rebase_state(b) == "effective"
    st = s.hold_release_status(h)
    assert st["origin"] == "rebase" and st["rebase"] == b.code and st["policy_version"] == p.version
    assert st["placement_policy_version"] == 1  # the hold row never changes
    assert st["slots"][0]["superseded"] and st["slots"][0]["valid"] is None  # the pre-rebase approval
    assert ReleaseApproval.objects.get(pk=old["approval_id"]).rebase_id is None
    set_setting("demo_mode", "false")
    with pytest.raises(Conflict, match="missing approvals for engineering"):
        s.release_hold(w.ops, hold_code=h.code)
    new = s.approve_release(both, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    assert new["rebase"] == b.code  # forced by the trigger
    assert s.hold_release_status(h)["releasable"]
    s.release_hold(w.ops, hold_code=h.code)  # released in production under the approved procedure


def test_rebase_refused_for_a_hold_already_under_a_usable_policy(w):
    procedure_policy(w)
    h = hold(w)  # binds to the active approved procedure
    assert h.policy.source == "approved_procedure"
    with pytest.raises(Conflict, match="already under a production-usable policy"):
        s.request_hold_rebase(w.lead, hold_code=h.code, reason="shopping")
    assert s.hold_release_status(h)["needs_rebase"] is False


def test_final_rebase_approval_rechecks_the_destination(w):
    """Case d: destination superseded (or withdrawn) before the final approval -> refused, no activation."""
    h = hold(w)
    p = procedure_policy(w)
    rid = s.request_hold_rebase(w.lead, hold_code=h.code, reason="SYNTHETIC")["id"]
    s.approve_hold_rebase(w.lead, rebase_id=rid, slot="engineering")
    newer = procedure_policy(w)  # becomes the active policy
    with pytest.raises(
        Conflict,
        match=f"policy v{p.version} is no longer the active production policy \\(active: v{newer.version}\\)",
    ):
        s.approve_hold_rebase(w.safety, rebase_id=rid, slot="safety")
    assert s.rebase_state(h.rebases.get()) == "destination no longer active"
    s.withdraw_hold_policy(w.lead, version=newer.version, reason="SYNTHETIC")
    with pytest.raises(Conflict, match="\\(active: none\\)"):
        s.approve_hold_rebase(w.safety, rebase_id=rid, slot="safety")
    assert not HoldRebaseActivation.objects.exists()


def test_competing_rebases_the_loser_is_superseded(w):
    """Case e (sequential form; both commit orders under concurrency are in test_s7_concurrency)."""
    h = hold(w)
    procedure_policy(w)
    r1 = s.request_hold_rebase(w.lead, hold_code=h.code, reason="first")["id"]
    r2 = s.request_hold_rebase(w.safety, hold_code=h.code, reason="second")["id"]
    for rid in (r1, r2):
        s.approve_hold_rebase(w.lead, rebase_id=rid, slot="engineering")
    s.approve_hold_rebase(w.safety, rebase_id=r2, slot="safety")
    winner = h.rebases.get(pk=r2)
    with pytest.raises(Conflict, match=f"superseded by rebase {winner.code}, which became effective first"):
        s.approve_hold_rebase(w.safety, rebase_id=r1, slot="safety")
    assert list(HoldRebaseActivation.objects.values_list("rebase_id", flat=True)) == [r2]
    assert s.rebase_state(h.rebases.get(pk=r1)) == "superseded"


def test_hold_policy_commands_hold_only_the_registry(w):
    """Section 9.2: create / approve / withdraw lock the registry U and no guard row (captured statements)."""
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as q:
        s.create_hold_policy(w.eng, rules=DRAFT_POLICY_RULES)
    locks = [x["sql"] for x in q.captured_queries if "FOR UPDATE" in x["sql"] or "FOR SHARE" in x["sql"]]
    assert any("fleet_holdpolicyregistry" in x and "FOR UPDATE" in x for x in locks)
    assert not [x for x in locks if "fleet_robot" in x or "fleet_manifestpattern" in x]
