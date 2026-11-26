"""v2 Phase 03B-1 (docs/13_v2_phase03_contract.md I-9, 10.G, 13.2, 13.4 items 1 and 2, 13.5): the regional pause and
its database gates.

TABLE operations_region_pause(id, hub_id, zone_id null, kind 'pause'|'lift', pause_id null, reason, actor_id,
created_at): append-only (UPDATE, DELETE, TRUNCATE refused); a lift names its pause (check constraint); at most
one lift per pause (partial unique index). ops_app has SELECT only and no sequence use.

WRITERS (SECURITY DEFINER, owned by ops_owner, fixed search_path, EXECUTE for ops_app only, one audit row each):
  operations_pause_region(actor, hub, zone, reason, request_id) -> pause id     operations.pause
      the regional ops lead, or a safety_pause grant AT that hub (accounts/0008). A hub technician or field
      operator grant is not enough. zone NULL pauses the whole hub; a zone must belong to the hub.
  operations_lift_pause(actor, pause, reason, request_id) -> lift id            operations.lift_pause
      the regional ops lead only (a safety_pause grant can never lift).
  Both refuse a NULL, zero, unknown, inactive or role-less actor first (insufficient_privilege), before any lock.

WHICH PAUSE APPLIES operations_active_pause(hub, zone) RETURNS TABLE(pause_id, scope, reason, since), for a
visit's DISPATCH hub and DESTINATION zone (both read from the visit row, never from the caller):
  * a hub pause applies to departures from the hub AND to visits whose destination zone belongs to it, so a
    cross-hub reassignment cannot route around it; when one applies it overrides zone-level state (only the hub
    rows are returned);
  * a zone pause applies to visits whose destination zone is that zone, whatever their dispatch hub;
  * an unknown (NULL) destination zone is covered by ANY active zone pause at the visit's hub.

GATES (I-9): triggers readiness_pause_gate BEFORE INSERT on readiness_departureauthorization (so
readiness_issue_authorization refuses) and on readiness_departureevent (the departure guard refuses). Robots
already out keep recording everything else (custody, trip events, sessions, incidents, holds, interruptions):
nothing else is gated, nothing is recalled, no departure is erased.

LOCK ORDER (13.4 item 2, binding; docs/evidence/03a_lock_graph.md section 4): the settings lock first, then the
hub row(s) by ascending id.
  * pause and lift: settings Sh -> hub FOR UPDATE -> the actor's grants FOR SHARE (hub_actor_has_at).
  * authorization and departure gates: settings Sh (re-entrant: both already hold it) -> their existing guard
    rows -> the visit FOR SHARE -> the dispatch hub and the destination zone's hub FOR SHARE, then the check in a
    fresh statement. A pause committed while the gate waited is seen; a pause started later waits for the gate's
    transaction. Neither can get past an uncommitted pause.
  * a change of a visit's hub or zone (trigger visit_pause_scope_lock): settings Sh -> the old and new hubs (and
    the hubs of the old and new zones) FOR SHARE. The gates read the visit's hub and zone at authorization and
    departure time, so a changed visit is always re-checked against every applicable pause.

Reverse drops the triggers, the functions and the table.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from platform_core.locks import SETTINGS_LOCK_KEY

APP = settings.DB_APP_ROLE
KEY = SETTINGS_LOCK_KEY
PAUSE_ROLES = "ARRAY['ops_lead', 'safety_pause']"
LIFT_ROLES = "ARRAY['ops_lead']"

SQL = rf"""
CREATE TRIGGER region_pause_append_only BEFORE UPDATE OR DELETE ON operations_region_pause
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER region_pause_no_truncate BEFORE TRUNCATE ON operations_region_pause
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();

-- "Oakland hub (OAK)" / "zone Downtown (DT) of Oakland hub (OAK)"
CREATE OR REPLACE FUNCTION operations_pause_scope_text(p_hub bigint, p_zone bigint) RETURNS text
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT CASE WHEN p_zone IS NULL THEN format('%s (%s)', h.name, h.code)
              ELSE format('zone %s (%s) of %s (%s)', z.name, z.code, h.name, h.code) END
  FROM platform_core_hub h LEFT JOIN platform_core_zone z ON z.id = p_zone WHERE h.id = p_hub
