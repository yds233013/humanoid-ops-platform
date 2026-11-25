from typing import Any

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


class Workspace(models.Model):
    """The one workspace of this deployment (v2 contract section 2 and 12.1): a singleton row.

    The database enforces it: the primary key is fixed at 1 (default 1, CHECK id = 1), and a primary key is
    unique, so a second row is impossible even through raw SQL. ``initialized`` flips to true in the same
    transaction that creates the first admin (platform_core.bootstrap) and never goes back.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    name = models.CharField(max_length=120, default="Workspace")
    region = models.CharField(max_length=80, blank=True, default="")
    time_zone = models.CharField(max_length=64, default="America/Los_Angeles")
    demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    initialized = models.BooleanField(default=False)
    initialized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="workspace_singleton_id_is_1")]

    def __str__(self) -> str:
        return self.name

    @classmethod
    def ensure(cls) -> None:
        """Create the singleton row if it is missing (INSERT .. ON CONFLICT DO NOTHING; safe under races)."""
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_core_workspace"
                " (id, name, region, time_zone, demo, created_at, initialized)"
                " VALUES (1, 'Workspace', '', 'America/Los_Angeles', false, now(), false)"
                " ON CONFLICT (id) DO NOTHING"
            )


class Hub(models.Model):
    """A hub (depot) of the workspace: where robots, vehicles and crews are based (v2 contract section 6).

    Setup data, like the workspace row: it records a fact and approves nothing. Since Phase 03A-1 every robot,
    vehicle, person (home hub), visit (dispatch hub), work order, charger, charging session, charger
    reservation
    and stock item belongs to exactly one hub (``hub_id`` NOT NULL; docs/13_v2_phase03_contract.md 11.1).
    """

    code = models.CharField(max_length=12, unique=True)
    name = models.CharField(max_length=80)
    area = models.CharField(max_length=80, blank=True, default="")
    time_zone = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class DefaultHub(models.Func):
    """The database default of every owner ``hub_id`` column (platform_core migration 0005): the only hub, a
    new
    HUB-1 when none exists yet, or a refusal ("hub required") when there are several. Evaluated by PostgreSQL
    when a row is inserted without a hub, so direct-SQL inserts get the same rule as the ORM."""

    function = "platform_core_default_hub"
    template = "%(function)s()"
    output_field = models.BigIntegerField()

    def __init__(self) -> None:
        super().__init__()


def hub_field() -> Any:
    """The owner hub of a record: NOT NULL, PROTECT, with the database default hub."""
    return models.ForeignKey(
        "platform_core.Hub", on_delete=models.PROTECT, related_name="+", db_default=DefaultHub()
    )


class FormToken(models.Model):
    """One-time token for a rendered form (double-submit protection, v2 01C).

    Issued when a form is rendered, consumed by one atomic UPDATE on the first POST. A replayed POST finds
    ``consumed_at`` set and is refused without running anything. Stored in the database (not the session) so
    two simultaneous submits cannot both win: only one UPDATE changes the row."""

    token = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"form token for {self.user_id}"
