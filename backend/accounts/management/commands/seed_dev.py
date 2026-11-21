"""Create SYNTHETIC development users, one per role. Never run against production."""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.models import RoleGrant
from accounts.roles import ALL_ROLES


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
            RoleGrant.objects.get_or_create(user=user, role=role, revoked_at=None)
            self.stdout.write(f"{'created' if created else 'exists '} {username} [{role}]")
        # v2 01A: the demo workspace is initialized (never claimable through /setup/).
        from django.utils import timezone

        from platform_core.models import Workspace

        Workspace.ensure()
        Workspace.objects.filter(pk=1, initialized=False).update(
            name="Demo workspace", demo=True, initialized=True, initialized_at=timezone.now()
        )
