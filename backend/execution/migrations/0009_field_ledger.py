"""v2 Phase 03C-1 (docs/13_v2_phase03_contract.md 14.3, 14.5, 14.8; sections 3 I-2..I-6, 6, 10.D/E/F): the
field-command ledger.

TABLES (append-only: UPDATE, DELETE and TRUNCATE refused by trigger; ops_app has SELECT only, no sequence use):
  execution_field_command        one durable decision per key: confirmed | rejected | abandoned (a barrier).
                                 ``seq`` is the sequence the row DECIDES (null when none); ``sent_seq`` what the
                                 device sent. Unique (run, generation, seq) where seq is set; one barrier per
                                 (run, generation).
  execution_field_generation     generation g >= 2 of a run's ordinary stream, opened by the barrier of g - 1
                                 (generation 1 is implicit).
  execution_field_device_report  the device's last reported unsent count (newest row wins).

READERS (plain, STABLE, fixed search_path, EXECUTE for ops_app; the run row must already be locked FOR UPDATE):
  execution_field_generation_now(run)            the current generation (highest row, else 1)
  execution_field_next_seq(run, generation)      1 + the highest decided seq of that generation
  execution_field_access(actor, run)             '' | NOT_YOUR_RUN (not crew and not the ops lead, or no such
                                                 user or run) | NOT_PERMITTED (no current ops_lead or
                                                 field_operator grant at the run's hub)
  execution_field_decide(actor, run, key, digest, channel, generation, seq) -> (decision, reason_code, ref_id)
      the ONE statement of the rules, used by field_ledger.claim and re-checked by execution_field_record:
        1. key known: identical digest, actor and run -> 'replay'; anything else -> rejected KEY_REUSED
        2. access (above), as a non-durable rejection
        3. protective -> 'apply' (no sequence rules)
        4. ordinary: an earlier generation with a barrier at or before seq -> AFTER_ABANDONED; any other
           generation than the current one -> STALE_GENERATION; seq = next -> 'apply'; seq > next -> 'held';
           seq < next -> SEQ_TAKEN (ref_id: the row that decided it)

WRITERS (SECURITY DEFINER, owned by ops_owner, fixed search_path, EXECUTE for ops_app only). Each refuses a NULL,
zero, unknown, inactive or role-less actor first (insufficient_privilege, DETAIL NOT_PERMITTED), before any lock;
then locks the run FOR UPDATE; then requires a crew member of the run or the (regional) ops lead, holding a
current ops_lead or field_operator grant at the run's hub (hub_actor_has_at: grants FOR SHARE).
  execution_field_record(actor, run, key, digest, device, channel, generation, seq, command, payload,
                         observed, time_flag, causal, outcome, reason_code, message, result) -> id
      outcome confirmed (only when the rule says 'apply') or rejected (a classified refusal: never KEY_REUSED,
      NOT_YOUR_RUN, RETRY or HELD). An ordinary row decides its seq only when the rule says 'apply'.
      received_at = now() (the transaction's start: database receipt), decided_at = clock_timestamp().
  execution_field_abandon(actor, run, generation, seq, reason, request_id) -> jsonb
      seq must be the next expected seq of the current generation. Writes the barrier row (outcome abandoned,
      reason ABANDONED, a server key), opens generation g + 1 and appends an audit row execution.field_abandon.
  execution_field_report_device(actor, run, device, unsent_count) -> id
Refusals carry the reason code in DETAIL (field_ledger maps them).

LOCKS (docs/evidence/03a_lock_graph.md section 5): the run FOR UPDATE first, then the actor's grants FOR SHARE,
then whatever the domain command takes.

Reverse drops the functions, the triggers and the tables.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

APP = settings.DB_APP_ROLE
TABLES = ("execution_field_command", "execution_field_generation", "execution_field_device_report")
FIELD_ROLES = "ARRAY['ops_lead', 'field_operator']"
# reason codes that are never stored: the key is already decided, the actor has no access, or nothing was decided
NOT_DURABLE = "('KEY_REUSED', 'NOT_YOUR_RUN', 'RETRY', 'HELD')"

APPEND_ONLY = "\n".join(
    f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} "
    f"FOR EACH ROW EXECUTE FUNCTION reject_mutation();\n"
    f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();"
    for t in TABLES
)
PRIVILEGES = "\n".join(
    f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {t} FROM {APP};\n"
    f"GRANT SELECT ON {t} TO {APP};\n"
    f"REVOKE ALL ON SEQUENCE {t}_id_seq FROM {APP};"
    for t in TABLES
)

SQL = rf"""
{APPEND_ONLY}

