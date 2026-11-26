"""v2 Phase 03C-1: the field-command ledger, exercised directly (docs/13_v2_phase03_contract.md 14.3, 14.5, 14.8;
sections 3 I-4..I-6, 6, 10.D, 10.E). The HTTP side is 03C-3's.

Every test builds a real run (departure through the readiness commands) as the schema owner, then switches the
Django connection to the RUNTIME role ops_app, so the ledger runs exactly as in production: the per-run sync lock, the
decision function, the SECURITY DEFINER writers and SELECT-only tables.

* replay, KEY_REUSED (every identity field), SEQ_TAKEN, held, generations and the abandonment barrier, the
  protective channel;
* the transaction failure cases of 14.8 item 5 (crash before commit, exception after the savepoint rollback,
  lost response, a competing claim during an uncommitted one with the waiter seen in pg_locks, a committed
  rejection followed by the next seq);
* direct SQL as ops_app (forged rows, updates, deletes, truncation) and forged, NULL, empty and wrong actors on
  the functions: refused, nothing written.

The domain command is a stub that counts its calls and appends one audit row (its committed effect).

SYNTHETIC data only.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection, transaction
from psycopg.types.json import Jsonb

from execution import field_ledger as fl
from execution.models import FieldCommand, FieldDeviceReport, FieldGeneration
from platform_core import testhooks
from platform_core.locks import lock_guards
from platform_core.models import AuditEvent
from tests.conftest import make_user
from tests.execution_world import build_execution

APP_USER = os.environ.get("DB_APP_USER", "ops_app")
APP_PASSWORD = os.environ.get("DB_APP_PASSWORD", "ops_app_dev")
PHONE_TIME = "2026-11-27T09:41:05-08:00"
DOMAIN = "test.field_domain_effect"
TABLES = (
    "execution_field_command",
    "execution_field_generation",
    "execution_field_device_report",
    "execution_field_sync_lock",
)


# ------------------------------------------------------------------------------------------------ fixtures
def _switch_to(user: str, password: str) -> None:
    connection.close()
    connection.settings_dict["USER"] = user
    connection.settings_dict["PASSWORD"] = password


@pytest.fixture
def w(transactional_db) -> Iterator[SimpleNamespace]:
    """A departed run (crew: P-1 = w.ops, ops lead; P-2 = w.field, field operator; w.field2 is NOT crew), then the
    Django connection as ops_app for the rest of the test (the owner again for the teardown flush)."""
    world = build_execution("lg_")
    world.admin = make_user("lg_admin", "admin")
    world.norole = make_user("lg_norole")
    world.founder_id = world.founder.pk
    owner = (connection.settings_dict["USER"], connection.settings_dict["PASSWORD"])
    _switch_to(APP_USER, APP_PASSWORD)
    with connection.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == APP_USER
    try:
        yield world
    finally:
        _switch_to(*owner)


class Domain:
    """The domain command stub: counts calls; each call appends one audit row (its effect)."""

    def __init__(self, w: SimpleNamespace) -> None:
        self.w, self.calls = w, 0

    def __call__(self, label: str = "") -> dict[str, Any]:
        self.calls += 1
        AuditEvent.objects.create(
            actor=self.w.field, command=DOMAIN, outcome="committed", payload={"l": label}
        )
        return {"effect": "recorded", "label": label}

    @staticmethod
    def effects() -> int:
        return AuditEvent.objects.filter(command=DOMAIN).count()


class Refusal(Exception):
    pass


def ident(
    w: SimpleNamespace,
    seq: int | None = 1,
    *,
    actor: Any = None,
    gen: int | None = 1,
    channel: str = "ordinary",
    command: str = "trip.arrived_site",
    payload: dict[str, Any] | None = None,
    device: str = "dev-A",
    observed: str | None = PHONE_TIME,
    run_id: int | None = None,
) -> fl.Identity:
    return fl.Identity(
        actor_id=(actor or w.field).pk,
        device_id=device,
        run_id=run_id or w.run.pk,
        channel=channel,
        seq=seq if channel == "ordinary" else None,
        generation=gen,
        command=command,
        payload={} if payload is None else payload,
        device_observed_at=observed,
    )


def key() -> str:
    return str(uuid.uuid4())


def send(key_: str, identity: fl.Identity, domain: Domain, *, refuse: str = "", **kw: Any) -> tuple[Any, Any]:
    """What the endpoint does with one command: replay / held / ledger rejection / apply -> confirm or reject.
    Returns (decision, Stored or None)."""
    with fl.claim(key_, identity, **kw) as c:
        if c.stored is not None:
            return c.decision, c.stored
        if c.decision == "held":
            return "held", None
        if isinstance(c.decision, tuple):
            return c.decision, c.reject(c.decision[1], c.decision[2])
        try:
            with transaction.atomic():  # the domain savepoint
                result = domain(identity.command)
                if refuse:
                    raise Refusal(refuse)
        except Refusal:
            return "apply", c.reject(refuse, "Not applied: the robot is not at the site")
        return "apply", c.confirm(result, "Recorded")


def rows(**filters: Any) -> list[FieldCommand]:
    return list(FieldCommand.objects.filter(**filters).order_by("id"))


def app_conn(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=APP_USER,
        password=APP_PASSWORD,
        autocommit=autocommit,
    )


RECORD_SQL = (
    "SELECT execution_field_record(%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def record_args(
    w: SimpleNamespace, *, actor: Any, seq: int | None = 1, gen: int | None = 1, **kw: Any
) -> list:
    identity = ident(w, seq, gen=gen)
    a = {
        "actor": actor,
        "run": w.run.pk,
        "key": key(),
        "digest": identity.digest(),
        "device": "dev-F",
        "channel": "ordinary",
        "generation": gen,
        "seq": seq,
        "command": "trip.arrived_site",
        "payload": Jsonb({}),
        "observed": None,
        "flag": "missing",
        "causal": None,
        "outcome": "confirmed",
        "reason": "",
        "message": "",
        "result": Jsonb({}),
    }
    a.update(kw)
    return list(a.values())


# ------------------------------------------------------------------------------------------------ 1. replay
def test_a_replay_returns_the_stored_outcome_without_running_the_domain_again(w):
    domain = Domain(w)
    k, i = key(), ident(w, 1)
    decision, first = send(k, i, domain)
    assert decision == "apply" and first.outcome == "confirmed" and first.reason_code == ""
    assert first.result == {"effect": "recorded", "label": "trip.arrived_site"}
    assert first.seq == 1 and first.generation == 1 and first.decided_at >= first.received_at
    for _ in range(2):  # the same batch sent twice, and the same command in a later batch
        decision, again = send(k, i, domain)
        assert decision == "replay" and again == first
    assert domain.calls == 1 and Domain.effects() == 1 and len(rows()) == 1

    # a durable rejection is replayed the same way (the domain does not run again)
    k2, i2 = key(), ident(w, 2, command="trip.departed_site")
    decision, rejected = send(k2, i2, domain, refuse="ROBOT_NOT_AT_SITE")
    assert (decision, rejected.outcome, rejected.reason_code) == ("apply", "rejected", "ROBOT_NOT_AT_SITE")
    assert domain.calls == 2 and Domain.effects() == 1  # the refused domain work rolled back to its savepoint
    assert send(k2, i2, domain) == ("replay", rejected)
    assert domain.calls == 2
    assert fl.state(w.run.pk) == {"generation": 1, "last_decided_seq": 2, "next_expected_seq": 3}


# ------------------------------------------------------------------------------------------------ 2. KEY_REUSED
CHANGES: dict[str, Callable[[SimpleNamespace], dict[str, Any]]] = {
    "actor_id": lambda w: {"actor": w.ops},  # another crew member (the ops lead) replaying the key
    "device_id": lambda w: {"device": "dev-B"},
    "run_id": lambda w: {"run_id": w.run.pk + 100000},  # no such run: still KEY_REUSED, nothing else is read
    "channel": lambda w: {"channel": "protective"},
    "seq": lambda w: {"seq": 2},
    "generation": lambda w: {"gen": 2},
    "command": lambda w: {"command": "trip.departed_site"},
    "payload": lambda w: {"payload": {"note": "x"}},
    "device_observed_at": lambda w: {"observed": "2026-11-27T09:41:06-08:00"},
}


@pytest.mark.parametrize("field", sorted(CHANGES))
def test_every_changed_identity_field_is_key_reused_and_nothing_runs(w, field):
    domain = Domain(w)
    k = key()
    _, original = send(k, ident(w, 1), domain)
    before = rows()
    changed = ident(w, **{"seq": 1, **CHANGES[field](w)})
    assert getattr(changed, field) != getattr(ident(w, 1), field)
    with fl.claim(k, changed) as c:
        assert c.stored is None
        assert c.decision[:2] == ("rejected", "KEY_REUSED")
        answer = c.reject("KEY_REUSED", c.decision[2])
    assert (answer.outcome, answer.reason_code, answer.decided_at) == ("rejected", "KEY_REUSED", None)
    assert domain.calls == 1 and rows() == before  # nothing written, the original row unchanged
    assert [(r.outcome, r.identity_digest) for r in rows()] == [("confirmed", ident(w, 1).digest())]
    assert send(k, ident(w, 1), domain) == ("replay", original)  # the original identity still replays


def test_the_digest_is_the_canonical_sorted_key_json_of_every_identity_field(w):
    i = ident(w, 3, payload={"b": 1, "a": "é"})
    text = i.canonical().decode("utf-8")
    assert text.startswith('{"actor_id":') and '"payload":{"a":"é","b":1}' in text and " " not in text
    assert len(i.digest()) == 64 and i.digest() == ident(w, 3, payload={"a": "é", "b": 1}).digest()
    with pytest.raises(ValueError):
        ident(w, 3, payload={"x": float("nan")}).digest()


# ------------------------------------------------------------------------------------------------ 3. SEQ_TAKEN
def test_two_keys_on_one_seq_the_second_is_seq_taken_and_takes_no_seq(w):
    domain = Domain(w)
    send(key(), ident(w, 1), domain)
    decision, other = send(key(), ident(w, 1, actor=w.ops, device="dev-B"), domain)  # the other crew phone
    assert decision[:2] == ("rejected", "SEQ_TAKEN")
    assert other.message.startswith(f"Already recorded by {w.field.username} at ")
    assert (other.outcome, other.reason_code, other.seq) == ("rejected", "SEQ_TAKEN", 1)
    assert domain.calls == 1
    taken = rows(reason_code="SEQ_TAKEN")[0]
    assert (taken.seq, taken.sent_seq) == (None, 1)  # stored durably, but it decides no seq
    assert fl.state(w.run.pk)["next_expected_seq"] == 2
    assert send(key(), ident(w, 2), domain)[1].outcome == "confirmed"


# ------------------------------------------------------------------------------------------------ 4. held
def test_a_gap_is_held_and_claims_nothing_then_applies_in_order(w):
    domain = Domain(w)
    k3, i3 = key(), ident(w, 3)
    assert send(k3, i3, domain) == ("held", None)
    with fl.claim(key(), ident(w, 2)) as c:
        assert c.decision == "held" and c.stored is None
        with pytest.raises(fl.LedgerMisuse):
            c.confirm({}, "")
    assert rows() == [] and domain.calls == 0
    assert fl.state(w.run.pk) == {"generation": 1, "last_decided_seq": 0, "next_expected_seq": 1}
    send(key(), ident(w, 1), domain)
    send(key(), ident(w, 2), domain)
    decision, third = send(k3, i3, domain)  # the held key, resent unchanged, now applies
    assert (decision, third.outcome, third.seq) == ("apply", "confirmed", 3)
    assert [r.seq for r in rows()] == [1, 2, 3] and domain.calls == 3


# ------------------------------------------------------------------------------------------------ 5. abandonment
def test_abandonment_is_a_barrier_and_opens_the_next_generation(w):
    domain = Domain(w)
    send(key(), ident(w, 1), domain)
    k2, i2 = key(), ident(w, 2)  # the step that never arrives (until much later)
    k3, i3 = key(), ident(w, 3)
    assert send(k3, i3, domain) == ("held", None)

    for actor, gen, seq, reason, code in (
        (w.field2, 1, 2, "lost", "NOT_YOUR_RUN"),  # a field operator who is not crew of the run
        (w.field, 2, 2, "lost", "STALE_GENERATION"),
        (w.field, 1, 1, "lost", "SEQ_TAKEN"),
        (w.field, 1, 3, "lost", "NOT_NEXT"),
        (w.field, 1, 2, "  ", "REASON_REQUIRED"),
    ):
        with pytest.raises(fl.LedgerRefused) as refused:
            fl.abandon(actor, w.run.code, gen, seq, reason)
        assert refused.value.code == code
    assert rows(outcome="abandoned") == []

    out = fl.abandon(w.field, w.run.code, 1, 2, "SYN: phone lost the step")  # a crew member
    assert out["abandoned_generation"] == 1 and out["abandoned_seq"] == 2 and out["generation"] == 2
    assert (out["last_decided_seq"], out["next_expected_seq"]) == (0, 1)
    assert fl.state(w.run.pk) == {"generation": 2, "last_decided_seq": 0, "next_expected_seq": 1}
    barrier = rows(outcome="abandoned")[0]
    assert (barrier.generation, barrier.seq, barrier.reason_code, barrier.actor_id) == (
        1,
        2,
        "ABANDONED",
        w.field.pk,
    )
    assert FieldGeneration.objects.get(run=w.run).barrier_id == barrier.pk
    assert AuditEvent.objects.filter(command="execution.field_abandon", entity_id=w.run.code).count() == 1

    # the late step and the held one: AFTER_ABANDONED, durably, without touching the barrier
    for k, i in ((k2, i2), (k3, i3)):
        decision, late = send(k, i, domain)
        assert decision[:2] == ("rejected", "AFTER_ABANDONED") and late.outcome == "rejected"
        assert "Step 2 was abandoned by" in late.message
        assert send(k, i, domain) == ("replay", late)
    assert rows(outcome="abandoned") == [barrier]  # never overwritten
    # an old-generation step before the barrier, under a new key: STALE_GENERATION; a future generation too
    assert send(key(), ident(w, 1, device="dev-B"), domain)[0][:2] == ("rejected", "STALE_GENERATION")
    assert send(key(), ident(w, 1, gen=3), domain)[0][:2] == ("rejected", "STALE_GENERATION")
    assert domain.calls == 1

    # generation 2 starts at seq 1; the ops lead (crew lead P-1, a regional role) may abandon too
    assert send(key(), ident(w, 1, gen=2), domain)[1].outcome == "confirmed"
    fl.abandon(w.ops, w.run.code, 2, 2, "SYN: re-recorded from the server state")
    assert fl.state(w.run.pk) == {"generation": 3, "last_decided_seq": 0, "next_expected_seq": 1}
    assert [(r.generation, r.seq, r.outcome) for r in rows(seq__isnull=False)] == [
        (1, 1, "confirmed"),
        (1, 2, "abandoned"),
        (2, 1, "confirmed"),
        (2, 2, "abandoned"),
    ]

    # the writer itself refuses to confirm anything at or after the barrier (direct call as ops_app)
    with app_conn() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation) as err:
        cur.execute(RECORD_SQL, record_args(w, actor=w.field.pk, seq=2, gen=1))
    assert err.value.diag.message_detail == "AFTER_ABANDONED"
    assert len(rows(outcome="abandoned")) == 2 and rows(outcome="abandoned")[0] == barrier


# ------------------------------------------------------------------------------------------------ 6. protective
def test_the_protective_channel_ignores_gaps_but_not_duplicates(w):
    domain = Domain(w)
    send(key(), ident(w, 1), domain)
    assert send(key(), ident(w, 3), domain) == ("held", None)  # seq 2 missing
    causal = {"last_decided_seq": 1, "outcome": "confirmed"}
    kp = key()
    ip = ident(w, None, channel="protective", command="incident.report", payload={"severity": "high"}, gen=1)
    decision, report = send(kp, ip, domain, causal=causal)
    assert (decision, report.outcome, report.seq) == ("apply", "confirmed", None)  # at once, despite the gap
    row = rows(channel="protective")[0]
    assert (row.seq, row.sent_seq, row.causal, row.device_time_flag) == (None, None, causal, "ok")
    assert send(kp, ip, domain, causal=causal) == ("replay", report)  # a duplicate: caught by the key
    changed = ident(
        w, None, channel="protective", command="incident.report", payload={"severity": "low"}, gen=1
    )
    assert send(kp, changed, domain)[0][:2] == ("rejected", "KEY_REUSED")
    assert domain.calls == 2 and len(rows(channel="protective")) == 1
    assert fl.state(w.run.pk)["next_expected_seq"] == 2  # the protective report took no seq

    # a protective phone time out of range is stored as unknown and flagged, never refused for the clock
    _, flagged = send(
        key(),
        ident(w, None, channel="protective", command="session.stop", observed="2031-01-01T00:00:00+00:00"),
        domain,
        device_time_flag="out_of_range",
    )
    assert flagged.outcome == "confirmed"
    last = rows(channel="protective")[-1]
    assert (last.device_observed_at, last.device_time_flag) == (None, "out_of_range")


# ------------------------------------------------------------------------------------------------ 7. failures
def test_a_crash_just_before_commit_stores_nothing_and_a_resend_applies_once(w):
    domain = Domain(w)
    k, i = key(), ident(w, 1)
    with pytest.raises(RuntimeError, match="crash"), fl.claim(k, i) as c:
        c.confirm(domain("first"), "Recorded")
        raise RuntimeError("crash before commit")
    assert rows() == [] and Domain.effects() == 0  # neither the effect nor its receipt
    decision, stored = send(k, i, domain)
    assert (decision, stored.outcome) == ("apply", "confirmed")
    assert send(k, i, domain) == ("replay", stored)
    assert len(rows()) == 1 and Domain.effects() == 1 and domain.calls == 2


def test_an_exception_after_the_savepoint_rollback_before_reject_stores_nothing(w):
    domain = Domain(w)
    k, i = key(), ident(w, 1)
    with pytest.raises(RuntimeError, match="lost"), fl.claim(k, i) as c:
        assert c.decision == "apply"
        try:
            with transaction.atomic():
                domain("refused")
                raise Refusal("ROBOT_NOT_AT_SITE")
        except Refusal:
            raise RuntimeError("connection lost before the rejection was written") from None
    assert rows() == [] and Domain.effects() == 0  # retry: no key, no seq, no partial state
    assert fl.state(w.run.pk)["next_expected_seq"] == 1
    assert send(k, i, domain)[1].outcome == "confirmed"


def test_an_applied_claim_left_undecided_rolls_back_and_raises(w):
    domain = Domain(w)
    with pytest.raises(fl.LedgerMisuse), fl.claim(key(), ident(w, 1)):
        domain("no receipt")
    assert rows() == [] and Domain.effects() == 0


def test_a_lost_response_after_commit_then_a_replay_gives_the_same_confirmed(w):
    domain = Domain(w)
    k, i = key(), ident(w, 1)
    send(k, i, domain)  # committed; the response never reaches the phone
    decision, replay = send(k, i, domain)
    assert decision == "replay"
    assert (replay.outcome, replay.seq, replay.result) == (
        "confirmed",
        1,
        {"effect": "recorded", "label": i.command},
    )
    assert domain.calls == 1 and Domain.effects() == 1


def test_a_committed_rejection_decides_its_seq_and_the_next_seq_still_applies(w):
    domain = Domain(w)
    _, first = send(key(), ident(w, 1), domain, refuse="ROBOT_NOT_AT_SITE")
    assert (first.outcome, first.reason_code) == ("rejected", "ROBOT_NOT_AT_SITE")
    assert fl.state(w.run.pk) == {"generation": 1, "last_decided_seq": 1, "next_expected_seq": 2}
    decision, second = send(key(), ident(w, 2, command="site_check.record"), domain)
    assert (decision, second.outcome, second.seq) == (
        "apply",
        "confirmed",
        2,
    )  # re-checked by its own domain run
    assert [(r.seq, r.outcome) for r in rows()] == [(1, "rejected"), (2, "confirmed")]


def _pg_waiting(pid: int) -> bool:
    """True once backend ``pid`` waits for a lock (pg_locks, not granted)."""
    with app_conn() as conn:
        for _ in range(200):
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted", [pid])
                if cur.fetchone()[0] > 0:
                    return True
            time.sleep(0.05)
    return False


@pytest.mark.parametrize(
    ("holder_ends", "competitor", "expected"),
    [
        ("commit", "same", "replay"),
        ("commit", "changed", "KEY_REUSED"),
        ("commit", "other_key", "SEQ_TAKEN"),
        ("rollback", "same", "apply"),
        ("rollback", "other_key", "apply"),
    ],
)
def test_a_competing_claim_waits_on_the_sync_lock_then_sees_one_outcome(w, holder_ends, competitor, expected):
    domain = Domain(w)
    k, i = key(), ident(w, 1)
    other = {"same": (k, i), "changed": (k, ident(w, 1, device="dev-B")), "other_key": (key(), ident(w, 1))}
    held, release, errors = threading.Event(), threading.Event(), []
    out: dict[str, Any] = {}

    def holder() -> None:
        try:
            with fl.claim(k, i) as c:
                c.confirm(domain("holder"), "Recorded")
                held.set()
                assert release.wait(20)
                if holder_ends == "rollback":
                    raise RuntimeError("holder crashed before commit")
        except RuntimeError:
            pass
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            held.set()
            connection.close()

    def waiter() -> None:
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                out["pid"] = cur.fetchone()[0]
            started.set()
            out["result"] = send(*other[competitor], Domain(w))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    started = threading.Event()
    t1 = threading.Thread(target=holder)
    t1.start()
    assert held.wait(20)
    t2 = threading.Thread(target=waiter)
    t2.start()
    assert started.wait(20)
    assert _pg_waiting(out["pid"]), "the competing claim should wait on the run's sync lock"
    assert "result" not in out
    release.set()
    t1.join(30)
    t2.join(30)
    assert not errors, errors
    decision, stored = out["result"]
    if expected == "replay":
        assert decision == "replay" and stored.outcome == "confirmed"
    elif expected == "apply":
        assert decision == "apply" and stored.outcome == "confirmed"
    else:
        assert decision[:2] == ("rejected", expected)
    decided = rows(seq__isnull=False)
    assert len(decided) == 1 and decided[0].outcome == "confirmed"  # one transactional outcome for seq 1
    assert Domain.effects() == 1


# ------------------------------------------------------------------------------------------------ 7b. online vs field
# EM ruling on 03C: the ledger serializes on its own per-run row (execution_field_sync_lock), never on the run row,
# so a field claim whose domain step takes the robot and then the run (the global order) and an ONLINE command on
# the same run (robot U, visit S, then run U: execution/logistics.py's order) never form a cycle. Under
# execution/0009 (run row first) the online-first order below was a deadlock.
def _online_guards(conn: psycopg.Connection, w: SimpleNamespace) -> None:
    with conn.cursor() as cur:
        cur.execute("SET lock_timeout = '20s'")
        cur.execute("SELECT id FROM fleet_robot WHERE id = %s FOR UPDATE", [w.run.robot_id])
        cur.execute("SELECT id FROM planning_visit WHERE id = %s FOR SHARE", [w.run.visit_id])


def _online_run(conn: psycopg.Connection, w: SimpleNamespace) -> float:
    started = time.monotonic()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM execution_executionrun WHERE id = %s FOR UPDATE", [w.run.pk])
        cur.execute(
            "INSERT INTO platform_core_auditevent (occurred_at, actor_id, command, outcome, entity_type, entity_id, "
            "payload, result, reason, request_id) VALUES (now(), %s, 'test.online_command', 'committed', '', '', "
            "'{}', '{}', '', '')",
            [w.ops.pk],
        )
    conn.commit()
    return time.monotonic() - started


def _field_with_domain(w: SimpleNamespace, out: dict[str, Any], errors: list[Exception]) -> None:
    """A field claim whose domain step locks robot U, visit S and run U (lock_guards, the global order)."""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            out["field_pid"] = cur.fetchone()[0]
        with fl.claim(key(), ident(w, 1, command="custody.back_to_vehicle")) as c:
            assert c.decision == "apply"
            with transaction.atomic():
                lock_guards(
                    update_robots=[w.run.robot_id],
                    share_visits=[w.run.visit_id],
                    update_runs=[w.run.pk],
                    label="field_domain",
                )
                AuditEvent.objects.create(actor=w.field, command=DOMAIN, outcome="committed", payload={})
            out["stored"] = c.confirm({"effect": "recorded"}, "Recorded")
    except Exception as exc:  # pragma: no cover - reported by the test
        errors.append(exc)
    finally:
        connection.close()


def _online_count() -> int:
    return AuditEvent.objects.filter(command="test.online_command").count()


def test_online_first_a_field_claim_waits_on_the_robot_and_the_online_command_takes_the_run(w):
    """Online command holds robot U + visit S (uncommitted); a field claim on the same run takes its sync lock and
    waits for the robot in its domain step (pg_locks). The online command then takes the run row at once (the
    claim does not hold it), commits; the claim continues and commits. One transaction each, no deadlock."""
    out: dict[str, Any] = {}
    errors: list[Exception] = []
    online = app_conn(autocommit=False)
    try:
        _online_guards(online, w)
        field = threading.Thread(target=_field_with_domain, args=(w, out, errors))
        field.start()
        for _ in range(200):
            if "field_pid" in out:
                break
            time.sleep(0.05)
        assert _pg_waiting(out["field_pid"]), "the field claim should wait for the robot row"
        took = _online_run(online, w)  # would deadlock if the claim held the run row (execution/0009)
        assert took < 2, f"the online command waited {took:.1f}s for the run row"
        field.join(30)
    finally:
        online.close()
    assert not errors, errors
    assert out["stored"].outcome == "confirmed" and out["stored"].seq == 1
    assert _online_count() == 1 and Domain.effects() == 1


def test_field_first_the_online_command_waits_on_the_robot_then_completes(w):
    """A field claim holds its sync lock and, in its domain step, robot U, visit S and run U (paused before its
    commit); an online command on the same run waits for the robot row (pg_locks). The claim commits, then the
    online command takes robot, visit and run and commits. No deadlock."""
    out: dict[str, Any] = {}
    errors: list[Exception] = []
    in_domain, release = threading.Event(), threading.Event()

    def pause() -> None:
        in_domain.set()
        assert release.wait(20)

    testhooks.register("locked:field_domain", pause)
    online = app_conn(autocommit=False)
    online_out: dict[str, Any] = {}
    try:
        with online.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            online_pid = cur.fetchone()[0]
        field = threading.Thread(target=_field_with_domain, args=(w, out, errors))
        field.start()
        assert in_domain.wait(20)

        def online_command() -> None:
            try:
                _online_guards(online, w)
                online_out["took"] = _online_run(online, w)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        bg = threading.Thread(target=online_command)
        bg.start()
        assert _pg_waiting(online_pid), "the online command should wait for the robot row"
        assert _online_count() == 0
        release.set()
        field.join(30)
        bg.join(30)
    finally:
        testhooks.clear()
        online.close()
    assert not errors, errors
    assert out["stored"].outcome == "confirmed" and _online_count() == 1 and Domain.effects() == 1


def test_only_the_field_ledger_takes_the_sync_lock(w):
    """No online path can wait for the ledger lock: only the ledger's own functions reference it in the catalog,
    and only field_ledger (plus the model's table name) in the application code."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.prosrc LIKE '%%execution_field_sync_lock%%' ORDER BY 1"
        )
        assert [r[0] for r in cur.fetchall()] == ["execution_field_lock_for", "execution_field_sync_lock"]
        cur.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE p.proname IN ('execution_field_lock_for', 'execution_field_sync_lock')"
        )
        assert cur.fetchone()[0] == 0
    root = Path(__file__).resolve().parent.parent
    users = sorted(
        str(f.relative_to(root))
        for f in root.rglob("*.py")
        if "tests" not in f.parts
        and "migrations" not in f.parts
        and ".venv" not in f.parts
        and "execution_field_sync_lock" in f.read_text(encoding="utf-8")
    )
    assert users == ["execution/field_ledger.py", "execution/models.py"]


