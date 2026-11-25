"""v2 Phase 03A-2 (docs/13_v2_phase03_contract.md 11.5, correction 10.C): stock transfers between hubs.

LEDGER. Two new movement kinds (hub/0012) in the append-only ledger, with their effect in the check constraint
hub_stock_movement_effect (re-created with the Phase 02 branches unchanged plus):

  transfer_out  at the source item: on hand -quantity, reserved 0, names to_hub_id (another hub), no visit;
  transfer_in   at the destination item: on hand +quantity, reserved 0, names transfer_of_id (the transfer_out;
                unique: one receipt per transfer), no visit.

While a transfer_out has no transfer_in the quantity counts at NEITHER hub (it left the source's on hand and has
not reached the destination's). Reconciliation at every stage: for an item code,
    sum(on hand at every hub) + sum(quantity of transfer_out rows with no transfer_in) = what it was before.

FUNCTIONS (SECURITY DEFINER, owned by ops_owner, fixed search_path, EXECUTE revoked from PUBLIC and granted to
ops_app only; the ledger stays SELECT-only for ops_app):

  hub_transfer_stock(actor, item, quantity, to_hub, request_id) -> transfer_out movement id
      hub_technician or ops_lead (any hub first, then AT THE ITEM'S HUB after its row lock); the item at the
      destination is matched by CODE (codes are unique per hub) and must exist there; on hand and available at
      the source stay >= 0 (hub_stock_apply's check: reserved stock cannot leave).
  hub_receive_transfer(actor, transfer_out movement, request_id) -> transfer_in movement id
      hub_technician or ops_lead AT THE DESTINATION hub, after the destination item's row lock. Two receipts of
      one transfer: the second waits on that lock, then finds the receipt and is refused (the unique
      transfer_of_id is the backstop).

Each writes one audit row (hub_audit). Reverse drops the functions and restores the Phase 02 effect constraint
(a database holding transfer movements cannot be reversed past hub/0012; it says so through the constraint).
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE
ROLES = "ARRAY['hub_technician', 'ops_lead']"

PHASE02_EFFECT = r"""
     (kind = 'receive' AND on_hand_delta = quantity AND reserved_delta = 0)
  OR (kind = 'adjust' AND on_hand_delta IN (quantity, -quantity) AND reserved_delta = 0 AND btrim(reason) <> '')
  OR (kind = 'reserve' AND on_hand_delta = 0 AND reserved_delta = quantity
      AND reservation_id IS NOT NULL AND visit_id IS NOT NULL)
  OR (kind = 'release' AND on_hand_delta = 0 AND reserved_delta = -quantity
      AND reservation_id IS NOT NULL AND visit_id IS NOT NULL)
  OR (kind IN ('issue', 'consume') AND on_hand_delta = -quantity AND reserved_delta BETWEEN -quantity AND 0
      AND (reserved_delta = 0 OR reservation_id IS NOT NULL))
  OR (kind = 'return' AND on_hand_delta = quantity AND reserved_delta = 0)"""

TRANSFER_EFFECT = r"""
  OR (kind = 'transfer_out' AND on_hand_delta = -quantity AND reserved_delta = 0 AND to_hub_id IS NOT NULL
      AND visit_id IS NULL AND reservation_id IS NULL AND transfer_of_id IS NULL)
  OR (kind = 'transfer_in' AND on_hand_delta = quantity AND reserved_delta = 0 AND transfer_of_id IS NOT NULL
      AND visit_id IS NULL AND reservation_id IS NULL AND to_hub_id IS NULL)"""

ONLY_TRANSFERS_NAME = r"""
ALTER TABLE hub_stock_movement ADD CONSTRAINT hub_stock_movement_transfer_refs CHECK (
  (kind = 'transfer_out' OR to_hub_id IS NULL) AND (kind = 'transfer_in' OR transfer_of_id IS NULL));
