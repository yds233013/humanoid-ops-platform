"""Deterministic interleavings for the Slice 4 logistics commands (Engineer B), on real Postgres, in BOTH orders.

The first command is paused at a named checkpoint (right after its locks, or before its commit, or - to prove
the re-read - right after its first read, before any lock); the second is started and must be blocked (or, in
the read-pause case, must finish); then the first is released and both outcomes are checked. Races covered
(contract section 7.2): two custody moves on one robot, crew verification vs a move, custody transfer vs
reconcile, reconcile vs close_trip, demo -> production switch vs record_site_check (synthetic template).
SYNTHETIC data only."""

import pytest

from execution import logistics as lg
from execution import reconciliation as rc
from execution import site_checks as sc
from execution.custody import current_custody
from execution.models import CustodyEvent, ReturnReconciliation, SiteCheck, TripClosure
from platform_core import testhooks
from platform_core.commands import Conflict
from tests import execution_world as ew
from tests.readiness_world import record_inspection
from tests.test_fleet_concurrency import Interleaving
from tests.test_readiness_settings_race import (
    SWITCHED,
    AdvisoryInterleaving,
    is_production,
    no_shared_settings_lock,
    switch_to_production,
)

pytestmark = pytest.mark.django_db(transaction=True)

SYNTHETIC_NO_GO = "a synthetic_demo template cannot produce go outside demo mode"


@pytest.fixture
def w():
    world = ew.build_execution(prefix="xlc_")
    yield world
    testhooks.clear()


def verify(w):
    return lg.verify_custody(w.field, run_code=w.run.code, custody_seq=1, verifier_code="P-2")


def trip(w, kind):
    return lg.record_trip_event(w.field, run_code=w.run.code, kind=kind)


def move(w, to, custodian="P-1", receiver="P-2", by=None):
    return lg.transfer_custody(
        by or w.field, run_code=w.run.code, to_kind=to, custodian_code=custodian, received_by_code=receiver
    )


def reconcile(w, inspection=None, discrepancies=None):
    return rc.reconcile_return(
        w.ops, run_code=w.run.code, inspection_code=inspection, discrepancies=discrepancies
    )


def close(w):
    return rc.close_trip(w.ops, run_code=w.run.code)


def check(w):
    return sc.record_site_check(
        w.field,
        run_code=w.run.code,
        template_version="SC1",
        adults_present=2,
        conditions=dict(ew.ALL_OK),
        verifier_code="P-1",
    )


def seqs(w):
    return list(CustodyEvent.objects.filter(robot=w.robot).order_by("seq").values_list("seq", "to_kind"))


def at_vehicle_back_at_hub(w):
    verify(w)
    trip(w, "arrived_site")
    move(w, "site")
    move(w, "vehicle")
    trip(w, "departed_site")
    trip(w, "arrived_hub")


# ================================================================== two custody moves on one robot
@pytest.mark.parametrize("order", [("P-1", "P-2"), ("P-2", "P-1")])
def test_two_concurrent_moves_one_wins_the_other_is_explained(w, order):
    verify(w)
    trip(w, "arrived_site")
    a, b = order
    blocked, first, second = Interleaving("locked:execution.transfer_custody").run(
        lambda: move(w, "site", custodian=a, receiver=b), lambda: move(w, "site", custodian=b, receiver=a)
    )
    assert blocked, "the second move must wait for the robot lock"
    assert isinstance(first, dict) and first["custodian"] == a, first
    assert isinstance(second, Conflict) and "already at the site" in str(second), second
    assert seqs(w) == [(1, "vehicle"), (2, "site")]


def test_a_move_paused_before_its_locks_rereads_custody_under_them(w):
    """First reads the run and pauses BEFORE locking; the second move commits meanwhile. The first then locks,
    reads the CURRENT custody and is refused (it never acts on what it saw before the lock)."""
    verify(w)
    trip(w, "arrived_site")
    blocked, first, second = Interleaving("read:execution.transfer_custody").run(
        lambda: move(w, "site"), lambda: move(w, "site", custodian="P-2", receiver="P-1")
    )
    assert not blocked, "nothing is locked yet: the second move must not wait"
    assert isinstance(second, dict) and second["custodian"] == "P-2", second
    assert isinstance(first, Conflict) and "already at the site" in str(first), first
    assert seqs(w) == [(1, "vehicle"), (2, "site")]


def test_verification_first_then_move_succeeds(w):
    trip(w, "arrived_site")
    blocked, first, second = Interleaving("locked:execution.verify_custody").run(
        lambda: verify(w), lambda: move(w, "site")
    )
    assert blocked
    assert isinstance(first, dict) and first["custody"]["verified"], first
    assert isinstance(second, dict) and second["to"] == "site", second


def test_move_first_then_verification_move_refused_unverified(w):
    trip(w, "arrived_site")
    blocked, first, second = Interleaving("locked:execution.transfer_custody").run(
        lambda: move(w, "site"), lambda: verify(w)
    )
    assert blocked
    assert isinstance(first, Conflict) and "not verified" in str(first), first
    assert isinstance(second, dict), second
    assert seqs(w) == [(1, "vehicle")] and current_custody(w.robot.pk).verified


