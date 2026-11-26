"""Field commands: classification, domain adapters and the protective fallback (v2 03C-2).

docs/13_v2_phase03_contract.md sections 3 (I-2, I-3), 10.D and 14 (14.8 wins). The shared contract is
``CLASS``, ``FieldRefusal``, ``Transient`` and ``apply``; field_ledger (03C-1) and field_sync (03C-3) use
them.

What this module does, and what it never does:

* ``CLASS`` names every field command and its class (A online only, B protective, C records).
* ``apply`` validates the payload against an explicit schema (unknown fields refused), then runs the EXISTING
  service (execution.logistics / site_checks / sessions / incidents) inside ONE savepoint. Each service runs
  through ``run_command`` with ``hub=`` the run's hub, so the existing permission checks and the database
  triggers decide, with the server's state at apply time (I-3). There is no second rules engine here.
* Nothing from the device is trusted: no device time, cached readiness, authorization or phone state is read.
  An incident's ``occurred_at`` is the reporter's statement of when it happened; it is never used for a gate,
  and a value later than the database clock is replaced by the database time (a protective report is never
  refused for a clock, 14.4).
* Classified domain refusals raise ``FieldRefusal(code, message)`` (the ledger stores them durably).
  Infrastructure failures (timeouts, deadlocks, serialization failures, lost connections, a command that kept
  being retried) raise ``Transient`` and are NEVER a FieldRefusal (14.8 item 5). Anything else is re-raised
  unchanged: the server cannot say what happened (HTTP 5xx), and nothing is stored.
* Protective commands (class B) never create a second incident, interruption or hold for the same report:
  the run_command idempotency key (from the field key) catches a resend, and a second key for the same report
  is caught by the domain state (already paused or stopped; the same reporter's open incident or interruption
  with the same words). A stop or pause that the session state does not allow records an interruption
  instead and answers ``effect = 'protective_state'``; it never claims the session stopped (10.D, 14.2).

Concurrency: the ledger takes the run row FOR UPDATE before calling ``apply`` (14.5), so the protective
duplicate checks below are serialized per run. ``apply`` itself takes no lock beyond the services' own.
SYNTHETIC data only; nothing here operates a robot.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from django.db import DatabaseError, InterfaceError, OperationalError, transaction

from execution import incidents, logistics, sessions, site_checks
from execution.custody import current_custody
from execution.models import ExecutionRun, Incident, Interruption, SessionEvent
from platform_core.commands import (
    CommandError,
    Conflict,
    Invalid,
    NotAuthenticated,
    PermissionDenied,
    RetryTransaction,
    holds_permission_locked,
)
from platform_core.middleware import current_request_id
from platform_core.models import AuditEvent, Hub
from platform_core.timezones import localize_utc_text, workspace_zone

Class = Literal["A", "B", "C"]

# ------------------------------------------------------------------ classification (contract 14.2)
CLASS: dict[str, Class] = {
    # A: unlocks robot movement or work. Never via sync.
    "departure.authorize": "A",
    "departure.record": "A",
    "session.start": "A",
    "session.resume": "A",
    "custody.handover_to_site": "A",
    "robot.recover": "A",
    # B: makes things safer. Channel 'protective', applied regardless of ordinary gaps (10.D).
    "session.pause": "B",
    "session.stop": "B",
    "incident.report": "B",
    "interruption.open": "B",
    # C: records what happened. Channel 'ordinary', applied in seq order.
    "trip.arrived_site": "C",
    "trip.departed_site": "C",
    "trip.arrived_hub": "C",
    "custody.back_to_vehicle": "C",
    "custody.verify": "C",
    "site_check.record": "C",
    "session.observation": "C",
}
CHANNEL: dict[Class, str] = {"B": "protective", "C": "ordinary"}
ONLINE_ONLY_MESSAGE = "Needs a connection. Do not start."

# Payload schemas, field -> (kind, required). Each maps 1:1 to the service's parameters (see _HANDLERS).
# Kinds: 'str' (text), 'int' (whole number, not a boolean), 'conditions' (item id -> 'ok' | 'not_ok' text),
# 'datetime' (ISO 8601 text with a zone offset).
Schema = dict[str, tuple[str, bool]]
SCHEMAS: dict[str, Schema] = {
    "trip.arrived_site": {},
    "trip.departed_site": {},
    "trip.arrived_hub": {},
    "custody.back_to_vehicle": {
        "custodian": ("str", True),
        "received_by": ("str", True),
        "note": ("str", False),
    },
    "custody.verify": {"custody_seq": ("int", True), "verifier": ("str", True)},
    "site_check.record": {
        "template": ("str", True),
        "adults_present": ("int", True),
        "conditions": ("conditions", True),
        "verifier": ("str", True),
    },
    "session.observation": {"note": ("str", True)},
    "session.pause": {"note": ("str", False)},
    "session.stop": {"note": ("str", False)},
    "incident.report": {
        "event_type": ("str", True),
        "description": ("str", True),
        "knowledge_source": ("str", True),
        "occurred_at": ("datetime", False),
    },
    "interruption.open": {"reason": ("str", True)},
}
CAUSAL_KEYS = {"last_decided_seq": "int", "outcome": "str", "generation": "int"}

RETRYABLE_SQLSTATES = {"40P01", "40001", "55P03", "57014", "57P01", "57P02", "57P03", "53300"}
PERMISSION_SQLSTATE = "42501"


# ------------------------------------------------------------------ errors (shared contract)
class FieldRefusal(Exception):  # noqa: N818 - contract name
    """A classified domain refusal: stored durably by the ledger as ``rejected`` with ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Transient(Exception):  # noqa: N818 - contract name
    """An infrastructure failure: nothing was committed and nothing may be stored; the phone resends."""


