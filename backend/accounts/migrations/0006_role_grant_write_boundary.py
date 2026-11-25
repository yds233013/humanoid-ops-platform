"""v2 Phase 03A-3 (docs/13_v2_phase03_contract.md I-8, 10.B, 11.2; the trusted write boundary of
docs/11_v2_phase02_contract.md 8.8): role grants are written only through database functions.

ops_app keeps SELECT on accounts_rolegrant and loses INSERT, UPDATE, DELETE, TRUNCATE and the id sequence, so a
compromised runtime role cannot forge a grant at any hub, revoke one silently or un-revoke one. Every write goes
through these functions, owned by ops_owner, with a fixed search_path and EXECUTE revoked from PUBLIC:

    accounts_grant_role(actor, user, role, hub, valid_from, valid_to, request_id) -> grant id   (ops_app)
    accounts_revoke_grant(actor, grant, reason, request_id) -> grant id                         (ops_app)
    accounts_bootstrap_admin(user, request_id) -> grant id                                       (ops_app)
    accounts_seed_grant(user, role, hub, request_id) -> grant id                    (owner only: seeds)
    accounts_lock_grants(user, roles, hub) -> boolean                                          (ops_app)

RULES (the same as accounts.services before this change, nothing new):
  * grant and revoke: the actor exists, is active and holds an active admin grant (accounts.grant_role and
    accounts.revoke_role are admin only). The actor's admin grants are taken FOR SHARE first (as run_command and
    the 03A-1 guarded writes do), so a revocation of the actor's admin grant and the grant are ordered.
  * grant: a known role; never to yourself (separation of duties); the user exists (row locked, so two grants to
    one person are serialized); a hub-scoped role is held at a hub (the only hub when none is named,
    platform_core_default_hub(): "hub required" when there are several), a regional role names no hub; at
    most one active grant per (user, role, hub); valid_from is never in the past (NULL = now), valid_to after it.
  * revoke: a reason; the grant exists (locked FOR UPDATE: an in-flight write that holds it FOR SHARE finishes
    first, a later one sees the revocation) and is not already revoked.
  A refusal raises (insufficient_privilege for who may do it, check_violation or unique_violation otherwise) and
  writes nothing. Each write appends its audit row (accounts.grant_role / accounts.revoke_role).

BOOTSTRAP. accounts_bootstrap_admin gives the first admin grant of an empty workspace: only while no admin grant
has ever existed (revoked ones included) and the workspace is not initialized, serialized by an advisory lock, so
it works once. Audited as accounts.bootstrap_admin.

SEEDS. accounts_seed_grant is for the owner-run seed commands only (not SECURITY DEFINER, not executable by
ops_app or PUBLIC): idempotent, audited as accounts.seed_grant with no actor.

LOCK CHECK. Taking a row lock (SELECT ... FOR SHARE) needs UPDATE privilege, which ops_app no longer has, so the
permission check of run_command (platform_core.commands.holds_permission_locked) calls accounts_lock_grants: the
same statement as before (active grants of the roles that apply at the hub, by id, FOR SHARE), run as the owner.
The locks belong to the caller's transaction.

Reverse: drops the functions and gives ops_app back INSERT, UPDATE, DELETE and the sequence.
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE
SCOPED = "('hub_technician', 'field_operator')"
ROLES = (
    "('admin', 'ops_lead', 'field_operator', 'engineer', 'engineering_lead', 'safety_reviewer', 'founder', "
    "'hub_technician')"
)

SQL = rf"""
-- Not SECURITY DEFINER, not executable by ops_app or PUBLIC: called by the owner functions below.
CREATE OR REPLACE FUNCTION accounts_audit(p_actor bigint, p_cmd text, p_id bigint, p_payload jsonb,
                                          p_result jsonb, p_rid text) RETURNS void
LANGUAGE sql SET search_path = public, pg_temp AS $$
  INSERT INTO platform_core_auditevent (occurred_at, actor_id, command, outcome, entity_type, entity_id, payload,
                                        result, reason, request_id)
  VALUES (clock_timestamp(), p_actor, p_cmd, 'committed', 'role_grant', p_id::text, p_payload, p_result, '',
          left(coalesce(p_rid, ''), 64))
