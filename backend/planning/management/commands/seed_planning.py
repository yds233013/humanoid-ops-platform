"""SYNTHETIC planning demo: sites, consent, crew, vehicles and a few visits, created through the commands.
Durations marked here are demo placeholders, never company data. Run after seed_dev and seed_fleet."""

from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from fleet.models import DeploymentSetting
from planning import services as p
from planning.models import ConsentRecord, Site, SiteAssessment

User = get_user_model()
TZ = "America/Los_Angeles"


class Command(BaseCommand):
    help = "Create the synthetic planning demo (idempotent: skips if S-001 exists)."

    def handle(self, *args: object, **opts: object) -> None:
        if Site.objects.filter(code="S-001").exists():
            self.stdout.write("planning demo already present")
            return
        u = {
            r: User.objects.get(username=f"dev_{r}")
            for r in ("ops_lead", "field_operator", "engineering_lead")
        }
        ops, field = u["ops_lead"], u["field_operator"]
        DeploymentSetting.objects.update_or_create(key="consent_form_version", defaults={"value": "CF-v1"})
        DeploymentSetting.objects.update_or_create(key="pre_visit_inspection_min", defaults={"value": "30"})

        start = timezone.now() - timedelta(days=30)
        end = timezone.now() + timedelta(days=120)
        for username, code, kinds in (
            ("dev_ops_lead", "P-OPS", ["field_lead", "robot_handler"]),
            ("dev_field_operator", "P-FLD", ["field_operator", "robot_handler"]),
        ):
            p.register_person(ops, username=username, code=code)
            for k in kinds:
                p.grant_qualification(ops, person_code=code, kind=k, valid_from=start, valid_to=end)
        p.register_vehicle(ops, code="VAN-1", robot_capacity=1, restraint_kit="SYNTHETIC restraint kit A")
        p.register_vehicle(ops, code="VAN-2")  # capacity and restraint unknown

        until = (timezone.now() + timedelta(days=90)).date()
        sites = [
            ("S-001", "Zone 2", 2, "eligible", 2),
            ("S-002", "Zone 3", 1, "eligible_restricted", 1),
            ("S-003", "Zone 1", None, "eligible", 1),
            ("S-004", "Zone 4", 2, "eligible", 2),
        ]
        for code, zone, adults, decision, consents in sites:
            p.create_site(ops, code=code, zone=zone, tz=TZ, crm_ref=f"CRM-SYN-{code}", expected_adults=adults)
            a = p.record_assessment(
                field,
                site_code=code,
                decision=decision,
                valid_until=until,
                restrictions="Ground floor only (synthetic)" if decision != "eligible" else "",
            )
            p.approve_assessment(ops, assessment_id=int(a["entity_id"]))
            for n in range(1, consents + 1):
                p.record_consent(
                    field,
                    site_code=code,
                    participant_ref=f"P{n}",
                    form_version="CF-v1",
                    scope=["presence", "recording"],
                    evidence_ref=f"DOC-SYN-{code}-{n}",
                )
        withdrawn = ConsentRecord.objects.get(site__code="S-004", participant_ref="P2")
        p.withdraw_consent(field, consent_id=withdrawn.pk, reason="Household member withdrew (synthetic)")
        assert SiteAssessment.objects.count() == 4

        day = timezone.localdate() + timedelta(days=1)

        def at(d: date, hh: int, mm: int) -> datetime:
            return p.local_to_utc(datetime.combine(d, time(hh, mm)), TZ)

        def visit(site: str, d: date, h0: int, m0: int, h1: int, m1: int) -> str:
            return p.request_visit(
                ops,
                site_code=site,
                on_site_start=at(d, h0, m0),
                on_site_end=at(d, h1, m1),
                travel_out_min=30,
                travel_back_min=30,
                tasks=["T-04"],
            )["entity_id"]

        v1 = visit("S-001", day, 10, 0, 11, 30)
        p.plan_visit(
            ops,
            visit_code=v1,
            robot_code="R-01",
            lead_code="P-OPS",
            second_code="P-FLD",
            vehicle_code="VAN-1",
        )
        p.confirm_visit(ops, visit_code=v1)
        v2 = visit("S-001", day, 11, 0, 12, 0)  # overlaps the crew and van of v1 -> blocked, explained
        p.plan_visit(
            ops,
            visit_code=v2,
            robot_code="R-04",
            lead_code="P-OPS",
            second_code="P-FLD",
            vehicle_code="VAN-1",
        )
        v3 = visit("S-002", day + timedelta(days=1), 10, 0, 11, 0)  # restricted home -> requires review
        p.plan_visit(
            ops,
            visit_code=v3,
            robot_code="R-04",
            lead_code="P-OPS",
            second_code="P-FLD",
            vehicle_code="VAN-1",
        )
        v4 = visit("S-004", day + timedelta(days=2), 13, 0, 14, 0)  # consent withdrawn -> blocked
        p.plan_visit(
            ops,
            visit_code=v4,
            robot_code="R-01",
            lead_code="P-OPS",
            second_code="P-FLD",
            vehicle_code="VAN-1",
        )
        self.stdout.write(
            f"planning demo created: {v1} confirmed, {v2} blocked (conflict), {v3} needs review, "
            f"{v4} blocked (consent)"
        )
