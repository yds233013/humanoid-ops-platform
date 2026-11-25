"""v2 02D fix (J8 walk, D1 and D2): issue and consume for a visit refuse a visit that is over, cancelled or closed,
exactly as a reservation does; quantities in refusals use the unit's singular for 1 ("1 box").

D1. hub_stock_take (issue / consume with a visit) now calls hub_stock_visit(visit, true), the reservation check:
the visit must exist, not be cancelled, not be over (on-site end passed) and its run must not be closed (trip
closure). hub_stock_visit gains the closed-run check, so a reservation refuses a closed visit too. Returns are
NOT affected: hub_return_stock only checks the visit exists and that the kits are out with it, so kits that came
back from a finished visit can always be recorded.

D2. New helper hub_stock_qty(n, unit) -> '1 box' / '3 boxes' (same rule as hub.stock.count_text), used by the
refusal messages, which also say "is" for 1. Not SECURITY DEFINER; EXECUTE revoked from PUBLIC (ops_app cannot
call it), like every stock helper.

Everything else of hub/0008 is re-created unchanged (CREATE OR REPLACE keeps owners and grants). Reverse restores
the 0008 definitions and drops the helper.
"""

import importlib

from django.db import migrations

base = importlib.import_module("hub.migrations.0008_stock_write_boundary")

QTY = r"""
CREATE OR REPLACE FUNCTION hub_stock_qty(n bigint, unit text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
  SELECT n::text || ' ' || CASE
    WHEN n <> 1 THEN unit
    WHEN unit ~ '(ch|sh|ss|x)es$' THEN left(unit, -2)
    WHEN unit ~ '[^aeiou]ies$' THEN left(unit, -3) || 'y'
    WHEN unit ~ '[^s]s$' THEN left(unit, -1)
    ELSE unit END
$$;
REVOKE ALL ON FUNCTION hub_stock_qty(bigint, text) FROM PUBLIC;
"""

REPLACEMENTS = [
    # D1: issue / consume for a visit use the same visit check as a reservation
    ("    PERFORM hub_stock_visit(p_visit, false);\n", "    PERFORM hub_stock_visit(p_visit, true);\n"),
    (
        """      PERFORM fleet_fail(format('Visit %s is already over', v.code));
    END IF;
""",
        """      PERFORM fleet_fail(format('Visit %s is already over', v.code));
    END IF;
    IF EXISTS (SELECT 1 FROM execution_tripclosure c JOIN execution_executionrun r ON r.id = c.run_id
               WHERE r.visit_id = v.id) THEN
      PERFORM fleet_fail(format('Visit %s is already closed', v.code));
    END IF;
""",
    ),
    # D2: singular unit for 1, and "is" for 1
    (
        "PERFORM fleet_fail(format('Only %s %s of %s (%s) are on hand', on_hand, it.unit, it.name, it.code));",
        "PERFORM fleet_fail(format('Only %s of %s (%s) %s on hand', hub_stock_qty(on_hand, it.unit), it.name, "
        "it.code,\n                              CASE WHEN on_hand = 1 THEN 'is' ELSE 'are' END));",
    ),
    (
        """      PERFORM fleet_fail(format('Only %s %s of %s (%s) are available to reserve (%s on hand, %s already reserved for visits)',
                                greatest(on_hand - reserved, 0), it.unit, it.name, it.code, on_hand, reserved));""",
        """      PERFORM fleet_fail(format('Only %s of %s (%s) %s available to reserve (%s on hand, %s already reserved for visits)',
                                hub_stock_qty(greatest(on_hand - reserved, 0), it.unit), it.name, it.code,
                                CASE WHEN on_hand - reserved = 1 THEN 'is' ELSE 'are' END, on_hand, reserved));""",
    ),
    (
        """    PERFORM fleet_fail(format('Only %s %s of %s (%s) are available (%s on hand, %s reserved for visits; release a reservation first)',
                              greatest(on_hand - reserved, 0), it.unit, it.name, it.code, on_hand, reserved));""",
        """    PERFORM fleet_fail(format('Only %s of %s (%s) %s available (%s on hand, %s reserved for visits; release a reservation first)',
                              hub_stock_qty(greatest(on_hand - reserved, 0), it.unit), it.name, it.code,
                              CASE WHEN on_hand - reserved = 1 THEN 'is' ELSE 'are' END, on_hand, reserved));""",
    ),
    (
        """    PERFORM fleet_fail(format('Only %s %s of %s (%s) are out%s, so %s cannot come back', out_now, it.unit, it.name,
                              it.code, CASE WHEN p_visit IS NULL THEN ' with visits' ELSE ' with that visit' END,
                              p_qty));""",
        """    PERFORM fleet_fail(format('Only %s of %s (%s) %s out%s, so %s cannot come back',
                              hub_stock_qty(out_now, it.unit), it.name, it.code,
                              CASE WHEN out_now = 1 THEN 'is' ELSE 'are' END,
                              CASE WHEN p_visit IS NULL THEN ' with visits' ELSE ' with that visit' END,
                              p_qty));""",
    ),
]

old_sql = base.HELPERS + base.FUNCTIONS
new_sql = old_sql
for before, after in REPLACEMENTS:
    assert new_sql.count(before) == 1, before[:60]
    new_sql = new_sql.replace(before, after)

forward = QTY + new_sql
reverse = old_sql + "DROP FUNCTION IF EXISTS hub_stock_qty(bigint, text);\n"


class Migration(migrations.Migration):
    dependencies = [("hub", "0008_stock_write_boundary"), ("execution", "0001_initial")]
    operations = [migrations.RunSQL(forward, reverse)]
