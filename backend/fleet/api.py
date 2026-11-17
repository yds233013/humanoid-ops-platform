from collections.abc import Callable
from typing import Any

from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.permissions import has_permission
from fleet import services as s
from fleet.models import Robot

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
IDEMPOTENT = {"register-robot", "place-hold", "submit-inspection"}


def _robot_json(r: Robot) -> dict[str, Any]:
    e = s.evaluate_eligibility(r)
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
    }


@api_view(["GET"])
def robots(request: Request) -> Response:
    if not has_permission(request.user, "fleet.read"):
        return Response({"error": "permission_denied"}, status=403)
    return Response([_robot_json(r) for r in Robot.objects.order_by("code")])


@api_view(["GET"])
def robot_detail(request: Request, code: str) -> Response:
    if not has_permission(request.user, "fleet.read"):
        return Response({"error": "permission_denied"}, status=403)
    r = Robot.objects.filter(code=code).first()
    if r is None:
        return Response({"error": "not_found"}, status=404)
    data = _robot_json(r)
    data["history"] = [{**e, "at": e["at"].isoformat()} for e in s.robot_history(r)]
    return Response(data)


@api_view(["POST"])
def command(request: Request, name: str) -> Response:
    fn = COMMANDS.get(name)
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
