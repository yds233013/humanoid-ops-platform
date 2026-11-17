"""Command-level tests for departure authorization, departure and revocation (readiness/commands.py) and the
advisory authorization_status (readiness/authorization.py). Real Postgres, real commands, SYNTHETIC data."""

import re
import time
from datetime import timedelta

import pytest

from accounts.models import RoleGrant
from accounts.services import revoke_role
from fleet import services as fs
from fleet.models import Robot
from planning.models import ConsentRecord, Qualification, Visit
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.models import AuditEvent
from readiness import commands as rc
from readiness.authorization import authorization_status
from readiness.evaluator import db_now, evaluate_readiness
from readiness.models import (
    AuthorizationRevocation,
    DepartureAuthorization,
    DepartureEvent,
    ReadinessEvidence,
)
from tests.conftest import make_user
from tests.fleet_world import set_setting
from tests.planning_world import DAY, plan
from tests.readiness_world import (
    EQUIPMENT_ALL_PRESENT,
    build_readiness,
    new_confirmed_visit,
    record_inspection,
)


@pytest.fixture
def w(transactional_db):
    return build_readiness(prefix="ra_")


def authorize(w, actor=None, visit=None, **kw):
    return rc.authorize_departure(actor or w.ops, visit_code=visit or w.visit, **kw)


def depart(w, code, actor=None, **kw):
    return rc.record_departure(actor or w.ops, authorization_code=code, **kw)


def auth_row(code):
    return DepartureAuthorization.objects.get(code=code)


def status(code):
    return authorization_status(auth_row(code))


def reschedule(w, hours=1, **kw):
    r = Visit.objects.get(code=w.visit).current_revision
    return plan(
        w,
        w.visit,
        on_site_start=r.on_site_start + timedelta(hours=hours),
        on_site_end=r.on_site_end + timedelta(hours=hours),
        **kw,
    )


def battery(w, soc=85, result="pass", **kw):
    return w.rx.record_evidence(
        w.field, visit_code=w.visit, kind="battery_check", result=result, details={"soc_pct": soc}, **kw
    )


def qualification(code="P-2", kind="robot_handler"):
    return Qualification.objects.get(person__code=code, kind=kind, revoked_at__isnull=True)


def consent():
    return ConsentRecord.objects.get(site__code="S-1", participant_ref="P1")


# ================================================================== happy path
def test_authorize_then_depart_happy_path(w):
    v_before = Visit.objects.get(code=w.visit)
    r = authorize(w)
    assert re.fullmatch(r"DA-\d{4}", r["code"]) and r["entity_id"] == r["code"]
    assert r["mode"] == "demo" and "SYNTHETIC DEMO" in r["note"] and "not real-world clearance" in r["note"]
    a = auth_row(r["code"])
    assert a.revision_id == v_before.current_revision_id and a.policy.version == w.policy_version
    assert a.guard_snapshot == r["snapshot"] and a.guard_snapshot["visit_version"] == v_before.version
    ev_ids = set(ReadinessEvidence.objects.filter(code__in=w.evidence.values()).values_list("pk", flat=True))
    assert set(a.evidence_ids) == ev_ids and a.recovery_plan_id == w.recovery_plan_id
    assert a.issued_by == w.ops and status(r["code"]) == {"state": "usable", "reasons": []}

    d = depart(w, r["code"], actor=w.field)  # w.field is the visit's second person (P-2)
    assert d["authorization"] == r["code"] and d["replayed"] is False
    ev = DepartureEvent.objects.get(authorization=a)
    assert ev.recorded_by == w.field and ev.guard_snapshot == a.guard_snapshot
    assert ev.departed_at >= a.issued_at
    assert status(r["code"])["state"] == "used"
    assert AuditEvent.objects.filter(command="readiness.record_departure", outcome="committed").count() == 1


def test_expiry_is_db_time_bounded_by_policy_ttl(w):
    r = authorize(w)
    a = auth_row(r["code"])
    ttl = timedelta(minutes=90)  # REQUIREMENTS authorization_ttl_min; all evidence is fresh
    assert a.issued_at + ttl - timedelta(minutes=1) < a.expires_at <= a.issued_at + ttl


def test_authorization_never_implies_departure_and_departure_changes_no_status(w):
    robot = Robot.objects.get(code="R-01")
    visit = Visit.objects.get(code=w.visit)
    r = authorize(w)
    assert not DepartureEvent.objects.exists()
    assert not visit.departures.exists()
    v2, robot2 = Visit.objects.get(pk=visit.pk), Robot.objects.get(pk=robot.pk)
    assert (v2.status, v2.version) == (visit.status, visit.version) == ("confirmed", visit.version)
    assert (robot2.lifecycle, robot2.version) == (robot.lifecycle, robot.version)
    depart(w, r["code"])
    v3, robot3 = Visit.objects.get(pk=visit.pk), Robot.objects.get(pk=robot.pk)
    assert (v3.status, v3.version) == ("confirmed", visit.version)  # departure is its own record
    assert (robot3.lifecycle, robot3.version) == (robot.lifecycle, robot.version)
    assert DepartureEvent.objects.count() == 1


