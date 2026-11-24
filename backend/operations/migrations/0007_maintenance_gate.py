"""v2 02C (docs/11_v2_phase02_contract.md 3.2, 5, 8.2): maintenance in the eligibility view and the production
blockers.

``operations_robot_eligibility``: the operations/0005 body, same columns, order and types, with ONE addition: the
maintenance reasons, right after the periodic reason and before the robot holds (inside the "has a manifest"
branch, the position of fleet.services.evaluate_eligibility). They come from the set-based
``fleet_maintenance_status_set`` (fleet/0010) called ONCE per statement for every robot, at
transaction_timestamp() in the current mode, and only while the owner-provisioned gate is active
(``fleet_maintenance_gate_active()``; while it is not, no maintenance reason is added and nothing is computed).
The per-robot ``fleet_maintenance_status`` is the same SQL (it calls the set function with one robot), so the
planning command, the planning triggers, the authorization function and this view cannot disagree. Still
security_invoker, STABLE read-only functions only, ops_app SELECT only.

``operations_production_blockers``: the operations/0003 body plus
  MAINTENANCE_RULE_NOT_APPROVED  entity pattern code: a pattern of a non-retired robot's current manifest whose
                                 maintenance status (as production) has a ``no_rule`` row: no requirement in force,
                                 a withdrawn version in force, or a synthetic_demo rule;
  MAINTENANCE_GATE_INACTIVE      entity 'maintenance-gate': the gate is not activated (setting absent or not
                                 'true'), so maintenance does not block yet.
Reverse restores both bodies exactly.
"""

import importlib

from django.conf import settings
from django.db import migrations

_OPS3 = importlib.import_module("operations.migrations.0003_s7_views")
_OPS5 = importlib.import_module("operations.migrations.0005_s7_eligibility_setbased")


def _once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


_MODE = "WITH mode AS MATERIALIZED (SELECT fleet_production_mode() AS production),\n"
_MSTAT = r"""-- v2 02C: blocking maintenance reasons per robot, once per statement, only while the gate is active
mstat AS MATERIALIZED (
  SELECT s.robot_id, array_agg(s.reason ORDER BY s.requirement_code NULLS FIRST) AS reasons
  FROM (SELECT 1 WHERE fleet_maintenance_gate_active()) g
  CROSS JOIN mode
  CROSS JOIN LATERAL fleet_maintenance_status_set(ARRAY(SELECT id FROM fleet_robot), mode.production,
                                                  transaction_timestamp()) s
  WHERE s.blocking
  GROUP BY s.robot_id
),
"""
_PERIODIC_END = "                      WHERE ps.robot_id = r.id AND ps.state <> 'due'), '{}'::text[])\n"
_MAINT_REASONS = (
    "         -- v2 02C: maintenance (fleet_maintenance_status_set), after the periodic reason\n"
    "         || coalesce((SELECT ms.reasons FROM mstat ms WHERE ms.robot_id = r.id), '{}'::text[])\n"
)

ELIGIBILITY_SQL = _once(
    _once(_OPS5.VIEW_SQL, _MODE, _MODE + _MSTAT), _PERIODIC_END, _PERIODIC_END + _MAINT_REASONS
)

_START = "CREATE VIEW operations_production_blockers"
_END = "CREATE VIEW operations_closeout_runs"
BLOCKERS_OLD = (
    _OPS3.SQL[_OPS3.SQL.index(_START) : _OPS3.SQL.index(_END)]
    .replace(_START, "CREATE OR REPLACE VIEW operations_production_blockers", 1)
    .rstrip()
)
_LAST = "WHERE d.id IS NULL OR d.source <> 'owner_approved';"
_ACTIVE = "active_policy AS (SELECT fleet_active_hold_policy_id() AS id)"
BLOCKERS_NEW = _once(
    _once(
        BLOCKERS_OLD,
        _ACTIVE,
        _ACTIVE
        + r""",
maint_rep AS (  -- v2 02C: one robot per configuration in use (the status depends on the configuration only)
  SELECT DISTINCT ON (m.pattern_id) r.id
  FROM fleet_robot r JOIN fleet_configurationmanifest m ON m.id = r.current_manifest_id
  WHERE r.lifecycle <> 'retired'
  ORDER BY m.pattern_id, r.id
)""",
    ),
    _LAST,
    _LAST[:-1]
    + r"""
UNION ALL
SELECT 'MAINTENANCE_RULE_NOT_APPROVED', s.pattern_code,
       format('configuration %s: %s', s.pattern_code, string_agg(s.reason, '; ' ORDER BY s.requirement_code NULLS FIRST))
FROM fleet_maintenance_status_set(ARRAY(SELECT id FROM maint_rep), true, transaction_timestamp()) s
WHERE s.state = 'no_rule' AND s.pattern_code IS NOT NULL
GROUP BY s.pattern_code
UNION ALL
SELECT 'MAINTENANCE_GATE_INACTIVE', 'maintenance-gate',
       'the maintenance gate is not activated yet (owner setting maintenance_gate_active): overdue or unknown '
       || 'maintenance does not block planning or departure'
WHERE NOT fleet_maintenance_gate_active();""",
)


def _grants(view: str) -> str:
    app = settings.DB_APP_ROLE
    return f"REVOKE ALL ON {view} FROM PUBLIC;\nREVOKE ALL ON {view} FROM {app};\nGRANT SELECT ON {view} TO {app};\n"


FORWARD = (
    ELIGIBILITY_SQL
    + "\n"
    + _grants("operations_robot_eligibility")
    + BLOCKERS_NEW
    + "\n"
    + _grants("operations_production_blockers")
)
REVERSE = (
    _OPS5.VIEW_SQL
    + "\n"
    + _grants("operations_robot_eligibility")
    + BLOCKERS_OLD
    + "\n"
    + _grants("operations_production_blockers")
)


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0006_s7_closeout_stage_once"),
        ("planning", "0007_maintenance"),
        ("fleet", "0010_maintenance_sql"),
    ]

    operations = [migrations.RunSQL(FORWARD, reverse_sql=REVERSE)]