$$;

CREATE OR REPLACE FUNCTION accounts_fail(msg text, code text) RETURNS void
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF code = 'deny' THEN RAISE EXCEPTION '%', msg USING ERRCODE = 'insufficient_privilege'; END IF;
  IF code = 'conflict' THEN RAISE EXCEPTION '%', msg USING ERRCODE = 'unique_violation'; END IF;
  RAISE EXCEPTION '%', msg USING ERRCODE = 'check_violation';
END $$;

-- The actor exists, is active and holds an active admin grant. Takes the actor's admin grants FOR SHARE first
-- (any validity), then checks in a fresh statement: a revocation committed while this waited is seen.
CREATE OR REPLACE FUNCTION accounts_actor_is_admin(uid bigint) RETURNS boolean
LANGUAGE plpgsql VOLATILE SET search_path = public, pg_temp AS $$
BEGIN
  IF uid IS NULL THEN RETURN false; END IF;
  IF NOT EXISTS (SELECT 1 FROM auth_user u WHERE u.id = uid AND u.is_active) THEN RETURN false; END IF;
  PERFORM 1 FROM accounts_rolegrant g WHERE g.user_id = uid AND g.role = 'admin' ORDER BY g.id FOR SHARE;
  RETURN EXISTS (SELECT 1 FROM accounts_rolegrant g
                 WHERE g.user_id = uid AND g.role = 'admin' AND g.revoked_at IS NULL
                   AND g.valid_from <= clock_timestamp() AND (g.valid_to IS NULL OR g.valid_to > clock_timestamp()));
END $$;

CREATE OR REPLACE FUNCTION accounts_hub_code(p_hub bigint) RETURNS text
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT code FROM platform_core_hub WHERE id = p_hub
$$;

