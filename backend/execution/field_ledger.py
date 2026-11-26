"""v2 03C-1: the field-command ledger (docs/13_v2_phase03_contract.md 14.3, 14.5, 14.8; the shared Python
contract for 03C). At-most-once application of field commands, not exactly-once delivery (10.E).

    with field_ledger.claim(key, identity) as c:   # one durable transaction: the run row FOR UPDATE first
        if c.stored is not None:                   # a replay: return the stored outcome, run nothing
            ...
        elif c.decision == "held":                 # a gap: nothing is stored, the phone resends later
            ...
        elif c.decision == "apply":                # run the domain command, then ONE of:
            c.confirm(result, message)             #   the durable confirmed row, same transaction
            c.reject(code, message)                #   a classified refusal, durable, same transaction
        else:                                      # ("rejected", code, message): a ledger rule refused it
            c.reject(code, message)                #   durable, except KEY_REUSED, NOT_YOUR_RUN, NOT_PERMITTED

Rules (execution_field_decide in execution/0009 is the single statement of them; the SECURITY DEFINER writer
execution_field_record re-checks them under the same run lock, so a confirmed row can never be forged out of
order):

1. The key first. A key already decided with an identical identity (digest, actor, run) is a replay:
   ``c.stored`` is its outcome and ``c.decision`` is ``"replay"``. Any changed field is ``KEY_REUSED``:
   nothing runs, nothing is written, the original row is unchanged.
2. Run access: not a crew member of the run and not the ops lead -> ``NOT_YOUR_RUN``; no current ops_lead or
   field_operator grant at the run's hub -> ``NOT_PERMITTED``. Neither is stored (14.8 item 6).
3. The protective channel skips the sequence rules (10.D) but keeps the key and the identity.
4. Ordinary: a command of an ended generation at or after its abandonment barrier -> ``AFTER_ABANDONED``; any
   other generation than the current one -> ``STALE_GENERATION``; the next expected seq -> ``"apply"``; a
   higher one -> ``"held"``; a lower (already decided) one -> ``SEQ_TAKEN``.

A rejection of an ``"apply"`` claim takes its seq (14.8: decided, not confirmed). A durable rejection of any
other claim takes none. ``held`` and ``retry`` claim nothing: leave the block without confirm or reject
(held), or raise (retry: the whole transaction rolls back, nothing is stored). Leaving an ``"apply"`` claim
without confirm or reject rolls everything back and raises LedgerMisuse, so domain work never commits without
its receipt.

Concurrency: the run row lock serializes the claims of one run. A competing claim waits on it, then sees the
replay, KEY_REUSED or SEQ_TAKEN. The lock order (run U -> the actor's grants S -> the domain's own locks) is
in docs/evidence/03a_lock_graph.md section 5.

Times: received_at is the database time at the start of the claim's transaction, decided_at the database time
of the decision; device_observed_at is stored, never trusted, and NULL when its flag is ``out_of_range``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Literal

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from execution.models import ExecutionRun, FieldCommand
from platform_core.middleware import current_request_id
from platform_core.testhooks import checkpoint
from platform_core.timezones import workspace_zone

ORDINARY = "ordinary"
PROTECTIVE = "protective"
CHANNELS = (ORDINARY, PROTECTIVE)
TIME_FLAGS = ("ok", "missing", "out_of_range")
FIELD_ROLES = ["ops_lead", "field_operator"]
# Ledger decisions that are answered but never stored (the key is someone else's, or the actor has no access).
NOT_DURABLE = frozenset({"KEY_REUSED", "NOT_YOUR_RUN", "NOT_PERMITTED"})

Decision = Literal["apply", "held", "replay"] | tuple[str, str, str]


class LedgerRefused(Exception):  # noqa: N818 - a classified refusal, like FieldRefusal
    """abandon() or report_device() refused by the database (who may, which step, which generation)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LedgerMisuse(RuntimeError):
    """A programming error: confirm on a claim that may not apply, a second decision, or an applied claim left
    undecided. The transaction is rolled back."""


