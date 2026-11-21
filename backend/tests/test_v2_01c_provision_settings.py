"""v2 01C: owner provisioning (manage.py provision_settings) and the read-only Operating settings page."""

import io
import threading
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client

from accounts.roles import ALL_ROLES, PERMISSIONS
from fleet import provisioning as pv
from fleet.models import DeploymentSetting
from platform_core.models import AuditEvent, Workspace
from tests.conftest import make_user

REQ = {
    "consent_form_version": "CF-v1",
    "pre_visit_inspection_min": "30",
    "ops_readiness_lookahead_min": "2880",
}


def run(*args: str, **opts: Any) -> str:
    out = io.StringIO()
    call_command("provision_settings", *args, stdout=out, **opts)
    return out.getvalue()


def required_options(**extra: Any) -> dict[str, Any]:
    return {**REQ, **extra}


def value(key: str) -> str | None:
    row = DeploymentSetting.objects.filter(key=key).first()
    return row.value if row else None


@pytest.fixture(autouse=True)
def clean(db: Any) -> None:
    DeploymentSetting.objects.filter(key__in=pv.ALLOWED_KEYS).delete()


def test_provisions_all_allowlisted_keys_and_audits(db: Any) -> None:
    out = run(
        **required_options(
            ops_closeout_timezone="America/Los_Angeles",
            ops_custody_verify_grace_min="30",
            ops_return_grace_min="60",
            ops_inspection_grace_min="120",
            ops_periodic_due_lookahead_min="1440",
        )
    )
    assert out.count("set ") == 8
    assert value("ops_closeout_timezone") == "America/Los_Angeles" and value("ops_return_grace_min") == "60"
    ev = AuditEvent.objects.get(command="fleet.provision_settings")
    assert ev.actor is None and ev.outcome == "committed" and len(ev.result["changed"]) == 8


def test_unknown_key_and_mode_settings_are_rejected(db: Any, monkeypatch: Any) -> None:
    with pytest.raises(CommandError):
        call_command("provision_settings", "--demo-mode", "true")  # not an option at all
    with pytest.raises(CommandError):
        call_command("provision_settings", "--allow-draft-policies", "true")
    monkeypatch.setenv(
        "PROVISION_DEMO_MODE", "true"
    )  # env of a non-allowlisted key is ignored, never written
    run(**required_options())
    assert value("demo_mode") is None and "demo_mode" not in pv.ALLOWED_KEYS
    assert "allow_draft_policies" not in pv.ALLOWED_KEYS


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("pre_visit_inspection_min", "0"),
        ("pre_visit_inspection_min", "abc"),
        ("pre_visit_inspection_min", "-5"),
        ("ops_readiness_lookahead_min", "99999999999"),
        ("ops_return_grace_min", "1.5"),
        ("ops_closeout_timezone", "Mars/Olympus"),
        ("ops_closeout_timezone", " America/Los_Angeles x"),
        ("consent_form_version", "x" * 201),
    ],
)
def test_validation_errors_change_nothing(db: Any, key: str, bad: str) -> None:
    opts = required_options()
    opts[key] = bad
    with pytest.raises(CommandError, match="Nothing was changed"):
        run(**opts)
    assert DeploymentSetting.objects.filter(key__in=pv.ALLOWED_KEYS).count() == 0
    assert not AuditEvent.objects.filter(command="fleet.provision_settings").exists()


def test_missing_required_lists_them(db: Any) -> None:
    with pytest.raises(CommandError) as e:
        run(consent_form_version="CF-v1")
    assert "pre_visit_inspection_min" in str(e.value) and "ops_readiness_lookahead_min" in str(e.value)
    assert "consent_form_version," not in str(e.value) and value("consent_form_version") is None


def test_defaults_only_with_flag(db: Any) -> None:
    with pytest.raises(CommandError, match="ops_readiness_lookahead_min"):
        run(consent_form_version="CF-v1", pre_visit_inspection_min="30")
    run(consent_form_version="CF-v1", pre_visit_inspection_min="30", with_defaults=True)
    assert value("ops_readiness_lookahead_min") == "2880"


