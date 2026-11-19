"""SYNTHETIC Slice 7 guardrail demo inputs (docs/07a_production_guardrails_contract.md sections 0a,
4.8, 6.5, 7.5, 8.3, 10). Demo mode ONLY: it refuses to run unless demo_mode = 'true'.

It never writes approved_procedure, engineering_doc or owner_approved, so every production gate keeps refusing
what it creates by source. Every value is clearly labelled SYNTHETIC and is not a company value. Idempotent:
each step runs only when its prerequisites exist and its own rows do not, so `make seed` runs it twice:

- after seed_planning: readiness_inspector grants for dev_ops_lead and dev_field_operator (granted by
  dev_safety_reviewer, effective now, basis_ref SYNTHETIC-TRAINING); one synthetic_demo periodic rule per
  pattern (4,320 min, author dev_engineer, approved by dev_engineering_lead + dev_safety_reviewer); one fresh
  synthetic periodic inspection per in-service robot (signed by the two qualified dev users, nothing
  backdated). This must precede seed_readiness: since Slice 7 the database refuses passing pre-departure
  evidence signed by people who are not qualified readiness inspectors.
- at the end: a synthetic_demo escalation deadline set for the active (synthetic) incident policy, approved
  by dev_safety_reviewer + dev_founder; the confirmed demo visit's pre-departure inspection and evidence are
  re-recorded when their signers are not covered and it has not departed; the synthetic close-out time zone
  and periodic due-soon lookahead settings.
"""

from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from execution.models import EscalationDeadlineSet, EscalationDeadlineSetApproval, IncidentPolicy
from fleet import services as fs
from fleet.management.commands.seed_fleet import ALL_PASS
from fleet.models import (
    DeploymentSetting,
    InspectionTemplate,
    ManifestPattern,
    PeriodicInspectionRule,
    PeriodicInspectionRuleApproval,
    Robot,
)
from planning.models import Person, Qualification, Visit
from platform_core.locks import lock_guards
from readiness import evidence as rx
from readiness.models import DepartureEvent, ReadinessEvidence

User = get_user_model()

# Slice 7 deployment setting keys (read models: Engineer C). Both are visibility settings only, never gates.
CLOSEOUT_TIMEZONE_KEY = "ops_closeout_timezone"
PERIODIC_DUE_LOOKAHEAD_KEY = "ops_periodic_due_lookahead_min"

SYNTHETIC_PERIODIC_INTERVAL_MIN = 4320  # EM decision 9: deliberately arbitrary SYNTHETIC demo value
SYNTHETIC_RULE_NOTES = "SYNTHETIC demo interval, not an engineering value"
SYNTHETIC_TRAINING = "SYNTHETIC-TRAINING"
SYNTHETIC_DEMO_TIMEZONE = "America/Los_Angeles"  # SYNTHETIC demo value, not a company value
SYNTHETIC_DUE_LOOKAHEAD_MIN = "1440"  # SYNTHETIC demo value
SYNTHETIC_DEADLINE_MIN = 600  # SYNTHETIC demo value for every level; the owners supply the real ones (D-21)
INSPECTOR_USERS = ("dev_ops_lead", "dev_field_operator")


def _demo_mode() -> bool:
    row = DeploymentSetting.objects.filter(key="demo_mode").first()
    return row is not None and row.value == "true"


