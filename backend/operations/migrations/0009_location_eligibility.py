"""v2 Phase 03A-2 (docs/13_v2_phase03_contract.md 11.4, correction 10.A): location in planning eligibility.

``operations_robot_eligibility``: the operations/0007 body, same columns, order and types, with ONE addition at the
end of the reasons (after the robot holds; fleet.services.evaluate_eligibility appends it at the same position,
same wording): the robot's location when it is not a verified hub (fleet_location_problem over
fleet_robot_location, fleet/0015):

    In transfer TR-0001 from HUB-1 to HB          Not at a hub: in vehicle VAN-1
    Not at a hub: at site S-1                     Not at a hub: unaccounted for
    Location unknown                              At hub HUB-1, not verified

So a robot in transit, in a vehicle, at a site, unaccounted or unknown is never eligible, at either hub. WHICH hub
a verified robot may be dispatched from (= the visit's hub) is visit-specific: the planner (planning.services),
the options list (planning.options) and the planning triggers (planning/0011) check it.

Still security_invoker, STABLE read-only functions only, ops_app SELECT only. Reverse restores 0007's body.
"""

import importlib

from django.conf import settings
from django.db import migrations

_OPS7 = importlib.import_module("operations.migrations.0007_maintenance_gate")


def _once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


_RS = ") ap ON p.id IS NOT NULL\nCROSS JOIN LATERAL (\n  SELECT\n"
_END = "             ORDER BY h.id)\n    AS reasons\n) rs;\n"

_MODE = "WITH mode AS MATERIALIZED (SELECT fleet_production_mode() AS production),\n"
_PLOCS = (
    "-- v2 03A-2: every robot's location, computed ONCE per statement (MATERIALIZED, like pstat), so a query\n"
    "-- joining or filtering this view never re-runs the per-robot function\n"
    "plocs AS MATERIALIZED (\n"
    "  SELECT r.id AS robot_id, l.kind, l.hub_id, l.ref, l.verified\n"
    "  FROM fleet_robot r CROSS JOIN LATERAL fleet_robot_location(r.id) l\n"
    "),\n"
)

VIEW_SQL = _once(
    _once(
        _once(_OPS7.ELIGIBILITY_SQL, _MODE, _MODE + _PLOCS),
        _RS,
        ") ap ON p.id IS NOT NULL\n"
        "-- v2 03A-2: the robot's location (once per robot and statement, see plocs)\n"
        "LEFT JOIN plocs loc ON loc.robot_id = r.id\n"
        "CROSS JOIN LATERAL (\n  SELECT\n",
    ),
    _END,
    "             ORDER BY h.id)\n"
    "    -- v2 03A-2 (10.A): only a verified hub location can make a robot available\n"
    "    || coalesce((SELECT ARRAY[x.problem] FROM (SELECT fleet_location_problem(loc.kind, loc.ref, loc.verified)\n"
    "                 AS problem) x WHERE x.problem IS NOT NULL), '{}'::text[])\n"
    "    AS reasons\n) rs;\n",
)


def _grants() -> str:
    app = settings.DB_APP_ROLE
    v = "operations_robot_eligibility"
    return f"REVOKE ALL ON {v} FROM PUBLIC;\nREVOKE ALL ON {v} FROM {app};\nGRANT SELECT ON {v} TO {app};\n"


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0008_predictor_log"),
        ("fleet", "0015_location_and_transfer_sql"),
    ]

    operations = [
        migrations.RunSQL(VIEW_SQL + "\n" + _grants(), reverse_sql=_OPS7.ELIGIBILITY_SQL + "\n" + _grants())
    ]
