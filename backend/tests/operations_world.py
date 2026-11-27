"""Synthetic worlds for the Slice 5 command center (Engineer A, shared with engineers B/C/D). SYNTHETIC data only:
no value here describes a real robot, procedure or safety approval.

Everything except `scale_world` is built through the REAL fleet / planning / readiness / execution commands, so
guard versions, audit rows and every insert trigger run exactly as in production.

    w = ops_world("ow_", robots=4)       readiness world (tests.readiness_world.build_readiness) + site-check
                                         template SC1 + optional incident policy; nothing departed yet
    run = depart_new(w, "R-02")          plan + confirm a fresh visit for the robot (its own day), record all
                                         readiness evidence, authorize and record the departure -> ExecutionRun
    advance(w, run, stage)               drive an open run forward through the commands to `stage`
                                         (RUN_STAGES, in order; each stage includes the previous ones)
    report(w, robot_code, event=...)     execution.incidents.report_incident (holds the robot)
    make_incident_policy(w)              approved synthetic_demo incident policy (scale low/medium/high)
    w.cmd                                command shorthands (see Cmd)

    random_world(seed)                   a varied fleet chosen by `random.Random(seed)`; returns the world with
                                         w.seed, w.story (list of what was built, for failure messages)
    EDGE_CASES                           hand-written (name, builder, expected reasons) rows: one per eligibility
                                         reason, missing/unknown datum and mode switch; builder(w) -> robot code
    edge_world()                         the small world every EDGE_CASES builder starts from
    scale_world()                        200 robots / 2000 visits / 600 runs / 300 incidents (see its docstring)

Mode: worlds are built in demo mode (synthetic policies are only usable there); `random_world` may switch to
production at the END (DeploymentSetting demo_mode), which changes the synthetic-profile eligibility reason.

Slice 7 (X-3): every robot built by the shared worlds gets one qualifying periodic inspection (tests.fleet_world).
`add_robot` / `_repattern` record one on the new manifest unless told not to; `random_world` leaves some robots
without one (a separate per-robot random stream, so the Slice 5 choices of a seed are unchanged); in production
the worlds' SYNTHETIC periodic rule is not usable, so every robot there shows "interval unknown".
"""

import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from django.db import connection
from django.utils import timezone

from execution import escalation as esc
from execution import incidents as inc
from execution import logistics as lg
from execution import policy as ipol
from execution import reconciliation as rcn
from execution import sessions as ses
from execution import site_checks as sc
from execution.models import ExecutionRun, SiteCheckTemplate
from fleet import services as fs
from fleet.management.commands.seed_fleet import DRAFT_POLICY_RULES, TEMPLATE_ITEMS
from fleet.models import DeploymentSetting, Hold, Inspection, InspectionTemplate, ManifestPattern
from readiness import commands as rc
from tests.execution_world import ALL_OK
from tests.execution_world import TEMPLATE_ITEMS as SITE_ITEMS
from tests.fleet_world import (
    make_engineering_template,
    make_periodic_rule,
    record_periodic_inspection,
    set_setting,
)
from tests.planning_world import DAY
from tests.readiness_world import build_readiness, new_confirmed_visit, record_all_evidence, record_inspection

INCIDENT_RULES = {
    "severity_scale": ["low", "medium", "high"],
    "escalation_required_from": "high",
    "downgrade_roles": ["safety_reviewer"],
}
HOLD_SOURCES = ["inspection", "incident", "maintenance", "engineering", "release_withdrawn", "manual"]
PLACEABLE_SOURCES = ["maintenance", "engineering", "release_withdrawn", "manual"]  # via fleet.place_hold
ODD_REASON = "SYNTHETIC: 50% done; it's 'odd' \\ (%s) {x}"

# Stages an open run can be driven to, in order (advance() applies every step up to the requested one).
RUN_STAGES = [
    "departed",  # departure recorded (administrative hub -> vehicle, unverified)
    "verified",  # crew verified the departure assignment
    "arrived_site",  # trip arrived_site
    "at_site",  # vehicle -> site handoff
    "session_active",  # Go site check + session start
    "session_paused",  # operator interruption (pauses the session)
    "session_stopped",  # resolve interruption, stop
    "back_in_vehicle",  # site -> vehicle
    "returning",  # trip departed_site
    "arrived_hub",  # trip arrived_hub (robot still in the vehicle)
    "at_hub",  # vehicle -> hub handoff (hub receipt)
    "inspected",  # post-visit PASS inspection with visit_ref
    "reconciled",  # return reconciliation (reconciled)
    "closed",  # TripClosure
]


def ago(minutes: int = 5) -> Any:
    return timezone.now() - timedelta(minutes=minutes)


# ------------------------------------------------------------------ command shorthands
class Cmd:
    """Thin wrappers over the real commands, with the world's default actors."""

    def __init__(self, w: SimpleNamespace) -> None:
        self.w = w

    def verify(self, run: ExecutionRun, seq: int = 1) -> dict[str, Any]:
        return lg.verify_custody(self.w.field, run_code=run.code, custody_seq=seq, verifier_code="P-2")

    def trip(self, run: ExecutionRun, kind: str) -> dict[str, Any]:
        return lg.record_trip_event(self.w.field, run_code=run.code, kind=kind)

    def move(self, run: ExecutionRun, to: str, receiver: str = "P-2", note: str = "") -> dict[str, Any]:
        by = self.w.ops if note else self.w.field
        return lg.transfer_custody(
            by, run_code=run.code, to_kind=to, custodian_code="P-1", received_by_code=receiver, note=note
        )

    def site_check(self, run: ExecutionRun, conditions: dict[str, str] | None = None) -> dict[str, Any]:
        return sc.record_site_check(
            self.w.field,
            run_code=run.code,
            template_version="SC1",
            adults_present=2,
            conditions=dict(ALL_OK) if conditions is None else conditions,
            verifier_code="P-1",
        )

    def session(self, run: ExecutionRun, kind: str, **kw: Any) -> dict[str, Any]:
        return ses.record_session_event(self.w.field, run_code=run.code, kind=kind, **kw)

    def interrupt(self, run: ExecutionRun) -> dict[str, Any]:
        return ses.open_interruption(self.w.ops, run_code=run.code, reason="SYNTHETIC operator pause")

    def resolve(self, code: str) -> dict[str, Any]:
        return ses.resolve_interruption(self.w.ops, code=code, note="SYNTHETIC resolved")

    def disposition(self, run: ExecutionRun, decision: str, incident: str | None = None) -> dict[str, Any]:
        return ses.record_disposition(
            self.w.ops, run_code=run.code, decision=decision, reason="SYNTHETIC", incident_code=incident
        )

    def inspect(self, run: ExecutionRun) -> str:
        return record_inspection(self.w, run.visit.code, robot=run.robot.code)

    def reconcile(
        self, run: ExecutionRun, inspection: str | None = None, discrepancies: list[str] | None = None
    ) -> dict[str, Any]:
        return rcn.reconcile_return(
            self.w.ops, run_code=run.code, inspection_code=inspection, discrepancies=discrepancies
        )

    def close(self, run: ExecutionRun) -> dict[str, Any]:
        return rcn.close_trip(self.w.ops, run_code=run.code)

    def recover(self, robot_code: str) -> dict[str, Any]:
        return lg.recover_robot(
            self.w.ops, robot_code=robot_code, received_by_code="P-2", note="SYNTHETIC: found and received"
        )


# ------------------------------------------------------------------ worlds
def ops_world(prefix: str = "ow_", robots: int = 3, incident_policy: bool = True) -> SimpleNamespace:
    """Readiness world + site-check template SC1 (+ an approved synthetic incident policy). Demo mode."""
    w = build_readiness(prefix=prefix, robots=robots)
    w.prefix = prefix
    w.template = SiteCheckTemplate.objects.create(
        version="SC1", items=SITE_ITEMS, source="synthetic_demo", recorded_by=w.ops
    )
    w.cmd = Cmd(w)
    w.next_day = 1  # each new departed visit gets its own day (crew and vehicle reservations never overlap)
    w.story = []
    w.incident_policy = make_incident_policy(w) if incident_policy else None
    return w


def make_incident_policy(w: SimpleNamespace, source: str = "synthetic_demo") -> int:
    version = int(ipol.create_incident_policy(w.lead, rules=dict(INCIDENT_RULES), source=source)["entity_id"])
    ipol.approve_incident_policy(w.lead2, version=version, slot="engineering")
    ipol.approve_incident_policy(w.safety, version=version, slot="safety")
    return version


