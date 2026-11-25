"""v2 02D: write the predictor log (owner only). Run as the schema owner (DJANGO_DB_IDENTITY=owner); NOT an
HTTP endpoint. Calls ``operations_log_predictor_signals()`` (operations/0008), which logs every departure
not yet logged and writes nothing else but one audit row. The runtime role cannot execute it.

The signals are observational only; nothing in planning, readiness, fleet or hub reads them.
"""

from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection, transaction


def log_predictor_signals() -> int:
    """Log every departure not yet logged; returns the number of new observations."""
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SELECT operations_log_predictor_signals()")
        row = cur.fetchone()
    return int(row[0]) if row else 0


class Command(BaseCommand):
    help = "Record candidate predictor signals for departures not yet logged (owner only; observational)."

    def handle(self, *args: Any, **opts: Any) -> None:
        n = log_predictor_signals()
        self.stdout.write(
            f"Logged {n} predictor observation(s). Observational only; not used for any decision."
        )
