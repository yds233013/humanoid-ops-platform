"""A small synthetic fleet built through the real commands, for tests."""

from types import SimpleNamespace

from fleet import services as s
from fleet.management.commands.seed_fleet import ALL_PASS, DRAFT_POLICY_RULES, TEMPLATE_ITEMS
from fleet.models import CapabilityProfile, DeploymentSetting, InspectionTemplate, ManifestPattern
from tests.conftest import make_user


def set_setting(key: str, value: str) -> None:
    DeploymentSetting.objects.update_or_create(key=key, defaults={"value": value})


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
        )
        prof = CapabilityProfile.objects.get(pattern__code=code, version_no=1)
        s.approve_capability_profile(w.lead, profile_id=prof.pk, slot="engineering")
        s.approve_capability_profile(w.safety, profile_id=prof.pk, slot="safety")
        InspectionTemplate.objects.create(
            pattern=ManifestPattern.objects.get(code=code), version="T1", items=TEMPLATE_ITEMS
        )
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
    return w