def add_robot(
    w: SimpleNamespace,
    code: str,
    pattern: str | None = "P-A",
    lifecycle: str = "in_service",
    auth: bool = True,
    periodic: bool = True,
    hub_code: str | None = None,
) -> str:
    """Register a robot; optionally record a manifest (+ a periodic inspection on it, X-3), walk the lifecycle
    and grant base authorization. ``hub_code``: v2 03B-3, its home hub when the workspace has several."""
    fs.register_robot(w.ops, code=code, serial=f"{w.prefix}SYN-{code}", hub_code=hub_code)
    if pattern is not None:
        fs.record_manifest(
            w.eng, robot_code=code, pattern_code=pattern, autonomy_version="a1", safety_config_hash="h" * 64
        )
        if periodic:
            record_periodic_inspection(w, code)
        if auth:
            fs.grant_base_authorization(w.lead, robot_code=code)
    if lifecycle in ("validation", "in_service", "retired"):
        fs.transition_lifecycle(w.lead, robot_code=code, to="validation", reason="SYNTHETIC")
    if lifecycle == "in_service":
        fs.transition_lifecycle(w.lead, robot_code=code, to="in_service", reason="SYNTHETIC")
    if lifecycle == "retired":
        fs.transition_lifecycle(w.founder, robot_code=code, to="retired", reason="SYNTHETIC")
    return code


def new_pattern(
    w: SimpleNamespace,
    code: str,
    decisions: list[str] = ("approved_home",),  # type: ignore[assignment]
    profiles: list[dict[str, Any]] | None = None,
    periodic_rule: bool = True,
) -> str:
    """Create a pattern with a decision history and capability profiles. Each profile dict: fields for
    publish_capability_profile plus `approve` in {"both", "engineering", "none"} and optional `source`."""
    fs.create_pattern(
        w.lead, code=code, hardware_rev="SYN-Z", release=f"{w.prefix}{code}", firmware_set="FW-S"
    )
    for d in decisions:
        fs.decide_pattern(w.lead, pattern_code=code, status=d, reason="SYNTHETIC")
    for spec in profiles or []:
        spec = dict(spec)
        approve = spec.pop("approve", "both")
        out = fs.publish_capability_profile(w.eng, pattern_code=code, **spec)
        pid = int(out["entity_id"])
        if approve in ("both", "engineering"):
            fs.approve_capability_profile(w.lead, profile_id=pid, slot="engineering")
        if approve == "both":
            fs.approve_capability_profile(w.safety, profile_id=pid, slot="safety")
    InspectionTemplate.objects.create(
        pattern=ManifestPattern.objects.get(code=code), version="T1", items=TEMPLATE_ITEMS, created_by=w.eng
    )
    if periodic_rule:
        make_periodic_rule(
            w, code
        )  # Slice 7: every pattern gets the world's approved SYNTHETIC periodic rule
    return code


FULL_PROFILE: dict[str, Any] = {
    "stop_behaviour": "SYNTHETIC",
    "lift_crew": 2,
    "max_runtime_min": 120,
    "charge_turnaround_min": 180,
    "load_unload_min": 20,
}


def place_hold(
    w: SimpleNamespace,
    scope: str,
    target: str,
    source: str = "manual",
    blocking: bool = True,
    reason: str = "",
) -> str:
    return str(
        fs.place_hold(
            w.lead,
            scope=scope,
            target=target,
            source_type=source,
            reason=reason or f"SYNTHETIC {source} hold",
            blocking=blocking,
        )["entity_id"]
    )


def release(w: SimpleNamespace, hold_code: str) -> None:
    """Fill every required slot of a hold with a distinct qualified signer and release it."""
    h = Hold.objects.get(code=hold_code)
    signers = {
        "engineering_lead": [w.lead, w.lead2],
        "engineer": [w.eng, w.eng2],
        "safety_reviewer": [w.safety],
        "founder": [w.founder],
    }
    used: set[int] = set()
    for slot in h.required_slots:
        for role in slot["roles"]:
            cand = [u for u in signers.get(role, []) if u.pk not in used and u.pk != h.placed_by_id]
            cand = cand or [u for u in signers.get(role, []) if u.pk not in used]
            if cand:
                fs.approve_release(cand[0], hold_code=h.code, slot=slot["slot"], role_used=role)
                used.add(cand[0].pk)
                break
    fs.release_hold(w.ops, hold_code=h.code)


def depart_new(w: SimpleNamespace, robot_code: str) -> ExecutionRun:
    """Plan + confirm a visit for `robot_code` on its own day, record all evidence, authorize and depart."""
    code = new_confirmed_visit(w, robot=robot_code, d=DAY + timedelta(days=w.next_day))
    w.next_day += 1
    record_all_evidence(w, code, robot=robot_code)
    auth = rc.authorize_departure(w.ops, visit_code=code)["code"]
    rc.record_departure(w.ops, authorization_code=auth)
    return ExecutionRun.objects.select_related("visit", "robot").get(visit__code=code)


def advance(w: SimpleNamespace, run: ExecutionRun, stage: str) -> None:
    """Drive `run` (freshly departed) through every RUN_STAGES step up to and including `stage`."""
    c: Cmd = w.cmd
    target = RUN_STAGES.index(stage)
    steps: dict[str, Any] = {
        "departed": lambda: None,
        "verified": lambda: c.verify(run),
        "arrived_site": lambda: c.trip(run, "arrived_site"),
        "at_site": lambda: c.move(run, "site"),
        "session_active": lambda: c.session(run, "start", site_check_id=c.site_check(run)["id"]),
        "session_paused": lambda: setattr(run, "_interruption", c.interrupt(run)["code"]),
        "session_stopped": lambda: (c.resolve(run._interruption), c.session(run, "stop")),  # type: ignore[attr-defined]
        "back_in_vehicle": lambda: c.move(run, "vehicle"),
        "returning": lambda: c.trip(run, "departed_site"),
        "arrived_hub": lambda: c.trip(run, "arrived_hub"),
        "at_hub": lambda: c.move(run, "hub", receiver="P-3"),
        "inspected": lambda: setattr(run, "_inspection", c.inspect(run)),
        "reconciled": lambda: c.reconcile(run, inspection=run._inspection),  # type: ignore[attr-defined]
        "closed": lambda: c.close(run),
    }
    for name in RUN_STAGES[1 : target + 1]:
        steps[name]()


def report(w: SimpleNamespace, robot_code: str, event: str = "fall", minutes_ago: int = 5) -> str:
    return str(
        inc.report_incident(
            w.field,
            robot_code=robot_code,
            event_type=event,
            description="SYNTHETIC incident",
            occurred_at=ago(minutes_ago),
            knowledge_source="witnessed",
        )["code"]
    )


def evolve_incident(w: SimpleNamespace, code: str, rng: random.Random) -> None:
    """Random severity, escalations, acknowledgments and status steps for an incident (all via commands)."""
    if w.incident_policy is not None and rng.random() < 0.7:
        inc.decide_severity(
            w.lead, incident_code=code, level=rng.choice(["low", "medium", "high"]), reason="SYN"
        )
    for _ in range(rng.randint(0, 2)):
        role, person = rng.choice(
            [("engineering_lead", "P-5"), ("engineering_lead", None), ("founder", None)]
        )
        e = esc.escalate_incident(
            w.ops,
            incident_code=code,
            to_role=role,
            requested_action="SYNTHETIC review",
            to_person_code=person,
        )
        if rng.random() < 0.5:
            esc.acknowledge_escalation(
                w.lead2 if role == "engineering_lead" else w.founder, escalation_id=e["id"]
            )
    steps = rng.randint(0, 4)
    for to in ["triaged", "response_in_progress", "resolved", "closed"][:steps]:
        try:
            inc.change_incident_status(w.safety, incident_code=code, to_status=to)
        except Exception:  # closing refused (unacknowledged escalation / severity needs one): stays resolved
            break


