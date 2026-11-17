"""Deterministic interleavings on real Postgres. A pause hook stops the FIRST transaction right after it
holds its locks; we then prove the second transaction is blocked, release the first, and check outcomes.
The background worker does not exist / is not involved: enforcement is purely transactional."""

import random
import threading
import time

import psycopg
import pytest
from django.db import OperationalError, connection

from accounts.models import RoleGrant
from accounts.services import revoke_role
from fleet import services as s
from fleet.models import EligibilityCheck, Robot
from platform_core import testhooks
from platform_core.commands import PermissionDenied
from platform_core.locks import lock_guards
from platform_core.models import AuditEvent  # noqa: F401
from tests.conftest import make_user
from tests.fleet_world import build_world

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def w():
    world = build_world(robots=3, prefix="cc_")
    yield world
    testhooks.clear()


class Interleaving:
    """first() runs and pauses at `checkpoint`; second() starts and must block until first commits."""

    def __init__(self, checkpoint):
        self.checkpoint, self.paused, self.go = checkpoint, threading.Event(), threading.Event()
        self.results = {}

        def hook():
            if threading.current_thread().name == "first":
                self.paused.set()
                assert self.go.wait(20)

        testhooks.register(checkpoint, hook)

    def run(self, first, second):
        def wrap(name, fn):
            try:
                self.results[name] = fn()
            except Exception as e:  # noqa: BLE001
                self.results[name] = e
            finally:
                connection.close()

        t1 = threading.Thread(target=wrap, args=("first", first), name="first")
        t1.start()
        assert self.paused.wait(20), "first transaction never reached its checkpoint"
        t2 = threading.Thread(target=wrap, args=("second", second), name="second")
        t2.start()
        time.sleep(0.6)
        blocked = t2.is_alive()
        self.go.set()
        t1.join(30)
        t2.join(30)
        return blocked, self.results.get("first"), self.results.get("second")


def check(w, code="R-01"):
    return s.check_eligibility(w.ops, robot_code=code)


# ---------- hold vs eligibility check: both lock orders
def test_hold_first_then_check_sees_hold(w):
    it = Interleaving("locked:fleet.place_hold")
    blocked, first, second = it.run(
        lambda: s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x"),
        lambda: check(w),
    )
    assert blocked, "the checker must wait for the writer's lock"
    assert isinstance(first, dict) and second["eligible_for_planning"] is False


def test_check_first_then_hold_check_stands_and_hold_applies_after(w):
    it = Interleaving("locked:fleet.check_eligibility")
    blocked, first, second = it.run(
        lambda: check(w),
        lambda: s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x"),
    )
    assert blocked and first["eligible_for_planning"] is True and isinstance(second, dict)
    assert Robot.objects.get(code="R-01").version > first["robot_version_observed"]
    assert check(w)["eligible_for_planning"] is False


def test_pattern_hold_first_then_check_on_any_robot_of_that_pattern(w):
    it = Interleaving("locked:fleet.place_hold")
    blocked, _, second = it.run(
        lambda: s.place_hold(
            w.lead, scope="manifest_pattern", target="P-A", source_type="engineering", reason="bug"
        ),
        lambda: check(w, "R-03"),
    )
    assert blocked and second["eligible_for_planning"] is False


# ---------- configuration change vs check: both orders
def test_manifest_change_first_then_check_sees_new_configuration(w):
    it = Interleaving("locked:fleet.record_manifest")
    blocked, first, second = it.run(
        lambda: s.record_manifest(
            w.eng, robot_code="R-01", pattern_code="P-C", autonomy_version="a9", safety_config_hash="n"
        ),
        lambda: check(w),
    )
    assert blocked
    rec = EligibilityCheck.objects.get(pk=int(second["entity_id"]))
    assert rec.manifest_id == first["manifest_id"]
    assert "No base home-use authorization for configuration P-C" in second["reasons"]


def test_check_first_then_manifest_change(w):
    old = Robot.objects.get(code="R-01").current_manifest_id
    it = Interleaving("locked:fleet.check_eligibility")
    blocked, first, second = it.run(
        lambda: check(w),
        lambda: s.record_manifest(
            w.eng, robot_code="R-01", pattern_code="P-C", autonomy_version="a9", safety_config_hash="n"
        ),
    )
    assert blocked and first["eligible_for_planning"] is True
    assert EligibilityCheck.objects.get(pk=int(first["entity_id"])).manifest_id == old
    assert isinstance(second, dict)


# ---------- role revocation vs privileged command: both orders
def _ops_grant(w):
    return RoleGrant.objects.get(user=w.ops, role="ops_lead", revoked_at__isnull=True)


