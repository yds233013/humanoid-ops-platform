from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from accounts import services
from accounts.models import must_change_password
from accounts.permissions import active_roles


@api_view(["GET"])
@permission_classes([AllowAny])
def csrf(request: Request) -> Response:
    return Response({"csrfToken": get_token(request._request)})


@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request: Request) -> Response:
    user = authenticate(
        request._request, username=request.data.get("username"), password=request.data.get("password")
    )
    if user is None:
        return Response({"error": "invalid_credentials"}, status=401)
    login(request._request, user)
    return Response(
        {
            "user_id": user.pk,
            "roles": sorted(active_roles(user)),
            # v2 01D: while true, every other API answers 403 password_change_required
            "password_change_required": must_change_password(user),
        }
    )


@api_view(["POST"])
def logout_view(request: Request) -> Response:
    logout(request._request)
    return Response({"ok": True})


@api_view(["GET"])
def me(request: Request) -> Response:
    return Response(
        {
            "user_id": request.user.pk,
            "username": request.user.get_username(),
            "roles": sorted(active_roles(request.user)),
        }
    )


class GrantRoleIn(serializers.Serializer):
    user_id = serializers.IntegerField()
    role = serializers.CharField()
    valid_to = serializers.CharField(required=False, allow_null=True)
    hub = serializers.CharField(required=False, allow_blank=True, allow_null=True)  # v2 03A-1: hub code


@api_view(["POST"])
def grant_role_view(request: Request) -> Response:
    data = GrantRoleIn(data=request.data)
    data.is_valid(raise_exception=True)
    raw = data.validated_data.get("valid_to")
    result = services.grant_role(
        request.user,
        user_id=data.validated_data["user_id"],
        role=data.validated_data["role"],
        valid_to=parse_datetime(raw) if raw else None,
        hub_code=data.validated_data.get("hub") or None,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return Response(result, status=200 if result["replayed"] else 201)


class RevokeRoleIn(serializers.Serializer):
    grant_id = serializers.IntegerField()
    reason = serializers.CharField()


@api_view(["POST"])
def revoke_role_view(request: Request) -> Response:
    data = RevokeRoleIn(data=request.data)
    data.is_valid(raise_exception=True)
    result = services.revoke_role(
        request.user,
        grant_id=data.validated_data["grant_id"],
        reason=data.validated_data["reason"],
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return Response(result)
