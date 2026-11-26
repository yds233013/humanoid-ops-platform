"""v2 03C-2 (EM review of 03C, contract 10.D): an unresolved operator interruption keeps the robot from leaving on a
new run.

The 03C protective fallback (execution.field_commands) answers a stop or pause that the session state does not
allow by opening an OPERATOR interruption: "the robot must not start". On its run, the session trigger refuses
start and resume while it is open. But the run itself can be brought back, reconciled and closed with the
interruption still open, and the robot could then be authorized and depart on a NEW run where nothing refers to it.

execution_robot_departure_problem(robot, hub) now also answers, after the open-run check:
  "robot R has an unresolved interruption (IN-0001 on run EX-0001); it must be resolved first"
for any unresolved interruption of source 'operator' on any run of the robot. The authorization and departure
triggers (execution_authorization_hub_guard, execution_departure_hub_guard), the readiness evaluator
(ROBOT_NOT_AT_HUB, through execution.custody.departure_problem) and the pages read this one function, so all of
them refuse. Resolving needs execution.triage_incident (ops_lead, engineering_lead, safety_reviewer; the
resolution trigger allows it on a closed run). Incident interruptions are not counted: their incident hold
(robot scope, unchanged) already blocks the departure until the hold-release workflow releases it.

No race: an interruption cannot be opened on a closed run (execution_lock_run), so once a run is closed its set
of unresolved interruptions only shrinks, and while it is open the open-run check refuses first.

Additive and reversible: the function is patched from its CURRENT definition (as execution/0008 does), and the
reverse removes exactly the inserted block.
"""

from django.db import migrations

ANCHOR = (
    "    RETURN format('robot %s has an open run (%s); it must be reconciled and its trip closed first', rcode,\n"
    "                  (SELECT code FROM execution_executionrun WHERE id = rid));\n"
    "  END IF;\n"
)
CHECK = (
    "  IF EXISTS (SELECT 1 FROM execution_interruption i JOIN execution_executionrun ix ON ix.id = i.run_id\n"
    "             WHERE ix.robot_id = p_robot AND i.source = 'operator'\n"
    "               AND NOT EXISTS (SELECT 1 FROM execution_interruptionresolution ir\n"
    "                               WHERE ir.interruption_id = i.id)) THEN\n"
    "    RETURN format('robot %s has an unresolved interruption (%s); it must be resolved first', rcode,\n"
    "                  (SELECT string_agg(i.code || ' on run ' || ix.code, ', ' ORDER BY i.id)\n"
    "                   FROM execution_interruption i JOIN execution_executionrun ix ON ix.id = i.run_id\n"
    "                   WHERE ix.robot_id = p_robot AND i.source = 'operator'\n"
    "                     AND NOT EXISTS (SELECT 1 FROM execution_interruptionresolution ir\n"
    "                                     WHERE ir.interruption_id = i.id)));\n"
    "  END IF;\n"
)
SIGNATURE = "execution_robot_departure_problem(bigint, bigint)"


def _definition(cur) -> str:
    cur.execute("SELECT pg_get_functiondef(%s::regprocedure)", [SIGNATURE])
    return str(cur.fetchone()[0])


def _patch(schema_editor, reverse: bool) -> None:
    a, b = (ANCHOR + CHECK, ANCHOR) if reverse else (ANCHOR, ANCHOR + CHECK)
    with schema_editor.connection.cursor() as cur:
        text = _definition(cur)
        assert text.count(a) == 1, (SIGNATURE, text.count(a))
        if not reverse:
            assert CHECK not in text
        cur.execute(text.replace(a, b))


def forward(apps, schema_editor) -> None:  # noqa: ARG001
    _patch(schema_editor, reverse=False)


def backward(apps, schema_editor) -> None:  # noqa: ARG001
    _patch(schema_editor, reverse=True)


class Migration(migrations.Migration):
    dependencies = [("execution", "0009_field_ledger")]
    operations = [migrations.RunPython(forward, backward)]