# ------------------------------------------------------------------ random worlds
def random_world(seed: int) -> SimpleNamespace:
    """A varied synthetic fleet. Deterministic for a seed (codes from sequences may differ between runs)."""
    rng = random.Random(seed)
    w = ops_world(prefix=f"rw{seed}_", robots=3, incident_policy=rng.random() < 0.8)
    w.seed = seed
    say = w.story.append

    # Extra hold-policy versions (holds copy the newest policy's rules at placement).
    for _ in range(rng.randint(0, 2)):
        fs.create_hold_policy(w.lead, rules=DRAFT_POLICY_RULES)
        say("new hold policy version")

    # Extra patterns with varied decisions and profiles.
    patterns = ["P-A", "P-C"]
    for n in range(rng.randint(1, 3)):
        code = f"P-Z{n}"
        decisions = rng.choice(
            [
                [],
                ["approved_home"],
                ["engineering_only"],
                ["approved_home", "withdrawn"],
                ["withdrawn", "approved_home"],
                ["approved_home", "engineering_only", "approved_home"],
            ]
        )
        profiles: list[dict[str, Any]] = []
        for _ in range(rng.randint(0, 3)):
            spec = dict(FULL_PROFILE)
            for f in ("stop_behaviour", "lift_crew", "max_runtime_min", "charge_turnaround_min"):
                if rng.random() < 0.2:
                    spec[f] = "" if f == "stop_behaviour" and rng.random() < 0.5 else None
            spec = {k: v for k, v in spec.items() if v is not None}
            spec["source"] = rng.choice(["synthetic_demo", "engineering_doc"])
            spec["approve"] = rng.choice(["both", "both", "engineering", "none"])
            profiles.append(spec)
        new_pattern(w, code, decisions=decisions, profiles=profiles)
        patterns.append(code)
        say(f"{code}: decisions={decisions} profiles={[(p['source'], p['approve']) for p in profiles]}")

    # Extra robots in varied lifecycles / configurations.
    robots = list(w.robots)
    for n in range(rng.randint(2, 5)):
        code = f"R-{10 + n}"
        pattern = rng.choice([*patterns, None])
        lifecycle = rng.choice(["commissioning", "validation", "in_service", "in_service", "retired"])
        if lifecycle != "commissioning" and pattern is None:
            pattern = "P-A"  # validation needs nothing, but keep most robots configured
        auth = pattern is not None and rng.random() < 0.8
        # X-3: own stream, so the Slice 5 choices above and below stay what they were for this seed.
        periodic = random.Random(seed * 1000 + n).random() < 0.7
        try:
            add_robot(w, code, pattern=pattern, lifecycle=lifecycle, auth=auth, periodic=periodic)
        except Exception:
            # in_service needs an approved profile with stop behaviour: the command refuses; keep validation
            say(f"{code}: in_service refused (pattern {pattern})")
        robots.append(code)
        say(f"{code}: pattern={pattern} lifecycle={lifecycle} auth={auth} periodic={periodic}")

    # Departures at varied stages (only robots that can actually depart: R-01..R-03 on P-A).
    departing = rng.sample(w.robots, k=rng.randint(0, 3))
    for code in departing:
        run = depart_new(w, code)
        story = rng.choice(
            [
                "departed",
                "verified",
                "at_site",
                "session_active",
                "session_paused",
                "returning",
                "arrived_hub",
                "at_hub",
                "inspected",
                "reconciled",
                "closed",
                "unresolved",
                "unaccounted_on_site",
                "unaccounted_outbound",
                "recovered_on_run",
                "closed_then_unaccounted",
            ]
        )
        say(f"{code}: run {run.code} -> {story}")
        if story in RUN_STAGES:
            advance(w, run, story)
        elif story == "unresolved":
            advance(w, run, "at_hub")
            w.cmd.reconcile(run, discrepancies=["SYNTHETIC: cable missing"])
        elif story == "unaccounted_on_site":
            advance(w, run, "at_site")
            report(w, code, event="robot_unaccounted")
        elif story == "unaccounted_outbound":
            advance(w, run, "verified")
            report(w, code, event="robot_unaccounted")
            w.cmd.trip(run, "arrived_hub")
        elif story == "recovered_on_run":
            advance(w, run, "verified")
            report(w, code, event="robot_unaccounted")
            w.cmd.trip(run, "arrived_hub")
            w.cmd.move(run, "hub", note="SYNTHETIC: found and received")
        elif story == "closed_then_unaccounted":
            advance(w, run, "closed")
            report(w, code, event="robot_unaccounted")
            if rng.random() < 0.5:
                w.cmd.recover(code)
                say(f"{code}: recovered with no run")

    # Incidents (each holds the robot) on random non-retired robots, then random severity/status/escalations.
    for _ in range(rng.randint(0, 3)):
        code = rng.choice(w.robots)
        icode = report(w, code, event=rng.choice(["fall", "contact", "other"]))
        evolve_incident(w, icode, rng)
        say(f"incident {icode} on {code}")

    # Holds: robot and pattern scope, all source types placeable by command, blocking or not; some released.
    for _ in range(rng.randint(0, 4)):
        scope = rng.choice(["robot", "robot", "manifest_pattern"])
        target = rng.choice(robots if scope == "robot" else patterns)
        try:
            hcode = place_hold(
                w,
                scope,
                target,
                source=rng.choice(PLACEABLE_SOURCES),
                blocking=rng.random() < 0.8,
                reason=ODD_REASON if rng.random() < 0.2 else "",
            )
        except Exception:
            continue  # e.g. a retired robot
        say(f"hold {hcode} on {scope} {target}")
        if rng.random() < 0.3:
            try:
                release(w, hcode)
                say(f"released {hcode}")
            except Exception:
                pass  # unreleasable (missing slots on a policy version): stays active

    # Base authorization revoked on some robots.
    for code in rng.sample(robots, k=rng.randint(0, 2)):
        try:
            fs.revoke_base_authorization(w.lead, robot_code=code, reason="SYNTHETIC")
            say(f"{code}: base authorization revoked")
        except Exception:
            pass

    # Mode switch at the end (demo -> production) or a missing / odd demo_mode value.
    mode = rng.choice(["true", "true", "false", None, "TRUE"])
    if mode is None:
        DeploymentSetting.objects.filter(key="demo_mode").delete()
    else:
        set_setting("demo_mode", mode)
    say(f"demo_mode={mode}")
    w.robot_codes = robots
    return w


# ------------------------------------------------------------------ edge cases (hand-written)
def edge_world() -> SimpleNamespace:
    """The small world every EDGE_CASES builder starts from (R-01..R-02 eligible on P-A, demo mode)."""
    return ops_world(prefix="eg_", robots=2, incident_policy=False)


def _repattern(
    w: SimpleNamespace,
    code: str,
    decisions: list[str],
    profiles: list[dict[str, Any]] | None = None,
    auth: bool = True,
    periodic: bool = True,
    periodic_rule: bool = True,
) -> str:
    new_pattern(
        w,
        code,
        decisions=decisions,
        profiles=profiles if profiles is not None else [dict(FULL_PROFILE)],
        periodic_rule=periodic_rule,
    )
    fs.record_manifest(
        w.eng, robot_code="R-01", pattern_code=code, autonomy_version="a2", safety_config_hash="g" * 64
    )
    if periodic:  # X-3: a new manifest restarts the periodic clock; keep each case to its own reason
        record_periodic_inspection(w, "R-01")
    if auth:
        fs.grant_base_authorization(w.lead, robot_code="R-01")
    return "R-01"


def _unknown(field: str) -> Any:
    def build(w: SimpleNamespace) -> str:
        spec = dict(FULL_PROFILE)
        spec.pop(field)
        return _repattern(w, "P-U", ["approved_home"], [spec])

    return build


def _hold(source: str, scope: str = "robot", blocking: bool = True, released: bool = False) -> Any:
    def build(w: SimpleNamespace) -> str:
        h = place_hold(w, scope, "R-01" if scope == "robot" else "P-A", source=source, blocking=blocking)
        if released:
            release(w, h)
        w.hold = h
        return "R-01"

    return build


def _production_periodic(w: SimpleNamespace, pattern: str, robot: str = "R-01") -> None:
    """SYNTHETIC production fixtures (test approvals by test users, never company values): an engineering_doc
    periodic rule and template E1 approved for 'periodic', and a PASS inspection on E1."""
    make_periodic_rule(w, pattern, source="engineering_doc")
    make_engineering_template(w, pattern, "E1", purposes=("periodic",))
    record_periodic_inspection(w, robot, template_version="E1")


def _mode(value: str | None, source: str = "synthetic_demo", production_periodic: bool = False) -> Any:
    def build(w: SimpleNamespace) -> str:
        _repattern(w, "P-M", ["approved_home"], [{**FULL_PROFILE, "source": source}])
        if production_periodic:
            _production_periodic(w, "P-M")
        if value is None:
            DeploymentSetting.objects.filter(key="demo_mode").delete()
        else:
            set_setting("demo_mode", value)
        return "R-01"

    return build


def _combined(w: SimpleNamespace) -> str:
    """Every reason at once, to pin the order."""
    spec = dict(FULL_PROFILE)
    spec.pop("lift_crew")
    spec["stop_behaviour"] = ""
    _repattern(w, "P-X", ["engineering_only"], [spec], auth=False)
    set_setting("demo_mode", "false")  # also: the SYNTHETIC periodic rule is not usable in production
    fs.transition_lifecycle(w.lead, robot_code="R-01", to="validation", reason="SYNTHETIC")
    w.p_hold = place_hold(w, "manifest_pattern", "P-X", source="engineering", reason=ODD_REASON)
    w.r_hold1 = place_hold(w, "robot", "R-01", source="maintenance")
    w.r_hold2 = place_hold(w, "robot", "R-01", source="manual")
    return "R-01"


def _owner_backdate(table: str, column: str, where: str, params: list[Any], minutes: int) -> None:
    """TEST-ONLY owner rewrite (the documented owner trust boundary; the test connection is the schema owner):
    move a forced database time `minutes` into the past, in the test's own transaction, because the commands can
    never backdate it. Pending deferred foreign-key checks are flushed first (ALTER TABLE refuses while trigger
    events are pending) and the user triggers are re-enabled before anything else runs."""
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        cur.execute(
            f"UPDATE {table} SET {column} = {column} - make_interval(mins => %s) WHERE {where}",
            [minutes, *params],
        )
        cur.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        cur.execute("SET CONSTRAINTS ALL DEFERRED")


