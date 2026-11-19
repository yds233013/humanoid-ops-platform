"""Deterministic interleavings for departure authorization and departure on real Postgres.

The Interleaving helper pauses the FIRST command at a named checkpoint (right after its locks, or before its
commit), proves the SECOND one is blocked, releases the first and checks both outcomes. Every guard change
that matters is tested in BOTH orders against authorize_departure and record_departure. No background worker
is involved: enforcement is purely transactional. SYNTHETIC data only."""

import re
import threading
import time
from datetime import timedelta

import pytest
from django.db import OperationalError, connection

from fleet import services as fs
from planning.models import ConsentRecord, Qualification, Visit
from platform_core import testhooks
from platform_core.commands import Conflict
from platform_core.locks import ORDER, lock_settings_shared
from platform_core.models import AuditEvent, IdempotencyKey
from readiness import commands as rc
from readiness.authorization import authorization_status
from readiness.models import AuthorizationRevocation, DepartureAuthorization, DepartureEvent
from tests.readiness_world import build_readiness
from tests.test_fleet_concurrency import Interleaving

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def w():
    world = build_readiness(prefix="rcc_")
    yield world
    testhooks.clear()


def authorize(w, **kw):
    return rc.authorize_departure(w.ops, visit_code=w.visit, **kw)


def depart(w, code, actor=None, **kw):
    return rc.record_departure(actor or w.ops, authorization_code=code, **kw)


def status(code):
    return authorization_status(DepartureAuthorization.objects.get(code=code))


def shift(w, hours=1, **kw):
    r = Visit.objects.get(code=w.visit).current_revision
    return w.p.plan_visit(
        w.ops,
        visit_code=w.visit,
        robot_code=kw.pop("robot", "R-01"),
        lead_code="P-1",
        second_code="P-2",
        vehicle_code="VAN-1",
        on_site_start=r.on_site_start + timedelta(hours=hours),
        on_site_end=r.on_site_end + timedelta(hours=hours),
        **kw,
    )


# ------------------------------------------------------------------ the guard changes under test
# name -> (checkpoint after the change's locks, change, refusal text for authorize, guard named at departure)
def _hold(w):
    return fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")


def _consent(w):
    c = ConsentRecord.objects.get(site__code="S-1", participant_ref="P1")
    return w.p.withdraw_consent(w.field, consent_id=c.pk, reason="withdrawn")


def _manifest(w):
    return fs.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a2", safety_config_hash="m" * 64
    )


def _reschedule(w):
    return shift(w)


def _qualification(w):
    q = Qualification.objects.get(person__code="P-2", kind="robot_handler", revoked_at__isnull=True)
    return w.p.revoke_qualification(w.ops, qualification_id=q.pk, reason="x")


def _withdraw_policy(w):
    return w.rx.withdraw_readiness_policy(w.lead, version=w.policy_version, reason="procedure pulled")


CHANGES = {
    "hold": ("locked:fleet.place_hold", _hold, "PLANNING_ROBOT_NOT_ELIGIBLE", "robot/hold or configuration"),
    "consent": ("locked:planning.withdraw_consent", _consent, "PLANNING_CONSENT", "site/consent/assessment"),
    "manifest": (
        "locked:fleet.record_manifest",
        _manifest,
        "EVIDENCE_WRONG_CONFIGURATION",
        "robot/hold or configuration",
    ),
    "reschedule": (
        "locked:planning.plan_visit",
        _reschedule,
        "VISIT_NOT_CONFIRMED",
        "visit reschedule/evidence",
    ),
    "qualification": (
        "locked:planning.revoke_qualification",
        _qualification,
        "PLANNING_QUALIFICATION",
        "crew qualification",
    ),
    "policy_withdrawal": (
        "locked:readiness.withdraw_readiness_policy",
        _withdraw_policy,
        "POLICY_MISSING",
        "policy approval/withdrawal",
    ),
}


@pytest.mark.parametrize("name", list(CHANGES))
def test_change_first_then_authorize_is_refused(w, name):
    cp, change, refusal, _ = CHANGES[name]
    blocked, first, second = Interleaving(cp).run(lambda: change(w), lambda: authorize(w))
    assert blocked, "authorize must wait for the guard writer's lock"
    assert isinstance(first, dict), first
    assert isinstance(second, Conflict) and refusal in str(second), second
    assert not DepartureAuthorization.objects.exists()


