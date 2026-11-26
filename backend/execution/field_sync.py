"""v2 03C-3: the authenticated field sync endpoint (docs/13_v2_phase03_contract.md 14.1; 14.8 wins).

    POST /api/field/sync     a phone sends its queued field commands of one run
    POST /api/field/abandon  a crew member or the ops lead abandons a missing ordinary step

Order of processing (each step only when the one before passed):

1. **Authentication first** (14.8 item 6). An invalid, ended or revoked session (signed out, password reset
   by an admin, deactivated account) gets HTTP 401 with one fixed body. Nothing is parsed, no run is looked
   up and no device report is written, so the answer is the same whether the run exists or not.
2. **CSRF**, as for every session-authenticated POST: HTTP 403.
3. **Size and shape.** More than 20 commands, a payload over 4 KB or a body over the cap: HTTP 413. Anything
   malformed: HTTP 400. Nothing is applied.
4. **Run access.** The actor's crew run (lead or second of the departed revision), or any run for a REGIONAL
   actor (14.4). Otherwise HTTP 403 ``NOT_YOUR_RUN``: the same body for a missing run, no run details, no
   device report and no command-history write.
5. **Device report** (``field_ledger.report_device``): the device's last reported unsent count.
6. **Per command, each in its OWN database transaction**, in request order:
   ``with field_ledger.claim(key, identity) as c`` takes the run row lock, then
   replay (``c.stored``) -> sequence decision (``held`` / ledger rejection) -> class and channel
   (``UNKNOWN_COMMAND``, ``ONLINE_ONLY``, ``WRONG_CHANNEL``) -> phone time bounds -> ``field_commands.apply``
   -> ``c.confirm`` or ``c.reject``. Classification and bounds are decided inside the claim so a rejected
   ordinary step takes its ``seq`` in order (14.8: a rejected step is decided) and a replay is answered before
   anything else (14.8 item 2).

Error classes (14.8 item 5): a durable ``rejected`` is written only for a classified refusal
(``FieldRefusal``). An exception ``field_commands.apply`` does not classify rolls the command back and
answers HTTP 500 ``SERVER_ERROR`` (nothing stored for it; the commands before it in the batch stay committed
and a resend gets their stored outcomes). A ``Transient``
from the domain, a database error, or any exception before the transaction ends rolls the whole command
back and answers ``retry`` (nothing claimed). An error while the transaction COMMITS means the server cannot
say what happened: the request answers HTTP 503 ``OUTCOME_UNKNOWN`` and never a guessed result; the phone
resends with the same keys and gets the stored outcome or a first application.

Times: every stored instant is UTC database time; the response shows ISO 8601 strings in the workspace zone.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import InterfaceError, OperationalError, connection
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.views.decorators.csrf import csrf_exempt

from execution import field_commands, field_ledger
from execution.models import ExecutionRun
from platform_core.testhooks import checkpoint
from platform_core.timezones import workspace_zone

log = logging.getLogger(__name__)

MAX_COMMANDS = 20
MAX_PAYLOAD_BYTES = 4096
MAX_BODY_BYTES = 128 * 1024  # 20 x 4 KB of payload plus the envelope, with room to spare
DEPARTURE_SLACK = timedelta(minutes=5)  # 14.4: no earlier than the departure minus 5 minutes
CLOCK_SKEW_KEY = "field_clock_skew_min"
CLOCK_SKEW_FALLBACK_MIN = 2  # the contract's demo value, used while the owner has not provisioned the key
CHANNELS = ("ordinary", "protective")
EXPECTED_CHANNEL = {"B": "protective", "C": "ordinary"}
COMMAND_RE = re.compile(r"^[a-z_]{1,40}\.[a-z_]{1,40}$")

SYNC_FIELDS = {"device_id", "run", "generation", "unsent_count", "commands"}
COMMAND_FIELDS = {"key", "channel", "seq", "last_decided_seq", "command", "payload", "device_observed_at"}
ABANDON_FIELDS = {"run", "generation", "seq", "reason"}

NOT_AUTHENTICATED = {"error": "NOT_AUTHENTICATED", "message": "Sign in again. Nothing was recorded."}
NOT_YOUR_RUN = {
    "error": "NOT_YOUR_RUN",
    "message": "This is not one of your runs. Nothing was recorded.",
}
OUTCOME_UNKNOWN = {
    "error": "OUTCOME_UNKNOWN",
    "message": "The server could not confirm what was recorded. Keep every step and send them again.",
}
SERVER_ERROR = {
    "error": "SERVER_ERROR",
    "message": "The server failed on a step and recorded nothing for it. Keep every step and resend them.",
}
MSG_HELD = "Waiting for an earlier step of this run to arrive. Keep it; it is sent again automatically."
MSG_RETRY = "The server could not record this step now. Nothing was recorded; send it again later."
MSG_ONLINE_ONLY = "Needs a connection. Do not start."
MSG_UNKNOWN = "This step is not known to the server. Nothing was recorded."
MSG_CLOCK = (
    "The phone's clock is out of range for this run, so this step was not recorded. Check the phone's date "
    "and time, then record it again."
)
MSG_CLOCK_FLAG = "Phone clock out of range: the phone time of this step is stored as unknown."


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class OutcomeUnknown(Exception):  # noqa: N818 - a signal: the commit's result is unknown
    pass


class Unclassified(Exception):  # noqa: N818 - a signal: field_commands.apply raised something unclassified
    pass


# ------------------------------------------------------------------ helpers
def _db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def iso_local(at: datetime | None) -> str | None:
    """An instant as ISO 8601 in the workspace zone (seconds), or None."""
    if at is None:
        return None
    return at.astimezone(workspace_zone()).isoformat(timespec="seconds")


def _json(body: dict[str, Any], status: int = 200) -> JsonResponse:
    resp = JsonResponse(body, status=status)
    resp["Cache-Control"] = "no-store"
    return resp


def _authenticated(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_active)


class _CsrfCheck(CsrfViewMiddleware):
    def _reject(self, request: HttpRequest, reason: str) -> str:  # type: ignore[override]
        return reason


def _csrf_failure(request: HttpRequest) -> str | None:
    check = _CsrfCheck(lambda r: None)  # type: ignore[arg-type,return-value]
    check.process_request(request)
    reason = check.process_view(request, None, (), {})  # type: ignore[arg-type]
    return str(reason) if reason else None


def _guard(request: HttpRequest) -> JsonResponse | None:
    """Steps 1 and 2: 401 before anything (even the method), then CSRF."""
    if not _authenticated(request):
        return _json(NOT_AUTHENTICATED, 401)
    if request.method != "POST":
        return _json({"error": "METHOD_NOT_ALLOWED", "message": "Use POST."}, 405)
    if (reason := _csrf_failure(request)) is not None:
        return _json({"error": "CSRF_FAILED", "message": f"Refused: {reason}. Nothing was recorded."}, 403)
    return None


def _body(request: HttpRequest) -> dict[str, Any]:
    length = request.META.get("CONTENT_LENGTH") or "0"
    if not length.isdigit() or int(length) > MAX_BODY_BYTES:
        raise BadRequest("The request is too large. Send at most 20 steps.", 413)
    raw = request.body
    if len(raw) > MAX_BODY_BYTES:
        raise BadRequest("The request is too large. Send at most 20 steps.", 413)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadRequest("The request is not valid JSON.") from None
    if not isinstance(data, dict):
        raise BadRequest("The request must be a JSON object.")
    return data


def _exact_fields(obj: dict[str, Any], fields: set[str], what: str) -> None:
    missing, extra = fields - obj.keys(), obj.keys() - fields
    if missing:
        raise BadRequest(f"{what} is missing: {', '.join(sorted(missing))}.")
    if extra:
        raise BadRequest(f"{what} has unknown fields: {', '.join(sorted(extra))}.")


def _uuid(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise BadRequest(f"{what} must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise BadRequest(f"{what} must be a UUID string.") from None


def _int(value: Any, what: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BadRequest(f"{what} must be a whole number from {minimum}.")
    return value


def _run_code(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 12:
        raise BadRequest("run must be a run code such as EX-0007.")
    return value.strip()


def _device_time(value: Any) -> tuple[str | None, datetime | None]:
    """(the string as sent, the parsed aware instant); None when the phone sent none."""
    if value is None:
        return None, None
    if not isinstance(value, str) or len(value) > 40:
        raise BadRequest("device_observed_at must be an ISO 8601 time with its UTC offset, or null.")
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise BadRequest(
            "device_observed_at must be an ISO 8601 time with its UTC offset, or null."
        ) from None
    if at.tzinfo is None:
        raise BadRequest("device_observed_at must include its UTC offset.")
    return value, at


def parse_sync(data: dict[str, Any]) -> dict[str, Any]:
    """Validate the whole request before anything is applied (14.1 + 14.8). Raises BadRequest (400 / 413)."""
    commands = data.get("commands")
    if isinstance(commands, list) and len(commands) > MAX_COMMANDS:
        raise BadRequest(f"At most {MAX_COMMANDS} steps per request.", 413)
    _exact_fields(data, SYNC_FIELDS, "The request")
    if not isinstance(commands, list):
        raise BadRequest("commands must be a list.")
    out: dict[str, Any] = {
        "device_id": _uuid(data["device_id"], "device_id"),
        "run": _run_code(data["run"]),
        "generation": _int(data["generation"], "generation", 1),
        "unsent_count": _int(data["unsent_count"], "unsent_count", 0),
        "commands": [],
    }
    for i, cmd in enumerate(commands, start=1):
        what = f"Step {i}"
        if not isinstance(cmd, dict):
            raise BadRequest(f"{what} must be an object.")
        payload = cmd.get("payload")
        if isinstance(payload, dict):
            size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            if size > MAX_PAYLOAD_BYTES:
                raise BadRequest(f"{what}: the payload is over 4 KB.", 413)
        _exact_fields(cmd, COMMAND_FIELDS, what)
        channel = cmd["channel"]
        if channel not in CHANNELS:
            raise BadRequest(f"{what}: channel must be ordinary or protective.")
        if channel == "ordinary":
            seq: int | None = _int(cmd["seq"], f"{what}: seq", 1)
        elif cmd["seq"] is not None:
            raise BadRequest(f"{what}: a protective step must send seq null.")
        else:
            seq = None
        last = cmd["last_decided_seq"]
        if last is not None:
            last = _int(last, f"{what}: last_decided_seq", 0)
        name = cmd["command"]
        if not isinstance(name, str) or not COMMAND_RE.match(name):
            raise BadRequest(f"{what}: command must be a name such as trip.arrived_site.")
        if not isinstance(payload, dict):
            raise BadRequest(f"{what}: payload must be an object.")
        raw_time, at = _device_time(cmd["device_observed_at"])
        out["commands"].append(
            {
                "key": _uuid(cmd["key"], f"{what}: key"),
                "channel": channel,
                "seq": seq,
                "last_decided_seq": last,
                "command": name,
                "payload": payload,
                "device_observed_at": raw_time,
                "device_at": at,
            }
        )
    return out


def parse_abandon(data: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(data, ABANDON_FIELDS, "The request")
    reason = data["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise BadRequest("reason must be 1 to 500 characters saying why the step is abandoned.")
    return {
        "run": _run_code(data["run"]),
        "generation": _int(data["generation"], "generation", 1),
        "seq": _int(data["seq"], "seq", 1),
        "reason": reason.strip(),
    }


# ------------------------------------------------------------------ run access
def _is_crew(actor: Any, run: ExecutionRun) -> bool:
    from planning.models import Person

    crew = [i for i in (run.revision.lead_id, run.revision.second_id) if i is not None]
    return Person.objects.filter(pk__in=crew, user_id=actor.pk).exists()


def run_for_sync(actor: Any, code: str) -> ExecutionRun | None:
    """The run when the actor may sync it, else None, whether or not the run exists. The ledger's access rule
    (03C-1 execution_field_decide): a crew member of the run (lead or second of the departed revision) or an
    ops lead at the run's hub (a regional grant or that hub's)."""
    from accounts.permissions import active_roles

    run = (
        ExecutionRun.objects.select_related("revision", "visit", "departure", "robot")
        .filter(code=code)
        .first()
    )
    if run is None:
        return None
    if _is_crew(actor, run) or "ops_lead" in active_roles(actor, run.visit.hub_id):
        return run
    return None


def run_for_abandon(actor: Any, code: str) -> ExecutionRun | None:
    """A crew member of the run, or an ops lead at the run's hub (regional or that hub's; 14.3 item 6)."""
    from accounts.permissions import active_roles

    run = ExecutionRun.objects.select_related("revision", "visit").filter(code=code).first()
    if run is None:
        return None
    if _is_crew(actor, run) or "ops_lead" in active_roles(actor, run.visit.hub_id):
        return run
    return None


def clock_skew() -> timedelta:
    from fleet import services as fleet_services

    raw = fleet_services.setting(CLOCK_SKEW_KEY)
    try:
        minutes = int(raw) if raw is not None else CLOCK_SKEW_FALLBACK_MIN
    except ValueError:
        minutes = CLOCK_SKEW_FALLBACK_MIN
    return timedelta(minutes=max(minutes, 0))


def in_bounds(at: datetime | None, departed_at: datetime, received_at: datetime, skew: timedelta) -> bool:
    """14.4: no earlier than the departure minus 5 minutes, no later than the receipt plus the skew."""
    return at is not None and departed_at - DEPARTURE_SLACK <= at <= received_at + skew


# ------------------------------------------------------------------ results
def _result(
    key: str,
    outcome: str,
    *,
    seq: int | None,
    code: str = "",
    message: str = "",
    recorded_at: datetime | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "outcome": outcome,
        "reason_code": code,
        "message": message,
        "seq": seq,
        "recorded_at": iso_local(recorded_at),
        "result": result or {},
    }


def _from_stored(key: str, s: field_ledger.Stored) -> dict[str, Any]:
    if s.outcome == "confirmed":
        return _result(
            key, "confirmed", seq=s.seq, message=s.message, recorded_at=s.decided_at, result=s.result
        )
    code = s.reason_code or ("ABANDONED" if s.outcome == "abandoned" else "REJECTED")
    return _result(key, "rejected", seq=s.seq, code=code, message=s.message, recorded_at=s.decided_at)


def _stored_or_now(key: str, written: Any, outcome: str, seq: int | None, **kw: Any) -> dict[str, Any]:
    """The answer for a row just written; the ledger's Stored when it returns one."""
    if isinstance(written, field_ledger.Stored):
        return _from_stored(key, written)
    return _result(key, outcome, seq=seq, recorded_at=_db_now(), **kw)


