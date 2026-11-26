"""Custody read helpers (Slice 4, integration lead).

The single definition of a robot's custody lives in SQL (execution migration 0002:
`execution_current_custody`, `execution_open_run_id`, `execution_robot_departure_problem`). Python calls it,
so the evaluator, the commands and the database can never disagree (contract D1, C3)."""

import re
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


# v2 03D (contract 15.7, EM): an unresolved operator interruption from an earlier run gets its own readiness
# and departure code instead of the misleading ROBOT_NOT_AT_HUB. The database gate is unchanged: this only
# reads the refusal text execution_robot_departure_problem returns for that case (execution/0011), so no
# extra statement runs and the evaluator, the pages and the gate keep reading the one SQL function.
ROBOT_INTERRUPTION_UNRESOLVED = "ROBOT_INTERRUPTION_UNRESOLVED"
_UNRESOLVED_RE = re.compile(
    r"^robot (\S+) has an unresolved interruption \((.+)\); it must be resolved first$"
)


def interruption_text(robot_code: str) -> str:
    return (
        f"Robot {robot_code} has an unresolved interruption from an earlier run. "
        "Resolve it before requesting departure authorization."
    )


@dataclass(frozen=True)
class UnresolvedInterruption:
    robot_code: str
    where: str  # "IN-0001 on run EX-0001[, ...]", for traceability (names first, codes second)

    @property
    def message(self) -> str:
        return interruption_text(self.robot_code)

    @property
    def action(self) -> str:
        return f"Ask an ops lead to review and resolve the interruption ({self.where})"


def unresolved_interruption(problem: str | None) -> UnresolvedInterruption | None:
    """The 15.7 case of a departure problem, else None (every other problem keeps its own code)."""
    m = _UNRESOLVED_RE.match(problem or "")
    return UnresolvedInterruption(m.group(1), m.group(2)) if m else None
