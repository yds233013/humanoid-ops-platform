"""v2 02C maintenance: commands and read helpers (docs/11_v2_phase02_contract.md 3.1, 3.2, 8.2, 8.6, 8.8).

COMMANDS. Maintenance rules, their approvals and withdrawals, and maintenance records are written ONLY by
the SECURITY DEFINER functions of fleet migration 0010 (ops_app has SELECT only on those tables). This
module calls them with the actor, maps a refusal to a plain CommandError and records a denied audit row
(the function writes the committed one). A maintenance record never releases a hold, never completes a
work order and never records an inspection: a required post-maintenance inspection is a separate, normal
inspection.

STATUS. ``robot_status`` reads ``fleet_maintenance_status`` (the one SQL definition the eligibility view,
the planning triggers and readiness_issue_authorization also use) in the current mode at database time,
with the gate state, in ONE statement. Nothing here re-decides a status in Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import DatabaseError, connection, transaction

from fleet.models import (
    InspectionTemplate,
    MaintenanceRecord,
    MaintenanceRule,
    ManifestPattern,
    Robot,
)
from platform_core.commands import CommandError, Conflict, Invalid, NotAuthenticated, PermissionDenied
from platform_core.middleware import current_request_id
from platform_core.models import AuditEvent

PROPOSE = "fleet.propose_maintenance_rule"
APPROVE = "fleet.approve_maintenance_rule"
WITHDRAW = "fleet.withdraw_maintenance_rule"
RECORD = "fleet.record_maintenance"

DEMO_LABEL = "Demo value, not an engineering requirement"
GATE_INACTIVE_LABEL = "Maintenance gate not yet activated"
STATE_LABELS = {
    "not_due": "Not due",
    "due_soon": "Due soon",
    "overdue": "Overdue",
    "unknown": "Unknown",
    "no_rule": "No approved rule",
    "inspection_missing": "Inspection missing",
}
BLOCKING = ("no_rule", "unknown", "overdue", "inspection_missing")


# ------------------------------------------------------------ guarded writes (SECURITY DEFINER functions)
def _guarded(actor: Any, command: str, sql: str, params: list[Any], payload: dict[str, Any]) -> Any:
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise NotAuthenticated("Authentication required")
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, [actor.pk, *params, current_request_id.get()])
            row = cur.fetchone()
    except DatabaseError as exc:
        cause = exc.__cause__
        state = getattr(cause, "sqlstate", None)
        text = getattr(getattr(cause, "diag", None), "message_primary", None) or str(exc)
        if state == "42501":
            err: CommandError = PermissionDenied(text)
        elif state in ("23514", "23505", "23503"):
            err = Conflict(text)
        else:
            raise
        AuditEvent.objects.create(
            actor=actor, command=command, outcome=AuditEvent.OUTCOME_DENIED, payload=payload, reason=text,
            request_id=current_request_id.get(),
        )  # fmt: skip
        raise err from exc
    return row[0] if row else None


def _positive_or_none(name: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise Invalid(f"{name} must be a whole number") from None
    if n < 1 or n > 100_000:
        raise Invalid(f"{name} must be from 1 to 100000")
    return n


def propose_rule(
    actor: Any,
    *,
    pattern_code: str,
    requirement_code: str,
    calendar_days: int | None = None,
    visit_count: int | None = None,
    active_hours: int | None = None,
    warning_pct: int | None = 10,
    inspection_template_id: int | None = None,
    effective_from: datetime | None = None,
    source: str,
    procedure_ref: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """A new version (a proposal) of the pattern's rule for ``requirement_code``. Never operational until the
    engineering lead and a safety reviewer (two other people) approve it; then it applies from its effective
    time. The interval values are the company's: the platform never supplies one."""
    pattern = ManifestPattern.objects.filter(code=pattern_code).first()
    if pattern is None:
        raise Invalid(f"No configuration pattern {pattern_code}")
    days = _positive_or_none("Calendar days", calendar_days)
    visits = _positive_or_none("Completed visits", visit_count)
    hours = _positive_or_none("Active hours", active_hours)
    if effective_from is not None and effective_from.tzinfo is None:
        raise Invalid("effective_from must be timezone-aware")
    payload = {
        "pattern": pattern_code, "requirement_code": requirement_code, "calendar_days": days,
        "visit_count": visits, "active_hours": hours, "warning_pct": warning_pct,
        "inspection_template_id": inspection_template_id,
        "effective_from": effective_from.isoformat() if effective_from else None, "source": source,
    }  # fmt: skip
    rule_id = _guarded(
        actor,
        PROPOSE,
        "SELECT fleet_propose_maintenance_rule(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [pattern.pk, requirement_code, days, visits, hours, warning_pct, inspection_template_id,
         effective_from, source, procedure_ref, notes],
        payload,
    )  # fmt: skip
    rule = MaintenanceRule.objects.get(pk=rule_id)
    return {"rule_id": rule.pk, "requirement_code": rule.requirement_code, "version": rule.version_no}


def approve_rule(actor: Any, *, rule_id: int, slot: str) -> dict[str, Any]:
    _guarded(
        actor,
        APPROVE,
        "SELECT fleet_approve_maintenance_rule(%s, %s, %s, %s)",
        [rule_id, slot],
        {"rule_id": rule_id, "slot": slot},
    )
    return {"rule_id": rule_id, "slot": slot, "status": rule_status(MaintenanceRule.objects.get(pk=rule_id))}