@pytest.mark.parametrize("name", list(CHANGES))
def test_authorize_first_then_change_applies_after_and_invalidates(w, name):
    _, change, _, guard = CHANGES[name]
    blocked, first, second = Interleaving("locked:readiness.authorize_departure").run(
        lambda: authorize(w), lambda: change(w)
    )
    assert blocked, "the guard writer must wait for authorize's locks"
    assert isinstance(first, dict) and isinstance(second, dict), (first, second)
    st = status(first["code"])
    assert st["state"] == "invalidated" and any(r.startswith(guard) for r in st["reasons"]), st
    with pytest.raises(Conflict, match=re.escape(guard)):
        depart(w, first["code"])
    assert not DepartureEvent.objects.exists()


@pytest.mark.parametrize("name", list(CHANGES))
def test_change_first_then_depart_is_refused(w, name):
    cp, change, _, guard = CHANGES[name]
    code = authorize(w)["code"]
    blocked, first, second = Interleaving(cp).run(lambda: change(w), lambda: depart(w, code))
    assert blocked, "depart must wait for the guard writer's lock"
    assert isinstance(first, dict), first
    assert isinstance(second, Conflict) and "invalidated" in str(second) and guard in str(second), second
    assert not DepartureEvent.objects.exists()


@pytest.mark.parametrize("name", list(CHANGES))
def test_depart_first_then_change_applies_after(w, name):
    _, change, _, _ = CHANGES[name]
    code = authorize(w)["code"]
    blocked, first, second = Interleaving("locked:readiness.record_departure").run(
        lambda: depart(w, code), lambda: change(w)
    )
    assert blocked, "the guard writer must wait for the departure's locks"
    assert isinstance(first, dict), first
    if name == "reschedule":
        # Once a visit has departed it can no longer be re-planned (readiness migration 0003).
        assert isinstance(second, Conflict) and "departed" in str(second), second
    else:
        assert isinstance(second, dict), second
    ev = DepartureEvent.objects.get()
    assert ev.guard_snapshot == DepartureAuthorization.objects.get(code=code).guard_snapshot
    assert status(code)["state"] == "used"


# ------------------------------------------------------------------ reschedule between read and locks
def _pause_first_read(cp):
    paused, go, calls = threading.Event(), threading.Event(), {"n": 0}

    def hook():
        calls["n"] += 1
        if calls["n"] == 1:  # only the first attempt pauses
            paused.set()
            assert go.wait(20)

    testhooks.register(cp, hook)
    return paused, go, calls


def _in_thread(fn, out, key):
    def run():
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            out[key] = e
        finally:
            connection.close()

    t = threading.Thread(target=run)
    t.start()
    return t


def test_reschedule_between_authorize_read_and_locks_is_retried_on_the_new_revision(w):
    """authorize reads revision rN (robot R-01) and pauses before locking. A reschedule to R-03 commits, the
    visit is confirmed again and evidence is recorded for rN+1. authorize must notice, retry with the CURRENT
    revision's locks, and issue an authorization bound to rN+1 / R-03."""
    old_rev = Visit.objects.get(code=w.visit).current_revision_id
    paused, go, calls = _pause_first_read("read:readiness.authorize_departure")
    out: dict = {}
    t = _in_thread(lambda: authorize(w), out, "auth")
    assert paused.wait(20)
    shift(w, robot="R-03")
    w.p.confirm_visit(w.ops, visit_code=w.visit)
    w.record_all_evidence(w.visit, robot="R-03")
    go.set()
    t.join(30)
    assert calls["n"] == 2, "authorize must have retried after detecting the newer revision"
    assert isinstance(out["auth"], dict), out["auth"]
    a = DepartureAuthorization.objects.get(code=out["auth"]["code"])
    v = Visit.objects.get(code=w.visit)
    assert a.revision_id == v.current_revision_id != old_rev
    assert a.robot.code == "R-03" and a.guard_snapshot["revision_id"] == v.current_revision_id


