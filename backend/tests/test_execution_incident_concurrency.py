"""Deterministic interleavings for Slice 4 incident and session commands on real Postgres (Engineer C).
docs/04_field_execution_contract.md section 7.2.

The FIRST command pauses at a named checkpoint (right after its locks, or before commit); the SECOND is proven
blocked, then the first is released and both outcomes are checked. Every race runs in BOTH orders:
report_incident vs session start and vs resume (robot U vs robot S), duplicate idempotent reports, consent
withdrawal vs session start (site U vs site S), and the demo -> production switch vs decide_severity and vs
incident-policy approval (settings advisory X vs S, observed waiting in pg_stat_activity). No background worker;
enforcement is purely transactional. SYNTHETIC data only.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from execution import incidents as inc
from execution import policy as pol
from execution import sessions as ses
from execution.models import Incident, IncidentPolicy, SessionEvent, SeverityDecision
from fleet.models import Hold
from planning.models import ConsentRecord
from platform_core import testhooks
from platform_core.commands import Conflict
from tests import execution_world as ew
from tests.test_fleet_concurrency import Interleaving
from tests.test_readiness_settings_race import (
    SWITCHED,
    AdvisoryInterleaving,
    is_production,
    switch_to_production,
)

pytestmark = pytest.mark.django_db(transaction=True)

RULES = {
    "severity_scale": ["low", "medium", "high"],
    "escalation_required_from": "high",
    "downgrade_roles": ["safety_reviewer"],
}
OCCURRED = timezone.now() - timedelta(minutes=5)


@pytest.fixture
def w():
    world = ew.build_execution(prefix="xc_")
    yield world
    testhooks.clear()


def report(w, key=None, robot=None):
    return inc.report_incident(
        w.field,
        robot_code=robot or w.robot.code,
        event_type="fall",
        description="SYNTHETIC: robot sat down",
        occurred_at=OCCURRED,
        knowledge_source="witnessed",
        idempotency_key=key,
    )


def session(w, kind, **kw):
    return ses.record_session_event(w.field, run_code=w.run.code, kind=kind, **kw)


def go(w, app_role_conn, first=False, **kw) -> int:
    with app_role_conn.cursor() as cur:
        if first:
            ew.to_site(cur, w)
        return ew.go_check(cur, w, **kw)


def paused_with_new_go(w, app_role_conn) -> int:
    session(w, "start", site_check_id=go(w, app_role_conn, first=True))
    session(w, "pause", note="SYN break")
    return go(w, app_role_conn)


def make_ipolicy(w, approve_safety=True) -> int:
    v = int(pol.create_incident_policy(w.lead, rules=dict(RULES), source="synthetic_demo")["entity_id"])
    pol.approve_incident_policy(w.lead2, version=v, slot="engineering")
    if approve_safety:
        pol.approve_incident_policy(w.safety, version=v, slot="safety")
    return v


# ------------------------------------------------------------------ report_incident vs session start
def test_report_first_then_start_is_refused(w, app_role_conn):
    sc = go(w, app_role_conn, first=True)
    blocked, first, second = Interleaving("locked:execution.report_incident").run(
        lambda: report(w), lambda: session(w, "start", site_check_id=sc)
    )
    assert blocked, "start (robot S) must wait for the incident report (robot U)"
    assert isinstance(first, dict) and first["session_paused"] is False
    assert isinstance(second, Conflict) and "open interruption" in str(second), second
    assert not SessionEvent.objects.filter(run=w.run).exists()


def test_start_first_then_report_pauses_the_session(w, app_role_conn):
    sc = go(w, app_role_conn, first=True)
    blocked, first, second = Interleaving("locked:execution.record_session_event").run(
        lambda: session(w, "start", site_check_id=sc), lambda: report(w)
    )
    assert blocked, "the report (robot U) must wait for start (robot S)"
    assert first["state"] == "active"
    assert isinstance(second, dict) and second["session_paused"] is True
    assert inc.session_state(w.run.pk) == "paused"
    assert list(SessionEvent.objects.filter(run=w.run).order_by("seq").values_list("kind", flat=True)) == [
        "start",
        "pause",
    ]


# ------------------------------------------------------------------ report_incident vs resume
def test_report_first_then_resume_is_refused(w, app_role_conn):
    sc = paused_with_new_go(w, app_role_conn)
    blocked, first, second = Interleaving("locked:execution.report_incident").run(
        lambda: report(w), lambda: session(w, "resume", site_check_id=sc)
    )
    assert blocked
    assert isinstance(first, dict) and first["session_paused"] is False  # already paused
    assert isinstance(second, Conflict) and "Cannot resume" in str(second), second
    assert inc.session_state(w.run.pk) == "paused"


def test_resume_first_then_report_pauses_again(w, app_role_conn):
    sc = paused_with_new_go(w, app_role_conn)
    blocked, first, second = Interleaving("locked:execution.record_session_event").run(
        lambda: session(w, "resume", site_check_id=sc), lambda: report(w)
    )
    assert blocked
    assert first["state"] == "active"
    assert isinstance(second, dict) and second["session_paused"] is True
    assert inc.session_state(w.run.pk) == "paused"


# ------------------------------------------------------------------ duplicate reports
def test_duplicate_idempotent_reports_create_one_incident_and_one_hold(w):
    blocked, first, second = Interleaving("before_commit:execution.report_incident").run(
        lambda: report(w, key="dup"), lambda: report(w, key="dup")
    )
    assert blocked, "the duplicate must wait on the idempotency claim"
    assert first["replayed"] is False and second["replayed"] is True and first["code"] == second["code"]
    assert Incident.objects.count() == 1 and Hold.objects.filter(source_type="incident").count() == 1
    testhooks.clear()
    # without a key two reports on one robot are serialized on the robot and both hold it
    blocked, first, second = Interleaving("locked:execution.report_incident").run(
        lambda: report(w), lambda: report(w)
    )
    assert blocked and isinstance(first, dict) and isinstance(second, dict), (first, second)
    assert Incident.objects.count() == 3 and Hold.objects.filter(source_type="incident").count() == 3


# ------------------------------------------------------------------ consent withdrawal vs session start
def _withdraw(w):
    c = ConsentRecord.objects.filter(site__code="S-1", withdrawn_at__isnull=True).order_by("pk").first()
    return w.p.withdraw_consent(w.field, consent_id=c.pk, reason="withdrawn")


def test_consent_withdrawal_first_then_start_is_refused(w, app_role_conn):
    sc = go(w, app_role_conn, first=True)  # 2 adults present, 2 valid consents
    blocked, first, second = Interleaving("locked:planning.withdraw_consent").run(
        lambda: _withdraw(w), lambda: session(w, "start", site_check_id=sc)
    )
    assert blocked, "start (site S) must wait for the withdrawal (site U)"
    assert isinstance(first, dict)
    assert isinstance(second, Conflict) and "valid consent(s)" in str(second), second
    assert not SessionEvent.objects.filter(run=w.run).exists()


def test_start_first_then_consent_withdrawal_applies_after(w, app_role_conn):
    sc = go(w, app_role_conn, first=True)
    blocked, first, second = Interleaving("locked:execution.record_session_event").run(
        lambda: session(w, "start", site_check_id=sc), lambda: _withdraw(w)
    )
    assert blocked, "the withdrawal (site U) must wait for start (site S)"
    assert first["state"] == "active" and isinstance(second, dict)
    assert ConsentRecord.objects.filter(site__code="S-1", withdrawn_at__isnull=False).count() == 1


# ------------------------------------------------------------------ demo -> production vs decide_severity
def test_switch_first_then_decide_severity_is_refused(w):
    make_ipolicy(w)
    code = report(w)["code"]
    waited, returned, first, second = AdvisoryInterleaving(SWITCHED).run(
        switch_to_production,
        lambda: inc.decide_severity(w.lead, incident_code=code, level="high", reason="x"),
    )
    assert waited and not returned, "decide_severity must wait on the settings lock"
    assert first == "switched"
    assert isinstance(second, Conflict) and "cannot classify" in str(second), second
    assert not SeverityDecision.objects.exists()


def test_decide_severity_first_then_switch_applies_after(w):
    make_ipolicy(w)
    code = report(w)["code"]
    waited, returned, first, second = AdvisoryInterleaving("locked:execution.decide_severity").run(
        lambda: inc.decide_severity(w.lead, incident_code=code, level="high", reason="x"),
        switch_to_production,
    )
    assert waited and not returned, "the switch must wait for the in-flight demo decision"
    assert isinstance(first, dict) and first["severity"] == "high" and second == "switched"
    assert SeverityDecision.objects.count() == 1 and is_production()
    with pytest.raises(Conflict, match="cannot classify"):
        inc.decide_severity(w.lead, incident_code=code, level="medium", reason="x")


# ------------------------------------------------------------------ demo -> production vs policy approval
def test_switch_first_then_policy_approval_is_refused(w):
    v = make_ipolicy(w, approve_safety=False)
    waited, returned, first, second = AdvisoryInterleaving(SWITCHED).run(
        switch_to_production,
        lambda: pol.approve_incident_policy(w.safety, version=v, slot="safety"),
    )
    assert waited and not returned
    assert first == "switched"
    assert isinstance(second, Conflict) and "usable only in demo mode" in str(second), second
    assert pol.current_incident_policy() is None


def test_policy_approval_first_then_switch_applies_after(w):
    v = make_ipolicy(w, approve_safety=False)
    waited, returned, first, second = AdvisoryInterleaving("locked:execution.approve_incident_policy").run(
        lambda: pol.approve_incident_policy(w.safety, version=v, slot="safety"),
        switch_to_production,
    )
    assert waited and not returned
    assert isinstance(first, dict) and first["active"] is True and second == "switched"
    assert pol.current_incident_policy() == IncidentPolicy.objects.get(version=v)
    code = report(w)["code"]  # the hold never depends on the mode or the policy
    with pytest.raises(Conflict, match="cannot classify"):  # but the synthetic policy no longer classifies
        inc.decide_severity(w.lead, incident_code=code, level="high", reason="x")
