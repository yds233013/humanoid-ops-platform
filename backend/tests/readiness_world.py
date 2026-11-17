"""Synthetic readiness world on top of the planning world (contract section 6). Built ONLY through the real
fleet / planning / readiness commands, so guard versions, audit rows and triggers are exercised exactly as in
production. SYNTHETIC data only.

    w = build_readiness("rx_")
    w.visit                       # confirmed visit code, with every piece of evidence + recovery plan recorded
    w.policy_version              # approved synthetic_demo policy (REQUIREMENTS)
    w.evidence                    # {"pre_departure_inspection": "EV-0001", "equipment_check": ..., ...}
    w.inspection                  # the PASS inspection code behind the pre-departure evidence
    w.recovery_plan_id

People (users from build_world plus):
    w.lead  engineering_lead (authors policies)   w.lead2  second engineering_lead (approves 'engineering')
    w.safety safety_reviewer (approves 'safety')   w.ops ops_lead (plans, confirms, records evidence)
    persons P-1 (ops, field lead), P-2 (field), P-3 (field2), P-4 (eng, on-call), P-5 (lead2, escalation)

Helpers (module functions, also attached to w):
    new_confirmed_visit(w, **kw) -> code     plan + confirm a fresh visit (planning_world.new_visit kwargs)
    record_inspection(w, visit_code) -> code a complete PASS fleet inspection with visit_ref
    record_all_evidence(w, visit_code)       inspection + equipment + battery evidence + recovery plan
    make_policy(w, source=..., requirements=..., approve=True) -> version
"""

import copy
from types import SimpleNamespace
from typing import Any

from fleet import services as fleet_services
from fleet.models import CapabilityProfile
from readiness import evidence as rx
from tests.conftest import make_user
from tests.planning_world import build_planning, new_visit, plan

REQUIREMENTS: dict[str, Any] = {
    "authorization_ttl_min": 90,
    "evidence": [
        {"kind": "pre_departure_inspection", "max_age_min": 240},
        {"kind": "equipment_check", "max_age_min": 240},
        {"kind": "battery_check", "max_age_min": 120},
    ],
    "required_equipment": ["restraint_kit", "recovery_kit", "charger"],
    "recovery_plan_required": True,
}
EQUIPMENT_ALL_PRESENT: dict[str, Any] = {
    "items": {"restraint_kit": "present", "recovery_kit": "present", "charger": "present"}
}
RECOVERY_EQUIPMENT = ["recovery_kit"]
MIN_DEPARTURE_SOC = 60
BATTERY_SOC = 85


def requirements(**overrides: Any) -> dict[str, Any]:
    """A deep copy of REQUIREMENTS with top-level keys replaced."""
    req = copy.deepcopy(REQUIREMENTS)
    req.update(overrides)
    return req


def make_policy(
    w: SimpleNamespace,
    source: str = "synthetic_demo",
    requirements: dict[str, Any] | None = None,
    approve: bool = True,
    notes: str = "SYNTHETIC test policy",
) -> int:
    """Create a policy as w.lead; approve both slots (w.lead2 engineering, w.safety safety) unless told not to."""
    version = int(
        rx.create_readiness_policy(
            w.lead, requirements=requirements or copy.deepcopy(REQUIREMENTS), source=source, notes=notes
        )["entity_id"]
    )
    if approve:
        rx.approve_readiness_policy(w.lead2, version=version, slot="engineering")
        rx.approve_readiness_policy(w.safety, version=version, slot="safety")
    return version


def new_confirmed_visit(w: SimpleNamespace, robot: str = "R-01", **kw: Any) -> str:
    code = new_visit(w, **kw)
    plan(w, code, robot=robot)
    w.p.confirm_visit(w.ops, visit_code=code)
    return code


def record_inspection(w: SimpleNamespace, visit_code: str, robot: str = "R-01") -> str:
    return fleet_services.submit_inspection(
        w.ops,
        robot_code=robot,
        template_version="T1",
        answers=dict(w.all_pass),
        performer=w.field.username,
        verifier=w.field2.username,
        visit_ref=visit_code,
    )["entity_id"]


def record_all_evidence(w: SimpleNamespace, visit_code: str, robot: str = "R-01") -> dict[str, Any]:
    """Inspection + passing evidence of every kind + recovery plan for the visit's current revision."""
    insp = record_inspection(w, visit_code, robot=robot)
    ev = {
        "pre_departure_inspection": rx.record_evidence(
            w.ops, visit_code=visit_code, kind="pre_departure_inspection", result="pass", inspection_code=insp
        )["entity_id"],
        "equipment_check": rx.record_evidence(
            w.field,
            visit_code=visit_code,
            kind="equipment_check",
            result="pass",
            details=copy.deepcopy(EQUIPMENT_ALL_PRESENT),
        )["entity_id"],
        "battery_check": rx.record_evidence(
            w.field,
            visit_code=visit_code,
            kind="battery_check",
            result="pass",
            details={"soc_pct": BATTERY_SOC},
        )["entity_id"],
    }
    plan_id = int(
        rx.record_recovery_plan(
            w.ops,
            visit_code=visit_code,
            on_call_code="P-4",
            escalation_code="P-5",
            escalation_path="SYNTHETIC: on-call engineer -> engineering lead -> ops lead",
            required_equipment=list(RECOVERY_EQUIPMENT),
        )["entity_id"]
    )
    return {"inspection": insp, "evidence": ev, "recovery_plan_id": plan_id}


def build_readiness(prefix: str = "", robots: int = 3) -> SimpleNamespace:
    w = build_planning(robots=robots, prefix=prefix)
    w.lead2 = make_user(f"{prefix}lead2", "engineering_lead")
    # Capability profile v2 for P-A with a known departure battery minimum (SYNTHETIC).
    fleet_services.publish_capability_profile(
        w.eng,
        pattern_code="P-A",
        stop_behaviour="SYNTHETIC",
        lift_crew=2,
        max_runtime_min=120,
        charge_turnaround_min=180,
        load_unload_min=20,
        min_departure_soc_pct=MIN_DEPARTURE_SOC,
    )
    prof = CapabilityProfile.objects.get(pattern__code="P-A", version_no=2)
    fleet_services.approve_capability_profile(w.lead, profile_id=prof.pk, slot="engineering")
    fleet_services.approve_capability_profile(w.safety, profile_id=prof.pk, slot="safety")
    # Recovery-plan people: on-call engineer and escalation contact.
    w.p.register_person(w.ops, username=w.eng.username, code="P-4")
    w.p.register_person(w.ops, username=w.lead2.username, code="P-5")

    w.policy_version = make_policy(w)
    w.visit = new_confirmed_visit(w)
    rec = record_all_evidence(w, w.visit)
    w.inspection = rec["inspection"]
    w.evidence = rec["evidence"]
    w.recovery_plan_id = rec["recovery_plan_id"]

    w.rx = rx
    w.make_policy = lambda **kw: make_policy(w, **kw)
    w.new_confirmed_visit = lambda **kw: new_confirmed_visit(w, **kw)
    w.record_inspection = lambda code, **kw: record_inspection(w, code, **kw)
    w.record_all_evidence = lambda code, **kw: record_all_evidence(w, code, **kw)
    return w
