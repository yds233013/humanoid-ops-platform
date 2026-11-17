"""Synthetic planning world on top of the fleet world."""

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

from django.utils import timezone

from planning import services as p
from tests.fleet_world import build_world, set_setting

TZ = "America/Los_Angeles"
DAY = date(2026, 12, 8)  # a Tuesday, no DST change


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return p.local_to_utc(datetime.combine(d, time(hh, mm)), TZ)


def build_planning(robots: int = 3, prefix: str = "") -> SimpleNamespace:
    w = build_world(robots=robots, prefix=prefix)
    set_setting("consent_form_version", "CF-v1")
    set_setting("pre_visit_inspection_min", "30")
    lo, hi = timezone.now() - timedelta(days=30), datetime(2027, 6, 1, tzinfo=UTC)
    for user, code, kinds in (
        (w.ops, "P-1", ["field_lead", "robot_handler"]),
        (w.field, "P-2", ["field_operator", "robot_handler"]),
        (w.field2, "P-3", ["field_lead", "field_operator", "robot_handler"]),
    ):
        p.register_person(w.ops, username=user.username, code=code)
        for k in kinds:
            p.grant_qualification(w.ops, person_code=code, kind=k, valid_from=lo, valid_to=hi)
    p.register_vehicle(w.ops, code="VAN-1", robot_capacity=1, restraint_kit="SYN kit")
    p.register_vehicle(w.ops, code="VAN-2", robot_capacity=1, restraint_kit="SYN kit")
    p.register_vehicle(w.ops, code="VAN-X")  # unknown capacity and restraint
    for code, adults, decision in (
        ("S-1", 2, "eligible"),
        ("S-2", 1, "eligible_restricted"),
        ("S-3", None, "eligible"),
    ):
        add_site(w, code, adults, decision)
    w.p = p
    return w


def add_site(
    w,
    code: str,
    adults: int | None,
    decision: str = "eligible",
    consents: int | None = None,
    scope: list[str] | None = None,
    excluded: list[str] | None = None,
) -> None:
    p.create_site(w.ops, code=code, zone="Zone S", tz=TZ, crm_ref=f"CRM-{code}", expected_adults=adults)
    a = p.record_assessment(
        w.field,
        site_code=code,
        decision=decision,
        valid_until=date(2027, 3, 1),
        restrictions="synthetic restriction" if decision != "eligible" else "",
        excluded_tasks=excluded or [],
    )
    p.approve_assessment(w.ops, assessment_id=int(a["entity_id"]))
    for n in range(1, (consents if consents is not None else (adults or 1)) + 1):
        p.record_consent(
            w.field,
            site_code=code,
            participant_ref=f"P{n}",
            form_version="CF-v1",
            scope=scope or ["presence", "recording"],
            evidence_ref=f"DOC-{code}-{n}",
        )


def new_visit(
    w,
    site: str = "S-1",
    d: date = DAY,
    h0: int = 10,
    h1: int = 11,
    travel: int | None = 30,
    tasks: list[str] | None = None,
    scopes: list[str] | None = None,
) -> str:
    return p.request_visit(
        w.ops,
        site_code=site,
        on_site_start=at(d, h0),
        on_site_end=at(d, h1),
        travel_out_min=travel,
        travel_back_min=travel,
        tasks=tasks or ["T-04"],
        required_scopes=scopes or ["presence"],
    )["entity_id"]


def plan(
    w, code: str, robot: str = "R-01", lead: str = "P-1", second: str = "P-2", vehicle: str = "VAN-1", **kw
) -> dict:
    return p.plan_visit(
        w.ops,
        visit_code=code,
        robot_code=robot,
        lead_code=lead,
        second_code=second,
        vehicle_code=vehicle,
        **kw,
    )