def test_idempotent_and_change_needs_allow_change(db: Any) -> None:
    run(**required_options())
    audits = AuditEvent.objects.filter(command="fleet.provision_settings").count()
    out = run(**required_options())
    assert (
        out.count("unchanged") == 3
        and AuditEvent.objects.filter(command="fleet.provision_settings").count() == audits
    )
    with pytest.raises(CommandError, match="--allow-change"):
        run(**required_options(pre_visit_inspection_min="45"))
    assert value("pre_visit_inspection_min") == "30"
    run(**required_options(pre_visit_inspection_min="45", allow_change=True))
    assert value("pre_visit_inspection_min") == "45"
    ev = AuditEvent.objects.filter(command="fleet.provision_settings").latest("id")
    assert ev.result["changed"]["pre_visit_inspection_min"] == {"from": "30", "to": "45"}


def test_values_from_environment(db: Any, monkeypatch: Any) -> None:
    for k, v in REQ.items():
        monkeypatch.setenv(f"PROVISION_{k.upper()}", v)
    run()
    assert value("consent_form_version") == "CF-v1"
    monkeypatch.setenv("PROVISION_CONSENT_FORM_VERSION", "CF-v2")
    run(consent_form_version="CF-v1")  # the option wins over the environment
    assert value("consent_form_version") == "CF-v1"


def test_settings_lock_is_taken_exclusively_first(db: Any, monkeypatch: Any) -> None:
    seen: list[bool] = []
    real = DeploymentSetting.objects.update_or_create

    def spy(*a: Any, **k: Any) -> Any:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT mode FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                "AND classid = 15 AND objid = 518479479 AND granted"
            )
            seen.extend(r[0] == "ExclusiveLock" for r in cur.fetchall())
        return real(*a, **k)

    monkeypatch.setattr(DeploymentSetting.objects, "update_or_create", spy)
    run(**required_options())
    assert seen and all(seen)


@pytest.mark.django_db(transaction=True)
def test_provisioning_waits_for_a_shared_settings_holder() -> None:
    from django.db import connections

    started, release, done = threading.Event(), threading.Event(), threading.Event()

    def holder() -> None:
        from django.db import transaction

        with transaction.atomic(), connections["default"].cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock_shared(64942988919)")
            started.set()
            release.wait(10)
        connections.close_all()

    t = threading.Thread(target=holder)
    t.start()
    assert started.wait(10)
    t2 = threading.Thread(target=lambda: (run(**required_options()), done.set(), connections.close_all()))
    t2.start()
    assert not done.wait(1.5), "the exclusive settings lock must wait for the shared holder"
    release.set()
    t.join()
    t2.join()
    assert done.is_set()


# ------------------------------------------------------------------ the read-only page
def _page_client(role: str) -> Client:
    c = Client()
    c.force_login(make_user(f"p_{role}", role))
    return c


def test_page_is_read_only_and_shows_values_and_missing(db: Any) -> None:
    Workspace.ensure()
    run(**required_options())
    c = _page_client("admin")
    r = c.get("/settings/operating/")
    html = r.content.decode()
    assert r.status_code == 200 and "Edit</button>" not in html and 'name="_form_token"' not in html
    assert "CF-v1" in html and "the deployment owner (via provision_settings)" in html
    assert "Not set - the deployment owner must provision it" in html  # e.g. the close-out time zone
    assert "data-incomplete" not in html
    assert c.post("/settings/operating/", {"x": "1"}).status_code in (403, 405)


def test_page_flags_missing_required_and_setup_checklist_incomplete(db: Any) -> None:
    Workspace.ensure()
    c = _page_client("admin")
    assert "data-incomplete" in c.get("/settings/operating/").content.decode()
    setup = c.get("/settings/setup/").content.decode()
    assert "Operating settings incomplete" in setup
    run(**required_options())
    setup = c.get("/settings/setup/").content.decode()
    assert "Operating settings incomplete" not in setup and "All required values are set" in setup


@pytest.mark.parametrize("role", ALL_ROLES)
def test_page_role_visibility(db: Any, role: str) -> None:
    Workspace.ensure()
    r = _page_client(role).get("/settings/operating/")
    assert r.status_code == (200 if role in PERMISSIONS["platform_core.read_operating_settings"] else 403)
    assert set(PERMISSIONS["platform_core.read_operating_settings"]) == {"admin", "ops_lead"}
