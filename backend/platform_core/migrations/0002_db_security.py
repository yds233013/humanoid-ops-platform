"""Database security boundary (runs as the schema owner).

* audit_event is append-only: trigger rejects UPDATE/DELETE/TRUNCATE; runtime role has no such privilege.
* The runtime role gets DML on application tables only - never ownership, so it cannot ALTER TABLE,
  DISABLE TRIGGER or DROP. Default privileges cover tables created by later migrations.
* Privileged DB administrators (superuser, schema owner) remain a separate, documented trust boundary.
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE
AUDIT = "platform_core_auditevent"

forward = f"""
CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER audit_no_update_delete BEFORE UPDATE OR DELETE ON {AUDIT}
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON {AUDIT}
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();

GRANT USAGE ON SCHEMA public TO {APP};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP};

REVOKE UPDATE, DELETE, TRUNCATE ON {AUDIT} FROM {APP};
REVOKE ALL ON django_migrations FROM {APP};
GRANT SELECT ON django_migrations TO {APP};
"""

reverse = f"""
DROP TRIGGER IF EXISTS audit_no_update_delete ON {AUDIT};
DROP TRIGGER IF EXISTS audit_no_truncate ON {AUDIT};
DROP FUNCTION IF EXISTS reject_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("platform_core", "0001_initial"),
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("sessions", "0001_initial"),
    ]
    operations = [migrations.RunSQL(forward, reverse)]
