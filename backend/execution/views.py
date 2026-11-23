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

import importlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Q
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
    DEADLINE_SLOTS,
    DEADLINE_SOURCES,
    DISPOSITIONS,
    INCIDENT_EVENT_TYPES,
    INCIDENT_STATUSES,
    KNOWLEDGE_SOURCES,
    POLICY_SLOTS,
    SOURCES,
    CustodyEvent,
    EscalationDeadlineSet,
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
from fleet.models import Inspection, Robot
from fleet.services import demo_mode
from planning.models import Person, Visit
from platform_core.commands import CommandError, Invalid
from platform_core.interaction import one_time_form, refused_message
from platform_core.models import AuditEvent
from platform_core.timezones import workspace_zone

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
    "manage_escalation_deadlines",
    "approve_escalation_deadlines",
    "withdraw_escalation_deadlines",
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
RECEIVER_REQUIRED = (
    "Refused (invalid input): choose the receiver. Every custody move is a handoff and needs the person who "
    "receives the robot. Nothing was recorded."
)
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
    messages.error(request, refused_message(exc, ERROR_KINDS))


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


# ------------------------------------------------------------------ next step (01D J5-2)
# The run page's guide: from the records alone, the ONE next thing the crew or the ops lead records, in the
# order the commands accept them. It changes no rule: every command still checks everything itself. Forms
# whose record would be a permanent negative only because of ordering (a site check before the robot is in
# site custody is a No-Go; a return reconciliation before the hub receipt or the post-visit inspection is
# unresolved) are not offered as actionable forms until their precondition holds; the guide names the step
# that comes first instead.
NEXT_STEPS: dict[str, tuple[str, str]] = {
    "done": (
        "Nothing left to record",
        "The trip is closed. Holds on the robot, if any, go through the fleet hold-release workflow.",
    ),
    "attention": (
        "The robot's whereabouts need the ops lead",
        "Custody is unaccounted for or unknown. The ops "
        "lead records the recovery or a disposition; see Recovery dispositions below.",
    ),
    "verify": (
        "Verify the departure handover",
        "A crew member confirms the robot is in the vehicle (the "
        "departure recorded it administratively, not verified).",
    ),
    "arrive_site": (
        "Record the arrival at the site",
        "When the vehicle reaches the home, record that it arrived.",
    ),
    "to_site": (
        "Hand the robot over to the site",
        "Record a custody move to site with a receiver from the "
        "crew. A site check counts only once the robot is in site custody.",
    ),
    "site_check": (
        "Record the site check",
        "Walk the checklist with the household before the robot works. "
        "Every item ok and enough consents give a Go.",
    ),
    "start": ("Start the session", "The latest site check is Go: start the robot's work session."),
    "resume": (
        "Resume or stop the session",
        "The session is paused. Resuming needs a new Go site check "
        "recorded after the pause; otherwise stop the session.",
    ),
    "stop": ("Stop the session when the work is done", "Record the stop when the robot has finished."),
    "to_vehicle": (
        "Move the robot back to the vehicle",
        "Record a custody move to vehicle with a receiver from the crew.",
    ),
    "depart_site": ("Record leaving the site", "Record that the vehicle left the home."),
    "arrive_hub": (
        "Record the arrival at the hub",
        "Record that the vehicle is back at the hub. This records "
        "the vehicle only; the robot is received separately.",
    ),
    "to_hub": (
        "Receive the robot at the hub",
        "Record a custody move to hub with the person who receives it.",
    ),
    "inspect": (
        "Record the post-visit inspection",
        "Inspect the robot after the hub receipt (an inspection "
        "for this visit). The return can be reconciled only after it.",
    ),
    "reconcile": ("Reconcile the return", "Record the return reconciliation with the post-visit inspection."),
    "close": ("Close the trip", "The return is reconciled: close the trip."),
}
STEP_FORM = {
    "verify": "verify",
    "arrive_site": "trip",
    "depart_site": "trip",
    "arrive_hub": "trip",
    "to_site": "custody",
    "to_vehicle": "custody",
    "to_hub": "custody",
    "site_check": "site_check",
    "start": "session",
    "resume": "session",
    "stop": "session",
    "inspect": "inspect",
    "reconcile": "reconcile",
    "close": "close",
}
STEP_PRESET = {"to_site": "site", "to_vehicle": "vehicle", "to_hub": "hub", "start": "start", "stop": "stop"}