# ================================================================== custody transfer vs reconcile
def test_hub_receipt_first_then_reconcile_sees_it(w):
    at_vehicle_back_at_hub(w)
    early = record_inspection(w, w.visit, robot=w.robot.code)  # before the receipt: cannot reconcile
    blocked, first, second = Interleaving("locked:execution.transfer_custody").run(
        lambda: move(w, "hub", receiver="P-3"), lambda: reconcile(w, inspection=early)
    )
    assert blocked, "reconcile must wait for the robot lock held by the custody move"
    assert isinstance(first, dict) and first["to"] == "hub", first
    assert isinstance(second, dict) and second["hub_receipt_seq"] == first["seq"], second
    assert second["outcome"] == "unresolved"
    assert second["problems"] == [f"inspection {early} was not submitted after the hub receipt"]


def test_reconcile_first_then_hub_receipt_reconcile_saw_robot_not_back(w):
    at_vehicle_back_at_hub(w)
    early = record_inspection(w, w.visit, robot=w.robot.code)
    blocked, first, second = Interleaving("locked:execution.reconcile_return").run(
        lambda: reconcile(w, inspection=early), lambda: move(w, "hub", receiver="P-3")
    )
    assert blocked, "the custody move must wait for the reconciliation's robot lock"
    assert isinstance(first, dict) and first["outcome"] == "unresolved", first
    assert first["hub_receipt_seq"] is None
    assert "the robot has not been received at the hub (it is in the vehicle)" in first["problems"]
    assert isinstance(second, dict) and second["to"] == "hub", second
    rec = ReturnReconciliation.objects.get()
    assert rec.hub_receipt_id is None and rec.outcome == "unresolved"


# ================================================================== reconcile vs close_trip
def _reconciled(w):
    at_vehicle_back_at_hub(w)
    move(w, "hub", receiver="P-3")
    insp = record_inspection(w, w.visit, robot=w.robot.code)
    good = reconcile(w, inspection=insp)
    assert good["outcome"] == "reconciled"
    return good


@pytest.mark.parametrize(
    "pause", ["locked:execution.reconcile_return", "before_commit:execution.reconcile_return"]
)
def test_reconcile_first_then_close_sees_the_newer_unresolved_row(w, pause):
    _reconciled(w)
    blocked, first, second = Interleaving(pause).run(
        lambda: reconcile(w, discrepancies=["SYNTHETIC: second look"]), lambda: close(w)
    )
    assert blocked, "close must wait for the run lock"
    assert isinstance(first, dict) and first["outcome"] == "unresolved", first
    assert isinstance(second, Conflict) and "latest reconciliation is unresolved" in str(second), second
    assert not TripClosure.objects.exists()


@pytest.mark.parametrize("pause", ["locked:execution.close_trip", "before_commit:execution.close_trip"])
def test_close_first_then_reconcile_is_refused(w, pause):
    good = _reconciled(w)
    blocked, first, second = Interleaving(pause).run(
        lambda: close(w), lambda: reconcile(w, discrepancies=["SYNTHETIC: late"])
    )
    assert blocked, "reconcile must wait for the closure's locks"
    assert isinstance(first, dict) and first["reconciliation"] == good["id"], first
    assert isinstance(second, Conflict) and "is closed" in str(second), second
    assert ReturnReconciliation.objects.count() == 1


# ================================================================== demo -> production vs record_site_check
def _at_site(w):
    verify(w)
    trip(w, "arrived_site")
    move(w, "site")


def test_switch_first_then_site_check_waits_and_is_no_go(w):
    _at_site(w)
    waited, returned, first, second = AdvisoryInterleaving(SWITCHED).run(
        switch_to_production, lambda: check(w)
    )
    assert waited and not returned, "the site check must wait for the uncommitted settings change"
    assert first == "switched" and is_production()
    assert isinstance(second, dict) and second["result"] == "no_go" and second["mode"] == "production", second
    assert second["reasons"] == [SYNTHETIC_NO_GO]


@pytest.mark.parametrize(
    "pause", ["locked:execution.record_site_check", "before_commit:execution.record_site_check"]
)
def test_site_check_first_then_switch_waits_check_is_demo_go(w, pause):
    _at_site(w)
    waited, returned, first, second = AdvisoryInterleaving(pause).run(lambda: check(w), switch_to_production)
    assert waited and not returned, "the settings change must wait until the site check commits"
    assert isinstance(first, dict) and first["result"] == "go" and first["mode"] == "demo", first
    assert second == "switched" and is_production()
    after = check(w)  # production is now in effect for the next check
    assert after["result"] == "no_go" and after["reasons"] == [SYNTHETIC_NO_GO]


def test_mutation_without_shared_lock_go_commits_after_production(w, monkeypatch):
    """Shared lock off: the check decided 'go' in demo and is about to commit; the switch is NOT blocked and
    commits first, so a demo 'go' lands after production was in effect. The lock is what prevents this."""
    _at_site(w)
    no_shared_settings_lock(monkeypatch)
    waited, returned, first, second = AdvisoryInterleaving("before_commit:execution.record_site_check").run(
        lambda: check(w), switch_to_production
    )
    assert not waited and returned
    assert second == "switched" and isinstance(first, dict) and first["result"] == "go", first
    assert is_production() and SiteCheck.objects.get().result == "go"


def test_mutation_without_shared_lock_check_ignores_pending_switch(w, monkeypatch):
    _at_site(w)
    no_shared_settings_lock(monkeypatch)
    waited, returned, first, second = AdvisoryInterleaving(SWITCHED).run(
        switch_to_production, lambda: check(w)
    )
    assert not waited and returned and first == "switched"
    assert isinstance(second, dict) and second["result"] == "go" and second["mode"] == "demo", second