def test_reschedule_between_authorize_read_and_locks_without_new_evidence_is_refused(w):
    paused, go, calls = _pause_first_read("read:readiness.authorize_departure")
    out: dict = {}
    t = _in_thread(lambda: authorize(w), out, "auth")
    assert paused.wait(20)
    shift(w, robot="R-03")
    go.set()
    t.join(30)
    assert calls["n"] == 2
    assert isinstance(out["auth"], Conflict) and "VISIT_NOT_CONFIRMED" in str(out["auth"])
    assert not DepartureAuthorization.objects.exists()


def test_reschedule_between_depart_read_and_locks_is_refused(w):
    code = authorize(w)["code"]
    paused, go, calls = _pause_first_read("read:readiness.record_departure")
    out: dict = {}
    t = _in_thread(lambda: depart(w, code), out, "dep")
    assert paused.wait(20)
    shift(w, robot="R-03")
    go.set()
    t.join(30)
    assert calls["n"] == 2, "depart must have retried with the current revision's locks"
    assert isinstance(out["dep"], Conflict) and "visit reschedule/evidence" in str(out["dep"])
    assert not DepartureEvent.objects.exists()


# ------------------------------------------------------------------ single use, revoke
def test_two_simultaneous_departures_exactly_one_event(w):
    code = authorize(w)["code"]
    blocked, first, second = Interleaving("locked:readiness.record_departure").run(
        lambda: depart(w, code), lambda: depart(w, code, actor=w.field)
    )
    assert blocked and isinstance(first, dict)
    assert isinstance(second, Conflict) and "already used" in str(second)
    assert DepartureEvent.objects.filter(authorization__code=code).count() == 1


def test_database_alone_prevents_double_departure_when_app_locks_are_bypassed(w, monkeypatch):
    """Mutation check: with the command's lock_guards disabled, both departures pass the application's
    'already used' check, and the unique constraint on DepartureEvent.authorization still allows one."""
    code = authorize(w)["code"]
    # Slice 7: the row locks stay bypassed, but the settings lock is still taken, since the departure trigger
    # now asserts it (EM ruling 1); without it both departures are refused before the unique key decides.
    monkeypatch.setattr(
        rc, "lock_guards", lambda **kw: lock_settings_shared() if kw.get("share_settings") else None
    )
    blocked, first, second = Interleaving("before_commit:readiness.record_departure").run(
        lambda: depart(w, code), lambda: depart(w, code, actor=w.field)
    )
    assert blocked, "the second insert must wait on the first one's unique key"
    assert isinstance(first, dict) and isinstance(second, Conflict), (first, second)
    assert "already used" not in str(second)  # the app check was bypassed; the database refused it
    assert DepartureEvent.objects.filter(authorization__code=code).count() == 1


def test_revoke_first_then_depart_is_refused(w):
    code = authorize(w)["code"]
    blocked, first, second = Interleaving("locked:readiness.revoke_authorization").run(
        lambda: rc.revoke_authorization(w.safety, authorization_code=code, reason="weather"),
        lambda: depart(w, code),
    )
    assert blocked and isinstance(first, dict)
    assert isinstance(second, Conflict) and "revoked" in str(second)
    assert not DepartureEvent.objects.exists()


def test_depart_first_then_revoke_is_refused(w):
    code = authorize(w)["code"]
    blocked, first, second = Interleaving("locked:readiness.record_departure").run(
        lambda: depart(w, code),
        lambda: rc.revoke_authorization(w.safety, authorization_code=code, reason="weather"),
    )
    assert blocked and isinstance(first, dict)
    assert isinstance(second, Conflict) and "already used" in str(second)
    assert not AuthorizationRevocation.objects.exists() and DepartureEvent.objects.count() == 1


# ------------------------------------------------------------------ idempotency under concurrency
def test_concurrent_idempotent_authorize_replays_one_decision(w):
    blocked, first, second = Interleaving("locked:readiness.authorize_departure").run(
        lambda: authorize(w, idempotency_key="same"), lambda: authorize(w, idempotency_key="same")
    )
    assert blocked and first["replayed"] is False and second["replayed"] is True
    assert second["code"] == first["code"] and DepartureAuthorization.objects.count() == 1