def next_step_key(
    summary: dict[str, Any],
    *,
    unverified: bool,
    latest_check_go: bool,
    post_inspections: bool,
    reconciled: bool,
) -> str:
    """The next step's key (NEXT_STEPS), from the run's records in command order."""
    if summary["closed"]:
        return "done"
    kind, trip, session = summary["custody"].kind, summary["trip"], summary["session"]
    if kind in ("unaccounted", "unknown"):
        return "attention"
    if unverified:
        return "verify"
    if "arrived_site" not in trip:
        return "arrive_site"
    if kind == "vehicle" and session == "not_started" and "departed_site" not in trip:
        return "to_site"
    if kind == "site" and session == "not_started":
        return "start" if latest_check_go else "site_check"
    if session == "paused":
        return "resume"
    if session == "active":
        return "stop"
    if kind == "site":
        return "to_vehicle"
    if "departed_site" not in trip:
        return "depart_site"
    if "arrived_hub" not in trip:
        return "arrive_hub"
    if kind != "hub":
        return "to_hub"
    if not post_inspections:
        return "inspect"
    return "close" if reconciled else "reconcile"


def site_check_actionable(summary: dict[str, Any]) -> bool:
    """A site check counts only with the robot in site custody; before that it can only be a No-Go."""
    return summary["custody"].kind == "site" and not summary["closed"]


# ------------------------------------------------------------------ runs
def _get_run(code: str) -> ExecutionRun:
    return get_object_or_404(
        ExecutionRun.objects.select_related("visit__site", "robot", "revision", "trip", "manifest"), code=code
    )


def _to_run(code: str) -> HttpResponse:
    return redirect("execution:run", code=code)


def _back_run(request: HttpRequest, code: str, **kw: Any) -> HttpResponse:
    return _to_run(code)


def _back_incident(request: HttpRequest, code: str, **kw: Any) -> HttpResponse:
    return _to_incident(code)


def _back_incidents(request: HttpRequest, **kw: Any) -> HttpResponse:
    return redirect("execution:incidents")


def _back_policies(request: HttpRequest, **kw: Any) -> HttpResponse:
    return _to_policies()


def _back_deadlines(request: HttpRequest, **kw: Any) -> HttpResponse:
    return _to_deadlines()


@login_required
def index(request: HttpRequest) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    runs = ExecutionRun.objects.select_related("visit__site", "robot", "trip").order_by("-recorded_at", "-id")
    return render(request, "execution/index.html", {"rows": [run_summary(r) for r in runs], **_base(request)})


@login_required
def my_visits(request: HttpRequest) -> HttpResponse:
    """01D J5-1: the signed-in person's visits today and tomorrow (workspace local days), where they are the
    lead or the second of the current plan, with links to the readiness checklist and the field run."""
    if (deny := _deny(request)) is not None:
        return deny
    from readiness.models import DepartureEvent  # noqa: PLC0415 - read only

    person = Person.objects.select_related("user").filter(user_id=request.user.pk).first()
    zone = workspace_zone()
    today = datetime.now(zone).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=zone)
    end = start + timedelta(days=2)
    rows: list[dict[str, Any]] = []
    if person is not None:
        visits = (
            Visit.objects.select_related("site", "current_revision__robot", "current_revision__lead__user",
                                         "current_revision__second__user")
            .exclude(status="cancelled")
            .filter(current_revision__on_site_start__gte=start, current_revision__on_site_start__lt=end)
            .filter(Q(current_revision__lead=person) | Q(current_revision__second=person))
            .order_by("current_revision__on_site_start", "code")
        )  # fmt: skip
        runs = {r.visit_id: r for r in ExecutionRun.objects.filter(visit__in=list(visits))}
        departed = set(
            DepartureEvent.objects.filter(visit__in=list(visits)).values_list("visit_id", flat=True)
        )
        for v in visits:
            rev = v.current_revision
            assert rev is not None
            local = rev.on_site_start.astimezone(zone)
            run = runs.get(v.pk)
            rows.append(
                {
                    "visit": v,
                    "rev": rev,
                    "day": "Today" if local.date() == today else "Tomorrow",
                    "role": "Lead" if rev.lead_id == person.pk else "Second",
                    "run": run,
                    "departed": v.pk in departed,
                }
            )
    return render(
        request, "execution/mine.html", {"person": person, "rows": rows, "today": today, **_base(request)}
    )


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
        .select_related(
            "custodian__user",
            "received_by__user",
            "incident",
            "recorded_by",
            "verification__verified_by",
        )
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
    crew = [
        p
        for p in Person.objects.select_related("user").filter(
            pk__in=[x for x in (run.revision.lead_id, run.revision.second_id) if x is not None]
        )
    ]
    crew.sort(key=lambda p: 0 if p.pk == run.revision.lead_id else 1)
    latest_check = checks[0]["sc"] if checks else None
    state = summary["session"]
    hub_receipt = CustodyEvent.objects.filter(run=run, to_kind="hub").order_by("-seq").first()
    post_inspections = (
        list(
            Inspection.objects.filter(
                robot_id=run.robot_id, visit_ref=run.visit.code, submitted_at__gt=hub_receipt.recorded_at
            ).order_by("-id")
        )
        if hub_receipt is not None
        else []
    )
    reconciled = bool(recs) and getattr(recs[0]["rec"], "outcome", "") == "reconciled"
    step = next_step_key(
        summary,
        unverified=bool(unverified),
        latest_check_go=getattr(latest_check, "result", "") == "go",
        post_inspections=bool(post_inspections),
        reconciled=reconciled,
    )
    title, text = NEXT_STEPS[step]
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
            "persons": Person.objects.select_related("user").order_by("code"),
            "next": {
                "key": step,
                "title": title,
                "text": text,
                "form": STEP_FORM.get(step, ""),
                "preset": STEP_PRESET.get(step, ""),
            },
            "site_check_ok": site_check_actionable(summary),
            "reconcile_ok": hub_receipt is not None and bool(post_inspections) and not summary["closed"],
            "post_inspections": post_inspections,
            "hub_receipt": hub_receipt,
            "move_targets": [k for k in ("vehicle", "site", "hub") if k != summary["custody"].kind],
            "event_types": INCIDENT_EVENT_TYPES,
            "knowledge_sources": KNOWLEDGE_SOURCES,
            "now_local": datetime.now(workspace_zone()).strftime("%Y-%m-%dT%H:%M"),
            "report_key": uuid.uuid4().hex,
            **_base(request),
        },
    )


