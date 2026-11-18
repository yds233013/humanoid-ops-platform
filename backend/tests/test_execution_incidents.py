"""Slice 4 incident response and session commands (Engineer C). docs/04_field_execution_contract.md sections 2-4.

Commands under test: execution.incidents (report_incident, add_fault_observation, decide_severity,
change_incident_status), execution.escalation, execution.sessions, execution.policy. The world is built through
the real commands (tests/execution_world.py); field steps owned by Engineer B (custody, trip events, site checks,
reconciliation) are recorded with the shared raw helpers as the runtime role. SYNTHETIC data only.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from execution import escalation as esc
from execution import incidents as inc
from execution import policy as pol
from execution import sessions as ses
from execution.custody import current_custody
from execution.models import (
    CustodyEvent,
    Incident,
    IncidentEscalation,
    IncidentPolicy,
    IncidentStatusChange,
    Interruption,
    SessionEvent,
    SeverityDecision,
)
from fleet import services as fs
from fleet.models import Hold, Robot
from planning.models import ConsentRecord
from platform_core import testhooks
from platform_core.commands import Conflict, Invalid, PermissionDenied
from platform_core.models import AuditEvent
from tests import execution_world as ew
from tests.conftest import make_user
from tests.fleet_world import set_setting

RULES = {
    "severity_scale": ["low", "medium", "high"],
    "escalation_required_from": "high",
    "downgrade_roles": ["safety_reviewer"],
}


@pytest.fixture
def w(transactional_db):
    world = ew.build_execution(prefix="xi_")
    yield world
    testhooks.clear()


# ------------------------------------------------------------------ helpers
def ago(minutes: int = 5):
    return timezone.now() - timedelta(minutes=minutes)


def report(w, actor=None, robot=None, event="fall", key=None, **kw):
    return inc.report_incident(
        actor or w.field,
        robot_code=robot or w.robot.code,
        event_type=event,
        description=kw.pop("description", "SYNTHETIC: robot sat down unexpectedly"),
        occurred_at=kw.pop("occurred_at", ago()),
        knowledge_source=kw.pop("knowledge_source", "witnessed"),
        idempotency_key=key,
    )


def make_ipolicy(w, source="synthetic_demo", approve=True, rules=None):
    version = int(pol.create_incident_policy(w.lead, rules=rules or dict(RULES), source=source)["entity_id"])
    if approve:
        pol.approve_incident_policy(w.lead2, version=version, slot="engineering")
        pol.approve_incident_policy(w.safety, version=version, slot="safety")
    return version


def session(w, kind, actor=None, **kw):
    return ses.record_session_event(actor or w.field, run_code=w.run.code, kind=kind, **kw)


def at_site_with_go(w, app_role_conn) -> int:
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        return ew.go_check(cur, w)


def new_go(w, app_role_conn, **kw) -> int:
    with app_role_conn.cursor() as cur:
        return ew.go_check(cur, w, **kw)


def started(w, app_role_conn) -> int:
    sc = at_site_with_go(w, app_role_conn)
    assert session(w, "start", site_check_id=sc)["state"] == "active"
    return sc


def snapshot(robot_id):
    return {
        "holds": Hold.objects.count(),
        "incidents": Incident.objects.count(),
        "custody": CustodyEvent.objects.count(),
        "interruptions": Interruption.objects.count(),
        "session": SessionEvent.objects.count(),
        "audit": AuditEvent.objects.filter(command="execution.report_incident", outcome="committed").count(),
        "robot_version": Robot.objects.get(pk=robot_id).version,
    }


# ================================================================== report_incident
def test_report_while_active_commits_hold_incident_custody_interruption_pause_and_audit(w, app_role_conn):
    started(w, app_role_conn)
    before = snapshot(w.robot.pk)
    out = report(w, event="robot_unaccounted", actor=w.eng)  # any operational role may report
    i = Incident.objects.get(code=out["code"])
    hold = Hold.objects.get(pk=i.hold_id)
    assert (hold.scope, hold.robot_id, hold.blocking, hold.source_type, hold.source_ref) == (
        "robot",
        w.robot.pk,
        True,
        "incident",
        i.code,
    )
    assert hold.released_at is None and out["hold"] == hold.code
    assert (i.run_id, i.visit_id, i.manifest_id) == (w.run.pk, w.run.visit_id, w.robot.current_manifest_id)
    assert i.reported_at >= i.occurred_at
    assert out["severity"] == "unclassified" and out["status"] == "reported"
    last = CustodyEvent.objects.filter(robot=w.robot).order_by("-seq").first()
    assert (last.from_kind, last.to_kind, last.incident_id, last.run_id) == (
        "site",
        "unaccounted",
        i.pk,
        w.run.pk,
    )
    assert current_custody(w.robot.pk).kind == "unaccounted"
    it = Interruption.objects.get(incident=i)
    assert (it.source, it.run_id, out["interruption"]) == ("incident", w.run.pk, it.code)
    pause = SessionEvent.objects.filter(run=w.run).order_by("-seq").first()
    assert (pause.kind, pause.interruption_id, out["session_paused"]) == ("pause", it.pk, True)
    assert inc.session_state(w.run.pk) == "paused"
    after = snapshot(w.robot.pk)
    assert after["audit"] == before["audit"] + 1 and after["robot_version"] > before["robot_version"]
    assert {
        k: after[k] - before[k] for k in ("holds", "incidents", "custody", "interruptions", "session")
    } == {
        "holds": 1,
        "incidents": 1,
        "custody": 1,
        "interruptions": 1,
        "session": 1,
    }
    # a second unaccounted report does not move custody again (already unaccounted, nothing rewritten)
    out2 = report(w, event="robot_unaccounted")
    assert out2["custody_event_seq"] is None and CustodyEvent.objects.count() == after["custody"]
    assert out2["session_paused"] is False  # already paused: a new interruption, no second pause
    assert Interruption.objects.filter(incident__code=out2["code"]).exists()


STEPS = [
    "_place_hold",
    "_insert_incident",
    "_mark_unaccounted",
    "_open_incident_interruption",
    "_pause_for_incident",
]


def test_injected_failure_at_each_step_rolls_back_everything(w, app_role_conn, monkeypatch):
    started(w, app_role_conn)
    before = snapshot(w.robot.pk)
    for step in [*STEPS, "audit"]:
        with monkeypatch.context() as m:
            if step == "audit":  # after the audit row and the idempotency response are written

                def boom_after_audit():
                    assert AuditEvent.objects.filter(command="execution.report_incident").exists()
                    raise RuntimeError("injected after audit")

                testhooks.register("before_commit:execution.report_incident", boom_after_audit)
            else:
                original = getattr(inc, step)

                def boom(*a, _orig=original, _step=step, **k):
                    _orig(*a, **k)  # the step's row IS written in the transaction, then the failure
                    raise RuntimeError(f"injected in {_step}")

                m.setattr(inc, step, boom)
            with pytest.raises(RuntimeError, match="injected"):
                report(w, event="robot_unaccounted", key=f"k-{step}")
            testhooks.clear()
        assert snapshot(w.robot.pk) == before, step
        assert inc.session_state(w.run.pk) == "active"
        assert current_custody(w.robot.pk).kind == "site"
    out = report(w, event="robot_unaccounted", key="k-audit")  # control: the same key now commits
    assert out["replayed"] is False and out["session_paused"] is True


def test_no_policy_still_holds_and_severity_stays_unclassified(w, monkeypatch):
    def never(*a, **k):
        raise AssertionError("report_incident must not read the incident policy")

    monkeypatch.setattr(pol, "current_incident_policy", never)
    monkeypatch.setattr(inc, "current_incident_policy", never)
    out = report(w)
    monkeypatch.undo()
    assert IncidentPolicy.objects.count() == 0
    assert Hold.objects.get(code=out["hold"]).released_at is None
    assert out["severity"] == "unclassified"
    with pytest.raises(Conflict, match="no active incident policy.*unclassified"):
        inc.decide_severity(w.lead, incident_code=out["code"], level="high", reason="x")
    assert inc.incident_dict(Incident.objects.get(code=out["code"]))["severity"] == "unclassified"
    # a draft policy is not active either
    make_ipolicy(w, approve=False)
    with pytest.raises(Conflict, match="no active incident policy"):
        inc.decide_severity(w.lead, incident_code=out["code"], level="high", reason="x")
    # production mode does not stop the protective hold either (report_incident never reads settings)
    set_setting("demo_mode", "false")
    out2 = report(w, robot="R-02")
    assert out2["run"] is None and Hold.objects.get(code=out2["hold"]).blocking


def test_late_reports_after_stop_after_closure_and_without_run_still_hold(w, app_role_conn):
    started(w, app_role_conn)
    session(w, "stop")
    late = report(w, event="damage", knowledge_source="found_later", occurred_at=ago(60))
    assert late["run"] == w.run.code and late["interruption"] and late["session_paused"] is False
    assert inc.session_state(w.run.pk) == "stopped"
    with app_role_conn.cursor() as cur:
        ew.to_hub(cur, w)
        ew.close_run(cur, w)  # a held robot's trip can still close
    assert Hold.objects.get(code=late["hold"]).released_at is None
    after_close = report(w, event="damage", knowledge_source="told_by_crew", occurred_at=ago(120))
    i = Incident.objects.get(code=after_close["code"])
    assert (i.run_id, i.visit_id, after_close["interruption"]) == (None, None, None)
    assert Hold.objects.get(pk=i.hold_id).released_at is None
    # a robot that never ran: hold, no run; unaccounted moves the baseline hub custody to unaccounted
    r2 = Robot.objects.get(code="R-02")
    lost = report(w, robot="R-02", event="robot_unaccounted")
    assert lost["run"] is None and Hold.objects.get(code=lost["hold"]).robot_id == r2.pk
    ev = CustodyEvent.objects.get(robot=r2)
    assert (ev.seq, ev.from_kind, ev.to_kind, ev.run_id) == (1, "hub", "unaccounted", None)


def test_report_is_idempotent_validated_and_permissioned(w):
    t = ago()
    first = report(w, key="same", occurred_at=t)
    again = report(w, key="same", occurred_at=t)
    assert again["replayed"] is True and again["code"] == first["code"]
    assert Incident.objects.count() == 1 and Hold.objects.filter(source_type="incident").count() == 1
    with pytest.raises(Conflict, match="Idempotency key reused"):
        report(w, key="same", event="contact", occurred_at=t)
    bad = [
        ({"event": "explosion"}, Invalid, "event_type"),
        ({"knowledge_source": "rumour"}, Invalid, "knowledge_source"),
        ({"description": "  "}, Invalid, "description"),
        ({"occurred_at": timezone.now().replace(tzinfo=None)}, Invalid, "timezone-aware"),
        ({"occurred_at": timezone.now() + timedelta(hours=1)}, Invalid, "future"),
        ({"robot": "R-99"}, Invalid, "No robot"),
    ]
    for kw, exc, msg in bad:
        with pytest.raises(exc, match=msg):
            report(w, **kw)
    with pytest.raises(PermissionDenied):
        report(w, actor=make_user("xi_nobody"))
    assert Incident.objects.count() == 1


# ================================================================== incident policy
def test_incident_policy_lifecycle_and_rules(w):
    with pytest.raises(PermissionDenied):
        pol.create_incident_policy(w.safety, rules=dict(RULES), source="synthetic_demo")
    with pytest.raises(Invalid, match="escalation_required_from"):
        pol.create_incident_policy(
            w.lead, rules={**RULES, "escalation_required_from": "x"}, source="synthetic_demo"
        )
    with pytest.raises(Invalid, match="unknown downgrade role"):
        pol.create_incident_policy(
            w.lead, rules={**RULES, "downgrade_roles": ["admin"]}, source="synthetic_demo"
        )
    v1 = make_ipolicy(w, approve=False)
    with pytest.raises(Conflict, match="author"):
        pol.approve_incident_policy(w.lead, version=v1, slot="engineering")
    with pytest.raises(PermissionDenied, match="requires role safety_reviewer"):
        pol.approve_incident_policy(w.lead2, version=v1, slot="safety")
    pol.approve_incident_policy(w.lead2, version=v1, slot="engineering")
    with pytest.raises(Conflict, match="already approved"):
        pol.approve_incident_policy(w.lead2, version=v1, slot="engineering")
    assert pol.current_incident_policy() is None and pol.active_incident_policy_id_in_db() is None
    out = pol.approve_incident_policy(w.safety, version=v1, slot="safety")
    assert out["status"] == "approved" and out["active"] is True
    p1 = IncidentPolicy.objects.get(version=v1)
    assert pol.current_incident_policy() == p1 and pol.active_incident_policy_id_in_db() == p1.pk
    v2 = make_ipolicy(w)
    p2 = IncidentPolicy.objects.get(version=v2)
    assert pol.current_incident_policy() == p2
    with pytest.raises(PermissionDenied):  # withdrawal: manage_incident_policy (engineering_lead) only
        pol.withdraw_incident_policy(w.safety, version=v2, reason="pulled")
    pol.withdraw_incident_policy(w.lead2, version=v2, reason="pulled")
    assert (
        pol.current_incident_policy() is None and pol.active_incident_policy_id_in_db() is None
    )  # no fallback
    with pytest.raises(Conflict, match="withdrawn"):
        pol.approve_incident_policy(w.lead2, version=v2, slot="engineering")
    # production: synthetic_demo policies can be neither created nor approved
    v3 = make_ipolicy(w, approve=False)
    set_setting("demo_mode", "false")
    with pytest.raises(Conflict, match="usable only in demo mode"):
        pol.create_incident_policy(w.lead, rules=dict(RULES), source="synthetic_demo")
    for user, slot in ((w.lead2, "engineering"), (w.safety, "safety")):
        with pytest.raises(Conflict, match="usable only in demo mode"):
            pol.approve_incident_policy(user, version=v3, slot=slot)
    v4 = make_ipolicy(w, source="approved_procedure")
    assert pol.current_incident_policy() == IncidentPolicy.objects.get(version=v4)


# ================================================================== severity
def test_severity_classification_downgrade_rules_and_mode(w):
    make_ipolicy(w)
    code = report(w, actor=w.safety)["code"]
    with pytest.raises(PermissionDenied):
        inc.decide_severity(w.ops, incident_code=code, level="high", reason="x")
    with pytest.raises(Invalid, match="not in incident policy v1's severity scale"):
        inc.decide_severity(w.lead, incident_code=code, level="extreme", reason="x")
    out = inc.decide_severity(w.lead, incident_code=code, level="high", reason="fell near a participant")
    assert out["previous_severity"] == "unclassified" and out["escalation_required"] is True
    inc.decide_severity(
        w.lead, incident_code=code, level="high", reason="same level again is not a downgrade"
    )
    with pytest.raises(Conflict, match="needs one of the roles"):
        inc.decide_severity(
            w.lead, incident_code=code, level="low", reason="engineering lead may not downgrade"
        )
    with pytest.raises(Conflict, match="reporter of an incident cannot downgrade"):
        inc.decide_severity(w.safety, incident_code=code, level="low", reason="self downgrade")
    safety2 = make_user("xi_safety2", "safety_reviewer")
    out = inc.decide_severity(safety2, incident_code=code, level="low", reason="reviewed footage")
    assert out["downgrade"] is True and out["previous_severity"] == "high"
    assert list(
        SeverityDecision.objects.filter(incident__code=code).order_by("pk").values_list("level", flat=True)
    ) == [
        "high",
        "high",
        "low",
    ]
    set_setting("demo_mode", "false")
    with pytest.raises(Conflict, match="synthetic_demo and cannot classify"):
        inc.decide_severity(w.lead, incident_code=code, level="medium", reason="x")
    make_ipolicy(w, source="approved_procedure")
    out = inc.decide_severity(w.lead, incident_code=code, level="medium", reason="production procedure")
    assert out["policy_version"] == 2


# ================================================================== status, escalation, faults
def test_status_escalation_and_closing_never_release_the_hold(w):
    make_ipolicy(w)
    out = report(w)
    code, hold_code = out["code"], out["hold"]
    inc.decide_severity(w.lead, incident_code=code, level="high", reason="x")
    with pytest.raises(PermissionDenied):
        inc.change_incident_status(w.field, incident_code=code, to_status="triaged")
    with pytest.raises(Conflict, match="one step at a time: reported cannot become resolved"):
        inc.change_incident_status(w.ops, incident_code=code, to_status="resolved")
    for s in ("triaged", "response_in_progress", "resolved"):
        assert inc.change_incident_status(w.ops, incident_code=code, to_status=s)["hold_released"] is False
    with pytest.raises(PermissionDenied):  # ops_lead triages but cannot close
        inc.change_incident_status(w.ops, incident_code=code, to_status="closed")
    with pytest.raises(Conflict, match="Severity high requires at least one escalation"):
        inc.change_incident_status(w.safety, incident_code=code, to_status="closed")
    f = inc.add_fault_observation(w.ops, incident_code=code, component="SYN-knee", status="suspected")
    assert f["status"] == "suspected"
    with pytest.raises(PermissionDenied):
        inc.add_fault_observation(w.field, incident_code=code, component="SYN-knee", status="confirmed")
    with pytest.raises(Conflict, match="does not hold the role safety_reviewer"):
        esc.escalate_incident(
            w.ops,
            incident_code=code,
            to_role="safety_reviewer",
            requested_action="review",
            to_person_code="P-2",
        )
    with pytest.raises(Invalid, match="to_role"):
        esc.escalate_incident(w.ops, incident_code=code, to_role="admin", requested_action="review")
    e = esc.escalate_incident(
        w.ops, incident_code=code, to_role="engineering_lead", requested_action="review", to_person_code="P-5"
    )
    with pytest.raises(Conflict, match="acknowledged before closing.*engineering_lead"):
        inc.change_incident_status(w.safety, incident_code=code, to_status="closed")
    with pytest.raises(PermissionDenied, match="holding the role engineering_lead"):
        esc.acknowledge_escalation(w.field, escalation_id=e["id"])
    with pytest.raises(PermissionDenied, match="Only P-5"):
        esc.acknowledge_escalation(w.lead, escalation_id=e["id"])
    assert esc.acknowledge_escalation(w.lead2, escalation_id=e["id"])["acknowledged"] is True
    with pytest.raises(Conflict, match="already acknowledged"):
        esc.acknowledge_escalation(w.lead2, escalation_id=e["id"])
    closed = inc.change_incident_status(w.safety, incident_code=code, to_status="closed")
    assert closed["status"] == "closed" and closed["hold_released"] is False
    with pytest.raises(Conflict, match="is closed"):
        esc.escalate_incident(w.ops, incident_code=code, to_role="founder", requested_action="late")
    with pytest.raises(Conflict, match="closed cannot become"):
        inc.change_incident_status(w.safety, incident_code=code, to_status="closed")
    assert Hold.objects.get(code=hold_code).released_at is None
    assert IncidentStatusChange.objects.filter(incident__code=code).count() == 4
    assert IncidentEscalation.objects.filter(incident__code=code).count() == 1


def test_low_severity_closes_without_escalation(w):
    make_ipolicy(w)
    code = report(w)["code"]
    inc.decide_severity(w.lead, incident_code=code, level="low", reason="x")
    for s in ("triaged", "response_in_progress", "resolved", "closed"):
        inc.change_incident_status(w.safety, incident_code=code, to_status=s)
    assert inc.incident_status(Incident.objects.get(code=code)) == "closed"


# ================================================================== session lifecycle and gates
def test_session_lifecycle(w, app_role_conn):
    with pytest.raises(Invalid, match="needs the site check"):
        session(w, "start")
    with pytest.raises(Conflict, match="Cannot pause: the session is not started"):
        session(w, "pause")
    with pytest.raises(Conflict, match="Cannot stop"):
        session(w, "stop")
    with app_role_conn.cursor() as cur:
        ew.to_site(cur, w)
        no_go = ew.raw_site_check(cur, w, conditions={"c1": "ok", "c2": "not_ok"}, result="no_go")
    with pytest.raises(Conflict, match="No-Go"):
        session(w, "start", site_check_id=no_go)
    old = new_go(w, app_role_conn)
    sc = new_go(w, app_role_conn)
    with pytest.raises(Conflict, match="Stale site check"):
        session(w, "start", site_check_id=old)
    with pytest.raises(PermissionDenied, match="field crew"):
        session(w, "start", actor=w.field2, site_check_id=sc)  # P-3: field operator, not crew of the run
    assert session(w, "start", site_check_id=sc)["state"] == "active"
    with pytest.raises(Conflict, match="Cannot start: the session is active"):
        session(w, "start", site_check_id=sc)
    with pytest.raises(Invalid, match="observation needs a note"):
        session(w, "observation")
    session(w, "observation", note="SYNTHETIC: participant asked a question")
    assert session(w, "pause", note="break")["state"] == "paused"
    with pytest.raises(Conflict, match="Stale site check: resuming"):
        session(w, "resume", site_check_id=sc)
    sc2 = new_go(w, app_role_conn)
    assert session(w, "resume", actor=w.ops, site_check_id=sc2)["state"] == "active"  # ops_lead may resume
    assert session(w, "stop")["state"] == "stopped"
    with pytest.raises(Conflict, match="stopped session never restarts"):
        session(w, "resume", site_check_id=new_go(w, app_role_conn))
    with pytest.raises(Conflict, match="stopped"):
        session(w, "start", site_check_id=sc2)
    assert session(w, "observation", note="after stop")["state"] == "stopped"


def test_start_gates_hold_interruption_consent_custody(w, app_role_conn):
    sc = at_site_with_go(w, app_role_conn)
    h = fs.place_hold(w.ops, scope="robot", target=w.robot.code, source_type="maintenance", reason="SYN")
    with pytest.raises(Conflict, match=f"unreleased blocking hold \\({h['entity_id']}\\)"):
        session(w, "start", site_check_id=sc)
    fs.approve_release(w.lead, hold_code=h["entity_id"], slot="engineering", role_used="engineering_lead")
    fs.release_hold(w.ops, hold_code=h["entity_id"])
    it = ses.open_interruption(w.field, run_code=w.run.code, reason="SYN: doorbell")
    assert it["session_paused"] is False  # nothing to pause yet
    with pytest.raises(Conflict, match=f"open interruption \\({it['code']}\\)"):
        session(w, "start", site_check_id=sc)
    ses.resolve_interruption(w.ops, code=it["code"], note="answered")
    sc = new_go(w, app_role_conn)
    c = ConsentRecord.objects.filter(site__code="S-1", withdrawn_at__isnull=True).first()
    w.p.withdraw_consent(w.field, consent_id=c.pk, reason="withdrawn")  # after the check: blocks
    with pytest.raises(Conflict, match="valid consent\\(s\\) cover this visit but the site check recorded 2"):
        session(w, "start", site_check_id=sc)
    sc = new_go(w, app_role_conn, adults=1)
    with app_role_conn.cursor() as cur:
        ew.raw_custody(cur, w, "site", "vehicle")  # the robot is put back in the vehicle
    with pytest.raises(Conflict, match="not in site custody"):
        session(w, "start", site_check_id=sc)
    assert not SessionEvent.objects.filter(run=w.run).exists()


# ================================================================== interruptions and dispositions
def test_interruptions_pause_and_incident_interruptions_wait_for_the_hold(w, app_role_conn):
    started(w, app_role_conn)
    it = ses.open_interruption(w.field, run_code=w.run.code, reason="SYN: participant unwell")
    assert it["session_paused"] is True and inc.session_state(w.run.pk) == "paused"
    with pytest.raises(PermissionDenied):
        ses.resolve_interruption(w.field, code=it["code"], note="x")
    with pytest.raises(Invalid, match="note"):
        ses.resolve_interruption(w.ops, code=it["code"], note=" ")
    ses.resolve_interruption(w.ops, code=it["code"], note="participant fine")
    with pytest.raises(Conflict, match="already resolved"):
        ses.resolve_interruption(w.ops, code=it["code"], note="again")
    session(w, "resume", site_check_id=new_go(w, app_role_conn))
    out = report(w)
    with pytest.raises(Conflict, match=f"hold {out['hold']} is not released"):
        ses.resolve_interruption(w.lead, code=out["interruption"], note="x")
    inc.change_incident_status(w.ops, incident_code=out["code"], to_status="triaged")
    with pytest.raises(Conflict, match="not released"):  # status never releases the hold
        ses.resolve_interruption(w.lead, code=out["interruption"], note="x")
    ew.release_hold(w, Hold.objects.get(code=out["hold"]).pk)
    assert (
        ses.resolve_interruption(w.lead, code=out["interruption"], note="checked")["open_interruptions"] == []
    )
    assert session(w, "resume", site_check_id=new_go(w, app_role_conn))["state"] == "active"


def test_dispositions(w, app_role_conn):
    started(w, app_role_conn)
    out = report(w)
    with pytest.raises(PermissionDenied):
        ses.record_disposition(w.field, run_code=w.run.code, decision="end_visit_return", reason="x")
    with pytest.raises(Conflict, match="continue_visit refused: the robot or its configuration pattern"):
        ses.record_disposition(w.ops, run_code=w.run.code, decision="continue_visit", reason="x")
    with pytest.raises(Invalid, match="needs the incident"):
        ses.record_disposition(w.ops, run_code=w.run.code, decision="robot_left_on_site", reason="x")
    with pytest.raises(Conflict, match="needs a robot_unaccounted incident"):
        ses.record_disposition(
            w.ops, run_code=w.run.code, decision="robot_unaccounted", reason="x", incident_code=out["code"]
        )
    other = report(w, robot="R-02")["code"]
    with pytest.raises(Conflict, match="not an incident of run"):
        ses.record_disposition(
            w.ops, run_code=w.run.code, decision="robot_left_on_site", reason="x", incident_code=other
        )
    ew.release_hold(w, Hold.objects.get(code=out["hold"]).pk)
    with pytest.raises(Conflict, match="continue_visit refused: the run has an open interruption"):
        ses.record_disposition(w.ops, run_code=w.run.code, decision="continue_visit", reason="x")
    ses.resolve_interruption(w.ops, code=out["interruption"], note="checked")
    d = ses.record_disposition(w.ops, run_code=w.run.code, decision="continue_visit", reason="all clear")
    assert d["decision"] == "continue_visit"
    left = ses.record_disposition(
        w.ops, run_code=w.run.code, decision="robot_left_on_site", reason="SYN", incident_code=out["code"]
    )
    assert left["incident"] == out["code"]
