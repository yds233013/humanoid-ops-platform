"""Minimal server-rendered fleet interface. Every action posts to the same commands the API uses.

Slice 7 (Engineer C, docs/07a sections 3.6, 4.6, 5.6): pattern pages (inspection templates with their
usability
per purpose, periodic inspection rule history), the hold-policy lifecycle page, the periodic inspection
state of
each robot, and each hold's release basis (policy fixed at placement, re-basing). Usability and policy
states are
read from Engineer A's SQL functions (the authoritative rules). The Slice 7 commands are Engineer B's
(fleet.services); each is looked up when the form is posted, so a page works in a build that does not have it
yet (the form then says so and records nothing). The commands enforce permissions, locks and invariants; the
views also refuse a POST from a role without the permission. Nothing here says a robot is cleared, safe
or ready.
"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import active_roles, has_permission
from fleet import maintenance as mnt
from fleet import services as s
from fleet.models import (
    APPROVAL_SLOTS,
    HOLD_POLICY_SOURCES,
    TEMPLATE_PURPOSES,
    TEMPLATE_SOURCES,
    Hold,
    HoldPolicy,
    HoldRebase,
    Inspection,
    InspectionTemplate,
    MaintenanceRecord,
    ManifestPattern,
    PeriodicInspectionRule,
    Robot,
)
from hub.charging import robot_battery_panel
from platform_core.commands import CommandError
from platform_core.interaction import spend_form_token
from platform_core.timezones import workspace_zone

SYNTHETIC_INTERVAL = "SYNTHETIC demo interval, not an engineering value"
UNUSABLE_TEMPLATE_WARNING = (
    "This inspection will be recorded, but it cannot be used as passing pre-departure evidence."
)
TEMPLATE_STATES = {
    "approved": "approved",
    "synthetic": "synthetic, demo only",
    "awaiting": "awaiting approval",
    "withdrawn": "withdrawn",
}
HOLD_POLICY_SLOTS = [
    ("engineering", "Engineering lead"),
    ("safety", "Safety reviewer"),
    ("founder", "Founder"),
]
EXAMPLE_POLICY_RULES = {
    src: {"slots": [{"slot": "engineering", "roles": ["engineering_lead"]}]} for src, _ in Hold.SOURCES
}
FLEET_ACTIONS = [
    "manage_hold_policy",
    "approve_hold_policy",
    "withdraw_hold_policy",
    "rebase_hold",
    "manage_inspection_template",
    "approve_inspection_template",
    "withdraw_inspection_template",
    "manage_periodic_rule",
    "approve_periodic_rule",
    "withdraw_periodic_rule",
    "place_hold",
    "approve_release",
    "release_hold",
    "check_eligibility",
    # v2 02C maintenance
    "propose_maintenance_rule",
    "approve_maintenance_rule",
    "withdraw_maintenance_rule",
    "record_maintenance",
]


def _banner() -> dict[str, Any]:
    demo = s.demo_mode()
    policy = s.current_policy() if Hold.objects.exists() or s.HoldPolicy.objects.exists() else None
    return {
        "demo_mode": demo,
        # The base banner says releases under a draft are allowed because this is a demo: never in production
        # (G-01, allow_draft_policies has no effect there).
        "policy_draft": demo and policy is not None and s.policy_status(policy) == "draft",
    }


def _can(request: HttpRequest) -> dict[str, bool]:
    return {a: has_permission(request.user, f"fleet.{a}") for a in FLEET_ACTIONS}


def _command(name: str) -> Callable[..., dict[str, Any]] | None:
    """A Slice 7 fleet.services command (Engineer B) when present in this build; None otherwise."""
    found = getattr(s, name, None)
    return found if callable(found) else None


def _production() -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT fleet_production_mode()")
        row = cur.fetchone()
    return bool(row[0]) if row else True


# ------------------------------------------------------------------ Slice 7 read helpers (A's SQL functions)
def periodic_status(robot_ids: list[int]) -> dict[int, dict[str, Any]]:
    """readiness_periodic_inspection_status per robot, in the current mode, at database time (one "
    "statement)."""
    if not robot_ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, ps.state, ps.due_at, ps.inspection_id, i.code, pr.version_no, pr.source,
                   pr.interval_min
            FROM fleet_robot r
            CROSS JOIN LATERAL readiness_periodic_inspection_status(r.id, fleet_production_mode(),
                                                                    clock_timestamp()) ps
            LEFT JOIN fleet_inspection i ON i.id = ps.inspection_id
            LEFT JOIN fleet_periodicinspectionrule pr ON pr.id = ps.rule_id
            WHERE r.id = ANY(%s)
            """,
            [robot_ids],
        )
        rows = cur.fetchall()
    return {
        rid: {
            "state": state,
            "due_at": due_at,
            "inspection": icode,
            "rule_version": version,
            "rule_source": source,
            "rule_interval_min": interval,
            "synthetic": source == "synthetic_demo",
        }
        for rid, state, due_at, _iid, icode, version, source, interval in rows
    }


def template_rows(pattern: ManifestPattern, production: bool) -> list[dict[str, Any]]:
    """Every template of the pattern with its state per purpose and fleet_template_usable in this mode."""
    templates = list(
        InspectionTemplate.objects.filter(pattern=pattern)
        .select_related("created_by", "withdrawal", "withdrawal__withdrawn_by")
        .prefetch_related("approvals__approver")
        .order_by("id")
    )
    usable: dict[tuple[int, str], bool] = {}
    if templates:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT t.id, p.purpose, fleet_template_usable(t.id, p.purpose, %s)
                FROM fleet_inspectiontemplate t CROSS JOIN (VALUES ('pre_departure'), ('periodic')) p(purpose)
                WHERE t.id = ANY(%s)
                """,
                [production, [t.pk for t in templates]],
            )
            usable = {(tid, purpose): bool(ok) for tid, purpose, ok in cur.fetchall()}
    out = []
    for t in templates:
        withdrawal = getattr(t, "withdrawal", None)
        approvals = list(t.approvals.all())
        purposes = []
        for purpose, label in TEMPLATE_PURPOSES:
            mine = {a.slot: a for a in approvals if a.purpose == purpose}
            if withdrawal is not None:
                state = "withdrawn"
            elif {"engineering", "safety"} <= set(mine):
                state = "approved"
            elif t.source == "synthetic_demo":
                state = "synthetic"
            else:
                state = "awaiting"
            purposes.append(
                {
                    "purpose": purpose,
                    "label": label,
                    "state": state,
                    "state_text": TEMPLATE_STATES[state],
                    "usable_now": usable.get((t.pk, purpose), False),
                    "slots": [
                        {"slot": slot, "label": slot_label, "approval": mine.get(slot)}
                        for slot, slot_label in APPROVAL_SLOTS
                    ],
                }
            )
        out.append(
            {
                "template": t,
                "withdrawal": withdrawal,
                "purposes": purposes,
                "approvable": withdrawal is None
                and t.source != "synthetic_demo"
                and t.created_by_id is not None,
                "legacy": t.created_by_id is None,
                "warning": None
                if usable.get((t.pk, "pre_departure"), False)
                else (UNUSABLE_TEMPLATE_WARNING if production else None),
            }
        )
    return out


