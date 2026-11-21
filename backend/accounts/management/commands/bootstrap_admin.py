"""Create the first admin of a fresh deployment (same rules as /setup/: one-time token, once only)."""

import os
from getpass import getpass
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from platform_core.bootstrap import BootstrapRefused, bootstrap_first_admin


class Command(BaseCommand):
    help = (
        "Create the first admin. Needs WORKSPACE_BOOTSTRAP_TOKEN in the environment and --token to match it; "
        "password from BOOTSTRAP_ADMIN_PASSWORD or a prompt. Refused once the workspace is initialized."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--token", required=True)
        parser.add_argument("--workspace-name", default="")

    def handle(self, *args: object, **opts: Any) -> None:
        password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or getpass("Admin password: ")
        try:
            bootstrap_first_admin(
                token=opts["token"],
                username=opts["username"],
                password=password,
                workspace_name=opts["workspace_name"],
            )
        except BootstrapRefused as refusal:
            raise CommandError(refusal.message) from None
        self.stdout.write(f"created admin {opts['username']}; workspace initialized")
