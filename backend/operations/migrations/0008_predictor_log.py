"""v2 02D predictor log (docs/11_v2_phase02_contract.md section 2 "Predictive models: candidate predictors are
logged only", 4 milestone 02D, 8.8). Additive and reversible. SYNTHETIC demo data only.

``operations_predictor_observation``: one row per departure with the candidate signals as they stood at the
departure time (battery at start, open holds, open incidents, overdue maintenance), their source references and
the time the row was written. OBSERVATIONAL ONLY: no planning, readiness, fleet or hub code, view or function
reads it, so it cannot change eligibility, authorization, holds or maintenance requirements.

WRITE BOUNDARY. The runtime role has SELECT only (no INSERT, UPDATE, DELETE, TRUNCATE, no sequence use). The
table is append-only for every role (reject_mutation for UPDATE, DELETE and TRUNCATE). The ONLY writer is
``operations_log_predictor_signals()``: SECURITY DEFINER, owned by the schema owner, fixed search_path, EXECUTE
revoked from PUBLIC and granted to nobody, so only the owner (the ``log_predictor_signals`` management command,
run as the owner) can call it. It takes no actor (nothing to forge): it is a system logger, audited with a NULL
actor. It logs every departure not yet logged, idempotently (ON CONFLICT DO NOTHING), and writes nothing else
but its one audit row.

Signals, all as of the departure time ``departed_at``:
  battery      the latest robot_soc_reading of the robot observed AND recorded by departed_at (value, id,
               observed_at, source); none -> 'unknown' with no value. Never estimated.
  open_holds   holds on the robot, or on the configuration pattern of the AUTHORIZED manifest, placed by
               departed_at and not released by then (blocking or not).
  open_incidents incidents of the robot reported by departed_at with no 'closed' status change by then.
  maintenance  fleet_maintenance_status_set(robot, current mode, departed_at): overdue = any requirement in
               state 'overdue'; every (requirement, state, rule) is kept. The status function reads the robot's
               CURRENT manifest; configuration history is not reconstructed.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

APP = settings.DB_APP_ROLE
TABLE = "operations_predictor_observation"

FUNCTION = r"""
CREATE OR REPLACE FUNCTION operations_log_predictor_signals() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE n integer; now_ts timestamptz := clock_timestamp(); prod boolean := fleet_production_mode();
BEGIN
  INSERT INTO operations_predictor_observation (
    departure_id, visit_id, robot_id, departed_at, battery_state, battery_soc_pct, battery_reading_id,
    battery_observed_at, battery_source, open_holds, open_incidents, maintenance_overdue, maintenance_states,
    source_refs, observed_at)
  SELECT t.id, t.visit_id, t.robot_id, t.departed_at,
         CASE WHEN bat.id IS NULL THEN 'unknown' ELSE 'known' END, bat.soc_pct, bat.id, bat.observed_at,
         coalesce(bat.source, ''), hs.n, ic.n, ms.overdue, ms.states,
         jsonb_build_object('departure', t.id, 'authorization', t.authorization_id, 'battery_reading', bat.id,
                            'holds', to_jsonb(hs.codes), 'incidents', to_jsonb(ic.codes),
                            'maintenance_rules', ms.rules),
         now_ts
  FROM (
    SELECT d.id, d.visit_id, d.robot_id, d.departed_at, d.authorization_id, m.pattern_id
    FROM readiness_departureevent d
    JOIN readiness_departureauthorization a ON a.id = d.authorization_id
    LEFT JOIN fleet_configurationmanifest m ON m.id = a.manifest_id
    WHERE NOT EXISTS (SELECT 1 FROM operations_predictor_observation o WHERE o.departure_id = d.id)
  ) t
  LEFT JOIN LATERAL (
    SELECT s.id, s.soc_pct, s.observed_at, s.source FROM robot_soc_reading s
    WHERE s.robot_id = t.robot_id AND s.observed_at <= t.departed_at AND s.recorded_at <= t.departed_at
    ORDER BY s.observed_at DESC, s.id DESC LIMIT 1
  ) bat ON true
  CROSS JOIN LATERAL (
    SELECT count(*)::int AS n, coalesce(array_agg(h.code::text ORDER BY h.code), '{}'::text[]) AS codes
    FROM fleet_hold h
    WHERE (h.robot_id = t.robot_id OR (h.scope = 'manifest_pattern' AND h.pattern_id = t.pattern_id))
      AND h.placed_at <= t.departed_at AND (h.released_at IS NULL OR h.released_at > t.departed_at)
  ) hs
  CROSS JOIN LATERAL (
    SELECT count(*)::int AS n, coalesce(array_agg(i.code::text ORDER BY i.code), '{}'::text[]) AS codes
    FROM execution_incident i
    WHERE i.robot_id = t.robot_id AND i.reported_at <= t.departed_at
      AND NOT EXISTS (SELECT 1 FROM execution_incidentstatuschange c
                      WHERE c.incident_id = i.id AND c.to_status = 'closed' AND c.recorded_at <= t.departed_at)
  ) ic
  CROSS JOIN LATERAL (
    SELECT coalesce(bool_or(s.state = 'overdue'), false) AS overdue,
           coalesce(jsonb_agg(jsonb_build_object('requirement', s.requirement_code, 'state', s.state,
                                                 'rule_id', s.rule_id, 'rule_version', s.rule_version)
                              ORDER BY s.requirement_code NULLS FIRST), '[]'::jsonb) AS states,
           coalesce(jsonb_agg(s.rule_id ORDER BY s.rule_id) FILTER (WHERE s.rule_id IS NOT NULL), '[]'::jsonb)
             AS rules
    FROM fleet_maintenance_status_set(ARRAY[t.robot_id], prod, t.departed_at) s
  ) ms
  ORDER BY t.id
  ON CONFLICT (departure_id) DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  INSERT INTO platform_core_auditevent (occurred_at, actor_id, command, outcome, entity_type, entity_id, payload,
                                        result, reason, request_id)
  VALUES (clock_timestamp(), NULL, 'operations.log_predictor_signals', 'committed', 'predictor_observation', '',
          '{}'::jsonb, jsonb_build_object('logged', n), 'observational only; not used for any decision', '');
  RETURN n;