@dataclass(frozen=True)
class Identity:
    """The immutable identity of one key (14.8 item 2). ``actor_id`` comes from the session, never from the
    request."""

    actor_id: int
    device_id: str
    run_id: int
    channel: str
    seq: int | None
    generation: int | None
    command: str
    payload: dict[str, Any]
    device_observed_at: str | None

    def canonical(self) -> bytes:
        """Sorted-key JSON, no whitespace, UTF-8. A payload that is not plain JSON (or holds NaN) raises."""
        return json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()


@dataclass(frozen=True)
class Stored:
    """A durable outcome (or, for a non-durable ledger rejection, the answer that was not stored:
    ``decided_at`` None)."""

    key: str
    outcome: str
    reason_code: str
    message: str
    seq: int | None
    generation: int | None
    received_at: datetime
    decided_at: datetime | None
    result: dict[str, Any]


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _stored(row: FieldCommand) -> Stored:
    return Stored(
        key=str(row.key),
        outcome=row.outcome,
        reason_code=row.reason_code,
        message=row.message,
        seq=row.sent_seq,
        generation=row.generation,
        received_at=row.received_at,
        decided_at=row.decided_at,
        result=dict(row.result or {}),
    )


def _who_when(row: FieldCommand) -> tuple[str, str]:
    user = row.actor
    name = user.get_full_name() or user.get_username()
    return name, timezone.localtime(row.decided_at, workspace_zone()).strftime("%H:%M")


def _message(code: str, ref_id: int | None, identity: Identity) -> str:
    ref = FieldCommand.objects.select_related("actor").get(pk=ref_id) if ref_id else None
    if code == "KEY_REUSED":
        return "This step's key was already used for a different step. Nothing was applied."
    if code == "NOT_YOUR_RUN":
        return "This is not your run. Nothing was applied."
    if code == "NOT_PERMITTED":
        return "You no longer have a field role at this run's hub. Nothing was applied."
    if code == "STALE_GENERATION":
        return (
            "This run's steps were reset after a step was abandoned. Nothing was applied: record it again "
            "from what the server shows now."
        )
    if code == "AFTER_ABANDONED" and ref is not None:
        name, at = _who_when(ref)
        return (
            f"Step {ref.seq} was abandoned by {name} at {at}, so this later step was not applied. Record it "
            "again from what the server shows now."
        )
    if code == "SEQ_TAKEN" and ref is not None:
        name, at = _who_when(ref)
        if ref.outcome == "confirmed":
            return f"Already recorded by {name} at {at}"
        if ref.outcome == "abandoned":
            return f"Step {ref.seq} was abandoned by {name} at {at}"
        return f"Step {ref.seq} was already decided at {at}: it was not applied ({ref.message})"
    return f"Not applied ({code}, step {identity.seq})"


def _validate(key: str, identity: Identity, device_time_flag: str | None) -> tuple[str, datetime | None, str]:
    try:
        canonical_key = str(uuid.UUID(str(key)))
    except ValueError:
        raise ValueError("a field command key is a UUID") from None
    if identity.channel not in CHANNELS:
        raise ValueError(f"unknown channel {identity.channel!r}")
    if identity.channel == ORDINARY:
        if not isinstance(identity.seq, int) or identity.seq < 1:
            raise ValueError("an ordinary command needs a seq from 1")
        if not isinstance(identity.generation, int) or identity.generation < 1:
            raise ValueError("an ordinary command needs a generation from 1")
    elif identity.seq is not None:
        raise ValueError("a protective command has no seq")
    if not identity.device_id or len(identity.device_id) > 64:
        raise ValueError("a field command needs a device id (up to 64 characters)")
    if not identity.command or len(identity.command) > 64:
        raise ValueError("a field command needs a command name (up to 64 characters)")
    if not isinstance(identity.payload, dict):
        raise ValueError("a field command payload is a JSON object")
    flag = device_time_flag or ("missing" if identity.device_observed_at is None else "ok")
    if flag not in TIME_FLAGS:
        raise ValueError(f"unknown device time flag {flag!r}")
    observed: datetime | None = None
    if identity.device_observed_at is not None:
        try:
            observed = datetime.fromisoformat(identity.device_observed_at)
        except ValueError:
            observed = None
        if observed is not None and observed.tzinfo is None:
            observed = None
    if flag == "ok" and observed is None:
        raise ValueError("device_observed_at is not an ISO 8601 time with an offset")
    if flag == "missing" and identity.device_observed_at is not None:
        raise ValueError("a device time flagged missing must be None")
    identity.digest()  # a payload that cannot be canonicalized fails here, before any lock
    return canonical_key, (observed if flag == "ok" else None), flag