def test_concurrent_idempotent_depart_replays_one_event(w):
    code = authorize(w)["code"]
    blocked, first, second = Interleaving("locked:readiness.record_departure").run(
        lambda: depart(w, code, idempotency_key="d"), lambda: depart(w, code, idempotency_key="d")
    )
    assert blocked and first["replayed"] is False and second["replayed"] is True
    assert DepartureEvent.objects.count() == 1


# ------------------------------------------------------------------ retry and rollback
class _Deadlock(Exception):
    sqlstate = "40P01"


def test_transient_deadlock_is_retried_with_one_authorization(w, monkeypatch):
    calls = {"n": 0}
    real = rc.evaluate_readiness

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("deadlock detected") from _Deadlock()
        return real(*a, **kw)

    monkeypatch.setattr(rc, "evaluate_readiness", flaky)
    r = authorize(w, idempotency_key="retry-1")
    assert calls["n"] == 2 and r["replayed"] is False
    assert DepartureAuthorization.objects.count() == 1
    assert (
        AuditEvent.objects.filter(command="readiness.authorize_departure", outcome="committed").count() == 1
    )


def test_transient_deadlock_on_depart_is_retried_with_one_event(w, monkeypatch):
    code = authorize(w)["code"]
    calls = {"n": 0}
    real = rc.evaluate_readiness

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("deadlock detected") from _Deadlock()
        return real(*a, **kw)

    monkeypatch.setattr(rc, "evaluate_readiness", flaky)
    depart(w, code)
    assert calls["n"] == 2 and DepartureEvent.objects.count() == 1


def _boom():
    raise RuntimeError("injected failure before commit")


def test_failure_before_commit_rolls_back_authorization(w):
    testhooks.register("before_commit:readiness.authorize_departure", _boom)
    with pytest.raises(RuntimeError):
        authorize(w, idempotency_key="boom")
    assert not DepartureAuthorization.objects.exists()
    assert not AuditEvent.objects.filter(command="readiness.authorize_departure").exists()
    assert not IdempotencyKey.objects.filter(key="boom").exists()
    testhooks.clear()
    assert authorize(w, idempotency_key="boom")["replayed"] is False  # nothing half-done blocks a retry


def test_failure_before_commit_rolls_back_departure(w):
    code = authorize(w)["code"]
    testhooks.register("before_commit:readiness.record_departure", _boom)
    with pytest.raises(RuntimeError):
        depart(w, code)
    assert not DepartureEvent.objects.exists() and status(code)["state"] == "usable"
    testhooks.clear()
    depart(w, code)
    assert DepartureEvent.objects.count() == 1


def test_failure_before_commit_rolls_back_revocation(w):
    code = authorize(w)["code"]
    testhooks.register("before_commit:readiness.revoke_authorization", _boom)
    with pytest.raises(RuntimeError):
        rc.revoke_authorization(w.ops, authorization_code=code, reason="x")
    assert not AuthorizationRevocation.objects.exists() and status(code)["state"] == "usable"


# ------------------------------------------------------------------ lock acquisition order
LOCK_RE = re.compile(r"FROM (\w+) WHERE .* FOR (UPDATE|SHARE)", re.S)
RANK = {"accounts_rolegrant": -1, **{t: i for i, (t, _) in enumerate(ORDER)}}


def _capture_locks(fn):
    seen: list[tuple[str, str, object]] = []

    def wrapper(execute, sql, params, many, context):
        m = LOCK_RE.search(sql)
        if m:
            seen.append((m.group(1), m.group(2), params[0] if params else None))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        fn()
    return seen


def _assert_global_order(seen):
    ranks = [(RANK[t], rid if t != "accounts_rolegrant" else 0) for t, _, rid in seen]
    assert ranks == sorted(ranks), seen


def _ids(model, **kw):
    return sorted(model.objects.filter(**kw).values_list("pk", flat=True))


