from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now
from django.utils import timezone

from accounts.roles import ALL_ROLES


class RoleGrant(models.Model):
    """A role held by a user for a period. Revoking sets revoked_at; rows are never deleted."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="role_grants")
    role = models.CharField(max_length=40, choices=[(r, r) for r in ALL_ROLES])
    # Database time, not the app host's clock: permission checks compare against clock_timestamp() in
    # PostgreSQL, so an app clock a few ms ahead would make a brand-new grant briefly inactive.
    valid_from = models.DateTimeField(db_default=Now())
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


class PasswordState(models.Model):
    """Whether a user must change a temporary password before using the app (v2 01D).

    ``must_change`` is set when an admin creates the account (accounts.services.create_user) or resets its
    password (reset_password), and cleared ONLY by that user's own successful change (change_own_password).
    No row means "not forced": the bootstrap admin and the synthetic dev users chose their own password.
    No password, hash or hint is ever stored here.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, primary_key=True, on_delete=models.CASCADE, related_name="password_state"
    )
    must_change = models.BooleanField(default=False)
    temporary_set_at = models.DateTimeField(null=True, blank=True)
    temporary_set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    changed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.user_id}: {'must change' if self.must_change else 'ok'}"


def must_change_password(user: object) -> bool:
    """True while the signed-in user still has a temporary password (one indexed primary-key lookup)."""
    pk = getattr(user, "pk", None)
    if pk is None or not getattr(user, "is_authenticated", False):
        return False
    return PasswordState.objects.filter(user_id=pk, must_change=True).exists()
