"""SYNTHETIC readiness demo: an approved synthetic_demo policy and complete departure evidence for the
confirmed demo visit, all created through the commands. Run after seed_dev, seed_fleet and seed_planning.

Evidence goes stale on the policy's clock (battery 2 h, inspection and equipment 4 h), exactly as real
evidence would; re-record it from the readiness page, or re-create the database, to demo again later.
Nothing here is real-world clearance: the policy is synthetic_demo and only works while demo_mode is on.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.management.commands.seed_dev import seed_grant
from fleet import services as fleet_services
from fleet.management.commands.seed_fleet import ALL_PASS
from planning import services as p
from planning.models import Visit
from readiness import evidence as rx
from readiness.models import ReadinessPolicy

User = get_user_model()

REQUIREMENTS = {
    "authorization_ttl_min": 90,
    "evidence": [
        {"kind": "pre_departure_inspection", "max_age_min": 240},
        {"kind": "equipment_check", "max_age_min": 240},
        {"kind": "battery_check", "max_age_min": 120},
    ],
    "required_equipment": ["restraint_kit", "recovery_kit", "charger"],
    "recovery_plan_required": True,
}


class Command(BaseCommand):
    help = "Create the synthetic readiness demo (idempotent: skips if a readiness policy exists)."

    def handle(self, *args: object, **opts: object) -> None:
        if ReadinessPolicy.objects.exists():
            self.stdout.write("readiness demo already present")
            return
        password = os.environ.get("DEV_SEED_PASSWORD")
        if not password:
            raise CommandError("DEV_SEED_PASSWORD is not set (see .env.example)")
        u = {
            r: User.objects.get(username=f"dev_{r}")
            for r in ("ops_lead", "field_operator", "engineer", "engineering_lead", "safety_reviewer")
        }
        # Policy approval is independent of its author, so a second engineering lead approves.
        lead2, created = User.objects.get_or_create(username="dev_engineering_lead_2")
        if created:
            lead2.set_password(password)
            lead2.save()
            seed_grant(lead2.pk, "engineering_lead")  # v2 03A-3: the owner-only seed function

        version = int(
            rx.create_readiness_policy(
                u["engineering_lead"],
                requirements=REQUIREMENTS,
                source="synthetic_demo",
                notes="SYNTHETIC demo policy - not an approved procedure; refused outside demo mode",
            )["entity_id"]
        )
        rx.approve_readiness_policy(lead2, version=version, slot="engineering")
        rx.approve_readiness_policy(u["safety_reviewer"], version=version, slot="safety")

        ops = u["ops_lead"]
        p.register_person(ops, username="dev_engineer", code="P-ENG")
        p.register_person(ops, username="dev_engineering_lead", code="P-EL")

        visit = Visit.objects.filter(status="confirmed").order_by("code").first()
        if visit is None or visit.current_revision is None or visit.current_revision.robot is None:
            self.stdout.write("no confirmed visit to prepare; readiness policy only")
            return
        robot = visit.current_revision.robot.code
        insp = fleet_services.submit_inspection(
            ops,
            robot_code=robot,
            template_version="T1",
            answers=dict(ALL_PASS),
            performer="dev_ops_lead",
            verifier="dev_field_operator",
            visit_ref=visit.code,
        )["entity_id"]
        rx.record_evidence(
            ops, visit_code=visit.code, kind="pre_departure_inspection", result="pass", inspection_code=insp
        )
        rx.record_evidence(
            u["field_operator"],
            visit_code=visit.code,
            kind="equipment_check",
            result="pass",
            details={"items": {"restraint_kit": "present", "recovery_kit": "present", "charger": "present"}},
        )
        rx.record_evidence(
            u["field_operator"],
            visit_code=visit.code,
            kind="battery_check",
            result="pass",
            details={"soc_pct": 92},
        )
        rx.record_recovery_plan(
            ops,
            visit_code=visit.code,
            on_call_code="P-ENG",
            escalation_code="P-EL",
            escalation_path="SYNTHETIC: on-call engineer -> engineering lead -> ops lead",
            required_equipment=["recovery_kit"],
        )
        self.stdout.write(
            f"readiness demo created: policy v{version} (synthetic_demo, approved); "
            f"{visit.code} has inspection, equipment, battery evidence and a recovery plan"
        )