class Claim:
    """See the module docstring. Use only as a context manager; it opens its own durable transaction."""

    def __init__(
        self,
        key: str,
        identity: Identity,
        *,
        causal: dict[str, Any] | None = None,
        device_time_flag: str | None = None,
    ) -> None:
        self.key, self._observed, self.device_time_flag = _validate(key, identity, device_time_flag)
        self.identity = identity
        self.digest = identity.digest()
        self.causal = causal
        self.stored: Stored | None = None
        self.decision: Decision = "held"
        self._atomic: Any = None
        self._done: Stored | None = None

    # ------------------------------------------------------------------------------------------ the block
    def __enter__(self) -> Claim:
        self._atomic = transaction.atomic(durable=True)
        self._atomic.__enter__()
        try:
            self._decide()
        except BaseException as exc:
            self._atomic.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc_type is None and self.decision == "apply" and self._done is None:
            misuse = LedgerMisuse(
                "an applied field command must be confirmed or rejected before the block ends"
            )
            self._atomic.__exit__(LedgerMisuse, misuse, None)  # rolls back the domain work too
            raise misuse
        self._atomic.__exit__(exc_type, exc, tb)

    def _decide(self) -> None:
        ident = self.identity
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM execution_executionrun WHERE id = %s FOR UPDATE", [ident.run_id])
            run_exists = cur.fetchone() is not None
            checkpoint("field_ledger.claim:locked")
            if run_exists:  # the actor's field grants at the run's hub FOR SHARE, until this transaction ends
                cur.execute(
                    "SELECT accounts_lock_grants(%s, %s::text[], execution_run_hub(%s))",
                    [ident.actor_id, FIELD_ROLES, ident.run_id],
                )
            cur.execute(
                "SELECT decision, reason_code, ref_id "
                "FROM execution_field_decide(%s, %s, %s, %s, %s, %s, %s)",
                [
                    ident.actor_id,
                    ident.run_id,
                    self.key,
                    self.digest,
                    ident.channel,
                    ident.generation,
                    ident.seq,
                ],
            )
            decision, code, ref_id = cur.fetchone()
        if decision == "replay":
            self.stored = _stored(FieldCommand.objects.get(pk=ref_id))
            self.decision = "replay"
        elif decision in ("apply", "held"):
            self.decision = decision
        else:
            self.decision = ("rejected", code, _message(code, ref_id, ident))

    # ------------------------------------------------------------------------------------------ decisions
    def confirm(self, result: dict[str, Any], message: str = "") -> Stored:
        """The durable confirmed row, in the caller's transaction. Only for an ``"apply"`` claim."""
        if self.decision != "apply":
            raise LedgerMisuse(f"cannot confirm a claim whose decision is {self.decision!r}")
        return self._write("confirmed", "", message, result)

    def reject(self, code: str, message: str) -> Stored:
        """A durable rejection for a CLASSIFIED refusal (permission, state, conflict, class, bounds, identity
        or sequence rule). Never for a timeout, deadlock or lost connection: raise instead (retry). Takes the
        seq only when the claim's decision was ``"apply"``. For KEY_REUSED, NOT_YOUR_RUN and NOT_PERMITTED
        ledger decisions nothing is written and the answer is returned unstored (decided_at None)."""
        if self.decision == "replay":
            raise LedgerMisuse("a replay is already decided: return c.stored")
        if isinstance(self.decision, tuple) and self.decision[1] in NOT_DURABLE:
            if self._done is not None:
                raise LedgerMisuse("this claim is already decided")
            self._done = Stored(
                key=self.key,
                outcome="rejected",
                reason_code=self.decision[1],
                message=self.decision[2],
                seq=self.identity.seq,
                generation=self.identity.generation,
                received_at=timezone.now(),
                decided_at=None,
                result={},
            )
            return self._done
        return self._write("rejected", code, message, {})

    def _write(self, outcome: str, code: str, message: str, result: dict[str, Any]) -> Stored:
        if self._done is not None:
            raise LedgerMisuse("this claim is already decided")
        ident = self.identity
        with connection.cursor() as cur:
            cur.execute(
                "SELECT execution_field_record(%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, "
                "%s::jsonb, %s, %s, %s, %s::jsonb)",
                [
                    ident.actor_id,
                    ident.run_id,
                    self.key,
                    self.digest,
                    ident.device_id,
                    ident.channel,
                    ident.generation,
                    ident.seq,
                    ident.command,
                    _json(ident.payload),
                    self._observed,
                    self.device_time_flag,
                    _json(self.causal) if self.causal is not None else None,
                    outcome,
                    code,
                    message,
                    _json(result),
                ],
            )
            row_id = cur.fetchone()[0]
        self._done = _stored(FieldCommand.objects.get(pk=row_id))
        return self._done


