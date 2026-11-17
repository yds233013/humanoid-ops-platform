"""Deterministic planning interleavings on real Postgres (pause hook after the first command's locks)."""

import threading

import pytest
from django.db import connection

from fleet import services as fs
from planning import services as p
from planning.models import ConsentRecord, Reservation, Visit
from platform_core import testhooks
from platform_core.commands import Conflict
from tests.fleet_world import build_world  # noqa: F401  (imported for fixture parity)
from tests.planning_world import build_planning, new_visit, plan
from tests.test_fleet_concurrency import Interleaving

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def w():
    world = build_planning(prefix="pc_")
    yield world
    testhooks.clear()


def overlaps(resource_kind: str) -> int:
    """Count pairs of ACTIVE reservations on the same resource that overlap - must always be zero."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM planning_reservation a JOIN planning_reservation b "
            "ON a.resource_id = b.resource_id "
            "AND a.id < b.id JOIN planning_resource r ON r.id = a.resource_id WHERE r.kind = %s "
            "AND a.released_at IS NULL AND b.released_at IS NULL "
            "AND tstzrange(a.starts_at, a.ends_at, '[)') && tstzrange(b.starts_at, b.ends_at, '[)')",
            [resource_kind],
        )
        return cur.fetchone()[0]


def test_two_planners_same_robot_same_time(w):
    v1, v2 = new_visit(w), new_visit(w)
    it = Interleaving("locked:planning.plan_visit")
    blocked, first, second = it.run(lambda: plan(w, v1), lambda: plan(w, v2, lead="P-3", vehicle="VAN-2"))
    assert blocked, "the second planner must wait for the first one's robot lock"
    assert first["feasibility"] == "feasible" and second["feasibility"] == "blocked"
    assert any(v1 in m for m in second["blockers"])
    assert overlaps("robot") == 0 and overlaps("person") == 0


def test_database_alone_prevents_double_booking_when_app_locks_are_bypassed(w, monkeypatch):
    """Lock ordering is not the only defence: with the application locks disabled, two simultaneous plans
    for the same robot still cannot both commit overlapping reservations."""
    monkeypatch.setattr(p, "lock_guards", lambda **kw: None)
    v1, v2 = new_visit(w), new_visit(w)
    barrier, results = threading.Barrier(2), []

    def go(code, lead, van):
        try:
            barrier.wait()
            results.append(plan(w, code, lead=lead, vehicle=van))
        except Conflict as e:
            results.append(e)
        finally:
            connection.close()

    t1 = threading.Thread(target=go, args=(v1, "P-1", "VAN-1"))
    t2 = threading.Thread(target=go, args=(v2, "P-3", "VAN-2"))
    t1.start(), t2.start(), t1.join(30), t2.join(30)
    assert overlaps("robot") == 0
    wins = [r for r in results if isinstance(r, dict) and r["feasibility"] == "feasible"]
    assert len(wins) == 1, results


def test_concurrent_reschedules_into_the_same_slot(w):
    from datetime import timedelta

    from tests.planning_world import DAY, at

    v1, v2 = new_visit(w), new_visit(w, d=DAY + timedelta(days=1), h0=15, h1=16)
    plan(w, v1)
    plan(w, v2, robot="R-02", lead="P-3", vehicle="VAN-2")
    before_v2 = set(
        Reservation.objects.filter(revision__visit__code=v2, released_at__isnull=True).values_list(
            "pk", flat=True
        )
    )
    it = Interleaving("locked:planning.plan_visit")
    _, first, second = it.run(
        lambda: plan(w, v1, on_site_start=at(DAY, 13), on_site_end=at(DAY, 14)),
        lambda: plan(
            w,
            v2,
            robot="R-02",
            lead="P-3",
            vehicle="VAN-2",
            on_site_start=at(DAY, 13),
            on_site_end=at(DAY, 14),
        ),
    )
    assert isinstance(first, dict) and isinstance(second, Conflict)  # P-2 is now busy at 13:00
    assert (
        set(
            Reservation.objects.filter(revision__visit__code=v2, released_at__isnull=True).values_list(
                "pk", flat=True
            )
        )
        == before_v2
    )  # the loser's plan is untouched
    assert overlaps("person") == 0


def test_hold_first_then_confirm_is_refused(w):
    v = new_visit(w)
    plan(w, v)
    it = Interleaving("locked:fleet.place_hold")
    blocked, first, second = it.run(
        lambda: fs.place_hold(w.ops, scope="robot", target="R-01", source_type="manual", reason="x"),
        lambda: p.confirm_visit(w.ops, visit_code=v),
    )
    assert blocked and isinstance(first, dict) and isinstance(second, Conflict)
    assert Visit.objects.get(code=v).status == "planned"


def test_confirm_first_then_hold_applies_after(w):
    v = new_visit(w)
    plan(w, v)
    it = Interleaving("locked:planning.confirm_visit")
    blocked, first, second = it.run(
        lambda: p.confirm_visit(w.ops, visit_code=v),
        lambda: fs.place_hold(w.ops, scope="robot", target="R-01", source_type="manual", reason="x"),
    )
    assert blocked and first["status"] == "confirmed" and isinstance(second, dict)
    assert p.live_feasibility(Visit.objects.get(code=v)).feasibility == "blocked"  # visible immediately


def test_consent_withdrawal_first_then_confirm_is_refused(w):
    v = new_visit(w)
    plan(w, v)
    c = ConsentRecord.objects.get(site__code="S-1", participant_ref="P1")
    it = Interleaving("locked:planning.withdraw_consent")
    blocked, first, second = it.run(
        lambda: p.withdraw_consent(w.field, consent_id=c.pk, reason="withdrawn"),
        lambda: p.confirm_visit(w.ops, visit_code=v),
    )
    assert blocked and isinstance(first, dict) and isinstance(second, Conflict)
    assert "consent" in str(second)
    assert Visit.objects.get(code=v).status == "planned"


def test_reschedule_between_confirm_read_and_lock_is_detected_and_retried(w):
    """confirm_visit reads revision rN, then a reschedule commits rN+1 with a DIFFERENT robot before confirm
    takes its locks. Confirm must notice, retry with the current revision's locks, and judge rN+1."""
    from tests.planning_world import DAY, at

    v = new_visit(w)
    plan(w, v)
    fs.place_hold(w.ops, scope="robot", target="R-02", source_type="manual", reason="R-02 is held")
    paused, go, out = threading.Event(), threading.Event(), {}
    calls = {"n": 0}

    def hook():
        calls["n"] += 1
        if calls["n"] == 1:  # only the first attempt pauses
            paused.set()
            assert go.wait(20)

    testhooks.register("read:planning.confirm_visit", hook)

    def confirm():
        try:
            out["confirm"] = p.confirm_visit(w.ops, visit_code=v)
        except Exception as e:  # noqa: BLE001
            out["confirm"] = e
        finally:
            connection.close()

    t = threading.Thread(target=confirm)
    t.start()
    assert paused.wait(20)
    # reschedule to the HELD robot R-02 is blocked... so instead switch to R-03 (eligible) at a new time
    out["resched"] = plan(w, v, robot="R-03", on_site_start=at(DAY, 13), on_site_end=at(DAY, 14))
    go.set()
    t.join(30)
    assert calls["n"] == 2, "confirm must have retried after detecting the newer revision"
    assert out["confirm"]["revision"] == out["resched"]["revision"]
    assert Visit.objects.get(code=v).status == "confirmed"
