"""v2 02D kit inventory: the append-only stock ledger, its invariants and the trusted write boundary (contract 11,
8.5, 8.6, 8.8).

LEDGER. ``hub_stock_movement`` is append-only (reject_mutation trigger on UPDATE, DELETE and TRUNCATE). Each row
stores the quantity the person entered and its signed effects, checked per kind by ``hub_stock_movement_effect``:

    receive  on hand +q                     adjust   on hand +q or -q (a reason is required)
    reserve  reserved +q                    release  reserved -q
    issue    on hand -q, reserved -0..q     consume  on hand -q, reserved -0..q  (draws the visit's reservation)
    return   on hand +q

    on hand = sum(on_hand_delta); reserved = sum(reserved_delta) = the active reservations; available = on hand
    - reserved. There is no counter column anywhere.

INVARIANTS. Every write takes the item's row lock (FOR UPDATE) FIRST, then reads the item's totals (a fresh
snapshot after the lock) and refuses before inserting when on hand or available would go below 0. Two
simultaneous movements of one item are therefore serialized: the second re-reads after the first commits. An
over-reservation is refused atomically by hub_reserve_stock(). The partial unique index (one active reservation
per item and visit) is the last line for two simultaneous first reservations of one visit.

KIND RULES. Return is for reusable items only, and never more than is out with visits (issued - returned, for
the visit when one is named). Consume is for consumables only. Issue works for both (a kit leaves the hub with a
visit; a consumable is handed out).

WRITE BOUNDARY. ops_app has SELECT only on hub_stock_item, hub_stock_reservation and hub_stock_movement (and no
use of their sequences). Every write goes through these SECURITY DEFINER functions owned by ops_owner, with a
fixed search_path, EXECUTE revoked from PUBLIC and granted to ops_app only:

    hub_create_stock_item(actor, code, name, kind, unit, request_id) -> item id      (admin, ops_lead)
    hub_receive_stock(actor, item, quantity, note, request_id) -> movement id       (hub_technician, ops_lead)
    hub_adjust_stock(actor, item, delta, reason, request_id) -> movement id         (hub_technician, ops_lead)
    hub_issue_stock(actor, item, quantity, visit, request_id) -> movement id        (hub_technician, ops_lead)
    hub_return_stock(actor, item, quantity, visit, request_id) -> movement id       (hub_technician, ops_lead)
    hub_consume_stock(actor, item, quantity, visit, request_id) -> movement id      (hub_technician, ops_lead)
    hub_reserve_stock(actor, item, visit, quantity, request_id) -> reservation id   (hub_technician, ops_lead)
    hub_release_stock(actor, reservation, reason, request_id)                       (hub_technician, ops_lead)

Each checks the named actor holds an active allowed role (hub_actor_has, as 02B), validates, writes the
movement and the audit row. A refusal raises (insufficient_privilege for a role, check_violation otherwise) and
writes nothing. The helpers below are not SECURITY DEFINER and not executable by ops_app or PUBLIC.

Nothing here reads or writes readiness evidence, an authorization, a hold, custody or eligibility: stock
availability never replaces the departure equipment check (8.5).
"""

from django.conf import settings
from django.db import migrations

APP = settings.DB_APP_ROLE
ROLES = "ARRAY['hub_technician', 'ops_lead']"
WHO = "Only the hub technician or the ops lead can"