def _backdate_inspections(robot_code: str, minutes: int) -> None:
    """Every inspection of the robot, and the readiness_inspector grants of its signers (so the moved
    inspections are still covered at their submitted_at), `minutes` into the past."""
    robot = "robot_id = (SELECT id FROM fleet_robot WHERE code = %s)"
    _owner_backdate(
        "planning_qualification",
        "valid_from",
        "kind = 'readiness_inspector' AND person_id IN (SELECT p.id FROM planning_person p "
        f"JOIN fleet_inspection i ON p.user_id IN (i.performer_id, i.verifier_id) WHERE i.{robot})",
        [robot_code],
        minutes,
    )
    _owner_backdate("fleet_inspection", "submitted_at", robot, [robot_code], minutes)


def _periodic_overdue(w: SimpleNamespace) -> str:
    """Every R-01 inspection moved one interval + 1 day into the past: due_at is before the snapshot time."""
    rule = make_periodic_rule(w, "P-A", interval_min=60)
    _backdate_inspections("R-01", 60 + 24 * 60)
    w.due_at = max(Inspection.objects.filter(robot__code="R-01").values_list("submitted_at", flat=True)) + (
        timedelta(minutes=rule.interval_min or 0)
    )
    return "R-01"


def _periodic_signer_revoked(w: SimpleNamespace) -> str:
    """P-2 signed every R-01 inspection; revoking the qualification makes none of them qualify (X-8)."""
    from planning import services as ps  # noqa: PLC0415
    from planning.models import Qualification  # noqa: PLC0415

    q = Qualification.objects.get(person__code="P-2", kind="readiness_inspector", revoked_at__isnull=True)
    ps.revoke_qualification(w.safety, qualification_id=q.pk, reason="SYNTHETIC")
    return "R-01"


def _periodic_rule_withdrawn(w: SimpleNamespace) -> str:
    rule = fs.active_periodic_rule(ManifestPattern.objects.get(code="P-A").pk)
    assert rule is not None
    fs.withdraw_periodic_rule(w.safety, rule_id=rule.pk, reason="SYNTHETIC")
    return "R-01"


def _incident_hold(w: SimpleNamespace) -> str:
    w.incident = report(w, "R-01")
    w.hold = Hold.objects.get(source_ref=w.incident).code
    return "R-01"


def _inspection_hold(w: SimpleNamespace) -> str:
    answers = dict(w.all_pass)
    answers["I1"] = "fail"
    out = fs.submit_inspection(
        w.ops,
        robot_code="R-01",
        template_version="T1",
        answers=answers,
        performer=w.field.username,
        verifier=w.field2.username,
    )
    w.hold = out["hold"]
    return "R-01"


def _revoked(w: SimpleNamespace) -> str:
    fs.revoke_base_authorization(w.lead, robot_code="R-01", reason="SYNTHETIC")
    return "R-01"


def _commissioning(w: SimpleNamespace) -> str:
    return add_robot(w, "R-20", lifecycle="commissioning")


def _validation(w: SimpleNamespace) -> str:
    return add_robot(w, "R-21", lifecycle="validation")


def _retired(w: SimpleNamespace) -> str:
    return add_robot(w, "R-22", lifecycle="retired")


def _no_manifest(w: SimpleNamespace) -> str:
    return add_robot(w, "R-23", pattern=None, lifecycle="commissioning")


def _older_profile_approved(w: SimpleNamespace) -> str:
    # v1 complete + approved, v2 published but only one slot: the approved one is v1 -> eligible
    return _repattern(
        w, "P-O", ["approved_home"], [dict(FULL_PROFILE), {**FULL_PROFILE, "approve": "engineering"}]
    )


def _newer_approved_unknown(w: SimpleNamespace) -> str:
    spec = dict(FULL_PROFILE)
    spec.pop("charge_turnaround_min")
    return _repattern(w, "P-N", ["approved_home"], [dict(FULL_PROFILE), spec])


def _other_pattern_hold(w: SimpleNamespace) -> str:
    place_hold(w, "manifest_pattern", "P-C", source="engineering")  # R-01 is on P-A
    return "R-01"


def _other_robot_hold(w: SimpleNamespace) -> str:
    place_hold(w, "robot", "R-02", source="manual")
    return "R-01"


def _auth_other_pattern(w: SimpleNamespace) -> str:
    # authorized on P-A, re-manifested to P-C with no authorization for P-C
    fs.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-C", autonomy_version="a2", safety_config_hash="g" * 64
    )
    record_periodic_inspection(w, "R-01")  # X-3: keep the case to its own reason
    return "R-01"


def _baseline(w: SimpleNamespace) -> str:
    return "R-01"


P = "Configuration {p} is not approved for home use ({s})"
PU = "Periodic inspection interval unknown for configuration {p}"
PN = "No qualifying periodic inspection on the current configuration"
CS = "Capability profile is synthetic demo data, not engineering-supplied"
EDGE_CASES: list[tuple[str, Any, Any]] = [
    # (name, builder(w) -> robot code, expected reasons: list[str] or callable(w) -> list[str])
    ("eligible_baseline", _baseline, []),
    ("lifecycle_commissioning", _commissioning, ["Lifecycle is Commissioning, not In service"]),
    ("lifecycle_validation", _validation, ["Lifecycle is Engineering validation, not In service"]),
    ("lifecycle_retired", _retired, ["Lifecycle is Retired, not In service"]),
    (
        "no_manifest",
        _no_manifest,
        ["Lifecycle is Commissioning, not In service", "No configuration manifest recorded"],
    ),
    ("pattern_no_decision", lambda w: _repattern(w, "P-D", []), [P.format(p="P-D", s="none")]),
    (
        "pattern_engineering_only",
        lambda w: _repattern(w, "P-E", ["engineering_only"]),
        [P.format(p="P-E", s="engineering_only")],
    ),
    (
        "pattern_withdrawn",
        lambda w: _repattern(w, "P-W", ["approved_home", "withdrawn"]),
        [P.format(p="P-W", s="withdrawn")],
    ),
    ("pattern_reapproved_latest_wins", lambda w: _repattern(w, "P-R", ["withdrawn", "approved_home"]), []),
    ("base_authorization_revoked", _revoked, ["No base home-use authorization for configuration P-A"]),
    (
        "base_authorization_other_pattern",
        _auth_other_pattern,
        ["No base home-use authorization for configuration P-C"],
    ),
    (
        "no_profile_at_all",
        lambda w: _repattern(w, "P-0", ["approved_home"], []),
        ["No approved capability profile for P-0"],
    ),
    (
        "profile_one_slot_only",
        lambda w: _repattern(w, "P-1S", ["approved_home"], [{**FULL_PROFILE, "approve": "engineering"}]),
        ["No approved capability profile for P-1S"],
    ),
    (
        "profile_unapproved",
        lambda w: _repattern(w, "P-UA", ["approved_home"], [{**FULL_PROFILE, "approve": "none"}]),
        ["No approved capability profile for P-UA"],
    ),
    ("unknown_stop_behaviour", _unknown("stop_behaviour"), ["Capability unknown: stop behaviour"]),
    (
        "empty_stop_behaviour",
        lambda w: _repattern(w, "P-ES", ["approved_home"], [{**FULL_PROFILE, "stop_behaviour": ""}]),
        ["Capability unknown: stop behaviour"],
    ),
    ("unknown_lift_crew", _unknown("lift_crew"), ["Capability unknown: lift crew"]),
    ("unknown_max_runtime", _unknown("max_runtime_min"), ["Capability unknown: max runtime min"]),
    (
        "unknown_charge_turnaround",
        _unknown("charge_turnaround_min"),
        ["Capability unknown: charge turnaround min"],
    ),
    ("unknown_load_unload_not_a_planning_field", _unknown("load_unload_min"), []),
    ("older_profile_approved_newer_not", _older_profile_approved, []),
    ("newer_approved_profile_wins", _newer_approved_unknown, ["Capability unknown: charge turnaround min"]),
    ("synthetic_profile_demo_mode", _mode("true"), []),
    # Production (X-3): the world's SYNTHETIC periodic rule is not usable there, so "interval unknown" joins in.
    (
        "synthetic_profile_production",
        _mode("false"),
        ["Capability profile is synthetic demo data, not engineering-supplied", PU.format(p="P-M")],
    ),
    (
        "synthetic_profile_mode_missing",
        _mode(None),
        ["Capability profile is synthetic demo data, not engineering-supplied", PU.format(p="P-M")],
    ),
    (
        "synthetic_profile_mode_uppercase",
        _mode("TRUE"),
        ["Capability profile is synthetic demo data, not engineering-supplied", PU.format(p="P-M")],
    ),
    (
        "synthetic_profile_production_with_production_periodic",
        _mode("false", production_periodic=True),
        ["Capability profile is synthetic demo data, not engineering-supplied"],
    ),
    ("engineering_profile_production", _mode("false", source="engineering_doc"), [PU.format(p="P-M")]),
    (
        "engineering_profile_production_with_production_periodic",
        _mode("false", source="engineering_doc", production_periodic=True),
        [],
    ),
    # X-3 periodic inspection (contract 07a section 4.2; EM decision 7), demo unless stated.
    (
        "periodic_never_inspected",
        lambda w: _repattern(w, "P-NI", ["approved_home"], periodic=False),
        [PN],
    ),
    ("periodic_signer_revoked", _periodic_signer_revoked, [PN]),
    (
        "periodic_no_rule",
        lambda w: _repattern(w, "P-NR", ["approved_home"], periodic_rule=False),
        [PU.format(p="P-NR")],
    ),
    (
        "periodic_interval_null",
        lambda w: (make_periodic_rule(w, "P-A", interval_min=None), "R-01")[1],
        [PU.format(p="P-A")],
    ),
    ("periodic_rule_withdrawn", _periodic_rule_withdrawn, [PU.format(p="P-A")]),
    (
        "periodic_synthetic_rule_production",
        lambda w: (set_setting("demo_mode", "false"), "R-01")[1],
        [CS, PU.format(p="P-A")],
    ),
    (
        "periodic_synthetic_template_production",
        lambda w: (
            make_periodic_rule(w, "P-A", source="engineering_doc"),
            set_setting("demo_mode", "false"),
            "R-01",
        )[2],
        [CS, PN],
    ),
    (
        "periodic_overdue",
        _periodic_overdue,
        lambda w: [f"Periodic inspection overdue since {fs.format_db_time(w.due_at)}"],
    ),
    (
        "robot_hold_inspection",
        _inspection_hold,
        lambda w: [f"Robot hold {w.hold} (Failed inspection): {Hold.objects.get(code=w.hold).reason}"],
    ),
    (
        "robot_hold_incident",
        _incident_hold,
        lambda w: [f"Robot hold {w.hold} (Incident): {Hold.objects.get(code=w.hold).reason}"],
    ),
    (
        "robot_hold_maintenance",
        _hold("maintenance"),
        lambda w: [f"Robot hold {w.hold} (Maintenance): SYNTHETIC maintenance hold"],
    ),
    (
        "robot_hold_engineering",
        _hold("engineering"),
        lambda w: [f"Robot hold {w.hold} (Engineering restriction): SYNTHETIC engineering hold"],
    ),
    (
        "robot_hold_release_withdrawn",
        _hold("release_withdrawn"),
        lambda w: [f"Robot hold {w.hold} (Release withdrawn): SYNTHETIC release_withdrawn hold"],
    ),
    (
        "robot_hold_manual",
        _hold("manual"),
        lambda w: [f"Robot hold {w.hold} (Manual): SYNTHETIC manual hold"],
    ),
    ("robot_hold_non_blocking", _hold("manual", blocking=False), []),
    ("robot_hold_released", _hold("manual", released=True), []),
    (
        "pattern_hold",
        _hold("engineering", scope="manifest_pattern"),
        lambda w: [f"Configuration hold {w.hold}: SYNTHETIC engineering hold"],
    ),
    ("pattern_hold_non_blocking", _hold("engineering", scope="manifest_pattern", blocking=False), []),
    ("pattern_hold_released", _hold("manual", scope="manifest_pattern", released=True), []),
    ("pattern_hold_other_pattern", _other_pattern_hold, []),
    ("robot_hold_other_robot", _other_robot_hold, []),
    (
        "combined_order",
        _combined,
        lambda w: [
            "Lifecycle is Engineering validation, not In service",
            P.format(p="P-X", s="engineering_only"),
            "No base home-use authorization for configuration P-X",
            "Capability unknown: stop behaviour",
            "Capability unknown: lift crew",
            "Capability profile is synthetic demo data, not engineering-supplied",
            f"Configuration hold {w.p_hold}: {ODD_REASON}",
            PU.format(p="P-X"),
            f"Robot hold {w.r_hold1} (Maintenance): SYNTHETIC maintenance hold",
            f"Robot hold {w.r_hold2} (Manual): SYNTHETIC manual hold",
        ],
    ),
]


