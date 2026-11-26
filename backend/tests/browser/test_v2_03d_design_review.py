"""v2 03D-3 design review: real-browser screenshots of every field-screen state at 1024, 768 and 375 px
(contract 15.8) into docs/evidence/03d_design_review/. Not a test of behaviour (test_v2_03d_run_page.py is);
it only runs when HOP_03D_SHOTS=1. A few states need a server answer the demo cannot produce on demand (a final
protective rejection, a snapshot showing the robot safe, a held step); those are injected with page.route and
say so in docs/evidence/03d_design_review.md. SYNTHETIC data only.

    HOP_03D_SHOTS=1 uv run pytest tests/browser/test_v2_03d_design_review.py -m browser
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from accounts import services as account_services
from accounts.models import RoleGrant
from tests.browser import shim_urls
from tests.conftest import make_user

pytestmark = [
    pytest.mark.browser,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("HOP_03D_SHOTS") != "1", reason="screenshots only with HOP_03D_SHOTS=1"
    ),
]

OUT = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "03d_design_review"
WIDTHS = (1024, 768, 375)


def shot(page: Page, name: str, *, full: bool = True, dialog: bool = False, at: str | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for w in WIDTHS:
        page.set_viewport_size({"width": w, "height": 900 if w != 375 else 812})
        page.wait_for_timeout(250)
        if not full and not dialog:
            page.evaluate(
                "sel => { const el = sel && document.querySelector(sel); if (el) el.scrollIntoView(); else window.scrollTo(0, 0); }",
                at,
            )
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, f"{name} at {w}px scrolls sideways by {overflow}px"
        page.screenshot(path=str(OUT / f"{name}_{w}.png"), full_page=full and not dialog)
    page.set_viewport_size({"width": 375, "height": 812})


def top_only(page: Page, name: str, at: str | None = None) -> None:
    """The top of the page (where the field screen lives), or the element ``at``; not the whole server page."""
    shot(page, name, full=False, at=at)


def record_trip(page: Page) -> None:
    """Record the next trip event, from the Next step card or from the Trip events card's fold."""
    visible = page.locator("form[data-field-command^='trip.'] button:visible")
    if not visible.count():
        page.click("details[data-other='trip'] > summary")
    page.locator("form[data-field-command^='trip.'] button:visible").first.click()


def observe(page: Page, note: str) -> None:
    """Record an observation note (class C) from the Session card's form."""
    fold = page.locator("details[data-other='session']")
    form = fold if fold.count() else page.locator("[data-next-step]")
    if fold.count():
        fold.locator("summary").click()
    form.locator("select[name='kind']").select_option("observation")
    form.locator("input[name='note']").fill(note)
    form.locator("button.primary").click()


