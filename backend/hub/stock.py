"""v2 02D kit inventory: commands and read models (docs/11_v2_phase02_contract.md 8.5, 8.6, 8.8).

COMMANDS. Stock items, movements and reservations are written ONLY by the SECURITY DEFINER functions of hub
migration 0008; this module calls them with the actor, maps a refusal to a plain CommandError and records a
denied audit row (the function writes the committed one). Nothing here writes readiness evidence, an
authorization, a hold, custody or eligibility: stock never stands in for the departure equipment check.

READ MODELS (one statement each, whatever the number of items, movements or visits):

* ``levels()``: per item on hand, reserved, available, out with visits (reusable), the owner-provisioned
  minimum (``stock_minimums``; absent = "No minimum set") and whether available is below it. Every number is a
  sum over the ledger: there is no counter column. 02E: also how much of "reserved" is still held for visits
  that were cancelled (active reservations of a cancelled visit), so it is visible and can be released.
* ``visit_needs()``: for the visits leaving in the next two days, what each still needs of the items listed in
  ``stock_per_visit`` (absent = not counted per visit) after its reservation and what was already issued or
  used for it. Visits are served in departure order from what is available; a visit that cannot be served is
  **short**, and shows "At risk" on Today and on its visit page. Planning information only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from hub.models import StockItem, StockReservation
from planning.models import Visit
from platform_core.commands import CommandError, Conflict, Invalid, NotAuthenticated, PermissionDenied
from platform_core.middleware import current_request_id
from platform_core.models import AuditEvent

MOVE = "hub.stock_move"  # receive, issue, return, consume
ADJUST = "hub.stock_adjust"
RESERVE = "hub.stock_reserve"
MANAGE_ITEMS = "hub.manage_stock_items"
HORIZON = timedelta(days=2)

# The owner-provisioned per-item settings, parsed in SQL (same format fleet.provisioning validates).
SETTING_CTE = r"""
  SELECT DISTINCT ON (split_part(p, '=', 1)) split_part(p, '=', 1) AS code, split_part(p, '=', 2)::int AS n
  FROM fleet_deploymentsetting s, regexp_split_to_table(s.value, ',') p
  WHERE s.key = '%s' AND p ~ '^[A-Z0-9][A-Z0-9-]{0,11}=[0-9]{1,6}$'
  ORDER BY split_part(p, '=', 1)
