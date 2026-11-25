"""v2 02E (carried from 02C, contract 8.2 "Counters"): a new maintenance record never hides an open session.

fleet/0010 counted active hours only over execution runs that DEPARTED after the last qualifying record. A run that
departed before the record and still had a session start with no stop was therefore dropped the moment a new
record was written: the hours counter went from Unknown to 0 and the robot from Unknown to Not due (eligible for
planning and authorization) while the run was still open (tests/test_v2_02e_carried.py). The contract says a run
with a session start but no stop makes the hours unknown, never zero.

The hours window now covers every run of the robot that departed after the record OR was not yet closed (trip
closure) at the record's time, i.e. it was still out when the maintenance was recorded. For those runs:

  * a session start with no stop (by ``at``) still makes the hours UNKNOWN (open_runs names the run);
  * once stopped, only the active time AFTER the record counts (each of its intervals is clipped to
    performed_at).

Runs that departed after the record are counted exactly as before (every interval in full). Closed
runs from before the record stay out of the window. Everything else of fleet_maintenance_status_set is unchanged
(CREATE OR REPLACE keeps the signature, the owner and the grants); fleet_maintenance_status and the callers are
untouched. Reverse restores the fleet/0010 body.
"""

import importlib

from django.db import migrations

_F10 = importlib.import_module("fleet.migrations.0010_maintenance_sql")


def _once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


_START = "CREATE OR REPLACE FUNCTION fleet_maintenance_status_set("
_END = "CREATE OR REPLACE FUNCTION fleet_maintenance_status("
OLD = _F10.STATUS[_F10.STATUS.index(_START) : _F10.STATUS.index(_END)]

NEW = _once(
    OLD,
    """    SELECT coalesce(sum(extract(epoch FROM (s.next_at - s.recorded_at)))
                    FILTER (WHERE s.kind IN ('start', 'resume') AND s.next_kind IN ('pause', 'stop')), 0)
             / 3600.0 AS hours_used,""",
    """    SELECT coalesce(sum(extract(epoch FROM (s.next_at - s.counted_from)))
                    FILTER (WHERE s.kind IN ('start', 'resume') AND s.next_kind IN ('pause', 'stop')
                            AND (s.after_record OR s.next_at > s.counted_from)), 0)
             / 3600.0 AS hours_used,""",
)
NEW = _once(
    NEW,
    """      SELECT e.kind, e.recorded_at, er.code::text AS run_code,""",
    """      SELECT e.kind, e.recorded_at, er.code::text AS run_code, er.recorded_at > x.performed_at AS after_record,
             CASE WHEN er.recorded_at > x.performed_at THEN e.recorded_at
                  ELSE greatest(e.recorded_at, x.performed_at) END AS counted_from,""",
)
NEW = _once(
    NEW,
    """      WHERE x.id IS NOT NULL AND er.robot_id = q.robot_id AND er.recorded_at > x.performed_at
        AND e.kind IN ('start', 'pause', 'resume', 'stop') AND e.recorded_at <= p_at""",
    """      WHERE x.id IS NOT NULL AND er.robot_id = q.robot_id
        AND (er.recorded_at > x.performed_at
             OR NOT EXISTS (SELECT 1 FROM execution_tripclosure tc
                            WHERE tc.run_id = er.id AND tc.recorded_at <= x.performed_at))
        AND e.kind IN ('start', 'pause', 'resume', 'stop') AND e.recorded_at <= p_at""",
)


class Migration(migrations.Migration):
    dependencies = [("fleet", "0010_maintenance_sql")]

    operations = [migrations.RunSQL(NEW, reverse_sql=OLD)]
