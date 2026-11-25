"""Create SYNTHETIC development users, one per role. Never run against production."""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from accounts.roles import ALL_ROLES, HUB_SCOPED_ROLES


def seed_grant(user_id: int, role: str, hub_id: int | None = None) -> int:
    """v2 03A-3: an active grant of ``role`` (at ``hub_id``), created when missing, through the owner-only
    accounts_seed_grant (accounts/0006, audited as accounts.seed_grant). Seeds run as the schema owner; the
    runtime role cannot call it."""
    with connection.cursor() as cur:
        cur.execute("SELECT accounts_seed_grant(%s, %s, %s, %s)", [user_id, role, hub_id, "seed"])
        return int(cur.fetchone()[0])


def _default_hub_id() -> int:
    """v2 03A-1: hub-scoped dev grants are held at the workspace's hub (platform_core_default_hub: the only
    hub, a new HUB-1 when there is none; refused as "hub required" when there are several)."""
    with connection.cursor() as cur:
        cur.execute("SELECT platform_core_default_hub()")
        return int(cur.fetchone()[0])


class Command(BaseCommand):
    help = "Create synthetic dev users (one per role). Password from DEV_SEED_PASSWORD."

    def handle(self, *args: object, **opts: object) -> None:
        password = os.environ.get("DEV_SEED_PASSWORD")
        if not password:
            raise CommandError("Set DEV_SEED_PASSWORD (see .env.example)")
        User = get_user_model()
        for role in ALL_ROLES:
            username = f"dev_{role}"
            user, created = User.objects.get_or_create(username=username)
            if created:
                user.set_password(password)
                user.save()
            seed_grant(user.pk, role, _default_hub_id() if role in HUB_SCOPED_ROLES else None)
            self.stdout.write(f"{'created' if created else 'exists '} {username} [{role}]")
        # v2 01A: the demo workspace is initialized (never claimable through /setup/).
        from django.utils import timezone

        from platform_core.models import Workspace

        Workspace.ensure()
        Workspace.objects.filter(pk=1, initialized=False).update(
            name="Demo workspace", demo=True, initialized=True, initialized_at=timezone.now()
        )
