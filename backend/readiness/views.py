"""Server-rendered readiness and departure-authorization UI (Engineer D). Contract: docs/03, s4.

Views only READ models and call the contract commands; every write goes through readiness.evidence
(Engineer A) or readiness.commands (Engineer C), which enforce permissions, locks and invariants. Buttons are
hidden for users who lack a permission, but the commands are the real gate.

SYNTHETIC data only. Nothing here grants real-world clearance, and no page ever says a robot may go.
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import active_roles, has_permission
from execution.models import ExecutionRun
from fleet import services as fleet_services
from fleet.models import Inspection, InspectionTemplate, Robot
from fleet.services import demo_mode
from planning import services as planning_services
from planning.models import Person, Visit
from platform_core.commands import CommandError, Invalid
from platform_core.hubscope import hub_scope, other_hub_refusal
from platform_core.interaction import (
    command_page,
    one_time_form,
    refusal_text,
    refused_message,
    who_can,
)
from platform_core.templatetags.ui import local_dt
from readiness import commands as rc
from readiness import evidence as rx
from readiness.authorization import authorization_status
from readiness.evaluator import ReadinessResult, evaluate_readiness
from readiness.forms import InspectionForm
from readiness.models import (
    EVIDENCE_KINDS,
    POLICY_SLOTS,
    DepartureAuthorization,
    DepartureEvent,
    ReadinessEvidence,
    ReadinessPolicy,
    RecoveryPlan,
)

DEMO_BANNER = "SYNTHETIC DEMO - not real-world clearance"
ACTIONS = [
    "manage_policy",
    "approve_policy",
    "withdraw_policy",
    "record_evidence",
    "record_recovery_plan",
    "authorize_departure",
    "revoke_authorization",
    "record_departure",
]
# Contract s4 gate order -> blocker code prefixes. Unknown codes land in "Other".
GATES = [
    ("Visit confirmed", ("VISIT_",)),
    ("Planning still feasible now", ("PLANNING_",)),
    ("Reservations still active", ("RESERVATIONS_",)),
    ("Approved readiness policy", ("POLICY_",)),
    ("Evidence (fresh, passing, current configuration)", ("EVIDENCE_",)),
    ("Battery above departure minimum", ("BATTERY_",)),
    ("Required equipment present", ("EQUIPMENT_",)),
    ("Recovery plan", ("RECOVERY_PLAN_",)),
    ("Authorization window in the future", ("EXPIRY_",)),
]
AUTH_LABELS = {
    "expired": "Authorization expired",
    "revoked": "Authorization revoked",
    "used": "Departure recorded",
}
KIND_LABELS = dict(EVIDENCE_KINDS)
EQUIPMENT_LABELS = {"restraint_kit": "Restraint kit", "recovery_kit": "Recovery kit", "charger": "Charger"}
MODE_LABELS = {"demo": "Demo approval", "production": "Production"}
DEPART_CONFIRM = (
    'Refused (invalid input): tick "The robot is leaving now" to record the departure. Nothing was recorded.'
)


def equipment_label(name: str) -> str:
    return EQUIPMENT_LABELS.get(name, name.replace("_", " ").capitalize())


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode, mode)


ERROR_KINDS = {
    "conflict": "conflict",
    "invalid": "invalid input",
    "permission_denied": "permission denied",
    "not_authenticated": "not signed in",
}


# ------------------------------------------------------------------ helpers
def _deny(request: HttpRequest) -> HttpResponse | None:
    if not has_permission(request.user, "readiness.read"):
        return HttpResponseForbidden("No readiness access")
    return None


def _base(request: HttpRequest) -> dict[str, Any]:
    demo = demo_mode()
    return {
        "roles": sorted(active_roles(request.user)),
        "demo_mode": demo,
        "demo_banner": DEMO_BANNER if demo else "",
        "can": {a: has_permission(request.user, f"readiness.{a}") for a in ACTIONS},
    }


def _auth_row(a: DepartureAuthorization, tz: str) -> dict[str, Any]:
    st = authorization_status(a)
    state = st["state"]
    reasons = [str(r) for r in st.get("reasons", [])]
    if state == "usable":
        label = f"Approved to leave, valid until {local_dt(a.expires_at)}"
    elif state == "invalidated":
        label = "Authorization invalidated (" + ("; ".join(reasons) or "guard changed") + ")"
    else:
        label = AUTH_LABELS.get(state, f"Authorization {state}")
    return {
        "auth": a,
        "state": state,
        "reasons": reasons,
        "label": label,
        "expires_local": a.expires_at.astimezone(ZoneInfo(tz)),
        "issued_local": a.issued_at.astimezone(ZoneInfo(tz)),
        "revocation": getattr(a, "revocation", None),
        "departure": getattr(a, "departure", None),
        "depart_key": uuid.uuid4().hex,
        "mode_label": mode_label(a.mode),
    }


def _planning(v: Visit) -> tuple[str, str, list[dict[str, Any]]]:
    """(css state, label, blockers) for the planning side, checked now."""
    if v.status == "cancelled":
        return "blocked", "Cancelled", []
    if v.current_revision is None:
        return "review", "Not planned", []
    live = planning_services.live_feasibility(v)
    return live.feasibility, planning_services.LABELS[live.feasibility], list(live.blockers)


def _gates(result: ReadinessResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [{"name": n, "prefixes": p, "blockers": []} for n, p in GATES]
    other: dict[str, Any] = {"name": "Other", "prefixes": (), "blockers": []}
    for b in result.blockers:
        code = str(b.get("code", ""))
        row = next((r for r in rows if code.startswith(r["prefixes"])), other)
        row["blockers"].append(b)
    if other["blockers"]:
        rows.append(other)
    for r in rows:
        levels = {b.get("level") for b in r["blockers"]}
        r["state"] = "blocked" if "blocked" in levels else ("review" if levels else "clear")
    return rows


# What a blocker needs and who may do it (the permission whose role names are shown). Wording of the roles
# comes from accounts.roles; nothing here decides readiness.
BLOCKER_PERMISSION: list[tuple[tuple[str, ...], str]] = [
    (("VISIT_NOT_CONFIRMED",), "planning.confirm"),
    (("PLANNING_", "RESERVATIONS_"), "planning.plan"),
    (("POLICY_MISSING",), "readiness.manage_policy"),
    (("POLICY_",), "readiness.approve_policy"),
    (("INSPECTOR_NOT_QUALIFIED",), "planning.grant_inspector_qualification"),
    (("INSPECTION_TEMPLATE_NOT_APPROVED",), "fleet.approve_inspection_template"),
    (("PERIODIC_INSPECTION_INTERVAL_UNKNOWN",), "fleet.manage_periodic_rule"),
    (("PERIODIC_INSPECTION_",), "fleet.submit_inspection"),
    (("MAINTENANCE_",), "fleet.record_maintenance"),  # v2 02C
    (("BATTERY_MIN_UNKNOWN",), "fleet.publish_capability"),
    (("EVIDENCE_", "BATTERY_", "EQUIPMENT_"), "readiness.record_evidence"),
    (("RECOVERY_",), "readiness.record_recovery_plan"),
    (("ROBOT_NOT_AT_HUB",), "readiness.record_departure"),
]


def blocker_permission(code: str) -> str:
    for prefixes, perm in BLOCKER_PERMISSION:
        if code.startswith(prefixes):
            return perm
    return "readiness.authorize_departure"


def missing_requirements(result: ReadinessResult) -> list[dict[str, str]]:
    """The plain list shown on the visit page: what is missing, what to do, who can do it."""
    out = []
    for b in result.blockers:
        code = str(b.get("code", ""))
        perm = blocker_permission(code)
        out.append(
            {
                "message": str(b.get("message", "")),
                "action": str(b.get("action", "")),
                "who": who_can(perm),
                "permission": perm,
                "code": code,
                "level": str(b.get("level", "")),
            }
        )
    return out


def _planning_from(result: ReadinessResult) -> tuple[str, str, list[dict[str, Any]]]:
    """The planning state read from the evaluator's own planning gate (it runs live_feasibility and reports
    every non-feasible plan as PLANNING_* blockers), so a list does not run the planning check twice."""
    found = [b for b in result.blockers if str(b.get("code", "")).startswith("PLANNING_")]
    levels = {b.get("level", "blocked") for b in found}
    state = "blocked" if "blocked" in levels else ("review" if levels else "feasible")
    return state, planning_services.LABELS[state], found


def _summary(v: Visit, *, live_planning: bool = True) -> dict[str, Any]:
    """Planning label, readiness label and the latest authorization / departure label for one visit.
    ``live_planning=False`` (the list): the planning label comes from the evaluator's planning gate."""
    tz = v.site.timezone
    if v.status == "cancelled" or v.current_revision is None:
        live_planning = True  # _planning's own wording for these
    result = evaluate_readiness(v)
    p_state, p_label, p_blockers = _planning(v) if live_planning else _planning_from(result)
    latest = v.authorizations.order_by("-issued_at", "-pk").first()
    auth = _auth_row(latest, tz) if latest is not None else None
    r = v.current_revision
    return {
        "visit": v,
        "rev": r,
        "start": r.on_site_start.astimezone(ZoneInfo(tz)) if r else None,
        "tz": tz,
        "planning_state": p_state,
        "planning_label": p_label,
        "planning_blockers": p_blockers,
        "result": result,
        "readiness_state": "ready" if result.ready else "incomplete",
        "auth": auth,
    }


