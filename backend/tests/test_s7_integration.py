"""Slice 7 integration (lead): deterministic evidence time source (release blocker from random world 29).

Root cause (readiness/migrations/0008_s7_evidence_time_source.py): an evidence row inserted with observed_at =
DEFAULT (statement_timestamp()) was refused as "in the future" because the guard compared it with a second, later
wall-clock read (clock_timestamp()); a backward step of the database clock between the two made the row look
future-dated. These tests make that step happen on purpose: a session-local public.clock_timestamp() that reads
1 us before the calling statement's start (search_path puts public before pg_catalog for this test transaction
only, so every unqualified clock_timestamp() in the triggers and the commands reads the stepped-back clock, while
the column default and statement_timestamp() keep the real one). SYNTHETIC data only.
"""

import copy
import importlib
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from psycopg.types.json import Jsonb

from readiness import evidence as rx
from readiness.models import ReadinessEvidence
from tests.planning_world import DAY
from tests.readiness_world import EQUIPMENT_ALL_PRESENT, build_readiness, record_inspection

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    return build_readiness(prefix="i7_")


def _step_clock_back() -> None:
    """From now on in this test transaction, every unqualified clock_timestamp() reads 1 us BEFORE the start of
    the statement that calls it: the smallest backward step between a statement's start and its trigger. Role
    grants and qualifications made while building the world (earlier statements) stay valid."""
    with connection.cursor() as cur:
        cur.execute(
            "CREATE FUNCTION public.clock_timestamp() RETURNS timestamptz LANGUAGE sql VOLATILE AS "
            "$$ SELECT least(pg_catalog.clock_timestamp(), "
            "pg_catalog.statement_timestamp() - interval '1 microsecond') $$"
        )
        cur.execute("SET LOCAL search_path = public, pg_catalog")
        cur.execute("SELECT clock_timestamp() < statement_timestamp()")
        assert cur.fetchone()[0] is True  # the stepped-back clock is in effect


def _equipment_check(w: Any) -> dict[str, Any]:
    return rx.record_evidence(
        w.field,
        visit_code=w.visit,
        kind="equipment_check",
        result="pass",
        details=copy.deepcopy(EQUIPMENT_ALL_PRESENT),
    )


def _pre_fix_guard() -> None:
    """Mutation: the readiness/0007 guard body (the reverse of 0008), inside this test transaction only."""
    mig = importlib.import_module("readiness.migrations.0008_s7_evidence_time_source")
    with connection.cursor() as cur:
        cur.execute(mig.REVERSE_SQL)


def test_defaulted_observed_at_is_accepted_after_a_backward_clock_step(w):
    _step_clock_back()
    out = _equipment_check(w)
    ev = ReadinessEvidence.objects.get(code=out["entity_id"])
    with connection.cursor() as cur:
        cur.execute("SELECT pg_catalog.clock_timestamp()")
        real_now = cur.fetchone()[0]
    assert ev.observed_at <= real_now  # the database's own statement time, never a host value


def test_pre_fix_guard_reproduces_the_random_world_failure(w):
    """Same command, same stepped clock, the 0007 guard body: refused exactly as in random world 29."""
    _step_clock_back()
    _pre_fix_guard()
    with pytest.raises(Exception, match="observed_at cannot be in the future"):
        _equipment_check(w)


def test_inspection_default_is_stamped_in_the_insert(w):
    visit = w.new_confirmed_visit(d=DAY + timedelta(days=1))
    insp = record_inspection(w, visit)
    _step_clock_back()
    out = rx.record_evidence(
        w.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    ev = ReadinessEvidence.objects.select_related("inspection").get(code=out["entity_id"])
    assert ev.observed_at == ev.inspection.submitted_at  # LEAST(submitted_at, statement_timestamp())


def test_caller_supplied_future_time_is_still_refused(w):
    with connection.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        now = cur.fetchone()[0]
    with pytest.raises(Exception, match="observed_at cannot be in the future"):
        rx.record_evidence(
            w.field,
            visit_code=w.visit,
            kind="equipment_check",
            result="pass",
            details=copy.deepcopy(EQUIPMENT_ALL_PRESENT),
            observed_at=now + timedelta(minutes=1),
        )


@pytest.mark.parametrize("offset_us,accepted", [(-1, True), (0, True), (1, False)])
def test_guard_boundary_is_the_statement_time_when_the_clock_is_behind(w, offset_us, accepted):
    """Direct SQL (as the schema owner, inside the test transaction): with the clock stepped back, the bound is
    exactly statement_timestamp(): -1 us and = accepted, +1 us refused ("future")."""
    ev = ReadinessEvidence.objects.get(code=w.evidence["equipment_check"])
    _step_clock_back()
    sql = (
        "INSERT INTO readiness_readinessevidence (code, visit_id, revision_id, robot_id, manifest_id, kind, "
        "result, details, observed_at, recorded_at, recorded_by_id) VALUES (%s, %s, %s, %s, %s, "
        "'equipment_check', 'pass', %s, statement_timestamp() + %s * interval '1 microsecond', now(), %s)"
    )
    params = [
        f"EV-I7-{offset_us + 1}",
        ev.visit_id,
        ev.revision_id,
        ev.robot_id,
        ev.manifest_id,
        Jsonb(EQUIPMENT_ALL_PRESENT),
        offset_us,
        ev.recorded_by_id,
    ]
    with connection.cursor() as cur:
        if accepted:
            cur.execute(sql, params)
        else:
            cur.execute("SAVEPOINT b")
            with pytest.raises(Exception, match="observed_at cannot be in the future"):
                cur.execute(sql, params)
            cur.execute("ROLLBACK TO SAVEPOINT b")
