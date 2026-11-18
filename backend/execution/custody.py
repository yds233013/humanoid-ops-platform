"""Custody read helpers (Slice 4, integration lead).

The single definition of a robot's custody lives in SQL (execution migration 0002:
`execution_current_custody`, `execution_open_run_id`, `execution_robot_departure_problem`). Python calls it,
so the evaluator, the commands and the database can never disagree (contract D1, C3)."""

from dataclasses import dataclass

from django.db import connection


@dataclass(frozen=True)
class Custody:
    kind: str  # hub | vehicle | site | unaccounted | unknown
    verified: bool
    custody_event_id: int | None

    @property
    def label(self) -> str:
        return self.kind if self.verified else f"{self.kind} (unverified)"


def current_custody(robot_id: int) -> Custody:
    with connection.cursor() as cur:
        cur.execute("SELECT kind, verified, custody_event_id FROM execution_current_custody(%s)", [robot_id])
        row = cur.fetchone()
    if row is None:
        return Custody("unknown", False, None)
    return Custody(row[0], bool(row[1]), row[2])


def open_run_id(robot_id: int) -> int | None:
    with connection.cursor() as cur:
        cur.execute("SELECT execution_open_run_id(%s)", [robot_id])
        row = cur.fetchone()
    return row[0] if row else None


def departure_problem(robot_id: int) -> str | None:
    """None if the robot may be authorized/depart (baseline or verified hub, no open run); else why not."""
    with connection.cursor() as cur:
        cur.execute("SELECT execution_robot_departure_problem(%s)", [robot_id])
        row = cur.fetchone()
    return row[0] if row else None
