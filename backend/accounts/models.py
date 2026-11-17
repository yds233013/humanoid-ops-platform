from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from accounts.roles import ALL_ROLES


class RoleGrant(models.Model):
    """A role held by a user for a period. Revoking sets revoked_at; rows are never deleted."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="role_grants")
    role = models.CharField(max_length=40, choices=[(r, r) for r in ALL_ROLES])
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField(null=True, blank=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role"], condition=Q(revoked_at__isnull=True), name="uniq_active_role_grant"
            ),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gt=models.F("valid_from")),
                name="role_grant_valid_window",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.role}"

    def is_active(self, at: object = None) -> bool:
        now = at or timezone.now()
        return (
            self.revoked_at is None
            and self.valid_from <= now  # type: ignore[operator]
            and (self.valid_to is None or now < self.valid_to)  # type: ignore[operator]
        )