# ------------------------------------------------------------------ scale fixture
SCALE = {"robots": 200, "visits": 2000, "runs": 600, "incidents": 300}
SCALE_CREWS = (
    40  # crew c serves robots r with r % 40 == c; robots of one crew get distinct 4-hour slots a day
)
SCALE_SLOTS = 5
INSP, LOAD, TRAVEL, CHARGE = 30, 20, 30, 180  # minutes: pre_visit_inspection_min, P-A v2 load/charge, travel


def scale_world(
    robots: int = SCALE["robots"],
    visits_per_robot: int = SCALE["visits"] // SCALE["robots"],
    closed_runs_per_robot: int = 2,
    incidents: int = SCALE["incidents"],
    seed: int = 5,
    crews: int = SCALE_CREWS,
    hubs: int = 1,
) -> SimpleNamespace:
    """The Slice 5 scale fixture: 200 robots, 2,000 visits, 600 runs (2 closed per robot + 1 more departure per
    robot, left at a varied stage or closed), 300 incidents. SYNTHETIC data only.

    HOW (contract section 6.5). The fleet, people, vehicles and incidents are built through the REAL commands.
    Visits, reservations, trips, readiness evidence, departures and run streams are inserted in bulk as the
    schema OWNER, in an order every insert trigger accepts (no trigger is disabled, so every row is valid):
      1. visits are inserted 'requested', their revision (planning-feasible, timeline of
         readiness/0006 section A), 7 robot + 2 crew + 1 vehicle reservations (exclusion constraint), the vehicle
         trip and its cargo; then UPDATEd to planned -> confirmed (visit_guard transitions);
      2. per departure wave (one visit per robot at a time, all robots in parallel): complete PASS inspection with
         visit_ref, pre_departure_inspection / equipment_check / battery_check evidence and a recovery plan,
         then ``readiness_issue_authorization`` (the database write boundary re-proves every gate) with a guard
         snapshot read from the live rows, then the departure INSERT (the C3 / departure triggers open the run
         and write the administrative hub -> vehicle custody);
      3. run streams as the crew / ops lead: verification, arrived_site, vehicle -> site, site -> vehicle,
         departed_site, arrived_hub, vehicle -> hub, post-visit inspection, reconciliation, closure -- each wave
         one set-based INSERT ... SELECT whose row triggers lock and re-check every row.
    Robots, people, vehicles and incidents use the commands (report_incident holds the robot). The Python
    readiness evaluator does not run for these departures: the SQL write boundary re-proves the gates the
    database can check (readiness/0005 and 0006 list the few it cannot). ANALYZE runs between the bulk steps so
    the per-row trigger queries use their indexes (the app's 10 s statement_timeout stays in force).
    Layout: crew c (persons PC-<c>L / PC-<c>S, vehicle VS-<c>) serves robots n with n % 40 == c; robot n's visit
    k is on DAY + k + 1 in 4-hour slot (n // 40) % 5, so no robot, crew or vehicle reservation overlaps.
    Returns the world with w.robot_codes, w.scale_open_stages {robot index: stage of its last run} and
    w.timings (cumulative seconds per phase).
    Phase 05 (500-robot validation): ``crews`` (default 40) and ``hubs`` (default 1, the Slice 5 fixture
    unchanged). With hubs > 1, HUB-2.. are created after the base world, and crew c (its two people, its van and
    the robots it serves) belongs to hub ``c * hubs // crews`` (contiguous blocks, so the base world's robots and
    crew 0 stay at the first hub). Visits are inserted with their robot's hub; the crew users get a field_operator
    grant at their hub, and the base world's two field operators one at every new hub. w.hub_codes lists them."""
    import time

    rng = random.Random(seed)
    t0 = time.perf_counter()
    w = ops_world(prefix="sc_", robots=3)
    w.timings = {}

    def lap(name: str) -> None:
        w.timings[name] = round(time.perf_counter() - t0, 1)

    from django.db import connection

    from accounts.models import RoleGrant
    from planning import services as ps
    from platform_core.models import Hub
    from tests.conftest import make_user

    # ---------------------------------------------------------------- hubs (Phase 05; one hub by default)
    first = Hub.objects.get()
    hub_codes = [first.code]
    for h in range(2, hubs + 1):
        hub = Hub.objects.create(code=f"HUB-{h}", name=f"Synthetic hub {h}", time_zone=first.time_zone)
        hub_codes.append(hub.code)
        for u in (w.field, w.field2):
            RoleGrant.objects.create(user=u, role="field_operator", hub=hub)
    w.hub_codes = hub_codes

    def hub_of(n: int) -> str | None:  # robot / crew index -> hub code (None: the only hub, as before)
        return hub_codes[(n % crews) * hubs // crews] if hubs > 1 else None

    # ---------------------------------------------------------------- robots, crews, vehicles (commands)
    codes = list(w.robots)
    for n in range(len(codes) + 1, robots + 1):
        codes.append(add_robot(w, f"R-{n:03d}", hub_code=hub_of(n - 1)))
    lo, hi_dt = timezone.now() - timedelta(days=30), datetime(2027, 6, 1, tzinfo=UTC)
    crew_users = []
    for c in range(crews):
        pair = []
        for role, kinds in (
            ("lead", ["field_lead", "robot_handler"]),
            ("second", ["field_operator", "robot_handler"]),
        ):
            if hubs > 1:
                u = make_user(f"sc_crew_{role}_{c}")
                RoleGrant.objects.create(user=u, role="field_operator", hub=Hub.objects.get(code=hub_of(c)))
            else:
                u = make_user(f"sc_crew_{role}_{c}", "field_operator")
            pcode = f"PC-{c}{role[0].upper()}"
            ps.register_person(w.ops, username=u.username, code=pcode, hub_code=hub_of(c))
            for k in kinds:
                ps.grant_qualification(w.ops, person_code=pcode, kind=k, valid_from=lo, valid_to=hi_dt)
            pair.append(u)
        ps.register_vehicle(
            w.ops, code=f"VS-{c}", robot_capacity=1, restraint_kit="SYN kit", hub_code=hub_of(c)
        )
        crew_users.append(pair)
    lap("fleet_people_vehicles")

    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO planning_resource (kind, robot_id) SELECT 'robot', r.id FROM fleet_robot r"
            " WHERE NOT EXISTS (SELECT 1 FROM planning_resource x WHERE x.robot_id = r.id)"
        )
        # ------------------------------------------------------------ the visit plan (one row per visit)
        cur.execute(
            """
            CREATE TEMP TABLE scale_plan ON COMMIT DROP AS
            WITH rb AS (
              SELECT r.id AS robot_id, row_number() OVER (ORDER BY r.id) - 1 AS n FROM fleet_robot r
              WHERE r.code = ANY(%(codes)s)
            )
            SELECT rb.robot_id, rb.n, k.k,
                   (%(day)s::date + (k.k + 1) * interval '1 day' + interval '2 hours'
                     + ((rb.n / %(crews)s) %% %(slots)s) * interval '4 hours')::timestamptz AS on_site_start,
                   (SELECT p.id FROM planning_person p WHERE p.code = 'PC-' || (rb.n %% %(crews)s) || 'L') AS lead_id,
                   (SELECT p.id FROM planning_person p WHERE p.code = 'PC-' || (rb.n %% %(crews)s) || 'S') AS second_id,
                   (SELECT v.id FROM planning_vehicle v WHERE v.code = 'VS-' || (rb.n %% %(crews)s)) AS vehicle_id,
                   NULL::bigint AS visit_id, NULL::bigint AS revision_id, NULL::bigint AS trip_id,
                   NULL::text AS status
            FROM rb CROSS JOIN generate_series(0, %(vpr)s - 1) k(k)
            """,
            {"codes": codes, "day": DAY, "crews": crews, "slots": SCALE_SLOTS, "vpr": visits_per_robot},
        )
        cur.execute(
            "UPDATE scale_plan SET status = CASE WHEN k <= %s THEN 'confirmed' END", [closed_runs_per_robot]
        )
        cur.execute("SELECT n, k FROM scale_plan WHERE status IS NULL ORDER BY n, k")
        later = cur.fetchall()
        statuses = [
            rng.choices(["confirmed", "planned", "requested", "cancelled"], [60, 25, 10, 5])[0] for _ in later
        ]
        cur.execute(
            "UPDATE scale_plan p SET status = s.status FROM unnest(%s::int[], %s::int[], %s::text[]) s(n, k, status)"
            " WHERE p.n = s.n AND p.k = s.k",
            [[n for n, _ in later], [k for _, k in later], statuses],
        )
        cur.execute("SELECT id FROM planning_site WHERE code = 'S-1'")
        site = cur.fetchone()[0]
        cur.execute(
            """
            WITH ins AS (
              INSERT INTO planning_visit (code, status, created_at, cancelled_reason, created_by_id, site_id, version,
                hub_id)
              SELECT execution_code('V', nextval('planning_visit_code_seq')), 'requested', now(), '', %s, %s, 1,
                (SELECT r.hub_id FROM fleet_robot r WHERE r.id = scale_plan.robot_id)
              FROM scale_plan ORDER BY n, k
              RETURNING id)
            SELECT array_agg(id ORDER BY id) FROM ins
            """,
            [w.ops.pk, site],
        )
        ids = cur.fetchone()[0]
        cur.execute(
            "UPDATE scale_plan p SET visit_id = s.id FROM (SELECT n, k, row_number() OVER (ORDER BY n, k) AS i"
            " FROM scale_plan) o JOIN unnest(%s::bigint[]) WITH ORDINALITY s(id, i) ON s.i = o.i"
            " WHERE p.n = o.n AND p.k = o.k",
            [ids],
        )
        cur.execute(
            """
            INSERT INTO planning_visitrevision (revision_no, on_site_start, on_site_end, travel_out_min,
              travel_back_min, tasks, required_consent_scopes, feasibility, blockers, snapshot, reason, created_at,
              created_by_id, lead_id, robot_id, second_id, vehicle_id, visit_id)
            SELECT 1, on_site_start, on_site_start + interval '1 hour', %(t)s, %(t)s, '["T-04"]', '["presence"]',
              'feasible', '[]', '{"synthetic": "scale fixture"}', '', now(), %(by)s, lead_id, robot_id, second_id,
              vehicle_id, visit_id
            FROM scale_plan WHERE status <> 'cancelled' AND status <> 'requested'
            """,
            {"t": TRAVEL, "by": w.ops.pk},
        )
        cur.execute(
            "UPDATE scale_plan p SET revision_id = r.id FROM planning_visitrevision r WHERE r.visit_id = p.visit_id"
        )
        # reservations: robot timeline, crew shifts, vehicle trip (readiness/0006 section A + E).
        # Inserted in 8 chunks of visits: at 500 robots one ~45,000-row statement can pass the app's 10 s
        # statement_timeout on a loaded host (Phase 06 install/perf run).
        for chunk in range(8):
            cur.execute(
                """
            INSERT INTO planning_reservation (purpose, starts_at, ends_at, released_reason, resource_id, revision_id)
            SELECT t.purpose, t.s0, t.s1, '', t.res, p.revision_id
            FROM scale_plan p
            CROSS JOIN LATERAL (SELECT
                p.on_site_start - make_interval(mins => %(tr)s) AS depart,
                p.on_site_start - make_interval(mins => %(tr)s + %(ld)s) AS load_start,
                p.on_site_start - make_interval(mins => %(tr)s + %(ld)s + %(insp)s) AS insp_start,
                p.on_site_start + interval '1 hour' + make_interval(mins => %(tr)s) AS back,
                p.on_site_start + interval '1 hour' + make_interval(mins => %(tr)s + %(ld)s) AS unload_end,
                p.on_site_start + interval '1 hour' + make_interval(mins => %(tr)s + %(ld)s + %(ch)s) AS charge_end
            ) x
            CROSS JOIN LATERAL (VALUES
              ((SELECT id FROM planning_resource WHERE robot_id = p.robot_id), 'inspection', x.insp_start, x.load_start),
              ((SELECT id FROM planning_resource WHERE robot_id = p.robot_id), 'loading', x.load_start, x.depart),
              ((SELECT id FROM planning_resource WHERE robot_id = p.robot_id), 'travel_out', x.depart, p.on_site_start),
              ((SELECT id FROM planning_resource WHERE robot_id = p.robot_id), 'on_site', p.on_site_start,
                p.on_site_start + interval '1 hour'),
              ((SELECT id FROM planning_resource WHERE robot_id = p.robot_id), 'travel_back',
                p.on_site_start + interval '1 hour', x.back),
              ((SELECT id FROM planning_resource WHERE robot_id = p.robot_id), 'unloading', x.back, x.unload_end),
              ((SELECT id FROM planning_resource WHERE robot_id = p.robot_id), 'charging', x.unload_end, x.charge_end),
              ((SELECT id FROM planning_resource WHERE person_id = p.lead_id), 'field_shift', x.insp_start, x.unload_end),
              ((SELECT id FROM planning_resource WHERE person_id = p.second_id), 'field_shift', x.insp_start,
                x.unload_end),
              ((SELECT id FROM planning_resource WHERE vehicle_id = p.vehicle_id), 'trip', x.load_start, x.unload_end)
            ) t(res, purpose, s0, s1)
            WHERE p.revision_id IS NOT NULL AND p.visit_id %% 8 = %(chunk)s
            """,
                {"tr": TRAVEL, "ld": LOAD, "insp": INSP, "ch": CHARGE, "chunk": chunk},
            )
        cur.execute(
            """
            WITH t AS (
              INSERT INTO planning_vehicletrip (depart_at, return_at, vehicle_id, revision_id)
              SELECT p.on_site_start - make_interval(mins => %(tr)s + %(ld)s),
                     p.on_site_start + interval '1 hour' + make_interval(mins => %(tr)s + %(ld)s), p.vehicle_id,
                     p.revision_id
              FROM scale_plan p WHERE p.revision_id IS NOT NULL RETURNING id, revision_id)
            UPDATE scale_plan p SET trip_id = t.id FROM t WHERE t.revision_id = p.revision_id
            """,
            {"tr": TRAVEL, "ld": LOAD},
        )
        cur.execute(
            "INSERT INTO planning_tripcargo (robot_id, trip_id) SELECT robot_id, trip_id FROM scale_plan"
            " WHERE trip_id IS NOT NULL"
        )
        # fresh statistics, so the per-row trigger queries of the bulk statements below use the indexes
        cur.execute(
            "ANALYZE planning_visit, planning_visitrevision, planning_reservation, planning_vehicletrip,"
            " planning_tripcargo, planning_resource, scale_plan"
        )
        cur.execute(
            "UPDATE planning_visit v SET current_revision_id = p.revision_id, status = 'planned' FROM scale_plan p"
            " WHERE v.id = p.visit_id AND p.revision_id IS NOT NULL"
        )
        cur.execute(
            "UPDATE planning_visit v SET status = 'confirmed' FROM scale_plan p"
            " WHERE v.id = p.visit_id AND p.status = 'confirmed'"
        )
        cur.execute(
            "UPDATE planning_visit v SET status = 'cancelled', cancelled_reason = 'SYNTHETIC' FROM scale_plan p"
            " WHERE v.id = p.visit_id AND p.status = 'cancelled'"
        )
        lap("visits")

        # ---------------------------------------------------------------- departures and run streams in waves
        # Statistics left by earlier work in the same database can be far off: refresh before the waves.
        cur.execute(ANALYZE_SCALE)
        first_wave_open = {}
        for k in range(closed_runs_per_robot + 1):
            _scale_depart_wave(cur, w, k)
            if k < closed_runs_per_robot:
                _scale_run_wave(cur, w, k, "closed")
            else:
                # the last departure of every robot is left at a varied stage (some closed too)
                cur.execute("SELECT n FROM scale_plan WHERE k = %s ORDER BY n", [k])
                for (n,) in cur.fetchall():
                    first_wave_open[n] = rng.choice(SCALE_OPEN_STAGES)
                for stage in SCALE_OPEN_STAGES:
                    ns = [n for n, s in first_wave_open.items() if s == stage]
                    if ns:
                        _scale_run_wave(cur, w, k, stage, only=ns)
            cur.execute(
                "ANALYZE execution_executionrun, execution_custodyevent, execution_tripevent,"
                " fleet_inspection, readiness_readinessevidence, readiness_departureauthorization,"
                " readiness_departureevent, execution_returnreconciliation, execution_tripclosure"
            )
            lap(f"runs_wave_{k}")

    # ---------------------------------------------------------------- incidents (commands; each holds the robot)
    robots_by_code = codes
    for n in range(incidents):
        code = robots_by_code[rng.randrange(len(robots_by_code))]
        event = rng.choices(["fall", "contact", "other", "robot_unaccounted"], [40, 30, 28, 2])[0]
        try:
            icode = report(w, code, event=event, minutes_ago=rng.randint(1, 600))
        except Exception:
            icode = report(w, code, event="other", minutes_ago=rng.randint(1, 600))
        if n % 3 == 0:
            evolve_incident(w, icode, rng)
    with connection.cursor() as cur:
        cur.execute(ANALYZE_SCALE)  # leave fresh statistics for the page queries run on the fixture
    lap("incidents")
    w.robot_codes = codes
    w.scale_open_stages = first_wave_open
    return w


