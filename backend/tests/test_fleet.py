"""Fleet core behaviour through the command layer (Slice 1)."""

import pytest
from rest_framework.test import APIClient

from fleet import services as s
from fleet.models import Hold, Inspection, Robot
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.models import AuditEvent
from tests.conftest import make_user
from tests.fleet_world import build_world, set_setting

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    return build_world(robots=2)


def elig(code):
    return s.evaluate_eligibility(Robot.objects.get(code=code))


# ---------- registry and lifecycle
def test_register_duplicate_and_role_checks(w):
    with pytest.raises(Conflict):
        s.register_robot(w.ops, code="R-01", serial="new")
    with pytest.raises(PermissionDenied):
        s.register_robot(w.field, code="R-09", serial="x")
    assert not Robot.objects.filter(code="R-09").exists()


def test_lifecycle_rules(w):
    with pytest.raises(Conflict, match="not allowed"):
        s.transition_lifecycle(w.lead, robot_code="R-01", to="commissioning", reason="x")
    with pytest.raises(PermissionDenied):
        s.transition_lifecycle(w.lead, robot_code="R-01", to="retired", reason="x")
    s.transition_lifecycle(w.founder, robot_code="R-01", to="retired", reason="end of life")
    with pytest.raises(Conflict):
        s.transition_lifecycle(w.lead, robot_code="R-01", to="in_service", reason="x")


def test_in_service_requires_documented_stop_behaviour(w):
    s.create_pattern(w.lead, code="P-U", hardware_rev="SYN-X", release="9", firmware_set="F")
    s.register_robot(w.ops, code="R-07", serial="u7")
    s.record_manifest(
        w.eng, robot_code="R-07", pattern_code="P-U", autonomy_version="a", safety_config_hash="h"
    )
    s.transition_lifecycle(w.lead, robot_code="R-07", to="validation", reason="t")
    with pytest.raises(Conflict, match="Stop behaviour"):
        s.transition_lifecycle(w.lead, robot_code="R-07", to="in_service", reason="t")


# ---------- eligibility
def test_eligible_robot_is_labelled_planning_only(w):
    e = elig("R-01")
    assert e.eligible_for_planning and e.label == "Eligible for planning - not cleared to depart"


def test_hold_makes_robot_ineligible_immediately(w):
    s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="gripper check")
    e = elig("R-01")
    assert not e.eligible_for_planning and any("Maintenance" in r for r in e.reasons)
    assert elig("R-02").eligible_for_planning


def test_pattern_hold_affects_every_robot_on_that_pattern(w):
    s.place_hold(w.lead, scope="manifest_pattern", target="P-A", source_type="engineering", reason="bug")
    assert not elig("R-01").eligible_for_planning and not elig("R-02").eligible_for_planning


def test_unsupported_hold_scope_is_refused(w):
    with pytest.raises(Invalid):
        s.place_hold(w.ops, scope="site", target="H-001", source_type="manual", reason="x")


def test_withdrawn_pattern_and_unknown_capability_block(w):
    s.decide_pattern(w.lead, pattern_code="P-A", status="withdrawn", reason="regression")
    assert any("not approved for home use" in r for r in elig("R-01").reasons)
    s.publish_capability_profile(w.eng, pattern_code="P-C")  # v2, all unknown (not yet approved)
    assert s.approved_profile(s._pattern("P-C")).version_no == 1


def test_synthetic_profile_blocks_outside_demo_mode(w):
    set_setting("demo_mode", "false")
    assert any("synthetic" in r for r in elig("R-01").reasons)


def test_api_never_exposes_departure_style_fields(w, client_for):
    data = client_for(w.ops).get("/api/fleet/robots").json()
    flat = str(data).lower()
    for word in ("ready", "cleared_to_depart", "authorized_to_depart", "'cleared'"):
        assert word not in flat
    assert data[0]["fleet_eligibility"]["label"].endswith("not cleared to depart")


def test_capability_approval_rules(w):
    s.publish_capability_profile(
        w.eng, pattern_code="P-A", stop_behaviour="x", lift_crew=2, max_runtime_min=1, charge_turnaround_min=1
    )
    prof = s._pattern("P-A").profiles.order_by("-version_no").first()
    s.approve_capability_profile(w.lead, profile_id=prof.pk, slot="engineering")
    with pytest.raises(Conflict):
        s.approve_capability_profile(w.lead, profile_id=prof.pk, slot="safety")
    with pytest.raises(Conflict, match="requires role"):
        s.approve_capability_profile(
            make_user("x_lead", "engineering_lead"), profile_id=prof.pk, slot="safety"
        )


