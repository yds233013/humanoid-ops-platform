"""Synthetic execution world on top of the readiness world (Slice 4). SYNTHETIC data only.

    w = build_execution("ex_")
    w.run                 ExecutionRun opened by the real departure (readiness commands)
    w.visit               the departed visit code; w.robot the run's Robot
    w.crew_lead / w.crew_second   Persons P-1 (w.ops, ops_lead) and P-2 (w.field, field_operator)
    w.other_field         Person P-3 (w.field2, field_operator), not crew of the run
    w.template            synthetic_demo SiteCheckTemplate "SC1" with items c1, c2

Departure goes through readiness.commands.authorize_departure / record_departure, so the run and the first
custody event (hub -> vehicle, administrative, custodian = revision lead) are created by the database trigger.

Raw helpers take a psycopg cursor (normally the ops_app connection `app_role_conn`) and insert exactly one
row each, returning its id. Defaults follow the happy path; pass keyword arguments to break one condition:

    raw_insert(cur, table, **cols)                       generic INSERT ... RETURNING id (dict/list -> jsonb)
    raw_verify(cur, w, event_id, verifier="P-2")         CustodyVerification
    raw_custody(cur, w, from_kind, to_kind, ...)         CustodyEvent (seq = previous + 1 unless given)
    raw_trip_event(cur, w, kind)                         TripEvent (seq from the kind)
    raw_site_check(cur, w, ...)                          SiteCheck (consent_count / result default: computed)
    raw_session(cur, w, kind, ...)                       SessionEvent (seq = previous + 1 unless given)
    raw_interruption(cur, w, ...) / raw_resolve(...)     Interruption / InterruptionResolution
    raw_incident(conn, w, ...)                           Hold + Incident (+ optional unaccounted custody) in ONE
                                                         transaction on `conn` (a psycopg connection)
    raw_disposition(cur, w, decision, ...)               RecoveryDisposition
    raw_reconcile(cur, w, ...) / raw_close(cur, w, rec)  ReturnReconciliation / TripClosure

Walkthrough steps (each built from the helpers above):
    to_site(cur, w)           verify the departure assignment, arrived_site, vehicle -> site handoff
    go_check(cur, w)          a 'go' site check, returns its id
    to_hub(cur, w)            site -> vehicle, departed_site, arrived_hub, vehicle -> hub handoff; returns the
                              hub receipt id
    post_visit_inspection(w)  a complete PASS fleet inspection with visit_ref = the visit; returns its id
    close_run(cur, w)         reconcile (reconciled) + close the trip; returns the closure id
"""

import time
from types import SimpleNamespace
from typing import Any

from psycopg.types.json import Jsonb

from execution.models import CustodyEvent, ExecutionRun, SiteCheckTemplate
from fleet import services as fleet_services
from fleet.models import Hold, HoldPolicy, Inspection, Robot
from planning.models import Person
from readiness import commands as rc
from tests.readiness_world import build_readiness, record_inspection

TEMPLATE_ITEMS = [
    {"id": "c1", "label": "SYNTHETIC: walkway clear"},
    {"id": "c2", "label": "SYNTHETIC: participants briefed"},
]
ALL_OK = {"c1": "ok", "c2": "ok"}
TRIP_SEQ = {"arrived_site": 1, "departed_site": 2, "arrived_hub": 3}


def person(code: str) -> Person:
    return Person.objects.get(code=code)


def pid(code: str | None) -> int | None:
    return None if code is None else person(code).pk


def depart(w: SimpleNamespace, visit_code: str | None = None) -> ExecutionRun:
    """Authorize + record the departure of `visit_code` (default w.visit) through the real commands."""
    code = visit_code or w.visit
    auth = rc.authorize_departure(w.ops, visit_code=code)["code"]
    rc.record_departure(w.ops, authorization_code=auth)
    return ExecutionRun.objects.get(visit__code=code)


def build_execution(prefix: str = "", robots: int = 3) -> SimpleNamespace:
    w = build_readiness(prefix=prefix, robots=robots)
    w.run = depart(w)
    w.robot = Robot.objects.get(pk=w.run.robot_id)
    w.crew_lead, w.crew_second, w.other_field = person("P-1"), person("P-2"), person("P-3")
    w.template = SiteCheckTemplate.objects.create(
        version="SC1", items=TEMPLATE_ITEMS, source="synthetic_demo", recorded_by=w.ops
    )
    return w


# ------------------------------------------------------------------ raw inserts
def _val(v: Any) -> Any:
    return Jsonb(v) if isinstance(v, dict | list) else v