ANALYZE_SCALE = (
    "ANALYZE fleet_robot, fleet_hold, fleet_inspection, fleet_configurationmanifest, fleet_baseauthorization,"
    " planning_visit, planning_visitrevision, planning_reservation, planning_person, planning_resource,"
    " readiness_readinessevidence, readiness_recoveryplan, readiness_departureauthorization,"
    " readiness_departureevent, execution_executionrun, execution_custodyevent, execution_custodyverification,"
    " execution_tripevent, execution_returnreconciliation, execution_tripclosure, execution_incident,"
    " execution_severitydecision, execution_incidentstatuschange, execution_incidentescalation,"
    " execution_escalationack, accounts_rolegrant"
)

SCALE_OPEN_STAGES = [
    "departed",
    "verified",
    "at_site",
    "returning",
    "arrived_hub",
    "at_hub",
    "inspected",
    "unresolved",
    "reconciled",
    "closed",
]


def _scale_depart_wave(cur: Any, w: SimpleNamespace, k: int) -> None:
    """Evidence + recovery plan + authorization (readiness_issue_authorization) + departure for visit k of
    every robot."""
    cur.execute(
        """
        INSERT INTO fleet_inspection (code, visit_ref, answers, result, complete, failed_robot_items,
          failed_visit_items, submitted_at, manifest_id, performer_id, submitted_by_id, verifier_id, template_id,
          robot_id)
        SELECT execution_code('RC', nextval('fleet_inspection_code_seq')), v.code, %(answers)s, 'PASS', true,
          '[]', '[]', clock_timestamp(), rb.current_manifest_id, %(perf)s, %(by)s, %(ver)s, t.id, rb.id
        FROM scale_plan p JOIN planning_visit v ON v.id = p.visit_id JOIN fleet_robot rb ON rb.id = p.robot_id
        JOIN fleet_configurationmanifest m ON m.id = rb.current_manifest_id
        JOIN fleet_inspectiontemplate t ON t.pattern_id = m.pattern_id AND t.version = 'T1'
        WHERE p.k = %(k)s
        """,
        {"answers": _json(dict(w.all_pass)), "perf": w.field.pk, "ver": w.field2.pk, "by": w.ops.pk, "k": k},
    )
    for kind, details, insp in (
        ("pre_departure_inspection", {}, True),
        (
            "equipment_check",
            {"items": {"restraint_kit": "present", "recovery_kit": "present", "charger": "present"}},
            False,
        ),
        ("battery_check", {"soc_pct": 85}, False),
    ):
        cur.execute(
            f"""
            INSERT INTO readiness_readinessevidence (code, kind, result, details, observed_at, recorded_at,
              inspection_id, manifest_id, recorded_by_id, revision_id, robot_id, visit_id)
            SELECT execution_code('EV', nextval('readiness_evidence_code_seq')), %(kind)s, 'pass', %(d)s,
              clock_timestamp(), clock_timestamp(),
              {"(SELECT i.id FROM fleet_inspection i WHERE i.robot_id = p.robot_id AND i.visit_ref = v.code ORDER BY i.id DESC LIMIT 1)" if insp else "NULL"},
              rb.current_manifest_id, %(by)s, p.revision_id, p.robot_id, p.visit_id
            FROM scale_plan p JOIN planning_visit v ON v.id = p.visit_id JOIN fleet_robot rb ON rb.id = p.robot_id
            WHERE p.k = %(k)s
            """,
            {"kind": kind, "d": _json(details), "by": w.ops.pk, "k": k},
        )
    cur.execute(
        """
        INSERT INTO readiness_recoveryplan (escalation_path, required_equipment, recorded_at, escalation_contact_id,
          manifest_id, on_call_engineer_id, recorded_by_id, revision_id, visit_id)
        SELECT 'SYNTHETIC: on-call engineer -> engineering lead -> ops lead', '["recovery_kit"]', now(),
          (SELECT id FROM planning_person WHERE code = 'P-5'), rb.current_manifest_id,
          (SELECT id FROM planning_person WHERE code = 'P-4'), %(by)s, p.revision_id, p.visit_id
        FROM scale_plan p JOIN fleet_robot rb ON rb.id = p.robot_id WHERE p.k = %(k)s
        """,
        {"by": w.ops.pk, "k": k},
    )
    # Phase 05: issued in chunks of SCALE_CHUNK robots, so one statement stays well inside the app's 10 s
    # statement_timeout at 500 robots (each row runs the whole write boundary; one 500-row statement took > 10 s).
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS scale_auth (visit_id bigint, auth_id bigint) ON COMMIT DROP")
    cur.execute("SELECT coalesce(max(n), 0) FROM scale_plan")
    top = int(cur.fetchone()[0])
    for lo in range(0, top + 1, SCALE_CHUNK):
        _scale_authorize(cur, w, k, lo, lo + SCALE_CHUNK)
    # Slice 7 (EM ruling 1): the departure trigger asserts the settings lock; take it SHARED first.
    cur.execute("SELECT pg_advisory_xact_lock_shared(64942988919)")
    for lo in range(0, top + 1, SCALE_CHUNK):
        cur.execute(
            """
            INSERT INTO readiness_departureevent (guard_snapshot, authorization_id, recorded_by_id, robot_id, visit_id)
            SELECT a.guard_snapshot, a.id, %(ops)s, a.robot_id, a.visit_id
            FROM scale_plan p JOIN scale_auth s ON s.visit_id = p.visit_id
            JOIN readiness_departureauthorization a ON a.id = s.auth_id
            WHERE p.k = %(k)s AND p.n >= %(lo)s AND p.n < %(hi)s
            """,
            {"ops": w.ops.pk, "k": k, "lo": lo, "hi": lo + SCALE_CHUNK},
        )