def claim(
    key: str,
    identity: Identity,
    *,
    causal: dict[str, Any] | None = None,
    device_time_flag: str | None = None,
) -> Claim:
    """A claim on ``key`` for ``identity`` (enter it with ``with``).

    ``causal``: for a protective command, the last ordinary seq the device saw and that step's outcome as the
    device saw it (14.8 items 1 and 3). ``device_time_flag``: ``ok`` (the default when a time is given),
    ``missing`` (the default without one) or ``out_of_range`` (the endpoint's bounds check: the time is then
    stored as unknown)."""
    return Claim(key, identity, causal=causal, device_time_flag=device_time_flag)


# ------------------------------------------------------------------------------------------------ the rest
def _refusal(exc: DatabaseError) -> LedgerRefused | None:
    cause = exc.__cause__
    state = getattr(cause, "sqlstate", None)
    if state not in ("42501", "23514", "23505"):
        return None
    diag = getattr(cause, "diag", None)
    code = getattr(diag, "message_detail", None) or ("NOT_PERMITTED" if state == "42501" else "INVALID")
    return LedgerRefused(code, getattr(diag, "message_primary", None) or str(exc))


def abandon(actor: Any, run_code: str, generation: int, seq: int, reason: str) -> dict[str, Any]:
    """Abandon ``seq`` (the next expected seq of the current ``generation``) of run ``run_code``: a durable
    barrier row, generation + 1 opened with seq from 1, an audit row. A crew member of the run or the ops
    lead, with a current field grant at the run's hub. Refusals raise LedgerRefused (NOT_YOUR_RUN,
    NOT_PERMITTED, STALE_GENERATION, SEQ_TAKEN, NOT_NEXT, REASON_REQUIRED). Returns the new state of the
    run's stream."""
    actor_id = getattr(actor, "pk", actor)
    run_id = ExecutionRun.objects.filter(code=run_code).values_list("pk", flat=True).first()
    if run_id is None:
        raise LedgerRefused("NOT_YOUR_RUN", "Sign in as a crew member of this run or the ops lead")
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "SELECT execution_field_abandon(%s, %s, %s, %s, %s, %s)",
                [actor_id, run_id, generation, seq, reason, current_request_id.get() or ""],
            )
            raw = cur.fetchone()[0]  # Django's cursor reads jsonb as text
            out: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except DatabaseError as exc:
        refused = _refusal(exc)
        if refused is None:
            raise
        raise refused from exc
    return out


def state(run_id: int) -> dict[str, int]:
    """The run's ordinary stream now: ``generation``, ``last_decided_seq`` (0 when none in this generation)
    and ``next_expected_seq``. Read without a lock: inside a claim it is exact, outside it is a snapshot."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT g, execution_field_next_seq(%s, g) FROM execution_field_generation_now(%s) g",
            [run_id, run_id],
        )
        generation, nxt = cur.fetchone()
    return {"generation": int(generation), "last_decided_seq": int(nxt) - 1, "next_expected_seq": int(nxt)}


def report_device(actor_id: int, device_id: str, run_id: int, unsent_count: int) -> None:
    """Store the device's last reported unsent count for the run (10.F: "last reported unsent count", the
    newest row wins). Same access rule as the ledger; a refusal raises LedgerRefused and writes nothing."""
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "SELECT execution_field_report_device(%s, %s, %s, %s)",
                [actor_id, run_id, device_id, unsent_count],
            )
    except DatabaseError as exc:
        refused = _refusal(exc)
        if refused is None:
            raise
        raise refused from exc
