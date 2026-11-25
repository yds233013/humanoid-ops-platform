"""v2 Phase 03A-1 (docs/13_v2_phase03_contract.md 11.2, correction B): hub-qualified role grants.

accounts_rolegrant.hub_id: NULL for a regional grant, set for a hub grant.
  * Hub-scoped roles (hub_technician, field_operator) MUST name a hub; every other role stays regional (NULL):
    check constraint role_grant_hub_scope.
  * The active-grant uniqueness becomes (user, role, coalesce(hub, 0)) over unrevoked grants
    (uniq_active_role_grant, re-created as an expression index).
  * Backfill: existing hub-scoped grants get the existing hub (created as HUB-1 only when such grants exist and
    no hub does).
  * Trigger accounts_rolegrant_default_hub: a hub-scoped grant inserted WITHOUT a hub gets
    platform_core_default_hub() (the only hub; "hub required" when there are several). A person's home hub is
    never consulted: it confers nothing.

SQL: has_active_role_at(uid, role, hub) is true for an active grant of that role whose hub is ``hub`` or NULL
(regional). has_active_role(uid, role) keeps its meaning: any hub. STABLE, fixed search_path, EXECUTE revoked
from PUBLIC and granted to ops_app only. The guarded write functions use the locking variant hub_actor_has_at
(hub migration 0011), which takes the grants FOR SHARE so a concurrent revocation and the write are ordered.

Reverse: drops the function and trigger, restores the (user, role) uniqueness and drops the column (a database
holding two active grants of one role at two hubs cannot be reversed; it says so).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q

APP = settings.DB_APP_ROLE
SCOPED = "('hub_technician', 'field_operator')"

BACKFILL = f"""
DO $bf$ DECLARE h bigint; BEGIN
  IF EXISTS (SELECT 1 FROM accounts_rolegrant WHERE role IN {SCOPED} AND hub_id IS NULL) THEN
    h := platform_core_backfill_hub();
    UPDATE accounts_rolegrant SET hub_id = h WHERE role IN {SCOPED} AND hub_id IS NULL;
  END IF;
END $bf$;
"""

UNIQUE = """
CREATE UNIQUE INDEX uniq_active_role_grant ON accounts_rolegrant (user_id, role, coalesce(hub_id, 0))
  WHERE revoked_at IS NULL;
"""
UNIQUE_REVERSE = "DROP INDEX IF EXISTS uniq_active_role_grant;"

SQL = f"""
CREATE OR REPLACE FUNCTION has_active_role_at(uid bigint, r text, p_hub bigint) RETURNS boolean
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT uid IS NOT NULL AND p_hub IS NOT NULL AND EXISTS (
    SELECT 1 FROM accounts_rolegrant g
    WHERE g.user_id = uid AND g.role = r AND (g.hub_id IS NULL OR g.hub_id = p_hub)
      AND g.revoked_at IS NULL AND g.valid_from <= clock_timestamp()
      AND (g.valid_to IS NULL OR g.valid_to > clock_timestamp()))
$$;
REVOKE ALL ON FUNCTION has_active_role_at(bigint, text, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION has_active_role_at(bigint, text, bigint) TO {APP};

-- The check every guarded write makes AFTER locking its target row: the actor exists and is active, and holds
-- one of ``roles`` at ``p_hub`` (the locked row's hub). It first takes the actor's grants of those roles FOR
-- SHARE (any hub, any validity: revoke_role locks the grant FOR UPDATE), then checks them in a fresh statement:
-- a revocation committed while this waited is seen, one that starts later waits for this transaction.
-- VOLATILE (it locks); not SECURITY DEFINER; EXECUTE revoked from PUBLIC (callers are owner functions).
CREATE OR REPLACE FUNCTION hub_actor_has_at(uid bigint, roles text[], p_hub bigint) RETURNS boolean
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $$
BEGIN
  IF uid IS NULL OR p_hub IS NULL OR roles IS NULL THEN RETURN false; END IF;
  IF NOT EXISTS (SELECT 1 FROM auth_user u WHERE u.id = uid AND u.is_active) THEN RETURN false; END IF;
  PERFORM 1 FROM accounts_rolegrant g WHERE g.user_id = uid AND g.role = ANY(roles) ORDER BY g.id FOR SHARE;
  RETURN EXISTS (SELECT 1 FROM unnest(roles) r WHERE has_active_role_at(uid, r, p_hub));
END $$;
REVOKE ALL ON FUNCTION hub_actor_has_at(bigint, text[], bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION accounts_rolegrant_default_hub() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF NEW.role IN {SCOPED} AND NEW.hub_id IS NULL THEN
    NEW.hub_id := platform_core_default_hub();
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION accounts_rolegrant_default_hub() FROM PUBLIC;
CREATE TRIGGER rolegrant_default_hub BEFORE INSERT ON accounts_rolegrant
  FOR EACH ROW EXECUTE FUNCTION accounts_rolegrant_default_hub();
"""
SQL_REVERSE = """
DROP TRIGGER IF EXISTS rolegrant_default_hub ON accounts_rolegrant;
DROP FUNCTION IF EXISTS accounts_rolegrant_default_hub();
DROP FUNCTION IF EXISTS hub_actor_has_at(bigint, text[], bigint);
DROP FUNCTION IF EXISTS has_active_role_at(bigint, text, bigint);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_hub_technician_role"),
        ("platform_core", "0005_default_hub"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="rolegrant",
            name="hub",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="platform_core.hub",
            ),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.RemoveConstraint(model_name="rolegrant", name="uniq_active_role_grant"),
        migrations.RunSQL(UNIQUE, UNIQUE_REVERSE),
        migrations.AddConstraint(
            model_name="rolegrant",
            constraint=models.CheckConstraint(
                condition=(
                    (Q(("role__in", ["hub_technician", "field_operator"])) & Q(("hub__isnull", False)))
                    | (~Q(("role__in", ["hub_technician", "field_operator"])) & Q(("hub__isnull", True)))
                ),
                name="role_grant_hub_scope",
            ),
        ),
        migrations.RunSQL(SQL, SQL_REVERSE),
    ]
