"""v2 Phase 03B-1 (docs/13_v2_phase03_contract.md 10.H, 13.2, 13.4 item 3, 13.5): zones of homes and visits, and
the versioned travel matrix.

ZONES
  * planning_site.zone_id (nullable; Python field Site.zone_ref, next to the coarse text label Site.zone): only an
    ACTIVE zone can be set (trigger planning_site_zone_guard).
  * planning_visit.zone_id (nullable): the visit's DESTINATION zone, always its site's zone at plan time
    (trigger planning_visit_zone_guard): on INSERT the database sets it from the site, whatever the caller sent;
    a later change must re-sync it to the site's current zone (plan_visit does). A retired zone is refused for a
    new visit and for a re-sync. Existing visits keep NULL (unknown) and their recorded travel minutes.

TRAVEL MATRIX planning_travel_time(id, from_zone_id, to_zone_id, minutes, version, source, effective_from,
created_by_id, created_at):
  * ORDERED pairs: A -> B and B -> A are separate rows (a pair may also be A -> A, within one zone);
  * append-only (UPDATE, DELETE and TRUNCATE refused); unique (from_zone, to_zone, version);
  * trigger planning_travel_time_guard on INSERT, for every role: effective_from >= now() (no retroactive value);
    both zones active; version = the pair's previous version + 1 (1 for a new pair); effective_from not before
    the previous version's. So exactly one version per pair is in force at any time: the highest version
    whose effective_from has passed (planning_travel_minutes). A missing pair is unknown: no fallback.
  * written only by planning_set_travel_time(actor, from_zone, to_zone, minutes, source, effective_from,
    request_id) (SECURITY DEFINER, fixed search_path, EXECUTE for ops_app only): the actor must be an active admin
    or ops lead (grants taken FOR SHARE, so a revocation and the write are ordered); both zone rows FOR SHARE (a
    concurrent retirement is ordered); the pair serialized on a transaction advisory lock; one audit row
    (planning.set_travel_time). ops_app has SELECT only on the table and no sequence use.
  * planning_travel_minutes(from_zone, to_zone, at) -> (minutes, version_id, source): the version in force at
    ``at``; no row for a missing pair or a retired zone (its pairs are not offered).

planning_visitrevision.travel_version_id / travel_back_version_id (nullable): the matrix rows a saved plan used
(out and back). NULL for plans whose minutes were not from the matrix (a visit with no zone, as before 03B).

Reverse drops the functions, the triggers, the table and the columns.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

APP = settings.DB_APP_ROLE

SQL = rf"""
CREATE OR REPLACE FUNCTION planning_site_zone_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE z platform_core_zone;
BEGIN
  IF NEW.zone_id IS NULL OR (TG_OP = 'UPDATE' AND NEW.zone_id IS NOT DISTINCT FROM OLD.zone_id) THEN
    RETURN NEW;
  END IF;
  SELECT * INTO z FROM platform_core_zone WHERE id = NEW.zone_id;
  IF NOT z.active THEN
    PERFORM fleet_fail(format('Zone %s is retired; choose an active zone for home %s', z.code, NEW.code));
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_site_zone_guard() FROM PUBLIC;
CREATE TRIGGER site_zone_guard BEFORE INSERT OR UPDATE OF zone_id ON planning_site
  FOR EACH ROW EXECUTE FUNCTION planning_site_zone_guard();

-- A visit's zone is its site's zone at plan time: set by the database on insert, re-synced (never chosen) later.
CREATE OR REPLACE FUNCTION planning_visit_zone_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE site_zone bigint; z platform_core_zone;
BEGIN
  site_zone := (SELECT s.zone_id FROM planning_site s WHERE s.id = NEW.site_id);
  IF TG_OP = 'INSERT' THEN
    NEW.zone_id := site_zone;
  ELSIF NEW.zone_id IS NOT DISTINCT FROM OLD.zone_id THEN
    RETURN NEW;
  ELSIF NEW.zone_id IS DISTINCT FROM site_zone THEN
    PERFORM fleet_fail(format('Visit %s: its zone is always its home''s zone; it cannot be set to another one',
                              NEW.code));
  END IF;
  IF NEW.zone_id IS NOT NULL THEN
    SELECT * INTO z FROM platform_core_zone WHERE id = NEW.zone_id;
    IF NOT z.active THEN
      PERFORM fleet_fail(format('Zone %s is retired; new visits cannot use it. Move the home to an active zone',
                                z.code));
    END IF;
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_visit_zone_guard() FROM PUBLIC;
CREATE TRIGGER visit_zone_guard BEFORE INSERT OR UPDATE OF zone_id ON planning_visit
  FOR EACH ROW EXECUTE FUNCTION planning_visit_zone_guard();

