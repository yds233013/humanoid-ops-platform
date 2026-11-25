"""Server-side permission checks.

v2 03A-1 (docs/13_v2_phase03_contract.md 11.2, correction B): a grant is regional (hub NULL) or held at
one hub. ``has_permission(user, permission)`` counts every active grant (any hub), as before. With ``hub=``
it counts only the grants that apply AT that hub: regional grants and grants of that hub. A person's home
hub is never consulted; only grants confer authority.
"""

from typing import Any

from django.db.models import Q
from django.utils import timezone

from accounts.roles import PERMISSIONS


def _hub_id(hub: Any) -> int | None:
    if hub is None:
        return None
    return int(getattr(hub, "pk", hub))


def _active_grants(user: Any) -> Any:
    from accounts.models import RoleGrant

    now = timezone.now()
    return RoleGrant.objects.filter(user=user, revoked_at__isnull=True, valid_from__lte=now).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gt=now)
    )


def _signed_in(user: Any) -> bool:
    return user is not None and getattr(user, "is_authenticated", False) and user.is_active


def active_roles(user: Any, hub: Any = None) -> set[str]:
    """The user's active roles; with ``hub``, only the roles that apply at that hub (regional or that
    hub's)."""
    if not _signed_in(user):
        return set()
    qs = _active_grants(user)
    hub_id = _hub_id(hub)
    if hub_id is not None:
        qs = qs.filter(Q(hub__isnull=True) | Q(hub_id=hub_id))
    return set(qs.values_list("role", flat=True))


def has_permission(user: Any, permission: str, hub: Any = None) -> bool:
    allowed = PERMISSIONS.get(permission)
    if allowed is None:
        return False  # unknown permission: deny by default
    return bool(active_roles(user, hub) & allowed)


def visible_hub_ids(user: Any) -> set[int] | None:
    """The hubs whose records this user may see on list pages: ``None`` = every hub (the user holds a regional
    grant), else the hubs of their active hub grants (possibly none). One query."""
    if not _signed_in(user):
        return set()
    hubs: set[int] = set()
    for hub_id in _active_grants(user).values_list("hub_id", flat=True):
        if hub_id is None:
            return None
        hubs.add(int(hub_id))
    return hubs
