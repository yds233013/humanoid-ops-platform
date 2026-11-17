"""One visit, one departure (found in the integrated UI check: after a departure the app could issue a second
authorization for the same visit, and so record a second departure).

- Unique index on readiness_departureevent(visit_id): the database alone guarantees at most one departure per
  visit, even for direct SQL and concurrent inserts.
- Authorization insert is refused once the visit has a departure. The command already holds the visit FOR
  UPDATE, and the departure trigger locks the visit FOR SHARE, so the check is ordered against departures.
"""

from django.db import migrations

SQL = r"""
CREATE UNIQUE INDEX uniq_departure_per_visit ON readiness_departureevent (visit_id);

CREATE OR REPLACE FUNCTION readiness_authorization_not_departed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM readiness_departureevent WHERE visit_id = NEW.visit_id) THEN
    PERFORM fleet_fail('this visit has already departed; one visit has exactly one departure');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER readiness_authorization_not_departed BEFORE INSERT ON readiness_departureauthorization
  FOR EACH ROW EXECUTE FUNCTION readiness_authorization_not_departed();
"""


class Migration(migrations.Migration):
    dependencies = [("readiness", "0003_active_policy_and_live_guards")]
    operations = [
        migrations.RunSQL(
            SQL,
            reverse_sql=(
                "DROP TRIGGER readiness_authorization_not_departed ON readiness_departureauthorization; "
                "DROP FUNCTION readiness_authorization_not_departed(); DROP INDEX uniq_departure_per_visit;"
            ),
        )
    ]
