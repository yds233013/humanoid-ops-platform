"""v2 Phase 03B-1 (docs/13_v2_phase03_contract.md 13.2, 13.4 item 3, 13.5): zones of a hub.

platform_core_zone(id, hub_id, code, name, active, created_at), unique (hub, code). Setup data written through
run_command (planning.zones; admin or ops lead). Retired = active false. Trigger platform_core_zone_guard:
  * a zone's hub, code and creation time never change (a zone pause and the travel history name it);
  * a retired zone never becomes active again (its history is kept; add a new zone instead);
  * a new zone starts active.
ops_app keeps SELECT, INSERT and UPDATE (the guarded setup commands) and loses DELETE and TRUNCATE: a zone is
never deleted (visits, travel rows and pauses keep referring to it).

platform_core_hub.base_zone_id (nullable): the zone the hub itself sits in (where trips start and end; the travel
matrix pair hub zone -> visit zone gives travel out, the reverse pair travel back). Trigger
platform_core_hub_base_zone_guard: it must be an ACTIVE zone OF THAT HUB when it is set or changed.

Reverse drops the triggers, the column and the table.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

APP = settings.DB_APP_ROLE

SQL = f"""
CREATE OR REPLACE FUNCTION platform_core_zone_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NOT NEW.active THEN
      RAISE EXCEPTION 'A new zone starts active' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.hub_id IS DISTINCT FROM OLD.hub_id OR NEW.code IS DISTINCT FROM OLD.code
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'Zone % cannot change its hub or code; retire it and add a new zone', OLD.code
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.active AND NOT OLD.active THEN
    RAISE EXCEPTION 'Zone % is retired; a retired zone keeps its history and cannot be used again. Add a new zone',
      OLD.code USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION platform_core_zone_guard() FROM PUBLIC;
CREATE TRIGGER zone_guard BEFORE INSERT OR UPDATE ON platform_core_zone
  FOR EACH ROW EXECUTE FUNCTION platform_core_zone_guard();
CREATE TRIGGER zone_no_delete BEFORE DELETE ON platform_core_zone
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER zone_no_truncate BEFORE TRUNCATE ON platform_core_zone
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();

CREATE OR REPLACE FUNCTION platform_core_hub_base_zone_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE z platform_core_zone;
BEGIN
  IF NEW.base_zone_id IS NULL OR (TG_OP = 'UPDATE' AND NEW.base_zone_id IS NOT DISTINCT FROM OLD.base_zone_id) THEN
    RETURN NEW;
  END IF;
  SELECT * INTO z FROM platform_core_zone WHERE id = NEW.base_zone_id;
  IF z.hub_id IS DISTINCT FROM NEW.id THEN
    RAISE EXCEPTION 'Zone % belongs to another hub; a hub sits in one of its own zones', z.code
      USING ERRCODE = 'check_violation';
  END IF;
  IF NOT z.active THEN
    RAISE EXCEPTION 'Zone % is retired; choose an active zone', z.code USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION platform_core_hub_base_zone_guard() FROM PUBLIC;
CREATE TRIGGER hub_base_zone_guard BEFORE INSERT OR UPDATE OF base_zone_id ON platform_core_hub
  FOR EACH ROW EXECUTE FUNCTION platform_core_hub_base_zone_guard();

REVOKE DELETE, TRUNCATE ON platform_core_zone FROM {APP};
"""
SQL_REVERSE = f"""
DROP TRIGGER IF EXISTS hub_base_zone_guard ON platform_core_hub;
DROP FUNCTION IF EXISTS platform_core_hub_base_zone_guard();
DROP TRIGGER IF EXISTS zone_no_truncate ON platform_core_zone;
DROP TRIGGER IF EXISTS zone_no_delete ON platform_core_zone;
DROP TRIGGER IF EXISTS zone_guard ON platform_core_zone;
DROP FUNCTION IF EXISTS platform_core_zone_guard();
GRANT DELETE ON platform_core_zone TO {APP};
"""


class Migration(migrations.Migration):
    dependencies = [
        ("platform_core", "0005_default_hub"),
    ]

    operations = [
        migrations.CreateModel(
            name="Zone",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("code", models.CharField(max_length=12)),
                ("name", models.CharField(max_length=80)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "hub",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="zones",
                        to="platform_core.hub",
                    ),
                ),
            ],
            options={
                "db_table": "platform_core_zone",
                "ordering": ["hub_id", "code"],
            },
        ),
        migrations.AddField(
            model_name="hub",
            name="base_zone",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="platform_core.zone",
            ),
        ),
        migrations.AddConstraint(
            model_name="zone",
            constraint=models.UniqueConstraint(fields=("hub", "code"), name="uniq_zone_hub_code"),
        ),
        migrations.RunSQL(SQL, SQL_REVERSE),
    ]