def test_authorize_lock_sequence(w):
    from planning.models import Person

    v = Visit.objects.get(code=w.visit)
    seen = _capture_locks(lambda: authorize(w))
    _assert_global_order(seen)
    # lead, second, on-call, escalation and (Slice 7, 07a 9.2) P-3, the signer-only person of the relied
    # inspections (P-2 is crew and signer: one entry)
    persons = _ids(Person, code__in=["P-1", "P-2", "P-3", "P-4", "P-5"])
    assert [(t, m) for t, m, _ in seen] == [
        ("accounts_rolegrant", "SHARE"),
        ("planning_site", "SHARE"),
        ("fleet_manifestpattern", "SHARE"),
        ("fleet_robot", "SHARE"),
        *[("planning_person", "SHARE")] * 5,  # Slice 7: + signer-only P-3
        ("planning_vehicle", "SHARE"),
        ("planning_visit", "UPDATE"),
        ("readiness_readinesspolicy", "SHARE"),
    ]
    assert [rid for t, _, rid in seen if t == "planning_person"] == persons
    assert [rid for t, _, rid in seen if t == "planning_visit"] == [v.pk]


def test_depart_lock_sequence(w):
    code = authorize(w)["code"]
    a = DepartureAuthorization.objects.get(code=code)
    seen = _capture_locks(lambda: depart(w, code))
    _assert_global_order(seen)
    # Slice 4 contract C2: departure locks the robot and the visit's trip FOR UPDATE (same order positions).
    assert [(t, m) for t, m, _ in seen] == [
        ("accounts_rolegrant", "SHARE"),
        ("planning_site", "SHARE"),
        ("fleet_manifestpattern", "SHARE"),
        ("fleet_robot", "UPDATE"),
        *[("planning_person", "SHARE")] * 5,  # Slice 7: + signer-only P-3
        ("planning_vehicle", "SHARE"),
        ("planning_visit", "UPDATE"),
        ("planning_vehicletrip", "UPDATE"),
        ("readiness_readinesspolicy", "SHARE"),
        ("readiness_departureauthorization", "UPDATE"),
    ]
    assert seen[-1][2] == a.pk


def test_revoke_lock_sequence(w):
    code = authorize(w)["code"]
    seen = _capture_locks(lambda: rc.revoke_authorization(w.ops, authorization_code=code, reason="x"))
    _assert_global_order(seen)
    assert [(t, m) for t, m, _ in seen] == [
        ("accounts_rolegrant", "SHARE"),
        ("planning_visit", "UPDATE"),
        ("readiness_departureauthorization", "UPDATE"),
    ]


def _deadlocks():
    with connection.cursor() as cur:
        cur.execute("SELECT pg_stat_clear_snapshot()")
        cur.execute("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
        return cur.fetchone()[0]


@pytest.mark.parametrize("seed", range(3))
def test_mixed_concurrent_commands_never_deadlock_and_keep_single_use(w, seed):
    """Everything at once on one visit: two departures, a revocation, a fresh authorization, a hold, a
    qualification grant and a policy approval of a draft. Every command takes its locks in the global order,
    so PostgreSQL reports no deadlock; at most one departure exists and it matches its authorization."""
    import random

    from planning.models import Person  # noqa: F401

    rnd = random.Random(seed)
    code = authorize(w)["code"]
    draft = w.make_policy(approve=False)
    lo = Visit.objects.get(code=w.visit).current_revision.on_site_start - timedelta(days=1)
    acts = [
        lambda: depart(w, code),
        lambda: depart(w, code, actor=w.field),
        lambda: rc.revoke_authorization(w.safety, authorization_code=code, reason="r"),
        lambda: authorize(w),
        lambda: fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x"),
        lambda: w.p.grant_qualification(
            w.ops, person_code="P-1", kind="field_operator", valid_from=lo, valid_to=lo + timedelta(days=9)
        ),
        lambda: w.rx.approve_readiness_policy(w.lead2, version=draft, slot="engineering"),
    ]
    rnd.shuffle(acts)
    before = _deadlocks()
    barrier, results = threading.Barrier(len(acts)), []

    def go(fn):
        try:
            barrier.wait()
            time.sleep(rnd.random() * 0.02)
            results.append(fn())
        except Exception as e:  # noqa: BLE001
            results.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=go, args=(a,)) for a in acts]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert all(isinstance(r, dict | Conflict) for r in results), results
    time.sleep(0.5)  # exiting backends flush their statistics
    assert _deadlocks() == before
    events = list(DepartureEvent.objects.select_related("authorization"))
    assert len(events) <= 1
    for ev in events:
        assert ev.guard_snapshot == ev.authorization.guard_snapshot
        assert not AuthorizationRevocation.objects.filter(authorization=ev.authorization).exists()