def periodic_rule_rows(pattern: ManifestPattern, production: bool) -> list[dict[str, Any]]:
    rules = list(
        PeriodicInspectionRule.objects.filter(pattern=pattern)
        .select_related("created_by", "withdrawal", "withdrawal__withdrawn_by")
        .prefetch_related("approvals__approver")
        .order_by("-version_no")
    )
    with connection.cursor() as cur:
        cur.execute(
            "SELECT fleet_active_periodic_rule_id(%s), fleet_usable_periodic_rule_id(%s, %s)",
            [pattern.pk, pattern.pk, production],
        )
        row = cur.fetchone()
    active_id, usable_id = (row[0], row[1]) if row else (None, None)
    out = []
    for r in rules:
        approvals = {a.slot: a for a in r.approvals.all()}
        withdrawal = getattr(r, "withdrawal", None)
        approved = {"engineering", "safety"} <= set(approvals)
        out.append(
            {
                "rule": r,
                "withdrawal": withdrawal,
                "status": "withdrawn" if withdrawal else ("approved" if approved else "awaiting approval"),
                "active": r.pk == active_id,
                "usable_now": r.pk == usable_id,
                "synthetic": r.source == "synthetic_demo",
                "slots": [
                    {"slot": slot, "label": label, "approval": approvals.get(slot)}
                    for slot, label in APPROVAL_SLOTS
                ],
            }
        )
    return out


