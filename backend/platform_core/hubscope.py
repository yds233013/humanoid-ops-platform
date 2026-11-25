"""Which hubs a page shows (v2 03A-1, docs/13_v2_phase03_contract.md 11.2 "Reads", invariant I-8).

A viewer with any active REGIONAL grant (hub NULL) sees every hub; a viewer with only hub grants sees
those hubs. A person's home hub plays no part. ``?hub=CODE`` narrows the page to one of the visible hubs; it
can never widen it: a hub the viewer holds no grant for shows nothing (and says so).

ONE statement per page, whatever the number of hubs, grants or records.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db import connection
from django.http import HttpRequest

from platform_core.models import Hub


@dataclass
class HubScope:
    hubs: list[Hub]  # the hubs this viewer may see, by code
    regional: bool  # True: every hub (a regional grant)
    selected: Hub | None = None  # ?hub=CODE, when visible
    refused_code: str = ""  # ?hub=CODE of a hub the viewer may not see (the page shows nothing)
    by_id: dict[int, Hub] = field(default_factory=dict)
    roles: set[str] = field(default_factory=set)  # the viewer's active roles, any hub (= active_roles(user))

    @property
    def ids(self) -> set[int] | None:
        """The hub ids to show; None = no restriction (regional viewer, no filter)."""
        if self.refused_code:
            return set()
        if self.selected is not None:
            return {self.selected.pk}
        return None if self.regional else {h.pk for h in self.hubs}

    def allows(self, hub_id: int | None) -> bool:
        ids = self.ids
        return ids is None or (hub_id is not None and hub_id in ids)

    def filter(self, qs: Any, field_name: str = "hub_id") -> Any:
        ids = self.ids
        return qs if ids is None else qs.filter(**{f"{field_name}__in": sorted(ids)})

    def keep(self, rows: Iterable[Any], hub_of: Any) -> list[Any]:
        """Python-side filter for read models that are one set-based statement: ``hub_of(row)`` -> hub id."""
        ids = self.ids
        return list(rows) if ids is None else [r for r in rows if hub_of(r) in ids]

    @property
    def show_filter(self) -> bool:
        return len(self.hubs) > 1 or bool(self.refused_code)

    @property
    def code(self) -> str:
        return self.selected.code if self.selected is not None else ""

    def label(self, hub_id: int | None) -> str:
        h = self.by_id.get(hub_id or 0)
        return h.code if h is not None else ""


# The viewer's active grants (the validity rule of accounts.permissions, database clock) -> regional or
# not, their roles, and the hubs they may see. Always at least one row; hub columns NULL when none.
SCOPE_SQL = """
WITH g AS MATERIALIZED (
  SELECT gr.hub_id, gr.role FROM accounts_rolegrant gr JOIN auth_user u ON u.id = gr.user_id AND u.is_active
  WHERE gr.user_id = %s AND gr.revoked_at IS NULL AND gr.valid_from <= clock_timestamp()
    AND (gr.valid_to IS NULL OR gr.valid_to > clock_timestamp())
),
r AS (SELECT EXISTS (SELECT 1 FROM g WHERE g.hub_id IS NULL) AS regional,
             ARRAY(SELECT DISTINCT g.role FROM g) AS roles)
SELECT r.regional, h.id, h.code, h.name, r.roles
FROM r LEFT JOIN platform_core_hub h ON (r.regional OR h.id IN (SELECT g.hub_id FROM g))
ORDER BY h.code
"""


def hub_scope(request: HttpRequest) -> HubScope:
    user = request.user
    regional, hubs, roles = False, [], set()
    if getattr(user, "is_authenticated", False):
        with connection.cursor() as cur:
            cur.execute(SCOPE_SQL, [user.pk])
            for reg, hub_id, code, name, role_list in cur.fetchall():
                regional, roles = bool(reg), set(role_list or [])
                if hub_id is not None:
                    hubs.append(Hub(id=hub_id, code=code, name=name))
    scope = HubScope(hubs=hubs, regional=regional, by_id={h.pk: h for h in hubs}, roles=roles)
    code = (request.GET.get("hub") or "").strip().upper()
    if code:
        scope.selected = next((h for h in hubs if h.code == code), None)
        if scope.selected is None:
            scope.refused_code = code[:12]
    return scope


# ------------------------------------------------------------------ the hub of a command's TARGET RECORD
# run_command(..., hub=<one of these>): resolved inside the command's transaction, from the record, never from
# client input. A missing record gives None (the command then refuses the unknown record itself).
def _first(model: str, field_name: str, **lookup: Any) -> int | None:
    from django.apps import apps

    value = apps.get_model(model).objects.filter(**lookup).values_list(field_name, flat=True).first()
    return int(value) if value is not None else None


def robot_hub(code: str) -> Any:
    return lambda: _first("fleet.Robot", "hub_id", code=(code or "").strip().upper())


def visit_hub(code: str) -> Any:
    return lambda: _first("planning.Visit", "hub_id", code=(code or "").strip())


def run_hub(code: str) -> Any:
    return lambda: _first("execution.ExecutionRun", "visit__hub_id", code=(code or "").strip())


def owner_hub(code: str | None) -> Hub | None:
    """The hub a NEW record is created at, named by its code. None when no code is given: the database default
    (platform_core_default_hub) then applies, which refuses to guess when there are several hubs."""
    from platform_core.commands import Invalid

    code = (code or "").strip().upper()
    if not code:
        return None
    hub = Hub.objects.filter(code=code).first()
    if hub is None:
        raise Invalid(f"No hub {code}")
    return hub


def add_owner_hub_field(form: Any, help_text: str = "The hub this belongs to.") -> None:
    """Give a create form a required "Hub" choice when the workspace has several hubs (v2 03A-1). With one hub
    there is nothing to choose: the database default puts the record there."""
    from django import forms

    hubs = list(Hub.objects.order_by("code"))
    if len(hubs) > 1:
        form.fields["hub"] = forms.ChoiceField(
            label="Hub", choices=[(h.code, f"{h.code} {h.name}") for h in hubs], help_text=help_text
        )