# ------------------------------------------------------------------------------------------------ 8. access
def test_a_revoked_grant_and_a_stranger_are_refused_without_a_write(w):
    from accounts.models import RoleGrant
    from accounts.services import revoke_role

    domain = Domain(w)
    decision, answer = send(key(), ident(w, 1, actor=w.field2), domain)  # a field operator, not crew
    assert decision[:2] == ("rejected", "NOT_YOUR_RUN") and answer.decided_at is None
    assert send(key(), ident(w, 1, actor=w.norole), domain)[0][:2] == ("rejected", "NOT_YOUR_RUN")
    assert send(key(), ident(w, 1, run_id=w.run.pk + 100000), domain)[0][:2] == ("rejected", "NOT_YOUR_RUN")
    grant = RoleGrant.objects.get(user=w.field, role="field_operator", revoked_at__isnull=True)
    revoke_role(w.admin, grant_id=grant.pk, reason="SYN: left the crew")
    decision, answer = send(key(), ident(w, 1), domain)  # crew, but no current grant at the run's hub
    assert decision[:2] == ("rejected", "NOT_PERMITTED")
    with pytest.raises(fl.LedgerRefused) as refused:
        fl.abandon(w.field, w.run.code, 1, 1, "x")
    assert refused.value.code == "NOT_PERMITTED"
    with pytest.raises(fl.LedgerRefused):
        fl.report_device(w.field.pk, "dev-A", w.run.pk, 3)
    assert rows() == [] and domain.calls == 0 and not FieldDeviceReport.objects.exists()
    assert send(key(), ident(w, 1, actor=w.ops), domain)[1].outcome == "confirmed"  # the ops lead still can


