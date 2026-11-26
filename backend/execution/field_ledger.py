"""STAND-IN until 03C-1; replaced at merge. Do not build on anything here except the shared contract names.

The field-command ledger of contract 14.3, 14.5 and 14.8 (docs/13_v2_phase03_contract.md), with exactly the
names and signatures of the shared Python contract (``Identity``, ``Stored``, ``claim``, ``abandon``,
``state``, ``report_device``), so 03C-3 can build and test the HTTP contract before 03C-1 lands the schema.

How this stand-in differs from the real ledger (on purpose: it must not add a migration):

* Rows live in the existing ``platform_core_idempotencykey`` table under ``command = 'field.standin.*'``, not
  in ``execution_field_command`` / ``execution_field_device_report``. That keeps every write inside the
  caller's database transaction, so a rollback stores nothing and a commit stores everything, which the
  failure-injection tests of 14.8 item 5 need. A pure Python dict could not honour a rollback.
* Uniqueness of the key and of (run, generation, seq) is enforced by the run row lock taken first (FOR UPDATE,
  through ``platform_core.locks.lock_guards``), not by unique indexes or SECURITY DEFINER functions.
* No privileges, no catalog audit, no append-only triggers (the generation row is updated in place).

``last_device_report`` is an addition the run page needs (the ops lead fragment); 03C-1 is asked to provide
it with the same return shape, or field_sync reads the real table at merge.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from django.db import connection

from platform_core.commands import Conflict, Invalid
from platform_core.locks import lock_guards
from platform_core.models import IdempotencyKey

STANDIN = True  # tests and field_sync may report that the real ledger is not merged yet

CMD = "field.standin.command"
GEN = "field.standin.generation"
DEVICE = "field.standin.device"


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

    def digest(self) -> str:
        """SHA-256 of the canonical sorted-key UTF-8 JSON of every field (contract 14.8 item 2)."""
        text = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Stored:
    key: str
    outcome: str  # confirmed | rejected | abandoned
    reason_code: str
    message: str
    seq: int | None
    generation: int | None
    received_at: datetime
    decided_at: datetime | None
    result: dict[str, Any] = field(default_factory=dict)


Decision = str | tuple[str, str, str]  # 'apply' | 'held' | ('rejected', code, message)


def _db_now() -> datetime:
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def _stored(row: IdempotencyKey) -> Stored:
    r = row.response
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
            "AND (response->>'run_id')::bigint = %s AND (response->>'generation')::int = %s",
            [CMD, run_id, gen],
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def state(run_id: int) -> dict[str, Any]:
    """last_decided_seq and next_expected_seq of the run's CURRENT generation, and the generation."""
    gen = int(_generation(run_id)["generation"])
    last = _last_seq(run_id, gen)
    return {"last_decided_seq": last, "next_expected_seq": last + 1, "generation": gen}


def _local(at: str) -> str:
    from platform_core.timezones import workspace_zone

    return datetime.fromisoformat(at).astimezone(workspace_zone()).strftime("%a %d %b, %H:%M %Z")


def _who(actor_id: int) -> str:
    from planning.models import Person

    p = Person.objects.select_related("user").filter(user_id=actor_id).first()
    if p is not None:
        return str(getattr(p, "label", "") or p.code)
    from django.contrib.auth import get_user_model

    u = get_user_model().objects.filter(pk=actor_id).first()
    return u.get_username() if u is not None else "another device"


