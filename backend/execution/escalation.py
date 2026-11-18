"""Incident escalations and their acknowledgments (Slice 4, Engineer C). Contract sections 2 and 4.

An escalation asks a role (optionally one named person holding it) for an action. It is acknowledged once,
by a user who holds the target role now (and is that person, when one is named). No escalation after the
incident is closed. Both commands lock only the incident U (the stream's parent row); the database re-checks
every rule (execution migration 0002). SYNTHETIC data only.
"""

from typing import Any

from execution.incidents import incident_status
from execution.models import EscalationAck, Incident, IncidentEscalation
from execution.policy import KNOWN_ROLES, has_role_now
from planning.models import Person
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, run_command
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint


def _incident(code: str) -> Incident:
    try:
        return Incident.objects.get(code=code)
    except Incident.DoesNotExist:
        raise Invalid(f"No incident {code}") from None


def escalation_dict(e: IncidentEscalation) -> dict[str, Any]:
    ack = EscalationAck.objects.filter(escalation=e).first()
    return {
        "id": e.pk,
        "incident": e.incident.code,
        "to_role": e.to_role,
        "to_person": e.to_person.code if e.to_person is not None else None,
        "requested_action": e.requested_action,
        "acknowledged": ack is not None,
        "acknowledged_by": ack.acknowledged_by.get_username() if ack else None,
    }


def escalate_incident(
    actor: Any,
    *,
    incident_code: str,
    to_role: str,
    requested_action: str,
    to_person_code: str | None = None,
) -> dict[str, Any]:
    """Escalate an open incident to a role (and optionally a named person who holds that role now).
    Locks: incident U."""

    def handler() -> Outcome:
        if to_role not in KNOWN_ROLES:
            raise Invalid(f"to_role must be one of {sorted(KNOWN_ROLES)}")
        if not requested_action.strip():
            raise Invalid("Say what action is requested")
        person = None
        if to_person_code:
            try:
                person = Person.objects.get(code=to_person_code)
            except Person.DoesNotExist:
                raise Invalid(f"No person {to_person_code}") from None
        i = _incident(incident_code)
        checkpoint("read:execution.escalate_incident")
        lock_guards(update_incidents=[i.pk], label="execution.escalate_incident")
        if incident_status(i) == "closed":
            raise Conflict(f"Incident {i.code} is closed; report a new incident instead of escalating it")
        if person is not None and not has_role_now(person.user_id, to_role):
            raise Conflict(
                f"{person.code} does not hold the role {to_role}, so cannot be the escalation target"
            )
        e = IncidentEscalation.objects.create(
            incident=i,
            to_role=to_role,
            to_person=person,
            requested_action=requested_action,
            recorded_by=actor,
        )
        return Outcome("incident_escalation", str(e.pk), escalation_dict(e))

    return run_command(
        actor,
        "execution.escalate_incident",
        permission="execution.triage_incident",
        payload={
            "incident": incident_code,
            "to_role": to_role,
            "to_person": to_person_code,
            "requested_action": requested_action,
        },
        handler=handler,
    )


def acknowledge_escalation(actor: Any, *, escalation_id: int) -> dict[str, Any]:
    """Acknowledge an escalation addressed to a role the actor holds now (and to the actor, when a person is
    named). Once only. Locks: incident U."""

    def handler() -> Outcome:
        try:
            e = IncidentEscalation.objects.get(pk=escalation_id)
        except IncidentEscalation.DoesNotExist:
            raise Invalid(f"No escalation {escalation_id}") from None
        checkpoint("read:execution.acknowledge_escalation")
        lock_guards(update_incidents=[e.incident_id], label="execution.acknowledge_escalation")
        if EscalationAck.objects.filter(escalation=e).exists():
            raise Conflict("This escalation is already acknowledged")
        if not has_role_now(actor.pk, e.to_role):
            raise PermissionDenied(
                f"Only a user holding the role {e.to_role} can acknowledge this escalation"
            )
        target = e.to_person
        if target is not None and target.user_id != actor.pk:
            raise PermissionDenied(f"Only {target.code} can acknowledge this escalation")
        EscalationAck.objects.create(escalation=e, acknowledged_by=actor)
        return Outcome("incident_escalation", str(e.pk), escalation_dict(e))

    return run_command(
        actor,
        "execution.acknowledge_escalation",
        permission="execution.read",  # gate only: the target role is checked above, under the lock
        payload={"escalation_id": escalation_id},
        handler=handler,
    )