def test_device_reports_are_appended_and_the_newest_wins(w):
    fl.report_device(w.field.pk, "dev-A", w.run.pk, 3)
    fl.report_device(w.field.pk, "dev-A", w.run.pk, 0)
    newest = FieldDeviceReport.objects.filter(run=w.run).latest("reported_at", "id")
    assert (newest.unsent_count, newest.device_id, newest.actor_id) == (0, "dev-A", w.field.pk)
    for bad in ((w.field2.pk, "dev-A", 1), (w.field.pk, "", 1), (w.field.pk, "dev-A", -1)):
        with pytest.raises(fl.LedgerRefused):
            fl.report_device(bad[0], bad[1], w.run.pk, bad[2])
    assert FieldDeviceReport.objects.count() == 2


# ------------------------------------------------------------------------------------------------ 9. direct SQL
def test_ops_app_cannot_write_the_ledger_tables_directly(w):
    domain = Domain(w)
    send(key(), ident(w, 1), domain)
    fl.abandon(w.field, w.run.code, 1, 2, "SYN: lost")
    fl.report_device(w.field.pk, "dev-A", w.run.pk, 1)
    forged = (
        "INSERT INTO execution_field_command (key, identity_digest, actor_id, device_id, run_id, channel, "
        "generation, seq, sent_seq, command, payload, device_time_flag, received_at, decided_at, outcome, "
        "reason_code, message, result) VALUES (%s, %s, %s, 'dev-F', %s, 'ordinary', 2, 1, 1, 'trip.arrived_site', "
        "'{}', 'missing', now(), now(), 'confirmed', '', '', '{}')"
    )
    statements = [
        (forged, [key(), "0" * 64, w.field.pk, w.run.pk]),
        ("UPDATE execution_field_command SET outcome = 'confirmed', reason_code = ''", []),
        ('UPDATE execution_field_command SET result = \'{"effect": "forged"}\'', []),
        ("DELETE FROM execution_field_command", []),
        ("TRUNCATE execution_field_command CASCADE", []),
        (
            "INSERT INTO execution_field_generation (run_id, generation, barrier_id, opened_by_id, opened_at) "
            "VALUES (%s, 9, %s, %s, now())",
            [w.run.pk, rows()[0].pk, w.field.pk],
        ),
        ("UPDATE execution_field_generation SET generation = generation + 1", []),
        ("DELETE FROM execution_field_generation", []),
        (
            "INSERT INTO execution_field_device_report (device_id, run_id, actor_id, unsent_count, reported_at) "
            "VALUES ('dev-F', %s, %s, 0, now())",
            [w.run.pk, w.field.pk],
        ),
        ("UPDATE execution_field_device_report SET unsent_count = 0", []),
        ("DELETE FROM execution_field_device_report", []),
        ("SELECT nextval('execution_field_command_id_seq')", []),
        ("INSERT INTO execution_field_sync_lock (run_id, created_at) VALUES (987654321, now())", []),
        ("SELECT run_id FROM execution_field_sync_lock FOR UPDATE", []),  # only through the function
        ("DELETE FROM execution_field_sync_lock", []),
    ]
    before = [
        list(m.objects.order_by("id").values()) for m in (FieldCommand, FieldGeneration, FieldDeviceReport)
    ]
    with app_conn() as conn:
        for sql, params in statements:
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql, params)
    after = [
        list(m.objects.order_by("id").values()) for m in (FieldCommand, FieldGeneration, FieldDeviceReport)
    ]
    assert after == before