-- refusals with the reason code in DETAIL (field_ledger reads it)
CREATE OR REPLACE FUNCTION execution_field_deny(p_code text, p_msg text) RETURNS void
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN RAISE EXCEPTION '%', p_msg USING ERRCODE = 'insufficient_privilege', DETAIL = p_code; END $$;
REVOKE ALL ON FUNCTION execution_field_deny(text, text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION execution_field_fail(p_code text, p_msg text) RETURNS void
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN RAISE EXCEPTION '%', p_msg USING ERRCODE = 'check_violation', DETAIL = p_code; END $$;
REVOKE ALL ON FUNCTION execution_field_fail(text, text) FROM PUBLIC;

-- ------------------------------------------------------------------------------------------------ readers
CREATE OR REPLACE FUNCTION execution_field_generation_now(p_run bigint) RETURNS integer
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT max(g.generation) FROM execution_field_generation g WHERE g.run_id = p_run), 1)::integer
$$;
REVOKE ALL ON FUNCTION execution_field_generation_now(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execution_field_generation_now(bigint) TO {APP};

CREATE OR REPLACE FUNCTION execution_field_next_seq(p_run bigint, p_generation integer) RETURNS integer
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT (coalesce(max(c.seq), 0) + 1)::integer FROM execution_field_command c
  WHERE c.run_id = p_run AND c.generation = p_generation AND c.seq IS NOT NULL
$$;
REVOKE ALL ON FUNCTION execution_field_next_seq(bigint, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execution_field_next_seq(bigint, integer) TO {APP};

-- Who may use a run's ledger: a crew member of the run or the ops lead (a regional role), holding a current
-- ops_lead or field_operator grant at the run's hub. No lock (the writers re-check with hub_actor_has_at).
CREATE OR REPLACE FUNCTION execution_field_access(p_actor bigint, p_run bigint) RETURNS text
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT CASE
    WHEN p_actor IS NULL OR p_run IS NULL
         OR NOT EXISTS (SELECT 1 FROM auth_user u WHERE u.id = p_actor AND u.is_active)
         OR NOT EXISTS (SELECT 1 FROM execution_executionrun x WHERE x.id = p_run) THEN 'NOT_YOUR_RUN'
    WHEN NOT (execution_user_is_crew(p_run, p_actor) OR has_active_role(p_actor, 'ops_lead')) THEN 'NOT_YOUR_RUN'
    WHEN NOT (has_active_role_at(p_actor, 'ops_lead', execution_run_hub(p_run))
              OR has_active_role_at(p_actor, 'field_operator', execution_run_hub(p_run))) THEN 'NOT_PERMITTED'
    ELSE '' END
$$;
REVOKE ALL ON FUNCTION execution_field_access(bigint, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execution_field_access(bigint, bigint) TO {APP};

CREATE OR REPLACE FUNCTION execution_field_decide(p_actor bigint, p_run bigint, p_key uuid, p_digest text,
                                                  p_channel text, p_generation integer, p_seq integer)
RETURNS TABLE (decision text, reason_code text, ref_id bigint)
LANGUAGE plpgsql STABLE SET search_path = public, pg_temp AS $fn$
DECLARE c execution_field_command; acc text; cur_gen integer; nxt integer;
BEGIN
  -- 1. the key first: a replay returns the stored outcome before anything else; a changed identity never runs
  SELECT * INTO c FROM execution_field_command f WHERE f.key = p_key;
  IF FOUND THEN
    IF c.identity_digest = p_digest AND c.actor_id = p_actor AND c.run_id = p_run THEN
      RETURN QUERY SELECT 'replay'::text, ''::text, c.id;
    ELSE
      RETURN QUERY SELECT 'rejected'::text, 'KEY_REUSED'::text, c.id;
    END IF;
    RETURN;
  END IF;
  -- 2. run access
  acc := execution_field_access(p_actor, p_run);
  IF acc <> '' THEN RETURN QUERY SELECT 'rejected'::text, acc, NULL::bigint; RETURN; END IF;
  -- 3. the protective channel skips the sequence rules (10.D)
  IF p_channel = 'protective' AND p_seq IS NULL THEN
    RETURN QUERY SELECT 'apply'::text, ''::text, NULL::bigint; RETURN;
  END IF;
  IF p_channel IS DISTINCT FROM 'ordinary' OR p_generation IS NULL OR p_generation < 1 OR p_seq IS NULL
     OR p_seq < 1 THEN
    PERFORM execution_field_fail('INVALID', 'An ordinary step needs a generation and a sequence number from 1; '
                                            'a protective step has no sequence number');
  END IF;
  -- 4. generations and the abandonment barrier (14.8 item 4)
  cur_gen := execution_field_generation_now(p_run);
  IF p_generation < cur_gen THEN
    SELECT * INTO c FROM execution_field_command f
    WHERE f.run_id = p_run AND f.generation = p_generation AND f.outcome = 'abandoned';
    IF FOUND AND p_seq >= c.seq THEN
      RETURN QUERY SELECT 'rejected'::text, 'AFTER_ABANDONED'::text, c.id; RETURN;
    END IF;
  END IF;
  IF p_generation <> cur_gen THEN
    RETURN QUERY SELECT 'rejected'::text, 'STALE_GENERATION'::text, NULL::bigint; RETURN;
  END IF;
  -- 5. the sequence (14.3 items 3, 5 and 8)
  nxt := execution_field_next_seq(p_run, cur_gen);
  IF p_seq = nxt THEN
    RETURN QUERY SELECT 'apply'::text, ''::text, NULL::bigint;
  ELSIF p_seq > nxt THEN
    RETURN QUERY SELECT 'held'::text, ''::text, NULL::bigint;
  ELSE
    RETURN QUERY SELECT 'rejected'::text, 'SEQ_TAKEN'::text,
      (SELECT f.id FROM execution_field_command f WHERE f.run_id = p_run AND f.generation = cur_gen AND f.seq = p_seq);
  END IF;
END $fn$;
REVOKE ALL ON FUNCTION execution_field_decide(bigint, bigint, uuid, text, text, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execution_field_decide(bigint, bigint, uuid, text, text, integer, integer) TO {APP};

-- The writers' shared actor check: refused (insufficient_privilege) before any lock when the actor is NULL, zero,
-- unknown, inactive or holds no field role anywhere; then the run FOR UPDATE; then crew or ops lead, with a
-- current grant at the run's hub (grants FOR SHARE). Returns the locked run.
CREATE OR REPLACE FUNCTION execution_field_lock_for(p_actor bigint, p_run bigint) RETURNS execution_executionrun
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $fn$
DECLARE x execution_executionrun;
BEGIN
  IF NOT hub_actor_has(p_actor, {FIELD_ROLES}) THEN
    PERFORM execution_field_deny('NOT_PERMITTED', 'You have no field role');
  END IF;
  SELECT * INTO x FROM execution_executionrun WHERE id = p_run FOR UPDATE;
  IF NOT FOUND THEN
    PERFORM execution_field_deny('NOT_YOUR_RUN', 'Sign in as a crew member of this run or the ops lead');
  END IF;
  IF NOT (execution_user_is_crew(x.id, p_actor) OR hub_actor_has(p_actor, ARRAY['ops_lead'])) THEN
    PERFORM execution_field_deny('NOT_YOUR_RUN', 'Sign in as a crew member of this run or the ops lead');
  END IF;
  IF NOT hub_actor_has_at(p_actor, {FIELD_ROLES}, execution_run_hub(x.id)) THEN
    PERFORM execution_field_deny('NOT_PERMITTED', 'You no longer have a field role at this run''s hub');
  END IF;
  RETURN x;
END $fn$;
REVOKE ALL ON FUNCTION execution_field_lock_for(bigint, bigint) FROM PUBLIC;

-- ------------------------------------------------------------------------------------------------ writers
CREATE OR REPLACE FUNCTION execution_field_record(
  p_actor bigint, p_run bigint, p_key uuid, p_digest text, p_device text, p_channel text, p_generation integer,
  p_seq integer, p_command text, p_payload jsonb, p_observed timestamptz, p_time_flag text, p_causal jsonb,
  p_outcome text, p_reason text, p_message text, p_result jsonb) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE x execution_executionrun; d record; new_id bigint;
BEGIN
  x := execution_field_lock_for(p_actor, p_run);
  IF p_key IS NULL OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{{64}}$' THEN
    PERFORM execution_field_fail('INVALID', 'A field step needs its key and identity');
  END IF;
  IF coalesce(btrim(p_device), '') = '' OR length(p_device) > 64 THEN
    PERFORM execution_field_fail('INVALID', 'A field step needs its device');
  END IF;
  IF coalesce(btrim(p_command), '') = '' OR length(p_command) > 64 OR p_payload IS NULL
     OR jsonb_typeof(p_payload) <> 'object' THEN
    PERFORM execution_field_fail('INVALID', 'A field step needs its command and payload');
  END IF;
  IF p_outcome IS NULL OR p_outcome NOT IN ('confirmed', 'rejected') THEN
    PERFORM execution_field_fail('INVALID', 'A field step is confirmed or rejected; abandon a step to skip it');
  END IF;
  IF p_outcome = 'confirmed' AND coalesce(p_reason, '') <> '' THEN
    PERFORM execution_field_fail('INVALID', 'A confirmed step has no reason code');
  END IF;
  IF p_outcome = 'rejected' AND (p_reason IS NULL OR p_reason !~ '^[A-Z][A-Z0-9_]{{1,39}}$'
                                 OR p_reason IN {NOT_DURABLE}) THEN
    PERFORM execution_field_fail('INVALID', 'A stored rejection needs a classified reason code');
  END IF;
  IF p_time_flag IS NULL OR p_time_flag NOT IN ('ok', 'missing', 'out_of_range')
     OR (p_time_flag = 'ok' AND p_observed IS NULL) OR (p_time_flag = 'missing' AND p_observed IS NOT NULL) THEN
    PERFORM execution_field_fail('INVALID', 'The phone time and its flag do not match');
  END IF;

  SELECT * INTO d FROM execution_field_decide(p_actor, x.id, p_key, p_digest, p_channel, p_generation, p_seq);
  IF d.decision = 'replay' OR d.reason_code = 'KEY_REUSED' THEN
    RAISE EXCEPTION 'This step''s key is already decided' USING ERRCODE = 'unique_violation', DETAIL = 'KEY_REUSED';
  END IF;
  IF d.reason_code IN ('NOT_YOUR_RUN', 'NOT_PERMITTED') THEN  -- unreachable after the lock check; kept closed
    PERFORM execution_field_deny(d.reason_code, 'Not allowed on this run');
  END IF;
  IF p_outcome = 'confirmed' AND d.decision <> 'apply' THEN
    PERFORM execution_field_fail(coalesce(nullif(d.reason_code, ''), upper(d.decision)),
                                 'Only the next step of the current generation, or a protective step, can be '
                                 'confirmed');
  END IF;
  IF p_outcome = 'confirmed' AND p_channel = 'ordinary' AND p_time_flag = 'out_of_range' THEN
    PERFORM execution_field_fail('DEVICE_TIME_OUT_OF_BOUNDS', 'An ordinary step with an out-of-range phone time '
                                                              'cannot be confirmed');
  END IF;
  IF p_outcome = 'rejected' AND d.decision = 'apply'
     AND p_reason IN ('SEQ_TAKEN', 'STALE_GENERATION', 'AFTER_ABANDONED', 'ABANDONED') THEN
    PERFORM execution_field_fail('INVALID', 'This step is the next one: it was not refused by a sequence rule');
  END IF;

  INSERT INTO execution_field_command (key, identity_digest, actor_id, device_id, run_id, channel, generation, seq,
                                       sent_seq, command, payload, device_observed_at, device_time_flag, causal,
                                       received_at, decided_at, outcome, reason_code, message, result)
  VALUES (p_key, p_digest, p_actor, p_device, x.id, p_channel, p_generation,
          CASE WHEN p_channel = 'ordinary' AND d.decision = 'apply' THEN p_seq END,
          p_seq, p_command, p_payload, CASE WHEN p_time_flag = 'ok' THEN p_observed END, p_time_flag, p_causal,
          now(), clock_timestamp(), p_outcome, coalesce(p_reason, ''), coalesce(p_message, ''),
          coalesce(p_result, '{{}}'::jsonb))
  RETURNING id INTO new_id;
  RETURN new_id;
END $fn$;
REVOKE ALL ON FUNCTION execution_field_record(bigint, bigint, uuid, text, text, text, integer, integer, text, jsonb,
  timestamptz, text, jsonb, text, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execution_field_record(bigint, bigint, uuid, text, text, text, integer, integer, text,
  jsonb, timestamptz, text, jsonb, text, text, text, jsonb) TO {APP};

CREATE OR REPLACE FUNCTION execution_field_abandon(p_actor bigint, p_run bigint, p_generation integer,
                                                   p_seq integer, p_reason text, p_rid text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE x execution_executionrun; cur_gen integer; nxt integer; new_id bigint; new_key uuid; at timestamptz;
BEGIN
  x := execution_field_lock_for(p_actor, p_run);
  IF coalesce(btrim(p_reason), '') = '' OR length(btrim(p_reason)) > 200 THEN
    PERFORM execution_field_fail('REASON_REQUIRED', 'Say why this step is abandoned (up to 200 characters)');
  END IF;
  cur_gen := execution_field_generation_now(x.id);
  IF p_generation IS DISTINCT FROM cur_gen THEN
    PERFORM execution_field_fail('STALE_GENERATION', format('This run''s steps were already reset (now list %s). '
                                                            'Start again from what the server shows now', cur_gen));
  END IF;
  nxt := execution_field_next_seq(x.id, cur_gen);
  IF p_seq IS NULL OR p_seq < nxt THEN
    PERFORM execution_field_fail('SEQ_TAKEN', format('Step %s was already decided', p_seq));
  END IF;
  IF p_seq > nxt THEN
    PERFORM execution_field_fail('NOT_NEXT', format('Step %s is the first missing step: abandon that one', nxt));
  END IF;
  new_key := gen_random_uuid();
  at := clock_timestamp();
  INSERT INTO execution_field_command (key, identity_digest, actor_id, device_id, run_id, channel, generation, seq,
                                       sent_seq, command, payload, device_observed_at, device_time_flag, causal,
                                       received_at, decided_at, outcome, reason_code, message, result)
  VALUES (new_key, NULL, p_actor, '', x.id, 'ordinary', cur_gen, p_seq, p_seq, 'field.abandon',
          jsonb_build_object('reason', btrim(p_reason)), NULL, '', NULL, now(), at, 'abandoned', 'ABANDONED',
          btrim(p_reason), jsonb_build_object('generation_opened', cur_gen + 1))
  RETURNING id INTO new_id;
  INSERT INTO execution_field_generation (run_id, generation, barrier_id, opened_by_id, opened_at)
  VALUES (x.id, cur_gen + 1, new_id, p_actor, at);
  PERFORM hub_audit(p_actor, 'execution.field_abandon', 'execution_run', x.code,
                    jsonb_build_object('run', x.code, 'generation', cur_gen, 'seq', p_seq, 'reason', btrim(p_reason)),
                    jsonb_build_object('barrier_key', new_key, 'generation', cur_gen + 1), p_rid);
  RETURN jsonb_build_object('run', x.code, 'abandoned_generation', cur_gen, 'abandoned_seq', p_seq,
                            'barrier_key', new_key, 'decided_at', at, 'generation', cur_gen + 1,
                            'last_decided_seq', 0, 'next_expected_seq', 1);
END $fn$;
REVOKE ALL ON FUNCTION execution_field_abandon(bigint, bigint, integer, integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execution_field_abandon(bigint, bigint, integer, integer, text, text) TO {APP};

CREATE OR REPLACE FUNCTION execution_field_report_device(p_actor bigint, p_run bigint, p_device text,
                                                         p_unsent integer) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE x execution_executionrun; new_id bigint;
BEGIN
  x := execution_field_lock_for(p_actor, p_run);
  IF coalesce(btrim(p_device), '') = '' OR length(p_device) > 64 THEN
    PERFORM execution_field_fail('INVALID', 'A device report needs its device');
  END IF;
  IF p_unsent IS NULL OR p_unsent < 0 OR p_unsent > 10000 THEN
    PERFORM execution_field_fail('INVALID', 'The unsent count is a whole number from 0');
  END IF;
  INSERT INTO execution_field_device_report (device_id, run_id, actor_id, unsent_count, reported_at)
  VALUES (p_device, x.id, p_actor, p_unsent, clock_timestamp())
  RETURNING id INTO new_id;
  RETURN new_id;
END $fn$;
REVOKE ALL ON FUNCTION execution_field_report_device(bigint, bigint, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execution_field_report_device(bigint, bigint, text, integer) TO {APP};

{PRIVILEGES}
"""

SQL_REVERSE = """
DROP FUNCTION IF EXISTS execution_field_report_device(bigint, bigint, text, integer);
DROP FUNCTION IF EXISTS execution_field_abandon(bigint, bigint, integer, integer, text, text);
DROP FUNCTION IF EXISTS execution_field_record(bigint, bigint, uuid, text, text, text, integer, integer, text, jsonb,
  timestamptz, text, jsonb, text, text, text, jsonb);
DROP FUNCTION IF EXISTS execution_field_lock_for(bigint, bigint);
DROP FUNCTION IF EXISTS execution_field_decide(bigint, bigint, uuid, text, text, integer, integer);
DROP FUNCTION IF EXISTS execution_field_access(bigint, bigint);
DROP FUNCTION IF EXISTS execution_field_next_seq(bigint, integer);
DROP FUNCTION IF EXISTS execution_field_generation_now(bigint);
DROP FUNCTION IF EXISTS execution_field_fail(text, text);
DROP FUNCTION IF EXISTS execution_field_deny(text, text);
""" + "\n".join(
    f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t};\nDROP TRIGGER IF EXISTS {t}_append_only ON {t};"
    for t in TABLES
)


class Migration(migrations.Migration):
    dependencies = [
        ("execution", "0008_field_roles_at_run_hub"),
        ("accounts", "0008_safety_pause_role"),  # has_active_role_at, hub_actor_has_at
        ("hub", "0015_guarded_creation"),  # hub_actor_has, hub_audit
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FieldCommand",
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
                ("key", models.UUIDField(unique=True)),
                (
                    "identity_digest",
                    models.CharField(blank=True, max_length=64, null=True),
                ),
                ("device_id", models.CharField(blank=True, max_length=64)),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("ordinary", "Ordinary"),
                            ("protective", "Protective"),
                        ],
                        max_length=12,
                    ),
                ),
                ("generation", models.PositiveIntegerField(blank=True, null=True)),
                ("seq", models.PositiveIntegerField(blank=True, null=True)),
                ("sent_seq", models.PositiveIntegerField(blank=True, null=True)),
                ("command", models.CharField(max_length=64)),
                ("payload", models.JSONField(default=dict)),
                ("device_observed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "device_time_flag",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("ok", "Phone time within bounds"),
                            ("missing", "No phone time"),
                            ("out_of_range", "Phone clock out of range"),
                            ("", "Not applicable (abandonment)"),
                        ],
                        max_length=16,
                    ),
                ),
                ("causal", models.JSONField(blank=True, null=True)),
                ("received_at", models.DateTimeField()),
                ("decided_at", models.DateTimeField()),
                (
                    "outcome",
                    models.CharField(
                        choices=[
                            ("confirmed", "Confirmed"),
                            ("rejected", "Rejected"),
                            ("abandoned", "Abandoned"),
                        ],
                        max_length=10,
                    ),
                ),
                ("reason_code", models.CharField(blank=True, max_length=40)),
                ("message", models.TextField(blank=True)),
                ("result", models.JSONField(default=dict)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="field_commands",
                        to="execution.executionrun",
                    ),
                ),
            ],
            options={
                "db_table": "execution_field_command",
                "ordering": ["id"],
            },
        ),
        migrations.CreateModel(
            name="FieldDeviceReport",
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
                ("device_id", models.CharField(max_length=64)),
                ("unsent_count", models.PositiveIntegerField()),
                ("reported_at", models.DateTimeField()),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="execution.executionrun",
                    ),
                ),
            ],
            options={
                "db_table": "execution_field_device_report",
                "ordering": ["id"],
            },
        ),
        migrations.CreateModel(
            name="FieldGeneration",
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
                ("generation", models.PositiveIntegerField()),
                ("opened_at", models.DateTimeField()),
                (
                    "barrier",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="execution.fieldcommand",
                    ),
                ),
                (
                    "opened_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="execution.executionrun",
                    ),
                ),
            ],
            options={
                "db_table": "execution_field_generation",
                "ordering": ["id"],
            },
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.UniqueConstraint(
                condition=models.Q(("seq__isnull", False)),
                fields=("run", "generation", "seq"),
                name="uniq_field_command_run_generation_seq",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.UniqueConstraint(
                condition=models.Q(("outcome", "abandoned")),
                fields=("run", "generation"),
                name="uniq_field_command_one_barrier_per_generation",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(("channel__in", ["ordinary", "protective"])),
                name="field_command_channel",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(("outcome__in", ["confirmed", "rejected", "abandoned"])),
                name="field_command_outcome",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(("device_time_flag__in", ["ok", "missing", "out_of_range", ""])),
                name="field_command_time_flag",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("channel", "protective"),
                        ("sent_seq__isnull", True),
                        ("seq__isnull", True),
                    ),
                    models.Q(
                        ("channel", "ordinary"),
                        ("generation__gte", 1),
                        ("sent_seq__gte", 1),
                    ),
                    _connector="OR",
                ),
                name="field_command_channel_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("seq__isnull", True),
                    ("seq", models.F("sent_seq")),
                    _connector="OR",
                ),
                name="field_command_seq_is_sent",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("channel", "ordinary"), ("outcome", "confirmed"), _negated=True),
                    ("seq__isnull", False),
                    _connector="OR",
                ),
                name="field_command_confirmed_decides_seq",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("channel", "ordinary"),
                        ("identity_digest__isnull", True),
                        ("outcome", "abandoned"),
                        ("seq__isnull", False),
                    ),
                    models.Q(
                        models.Q(("outcome", "abandoned"), _negated=True),
                        ("identity_digest__regex", "^[0-9a-f]{64}$"),
                    ),
                    _connector="OR",
                ),
                name="field_command_identity",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("outcome", "confirmed"), ("reason_code", "")),
                    models.Q(
                        models.Q(("outcome", "confirmed"), _negated=True),
                        models.Q(("reason_code", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="field_command_reason_code",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldcommand",
            constraint=models.CheckConstraint(
                condition=models.Q(("decided_at__gte", models.F("received_at"))),
                name="field_command_decided_after_received",
            ),
        ),
        migrations.AddIndex(
            model_name="fielddevicereport",
            index=models.Index(fields=["run", "reported_at"], name="field_device_report_run_idx"),
        ),
        migrations.AddConstraint(
            model_name="fieldgeneration",
            constraint=models.UniqueConstraint(
                fields=("run", "generation"),
                name="uniq_field_generation_run_generation",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldgeneration",
            constraint=models.CheckConstraint(
                condition=models.Q(("generation__gte", 2)),
                name="field_generation_from_two",
            ),
        ),
        migrations.RunSQL(SQL, SQL_REVERSE),
    ]