def hold_basis(holds: list[Hold]) -> dict[int, dict[str, Any]]:
    """Per hold: the effective rule (placement or latest effective re-basing), its policy state, production
    usability and the database's release problem (fleet_hold_release_problem, NULL = no problem found now)."""
    if not holds:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT h.id, e.policy_id, e.rebase_id, p.version, p.source,
                   fleet_hold_policy_approved(e.policy_id), fleet_hold_policy_withdrawn(e.policy_id),
                   fleet_policy_production_usable(e.policy_id),
                   CASE WHEN h.released_at IS NULL THEN fleet_hold_release_problem(h) END,
                   rb.code
            FROM fleet_hold h
            CROSS JOIN LATERAL (SELECT * FROM fleet_hold_effective_rule(h) LIMIT 1) e
            LEFT JOIN fleet_holdpolicy p ON p.id = e.policy_id
            LEFT JOIN fleet_holdrebase rb ON rb.id = e.rebase_id
            WHERE h.id = ANY(%s)
            """,
            [[h.pk for h in holds]],
        )
        rows = cur.fetchall()
    out: dict[int, dict[str, Any]] = {}
    for hid, pid, rebase_id, version, source, approved, withdrawn, prod_usable, problem, rebase_code in rows:
        if pid is None:
            state = "none"
        elif withdrawn:
            state = "withdrawn"
        elif approved and source == "approved_procedure":
            state = "approved procedure"
        elif approved:
            state = "synthetic approved"
        else:
            state = "synthetic draft" if source == "synthetic_demo" else "draft"
        out[hid] = {
            "policy_id": pid,
            "policy_version": version,
            "policy_state": state,
            "origin": "rebase" if rebase_id else "placement",
            "rebase_code": rebase_code,
            "production_usable": bool(prod_usable),
            "problem": problem,
        }
    return out


def rebase_rows(hold: Hold, basis: dict[str, Any] | None) -> list[dict[str, Any]]:
    rebases = list(
        HoldRebase.objects.filter(hold=hold)
        .select_related("to_policy", "from_policy", "requested_by", "based_on_rebase")
        .prefetch_related("approvals__approver")
        .order_by("id")
    )
    if not rebases:
        return []
    with connection.cursor() as cur:
        cur.execute("SELECT fleet_active_hold_policy_id()")
        row = cur.fetchone()
    active_id = row[0] if row else None
    effective_rebase_id = None
    if basis and basis.get("origin") == "rebase":
        effective_rebase_id = (
            HoldRebase.objects.filter(code=basis["rebase_code"]).values_list("pk", flat=True).first()
        )
    out = []
    for rb in rebases:
        approvals = {a.slot: a for a in rb.approvals.all()}
        if hasattr(rb, "activation"):
            status = "effective"
        elif hold.released_at is not None:
            status = "not effective (hold released)"
        elif rb.based_on_rebase_id != effective_rebase_id:
            status = "superseded (another re-basing became effective first)"
        elif rb.to_policy_id != active_id:
            status = "destination no longer the active policy"
        else:
            status = f"pending ({len(approvals)} of 2 approvals)"
        out.append(
            {
                "rebase": rb,
                "status": status,
                "pending": status.startswith("pending"),
                "slots": [
                    {"slot": slot, "label": label, "approval": approvals.get(slot)}
                    for slot, label in APPROVAL_SLOTS
                ],
            }
        )
    return out


@login_required
def fleet_list(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, "fleet.read"):
        return HttpResponseForbidden("No fleet access")
    rows = []
    robots = list(Robot.objects.order_by("code"))
    periodic = periodic_status([r.pk for r in robots])
    for r in robots:
        rows.append(
            {
                "robot": r,
                "e": s.evaluate_eligibility(r),
                "open_holds": list(r.holds.filter(released_at__isnull=True)),
                "periodic": periodic.get(r.pk),
            }
        )
    return render(
        request, "fleet/list.html", {"rows": rows, "roles": sorted(active_roles(request.user)), **_banner()}
    )


@login_required
def robot_detail(request: HttpRequest, code: str) -> HttpResponse:
    if not has_permission(request.user, "fleet.read"):
        return HttpResponseForbidden("No fleet access")
    r = get_object_or_404(Robot, code=code)
    production = _production()
    own = list(r.holds.select_related("policy").order_by("-placed_at"))
    pattern_open = (
        list(Hold.objects.filter(pattern=r.current_manifest.pattern, released_at__isnull=True))
        if r.current_manifest is not None
        else []
    )
    basis = hold_basis(own + pattern_open)
    holds = []
    for h in own:
        holds.append(
            {
                "hold": h,
                "status": s.hold_release_status(h) if h.released_at is None else None,
                "basis": basis.get(h.pk),
                "rebases": rebase_rows(h, basis.get(h.pk)),
            }
        )
    pattern_holds = []
    for h in pattern_open:
        pattern_holds.append({"hold": h, "status": s.hold_release_status(h), "basis": basis.get(h.pk)})
    templates = (
        template_rows(r.current_manifest.pattern, production) if r.current_manifest is not None else []
    )
    recent = list(
        Inspection.objects.filter(robot=r)
        .select_related("template", "performer", "verifier")
        .order_by("-id")[:5]
    )
    return render(
        request,
        "fleet/detail.html",
        {
            "robot": r,
            "e": s.evaluate_eligibility(r),
            "manifest": r.current_manifest,
            "holds": holds,
            "pattern_holds": pattern_holds,
            "history": list(reversed(s.robot_history(r))),
            "roles": sorted(active_roles(request.user)),
            "production": production,
            "periodic": periodic_status([r.pk]).get(r.pk),
            "battery": robot_battery_panel(r),  # v2 02B: last recorded / estimated at departure / evidence
            "maintenance": mnt.robot_status(r),  # v2 02C: one statement (fleet_maintenance_status)
            "templates": templates,
            "recent_inspections": signer_rows(recent),
            "synthetic_interval": SYNTHETIC_INTERVAL,
            "base_auth": r.base_authorizations.filter(revoked_at__isnull=True)
            .select_related("pattern", "granted_by")
            .first(),
            "can": _can(request),
            **_banner(),
        },
    )


def signer_rows(inspections: list[Inspection]) -> list[dict[str, Any]]:
    """Each inspection with planning_inspection_signer_problem (X-8; NULL = both signers covered)."""
    if not inspections:
        return []
    with connection.cursor() as cur:
        cur.execute(
            "SELECT i.id, planning_inspection_signer_problem(i.id) FROM fleet_inspection i "
            "WHERE i.id = ANY(%s)",
            [[i.pk for i in inspections]],
        )
        problems = dict(cur.fetchall())
    return [{"inspection": i, "signer_problem": problems.get(i.pk)} for i in inspections]


# ------------------------------------------------------------------ Slice 7 pages
@login_required
def pattern_detail(request: HttpRequest, code: str) -> HttpResponse:
    if not has_permission(request.user, "fleet.read"):
        return HttpResponseForbidden("No fleet access")
    pattern = get_object_or_404(ManifestPattern, code=code)
    production = _production()
    robots = list(
        Robot.objects.filter(current_manifest__pattern=pattern).exclude(lifecycle="retired").order_by("code")
    )
    periodic = periodic_status([r.pk for r in robots])
    return render(
        request,
        "fleet/pattern.html",
        {
            "pattern": pattern,
            "production": production,
            "templates": template_rows(pattern, production),
            "rules": periodic_rule_rows(pattern, production),
            # v2 02C: maintenance rules (every version) and the templates a rule may require
            "maintenance_rules": mnt.pattern_rules(pattern),
            "maintenance_templates": mnt.pattern_templates(pattern),
            "maintenance_demo_label": mnt.DEMO_LABEL,
            "robots": [{"robot": r, "periodic": periodic.get(r.pk)} for r in robots],
            "purposes": TEMPLATE_PURPOSES,
            "slots": APPROVAL_SLOTS,
            "template_sources": TEMPLATE_SOURCES,
            "synthetic_interval": SYNTHETIC_INTERVAL,
            "unusable_warning": UNUSABLE_TEMPLATE_WARNING,
            "can": _can(request),
            "decision": s.pattern_status(pattern),
            "roles": sorted(active_roles(request.user)),
            **_banner(),
        },
    )


@login_required
def hold_policies(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, "fleet.read"):
        return HttpResponseForbidden("No fleet access")
    production = _production()
    policies = list(
        HoldPolicy.objects.select_related("created_by", "withdrawal", "withdrawal__withdrawn_by")
        .prefetch_related("approvals__approver")
        .order_by("-version")
    )
    with connection.cursor() as cur:
        cur.execute("SELECT fleet_active_hold_policy_id(), fleet_hold_binding_policy_id()")
        row = cur.fetchone()
        active_id, binding_id = (row[0], row[1]) if row else (None, None)
        state: dict[int, tuple[bool, bool, bool]] = {}
        if policies:
            cur.execute(
                """
                SELECT p.id, fleet_hold_policy_approved(p.id), fleet_policy_production_usable(p.id),
                       fleet_policy_usable(p.id)
                FROM fleet_holdpolicy p WHERE p.id = ANY(%s)
                """,
                [[p.pk for p in policies]],
            )
            state = {pid: (bool(a), bool(pu), bool(u)) for pid, a, pu, u in cur.fetchall()}
    rows = []
    for p in policies:
        approved, prod_usable, usable = state.get(p.pk, (False, False, False))
        withdrawal = getattr(p, "withdrawal", None)
        approvals = {a.slot: a for a in p.approvals.all()}
        slots = [x for x in HOLD_POLICY_SLOTS if x[0] != "founder" or p.source == "approved_procedure"]
        if withdrawal is not None:
            status = "withdrawn"
        elif approved and p.source == "approved_procedure":
            status = "approved procedure"
        elif approved:
            status = "synthetic approved"
        else:
            status = "synthetic draft" if p.source == "synthetic_demo" else "draft (awaiting approval)"
        rows.append(
            {
                "policy": p,
                "status": status,
                "withdrawal": withdrawal,
                "active": p.pk == active_id,
                "binding": p.pk == binding_id,
                "production_usable": prod_usable,
                "usable_now": usable,
                "has_incident_rule": "incident" in (p.rules or {}),
                "slots": [{"slot": sl, "label": lbl, "approval": approvals.get(sl)} for sl, lbl in slots],
                "rules": json.dumps(p.rules, indent=2, sort_keys=True),
            }
        )
    return render(
        request,
        "fleet/hold_policies.html",
        {
            "rows": rows,
            "production": production,
            "active_id": active_id,
            "binding_id": binding_id,
            "sources": HOLD_POLICY_SOURCES,
            "example": request.session.pop("fleet_policy_draft", None)
            or json.dumps(EXAMPLE_POLICY_RULES, indent=2, sort_keys=True),
            "can": _can(request),
            "roles": sorted(active_roles(request.user)),
            **_banner(),
        },
    )


def _run(request: HttpRequest, code: str, fn: Any, ok: str, **kwargs: Any) -> HttpResponse:
    try:
        fn(request.user, **kwargs)
        messages.success(request, ok)
    except CommandError as exc:
        messages.error(request, f"Refused: {exc}")
    return redirect("fleet-detail", code=code)


@login_required
@require_POST
def place_hold_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    return _run(
        request,
        code,
        s.place_hold,
        "Hold placed",
        scope="robot",
        target=code,
        source_type=p.get("source_type", "manual"),
        source_ref=p.get("source_ref", ""),
        reason=p.get("reason", ""),
    )


@login_required
@require_POST
def approve_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    return _run(
        request,
        code,
        s.approve_release,
        "Approval recorded",
        hold_code=p["hold"],
        slot=p["slot"],
        role_used=p["role"],
        note=p.get("note", ""),
    )


@login_required
@require_POST
def release_view(request: HttpRequest, code: str) -> HttpResponse:
    return _run(request, code, s.release_hold, "Hold released", hold_code=request.POST["hold"])


@login_required
@require_POST
def check_view(request: HttpRequest, code: str) -> HttpResponse:
    return _run(request, code, s.check_eligibility, "Eligibility check recorded", robot_code=code)


# ------------------------------------------------------------------ Slice 7 forms (Engineer B's commands)
def _s7(
    request: HttpRequest,
    permission: str,
    name: str,
    ok: str,
    back: HttpResponse,
    **kwargs: Any,
) -> HttpResponse:
    """Refuse a role without ``permission`` (403); else call fleet.services.<name> when this build has it."""
    if not has_permission(request.user, permission):
        return HttpResponseForbidden("You do not have permission for this action.")
    fn = _command(name)
    if fn is None:
        # B's command not merged into this build yet: record nothing and say so.
        messages.error(request, f"Refused: this build does not provide {name} yet; nothing was recorded")
        return back
    try:
        fn(request.user, **kwargs)
        messages.success(request, ok)
    except CommandError as exc:
        messages.error(request, f"Refused: {exc}")
    except (TypeError, ValueError) as exc:
        messages.error(request, f"Refused (invalid input): {exc}")
    return back


def _int(raw: str | None) -> int | None:
    try:
        return int(raw or "")
    except ValueError:
        return None


def _to_pattern(code: str) -> HttpResponse:
    return redirect("fleet-pattern", code=code)


def _to_policies() -> HttpResponse:
    return redirect("fleet-hold-policies")


@login_required
@require_POST
def template_create_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    try:
        items = json.loads(p.get("items", ""))
    except json.JSONDecodeError as exc:
        if not has_permission(request.user, "fleet.manage_inspection_template"):
            return HttpResponseForbidden("You do not have permission for this action.")
        messages.error(request, f"Refused (invalid input): items are not valid JSON ({exc.msg})")
        return _to_pattern(code)
    # contract 07a 3.5: create_inspection_template(actor, *, pattern_code, version, items, source,
    # procedure_ref)
    return _s7(
        request,
        "fleet.manage_inspection_template",
        "create_inspection_template",
        "Inspection template recorded; it needs engineering and safety approvals per purpose",
        _to_pattern(code),
        pattern_code=code,
        version=p.get("version", "").strip(),
        items=items,
        source=p.get("source", "engineering_doc"),
        procedure_ref=p.get("procedure_ref", "").strip(),
    )


@login_required
@require_POST
def template_approve_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    # contract 07a 3.5: approve_inspection_template(actor, *, template_id, purpose, slot)
    return _s7(
        request,
        "fleet.approve_inspection_template",
        "approve_inspection_template",
        "Template approval recorded",
        _to_pattern(code),
        template_id=_int(p.get("template")),
        purpose=p.get("purpose", ""),
        slot=p.get("slot", ""),
    )


@login_required
@require_POST
def template_withdraw_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    # contract 07a 3.5: withdraw_inspection_template(actor, *, template_id, reason)
    return _s7(
        request,
        "fleet.withdraw_inspection_template",
        "withdraw_inspection_template",
        "Template withdrawn (every purpose)",
        _to_pattern(code),
        template_id=_int(p.get("template")),
        reason=p.get("reason", ""),
    )


@login_required
@require_POST
def periodic_rule_create_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    raw = p.get("interval_min", "").strip()
    # ASSUMED signature (contract 07a 4.5 names the command only):
    # create_periodic_rule(actor, *, pattern_code, interval_min, source, procedure_ref, notes)
    return _s7(
        request,
        "fleet.manage_periodic_rule",
        "create_periodic_rule",
        "Periodic inspection rule version recorded; it needs engineering and safety approvals",
        _to_pattern(code),
        pattern_code=code,
        interval_min=_int(raw) if raw else None,
        source=p.get("source", "engineering_doc"),
        procedure_ref=p.get("procedure_ref", "").strip(),
        notes=p.get("notes", ""),
    )


@login_required
@require_POST
def periodic_rule_approve_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    # ASSUMED signature: approve_periodic_rule(actor, *, rule_id, slot)
    return _s7(
        request,
        "fleet.approve_periodic_rule",
        "approve_periodic_rule",
        "Periodic rule approval recorded",
        _to_pattern(code),
        rule_id=_int(p.get("rule")),
        slot=p.get("slot", ""),
    )


@login_required
@require_POST
def periodic_rule_withdraw_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    # ASSUMED signature: withdraw_periodic_rule(actor, *, rule_id, reason)
    return _s7(
        request,
        "fleet.withdraw_periodic_rule",
        "withdraw_periodic_rule",
        "Periodic rule withdrawn; there is no fallback to an older version",
        _to_pattern(code),
        rule_id=_int(p.get("rule")),
        reason=p.get("reason", ""),
    )


@login_required
@require_POST
def hold_policy_create_view(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, "fleet.manage_hold_policy"):
        return HttpResponseForbidden("You do not have permission for this action.")
    p = request.POST
    raw = p.get("rules", "")
    try:
        rules = json.loads(raw)
    except json.JSONDecodeError as exc:
        messages.error(request, f"Refused (invalid input): rules are not valid JSON ({exc.msg})")
        request.session["fleet_policy_draft"] = raw
        return _to_policies()
    # contract 07a 5.5: create_hold_policy(actor, *, rules, source, procedure_ref)
    return _s7(
        request,
        "fleet.manage_hold_policy",
        "create_hold_policy",
        "Hold policy version recorded as a draft; it needs its approvals",
        _to_policies(),
        rules=rules,
        source=p.get("source", "synthetic_demo"),
        procedure_ref=p.get("procedure_ref", "").strip(),
    )


@login_required
@require_POST
def hold_policy_approve_view(request: HttpRequest, version: int) -> HttpResponse:
    # existing command: approve_hold_policy(actor, *, version, slot)
    return _s7(
        request,
        "fleet.approve_hold_policy",
        "approve_hold_policy",
        f"Hold policy v{version}: approval recorded",
        _to_policies(),
        version=version,
        slot=request.POST.get("slot", ""),
    )


@login_required
@require_POST
def hold_policy_withdraw_view(request: HttpRequest, version: int) -> HttpResponse:
    # ASSUMED signature (contract 07a 5.5 names withdraw_hold_policy): withdraw_hold_policy(actor, *,
    # version, reason)
    return _s7(
        request,
        "fleet.withdraw_hold_policy",
        "withdraw_hold_policy",
        f"Hold policy v{version} withdrawn; there is no fallback to an older version",
        _to_policies(),
        version=version,
        reason=request.POST.get("reason", ""),
    )


@login_required
@require_POST
def rebase_request_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    # contract 07a 5.5: request_hold_rebase(actor, *, hold_code, reason)
    return _s7(
        request,
        "fleet.rebase_hold",
        "request_hold_rebase",
        "Re-basing request recorded; it needs two distinct approvers, neither the placer",
        redirect("fleet-detail", code=code),
        hold_code=p.get("hold", ""),
        reason=p.get("reason", ""),
    )


@login_required
@require_POST
def rebase_approve_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    # contract 07a 5.5: approve_hold_rebase(actor, *, rebase_id, slot)
    return _s7(
        request,
        "fleet.rebase_hold",
        "approve_hold_rebase",
        "Re-basing approval recorded",
        redirect("fleet-detail", code=code),
        rebase_id=_int(p.get("rebase")),
        slot=p.get("slot", ""),
    )


# ------------------------------------------------------------------ v2 02C maintenance screens
def _mnt_post(
    request: HttpRequest, permission: str, go: Callable[[], Any], ok: str, back: str
) -> HttpResponse:
    """POST-redirect-GET for a maintenance command: role check (403), one-time form token, then the command
    (the SECURITY DEFINER function re-checks role, qualification and invariants)."""
    if not has_permission(request.user, permission):
        return HttpResponseForbidden("You do not have permission for this action.")
    if not spend_form_token(request):
        return redirect(back)
    try:
        go()
        messages.success(request, ok)
    except CommandError as exc:
        messages.error(request, f"Refused: {str(exc).rstrip('.')}. Nothing was changed.")
    return redirect(back)


def _local_datetime(raw: str | None) -> datetime | None:
    """A ``datetime-local`` value in the workspace display zone, or None when empty."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        raise CommandError("The effective time is not a valid date and time") from None
    return naive.replace(tzinfo=workspace_zone()) if naive.tzinfo is None else naive