def test_the_writer_refuses_an_out_of_order_or_reused_confirmation_as_ops_app(w):
    domain = Domain(w)
    k = key()
    send(k, ident(w, 1), domain)
    with app_conn() as conn:
        for args, error, detail in (
            (record_args(w, actor=w.field.pk, seq=3), psycopg.errors.CheckViolation, "HELD"),
            (record_args(w, actor=w.field.pk, seq=1), psycopg.errors.CheckViolation, "SEQ_TAKEN"),
            (
                record_args(w, actor=w.field.pk, seq=2, gen=2),
                psycopg.errors.CheckViolation,
                "STALE_GENERATION",
            ),
            (record_args(w, actor=w.field.pk, seq=2, key=k), psycopg.errors.UniqueViolation, "KEY_REUSED"),
            (record_args(w, actor=w.field.pk, seq=2, outcome="abandoned", reason="X"), None, "INVALID"),
            (
                record_args(w, actor=w.field.pk, seq=2, outcome="rejected", reason="KEY_REUSED"),
                None,
                "INVALID",
            ),
            (
                record_args(w, actor=w.field.pk, seq=2, outcome="rejected", reason="SEQ_TAKEN"),
                None,
                "INVALID",
            ),
            (record_args(w, actor=w.field.pk, seq=2, digest="nothex"), None, "INVALID"),
            (record_args(w, actor=w.field.pk, seq=2, flag="out_of_range"), None, "DEVICE_TIME_OUT_OF_BOUNDS"),
        ):
            with conn.cursor() as cur, pytest.raises(error or psycopg.errors.CheckViolation) as err:
                cur.execute(RECORD_SQL, args)
            assert err.value.diag.message_detail == detail
    assert len(rows()) == 1 and fl.state(w.run.pk)["next_expected_seq"] == 2