-- The permission check of run_command (platform_core.commands.holds_permission_locked), unchanged, run as the
-- owner because SELECT ... FOR SHARE needs UPDATE privilege. True when the user holds an active grant of one of
-- ``roles`` that applies at ``p_hub`` (any hub when NULL); every such grant is then locked FOR SHARE.
CREATE OR REPLACE FUNCTION accounts_lock_grants(uid bigint, roles text[], p_hub bigint) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF uid IS NULL OR roles IS NULL THEN RETURN false; END IF;
  PERFORM 1 FROM accounts_rolegrant g
  WHERE g.user_id = uid AND g.role = ANY(roles) AND g.revoked_at IS NULL
    AND g.valid_from <= clock_timestamp() AND (g.valid_to IS NULL OR g.valid_to > clock_timestamp())
    AND (p_hub IS NULL OR g.hub_id IS NULL OR g.hub_id = p_hub)
  ORDER BY g.id FOR SHARE;
  RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION accounts_grant_role(p_actor bigint, p_user bigint, p_role text, p_hub bigint,
                                               p_valid_from timestamptz, p_valid_to timestamptz, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE h bigint := p_hub; vf timestamptz := coalesce(p_valid_from, now()); new_id bigint; code text;
BEGIN
  IF NOT accounts_actor_is_admin(p_actor) THEN
    PERFORM accounts_fail('Only the workspace admin can grant a role', 'deny');
  END IF;
  IF p_role IS NULL OR p_role NOT IN {ROLES} THEN
    PERFORM accounts_fail(format('Unknown role %s', coalesce(p_role, '')), 'invalid');
  END IF;
  IF p_user IS NULL THEN PERFORM accounts_fail('Choose the person', 'invalid'); END IF;
  IF p_user = p_actor THEN
    PERFORM accounts_fail('Separation of duties: you cannot grant a role to yourself', 'invalid');
  END IF;
  PERFORM 1 FROM auth_user u WHERE u.id = p_user FOR UPDATE;  -- grants to one person are serialized
  IF NOT FOUND THEN PERFORM accounts_fail(format('No user %s', p_user), 'invalid'); END IF;
  IF p_role IN {SCOPED} THEN
    IF h IS NULL THEN h := platform_core_default_hub(); END IF;  -- the only hub; "hub required" when several
    IF NOT EXISTS (SELECT 1 FROM platform_core_hub WHERE id = h) THEN
      PERFORM accounts_fail(format('No hub %s', h), 'invalid');
    END IF;
  ELSIF h IS NOT NULL THEN
    PERFORM accounts_fail(format('The %s role applies to every hub; do not choose a hub',
                                 replace(p_role, '_', ' ')), 'invalid');
  END IF;
  IF p_valid_from IS NOT NULL AND p_valid_from < now() THEN
    PERFORM accounts_fail('A grant cannot start in the past', 'invalid');
  END IF;
  IF p_valid_to IS NOT NULL AND p_valid_to <= vf THEN
    PERFORM accounts_fail('The grant must end after it starts', 'invalid');
  END IF;
  code := accounts_hub_code(h);
  IF EXISTS (SELECT 1 FROM accounts_rolegrant g WHERE g.user_id = p_user AND g.role = p_role
             AND coalesce(g.hub_id, 0) = coalesce(h, 0) AND g.revoked_at IS NULL) THEN
    PERFORM accounts_fail(format('User %s already holds an active %s grant%s', p_user, p_role,
                                 CASE WHEN code IS NULL THEN '' ELSE ' at hub ' || code END), 'conflict');
  END IF;
  INSERT INTO accounts_rolegrant (user_id, role, valid_from, valid_to, granted_by_id, granted_at, hub_id)
  VALUES (p_user, p_role, vf, p_valid_to, p_actor, clock_timestamp(), h)
  RETURNING id INTO new_id;
  PERFORM accounts_audit(p_actor, 'accounts.grant_role', new_id,
                         jsonb_strip_nulls(jsonb_build_object('user_id', p_user, 'role', p_role, 'hub', code))
                           || jsonb_build_object('valid_to', p_valid_to),
                         jsonb_build_object('user_id', p_user, 'role', p_role, 'hub', code), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION accounts_revoke_grant(p_actor bigint, p_grant bigint, p_reason text, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE g accounts_rolegrant;
BEGIN
  IF NOT accounts_actor_is_admin(p_actor) THEN
    PERFORM accounts_fail('Only the workspace admin can revoke a role', 'deny');
  END IF;
  IF coalesce(btrim(p_reason), '') = '' THEN PERFORM accounts_fail('A reason is required', 'invalid'); END IF;
  SELECT * INTO g FROM accounts_rolegrant WHERE id = p_grant FOR UPDATE;
  IF NOT FOUND THEN PERFORM accounts_fail(format('No grant %s', coalesce(p_grant::text, '')), 'invalid'); END IF;
  IF g.revoked_at IS NOT NULL THEN PERFORM accounts_fail('Grant already revoked', 'conflict'); END IF;
  UPDATE accounts_rolegrant SET revoked_at = clock_timestamp(), revoked_by_id = p_actor WHERE id = p_grant;
  PERFORM accounts_audit(p_actor, 'accounts.revoke_role', p_grant,
                         jsonb_build_object('grant_id', p_grant, 'reason', p_reason),
                         jsonb_build_object('revoked', true, 'role', g.role, 'hub', accounts_hub_code(g.hub_id)),
                         p_rid);
  RETURN p_grant;
END $fn$;

-- The first admin of an empty workspace (platform_core.bootstrap, the /setup/ page and bootstrap_admin; the demo
-- builder). Works only while no admin grant has ever existed and the workspace is not initialized: once.
CREATE OR REPLACE FUNCTION accounts_bootstrap_admin(p_user bigint, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE new_id bigint;
BEGIN
  IF p_user IS NULL OR NOT EXISTS (SELECT 1 FROM auth_user u WHERE u.id = p_user AND u.is_active) THEN
    PERFORM accounts_fail('No such user', 'invalid');
  END IF;
  -- refuse cheaply first (no lock for a caller that can never succeed), then serialize and look again
  FOR i IN 1..2 LOOP
    IF EXISTS (SELECT 1 FROM accounts_rolegrant WHERE role = 'admin')
       OR EXISTS (SELECT 1 FROM platform_core_workspace WHERE initialized) THEN
      PERFORM accounts_fail('This workspace already has an admin', 'deny');
    END IF;
    IF i = 1 THEN PERFORM pg_advisory_xact_lock(hashtext('accounts_bootstrap_admin')); END IF;
  END LOOP;
  INSERT INTO accounts_rolegrant (user_id, role, valid_from, granted_by_id, granted_at)
  VALUES (p_user, 'admin', now(), NULL, clock_timestamp())
  RETURNING id INTO new_id;
  PERFORM accounts_audit(p_user, 'accounts.bootstrap_admin', new_id, jsonb_build_object('user_id', p_user),
                         jsonb_build_object('user_id', p_user, 'role', 'admin'), p_rid);
  RETURN new_id;
END $fn$;

-- Owner-run seed commands only (synthetic data): an active grant of the role (at the hub), created when missing.
CREATE OR REPLACE FUNCTION accounts_seed_grant(p_user bigint, p_role text, p_hub bigint, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SET search_path = public, pg_temp AS $fn$
DECLARE h bigint := p_hub; gid bigint;
BEGIN
  IF p_role IN {SCOPED} AND h IS NULL THEN h := platform_core_default_hub(); END IF;
  SELECT id INTO gid FROM accounts_rolegrant
  WHERE user_id = p_user AND role = p_role AND coalesce(hub_id, 0) = coalesce(h, 0) AND revoked_at IS NULL;
  IF FOUND THEN RETURN gid; END IF;
  INSERT INTO accounts_rolegrant (user_id, role, valid_from, granted_by_id, granted_at, hub_id)
  VALUES (p_user, p_role, now(), NULL, clock_timestamp(), h)
  RETURNING id INTO gid;
  PERFORM accounts_audit(NULL, 'accounts.seed_grant', gid,
                         jsonb_build_object('user_id', p_user, 'role', p_role, 'hub', accounts_hub_code(h)),
                         jsonb_build_object('user_id', p_user, 'role', p_role), p_rid);
  RETURN gid;
END $fn$;
"""

PUBLIC_SIGNATURES = [
    "accounts_lock_grants(bigint, text[], bigint)",
    "accounts_grant_role(bigint, bigint, text, bigint, timestamptz, timestamptz, text)",
    "accounts_revoke_grant(bigint, bigint, text, text)",
    "accounts_bootstrap_admin(bigint, text)",
]
OWNER_SIGNATURES = [
    "accounts_seed_grant(bigint, text, bigint, text)",
    "accounts_actor_is_admin(bigint)",
    "accounts_hub_code(bigint)",
    "accounts_fail(text, text)",
    "accounts_audit(bigint, text, bigint, jsonb, jsonb, text)",
]

forward = (
    SQL
    + "".join(
        f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {s} TO {APP};\n"
        for s in PUBLIC_SIGNATURES
    )
    + "".join(f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\n" for s in OWNER_SIGNATURES)
    + f"REVOKE ALL ON accounts_rolegrant FROM {APP};\nGRANT SELECT ON accounts_rolegrant TO {APP};\n"
    + f"REVOKE ALL ON SEQUENCE accounts_rolegrant_id_seq FROM {APP};\n"
)

reverse = (
    "".join(f"DROP FUNCTION IF EXISTS {s};\n" for s in PUBLIC_SIGNATURES + OWNER_SIGNATURES)
    + f"GRANT SELECT, INSERT, UPDATE, DELETE ON accounts_rolegrant TO {APP};\n"
    + f"GRANT USAGE, SELECT ON SEQUENCE accounts_rolegrant_id_seq TO {APP};\n"
)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_hub_qualified_grants"),
        ("platform_core", "0005_default_hub"),
    ]

    operations = [migrations.RunSQL(forward, reverse)]