-- --------------------------------------------------------------------------------------------- travel matrix
CREATE TRIGGER travel_time_append_only BEFORE UPDATE OR DELETE ON planning_travel_time
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER travel_time_no_truncate BEFORE TRUNCATE ON planning_travel_time
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();

CREATE OR REPLACE FUNCTION planning_travel_time_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE prev planning_travel_time;
BEGIN
  IF NEW.effective_from IS NULL OR NEW.effective_from < now() THEN
    PERFORM fleet_fail('A travel time takes effect now or later, never in the past: past dispatch decisions are '
                       'never rewritten');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM platform_core_zone z WHERE z.id = NEW.from_zone_id AND z.active)
     OR NOT EXISTS (SELECT 1 FROM platform_core_zone z WHERE z.id = NEW.to_zone_id AND z.active) THEN
    PERFORM fleet_fail('A travel time needs two active zones; a retired zone keeps its history only');
  END IF;
  SELECT * INTO prev FROM planning_travel_time t
  WHERE t.from_zone_id = NEW.from_zone_id AND t.to_zone_id = NEW.to_zone_id ORDER BY t.version DESC LIMIT 1;
  IF NEW.version IS DISTINCT FROM coalesce(prev.version, 0) + 1 THEN
    PERFORM fleet_fail(format('The next version of this pair is %s', coalesce(prev.version, 0) + 1));
  END IF;
  IF prev.id IS NOT NULL AND NEW.effective_from < prev.effective_from THEN
    PERFORM fleet_fail('A new travel time cannot take effect before the version it replaces');
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION planning_travel_time_guard() FROM PUBLIC;
CREATE TRIGGER travel_time_guard BEFORE INSERT ON planning_travel_time
  FOR EACH ROW EXECUTE FUNCTION planning_travel_time_guard();

-- The version of an ordered pair in force at p_at; no row when the pair is missing or a zone is retired.
CREATE OR REPLACE FUNCTION planning_travel_minutes(p_from bigint, p_to bigint, p_at timestamptz)
RETURNS TABLE (minutes integer, version_id bigint, source text)
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT t.minutes, t.id, t.source::text FROM planning_travel_time t
  JOIN platform_core_zone f ON f.id = t.from_zone_id AND f.active
  JOIN platform_core_zone z ON z.id = t.to_zone_id AND z.active
  WHERE t.from_zone_id = p_from AND t.to_zone_id = p_to AND t.effective_from <= p_at
  ORDER BY t.version DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION planning_travel_minutes(bigint, bigint, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION planning_travel_minutes(bigint, bigint, timestamptz) TO {APP};

CREATE OR REPLACE FUNCTION planning_set_travel_time(p_actor bigint, p_from bigint, p_to bigint, p_minutes integer,
                                                    p_source text, p_effective_from timestamptz, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE f platform_core_zone; t platform_core_zone; eff timestamptz := coalesce(p_effective_from, now());
        nxt integer; new_id bigint;
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['admin', 'ops_lead']) THEN
    PERFORM hub_deny('Only the workspace admin or the ops lead can set a travel time');
  END IF;
  SELECT * INTO f FROM platform_core_zone WHERE id = p_from FOR SHARE;
  IF NOT FOUND THEN PERFORM fleet_fail('Choose the zone the trip starts from'); END IF;
  SELECT * INTO t FROM platform_core_zone WHERE id = p_to FOR SHARE;
  IF NOT FOUND THEN PERFORM fleet_fail('Choose the zone the trip goes to'); END IF;
  -- admin and ops lead are regional roles: the grants are taken FOR SHARE (ordered against a revocation)
  IF NOT hub_actor_has_at(p_actor, ARRAY['admin', 'ops_lead'], f.hub_id) THEN
    PERFORM hub_deny('Only the workspace admin or the ops lead can set a travel time');
  END IF;
  IF NOT f.active OR NOT t.active THEN
    PERFORM fleet_fail(format('Zone %s is retired; its travel times keep their history and cannot change',
                              CASE WHEN f.active THEN t.code ELSE f.code END));
  END IF;
  IF p_minutes IS NULL OR p_minutes < 1 OR p_minutes > 1440 THEN
    PERFORM fleet_fail('Travel time is a whole number of minutes from 1 to 1440');
  END IF;
  IF p_source IS NULL OR p_source NOT IN ('company', 'demo') THEN
    PERFORM fleet_fail('Say where the value comes from: a company value or a demo value');
  END IF;
  IF eff < now() THEN
    PERFORM fleet_fail('A travel time takes effect now or later, never in the past: past dispatch decisions are '
                       'never rewritten');
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(format('planning_travel_time:%s:%s', f.id, t.id), 0));
  nxt := coalesce((SELECT max(x.version) FROM planning_travel_time x
                   WHERE x.from_zone_id = f.id AND x.to_zone_id = t.id), 0) + 1;
  INSERT INTO planning_travel_time (from_zone_id, to_zone_id, minutes, version, source, effective_from,
                                    created_by_id, created_at)
  VALUES (f.id, t.id, p_minutes, nxt, p_source, eff, p_actor, clock_timestamp())
  RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'planning.set_travel_time', 'travel_time', new_id::text,
                    jsonb_build_object('from_zone', f.code, 'from_hub', fleet_hub_code(f.hub_id), 'to_zone', t.code,
                                       'to_hub', fleet_hub_code(t.hub_id), 'minutes', p_minutes,
                                       'source', p_source, 'effective_from', eff),
                    jsonb_build_object('travel_time_id', new_id, 'version', nxt), p_rid);
  RETURN new_id;
