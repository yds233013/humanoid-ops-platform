"""A demo -> production switch racing departure authorization and departure (Slice 3, Engineer F).

Settings changes take the settings advisory lock EXCLUSIVE (fleet migration 0007); authorize_departure and
record_departure take it SHARED before any guard row (platform_core.locks.lock_guards(share_settings=True)).
Every interleaving is forced with AdvisoryInterleaving (first transaction paused at a named checkpoint; while
it is paused we prove from pg_stat_activity that the second backend is WAITING on the advisory lock and has
not returned), in BOTH orders, for both commands. The mutation tests switch the shared lock
off and show the bad interleaving then happens, so these tests really detect the race. SYNTHETIC data only.
"""

import re
import threading
import time

import psycopg
import pytest
from django.db import connection, transaction

from fleet.models import DeploymentSetting
from platform_core import locks, testhooks
from platform_core.commands import Conflict
from readiness import commands as rc
from readiness.models import DepartureAuthorization, DepartureEvent
from tests.fleet_world import set_setting
from tests.readiness_world import build_readiness

pytestmark = pytest.mark.django_db(transaction=True)

SWITCHED = "test:settings_switched"  # checkpoint inside the owner's settings transaction, after the UPDATE


@pytest.fixture
def w():
    world = build_readiness(prefix="rsr_")
    yield world
    testhooks.clear()


def authorize(w):
    return rc.authorize_departure(w.ops, visit_code=w.visit)


def depart(w, code):
    return rc.record_departure(w.ops, authorization_code=code)


class AdvisoryInterleaving:
    """first() runs and pauses at `checkpoint` (thread name 'first'). second() starts; WHILE first is still
    paused we poll pg_stat_activity for a backend waiting on an advisory lock and record whether second has
    already returned. Then first is released and both finish.

    run() -> (waited_on_advisory, returned_while_paused, first_result, second_result)"""

    def __init__(self, checkpoint):
        self.paused, self.go, self.results = threading.Event(), threading.Event(), {}

        def hook():
            if threading.current_thread().name == "first":
                self.paused.set()
                assert self.go.wait(20)

        testhooks.register(checkpoint, hook)

    @staticmethod
    def _advisory_waiters() -> int:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
            )
            return int(cur.fetchone()[0])

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
        assert self._advisory_waiters() == 0
        t2 = threading.Thread(target=wrap, args=("second", second), name="second")
        t2.start()
        waited, deadline = False, time.monotonic() + 5
        while time.monotonic() < deadline and t2.is_alive():
            if self._advisory_waiters() > 0:
                waited = True
                break
            time.sleep(0.05)
        if waited:
            time.sleep(0.3)  # still waiting a moment later, not a transient blip
            waited = t2.is_alive() and self._advisory_waiters() > 0
        else:
            t2.join(5)  # unblocked (mutation): let it finish while first is still paused
        returned_while_paused = not t2.is_alive()
        assert t1.is_alive(), "first must still be paused while second is observed"
        self.go.set()
        t1.join(30)
        t2.join(30)
        return waited, returned_while_paused, self.results.get("first"), self.results.get("second")


def switch_to_production():
    """The schema owner flips demo_mode in its own transaction and pauses (holding the exclusive settings
    lock taken by the statement trigger) at SWITCHED before committing."""
    with transaction.atomic():
        set_setting("demo_mode", "false")
        testhooks.checkpoint(SWITCHED)
    return "switched"


def is_production():
    return DeploymentSetting.objects.get(key="demo_mode").value == "false"


def no_shared_settings_lock(monkeypatch):
    """Mutation: lock_guards(share_settings=True) silently takes no settings lock."""
    monkeypatch.setattr(locks, "lock_settings_shared", lambda: None)


# ------------------------------------------------------------------ record_departure
def test_switch_first_then_depart_waits_and_is_refused(w):
    code = authorize(w)["code"]
    assert DepartureAuthorization.objects.get(code=code).mode == "demo"
    waited, returned, first, second = AdvisoryInterleaving(SWITCHED).run(
        switch_to_production, lambda: depart(w, code)
    )
    assert waited and not returned, "the departure must wait for the uncommitted settings change"
    assert first == "switched" and is_production()
    assert isinstance(second, Conflict), second
    assert "demo authorization can never be used for departure in production" in str(second)
    assert not DepartureEvent.objects.exists()