def _refused(request: HttpRequest, exc: CommandError) -> None:
    messages.error(request, refused_message(exc, ERROR_KINDS))


def _run(request: HttpRequest, fn: Any, ok: str, **kw: Any) -> bool:
    try:
        fn(request.user, **kw)
    except CommandError as exc:
        _refused(request, exc)
        return False
    messages.success(request, ok)
    return True


def _to_visit(code: str) -> HttpResponse:
    return redirect("readiness:visit", code=code)


def _back_visit(request: HttpRequest, code: str, **kw: Any) -> HttpResponse:
    return _to_visit(code)


def _back_auth(request: HttpRequest, code: str, **kw: Any) -> HttpResponse:
    a = get_object_or_404(DepartureAuthorization.objects.select_related("visit"), code=code)
    return _to_visit(a.visit.code)


def _back_policies(request: HttpRequest, **kw: Any) -> HttpResponse:
    return redirect("readiness:policies")


# ------------------------------------------------------------------ pages
NOT_ASSESSED_OUTSIDE = (
    "Not assessed yet: it starts after the readiness look-ahead. Open the visit to check it now."
)
NOT_ASSESSED_CAP = (
    "Not assessed yet: more visits fall in the look-ahead than this list checks at once. Open the visit to "
    "check it now."
)
NOT_ASSESSED_UNCONFIGURED = (
    "Not assessed yet: the readiness look-ahead is not configured. Open the visit to check it now."
)