@login_required
@require_POST
def maintenance_rule_create_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST

    def go() -> None:
        mnt.propose_rule(
            request.user,
            pattern_code=code,
            requirement_code=p.get("requirement_code", ""),
            calendar_days=_int(p.get("calendar_days")),
            visit_count=_int(p.get("visit_count")),
            active_hours=_int(p.get("active_hours")),
            warning_pct=_int(p.get("warning_pct")) if p.get("warning_pct", "").strip() else 10,
            inspection_template_id=_int(p.get("inspection_template")),
            effective_from=_local_datetime(p.get("effective_from")),
            source=p.get("source", "engineering_doc"),
            procedure_ref=p.get("procedure_ref", "").strip(),
            notes=p.get("notes", ""),
        )

    return _mnt_post(
        request,
        mnt.PROPOSE,
        go,
        "Maintenance rule proposal recorded. It is not in force until the engineering lead and a safety "
        "reviewer approve it.",
        f"/fleet/patterns/{code}/#maintenance-rules",
    )


@login_required
@require_POST
def maintenance_rule_approve_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    return _mnt_post(
        request,
        mnt.APPROVE,
        lambda: mnt.approve_rule(request.user, rule_id=_int(p.get("rule")) or 0, slot=p.get("slot", "")),
        "Maintenance rule approval recorded",
        f"/fleet/patterns/{code}/#maintenance-rules",
    )