"""
MINIMUMS_CTE = SETTING_CTE % "stock_minimums"
PER_VISIT_CTE = SETTING_CTE % "stock_per_visit"


# ------------------------------------------------------------ guarded writes (SECURITY DEFINER functions)
def _guarded(actor: Any, command: str, sql: str, params: list[Any], payload: dict[str, Any]) -> Any:
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise NotAuthenticated("Authentication required")
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, [actor.pk, *params, current_request_id.get()])
            row = cur.fetchone()
    except DatabaseError as exc:
        cause = exc.__cause__
        state = getattr(cause, "sqlstate", None)
        text = getattr(getattr(cause, "diag", None), "message_primary", None) or str(exc)
        if state == "42501":
            err: CommandError = PermissionDenied(text)
        elif state in ("23514", "23505", "23503"):
            if state == "23505":
                text = "Someone reserved this item for that visit at the same moment; reload and try again"
            err = Conflict(text)
        else:
            raise
        AuditEvent.objects.create(
            actor=actor, command=command, outcome=AuditEvent.OUTCOME_DENIED, payload=payload, reason=text,
            request_id=current_request_id.get(),
        )  # fmt: skip
        raise err from exc
    return row[0] if row else None


def _item(code: str) -> StockItem:
    it = StockItem.objects.filter(code=(code or "").strip().upper()).first()
    if it is None:
        raise Invalid(f"No stock item {code}")
    return it


def _visit_id(code: str | None) -> int | None:
    if not code:
        return None
    v = Visit.objects.filter(code=code.strip().upper()).only("id").first()
    if v is None:
        raise Invalid(f"No visit {code}")
    return v.pk


def create_item(actor: Any, *, code: str, name: str, kind: str, unit: str = "pieces") -> int:
    code, name, unit = (code or "").strip().upper(), (name or "").strip(), (unit or "").strip()
    payload = {"code": code, "name": name, "kind": kind, "unit": unit}
    return int(
        _guarded(actor, "hub.create_stock_item", "SELECT hub_create_stock_item(%s, %s, %s, %s, %s, %s)",
                 [code, name, kind, unit], payload)
    )  # fmt: skip


def receive(actor: Any, *, item_code: str, quantity: int, note: str = "") -> int:
    it = _item(item_code)
    payload = {"item": it.code, "quantity": quantity, "note": note}
    return int(
        _guarded(actor, "hub.receive_stock", "SELECT hub_receive_stock(%s, %s, %s, %s, %s)",
                 [it.pk, quantity, note.strip()], payload)
    )  # fmt: skip


def adjust(actor: Any, *, item_code: str, delta: int, reason: str) -> int:
    it = _item(item_code)
    payload = {"item": it.code, "delta": delta, "reason": reason}
    return int(
        _guarded(actor, "hub.adjust_stock", "SELECT hub_adjust_stock(%s, %s, %s, %s, %s)",
                 [it.pk, delta, reason], payload)
    )  # fmt: skip


def _take(fn: str, actor: Any, item_code: str, quantity: int, visit_code: str | None) -> int:
    it = _item(item_code)
    visit_id = _visit_id(visit_code)
    payload = {"item": it.code, "quantity": quantity, "visit": visit_code or None}
    return int(
        _guarded(actor, f"hub.{fn}_stock", f"SELECT hub_{fn}_stock(%s, %s, %s, %s, %s)",
                 [it.pk, quantity, visit_id], payload)
    )  # fmt: skip


def issue(actor: Any, *, item_code: str, quantity: int, visit_code: str | None = None) -> int:
    return _take("issue", actor, item_code, quantity, visit_code)


def return_stock(actor: Any, *, item_code: str, quantity: int, visit_code: str | None = None) -> int:
    return _take("return", actor, item_code, quantity, visit_code)


def consume(actor: Any, *, item_code: str, quantity: int, visit_code: str | None = None) -> int:
    return _take("consume", actor, item_code, quantity, visit_code)


def reserve(actor: Any, *, item_code: str, visit_code: str, quantity: int) -> int:
    """Set the visit's reservation of the item to ``quantity`` (creates it the first time; the same
    reservation id afterwards)."""
    it = _item(item_code)
    visit_id = _visit_id(visit_code)
    payload = {"item": it.code, "visit": visit_code, "quantity": quantity}
    return int(
        _guarded(actor, "hub.reserve_stock", "SELECT hub_reserve_stock(%s, %s, %s, %s, %s)",
                 [it.pk, visit_id, quantity], payload)
    )  # fmt: skip


def release(actor: Any, *, reservation_id: int, reason: str) -> None:
    _guarded(actor, "hub.release_stock", "SELECT hub_release_stock(%s, %s, %s, %s)",
             [reservation_id, reason], {"reservation_id": reservation_id, "reason": reason})  # fmt: skip


def release_for_cancelled_visit(actor: Any, *, reservation_id: int) -> str:
    """02E: the one-click release of a reservation whose visit was cancelled, through hub_release_stock (same
    role check, lock and audit) with the reason filled in. Refused for a visit that is not cancelled: that
    needs the release page and a reason. Returns the visit code."""
    r = StockReservation.objects.select_related("visit").filter(pk=reservation_id).first()
    if r is None:
        raise Invalid("No such stock reservation")
    if r.visit.status != "cancelled":
        raise Conflict(f"Visit {r.visit.code} is not cancelled; release it with a reason on its release page")
    release(actor, reservation_id=r.pk, reason=f"Visit {r.visit.code} was cancelled")
    return str(r.visit.code)


# ------------------------------------------------------------------ wording
def singular(unit: str) -> str:
    """The unit for a count of 1 (``boxes`` -> ``box``, ``kits`` -> ``kit``). Same rule as hub_stock_qty() in
    SQL (hub/0009)."""
    if re.search(r"(ch|sh|ss|x)es$", unit):
        return unit[:-2]
    if re.search(r"[^aeiou]ies$", unit):
        return unit[:-3] + "y"
    if re.search(r"[^s]s$", unit):
        return unit[:-1]
    return unit


def count_text(n: int, unit: str) -> str:
    """``1 box``, ``3 boxes``, ``0 boxes``."""
    return f"{n} {singular(unit) if n == 1 else unit}"


# ------------------------------------------------------------------ read models
LEVELS_SQL = (
    r"""
WITH mins AS MATERIALIZED ("""
    + MINIMUMS_CTE
    + r"""),
per AS MATERIALIZED ("""
    + PER_VISIT_CTE
    + r"""),
tot AS MATERIALIZED (
  SELECT m.item_id, sum(m.on_hand_delta) AS on_hand, sum(m.reserved_delta) AS reserved,
         sum(CASE m.kind WHEN 'issue' THEN m.quantity WHEN 'return' THEN -m.quantity ELSE 0 END) AS out_qty
  FROM hub_stock_movement m GROUP BY m.item_id
),
held AS MATERIALIZED (
  SELECT r.item_id, sum(r.quantity) AS q FROM hub_stock_reservation r
  JOIN planning_visit v ON v.id = r.visit_id
  WHERE r.released_at IS NULL AND v.status = 'cancelled' GROUP BY r.item_id
)
SELECT i.id, i.code, i.name, i.kind, i.unit, coalesce(t.on_hand, 0), coalesce(t.reserved, 0),
       coalesce(t.out_qty, 0), mins.n, per.n, coalesce(h.q, 0)