@login_required
def index(request: HttpRequest) -> HttpResponse:
    """Every confirmed future visit is listed; the full evaluator (and the live planning check and the
    authorization status) runs only for visits inside the look-ahead (``ops_readiness_lookahead_min``,
    the window Today uses), at most READINESS_CAP of them, in (on-site start, visit) order, exactly like
    Today's timeline. The others say "Not assessed yet" and why, never a ready or approved state. The visit's
    own page always runs the full evaluator; nothing here grants or decides anything (01D release fix)."""
    if (deny := _deny(request)) is not None:
        return deny
    from operations.readmodels import READINESS_CAP, READINESS_LOOKAHEAD, read_thresholds  # noqa: PLC0415

    now = timezone.now()
    # v2 03A-3 (I-8): the viewer's hubs only (regional = all), BEFORE the cap, so the cap and the
    # "not assessed" notes describe the visits this viewer can open.
    visits = list(
        hub_scope(request)
        .filter(Visit.objects.select_related("site", "current_revision__robot"))
        .filter(status="confirmed", current_revision__on_site_end__gt=now)
        .order_by("current_revision__on_site_start", "pk")
    )
    lookahead = read_thresholds()[READINESS_LOOKAHEAD]
    try:
        lookahead_end = now + timedelta(minutes=lookahead) if lookahead is not None else None
    except OverflowError:
        lookahead_end = datetime.max.replace(tzinfo=now.tzinfo)
    qualifying = [
        v
        for v in visits
        if lookahead_end is not None
        and v.current_revision is not None
        and v.current_revision.on_site_start <= lookahead_end
    ]
    chosen = {v.pk for v in qualifying[:READINESS_CAP]}
    qualifying_ids = {v.pk for v in qualifying}
    rows: list[dict[str, Any]] = []
    for v in visits:
        if v.pk in chosen:
            rows.append({**_summary(v, live_planning=False), "assessed": True})
            continue
        r = v.current_revision
        if lookahead_end is None:
            why = NOT_ASSESSED_UNCONFIGURED
        elif v.pk in qualifying_ids:
            why = NOT_ASSESSED_CAP
        else:
            why = NOT_ASSESSED_OUTSIDE
        rows.append(
            {
                "visit": v,
                "rev": r,
                "start": r.on_site_start if r else None,
                "assessed": False,
                "why": why,
            }
        )
    counts = {"all": len(visits), "assessed": len(chosen), "not_assessed": len(visits) - len(chosen)}
    return render(
        request,
        "readiness/index.html",
        {"rows": rows, "counts": counts, "cap": READINESS_CAP, **_base(request)},
    )


