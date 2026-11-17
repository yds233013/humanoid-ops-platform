"""Concurrency against real Postgres: two connections released together by a barrier."""

import threading

import pytest
from django.db import connection

from accounts.models import RoleGrant
from accounts.services import grant_role
from platform_core.commands import Conflict
from platform_core.models import AuditEvent
from tests.conftest import make_user


def run_parallel(fns):
    barrier = threading.Barrier(len(fns))
    results: list[object] = [None] * len(fns)

    def wrap(i, fn):
        try:
            barrier.wait()
            results[i] = fn()
        except Exception as e:  # noqa: BLE001 - we inspect it in the test
            results[i] = e
        finally:
            connection.close()

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("attempt", range(5))
def test_same_idempotency_key_in_parallel_has_one_effect(attempt):
    admin = make_user(f"c_admin{attempt}", "admin")
    target = make_user(f"c_target{attempt}")
    results = run_parallel(
        [lambda: grant_role(admin, user_id=target.pk, role="ops_lead", idempotency_key="same")] * 2
    )
    assert all(isinstance(r, dict) for r in results), results
    assert sorted(r["replayed"] for r in results) == [False, True]
    assert RoleGrant.objects.filter(user=target).count() == 1
    assert AuditEvent.objects.filter(outcome="committed").count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("attempt", range(5))
def test_parallel_duplicate_grants_with_different_keys_yield_one_grant(attempt):
    admin = make_user(f"d_admin{attempt}", "admin")
    target = make_user(f"d_target{attempt}")
    results = run_parallel(
        [
            lambda k=k: grant_role(admin, user_id=target.pk, role="engineer", idempotency_key=k)
            for k in ("a", "b")
        ]
    )
    successes = [r for r in results if isinstance(r, dict)]
    conflicts = [r for r in results if isinstance(r, Conflict)]
    assert (len(successes), len(conflicts)) == (1, 1), results
    assert RoleGrant.objects.filter(user=target, role="engineer").count() == 1
