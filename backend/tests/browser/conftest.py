"""Fixtures for the 03D-3 real-browser tests (Playwright, Chromium). Marked ``browser``.

The live server runs this build with ``tests.browser.shim_urls`` (the real URLs plus test stand-ins for the
03D pieces another engineer owns, only where the real route is missing). Users get synthetic names so the
screens read like the product. Sessions are created with ``Client.force_login`` and handed to the browser as
cookies (test sessions only, on the local live server).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import clear_url_caches

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")  # Playwright's sync API runs an event loop

NAMES = {"ops": ("Dana", "Ortiz"), "field": ("Ravi", "Menon"), "field2": ("Jordan", "Lee")}


@pytest.fixture(scope="session", autouse=True)
def _static_url_for_live_server() -> None:
    """Django's live-server static handler needs a STATIC_URL; this project serves no static files."""
    from django.conf import settings

    if not settings.STATIC_URL:
        settings.STATIC_URL = "/static/"


@pytest.fixture
def field_urls(settings) -> Iterator[None]:
    settings.ROOT_URLCONF = "tests.browser.shim_urls"
    clear_url_caches()
    yield
    clear_url_caches()


@pytest.fixture
def world(transactional_db, field_urls) -> SimpleNamespace:
    from tests.execution_world import build_execution

    w = build_execution("ex_")
    for attr, (first, last) in NAMES.items():
        u = getattr(w, attr)
        u.first_name, u.last_name = first, last
        u.save(update_fields=["first_name", "last_name"])
    return w


@pytest.fixture
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "service_workers": "allow",
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
    }


@pytest.fixture
def sign_in(live_server) -> Callable:
    """sign_in(context, user): put a real session cookie for ``user`` into the browser context."""

    def _sign_in(context, user) -> None:
        c = Client()
        c.force_login(user)
        context.add_cookies(
            [{"name": "sessionid", "value": c.cookies["sessionid"].value, "url": live_server.url}]
        )

    return _sign_in
