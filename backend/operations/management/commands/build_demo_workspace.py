"""Build the demo workspace "Northbay Home Robotics" (v2 contract section 5, 01D). Demo databases only.

    DJANGO_DB_IDENTITY=owner python manage.py build_demo_workspace            # empty database
    DJANGO_DB_IDENTITY=owner python manage.py build_demo_workspace --reset    # daily rebuild (destroys data)

Refuses a production or non-demo database, and a non-empty one without --reset; --reset itself refuses
unless the workspace is a demo workspace (demo flag on AND demo_mode = 'true'). Deterministic for a given
date: visits are placed from 7 days before to 14 days after the workspace's local today. See
operations/demo_workspace.py and docs/runbook_deploy_backup_rollback.md (section 8b, "Demo workspace").
"""

import os
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from operations import demo_workspace as demo


class Command(BaseCommand):
    help = "Build the fictional demo workspace (demo databases only; --reset destroys and rebuilds it)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Destroy every row of this demo workspace first (refused unless it is a demo workspace and "
            "DEMO_DATABASE_FINGERPRINT matches this database).",
        )
        parser.add_argument(
            "--print-fingerprint",
            action="store_true",
            help="Print this database's fingerprint (DEMO_DATABASE_FINGERPRINT) and exit; writes nothing.",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        if opts["print_fingerprint"]:
            self.stdout.write(demo.database_fingerprint())
            return
        t0 = time.perf_counter()
        password = os.environ.get(demo.PASSWORD_ENV, "")
        try:
            if not password:
                raise demo.DemoRefused(
                    f"Set {demo.PASSWORD_ENV} (see .env.example): the demo logins use it as their password."
                )
            with demo.build_lock():
                demo.check_can_build(reset=bool(opts["reset"]))
                if not demo.is_empty():
                    demo.wipe()  # checks the demo markers and the database fingerprint again itself
                    self.stdout.write("reset: every row of the previous demo workspace was destroyed")
                summary = demo.build(password=password)
        except demo.DemoRefused as refused:
            raise CommandError(str(refused)) from None
        total = time.perf_counter() - t0
        self.stdout.write(f"{demo.COMPANY}: demo workspace built for {summary.anchor} ({demo.TZ})")
        for name, at in summary.timings.items():
            self.stdout.write(f"  {at:6.1f} s  {name}")
        self.stdout.write(f"  total {total:.1f} s")
        self.stdout.write(
            f"Journey J4: visit {summary.j4_visit} is ready for the day's checks and the ops lead's "
            f"decision. Open incident {summary.incident}; robot {summary.attention_robot} has no periodic "
            "inspection yet. Past visits are historical scheduled visits (no execution records)."
        )
        if summary.practice:
            runs = ", ".join(f"{visit} ({run}, {robot})" for visit, run, robot in summary.practice)
            self.stdout.write(
                f"Hub day (J8): practice runs of today, recorded and closed at build time: {runs}. "
                f"Maintenance due soon on {summary.due_soon_robot}; "
                f"overdue (blocked) on {summary.overdue_robot}. "
                f"Predictor log: {summary.predictor_rows} observation(s)."
            )
        self.stdout.write(f"Demo logins (password: the {demo.PASSWORD_ENV} environment variable):")
        for username, role, note in summary.logins:
            self.stdout.write(f"  {username:16s} {role:17s} {note}")