END $fn$;
"""

FORWARD = (
    f"""
CREATE TRIGGER predictor_obs_append_only BEFORE UPDATE OR DELETE ON {TABLE}
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER predictor_obs_no_truncate BEFORE TRUNCATE ON {TABLE}
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE ALL ON {TABLE} FROM {APP};
GRANT SELECT ON {TABLE} TO {APP};
REVOKE ALL ON SEQUENCE {TABLE}_id_seq FROM {APP};
"""
    + FUNCTION
    + """
REVOKE ALL ON FUNCTION operations_log_predictor_signals() FROM PUBLIC;
"""
)

REVERSE = f"""
DROP FUNCTION IF EXISTS operations_log_predictor_signals();
DROP TRIGGER IF EXISTS predictor_obs_append_only ON {TABLE};
DROP TRIGGER IF EXISTS predictor_obs_no_truncate ON {TABLE};
"""


class Migration(migrations.Migration):
    dependencies = [
        ("fleet", "0010_maintenance_sql"),
        ("hub", "0006_maintenance_tasks"),
        ("operations", "0007_maintenance_gate"),
        ("planning", "0008_maintenance_gate"),
        ("readiness", "0009_maintenance_gate"),
    ]

    operations = [
        migrations.CreateModel(
            name="PredictorObservation",
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
                ("departed_at", models.DateTimeField()),
                ("battery_state", models.CharField(max_length=8)),
                (
                    "battery_soc_pct",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                ("battery_observed_at", models.DateTimeField(blank=True, null=True)),
                ("battery_source", models.CharField(blank=True, max_length=24)),
                ("open_holds", models.PositiveIntegerField()),
                ("open_incidents", models.PositiveIntegerField()),
                ("maintenance_overdue", models.BooleanField()),
                ("maintenance_states", models.JSONField(default=list)),
                ("source_refs", models.JSONField(default=dict)),
                ("observed_at", models.DateTimeField()),
                (
                    "battery_reading",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="hub.robotsocreading",
                    ),
                ),
                (
                    "departure",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="readiness.departureevent",
                    ),
                ),
                (
                    "robot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="fleet.robot",
                    ),
                ),
                (
                    "visit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="planning.visit",
                    ),
                ),
            ],
            options={
                "db_table": "operations_predictor_observation",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("battery_reading__isnull", False),
                                ("battery_soc_pct__isnull", False),
                                ("battery_state", "known"),
                            ),
                            models.Q(
                                ("battery_reading__isnull", True),
                                ("battery_soc_pct__isnull", True),
                                ("battery_state", "unknown"),
                            ),
                            _connector="OR",
                        ),
                        name="predictor_obs_battery_state",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("battery_soc_pct__isnull", True),
                            ("battery_soc_pct__lte", 100),
                            _connector="OR",
                        ),
                        name="predictor_obs_battery_pct",
                    ),
                ],
            },
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
