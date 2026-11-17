"""Slice 2 behaviour through the command layer."""

from datetime import datetime, timedelta

import pytest
from rest_framework.test import APIClient

from fleet import services as fs
from planning import services as p
from planning.models import ConsentRecord, Qualification, Reservation, TripCargo, Visit, VisitRevision
from platform_core.commands import Conflict, Invalid
from tests.fleet_world import set_setting
from tests.planning_world import DAY, TZ, add_site, at, build_planning, new_visit, plan

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    return build_planning()


def active(code):
    return Reservation.objects.filter(revision__visit__code=code, released_at__isnull=True)


def codes(result):
    return [b for b in result["blockers"]]


# ---------- the happy path and what it reserves
def test_feasible_plan_reserves_robot_crew_and_vehicle(w):
    v = new_visit(w)
    r = plan(w, v)
    assert r["feasibility"] == "feasible" and r["label"] == "Planning feasible"
    purposes = sorted(active(v).filter(resource__kind="robot").values_list("purpose", flat=True))
    assert purposes == sorted(
        ["inspection", "loading", "travel_out", "on_site", "travel_back", "unloading", "charging"]
    )
    assert active(v).filter(resource__kind="person").count() == 2
    assert active(v).filter(resource__kind="vehicle").count() == 1
    assert TripCargo.objects.filter(trip__revision__visit__code=v).count() == 1
    crew = active(v).filter(resource__kind="person").first()
    # inspection 30 + loading 20 + travel 30 before; travel 30 + unloading 20 after
    assert crew.starts_at == at(DAY, 10) - timedelta(minutes=80)
    assert crew.ends_at == at(DAY, 11) + timedelta(minutes=50)
    charging = active(v).get(resource__kind="robot", purpose="charging")
    assert charging.ends_at - charging.starts_at == timedelta(minutes=180)


def test_confirm_is_planning_only(w):
    v = new_visit(w)
    plan(w, v)
    r = p.confirm_visit(w.ops, visit_code=v)
    assert r["status"] == "confirmed" and "not cleared to depart" in r["note"]
    c = APIClient()
    c.force_login(w.ops)
    page = c.get(f"/planning/visits/{v}/").content.decode()
    assert "not cleared to depart" in page
    for word in ("Ready to deploy", "Cleared to depart", "Approved to depart"):
        assert word not in page


# ---------- conflicts and adjacency
def test_overlapping_crew_is_blocked_and_explained(w):
    v1 = new_visit(w)
    plan(w, v1)
    v2 = new_visit(w, h0=11, h1=12)
    r = plan(w, v2, robot="R-02")
    assert r["feasibility"] == "blocked"
    assert any(f"already booked for {v1}" in m for m in r["blockers"])
    assert Visit.objects.get(code=v2).status == "requested" and not active(v2).exists()
    assert VisitRevision.objects.get(visit__code=v2, revision_no=2).feasibility == "blocked"


def test_back_to_back_is_allowed_half_open(w):
    v1 = new_visit(w)  # crew busy [08:40, 11:50) local
    plan(w, v1)
    start2 = at(DAY, 11, 50) + timedelta(minutes=80)  # crew block starts exactly at 11:50
    v2 = p.request_visit(
        w.ops,
        site_code="S-1",
        on_site_start=start2,
        on_site_end=start2 + timedelta(hours=1),
        travel_out_min=30,
        travel_back_min=30,
        tasks=["T-04"],
    )["entity_id"]
    assert plan(w, v2, robot="R-02", vehicle="VAN-2")["feasibility"] == "feasible"


def test_robot_charging_turnaround_blocks_reuse(w):
    v1 = new_visit(w)
    plan(w, v1)
    v2 = new_visit(w, h0=14, h1=15)  # different crew and van, same robot inside its charging window
    r = plan(w, v2, lead="P-3", second="P-2", vehicle="VAN-2")
    assert r["feasibility"] == "blocked" and any("Charging" in m for m in r["blockers"])


# ---------- unknowns block
def test_unknown_facts_block(w):
    v = new_visit(w, travel=None)
    assert any("Travel time is unknown" in m for m in plan(w, v)["blockers"])
    v2 = new_visit(w, d=DAY + timedelta(days=1))
    assert any("capacity is unknown" in m for m in plan(w, v2, vehicle="VAN-X")["blockers"])
    set_setting("pre_visit_inspection_min", "")
    v3 = new_visit(w, d=DAY + timedelta(days=2))
    assert any("inspection duration" in m for m in plan(w, v3)["blockers"])