@pytest.mark.parametrize("actor", ["NULL", "0", "missing", "no_role", "founder", "not_crew"])
def test_forged_null_and_empty_actors_on_the_functions_write_nothing(w, actor):
    a = {
        "NULL": None,
        "0": 0,
        "missing": 987654321,
        "no_role": w.norole.pk,
        "founder": w.founder_id,
        "not_crew": w.field2.pk,  # a real field operator at the run's hub, not crew of this run
    }[actor]
    calls = [
        (RECORD_SQL, record_args(w, actor=a)),
        ("SELECT execution_field_abandon(%s, %s, 1, 1, 'x', '')", [a, w.run.pk]),
        ("SELECT execution_field_report_device(%s, %s, 'dev-F', 1)", [a, w.run.pk]),
    ]
    before = (
        FieldCommand.objects.count(),
        FieldGeneration.objects.count(),
        FieldDeviceReport.objects.count(),
    )
    audits = AuditEvent.objects.count()
    with app_conn() as conn:
        for sql, params in calls:
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege) as err:
                cur.execute(sql, params)
            assert err.value.diag.message_detail in ("NOT_YOUR_RUN", "NOT_PERMITTED")
    after = (FieldCommand.objects.count(), FieldGeneration.objects.count(), FieldDeviceReport.objects.count())
    assert after == before == (0, 0, 0) and AuditEvent.objects.count() == audits


