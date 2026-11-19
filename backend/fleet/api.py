from collections.abc import Callable
from typing import Any

from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.permissions import has_permission
from fleet import services as s
from fleet.models import Robot
from fleet.views import periodic_status

# Whitelisted commands: POST /api/fleet/<name> with the keyword arguments as the JSON body.
COMMANDS: dict[str, Callable[..., dict[str, Any]]] = {
    "register-robot": s.register_robot,
    "transition-lifecycle": s.transition_lifecycle,
    "create-pattern": s.create_pattern,
    "decide-pattern": s.decide_pattern,
    "record-manifest": s.record_manifest,
    "publish-capability-profile": s.publish_capability_profile,
    "approve-capability-profile": s.approve_capability_profile,
    "grant-base-authorization": s.grant_base_authorization,
    "revoke-base-authorization": s.revoke_base_authorization,
    "create-hold-policy": s.create_hold_policy,
    "approve-hold-policy": s.approve_hold_policy,
    "place-hold": s.place_hold,
    "approve-release": s.approve_release,
    "withdraw-release-approval": s.withdraw_release_approval,
    "release-hold": s.release_hold,
    "submit-inspection": s.submit_inspection,
    "check-eligibility": s.check_eligibility,
}
# Slice 7 commands (Engineer B, fleet.services; contract 07a 3.5, 4.5, 5.5). Whitelisted only when this build
# provides them, so an unknown command is a 404 rather than a crash.
S7_COMMANDS = {
    "create-inspection-template": "create_inspection_template",
    "approve-inspection-template": "approve_inspection_template",
    "withdraw-inspection-template": "withdraw_inspection_template",
    "create-periodic-rule": "create_periodic_rule",
    "approve-periodic-rule": "approve_periodic_rule",
    "withdraw-periodic-rule": "withdraw_periodic_rule",
    "withdraw-hold-policy": "withdraw_hold_policy",
    "request-hold-rebase": "request_hold_rebase",
    "approve-hold-rebase": "approve_hold_rebase",
}
IDEMPOTENT = {"register-robot", "place-hold", "submit-inspection"}


def _command_fn(name: str) -> Callable[..., dict[str, Any]] | None:
    fn = COMMANDS.get(name)
    if fn is None and name in S7_COMMANDS:
        found = getattr(s, S7_COMMANDS[name], None)
        fn = found if callable(found) else None
    return fn


def _robot_json(r: Robot, periodic: dict[str, Any] | None = None) -> dict[str, Any]:
    e = s.evaluate_eligibility(r)
    p = periodic or {}
    return {
        "code": r.code,
        "serial": r.serial,
        "lifecycle": r.lifecycle,
        "configuration_pattern": e.pattern_code,
        "fleet_eligibility": {
            "eligible_for_planning": e.eligible_for_planning,
            "label": e.label,
            "reasons": e.reasons,
        },
        "open_holds": [h.code for h in r.holds.filter(released_at__isnull=True)],
        "guard_version": r.version,
        # Slice 7 (X-3): A's readiness_periodic_inspection_status in the current mode, at database time
        "periodic_inspection": {
            "state": p.get("state"),
            "due_at": p["due_at"].isoformat() if p.get("due_at") else None,
            "rule_version": p.get("rule_version"),
            "rule_source": p.get("rule_source"),
        },
    }


@api_view(["GET"])
def robots(request: Request) -> Response:
    if not has_permission(request.user, "fleet.read"):
        return Response({"error": "permission_denied"}, status=403)
    robots = list(Robot.objects.order_by("code"))
    periodic = periodic_status([r.pk for r in robots])
    return Response([_robot_json(r, periodic.get(r.pk)) for r in robots])


@api_view(["GET"])
def robot_detail(request: Request, code: str) -> Response:
    if not has_permission(request.user, "fleet.read"):
        return Response({"error": "permission_denied"}, status=403)
    r = Robot.objects.filter(code=code).first()
    if r is None:
        return Response({"error": "not_found"}, status=404)
    data = _robot_json(r, periodic_status([r.pk]).get(r.pk))
    data["history"] = [{**e, "at": e["at"].isoformat()} for e in s.robot_history(r)]
    return Response(data)


@api_view(["POST"])
def command(request: Request, name: str) -> Response:
    fn = _command_fn(name)
    if fn is None:
        return Response({"error": "unknown_command"}, status=404)
    kwargs = dict(request.data)
    if name in IDEMPOTENT:
        kwargs["idempotency_key"] = request.headers.get("Idempotency-Key")
    try:
        result = fn(request.user, **kwargs)
    except TypeError as exc:
        return Response({"error": "invalid", "detail": str(exc)}, status=422)
    return Response(result, status=200 if result.get("replayed") else 201)
