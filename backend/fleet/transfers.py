"""v2 03A-2: moving a robot or a vehicle to another hub (docs/13_v2_phase03_contract.md 11.5, correction
10.C).

COMMANDS. Each runs through run_command with the hub read from the RECORD (never from the form): the
permission is checked at that hub, the step is written ONLY by the SECURITY DEFINER function of fleet
migration 0015 (which locks the asset row, then checks the actor's grant at the hub of the locked row
again, and audits), and run_command writes its own audit row. A database refusal becomes a plain Conflict
or PermissionDenied.

    start_transfer    fleet.transfer          at the source (the asset's home hub)
    receive_transfer  fleet.transfer          at the destination, or at the source after an abandonment
    refuse_transfer   fleet.transfer          at the destination or the source
    abandon_transfer  fleet.abandon_transfer  at the source (ops lead)

READ MODEL. ``transfers(hub_ids)``: every transfer touching those hubs with its current state, one statement.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import DatabaseError, connection

from accounts.permissions import has_permission
from fleet.models import HubTransfer, Robot
from planning.models import Vehicle
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, run_command
from platform_core.middleware import current_request_id
from platform_core.models import Hub
from platform_core.timezones import localize_utc_text

KINDS = ("robot", "vehicle")
STATE_LABELS = dict(HubTransfer._meta.get_field("state").choices)  # type: ignore[arg-type]


def code_of(transfer_id: int) -> str:
    return f"TR-{transfer_id:04d}"


def open_transfer_text(kind: str, asset_id: int) -> str | None:
    """ "TR-0001 from HUB-1 to HB" when the asset's latest transfer is not received; else None."""
    field = "robot_id" if kind == "robot" else "vehicle_id"
    last = (
        HubTransfer.objects.filter(**{field: asset_id})
        .select_related("from_hub", "to_hub")
        .order_by("-id")
        .first()
    )
    if last is None or last.state == "received":
        return None
    extra = {"receipt_refused": ", receipt refused", "abandoned": ", abandoned"}.get(last.state, "")
    return f"{last.code} from {last.from_hub.code} to {last.to_hub.code}{extra}"


# What each step needs, in plain words: (what the viewer tried, who can do it), for the permission refusal.
ACTIONS = {
    "fleet.start_transfer": ("send it to another hub", "a hub technician of {code} or a regional ops lead"),
    "fleet.receive_transfer": ("receive this transfer", "a hub technician of {code} or a regional ops lead"),
    "fleet.refuse_transfer": ("refuse this receipt", "a hub technician of {code} or a regional ops lead"),
    "fleet.abandon_transfer": ("abandon this transfer", "a regional ops lead"),
}