def test_unknown_load_time_blocks(w):
    fs.publish_capability_profile(
        w.eng, pattern_code="P-C", stop_behaviour="x", lift_crew=2, max_runtime_min=1, charge_turnaround_min=1
    )  # no load_unload_min
    prof = fs._pattern("P-C").profiles.order_by("-version_no").first()
    fs.approve_capability_profile(w.lead, profile_id=prof.pk, slot="engineering")
    fs.approve_capability_profile(w.safety, profile_id=prof.pk, slot="safety")
    fs.record_manifest(
        w.eng, robot_code="R-03", pattern_code="P-C", autonomy_version="a", safety_config_hash="h"
    )
    fs.grant_base_authorization(w.lead, robot_code="R-03")
    v = new_visit(w)
    assert any("Loading/unloading time" in m for m in plan(w, v, robot="R-03")["blockers"])


# ---------- consent
def test_unknown_adult_count_requires_review_and_cannot_confirm(w):
    v = new_visit(w, site="S-3")
    r = plan(w, v)
    assert r["feasibility"] == "review"
    with pytest.raises(Conflict, match="Requires review"):
        p.confirm_visit(w.ops, visit_code=v)


def test_consent_withdrawal_is_immediately_effective(w):
    v = new_visit(w)
    plan(w, v)
    c = ConsentRecord.objects.get(site__code="S-1", participant_ref="P2")
    p.withdraw_consent(w.field, consent_id=c.pk, reason="changed mind")
    live = p.live_feasibility(Visit.objects.get(code=v))
    assert live.feasibility == "blocked" and any(b["code"] == "CONSENT" for b in live.blockers)
    with pytest.raises(Conflict, match="Blocked"):
        p.confirm_visit(w.ops, visit_code=v)
    assert ConsentRecord.objects.filter(site__code="S-1").count() == 2  # history kept


def test_consent_scope_and_form_version(w):
    add_site(w, "S-9", 1, scope=["presence"])
    v = new_visit(w, site="S-9", scopes=["presence", "recording"])
    assert any("covering presence, recording" in m for m in plan(w, v)["blockers"])
    set_setting("consent_form_version", "CF-v2")
    v2 = new_visit(w, d=DAY + timedelta(days=1))
    assert any("CF-v2" in m for m in plan(w, v2)["blockers"])


# ---------- site
def test_restricted_site_needs_review_and_excluded_task_blocks(w):
    v = new_visit(w, site="S-2")
    assert plan(w, v)["feasibility"] == "review"
    add_site(w, "S-8", 1, excluded=["T-02"])
    v2 = new_visit(w, site="S-8", tasks=["T-02"], d=DAY + timedelta(days=1))
    assert any("excluded" in m for m in plan(w, v2)["blockers"])


# ---------- people
def test_qualification_must_cover_the_whole_activity(w):
    q = Qualification.objects.get(person__code="P-2", kind="robot_handler")
    p.revoke_qualification(w.ops, qualification_id=q.pk, reason="expired")
    p.grant_qualification(
        w.ops, person_code="P-2", kind="robot_handler", valid_from=at(DAY, 0), valid_to=at(DAY, 10, 30)
    )  # expires mid-visit
    v = new_visit(w)
    assert any("robot handling" in m for m in plan(w, v)["blockers"])


def test_crew_must_be_two_different_people(w):
    v = new_visit(w)
    assert any("different people" in m for m in plan(w, v, second="P-1")["blockers"])


# ---------- changing eligibility after planning
def test_hold_after_planning_blocks_confirmation(w):
    v = new_visit(w)
    plan(w, v)
    fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="noise in hip")
    assert p.live_feasibility(Visit.objects.get(code=v)).feasibility == "blocked"
    assert active(v).exists()  # the reservation is NOT evidence of eligibility
    with pytest.raises(Conflict, match="Robot hold"):
        p.confirm_visit(w.ops, visit_code=v)


# ---------- reschedule and cancel
def test_reschedule_is_atomic_and_keeps_history(w):
    v = new_visit(w)
    plan(w, v)
    old_ids = set(active(v).values_list("pk", flat=True))
    plan(w, v, on_site_start=at(DAY, 13), on_site_end=at(DAY, 14), reason="customer asked for afternoon")
    assert not (set(active(v).values_list("pk", flat=True)) & old_ids)
    released = Reservation.objects.filter(pk__in=old_ids)
    assert all(r.released_reason == "superseded by r3" for r in released)
    assert Visit.objects.get(code=v).current_revision.revision_no == 3