def raw_insert(cur, table: str, **cols: Any) -> int:
    cols.setdefault("recorded_at", None)  # forced by the trigger; NULL proves the trigger sets it
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    cur.execute(
        f"INSERT INTO {table} ({names}) VALUES ({marks}) RETURNING id", [_val(v) for v in cols.values()]
    )
    return int(cur.fetchone()[0])


def _uid(w: SimpleNamespace, by: Any) -> int:
    return (by or w.ops).pk


def next_custody_seq(robot_id: int) -> int:
    last = CustodyEvent.objects.filter(robot_id=robot_id).order_by("-seq").first()
    return 1 if last is None else last.seq + 1


def latest_custody(w: SimpleNamespace) -> CustodyEvent:
    return CustodyEvent.objects.filter(robot_id=w.run.robot_id).order_by("-seq").first()


def raw_verify(
    cur, w: SimpleNamespace, event_id: int | None = None, verifier: str = "P-2", by: Any = None
) -> int:
    if event_id is None:
        event_id = CustodyEvent.objects.get(run=w.run, seq=1).pk  # the departure assignment
    return raw_insert(
        cur,
        "execution_custodyverification",
        custody_event_id=event_id,
        verified_by_id=pid(verifier),
        recorded_by_id=_uid(w, by),
    )


def raw_custody(
    cur,
    w: SimpleNamespace,
    from_kind: str,
    to_kind: str,
    *,
    custodian: str | None = "P-1",
    received_by: str | None = "P-2",
    basis: str = "handoff",
    incident_id: int | None = None,
    note: str = "",
    run: bool = True,
    seq: int | None = None,
    by: Any = None,
    robot_id: int | None = None,
) -> int:
    rid = robot_id or w.run.robot_id
    return raw_insert(
        cur,
        "execution_custodyevent",
        robot_id=rid,
        seq=seq if seq is not None else next_custody_seq(rid),
        from_kind=from_kind,
        to_kind=to_kind,
        run_id=w.run.pk if run else None,
        trip_id=w.run.trip_id if run else None,
        visit_id=w.run.visit_id if run else None,
        custodian_id=pid(custodian),
        received_by_id=pid(received_by),
        basis=basis,
        incident_id=incident_id,
        note=note,
        recorded_by_id=_uid(w, by),
    )


def raw_trip_event(cur, w: SimpleNamespace, kind: str, *, seq: int | None = None, by: Any = None) -> int:
    return raw_insert(
        cur,
        "execution_tripevent",
        trip_id=w.run.trip_id,
        run_id=w.run.pk,
        seq=seq if seq is not None else TRIP_SEQ[kind],
        kind=kind,
        recorded_by_id=_uid(w, by),
    )


def valid_consents(cur, w: SimpleNamespace) -> int:
    cur.execute("SELECT execution_valid_consents(%s)", [w.run.pk])
    return int(cur.fetchone()[0])


def raw_site_check(
    cur,
    w: SimpleNamespace,
    *,
    adults: int = 2,
    conditions: dict[str, Any] | None = None,
    result: str = "go",
    consent_count: int | None = None,
    verifier: str = "P-1",
    template_id: int | None = None,
    by: Any = None,
) -> int:
    return raw_insert(
        cur,
        "execution_sitecheck",
        run_id=w.run.pk,
        template_id=template_id or w.template.pk,
        adults_present=adults,
        conditions=conditions if conditions is not None else dict(ALL_OK),
        consent_count=consent_count if consent_count is not None else valid_consents(cur, w),
        result=result,
        verifier_id=pid(verifier),
        recorded_by_id=_uid(w, by),
    )


def raw_session(
    cur,
    w: SimpleNamespace,
    kind: str,
    *,
    site_check_id: int | None = None,
    interruption_id: int | None = None,
    note: str = "",
    seq: int | None = None,
    by: Any = None,
) -> int:
    if seq is None:
        cur.execute(
            "SELECT coalesce(max(seq), 0) + 1 FROM execution_sessionevent WHERE run_id = %s", [w.run.pk]
        )
        seq = int(cur.fetchone()[0])
    return raw_insert(
        cur,
        "execution_sessionevent",
        run_id=w.run.pk,
        seq=seq,
        kind=kind,
        site_check_id=site_check_id,
        interruption_id=interruption_id,
        note=note,
        recorded_by_id=_uid(w, by),
    )


def _code(cur, seq: str, prefix: str) -> str:
    cur.execute("SELECT execution_code(%s, nextval(%s))", [prefix, seq])
    return str(cur.fetchone()[0])