# ================================================================== permissions
@pytest.mark.parametrize("who", ["field", "eng", "lead", "safety", "founder"])
def test_only_ops_lead_may_authorize(w, who):
    before = AuditEvent.objects.filter(outcome="denied").count()
    with pytest.raises(PermissionDenied):
        authorize(w, actor=getattr(w, who))
    assert not DepartureAuthorization.objects.exists()
    assert AuditEvent.objects.filter(outcome="denied").count() == before + 1


def test_depart_by_non_crew_field_operator_is_refused(w):
    code = authorize(w)["code"]
    before = AuditEvent.objects.filter(outcome="denied", command="readiness.record_departure").count()
    with pytest.raises(PermissionDenied):
        depart(w, code, actor=w.field2)  # field_operator, P-3, not on this visit
    with pytest.raises(PermissionDenied):
        depart(w, code, actor=w.eng)  # no readiness.record_departure permission at all
    assert not DepartureEvent.objects.exists()
    assert (
        AuditEvent.objects.filter(outcome="denied", command="readiness.record_departure").count()
        == before + 2
    )
    assert status(code)["state"] == "usable"


def test_depart_by_another_ops_lead_is_allowed(w):
    other = make_user("ra_ops2", "ops_lead")
    code = authorize(w)["code"]
    depart(w, code, actor=other)
    assert DepartureEvent.objects.get().recorded_by == other


@pytest.mark.parametrize("who", ["field", "eng"])
def test_revoke_requires_permission(w, who):
    code = authorize(w)["code"]
    with pytest.raises(PermissionDenied):
        rc.revoke_authorization(getattr(w, who), authorization_code=code, reason="x")
    assert not AuthorizationRevocation.objects.exists()


@pytest.mark.parametrize("who", ["ops", "lead", "safety", "founder"])
def test_revoke_allowed_roles(w, who):
    code = authorize(w)["code"]
    rc.revoke_authorization(getattr(w, who), authorization_code=code, reason="weather")
    assert status(code)["state"] == "revoked"


# ================================================================== idempotency
def test_authorize_idempotent_replay(w):
    a = authorize(w, idempotency_key="auth-1")
    b = authorize(w, idempotency_key="auth-1")
    assert b["replayed"] is True and b["code"] == a["code"] and b["expires_at"] == a["expires_at"]
    assert DepartureAuthorization.objects.count() == 1
    c = authorize(w, idempotency_key="auth-2")  # a different key is a different decision
    assert c["code"] != a["code"] and DepartureAuthorization.objects.count() == 2


def test_depart_idempotent_replay_is_not_a_second_departure(w):
    code = authorize(w)["code"]
    a = depart(w, code, idempotency_key="dep-1")
    b = depart(w, code, idempotency_key="dep-1")
    assert b["replayed"] is True and b["entity_id"] == a["entity_id"]
    assert DepartureEvent.objects.count() == 1
    with pytest.raises(Conflict, match="already used"):
        depart(w, code, idempotency_key="dep-2")


def test_replay_after_permission_revoked_is_denied(w):
    admin = make_user("ra_admin", "admin")
    authorize(w, idempotency_key="auth-r")
    grant = RoleGrant.objects.get(user=w.ops, role="ops_lead", revoked_at__isnull=True)
    revoke_role(admin, grant_id=grant.pk, reason="left")
    with pytest.raises(PermissionDenied):
        authorize(w, idempotency_key="auth-r")
    assert DepartureAuthorization.objects.count() == 1


def test_idempotency_key_reuse_for_other_visit_conflicts(w):
    authorize(w, idempotency_key="auth-x")
    other = new_confirmed_visit(w, d=DAY + timedelta(days=1))
    with pytest.raises(Conflict, match="Idempotency key"):
        authorize(w, visit=other, idempotency_key="auth-x")


# ================================================================== refusals per blocker category
def _refused(w, *codes, visit=None):
    with pytest.raises(Conflict) as exc:
        authorize(w, visit=visit)
    for c in codes:
        assert c in str(exc.value), str(exc.value)
    assert not DepartureAuthorization.objects.exists()
    return exc.value


def test_refused_visit_not_confirmed(w):
    reschedule(w)  # a reschedule puts the visit back to planned (and leaves no evidence for the new revision)
    e = _refused(w, "VISIT_NOT_CONFIRMED", "EVIDENCE_MISSING")
    assert any(b["code"] == "VISIT_NOT_CONFIRMED" for b in e.blockers)


