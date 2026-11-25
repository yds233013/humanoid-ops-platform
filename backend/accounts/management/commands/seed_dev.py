"""Create SYNTHETIC development users, one per role. Never run against production."""

import os
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from accounts.models import RoleGrant
from accounts.roles import ALL_ROLES, HUB_SCOPED_ROLES


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
            hub: dict[str, Any] = (
                {"hub_id": _default_hub_id()} if role in HUB_SCOPED_ROLES else {"hub__isnull": True}
            )
            RoleGrant.objects.get_or_create(user=user, role=role, revoked_at=None, **hub)
            self.stdout.write(f"{'created' if created else 'exists '} {username} [{role}]")
        # v2 01A: the demo workspace is initialized (never claimable through /setup/).
        from django.utils import timezone

        from platform_core.models import Workspace

        Workspace.ensure()
        Workspace.objects.filter(pk=1, initialized=False).update(
            name="Demo workspace", demo=True, initialized=True, initialized_at=timezone.now()
        )