# ------------------------------------------------------------------------------------------------ 10. catalog
def test_the_ledger_is_classified_in_the_catalog_audit_and_select_only_for_ops_app(w):
    from tests import test_v2_03a_definer_audit as audit

    for sig in (
        "execution_field_record(bigint,bigint,uuid,text,text,text,integer,integer,text,jsonb,"
        "timestamp with time zone,text,jsonb,text,text,text,jsonb)",
        "execution_field_abandon(bigint,bigint,integer,integer,text,text)",
        "execution_field_report_device(bigint,bigint,text,integer)",
    ):
        assert sig in audit.APP_FUNCTIONS and sig in audit.ACTOR_CALLS
    assert set(TABLES) <= set(audit.GUARDED_TABLES)
    with connection.cursor() as cur:
        for t in TABLES:
            cur.execute(
                "SELECT has_table_privilege(current_user, %s, 'SELECT'), "
                "has_table_privilege(current_user, %s, 'INSERT') OR has_any_column_privilege(current_user, %s, "
                "'INSERT'), has_table_privilege(current_user, %s, 'UPDATE') OR "
                "has_any_column_privilege(current_user, %s, 'UPDATE'), has_table_privilege(current_user, %s, "
                "'DELETE'), has_table_privilege(current_user, %s, 'TRUNCATE')",
                [t] * 7,
            )
            assert cur.fetchone() == (True, False, False, False, False), t
        # the decision rule and the readers are plain functions (not SECURITY DEFINER) ops_app may call
        cur.execute(
            "SELECT p.proname, p.prosecdef, has_function_privilege('public', p.oid, 'EXECUTE') FROM pg_proc p "
            "WHERE p.proname LIKE 'execution\\_field\\_%' ORDER BY 1"
        )
        found = {name: (definer, public) for name, definer, public in cur.fetchall()}
    assert found == {
        "execution_field_abandon": (True, False),
        "execution_field_access": (False, False),
        "execution_field_decide": (False, False),
        "execution_field_deny": (False, False),
        "execution_field_fail": (False, False),
        "execution_field_generation_now": (False, False),
        "execution_field_lock_for": (False, False),
        "execution_field_next_seq": (False, False),
        "execution_field_record": (True, False),
        "execution_field_report_device": (True, False),
        "execution_field_sync_lock": (True, False),
    }