class Claim:
    """One command's claim, inside the caller's transaction, with the run row held FOR UPDATE."""

    def __init__(self, key: str, identity: Identity) -> None:
        self.key = key
        self.identity = identity
        self.stored: Stored | None = None
        self.decision: Decision = "apply"
        self._received_at: datetime | None = None
        self._written = False

    # ------------------------------------------------------------------ decide
    def _enter(self) -> None:
        ident = self.identity
        lock_guards(update_runs=[ident.run_id], label="execution.field_claim")
        self._received_at = _db_now()
        prior = IdempotencyKey.objects.filter(command=CMD, key=self.key).first()
        if prior is not None:
            if prior.response.get("digest") == ident.digest():
                self.stored = _stored(prior)
                return
            self.decision = (
                "rejected",
                "KEY_REUSED",
                "This step's key was already used for a different step. Nothing was recorded.",
            )
            return
        if ident.channel != "ordinary":
            return
        gen_row = _generation(ident.run_id)
        current = int(gen_row["generation"])
        g, seq = ident.generation, ident.seq
        assert seq is not None
        barrier = gen_row["barriers"].get(str(g))
        if g != current:
            if barrier is not None and seq == int(barrier["seq"]):
                self.decision = ("rejected", "ABANDONED", "This step was abandoned. Nothing was recorded.")
            elif barrier is not None and seq > int(barrier["seq"]):
                self.decision = (
                    "rejected",
                    "AFTER_ABANDONED",
                    "An earlier step was abandoned, so this step was not recorded. Re-record it from the "
                    "current state.",
                )
            else:
                self.decision = (
                    "rejected",
                    "STALE_GENERATION",
                    "This queue is out of date. Re-record from the current state.",
                )
            return
        found = (
            IdempotencyKey.objects.filter(
                command=CMD, response__run_id=ident.run_id, response__generation=g, response__seq=seq
            )
            .order_by("id")
            .first()
        )
        taken = found.response if found is not None else None
        if taken is not None:
            self.decision = (
                "rejected",
                "SEQ_TAKEN",
                f"Already recorded by {_who(int(taken['actor_id']))} at {_local(taken['decided_at'])}.",
            )
            return
        expected = _last_seq(ident.run_id, current) + 1
        if seq > expected:
            self.decision = "held"
        elif seq < expected:  # a lower seq with no row cannot happen in one generation; refuse, never reuse
            self.decision = ("rejected", "SEQ_TAKEN", "This step number was already decided.")

    # ------------------------------------------------------------------ write
    def _write(self, outcome: str, code: str, message: str, result: dict[str, Any]) -> Stored:
        if self.stored is not None or self.decision != "apply" or self._written:
            raise AssertionError("field_ledger stand-in: only an 'apply' claim writes, once")
        ident = self.identity
        decided = _db_now()
        assert self._received_at is not None
        response = {
            "key": self.key,
            "digest": ident.digest(),
            "actor_id": ident.actor_id,
            "device_id": ident.device_id,
            "run_id": ident.run_id,
            "channel": ident.channel,
            "seq": ident.seq,
            "generation": ident.generation,
            "command": ident.command,
            "device_observed_at": ident.device_observed_at,
            "outcome": outcome,
            "reason_code": code,
            "message": message,
            "result": result,
            "received_at": self._received_at.isoformat(),
            "decided_at": decided.isoformat(),
        }
        row = IdempotencyKey.objects.create(
            actor_id=ident.actor_id, key=self.key, command=CMD, payload_hash=ident.digest(), response=response
        )
        self._written = True
        return _stored(row)

    def confirm(self, result: dict[str, Any], message: str) -> Stored:
        return self._write("confirmed", "", message, result)

    def reject(self, code: str, message: str) -> Stored:
        return self._write("rejected", code, message, {})


@contextmanager
def claim(key: str, identity: Identity) -> Iterator[Claim]:
    if not connection.in_atomic_block:
        raise AssertionError("field_ledger.claim must run inside the command's transaction")
    c = Claim(key, identity)
    c._enter()
    yield c


def abandon(actor: Any, run_code: str, generation: int, seq: int, reason: str) -> dict[str, Any]:
    """Write the barrier for (run, generation, seq) and open generation + 1. Caller holds the transaction."""
    from execution.models import ExecutionRun

    if not reason.strip():
        raise Invalid("Say why this step is abandoned")
    run = ExecutionRun.objects.filter(code=run_code).first()
    if run is None:
        raise Invalid("No such run")
    lock_guards(update_runs=[run.pk], label="execution.field_abandon")
    st = state(run.pk)
    gen_row = _generation(run.pk)
    barrier = gen_row["barriers"].get(str(generation))
    if barrier is not None and int(barrier["seq"]) == seq:
        return {**barrier, "replayed": True, **state(run.pk)}
    if generation != st["generation"]:
        raise Conflict("This queue is out of date: it belongs to an earlier generation")
    if seq != st["next_expected_seq"]:
        raise Conflict(f"Only the next step ({st['next_expected_seq']}) can be abandoned")
    at = _db_now().isoformat()
    barrier = {"generation": generation, "seq": seq, "reason": reason, "by": actor.pk, "at": at}
    new = {
        "generation": generation + 1,
        "barriers": {**gen_row["barriers"], str(generation): barrier},
    }
    row = IdempotencyKey.objects.filter(command=GEN, key=f"run:{run.pk}").first()
    if row is None:
        IdempotencyKey.objects.create(
            actor_id=actor.pk, key=f"run:{run.pk}", command=GEN, payload_hash="", response=new
        )
    else:
        row.response = new
        row.save(update_fields=["response"])
    return {**barrier, "replayed": False, **state(run.pk)}


def report_device(actor_id: int, device_id: str, run_id: int, unsent_count: int) -> None:
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
    """The newest device report of the run: device_id, actor_id, unsent_count, reported_at (datetime)."""
    row = IdempotencyKey.objects.filter(command=DEVICE, response__run_id=run_id).order_by("-id").first()
    if row is None:
        return None
    r = dict(row.response)
    r["reported_at"] = datetime.fromisoformat(r["reported_at"])
    return r