INSPECTOR_NOTE = (
    "Passing pre-departure evidence needs both inspection signers to be qualified readiness inspectors at "
    "the "
    "time of the inspection (and not revoked since). A failing result is always recorded."
)
UNUSABLE_TEMPLATE_WARNING = (
    "This inspection is recorded, but it cannot be used as passing pre-departure evidence in production: its "
    "template is not approved for pre-departure use."
)


def inspection_warnings(inspection_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Slice 7 (X-8, X-1), Engineer A's SQL rules, one statement: per inspection the signer problem
    (planning_inspection_signer_problem; None = both signers covered) and, in production, whether its
    template is
    usable for pre-departure evidence (fleet_template_usable). Warnings only: nothing is refused here."""
    if not inspection_ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.code, planning_inspection_signer_problem(i.id), fleet_production_mode(),
                   fleet_template_usable(i.template_id, 'pre_departure', fleet_production_mode()), t.version
            FROM fleet_inspection i JOIN fleet_inspectiontemplate t ON t.id = i.template_id
            WHERE i.id = ANY(%s)
            """,
            [inspection_ids],
        )
        rows = cur.fetchall()
    out: dict[int, dict[str, Any]] = {}
    for iid, icode, signer_problem, production, usable, version in rows:
        warnings = []
        if signer_problem:
            warnings.append(f"Signer qualification: {signer_problem}")
        if production and not usable:
            warnings.append(f"Template {version}: {UNUSABLE_TEMPLATE_WARNING}")
        out[iid] = {"code": icode, "signer_problem": signer_problem, "warnings": warnings}
    return out


def _robot_inspections(v: Visit) -> list[Inspection]:
    r = v.current_revision
    if r is None or r.robot_id is None:
        return []
    return list(Inspection.objects.filter(robot_id=r.robot_id).order_by("-id")[:10])


def usable_inspections(v: Visit) -> list[dict[str, Any]]:
    """The inspections this visit's pre-departure evidence can use, by the rule readiness.record_evidence
    applies (01D J4-1): same robot, the robot's CURRENT configuration, recorded for THIS visit. Each row says
    whether it can count as PASSING evidence (a complete PASS, both signers qualified, and in production a
    template approved for pre-departure use); one that cannot is offered as a fail only. The server remains
    the authority; this list only stops offering what it would refuse."""
    r = v.current_revision
    if r is None or r.robot_id is None:
        return []
    robot = Robot.objects.filter(pk=r.robot_id).only("current_manifest_id").first()
    if robot is None or robot.current_manifest_id is None:
        return []
    rows = list(
        Inspection.objects.filter(
            robot_id=r.robot_id, manifest_id=robot.current_manifest_id, visit_ref=v.code
        ).order_by("-id")[:10]
    )
    checks = inspection_warnings([i.pk for i in rows])
    out = []
    for i in rows:
        problems = list(checks.get(i.pk, {}).get("warnings", []))
        if not (i.result == "PASS" and i.complete):
            problems.insert(0, f"it is {i.get_result_display().lower()}, not a complete pass")
        out.append({"i": i, "passing": not problems, "problems": problems})
    return out