SCALE_CHUNK = 100


def _scale_authorize(cur: Any, w: SimpleNamespace, k: int, lo: int, hi: int) -> None:
    cur.execute(
        """
        INSERT INTO scale_auth (visit_id, auth_id)
        SELECT p.visit_id, (readiness_issue_authorization(p.visit_id, %(ops)s, clock_timestamp() + interval '60 minutes',
            (SELECT jsonb_agg(e.id ORDER BY e.id) FROM readiness_readinessevidence e WHERE e.visit_id = p.visit_id),
            rp.id, s.snap, 'demo')).id
        FROM scale_plan p
        JOIN LATERAL (SELECT x.id, x.on_call_engineer_id, x.escalation_contact_id FROM readiness_recoveryplan x
                      WHERE x.visit_id = p.visit_id ORDER BY x.id DESC LIMIT 1) rp ON true
        CROSS JOIN LATERAL (
          SELECT jsonb_build_object(
            'visit_id', v.id, 'visit_version', v.version, 'revision_id', v.current_revision_id,
            'visit_status', v.status, 'site_id', v.site_id,
            'site_version', (SELECT version FROM planning_site WHERE id = v.site_id),
            'robot_id', rb.id, 'robot_version', rb.version, 'manifest_id', rb.current_manifest_id,
            'pattern_id', m.pattern_id,
            'pattern_version', (SELECT version FROM fleet_manifestpattern WHERE id = m.pattern_id),
            'person_versions', (SELECT jsonb_object_agg(pp.id::text, pp.version) FROM planning_person pp
                                WHERE pp.id IN (r.lead_id, r.second_id, rp.on_call_engineer_id,
                                                rp.escalation_contact_id)
                                   -- Slice 7 (07a 7.2): the inspection signers are dependency persons
                                   OR pp.user_id IN (%(perf)s, %(ver)s)),
            'vehicle_id', r.vehicle_id,
            'policy_id', readiness_active_policy_id(),
            'policy_lock_version', (SELECT lock_version FROM readiness_readinesspolicy
                                    WHERE id = readiness_active_policy_id())) AS snap
          FROM planning_visit v JOIN planning_visitrevision r ON r.id = v.current_revision_id
          JOIN fleet_robot rb ON rb.id = r.robot_id JOIN fleet_configurationmanifest m ON m.id = rb.current_manifest_id
          WHERE v.id = p.visit_id
        ) s
        WHERE p.k = %(k)s AND p.n >= %(lo)s AND p.n < %(hi)s
        """,
        {"ops": w.ops.pk, "k": k, "perf": w.field.pk, "ver": w.field2.pk, "lo": lo, "hi": hi},
    )


