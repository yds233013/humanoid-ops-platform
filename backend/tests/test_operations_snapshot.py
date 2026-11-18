"""Slice 5 D5: the command-center snapshot is the first statement of a real REPEATABLE READ READ ONLY
transaction, refuses to nest, cannot write, takes no locks, and never blocks a writer."""

import threading

import pytest
from django.db import connection, transaction
from django.db.utils import InternalError, OperationalError, ProgrammingError

from fleet.models import DeploymentSetting
from operations.snapshot import SET_SNAPSHOT, SnapshotNotOutermost, read_snapshot

pytestmark = pytest.mark.django_db(transaction=True)


def test_isolation_statement_is_first_and_snapshot_is_read_only_repeatable_read():
    seen: list[str] = []

    def wrapper(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper), read_snapshot() as at:
        with connection.cursor() as cur:
            cur.execute("SHOW transaction_isolation")
            iso = cur.fetchone()[0]
            cur.execute("SHOW transaction_read_only")
            ro = cur.fetchone()[0]
    assert seen[0] == SET_SNAPSHOT
    assert iso == "repeatable read" and ro == "on" and at is not None


def test_snapshot_refuses_to_nest_inside_an_existing_transaction():
    with transaction.atomic(), pytest.raises(SnapshotNotOutermost), read_snapshot():
        pass


def test_writes_are_refused_inside_the_snapshot():
    with (
        pytest.raises((InternalError, OperationalError, ProgrammingError), match="read-only"),
        read_snapshot(),
        connection.cursor() as cur,
    ):
        cur.execute("CREATE TEMP TABLE should_not_exist (x int)")


def test_snapshot_is_consistent_and_never_blocks_a_writer():
    """A writer commits while a snapshot is open: it is not blocked, and the open snapshot keeps its view."""
    done = threading.Event()

    def writer():
        from django.db import connections

        with transaction.atomic():
            DeploymentSetting.objects.create(key="ops_snapshot_probe", value="1")
        connections.close_all()
        done.set()

    with read_snapshot(), connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM fleet_deploymentsetting")
        before = cur.fetchone()[0]
        t = threading.Thread(target=writer)
        t.start()
        assert done.wait(10), "writer was blocked by the open snapshot"
        t.join(5)
        cur.execute("SELECT count(*) FROM fleet_deploymentsetting")
        assert cur.fetchone()[0] == before  # same snapshot
    assert DeploymentSetting.objects.filter(key="ops_snapshot_probe").exists()