def raw_interruption(
    cur,
    w: SimpleNamespace,
    *,
    source: str = "operator",
    incident_id: int | None = None,
    reason: str = "SYNTHETIC pause",
    by: Any = None,
) -> int:
    return raw_insert(
        cur,
        "execution_interruption",
        code=_code(cur, "execution_interruption_code_seq", "IN"),
        run_id=w.run.pk,
        source=source,
        incident_id=incident_id,
        reason=reason,
        recorded_by_id=_uid(w, by),
    )


def raw_resolve(
    cur, w: SimpleNamespace, interruption_id: int, *, note: str = "resolved", by: Any = None
) -> int:
    return raw_insert(
        cur,
        "execution_interruptionresolution",
        interruption_id=interruption_id,
        note=note,
        recorded_by_id=_uid(w, by),
    )


def raw_hold(cur, w: SimpleNamespace, *, source_ref: str, robot_id: int | None = None, **over: Any) -> int:
    """A robot-scope blocking incident hold under the binding hold policy (releasable with release_hold(w, code)).
    Slice 7: the binding is the active policy, else the newest (fleet_hold_binding_policy_id, fleet/0008)."""
    rid = robot_id or w.run.robot_id
    cur.execute("SELECT fleet_hold_binding_policy_id()")
    binding = cur.fetchone()[0]
    policy = HoldPolicy.objects.filter(pk=binding).first() if binding is not None else None
    source_type = over.get("source_type", "incident")
    slots = (policy.rules.get(source_type) or {}).get("slots", []) if policy else []
    cols: dict[str, Any] = {
        "code": f"HD-X{time.monotonic_ns() % 10**7}",
        "scope": "robot",
        "robot_id": rid,
        "pattern_id": None,
        "blocking": True,
        "source_type": "incident",
        "source_ref": source_ref,
        "reason": "SYNTHETIC incident hold",
        "policy_id": policy.pk if policy else None,
        "required_slots": slots,
        "manifest_at_placement_id": Robot.objects.get(pk=rid).current_manifest_id,
        "placed_by_id": w.ops.pk,
    }
    cols.update(over)
    if cols["scope"] == "manifest_pattern":
        cols["robot_id"] = None
    names = ", ".join([*cols, "placed_at"])
    marks = ", ".join(["%s"] * len(cols) + ["now()"])
    cur.execute(
        f"INSERT INTO fleet_hold ({names}) VALUES ({marks}) RETURNING id", [_val(v) for v in cols.values()]
    )
    return int(cur.fetchone()[0])


def release_hold(w: SimpleNamespace, hold_id: int) -> None:
    """Fill every slot of an incident hold (DRAFT_POLICY_RULES) and release it through the fleet commands."""
    h = Hold.objects.get(pk=hold_id)
    signers = {
        "eng_review_1": (w.eng, "engineer"),
        "eng_review_2": (w.eng2, "engineer"),
        "safety": (w.safety, "safety_reviewer"),
        "founder": (w.founder, "founder"),
    }
    for s in h.required_slots:
        user, role = signers[s["slot"]]
        fleet_services.approve_release(user, hold_code=h.code, slot=s["slot"], role_used=role)
    fleet_services.release_hold(w.ops, hold_code=h.code)


def incident_row(cur, w: SimpleNamespace, code: str, hold_id: int | None, **over: Any) -> int:
    robot = Robot.objects.get(pk=over.pop("robot_id", w.run.robot_id))
    cur.execute(
        "SELECT x.id, x.visit_id FROM execution_executionrun x WHERE x.robot_id = %s "
        "AND NOT EXISTS (SELECT 1 FROM execution_tripclosure c WHERE c.run_id = x.id)",
        [robot.pk],
    )
    row = cur.fetchone()
    cols: dict[str, Any] = {
        "code": code,
        "robot_id": robot.pk,
        "manifest_id": robot.current_manifest_id,
        "run_id": row[0] if row else None,
        "visit_id": row[1] if row else None,
        "event_type": "fall",
        "description": "SYNTHETIC incident",
        "knowledge_source": "witnessed",
        "hold_id": hold_id,
        "recorded_by_id": w.ops.pk,
    }
    cols.update(over)
    cur.execute("SELECT clock_timestamp() - interval '1 minute'")
    cols.setdefault("occurred_at", cur.fetchone()[0])
    cols.setdefault("reported_at", None)
    return raw_insert(cur, "execution_incident", **cols)


