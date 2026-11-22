"""Field execution and incident operator UI (Slice 4, Engineer D): pages, permissions, every form, wording and the
full synthetic walkthrough driven through POSTs. SYNTHETIC data only.

Built on tests/execution_world.py. Assertions rely on contract behaviour (labels, command outcomes), never on
the commands' internals. The only steps not taken through these pages are the ones the contract puts elsewhere:
releasing an incident hold (fleet hold-release workflow) and the post-visit inspection (fleet command).
"""

import re
import secrets
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone

from execution import logistics as lg
from execution.models import (
    CustodyVerification,
    EscalationAck,
    FaultObservation,
    Incident,
    IncidentEscalation,
    IncidentPolicy,
    IncidentPolicyApproval,
    IncidentPolicyWithdrawal,
    IncidentStatusChange,
    Interruption,
    RecoveryDisposition,
    ReturnReconciliation,
    SessionEvent,
    SeverityDecision,
    SiteCheck,
    TripClosure,
    TripEvent,
)
from fleet.models import Hold, Inspection
from platform_core import testhooks
from platform_core.models import FormToken
from tests import execution_world as ew
from tests.conftest import make_user

pytestmark = pytest.mark.django_db(transaction=True)

BANNER = "Demo workspace"  # the one persistent header badge (01A); no per-page banners
NOT_BACK = "Robot is NOT back: trip cannot close"
READ_ROLES = ["ops", "field", "eng", "lead", "safety", "founder"]  # world users with execution.read
RULES_JSON = (
    '{"severity_scale": ["low", "medium", "high"], "escalation_required_from": "high", '
    '"downgrade_roles": ["safety_reviewer"]}'
)


@pytest.fixture
def w(transactional_db):
    world = ew.build_execution(prefix="xui_")
    yield world
    testhooks.clear()


# ------------------------------------------------------------------ helpers
def _client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _html(resp: Any) -> str:
    return resp.content.decode()


def _assert_safe(html: str) -> None:
    low = html.lower()
    assert "cleared" not in low
    assert "safe to" not in low
    assert "green" not in low
    assert not re.search(r'class="badge[^"]*\bready\b', low)


def _get(user: Any, url: str) -> str:
    resp = _client(user).get(url)
    assert resp.status_code == 200, (url, resp.status_code)
    html = _html(resp)
    _assert_safe(html)
    return html


def _issue(user: Any) -> str:
    """A shared one-time form token for this user, as the page would have rendered it."""
    token = secrets.token_urlsafe(24)
    FormToken.objects.create(token=token, user=user)
    return token


def _post(user: Any, url: str, data: dict[str, Any] | None = None) -> str:
    resp = _client(user).post(url, {**(data or {}), "_form_token": _issue(user)}, follow=True)
    assert resp.status_code == 200
    html = _html(resp)
    _assert_safe(html)
    return html


def _run_url(w: Any, suffix: str = "") -> str:
    return f"/execution/runs/{w.run.code}/{suffix}"


def _occurred(minutes: int = 5) -> str:
    return (
        (timezone.now() - timedelta(minutes=minutes))
        .astimezone(ZoneInfo(settings.TIME_ZONE))
        .strftime("%Y-%m-%dT%H:%M")
    )


def _report(w: Any, user: Any = None, *, from_run: bool = True, **over: Any) -> str:
    data = {
        "robot": w.robot.code,
        "event_type": "fall",
        "knowledge_source": "witnessed",
        "description": "SYNTHETIC: robot sat down unexpectedly",
        "occurred_at": _occurred(),
    }
    if from_run:
        data["run"] = w.run.code
    data.update(over)
    return _post(user or w.field, "/execution/incidents/report/", data)


def _make_policy(w: Any) -> int:
    html = _post(w.lead, "/execution/policies/create/", {"source": "synthetic_demo", "rules": RULES_JSON})
    assert "created as a DRAFT" in html, html
    version = IncidentPolicy.objects.order_by("-version").first().version
    assert "approval recorded" in _post(
        w.lead2, f"/execution/policies/{version}/approve/", {"slot": "engineering"}
    )
    assert "approval recorded" in _post(
        w.safety, f"/execution/policies/{version}/approve/", {"slot": "safety"}
    )
    return version