@login_required
@require_POST
@one_time_form(_back_run)
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
@one_time_form(_back_run)
def trip_event_view(request: HttpRequest, code: str) -> HttpResponse:
    kind = request.POST.get("kind", "")

    def ok(o: dict[str, Any]) -> str:
        msg = f"Trip event recorded: {TRIP_LABELS.get(kind, kind)}"
        return msg + (f". WARNING: {o['warning']}" if o.get("warning") else "")

    _run(request, lg.record_trip_event, ok, run_code=code, kind=kind)
    return _to_run(code)


@login_required
@require_POST
@one_time_form(_back_run)
def transfer_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    if not f.get("received_by", "").strip():
        # 01D J5-5: said before the command runs (it refuses the same way: "A handoff needs a receiver")
        messages.error(request, RECEIVER_REQUIRED)
        return _to_run(code)
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
@one_time_form(_back_run)
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
@one_time_form(_back_run)
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
@one_time_form(_back_run)
def close_view(request: HttpRequest, code: str) -> HttpResponse:
    _run(request, recon.close_trip, f"Trip closed; run {code} is closed", run_code=code)
    return _to_run(code)


@login_required
@require_POST
@one_time_form(_back_run)
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
@one_time_form(_back_run)
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
@one_time_form(_back_run)
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
@one_time_form(_back_run)
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
        "now_local": datetime.now(workspace_zone()).strftime("%Y-%m-%dT%H:%M"),
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
    escalations = list(
        IncidentEscalation.objects.filter(incident=i)
        .select_related("to_person", "recorded_by", "ack", "ack__acknowledged_by")
        .order_by("id")
    )
    deadlines = incident_deadlines(i, escalations)
    for d in deadlines["entries"]:
        d["due_local"] = _local(d["due"], tz)
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
            "deadlines": deadlines,
            **_base(request),
        },
    )


# ------------------------------------------------------------------ Slice 7 X-4: escalation deadlines (read)
DEADLINES_SQL = """
SELECT d.id, d.version_no, d.source, d.incident_policy_id, d.deadlines, ip.version,
       m.production, clock_timestamp()
FROM (SELECT fleet_production_mode() AS production) m
LEFT JOIN execution_escalationdeadlineset d
  ON d.id = execution_active_deadline_set_id() AND (d.source = 'owner_approved' OR NOT m.production)
LEFT JOIN execution_incidentpolicy ip ON ip.id = d.incident_policy_id
"""


