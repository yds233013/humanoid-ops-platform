"""v2 Phase 03A-2 (docs/13_v2_phase03_contract.md 10.B, I-8; a gap left by 03A-1): field commands check the crew or
recorder role AT THE RUN'S HUB in the database.

Until now the execution and readiness triggers accepted an ops_lead or field_operator grant at ANY hub
(execution_is_field_user). A field operator holding a grant only at hub A could therefore record steps on a hub B
run by direct SQL as the runtime role. New helpers:

  execution_run_hub(run)                       the run's visit hub
  execution_is_field_user_at(uid, run)         ops_lead or field_operator AT that hub (has_active_role_at:
                                               a regional grant, hub NULL, applies everywhere; ops_lead is
                                               regional, so it keeps working)
  execution_person_is_field_user_at(pid, run)  the same for a planning person (receivers, verifiers)

and these trigger functions now use them (only these checks change; each replacement is asserted to match once):

  execution_custody_guard               handoff recorder and receiver (vehicle -> site, site -> vehicle,
                                        vehicle -> hub). The recovery branch (ops_lead, regional) is unchanged.
  execution_custody_verification_guard  verifier and recorder
  execution_trip_event_guard            recorder
  execution_site_check_guard            recorder
  execution_session_guard               start / resume recorder (crew of the run AND a field role at its hub),
                                        observation recorder. Pause and stop stay "a field role at any hub":
                                        they only make things safer (contract I-2 class B).
  readiness_departure_guard             recorder of a departure, at the visit's hub

Deliberately NOT scoped (they only make things safer and stay available to anyone allowed today): incident
reporting, hold placement, interruptions, pause and stop. Post-visit inspections have no field-role check in the
database (the fleet inspection triggers check signers' qualifications), so nothing changes there.

The functions are patched from their CURRENT definitions (pg_get_functiondef), so whichever earlier migration last
replaced a body, only the role checks change. Reverse applies the inverse replacements and drops the helpers.
"""

from django.db import migrations

HELPERS = r"""
CREATE OR REPLACE FUNCTION execution_run_hub(rid bigint) RETURNS bigint
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT v.hub_id FROM execution_executionrun x JOIN planning_visit v ON v.id = x.visit_id WHERE x.id = rid
$$;

CREATE OR REPLACE FUNCTION execution_is_field_user_at(uid bigint, rid bigint) RETURNS boolean
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT uid IS NOT NULL AND (has_active_role_at(uid, 'ops_lead', execution_run_hub(rid))
                              OR has_active_role_at(uid, 'field_operator', execution_run_hub(rid)))
$$;

CREATE OR REPLACE FUNCTION execution_person_is_field_user_at(pid bigint, rid bigint) RETURNS boolean
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT execution_is_field_user_at(p.user_id, rid) FROM planning_person p WHERE p.id = pid), false)
$$;
"""
HELPERS_REVERSE = """
DROP FUNCTION IF EXISTS execution_person_is_field_user_at(bigint, bigint);
DROP FUNCTION IF EXISTS execution_is_field_user_at(bigint, bigint);
DROP FUNCTION IF EXISTS execution_run_hub(bigint);
"""

DEPARTURE_OLD = (
    "IF NOT (has_active_role(NEW.recorded_by_id, 'ops_lead') "
    "OR has_active_role(NEW.recorded_by_id, 'field_operator')) THEN"
)
DEPARTURE_NEW = (
    "IF NOT (has_active_role_at(NEW.recorded_by_id, 'ops_lead', "
    "(SELECT vh.hub_id FROM planning_visit vh WHERE vh.id = a.visit_id)) "
    "OR has_active_role_at(NEW.recorded_by_id, 'field_operator', "
    "(SELECT vh.hub_id FROM planning_visit vh WHERE vh.id = a.visit_id))) THEN"
)

# function -> [(old, new)], each old must occur exactly once in the current definition
PATCHES: dict[str, list[tuple[str, str]]] = {
    "execution_custody_guard": [
        (
            "IF NOT execution_is_field_user(NEW.recorded_by_id) THEN",
            "IF NOT execution_is_field_user_at(NEW.recorded_by_id, x.id) THEN",
        ),
        (
            "IF NEW.received_by_id IS NULL THEN PERFORM fleet_fail('a handoff needs a receiver'); END IF;\n"
            "  IF NOT execution_person_is_field_user(NEW.received_by_id) THEN",
            "IF NEW.received_by_id IS NULL THEN PERFORM fleet_fail('a handoff needs a receiver'); END IF;\n"
            "  IF NOT execution_person_is_field_user_at(NEW.received_by_id, x.id) THEN",
        ),
    ],
    "execution_custody_verification_guard": [
        (
            "IF NOT execution_person_is_field_user(NEW.verified_by_id) THEN",
            "IF NOT execution_person_is_field_user_at(NEW.verified_by_id, e.run_id) THEN",
        ),
        (
            "IF NOT execution_is_field_user(NEW.recorded_by_id) THEN",
            "IF NOT execution_is_field_user_at(NEW.recorded_by_id, e.run_id) THEN",
        ),
    ],
    "execution_trip_event_guard": [
        (
            "IF NOT execution_is_field_user(NEW.recorded_by_id) THEN",
            "IF NOT execution_is_field_user_at(NEW.recorded_by_id, x.id) THEN",
        ),
    ],
    "execution_site_check_guard": [
        (
            "IF NOT execution_is_field_user(NEW.recorded_by_id) THEN",
            "IF NOT execution_is_field_user_at(NEW.recorded_by_id, x.id) THEN",
        ),
    ],
    "execution_session_guard": [
        (
            "OR (execution_is_field_user(NEW.recorded_by_id) AND execution_user_is_crew(x.id, NEW.recorded_by_id))",
            "OR (execution_is_field_user_at(NEW.recorded_by_id, x.id) "
            "AND execution_user_is_crew(x.id, NEW.recorded_by_id))",
        ),
        (
            "  ELSIF NOT execution_is_field_user(NEW.recorded_by_id) THEN",
            "  ELSIF NOT (CASE WHEN NEW.kind IN ('pause', 'stop') THEN execution_is_field_user(NEW.recorded_by_id)\n"
            "                  ELSE execution_is_field_user_at(NEW.recorded_by_id, x.id) END) THEN",
        ),
    ],
    "readiness_departure_guard": [(DEPARTURE_OLD, DEPARTURE_NEW)],
}


def _definition(cur, name: str) -> str:
    cur.execute(
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = %s",
        [name],
    )
    rows = cur.fetchall()
    assert len(rows) == 1, (name, len(rows))
    return str(rows[0][0])


def _patch(schema_editor, reverse: bool) -> None:
    with schema_editor.connection.cursor() as cur:
        for name, pairs in PATCHES.items():
            text = _definition(cur, name)
            for old, new in pairs:
                a, b = (new, old) if reverse else (old, new)
                assert text.count(a) == 1, (name, a, text.count(a))
                text = text.replace(a, b)
            cur.execute(text)


def forward(apps, schema_editor) -> None:  # noqa: ARG001
    _patch(schema_editor, reverse=False)


def backward(apps, schema_editor) -> None:  # noqa: ARG001
    _patch(schema_editor, reverse=True)


class Migration(migrations.Migration):
    dependencies = [
        ("execution", "0007_departure_location_gate"),
        ("readiness", "0009_maintenance_gate"),
        ("operations", "0009_location_eligibility"),
        ("accounts", "0005_hub_qualified_grants"),
    ]
    operations = [
        migrations.RunSQL(HELPERS, HELPERS_REVERSE),
        migrations.RunPython(forward, backward),
    ]