def test_failed_reschedule_leaves_the_confirmed_plan_intact(w):
    v1 = new_visit(w)
    plan(w, v1)
    p.confirm_visit(w.ops, visit_code=v1)
    v2 = new_visit(w, h0=15, h1=16)
    plan(w, v2, robot="R-02", lead="P-3", vehicle="VAN-2")
    before = set(active(v1).values_list("pk", flat=True))
    with pytest.raises(Conflict, match="current plan is unchanged"):
        plan(w, v1, on_site_start=at(DAY, 15), on_site_end=at(DAY, 16))  # P-2 busy with v2
    v = Visit.objects.get(code=v1)
    assert v.status == "confirmed" and v.current_revision.revision_no == 2
    assert set(active(v1).values_list("pk", flat=True)) == before


def test_cancel_releases_everything_and_is_final(w):
    v = new_visit(w)
    plan(w, v)
    p.cancel_visit(w.ops, visit_code=v, reason="customer cancelled")
    assert not active(v).exists() and Reservation.objects.filter(revision__visit__code=v).exists()
    with pytest.raises(Conflict):
        plan(w, v)
    assert plan(w, new_visit(w))["feasibility"] == "feasible"  # resources free again


# ---------- time
def test_local_times_refuse_dst_gaps_and_ambiguity():
    with pytest.raises(Invalid, match="does not exist"):
        p.local_to_utc(datetime(2027, 3, 14, 2, 30), TZ)
    with pytest.raises(Invalid, match="ambiguous"):
        p.local_to_utc(datetime(2026, 11, 1, 1, 30), TZ)
    assert p.local_to_utc(datetime(2027, 3, 14, 10, 0), TZ).hour == 17  # PDT, UTC-7
    assert p.local_to_utc(datetime(2026, 12, 8, 10, 0), TZ).hour == 18  # PST, UTC-8


def test_visit_spanning_fall_back_uses_absolute_time(w):
    from zoneinfo import ZoneInfo

    utc = ZoneInfo("UTC")
    start = datetime(2026, 11, 1, 8, 0, tzinfo=utc)  # 01:00 PDT
    end = datetime(2026, 11, 1, 10, 0, tzinfo=utc)  # 02:00 PST: 2 real hours, 1 wall-clock hour
    v = p.request_visit(
        w.ops,
        site_code="S-1",
        on_site_start=start,
        on_site_end=end,
        travel_out_min=30,
        travel_back_min=30,
        tasks=["T-04"],
    )["entity_id"]
    plan(w, v)
    on_site = active(v).get(resource__kind="robot", purpose="on_site")
    assert on_site.ends_at - on_site.starts_at == timedelta(hours=2)


def test_naive_times_are_refused(w):
    with pytest.raises(Invalid, match="timezone-aware"):
        p.request_visit(
            w.ops,
            site_code="S-1",
            on_site_start=datetime(2026, 12, 8, 10),
            on_site_end=datetime(2026, 12, 8, 11),
        )


# ---------- UI
def test_planner_pages_render_and_plan_form_works(w):
    v = new_visit(w)
    c = APIClient()
    c.force_login(w.ops)
    assert "Planner" in c.get(f"/planning/?week={DAY.isoformat()}").content.decode()
    assert "S-3" in c.get("/planning/sites/").content.decode()
    r = c.post(
        f"/planning/visits/{v}/plan/", {"robot": "R-01", "lead": "P-1", "second": "P-2", "vehicle": "VAN-1"}
    )
    assert r.status_code == 302
    page = c.get(f"/planning/visits/{v}/").content.decode()
    assert "Plan saved" in page and "Planning feasible" in page
    week = c.get(f"/planning/?week={DAY.isoformat()}").content.decode()
    assert v in week


# ---------- Slice 2 closure fixes
def test_invalid_counts_and_durations_are_refused(w):
    with pytest.raises(Invalid):
        p.create_site(w.ops, code="S-0", zone="z", tz=TZ, crm_ref="c", expected_adults=0)
    with pytest.raises(Invalid):
        p.register_vehicle(w.ops, code="VAN-0", robot_capacity=0)
    with pytest.raises(Invalid):
        p.request_visit(
            w.ops,
            site_code="S-1",
            on_site_start=at(DAY, 10),
            on_site_end=at(DAY, 11),
            travel_out_min=0,
            travel_back_min=30,
        )
    v = new_visit(w)
    with pytest.raises(Invalid):
        plan(w, v, travel_out_min=-5)
    set_setting("pre_visit_inspection_min", "0")
    assert any("inspection duration" in m for m in plan(w, v)["blockers"])