def test_command_first_then_revoke_waits(w):
    admin = make_user("cc_admin", "admin")
    it = Interleaving("authorized:fleet.place_hold")
    blocked, first, second = it.run(
        lambda: s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x"),
        lambda: revoke_role(admin, grant_id=_ops_grant(w).pk, reason="left"),
    )
    assert blocked, "revocation must wait for the in-flight privileged command"
    assert isinstance(first, dict) and isinstance(second, dict)


def test_revoke_first_then_command_is_denied(w):
    admin = make_user("cc_admin", "admin")
    grant_id = _ops_grant(w).pk
    it = Interleaving("before_commit:accounts.revoke_role")
    blocked, first, second = it.run(
        lambda: revoke_role(admin, grant_id=grant_id, reason="left"),
        lambda: s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x"),
    )
    assert blocked and isinstance(first, dict)
    assert isinstance(second, PermissionDenied)
    assert not Robot.objects.get(code="R-01").holds.exists()


# ---------- deadlocks
def _app_conn():
    import os

    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
    )


def test_opposite_lock_order_deadlock_is_detected(w):
    """Proves the test harness can see a deadlock: two raw transactions lock robots in opposite orders."""
    ids = list(Robot.objects.order_by("code").values_list("pk", flat=True))[:2]
    a, b = _app_conn(), _app_conn()
    errors = []

    def tx(conn, first, second, barrier):
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM fleet_robot WHERE id = %s FOR UPDATE", [first])
                barrier.wait()
                cur.execute("SELECT id FROM fleet_robot WHERE id = %s FOR UPDATE", [second])
            conn.commit()
        except psycopg.errors.DeadlockDetected as e:
            errors.append(e)
            conn.rollback()

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=tx, args=(a, ids[0], ids[1], barrier))
    t2 = threading.Thread(target=tx, args=(b, ids[1], ids[0], barrier))
    t1.start(), t2.start(), t1.join(30), t2.join(30)
    a.close(), b.close()
    assert len(errors) == 1


def test_lock_guards_orders_locks_so_opposite_requests_never_deadlock(w):
    ids = list(Robot.objects.order_by("code").values_list("pk", flat=True))
    from django.db import transaction

    errors = []

    def worker(order):
        try:
            for _ in range(15):
                with transaction.atomic():
                    lock_guards(update_robots=order)
                    time.sleep(0.005)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            connection.close()

    t1 = threading.Thread(target=worker, args=(ids,))
    t2 = threading.Thread(target=worker, args=(list(reversed(ids)),))
    t1.start(), t2.start(), t1.join(60), t2.join(60)
    assert errors == []


# ---------- transient failure retry, single effect
def test_transient_deadlock_is_retried_with_one_effect(w, monkeypatch):
    class Cause(Exception):
        sqlstate = "40P01"

    calls = {"n": 0}
    real = s._create_hold

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("deadlock detected") from Cause()
        return real(*a, **kw)

    monkeypatch.setattr(s, "_create_hold", flaky)
    r = s.place_hold(
        w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x", idempotency_key="retry-1"
    )
    assert calls["n"] == 2 and r["replayed"] is False
    assert Robot.objects.get(code="R-01").holds.count() == 1
    assert AuditEvent.objects.filter(command="fleet.place_hold", outcome="committed").count() == 1


# ---------- randomized oracle: no eligible check after a committed blocking hold
@pytest.mark.parametrize("seed", range(10))
def test_randomized_hold_vs_check_oracle(w, seed):
    rnd = random.Random(seed)
    plan = ["hold"] + ["check"] * 4
    rnd.shuffle(plan)
    barrier = threading.Barrier(len(plan))

    def act(kind):
        try:
            barrier.wait()
            time.sleep(rnd.random() * 0.02)
            if kind == "hold":
                s.place_hold(w.ops, scope="robot", target="R-02", source_type="maintenance", reason="r")
            else:
                check(w, "R-02")
        finally:
            connection.close()

    threads = [threading.Thread(target=act, args=(k,)) for k in plan]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    # Oracle from GUARD VERSIONS (not sequence ids): the robot version changes only under the robot's FOR
    # UPDATE lock, and only the hold changes it here (+1). Each check reads the version under FOR SHARE.
    # So a check that observed the final version ran after the hold committed and must be ineligible; a
    # check that observed the earlier version ran before it and must be eligible.
    final = Robot.objects.get(code="R-02").version
    checks = list(EligibilityCheck.objects.filter(robot__code="R-02"))
    assert len(checks) == 4
    for c in checks:
        assert c.robot_version_observed in (final - 1, final)
        assert c.eligible_for_planning == (c.robot_version_observed == final - 1), (
            "an eligible check observed the post-hold guard version"
        )
