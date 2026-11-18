"""Consistent read snapshots for the operations command center (Slice 5 contract section 5, D5).

Every page and API response is built inside ONE PostgreSQL transaction opened with
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` as its first statement, so every query sees
the same snapshot and PostgreSQL itself refuses any write. Its time is `transaction_timestamp()`, passed
explicitly to every time-dependent calculation. No row locks, advisory locks, `run_command`, `nextval` or
`set_config` run inside it. This is a consistent snapshot, not a promise of the latest state at response time.

The snapshot must be the OUTERMOST transaction: if a caller is already inside `atomic()` (for example
ATOMIC_REQUESTS), a nested savepoint would silently inherit the outer isolation level, so we refuse instead.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from typing import Any

from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse

from accounts.permissions import has_permission

SET_SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


class SnapshotNotOutermost(RuntimeError):
    pass


@contextmanager
def read_snapshot() -> Iterator[datetime]:
    """Open the snapshot transaction and yield its time. Must not be nested in another transaction."""
    if connection.in_atomic_block:
        raise SnapshotNotOutermost("the operations snapshot must be the outermost transaction")
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(SET_SNAPSHOT)  # first statement of the real PostgreSQL transaction
            cur.execute("SELECT transaction_timestamp()")
            row = cur.fetchone()
        assert row is not None
        yield row[0]


def snapshot_view(permission: str, *, json: bool = False) -> Callable[..., Any]:
    """View decorator: login + permission check INSIDE the snapshot, then `view(request, at, ...)`."""

    def decorate(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            with read_snapshot() as at:
                if not request.user.is_authenticated or not has_permission(request.user, permission):
                    if json:
                        return JsonResponse({"detail": "forbidden"}, status=403)
                    return HttpResponseForbidden("You do not have access to the operations command center.")
                return view(request, at, *args, **kwargs)

        return wrapper

    return decorate
