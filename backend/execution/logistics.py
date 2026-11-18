"""Field logistics: custody verification, custody moves and trip progress (Slice 4, Engineer B).
See docs/04_field_execution_contract.md sections 2 (custody move table), 4 and 5.

SYNTHETIC data only. Nothing here moves a robot: every command records what an authorized person reports.

Every command: run_command (auth, in-transaction permission, audit) -> read the run
-> checkpoint("read:<name>") -> lock_guards in the global order -> read the CURRENT custody / trip state
under the locks -> explained checks -> insert. Runs, revisions and trips are immutable, so what we lock
cannot change under us; the mutable state (custody, trip events, dispositions, closure) is read only after
the locks are held. The insert triggers (execution migration 0002) re-check every rule; Python gives the
explained answer, the database is the backstop.

Locks (U = FOR UPDATE, S = FOR SHARE), after run_command's role-grant S:
  verify_custody      robot U, persons S (verifier), run U
  record_trip_event   robot U, visit S, trip U, run U
  transfer_custody    robot U, persons S (custodian, receiver), visit S, trip U, run U
  recover_robot       robot U, persons S (receiver)                 (no open run; migration 0003)

Custody is read only through execution.custody.current_custody (the SQL definition, contract D1).
"""

from collections.abc import Sequence
from typing import Any

from django.db import connection

from execution.custody import Custody, current_custody, open_run_id
from execution.models import CustodyEvent, CustodyVerification, ExecutionRun, RecoveryDisposition, TripEvent
from fleet.models import Robot
from planning.models import Person
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, run_command
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint

TRIP_ORDER = ["arrived_site", "departed_site", "arrived_hub"]
TRIP_SEQ = {k: i + 1 for i, k in enumerate(TRIP_ORDER)}  # fixed per kind (model constraint trip_event_order)
TRIP_LABEL = {
    "arrived_site": "arrived at the site",
    "departed_site": "departed the site",
    "arrived_hub": "arrived at the hub",
}
MOVE_TARGETS = ("hub", "vehicle", "site")
STRANDED_DISPOSITIONS = ("robot_left_on_site", "robot_unaccounted")


# ------------------------------------------------------------ shared helpers (also used by B's other files)
def get_run(code: str) -> ExecutionRun:
    try:
        return ExecutionRun.objects.select_related("revision", "visit").get(code=code)
    except ExecutionRun.DoesNotExist:
        raise Invalid(f"No execution run {code}") from None


def get_person(code: str | None, what: str) -> Person:
    if not code:
        raise Invalid(f"A {what} is required")
    try:
        return Person.objects.get(code=code)
    except Person.DoesNotExist:
        raise Invalid(f"No person {code} ({what})") from None


def _sql_bool(sql: str, params: list[Any]) -> bool:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return bool(row and row[0])


def has_role(user_id: int, role: str) -> bool:
    return _sql_bool("SELECT has_active_role(%s, %s)", [user_id, role])


def person_is_field_user(person: Person) -> bool:
    """Same rule as the triggers: the person's user holds an active ops_lead or field_operator role."""
    return _sql_bool("SELECT execution_person_is_field_user(%s)", [person.pk])


def crew_ids(run: ExecutionRun) -> set[int]:
    """Field crew of a run = the departed revision's lead and second."""
    return {i for i in (run.revision.lead_id, run.revision.second_id) if i is not None}


def is_closed(run: ExecutionRun) -> bool:
    return _sql_bool("SELECT execution_run_closed(%s)", [run.pk])


def require_open(run: ExecutionRun, doing: str) -> None:
    if is_closed(run):
        raise Conflict(f"Run {run.code} is closed (its trip was closed); {doing} can no longer be recorded")


def trip_kinds(run: ExecutionRun) -> list[str]:
    return list(TripEvent.objects.filter(trip_id=run.trip_id).order_by("seq").values_list("kind", flat=True))


def latest_event(robot_id: int) -> CustodyEvent | None:
    return CustodyEvent.objects.filter(robot_id=robot_id).order_by("-seq").first()