def _decide(
    c: Any,
    actor: Any,
    run: ExecutionRun,
    cmd: dict[str, Any],
    generation: int,
    received_at: datetime,
    skew: timedelta,
) -> dict[str, Any]:
    key, seq, name, channel = cmd["key"], cmd["seq"], cmd["command"], cmd["channel"]
    decision = c.decision
    if (
        decision == "replay" or c.stored is not None
    ):  # the stored outcome, before any domain work (14.8 item 2)
        return _from_stored(key, c.stored)
    if decision == "held":  # nothing is stored; the block ends without a decision
        return _result(key, "held", seq=seq, message=MSG_HELD)
    if isinstance(
        decision, tuple
    ):  # a ledger rule: durable, except KEY_REUSED / NOT_YOUR_RUN / NOT_PERMITTED
        _, code, message = decision
        written = c.reject(code, message)
        return _stored_or_now(key, written, "rejected", seq, code=code, message=message)
    checkpoint("field_sync:claimed")

    cls = field_commands.CLASS.get(name)
    refusal: tuple[str, str] | None = None
    if cls is None:
        refusal = ("UNKNOWN_COMMAND", MSG_UNKNOWN)
    elif cls == "A":
        refusal = ("ONLINE_ONLY", MSG_ONLINE_ONLY)
    elif EXPECTED_CHANNEL[cls] != channel:
        refusal = (
            "WRONG_CHANNEL",
            f"This step must be sent on the {EXPECTED_CHANNEL[cls]} channel. Nothing was recorded.",
        )
    elif channel == "ordinary" and cmd["time_flag"] != "ok":
        refusal = ("DEVICE_TIME_OUT_OF_BOUNDS", MSG_CLOCK)
    if refusal is not None:
        written = c.reject(*refusal)
        return _stored_or_now(key, written, "rejected", seq, code=refusal[0], message=refusal[1])

    clock_flag = channel == "protective" and cmd["time_flag"] == "out_of_range"
    causal = causal_of(cmd, generation)
    try:
        result = field_commands.apply(
            actor,
            run,
            name,
            cmd["payload"],
            channel=channel,
            causal=causal if channel == "protective" else None,
            key=key,  # the domain's own idempotency (incident reports), on top of the ledger's
        )
    except field_commands.FieldRefusal as refused:
        checkpoint("field_sync:before_reject")
        written = c.reject(refused.code, refused.message)
        return _stored_or_now(key, written, "rejected", seq, code=refused.code, message=refused.message)
    except field_commands.Transient:
        raise
    except Exception as exc:  # unclassified by field_commands: never a guessed result (14.8 item 5)
        raise Unclassified from exc
    result = dict(result)
    message = str(result.get("message") or "Recorded.")
    if clock_flag:
        result["device_time_flag"] = "out_of_range"
        message = f"{message} {MSG_CLOCK_FLAG}"
    written = c.confirm(result, message)
    return _stored_or_now(key, written, "confirmed", seq, message=message, result=result)