SCALE_STEPS = [
    "verified",
    "arrived_site",
    "at_site",
    "back_in_vehicle",
    "returning",
    "arrived_hub",
    "at_hub",
    "inspected",
    "reconciled",
    "closed",
]


def _scale_run_wave(cur: Any, w: SimpleNamespace, k: int, stage: str, only: list[int] | None = None) -> None:
    """Drive the runs of visit k (optionally only robots `only`, by index) to `stage` with set-based inserts."""
    if stage == "departed":
        return
    target = "reconciled" if stage == "unresolved" else stage
    steps = SCALE_STEPS[: SCALE_STEPS.index(target) + 1]
    sel = (
        "FROM scale_plan p JOIN execution_executionrun x ON x.visit_id = p.visit_id"
        " JOIN planning_visitrevision r ON r.id = x.revision_id"
        " JOIN planning_person ld ON ld.id = r.lead_id JOIN planning_person sd ON sd.id = r.second_id"
        " WHERE p.k = %(k)s AND (%(only)s::int[] IS NULL OR p.n = ANY(%(only)s::int[]))"
    )
    args = {"k": k, "only": only, "ops": w.ops.pk}

    def custody(frm: str, to: str, receiver: str = "sd.id") -> None:
        cur.execute(
            "INSERT INTO execution_custodyevent (seq, from_kind, to_kind, basis, note, recorded_at, custodian_id,"
            " received_by_id, recorded_by_id, robot_id, trip_id, visit_id, run_id)"
            f" SELECT (SELECT max(seq) + 1 FROM execution_custodyevent e WHERE e.robot_id = x.robot_id), '{frm}',"
            f" '{to}', 'handoff', '', now(), ld.id, {receiver}, sd.user_id, x.robot_id, x.trip_id, x.visit_id, x.id "
            + sel,
            args,
        )

    def trip(kind: str, seq: int) -> None:
        cur.execute(
            "INSERT INTO execution_tripevent (trip_id, run_id, seq, kind, recorded_by_id, recorded_at)"
            f" SELECT x.trip_id, x.id, {seq}, '{kind}', sd.user_id, now() " + sel,
            args,
        )

    for step in steps:
        if step == "verified":
            cur.execute(
                "INSERT INTO execution_custodyverification (custody_event_id, verified_by_id, recorded_by_id,"
                " recorded_at) SELECT (SELECT e.id FROM execution_custodyevent e WHERE e.run_id = x.id AND"
                " e.basis = 'administrative'), sd.id, sd.user_id, now() " + sel,
                args,
            )
        elif step == "arrived_site":
            trip("arrived_site", 1)
        elif step == "at_site":
            custody("vehicle", "site")
        elif step == "back_in_vehicle":
            custody("site", "vehicle")
        elif step == "returning":
            trip("departed_site", 2)
        elif step == "arrived_hub":
            trip("arrived_hub", 3)
        elif step == "at_hub":
            custody("vehicle", "hub")
        elif step == "inspected":
            cur.execute(
                "INSERT INTO fleet_inspection (code, visit_ref, answers, result, complete, failed_robot_items,"
                " failed_visit_items, submitted_at, manifest_id, performer_id, submitted_by_id, verifier_id,"
                " template_id, robot_id)"
                " SELECT execution_code('RC', nextval('fleet_inspection_code_seq')), p2.code, %(answers)s,"
                " 'PASS', true, '[]', '[]', clock_timestamp(), rb.current_manifest_id, %(perf)s, %(ops)s, %(ver)s,"
                " (SELECT t.id FROM fleet_inspectiontemplate t JOIN fleet_configurationmanifest m"
                "  ON m.pattern_id = t.pattern_id WHERE m.id = rb.current_manifest_id AND t.version = 'T1'), rb.id"
                " FROM scale_plan p JOIN execution_executionrun x ON x.visit_id = p.visit_id"
                " JOIN planning_visit p2 ON p2.id = p.visit_id JOIN fleet_robot rb ON rb.id = x.robot_id"
                " WHERE p.k = %(k)s AND (%(only)s::int[] IS NULL OR p.n = ANY(%(only)s::int[]))",
                {**args, "answers": _json(dict(w.all_pass)), "perf": w.field.pk, "ver": w.field2.pk},
            )
        elif step == "reconciled":
            unresolved = stage == "unresolved"
            cur.execute(
                "INSERT INTO execution_returnreconciliation (run_id, hub_receipt_id, cargo_matches,"
                " post_visit_inspection_id, discrepancies, outcome, recorded_by_id, recorded_at)"
                " SELECT x.id, (SELECT e.id FROM execution_custodyevent e WHERE e.run_id = x.id AND e.to_kind = 'hub'"
                "  ORDER BY e.seq DESC LIMIT 1), true, (SELECT i.id FROM fleet_inspection i WHERE i.robot_id ="
                "  x.robot_id AND i.visit_ref = (SELECT code FROM planning_visit WHERE id = x.visit_id)"
                "  ORDER BY i.id DESC LIMIT 1), %(disc)s, %(outcome)s, %(ops)s, now() " + sel,
                {
                    **args,
                    "disc": _json(["SYNTHETIC: strap frayed"] if unresolved else []),
                    "outcome": "unresolved" if unresolved else "reconciled",
                },
            )
        elif step == "closed":
            cur.execute(
                "INSERT INTO execution_tripclosure (trip_id, run_id, reconciliation_id, recorded_by_id, recorded_at)"
                " SELECT x.trip_id, x.id, (SELECT q.id FROM execution_returnreconciliation q WHERE q.run_id = x.id"
                "  ORDER BY q.recorded_at DESC, q.id DESC LIMIT 1), %(ops)s, now() " + sel,
                args,
            )


def _json(v: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(v)


__all__ = [
    "EDGE_CASES",
    "SCALE",
    "SCALE_OPEN_STAGES",
    "FULL_PROFILE",
    "HOLD_SOURCES",
    "INCIDENT_RULES",
    "RUN_STAGES",
    "Cmd",
    "add_robot",
    "advance",
    "depart_new",
    "edge_world",
    "evolve_incident",
    "make_incident_policy",
    "new_pattern",
    "ops_world",
    "place_hold",
    "random_world",
    "release",
    "report",
    "scale_world",
]
