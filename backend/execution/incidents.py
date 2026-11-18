"""Incident reporting, fault observations, severity and status (Slice 4, Engineer C).
See docs/04_field_execution_contract.md sections 2 (Incident ...), 3 (Incident), 4 and 5.

Every incident holds the robot in the same transaction (contract D2). report_incident never reads the incident
policy and never depends on the mode: it works with no policy, with no run, after the session stopped
and after the trip closed. Status and severity are independent, append-only streams. Resolving or closing an
incident never releases its hold: the only write to fleet_hold in execution code is the insert in
report_incident; release stays with the fleet hold-release workflow.

Locks (U = FOR UPDATE, S = FOR SHARE), after run_command's role-grant S:
  report_incident          robot U, visit S + run U (only when the robot has an open run); the fleet hold
                           insert trigger then bumps the robot (held) and the incident trigger re-locks it
  add_fault_observation    incident U
  decide_severity          settings S, the active incident policy and every newer one S, incident U
  change_incident_status   incident U; to 'closed' first settings S + the latest severity's policy S
PostgreSQL re-checks every rule (execution migration 0002); these checks give the explained answer.
SYNTHETIC data only: nothing here stops, moves or recovers a robot.
"""

from datetime import datetime
from typing import Any

from django.db import connection

from execution.custody import current_custody, open_run_id
from execution.models import (
    INCIDENT_EVENT_TYPES,
    INCIDENT_STATUSES,
    KNOWLEDGE_SOURCES,
    CustodyEvent,
    ExecutionRun,
    FaultObservation,
    Incident,
    IncidentEscalation,
    IncidentPolicy,
    IncidentStatusChange,
    Interruption,
    SessionEvent,
    SeverityDecision,
)
from execution.policy import current_incident_policy, has_role_now, level_rank, policy_usable
from fleet import services as fleet_services
from fleet.models import Hold, Robot
from platform_core.commands import Conflict, Invalid, Outcome, RetryTransaction, run_command
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint

EVENT_TYPES = [k for k, _ in INCIDENT_EVENT_TYPES]
KNOWLEDGE = [k for k, _ in KNOWLEDGE_SOURCES]
STATUS_STEPS = ["reported", *[k for k, _ in INCIDENT_STATUSES]]  # reported -> triaged -> ... -> closed
FAULT_STATUSES = [k for k, _ in FaultObservation.STATUSES]


# ------------------------------------------------------------------ small helpers
def db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        value: datetime = cur.fetchone()[0]
        return value


def next_code(seq: str, prefix: str) -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT execution_code(%s, nextval(%s))", [prefix, seq])
        return str(cur.fetchone()[0])


def session_state(run_id: int) -> str:
    """not_started | active | paused | stopped (observations do not change the state)."""
    last = SessionEvent.objects.filter(run_id=run_id).exclude(kind="observation").order_by("-seq").first()
    if last is None:
        return "not_started"
    return {"start": "active", "resume": "active", "pause": "paused", "stop": "stopped"}[last.kind]


def next_session_seq(run_id: int) -> int:
    last = SessionEvent.objects.filter(run_id=run_id).order_by("-seq").first()
    return 1 if last is None else last.seq + 1


def incident_status(incident: Incident) -> str:
    last = IncidentStatusChange.objects.filter(incident=incident).order_by("-recorded_at", "-id").first()
    return last.to_status if last else "reported"


def latest_severity(incident: Incident) -> SeverityDecision | None:
    return SeverityDecision.objects.filter(incident=incident).order_by("-recorded_at", "-id").first()


def _incident(code: str) -> Incident:
    try:
        return Incident.objects.get(code=code)
    except Incident.DoesNotExist:
        raise Invalid(f"No incident {code}") from None


def _robot(code: str) -> Robot:
    try:
        return Robot.objects.get(code=code)
    except Robot.DoesNotExist:
        raise Invalid(f"No robot {code}") from None


def incident_dict(i: Incident) -> dict[str, Any]:
    sev = latest_severity(i)
    hold = Hold.objects.get(pk=i.hold_id)
    return {
        "code": i.code,
        "robot": i.robot.code,
        "run": i.run.code if i.run is not None else None,
        "visit": i.visit.code if i.visit is not None else None,
        "event_type": i.event_type,
        "status": incident_status(i),
        "severity": sev.level if sev else "unclassified",
        "hold": hold.code,
        "hold_released": hold.released_at is not None,
    }


