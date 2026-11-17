"""Planning invariants hold against DIRECT SQL from the runtime role, independent of application locks."""

import threading
from datetime import timedelta

import psycopg
import pytest
from django.db import connection

from planning.models import Reservation, Resource, Visit, VisitRevision
from tests.planning_world import DAY, at, build_planning, new_visit, plan


@pytest.fixture
def w(transactional_db):
    return build_planning(prefix="pdb_")


def _conn():
    import os

    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )


INSERT = (
    "INSERT INTO planning_reservation "
    "(resource_id, revision_id, purpose, starts_at, ends_at, released_reason) "
    "VALUES (%s, %s, 'on_site', %s, %s, '')"
)


def _fixture_rows(w):
    v = new_visit(w)
    plan(w, v)
    rev = Visit.objects.get(code=v).current_revision
    robot_res = Resource.objects.get(kind="robot", robot__code="R-01")
    on_site = Reservation.objects.get(revision=rev, resource=robot_res, purpose="on_site")
    return v, rev, robot_res, on_site


def test_overlapping_reservation_is_rejected_by_the_database(w, app_role_conn):
    v, rev, res, on_site = _fixture_rows(w)
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.errors.ExclusionViolation):
        cur.execute(
            INSERT,
            [
                res.pk,
                rev.pk,
                on_site.starts_at + timedelta(minutes=10),
                on_site.ends_at + timedelta(minutes=10),
            ],
        )


def test_adjacent_and_released_reservations_do_not_conflict(w, app_role_conn):
    v, rev, res, on_site = _fixture_rows(w)
    last_end = Reservation.objects.filter(revision=rev, resource=res).order_by("-ends_at").first().ends_at
    with app_role_conn.cursor() as cur:
        cur.execute(INSERT, [res.pk, rev.pk, last_end, last_end + timedelta(hours=1)])  # [a,b) then [b,c)
        cur.execute(
            "UPDATE planning_reservation SET released_at = now(), released_reason = 'x' WHERE id = %s",
            [on_site.pk],
        )
        cur.execute(INSERT, [res.pk, rev.pk, on_site.starts_at, on_site.ends_at])  # freed slot is reusable


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE planning_reservation SET ends_at = ends_at + interval '1 hour' WHERE id = {id}",
        "DELETE FROM planning_reservation WHERE id = {id}",
    ],
)
def test_reservations_cannot_be_moved_or_deleted(w, app_role_conn, sql):
    _, _, _, on_site = _fixture_rows(w)
    with app_role_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(sql.format(id=on_site.pk))


def test_history_tables_are_append_only(w, app_role_conn):
    v, rev, _, _ = _fixture_rows(w)
    with app_role_conn.cursor() as cur:
        for sql in (
            "UPDATE planning_visitrevision SET feasibility = 'feasible'",
            "DELETE FROM planning_siteassessment",
            "UPDATE planning_consentrecord SET withdrawn_at = NULL",
            "DELETE FROM planning_visit",
            "DELETE FROM planning_site",
        ):
            with pytest.raises(psycopg.Error):
                cur.execute(sql)


def test_visit_status_rules(w, app_role_conn):
    v, rev, _, _ = _fixture_rows(w)
    blocked = new_visit(w, h0=11, h1=12)
    plan(w, blocked, robot="R-02")  # conflicts -> blocked revision, status requested
    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.Error, match="blocked plan"):
            cur.execute("UPDATE planning_visit SET status = 'planned' WHERE code = %s", [blocked])
        with pytest.raises(psycopg.Error, match="release every reservation"):
            cur.execute("UPDATE planning_visit SET status = 'cancelled' WHERE code = %s", [v])
        with pytest.raises(psycopg.Error, match="not allowed"):
            cur.execute("UPDATE planning_visit SET status = 'confirmed' WHERE code = %s", [blocked])


def test_one_robot_per_trip_and_unknown_capacity(w, app_role_conn):
    v, rev, _, _ = _fixture_rows(w)
    trip = rev.trip
    from fleet.models import Robot

    with app_role_conn.cursor() as cur:
        with pytest.raises(psycopg.Error, match="limit is 1"):
            cur.execute(
                "INSERT INTO planning_tripcargo (trip_id, robot_id) VALUES (%s, %s)",
                [trip.pk, Robot.objects.get(code="R-02").pk],
            )
        cur.execute(
            "INSERT INTO planning_vehicletrip (revision_id, vehicle_id, depart_at, return_at) "
            "SELECT %s, id, now(), now() + interval '1 hour' FROM planning_vehicle WHERE code = 'VAN-X' "
            "RETURNING id",
            [VisitRevision.objects.filter(visit__code=v).first().pk],
        )
        unknown_trip = cur.fetchone()[0]
        with pytest.raises(psycopg.Error, match="capacity is unknown"):
            cur.execute(
                "INSERT INTO planning_tripcargo (trip_id, robot_id) VALUES (%s, %s)",
                [unknown_trip, Robot.objects.get(code="R-03").pk],
            )


def test_two_raw_transactions_cannot_double_book(w):
    """No application code, no application locks: the exclusion constraint alone decides."""
    v, rev, res, _ = _fixture_rows(w)
    s0 = at(DAY + timedelta(days=3), 10)
    a, b = _conn(), _conn()
    a.autocommit = b.autocommit = False
    barrier, outcomes = threading.Barrier(2), []

    def book(conn):
        try:
            with conn.cursor() as cur:
                barrier.wait()
                cur.execute(INSERT, [res.pk, rev.pk, s0, s0 + timedelta(hours=1)])
            conn.commit()
            outcomes.append("ok")
        except (psycopg.errors.ExclusionViolation, psycopg.errors.DeadlockDetected):
            # Two simultaneous conflicting inserts each wait to check the other's tuple; Postgres resolves it
            # as either an exclusion violation or a deadlock. Both are refusals; exactly one booking commits.
            # (run_command retries 40P01, after which the loser sees a plain exclusion violation.)
            conn.rollback()
            outcomes.append("rejected")
        except Exception as e:  # noqa: BLE001 - surface anything unexpected in the assertion
            outcomes.append(repr(e))

    t1, t2 = threading.Thread(target=book, args=(a,)), threading.Thread(target=book, args=(b,))
    t1.start(), t2.start(), t1.join(30), t2.join(30)
    a.close(), b.close()
    assert sorted(outcomes) == ["ok", "rejected"]
    assert Reservation.objects.filter(resource=res, starts_at=s0, released_at__isnull=True).count() == 1