def custody_text(c: Custody) -> str:
    return {
        "hub": "at the hub",
        "vehicle": "in the vehicle",
        "site": "at the site",
        "unaccounted": "unaccounted for",
        "unknown": "unknown",
    }.get(c.kind, c.kind) + ("" if c.verified or c.kind in ("unknown", "unaccounted") else " (not verified)")


def custody_dict(c: Custody) -> dict[str, Any]:
    return {"kind": c.kind, "verified": c.verified, "label": c.label}


def _lock(
    run: ExecutionRun,
    label: str,
    *,
    persons: Sequence[Person] = (),
    visit: bool = True,
    trip: bool = True,
) -> None:
    lock_guards(
        update_robots=[run.robot_id],
        share_persons=[p.pk for p in persons],
        share_visits=[run.visit_id] if visit else [],
        update_trips=[run.trip_id] if trip else [],
        update_runs=[run.pk],
        label=label,
    )


# ------------------------------------------------------------------ verify_custody
def verify_custody(actor: Any, *, run_code: str, custody_seq: int, verifier_code: str) -> dict[str, Any]:
    """Field crew confirm the administrative hub -> vehicle assignment made at departure (turns it into a
    verified handoff). No further custody move is possible until this is recorded."""
    name = "execution.verify_custody"
    payload = {"run": run_code, "custody_seq": custody_seq, "verifier": verifier_code}

    def handler() -> Outcome:
        if isinstance(custody_seq, bool) or not isinstance(custody_seq, int) or custody_seq < 1:
            raise Invalid("custody_seq must be a positive whole number")
        run = get_run(run_code)
        verifier = get_person(verifier_code, "verifier")
        checkpoint(f"read:{name}")
        _lock(run, name, persons=[verifier], visit=False, trip=False)

        require_open(run, "custody verification")
        event = CustodyEvent.objects.filter(robot_id=run.robot_id, seq=custody_seq).first()
        if event is None or event.run_id != run.pk:
            raise Invalid(f"Run {run.code} has no custody event #{custody_seq}")
        if event.basis != "administrative":
            raise Conflict(
                f"Custody event #{custody_seq} is a {event.basis} move; only the administrative "
                "assignment made at departure needs crew verification"
            )
        if CustodyVerification.objects.filter(custody_event=event).exists():
            raise Conflict(f"Custody event #{custody_seq} is already verified")
        if verifier.pk not in crew_ids(run):
            raise Conflict(
                f"{verifier.code} is not field crew of run {run.code}; only the crew can verify custody"
            )
        if not person_is_field_user(verifier):
            raise Conflict(f"{verifier.code} does not hold an active ops_lead or field_operator role")

        v = CustodyVerification.objects.create(custody_event=event, verified_by=verifier, recorded_by=actor)
        custody = current_custody(run.robot_id)
        return Outcome(
            "custody_verification",
            str(v.pk),
            {
                "id": v.pk,
                "run": run.code,
                "custody_seq": event.seq,
                "verified_by": verifier.code,
                "custody": custody_dict(custody),
            },
        )

    return run_command(
        actor, name, permission="execution.record_field_event", payload=payload, handler=handler
    )