def test_refused_cancelled_visit(w):
    w.p.cancel_visit(w.ops, visit_code=w.visit, reason="household asked")
    _refused(w, "VISIT_NOT_CONFIRMED")


def test_refused_robot_hold(w):
    fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")
    _refused(w, "PLANNING_ROBOT_NOT_ELIGIBLE")


def test_refused_consent_withdrawn(w):
    w.p.withdraw_consent(w.field, consent_id=consent().pk, reason="withdrawn")
    _refused(w, "PLANNING_CONSENT")


def test_refused_crew_qualification_revoked(w):
    w.p.revoke_qualification(w.ops, qualification_id=qualification().pk, reason="expired training")
    _refused(w, "PLANNING_QUALIFICATION")


def test_refused_policy_withdrawn(w):
    w.rx.withdraw_readiness_policy(w.lead, version=w.policy_version, reason="superseded procedure")
    _refused(w, "POLICY_MISSING")


def test_refused_unapproved_policy_only(w):
    w.rx.withdraw_readiness_policy(w.lead, version=w.policy_version, reason="x")
    w.make_policy(approve=False)  # a draft never authorizes
    _refused(w, "POLICY_MISSING")


def test_refused_evidence_missing(w):
    w.rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="wrong robot read")
    _refused(w, "EVIDENCE_MISSING")


def test_refused_evidence_failed(w):
    battery(w, result="fail")
    _refused(w, "EVIDENCE_FAILED")


def test_refused_evidence_stale(w):
    battery(w, observed_at=db_now() - timedelta(minutes=121))  # battery max_age_min is 120
    w.rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="replaced")
    _refused(w, "EVIDENCE_STALE")


def test_refused_wrong_configuration_then_unknown_battery_minimum(w):
    fs.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-C", autonomy_version="a9", safety_config_hash="n" * 64
    )
    fs.grant_base_authorization(w.lead, robot_code="R-01")  # P-C is approved for home use
    _refused(w, "EVIDENCE_WRONG_CONFIGURATION", "RECOVERY_PLAN_MISSING")
    w.record_all_evidence(w.visit)  # fresh evidence on P-C, whose profile has no departure battery minimum
    _refused(w, "BATTERY_MIN_UNKNOWN")


def test_refused_battery_low(w):
    battery(w, soc=50)
    _refused(w, "BATTERY_LOW")


def test_refused_equipment_missing(w):
    items = dict(EQUIPMENT_ALL_PRESENT["items"], charger="missing")
    w.rx.record_evidence(
        w.field, visit_code=w.visit, kind="equipment_check", result="pass", details={"items": items}
    )
    _refused(w, "EQUIPMENT_MISSING")