def causal_of(cmd: dict[str, Any], generation: int) -> dict[str, Any]:
    """The device's causal reference (14.8 item 1): only the keys field_commands and the ledger accept."""
    return {"last_decided_seq": cmd["last_decided_seq"], "generation": generation}


def process_command(
    actor: Any,
    run: ExecutionRun,
    device_id: str,
    generation: int,
    cmd: dict[str, Any],
    received_at: datetime,
    skew: timedelta,
) -> dict[str, Any]:
    """One command in its own transaction. Raises OutcomeUnknown only when the COMMIT itself failed, and
    Unclassified when field_commands.apply raised what it could not classify (rolled back, answered 5xx).
    Anything else before the commit (Transient, a lock or connection error, a unique violation of the same
    key claimed on two runs at once) rolls back and answers ``retry``: nothing is claimed."""
    channel = cmd["channel"]
    identity = field_ledger.Identity(
        actor_id=int(actor.pk),
        device_id=device_id,
        run_id=int(run.pk),
        channel=channel,
        seq=cmd["seq"],
        generation=generation if channel == "ordinary" else None,
        command=cmd["command"],
        payload=cmd["payload"],
        device_observed_at=cmd["device_observed_at"],
    )
    if cmd["device_at"] is None:
        cmd["time_flag"] = "missing"
    elif in_bounds(cmd["device_at"], run.departure.departed_at, received_at, skew):
        cmd["time_flag"] = "ok"
    else:
        cmd["time_flag"] = "out_of_range"  # stored as unknown by the ledger
    body_done = False
    out: dict[str, Any] = {}
    try:
        # claim() opens its OWN durable transaction: it must be the outermost one (no atomic around it)
        with field_ledger.claim(
            cmd["key"],
            identity,
            causal=causal_of(cmd, generation) if channel == "protective" else None,
            device_time_flag=cmd["time_flag"],
        ) as c:
            out = _decide(c, actor, run, cmd, generation, received_at, skew)
            checkpoint("field_sync:before_commit")
            body_done = True
    except Unclassified as exc:  # rolled back: nothing stored for this command; the request answers 5xx
        log.exception(
            "field sync: %s raised an unclassified error; answered 500", cmd["key"], exc_info=exc.__cause__
        )
        raise
    except Exception as exc:
        if body_done:
            log.exception("field sync: commit of %s failed; outcome unknown", cmd["key"])
            raise OutcomeUnknown from exc
        if not isinstance(exc, field_commands.Transient):
            log.exception("field sync: %s rolled back; answered retry", cmd["key"])
        if connection.connection is not None and not connection.is_usable():
            connection.close()
        return _result(cmd["key"], "retry", seq=cmd["seq"], message=MSG_RETRY)
    return out


