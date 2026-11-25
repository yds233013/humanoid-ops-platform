"""v2 02D predictor log: READ side only (docs/11_v2_phase02_contract.md section 2: "Predictive models:
candidate predictors are logged only"). SYNTHETIC demo data only.

The rows are written by the owner-only SQL function ``operations_log_predictor_signals()`` (operations/0008,
run by the ``log_predictor_signals`` management command). This module only reads them, inside the caller's
snapshot, in ONE statement. OBSERVATIONAL ONLY: no planning, readiness, fleet or hub module imports this
module or reads the table (tests/test_v2_02d_health.py checks the imports), so the signals cannot change any
decision.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection

PAGE_LIMIT = 50
NOT_USED_NOTE = (
    "Observational only; not used for any decision. Candidate signals recorded at each departure, for later "
    "study. They do not predict anything and nothing in planning, readiness or holds reads them."
)

RECENT_SQL = """
SELECT o.id, o.departed_at, o.observed_at, rb.code AS robot, v.code AS visit, o.battery_state,
       o.battery_soc_pct, o.battery_observed_at, o.battery_source, o.battery_reading_id, o.open_holds,
       o.open_incidents, o.maintenance_overdue, o.maintenance_states, o.source_refs, count(*) OVER () AS total
FROM operations_predictor_observation o
JOIN fleet_robot rb ON rb.id = o.robot_id
JOIN planning_visit v ON v.id = o.visit_id
WHERE o.observed_at <= %(at)s
ORDER BY o.departed_at DESC, o.id DESC
LIMIT %(limit)s
"""


@dataclass(frozen=True)
class PredictorLog:
    rows: list[dict[str, Any]]
    total: int
    limit: int
    note: str = NOT_USED_NOTE


def recent_observations(at: datetime, limit: int = PAGE_LIMIT) -> PredictorLog:
    """The latest ``limit`` observations written by ``at`` (newest departure first), and how many exist."""
    with connection.cursor() as cur:
        cur.execute(RECENT_SQL, {"at": at, "limit": limit})
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    total = int(rows[0]["total"]) if rows else 0
    return PredictorLog(rows=rows, total=total, limit=limit)


__all__ = ["NOT_USED_NOTE", "PredictorLog", "recent_observations"]
