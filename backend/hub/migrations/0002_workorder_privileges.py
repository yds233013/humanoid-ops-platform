"""ops_app privileges on hub_workorder (docs/11_v2_phase02_contract.md 8.8).

The runtime role writes work orders directly through the command layer (they are operational, never evidence),
so it keeps SELECT, INSERT and UPDATE (and the id sequence) from the default privileges of platform_core 0002,
and loses DELETE and TRUNCATE: a task is closed (done / cancelled), never deleted. Reverse restores the default.
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE

forward = f"""
REVOKE ALL ON hub_workorder FROM {APP};
GRANT SELECT, INSERT, UPDATE ON hub_workorder TO {APP};
GRANT USAGE, SELECT ON SEQUENCE hub_workorder_id_seq TO {APP};
"""
reverse = f"GRANT SELECT, INSERT, UPDATE, DELETE ON hub_workorder TO {APP};"


class Migration(migrations.Migration):
    dependencies = [("hub", "0001_initial"), ("platform_core", "0002_db_security")]
    operations = [migrations.RunSQL(forward, reverse)]