def test_refused_recovery_plan_missing(w):
    v = new_confirmed_visit(w, d=DAY + timedelta(days=1))
    insp = record_inspection(w, v)
    w.rx.record_evidence(
        w.ops, visit_code=v, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    w.rx.record_evidence(
        w.field, visit_code=v, kind="equipment_check", result="pass", details=EQUIPMENT_ALL_PRESENT
    )
    w.rx.record_evidence(w.field, visit_code=v, kind="battery_check", result="pass", details={"soc_pct": 85})
    _refused(w, "RECOVERY_PLAN_MISSING", visit=v)


def test_refused_unknown_visit(w):
    with pytest.raises(Invalid):
        authorize(w, visit="V-NOPE")


# ================================================================== production fails closed
def test_production_refuses_with_synthetic_policy(w):
    set_setting("demo_mode", "false")
    with pytest.raises(Conflict):
        authorize(w)
    assert not DepartureAuthorization.objects.exists()
    codes = {b["code"] for b in evaluate_readiness(Visit.objects.get(code=w.visit)).blockers}
    assert "POLICY_NOT_APPROVED_PROCEDURE" in codes


def test_demo_authorization_cannot_depart_after_switch_to_production(w):
    code = authorize(w)["code"]
    set_setting("demo_mode", "false")
    with pytest.raises(Conflict, match="POLICY_NOT_APPROVED_PROCEDURE"):
        depart(w, code)
    assert not DepartureEvent.objects.exists()


# ================================================================== expiry, single use, revoke
def test_expired_authorization_cannot_depart(w):
    # Battery evidence observed just inside its 120 min window caps expires_at a few seconds from now.
    battery(w, observed_at=db_now() - timedelta(minutes=120) + timedelta(seconds=4))
    w.rx.void_evidence(w.ops, evidence_code=w.evidence["battery_check"], reason="replaced")
    r = authorize(w)
    a = auth_row(r["code"])
    assert a.expires_at - a.issued_at < timedelta(seconds=5)
    assert status(r["code"])["state"] == "usable"
    deadline = time.monotonic() + 8
    while db_now() < a.expires_at:  # wait on DATABASE time, a few seconds at most
        assert time.monotonic() < deadline
        time.sleep(0.25)
    st = status(r["code"])
    assert st["state"] == "expired" and any("Expired" in x for x in st["reasons"])
    with pytest.raises(Conflict, match="expired"):
        depart(w, r["code"])
    assert not DepartureEvent.objects.exists()


def test_single_use(w):
    code = authorize(w)["code"]
    depart(w, code)
    with pytest.raises(Conflict, match="already used"):
        depart(w, code, actor=w.field)
    assert DepartureEvent.objects.count() == 1


def test_revoke_blocks_departure(w):
    code = authorize(w)["code"]
    with pytest.raises(Invalid):
        rc.revoke_authorization(w.ops, authorization_code=code, reason="  ")
    r = rc.revoke_authorization(w.safety, authorization_code=code, reason="weather warning")
    assert r["revoked"] is True
    st = status(code)
    assert st["state"] == "revoked" and "Revoked: weather warning" in st["reasons"]
    with pytest.raises(Conflict, match="revoked"):
        depart(w, code)
    with pytest.raises(Conflict, match="already revoked"):
        rc.revoke_authorization(w.ops, authorization_code=code, reason="again")
    assert not DepartureEvent.objects.exists() and AuthorizationRevocation.objects.count() == 1


def test_used_authorization_cannot_be_revoked(w):
    code = authorize(w)["code"]
    depart(w, code)
    with pytest.raises(Conflict, match="already used"):
        rc.revoke_authorization(w.ops, authorization_code=code, reason="late")
    assert status(code)["state"] == "used"


def test_new_authorization_after_revoke(w):
    first = authorize(w)["code"]
    rc.revoke_authorization(w.ops, authorization_code=first, reason="re-issue")
    second = authorize(w)["code"]
    assert second != first and status(second)["state"] == "usable"
    depart(w, second)
    assert DepartureEvent.objects.get().authorization.code == second


# ================================================================== invalidation causes
def _hold(w):
    fs.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")


def _manifest(w):
    fs.record_manifest(
        w.eng, robot_code="R-01", pattern_code="P-A", autonomy_version="a2", safety_config_hash="m" * 64
    )


def _consent(w):
    w.p.withdraw_consent(w.field, consent_id=consent().pk, reason="withdrawn")


def _assessment(w):
    w.p.set_expected_adults(w.ops, site_code="S-1", expected_adults=3)


def _qual(w):
    w.p.revoke_qualification(w.ops, qualification_id=qualification().pk, reason="x")


def _recovery_person_qual(w):
    w.p.grant_qualification(
        w.ops,
        person_code="P-4",
        kind="field_operator",
        valid_from=db_now(),
        valid_to=db_now() + timedelta(days=1),
    )


def _reschedule(w):
    reschedule(w)


def _evidence(w):
    battery(w, soc=90)


def _withdraw(w):
    w.rx.withdraw_readiness_policy(w.lead, version=w.policy_version, reason="x")


def _supersede(w):
    w.make_policy()


@pytest.mark.parametrize(
    "change,guard,extra",
    [
        (_hold, "robot/hold or configuration", None),
        (_manifest, "robot/hold or configuration", None),
        (_consent, "site/consent/assessment", None),
        (_assessment, "site/consent/assessment", None),
        (_qual, "crew qualification", None),
        (_recovery_person_qual, "crew qualification", None),
        (_reschedule, "visit reschedule/evidence", None),
        (_evidence, "visit reschedule/evidence", None),
        (_withdraw, "policy approval/withdrawal", "was withdrawn"),
        (_supersede, "policy approval/withdrawal", "superseded by"),
    ],
)
def test_status_invalidated_and_departure_refused(w, change, guard, extra):
    code = authorize(w)["code"]
    assert status(code)["state"] == "usable"
    change(w)
    st = status(code)
    assert st["state"] == "invalidated", st
    assert any(r.startswith(guard) for r in st["reasons"]), st
    if extra:
        assert any(extra in r for r in st["reasons"]), st
    with pytest.raises(Conflict) as exc:
        depart(w, code)
    assert "invalidated" in str(exc.value) or "Departure refused" in str(exc.value)
    assert not DepartureEvent.objects.exists()


def test_status_precedence_used_over_later_changes(w):
    code = authorize(w)["code"]
    depart(w, code)
    _hold(w)
    st = status(code)
    assert st["state"] == "used" and any(r.startswith("robot/hold") for r in st["reasons"])
