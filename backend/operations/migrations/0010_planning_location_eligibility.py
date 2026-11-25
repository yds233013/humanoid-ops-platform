"""v2 Phase 03A-2 corrective (docs/13_v2_phase03_contract.md section 12): planning eligibility uses the
FUTURE-PLANNING location predicate.

``operations_robot_eligibility``: the operations/0009 body, same columns, order and types. The location reason is
now ``planning_problem`` of fleet_planning_location (fleet/0017) instead of the physical-dispatch reason: a robot
away on an open run whose reserved window has not ended has NO location reason (it may be planned outside that
window, At risk, for its run's hub; the planner, options and triggers check hub and window); an overdue or
indeterminate return ("Away on run EX: ..."), a transfer, unaccounted, unknown or an unverified hub keep a reason.
Computed once per robot and statement (MATERIALIZED plocs), as in 0009. Reverse restores 0009's body.
"""

import importlib

from django.conf import settings
from django.db import migrations

_OPS9 = importlib.import_module("operations.migrations.0009_location_eligibility")


def _once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


VIEW_SQL = _once(
    _once(
        _OPS9.VIEW_SQL,
        "  SELECT r.id AS robot_id, l.kind, l.hub_id, l.ref, l.verified\n"
        "  FROM fleet_robot r CROSS JOIN LATERAL fleet_robot_location(r.id) l\n",
        "  SELECT r.id AS robot_id, l.kind, l.hub_id, l.ref, l.verified, l.planning_problem\n"
        "  FROM fleet_robot r CROSS JOIN LATERAL fleet_planning_location(r.id) l\n",
    ),
    "(SELECT fleet_location_problem(loc.kind, loc.ref, loc.verified)\n",
    "(SELECT loc.planning_problem\n",
)


def _grants() -> str:
    app = settings.DB_APP_ROLE
    v = "operations_robot_eligibility"
    return f"REVOKE ALL ON {v} FROM PUBLIC;\nREVOKE ALL ON {v} FROM {app};\nGRANT SELECT ON {v} TO {app};\n"


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0009_location_eligibility"),
        ("fleet", "0017_planning_location_predicate"),
    ]

    operations = [
        migrations.RunSQL(VIEW_SQL + "\n" + _grants(), reverse_sql=_OPS9.VIEW_SQL + "\n" + _grants())
    ]