# ------------------------------------------------------------------ record_trip_event
def record_trip_event(actor: Any, *, run_code: str, kind: str) -> dict[str, Any]:
    """Physical trip progress, in order: arrived_site -> departed_site -> arrived_hub. arrived_hub records
    that the VEHICLE is back; it says nothing about the robot (custody does)."""
    name = "execution.record_trip_event"
    payload = {"run": run_code, "kind": kind}

    def handler() -> Outcome:
        if kind not in TRIP_SEQ:
            raise Invalid(f"kind must be one of {TRIP_ORDER}")
        run = get_run(run_code)
        checkpoint(f"read:{name}")
        _lock(run, name)

        require_open(run, "trip events")
        done = trip_kinds(run)
        custody = current_custody(run.robot_id)
        if "arrived_hub" in done:
            raise Conflict(
                f"The trip of run {run.code} has already arrived at the hub; no further trip events"
            )
        if kind in done:
            raise Conflict(f"The trip of run {run.code} has already {TRIP_LABEL[kind]}")
        first_hub = kind == "arrived_hub" and not done
        ev_now = latest_event(run.robot_id)
        missing = (
            first_hub
            and custody.kind == "unaccounted"
            and ev_now is not None
            and ev_now.run_id == run.pk
            and ev_now.incident is not None
            and ev_now.incident.robot_id == run.robot_id
            and ev_now.incident.event_type == "robot_unaccounted"
        )
        # Migration 0004 (EM): `missing` = the vehicle came back straight after departure while the robot is
        # unaccounted on this run. Recorded as the vehicle's arrival only; nothing is received at the hub.
        aborted = first_hub and not missing
        if aborted:
            # Aborted outbound (migration 0003): back at the hub straight after departure, robot still aboard.
            ev = latest_event(run.robot_id)
            if not (custody.kind == "vehicle" and ev is not None and ev.trip_id == run.trip_id):
                raise Conflict(
                    f"An aborted outbound (arrived_hub straight after departure) needs the robot in the "
                    f"vehicle on this trip; it is {custody_text(custody)}"
                )
            if not custody.verified:
                raise Conflict(
                    "An aborted outbound needs the departure assignment verified by the field crew first "
                    "(the robot's vehicle custody is not verified)"
                )
        elif not missing:
            expected = TRIP_ORDER[len(done)]
            if kind != expected:
                raise Conflict(
                    f"Trip events go arrived_site -> departed_site -> arrived_hub; record {expected} first"
                )
        if kind == "departed_site":
            ev = latest_event(run.robot_id)
            on_vehicle = (
                custody.kind == "vehicle"
                and custody.verified
                and ev is not None
                and ev.trip_id == run.trip_id
            )
            stranded = RecoveryDisposition.objects.filter(
                run=run, decision__in=STRANDED_DISPOSITIONS
            ).exists()
            if not (on_vehicle or stranded):
                raise Conflict(
                    f"The vehicle cannot leave the site: the robot is {custody_text(custody)}. Record the "
                    "site -> vehicle handoff first, or (ops lead) a robot_left_on_site / robot_unaccounted "
                    "disposition"
                )

        te = TripEvent.objects.create(
            trip_id=run.trip_id, run=run, seq=TRIP_SEQ[kind], kind=kind, recorded_by=actor
        )
        result: dict[str, Any] = {
            "id": te.pk,
            "run": run.code,
            "kind": kind,
            "seq": te.seq,
            "aborted_outbound": aborted,
            "custody": custody_dict(custody),
        }
        result["vehicle_returned_without_robot"] = bool(missing)
        if kind == "arrived_hub" and custody.kind != "vehicle":
            result["warning"] = (
                f"The vehicle is back but the robot is {custody_text(custody)}; the trip cannot close until "
                "the robot is received at the hub and the return is reconciled"
            )
        return Outcome("trip_event", str(te.pk), result)

    return run_command(
        actor, name, permission="execution.record_field_event", payload=payload, handler=handler
    )


