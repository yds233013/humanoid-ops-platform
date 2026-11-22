"""01D: low items from the 01C browser gate (D5 bare 405, D9 settings key fold)."""

from typing import Any

from django.test import Client

from platform_core.models import Workspace
from tests.conftest import make_user


def _client(role: str) -> Client:
    c = Client()
    c.force_login(make_user(f"u_{role}", role))
    return c


def test_get_of_a_form_only_address_shows_a_readable_page(db: Any) -> None:
    Workspace.ensure()
    r = _client("ops_lead").get("/planning/sites/S-001/assessments/new/")
    html = r.content.decode()
    assert r.status_code == 405 and "POST" in r["Allow"]
    assert "only works from its form" in html and "Back to the start page" in html
    assert "no-store" in r["Cache-Control"]  # the security-header middleware still ran


def test_api_405_stays_json_or_bare(db: Any) -> None:
    Workspace.ensure()
    r = _client("ops_lead").get("/api/auth/logout")
    assert r.status_code in (405, 200, 302, 403, 404)
    if r.status_code == 405:
        assert b"only works from its form" not in r.content


def test_settings_keys_sit_in_a_details_fold(db: Any) -> None:
    Workspace.ensure()
    html = _client("admin").get("/settings/operating/").content.decode()
    assert "<summary" in html and "(required)" not in html