def test_design_review_screens(world, live_server, page, context, browser, sign_in, monkeypatch):
    w = world
    url = f"{live_server.url}/execution/runs/{w.run.code}/"
    page.set_viewport_size({"width": 375, "height": 812})
    page.emulate_media(reduced_motion="reduce")
    # Full-page captures stitch the viewport; a sticky header would be painted mid-page. Review captures only.
    context.add_init_script(
        "addEventListener('DOMContentLoaded', () => { const s = document.createElement('style');"
        " s.textContent = '.site-header{position:static!important}'; document.head.append(s); })"
    )
    sign_in(context, w.field)

    # 01 online, up to date (the whole page: the field screen adds only the pill)
    page.goto(url)
    page.wait_for_selector("[data-field-pill='uptodate']")
    page.wait_for_function("!!navigator.serviceWorker.controller")
    shot(page, "01_online_up_to_date")

    # 02 offline: a class C step saved on this phone, class A disabled with its words
    context.set_offline(True)
    page.wait_for_selector("[data-field-pill='offline']")
    page.click("[data-next-step] button:has-text('Record crew verification')")
    page.click("details[data-other='custody'] > summary")
    page.wait_for_selector("[data-field-row]")
    shot(page, "02_offline_saved_and_class_a_disabled")

    # 03 the offline shell after a refresh: "Last known at HH:MM"
    page.reload()
    page.wait_for_selector("[data-field-app][data-mode='shell'] .f-app")
    shot(page, "03_offline_shell_last_known")

    # 04 a record dialog in the shell (site check, from the saved form data)
    page.click("[data-field-step='site_check.record'] button")
    page.wait_for_selector("dialog.f-dialog")
    shot(page, "04_offline_shell_site_check_dialog", dialog=True)
    page.click("dialog.f-dialog button[data-cancel]")

    # 05 protective report saved offline: the banner, first on the page
    page.click("text=Open an interruption")
    page.fill("dialog.f-dialog input[name='reason']", "SYNTHETIC: a child came close to the robot")
    page.click("dialog.f-dialog button:has-text('Save on this phone')")
    page.wait_for_selector("[data-field-banner='saved']")
    top_only(page, "05_protective_saved_offline")

    # 06 syncing (the server answer is slowed down so the state can be seen)
    def slow(route: Route) -> None:
        page.wait_for_timeout(2500)
        try:
            route.continue_()
        except Exception:  # noqa: BLE001 - the route was removed while this answer waited
            pass

    page.route("**/api/field/sync", slow)
    context.set_offline(False)
    page.wait_for_selector("[data-field-pill='syncing']", timeout=15000)
    top_only(page, "06_syncing")
    page.unroute("**/api/field/sync")

    # 07 back on the real page: confirmed, protective report accepted (banner stays)
    page.wait_for_selector("[data-field-app][data-mode='page']", timeout=30000)
    page.wait_for_selector("[data-field-banner='accepted']", timeout=30000)
    top_only(page, "07_confirmed_protective_accepted")

    # 08 a final protective rejection (injected), with the ops lead names open
    def reject(route: Route) -> None:
        body = json.loads(route.request.post_data)
        resp = route.fetch()
        data = resp.json()
        for res, cmd in zip(data["results"], body["commands"], strict=True):
            if cmd["channel"] == "protective":
                res.update(
                    outcome="rejected",
                    reason_code="STATE_CONFLICT",
                    final=True,
                    message="Run EX-0001 is closed; this report was not recorded. (SYNTHETIC answer)",
                )
        route.fulfill(response=resp, json=data)

    page.route("**/api/field/sync", reject)
    page.fill("form[data-field-command='interruption.open'] input[name='reason']", "SYNTHETIC: wheel noise")
    page.click("form[data-field-command='interruption.open'] button")
    page.wait_for_selector("[data-field-banner='not_accepted']", timeout=30000)
    page.unroute("**/api/field/sync")
    page.click("text=Contact your ops lead")
    top_only(page, "08_protective_not_accepted_contact_lead")

    # 09 the EM's explicit reconciliation, offered only with a fresh snapshot showing no open protective state
    def safe(route: Route) -> None:
        resp = route.fetch()
        data = resp.json()
        data["protective"] = {"open": False, "kind": None, "since": None, "message": ""}
        route.fulfill(response=resp, json=data)

    page.route("**/api/field/runs/*/snapshot*", safe)
    page.reload()
    page.wait_for_selector("text=Confirm with ops lead: robot is safe", timeout=30000)
    top_only(page, "09_protective_not_accepted_robot_safe_offered")
    page.click("text=Confirm with ops lead: robot is safe")
    page.wait_for_selector("dialog.f-dialog")
    shot(page, "10_robot_safe_confirmation", dialog=True)
    page.click("dialog.f-dialog button[data-cancel]")
    page.unroute("**/api/field/runs/*/snapshot*")

    # 11 waiting for access, with Try again
    page.reload()
    page.route("**/api/field/sync", lambda r: r.abort())
    record_trip(page)  # saved while the grant is still there, then the grant is revoked before it is sent
    page.wait_for_selector("[data-field-row][data-state='local']")
    admin = make_user("shots_admin", "admin")
    for g in RoleGrant.objects.filter(user=w.field, revoked_at__isnull=True):
        account_services.revoke_role(admin, grant_id=g.pk, reason="SYNTHETIC revoked for the screenshot")
    page.unroute("**/api/field/sync")
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_selector("[data-field-row][data-state='blocked_access']", timeout=30000)
    top_only(page, "11_waiting_for_access")
    RoleGrant.objects.create(user=w.field, role="field_operator", hub_id=w.run.visit.hub_id)
    page.click("[data-field-row][data-state='blocked_access'] >> text=Try again")
    page.wait_for_selector("[data-field-notice='confirmed']", timeout=30000)

    # 12 sign-out confirmation with unsent steps
    page.route("**/api/field/sync", lambda r: r.abort())
    observe(page, "SYNTHETIC: household asked a question")
    page.wait_for_selector("[data-field-row][data-state='local']")
    page.set_viewport_size({"width": 1024, "height": 900})
    page.evaluate("document.querySelector('.usermenu').open = true")
    page.evaluate("document.querySelector(\"form[action='/logout/']\").requestSubmit()")
    page.wait_for_selector("dialog.f-dialog", timeout=10000)
    shot(page, "12_sign_out_with_unsent_steps", dialog=True)
    page.click("dialog.f-dialog button[data-cancel]")
    page.unroute("**/api/field/sync")

    # 13 update ready (a new service worker version while steps may be unsent)
    shim_urls.publish_new_service_worker(monkeypatch, "shots")
    page.evaluate("navigator.serviceWorker.getRegistration('/execution/').then(r => r.update())")
    page.wait_for_selector("[data-field-notice='update-ready']", timeout=30000)
    page.wait_for_selector("text=reload when it suits you", timeout=30000)
    top_only(page, "13_update_ready")

    # 14 sign in again (the session ended): steps stay on this phone
    context.clear_cookies()  # the session ended while the page is open
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_selector("[data-field-notice='signin']", timeout=30000)
    top_only(page, "14_sign_in_again")
    sign_in(context, w.field)

    # 15 a second phone of the crew: Already recorded by ..., Record again as a new step
    phone = browser.new_context(
        service_workers="allow", timezone_id="America/Los_Angeles", reduced_motion="reduce"
    )
    sign_in(phone, w.ops)
    p2 = phone.new_page()
    p2.set_viewport_size({"width": 375, "height": 812})
    p2.goto(url)
    p2.wait_for_selector("[data-field-pill='uptodate']")
    p2.wait_for_function("!!navigator.serviceWorker.controller")
    phone.set_offline(True)
    p2.wait_for_selector("[data-field-pill='offline']")
    page.goto(url)
    page.wait_for_selector("[data-field-pill='uptodate']")
    context.set_offline(True)
    page.wait_for_selector("[data-field-pill='offline']")
    for pg in (page, p2):
        record_trip(pg)
    context.set_offline(False)
    page.wait_for_selector("[data-field-notice='confirmed']", timeout=30000)
    phone.set_offline(False)
    p2.wait_for_selector("[data-field-row][data-reason='SEQ_TAKEN']", timeout=30000)
    top_only(p2, "15_already_recorded_by")

    # 16 skip a missing step: the gap, the confirmation naming the step, then Reconcile (held step injected)
    phone2 = browser.new_context(
        service_workers="allow", timezone_id="America/Los_Angeles", reduced_motion="reduce"
    )
    sign_in(phone2, w.ops)
    p3 = phone2.new_page()
    p3.set_viewport_size({"width": 375, "height": 812})
    first = {"done": False}

    def ahead(route: Route) -> None:
        resp = route.fetch()
        data = resp.json()
        if not first["done"]:
            first["done"] = True
            data["ledger"]["next_expected_seq"] = data["ledger"]["next_expected_seq"] + 2
        route.fulfill(response=resp, json=data)

    p3.route("**/api/field/runs/*/snapshot*", ahead)
    p3.goto(url)
    p3.wait_for_selector("[data-field-pill]")
    observe(p3, "SYNTHETIC: robot waited near the stairs")
    p3.wait_for_selector("[data-field-gap]", timeout=30000)
    top_only(p3, "16_waiting_for_an_earlier_step")
    p3.click("[data-field-gap] button")
    p3.wait_for_selector("dialog.f-dialog")
    p3.fill("dialog.f-dialog input", "SYNTHETIC: the other phone was lost")
    shot(p3, "17_skip_step_confirmation", dialog=True)
    p3.click("dialog.f-dialog button.f-danger")
    p3.wait_for_selector("[data-field-reconcile]", timeout=30000)
    top_only(p3, "18_reconcile_after_skip", at="[data-field-reconcile]")
    phone2.close()
    phone.close()

    # 19 update required (this phone meets a queue written by a newer client)
    page.goto(url)
    page.wait_for_selector("[data-field-pill]")
    if shim_urls.FIELD_PIECES["queue_js"] == "real":
        page.evaluate("""() => new Promise((ok, no) => {
          const r = indexedDB.open('ops-field');
          r.onsuccess = () => { const db = r.result; const t = db.transaction('queues', 'readwrite'); const s = t.objectStore('queues');
            s.openCursor().onsuccess = (e) => { const c = e.target.result; if (c) { const v = c.value; v.queue_schema = 99; c.update(v); c.continue(); } };
            t.oncomplete = () => ok(); t.onerror = () => no(t.error); };
          r.onerror = () => no(r.error); })""")
        page.evaluate("window.dispatchEvent(new Event('online'))")
        page.reload()
        page.wait_for_selector("[data-field-notice='update-required']", timeout=30000)
        top_only(page, "19_update_required")

    # 20 the offline shell after sign-out: locked
    page.evaluate("document.querySelector('.usermenu').open = true")
    page.evaluate("document.querySelector(\"form[action='/logout/']\").requestSubmit()")
    page.wait_for_timeout(1000)
    if page.locator("dialog.f-dialog").count():
        page.click("dialog.f-dialog button:has-text('Sign out')")
    page.wait_for_url("**/login/**")
    context.set_offline(True)
    page.goto(url)
    page.wait_for_selector("[data-field-locked]")
    shot(page, "20_offline_shell_signed_out_locked")
    context.set_offline(False)

    # 21 not your run (a field operator of the hub who is not on this run's crew)
    other = browser.new_context(service_workers="allow", timezone_id="America/Los_Angeles")
    sign_in(other, w.field2)
    p4 = other.new_page()
    p4.goto(url)
    p4.wait_for_selector("[data-field-notice='not-your-run']", timeout=30000)
    p4.set_viewport_size({"width": 375, "height": 812})
    top_only(p4, "21_not_your_run")
    other.close()
    expect(page.locator("body")).to_be_visible()
