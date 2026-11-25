from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now
from django.utils import timezone

from accounts.roles import ALL_ROLES, HUB_SCOPED_ROLES


class RoleGrant(models.Model):
    """A role held by a user for a period. Revoking sets revoked_at; rows are never deleted.

    v2 03A-1 (contract 11.2, correction B): ``hub`` is NULL for a regional grant and names the hub for a
    hub-qualified one. Hub-scoped roles (HUB_SCOPED_ROLES) must name a hub, every other role must not (check
    constraint). When a hub-scoped grant is inserted without a hub, the database fills in the only hub
    (trigger accounts_rolegrant_default_hub, platform_core_default_hub(): refused as "hub required" when
    there are several), so a single-hub workspace keeps working unchanged. At most one ACTIVE grant per
    (user, role, hub).
    """

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
    hub = models.ForeignKey(
        "platform_core.Hub", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            # v2 03A-1: at most one ACTIVE grant per (user, role, coalesce(hub, 0)). An expression index
            # created in SQL by accounts/0005 (uniq_active_role_grant), not declared here: Django cannot
            # compare an expression constraint over a ForeignKey without re-detecting it on every
            # makemigrations.
            models.CheckConstraint(
                condition=(Q(role__in=list(HUB_SCOPED_ROLES)) & Q(hub__isnull=False))
                | (~Q(role__in=list(HUB_SCOPED_ROLES)) & Q(hub__isnull=True)),
                name="role_grant_hub_scope",
            ),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gt=models.F("valid_from")),
                name="role_grant_valid_window",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.role}" + (f"@{self.hub_id}" if self.hub_id else "")

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