$$;
REVOKE ALL ON FUNCTION operations_pause_scope_text(bigint, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION operations_pause_scope_text(bigint, bigint) TO {APP};

CREATE OR REPLACE FUNCTION operations_active_pause(p_hub bigint, p_zone bigint)
RETURNS TABLE (pause_id bigint, scope text, reason text, since timestamptz)
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  WITH act AS (
    SELECT p.id, p.hub_id, p.zone_id, p.reason, p.created_at FROM operations_region_pause p
    WHERE p.kind = 'pause'
      AND NOT EXISTS (SELECT 1 FROM operations_region_pause l WHERE l.kind = 'lift' AND l.pause_id = p.id)
  ), hit AS (
    SELECT a.id, 'hub'::text AS scope, a.reason, a.created_at, 0 AS rank FROM act a
    WHERE a.zone_id IS NULL
      AND (a.hub_id = p_hub OR a.hub_id = (SELECT z.hub_id FROM platform_core_zone z WHERE z.id = p_zone))
    UNION ALL
    SELECT a.id, 'zone'::text, a.reason, a.created_at, 1 FROM act a
    WHERE a.zone_id IS NOT NULL AND (a.zone_id = p_zone OR (p_zone IS NULL AND a.hub_id = p_hub))
  )
  SELECT h.id, h.scope, h.reason, h.created_at FROM hit h
  WHERE h.rank = (SELECT min(x.rank) FROM hit x) ORDER BY h.created_at, h.id
$$;
REVOKE ALL ON FUNCTION operations_active_pause(bigint, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION operations_active_pause(bigint, bigint) TO {APP};

-- The gate both triggers use: lock (settings Sh -> visit S -> hubs S, ascending), then check in a fresh statement.
-- NULL when no pause covers the visit, else the plain reason (no time: refusal texts never show UTC).
CREATE OR REPLACE FUNCTION operations_pause_gate_problem(p_visit bigint) RETURNS text
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $fn$
DECLARE v record; zone_hub bigint; a record; pz bigint; ph bigint;
BEGIN
  PERFORM pg_advisory_xact_lock_shared({KEY});
  SELECT x.hub_id, x.zone_id INTO v FROM planning_visit x WHERE x.id = p_visit FOR SHARE;
  IF NOT FOUND THEN RETURN NULL; END IF;  -- the other guards refuse an unknown visit
  zone_hub := (SELECT z.hub_id FROM platform_core_zone z WHERE z.id = v.zone_id);
  PERFORM 1 FROM platform_core_hub h WHERE h.id = v.hub_id OR h.id = zone_hub ORDER BY h.id FOR SHARE;
  SELECT * INTO a FROM operations_active_pause(v.hub_id, v.zone_id) LIMIT 1;
  IF a.pause_id IS NULL THEN RETURN NULL; END IF;
  SELECT p.hub_id, p.zone_id INTO ph, pz FROM operations_region_pause p WHERE p.id = a.pause_id;
  RETURN format('Paused: cannot leave until lifted. Departures %s %s are paused: %s',
                CASE WHEN pz IS NULL AND ph = v.hub_id THEN 'from' ELSE 'to' END,
                operations_pause_scope_text(ph, pz), a.reason)
         || CASE WHEN v.zone_id IS NULL AND pz IS NOT NULL
                 THEN ' (this visit''s zone is unknown, so every zone pause at its hub covers it)' ELSE '' END;
END $fn$;
REVOKE ALL ON FUNCTION operations_pause_gate_problem(bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION operations_authorization_pause_gate() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE problem text;
BEGIN
  problem := operations_pause_gate_problem(NEW.visit_id);
  IF problem IS NOT NULL THEN PERFORM fleet_fail('authorization refused: ' || problem); END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION operations_authorization_pause_gate() FROM PUBLIC;
CREATE TRIGGER readiness_pause_gate BEFORE INSERT ON readiness_departureauthorization
  FOR EACH ROW EXECUTE FUNCTION operations_authorization_pause_gate();

CREATE OR REPLACE FUNCTION operations_departure_pause_gate() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE problem text;
BEGIN
  problem := operations_pause_gate_problem(NEW.visit_id);
  IF problem IS NOT NULL THEN PERFORM fleet_fail('departure refused: ' || problem); END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION operations_departure_pause_gate() FROM PUBLIC;
CREATE TRIGGER readiness_pause_gate BEFORE INSERT ON readiness_departureevent
  FOR EACH ROW EXECUTE FUNCTION operations_departure_pause_gate();

-- A change of a visit's dispatch hub or destination zone takes the same locks in the same order.
CREATE OR REPLACE FUNCTION operations_visit_scope_change_lock() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
BEGIN
  PERFORM pg_advisory_xact_lock_shared({KEY});
  PERFORM 1 FROM platform_core_hub h
  WHERE h.id IN (OLD.hub_id, NEW.hub_id,
                 (SELECT z.hub_id FROM platform_core_zone z WHERE z.id = OLD.zone_id),
                 (SELECT z.hub_id FROM platform_core_zone z WHERE z.id = NEW.zone_id))
  ORDER BY h.id FOR SHARE;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION operations_visit_scope_change_lock() FROM PUBLIC;
CREATE TRIGGER visit_pause_scope_lock BEFORE UPDATE OF hub_id, zone_id ON planning_visit
  FOR EACH ROW WHEN (OLD.hub_id IS DISTINCT FROM NEW.hub_id OR OLD.zone_id IS DISTINCT FROM NEW.zone_id)
  EXECUTE FUNCTION operations_visit_scope_change_lock();

-- ------------------------------------------------------------------------------------------- pause and lift
CREATE OR REPLACE FUNCTION operations_pause_region(p_actor bigint, p_hub bigint, p_zone bigint, p_reason text,
                                                   p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE h platform_core_hub; z platform_core_zone; new_id bigint; what text;
BEGIN
  IF NOT hub_actor_has(p_actor, {PAUSE_ROLES}) THEN
    PERFORM hub_deny('Only the regional ops lead, or someone with the safety pause permission at the hub, can '
                     'pause departures');
  END IF;
  PERFORM pg_advisory_xact_lock_shared({KEY});  -- settings first, then the hub row (13.4 item 2)
  SELECT * INTO h FROM platform_core_hub WHERE id = p_hub FOR UPDATE;
  IF NOT FOUND THEN PERFORM fleet_fail('Choose the hub to pause'); END IF;
  IF NOT hub_actor_has_at(p_actor, {PAUSE_ROLES}, h.id) THEN
    PERFORM hub_deny(format('You can''t pause departures at %s (%s). Ask a regional ops lead or someone with the '
                            'safety pause permission there', h.name, h.code));
  END IF;
  IF p_zone IS NOT NULL THEN
    SELECT * INTO z FROM platform_core_zone WHERE id = p_zone;
    IF NOT FOUND OR z.hub_id IS DISTINCT FROM h.id THEN
      PERFORM fleet_fail(format('Choose a zone of %s (%s), or pause the whole hub', h.name, h.code));
    END IF;
  END IF;
  IF coalesce(btrim(p_reason), '') = '' OR length(btrim(p_reason)) > 200 THEN
    PERFORM fleet_fail('Give the reason for the pause (up to 200 characters)');
  END IF;
  what := operations_pause_scope_text(h.id, p_zone);
  IF EXISTS (SELECT 1 FROM operations_region_pause p
             WHERE p.kind = 'pause' AND p.hub_id = h.id AND p.zone_id IS NOT DISTINCT FROM p_zone
               AND NOT EXISTS (SELECT 1 FROM operations_region_pause l WHERE l.kind = 'lift' AND l.pause_id = p.id)) THEN
    PERFORM fleet_fail(format('Departures for %s are already paused', what));
  END IF;
  INSERT INTO operations_region_pause (hub_id, zone_id, kind, pause_id, reason, actor_id, created_at)
  VALUES (h.id, p_zone, 'pause', NULL, btrim(p_reason), p_actor, clock_timestamp())
  RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'operations.pause_region', 'region_pause', new_id::text,
                    jsonb_build_object('hub', h.code, 'zone', z.code, 'reason', btrim(p_reason)),
                    jsonb_build_object('pause_id', new_id, 'scope', CASE WHEN p_zone IS NULL THEN 'hub' ELSE 'zone' END),
                    p_rid);
  RETURN new_id;
END $fn$;
REVOKE ALL ON FUNCTION operations_pause_region(bigint, bigint, bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION operations_pause_region(bigint, bigint, bigint, text, text) TO {APP};

CREATE OR REPLACE FUNCTION operations_lift_pause(p_actor bigint, p_pause bigint, p_reason text, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE p operations_region_pause; h platform_core_hub; new_id bigint;
BEGIN
  IF NOT hub_actor_has(p_actor, {LIFT_ROLES}) THEN
    PERFORM hub_deny('Only the regional ops lead can lift a pause');
  END IF;
  PERFORM pg_advisory_xact_lock_shared({KEY});  -- settings first, then the hub row (13.4 item 2)
  SELECT * INTO p FROM operations_region_pause WHERE id = p_pause;  -- append-only: its hub never changes
  IF NOT FOUND OR p.kind <> 'pause' THEN PERFORM fleet_fail('No such pause'); END IF;
  SELECT * INTO h FROM platform_core_hub WHERE id = p.hub_id FOR UPDATE;
  IF NOT hub_actor_has_at(p_actor, {LIFT_ROLES}, h.id) THEN
    PERFORM hub_deny('Only the regional ops lead can lift a pause');
  END IF;
  IF coalesce(btrim(p_reason), '') = '' OR length(btrim(p_reason)) > 200 THEN
    PERFORM fleet_fail('Give the reason for lifting the pause (up to 200 characters)');
  END IF;
  IF EXISTS (SELECT 1 FROM operations_region_pause l WHERE l.kind = 'lift' AND l.pause_id = p.id) THEN
    PERFORM fleet_fail(format('The pause of %s was already lifted', operations_pause_scope_text(p.hub_id, p.zone_id)));
  END IF;
  INSERT INTO operations_region_pause (hub_id, zone_id, kind, pause_id, reason, actor_id, created_at)
  VALUES (p.hub_id, p.zone_id, 'lift', p.id, btrim(p_reason), p_actor, clock_timestamp())
  RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'operations.lift_pause', 'region_pause', p.id::text,
                    jsonb_build_object('pause_id', p.id, 'hub', h.code, 'reason', btrim(p_reason)),
                    jsonb_build_object('lift_id', new_id, 'lifted', true), p_rid);
  RETURN new_id;
END $fn$;
REVOKE ALL ON FUNCTION operations_lift_pause(bigint, bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION operations_lift_pause(bigint, bigint, text, text) TO {APP};

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON operations_region_pause FROM {APP};
GRANT SELECT ON operations_region_pause TO {APP};
REVOKE ALL ON SEQUENCE operations_region_pause_id_seq FROM {APP};
"""

SQL_REVERSE = """
DROP FUNCTION IF EXISTS operations_lift_pause(bigint, bigint, text, text);
DROP FUNCTION IF EXISTS operations_pause_region(bigint, bigint, bigint, text, text);
DROP TRIGGER IF EXISTS visit_pause_scope_lock ON planning_visit;
DROP FUNCTION IF EXISTS operations_visit_scope_change_lock();
DROP TRIGGER IF EXISTS readiness_pause_gate ON readiness_departureevent;
DROP FUNCTION IF EXISTS operations_departure_pause_gate();
DROP TRIGGER IF EXISTS readiness_pause_gate ON readiness_departureauthorization;
DROP FUNCTION IF EXISTS operations_authorization_pause_gate();
DROP FUNCTION IF EXISTS operations_pause_gate_problem(bigint);
DROP FUNCTION IF EXISTS operations_active_pause(bigint, bigint);
DROP FUNCTION IF EXISTS operations_pause_scope_text(bigint, bigint);
DROP TRIGGER IF EXISTS region_pause_no_truncate ON operations_region_pause;
DROP TRIGGER IF EXISTS region_pause_append_only ON operations_region_pause;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0010_planning_location_eligibility"),
        ("platform_core", "0006_zones"),
        ("planning", "0013_zones_and_travel"),
        ("readiness", "0009_maintenance_gate"),
        ("execution", "0008_field_roles_at_run_hub"),
        ("accounts", "0008_safety_pause_role"),
        ("hub", "0015_guarded_creation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RegionPause",
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
                (
                    "kind",
                    models.CharField(choices=[("pause", "Pause"), ("lift", "Lift")], max_length=8),
                ),
                ("reason", models.TextField()),
                ("created_at", models.DateTimeField()),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "hub",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="platform_core.hub",
                    ),
                ),
                (
                    "pause",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lifts",
                        to="operations.regionpause",
                    ),
                ),
                (
                    "zone",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="platform_core.zone",
                    ),
                ),
            ],
            options={
                "db_table": "operations_region_pause",
                "ordering": ["id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("kind", "pause"), ("pause__isnull", True)),
                            models.Q(("kind", "lift"), ("pause__isnull", False)),
                            _connector="OR",
                        ),
                        name="region_pause_kind_link",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("kind", "lift")),
                        fields=("pause",),
                        name="uniq_region_pause_one_lift",
                    ),
                ],
            },
        ),
        migrations.RunSQL(SQL, SQL_REVERSE),
    ]
