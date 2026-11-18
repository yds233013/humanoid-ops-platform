"""SYNTHETIC command-center thresholds (Slice 5): owner-written deployment settings used by the time-based
exception rules. The values are demo placeholders, not operating standards. Run after the other seeds."""

from django.core.management.base import BaseCommand

from fleet.models import DeploymentSetting

THRESHOLDS = {
    "ops_custody_verify_grace_min": "30",
    "ops_return_grace_min": "60",
    "ops_inspection_grace_min": "120",
    "ops_readiness_lookahead_min": "1440",
}


class Command(BaseCommand):
    help = "Write the synthetic operations thresholds (idempotent; existing values are kept)."

    def handle(self, *args: object, **opts: object) -> None:
        for key, value in THRESHOLDS.items():
            _, created = DeploymentSetting.objects.get_or_create(key=key, defaults={"value": value})
            self.stdout.write(f"{key}={value}" if created else f"{key} already set")