FROM hub_stock_item i
LEFT JOIN tot t ON t.item_id = i.id
LEFT JOIN held h ON h.item_id = i.id
LEFT JOIN mins ON mins.code = i.code
LEFT JOIN per ON per.code = i.code
ORDER BY i.code
"""
)


@dataclass
class Level:
    item_id: int
    code: str
    name: str
    kind: str
    unit: str
    on_hand: int
    reserved: int
    out: int
    minimum: int | None
    per_visit: int | None
    cancelled: int = 0  # 02E: of ``reserved``, how much is still held for visits that were cancelled

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved

    @property
    def on_hand_text(self) -> str:
        return count_text(self.on_hand, self.unit)

    @property
    def below_minimum(self) -> bool:
        return self.minimum is not None and self.available < self.minimum

    @property
    def reusable(self) -> bool:
        return self.kind == "reusable"


def levels() -> list[Level]:
    with connection.cursor() as cur:
        cur.execute(LEVELS_SQL)
        return [
            Level(r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6]), int(r[7]), r[8], r[9], int(r[10]))
            for r in cur.fetchall()
        ]


NEEDS_SQL = (
    r"""
WITH per AS MATERIALIZED ("""
    + PER_VISIT_CTE
    + r"""),
items AS MATERIALIZED (
  SELECT i.id, i.code, i.name, i.unit, per.n FROM hub_stock_item i
  JOIN per ON per.code = i.code WHERE per.n > 0
),
tot AS MATERIALIZED (
  SELECT m.item_id, sum(m.on_hand_delta) - sum(m.reserved_delta) AS available
  FROM hub_stock_movement m WHERE m.item_id IN (SELECT id FROM items) GROUP BY m.item_id
),
vis AS MATERIALIZED (
  SELECT v.id AS visit_id, v.code,
         vr.on_site_start - make_interval(mins => coalesce(vr.travel_out_min, 0)) AS departure_at
  FROM planning_visit v JOIN planning_visitrevision vr ON vr.id = v.current_revision_id
  WHERE v.status <> 'cancelled'
    AND vr.on_site_start - make_interval(mins => coalesce(vr.travel_out_min, 0)) >= %(at)s
    AND vr.on_site_start - make_interval(mins => coalesce(vr.travel_out_min, 0)) < %(until)s
),
cover AS MATERIALIZED (
  SELECT x.visit_id, x.item_id, sum(x.q) AS q FROM (
    SELECT r.visit_id, r.item_id, r.quantity AS q FROM hub_stock_reservation r
    WHERE r.released_at IS NULL AND r.visit_id IN (SELECT visit_id FROM vis)
    UNION ALL
    SELECT m.visit_id, m.item_id, m.quantity FROM hub_stock_movement m
    WHERE m.kind IN ('issue', 'consume') AND m.visit_id IN (SELECT visit_id FROM vis)
  ) x GROUP BY x.visit_id, x.item_id
),
gap AS (
  SELECT vis.visit_id, vis.code, vis.departure_at, items.id AS item_id, items.code AS item_code, items.name,
         items.unit, items.n, coalesce(c.q, 0) AS covered,
         greatest(items.n - coalesce(c.q, 0), 0) AS missing,
         greatest(coalesce(t.available, 0), 0) AS available
  FROM vis CROSS JOIN items
  LEFT JOIN cover c ON c.visit_id = vis.visit_id AND c.item_id = items.id
  LEFT JOIN tot t ON t.item_id = items.id
)
SELECT g.visit_id, g.code, g.departure_at, g.item_code, g.name, g.unit, g.n, g.covered, g.missing,
       g.available,
       sum(g.missing) OVER (PARTITION BY g.item_id ORDER BY g.departure_at, g.visit_id) > g.available AS short
FROM gap g
WHERE g.missing > 0
ORDER BY g.departure_at, g.code, g.item_code
"""
)


@dataclass
class Need:
    visit_id: int
    visit_code: str
    departure_at: datetime
    item_code: str
    item_name: str
    unit: str
    per_visit: int
    covered: int
    missing: int
    available: int
    short: bool

    @property
    def reason(self) -> str:
        need = count_text(self.per_visit, self.unit)
        return f"Kit short: {self.item_name} ({self.missing} of {need} not available)"


def visit_needs(at: datetime | None = None) -> list[Need]:
    """Every (visit, item) of the next two days that still needs stock, in departure order (one statement)."""
    at = at or timezone.now()
    with connection.cursor() as cur:
        cur.execute(NEEDS_SQL, {"at": at, "until": at + HORIZON})
        return [Need(*r) for r in cur.fetchall()]


def visit_shortages(at: datetime | None = None) -> dict[int, list[Need]]:
    """visit id -> the items it is short of (At risk)."""
    out: dict[int, list[Need]] = {}
    for n in visit_needs(at):
        if n.short:
            out.setdefault(n.visit_id, []).append(n)
    return out