@login_required
@require_POST
def maintenance_rule_withdraw_view(request: HttpRequest, code: str) -> HttpResponse:
    p = request.POST
    return _mnt_post(
        request,
        mnt.WITHDRAW,
        lambda: mnt.withdraw_rule(request.user, rule_id=_int(p.get("rule")) or 0, reason=p.get("reason", "")),
        "Maintenance rule withdrawn; there is no fallback to an older version",
        f"/fleet/patterns/{code}/#maintenance-rules",
    )


def _maintainer_now(user_id: int) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT planning_maintainer_covered(%s, clock_timestamp())", [user_id])
        return bool(cur.fetchone()[0])


@login_required
def robot_maintenance(request: HttpRequest, code: str) -> HttpResponse:
    """The robot's maintenance panel: status per requirement, counters with their sources, why a status is
    Unknown, the required inspection, and "Record maintenance" for a qualified person. Readable with fleet
    access or the record permission (the hub technician, who has no other fleet page)."""
    r = get_object_or_404(Robot, code=code)
    if request.method == "POST":
        p = request.POST
        return _mnt_post(
            request,
            mnt.RECORD,
            lambda: mnt.record_maintenance(
                request.user, robot_code=code, requirement_code=p.get("requirement_code", ""),
                notes=p.get("notes", ""),
            ),
            "Maintenance recorded. It never releases a hold; a required post-maintenance inspection is "
            "recorded separately.",
            f"/fleet/{code}/maintenance/",
        )  # fmt: skip
    if not (has_permission(request.user, "fleet.read") or has_permission(request.user, mnt.RECORD)):
        return HttpResponseForbidden("No fleet access")
    status = mnt.robot_status(r)
    now = s.db_now()
    records = {
        x.pk: x
        for x in MaintenanceRecord.objects.filter(pk__in=[q.record_id for q in status.rows if q.record_id])
        .select_related("performed_by")
    }  # fmt: skip
    templates = {
        t.pk: t
        for t in InspectionTemplate.objects.filter(
            pk__in=[q.inspection_template_id for q in status.rows if q.inspection_template_id]
        )
    }
    inspections = {
        i.pk: i
        for i in Inspection.objects.filter(pk__in=[q.inspection_id for q in status.rows if q.inspection_id])
    }
    rows = []
    for q in status.rows:
        rec = records.get(q.record_id) if q.record_id else None
        rows.append(
            {
                "q": q,
                "record": rec,
                "days_used": (now - q.performed_at).days if q.performed_at else None,
                "template": templates.get(q.inspection_template_id) if q.inspection_template_id else None,
                "inspection": inspections.get(q.inspection_id) if q.inspection_id else None,
            }
        )
    may_record = has_permission(request.user, mnt.RECORD)
    return render(
        request,
        "fleet/maintenance.html",
        {
            "robot": r,
            "status": status,
            "rows": rows,
            "may_record": may_record,
            "qualified": may_record and _maintainer_now(request.user.pk),
            "requirements": [q.requirement_code for q in status.rows if q.rule_id and q.state != "no_rule"],
            "demo_label": mnt.DEMO_LABEL,
            "gate_inactive_label": mnt.GATE_INACTIVE_LABEL,
            "fleet_access": has_permission(request.user, "fleet.read"),
        },
    )
