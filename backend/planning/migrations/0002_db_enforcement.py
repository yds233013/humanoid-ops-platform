"""Planning invariants enforced by PostgreSQL itself (the runtime role cannot bypass them).

- Reservations: EXCLUDE USING gist (resource, [start, end)) for every unreleased reservation. Overlaps are
  rejected even for direct SQL and concurrent transactions; half-open intervals so back-to-back is fine.
  Reservations are never deleted; the only change is a single release.
- Append-only: assessments, assessment approvals, visit revisions, resources, persons, trip cargo.
- Consent: a single withdrawal transition; insert/withdraw bump the site's guard version.
- Site: identity immutable, guard version owned by the database.
- Visit: status transitions validated; a planned visit's revision cannot be 'blocked'; confirmed needs
  'feasible'; cancelled is terminal and needs every reservation of the visit released.
- Trip cargo: count <= max_robots_per_trip (deployment setting) and <= the vehicle's KNOWN capacity, and the
  vehicle must have a recorded restraint kit. Unknown capacity refuses cargo.

Lock graph (documented in docs/02_lock_graph.md): commands take role grants -> sites -> manifest patterns ->
robots -> persons -> vehicles -> trips -> visits, in that order. Triggers here only touch rows the calling
command already holds (site bump, trip row) or rows lower in the order (trip lock inside cargo insert).
"""

from django.db import migrations

APPEND_ONLY = [
    "planning_siteassessment",
    "planning_assessmentapproval",
    "planning_visitrevision",
    "planning_resource",
    "planning_person",
    "planning_tripcargo",
]

