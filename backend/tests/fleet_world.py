"""A small synthetic fleet built through the real commands, for tests.

Slice 7 (lead, phase 0): templates have an author (w.eng); every pattern gets an approved SYNTHETIC periodic
inspection rule (SYNTHETIC_PERIODIC_INTERVAL_MIN, a deliberately arbitrary test value, never an engineering value;
the new rule commands are Engineer B's, so the rule rows are written through the ORM and the database triggers
still check author, roles and versions). Production fixtures for tests that need a production-usable world:
make_engineering_template / make_periodic_rule.

Slice 7 integration (X-3, EM decision 7): a robot with no qualifying periodic inspection is not eligible. The
world therefore registers the two field operators as persons P-2 (w.field) and P-3 (w.field2), grants them the
SYNTHETIC readiness_inspector qualification (X-8), and records ONE complete PASS periodic inspection per robot on
T1 (synthetic, usable for 'periodic' in demo) on its current manifest, signed by both, through
fleet.services.submit_inspection, before any visit is planned (w.periodic_inspections: robot code -> inspection
code). tests.planning_world reuses these person codes (P-1 = w.ops is registered there).
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fleet import services as s
from fleet.management.commands.seed_fleet import ALL_PASS, DRAFT_POLICY_RULES, TEMPLATE_ITEMS
from fleet.models import (
    CapabilityProfile,
    DeploymentSetting,
    InspectionTemplate,
    InspectionTemplateApproval,
    ManifestPattern,
    PeriodicInspectionRule,
    PeriodicInspectionRuleApproval,
)
from planning import services as planning_services
from planning.models import Person, Qualification
from tests.conftest import make_user

# SYNTHETIC test interval (90 days): long enough that a fresh inspection covers the test visits planned on
# tests.planning_world.DAY. Deliberately arbitrary; the playbook's illustrative figure is never used.
SYNTHETIC_PERIODIC_INTERVAL_MIN = 129_600


def set_setting(key: str, value: str) -> None:
    DeploymentSetting.objects.update_or_create(key=key, defaults={"value": value})


def make_periodic_rule(
    w: SimpleNamespace,
    pattern_code: str,
    interval_min: int | None = SYNTHETIC_PERIODIC_INTERVAL_MIN,
    source: str = "synthetic_demo",
    approve: bool = True,
    procedure_ref: str = "",
) -> PeriodicInspectionRule:
    """A new periodic rule version authored by w.eng, approved by w.lead (engineering) + w.safety (safety)."""
    pattern = ManifestPattern.objects.get(code=pattern_code)
    last = PeriodicInspectionRule.objects.filter(pattern=pattern).order_by("-version_no").first()
    rule = PeriodicInspectionRule.objects.create(
        pattern=pattern,
        version_no=(last.version_no if last else 0) + 1,
        interval_min=interval_min,
        source=source,
        procedure_ref=procedure_ref or ("DOC-SYNTHETIC-TEST" if source == "engineering_doc" else ""),
        notes="SYNTHETIC test interval, not an engineering value",
        created_by=w.eng,
    )
    if approve:
        PeriodicInspectionRuleApproval.objects.create(rule=rule, slot="engineering", approver=w.lead)
        PeriodicInspectionRuleApproval.objects.create(rule=rule, slot="safety", approver=w.safety)
    return rule


def grant_inspector(w: SimpleNamespace, person_code: str, valid_to: datetime | None = None) -> Qualification:
    """SYNTHETIC readiness_inspector grant by w.safety, effective now (database time)."""
    return Qualification.objects.create(
        person=Person.objects.get(code=person_code),
        kind="readiness_inspector",
        valid_from=None,
        valid_to=valid_to or datetime(2027, 6, 1, tzinfo=UTC),
        granted_by=w.safety,
        basis_ref="SYNTHETIC-TRAINING",
    )


def record_periodic_inspection(w: SimpleNamespace, robot_code: str, template_version: str = "T1") -> str:
    """One complete PASS inspection of the robot's current manifest, signed by P-2 (w.field) + P-3 (w.field2),
    both qualified readiness inspectors: with T1 (synthetic) it qualifies as the periodic inspection in demo."""
    code: str = s.submit_inspection(
        w.ops,
        robot_code=robot_code,
        template_version=template_version,
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
    )["entity_id"]
    return code


def make_engineering_template(
    w: SimpleNamespace,
    pattern_code: str,
    version: str = "E1",
    purposes: tuple[str, ...] = ("pre_departure", "periodic"),
    items: list[dict[str, Any]] | None = None,
) -> InspectionTemplate:
    """An engineering_doc template (SYNTHETIC content; test fixture only) approved for the given purposes."""
    t = InspectionTemplate.objects.create(
        pattern=ManifestPattern.objects.get(code=pattern_code),
        version=version,
        items=items or TEMPLATE_ITEMS,
        source="engineering_doc",
        procedure_ref="DOC-SYNTHETIC-TEST",
        created_by=w.eng,
    )
    for purpose in purposes:
        InspectionTemplateApproval.objects.create(
            template=t, purpose=purpose, slot="engineering", approver=w.lead
        )
        InspectionTemplateApproval.objects.create(
            template=t, purpose=purpose, slot="safety", approver=w.safety
        )
    return t


def build_world(robots: int = 2, prefix: str = "") -> SimpleNamespace:
    w = SimpleNamespace()
    w.lead = make_user(f"{prefix}lead", "engineering_lead")
    w.eng = make_user(f"{prefix}eng", "engineer")
    w.eng2 = make_user(f"{prefix}eng2", "engineer")
    w.safety = make_user(f"{prefix}safety", "safety_reviewer")
    w.founder = make_user(f"{prefix}founder", "founder")
    w.ops = make_user(f"{prefix}ops", "ops_lead")
    w.field = make_user(f"{prefix}field", "field_operator")
    w.field2 = make_user(f"{prefix}field2", "field_operator")
    set_setting("allow_draft_policies", "true")
    set_setting("demo_mode", "true")
    # X-8: the periodic inspection signers are qualified readiness inspectors (codes shared with planning_world).
    for user, person_code in ((w.field, "P-2"), (w.field2, "P-3")):
        planning_services.register_person(w.ops, username=user.username, code=person_code)
        grant_inspector(w, person_code)
    s.create_hold_policy(w.lead, rules=DRAFT_POLICY_RULES)
    for code, rel in (("P-A", "4.3.0"), ("P-C", "4.3.1")):
        s.create_pattern(w.lead, code=code, hardware_rev="SYN-B", release=rel, firmware_set="FW-12")
        s.decide_pattern(w.lead, pattern_code=code, status="approved_home", reason="test")
        s.publish_capability_profile(
            w.eng,
            pattern_code=code,
            stop_behaviour="SYNTHETIC",
            lift_crew=2,
            max_runtime_min=120,
            charge_turnaround_min=180,
            load_unload_min=20,
        )
        prof = CapabilityProfile.objects.get(pattern__code=code, version_no=1)
        s.approve_capability_profile(w.lead, profile_id=prof.pk, slot="engineering")
        s.approve_capability_profile(w.safety, profile_id=prof.pk, slot="safety")
        InspectionTemplate.objects.create(
            pattern=ManifestPattern.objects.get(code=code),
            version="T1",
            items=TEMPLATE_ITEMS,
            created_by=w.eng,
        )
        make_periodic_rule(w, code)
    w.robots = []
    for n in range(1, robots + 1):
        code = f"R-{n:02d}"
        s.register_robot(w.ops, code=code, serial=f"{prefix}SYN-{n}")
        s.record_manifest(
            w.eng, robot_code=code, pattern_code="P-A", autonomy_version="a1", safety_config_hash="h" * 64
        )
        s.transition_lifecycle(w.lead, robot_code=code, to="validation", reason="t")
        s.transition_lifecycle(w.lead, robot_code=code, to="in_service", reason="t")
        s.grant_base_authorization(w.lead, robot_code=code)
        w.robots.append(code)
    w.all_pass = dict(ALL_PASS)
    # X-3: one qualifying periodic inspection per robot, after the inspector grants, before any planning.
    w.periodic_inspections = {code: record_periodic_inspection(w, code) for code in w.robots}
    return w
