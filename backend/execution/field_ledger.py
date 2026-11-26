"""STAND-IN until 03C-1; replaced at merge by v2/03c-1 f1725a7 (execution/field_ledger.py + execution/0009).

Same public API as the shipped ledger, so field_sync and its tests need no change at merge:
``Identity`` (``canonical``, ``digest``), ``Stored``, ``claim(key, identity, *, causal, device_time_flag)``
(a context manager opening its OWN durable transaction), ``Claim.decision`` ('apply' | 'held' | 'replay' |
('rejected', code, message)), ``confirm`` / ``reject`` returning ``Stored``, ``abandon``, ``state``,
``report_device``, ``LedgerRefused`` and ``LedgerMisuse``. Same decision rules: key first (replay or
KEY_REUSED), run access (NOT_YOUR_RUN: not crew and not the ops lead; NOT_PERMITTED: no current ops_lead or
field_operator grant at the run's hub), protective skips the sequence rules, ordinary: ended generation at or
after its barrier AFTER_ABANDONED, other generation STALE_GENERATION, next seq apply, higher held, lower
SEQ_TAKEN. KEY_REUSED, NOT_YOUR_RUN and NOT_PERMITTED are answered but never stored.

How it differs (no migration allowed in 03C-3): rows live in ``platform_core_idempotencykey`` under
``command = 'field.standin.*'`` (still inside the claim's transaction, so a rollback stores nothing), the
rules are Python under the run row lock instead of ``execution_field_decide`` / SECURITY DEFINER writers, and
there are no privileges or append-only triggers. ``last_device_report`` is NOT in the shipped ledger: the run
page reads it through ``field_sync.last_device_report``, which must read ``execution_field_device_report`` at
merge (see field_sync).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Literal

from django.db import connection, transaction

from platform_core.models import IdempotencyKey
from platform_core.testhooks import checkpoint

STANDIN = True

CMD = "field.standin.command"
GEN = "field.standin.generation"
DEVICE = "field.standin.device"
ORDINARY, PROTECTIVE = "ordinary", "protective"
TIME_FLAGS = ("ok", "missing", "out_of_range")
NOT_DURABLE = frozenset({"KEY_REUSED", "NOT_YOUR_RUN", "NOT_PERMITTED"})

Decision = Literal["apply", "held", "replay"] | tuple[str, str, str]


class LedgerRefused(Exception):  # noqa: N818 - contract name
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LedgerMisuse(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
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
        return json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()


@dataclass(frozen=True)
class Stored:
    key: str
    outcome: str
    reason_code: str
    message: str
    seq: int | None
    generation: int | None
    received_at: datetime
    decided_at: datetime | None
    result: dict[str, Any]


def _db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def _stored(r: dict[str, Any]) -> Stored:
    return Stored(
        key=r["key"],
        outcome=r["outcome"],
        reason_code=r["reason_code"],
        message=r["message"],
        seq=r["seq"],
        generation=r["generation"],
        received_at=datetime.fromisoformat(r["received_at"]),
        decided_at=datetime.fromisoformat(r["decided_at"]) if r["decided_at"] else None,
        result=r["result"],
    )


def _generation(run_id: int) -> dict[str, Any]:
    row = IdempotencyKey.objects.filter(command=GEN, key=f"run:{run_id}").first()
    return row.response if row is not None else {"generation": 1, "barriers": {}}


def _last_seq(run_id: int, gen: int) -> int:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT max((response->>'seq')::int) FROM platform_core_idempotencykey WHERE command = %s "
            "AND (response->>'run_id')::bigint = %s AND (response->>'generation')::int = %s "
            "AND response->>'took_seq' = 'true'",
            [CMD, run_id, gen],
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def state(run_id: int) -> dict[str, int]:
    gen = int(_generation(run_id)["generation"])
    nxt = _last_seq(run_id, gen) + 1
    return {"generation": gen, "last_decided_seq": nxt - 1, "next_expected_seq": nxt}


def _who_when(r: dict[str, Any]) -> tuple[str, str]:
    from django.contrib.auth import get_user_model

    from platform_core.timezones import workspace_zone

    user = get_user_model().objects.filter(pk=r["actor_id"]).first()
    name = (user.get_full_name() or user.get_username()) if user is not None else "another device"
    at = datetime.fromisoformat(r["decided_at"]).astimezone(workspace_zone()).strftime("%H:%M")
    return name, at


def _access(actor_id: int, run_id: int) -> str | None:
    """NOT_YOUR_RUN / NOT_PERMITTED / None, as execution_field_decide."""
    from django.contrib.auth import get_user_model

    from accounts.permissions import active_roles
    from execution.models import ExecutionRun
    from planning.models import Person

    run = ExecutionRun.objects.select_related("revision", "visit").filter(pk=run_id).first()
    user = get_user_model().objects.filter(pk=actor_id).first()
    if run is None or user is None:
        return "NOT_YOUR_RUN"
    crew = [i for i in (run.revision.lead_id, run.revision.second_id) if i is not None]
    is_crew = Person.objects.filter(pk__in=crew, user_id=actor_id).exists()
    roles = active_roles(user, run.visit.hub_id)
    if not is_crew and "ops_lead" not in roles:
        return "NOT_YOUR_RUN"
    if not roles & {"ops_lead", "field_operator"}:
        return "NOT_PERMITTED"
    return None


MESSAGES = {
    "KEY_REUSED": "This step's key was already used for a different step. Nothing was applied.",
    "NOT_YOUR_RUN": "This is not your run. Nothing was applied.",
    "NOT_PERMITTED": "You no longer have a field role at this run's hub. Nothing was applied.",
    "STALE_GENERATION": "This run's steps were reset after a step was abandoned. Nothing was applied: record "
    "it again from what the server shows now.",
}


class Claim:
    def __init__(
        self,
        key: str,
        identity: Identity,
        *,
        causal: dict[str, Any] | None = None,
        device_time_flag: str | None = None,
    ) -> None:
        self.key = str(uuid.UUID(str(key)))
        flag = device_time_flag or ("missing" if identity.device_observed_at is None else "ok")
        if flag not in TIME_FLAGS:
            raise ValueError(f"unknown device time flag {flag!r}")
        if flag == "missing" and identity.device_observed_at is not None:
            raise ValueError("a device time flagged missing must be None")
        self.device_time_flag = flag
        self.identity = identity
        self.digest = identity.digest()
        self.causal = causal
        self.stored: Stored | None = None
        self.decision: Decision = "held"
        self._atomic: Any = None
        self._done: Stored | None = None
        self._received: datetime | None = None

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
            self._atomic.__exit__(LedgerMisuse, misuse, None)
            raise misuse
        self._atomic.__exit__(exc_type, exc, tb)

    def _reject_rule(self, code: str, message: str | None = None) -> None:
        self.decision = ("rejected", code, message or MESSAGES.get(code, f"Not applied ({code})"))

    def _decide(self) -> None:
        ident = self.identity
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM execution_executionrun WHERE id = %s FOR UPDATE", [ident.run_id])
        checkpoint("field_ledger.claim:locked")
        self._received = _db_now()
        prior = IdempotencyKey.objects.filter(command=CMD, key=self.key).first()
        if prior is not None:
            r = prior.response
            if r["digest"] == self.digest and r["actor_id"] == ident.actor_id and r["run_id"] == ident.run_id:
                self.stored, self.decision = _stored(r), "replay"
            else:
                self._reject_rule("KEY_REUSED")
            return
        refused = _access(ident.actor_id, ident.run_id)
        if refused is not None:
            self._reject_rule(refused)
            return
        if ident.channel == PROTECTIVE:
            self.decision = "apply"
            return
        gen_row = _generation(ident.run_id)
        current = int(gen_row["generation"])
        g, seq = ident.generation, ident.seq
        assert g is not None and seq is not None
        barrier = gen_row["barriers"].get(str(g))
        if g != current:
            if barrier is not None and seq >= int(barrier["seq"]):
                name, at = _who_when(barrier)
                self._reject_rule(
                    "AFTER_ABANDONED",
                    f"Step {barrier['seq']} was abandoned by {name} at {at}, so this later step was not "
                    "applied. Record it again from what the server shows now.",
                )
            else:
                self._reject_rule("STALE_GENERATION")
            return
        nxt = _last_seq(ident.run_id, g) + 1
        if seq == nxt:
            self.decision = "apply"
        elif seq > nxt:
            self.decision = "held"
        else:
            found = IdempotencyKey.objects.filter(
                command=CMD, response__run_id=ident.run_id, response__generation=g, response__seq=seq,
                response__took_seq=True,
            ).first()  # fmt: skip
            if found is not None and found.response["outcome"] == "confirmed":
                name, at = _who_when(found.response)
                self._reject_rule("SEQ_TAKEN", f"Already recorded by {name} at {at}")
            elif found is not None:
                r = found.response
                self._reject_rule(
                    "SEQ_TAKEN", f"Step {seq} was already decided: it was not applied ({r['message']})"
                )
            else:
                self._reject_rule("SEQ_TAKEN", f"Not applied (SEQ_TAKEN, step {seq})")

    def confirm(self, result: dict[str, Any], message: str = "") -> Stored:
        if self.decision != "apply":
            raise LedgerMisuse(f"cannot confirm a claim whose decision is {self.decision!r}")
        if self.identity.channel == ORDINARY and self.device_time_flag == "out_of_range":
            raise LedgerMisuse("an ordinary command with a phone time out of range cannot be confirmed")
        return self._write("confirmed", "", message, result)

    def reject(self, code: str, message: str) -> Stored:
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
                received_at=self._received or _db_now(),
                decided_at=None,
                result={},
            )
            return self._done
        return self._write("rejected", code, message, {})

    def _write(self, outcome: str, code: str, message: str, result: dict[str, Any]) -> Stored:
        if self._done is not None:
            raise LedgerMisuse("this claim is already decided")
        ident = self.identity
        assert self._received is not None
        observed = ident.device_observed_at if self.device_time_flag == "ok" else None
        r = {
            "key": self.key,
            "digest": self.digest,
            "actor_id": ident.actor_id,
            "device_id": ident.device_id,
            "run_id": ident.run_id,
            "channel": ident.channel,
            "seq": ident.seq,
            "generation": ident.generation,
            "took_seq": ident.channel == ORDINARY and self.decision == "apply",
            "command": ident.command,
            "device_observed_at": observed,
            "device_time_flag": self.device_time_flag,
            "causal": self.causal,
            "outcome": outcome,
            "reason_code": code,
            "message": message,
            "result": result,
            "received_at": self._received.isoformat(),
            "decided_at": _db_now().isoformat(),
        }
        IdempotencyKey.objects.create(
            actor_id=ident.actor_id, key=self.key, command=CMD, payload_hash=self.digest, response=r
        )
        self._done = _stored(r)
        return self._done


def claim(
    key: str,
    identity: Identity,
    *,
    causal: dict[str, Any] | None = None,
    device_time_flag: str | None = None,
) -> Claim:
    return Claim(key, identity, causal=causal, device_time_flag=device_time_flag)


def abandon(actor: Any, run_code: str, generation: int, seq: int, reason: str) -> dict[str, Any]:
    from execution.models import ExecutionRun

    actor_id = int(getattr(actor, "pk", actor))
    run_id = ExecutionRun.objects.filter(code=run_code).values_list("pk", flat=True).first()
    if run_id is None:
        raise LedgerRefused("NOT_YOUR_RUN", "Sign in as a crew member of this run or the ops lead")
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM execution_executionrun WHERE id = %s FOR UPDATE", [run_id])
        refused = _access(actor_id, run_id)
        if refused is not None:
            raise LedgerRefused(refused, MESSAGES[refused])
        if not reason.strip():
            raise LedgerRefused("REASON_REQUIRED", "Say why this step is abandoned")
        st = state(run_id)
        if generation != st["generation"]:
            raise LedgerRefused("STALE_GENERATION", MESSAGES["STALE_GENERATION"])
        if seq < st["next_expected_seq"]:
            raise LedgerRefused("SEQ_TAKEN", f"Step {seq} was already decided")
        if seq != st["next_expected_seq"]:
            raise LedgerRefused(
                "NOT_NEXT", f"Only the next step ({st['next_expected_seq']}) can be abandoned"
            )
        gen_row = _generation(run_id)
        barrier = {
            "generation": generation,
            "seq": seq,
            "reason": reason,
            "actor_id": actor_id,
            "decided_at": _db_now().isoformat(),
        }
        new = {"generation": generation + 1, "barriers": {**gen_row["barriers"], str(generation): barrier}}
        row = IdempotencyKey.objects.filter(command=GEN, key=f"run:{run_id}").first()
        if row is None:
            IdempotencyKey.objects.create(
                actor_id=actor_id, key=f"run:{run_id}", command=GEN, payload_hash="", response=new
            )
        else:
            row.response = new
            row.save(update_fields=["response"])
        return state(run_id)


def report_device(actor_id: int, device_id: str, run_id: int, unsent_count: int) -> None:
    with transaction.atomic():
        refused = _access(actor_id, run_id)
        if refused is not None:
            raise LedgerRefused(refused, MESSAGES[refused])
        IdempotencyKey.objects.create(
            actor_id=actor_id,
            key=uuid.uuid4().hex,
            command=DEVICE,
            payload_hash="",
            response={
                "device_id": device_id,
                "run_id": run_id,
                "actor_id": actor_id,
                "unsent_count": unsent_count,
                "reported_at": _db_now().isoformat(),
            },
        )


def last_device_report(run_id: int) -> dict[str, Any] | None:
    """STAND-IN ONLY (not in the shipped ledger): the newest device report of the run."""
    row = IdempotencyKey.objects.filter(command=DEVICE, response__run_id=run_id).order_by("-id").first()
    if row is None:
        return None
    r = dict(row.response)
    r["reported_at"] = datetime.fromisoformat(r["reported_at"])
    return r
