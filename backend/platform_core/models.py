from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    """Append-only record of every state-changing command and every denied attempt.

    Immutability is enforced in the database (migration 0002): the runtime role has no
    UPDATE/DELETE/TRUNCATE privilege and a trigger rejects those statements for everyone
    except a superuser who disables it (a separate, documented trust boundary).
    """

    OUTCOME_COMMITTED = "committed"
    OUTCOME_DENIED = "denied"

    occurred_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    command = models.CharField(max_length=100)
    outcome = models.CharField(max_length=20)
    entity_type = models.CharField(max_length=60, blank=True)
    entity_id = models.CharField(max_length=60, blank=True)
    payload = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    reason = models.TextField(blank=True)
    request_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["entity_type", "entity_id"]), models.Index(fields=["command"])]

    def __str__(self) -> str:
        return f"{self.occurred_at} {self.command} {self.outcome} {self.entity_type}:{self.entity_id}"


class IdempotencyKey(models.Model):
    """One row per (actor, key). A retried command returns the stored response instead of re-running."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    key = models.CharField(max_length=100)
    command = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "key"], name="uniq_idempotency_actor_key")]

    def __str__(self) -> str:
        return f"{self.command}:{self.key}"
