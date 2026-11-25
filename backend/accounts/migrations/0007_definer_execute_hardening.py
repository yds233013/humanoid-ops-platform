"""v2 Phase 03A closure (docs/13_v2_phase03_contract.md section 12): every SECURITY DEFINER function has restricted
EXECUTE.

The catalog audit (tests/test_v2_03a_definer_audit.py) found one SECURITY DEFINER function still executable by
PUBLIC: fleet_hold_rebase_approval_guard() (fleet/0008), the trigger on fleet_holdrebaseapproval. It is a trigger
function, so PostgreSQL refuses a direct call anyway, and a trigger fires without its EXECUTE privilege being
checked; revoking PUBLIC changes nothing at runtime and makes the restriction explicit.

Kept in the accounts app (not fleet) so it does not collide with parallel fleet migrations. Reverse restores the
PUBLIC grant.
"""

from django.db import migrations

FORWARD = "REVOKE ALL ON FUNCTION fleet_hold_rebase_approval_guard() FROM PUBLIC;"
REVERSE = "GRANT EXECUTE ON FUNCTION fleet_hold_rebase_approval_guard() TO PUBLIC;"


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_role_grant_write_boundary"),
        ("fleet", "0008_s7_templates_rules_policies"),
    ]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