SQL = r"""
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE planning_reservation ADD CONSTRAINT reservation_no_overlap
  EXCLUDE USING gist (resource_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)
  WHERE (released_at IS NULL);

{append_only}

-- ---------- reservations: single release only ----------
CREATE OR REPLACE FUNCTION planning_reservation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('reservations cannot be deleted; release them'); END IF;
  IF OLD.released_at IS NOT NULL THEN PERFORM fleet_fail('reservation already released'); END IF;
  IF NEW.released_at IS NULL OR (NEW.resource_id, NEW.revision_id, NEW.purpose, NEW.starts_at, NEW.ends_at)
     IS DISTINCT FROM (OLD.resource_id, OLD.revision_id, OLD.purpose, OLD.starts_at, OLD.ends_at) THEN
    PERFORM fleet_fail('the only allowed change to a reservation is its release');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER reservation_guard BEFORE UPDATE OR DELETE ON planning_reservation
  FOR EACH ROW EXECUTE FUNCTION planning_reservation_guard();

CREATE OR REPLACE FUNCTION planning_trip_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('trips cannot be deleted; release them'); END IF;
  IF OLD.released_at IS NOT NULL OR NEW.released_at IS NULL OR
     (NEW.revision_id, NEW.vehicle_id, NEW.depart_at, NEW.return_at)
     IS DISTINCT FROM (OLD.revision_id, OLD.vehicle_id, OLD.depart_at, OLD.return_at) THEN
    PERFORM fleet_fail('the only allowed change to a trip is its release');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trip_guard BEFORE UPDATE OR DELETE ON planning_vehicletrip
  FOR EACH ROW EXECUTE FUNCTION planning_trip_guard();

-- ---------- trip cargo: configurable limit and known vehicle capacity ----------
CREATE OR REPLACE FUNCTION planning_cargo_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t planning_vehicletrip; v planning_vehicle; n int; lim int;
BEGIN
  SELECT * INTO t FROM planning_vehicletrip WHERE id = NEW.trip_id FOR UPDATE;  -- serialize cargo per trip
  IF t.released_at IS NOT NULL THEN PERFORM fleet_fail('trip already released'); END IF;
  SELECT * INTO v FROM planning_vehicle WHERE id = t.vehicle_id;
  IF v.robot_capacity IS NULL THEN PERFORM fleet_fail('vehicle robot capacity is unknown'); END IF;
  IF coalesce(v.restraint_kit, '') = '' THEN PERFORM fleet_fail('vehicle restraint kit is not recorded'); END IF;
  lim := coalesce(fleet_setting('max_robots_per_trip'), '1')::int;
  n := (SELECT count(*) FROM planning_tripcargo WHERE trip_id = NEW.trip_id) + 1;
  IF n > lim THEN PERFORM fleet_fail(format('trip would carry %s robots; the limit is %s', n, lim)); END IF;
  IF n > v.robot_capacity THEN PERFORM fleet_fail('trip exceeds the vehicle''s robot capacity'); END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER cargo_guard BEFORE INSERT ON planning_tripcargo
  FOR EACH ROW EXECUTE FUNCTION planning_cargo_guard();

-- ---------- site ----------
CREATE OR REPLACE FUNCTION planning_site_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('sites cannot be deleted'); END IF;
  IF (NEW.code, NEW.created_by_id, NEW.created_at) IS DISTINCT FROM (OLD.code, OLD.created_by_id, OLD.created_at) THEN
    PERFORM fleet_fail('site identity is immutable');
  END IF;
  NEW.version := OLD.version + 1;
  RETURN NEW;
END $$;
CREATE TRIGGER site_guard BEFORE UPDATE OR DELETE ON planning_site
  FOR EACH ROW EXECUTE FUNCTION planning_site_guard();

CREATE OR REPLACE FUNCTION planning_bump_site() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN UPDATE planning_site SET version = version WHERE id = NEW.site_id; RETURN NEW; END $$;
CREATE TRIGGER assessment_bump AFTER INSERT ON planning_siteassessment
  FOR EACH ROW EXECUTE FUNCTION planning_bump_site();

CREATE OR REPLACE FUNCTION planning_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT (has_active_role(NEW.approver_id, 'ops_lead') OR has_active_role(NEW.approver_id, 'engineering_lead')) THEN
    PERFORM fleet_fail('assessment approval requires ops_lead or engineering_lead');
  END IF;
  UPDATE planning_site SET version = version
   WHERE id = (SELECT site_id FROM planning_siteassessment WHERE id = NEW.assessment_id);
  RETURN NEW;
END $$;
CREATE TRIGGER approval_guard BEFORE INSERT ON planning_assessmentapproval
  FOR EACH ROW EXECUTE FUNCTION planning_approval_guard();

-- ---------- consent: single withdrawal, bumps site ----------
CREATE OR REPLACE FUNCTION planning_consent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('consent records cannot be deleted; withdraw them'); END IF;
  IF TG_OP = 'UPDATE' THEN
    IF OLD.withdrawn_at IS NOT NULL OR NEW.withdrawn_at IS NULL OR
       (NEW.site_id, NEW.participant_ref, NEW.form_version, NEW.scope, NEW.evidence_ref, NEW.verified_by_id,
        NEW.verified_at) IS DISTINCT FROM
       (OLD.site_id, OLD.participant_ref, OLD.form_version, OLD.scope, OLD.evidence_ref, OLD.verified_by_id,
        OLD.verified_at) THEN
      PERFORM fleet_fail('the only allowed change to a consent record is its withdrawal');
    END IF;
  END IF;
  UPDATE planning_site SET version = version WHERE id = NEW.site_id;
  RETURN NEW;
END $$;
CREATE TRIGGER consent_guard BEFORE INSERT OR UPDATE OR DELETE ON planning_consentrecord
  FOR EACH ROW EXECUTE FUNCTION planning_consent_guard();

-- ---------- qualification: single revoke ----------
CREATE OR REPLACE FUNCTION planning_qualification_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('qualifications cannot be deleted; revoke them'); END IF;
  IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR
     (NEW.person_id, NEW.kind, NEW.valid_from, NEW.valid_to, NEW.granted_by_id, NEW.granted_at)
     IS DISTINCT FROM (OLD.person_id, OLD.kind, OLD.valid_from, OLD.valid_to, OLD.granted_by_id, OLD.granted_at) THEN
    PERFORM fleet_fail('the only allowed change to a qualification is its revocation');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER qualification_guard BEFORE UPDATE OR DELETE ON planning_qualification
  FOR EACH ROW EXECUTE FUNCTION planning_qualification_guard();

-- ---------- visit status ----------
CREATE OR REPLACE FUNCTION planning_visit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE feas text;
BEGIN
  IF TG_OP = 'DELETE' THEN PERFORM fleet_fail('visits cannot be deleted; cancel them'); END IF;
  IF (NEW.code, NEW.site_id, NEW.created_by_id, NEW.created_at) IS DISTINCT FROM
     (OLD.code, OLD.site_id, OLD.created_by_id, OLD.created_at) THEN
    PERFORM fleet_fail('visit identity is immutable');
  END IF;
  IF OLD.status = 'cancelled' THEN PERFORM fleet_fail('a cancelled visit cannot change'); END IF;
  IF NEW.status <> OLD.status AND NOT ((OLD.status, NEW.status) IN (
      ('requested','planned'), ('requested','cancelled'), ('planned','confirmed'), ('planned','requested'),
      ('planned','cancelled'), ('confirmed','planned'), ('confirmed','requested'), ('confirmed','cancelled'))) THEN
    PERFORM fleet_fail(format('visit status %s -> %s is not allowed', OLD.status, NEW.status));
  END IF;
  IF NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM planning_visitrevision r WHERE r.id = NEW.current_revision_id AND r.visit_id = NEW.id) THEN
    PERFORM fleet_fail('revision belongs to a different visit');
  END IF;
  feas := (SELECT feasibility FROM planning_visitrevision WHERE id = NEW.current_revision_id);
  IF NEW.status IN ('planned', 'confirmed') AND feas = 'blocked' THEN
    PERFORM fleet_fail('a blocked plan cannot be planned or confirmed');
  END IF;
  IF NEW.status = 'confirmed' AND feas IS DISTINCT FROM 'feasible' THEN
    PERFORM fleet_fail('only a planning-feasible revision can be confirmed');
  END IF;
  IF NEW.status = 'cancelled' AND EXISTS (
       SELECT 1 FROM planning_reservation res JOIN planning_visitrevision r ON r.id = res.revision_id
       WHERE r.visit_id = NEW.id AND res.released_at IS NULL) THEN
    PERFORM fleet_fail('release every reservation before cancelling');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER visit_guard BEFORE UPDATE OR DELETE ON planning_visit
  FOR EACH ROW EXECUTE FUNCTION planning_visit_guard();

CREATE OR REPLACE FUNCTION planning_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.revision_no <> coalesce((SELECT max(revision_no) FROM planning_visitrevision WHERE visit_id = NEW.visit_id), 0) + 1 THEN
    PERFORM fleet_fail('revisions must be numbered consecutively');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER revision_guard BEFORE INSERT ON planning_visitrevision
  FOR EACH ROW EXECUTE FUNCTION planning_revision_guard();

CREATE SEQUENCE IF NOT EXISTS planning_visit_code_seq;
GRANT USAGE, SELECT ON SEQUENCE planning_visit_code_seq TO {app};
INSERT INTO fleet_deploymentsetting (key, value) VALUES ('max_robots_per_trip', '1') ON CONFLICT (key) DO NOTHING;
"""


def _sql() -> str:
    from django.conf import settings

    parts = []
    for t in APPEND_ONLY:
        parts.append(
            f"CREATE TRIGGER {t}_no_update_delete BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_mutation();\n"
            f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();"
        )
    return SQL.replace("{append_only}", "\n".join(parts)).replace("{app}", settings.DB_APP_ROLE)


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0001_initial"),
        ("fleet", "0004_fail_closed_holds_and_inspection_causality"),
    ]
    operations = [migrations.RunSQL(_sql(), reverse_sql=migrations.RunSQL.noop)]