# ------------------------------------------------------------------ small helpers
def _name(user: Any) -> str:
    if user is None:
        return "Someone"
    return str(user.get_full_name() or user.get_username())


def _local(at: datetime | None) -> str:
    if at is None:
        return "an unknown time"
    return at.astimezone(workspace_zone()).strftime("%a %d %b, %H:%M %Z")


def _sqlstate(exc: BaseException | None) -> str | None:
    """The SQLSTATE anywhere in the exception's cause chain (run_command wraps a database refusal in Conflict,
    and Django wraps the psycopg error)."""
    seen = 0
    while exc is not None and seen < 6:
        state = getattr(exc, "sqlstate", None)
        if isinstance(state, str) and state:
            return state
        exc = exc.__cause__
        seen += 1
    return None


def _run(run: ExecutionRun | str | int) -> ExecutionRun:
    qs = ExecutionRun.objects.select_related("robot", "visit")
    try:
        if isinstance(run, ExecutionRun):
            return qs.get(pk=run.pk)
        if isinstance(run, int) and not isinstance(run, bool):
            return qs.get(pk=run)
        return qs.get(code=str(run))
    except ExecutionRun.DoesNotExist:
        raise FieldRefusal("INVALID_PAYLOAD", f"There is no run {run}.") from None


def _validate(command: str, payload: Any) -> dict[str, Any]:
    schema = SCHEMAS[command]
    if not isinstance(payload, dict):
        raise FieldRefusal("INVALID_PAYLOAD", f"The details of {command} must be a set of named fields.")
    unknown = sorted(str(k) for k in payload if k not in schema)
    if unknown:
        raise FieldRefusal(
            "INVALID_PAYLOAD",
            f"{command} does not take these fields: {', '.join(unknown)}. Nothing was recorded.",
        )
    missing = sorted(k for k, (_, required) in schema.items() if required and payload.get(k) is None)
    if missing:
        raise FieldRefusal("INVALID_PAYLOAD", f"{command} needs these fields: {', '.join(missing)}.")
    data: dict[str, Any] = {}
    for key, value in payload.items():
        kind = schema[key][0]
        if value is None:
            continue
        if kind == "str" and not isinstance(value, str):
            raise FieldRefusal("INVALID_PAYLOAD", f"{key} must be text.")
        if kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            raise FieldRefusal("INVALID_PAYLOAD", f"{key} must be a whole number.")
        if kind == "conditions" and not (
            isinstance(value, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        ):
            raise FieldRefusal("INVALID_PAYLOAD", f"{key} must map each item id to 'ok' or 'not_ok'.")
        if kind == "datetime":
            value = _parse_time(key, value)
        data[key] = value
    return data


def _parse_time(key: str, value: Any) -> datetime:
    if not isinstance(value, str):
        raise FieldRefusal("INVALID_PAYLOAD", f"{key} must be a date and time with its zone offset.")
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise FieldRefusal(
            "INVALID_PAYLOAD", f"{key} must be a date and time with its zone offset."
        ) from None
    if at.tzinfo is None:
        raise FieldRefusal("INVALID_PAYLOAD", f"{key} must include its zone offset.")
    return at


def _causal(causal: Any) -> dict[str, Any] | None:
    """The device's causal reference, kept as data only (never treated as a confirmation, 14.8 item 1)."""
    if causal is None:
        return None
    if not isinstance(causal, dict):
        raise FieldRefusal("INVALID_PAYLOAD", "The causal reference must be a set of named fields.")
    ref: dict[str, Any] = {}
    for key, kind in CAUSAL_KEYS.items():
        value = causal.get(key)
        if value is None:
            continue
        ok = (
            isinstance(value, str)
            if kind == "str"
            else isinstance(value, int) and not isinstance(value, bool)
        )
        if not ok:
            raise FieldRefusal("INVALID_PAYLOAD", f"The causal reference's {key} is not valid.")
        ref[key] = value
    return ref


def _hub_code(run: ExecutionRun) -> str:
    return Hub.objects.filter(pk=run.visit.hub_id).values_list("code", flat=True).first() or "?"


# ------------------------------------------------------------------ error classification (14.8 item 5)
def _infrastructure(exc: BaseException) -> bool:
    if isinstance(exc, OperationalError | InterfaceError):
        return True
    state = _sqlstate(exc)
    return state is not None and (state in RETRYABLE_SQLSTATES or state.startswith("08"))


def _classify(exc: Exception, actor: Any, run: ExecutionRun) -> Exception:
    """The FieldRefusal or Transient for an exception raised by a service, or the exception itself when it
    cannot be classified (the caller re-raises it: HTTP 5xx, nothing stored)."""
    if isinstance(exc, FieldRefusal | Transient):
        return exc
    if _infrastructure(exc):
        return Transient(
            f"Temporary server problem ({type(exc).__name__}); nothing was recorded. Resend later."
        )
    state = _sqlstate(exc)
    text = localize_utc_text(str(exc))
    if isinstance(exc, NotAuthenticated):
        return FieldRefusal("NOT_PERMITTED", "Sign in again; nothing was recorded.")
    if isinstance(exc, PermissionDenied) or state == PERMISSION_SQLSTATE:
        detail = "" if text.startswith("Not allowed:") else f" ({text})"
        return FieldRefusal(
            "NOT_PERMITTED",
            f"{_name(actor)} is not allowed to record this for run {run.code} at hub {_hub_code(run)}"
            f"{detail}. Nothing was recorded; ask your ops lead.",
        )
    if isinstance(exc, Conflict):
        if exc.__cause__ is None and isinstance(exc.__context__, RetryTransaction):
            return Transient("The records kept changing while this ran; nothing was recorded. Resend later.")
        if text.startswith("Idempotency key reused"):
            return FieldRefusal("KEY_REUSED", "This step's key was already used for a different step.")
        return FieldRefusal("STATE_CONFLICT", text)
    if isinstance(exc, Invalid):
        return FieldRefusal("INVALID_PAYLOAD", text)
    if isinstance(exc, CommandError):
        return FieldRefusal("STATE_CONFLICT", text)
    if isinstance(exc, DatabaseError) and state in ("23514", "23505"):
        return FieldRefusal("STATE_CONFLICT", text)  # a trigger refusal raised outside run_command
    return exc


# ------------------------------------------------------------------ adapters (one per command)
Handler = Callable[[Any, ExecutionRun, dict[str, Any], str | None], dict[str, Any]]


def _recorded(record: dict[str, Any], message: str) -> dict[str, Any]:
    return {"effect": "recorded", "message": message, "record": record}


def _trip(kind: str) -> Handler:
    def handler(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
        record = logistics.record_trip_event(actor, run_code=run.code, kind=kind)
        return _recorded(record, f"Run {run.code}: {logistics.TRIP_LABEL[kind]} recorded.")

    return handler


def _back_to_vehicle(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
    """site -> vehicle handoff only. Any other move (a recovery from unaccounted, a handover to the site) is
    class A or another command, so it is refused here, before and after the service (the service moves from
    the CURRENT custody, read under its locks)."""
    before = current_custody(run.robot_id)
    if before.kind != "site":
        raise FieldRefusal(
            "STATE_CONFLICT",
            f"Robot {run.robot.code} is not at the site (custody: {before.label}); back to vehicle records "
            "only a site to vehicle handoff.",
        )
    record = logistics.transfer_custody(
        actor,
        run_code=run.code,
        to_kind="vehicle",
        custodian_code=data["custodian"],
        received_by_code=data["received_by"],
        note=data.get("note", ""),
    )
    if record.get("from") != "site":  # custody changed between our read and the service's locks
        raise FieldRefusal(
            "STATE_CONFLICT",
            f"Robot {run.robot.code}'s custody changed (it was {record.get('from')}); nothing was recorded.",
        )
    return _recorded(record, f"Robot {run.robot.code} is back in the vehicle (run {run.code}).")


def _verify(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
    record = logistics.verify_custody(
        actor, run_code=run.code, custody_seq=data["custody_seq"], verifier_code=data["verifier"]
    )
    return _recorded(
        record, f"Custody #{data['custody_seq']} of run {run.code} verified by {data['verifier']}."
    )


def _site_check(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
    record = site_checks.record_site_check(
        actor,
        run_code=run.code,
        template_version=data["template"],
        adults_present=data["adults_present"],
        conditions=data["conditions"],
        verifier_code=data["verifier"],
    )
    result = "Go" if record.get("result") == "go" else "No-Go"
    return _recorded(record, f"Site check for run {run.code} recorded: {result}.")


def _observation(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
    record = sessions.record_session_event(actor, run_code=run.code, kind="observation", note=data["note"])
    return _recorded(record, f"Observation recorded on run {run.code}.")


def _last_event(run: ExecutionRun, kind: str) -> SessionEvent | None:
    return (
        SessionEvent.objects.filter(run=run, kind=kind).select_related("recorded_by").order_by("-seq").first()
    )


def _no_change(message: str, **extra: Any) -> dict[str, Any]:
    return {"effect": "no_change", "message": message, "record": None, **extra}


def _protective_state(actor: Any, run: ExecutionRun, what: str, note: str) -> dict[str, Any]:
    """10.D / 14.2: a pause or stop that the session state does not allow. Keep or create a protective state:
    an open interruption blocks start and resume until it is resolved. The incident rules place a hold only
    for a reported incident (incident.report), so none is placed here; existing holds are listed. Never claims
    the session stopped or paused."""
    state = incidents.session_state(run.pk).replace("_", " ")
    past = "stopped" if what == "stop" else "paused"
    holds = sessions.blocking_holds(run.robot_id)
    existing = (
        Interruption.objects.filter(run=run, resolution__isnull=True)
        .select_related("recorded_by")
        .order_by("pk")
        .first()
    )
    if existing is not None:
        return {
            "effect": "protective_state",
            "message": (
                f"The session was {state}, so it was not {past}. Interruption {existing.code} (opened by "
                f"{_name(existing.recorded_by)} at {_local(existing.recorded_at)}) is still open, so the "
                "session cannot start until it is resolved. Nothing new was recorded."
            ),
            "record": None,
            "session_state": state,
            "interruption": existing.code,
            "interruption_created": False,
            "holds": holds,
        }
    reason = f"Field {what} request: the session was {state}, so the robot must not start."
    if note.strip():
        reason += f" Note: {note.strip()}"
    record = sessions.open_interruption(actor, run_code=run.code, reason=reason)
    return {
        "effect": "protective_state",
        "message": (
            f"The session was {state}, so it was not {past}. {_name(actor)}'s {what} request was recorded as "
            f"interruption {record['code']} instead; the session cannot start until it is resolved."
        ),
        "record": record,
        "session_state": state,
        "interruption": record["code"],
        "interruption_created": True,
        "holds": holds,
    }


def _pause(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
    note = data.get("note", "")
    state = incidents.session_state(run.pk)
    if state == "active":
        record = sessions.record_session_event(actor, run_code=run.code, kind="pause", note=note)
        return _recorded(record, f"Session of run {run.code} paused.")
    if state == "paused":
        ev = _last_event(run, "pause")
        who = f"{_name(ev.recorded_by)} at {_local(ev.recorded_at)}" if ev else "earlier"
        return _no_change(
            f"The session of run {run.code} was already paused ({who}). Nothing new was recorded.",
            session_state="paused",
        )
    if state == "stopped":
        return _stopped_already(run)
    return _protective_state(actor, run, "pause", note)


def _stopped_already(run: ExecutionRun) -> dict[str, Any]:
    ev = _last_event(run, "stop")
    who = f"{_name(ev.recorded_by)} at {_local(ev.recorded_at)}" if ev else "earlier"
    return _no_change(
        f"The session of run {run.code} was already stopped ({who}). Nothing new was recorded.",
        session_state="stopped",
    )


def _stop(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
    note = data.get("note", "")
    state = incidents.session_state(run.pk)
    if state in sessions.ALLOWED_FROM["stop"]:
        record = sessions.record_session_event(actor, run_code=run.code, kind="stop", note=note)
        return _recorded(record, f"Session of run {run.code} stopped.")
    if state == "stopped":
        return _stopped_already(run)
    return _protective_state(actor, run, "stop", note)


def _incident(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
    # Authorized run access (14.8): the reporter needs a current grant at the run's hub. report_incident
    # itself is deliberately hub-free online (execution/0008), so the same permission is checked here, with
    # the same locked function run_command uses.
    if not holds_permission_locked(actor, "execution.report_incident", run.visit.hub_id):
        raise PermissionDenied(f"Not allowed: execution.report_incident at hub {_hub_code(run)}")
    same = (
        Incident.objects.filter(
            robot_id=run.robot_id,
            event_type=data["event_type"],
            description=data["description"],
            recorded_by=actor,
            hold__released_at__isnull=True,
        )
        .select_related("hold")
        .order_by("pk")
        .first()
    )
    if same is not None:
        return _no_change(
            f"{_name(actor)} already reported incident {same.code} ({same.event_type}) at "
            f"{_local(same.recorded_at)}; robot {run.robot.code} stays held under {same.hold.code}. "
            "No second incident or hold was recorded.",
            incident=same.code,
            hold=same.hold.code,
        )
    now = incidents.db_now()
    occurred = data.get("occurred_at")
    flag = ""
    if occurred is None or occurred > now:
        flag = "phone time missing" if occurred is None else "phone time later than the server clock"
        occurred = now
    record = incidents.report_incident(
        actor,
        robot_code=run.robot.code,
        event_type=data["event_type"],
        description=data["description"],
        occurred_at=occurred,
        knowledge_source=data["knowledge_source"],
        idempotency_key=f"field:{key}" if key else None,
    )
    if record.get("replayed"):
        return _no_change(
            f"Incident {record['code']} was already recorded for this report; robot {run.robot.code} stays "
            f"held under {record['hold']}.",
            incident=record["code"],
            hold=record["hold"],
        )
    out = _recorded(
        record, f"Incident {record['code']} recorded; robot {run.robot.code} is held ({record['hold']})."
    )
    out["occurred_at_source"] = "server" if flag else "reporter"
    if flag:
        out["occurred_at_flag"] = flag
    return out


def _interruption(actor: Any, run: ExecutionRun, data: dict[str, Any], key: str | None) -> dict[str, Any]:
    reason = data["reason"]
    same = (
        Interruption.objects.filter(
            run=run, source="operator", reason=reason, recorded_by=actor, resolution__isnull=True
        )
        .order_by("pk")
        .first()
    )
    if same is not None:
        return _no_change(
            f"{_name(actor)} already opened interruption {same.code} at {_local(same.recorded_at)} for this "
            "reason; it is still open. No second interruption was recorded.",
            interruption=same.code,
        )
    record = sessions.open_interruption(actor, run_code=run.code, reason=reason)
    paused = " and the session was paused" if record.get("session_paused") else ""
    return _recorded(record, f"Interruption {record['code']} opened on run {run.code}{paused}.")


_HANDLERS: dict[str, Handler] = {
    "trip.arrived_site": _trip("arrived_site"),
    "trip.departed_site": _trip("departed_site"),
    "trip.arrived_hub": _trip("arrived_hub"),
    "custody.back_to_vehicle": _back_to_vehicle,
    "custody.verify": _verify,
    "site_check.record": _site_check,
    "session.observation": _observation,
    "session.pause": _pause,
    "session.stop": _stop,
    "incident.report": _incident,
    "interruption.open": _interruption,
}


# ------------------------------------------------------------------ the contract entry point
def apply(
    actor: Any,
    run: ExecutionRun | str | int,
    command: str,
    payload: dict[str, Any],
    *,
    channel: str,
    causal: dict[str, Any] | None,
    key: str | None = None,
) -> dict[str, Any]:
    """Apply one field command now, against the server's current state (I-3). Returns the result dict
    (``effect`` is 'recorded', 'no_change' or 'protective_state'). Raises FieldRefusal for a classified
    refusal and Transient for an infrastructure failure; the savepoint is rolled back in both cases.

    ``key`` (optional, the field idempotency key) is passed to the domain's own idempotency where the
    service has one (report_incident); the ledger already returns a stored outcome for a replayed key before
    this runs.
    """
    cls = CLASS.get(command)
    if cls is None:
        raise FieldRefusal(
            "UNKNOWN_COMMAND", f"{command!s:.60} is not a field command. Nothing was recorded."
        )
    if cls == "A":
        raise FieldRefusal("ONLINE_ONLY", ONLINE_ONLY_MESSAGE)
    if channel != CHANNEL[cls]:
        raise FieldRefusal(
            "WRONG_CHANNEL", f"{command} must be sent on the {CHANNEL[cls]} channel. Nothing was recorded."
        )
    data = _validate(command, payload)
    ref = _causal(causal)
    target = _run(run)
    try:
        with transaction.atomic():  # a savepoint inside the ledger's transaction
            result = _HANDLERS[command](actor, target, data, key)
    except Exception as exc:  # noqa: BLE001 - classified below; anything unclassified is re-raised as is
        mapped = _classify(exc, actor, target)
        if isinstance(exc, PermissionDenied):
            _audit_denied(actor, command, data, target, str(exc))
        if mapped is exc:
            raise
        raise mapped from exc
    return {"command": command, "class": cls, "run": target.code, "causal": ref, **result}


def _audit_denied(actor: Any, command: str, data: dict[str, Any], run: ExecutionRun, reason: str) -> None:
    """run_command records a denial as a 'denied' audit row, but that row was inside the savepoint just rolled
    back; record it again so a refused field command leaves the same trace as a refused online one."""
    if actor is None or not getattr(actor, "is_authenticated", False):
        return
    payload = {
        "run": run.code,
        **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in data.items()},
    }
    AuditEvent.objects.create(
        actor=actor,
        command=f"field.{command}",
        outcome=AuditEvent.OUTCOME_DENIED,
        payload=payload,
        reason=reason,
        request_id=current_request_id.get(),
    )