def _plain(name: str, hub: Callable[[], int | None], call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a transfer command; a permission refusal names the record's hub and who can act there (never more
    of another hub than the record already shows), and refusal times are in the workspace zone."""
    try:
        return call()
    except PermissionDenied as exc:
        hub_id = hub()
        h = Hub.objects.filter(pk=hub_id).only("code", "name").first() if hub_id is not None else None
        what, who = ACTIONS.get(name, ("do this", "a regional ops lead"))
        label = "" if h is None else (h.name if h.name.lower().endswith("hub") else f"{h.name} hub")
        where = f" at {label} ({h.code})" if h is not None else ""
        raise PermissionDenied(
            f"You don't have permission to {what}{where}. Ask {who.format(code=h.code if h else 'that hub')}."
        ) from exc
    except Conflict as exc:
        raise Conflict(localize_utc_text(str(exc))) from exc


def _sql(sql: str, params: list[Any]) -> int:
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    except DatabaseError as exc:
        cause = exc.__cause__
        state = getattr(cause, "sqlstate", None)
        text = getattr(getattr(cause, "diag", None), "message_primary", None) or str(exc)
        if state == "42501":
            raise PermissionDenied(text) from exc
        if state in ("23514", "23505", "23503"):
            raise Conflict(text) from exc
        raise
    return int(row[0])


def _asset(kind: str, code: str) -> Robot | Vehicle:
    code = (code or "").strip().upper()
    if kind == "robot":
        robot = Robot.objects.filter(code=code).first()
        if robot is None:
            raise Invalid(f"No robot {code}")
        return robot
    if kind == "vehicle":
        vehicle = Vehicle.objects.filter(code=code).first()
        if vehicle is None:
            raise Invalid(f"No vehicle {code}")
        return vehicle
    raise Invalid("Choose a robot or a vehicle")


def _first_step(transfer_id: int) -> HubTransfer:
    s = HubTransfer.objects.filter(pk=transfer_id, seq=1).first()
    if s is None:
        raise Invalid("No such transfer")
    return s


def _latest(transfer_id: int) -> HubTransfer | None:
    return HubTransfer.objects.filter(transfer_id=transfer_id).order_by("-seq").first()


def start_transfer(
    actor: Any, *, kind: str, code: str, to_hub_code: str, reason: str, idempotency_key: str | None = None
) -> dict[str, Any]:
    """Send a robot or a vehicle to another hub. It is then on its way: dispatchable at neither hub until
    it is received. Refused (with every blocker listed) while it has an open run or trip, a booking that
    has not ended, an unused departure authorization, or is not verified at its hub."""
    name = "fleet.start_transfer"
    payload = {"kind": kind, "asset": code, "to": to_hub_code, "reason": reason}
    if kind not in KINDS:
        raise Invalid("Choose a robot or a vehicle")

    def hub() -> int | None:
        a = (Robot if kind == "robot" else Vehicle).objects.filter(code=(code or "").strip().upper()).first()
        return a.hub_id if a is not None else None

    def handler() -> Outcome:
        asset = _asset(kind, code)
        dest = Hub.objects.filter(code=(to_hub_code or "").strip().upper()).first()
        if dest is None:
            raise Invalid(f"No hub {to_hub_code}")
        tid = _sql(
            "SELECT fleet_transfer_start(%s, %s, %s, %s, %s, %s)",
            [actor.pk, kind, asset.pk, dest.pk, (reason or "").strip(), current_request_id.get()],
        )
        return Outcome("hub_transfer", code_of(tid), {"id": tid, "code": code_of(tid), "state": "in_transit"})

    return _plain(name, hub, lambda: run_command(
        actor, name, permission="fleet.transfer", payload=payload, handler=handler, hub=hub,
        idempotency_key=idempotency_key,
    ))  # fmt: skip


def _step(
    actor: Any,
    name: str,
    permission: str,
    transfer_id: int,
    sql: str,
    params: list[Any],
    hub: Callable[[], int | None],
    payload: dict[str, Any],
) -> dict[str, Any]:
    def handler() -> Outcome:
        _first_step(transfer_id)
        step = _sql(sql, [actor.pk, transfer_id, *params, current_request_id.get()])
        latest = _latest(transfer_id)
        state = latest.state if latest else ""
        return Outcome(
            "hub_transfer", code_of(transfer_id), {"id": transfer_id, "step_id": step, "state": state}
        )

    return _plain(
        name,
        hub,
        lambda: run_command(actor, name, permission=permission, payload=payload, handler=handler, hub=hub),
    )


def receive_transfer(actor: Any, *, transfer_id: int) -> dict[str, Any]:
    """Receive it at the destination hub, or back at the source after the ops lead abandoned the transfer. It
    becomes verified at that hub, which becomes its home hub. Holds are never touched."""

    def hub() -> int | None:
        latest = _latest(transfer_id)
        if latest is None:
            return None
        return latest.from_hub_id if latest.state == "abandoned" else latest.to_hub_id

    return _step(
        actor, "fleet.receive_transfer", "fleet.transfer", transfer_id,
        "SELECT fleet_transfer_receive(%s, %s, %s)", [], hub, {"transfer": transfer_id},
    )  # fmt: skip


def refuse_transfer(actor: Any, *, transfer_id: int, reason: str) -> dict[str, Any]:
    """The receipt is refused, with a reason: it stays on its way (dispatchable nowhere) until it is
    received or the ops lead abandons the transfer."""

    def hub() -> int | None:
        latest = _latest(transfer_id)
        if latest is None:
            return None
        if has_permission(actor, "fleet.transfer", hub=latest.to_hub_id):
            return latest.to_hub_id
        return latest.from_hub_id

    return _step(
        actor, "fleet.refuse_transfer", "fleet.transfer", transfer_id,
        "SELECT fleet_transfer_refuse(%s, %s, %s, %s)", [(reason or "").strip()], hub,
        {"transfer": transfer_id, "reason": reason},
    )  # fmt: skip


def abandon_transfer(actor: Any, *, transfer_id: int, reason: str) -> dict[str, Any]:
    """The ops lead abandons the transfer: it must then be received back at the source hub (an explicit
    receipt there); it can never be received at the destination any more."""

    def hub() -> int | None:
        latest = _latest(transfer_id)
        return latest.from_hub_id if latest is not None else None

    return _step(
        actor, "fleet.abandon_transfer", "fleet.abandon_transfer", transfer_id,
        "SELECT fleet_transfer_abandon(%s, %s, %s, %s)", [(reason or "").strip()], hub,
        {"transfer": transfer_id, "reason": reason},
    )  # fmt: skip


# ------------------------------------------------------------------ read model
@dataclass
class TransferRow:
    id: int
    code: str
    kind: str
    asset: str
    from_hub_id: int
    from_hub: str
    to_hub_id: int
    to_hub: str
    state: str
    reason: str
    started_at: datetime
    updated_at: datetime
    at_hub: str | None
    last_reason: str

    @property
    def state_label(self) -> str:
        return str(STATE_LABELS.get(self.state, self.state))

    @property
    def open(self) -> bool:
        return self.state != "received"

    @property
    def receive_hub_id(self) -> int:
        return self.from_hub_id if self.state == "abandoned" else self.to_hub_id


LIST_SQL = """
SELECT s.id, s.asset_kind, coalesce(r.code, v.code), s.from_hub_id, hf.code, s.to_hub_id, ht.code, l.state,
       s.reason, s.recorded_at, l.recorded_at, ha.code, l.reason