CONSTRAINTS = r"""
ALTER TABLE hub_stock_movement ADD CONSTRAINT hub_stock_movement_effect CHECK (
     (kind = 'receive' AND on_hand_delta = quantity AND reserved_delta = 0)
  OR (kind = 'adjust' AND on_hand_delta IN (quantity, -quantity) AND reserved_delta = 0 AND btrim(reason) <> '')
  OR (kind = 'reserve' AND on_hand_delta = 0 AND reserved_delta = quantity
      AND reservation_id IS NOT NULL AND visit_id IS NOT NULL)
  OR (kind = 'release' AND on_hand_delta = 0 AND reserved_delta = -quantity
      AND reservation_id IS NOT NULL AND visit_id IS NOT NULL)
  OR (kind IN ('issue', 'consume') AND on_hand_delta = -quantity AND reserved_delta BETWEEN -quantity AND 0
      AND (reserved_delta = 0 OR reservation_id IS NOT NULL))
  OR (kind = 'return' AND on_hand_delta = quantity AND reserved_delta = 0)
);
ALTER TABLE hub_stock_item ADD CONSTRAINT hub_stock_item_code_format CHECK (code ~ '^[A-Z0-9][A-Z0-9-]{0,11}$');
CREATE TRIGGER hub_stock_movement_append_only BEFORE UPDATE OR DELETE ON hub_stock_movement
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER hub_stock_movement_no_truncate BEFORE TRUNCATE ON hub_stock_movement
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER hub_stock_reservation_no_delete BEFORE DELETE ON hub_stock_reservation
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER hub_stock_reservation_no_truncate BEFORE TRUNCATE ON hub_stock_reservation
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER hub_stock_item_no_delete BEFORE DELETE ON hub_stock_item
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER hub_stock_item_no_truncate BEFORE TRUNCATE ON hub_stock_item
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
"""