# ---------- hold release
def _incident_hold(w):
    return s.place_hold(
        w.ops, scope="robot", target="R-01", source_type="incident", source_ref="INC-1", reason="fall"
    )["entity_id"]


def test_release_needs_every_slot(w):
    h = _incident_hold(w)
    s.approve_release(w.eng, hold_code=h, slot="eng_review_1", role_used="engineer")
    s.approve_release(
        w.eng2, hold_code=h, slot="eng_review_2", role_used="engineer"
    )  # two reviewers, same role
    s.approve_release(w.safety, hold_code=h, slot="safety", role_used="safety_reviewer")
    with pytest.raises(Conflict, match="founder"):
        s.release_hold(w.lead, hold_code=h)
    s.approve_release(w.founder, hold_code=h, slot="founder", role_used="founder")
    s.release_hold(w.lead, hold_code=h)
    assert elig("R-01").eligible_for_planning


def test_distinct_and_independent_approvers(w):
    h = _incident_hold(w)
    s.approve_release(w.eng, hold_code=h, slot="eng_review_1", role_used="engineer")
    with pytest.raises(Conflict, match="same person"):
        s.approve_release(w.eng, hold_code=h, slot="eng_review_2", role_used="engineer")
    dual = make_user("dual", "ops_lead", "engineer")
    h2 = s.place_hold(dual, scope="robot", target="R-02", source_type="incident", reason="x")["entity_id"]
    with pytest.raises(Conflict, match="placed the hold"):
        s.approve_release(dual, hold_code=h2, slot="eng_review_1", role_used="engineer")


def test_wrong_role_for_slot_and_role_not_held(w):
    h = _incident_hold(w)
    with pytest.raises(Conflict, match="may not fill"):
        s.approve_release(w.safety, hold_code=h, slot="eng_review_1", role_used="safety_reviewer")
    with pytest.raises(PermissionDenied):
        s.approve_release(w.safety, hold_code=h, slot="founder", role_used="founder")


def test_approval_on_old_configuration_is_stale(w):
    h = s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")["entity_id"]
    s.approve_release(w.lead, hold_code=h, slot="engineering", role_used="engineering_lead")
    s.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a2", safety_config_hash="h2"
    )
    st = s.hold_release_status(Hold.objects.get(code=h))
    assert st["missing"] == ["engineering"] and st["slots"][0]["stale"]
    with pytest.raises(Conflict, match="missing approvals"):
        s.release_hold(w.lead, hold_code=h)
    a = Hold.objects.get(code=h).approvals.get()
    s.withdraw_release_approval(w.lead, approval_id=a.pk, reason="config changed")
    s.approve_release(w.lead, hold_code=h, slot="engineering", role_used="engineering_lead")
    s.release_hold(w.lead, hold_code=h)


def test_production_mode_holds_are_always_placeable_but_draft_policy_blocks_release(w):
    """Fail closed: restriction never depends on policy approval; only release does."""
    h = s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")["entity_id"]
    s.approve_release(w.lead, hold_code=h, slot="engineering", role_used="engineering_lead")
    set_setting("allow_draft_policies", "false")
    with pytest.raises(Conflict, match="draft"):
        s.release_hold(w.lead, hold_code=h)
    h2 = s.place_hold(w.ops, scope="robot", target="R-02", source_type="maintenance", reason="y")
    assert h2["entity_id"] and not elig("R-02").eligible_for_planning
    r = insp(w, {**w.all_pass, "I4": "fail"}, robot="R-02")  # a failed inspection still holds the robot
    assert r["result"] == "FAIL" and r["hold"]


def test_hold_without_any_release_rule_is_placeable_and_unreleasable(w):
    from fleet.models import HoldPolicy

    s.create_hold_policy(
        w.lead, rules={"manual": {"slots": [{"slot": "engineering", "roles": ["engineering_lead"]}]}}
    )
    assert HoldPolicy.objects.order_by("-version").first().rules.get("incident") is None
    h = s.place_hold(w.ops, scope="robot", target="R-01", source_type="incident", reason="fall")["entity_id"]
    hold = Hold.objects.get(code=h)
    assert hold.required_slots == [] and hold.policy is None
    assert s.hold_release_status(hold)["releasable"] is False
    with pytest.raises(Conflict):
        s.release_hold(w.lead, hold_code=h)