@pytest.mark.parametrize(
    "pause", ["locked:readiness.record_departure", "before_commit:readiness.record_departure"]
)
def test_depart_first_then_switch_waits_departure_is_demo(w, pause):
    code = authorize(w)["code"]
    waited, returned, first, second = AdvisoryInterleaving(pause).run(
        lambda: depart(w, code), switch_to_production
    )
    assert waited and not returned, "the settings change must wait until the departure commits"
    assert isinstance(first, dict) and first["mode"] == "demo", first
    assert second == "switched" and is_production()
    ev = DepartureEvent.objects.get()
    assert ev.authorization.code == code and ev.authorization.mode == "demo"
    # Production is now in effect: a new authorization on the synthetic policy is refused.
    with pytest.raises(Conflict, match="POLICY_NOT_APPROVED_PROCEDURE"):
        authorize(w)
    assert DepartureAuthorization.objects.count() == 1


def test_mutation_without_shared_lock_depart_commits_demo_after_production(w, monkeypatch):
    """Shared lock off: the departure has decided (demo) and inserted its event; the switch is NOT blocked and
    commits; then the demo departure commits after production mode was already in effect."""
    code = authorize(w)["code"]
    no_shared_settings_lock(monkeypatch)
    waited, returned, first, second = AdvisoryInterleaving("before_commit:readiness.record_departure").run(
        lambda: depart(w, code), switch_to_production
    )
    assert not waited and returned, (
        "without the shared lock the switch commits while the departure is in flight"
    )
    assert second == "switched" and isinstance(first, dict) and first["mode"] == "demo"
    assert is_production() and DepartureEvent.objects.filter(authorization__code=code).exists()


def test_mutation_without_shared_lock_depart_ignores_pending_switch(w, monkeypatch):
    """Shared lock off, other order: the departure does not wait for the pending switch and records a demo
    departure on the old setting."""
    code = authorize(w)["code"]
    no_shared_settings_lock(monkeypatch)
    waited, returned, first, second = AdvisoryInterleaving(SWITCHED).run(
        switch_to_production, lambda: depart(w, code)
    )
    assert not waited and returned and first == "switched"
    assert isinstance(second, dict) and second["mode"] == "demo", second


# ------------------------------------------------------------------ authorize_departure
def test_switch_first_then_authorize_waits_and_is_refused(w):
    waited, returned, first, second = AdvisoryInterleaving(SWITCHED).run(
        switch_to_production, lambda: authorize(w)
    )
    assert waited and not returned, "authorize must wait for the uncommitted settings change"
    assert first == "switched" and is_production()
    assert isinstance(second, Conflict) and "POLICY_NOT_APPROVED_PROCEDURE" in str(second), second
    assert not DepartureAuthorization.objects.exists()


@pytest.mark.parametrize(
    "pause", ["locked:readiness.authorize_departure", "before_commit:readiness.authorize_departure"]
)
def test_authorize_first_then_switch_waits_authorization_is_demo_and_unusable(w, pause):
    waited, returned, first, second = AdvisoryInterleaving(pause).run(
        lambda: authorize(w), switch_to_production
    )
    assert waited and not returned, "the settings change must wait until the authorization commits"
    assert isinstance(first, dict) and first["mode"] == "demo", first
    assert second == "switched" and is_production()
    with pytest.raises(Conflict, match="POLICY_NOT_APPROVED_PROCEDURE"):
        authorize(w)
    with pytest.raises(Conflict, match="demo authorization can never be used for departure in production"):
        depart(w, first["code"])
    assert DepartureAuthorization.objects.count() == 1 and not DepartureEvent.objects.exists()


def test_mutation_without_shared_lock_demo_authorization_commits_after_production(w, monkeypatch):
    no_shared_settings_lock(monkeypatch)
    waited, returned, first, second = AdvisoryInterleaving("before_commit:readiness.authorize_departure").run(
        lambda: authorize(w), switch_to_production
    )
    assert not waited and returned, "without the shared lock the switch commits while authorize is in flight"
    assert second == "switched" and isinstance(first, dict) and first["mode"] == "demo"
    assert is_production() and DepartureAuthorization.objects.filter(mode="demo").exists()


def test_mutation_without_shared_lock_authorize_ignores_pending_switch(w, monkeypatch):
    no_shared_settings_lock(monkeypatch)
    waited, returned, first, second = AdvisoryInterleaving(SWITCHED).run(
        switch_to_production, lambda: authorize(w)
    )
    assert not waited and returned and first == "switched"
    assert isinstance(second, dict) and second["mode"] == "demo", second


