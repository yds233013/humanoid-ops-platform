"""v2 03D-2: the field screen's scripts are served same-origin from execution/field_static/ (03d_modules.md).

/field/queue.js, /field/run.js and /execution/sw.js: JavaScript content type, no-cache, the service worker's
Service-Worker-Allowed header, whitelisted names only (no path traversal), and the CSP still allows them by
script-src 'self' with base.html keeping its single hashed inline script.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings

from execution import field_assets
from tests.conftest import make_user

pytestmark = pytest.mark.django_db

ASSETS = {
    "/field/queue.js": "queue.js",
    "/field/run.js": "run.js",
    "/execution/sw.js": "sw.js",
}


def test_project_has_no_staticfiles_app_so_the_tiny_view_serves_them():
    assert "django.contrib.staticfiles" not in settings.INSTALLED_APPS


@pytest.mark.parametrize("path", sorted(ASSETS))
def test_each_script_is_served_with_its_headers(path):
    for c in (Client(), _signed_in()):
        resp = c.get(path)
        assert resp.status_code == 200, path
        assert resp["Content-Type"] == "text/javascript; charset=utf-8"
        assert "no-cache" in resp["Cache-Control"]
        assert resp["X-Content-Type-Options"] == "nosniff"
        assert resp.content == (field_assets.ROOT / ASSETS[path]).read_bytes()
        if path == "/execution/sw.js":
            assert resp["Service-Worker-Allowed"] == "/execution/"
        else:
            assert "Service-Worker-Allowed" not in resp
    assert Client().head(path).status_code == 200
    assert Client().post(path).status_code == 405


def test_anonymous_response_is_exactly_no_cache():
    assert Client().get("/field/queue.js")["Cache-Control"] == "no-cache"


def test_placeholders_exist_and_only_whitelisted_names_are_served():
    assert {p.name for p in field_assets.ROOT.iterdir()} >= set(field_assets.FILES)
    c = Client()
    for path in (
        "/field/other.js",
        "/field/sw.js",
        "/execution/queue.js",
        "/field/../config/settings.py",
        "/field/%2e%2e/config/settings.py",
        "/field/..%2fconfig%2fsettings.py",
        "/field/queue.js/../../manage.py",
        "/field/%2e%2e%2f%2e%2e%2fmanage.py",
        "/field/queue.js%00.py",
        "/field/QUEUE.JS",
        "/field/",
    ):
        resp = c.get(path)
        assert resp.status_code in (301, 302, 404), (path, resp.status_code)
        assert b"SECRET_KEY" not in resp.content and b"import" not in resp.content[:200], path
    with pytest.raises(ValueError):
        field_assets.asset("../config/settings.py")
    with pytest.raises(Exception):  # noqa: B017 - Http404 from the reader itself
        field_assets._read("../manage.py")


def test_files_are_inside_the_field_static_folder():
    root = field_assets.ROOT
    assert root == Path(field_assets.__file__).resolve().parent / "field_static"
    for name in field_assets.FILES:
        assert (root / name).resolve().parent == root


@override_settings(CSP_ENABLED=True)
def test_csp_allows_same_origin_scripts_and_no_inline_script_was_added():
    policy = settings.CONTENT_SECURITY_POLICY
    script_src = policy.split("script-src", 1)[1].split(";", 1)[0]
    assert (
        "'self'" in script_src and "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    )
    resp = Client().get("/field/run.js")
    assert resp["Content-Security-Policy"] == policy
    html = (Path(settings.BASE_DIR) / "templates" / "base.html").read_text()
    assert html.count("<script>") == 1


def _signed_in() -> Client:
    c = Client()
    c.force_login(make_user("fa_viewer", "engineer"))
    return c
