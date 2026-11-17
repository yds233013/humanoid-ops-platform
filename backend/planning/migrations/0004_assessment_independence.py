"""Assessment approval independence enforced in PostgreSQL: the approver must hold ops_lead or
engineering_lead AND must not be the person who performed the assessment."""

from django.db import migrations

SQL = r"""
CREATE OR REPLACE FUNCTION planning_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT (has_active_role(NEW.approver_id, 'ops_lead') OR has_active_role(NEW.approver_id, 'engineering_lead')) THEN
    PERFORM fleet_fail('assessment approval requires ops_lead or engineering_lead');
  END IF;
  IF NEW.approver_id = (SELECT assessed_by_id FROM planning_siteassessment WHERE id = NEW.assessment_id) THEN
    PERFORM fleet_fail('the assessor cannot approve their own assessment');
  END IF;
  UPDATE planning_site SET version = version
   WHERE id = (SELECT site_id FROM planning_siteassessment WHERE id = NEW.assessment_id);
  RETURN NEW;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("planning", "0003_positive_values")]
    operations = [migrations.RunSQL(SQL, reverse_sql=migrations.RunSQL.noop)]