def withdraw_rule(actor: Any, *, rule_id: int, reason: str) -> dict[str, Any]:
    _guarded(
        actor,
        WITHDRAW,
        "SELECT fleet_withdraw_maintenance_rule(%s, %s, %s, %s)",
        [rule_id, reason],
        {"rule_id": rule_id, "reason": reason},
    )
    return {"rule_id": rule_id, "status": "withdrawn"}


def record_maintenance(
    actor: Any, *, robot_code: str, requirement_code: str, notes: str = ""
) -> dict[str, Any]:
    robot = Robot.objects.filter(code=robot_code.strip().upper()).first()
    if robot is None:
        raise Invalid(f"No robot {robot_code}")
    record_id = _guarded(
        actor,
        RECORD,
        "SELECT fleet_record_maintenance(%s, %s, %s, %s, %s)",
        [robot.pk, requirement_code, notes],
        {"robot": robot.code, "requirement_code": requirement_code},
    )
    rec = MaintenanceRecord.objects.select_related("rule").get(pk=record_id)
    return {
        "record_id": rec.pk,
        "robot": robot.code,
        "requirement_code": rec.requirement_code,
        "rule_version": rec.rule.version_no,
        "performed_at": rec.performed_at.isoformat(),
        "inspection_required": rec.rule.inspection_template_id is not None,
    }


# ------------------------------------------------------------------ reads
def gate_active() -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT fleet_maintenance_gate_active()")
        return bool(cur.fetchone()[0])


@dataclass(frozen=True)
class Requirement:
    """One row of fleet_maintenance_status, in plain fields for the screens."""

    requirement_code: str | None
    state: str
    blocking: bool
    rule_id: int | None
    rule_version: int | None
    rule_source: str | None
    calendar_days: int | None
    visit_count: int | None
    active_hours: int | None
    warning_pct: int | None
    record_id: int | None
    performed_at: datetime | None
    visits_used: int | None
    hours_used: float | None
    open_runs: str | None
    due_at: datetime | None
    inspection_template_id: int | None
    inspection_id: int | None
    reason: str | None

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def demo(self) -> bool:
        return self.rule_source == "synthetic_demo"

    @property
    def tone(self) -> str:
        return "bad" if self.blocking else ("warn" if self.state == "due_soon" else "ok")


@dataclass
class RobotMaintenance:
    gate_active: bool
    production: bool
    rows: list[Requirement] = field(default_factory=list)

    @property
    def blocking(self) -> list[Requirement]:
        return [r for r in self.rows if r.blocking]

    @property
    def blocks_now(self) -> bool:
        return self.gate_active and bool(self.blocking)


STATUS_SQL = """
SELECT g.active, g.production, s.requirement_code, s.state, s.blocking, s.rule_id, s.rule_version,
       s.rule_source, s.calendar_days, s.visit_count, s.active_hours, s.warning_pct, s.record_id,
       s.performed_at, s.visits_used, s.hours_used, s.open_runs, s.due_at, s.inspection_template_id,
       s.inspection_id, s.reason
FROM (SELECT fleet_maintenance_gate_active() AS active, fleet_production_mode() AS production) g
LEFT JOIN LATERAL fleet_maintenance_status(%s, g.production, coalesce(%s, clock_timestamp())) s ON true
ORDER BY s.requirement_code NULLS FIRST
"""


def robot_status(robot: Robot, at: datetime | None = None) -> RobotMaintenance:
    """The maintenance status of ``robot`` in the current mode (one statement)."""
    with connection.cursor() as cur:
        cur.execute(STATUS_SQL, [robot.pk, at])
        rows = cur.fetchall()
    out = RobotMaintenance(gate_active=bool(rows[0][0]), production=bool(rows[0][1]))
    for r in rows:
        if r[3] is None:
            continue
        values = list(r[2:])
        values[13] = float(values[13]) if values[13] is not None else None  # hours_used (numeric)
        out.rows.append(Requirement(*values))
    return out


def rule_status(rule: MaintenanceRule) -> str:
    """proposed | approved (both slots) | withdrawn. Whether an approved version is the one in force is shown
    by the robot status (it also depends on the effective time and newer versions)."""
    if _has_withdrawal(rule):
        return "withdrawn"
    slots = {a.slot for a in rule.approvals.all()}
    return "approved" if {"engineering", "safety"} <= slots else "proposed"


def pattern_rules(pattern: ManifestPattern) -> list[dict[str, Any]]:
    """Every version of every maintenance requirement of ``pattern`` (newest first), for the configuration
    page. Three statements whatever the number of rules."""
    rules = list(
        MaintenanceRule.objects.filter(pattern=pattern)
        .select_related("created_by", "inspection_template", "withdrawal", "withdrawal__withdrawn_by")
        .prefetch_related("approvals__approver")
        .order_by("requirement_code", "-version_no")
    )
    out = []
    for r in rules:
        approvals = {a.slot: a for a in r.approvals.all()}
        withdrawal = r.withdrawal if _has_withdrawal(r) else None
        approved = {"engineering", "safety"} <= set(approvals)
        status = "withdrawn" if withdrawal else ("approved" if approved else "proposed")
        out.append({"rule": r, "approvals": approvals, "withdrawal": withdrawal, "status": status,
                    "demo": r.source == "synthetic_demo"})  # fmt: skip
    return out


def _has_withdrawal(rule: MaintenanceRule) -> bool:
    try:
        return rule.withdrawal is not None
    except MaintenanceRule.withdrawal.RelatedObjectDoesNotExist:  # type: ignore[attr-defined]
        return False


def pattern_templates(pattern: ManifestPattern) -> list[InspectionTemplate]:
    return list(InspectionTemplate.objects.filter(pattern=pattern).order_by("version"))