def incident_deadlines(i: Incident, escalations: list[IncidentEscalation]) -> dict[str, Any]:
    """Each required (role, deadline) of the incident under the usable deadline set, with its state at
    database
    time: the same rule as the inbox's ESCALATION_OVERDUE (contract 07a 6.2). Shown only, never sent."""
    with connection.cursor() as cur:
        cur.execute(DEADLINES_SQL)
        row = cur.fetchone()
    assert row is not None
    set_id, version_no, source, policy_id, deadlines, policy_version, production, now = row
    if set_id is None:
        return {
            "configured": False,
            "reason": "No escalation deadline set is usable in "
            + ("production (it must be owner approved)" if production else "demo")
            + " mode for the active incident policy.",
            "entries": [],
        }
    decision = inc.latest_severity(i)
    if decision is None:
        level: str | None = "unclassified"
    elif decision.policy_id == policy_id:
        level = decision.level
    else:
        level = None
    if isinstance(deadlines, str):
        deadlines = json.loads(deadlines)
    if level is None or level not in deadlines:
        return {
            "configured": False,
            "reason": "The latest severity decision was made under another incident policy version; no "
            f"escalation deadline of set v{version_no} applies to this incident.",
            "entries": [],
        }
    closed = inc.incident_status(i) == "closed"
    entries = []
    for entry in deadlines[level]:
        role, minutes = entry["to_role"], int(entry["within_min"])
        due = i.reported_at + timedelta(minutes=minutes)
        to_role = [e for e in escalations if e.to_role == role]
        acked = [e for e in to_role if getattr(e, "ack", None) is not None]
        if acked:
            state = "acknowledged"
        elif closed:
            state = "incident closed"
        elif now >= due:
            state = "overdue: escalated, not acknowledged" if to_role else "overdue: no escalation recorded"
        else:
            state = "escalated, awaiting acknowledgment" if to_role else "not yet escalated"
        entries.append(
            {
                "role": role,
                "within_min": minutes,
                "due": due,
                "state": state,
                "overdue": state.startswith("overdue"),
            }
        )
    return {
        "configured": True,
        "level": level,
        "set_version": version_no,
        "policy_version": policy_version,
        "synthetic": source == "synthetic_demo",
        "entries": entries,
    }


def _to_incident(code: str) -> HttpResponse:
    return redirect("execution:incident", code=code)


@login_required
@require_POST
@one_time_form(_back_incidents)
def report_view(request: HttpRequest) -> HttpResponse:
    f = request.POST
    back_run = f.get("run", "").strip()
    back = _to_run(back_run) if back_run else redirect("execution:incidents")
    raw = f.get("occurred_at", "").strip()
    try:
        if not raw:
            raise Invalid("Occurred at is required (when it happened)")
        try:
            occurred = datetime.fromisoformat(raw)
        except ValueError:
            raise Invalid("Occurred at must be a date and time") from None
        # a typed time is in the workspace display zone (the form's label); the database stores UTC
        occurred = occurred.replace(tzinfo=workspace_zone()) if occurred.tzinfo is None else occurred
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
@one_time_form(_back_incident)
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
@one_time_form(_back_incident)
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
@one_time_form(_back_incident)
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
@one_time_form(_back_incident)
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
@one_time_form(_back_incident)
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
@one_time_form(_back_incident)
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
@one_time_form(_back_policies)
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
@one_time_form(_back_policies)
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
@one_time_form(_back_policies)
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


# ------------------------------------------------------------------ Slice 7 X-4: escalation deadline
# sets page
def _deadline_command(name: str) -> Callable[..., dict[str, Any]] | None:
    """execution.escalation_deadlines.<name> (Engineer B, contract 07a 6.3) when this build has it."""
    try:
        module = importlib.import_module("execution.escalation_deadlines")
    except ImportError:
        return None
    found = getattr(module, name, None)
    return found if callable(found) else None