def test_idempotent_replay_after_revocation_is_denied(w):
    from accounts.models import RoleGrant
    from accounts.services import revoke_role

    s.place_hold(w.ops, scope="robot", target="R-01", source_type="manual", reason="x", idempotency_key="k1")
    admin = make_user("rv_admin", "admin")
    revoke_role(admin, grant_id=RoleGrant.objects.get(user=w.ops, role="ops_lead").pk, reason="left")
    with pytest.raises(PermissionDenied):
        s.place_hold(
            w.ops, scope="robot", target="R-01", source_type="manual", reason="x", idempotency_key="k1"
        )
    assert Robot.objects.get(code="R-01").holds.count() == 1


# ---------- inspections
def insp(w, answers, performer="field", verifier="field2", robot="R-01"):
    return s.submit_inspection(
        w.field,
        robot_code=robot,
        template_version="T1",
        answers=answers,
        performer=getattr(w, performer).username if performer else None,
        verifier=getattr(w, verifier).username if verifier else None,
    )


def test_inspection_results(w):
    assert insp(w, w.all_pass)["result"] == "PASS"
    partial = {k: v for k, v in w.all_pass.items() if k != "I3"}
    r = insp(w, partial)
    assert (r["result"], r["complete"], r["hold"]) == ("INCOMPLETE", False, None)
    assert insp(w, w.all_pass, verifier="field")["result"] == "INCOMPLETE"  # same person twice


def test_observed_robot_failure_holds_even_when_incomplete(w):
    answers = {"I6": "fail"}  # everything else unanswered, no verifier
    r = insp(w, answers, verifier=None)
    assert (r["result"], r["complete"]) == ("FAIL", False) and r["hold"]
    assert not elig("R-01").eligible_for_planning


def test_visit_item_failure_does_not_hold_robot(w):
    r = insp(w, {**w.all_pass, "I10": "fail"})
    assert r["result"] == "FAIL" and r["hold"] is None and elig("R-01").eligible_for_planning


def test_malformed_and_unqualified_inspections_store_nothing(w):
    with pytest.raises(Invalid):
        insp(w, {"I99": "pass"})
    with pytest.raises(Invalid):
        insp(w, {"I1": "maybe"})
    with pytest.raises(Invalid):
        s.submit_inspection(
            w.field, robot_code="R-01", template_version="T9", answers={}, performer=None, verifier=None
        )
    with pytest.raises(Invalid, match="not qualified"):
        insp(w, w.all_pass, performer="eng")
    assert Inspection.objects.count() == 0


def test_inspection_hold_release_needs_newer_pass(w):
    r = insp(w, {**w.all_pass, "I2": "fail"})
    s.approve_release(w.eng, hold_code=r["hold"], slot="engineering_review", role_used="engineer")
    with pytest.raises(Conflict, match="newer PASS"):
        s.release_hold(w.ops, hold_code=r["hold"])
    insp(w, w.all_pass)
    s.release_hold(w.ops, hold_code=r["hold"])
    assert elig("R-01").eligible_for_planning


def test_inspection_failure_rolls_back_hold_too(w, monkeypatch):
    def boom(**kw):
        raise RuntimeError("injected")

    monkeypatch.setattr(Inspection.objects, "create", boom)
    with pytest.raises(RuntimeError):
        insp(w, {**w.all_pass, "I1": "fail"})
    assert not Hold.objects.exists() and elig("R-01").eligible_for_planning


# ---------- history and audit
def test_history_keeps_configuration_at_each_event(w):
    h = s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")["entity_id"]
    old = Robot.objects.get(code="R-01").current_manifest_id
    s.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a2", safety_config_hash="z"
    )
    hold = Hold.objects.get(code=h)
    assert hold.manifest_at_placement_id == old
    kinds = [e["kind"] for e in s.robot_history(Robot.objects.get(code="R-01"))]
    assert kinds.count("manifest") == 2 and "hold" in kinds
    assert AuditEvent.objects.filter(command="fleet.place_hold", entity_id=h).count() == 1


# ---------- UI
def test_fleet_ui_pages_and_hold_form(w):
    c = APIClient()
    c.force_login(w.ops)
    page = c.get("/fleet/").content.decode()
    assert "Eligible for planning - not cleared to depart" in page and "SYNTHETIC" in page
    assert "DRAFT" in page
    r = c.post("/fleet/R-01/hold/", {"source_type": "manual", "reason": "UI test"})
    assert r.status_code == 302
    detail = c.get("/fleet/R-01/").content.decode()
    assert "Hold placed" in detail and "Missing" in detail and "Not eligible for planning" in detail