class Command(BaseCommand):
    help = "Create the SYNTHETIC Slice 7 guardrail demo inputs (demo mode only, idempotent)."

    def handle(self, *args: object, **opts: object) -> None:
        if not _demo_mode():
            raise CommandError(
                "seed_guardrails runs only in demo mode (demo_mode = 'true'); nothing was written"
            )
        users = {u.username: u for u in User.objects.filter(username__startswith="dev_")}
        if "dev_safety_reviewer" not in users:
            raise CommandError("run seed_dev first")
        self._inspectors(users)
        self._periodic_rules(users)
        self._periodic_inspections(users)
        self._deadline_set(users)
        self._demo_visit_evidence(users)
        self._settings()

    # ------------------------------------------------------------------ X-8
    def _inspectors(self, users: dict[str, Any]) -> None:
        safety = users["dev_safety_reviewer"]
        for username in INSPECTOR_USERS:
            person = Person.objects.filter(user__username=username).first()
            if person is None:
                continue  # seed_planning not run yet
            if Qualification.objects.filter(
                person=person,
                kind="readiness_inspector",
                revoked_at__isnull=True,
                valid_to__gt=timezone.now(),
            ).exists():
                continue
            with transaction.atomic():
                lock_guards(update_persons=[person.pk])
                Qualification.objects.create(
                    person=person,
                    kind="readiness_inspector",
                    valid_from=None,  # type: ignore[misc]  # effective now: DB sets valid_from = granted_at
                    valid_to=timezone.now() + timedelta(days=365),
                    granted_by=safety,
                    basis_ref=SYNTHETIC_TRAINING,
                )
            self.stdout.write(f"readiness_inspector (SYNTHETIC) granted to {person.code}")

    # ------------------------------------------------------------------ X-3
    def _periodic_rules(self, users: dict[str, Any]) -> None:
        for pattern in ManifestPattern.objects.order_by("code"):
            if PeriodicInspectionRule.objects.filter(pattern=pattern).exists():
                continue
            robot_ids = list(
                Robot.objects.filter(current_manifest__pattern=pattern).values_list("pk", flat=True)
            )
            with transaction.atomic():
                lock_guards(update_patterns=[pattern.pk], update_robots=robot_ids)
                rule = PeriodicInspectionRule.objects.create(
                    pattern=pattern,
                    version_no=1,
                    interval_min=SYNTHETIC_PERIODIC_INTERVAL_MIN,
                    source="synthetic_demo",
                    notes=SYNTHETIC_RULE_NOTES,
                    created_by=users["dev_engineer"],
                )
                PeriodicInspectionRuleApproval.objects.create(
                    rule=rule, slot="engineering", approver=users["dev_engineering_lead"]
                )
                PeriodicInspectionRuleApproval.objects.create(
                    rule=rule, slot="safety", approver=users["dev_safety_reviewer"]
                )
            self.stdout.write(f"periodic rule v1 for {pattern.code}: {SYNTHETIC_RULE_NOTES}")

    def _periodic_status(self, robot: Robot) -> str:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT state FROM readiness_periodic_inspection_status(%s, fleet_production_mode(), "
                "clock_timestamp())",
                [robot.pk],
            )
            return str(cur.fetchone()[0])

    def _periodic_inspections(self, users: dict[str, Any]) -> None:
        if not Person.objects.filter(user__username__in=INSPECTOR_USERS).exists():
            return
        for robot in Robot.objects.filter(lifecycle="in_service").order_by("code"):
            if self._periodic_status(robot) in ("due", "unknown_interval"):
                continue
            if (
                robot.current_manifest is None
                or not InspectionTemplate.objects.filter(
                    pattern_id=robot.current_manifest.pattern_id, version="T1"
                ).exists()
            ):
                continue
            out = fs.submit_inspection(
                users["dev_ops_lead"],
                robot_code=robot.code,
                template_version="T1",
                answers=dict(ALL_PASS),
                performer="dev_ops_lead",
                verifier="dev_field_operator",
            )
            self.stdout.write(f"SYNTHETIC periodic inspection {out['entity_id']} for {robot.code}")

    # ------------------------------------------------------------------ X-4
    def _deadline_set(self, users: dict[str, Any]) -> None:
        with connection.cursor() as cur:
            cur.execute("SELECT execution_active_incident_policy_id()")
            pid = cur.fetchone()[0]
        if pid is None:
            return  # seed_execution not run yet
        policy = IncidentPolicy.objects.get(pk=pid)
        if EscalationDeadlineSet.objects.filter(incident_policy=policy).exists():
            return
        levels = [*policy.rules["severity_scale"], "unclassified"]
        deadlines = {
            lvl: [{"to_role": "safety_reviewer", "within_min": SYNTHETIC_DEADLINE_MIN}] for lvl in levels
        }
        with transaction.atomic():
            lock_guards(share_settings=True)  # the approval trigger asserts it (production source check)
            ds = EscalationDeadlineSet.objects.create(
                incident_policy=policy,
                version_no=1,
                deadlines=deadlines,
                source="synthetic_demo",
                notes="SYNTHETIC demo deadlines, not a company value",
                created_by=users["dev_ops_lead"],
            )
            EscalationDeadlineSetApproval.objects.create(
                deadline_set=ds, slot="safety", approver=users["dev_safety_reviewer"]
            )
            EscalationDeadlineSetApproval.objects.create(
                deadline_set=ds, slot="founder", approver=users["dev_founder"]
            )
        self.stdout.write(f"escalation deadline set v1 (SYNTHETIC) for incident policy v{policy.version}")

    # ------------------------------------------------------------------ X-8: demo visit evidence
    def _demo_visit_evidence(self, users: dict[str, Any]) -> None:
        visit = Visit.objects.filter(status="confirmed").order_by("code").first()
        if visit is None or visit.current_revision is None or visit.current_revision.robot is None:
            return
        if DepartureEvent.objects.filter(visit=visit).exists():
            return
        latest = (
            ReadinessEvidence.objects.filter(
                visit=visit,
                revision=visit.current_revision,
                kind="pre_departure_inspection",
                void__isnull=True,
            )
            .order_by("-observed_at", "-id")
            .first()
        )
        if latest is None or latest.inspection_id is None:
            return
        with connection.cursor() as cur:
            cur.execute("SELECT planning_inspection_signer_problem(%s)", [latest.inspection_id])
            if cur.fetchone()[0] is None:
                return
        ops = users["dev_ops_lead"]
        insp = fs.submit_inspection(
            ops,
            robot_code=visit.current_revision.robot.code,
            template_version="T1",
            answers=dict(ALL_PASS),
            performer="dev_ops_lead",
            verifier="dev_field_operator",
            visit_ref=visit.code,
        )["entity_id"]
        rx.record_evidence(
            ops, visit_code=visit.code, kind="pre_departure_inspection", result="pass", inspection_code=insp
        )
        self.stdout.write(f"{visit.code}: pre-departure inspection {insp} re-recorded with qualified signers")

    # ------------------------------------------------------------------ X-10 / X-3 visibility settings
    def _settings(self) -> None:
        for key, value in (
            (CLOSEOUT_TIMEZONE_KEY, SYNTHETIC_DEMO_TIMEZONE),
            (PERIODIC_DUE_LOOKAHEAD_KEY, SYNTHETIC_DUE_LOOKAHEAD_MIN),
        ):
            if not DeploymentSetting.objects.filter(key=key).exists():
                DeploymentSetting.objects.create(key=key, value=value)  # owner only (runtime role cannot)
                self.stdout.write(f"{key} = {value} (SYNTHETIC demo value, not a company value)")
