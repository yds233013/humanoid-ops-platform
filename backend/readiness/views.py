"""Server-rendered readiness and departure-authorization UI (Engineer D). Contract: docs/03, s4.

Views only READ models and call the contract commands; every write goes through readiness.evidence
(Engineer A) or readiness.commands (Engineer C), which enforce permissions, locks and invariants. Buttons are
hidden for users who lack a permission, but the commands are the real gate.

SYNTHETIC data only. Nothing here grants real-world clearance, and no page ever says a robot may go.
"""

import json
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import active_roles, has_permission
from fleet.services import demo_mode
from planning import services as planning_services
from planning.models import Person, Visit
from platform_core.commands import CommandError, Invalid
from readiness import commands as rc
from readiness import evidence as rx
from readiness.authorization import authorization_status
from readiness.evaluator import ReadinessResult, evaluate_readiness
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


def _hhmm(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%H:%M")


def _auth_row(a: DepartureAuthorization, tz: str) -> dict[str, Any]:
    st = authorization_status(a)
    state = st["state"]
    reasons = [str(r) for r in st.get("reasons", [])]
    if state == "usable":
        label = f"Authorization issued (expires {_hhmm(a.expires_at, tz)})"
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


def _summary(v: Visit) -> dict[str, Any]:
    """Planning label, readiness label and the latest authorization / departure label for one visit."""
    tz = v.site.timezone
    p_state, p_label, p_blockers = _planning(v)
    result = evaluate_readiness(v)
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
    kind = ERROR_KINDS.get(exc.code, exc.code.replace("_", " "))
    messages.error(request, f"Refused ({kind}): {exc}")


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


# ------------------------------------------------------------------ pages
@login_required
def index(request: HttpRequest) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    visits = (
        Visit.objects.select_related("site", "current_revision")
        .filter(status="confirmed", current_revision__on_site_end__gt=timezone.now())
        .order_by("current_revision__on_site_start", "code")
    )
    return render(
        request,
        "readiness/index.html",
        {"rows": [_summary(v) for v in visits], **_base(request)},
    )


@login_required
def visit_detail(request: HttpRequest, code: str) -> HttpResponse:
    if (deny := _deny(request)) is not None:
        return deny
    v = get_object_or_404(Visit.objects.select_related("site", "current_revision"), code=code)
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
            "policy", "issued_by", "revision", "revocation", "revocation__revoked_by", "departure"
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
        history.append({"at": e.recorded_at, "what": f"Evidence {e.code} recorded ({e.kind}: {e.result})"})
        void = getattr(e, "void", None)
        if void is not None:
            history.append({"at": void.voided_at, "what": f"Evidence {e.code} voided: {void.reason}"})
    for p in plans:
        history.append({"at": p.recorded_at, "what": f"Recovery plan {p.pk} recorded"})
    for row in auths:
        a = row["auth"]
        history.append({"at": a.issued_at, "what": f"Authorization {a.code} issued ({a.mode})"})
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
                }
                for e in evidence
            ],
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
            "persons": Person.objects.order_by("code"),
            "authorize_key": uuid.uuid4().hex,
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
    _run(request, rx.record_evidence, f"Evidence recorded ({kind})", **kw)
    return _to_visit(code)


@login_required
@require_POST
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
def authorize_view(request: HttpRequest, code: str) -> HttpResponse:
    try:
        out = rc.authorize_departure(
            request.user, visit_code=code, idempotency_key=request.POST.get("idempotency_key") or None
        )
    except CommandError as exc:
        _refused(request, exc)
    else:
        messages.success(
            request,
            f"Authorization {out.get('entity_id', '')} issued. This is a time-limited decision; "
            "record the departure separately when the robot actually leaves.",
        )
    return _to_visit(code)


@login_required
@require_POST
def depart_view(request: HttpRequest, code: str) -> HttpResponse:
    a = get_object_or_404(DepartureAuthorization.objects.select_related("visit"), code=code)
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
def policy_withdraw_view(request: HttpRequest, version: int) -> HttpResponse:
    _run(
        request,
        rx.withdraw_readiness_policy,
        f"Policy v{version} withdrawn",
        version=version,
        reason=request.POST.get("reason", ""),
    )
    return _to_policies()