@login_required
def visit_detail(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    v = get_object_or_404(Visit.objects.select_related("site", "current_revision"), code=code)
    if (deny := other_hub_refusal(request, v.hub_id)) is not None:  # v2 03A-3: another hub's visit
        return deny
    tz = v.site.timezone
    summary = _summary(v)
    evidence = (
        ReadinessEvidence.objects.filter(visit=v)
        .select_related("revision", "inspection", "recorded_by", "void", "void__voided_by")
        .order_by("-recorded_at", "-pk")
    )
    plans = (
        RecoveryPlan.objects.filter(visit=v)
        .select_related("revision", "on_call_engineer", "escalation_contact", "recorded_by")
        .order_by("-recorded_at", "-pk")
    )
    auths = [
        _auth_row(a, tz)
        for a in v.authorizations.select_related(
            "policy", "issued_by", "revision", "robot", "revocation", "revocation__revoked_by", "departure"
        ).order_by("-issued_at", "-pk")
    ]
    departures = (
        DepartureEvent.objects.filter(visit=v)
        .select_related("authorization", "recorded_by", "robot")
        .order_by("-departed_at", "-pk")
    )
    policy = rx.current_policy()
    equipment: list[str] = list(policy.requirements.get("required_equipment", [])) if policy else []
    latest_plan = plans.first()
    if latest_plan is not None:
        equipment += [e for e in latest_plan.required_equipment if e not in equipment]

    history: list[dict[str, Any]] = []
    for e in evidence:
        kind_label = KIND_LABELS.get(e.kind, e.kind)
        history.append({"at": e.recorded_at, "what": f"{kind_label} recorded: {e.result} ({e.code})"})
        void = getattr(e, "void", None)
        if void is not None:
            history.append({"at": void.voided_at, "what": f"Evidence {e.code} voided: {void.reason}"})
    for p in plans:
        history.append({"at": p.recorded_at, "what": f"Recovery plan {p.pk} recorded"})
    for row in auths:
        a = row["auth"]
        history.append({"at": a.issued_at, "what": f"Authorization {a.code} issued ({mode_label(a.mode)})"})
        if row["revocation"] is not None:
            history.append(
                {
                    "at": row["revocation"].revoked_at,
                    "what": f"Authorization {a.code} revoked: {row['revocation'].reason}",
                }
            )
    for d in departures:
        history.append({"at": d.departed_at, "what": f"Departure recorded on {d.authorization.code}"})
    history.sort(key=lambda h: h["at"], reverse=True)
    warn = inspection_warnings([e.inspection_id for e in evidence if e.inspection_id is not None])
    for h in history:
        h["at_local"] = h["at"].astimezone(ZoneInfo(tz))

    return render(
        request,
        "readiness/visit.html",
        {
            **summary,
            "gates": _gates(summary["result"]),
            "evidence": [
                {
                    "ev": e,
                    "void": getattr(e, "void", None),
                    "observed_local": e.observed_at.astimezone(ZoneInfo(tz)),
                    "current": v.current_revision_id == e.revision_id,
                    "warnings": warn.get(e.inspection_id, {}).get("warnings", []) if e.inspection_id else [],
                }
                for e in evidence
            ],
            "inspector_note": INSPECTOR_NOTE,
            "plans": [
                {
                    "plan": p,
                    "local": p.recorded_at.astimezone(ZoneInfo(tz)),
                    "current": p.revision_id == v.current_revision_id,
                }
                for p in plans
            ],
            "auths": auths,
            "departures": [{"d": d, "local": d.departed_at.astimezone(ZoneInfo(tz))} for d in departures],
            "history": history,
            "kinds": EVIDENCE_KINDS,
            "equipment": equipment,
            "persons": Person.objects.select_related("user").order_by("code"),
            "authorize_key": uuid.uuid4().hex,
            "missing": missing_requirements(summary["result"]),
            "robot_inspections": _robot_inspections(v),
            "usable_inspections": (usable := usable_inspections(v)),
            "preselect": request.GET.get("inspection", ""),
            "next_evidence": request.GET.get("inspection", "") in {u["i"].code for u in usable},
            "equipment_rows": [{"name": n, "label": equipment_label(n)} for n in equipment],
            "departed": departures.first(),
            "departed_run": ExecutionRun.objects.filter(visit=v).only("code").first(),
            "who_inspect": refusal_text("fleet.submit_inspection"),
            "who_authorize": who_can("readiness.authorize_departure"),
            "valid_auth": next((a for a in auths if a["state"] == "usable"), None),
            **_base(request),
        },
    )


# ------------------------------------------------------------------ visit actions (POST)
def _equipment_items(post: Any, names: list[str]) -> dict[str, str]:
    items: dict[str, str] = {}
    for name in names:
        state = post.get(f"item_{name}", "")
        if state:
            items[name] = state
    extra = post.get("extra_items", "").strip()
    if extra:
        for part in extra.replace("\n", ",").split(","):
            if not part.strip():
                continue
            name, sep, state = part.partition("=")
            if not sep:
                raise Invalid(
                    f"Extra equipment item {part.strip()!r} must look like name=present or name=missing"
                )
            items[name.strip()] = state.strip()
    return items


@login_required
@require_POST
@one_time_form(_back_visit)
def record_evidence_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    v = get_object_or_404(Visit.objects.select_related("site"), code=code)
    kind = f.get("kind", "")
    kw: dict[str, Any] = {"visit_code": code, "kind": kind, "result": f.get("result", "")}
    try:
        if kind == "pre_departure_inspection":
            kw["inspection_code"] = f.get("inspection_code", "").strip() or None
        elif kind == "battery_check":
            raw = f.get("soc_pct", "").strip()
            try:
                kw["details"] = {"soc_pct": int(raw)}
            except ValueError:
                raise Invalid("Battery % must be a whole number from 1 to 100") from None
        elif kind == "equipment_check":
            names = [n for n in f.getlist("item_names") if n]
            kw["details"] = {"items": _equipment_items(f, names)}
        if f.get("observed_at"):
            try:
                naive = datetime.fromisoformat(f["observed_at"])
            except ValueError:
                raise Invalid("Observed at must be a date and time") from None
            kw["observed_at"] = planning_services.local_to_utc(naive, v.site.timezone)
    except CommandError as exc:
        _refused(request, exc)
        return _to_visit(code)
    label = KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())
    _run(request, rx.record_evidence, f"{label} recorded", **kw)
    return _to_visit(code)


