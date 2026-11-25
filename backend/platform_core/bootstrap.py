"""First-admin bootstrap (v2 contract section 12.2).

A fresh deployment has no users. The first admin can only be created with the one-time token from the
deployment environment (WORKSPACE_BOOTSTRAP_TOKEN), and only while the workspace is not initialized. The admin
user, the admin role grant, the audit row and the ``initialized`` flag are written in ONE transaction under a
row lock on the workspace singleton, so two concurrent attempts produce exactly one admin and the loser is
refused. Afterwards bootstrap is permanently disabled (the flag is in the database, not in the environment).

Bootstrap records facts only: one user, one admin role grant, its audit rows. It never creates an approval of
any kind (contract section 4).
"""

import hmac
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from accounts.roles import ADMIN
from platform_core.middleware import current_request_id
from platform_core.models import AuditEvent, Workspace

TOKEN_ENV = "WORKSPACE_BOOTSTRAP_TOKEN"


class BootstrapRefused(Exception):  # noqa: N818 - a refusal, shown to the visitor
    """Setup was refused. ``reason`` is one of: disabled, initialized, bad_token, invalid."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def configured_token() -> str:
    return os.environ.get(TOKEN_ENV, "")


def setup_open() -> bool:
    """True while a token is configured and the workspace has not been initialized."""
    if not configured_token():
        return False
    return not Workspace.objects.filter(pk=1, initialized=True).exists()


def token_matches(supplied: str) -> bool:
    expected = configured_token()
    if not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


def bootstrap_first_admin(
    *, token: str, username: str, password: str, workspace_name: str = "", email: str = ""
) -> int:
    """Create the first admin. Returns the new user's id. Raises BootstrapRefused."""
    if not configured_token():
        raise BootstrapRefused("disabled", "Setup is not enabled on this deployment.")
    Workspace.ensure()
    username = username.strip()
    with transaction.atomic():
        ws = Workspace.objects.select_for_update().get(pk=1)  # serialization point for concurrent attempts
        if ws.initialized:
            raise BootstrapRefused("initialized", "This workspace is already set up.")
        if not token_matches(token):
            raise BootstrapRefused("bad_token", "The setup token is not correct.")
        if not username:
            raise BootstrapRefused("invalid", "Choose a username.")
        User = get_user_model()
        try:
            validate_password(password)
        except ValidationError as exc:
            raise BootstrapRefused("invalid", " ".join(exc.messages)) from None
        try:
            with transaction.atomic():
                user = User.objects.create_user(username=username, password=password, email=email)
        except IntegrityError:
            raise BootstrapRefused("invalid", "That username is taken.") from None
        # v2 03A-3: the runtime role cannot write accounts_rolegrant; accounts_bootstrap_admin (accounts/0006)
        # gives the first admin grant only while no admin grant exists and the workspace is not initialized.
        with connection.cursor() as cur:
            cur.execute("SELECT accounts_bootstrap_admin(%s, %s)", [user.pk, current_request_id.get()])
        if workspace_name.strip():
            ws.name = workspace_name.strip()[:120]
        ws.initialized = True
        ws.initialized_at = timezone.now()
        ws.save(update_fields=["name", "initialized", "initialized_at"])
        AuditEvent.objects.create(
            actor=user,
            command="platform_core.bootstrap_first_admin",
            outcome=AuditEvent.OUTCOME_COMMITTED,
            entity_type="workspace",
            entity_id="1",
            payload={"username": username},
            result={"user_id": user.pk, "role": ADMIN},
            request_id=current_request_id.get(),
        )
        return int(user.pk)