FROM fleet_hub_transfer s
JOIN LATERAL (SELECT t.* FROM fleet_hub_transfer t WHERE t.transfer_id = s.id ORDER BY t.seq DESC LIMIT 1) l
  ON true
JOIN platform_core_hub hf ON hf.id = s.from_hub_id
JOIN platform_core_hub ht ON ht.id = s.to_hub_id
LEFT JOIN platform_core_hub ha ON ha.id = l.at_hub_id
LEFT JOIN fleet_robot r ON r.id = s.robot_id
LEFT JOIN planning_vehicle v ON v.id = s.vehicle_id
WHERE s.seq = 1
  AND (%(hubs)s::bigint[] IS NULL OR s.from_hub_id = ANY(%(hubs)s::bigint[])
       OR s.to_hub_id = ANY(%(hubs)s::bigint[]))
  AND (%(asset_kind)s::text IS NULL
       OR (s.asset_kind = %(asset_kind)s AND (s.robot_id = %(asset)s OR s.vehicle_id = %(asset)s)))
ORDER BY (l.state = 'received'), s.id DESC
LIMIT %(limit)s
"""


def transfers(
    hub_ids: Any = None, *, asset_kind: str | None = None, asset_id: int | None = None, limit: int = 200
) -> list[TransferRow]:
    """Transfers from or to ``hub_ids`` (None = every hub), open ones first, newest first. One statement."""
    hubs = None if hub_ids is None else sorted(int(h) for h in hub_ids)
    with connection.cursor() as cur:
        cur.execute(LIST_SQL, {"hubs": hubs, "asset_kind": asset_kind, "asset": asset_id, "limit": limit})
        return [
            TransferRow(
                r[0], code_of(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12]
            )
            for r in cur.fetchall()
        ]
