"""SYNTHETIC field-execution demo setup (Slice 4): a synthetic site-check template and an approved
synthetic_demo incident policy, created through the commands where commands exist. Run after seed_readiness.

The demo visit is NOT departed here: authorize and record departure from the readiness page, then work the run
on /execution/. Nothing here is a real procedure: the template items and the severity scale are placeholders,
usable only while demo_mode is on, and production refuses both.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from execution import policy as ip
from execution.models import IncidentPolicy, SiteCheckTemplate

User = get_user_model()

TEMPLATE_ITEMS = [
    {"id": "c1", "label": "SYNTHETIC: walkway and work area clear"},
    {"id": "c2", "label": "SYNTHETIC: participants briefed on the visit"},
    {"id": "c3", "label": "SYNTHETIC: no unconsented people present"},
]
RULES = {
    "severity_scale": ["S0", "S1", "S2", "S3"],  # placeholder labels, not a real severity standard
    "escalation_required_from": "S2",
    "downgrade_roles": ["safety_reviewer"],
}


class Command(BaseCommand):
    help = "Create the synthetic execution demo setup (idempotent)."

    def handle(self, *args: object, **opts: object) -> None:
        ops = User.objects.get(username="dev_ops_lead")
        if not SiteCheckTemplate.objects.exists():
            # No command creates templates (they are engineering-supplied content); the owner seeds one.
            SiteCheckTemplate.objects.create(
                version="SC-DEMO-1", items=TEMPLATE_ITEMS, source="synthetic_demo", recorded_by=ops
            )
            self.stdout.write("site check template SC-DEMO-1 (synthetic)")
        if IncidentPolicy.objects.exists():
            self.stdout.write("incident policy already present")
            return
        eng = User.objects.get(username="dev_engineering_lead")
        eng2 = User.objects.get(username="dev_engineering_lead_2")
        safety = User.objects.get(username="dev_safety_reviewer")
        version = int(
            ip.create_incident_policy(
                eng, rules=RULES, source="synthetic_demo", notes="SYNTHETIC demo severity scale"
            )["version"]
        )
        ip.approve_incident_policy(eng2, version=version, slot="engineering")
        ip.approve_incident_policy(safety, version=version, slot="safety")
        self.stdout.write(f"incident policy v{version} (synthetic_demo, approved for demo mode only)")