@login_required
def escalation_deadlines(request: HttpRequest) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    with connection.cursor() as cur:
        cur.execute(
            "SELECT execution_active_deadline_set_id(), execution_active_incident_policy_id(), "
            "fleet_production_mode()"
        )
        row = cur.fetchone()
    active_id, active_policy_id, production = row if row else (None, None, True)
    rows = []
    for d in (
        EscalationDeadlineSet.objects.select_related(
            "incident_policy", "created_by", "withdrawal", "withdrawal__withdrawn_by"
        )
        .prefetch_related("approvals__approver")
        .order_by("-incident_policy__version", "-version_no")
    ):
        approvals = {a.slot: a for a in d.approvals.all()}
        withdrawal = getattr(d, "withdrawal", None)
        approved = {s for s, _ in DEADLINE_SLOTS} <= set(approvals)
        rows.append(
            {
                "set": d,
                "status": "withdrawn" if withdrawal else ("approved" if approved else "awaiting approval"),
                "withdrawal": withdrawal,
                "active": d.pk == active_id,
                "usable_now": d.pk == active_id and (d.source == "owner_approved" or not production),
                "synthetic": d.source == "synthetic_demo",
                "for_active_policy": d.incident_policy_id == active_policy_id,
                "slots": [
                    {"slot": s, "label": lbl, "approval": approvals.get(s)} for s, lbl in DEADLINE_SLOTS
                ],
                "deadlines": json.dumps(d.deadlines, indent=2, sort_keys=True),
            }
        )
    active_policy = IncidentPolicy.objects.filter(pk=active_policy_id).first() if active_policy_id else None
    example: dict[str, list[dict[str, Any]]] = {}
    if active_policy is not None:
        example = {lvl: [] for lvl in (active_policy.rules or {}).get("severity_scale", [])}
        example["unclassified"] = []
    return render(
        request,
        "execution/escalation_deadlines.html",
        {
            "rows": rows,
            "production": production,
            "usable": any(r["usable_now"] for r in rows),
            "active_policy": active_policy,
            "sources": DEADLINE_SOURCES,
            "example": request.session.pop("execution_deadlines_draft", None)
            or json.dumps(example, indent=2, sort_keys=True),
            **_base(request),
        },
    )


def _to_deadlines() -> HttpResponse:
    return redirect("execution:escalation-deadlines")


def _deadline_action(request: HttpRequest, permission: str, name: str, ok: str, **kw: Any) -> HttpResponse:
    if not has_permission(request.user, permission):
        return HttpResponseForbidden("You do not have permission for this action.")
    fn = _deadline_command(name)
    if fn is None:
        messages.error(request, f"Refused: this build does not provide {name} yet; nothing was recorded")
        return _to_deadlines()
    _run(request, fn, ok, **kw)
    return _to_deadlines()


@login_required
@require_POST
@one_time_form(_back_deadlines, permission="execution.manage_escalation_deadlines")
def deadline_set_create_view(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, "execution.manage_escalation_deadlines"):
        return HttpResponseForbidden("You do not have permission for this action.")
    f = request.POST
    raw = f.get("deadlines", "")
    try:
        deadlines = json.loads(raw)
    except json.JSONDecodeError as exc:
        messages.error(request, f"Refused (invalid input): deadlines are not valid JSON ({exc.msg})")
        request.session["execution_deadlines_draft"] = raw
        return _to_deadlines()
    # Engineer B: create_deadline_set(actor, *, incident_policy_version, deadlines, source, notes),
    # always for the ACTIVE incident policy (the page says so); no active policy: nothing is recorded.
    with connection.cursor() as cur:
        cur.execute(
            "SELECT p.version FROM execution_incidentpolicy p"
            " WHERE p.id = execution_active_incident_policy_id()"
        )
        row = cur.fetchone()
    if row is None:
        messages.error(request, "Refused: there is no active incident policy; nothing was recorded")
        return _to_deadlines()
    return _deadline_action(
        request,
        "execution.manage_escalation_deadlines",
        "create_deadline_set",
        "Escalation deadline set recorded; it needs the safety and founder approvals",
        incident_policy_version=row[0],
        deadlines=deadlines,
        source=f.get("source", ""),
        notes=f.get("notes", ""),
    )


@login_required
@require_POST
@one_time_form(_back_deadlines, permission="execution.approve_escalation_deadlines")
def deadline_set_approve_view(request: HttpRequest, set_id: int) -> HttpResponse:
    # Engineer B: approve_deadline_set(actor, *, set_id, slot)
    return _deadline_action(
        request,
        "execution.approve_escalation_deadlines",
        "approve_deadline_set",
        "Deadline set approval recorded",
        set_id=set_id,
        slot=request.POST.get("slot", ""),
    )


@login_required
@require_POST
@one_time_form(_back_deadlines, permission="execution.withdraw_escalation_deadlines")
def deadline_set_withdraw_view(request: HttpRequest, set_id: int) -> HttpResponse:
    # Engineer B: withdraw_deadline_set(actor, *, set_id, reason)
    return _deadline_action(
        request,
        "execution.withdraw_escalation_deadlines",
        "withdraw_deadline_set",
        "Deadline set withdrawn; there is no fallback to an older version",
        set_id=set_id,
        reason=request.POST.get("reason", ""),
    )
