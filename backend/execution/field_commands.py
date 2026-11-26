"""STAND-IN until 03C-2; replaced at merge. Do not build on anything here except the shared contract names.

Command classification and domain adapters of contract 14.2 (docs/13_v2_phase03_contract.md), with exactly the
names of the shared Python contract (``CLASS``, ``FieldRefusal``, ``Transient``, ``apply``), so 03C-3 can test
the HTTP contract against the REAL field services before 03C-2 lands.

What this stand-in does: each class B and C command calls the existing service (which runs ``run_command``
with its permission check and the database triggers) inside a savepoint. A ``CommandError`` becomes a
classified ``FieldRefusal``; a deadlock, lock timeout, serialization failure or lost connection becomes
``Transient``. A protective pause or stop that the session state does not allow records an interruption
instead and answers ``effect = "protective_state"`` (10.D), never "stopped".

What it does NOT do (03C-2 owns it): payload schemas per command, the duplicate-report check across keys,
and the causal reference stored with protective commands (``causal`` is accepted and echoed only).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from django.db import DatabaseError, InterfaceError, OperationalError, transaction

from platform_core.commands import CommandError, PermissionDenied

STANDIN = True

CLASS: dict[str, Literal["A", "B", "C"]] = {
    # C: records, ordinary channel, applied in seq order
    "trip.arrived_site": "C",
    "trip.departed_site": "C",
    "trip.arrived_hub": "C",
    "custody.back_to_vehicle": "C",
    "custody.verify": "C",
    "site_check.record": "C",
    "session.observation": "C",
    # B: protective channel, applied regardless of gaps
    "session.pause": "B",
    "session.stop": "B",
    "incident.report": "B",
    "interruption.open": "B",
    # A: online only, always refused through sync
    "departure.record": "A",
    "session.start": "A",
    "session.resume": "A",
    "custody.handover_to_site": "A",
    "robot.recover": "A",
}

TRANSIENT_SQLSTATES = {"40P01", "40001", "55P03", "57014"}  # deadlock, serialization, lock n/a, timeout


class FieldRefusal(Exception):  # noqa: N818 - contract name
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Transient(Exception):  # noqa: N818 - contract name
    pass


def _text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name, "")
    return value if isinstance(value, str) else ""


def _session_state(run: Any) -> str:
    from execution.incidents import session_state

    return session_state(run.pk)


def _protective_session(actor: Any, run: Any, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    from execution import sessions as ses

    state = _session_state(run)
    if state in ses.ALLOWED_FROM[kind]:
        out = ses.record_session_event(actor, run_code=run.code, kind=kind, note=_text(payload, "note"))
        return {"effect": "recorded", "session": out.get("state"), "message": f"Session {kind} recorded."}
    reason = _text(payload, "note") or f"Phone asked to {kind} the session while it was {state}"
    out = ses.open_interruption(actor, run_code=run.code, reason=reason)
    return {
        "effect": "protective_state",
        "interruption": out.get("code") or out.get("entity_id"),
        "session": _session_state(run),
        "message": f"The session was {state.replace('_', ' ')}, so it was not {kind}ped by this step: an "
        "interruption was recorded instead.",
    }


def _dispatch(actor: Any, run: Any, command: str, payload: dict[str, Any]) -> dict[str, Any]:
    from execution import incidents as inc
    from execution import logistics as lg
    from execution import sessions as ses
    from execution import site_checks as sck

    if command.startswith("trip."):
        out = lg.record_trip_event(actor, run_code=run.code, kind=command.split(".", 1)[1])
        return {
            "effect": "recorded",
            "trip_event": out.get("kind", command),
            "message": "Trip event recorded.",
        }
    if command == "session.observation":
        ses.record_session_event(actor, run_code=run.code, kind="observation", note=_text(payload, "note"))
        return {"effect": "recorded", "message": "Observation recorded."}
    if command in ("session.pause", "session.stop"):
        return _protective_session(actor, run, command.split(".", 1)[1], payload)
    if command == "interruption.open":
        out = ses.open_interruption(actor, run_code=run.code, reason=_text(payload, "reason"))
        return {"effect": "recorded", "interruption": out.get("code"), "message": "Interruption recorded."}
    if command == "incident.report":
        raw = _text(payload, "occurred_at")
        try:
            occurred = datetime.fromisoformat(raw) if raw else inc.db_now()
        except ValueError:
            occurred = inc.db_now()
        if occurred.tzinfo is None or occurred > inc.db_now():
            occurred = inc.db_now()  # the phone's time is never trusted for a record's order
        out = inc.report_incident(
            actor,
            robot_code=run.robot.code,
            event_type=_text(payload, "event_type") or "other",
            description=_text(payload, "description"),
            occurred_at=occurred,
            knowledge_source=_text(payload, "knowledge_source") or "witnessed",
        )
        return {
            "effect": "recorded",
            "incident": out.get("code"),
            "hold": out.get("hold"),
            "session_paused": out.get("session_paused"),
            "message": "Incident recorded and the robot is on hold.",
        }
    if command == "custody.verify":
        seq = payload.get("custody_seq")
        lg.verify_custody(
            actor,
            run_code=run.code,
            custody_seq=int(seq) if isinstance(seq, int) else 0,
            verifier_code=_text(payload, "verifier"),
        )
        return {"effect": "recorded", "message": "Handover confirmed."}
    if command == "custody.back_to_vehicle":
        lg.transfer_custody(
            actor,
            run_code=run.code,
            to_kind="vehicle",
            custodian_code=_text(payload, "custodian"),
            received_by_code=_text(payload, "received_by"),
            note=_text(payload, "note"),
        )
        return {"effect": "recorded", "message": "Robot back in the vehicle recorded."}
    if command == "site_check.record":
        adults = payload.get("adults_present")
        conditions = payload.get("conditions")
        out = sck.record_site_check(
            actor,
            run_code=run.code,
            template_version=_text(payload, "template"),
            adults_present=adults if isinstance(adults, int) else -1,
            conditions=conditions if isinstance(conditions, dict) else {},
            verifier_code=_text(payload, "verifier"),
        )
        return {"effect": "recorded", "result": out.get("result"), "message": "Site check recorded."}
    raise FieldRefusal("UNKNOWN_COMMAND", "This step is not known to the server. Nothing was recorded.")


def apply(
    actor: Any,
    run: Any,
    command: str,
    payload: dict[str, Any],
    *,
    channel: str,
    causal: dict[str, Any] | None,
) -> dict[str, Any]:
    cls = CLASS.get(command)
    if cls is None:
        raise FieldRefusal("UNKNOWN_COMMAND", "This step is not known to the server. Nothing was recorded.")
    if cls == "A":
        raise FieldRefusal("ONLINE_ONLY", "Needs a connection. Do not start.")
    try:
        with transaction.atomic():  # the savepoint: a refusal rolls the domain work back
            result = _dispatch(actor, run, command, payload)
    except PermissionDenied as exc:
        raise FieldRefusal("NOT_PERMITTED", str(exc)) from exc
    except CommandError as exc:
        raise FieldRefusal("REFUSED", str(exc)) from exc
    except (OperationalError, InterfaceError) as exc:
        raise Transient(str(exc)) from exc
    except DatabaseError as exc:
        state = getattr(exc.__cause__, "sqlstate", None)
        if state in TRANSIENT_SQLSTATES:
            raise Transient(str(exc)) from exc
        raise
    if causal is not None and channel == "protective":
        result = {**result, "causal": causal}
    return result