# ------------------------------------------------------------------ views
def _state_body(run: ExecutionRun) -> dict[str, Any]:
    st = field_ledger.state(run.pk)
    return {
        "server_time": iso_local(_db_now()),
        "run": run.code,
        "generation": st["generation"],
        "last_decided_seq": st["last_decided_seq"],
        "next_expected_seq": st["next_expected_seq"],
    }


@csrf_exempt  # CSRF is checked by _guard, AFTER authentication (14.8 item 6: 401 before anything)
def sync(request: HttpRequest) -> JsonResponse:
    if (refused := _guard(request)) is not None:
        return refused
    actor = request.user
    try:
        req = parse_sync(_body(request))
    except BadRequest as bad:
        return _json(
            {"error": "TOO_LARGE" if bad.status == 413 else "BAD_REQUEST", "message": str(bad)}, bad.status
        )
    run = run_for_sync(actor, req["run"])
    if run is None:
        return _json(NOT_YOUR_RUN, 403)
    try:
        field_ledger.report_device(int(actor.pk), req["device_id"], int(run.pk), req["unsent_count"])
    except field_ledger.LedgerRefused as refusal:
        if refusal.code == "NOT_YOUR_RUN":
            return _json(NOT_YOUR_RUN, 403)
        # NOT_PERMITTED (a revoked grant): no report is stored; each command is answered by the ledger
        log.info("field sync: device report refused (%s)", refusal.code)
    received_at = _db_now()
    skew = clock_skew()
    results = []
    try:
        for cmd in req["commands"]:
            results.append(
                process_command(actor, run, req["device_id"], req["generation"], cmd, received_at, skew)
            )
    except OutcomeUnknown:
        return _json(OUTCOME_UNKNOWN, 503)
    except Unclassified:
        return _json(SERVER_ERROR, 500)
    return _json({**_state_body(run), "results": results})