# ------------------------------------------------------------------ report_incident steps
# Each step is a module-level function so a test can inject a failure at exactly that step.
def _place_hold(actor: Any, robot: Robot, code: str, event_type: str) -> Hold:
    return fleet_services._create_hold(
        actor,
        scope="robot",
        robot=robot,
        pattern=None,
        source_type="incident",
        source_ref=code,
        reason=f"Incident {code} ({event_type}) reported; release through the hold-release workflow",
    )


def _insert_incident(actor: Any, **fields: Any) -> Incident:
    return Incident.objects.create(recorded_by=actor, **fields)


def _mark_unaccounted(actor: Any, incident: Incident, run: ExecutionRun | None) -> CustodyEvent | None:
    """any -> unaccounted at the current time; earlier custody is never rewritten. Skipped when the robot is
    already unaccounted."""
    last = CustodyEvent.objects.filter(robot_id=incident.robot_id).order_by("-seq").first()
    from_kind = last.to_kind if last else "hub"  # no events and (necessarily) no run: D1 baseline hub
    if from_kind == "unaccounted":
        return None
    return CustodyEvent.objects.create(
        robot_id=incident.robot_id,
        seq=(last.seq + 1) if last else 1,
        from_kind=from_kind,
        to_kind="unaccounted",
        run=run,
        trip_id=run.trip_id if run else None,
        visit_id=run.visit_id if run else None,
        custodian=None,
        received_by=None,
        basis="handoff",
        incident=incident,
        note=f"Reported unaccounted in incident {incident.code}",
        recorded_by=actor,
    )


def _open_incident_interruption(actor: Any, incident: Incident, run: ExecutionRun) -> Interruption:
    return Interruption.objects.create(
        code=next_code("execution_interruption_code_seq", "IN"),
        run=run,
        source="incident",
        incident=incident,
        reason=f"Incident {incident.code} ({incident.event_type}) reported",
        recorded_by=actor,
    )


def _pause_for_incident(actor: Any, run: ExecutionRun, interruption: Interruption) -> SessionEvent:
    return SessionEvent.objects.create(
        run=run,
        seq=next_session_seq(run.pk),
        kind="pause",
        interruption=interruption,
        note=f"Paused by incident {interruption.incident.code if interruption.incident else ''}".strip(),
        recorded_by=actor,
    )


