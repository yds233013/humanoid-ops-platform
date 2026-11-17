from typing import Any

from django.db.models import Q
from django.utils import timezone

from accounts.roles import PERMISSIONS


def active_roles(user: Any) -> set[str]:
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return set()
    from accounts.models import RoleGrant

    now = timezone.now()
    return set(
        RoleGrant.objects.filter(user=user, revoked_at__isnull=True, valid_from__lte=now)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
        .values_list("role", flat=True)
    )


def has_permission(user: Any, permission: str) -> bool:
    allowed = PERMISSIONS.get(permission)
    if allowed is None:
        return False  # unknown permission: deny by default
    return bool(active_roles(user) & allowed)