END $fn$;
REVOKE ALL ON FUNCTION planning_set_travel_time(bigint, bigint, bigint, integer, text, timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION planning_set_travel_time(bigint, bigint, bigint, integer, text, timestamptz, text) TO {APP};

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON planning_travel_time FROM {APP};
GRANT SELECT ON planning_travel_time TO {APP};
REVOKE ALL ON SEQUENCE planning_travel_time_id_seq FROM {APP};
"""

SQL_REVERSE = """
DROP FUNCTION IF EXISTS planning_set_travel_time(bigint, bigint, bigint, integer, text, timestamptz, text);
DROP FUNCTION IF EXISTS planning_travel_minutes(bigint, bigint, timestamptz);
DROP TRIGGER IF EXISTS travel_time_guard ON planning_travel_time;
DROP FUNCTION IF EXISTS planning_travel_time_guard();
DROP TRIGGER IF EXISTS travel_time_no_truncate ON planning_travel_time;
DROP TRIGGER IF EXISTS travel_time_append_only ON planning_travel_time;
DROP TRIGGER IF EXISTS visit_zone_guard ON planning_visit;
DROP FUNCTION IF EXISTS planning_visit_zone_guard();
DROP TRIGGER IF EXISTS site_zone_guard ON planning_site;
DROP FUNCTION IF EXISTS planning_site_zone_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0012_future_planning_location"),
        ("platform_core", "0006_zones"),
        ("hub", "0015_guarded_creation"),  # hub_actor_has, hub_deny, hub_audit
        ("fleet", "0015_location_and_transfer_sql"),  # fleet_hub_code
        ("accounts", "0008_safety_pause_role"),  # hub_actor_has_at
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="site",
            name="zone_ref",
            field=models.ForeignKey(
                blank=True,
                db_column="zone_id",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="platform_core.zone",
            ),
        ),
        migrations.AddField(
            model_name="visit",
            name="zone",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="platform_core.zone",
            ),
        ),
        migrations.CreateModel(
            name="TravelTime",
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
                ("minutes", models.PositiveIntegerField()),
                ("version", models.PositiveIntegerField()),
                (
                    "source",
                    models.CharField(
                        choices=[("company", "Company value"), ("demo", "Demo value")],
                        max_length=10,
                    ),
                ),
                ("effective_from", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "from_zone",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="platform_core.zone",
                    ),
                ),
                (
                    "to_zone",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="platform_core.zone",
                    ),
                ),
            ],
            options={
                "db_table": "planning_travel_time",
            },
        ),
        migrations.AddField(
            model_name="visitrevision",
            name="travel_back_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="planning.traveltime",
            ),
        ),
        migrations.AddField(
            model_name="visitrevision",
            name="travel_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="planning.traveltime",
            ),
        ),
        migrations.AddConstraint(
            model_name="traveltime",
            constraint=models.UniqueConstraint(
                fields=("from_zone", "to_zone", "version"),
                name="uniq_travel_time_pair_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="traveltime",
            constraint=models.CheckConstraint(
                condition=models.Q(("minutes__gte", 1), ("minutes__lte", 1440)),
                name="travel_time_minutes_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="traveltime",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="travel_time_version_positive",
            ),
        ),
        migrations.RunSQL(SQL, SQL_REVERSE),
    ]