@login_required
@require_POST
@one_time_form(_back_visit)
def void_evidence_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    ev_code = f.get("evidence", "")
    if not ReadinessEvidence.objects.filter(code=ev_code, visit__code=code).exists():
        messages.error(request, f"Refused (invalid input): no evidence {ev_code} on visit {code}")
        return _to_visit(code)
    _run(
        request,
        rx.void_evidence,
        f"Evidence {ev_code} voided",
        evidence_code=ev_code,
        reason=f.get("reason", ""),
    )
    return _to_visit(code)


@login_required
@require_POST
@one_time_form(_back_visit)
def recovery_plan_view(request: HttpRequest, code: str) -> HttpResponse:
    f = request.POST
    equipment = [
        e.strip() for e in f.get("required_equipment", "").replace("\n", ",").split(",") if e.strip()
    ]
    _run(
        request,
        rx.record_recovery_plan,
        "Recovery plan recorded",
        visit_code=code,
        on_call_code=f.get("on_call", ""),
        escalation_code=f.get("escalation", ""),
        escalation_path=f.get("escalation_path", ""),
        required_equipment=equipment,
    )
    return _to_visit(code)


@login_required
@require_POST
@one_time_form(_back_visit)
def authorize_view(request: HttpRequest, code: str) -> HttpResponse:
    try:
        out = rc.authorize_departure(
            request.user, visit_code=code, idempotency_key=request.POST.get("idempotency_key") or None
        )
    except CommandError as exc:
        _refused(request, exc)
    else:
        auth = DepartureAuthorization.objects.select_related("issued_by", "robot", "visit").get(
            code=out.get("entity_id", "")
        )
        # Never green: an approval is a time-limited decision, shown as information.
        messages.info(
            request,
            f"Approved to leave by {auth.issued_by.username} at {local_dt(auth.issued_at)}, valid until "
            f"{local_dt(auth.expires_at)}, for visit {auth.visit.code} with robot {auth.robot.code}. "
            "This is a time-limited decision; record the departure separately when the robot "
            "actually leaves.",
        )
    return _to_visit(code)


@login_required
@require_POST
@one_time_form(_back_auth)
def depart_view(request: HttpRequest, code: str) -> HttpResponse:
    a = get_object_or_404(DepartureAuthorization.objects.select_related("visit"), code=code)
    # 01D J5-4: a confirm tick. Without it nothing runs (a person without the permission still gets the
    # command's own permission refusal, so the audit log records the denied attempt as before).
    if request.POST.get("confirm") != "1" and has_permission(request.user, "readiness.record_departure"):
        messages.error(request, DEPART_CONFIRM)
        return _to_visit(a.visit.code)
    _run(
        request,
        rc.record_departure,
        f"Departure recorded on authorization {code}",
        authorization_code=code,
        idempotency_key=request.POST.get("idempotency_key") or None,
    )
    return _to_visit(a.visit.code)


