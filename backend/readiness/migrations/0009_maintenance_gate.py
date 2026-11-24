"""v2 02C (docs/11_v2_phase02_contract.md 3.2, 8.2 "Gates"): "Maintenance up to date" in the authorization
function and the departure guard.

Both are the readiness/0007 bodies (taken from that migration's text, so nothing else changes) with additions:

readiness_issue_authorization (SECURITY DEFINER, signature, search_path and grants kept by CREATE OR REPLACE), right
after the periodic inspection block and while the owner-provisioned gate is active (read under the settings lock the
function already takes SHARED):
  * any blocking maintenance row (overdue, unknown, no rule, required inspection missing) refuses, with the plain
    reasons of fleet_maintenance_block_reasons -- the same SQL the eligibility view and the planning triggers use;
  * the earliest calendar due time must be strictly after the planned unloading end (the periodic rule of EM ruling
    6), and expires_at is bounded by it (defence in depth, as the periodic due date).

readiness_departure_guard: after the relied periodic inspection and the signers, the maintenance status is re-checked
in the CURRENT mode and gate state. This is how Slice 7 treats a periodic inspection that stops qualifying after
authorization (the departure re-proves it; issue bounds expires_at by the due date), applied to maintenance: an
authorization issued before a robot became overdue cannot be used to depart once it is overdue (by calendar, by a
newer rule version, a withdrawal or a newly activated gate). A rule approval or withdrawal also bumps the pattern
and a maintenance record bumps the robot, so the guard snapshot of such an authorization no longer matches either.

Reverse restores the readiness/0007 bodies.
"""

import importlib

from django.db import migrations

from platform_core.locks import SETTINGS_LOCK_KEY

_S7 = importlib.import_module("readiness.migrations.0007_s7_guardrails")


def _once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


_ISSUE_OLD = _S7.ISSUE.replace("{settings_key}", str(SETTINGS_LOCK_KEY))
_DECL = "prod boolean; insp fleet_inspection; tpl fleet_inspectiontemplate; ps record; pat_code text;"
_VEHICLE = "  -- ---------------------------------------------------------------- vehicle (C)"
_MAINT_ISSUE = r"""  -- ---------------------------------------------------------------- v2 02C: maintenance up to date
  IF fleet_maintenance_gate_active() THEN
    m_reasons := fleet_maintenance_block_reasons(rb.id, prod, clock_timestamp());
    IF cardinality(m_reasons) > 0 THEN
      PERFORM fleet_fail('authorization refused: maintenance not up to date: ' || array_to_string(m_reasons, '; '));
    END IF;
    m_due := (SELECT min(s.due_at) FROM fleet_maintenance_status(rb.id, prod, clock_timestamp()) s);
    IF m_due IS NOT NULL AND NOT (m_due > unload_end) THEN
      PERFORM fleet_fail(format('authorization refused: maintenance falls due at %s, not after the planned unloading '
                                'end %s', m_due, unload_end));
    END IF;
  END IF;

"""
_BOUND = "bound := least(bound, ps.due_at);"

ISSUE_NEW = _once(
    _once(
        _once(_ISSUE_OLD, _DECL, _DECL + "\n  m_reasons text[]; m_due timestamptz;"),
        _VEHICLE,
        _MAINT_ISSUE + _VEHICLE,
    ),
    _BOUND,
    _BOUND + "\n  bound := least(bound, m_due);  -- v2 02C: expires_at <= the maintenance calendar due time",
)
ISSUE_NEW = _once(
    ISSUE_NEW,
    "'evidence ages and periodic due date)'",
    "'evidence ages, periodic and maintenance due dates)'",
)

_GUARD_OLD = _S7.GUARDS[_S7.GUARDS.index("CREATE OR REPLACE FUNCTION readiness_departure_guard()") :]
_PROD_LOOP = "  IF prod THEN\n    FOR ins IN"
_MAINT_DEPART = r"""  -- v2 02C: maintenance re-checked in the CURRENT mode and gate state (the function planning and issue use).
  IF fleet_maintenance_gate_active() THEN
    m_reasons := fleet_maintenance_block_reasons(a.robot_id, prod, clock_timestamp());
    IF cardinality(m_reasons) > 0 THEN
      PERFORM fleet_fail('maintenance is not up to date: ' || array_to_string(m_reasons, '; '));
    END IF;
  END IF;
"""
GUARD_NEW = _once(
    _once(
        _GUARD_OLD, "        prod boolean; ins record;", "        prod boolean; ins record; m_reasons text[];"
    ),
    _PROD_LOOP,
    _MAINT_DEPART + _PROD_LOOP,
)


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0008_s7_evidence_time_source"),
        ("fleet", "0010_maintenance_sql"),
    ]

    operations = [
        migrations.RunSQL(ISSUE_NEW + "\n" + GUARD_NEW, reverse_sql=_ISSUE_OLD + "\n" + _GUARD_OLD),
    ]
