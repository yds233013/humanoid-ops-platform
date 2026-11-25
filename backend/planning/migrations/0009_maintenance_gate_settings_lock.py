"""v2 02E (carried from 02C, contract 8.2 "Gates" and 8.8): the planning maintenance triggers take the settings
lock SHARED before they read the gate.

planning/0008 read the owner setting maintenance_gate_active without any lock. Activation (provision_settings,
or any write to fleet_deploymentsetting through the fleet/0007 statement trigger) takes the settings advisory lock
EXCLUSIVE, so a direct-SQL reservation, revision or visit transition as the runtime role could read the gate as
inactive while the activation was uncommitted, and then commit AFTER the activation: a blocked robot got a new
booking or confirmation under an active gate, without the gate applying (tests/test_v2_02e_carried.py).

Now each of the three triggers, when it would consult the gate (a robot reservation, a non-blocked revision with a
robot, a requested -> planned or any -> confirmed transition), first runs
pg_advisory_xact_lock_shared(SETTINGS_LOCK_KEY), then reads the gate and the status in a NEW statement (these
trigger functions are VOLATILE, so under READ COMMITTED that statement takes a fresh snapshot and sees an
activation that committed while it waited). Therefore:

  * activation first: the writer waits until the activation commits, then sees the gate active and is refused;
  * writer first: the activation waits until the writer commits; the booking is then an existing booking, which
    live feasibility shows as blocked with its reasons and which is never re-planned silently (02C point 7).

Lock order: everywhere else the settings lock is taken before any guard row. Here it may be taken after the
statement's row locks. That cannot deadlock against an activation, because a settings change takes the lock
EXCLUSIVE in its own transaction and holds no guard row (platform_core.locks); shared holders never conflict with
each other, and a transaction that already holds the key (the plan command, share_settings=True) is granted it
again at once. No Python statement is added, so the statement budgets are unchanged.

Only the three trigger functions change (CREATE OR REPLACE; the triggers stay as they are). Reverse restores the
planning/0008 bodies.
"""

import importlib

from django.db import migrations

from platform_core.locks import SETTINGS_LOCK_KEY

_P8 = importlib.import_module("planning.migrations.0008_maintenance_gate")


def _once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def _functions(sql: str) -> str:
    """The three trigger functions of planning/0008 (no CREATE TRIGGER, no helper)."""
    out = []
    for name in (
        "planning_revision_maintenance_guard",
        "planning_reservation_maintenance_guard",
        "planning_visit_maintenance_guard",
    ):
        start = sql.index(f"CREATE OR REPLACE FUNCTION {name}()")
        end = sql.index("END $$;", start) + len("END $$;")
        out.append(sql[start:end])
    return "\n\n".join(out) + "\n"


LOCK = (
    f"PERFORM pg_advisory_xact_lock_shared({SETTINGS_LOCK_KEY});  -- settings lock SHARED, then read the gate"
)

OLD = _functions(_P8.SQL)

NEW = _once(
    OLD,
    """  IF NEW.feasibility IS DISTINCT FROM 'blocked' THEN
    problem := planning_maintenance_problem(NEW.robot_id);""",
    f"""  IF NEW.feasibility IS DISTINCT FROM 'blocked' AND NEW.robot_id IS NOT NULL THEN
    {LOCK}
    problem := planning_maintenance_problem(NEW.robot_id);""",
)
NEW = _once(
    NEW,
    """DECLARE problem text;
BEGIN
  problem := planning_maintenance_problem(
    (SELECT robot_id FROM planning_resource WHERE id = NEW.resource_id AND kind = 'robot'));""",
    f"""DECLARE problem text; rb bigint;
BEGIN
  rb := (SELECT robot_id FROM planning_resource WHERE id = NEW.resource_id AND kind = 'robot');
  IF rb IS NULL THEN RETURN NEW; END IF;
  {LOCK}
  problem := planning_maintenance_problem(rb);""",
)
NEW = _once(
    NEW,
    """     OR (NEW.status = 'confirmed' AND OLD.status IS DISTINCT FROM 'confirmed') THEN
    problem""",
    f"""     OR (NEW.status = 'confirmed' AND OLD.status IS DISTINCT FROM 'confirmed') THEN
    {LOCK}
    problem""",
)


class Migration(migrations.Migration):
    dependencies = [("planning", "0008_maintenance_gate")]

    operations = [migrations.RunSQL(NEW, reverse_sql=OLD)]