# ------------------------------------------------------------------ demo authorization in production, no race
def test_demo_authorization_refused_in_production_by_the_explicit_mode_check(w):
    """No race: a demo-mode authorization presented after production is in effect is refused by
    record_departure's own mode check, before (and independent of) the evaluator's policy gate."""
    code = authorize(w)["code"]
    set_setting("demo_mode", "false")
    with pytest.raises(Conflict, match="issued in demo mode"):
        depart(w, code)
    assert not DepartureEvent.objects.exists()


# ------------------------------------------------------------------ lock placement and the database side
LOCK_RE = re.compile(r"FROM (\w+) WHERE .* FOR (UPDATE|SHARE)", re.S)


def _capture(fn):
    seen: list[str] = []

    def wrapper(execute, sql, params, many, context):
        if "pg_advisory_xact_lock_shared" in sql:
            seen.append("settings")
        else:
            m = LOCK_RE.search(sql)
            if m:
                seen.append(m.group(1))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        fn()
    return seen


def test_settings_lock_is_taken_once_right_after_role_grants(w):
    seen = _capture(lambda: authorize(w))
    assert seen[:3] == ["accounts_rolegrant", "settings", "planning_site"], seen
    assert seen.count("settings") == 1
    code = DepartureAuthorization.objects.get().code
    seen = _capture(lambda: depart(w, code))
    assert seen[:3] == ["accounts_rolegrant", "settings", "planning_site"], seen
    assert seen.count("settings") == 1


def test_revocation_does_not_take_the_settings_lock(w):
    code = authorize(w)["code"]
    seen = _capture(lambda: rc.revoke_authorization(w.ops, authorization_code=code, reason="x"))
    assert "settings" not in seen, seen


def test_installed_trigger_uses_the_same_key():
    with connection.cursor() as cur:
        cur.execute("SELECT pg_get_functiondef('fleet_setting_change_lock'::regproc)")
        body = cur.fetchone()[0]
        cur.execute(
            "SELECT tgtype FROM pg_trigger WHERE tgname = 'fleet_deploymentsetting_change_lock' "
            "AND tgrelid = 'fleet_deploymentsetting'::regclass"
        )
        assert cur.fetchone() is not None
    assert f"pg_advisory_xact_lock({locks.SETTINGS_LOCK_KEY})" in body


def test_owner_write_holds_exclusive_lock_and_runtime_role_can_take_shared(app_role_conn):
    """The runtime role has SELECT only on settings yet can take the shared advisory lock; while the owner's
    settings write is uncommitted it cannot."""
    owner = psycopg.connect(
        host=connection.settings_dict["HOST"],
        port=connection.settings_dict["PORT"],
        dbname=connection.settings_dict["NAME"],
        user=connection.settings_dict["USER"],
        password=connection.settings_dict["PASSWORD"],
    )
    try:
        with app_role_conn.transaction(), app_role_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock_shared(%s)", [locks.SETTINGS_LOCK_KEY])
            assert cur.fetchone()[0] is True
        with owner.cursor() as oc:  # non-autocommit: transaction stays open
            oc.execute("UPDATE fleet_deploymentsetting SET value = value WHERE key = 'demo_mode'")
            with app_role_conn.transaction(), app_role_conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock_shared(%s)", [locks.SETTINGS_LOCK_KEY])
                assert cur.fetchone()[0] is False, "an uncommitted settings write must exclude readers"
        owner.rollback()
        with app_role_conn.transaction(), app_role_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock_shared(%s)", [locks.SETTINGS_LOCK_KEY])
            assert cur.fetchone()[0] is True
    finally:
        owner.close()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE fleet_deploymentsetting SET value = 'false' WHERE key = 'demo_mode'",
        "INSERT INTO fleet_deploymentsetting (key, value) VALUES ('rsr_new', 'x')",
        "DELETE FROM fleet_deploymentsetting WHERE key = 'demo_mode'",
        "TRUNCATE fleet_deploymentsetting",
    ],
)
def test_runtime_role_cannot_modify_settings_so_cannot_bypass_the_trigger(app_role_conn, sql):
    set_setting("demo_mode", "true")
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute(sql)
    assert DeploymentSetting.objects.get(key="demo_mode").value == "true"
    assert not DeploymentSetting.objects.filter(key="rsr_new").exists()
