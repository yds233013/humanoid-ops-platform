"""v2 03D-3: server-side checks for the progressive run page (no browser; the browser tests are in
tests/browser/test_v2_03d_run_page.py).

* The offline shell (/execution/field-shell/) carries no account or run data, whoever asks for it.
* The run page keeps working without JavaScript: the same forms, posting to the same URLs; the field mount is
  empty; the only script added is an external same-origin module (CSP: still exactly one inline script).
* The field files keep their promises: sw.js caches only the shell and the field modules and never touches
  IndexedDB; run.js uses no eval and no inline handlers.
SYNTHETIC data only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client

from tests.execution_world import build_execution

pytestmark = pytest.mark.django_db
STATIC = Path(settings.BASE_DIR) / "execution" / "field_static"


@pytest.fixture
def w():
    return build_execution("d3s_")


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def test_offline_shell_carries_no_account_or_run_data(w):
    c = _client(w.field)
    c.get(f"/execution/runs/{w.run.code}/")  # a signed-in visit first
    resp = c.get("/execution/field-shell/")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert resp["Cache-Control"] == "no-cache"
    assert w.field.username not in html and "Sign out" not in html and "Signed in as" not in html
    assert w.run.code not in html and w.robot.code not in html
    assert 'data-field-app data-mode="shell"' in html
    assert 'data-user=""' in html  # the boot module never learns an identity from the shell
    signed_out = Client().get("/execution/field-shell/").content.decode()
    assert re.sub(r'value="[^"]{20,}"', "", signed_out) == re.sub(r'value="[^"]{20,}"', "", html)


def test_run_page_without_javascript_keeps_every_form_and_adds_only_an_empty_mount(w):
    html = _client(w.field).get(f"/execution/runs/{w.run.code}/").content.decode()
    assert re.search(r"<div data-field-app [^>]*></div>", html)
    for action in ("verify", "trip-event", "custody", "session", "interruptions/open"):
        assert f'action="/execution/runs/{w.run.code}/{action}/"' in html
    assert 'action="/execution/incidents/report/"' in html
    assert 'data-field-command="custody.verify"' in html and 'data-field-command="trip.arrived_site"' in html
    assert 'data-field-command="interruption.open"' in html and 'data-field-command="incident.report"' in html
    assert re.findall(r"<script>", html) == ["<script>"]  # one inline script (hashed by the CSP)
    assert f'<script type="module" src="/field/run.js" data-field-boot data-user="{w.field.pk}"' in html


def test_sign_in_page_clears_the_identity_through_the_boot_module():
    html = Client().get("/login/").content.decode()
    assert '<script type="module" src="/field/run.js" data-field-boot data-user=""' in html


def test_service_worker_caches_only_the_shell_and_field_modules_and_never_touches_indexeddb():
    sw = (STATIC / "sw.js").read_text()
    assert "indexedDB" not in sw.replace("IndexedDB", "")  # mentioned in comments only, never called
    assert re.search(r'const ASSETS = \["/field/run.js", "/field/queue.js"\];', sw)
    assert 'url.pathname.startsWith("/api/")) return;' in sw  # no API response is ever answered or cached
    puts = re.findall(r"cache\.put\(([^,]+),", sw)
    assert sorted(puts) == ["SHELL", "url", "url"]  # the shell and ASSETS entries only
    assert "skipWaiting()" in sw and sw.index("SKIP_WAITING") < sw.index("skipWaiting()")
    assert 'credentials: "omit"' in sw  # the shell is always the signed-out rendering


def test_run_js_has_no_eval_no_inline_handlers_and_no_innerhtml():
    js = (STATIC / "run.js").read_text()
    for banned in ("eval(", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert banned not in js, banned
    assert 'import * as Q from "/field/queue.js";' in js
