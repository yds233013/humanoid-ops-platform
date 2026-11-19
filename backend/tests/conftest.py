import os
from collections.abc import Callable, Iterator
from datetime import timedelta

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import RoleGrant

User = get_user_model()
AUDIT = "platform_core_auditevent"


@pytest.fixture(autouse=True, scope="session")
def _allow_test_teardown_to_flush_audit():
    """Test-only: Django's transactional-test teardown TRUNCATEs every table, which the audit trigger
    (correctly) rejects. The schema OWNER may disable its own trigger - that is the documented owner
    trust boundary - so teardown does exactly that around the flush. The runtime role cannot."""
    from django.db.backends.postgresql.operations import DatabaseOperations

    original = DatabaseOperations.sql_flush

    def sql_flush(self, style, tables, *, reset_sequences=False, allow_cascade=False):
        sql = original(self, style, tables, reset_sequences=reset_sequences, allow_cascade=allow_cascade)
        guarded = [
            t
            for t in tables
            if t.startswith(
                ("platform_core_", "fleet_", "planning_", "readiness_", "execution_", "operations_")
            )
        ]
        # Test-only: teardown TRUNCATEs ~100 tables; on a loaded Docker host that alone can exceed the app's
        # 10 s statement_timeout. Lift it for the flush transaction only (SET LOCAL); lock_timeout is unchanged,
        # so a flush blocked by a leaked lock still fails fast.
        return (
            ["SET LOCAL statement_timeout = '120s';"]
            + [f"ALTER TABLE {t} DISABLE TRIGGER USER;" for t in guarded]
            + list(sql)
            + [f"ALTER TABLE {t} ENABLE TRIGGER USER;" for t in guarded]
            # Slice 7: the hold-policy registry row (id 1) is schema, not test data (inserted by fleet/0008); the
            # flush must not leave the serialization point missing for the next test.
            + (
                ["INSERT INTO fleet_holdpolicyregistry (id) VALUES (1) ON CONFLICT (id) DO NOTHING;"]
                if "fleet_holdpolicyregistry" in tables
                else []
            )
        )

    DatabaseOperations.sql_flush = sql_flush
    yield
    DatabaseOperations.sql_flush = original


@pytest.fixture(autouse=True)
def _fast_password_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # tests only


def make_user(username: str, *roles: str, expired: bool = False):
    user = User.objects.create_user(username=username, password="test-pass-123456")
    for role in roles:
        if expired:
            start = timezone.now() - timedelta(days=10)
            RoleGrant.objects.create(
                user=user, role=role, valid_from=start, valid_to=start + timedelta(days=1)
            )
        else:
            RoleGrant.objects.create(user=user, role=role)
    return user


@pytest.fixture
def admin(db):
    return make_user("t_admin", "admin")


@pytest.fixture
def ops_lead(db):
    return make_user("t_ops", "ops_lead")


@pytest.fixture
def plain_user(db):
    return make_user("t_plain")


@pytest.fixture
def client_for() -> Callable[[object], APIClient]:
    def _make(user: object) -> APIClient:
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    return _make


@pytest.fixture
def app_role_conn(transactional_db) -> Iterator[psycopg.Connection]:
    """A raw connection to the TEST database as the runtime role (ops_app), autocommit."""
    conn = psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()