@login_required
@require_POST
@one_time_form(_back_auth)
def revoke_view(request: HttpRequest, code: str) -> HttpResponse:
    a = get_object_or_404(DepartureAuthorization.objects.select_related("visit"), code=code)
    _run(
        request,
        rc.revoke_authorization,
        f"Authorization {code} revoked",
        authorization_code=code,
        reason=request.POST.get("reason", ""),
    )
    return _to_visit(a.visit.code)


# ------------------------------------------------------------------ policies
EXAMPLE_REQUIREMENTS = {
    "authorization_ttl_min": 90,
    "evidence": [
        {"kind": "pre_departure_inspection", "max_age_min": 240},
        {"kind": "equipment_check", "max_age_min": 240},
        {"kind": "battery_check", "max_age_min": 120},
    ],
    "required_equipment": ["restraint_kit", "recovery_kit", "charger"],
    "recovery_plan_required": True,
}


@login_required
def policies(request: HttpRequest) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    current = rx.current_policy()
    rows = []
    for p in ReadinessPolicy.objects.select_related("created_by").order_by("-version"):
        status = rx.policy_status(p)
        approvals = {a.slot: a for a in p.approvals.select_related("approver")}
        withdrawal = getattr(p, "withdrawal", None)
        rows.append(
            {
                "policy": p,
                "status": status,
                "is_current": current is not None and current.pk == p.pk,
                "synthetic": p.source == "synthetic_demo",
                "approvals": [
                    {"slot": s, "label": lbl, "approval": approvals.get(s)} for s, lbl in POLICY_SLOTS
                ],
                "withdrawal": withdrawal,
                "requirements": json.dumps(p.requirements, indent=2, sort_keys=True),
            }
        )
    return render(
        request,
        "readiness/policies.html",
        {
            "rows": rows,
            "current": current,
            "sources": ReadinessPolicy.SOURCES,
            "example": request.session.pop("readiness_policy_draft", None)
            or json.dumps(EXAMPLE_REQUIREMENTS, indent=2),
            **_base(request),
        },
    )


def _to_policies() -> HttpResponse:
    return redirect("readiness:policies")


@login_required
@require_POST
@one_time_form(_back_policies)
def policy_create_view(request: HttpRequest) -> HttpResponse:
    f = request.POST
    raw = f.get("requirements", "")
    try:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Invalid(f"Requirements are not valid JSON: {exc.msg} (line {exc.lineno})") from None
        rx.validate_requirements(req)
    except CommandError as exc:
        _refused(request, exc)
        request.session["readiness_policy_draft"] = raw
        return _to_policies()
    try:
        out = rx.create_readiness_policy(
            request.user, requirements=req, source=f.get("source", ""), notes=f.get("notes", "")
        )
    except CommandError as exc:
        _refused(request, exc)
        request.session["readiness_policy_draft"] = raw
    else:
        messages.success(request, f"Policy v{out['entity_id']} created as a DRAFT; it needs both approvals")
    return _to_policies()


@login_required
@require_POST
@one_time_form(_back_policies)
def policy_approve_view(request: HttpRequest, version: int) -> HttpResponse:
    slot = request.POST.get("slot", "")
    _run(
        request,
        rx.approve_readiness_policy,
        f"Policy v{version}: {slot} approval recorded",
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
        rx.withdraw_readiness_policy,
        f"Policy v{version} withdrawn",
        version=version,
        reason=request.POST.get("reason", ""),
    )
    return _to_policies()


# ------------------------------------------------------------------ submit an inspection (01C.5)
QUALIFICATION_NOTE = (
    "Only people who are qualified inspectors can sign an inspection. Only {who} can confirm someone's "
    "inspector qualification."
)


def _inspection_templates(robot: Robot) -> list[dict[str, Any]]:
    """Templates for the robot's configuration, newest first, each marked usable or not for pre-departure
    evidence by the existing SQL rule (a template that is not usable can still be recorded against)."""
    m = robot.current_manifest
    if m is None:
        return []
    rows = list(InspectionTemplate.objects.filter(pattern_id=m.pattern_id).order_by("-id"))
    usable: dict[int, bool] = {}
    if rows:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT t.id, fleet_template_usable(t.id, 'pre_departure', fleet_production_mode()) "
                "FROM fleet_inspectiontemplate t WHERE t.id = ANY(%s)",
                [[t.pk for t in rows]],
            )
            usable = {tid: bool(u) for tid, u in cur.fetchall()}
    return [{"t": t, "usable": usable.get(t.pk, False)} for t in rows]


