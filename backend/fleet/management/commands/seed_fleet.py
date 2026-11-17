"""SYNTHETIC demo fleet, created through the real commands so the audit trail is complete.

Run as the schema owner (it switches this deployment into demo mode, which the runtime role cannot do).
Nothing here describes a real robot: capability values are placeholders marked synthetic_demo, and the
hold policy stays a DRAFT (no safety approval is faked)."""

from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from fleet import services as s
from fleet.models import CapabilityProfile, DeploymentSetting, InspectionTemplate, ManifestPattern, Robot

User = get_user_model()

DRAFT_POLICY_RULES = {
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
    "maintenance": {
        "slots": [{"slot": "engineering", "roles": ["engineering_lead"]}],
        "distinct_approvers": True,
    },
    "engineering": {
        "slots": [{"slot": "engineering", "roles": ["engineering_lead"]}],
        "distinct_approvers": True,
    },
    "release_withdrawn": {
        "slots": [{"slot": "engineering", "roles": ["engineering_lead"]}],
        "distinct_approvers": True,
    },
    "manual": {"slots": [{"slot": "engineering", "roles": ["engineering_lead"]}], "distinct_approvers": True},
}

TEMPLATE_ITEMS: list[dict[str, Any]] = [
    {
        "id": f"I{n}",
        "label": f"SYNTHETIC robot-condition item {n} (engineering to supply)",
        "robot_condition": True,
    }
    for n in range(1, 8)
] + [
    {"id": f"I{n}", "label": f"SYNTHETIC visit item {n} (kit / consent / home)", "robot_condition": False}
    for n in range(8, 13)
]
ALL_PASS: dict[str, str] = {i["id"]: "pass" for i in TEMPLATE_ITEMS}


class Command(BaseCommand):
    help = "Create the synthetic demo fleet (idempotent: does nothing if R-01 exists)."

    def handle(self, *args: object, **opts: object) -> None:
        if Robot.objects.filter(code="R-01").exists():
            self.stdout.write("demo fleet already present")
            return
        u = {
            r: User.objects.get(username=f"dev_{r}")
            for r in [
                "ops_lead",
                "engineer",
                "engineering_lead",
                "safety_reviewer",
                "founder",
                "field_operator",
            ]
        }
        for key in ("demo_mode", "allow_draft_policies"):
            DeploymentSetting.objects.update_or_create(key=key, defaults={"value": "true"})

        lead, eng = u["engineering_lead"], u["engineer"]
        s.create_hold_policy(lead, rules=DRAFT_POLICY_RULES)  # stays DRAFT on purpose
        s.create_pattern(lead, code="P-A", hardware_rev="SYN-B", release="4.3.0", firmware_set="FW-12")
        s.create_pattern(lead, code="P-B", hardware_rev="SYN-B", release="4.4.0-rc", firmware_set="FW-13")
        s.decide_pattern(lead, pattern_code="P-A", status="approved_home", reason="Synthetic demo approval")
        s.decide_pattern(lead, pattern_code="P-B", status="engineering_only", reason="Test build")
        s.publish_capability_profile(
            eng,
            pattern_code="P-A",
            stop_behaviour="SYNTHETIC DEMO placeholder - not a real robot",
            lift_crew=2,
            transport_posture="SYNTHETIC placeholder",
            max_runtime_min=120,
            min_departure_soc_pct=80,
            charge_turnaround_min=180,
            load_unload_min=20,
        )
        prof = CapabilityProfile.objects.get(pattern__code="P-A", version_no=1)
        s.approve_capability_profile(lead, profile_id=prof.pk, slot="engineering")
        s.approve_capability_profile(u["safety_reviewer"], profile_id=prof.pk, slot="safety")
        s.publish_capability_profile(eng, pattern_code="P-B")  # everything unknown
        for code in ("P-A", "P-B"):
            InspectionTemplate.objects.create(
                pattern=ManifestPattern.objects.get(code=code), version="T1", items=TEMPLATE_ITEMS
            )

        for n in range(1, 6):
            code = f"R-0{n}"
            pattern = "P-B" if n == 5 else "P-A"
            s.register_robot(u["ops_lead"], code=code, serial=f"SYN-000{n}")
            s.record_manifest(
                eng,
                robot_code=code,
                pattern_code=pattern,
                autonomy_version="SYN-auto-7",
                safety_config_hash=f"syn{n:061d}",
                calibration_ref=f"CAL-{n}",
            )
            s.transition_lifecycle(
                lead, robot_code=code, to="validation", reason="Synthetic commissioning done"
            )
            if n != 5:
                s.transition_lifecycle(
                    lead, robot_code=code, to="in_service", reason="Synthetic validation done"
                )
                s.grant_base_authorization(lead, robot_code=code)

        ops, field = "dev_ops_lead", "dev_field_operator"
        s.submit_inspection(
            u["field_operator"],
            robot_code="R-02",
            template_version="T1",
            answers={**ALL_PASS, "I6": "fail"},
            performer=field,
            verifier=ops,
        )
        h = s.place_hold(
            u["ops_lead"],
            scope="robot",
            target="R-03",
            source_type="incident",
            source_ref="INC-001",
            reason="Fell during visit V-002 (synthetic); quarantined",
        )
        s.approve_release(
            eng,
            hold_code=h["entity_id"],
            slot="eng_review_1",
            role_used="engineer",
            note="Root cause: rug edge (synthetic)",
        )
        s.approve_release(lead, hold_code=h["entity_id"], slot="eng_review_2", role_used="engineering_lead")
        s.approve_release(
            u["safety_reviewer"], hold_code=h["entity_id"], slot="safety", role_used="safety_reviewer"
        )
        self.stdout.write(
            "demo fleet created: R-01, R-04 eligible; R-02 inspection hold; "
            "R-03 incident hold awaiting founder; R-05 engineering-only build"
        )