# ------------------------------------------------------------------ transfer_custody
def _check_handoff(
    run: ExecutionRun,
    frm: str,
    to: str,
    custody: Custody,
    custodian: Person,
    receiver: Person | None,
    kinds: list[str],
) -> None:
    pair = (frm, to)
    if pair == ("hub", "vehicle"):
        raise Conflict("hub -> vehicle custody is assigned only by recording the departure")
    if pair not in (("vehicle", "site"), ("site", "vehicle"), ("vehicle", "hub")):
        raise Conflict(f"Custody cannot move from {frm} to {to}")
    if not custody.verified:
        raise Conflict(
            f"The current custody ({frm}) is not verified: the field crew must verify the departure "
            "assignment before any further move"
        )
    if receiver is None:
        raise Invalid("A handoff needs a receiver")
    if not person_is_field_user(receiver):
        raise Conflict(
            f"The receiver {receiver.code} does not hold an active ops_lead or field_operator role"
        )
    crew = crew_ids(run)
    if pair == ("vehicle", "site"):
        if "arrived_site" not in kinds:
            raise Conflict("vehicle -> site needs the trip to have arrived at the site (record arrived_site)")
        if custodian.pk not in crew or receiver.pk not in crew:
            raise Conflict("vehicle -> site: custodian and receiver must both be field crew of the run")
    elif pair == ("site", "vehicle"):
        if "departed_site" in kinds:
            raise Conflict("site -> vehicle: the trip has already departed the site")
        if custodian.pk not in crew or receiver.pk not in crew:
            raise Conflict("site -> vehicle: custodian and receiver must both be field crew of the run")
    else:  # vehicle -> hub
        if "arrived_hub" not in kinds:
            raise Conflict("vehicle -> hub needs the trip to have arrived at the hub (record arrived_hub)")
        if custodian.pk not in crew:
            raise Conflict("vehicle -> hub: the custodian must be field crew of the run")


def transfer_custody(
    actor: Any,
    *,
    run_code: str,
    to_kind: str,
    custodian_code: str,
    received_by_code: str,
    note: str = "",
) -> dict[str, Any]:
    """Record a custody move from the robot's CURRENT custody to `to_kind` (custody move table, section 2).
    From unaccounted the move is a recovery (ops_lead only, receiver and acknowledgment note required); it
    never resolves the incident or releases its hold. A robot becomes unaccounted only through
    report_incident."""
    name = "execution.transfer_custody"
    payload = {
        "run": run_code,
        "to": to_kind,
        "custodian": custodian_code,
        "received_by": received_by_code,
        "note": note,
    }

    def handler() -> Outcome:
        if to_kind == "unaccounted":
            raise Invalid("A robot becomes unaccounted only by reporting a robot_unaccounted incident")
        if to_kind not in MOVE_TARGETS:
            raise Invalid(f"to_kind must be one of {list(MOVE_TARGETS)}")
        run = get_run(run_code)
        custodian = get_person(custodian_code, "custodian")
        receiver = get_person(received_by_code, "receiver") if received_by_code else None
        checkpoint(f"read:{name}")
        _lock(run, name, persons=[p for p in (custodian, receiver) if p is not None])

        require_open(run, "custody moves")
        if open_run_id(run.robot_id) != run.pk:
            raise Conflict(f"Run {run.code} is not the open run of its robot")
        custody = current_custody(run.robot_id)
        prev = latest_event(run.robot_id)
        if custody.kind == "unknown" or prev is None:
            raise Conflict("The robot's custody is unknown; no move can be recorded until it is resolved")
        frm = custody.kind
        if frm == to_kind:
            raise Conflict(f"The robot is already {custody_text(custody)}")

        if frm == "unaccounted":
            basis = "recovered"
            if not has_role(actor.pk, "ops_lead"):
                raise PermissionDenied("Recording a recovered robot requires an ops_lead")
            if receiver is None:
                raise Invalid("A recovered robot needs a receiver")
            if not person_is_field_user(receiver):
                raise Conflict(
                    f"The receiver {receiver.code} does not hold an active ops_lead or field_operator role"
                )
            if not note.strip():
                raise Invalid("A recovery needs a note (the recovery acknowledgment)")
            incident_id = prev.incident_id
        else:
            basis = "handoff"
            _check_handoff(run, frm, to_kind, custody, custodian, receiver, trip_kinds(run))
            incident_id = None

        ev = CustodyEvent.objects.create(
            robot_id=run.robot_id,
            seq=prev.seq + 1,
            from_kind=frm,
            to_kind=to_kind,
            run=run,
            trip_id=run.trip_id,
            visit_id=run.visit_id,
            custodian=custodian,
            received_by=receiver,
            basis=basis,
            incident_id=incident_id,
            note=note,
            recorded_by=actor,
        )
        after = current_custody(run.robot_id)
        return Outcome(
            "custody_event",
            str(ev.pk),
            {
                "id": ev.pk,
                "run": run.code,
                "seq": ev.seq,
                "from": frm,
                "to": to_kind,
                "basis": basis,
                "custodian": custodian.code,
                "received_by": receiver.code if receiver else None,
                "custody": custody_dict(after),
            },
        )

    return run_command(
        actor, name, permission="execution.record_field_event", payload=payload, handler=handler
    )