HELPERS = r"""
-- The item's row lock: every stock write takes it first, so movements of one item are serialized.
CREATE OR REPLACE FUNCTION hub_stock_lock(p_item bigint) RETURNS hub_stock_item
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE it hub_stock_item;
BEGIN
  SELECT * INTO it FROM hub_stock_item WHERE id = p_item FOR UPDATE;
  IF NOT FOUND THEN PERFORM fleet_fail('No such stock item'); END IF;
  RETURN it;
END $$;

CREATE OR REPLACE FUNCTION hub_stock_quantity_ok(q int) RETURNS void
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF q IS NULL OR q < 1 OR q > 100000 THEN PERFORM fleet_fail('Enter a whole number from 1 to 100000'); END IF;
END $$;

-- Check the invariants for a change (d_on, d_res) of a LOCKED item, then (when p_kind is not NULL) append the
-- movement and its audit row. Totals are read after the lock: a fresh snapshot in READ COMMITTED.
CREATE OR REPLACE FUNCTION hub_stock_apply(it hub_stock_item, p_kind text, p_qty int, d_on int, d_res int,
                                           p_visit bigint, p_res bigint, p_reason text, p_actor bigint,
                                           p_rid text) RETURNS bigint
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE on_hand bigint; reserved bigint; new_id bigint; now_ts timestamptz := clock_timestamp();
BEGIN
  SELECT coalesce(sum(m.on_hand_delta), 0), coalesce(sum(m.reserved_delta), 0) INTO on_hand, reserved
  FROM hub_stock_movement m WHERE m.item_id = it.id;
  IF on_hand + d_on < 0 THEN
    PERFORM fleet_fail(format('Only %s %s of %s (%s) are on hand', on_hand, it.unit, it.name, it.code));
  END IF;
  IF (on_hand + d_on) - (reserved + d_res) < 0 THEN
    IF d_res > 0 THEN
      PERFORM fleet_fail(format('Only %s %s of %s (%s) are available to reserve (%s on hand, %s already reserved for visits)',
                                greatest(on_hand - reserved, 0), it.unit, it.name, it.code, on_hand, reserved));
    END IF;
    PERFORM fleet_fail(format('Only %s %s of %s (%s) are available (%s on hand, %s reserved for visits; release a reservation first)',
                              greatest(on_hand - reserved, 0), it.unit, it.name, it.code, on_hand, reserved));
  END IF;
  IF p_kind IS NULL THEN RETURN NULL; END IF;
  INSERT INTO hub_stock_movement (item_id, kind, quantity, on_hand_delta, reserved_delta, visit_id, reservation_id,
                                  reason, actor_id, occurred_at, request_id)
  VALUES (it.id, p_kind, p_qty, d_on, d_res, p_visit, p_res, coalesce(btrim(p_reason), ''), p_actor, now_ts,
          left(coalesce(p_rid, ''), 64))
  RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'hub.' || p_kind || '_stock', 'stock_movement', new_id::text,
                    jsonb_build_object('item', it.code, 'quantity', p_qty, 'visit_id', p_visit,
                                       'reservation_id', p_res, 'reason', coalesce(btrim(p_reason), '')),
                    jsonb_build_object('on_hand', on_hand + d_on, 'reserved', reserved + d_res,
                                       'available', (on_hand + d_on) - (reserved + d_res)), p_rid);
  RETURN new_id;
END $$;

-- A visit stock can be reserved for, issued to or consumed for: exists, not cancelled; for a reservation, not
-- over yet.
CREATE OR REPLACE FUNCTION hub_stock_visit(p_visit bigint, p_for_reservation boolean) RETURNS planning_visit
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v planning_visit; ends timestamptz;
BEGIN
  SELECT * INTO v FROM planning_visit WHERE id = p_visit;
  IF NOT FOUND OR v.status = 'cancelled' THEN PERFORM fleet_fail('That visit does not exist or is cancelled'); END IF;
  IF p_for_reservation THEN
    SELECT vr.on_site_end INTO ends FROM planning_visitrevision vr WHERE vr.id = v.current_revision_id;
    IF ends IS NOT NULL AND ends <= clock_timestamp() THEN
      PERFORM fleet_fail(format('Visit %s is already over', v.code));
    END IF;
  END IF;
  RETURN v;
END $$;

-- Issue or consume: draw the visit's active reservation down first (the stock was held for it).
CREATE OR REPLACE FUNCTION hub_stock_take(p_kind text, p_actor bigint, p_item bigint, p_qty int, p_visit bigint,
                                          p_rid text) RETURNS bigint
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE it hub_stock_item; r hub_stock_reservation; drawn int := 0; mid bigint;
BEGIN
  PERFORM hub_stock_quantity_ok(p_qty);
  it := hub_stock_lock(p_item);
  IF p_kind = 'consume' AND it.kind <> 'consumable' THEN
    PERFORM fleet_fail(format('%s (%s) is a reusable kit: issue it and record its return; to write one off, adjust with a reason',
                              it.name, it.code));
  END IF;
  IF p_visit IS NOT NULL THEN
    PERFORM hub_stock_visit(p_visit, false);
    SELECT * INTO r FROM hub_stock_reservation
    WHERE item_id = p_item AND visit_id = p_visit AND released_at IS NULL FOR UPDATE;
    IF FOUND THEN drawn := least(p_qty, r.quantity); END IF;
  END IF;
  mid := hub_stock_apply(it, p_kind, p_qty, -p_qty, -drawn, p_visit, CASE WHEN drawn > 0 THEN r.id END, '',
                         p_actor, p_rid);
  IF drawn > 0 AND drawn = r.quantity THEN
    UPDATE hub_stock_reservation SET released_at = clock_timestamp(), released_by_id = p_actor,
           released_reason = CASE WHEN p_kind = 'issue' THEN 'Issued for the visit' ELSE 'Used for the visit' END,
           updated_at = clock_timestamp()
    WHERE id = r.id;
  ELSIF drawn > 0 THEN
    UPDATE hub_stock_reservation SET quantity = quantity - drawn, updated_at = clock_timestamp() WHERE id = r.id;
  END IF;
  RETURN mid;
END $$;
"""

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION hub_create_stock_item(p_actor bigint, p_code text, p_name text, p_kind text,
                                                 p_unit text, p_rid text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE new_id bigint; unit text := coalesce(nullif(btrim(p_unit), ''), 'pieces');
BEGIN
  IF NOT hub_actor_has(p_actor, ARRAY['ops_lead', 'admin']) THEN
    PERFORM hub_deny('Only the ops lead or the workspace admin can add a stock item');
  END IF;
  IF p_code IS NULL OR p_code !~ '^[A-Z0-9][A-Z0-9-]{0,11}$' THEN
    PERFORM fleet_fail('The item code uses capital letters, digits and dashes, up to 12 characters');
  END IF;
  IF coalesce(btrim(p_name), '') = '' OR length(p_name) > 80 THEN
    PERFORM fleet_fail('Give the item a name (up to 80 characters)');
  END IF;
  IF p_kind IS NULL OR p_kind NOT IN ('reusable', 'consumable') THEN
    PERFORM fleet_fail('Choose whether the item is reusable or consumable');
  END IF;
  IF length(unit) > 20 THEN PERFORM fleet_fail('The unit is up to 20 characters, for example kits'); END IF;
  IF EXISTS (SELECT 1 FROM hub_stock_item WHERE code = p_code) THEN
    PERFORM fleet_fail(format('A stock item with code %s already exists', p_code));
  END IF;
  INSERT INTO hub_stock_item (code, name, kind, unit, hub_id, created_by_id, created_at)
  VALUES (p_code, btrim(p_name), p_kind, unit, (SELECT min(id) FROM platform_core_hub), p_actor, clock_timestamp())
  RETURNING id INTO new_id;
  PERFORM hub_audit(p_actor, 'hub.create_stock_item', 'stock_item', p_code,
                    jsonb_build_object('name', btrim(p_name), 'kind', p_kind, 'unit', unit),
                    jsonb_build_object('item_id', new_id), p_rid);
  RETURN new_id;
END $fn$;

CREATE OR REPLACE FUNCTION hub_receive_stock(p_actor bigint, p_item bigint, p_qty int, p_note text, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE it hub_stock_item;
BEGIN
  IF NOT hub_actor_has(p_actor, %(roles)s) THEN PERFORM hub_deny('%(who)s record stock received'); END IF;
  PERFORM hub_stock_quantity_ok(p_qty);
  IF length(coalesce(p_note, '')) > 200 THEN PERFORM fleet_fail('The note is up to 200 characters'); END IF;
  it := hub_stock_lock(p_item);
  RETURN hub_stock_apply(it, 'receive', p_qty, p_qty, 0, NULL, NULL, p_note, p_actor, p_rid);
END $fn$;

CREATE OR REPLACE FUNCTION hub_adjust_stock(p_actor bigint, p_item bigint, p_delta int, p_reason text, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE it hub_stock_item;
BEGIN
  IF NOT hub_actor_has(p_actor, %(roles)s) THEN PERFORM hub_deny('%(who)s adjust stock'); END IF;
  IF p_delta IS NULL OR p_delta = 0 OR abs(p_delta) > 100000 THEN
    PERFORM fleet_fail('An adjustment is a whole number from -100000 to 100000, not 0');
  END IF;
  IF coalesce(btrim(p_reason), '') = '' OR length(p_reason) > 200 THEN
    PERFORM fleet_fail('An adjustment needs a reason (up to 200 characters)');
  END IF;
  it := hub_stock_lock(p_item);
  RETURN hub_stock_apply(it, 'adjust', abs(p_delta), p_delta, 0, NULL, NULL, p_reason, p_actor, p_rid);
END $fn$;

CREATE OR REPLACE FUNCTION hub_issue_stock(p_actor bigint, p_item bigint, p_qty int, p_visit bigint, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
BEGIN
  IF NOT hub_actor_has(p_actor, %(roles)s) THEN PERFORM hub_deny('%(who)s issue stock'); END IF;
  RETURN hub_stock_take('issue', p_actor, p_item, p_qty, p_visit, p_rid);
END $fn$;

CREATE OR REPLACE FUNCTION hub_consume_stock(p_actor bigint, p_item bigint, p_qty int, p_visit bigint, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
BEGIN
  IF NOT hub_actor_has(p_actor, %(roles)s) THEN PERFORM hub_deny('%(who)s record stock used'); END IF;
  RETURN hub_stock_take('consume', p_actor, p_item, p_qty, p_visit, p_rid);
END $fn$;

CREATE OR REPLACE FUNCTION hub_return_stock(p_actor bigint, p_item bigint, p_qty int, p_visit bigint, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE it hub_stock_item; out_now bigint;
BEGIN
  IF NOT hub_actor_has(p_actor, %(roles)s) THEN PERFORM hub_deny('%(who)s record stock returned'); END IF;
  PERFORM hub_stock_quantity_ok(p_qty);
  it := hub_stock_lock(p_item);
  IF it.kind <> 'reusable' THEN
    PERFORM fleet_fail(format('%s (%s) is a consumable and does not come back; record what was used as consumed',
                              it.name, it.code));
  END IF;
  IF p_visit IS NOT NULL AND NOT EXISTS (SELECT 1 FROM planning_visit WHERE id = p_visit) THEN
    PERFORM fleet_fail('No such visit');
  END IF;
  SELECT coalesce(sum(CASE WHEN m.kind = 'issue' THEN m.quantity ELSE -m.quantity END), 0) INTO out_now
  FROM hub_stock_movement m
  WHERE m.item_id = p_item AND m.kind IN ('issue', 'return')
    AND (p_visit IS NULL OR m.visit_id = p_visit);
  IF p_qty > out_now THEN
    PERFORM fleet_fail(format('Only %s %s of %s (%s) are out%s, so %s cannot come back', out_now, it.unit, it.name,
                              it.code, CASE WHEN p_visit IS NULL THEN ' with visits' ELSE ' with that visit' END,
                              p_qty));
  END IF;
  RETURN hub_stock_apply(it, 'return', p_qty, p_qty, 0, p_visit, NULL, '', p_actor, p_rid);
END $fn$;

-- Sets the visit's reservation of the item to p_qty: the first call creates it, later calls change its quantity
-- (the same reservation id: re-planning never duplicates it). Refuses over-reservation atomically.
CREATE OR REPLACE FUNCTION hub_reserve_stock(p_actor bigint, p_item bigint, p_visit bigint, p_qty int, p_rid text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE it hub_stock_item; r hub_stock_reservation; delta int; now_ts timestamptz := clock_timestamp(); rid bigint;
BEGIN
  IF NOT hub_actor_has(p_actor, %(roles)s) THEN PERFORM hub_deny('%(who)s reserve stock for a visit'); END IF;
  PERFORM hub_stock_quantity_ok(p_qty);
  IF p_visit IS NULL THEN PERFORM fleet_fail('Choose the visit the stock is for'); END IF;
  it := hub_stock_lock(p_item);
  PERFORM hub_stock_visit(p_visit, true);
  SELECT * INTO r FROM hub_stock_reservation
  WHERE item_id = p_item AND visit_id = p_visit AND released_at IS NULL FOR UPDATE;
  IF FOUND THEN
    delta := p_qty - r.quantity;
    IF delta = 0 THEN RETURN r.id; END IF;
    IF delta > 0 THEN
      PERFORM hub_stock_apply(it, 'reserve', delta, 0, delta, p_visit, r.id, '', p_actor, p_rid);
    ELSE
      PERFORM hub_stock_apply(it, 'release', -delta, 0, delta, p_visit, r.id, 'Reservation reduced', p_actor, p_rid);
    END IF;
    UPDATE hub_stock_reservation SET quantity = p_qty, updated_at = now_ts WHERE id = r.id;
    RETURN r.id;
  END IF;
  PERFORM hub_stock_apply(it, NULL, p_qty, 0, p_qty, NULL, NULL, NULL, p_actor, p_rid);  -- check only
  INSERT INTO hub_stock_reservation (item_id, visit_id, quantity, created_by_id, created_at, updated_at,
                                     released_reason)
  VALUES (p_item, p_visit, p_qty, p_actor, now_ts, now_ts, '') RETURNING id INTO rid;
  PERFORM hub_stock_apply(it, 'reserve', p_qty, 0, p_qty, p_visit, rid, '', p_actor, p_rid);
  RETURN rid;
END $fn$;

CREATE OR REPLACE FUNCTION hub_release_stock(p_actor bigint, p_reservation bigint, p_reason text, p_rid text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE it hub_stock_item; r hub_stock_reservation;
BEGIN
  IF NOT hub_actor_has(p_actor, %(roles)s) THEN PERFORM hub_deny('%(who)s release reserved stock'); END IF;
  IF coalesce(btrim(p_reason), '') = '' OR length(p_reason) > 200 THEN
    PERFORM fleet_fail('Give a short reason (up to 200 characters)');
  END IF;
  SELECT * INTO r FROM hub_stock_reservation WHERE id = p_reservation;
  IF NOT FOUND THEN PERFORM fleet_fail('No such stock reservation'); END IF;
  it := hub_stock_lock(r.item_id);
  SELECT * INTO r FROM hub_stock_reservation WHERE id = p_reservation FOR UPDATE;
  IF r.released_at IS NOT NULL THEN PERFORM fleet_fail('That reservation was already released'); END IF;
  PERFORM hub_stock_apply(it, 'release', r.quantity, 0, -r.quantity, r.visit_id, r.id, p_reason, p_actor, p_rid);
  UPDATE hub_stock_reservation SET released_at = clock_timestamp(), released_by_id = p_actor,
         released_reason = btrim(p_reason), updated_at = clock_timestamp()
  WHERE id = p_reservation;
END $fn$;
""".replace("%(roles)s", ROLES).replace("%(who)s", WHO)

SIGNATURES = [
    "hub_create_stock_item(bigint, text, text, text, text, text)",
    "hub_receive_stock(bigint, bigint, int, text, text)",
    "hub_adjust_stock(bigint, bigint, int, text, text)",
    "hub_issue_stock(bigint, bigint, int, bigint, text)",
    "hub_consume_stock(bigint, bigint, int, bigint, text)",
    "hub_return_stock(bigint, bigint, int, bigint, text)",
    "hub_reserve_stock(bigint, bigint, bigint, int, text)",
    "hub_release_stock(bigint, bigint, text, text)",
]
HELPER_SIGNATURES = [
    "hub_stock_take(text, bigint, bigint, int, bigint, text)",
    "hub_stock_visit(bigint, boolean)",
    "hub_stock_apply(hub_stock_item, text, int, int, int, bigint, bigint, text, bigint, text)",
    "hub_stock_quantity_ok(int)",
    "hub_stock_lock(bigint)",
]
GUARDED = ["hub_stock_item", "hub_stock_reservation", "hub_stock_movement"]

forward = (
    CONSTRAINTS
    + HELPERS
    + FUNCTIONS
    + "".join(
        f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION {s} TO {APP};\n"
        for s in SIGNATURES
    )
    + "".join(f"REVOKE ALL ON FUNCTION {s} FROM PUBLIC;\n" for s in HELPER_SIGNATURES)
    + "".join(
        f"REVOKE ALL ON {t} FROM {APP};\nGRANT SELECT ON {t} TO {APP};\nREVOKE ALL ON SEQUENCE {t}_id_seq FROM {APP};\n"
        for t in GUARDED
    )
)

reverse = (
    "".join(f"DROP FUNCTION IF EXISTS {s};\n" for s in SIGNATURES + HELPER_SIGNATURES)
    + "".join(
        f"DROP TRIGGER IF EXISTS {name} ON {table};\n"
        for name, table in (
            ("hub_stock_movement_append_only", "hub_stock_movement"),
            ("hub_stock_movement_no_truncate", "hub_stock_movement"),
            ("hub_stock_reservation_no_delete", "hub_stock_reservation"),
            ("hub_stock_reservation_no_truncate", "hub_stock_reservation"),
            ("hub_stock_item_no_delete", "hub_stock_item"),
            ("hub_stock_item_no_truncate", "hub_stock_item"),
        )
    )
    + "ALTER TABLE hub_stock_movement DROP CONSTRAINT IF EXISTS hub_stock_movement_effect;\n"
    + "ALTER TABLE hub_stock_item DROP CONSTRAINT IF EXISTS hub_stock_item_code_format;\n"
)


class Migration(migrations.Migration):
    dependencies = [
        ("hub", "0007_stock"),
        ("hub", "0004_charging_write_boundary"),
        ("platform_core", "0002_db_security"),
        ("fleet", "0002_db_enforcement"),
    ]
    operations = [migrations.RunSQL(forward, reverse)]