POST_VISIT_NEXT = "Post-visit inspection recorded. Next: the ops lead reconciles the return."


def _inspection_page(
    request: HttpRequest,
    robot: Robot,
    *,
    visit: Visit | None,
    back_url: str,
    run_code: str | None = None,
) -> HttpResponse:
    """``run_code``: the visit has departed, so this is its POST-visit inspection (01D P2-1): back to the run
    with the reconciliation as the next step, never the pre-departure evidence prompt."""
    rows = _inspection_templates(robot)
    wanted = request.POST.get("template_version") or request.GET.get("template") or ""
    chosen = next((r for r in rows if r["t"].version == wanted), rows[0] if rows else None)
    template = chosen["t"] if chosen else None

    def call(actor: Any, d: dict[str, Any]) -> dict[str, Any]:
        form_answers = {k[5:]: v for k, v in d.items() if k.startswith("item_") and v}
        try:
            return fleet_services.submit_inspection(
                actor,
                robot_code=robot.code,
                template_version=d["template_version"],
                answers=form_answers,
                performer=d.get("performer") or None,
                verifier=d.get("verifier") or None,
                visit_ref=visit.code if visit else "",
            )
        except Invalid as exc:
            if "not qualified to sign inspections" in str(exc):
                raise Invalid(
                    f"{exc}. "
                    + QUALIFICATION_NOTE.format(who=who_can("planning.grant_inspector_qualification"))
                ) from None
            raise

    def ok(out: dict[str, Any]) -> str:
        result = str(out.get("result", ""))
        word = {"PASS": "passed", "FAIL": "failed", "INCOMPLETE": "is incomplete"}.get(result, "recorded")
        text = f"Inspection {out.get('entity_id', '')} recorded: it {word}."
        if run_code:
            return f"{text} {POST_VISIT_NEXT}"
        return text + (" Next: record it as pre-departure evidence below." if visit else "")

    def success(out: dict[str, Any]) -> str:
        # 01D J4-2: back to the visit with the new inspection preselected in the evidence form
        if visit is None or run_code:
            return back_url
        return f"{back_url}?inspection={out.get('entity_id', '')}#evidence"

    return command_page(
        request,
        permission="fleet.submit_inspection",
        form_class=InspectionForm,
        template="readiness/inspection_new.html",
        call=call,
        ok=ok,
        success_url=success,
        form_kwargs={"template": template},
        initial={"template_version": template.version} if template else None,
        context={
            "robot": robot,
            "visit": visit,
            "templates": rows,
            "chosen": chosen,
            "back_url": back_url,
            "base_path": request.path,
            "post_visit_run": run_code,
        },
    )


@login_required
def visit_inspection(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    v = get_object_or_404(Visit.objects.select_related("current_revision__robot"), code=code)
    if (deny := other_hub_refusal(request, v.hub_id)) is not None:  # v2 03A-3: another hub's visit
        return deny
    rev = v.current_revision
    if rev is None or rev.robot is None:
        messages.error(
            request, "This visit has no robot yet, so there is nothing to inspect. Plan the visit first."
        )
        return _to_visit(code)
    run = ExecutionRun.objects.filter(visit=v).only("code").first()
    if run is not None:  # departed: the post-visit inspection, back to the run (01D P2-1)
        return _inspection_page(
            request, rev.robot, visit=v, back_url=f"/execution/runs/{run.code}/", run_code=run.code
        )
    return _inspection_page(request, rev.robot, visit=v, back_url=f"/readiness/visits/{code}/")


@login_required
def robot_inspection(request: HttpRequest, code: str) -> HttpResponse:
    if not has_permission(request.user, "fleet.read"):
        return HttpResponseForbidden("No fleet access")
    robot = get_object_or_404(Robot.objects.select_related("current_manifest"), code=code)
    if (deny := other_hub_refusal(request, robot.hub_id)) is not None:  # v2 03A-3: another hub's robot
        return deny
    return _inspection_page(request, robot, visit=None, back_url=f"/fleet/{code}/")