@csrf_exempt  # as sync
def abandon(request: HttpRequest) -> JsonResponse:
    if (refused := _guard(request)) is not None:
        return refused
    actor = request.user
    try:
        req = parse_abandon(_body(request))
    except BadRequest as bad:
        return _json(
            {"error": "TOO_LARGE" if bad.status == 413 else "BAD_REQUEST", "message": str(bad)}, bad.status
        )
    run = run_for_abandon(actor, req["run"])
    if run is None:
        return _json(NOT_YOUR_RUN, 403)
    try:
        field_ledger.abandon(actor, run.code, req["generation"], req["seq"], req["reason"])
    except field_ledger.LedgerRefused as refusal:
        if refusal.code == "NOT_YOUR_RUN":
            return _json(NOT_YOUR_RUN, 403)
        status = 403 if refusal.code == "NOT_PERMITTED" else 409
        return _json({"error": refusal.code, "message": refusal.message}, status)
    except (OperationalError, InterfaceError):
        return _json({"error": "RETRY", "message": MSG_RETRY}, 503)
    return _json({**_state_body(run), "abandoned": {"generation": req["generation"], "seq": req["seq"]}})


def last_device_report(run: ExecutionRun) -> dict[str, Any] | None:
    """For the run page (ops lead): the newest device report, never presented as live."""
    reader = getattr(field_ledger, "last_device_report", None)  # the 03C-1 stand-in only
    if reader is not None:
        report = reader(run.pk)
    else:  # the shipped ledger (execution/0009): newest row of execution_field_device_report
        with connection.cursor() as cur:
            cur.execute(
                "SELECT device_id, unsent_count, reported_at FROM execution_field_device_report "
                "WHERE run_id = %s ORDER BY reported_at DESC, id DESC LIMIT 1",
                [run.pk],
            )
            row = cur.fetchone()
        report = None if row is None else {"device_id": row[0], "unsent_count": row[1], "reported_at": row[2]}
    if report is None:
        return None
    device = str(report.get("device_id") or "")
    return {
        "unsent_count": int(report["unsent_count"]),
        "reported_at": report["reported_at"],
        "device_tail": device[-4:],
    }
