"""01D: no half-built demo is ever served. build_demo_workspace writes a completion marker as its LAST step;
with DEMO_WORKSPACE=1 every request except /healthz and /static/ gets a 503 "being refreshed" page while the
marker is absent (a refresh running, or a failed one). /healthz keeps reporting the database and migrations."""

import pytest
from django.test import Client

from fleet.models import DeploymentSetting
from operations import demo_workspace as demo
from platform_core.middleware import DEMO_BUILD_MARKER, DEMO_REFRESHING_TEXT
from tests.conftest import make_user

pytestmark = pytest.mark.django_db(transaction=True)


def _marker(present: bool) -> None:
    if present:
        DeploymentSetting.objects.update_or_create(key=DEMO_BUILD_MARKER, defaults={"value": "now"})
    else:
        DeploymentSetting.objects.filter(key=DEMO_BUILD_MARKER).delete()


def _signed_in() -> Client:
    c = Client()
    c.force_login(make_user("dr_ops", "ops_lead"))
    return c


def test_without_the_marker_a_demo_deployment_serves_the_refresh_page(monkeypatch):
    monkeypatch.setenv("DEMO_WORKSPACE", "1")
    _marker(False)
    c = _signed_in()
    for url in ("/fleet/", "/ops/", "/login/", "/"):
        r = c.get(url)
        assert r.status_code == 503, url
        assert DEMO_REFRESHING_TEXT in r.content.decode() and r["Retry-After"] == "60"
        assert "<script" not in r.content.decode()
    api = c.get("/api/ops/overview")
    assert api.status_code == 503 and api.json()["error"] == "demo_refreshing" and api["Retry-After"] == "60"
    health = Client().get("/healthz")
    assert health.status_code == 200 and health.json()["status"] == "ok"  # never fails during a refresh


def test_with_the_marker_the_demo_is_served_normally(monkeypatch):
    monkeypatch.setenv("DEMO_WORKSPACE", "1")
    _marker(True)
    c = _signed_in()
    assert c.get("/fleet/").status_code == 200
    assert c.get("/api/ops/overview").status_code == 200


def test_without_demo_workspace_the_marker_is_never_looked_at(monkeypatch):
    monkeypatch.delenv("DEMO_WORKSPACE", raising=False)
    _marker(False)
    c = _signed_in()
    assert c.get("/fleet/").status_code == 200
    assert c.get("/api/ops/overview").status_code == 200


def test_a_build_that_fails_halfway_leaves_no_marker(monkeypatch):
    monkeypatch.setenv("DEMO_WORKSPACE", "1")
    _marker(True)  # a previous good build

    def boom(self, homes):  # noqa: ANN001, ARG001 - the failure in the middle of the build
        raise RuntimeError("simulated failure while booking visits")

    monkeypatch.setattr(demo._Builder, "visits", boom)
    with pytest.raises(RuntimeError, match="simulated failure"):
        demo.build(password="demo-refresh-password-1")
    assert not DeploymentSetting.objects.filter(key=DEMO_BUILD_MARKER).exists()
    assert Client().get("/fleet/").status_code == 503  # the partial workspace is not served
    assert Client().get("/healthz").status_code == 200