def _go_check(w: Any, user: Any = None) -> str:
    return _post(
        user or w.field,
        _run_url(w, "site-check/"),
        {"template": "SC1", "cond_c1": "ok", "cond_c2": "ok", "adults_present": "2", "verifier": "P-1"},
    )


def _latest_check(w: Any) -> SiteCheck:
    return SiteCheck.objects.filter(run=w.run).order_by("-id").first()


def _to_site_via_ui(w: Any) -> None:
    assert "Custody #1 verified by P-2" in _post(
        w.field, _run_url(w, "verify/"), {"custody_seq": "1", "verifier": "P-2"}
    )
    assert "Trip event recorded: Vehicle arrived at site" in _post(
        w.field, _run_url(w, "trip-event/"), {"kind": "arrived_site"}
    )
    html = _post(
        w.field, _run_url(w, "custody/"), {"to_kind": "site", "custodian": "P-1", "received_by": "P-2"}
    )
    assert "Custody move recorded: vehicle -&gt; site (handoff)" in html, html


# ------------------------------------------------------------------ pages and permissions
def test_every_page_renders_for_read_roles_and_403s_without_execution_read(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        iid = ew.raw_incident(app_role_conn, w)
        cur.execute("SELECT code FROM execution_incident WHERE id = %s", [iid])
        code = cur.fetchone()[0]
    pages = [
        "/execution/",
        _run_url(w),
        "/execution/incidents/",
        f"/execution/incidents/{code}/",
        "/execution/policies/",
        f"/execution/visits/{w.visit}/dossier/",
    ]
    for role in READ_ROLES:
        user = getattr(w, role)
        for url in pages:
            html = _get(user, url)
            assert BANNER in html, (role, url)
    for user in (make_user("xui_plain"), make_user("xui_admin", "admin")):
        for url in pages:
            assert _client(user).get(url).status_code == 403, url
    anon = Client().get("/execution/")
    assert anon.status_code == 302 and "/login" in anon["Location"]
    assert _client(w.ops).get("/execution/runs/EX-9999/").status_code == 404
    assert _client(w.ops).get("/execution/incidents/IC-9999/").status_code == 404


def test_post_endpoints_refuse_get_and_hide_forms_without_permission(w):
    assert _client(w.ops).get(_run_url(w, "close/")).status_code == 405
    assert _client(w.ops).get("/execution/incidents/report/").status_code == 405
    eng_page = _get(w.eng, _run_url(w))
    assert "Record custody move" not in eng_page and "Close trip" not in eng_page
    assert "Report incident" in eng_page  # engineers may report
    field_page = _get(w.field, _run_url(w))
    assert (
        "Record custody move" in field_page
        and 'action="/execution/runs/' + w.run.code + '/close/"' not in field_page
    )


def test_list_shows_run_row(w):
    html = _get(w.ops, "/execution/")
    assert w.run.code in html and w.visit in html and w.robot.code in html
    assert "Vehicle - administrative assignment, not verified" in html
    assert "Departed hub (in transit to site)" in html and "Not started" in html and "Open" in html


# ------------------------------------------------------------------ custody wording
def test_administrative_assignment_is_never_shown_as_verified(w):
    html = _get(w.ops, _run_url(w))
    assert "Administrative assignment - not verified" in html
    assert "Vehicle - administrative assignment, not verified" in html
    assert "Record crew verification of #1" in html
    assert "verified by crew" not in html

    html = _post(w.field, _run_url(w, "verify/"), {"custody_seq": "1", "verifier": "P-3"})
    assert "Refused (conflict)" in html and "not field crew" in html
    assert not CustodyVerification.objects.exists()

    html = _post(w.field, _run_url(w, "verify/"), {"custody_seq": "1", "verifier": "P-2"})
    assert "Custody #1 verified by P-2" in html
    assert "Administrative assignment, verified by crew P-2" in html
    assert "Administrative assignment - not verified" not in html
    assert "Record crew verification" not in html
    assert "Vehicle (handoff recorded)" not in html  # the assignment stays administrative, now verified


def test_logistics_forms_refusals_and_permission_messages(w):
    html = _post(w.eng, _run_url(w, "trip-event/"), {"kind": "arrived_site"})
    assert "Refused (permission denied)" in html
    html = _post(w.field, _run_url(w, "trip-event/"), {"kind": "arrived_hub"})
    # arrived_hub first is the EM-approved aborted outbound, refused while the departure assignment is unverified
    assert "Refused (conflict)" in html and "needs the departure assignment verified" in html
    html = _post(
        w.field, _run_url(w, "custody/"), {"to_kind": "site", "custodian": "P-1", "received_by": "P-2"}
    )
    assert "Refused (conflict)" in html and "not verified" in html
    html = _post(w.field, _run_url(w, "verify/"), {"custody_seq": "x", "verifier": "P-2"})
    assert "Refused (invalid input)" in html
    html = _post(w.field, _run_url(w, "site-check/"), {"template": "NOPE", "adults_present": "2"})
    assert "Refused (invalid input)" in html and "No site check template" in html
    # A site check before arrival is recorded as No-Go with its reasons.
    html = _go_check(w)
    assert "Site check recorded: No-Go" in html and "the trip has not arrived at the site" in html
    assert SiteCheck.objects.get(run=w.run).result == "no_go"
    assert "the trip has not arrived at the site" in _get(w.ops, _run_url(w))
    html = _post(w.ops, _run_url(w, "close/"))
    assert "Refused (conflict)" in html
    html = _post(w.ops, _run_url(w, "reconcile/"), {"discrepancies": "SYNTHETIC: charger missing"})
    assert "Return reconciliation recorded: UNRESOLVED" in html
    rec = ReturnReconciliation.objects.get(run=w.run)
    assert rec.outcome == "unresolved"
    page = _get(w.ops, _run_url(w))
    assert "SYNTHETIC: charger missing" in page and "Unresolved" in page


# ------------------------------------------------------------------ sessions, interruptions, dispositions
def test_session_and_interruption_forms(w):
    _to_site_via_ui(w)
    html = _post(w.field, _run_url(w, "session/"), {"kind": "start", "site_check_id": ""})
    assert "Refused" in html
    assert "Site check recorded: Go" in _go_check(w)
    sc = _latest_check(w)
    html = _post(w.field, _run_url(w, "session/"), {"kind": "start", "site_check_id": str(sc.pk)})
    assert "Session event recorded: start" in html and ">Active<" in html
    html = _post(w.field, _run_url(w, "session/"), {"kind": "observation", "note": "SYNTHETIC note"})
    assert "Session event recorded: observation" in html
    html = _post(w.field, _run_url(w, "interruptions/open/"), {"reason": "SYNTHETIC: participant asked"})
    it = Interruption.objects.get(run=w.run)
    assert f"Interruption {it.code} opened" in html and ">Paused<" in html
    html = _post(w.field, _run_url(w, "interruptions/resolve/"), {"interruption": it.code, "note": "ok"})
    assert "Refused (permission denied)" in html
    html = _post(w.ops, _run_url(w, "interruptions/resolve/"), {"interruption": "IN-9999", "note": "ok"})
    assert "Refused (invalid input)" in html
    html = _post(w.ops, _run_url(w, "interruptions/resolve/"), {"interruption": it.code, "note": "ok"})
    assert f"Interruption {it.code} resolved" in html
    html = _post(w.field, _run_url(w, "session/"), {"kind": "resume", "site_check_id": str(sc.pk)})
    assert "Refused (conflict)" in html  # resume needs a newer Go check
    _go_check(w)
    html = _post(
        w.field, _run_url(w, "session/"), {"kind": "resume", "site_check_id": str(_latest_check(w).pk)}
    )
    assert "Session event recorded: resume" in html
    assert "Session event recorded: stop" in _post(w.field, _run_url(w, "session/"), {"kind": "stop"})
    assert [e.kind for e in SessionEvent.objects.filter(run=w.run).order_by("seq")] == [
        "start",
        "observation",
        "pause",
        "resume",
        "stop",
    ]
    html = _post(
        w.ops, _run_url(w, "disposition/"), {"decision": "end_visit_return", "reason": "SYNTHETIC done"}
    )
    assert "Disposition recorded: end_visit_return" in html
    html = _post(w.field, _run_url(w, "disposition/"), {"decision": "end_visit_return", "reason": "x"})
    assert "Refused (permission denied)" in html
    assert RecoveryDisposition.objects.filter(run=w.run).count() == 1


def test_unaccounted_robot_shows_not_back_and_trip_cannot_close(w):
    _to_site_via_ui(w)
    html = _report(w, event_type="robot_unaccounted", knowledge_source="found_later")
    i = Incident.objects.get(run=w.run)
    assert f"Incident {i.code} reported" in html
    assert NOT_BACK in html and "Unaccounted for" in html
    html = _post(
        w.ops,
        _run_url(w, "disposition/"),
        {"decision": "robot_unaccounted", "reason": "SYNTHETIC", "incident_code": i.code},
    )
    assert "Disposition recorded: robot_unaccounted" in html
    _post(w.field, _run_url(w, "trip-event/"), {"kind": "departed_site"})
    html = _post(w.field, _run_url(w, "trip-event/"), {"kind": "arrived_hub"})
    assert "Trip event recorded: Vehicle arrived at hub" in html and "WARNING" in html
    assert NOT_BACK in html
    html = _post(w.ops, _run_url(w, "close/"))
    assert "Refused (conflict)" in html and not TripClosure.objects.exists()
    assert "Robot is NOT back" in _get(w.ops, "/execution/")
    # Recovery on the open run goes through the custody form (ops lead only) and never touches the hold.
    html = _post(
        w.field, _run_url(w, "custody/"), {"to_kind": "hub", "custodian": "P-1", "received_by": "P-3"}
    )
    assert "Refused (permission denied)" in html
    html = _post(
        w.ops,
        _run_url(w, "custody/"),
        {"to_kind": "hub", "custodian": "P-1", "received_by": "P-3", "note": "SYNTHETIC: found at hub"},
    )
    assert "Custody move recorded: unaccounted -&gt; hub (recovered)" in html
    assert "Recovery recorded by ops lead" in html
    assert Hold.objects.get(pk=i.hold_id).released_at is None


def test_left_on_site_shows_not_back(w):
    _to_site_via_ui(w)
    _report(w)
    i = Incident.objects.get(run=w.run)
    html = _post(
        w.ops,
        _run_url(w, "disposition/"),
        {"decision": "robot_left_on_site", "reason": "SYNTHETIC", "incident_code": i.code},
    )
    assert "Disposition recorded: robot_left_on_site" in html
    assert NOT_BACK in html


# ------------------------------------------------------------------ incidents
def test_report_incident_without_run_and_detail_page(w):
    html = _post(w.eng, "/execution/incidents/report/", {"robot": "R-02", "occurred_at": ""})
    assert "Refused (invalid input)" in html and "Occurred at is required" in html
    html = _report(w, w.eng, from_run=False, robot="R-02", knowledge_source="told_by_crew")
    i = Incident.objects.get(robot__code="R-02")
    assert f"Incident {i.code} reported" in html
    assert i.run is None and "no open run when reported" in html
    assert "Unclassified: no severity decision recorded." in html
    assert "Still active - robot held" in html and 'href="/fleet/R-02/"' in html
    assert "Told by crew" in html and "Occurred at" in html and "Reported at" in html
    assert "Record severity decision" not in html
    assert "No active incident policy" in _get(w.safety, f"/execution/incidents/{i.code}/")
    page = _get(w.ops, "/execution/incidents/")
    assert i.code in page and "no run" in page and "unclassified" in page and "Active hold" in page
    assert "Report incident" in page
    # Engineers may report but not triage: no triage forms for them.
    assert "Record fault observation" not in html
    html = _post(
        w.ops,
        "/execution/incidents/report/",
        {
            "robot": "R-02",
            "event_type": "nope",
            "knowledge_source": "witnessed",
            "description": "x",
            "occurred_at": _occurred(),
        },
    )
    assert "Refused (invalid input)" in html


def test_incident_triage_severity_status_escalation_and_faults(w):
    _report(w, from_run=False, robot="R-03")
    i = Incident.objects.get(robot__code="R-03")
    url = f"/execution/incidents/{i.code}/"
    html = _post(w.safety, url + "severity/", {"level": "high", "reason": "SYNTHETIC"})
    assert "Refused (conflict)" in html and not SeverityDecision.objects.exists()
    _make_policy(w)
    html = _post(w.safety, url + "severity/", {"level": "high", "reason": "SYNTHETIC: fall near people"})
    assert "Severity decision recorded: high" in html and "SYNTHETIC: fall near people" in html
    html = _post(
        w.ops, url + "fault/", {"component": "SYN-knee", "status": "suspected", "evidence_ref": "LOG-1"}
    )
    assert "Fault observation recorded" in html and "SYN-knee" in html
    assert FaultObservation.objects.filter(incident=i).count() == 1
    for step in ("triaged", "response_in_progress", "resolved"):
        html = _post(w.ops, url + "status/", {"to_status": step})
        assert f"Incident status changed to {step}" in html
    html = _post(w.lead, url + "status/", {"to_status": "closed"})
    assert "Refused (conflict)" in html  # high severity needs an escalation first
    html = _post(w.ops, url + "escalate/", {"to_role": "safety_reviewer", "requested_action": "SYN review"})
    assert "Incident escalated to safety_reviewer" in html and "Not acknowledged" in html
    esc = IncidentEscalation.objects.get(incident=i)
    assert "Acknowledge</button>" not in _get(w.ops, url)
    html = _post(w.ops, url + "ack/", {"escalation": str(esc.pk)})
    assert "Refused (permission denied)" in html
    html = _post(w.safety, url + "ack/", {"escalation": str(esc.pk)})
    assert "Escalation acknowledged" in html and EscalationAck.objects.filter(escalation=esc).exists()
    html = _post(w.lead, url + "status/", {"to_status": "closed"})
    assert "Incident status changed to closed" in html
    assert [s.to_status for s in IncidentStatusChange.objects.filter(incident=i).order_by("id")] == [
        "triaged",
        "response_in_progress",
        "resolved",
        "closed",
    ]
    assert "Still active - robot held" in html  # closing never releases the hold
    assert Hold.objects.get(pk=i.hold_id).released_at is None


def test_no_run_recovery_form(w, app_role_conn):
    if not callable(getattr(lg, "recover_robot", None)):
        pytest.skip("logistics.recover_robot is not merged yet (Engineer B)")
    _report(w, from_run=False, robot="R-02", event_type="robot_unaccounted")
    i = Incident.objects.get(robot__code="R-02")
    url = f"/execution/incidents/{i.code}/"
    assert "Record recovery to hub" in _get(w.ops, url)
    assert "Record recovery to hub" not in _get(w.field, url)
    html = _post(w.ops, url + "recover/", {"received_by": "P-2", "note": "SYNTHETIC: received at hub"})
    assert "Recovery recorded" in html and "Still active - robot held" in html


# ------------------------------------------------------------------ policies
def test_policy_create_approve_withdraw(w):
    html = _post(w.lead, "/execution/policies/create/", {"source": "synthetic_demo", "rules": "{nope"})
    assert "Refused (invalid input)" in html and "not valid JSON" in html and "{nope" in html
    html = _post(w.eng, "/execution/policies/create/", {"source": "synthetic_demo", "rules": RULES_JSON})
    assert "Refused (permission denied)" in html
    html = _post(w.lead, "/execution/policies/create/", {"source": "synthetic_demo", "rules": RULES_JSON})
    assert "Incident policy v1 created as a DRAFT" in html and "No active incident policy" in html
    html = _post(w.lead, "/execution/policies/1/approve/", {"slot": "engineering"})
    assert "Refused" in html  # no self-approval
    assert "approval recorded" in _post(w.lead2, "/execution/policies/1/approve/", {"slot": "engineering"})
    html = _post(w.safety, "/execution/policies/1/approve/", {"slot": "safety"})
    assert "Incident policy v1: safety approval recorded" in html
    assert "Active policy: <b>v1</b>" in html and "Demo approval" in html  # v2 01D ruling C wording
    assert "Not approved for production" in html
    assert IncidentPolicyApproval.objects.count() == 2
    html = _post(w.lead, "/execution/policies/1/withdraw/", {"reason": ""})
    assert "Refused" in html
    html = _post(w.lead, "/execution/policies/1/withdraw/", {"reason": "SYNTHETIC: superseded"})
    assert "Incident policy v1 withdrawn" in html and "No active incident policy" in html
    assert IncidentPolicyWithdrawal.objects.count() == 1


# ------------------------------------------------------------------ full walkthrough
def test_full_synthetic_walkthrough_through_posts(w):
    _to_site_via_ui(w)
    assert "Site check recorded: Go" in _go_check(w)
    html = _post(
        w.field, _run_url(w, "session/"), {"kind": "start", "site_check_id": str(_latest_check(w).pk)}
    )
    assert "Session event recorded: start" in html

    html = _report(w)
    i = Incident.objects.get(run=w.run)
    assert f"Incident {i.code} reported" in html and ">Paused<" in html
    it = Interruption.objects.get(incident=i)
    assert it.code in html and "Active hold" in html
    html = _post(w.ops, _run_url(w, "interruptions/resolve/"), {"interruption": it.code, "note": "SYN"})
    assert "Refused (conflict)" in html and "not released" in html

    # Hold release belongs to the fleet hold-release workflow, not to this UI.
    ew.release_hold(w, i.hold_id)
    detail = _get(w.ops, f"/execution/incidents/{i.code}/")
    assert "Released" in detail and "Still active" not in detail
    html = _post(
        w.ops, _run_url(w, "interruptions/resolve/"), {"interruption": it.code, "note": "SYN checked"}
    )
    assert f"Interruption {it.code} resolved" in html
    _go_check(w)
    html = _post(
        w.field, _run_url(w, "session/"), {"kind": "resume", "site_check_id": str(_latest_check(w).pk)}
    )
    assert "Session event recorded: resume" in html
    assert "Session event recorded: stop" in _post(w.field, _run_url(w, "session/"), {"kind": "stop"})

    html = _post(
        w.field, _run_url(w, "custody/"), {"to_kind": "vehicle", "custodian": "P-1", "received_by": "P-2"}
    )
    assert "Custody move recorded: site -&gt; vehicle (handoff)" in html
    assert "Trip event recorded: Vehicle departed site" in _post(
        w.field, _run_url(w, "trip-event/"), {"kind": "departed_site"}
    )
    html = _post(w.field, _run_url(w, "trip-event/"), {"kind": "arrived_hub"})
    assert "Trip event recorded: Vehicle arrived at hub" in html and NOT_BACK not in html
    html = _post(
        w.field, _run_url(w, "custody/"), {"to_kind": "hub", "custodian": "P-1", "received_by": "P-3"}
    )
    assert "Custody move recorded: vehicle -&gt; hub (handoff)" in html

    insp = Inspection.objects.get(pk=ew.post_visit_inspection(w)).code  # fleet command
    html = _post(w.ops, _run_url(w, "reconcile/"), {"inspection_code": insp})
    assert "Return reconciliation recorded: reconciled" in html, html
    html = _post(w.ops, _run_url(w, "close/"))
    assert f"Trip closed; run {w.run.code} is closed" in html and "Trip closed" in html
    assert TripClosure.objects.filter(run=w.run).exists()
    assert "Record custody move" not in html and "Record session event" not in html
    assert TripEvent.objects.filter(run=w.run).count() == 3
    assert "Closed" in _get(w.ops, "/execution/")

    dossier = _get(w.founder, f"/execution/visits/{w.visit}/dossier/")
    assert i.code in dossier and w.visit in dossier

    # A late report after closure still works from the incidents page and still holds the robot.
    html = _report(
        w, from_run=False, description="SYNTHETIC: found a scratch later", knowledge_source="found_later"
    )
    late = Incident.objects.exclude(pk=i.pk).get(robot=w.robot)
    assert f"Incident {late.code} reported" in html and late.run is None
