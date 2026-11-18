"""Server-rendered field execution and incident UI (Slice 4, Engineer D). Contract: docs/04, sections 2-4.

Views only READ models and call the contract commands; every write goes through execution.logistics,
execution.site_checks, execution.reconciliation (Engineer B) or execution.incidents, execution.sessions,
execution.escalation, execution.policy (Engineer C). The commands enforce permissions, locks and invariants;
buttons are hidden for users who lack a permission, but the commands are the real gate. Derived state (session
state, incident status, severity, active policy) comes from the same read helpers the commands use. Only
logistics.recover_robot is looked up at request time, so the page works in a build that does not have it.

SYNTHETIC data only. The system records what authorized people report. No page says a robot is cleared, safe
or ready, and an administrative custody assignment is never shown as a verified handoff.
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import active_roles, has_permission
from accounts.roles import ALL_ROLES
from execution import escalation as esc
from execution import incidents as inc
from execution import logistics as lg
from execution import policy as pol
from execution import reconciliation as recon
from execution import sessions as ses
from execution import site_checks as sck
from execution.custody import Custody, current_custody, open_run_id
from execution.dossier import visit_dossier
from execution.models import (
    CUSTODY_KINDS,
    DISPOSITIONS,
    INCIDENT_EVENT_TYPES,
    INCIDENT_STATUSES,
    KNOWLEDGE_SOURCES,
    POLICY_SLOTS,
    SOURCES,
    CustodyEvent,
    ExecutionRun,
    FaultObservation,
    Incident,
    IncidentEscalation,
    IncidentPolicy,
    IncidentStatusChange,
    Interruption,
    RecoveryDisposition,
    ReturnReconciliation,
    SessionEvent,
    SeverityDecision,
    SiteCheck,
    SiteCheckTemplate,
    TripEvent,
)
from fleet.models import Robot
from fleet.services import demo_mode
from planning.models import Person, Visit
from platform_core.commands import CommandError, Invalid
from platform_core.models import AuditEvent

DEMO_BANNER = "SYNTHETIC DEMO - not real-world clearance"
ACTIONS = [
    "record_field_event",
    "report_incident",
    "triage_incident",
    "change_severity",
    "close_incident",
    "record_disposition",
    "reconcile",
    "close_trip",
    "manage_incident_policy",
    "approve_incident_policy",
]
ERROR_KINDS = {
    "conflict": "conflict",
    "invalid": "invalid input",
    "permission_denied": "permission denied",
    "not_authenticated": "not signed in",
}
TRIP_ORDER = ["arrived_site", "departed_site", "arrived_hub"]
TRIP_LABELS = {
    "arrived_site": "Vehicle arrived at site",
    "departed_site": "Vehicle departed site",
    "arrived_hub": "Vehicle arrived at hub",
}
SESSION_LABELS = {"not_started": "Not started", "active": "Active", "paused": "Paused", "stopped": "Stopped"}
STATUS_STEPS = ["reported"] + [s for s, _ in INCIDENT_STATUSES]
STRANDED = ("robot_left_on_site", "robot_unaccounted")
KIND_TEXT = dict(CUSTODY_KINDS)
RECOVERY_NOT_BACK = "Robot is NOT back: trip cannot close"
EXAMPLE_RULES = {
    "severity_scale": ["low", "medium", "high"],
    "escalation_required_from": "high",
    "downgrade_roles": ["safety_reviewer"],
}


# ------------------------------------------------------------------ helpers
def _deny(request: HttpRequest) -> HttpResponse | None:
    if not has_permission(request.user, "execution.read"):
        return HttpResponseForbidden("No execution access")
    return None


def _base(request: HttpRequest) -> dict[str, Any]:
    demo = demo_mode()
    roles = active_roles(request.user)
    return {
        "roles": sorted(roles),
        "demo_mode": demo,
        "demo_banner": DEMO_BANNER if demo else "",
        "can": {a: has_permission(request.user, f"execution.{a}") for a in ACTIONS},
        "is_ops_lead": "ops_lead" in roles,
    }


def _recover_robot() -> Callable[..., dict[str, Any]] | None:
    """logistics.recover_robot (Engineer B) when present in this build; None otherwise."""
    found = getattr(lg, "recover_robot", None)
    return found if callable(found) else None


def _refused(request: HttpRequest, exc: CommandError) -> None:
    kind = ERROR_KINDS.get(exc.code, exc.code.replace("_", " "))
    messages.error(request, f"Refused ({kind}): {exc}")


def _run(
    request: HttpRequest,
    command: Callable[..., dict[str, Any]],
    ok: str | Callable[[dict[str, Any]], str],
    **kw: Any,
) -> dict[str, Any] | None:
    try:
        out = command(request.user, **kw)
    except CommandError as exc:
        _refused(request, exc)
        return None
    messages.success(request, ok(out) if callable(ok) else ok)
    return out


def _local(dt: datetime | None, tz: str) -> datetime | None:
    return dt.astimezone(ZoneInfo(tz)) if dt is not None else None


def _int(raw: str, what: str) -> int:
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        raise Invalid(f"{what} must be a whole number") from None


def _audit_result(entity_type: str, entity_id: int) -> dict[str, Any] | None:
    ev = (
        AuditEvent.objects.filter(
            entity_type=entity_type, entity_id=str(entity_id), outcome=AuditEvent.OUTCOME_COMMITTED
        )
        .order_by("id")
        .first()
    )
    return ev.result if ev is not None else None


# ------------------------------------------------------------------ derived state (read only)
def incident_severity(i: Incident) -> str:
    last = inc.latest_severity(i)
    return last.level if last else "unclassified"


def custody_text(c: Custody, event: CustodyEvent | None) -> str:
    """Never call an unverified administrative assignment a verified handoff."""
    if c.kind == "unknown":
        return "Unknown custody (blocks every next step)"
    where = KIND_TEXT.get(c.kind, c.kind)
    if c.kind == "unaccounted":
        return "Unaccounted for"
    if event is not None and event.basis == "administrative" and not c.verified:
        return f"{where} - administrative assignment, not verified"
    if not c.verified:
        return f"{where} (not verified)"
    if event is None:
        return f"{where} (baseline, no custody events)"
    return f"{where} ({event.get_basis_display().lower()} recorded)"


def custody_row(ev: CustodyEvent) -> dict[str, Any]:
    verification = getattr(ev, "verification", None)
    if ev.basis == "administrative":
        if verification is None:
            state, label = "unverified", "Administrative assignment - not verified"
        else:
            state = "verified"
            label = f"Administrative assignment, verified by crew {verification.verified_by.code}"
    elif ev.to_kind == "unaccounted":
        state, label = "unaccounted", "Reported unaccounted (no receiver)"
    elif ev.basis == "recovered":
        state, label = "recovered", "Recovery recorded by ops lead (does not resolve the incident or hold)"
    else:
        state, label = "handoff", "Handoff recorded (custodian and receiver named)"
    return {
        "ev": ev,
        "verification": verification,
        "state": state,
        "label": label,
        "move": f"{KIND_TEXT.get(ev.from_kind, ev.from_kind)} -> {KIND_TEXT.get(ev.to_kind, ev.to_kind)}",
    }


def run_summary(run: ExecutionRun) -> dict[str, Any]:
    custody = current_custody(run.robot_id)
    last_ev = CustodyEvent.objects.filter(robot_id=run.robot_id).order_by("-seq").first()
    trip = list(TripEvent.objects.filter(trip_id=run.trip_id).order_by("seq").values_list("kind", flat=True))
    closure = getattr(run, "closure", None)
    open_interruptions = list(
        Interruption.objects.filter(run=run, resolution__isnull=True)
        .order_by("id")
        .values_list("code", flat=True)
    )
    incidents = list(Incident.objects.filter(run=run).order_by("id"))
    open_incidents = [i.code for i in incidents if inc.incident_status(i) != "closed"]
    stranded = RecoveryDisposition.objects.filter(run=run, decision__in=STRANDED).exists()
    not_back = closure is None and (
        custody.kind in ("unaccounted", "unknown")
        or (custody.kind == "site" and ("departed_site" in trip or stranded))
        or (stranded and custody.kind != "hub")
    )
    progress = TRIP_LABELS[trip[-1]] if trip else "Departed hub (in transit to site)"
    return {
        "run": run,
        "custody": custody,
        "custody_text": custody_text(custody, last_ev),
        "custody_state": (
            "unverified"
            if (not custody.verified or custody.kind in ("unknown", "unaccounted"))
            else "verified"
        ),
        "trip": trip,
        "trip_progress": progress,
        "session": (state := inc.session_state(run.pk)),
        "session_label": SESSION_LABELS.get(state, state),
        "open_interruptions": open_interruptions,
        "open_incidents": open_incidents,
        "closure": closure,
        "closed": closure is not None,
        "not_back": not_back,
        "stranded": stranded,
    }


def _site_check_reasons(sc: SiteCheck, labels: dict[str, str]) -> list[str]:
    audit = _audit_result("site_check", sc.pk)
    if audit is not None and isinstance(audit.get("reasons"), list):
        return [str(r) for r in audit["reasons"]]
    reasons = [
        f"condition not ok: {labels.get(k, k)} ({k})" for k, v in sorted(sc.conditions.items()) if v != "ok"
    ]
    if sc.adults_present > sc.consent_count:
        reasons.append(f"{sc.adults_present} adults present but only {sc.consent_count} valid consents")
    if sc.result == "no_go" and not reasons:
        reasons.append("No-Go (recorded outside the operator UI; reasons not stored)")
    return reasons


def _reconciliation_problems(rec: ReturnReconciliation) -> list[str]:
    audit = _audit_result("return_reconciliation", rec.pk)
    notes = [f"discrepancy: {d}" for d in rec.discrepancies or []]
    if audit is not None and isinstance(audit.get("problems"), list):
        found = [str(p) for p in audit["problems"]]
        return found + [n for n in notes if not any(n[len("discrepancy: ") :] in f for f in found)]
    problems = list(notes)
    if not rec.cargo_matches:
        problems.append("cargo does not match")
    if rec.outcome == "unresolved" and not problems:
        problems.append("Unresolved (recorded outside the operator UI; problems not stored)")
    return problems


# ------------------------------------------------------------------ runs
def _get_run(code: str) -> ExecutionRun:
    return get_object_or_404(
        ExecutionRun.objects.select_related("visit__site", "robot", "revision", "trip", "manifest"), code=code
    )


def _to_run(code: str) -> HttpResponse:
    return redirect("execution:run", code=code)


@login_required
def index(request: HttpRequest) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    runs = ExecutionRun.objects.select_related("visit__site", "robot", "trip").order_by("-recorded_at", "-id")
    return render(request, "execution/index.html", {"rows": [run_summary(r) for r in runs], **_base(request)})


@login_required
def run_detail(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    run = _get_run(code)
    tz = run.visit.site.timezone
    summary = run_summary(run)
    custody = [
        custody_row(e)
        for e in CustodyEvent.objects.filter(run=run)
        .select_related("custodian", "received_by", "incident", "recorded_by", "verification__verified_by")
        .order_by("seq")
    ]
    unverified = [c["ev"] for c in custody if c["state"] == "unverified"]
    trip_events = TripEvent.objects.filter(run=run).select_related("recorded_by").order_by("seq")
    templates = list(SiteCheckTemplate.objects.order_by("-recorded_at", "-id"))
    labels = {i["id"]: i["label"] for t in templates for i in t.items}
    checks = [
        {"sc": sc, "reasons": _site_check_reasons(sc, labels), "local": _local(sc.recorded_at, tz)}
        for sc in SiteCheck.objects.filter(run=run)
        .select_related("template", "verifier", "recorded_by")
        .order_by("-recorded_at", "-id")
    ]
    sessions = (
        SessionEvent.objects.filter(run=run).select_related("site_check", "interruption").order_by("seq")
    )
    interruptions = (
        Interruption.objects.filter(run=run)
        .select_related("incident", "recorded_by", "resolution", "resolution__recorded_by")
        .order_by("id")
    )
    dispositions = RecoveryDisposition.objects.filter(run=run).select_related("incident").order_by("id")
    recs = [
        {"rec": r, "problems": _reconciliation_problems(r)}
        for r in ReturnReconciliation.objects.filter(run=run)
        .select_related("hub_receipt", "post_visit_inspection", "recorded_by")
        .order_by("-recorded_at", "-id")
    ]
    incidents = [
        {"i": i, "status": inc.incident_status(i), "severity": incident_severity(i)}
        for i in Incident.objects.filter(run=run).select_related("hold").order_by("id")
    ]
    crew = [p for p in (run.revision.lead, run.revision.second) if p is not None]
    latest_check = checks[0]["sc"] if checks else None
    state = summary["session"]
    return render(
        request,
        "execution/run.html",
        {
            **summary,
            "tz": tz,
            "custody_rows": custody,
            "unverified": unverified,
            "trip_events": [{"te": t, "local": _local(t.recorded_at, tz)} for t in trip_events],
            "next_trip": TRIP_ORDER[len(summary["trip"])] if len(summary["trip"]) < 3 else None,
            "trip_labels": TRIP_LABELS,
            "templates": templates,
            "checks": checks,
            "latest_check": latest_check,
            "sessions": [{"e": e, "local": _local(e.recorded_at, tz)} for e in sessions],
            "session_kinds": [
                k for k in ses.KINDS if k == "observation" or state in ses.ALLOWED_FROM.get(k, set())
            ],
            "interruptions": interruptions,
            "dispositions": dispositions,
            "decisions": DISPOSITIONS,
            "recs": recs,
            "incidents": incidents,
            "crew": crew,
            "persons": Person.objects.order_by("code"),
            "move_targets": [k for k in ("vehicle", "site", "hub") if k != summary["custody"].kind],
            "event_types": INCIDENT_EVENT_TYPES,
            "knowledge_sources": KNOWLEDGE_SOURCES,
            "now_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M"),
            "report_key": uuid.uuid4().hex,
            **_base(request),
        },
    )


@login_required
@require_POST
def verify_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    try:
        seq = _int(f.get("custody_seq", ""), "Custody sequence")
    except CommandError as exc:
        _refused(request, exc)
        return _to_run(code)
    _run(
        request,
        lg.verify_custody,
        lambda o: (
            f"Custody #{seq} verified by {o.get('verified_by')}; custody is now {o['custody']['label']}"
        ),
        run_code=code,
        custody_seq=seq,
        verifier_code=f.get("verifier", ""),
    )
    return _to_run(code)


@login_required
@require_POST
def trip_event_view(request: HttpRequest, code: str) -> HttpResponse:
    kind = request.POST.get("kind", "")

    def ok(o: dict[str, Any]) -> str:
        msg = f"Trip event recorded: {TRIP_LABELS.get(kind, kind)}"
        return msg + (f". WARNING: {o['warning']}" if o.get("warning") else "")

    _run(request, lg.record_trip_event, ok, run_code=code, kind=kind)
    return _to_run(code)


@login_required
@require_POST
def transfer_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    _run(
        request,
        lg.transfer_custody,
        lambda o: (
            f"Custody move recorded: {o['from']} -> {o['to']} ({o['basis']}); "
            f"custody is now {o['custody']['label']}"
        ),
        run_code=code,
        to_kind=f.get("to_kind", ""),
        custodian_code=f.get("custodian", ""),
        received_by_code=f.get("received_by", ""),
        note=f.get("note", ""),
    )
    return _to_run(code)


@login_required
@require_POST
def site_check_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    version = f.get("template", "")
    template = SiteCheckTemplate.objects.filter(version=version).first()
    try:
        if template is None:
            raise Invalid(f"No site check template {version!r}")
        adults = _int(f.get("adults_present", ""), "Adults present")
        conditions = {i["id"]: f.get(f"cond_{i['id']}", "") for i in template.items}
    except CommandError as exc:
        _refused(request, exc)
        return _to_run(code)

    def ok(o: dict[str, Any]) -> str:
        if o["result"] == "go":
            return "Site check recorded: Go (every recorded check passed; this is not real-world clearance)"
        return "Site check recorded: No-Go - " + "; ".join(o.get("reasons") or [])

    _run(
        request,
        sck.record_site_check,
        ok,
        run_code=code,
        template_version=version,
        adults_present=adults,
        conditions=conditions,
        verifier_code=f.get("verifier", ""),
    )
    return _to_run(code)


@login_required
@require_POST
def reconcile_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    discrepancies = [d.strip() for d in f.get("discrepancies", "").splitlines() if d.strip()]

    def ok(o: dict[str, Any]) -> str:
        if o["outcome"] == "reconciled":
            return "Return reconciliation recorded: reconciled"
        return "Return reconciliation recorded: UNRESOLVED - " + "; ".join(o.get("problems") or [])

    _run(
        request,
        recon.reconcile_return,
        ok,
        run_code=code,
        inspection_code=f.get("inspection_code", "").strip() or None,
        discrepancies=discrepancies,
    )
    return _to_run(code)


@login_required
@require_POST
def close_view(request: HttpRequest, code: str) -> HttpResponse:
    _run(request, recon.close_trip, f"Trip closed; run {code} is closed", run_code=code)
    return _to_run(code)


@login_required
@require_POST
def session_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    kind = f.get("kind", "")
    raw = f.get("site_check_id", "").strip()
    try:
        check_id = _int(raw, "Site check") if raw and kind in ("start", "resume") else None
    except CommandError as exc:
        _refused(request, exc)
        return _to_run(code)
    _run(
        request,
        ses.record_session_event,
        f"Session event recorded: {kind}",
        run_code=code,
        kind=kind,
        site_check_id=check_id,
        note=f.get("note", ""),
    )
    return _to_run(code)


@login_required
@require_POST
def interruption_open_view(request: HttpRequest, code: str) -> HttpResponse:
    _run(
        request,
        ses.open_interruption,
        lambda o: f"Interruption {o.get('code') or o.get('entity_id')} opened",
        run_code=code,
        reason=request.POST.get("reason", ""),
    )
    return _to_run(code)


@login_required
@require_POST
def interruption_resolve_view(request: HttpRequest, code: str) -> HttpResponse:
    ic = request.POST.get("interruption", "")
    if not Interruption.objects.filter(code=ic, run__code=code).exists():
        messages.error(request, f"Refused (invalid input): no interruption {ic} on run {code}")
        return _to_run(code)
    _run(
        request,
        ses.resolve_interruption,
        f"Interruption {ic} resolved",
        code=ic,
        note=request.POST.get("note", ""),
    )
    return _to_run(code)


@login_required
@require_POST
def disposition_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    decision = f.get("decision", "")
    _run(
        request,
        ses.record_disposition,
        f"Disposition recorded: {decision}",
        run_code=code,
        decision=decision,
        reason=f.get("reason", ""),
        incident_code=f.get("incident_code", "").strip() or None,
    )
    return _to_run(code)


# ------------------------------------------------------------------ incidents
def _incident_tz(i: Incident) -> str:
    return i.visit.site.timezone if i.visit is not None else "UTC"


def _incident_row(i: Incident) -> dict[str, Any]:
    tz = _incident_tz(i)
    return {
        "i": i,
        "tz": tz,
        "status": inc.incident_status(i),
        "severity": incident_severity(i),
        "hold_active": i.hold.released_at is None,
        "occurred_local": _local(i.occurred_at, tz),
        "reported_local": _local(i.reported_at, tz),
    }


def _report_context() -> dict[str, Any]:
    return {
        "robots": Robot.objects.order_by("code"),
        "event_types": INCIDENT_EVENT_TYPES,
        "knowledge_sources": KNOWLEDGE_SOURCES,
        "now_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M"),
        "report_key": uuid.uuid4().hex,
    }


@login_required
def incidents(request: HttpRequest) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    rows = [
        _incident_row(i)
        for i in Incident.objects.select_related("robot", "run", "visit__site", "hold").order_by("-id")
    ]
    return render(
        request,
        "execution/incidents.html",
        {"rows": rows, "preselect": request.GET.get("robot", ""), **_report_context(), **_base(request)},
    )


@login_required
def incident_detail(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    i = get_object_or_404(
        Incident.objects.select_related(
            "robot", "run", "visit__site", "hold", "manifest__pattern", "recorded_by"
        ),
        code=code,
    )
    row = _incident_row(i)
    tz = row["tz"]
    severities = (
        SeverityDecision.objects.filter(incident=i).select_related("policy", "recorded_by").order_by("id")
    )
    statuses = IncidentStatusChange.objects.filter(incident=i).select_related("recorded_by").order_by("id")
    escalations = (
        IncidentEscalation.objects.filter(incident=i)
        .select_related("to_person", "recorded_by", "ack", "ack__acknowledged_by")
        .order_by("id")
    )
    my_roles = active_roles(request.user)
    faults = FaultObservation.objects.filter(incident=i).select_related("recorded_by").order_by("id")
    status = row["status"]
    idx = STATUS_STEPS.index(status) if status in STATUS_STEPS else len(STATUS_STEPS) - 1
    next_status = STATUS_STEPS[idx + 1] if idx + 1 < len(STATUS_STEPS) else None
    policy = pol.current_incident_policy()
    custody = current_custody(i.robot_id)
    no_run_unaccounted = custody.kind == "unaccounted" and open_run_id(i.robot_id) is None
    return render(
        request,
        "execution/incident.html",
        {
            **row,
            "severities": [{"d": d, "local": _local(d.recorded_at, tz)} for d in severities],
            "statuses": [{"s": s, "local": _local(s.recorded_at, tz)} for s in statuses],
            "escalations": [
                {
                    "e": e,
                    "ack": getattr(e, "ack", None),
                    "can_ack": getattr(e, "ack", None) is None and e.to_role in my_roles,
                }
                for e in escalations
            ],
            "faults": faults,
            "next_status": next_status,
            "policy": policy,
            "scale": list((policy.rules or {}).get("severity_scale", [])) if policy else [],
            "roles_all": [r for r in ALL_ROLES if r != "admin"],
            "persons": Person.objects.order_by("code"),
            "custody_text": custody_text(
                custody, CustodyEvent.objects.filter(robot_id=i.robot_id).order_by("-seq").first()
            ),
            "can_recover": no_run_unaccounted and _recover_robot() is not None,
            **_base(request),
        },
    )


def _to_incident(code: str) -> HttpResponse:
    return redirect("execution:incident", code=code)


@login_required
@require_POST
def report_view(request: HttpRequest) -> HttpResponse:
    f = request.POST
    back_run = f.get("run", "").strip()
    back = _to_run(back_run) if back_run else redirect("execution:incidents")
    raw = f.get("occurred_at", "").strip()
    try:
        if not raw:
            raise Invalid("Occurred at is required (when it happened, UTC)")
        try:
            occurred = datetime.fromisoformat(raw)
        except ValueError:
            raise Invalid("Occurred at must be a date and time") from None
        occurred = occurred.replace(tzinfo=UTC) if occurred.tzinfo is None else occurred
    except CommandError as exc:
        _refused(request, exc)
        return back
    out = _run(
        request,
        inc.report_incident,
        lambda o: (
            f"Incident {o.get('code') or o.get('entity_id')} reported; robot {o.get('robot', '')} "
            f"is held by {o.get('hold', 'an incident hold')} until released through the fleet hold workflow"
        ),
        robot_code=f.get("robot", ""),
        event_type=f.get("event_type", ""),
        description=f.get("description", ""),
        occurred_at=occurred,
        knowledge_source=f.get("knowledge_source", ""),
        idempotency_key=f.get("idempotency_key") or None,
    )
    if out is not None and not back_run:
        code = out.get("code") or out.get("entity_id")
        if code and Incident.objects.filter(code=code).exists():
            return _to_incident(str(code))
    return back


@login_required
@require_POST
def fault_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    _run(
        request,
        inc.add_fault_observation,
        "Fault observation recorded",
        incident_code=code,
        component=f.get("component", ""),
        status=f.get("status", ""),
        evidence_ref=f.get("evidence_ref", ""),
    )
    return _to_incident(code)


@login_required
@require_POST
def severity_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    level = f.get("level", "")
    _run(
        request,
        inc.decide_severity,
        f"Severity decision recorded: {level}",
        incident_code=code,
        level=level,
        reason=f.get("reason", ""),
    )
    return _to_incident(code)


@login_required
@require_POST
def status_view(request: HttpRequest, code: str) -> HttpResponse:
    to = request.POST.get("to_status", "")
    _run(
        request,
        inc.change_incident_status,
        f"Incident status changed to {to} (the robot's incident hold is unchanged)",
        incident_code=code,
        to_status=to,
    )
    return _to_incident(code)


@login_required
@require_POST
def escalate_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    _run(
        request,
        esc.escalate_incident,
        f"Incident escalated to {f.get('to_role', '')}",
        incident_code=code,
        to_role=f.get("to_role", ""),
        requested_action=f.get("requested_action", ""),
        to_person_code=f.get("to_person", "").strip() or None,
    )
    return _to_incident(code)


@login_required
@require_POST
def ack_view(request: HttpRequest, code: str) -> HttpResponse:
    raw = request.POST.get("escalation", "")
    try:
        eid = _int(raw, "Escalation")
        if not IncidentEscalation.objects.filter(pk=eid, incident__code=code).exists():
            raise Invalid(f"No escalation {raw} on incident {code}")
    except CommandError as exc:
        _refused(request, exc)
        return _to_incident(code)
    _run(request, esc.acknowledge_escalation, "Escalation acknowledged", escalation_id=eid)
    return _to_incident(code)


@login_required
@require_POST
def recover_view(request: HttpRequest, code: str) -> HttpResponse:
    i = get_object_or_404(Incident.objects.select_related("robot"), code=code)
    f = request.POST
    recover = _recover_robot()
    if recover is None:
        messages.error(
            request, "Refused (not available): recording a no-run recovery is not in this build yet"
        )
        return _to_incident(code)
    _run(
        request,
        recover,
        lambda o: (
            f"Recovery recorded: robot {i.robot.code} received at the hub. The incident and its hold are "
            "unchanged."
        ),
        robot_code=i.robot.code,
        received_by_code=f.get("received_by", ""),
        note=f.get("note", ""),
    )
    return _to_incident(code)


# ------------------------------------------------------------------ incident policies
@login_required
def policies(request: HttpRequest) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    current = pol.current_incident_policy()
    rows = []
    for p in IncidentPolicy.objects.select_related("recorded_by").order_by("-version"):
        approvals = {a.slot: a for a in p.approvals.select_related("approver")}
        rows.append(
            {
                "policy": p,
                "status": pol.incident_policy_status(p),
                "is_current": current is not None and current.pk == p.pk,
                "synthetic": p.source == "synthetic_demo",
                "approvals": [
                    {"slot": s, "label": lbl, "approval": approvals.get(s)} for s, lbl in POLICY_SLOTS
                ],
                "withdrawal": getattr(p, "withdrawal", None),
                "rules": json.dumps(p.rules, indent=2, sort_keys=True),
            }
        )
    return render(
        request,
        "execution/policies.html",
        {
            "rows": rows,
            "current": current,
            "sources": SOURCES,
            "example": request.session.pop("execution_policy_draft", None)
            or json.dumps(EXAMPLE_RULES, indent=2),
            **_base(request),
        },
    )


def _to_policies() -> HttpResponse:
    return redirect("execution:policies")


@login_required
@require_POST
def policy_create_view(request: HttpRequest) -> HttpResponse:
    f = request.POST
    raw = f.get("rules", "")
    try:
        try:
            rules = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Invalid(f"Rules are not valid JSON: {exc.msg} (line {exc.lineno})") from None
    except CommandError as exc:
        _refused(request, exc)
        request.session["execution_policy_draft"] = raw
        return _to_policies()
    out = _run(
        request,
        pol.create_incident_policy,
        lambda o: (
            f"Incident policy v{o.get('version') or o.get('entity_id')} created as a DRAFT; "
            "it needs both approvals"
        ),
        rules=rules,
        source=f.get("source", ""),
        notes=f.get("notes", ""),
    )
    if out is None:
        request.session["execution_policy_draft"] = raw
    return _to_policies()


@login_required
@require_POST
def policy_approve_view(request: HttpRequest, version: int) -> HttpResponse:
    slot = request.POST.get("slot", "")
    _run(
        request,
        pol.approve_incident_policy,
        f"Incident policy v{version}: {slot} approval recorded",
        version=version,
        slot=slot,
    )
    return _to_policies()


@login_required
@require_POST
def policy_withdraw_view(request: HttpRequest, version: int) -> HttpResponse:
    _run(
        request,
        pol.withdraw_incident_policy,
        f"Incident policy v{version} withdrawn",
        version=version,
        reason=request.POST.get("reason", ""),
    )
    return _to_policies()


# ------------------------------------------------------------------ visit dossier
@login_required
def dossier(request: HttpRequest, visit_code: str) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    visit = get_object_or_404(Visit.objects.select_related("site"), code=visit_code)
    tz = visit.site.timezone
    entries = [{"e": e, "local": _local(e.at, tz)} for e in visit_dossier(visit)]
    run = ExecutionRun.objects.filter(visit=visit).first()
    return render(
        request,
        "execution/dossier.html",
        {"visit": visit, "tz": tz, "entries": entries, "run": run, **_base(request)},
    )