# ------------------------------------------------------------------ recover_robot (no open run)
def recover_robot(
    actor: Any, *, robot_code: str, received_by_code: str, note: str, idempotency_key: str | None = None
) -> dict[str, Any]:
    """Record an unaccounted robot that has NO open run (its trip already closed, or it was reported
    unaccounted with no run) as received back at the hub: unaccounted -> hub, basis recovered, no run, trip
    or visit (migration 0003). ops_lead only; the receiver (who becomes the custodian) must hold a field
    role; the note is the recovery acknowledgment. It never opens a run and never touches the incident or
    its hold: the incident hold still blocks planning and readiness until released through the fleet
    workflow. A robot that still has an open run is recovered through transfer_custody on that run."""
    name = "execution.recover_robot"
    payload = {"robot": robot_code, "received_by": received_by_code, "note": note}

    def handler() -> Outcome:
        try:
            robot = Robot.objects.get(code=robot_code)
        except Robot.DoesNotExist:
            raise Invalid(f"No robot {robot_code}") from None
        receiver = get_person(received_by_code, "receiver")
        checkpoint(f"read:{name}")
        lock_guards(update_robots=[robot.pk], share_persons=[receiver.pk], label=name)

        if not has_role(actor.pk, "ops_lead"):
            raise PermissionDenied("Recording a recovered robot requires an ops_lead")
        run_id = open_run_id(robot.pk)
        if run_id is not None:
            code = ExecutionRun.objects.get(pk=run_id).code
            raise Conflict(
                f"Robot {robot.code} has an open run ({code}); record the recovery with transfer_custody "
                "on that run"
            )
        custody = current_custody(robot.pk)
        prev = latest_event(robot.pk)
        if custody.kind != "unaccounted" or prev is None:
            raise Conflict(
                f"Robot {robot.code} is not unaccounted for (it is {custody_text(custody)}); "
                "only an unaccounted robot can be recovered"
            )
        inc = prev.incident
        if inc is None or inc.robot_id != robot.pk or inc.event_type != "robot_unaccounted":
            raise Conflict(
                f"Robot {robot.code}'s unaccounted custody does not reference a robot_unaccounted incident "
                "on this robot; it cannot be recovered"
            )
        if not person_is_field_user(receiver):
            raise Conflict(
                f"The receiver {receiver.code} does not hold an active ops_lead or field_operator role"
            )
        if not note.strip():
            raise Invalid("A recovery needs a note (the recovery acknowledgment)")

        ev = CustodyEvent.objects.create(
            robot=robot,
            seq=prev.seq + 1,
            from_kind="unaccounted",
            to_kind="hub",
            run=None,
            trip=None,
            visit=None,
            custodian=receiver,
            received_by=receiver,
            basis="recovered",
            incident_id=prev.incident_id,
            note=note,
            recorded_by=actor,
        )
        incident_code = ev.incident.code if ev.incident else None
        return Outcome(
            "custody_event",
            str(ev.pk),
            {
                "id": ev.pk,
                "robot": robot.code,
                "seq": ev.seq,
                "from": "unaccounted",
                "to": "hub",
                "basis": "recovered",
                "custodian": receiver.code,
                "received_by": receiver.code,
                "incident": incident_code,
                "custody": custody_dict(current_custody(robot.pk)),
            },
        )

    return run_command(
        actor,
        name,
        permission="execution.record_field_event",
        payload=payload,
        handler=handler,
        idempotency_key=idempotency_key,
    )
