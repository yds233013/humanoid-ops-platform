"""Return reconciliation and trip closure (Slice 4, Engineer B). Contract section 2 (ReturnReconciliation,
TripClosure), section 11 C and D.

SYNTHETIC data only. A vehicle that is back is not a robot that is back: reconciliation looks at custody, the
planned cargo, the post-visit inspection and the stated discrepancies. The outcome is computed, never chosen:
'reconciled' only when every condition holds, otherwise 'unresolved' with every reason in plain words. Only a
closure that references the run's latest, reconciled reconciliation closes the run. A held robot's trip may
close (the hold stays with the fleet release workflow); a missing robot's trip never can.

Locks (after run_command's role-grant S):
  reconcile_return   robot U, visit S, trip U, run U
  close_trip         robot S, visit S, trip U, run U
Both lock the run U and the insert triggers re-lock it, so a reconciliation and a closure are totally ordered:
no reconciliation is ever recorded after closure, and a closure always sees the latest reconciliation.
"""

from typing import Any

from execution.custody import current_custody
from execution.logistics import (
    custody_dict,
    custody_text,
    get_run,
    person_is_field_user,
    require_open,
    trip_kinds,
)
from execution.models import CustodyEvent, ReturnReconciliation, TripClosure
from fleet.models import Inspection
from planning.models import Person, TripCargo
from platform_core.commands import Conflict, Invalid, Outcome, run_command
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint


def _discrepancies(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(d, str) and d.strip() for d in value):
        raise Invalid("discrepancies must be a list of non-empty notes")
    if len(set(value)) != len(value):
        raise Invalid("discrepancies must not repeat the same note")
    return list(value)


def _inspection(code: str | None) -> Inspection | None:
    if not code:
        return None
    try:
        return Inspection.objects.get(code=code)
    except Inspection.DoesNotExist:
        raise Invalid(f"No inspection {code}") from None


def latest_reconciliation(run_id: int) -> ReturnReconciliation | None:
    """Same ordering as the closure trigger."""
    return ReturnReconciliation.objects.filter(run_id=run_id).order_by("-recorded_at", "-id").first()


# ------------------------------------------------------------------ reconcile_return
def reconcile_return(
    actor: Any, *, run_code: str, inspection_code: str | None = None, discrepancies: list[str] | None = None
) -> dict[str, Any]:
    name = "execution.reconcile_return"
    notes = discrepancies if discrepancies is not None else []
    payload = {"run": run_code, "inspection": inspection_code, "discrepancies": notes}

    def handler() -> Outcome:
        found = _discrepancies(notes)
        insp = _inspection(inspection_code)
        run = get_run(run_code)
        checkpoint(f"read:{name}")
        lock_guards(
            update_robots=[run.robot_id],
            share_visits=[run.visit_id],
            update_trips=[run.trip_id],
            update_runs=[run.pk],
            label=name,
        )

        require_open(run, "a return reconciliation")
        if insp is not None and (insp.robot_id != run.robot_id or insp.visit_ref != run.visit.code):
            raise Conflict(
                f"Inspection {insp.code} is not a post-visit inspection of this robot "
                f"for visit {run.visit.code}"
            )
        problems: list[str] = []

        custody = current_custody(run.robot_id)
        hub: CustodyEvent | None = (
            CustodyEvent.objects.filter(run=run, to_kind="hub").order_by("-seq").first()
        )
        if hub is None or custody.custody_event_id != hub.pk:
            problems.append(f"the robot has not been received at the hub (it is {custody_text(custody)})")
        elif (
            hub.basis != "handoff"
            or hub.received_by_id is None
            or not person_is_field_user(Person.objects.get(pk=hub.received_by_id))
        ):
            problems.append("the hub receipt must be a handoff received by a field user")

        cargo = sorted(TripCargo.objects.filter(trip_id=run.trip_id).values_list("robot_id", flat=True))
        cargo_matches = cargo == [run.robot_id]
        if not cargo_matches:
            problems.append("the trip's planned cargo is not exactly this run's robot")

        unverified = [
            e.seq
            for e in CustodyEvent.objects.filter(run=run).select_related("verification").order_by("seq")
            if (e.basis == "administrative" and not hasattr(e, "verification"))
            or (e.basis == "recovered" and e.received_by_id is None)
        ]
        if unverified:
            problems.append(f"custody events {unverified} of the run are neither verified nor a handoff")

        if insp is None:
            problems.append("no post-visit inspection of this robot for this visit was given")
        elif hub is not None and insp.submitted_at <= hub.recorded_at:
            problems.append(f"inspection {insp.code} was not submitted after the hub receipt")

        if found:
            problems.append(f"{len(found)} open discrepanc{'y' if len(found) == 1 else 'ies'}")

        outcome = "reconciled" if not problems else "unresolved"
        rec = ReturnReconciliation.objects.create(
            run=run,
            hub_receipt=hub,
            cargo_matches=cargo_matches,
            post_visit_inspection=insp,
            discrepancies=found,
            outcome=outcome,
            recorded_by=actor,
        )
        return Outcome(
            "return_reconciliation",
            str(rec.pk),
            {
                "id": rec.pk,
                "run": run.code,
                "outcome": outcome,
                "problems": problems,
                "cargo_matches": cargo_matches,
                "hub_receipt_seq": hub.seq if hub else None,
                "post_visit_inspection": insp.code if insp else None,
                "post_visit_inspection_result": insp.result if insp else None,
                "discrepancies": found,
                "custody": custody_dict(custody),
            },
        )

    return run_command(actor, name, permission="execution.reconcile", payload=payload, handler=handler)


# ------------------------------------------------------------------ close_trip
def close_trip(actor: Any, *, run_code: str) -> dict[str, Any]:
    name = "execution.close_trip"
    payload = {"run": run_code}

    def handler() -> Outcome:
        run = get_run(run_code)
        checkpoint(f"read:{name}")
        lock_guards(
            share_robots=[run.robot_id],
            share_visits=[run.visit_id],
            update_trips=[run.trip_id],
            update_runs=[run.pk],
            label=name,
        )

        if TripClosure.objects.filter(run=run).exists():
            raise Conflict(f"The trip of run {run.code} is already closed")
        custody = current_custody(run.robot_id)
        if "arrived_hub" not in trip_kinds(run):
            raise Conflict("Cannot close: the trip has not arrived at the hub (record arrived_hub)")
        rec = latest_reconciliation(run.pk)
        if rec is None:
            raise Conflict(
                f"Cannot close: the return has not been reconciled (the robot is {custody_text(custody)})"
            )
        if rec.outcome != "reconciled":
            raise Conflict(
                "Cannot close: the latest reconciliation is unresolved "
                f"(the robot is {custody_text(custody)}); record a new reconciliation once every "
                "condition holds"
            )
        closure = TripClosure.objects.create(
            trip_id=run.trip_id, run=run, reconciliation=rec, recorded_by=actor
        )
        return Outcome(
            "trip_closure",
            str(closure.pk),
            {
                "id": closure.pk,
                "run": run.code,
                "visit": run.visit.code,
                "reconciliation": rec.pk,
                "closed": True,
                "custody": custody_dict(custody),
            },
        )

    return run_command(actor, name, permission="execution.close_trip", payload=payload, handler=handler)