"""

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION hub_transfer_stock(p_actor bigint, p_item bigint, p_qty int, p_to_hub bigint,
                                              p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE it hub_stock_item; dest platform_core_hub; dest_item bigint; on_hand bigint; reserved bigint;
        new_id bigint; now_ts timestamptz;
BEGIN
  IF NOT hub_actor_has(p_actor, {roles}) THEN
    PERFORM hub_deny('Only the hub technician or the ops lead can send stock to another hub');
  END IF;
  it := hub_stock_lock(p_item);
  IF NOT hub_actor_has_at(p_actor, {roles}, it.hub_id) THEN
    PERFORM hub_deny(format('You hold no hub technician or ops lead role at hub %s', fleet_hub_code(it.hub_id)));
  END IF;
  PERFORM hub_stock_quantity_ok(p_qty);
  SELECT * INTO dest FROM platform_core_hub WHERE id = p_to_hub FOR SHARE;
  IF NOT FOUND THEN PERFORM fleet_fail('Choose the hub to send the stock to'); END IF;
  IF dest.id = it.hub_id THEN PERFORM fleet_fail(format('%s (%s) is already kept at hub %s', it.name, it.code, dest.code)); END IF;
  dest_item := (SELECT d.id FROM hub_stock_item d WHERE d.hub_id = dest.id AND d.code = it.code);
  IF dest_item IS NULL THEN
    PERFORM fleet_fail(format('Hub %s keeps no item %s; the ops lead adds it there first', dest.code, it.code));
  END IF;
  -- on hand and available at the source stay >= 0 (reserved stock never leaves); no row is written by this call
  PERFORM hub_stock_apply(it, NULL, p_qty, -p_qty, 0, NULL, NULL, NULL, p_actor, p_rid);
  now_ts := clock_timestamp();
  INSERT INTO hub_stock_movement (item_id, kind, quantity, on_hand_delta, reserved_delta, visit_id, reservation_id,
                                  reason, actor_id, occurred_at, request_id, to_hub_id, transfer_of_id)
  VALUES (it.id, 'transfer_out', p_qty, -p_qty, 0, NULL, NULL, 'to hub ' || dest.code, p_actor, now_ts,
          left(coalesce(p_rid, ''), 64), dest.id, NULL)
  RETURNING id INTO new_id;
  SELECT coalesce(sum(m.on_hand_delta), 0), coalesce(sum(m.reserved_delta), 0) INTO on_hand, reserved
  FROM hub_stock_movement m WHERE m.item_id = it.id;
  PERFORM hub_audit(p_actor, 'hub.transfer_stock', 'stock_movement', new_id::text,
                    jsonb_build_object('item', it.code, 'quantity', p_qty, 'from', fleet_hub_code(it.hub_id),
                                       'to', dest.code),
                    jsonb_build_object('on_hand', on_hand, 'reserved', reserved, 'available', on_hand - reserved),
                    p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION hub_receive_transfer(p_actor bigint, p_transfer bigint, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE m hub_stock_movement; src hub_stock_item; it hub_stock_item; dest_item bigint; on_hand bigint;
        reserved bigint; new_id bigint;
BEGIN
  IF NOT hub_actor_has(p_actor, {roles}) THEN
    PERFORM hub_deny('Only the hub technician or the ops lead can receive stock from another hub');
  END IF;
  SELECT * INTO m FROM hub_stock_movement WHERE id = p_transfer AND kind = 'transfer_out';
  IF m.id IS NULL THEN PERFORM fleet_fail('No such stock transfer'); END IF;
  SELECT * INTO src FROM hub_stock_item WHERE id = m.item_id;
  dest_item := (SELECT d.id FROM hub_stock_item d WHERE d.hub_id = m.to_hub_id AND d.code = src.code);
  IF dest_item IS NULL THEN
    PERFORM fleet_fail(format('Hub %s keeps no item %s', fleet_hub_code(m.to_hub_id), src.code));
  END IF;
  it := hub_stock_lock(dest_item);
  IF NOT hub_actor_has_at(p_actor, {roles}, it.hub_id) THEN
    PERFORM hub_deny(format('This stock is received at hub %s; you hold no hub technician or ops lead role there',
                            fleet_hub_code(it.hub_id)));
  END IF;
  IF EXISTS (SELECT 1 FROM hub_stock_movement r WHERE r.transfer_of_id = m.id) THEN
    PERFORM fleet_fail(format('This transfer of %s %s was already received at hub %s', m.quantity, src.unit,
                              fleet_hub_code(it.hub_id)));
  END IF;
  INSERT INTO hub_stock_movement (item_id, kind, quantity, on_hand_delta, reserved_delta, visit_id, reservation_id,
                                  reason, actor_id, occurred_at, request_id, to_hub_id, transfer_of_id)
  VALUES (it.id, 'transfer_in', m.quantity, m.quantity, 0, NULL, NULL, 'from hub ' || fleet_hub_code(src.hub_id),
          p_actor, clock_timestamp(), left(coalesce(p_rid, ''), 64), NULL, m.id)
  RETURNING id INTO new_id;
  SELECT coalesce(sum(x.on_hand_delta), 0), coalesce(sum(x.reserved_delta), 0) INTO on_hand, reserved
  FROM hub_stock_movement x WHERE x.item_id = it.id;
  PERFORM hub_audit(p_actor, 'hub.receive_transfer', 'stock_movement', new_id::text,
                    jsonb_build_object('item', it.code, 'quantity', m.quantity, 'transfer', m.id,
                                       'from', fleet_hub_code(src.hub_id), 'at', fleet_hub_code(it.hub_id)),
                    jsonb_build_object('on_hand', on_hand, 'reserved', reserved, 'available', on_hand - reserved),
                    p_rid);
  RETURN new_id;
END $fn$;
"""

SIGNATURES = [
    "hub_transfer_stock(bigint, bigint, int, bigint, text)",
    "hub_receive_transfer(bigint, bigint, text)",
]

forward = (
    "ALTER TABLE hub_stock_movement DROP CONSTRAINT hub_stock_movement_effect;\n"
    f"ALTER TABLE hub_stock_movement ADD CONSTRAINT hub_stock_movement_effect CHECK ({PHASE02_EFFECT}{TRANSFER_EFFECT}\n);\n"
    + ONLY_TRANSFERS_NAME
    + FUNCTIONS.replace("{roles}", ROLES)
    + "".join(
        f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {s} TO {APP};\n" for s in SIGNATURES
    )
)
reverse = (
    "".join(f"DROP FUNCTION IF EXISTS {s};\n" for s in SIGNATURES)
    + "ALTER TABLE hub_stock_movement DROP CONSTRAINT IF EXISTS hub_stock_movement_transfer_refs;\n"
    + "ALTER TABLE hub_stock_movement DROP CONSTRAINT hub_stock_movement_effect;\n"
    + f"ALTER TABLE hub_stock_movement ADD CONSTRAINT hub_stock_movement_effect CHECK ({PHASE02_EFFECT}\n);\n"
)


class Migration(migrations.Migration):
    dependencies = [
        ("hub", "0012_stock_transfer"),
        ("fleet", "0015_location_and_transfer_sql"),
    ]
    operations = [migrations.RunSQL(forward, reverse)]