def raw_incident(conn, w: SimpleNamespace, *, unaccounted: bool = False, **over: Any) -> int:
    """report_incident's order in ONE transaction: hold -> incident -> (unaccounted custody)."""
    with conn.transaction(), conn.cursor() as cur:
        code = _code(cur, "execution_incident_code_seq", "IC")
        hold = raw_hold(cur, w, source_ref=code, robot_id=over.get("robot_id"))
        if unaccounted:
            over.setdefault("event_type", "robot_unaccounted")
        rid = over.get("robot_id") or w.run.robot_id
        iid = incident_row(cur, w, code, hold, **over)
        if unaccounted:
            last = CustodyEvent.objects.filter(robot_id=rid).order_by("-seq").first()
            cur.execute(
                "SELECT x.id FROM execution_executionrun x WHERE x.robot_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM execution_tripclosure c WHERE c.run_id = x.id)",
                [rid],
            )
            raw_custody(
                cur,
                w,
                last.to_kind if last else "hub",
                "unaccounted",
                custodian=None,
                received_by=None,
                incident_id=iid,
                run=cur.fetchone() is not None,
                robot_id=rid,
            )
        return iid


def raw_disposition(
    cur,
    w: SimpleNamespace,
    decision: str,
    *,
    incident_id: int | None = None,
    reason: str = "SYNTHETIC",
    by: Any = None,
) -> int:
    return raw_insert(
        cur,
        "execution_recoverydisposition",
        run_id=w.run.pk,
        decision=decision,
        reason=reason,
        incident_id=incident_id,
        recorded_by_id=_uid(w, by),
    )


def raw_reconcile(
    cur,
    w: SimpleNamespace,
    *,
    outcome: str = "reconciled",
    hub_receipt_id: int | None = None,
    inspection_id: int | None = None,
    discrepancies: list[str] | None = None,
    cargo_matches: bool = True,
    by: Any = None,
) -> int:
    return raw_insert(
        cur,
        "execution_returnreconciliation",
        run_id=w.run.pk,
        hub_receipt_id=hub_receipt_id,
        cargo_matches=cargo_matches,
        post_visit_inspection_id=inspection_id,
        discrepancies=discrepancies or [],
        outcome=outcome,
        recorded_by_id=_uid(w, by),
    )


def raw_close(cur, w: SimpleNamespace, reconciliation_id: int, *, by: Any = None) -> int:
    return raw_insert(
        cur,
        "execution_tripclosure",
        trip_id=w.run.trip_id,
        run_id=w.run.pk,
        reconciliation_id=reconciliation_id,
        recorded_by_id=_uid(w, by),
    )


# ------------------------------------------------------------------ walkthrough steps
def to_site(cur, w: SimpleNamespace) -> int:
    raw_verify(cur, w)
    raw_trip_event(cur, w, "arrived_site")
    return raw_custody(cur, w, "vehicle", "site", custodian="P-1", received_by="P-2")


def go_check(cur, w: SimpleNamespace, **kw: Any) -> int:
    return raw_site_check(cur, w, **kw)


def to_hub(cur, w: SimpleNamespace) -> int:
    raw_custody(cur, w, "site", "vehicle", custodian="P-1", received_by="P-2")
    raw_trip_event(cur, w, "departed_site")
    raw_trip_event(cur, w, "arrived_hub")
    return raw_custody(cur, w, "vehicle", "hub", custodian="P-1", received_by="P-3")


def post_visit_inspection(w: SimpleNamespace) -> int:
    code = record_inspection(w, w.visit, robot=w.robot.code)
    return Inspection.objects.get(code=code).pk


def close_run(cur, w: SimpleNamespace) -> int:
    hub = latest_custody(w).pk
    rec = raw_reconcile(cur, w, hub_receipt_id=hub, inspection_id=post_visit_inspection(w))
    return raw_close(cur, w, rec)


def raw_recover_to_hub(
    cur,
    w: SimpleNamespace,
    incident_id: int | None,
    *,
    robot_id: int | None = None,
    custodian: str | None = "P-1",
    received_by: str | None = "P-2",
    note: str = "SYNTHETIC: robot recovered and received at hub",
    by: Any = None,
) -> int:
    """0003: unaccounted -> hub, basis recovered, with NO run (the robot has no open run)."""
    return raw_custody(
        cur,
        w,
        "unaccounted",
        "hub",
        custodian=custodian,
        received_by=received_by,
        basis="recovered",
        incident_id=incident_id,
        note=note,
        run=False,
        by=by,
        robot_id=robot_id,
    )