# ------------------------------------------------------------------ commands
def report_incident(
    actor: Any,
    *,
    robot_code: str,
    event_type: str,
    description: str,
    occurred_at: datetime,
    knowledge_source: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Report an incident and hold the robot, in ONE transaction, in this exact order (contract section 4):
    robot U (+ visit S, run U when the robot has an open run) -> blocking robot hold (source incident,
    source_ref = the incident code) -> Incident -> unaccounted custody (robot_unaccounted only) -> incident
    Interruption + a pause if the session is active (open run only) -> audit. Any failure rolls back all
    of it. Never reads the incident policy; works with no run, after stop and after trip closure.
    Idempotent on key."""
    payload = {
        "robot": robot_code,
        "event_type": event_type,
        "description": description,
        "occurred_at": occurred_at.isoformat() if isinstance(occurred_at, datetime) else str(occurred_at),
        "knowledge_source": knowledge_source,
    }

    def handler() -> Outcome:
        if event_type not in EVENT_TYPES:
            raise Invalid(f"event_type must be one of {EVENT_TYPES}")
        if knowledge_source not in KNOWLEDGE:
            raise Invalid(f"knowledge_source must be one of {KNOWLEDGE}")
        if not description.strip():
            raise Invalid("A description of what happened is required")
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
            raise Invalid("occurred_at must be a timezone-aware date and time")
        robot = _robot(robot_code)
        read_run = open_run_id(robot.pk)
        run = ExecutionRun.objects.get(pk=read_run) if read_run else None
        checkpoint("read:execution.report_incident")
        lock_guards(
            update_robots=[robot.pk],
            share_visits=[run.visit_id] if run else [],
            update_runs=[run.pk] if run else [],
            label="execution.report_incident",
        )
        if open_run_id(robot.pk) != read_run:
            raise RetryTransaction()  # a run opened or closed between our read and our locks
        robot.refresh_from_db()
        if occurred_at > db_now():
            raise Invalid("occurred_at cannot be in the future")

        code = next_code("execution_incident_code_seq", "IC")
        hold = _place_hold(actor, robot, code, event_type)
        incident = _insert_incident(
            actor,
            code=code,
            robot=robot,
            manifest_id=robot.current_manifest_id,
            run=run,
            visit_id=run.visit_id if run else None,
            event_type=event_type,
            description=description,
            occurred_at=occurred_at,
            knowledge_source=knowledge_source,
            hold=hold,
        )
        custody_event = None
        if event_type == "robot_unaccounted":
            custody_event = _mark_unaccounted(actor, incident, run)
        interruption = pause = None
        if run is not None:
            interruption = _open_incident_interruption(actor, incident, run)
            if session_state(run.pk) == "active":
                pause = _pause_for_incident(actor, run, interruption)
        return Outcome(
            "incident",
            code,
            {
                "code": code,
                "robot": robot.code,
                "hold": hold.code,
                "hold_releasable": bool(hold.required_slots),
                "run": run.code if run else None,
                "visit": run.visit.code if run else None,
                "severity": "unclassified",
                "status": "reported",
                "custody_event_seq": custody_event.seq if custody_event else None,
                "custody": current_custody(robot.pk).label,
                "interruption": interruption.code if interruption else None,
                "session_paused": pause is not None,
            },
        )

    return run_command(
        actor,
        "execution.report_incident",
        permission="execution.report_incident",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
    )


def add_fault_observation(
    actor: Any, *, incident_code: str, component: str, status: str, evidence_ref: str = ""
) -> dict[str, Any]:
    """A suspected or confirmed fault on a named component. Corrections are new rows. Locks: incident U."""

    def handler() -> Outcome:
        if not component.strip():
            raise Invalid("Name the component the fault concerns")
        if status not in FAULT_STATUSES:
            raise Invalid(f"status must be one of {FAULT_STATUSES}")
        i = _incident(incident_code)
        lock_guards(update_incidents=[i.pk], label="execution.add_fault_observation")
        f = FaultObservation.objects.create(
            incident=i, component=component, status=status, evidence_ref=evidence_ref, recorded_by=actor
        )
        return Outcome(
            "fault_observation",
            str(f.pk),
            {"incident": i.code, "component": component, "status": status, "evidence_ref": evidence_ref},
        )

    return run_command(
        actor,
        "execution.add_fault_observation",
        permission="execution.triage_incident",
        payload={
            "incident": incident_code,
            "component": component,
            "status": status,
            "evidence_ref": evidence_ref,
        },
        handler=handler,
    )


def _policy_ids_from(policy_id: int) -> list[int]:
    return list(IncidentPolicy.objects.filter(pk__gte=policy_id).order_by("pk").values_list("pk", flat=True))


def decide_severity(actor: Any, *, incident_code: str, level: str, reason: str) -> dict[str, Any]:
    """Classify severity under the ACTIVE incident policy (usable in the current mode). With no active policy
    the incident stays 'unclassified' (refused). A level lower than the latest decision is a downgrade: it
    needs a role in the policy's downgrade_roles and a decider other than the reporter.
    Locks: settings S, the active policy and every newer policy S, incident U."""

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("A reason is required")
        i = _incident(incident_code)
        read_policy = current_incident_policy()
        checkpoint("read:execution.decide_severity")
        lock_guards(
            share_settings=True,
            share_incident_policies=_policy_ids_from(read_policy.pk) if read_policy else [],
            update_incidents=[i.pk],
            label="execution.decide_severity",
        )
        policy = current_incident_policy()
        if (policy.pk if policy else None) != (read_policy.pk if read_policy else None):
            raise RetryTransaction()  # a policy was approved or withdrawn between our read and our locks
        if policy is None:
            raise Conflict(
                "There is no active incident policy, so severity stays unclassified. The robot is held "
                "regardless; an approved incident policy is needed to classify severity"
            )
        if not policy_usable(policy):
            raise Conflict(
                f"The active incident policy v{policy.version} is synthetic_demo and cannot classify "
                "severity outside demo mode; severity stays unclassified"
            )
        new_rank = level_rank(policy, level)
        if new_rank is None:
            raise Invalid(
                f"Level {level!r} is not in incident policy v{policy.version}'s severity scale "
                f"{policy.rules['severity_scale']}"
            )
        prev = latest_severity(i)
        downgrade = False
        if prev is not None:
            prev_rank = level_rank(policy, prev.level)
            downgrade = prev_rank is None or new_rank < prev_rank
        if downgrade:
            assert prev is not None
            roles = policy.rules.get("downgrade_roles") or []
            if not any(has_role_now(actor.pk, r) for r in roles):
                raise Conflict(
                    f"Lowering severity from {prev.level} to {level} needs one of the roles {roles} "
                    f"(incident policy v{policy.version})"
                )
            if i.recorded_by_id == actor.pk:
                raise Conflict("The reporter of an incident cannot downgrade its severity")
        SeverityDecision.objects.create(
            incident=i, level=level, policy=policy, reason=reason, recorded_by=actor
        )
        return Outcome(
            "incident",
            i.code,
            {
                "severity": level,
                "previous_severity": prev.level if prev else "unclassified",
                "downgrade": downgrade,
                "policy_version": policy.version,
                "escalation_required": new_rank
                >= (level_rank(policy, policy.rules["escalation_required_from"]) or 0),
            },
        )

    return run_command(
        actor,
        "execution.decide_severity",
        permission="execution.change_severity",
        payload={"incident": incident_code, "level": level, "reason": reason},
        handler=handler,
    )


def _closing_problem(i: Incident) -> str | None:
    """Why the incident cannot be closed yet, or None. Read under the incident lock."""
    unacked = IncidentEscalation.objects.filter(incident=i, ack__isnull=True).order_by("pk")
    if unacked.exists():
        roles = ", ".join(e.to_role for e in unacked)
        return f"Every escalation must be acknowledged before closing (waiting on: {roles})"
    sev = latest_severity(i)
    if sev is not None:
        policy = IncidentPolicy.objects.get(pk=sev.policy_id)
        rank = level_rank(policy, sev.level) or 0
        threshold = level_rank(policy, policy.rules["escalation_required_from"]) or 0
        if rank >= threshold and not IncidentEscalation.objects.filter(incident=i).exists():
            return (
                f"Severity {sev.level} requires at least one escalation before closing "
                f"(policy v{policy.version}: from {policy.rules['escalation_required_from']})"
            )
    return None


def change_incident_status(actor: Any, *, incident_code: str, to_status: str) -> dict[str, Any]:
    """reported -> triaged -> response_in_progress -> resolved -> closed, one step at a time. Closing needs
    every escalation acknowledged and, when the latest severity is at or above the policy's
    escalation_required_from, at least one escalation. Never touches the hold: a resolved or closed incident
    keeps the robot held until the fleet hold-release workflow releases it.
    Locks: incident U; to 'closed' first settings S and the latest severity decision's policy S."""
    closing = to_status == "closed"

    def handler() -> Outcome:
        if to_status not in STATUS_STEPS[1:]:
            raise Invalid(f"to_status must be one of {STATUS_STEPS[1:]}")
        i = _incident(incident_code)
        read_sev = latest_severity(i) if closing else None
        checkpoint("read:execution.change_incident_status")
        lock_guards(
            share_settings=closing,
            share_incident_policies=[read_sev.policy_id] if read_sev else [],
            update_incidents=[i.pk],
            label="execution.change_incident_status",
        )
        if closing:
            sev = latest_severity(i)
            if (sev.pk if sev else None) != (read_sev.pk if read_sev else None):
                raise RetryTransaction()  # severity changed between our read and our locks
        current = incident_status(i)
        if STATUS_STEPS.index(to_status) != STATUS_STEPS.index(current) + 1:
            raise Conflict(
                f"Incident status goes one step at a time: {current} cannot become {to_status}"
                + (
                    f" (next is {STATUS_STEPS[STATUS_STEPS.index(current) + 1]})"
                    if current != "closed"
                    else ""
                )
            )
        if closing:
            problem = _closing_problem(i)
            if problem:
                raise Conflict(problem)
        IncidentStatusChange.objects.create(incident=i, to_status=to_status, recorded_by=actor)
        hold = Hold.objects.get(pk=i.hold_id)
        return Outcome(
            "incident",
            i.code,
            {
                "status": to_status,
                "previous_status": current,
                "hold": hold.code,
                "hold_released": hold.released_at is not None,
            },
        )

    return run_command(
        actor,
        "execution.change_incident_status",
        permission="execution.close_incident" if closing else "execution.triage_incident",
        payload={"incident": incident_code, "to_status": to_status},
        handler=handler,
    )
