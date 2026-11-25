"""v2 Phase 03A-1 (docs/13_v2_phase03_contract.md 11.2, correction B): recording maintenance gains the hub
dimension.

fleet_record_maintenance (fleet/0010) keeps its first role check (any hub) and now ALSO checks, after taking the
robot's row lock (its guard-version bump), that the actor holds hub_technician, engineer or engineering_lead AT
THE ROBOT'S HOME HUB: hub_actor_has_at (accounts 0005; it takes the actor's grants FOR SHARE, so a concurrent
revocation and the record are ordered). A hub technician of another hub is refused (insufficient_privilege) and
nothing is written. Engineers and the engineering lead are regional roles, so their behaviour is unchanged.

Only this function's body changes (CREATE OR REPLACE keeps the owner, search_path and the ops_app-only EXECUTE).
Reverse restores the fleet/0010 body.
"""

import importlib

from django.db import migrations

from platform_core.locks import SETTINGS_LOCK_KEY

base = importlib.import_module("fleet.migrations.0010_maintenance_sql")

START = "CREATE OR REPLACE FUNCTION fleet_record_maintenance("
text = base.FUNCTIONS.replace("{settings_key}", str(SETTINGS_LOCK_KEY))
original = text[text.index(START) : text.index("END $fn$;\n", text.index(START)) + len("END $fn$;\n")]

BEFORE = "  SELECT * INTO rb FROM fleet_robot WHERE id = p_robot;\n  IF rb.lifecycle = 'retired' THEN"
AFTER = (
    "  SELECT * INTO rb FROM fleet_robot WHERE id = p_robot;\n"
    "  IF NOT hub_actor_has_at(p_actor, ARRAY['hub_technician', 'engineer', 'engineering_lead'], rb.hub_id) THEN\n"
    "    PERFORM fleet_maint_deny(format('You hold no role that records maintenance at hub %s',\n"
    "                                    (SELECT h.code FROM platform_core_hub h WHERE h.id = rb.hub_id)));\n"
    "  END IF;\n"
    "  IF rb.lifecycle = 'retired' THEN"
)
assert original.count(BEFORE) == 1
forward = original.replace(BEFORE, AFTER)


class Migration(migrations.Migration):
    dependencies = [
        ("fleet", "0012_hub_ownership_and_hub_settings"),
        ("accounts", "0005_hub_qualified_grants"),
    ]
    operations = [migrations.RunSQL(forward, original)]
