"""v2 02C (docs/11_v2_phase02_contract.md 8.2, 8.6): the maintenance_technician qualification kind.

- planning_qualification.kind: varchar(20) -> varchar(24) and the kind CHECK gains ``maintenance_technician``.
  PostgreSQL refuses to change the type of a column a view reads, and operations_robot_eligibility (the
  set-based body of operations/0005) reads ``q.kind``. So the view is first switched to its operations/0004 body
  (same columns, order and types; it reads the qualification only inside plpgsql functions, which PostgreSQL does
  not track), the column is altered, and the operations/0005 body is put back unchanged. CREATE OR REPLACE keeps
  the dependent view operations_robot_status and the privileges (re-asserted below anyway).
- planning_qualification_insert_guard(): ``maintenance_technician`` is granted exactly like
  ``readiness_inspector`` (contract 8.6 "Grant qualifications: safety_reviewer"): the grantor holds an active
  safety_reviewer role now, never to oneself, never backdated (valid_from NULL = now, the same clock_timestamp()
  as granted_at), a non-blank training-record reference in basis_ref. Other kinds keep their behaviour.
- planning_maintainer_covered(user, at): an unrevoked maintenance_technician grant of the user's Person whose
  [valid_from, valid_to] contains ``at`` (revocation counts, checked now), the twin of planning_inspector_covered.
No existing qualification changes kind; this migration inserts no row.
"""

import importlib

from django.conf import settings
from django.db import migrations, models

_OPS4 = importlib.import_module("operations.migrations.0004_s7_eligibility_periodic")
_OPS5 = importlib.import_module("operations.migrations.0005_s7_eligibility_setbased")

GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION planning_qualification_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_now timestamptz := clock_timestamp();
BEGIN
  NEW.granted_at := v_now;
  IF NEW.kind IN ('readiness_inspector', 'maintenance_technician') THEN
    IF NEW.valid_from IS NULL THEN NEW.valid_from := v_now; END IF;
    IF NOT has_active_role(NEW.granted_by_id, 'safety_reviewer') THEN
      PERFORM fleet_fail(format('a %s qualification can be granted only by a safety_reviewer', NEW.kind));
    END IF;
    IF NEW.granted_by_id = (SELECT user_id FROM planning_person WHERE id = NEW.person_id) THEN
      PERFORM fleet_fail(format('a %s qualification cannot be granted to oneself', NEW.kind));
    END IF;
    IF NEW.valid_from < NEW.granted_at THEN
      PERFORM fleet_fail(format('a %s qualification cannot be backdated (valid_from before granted_at)', NEW.kind));
    END IF;
    IF btrim(coalesce(NEW.basis_ref, '')) = '' THEN
      PERFORM fleet_fail(format('a %s qualification needs a training record reference (basis_ref)', NEW.kind));
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION planning_maintainer_covered(p_user bigint, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT p_user IS NOT NULL AND p_at IS NOT NULL AND EXISTS (
    SELECT 1 FROM planning_qualification q JOIN planning_person p ON p.id = q.person_id
    WHERE p.user_id = p_user AND q.kind = 'maintenance_technician' AND q.revoked_at IS NULL
      AND q.valid_from <= p_at AND p_at <= q.valid_to)
$$;
"""

GUARD_REVERSE = r"""
DROP FUNCTION IF EXISTS planning_maintainer_covered(bigint, timestamptz);
CREATE OR REPLACE FUNCTION planning_qualification_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_now timestamptz := clock_timestamp();
BEGIN
  NEW.granted_at := v_now;
  IF NEW.kind = 'readiness_inspector' THEN
    IF NEW.valid_from IS NULL THEN NEW.valid_from := v_now; END IF;
    IF NOT has_active_role(NEW.granted_by_id, 'safety_reviewer') THEN
      PERFORM fleet_fail('a readiness_inspector qualification can be granted only by a safety_reviewer');
    END IF;
    IF NEW.granted_by_id = (SELECT user_id FROM planning_person WHERE id = NEW.person_id) THEN
      PERFORM fleet_fail('a readiness_inspector qualification cannot be granted to oneself');
    END IF;
    IF NEW.valid_from < NEW.granted_at THEN
      PERFORM fleet_fail('a readiness_inspector qualification cannot be backdated (valid_from before granted_at)');
    END IF;
    IF btrim(coalesce(NEW.basis_ref, '')) = '' THEN
      PERFORM fleet_fail('a readiness_inspector qualification needs a training record reference (basis_ref)');
    END IF;
  END IF;
  RETURN NEW;
END $$;
"""

# The view swap around the column type change (see the module docstring). Both bodies have the same columns.
VIEW_TO_0004 = _OPS4.VIEW_SQL + "\n" + _OPS5._grants()
VIEW_TO_0005 = _OPS5.VIEW_SQL + "\n" + _OPS5._grants()

KINDS = [
    ("field_lead", "Field lead"),
    ("field_operator", "Field operator"),
    ("robot_handler", "Robot handling / loading"),
    ("readiness_inspector", "Readiness inspector"),
    ("maintenance_technician", "Maintenance technician"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0006_s7_qualification"),
        ("operations", "0006_s7_closeout_stage_once"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunSQL(VIEW_TO_0004, reverse_sql=VIEW_TO_0005),
        migrations.RemoveConstraint(
            model_name="qualification",
            name="qualification_kind_valid",
        ),
        migrations.AlterField(
            model_name="qualification",
            name="kind",
            field=models.CharField(choices=KINDS, max_length=24),
        ),
        migrations.AddConstraint(
            model_name="qualification",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", [k for k, _ in KINDS])),
                name="qualification_kind_valid",
            ),
        ),
        migrations.RunSQL(VIEW_TO_0005, reverse_sql=VIEW_TO_0004),
        migrations.RunSQL(GUARD_SQL, reverse_sql=GUARD_REVERSE),
    ]
