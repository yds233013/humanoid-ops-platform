"""Refresh the hub tasks derived from records (also done on every Hub page load). Safe to run any time."""

from typing import Any

from django.core.management.base import BaseCommand

from hub.derive import refresh


class Command(BaseCommand):
    help = (
        "Create hub tasks for new facts (inspections due, holds, failed equipment checks); mark cleared ones."
    )

    def handle(self, *args: Any, **opts: Any) -> None:
        out = refresh()
        self.stdout.write(f"created {out['created']}, marked {out['marked']}")
