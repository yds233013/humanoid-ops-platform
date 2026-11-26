"""v2 Phase 03B-1 (docs/13_v2_phase03_contract.md 13.4 item 1, 13.5): the ``safety_pause`` role.

A hub-scoped user may PAUSE departures only with an explicitly defined, separately granted ``safety_pause``
permission; a general hub grant (hub technician, field operator) is not enough. The smallest change consistent
with accounts.roles: one new role, held AT ONE HUB like the other hub-scoped roles, granted and revoked through
the existing audited role-grant functions. It confers ``operations.pause`` only (never ``operations.lift_pause``).

Changes:
  * the role choice list (Django state only);
  * check constraint role_grant_hub_scope: safety_pause must name a hub, like hub_technician and field_operator;
  * accounts_grant_role (accounts/0006): safety_pause is a known role and is hub-scoped;
  * accounts_seed_grant (accounts/0006) and the trigger function accounts_rolegrant_default_hub (accounts/0005):
    safety_pause is hub-scoped (the only hub when none is named; "hub required" when there are several).
The bodies are the earlier migrations' text with only the role lists changed (asserted), so nothing else moves;
CREATE OR REPLACE keeps owners, search_path and EXECUTE grants.

Reverse restores the earlier bodies and constraint (it fails, and says why, while a safety_pause grant exists).
"""

import importlib

from django.db import migrations, models
from django.db.models import Q

_A5 = importlib.import_module("accounts.migrations.0005_hub_qualified_grants")
_A6 = importlib.import_module("accounts.migrations.0006_role_grant_write_boundary")

OLD_SCOPED = "('hub_technician', 'field_operator')"
NEW_SCOPED = "('hub_technician', 'field_operator', 'safety_pause')"
OLD_ROLES = _A6.ROLES
NEW_ROLES = OLD_ROLES.replace("'hub_technician')", "'hub_technician', 'safety_pause')")
assert NEW_ROLES != OLD_ROLES


def _slice(sql: str, start: str, end: str | None) -> str:
    i = sql.index(start)
    j = sql.index(end, i) if end else len(sql)
    return sql[i:j]


_GRANT_OLD = _slice(
    _A6.SQL,
    "CREATE OR REPLACE FUNCTION accounts_grant_role(",
    "CREATE OR REPLACE FUNCTION accounts_revoke_grant(",
)
_SEED_OLD = _slice(_A6.SQL, "CREATE OR REPLACE FUNCTION accounts_seed_grant(", None)
_DEFAULT_OLD = _slice(
    _A5.SQL,
    "CREATE OR REPLACE FUNCTION accounts_rolegrant_default_hub()",
    "REVOKE ALL ON FUNCTION accounts_rolegrant_default_hub()",
)


def _swap(text: str, pairs: list[tuple[str, str, int]]) -> str:
    for old, new, n in pairs:
        assert text.count(old) == n, (old, text.count(old))
        text = text.replace(old, new)
    return text


_GRANT_NEW = _swap(_GRANT_OLD, [(OLD_ROLES, NEW_ROLES, 1), (OLD_SCOPED, NEW_SCOPED, 1)])
_SEED_NEW = _swap(_SEED_OLD, [(OLD_SCOPED, NEW_SCOPED, 1)])
_DEFAULT_NEW = _swap(_DEFAULT_OLD, [(OLD_SCOPED, NEW_SCOPED, 1)])

FORWARD = _GRANT_NEW + "\n" + _SEED_NEW + "\n" + _DEFAULT_NEW
REVERSE = _GRANT_OLD + "\n" + _SEED_OLD + "\n" + _DEFAULT_OLD

ROLES = [
    "admin",
    "ops_lead",
    "field_operator",
    "engineer",
    "engineering_lead",
    "safety_reviewer",
    "founder",
    "hub_technician",
    "safety_pause",
]
SCOPED = ["hub_technician", "field_operator", "safety_pause"]


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_definer_execute_hardening"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rolegrant",
            name="role",
            field=models.CharField(choices=[(r, r) for r in ROLES], max_length=40),
        ),
        migrations.RemoveConstraint(model_name="rolegrant", name="role_grant_hub_scope"),
        migrations.AddConstraint(
            model_name="rolegrant",
            constraint=models.CheckConstraint(
                condition=(
                    (Q(("role__in", SCOPED)) & Q(("hub__isnull", False)))
                    | (~Q(("role__in", SCOPED)) & Q(("hub__isnull", True)))
                ),
                name="role_grant_hub_scope",
            ),
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
