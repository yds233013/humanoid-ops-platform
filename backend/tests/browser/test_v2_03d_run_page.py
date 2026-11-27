"""v2 03D-3: the progressive run page and service worker in a real browser (contract 15.9 with 15.10).

Chromium through Playwright against the live server, with the network cut by ``context.set_offline`` and
server answers replaced by ``page.route`` only where a test injects a failure (a lost answer, a final
rejection). Everything else is the real 03C endpoint, the real domain and the real database.

Pieces owned by other 03D engineers: the serving view (/field/*.js, /execution/sw.js) and the snapshot API
(03D-2), and queue.js (03D-1). ``tests.browser.shim_urls`` uses the real ones when they resolve and test
stand-ins otherwise; ``FIELD_PIECES`` records which. Tests marked ``needs_merge`` assert behaviour that only the
real queue.js guarantees (Web Locks, IndexedDB transactions, schema checks).

SYNTHETIC data only. Run: ``uv run pytest tests/browser -m browser``.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page, Route, expect

from accounts import services as account_services
from accounts.models import RoleGrant
from execution.models import CustodyVerification, FieldCommand, Interruption, TripEvent
from tests.browser import shim_urls
from tests.conftest import make_user

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

NEEDS_A = "Needs a connection. Do not start."
NOT_ACCEPTED = "Not accepted by the server: the robot must still not operate. Contact your ops lead."
needs_merge = pytest.mark.skipif(
    shim_urls.FIELD_PIECES["queue_js"] != "real",
    reason="needs the real queue.js (03D-1): runs after the 03D merge",
)


# ------------------------------------------------------------------ helpers
def run_url(live_server: Any, w: Any) -> str:
    return f"{live_server.url}/execution/runs/{w.run.code}/"


def open_run(page: Page, url: str, pill: str = "uptodate") -> None:
    page.goto(url)
    page.wait_for_selector(f"[data-field-pill='{pill}']", timeout=20000)
    page.wait_for_function("!!navigator.serviceWorker.controller", timeout=20000)


def go_offline(context: BrowserContext, page: Page) -> None:
    context.set_offline(True)
    page.wait_for_selector("[data-field-pill='offline']")


def rows(page: Page) -> list[tuple[str, str, str]]:
    return page.eval_on_selector_all(
        "[data-field-row]",
        "els => els.map(e => [e.querySelector('b').textContent, e.dataset.state, e.dataset.reason || ''])",
    )


def open_fold(page: Page, name: str) -> None:
    page.click(f"details[data-other='{name}'] > summary")


def record_arrival(page: Page) -> None:
    open_fold(page, "trip")
    page.click("details[data-other='trip'] button:has-text('Vehicle arrived at site')")


def record_verify(page: Page) -> None:
    page.click("[data-next-step] button:has-text('Record crew verification')")


def sync_bodies(page: Page) -> list[dict]:
    seen: list[dict] = []
    page.on(
        "request",
        lambda r: seen.append(json.loads(r.post_data)) if r.url.endswith("/api/field/sync") else None,
    )
    return seen


def field_commands(w: Any) -> list[tuple[int | None, str, str]]:
    return list(
        FieldCommand.objects.filter(run=w.run).order_by("id").values_list("seq", "command", "outcome")
    )


# ------------------------------------------------------------------ 15.1: works exactly as today without JavaScript
def test_without_javascript_the_server_page_works_as_before(world, live_server, browser, sign_in):
    context = browser.new_context(java_script_enabled=False)
    sign_in(context, world.field)
    page = context.new_page()
    page.goto(run_url(live_server, world))
    expect(page.locator("[data-field-app]")).to_be_hidden()
    expect(page.locator("[data-field-pill]")).to_have_count(0)
    record_verify(page)  # a normal form post
    page.wait_for_load_state()
    assert CustodyVerification.objects.filter(custody_event__run=world.run).count() == 1
    assert FieldCommand.objects.filter(run=world.run).count() == 0  # nothing went through the queue
    context.close()


# ------------------------------------------------------------------ 15.9 network: cut, record, refresh, restart, reconnect
def test_network_cut_steps_survive_refresh_and_restart_then_confirm_in_order(
    world, live_server, page, context, sign_in
):
    sign_in(context, world.field)
    url = run_url(live_server, world)
    open_run(page, url)
    go_offline(context, page)
    record_verify(page)
    record_arrival(page)
    expect(page.locator("[data-field-row]")).to_have_count(2)
    assert [r[1] for r in rows(page)] == ["local", "local"]
    expect(page.locator("[data-field-steps]")).to_contain_text("Saved on this phone")

    page.reload()  # refresh while offline: the service worker answers with the shell
    expect(page.locator("[data-field-app][data-mode='shell']")).to_be_visible()
    expect(page.locator("main")).to_contain_text("Last known at")
    expect(page.locator("[data-field-row]")).to_have_count(2)

    page.close()  # restart: a new page in the same browser profile
    page = context.new_page()
    page.goto(url)
    expect(page.locator("[data-field-row]")).to_have_count(2)
    assert TripEvent.objects.filter(run=world.run).count() == 0

    context.set_offline(False)  # reconnect: sent in seq order, each confirmed, then the real page returns
    page.wait_for_selector("[data-field-app][data-mode='page']", timeout=30000)
    expect(page.locator("[data-field-row][data-state='confirmed']")).to_have_count(2)
    assert field_commands(world) == [
        (1, "custody.verify", "confirmed"),
        (2, "trip.arrived_site", "confirmed"),
    ]
    assert TripEvent.objects.filter(run=world.run, kind="arrived_site").count() == 1


# ------------------------------------------------------------------ 15.9 class A
def test_class_a_is_disabled_offline_and_never_queued(world, live_server, page, context, sign_in):
    sign_in(context, world.field)
    open_run(page, run_url(live_server, world))
    open_fold(page, "custody")  # to site = handover, class A
    custody = page.locator("details[data-other='custody'] form")
    expect(custody.locator("select[name='to_kind']")).to_have_value("site")
    expect(custody.locator("button.primary")).to_be_enabled()
    go_offline(context, page)
    expect(custody.locator("button.primary")).to_be_disabled()
    expect(custody.locator(".f-needs")).to_have_text("■" + NEEDS_A)
    page.evaluate("document.querySelector(\"details[data-other='custody'] form\").requestSubmit()")
    expect(page.locator("[data-field-row]")).to_have_count(0)
    page.reload()  # the shell: class A steps are shown, disabled, with the same words
    expect(page.locator("[data-field-step='custody.handover_to_site'] button")).to_be_disabled()
    expect(page.locator("[data-field-step='session.start']")).to_contain_text(NEEDS_A)
    context.set_offline(False)
    page.wait_for_selector("[data-field-app][data-mode='page']", timeout=30000)
    open_fold(page, "custody")
    expect(page.locator("details[data-other='custody'] button.primary")).to_be_enabled()
    assert FieldCommand.objects.filter(run=world.run).count() == 0


# ------------------------------------------------------------------ 15.9 protective
def test_protective_offline_shows_banner_first_and_is_sent_before_ordinary(
    world, live_server, page, context, sign_in
):
    sign_in(context, world.field)
    open_run(page, run_url(live_server, world))
    go_offline(context, page)
    record_arrival(page)
    page.fill(
        "form[data-field-command='interruption.open'] input[name='reason']", "SYNTHETIC: a child came close"
    )
    page.click("form[data-field-command='interruption.open'] button")
    banner = page.locator("[data-field-banner]")
    expect(banner).to_have_attribute("data-field-banner", "saved")
    expect(banner).to_contain_text("Robot must not operate")
    expect(banner).to_contain_text("saved on this phone, not yet confirmed")
    first = page.evaluate(
        "document.querySelector('main').firstElementChild.hasAttribute('data-field-protect')"
    )
    assert first, "the protective banner is the first thing on the page"

    bodies = sync_bodies(page)
    context.set_offline(False)
    page.wait_for_selector("[data-field-banner='accepted']", timeout=30000)
    expect(page.locator("[data-field-banner]")).to_contain_text("confirmed by the server")
    expect(page.locator("[data-field-banner]")).to_contain_text("stays stopped")
    first_batch = next(b for b in bodies if b["commands"])
    assert first_batch["commands"][0]["channel"] == "protective"
    assert [c["channel"] for c in first_batch["commands"]] == ["protective", "ordinary"]
    assert Interruption.objects.filter(run=world.run, resolution__isnull=True).count() == 1


def test_protective_final_rejection_keeps_the_banner_and_names_the_ops_lead(
    world, live_server, page, context, sign_in
):
    """15.10 item 3 + EM clarification: a final rejection never clears the banner; the explicit "robot is safe"
    action appears only online with a fresh snapshot showing no open protective state."""
    sign_in(context, world.field)
    open_run(page, run_url(live_server, world))

    def reject(route: Route) -> None:
        body = json.loads(route.request.post_data)
        resp = route.fetch()
        data = resp.json()
        for res, cmd in zip(data["results"], body["commands"], strict=True):
            if cmd["channel"] == "protective":
                res.update(
                    outcome="rejected",
                    reason_code="NOT_PERMITTED_FAKE",
                    final=True,
                    message="SYNTHETIC: refused for this test.",
                )
        route.fulfill(response=resp, json=data)

    page.route("**/api/field/sync", reject)
    go_offline(context, page)
    page.fill("form[data-field-command='interruption.open'] input[name='reason']", "SYNTHETIC: smoke smell")
    page.click("form[data-field-command='interruption.open'] button")
    context.set_offline(False)
    banner = page.locator("[data-field-banner='not_accepted']")
    expect(banner).to_be_visible(timeout=30000)
    expect(banner).to_contain_text(NOT_ACCEPTED)
    expect(banner).to_contain_text("SYNTHETIC: refused for this test.")
    page.click("text=Contact your ops lead")
    expect(page.locator("#f-leads")).to_contain_text("Dana Ortiz")
    # EM condition (03D ruling): ops leads are regional, and the list says so (never "for this hub").
    expect(page.locator("#f-leads")).to_contain_text("Regional ops leads")
    expect(page.locator("#f-leads")).not_to_contain_text("for this hub")
    # The interruption WAS recorded by the real server, so the snapshot shows it open: no "robot is safe" action.
    expect(page.locator("text=Confirm with ops lead: robot is safe")).to_have_count(0)
    page.reload()
    expect(page.locator("[data-field-banner='not_accepted']")).to_be_visible()


# ------------------------------------------------------------------ 15.9 unknown outcome and 200 without a result
def test_lost_answer_503_resends_the_same_keys_and_is_confirmed_once(
    world, live_server, page, context, sign_in
):
    sign_in(context, world.field)
    open_run(page, run_url(live_server, world))
    bodies = sync_bodies(page)
    lost = {"n": 0}

    def lose_first_answer(route: Route) -> None:
        body = json.loads(route.request.post_data)
        if body["commands"] and lost["n"] == 0:
            lost["n"] = 1
            route.fetch()  # the server commits ...
            route.fulfill(status=503, json={"error": "OUTCOME_UNKNOWN", "message": "SYNTHETIC lost answer"})
            return
        route.continue_()

    page.route("**/api/field/sync", lose_first_answer)
    record_verify(page)
    page.wait_for_selector("[data-field-notice='confirmed']", timeout=45000)  # confirmed, then refreshed
    sent = [b["commands"] for b in bodies if b["commands"]]
    assert len(sent) >= 2 and sent[0][0]["key"] == sent[1][0]["key"]
    assert CustodyVerification.objects.filter(custody_event__run=world.run).count() == 1
    assert field_commands(world) == [(1, "custody.verify", "confirmed")]


def test_http_200_without_a_confirmed_result_never_marks_a_step_done(
    world, live_server, page, context, sign_in
):
    sign_in(context, world.field)
    open_run(page, run_url(live_server, world))
    page.route(
        "**/api/field/sync",
        lambda r: r.fulfill(
            status=200,
            json={
                "server_time": "2026-11-27T09:41:07-08:00",
                "run": world.run.code,
                "generation": 1,
                "last_decided_seq": 0,
                "next_expected_seq": 1,
                "results": [],
            },
        ),
    )
    record_verify(page)
    page.wait_for_timeout(3000)
    assert [r[1] for r in rows(page)] == ["local"]
    expect(page.locator("[data-field-row] .status")).to_contain_text("Saved on this phone")
    expect(page.locator("[data-field-pill='uptodate']")).to_have_count(0)


# ------------------------------------------------------------------ 15.9 conflicting devices
def test_two_devices_record_the_same_step_one_is_already_recorded(world, live_server, browser, sign_in):
    url = run_url(live_server, world)
    phones = []
    for user in (world.field, world.ops):  # the run's crew: second and lead, each on their own phone
        ctx = browser.new_context(service_workers="allow", timezone_id="America/Los_Angeles")
        sign_in(ctx, user)
        pg = ctx.new_page()
        open_run(pg, url)
        go_offline(ctx, pg)
        record_arrival(pg)
        phones.append((ctx, pg))
    (ctx_a, pg_a), (ctx_b, pg_b) = phones
    ctx_a.set_offline(False)
    pg_a.wait_for_selector("[data-field-notice='confirmed']", timeout=30000)
    ctx_b.set_offline(False)
    row = pg_b.locator("[data-field-row][data-reason='SEQ_TAKEN']")
    expect(row).to_be_visible(timeout=30000)
    expect(row).to_contain_text("Already recorded by")
    expect(row).to_contain_text("Ravi Menon")
    expect(row.locator("button")).to_have_text("Record again as a new step")
    assert TripEvent.objects.filter(run=world.run, kind="arrived_site").count() == 1
    for ctx, _ in phones:
        ctx.close()


# ------------------------------------------------------------------ 15.9 accounts
def test_sign_out_asks_with_unsent_steps_and_another_user_never_sees_them(
    world, live_server, page, context, sign_in
):
    sign_in(context, world.field)
    page.set_viewport_size({"width": 1024, "height": 900})
    url = run_url(live_server, world)
    open_run(page, url)
    page.route(
        "**/api/field/sync", lambda r: r.abort()
    )  # the server cannot be reached: the step stays unsent
    record_verify(page)
    expect(page.locator("[data-field-row][data-state='local']")).to_have_count(1)
    page.evaluate("document.querySelector('.usermenu').open = true")
    page.click("form[action='/logout/'] button")
    dlg = page.locator("dialog.f-dialog")
    expect(dlg).to_contain_text(
        "1 step is not yet confirmed. They stay on this phone for Ravi Menon; another person cannot send them."
    )
    dlg.locator("text=Stay signed in").click()
    expect(page).to_have_url(url)
    page.evaluate("document.querySelector('.usermenu').open = true")
    page.click("form[action='/logout/'] button")
    page.locator("dialog.f-dialog button:has-text('Sign out')").click()
    page.wait_for_url(re.compile(r"/login/"))
    page.unroute("**/api/field/sync")

    sign_in(context, world.ops)  # another account on the same phone
    open_run(page, url)
    expect(page.locator("[data-field-row]")).to_have_count(0)
    assert FieldCommand.objects.filter(run=world.run).count() == 0  # nothing of Ravi's was sent by Dana
    assert CustodyVerification.objects.filter(custody_event__run=world.run).count() == 0


def test_after_sign_out_the_offline_shell_is_locked(world, live_server, page, context, sign_in):
    """15.10 item 1: sign-out makes the previous account's snapshot unreadable offline."""
    sign_in(context, world.field)
    page.set_viewport_size({"width": 1024, "height": 900})
    url = run_url(live_server, world)
    open_run(page, url)
    page.evaluate("document.querySelector('.usermenu').open = true")
    page.click("form[action='/logout/'] button")
    page.wait_for_url(re.compile(r"/login/"))
    context.set_offline(True)
    page.goto(url)
    locked = page.locator("[data-field-locked]")
    expect(locked).to_contain_text("Sign in again to see this run")
    expect(page.locator("main")).not_to_contain_text(world.robot.code)


def test_a_snapshot_is_never_shown_to_another_account(world, live_server, page, context, sign_in):
    sign_in(context, world.field)
    url = run_url(live_server, world)
    open_run(page, url)  # Ravi's snapshot is saved on this phone
    sign_in(context, world.ops)  # Dana signs in on the same phone (account switch)
    page.goto(f"{live_server.url}/execution/")
    page.wait_for_function("document.querySelector('script[data-field-boot]')")
    page.wait_for_timeout(1000)
    context.set_offline(True)
    page.goto(url)
    expect(page.locator("[data-field-nosnapshot]")).to_contain_text("This run is not saved on this phone")
    expect(page.locator("main")).not_to_contain_text("Last known at")


# ------------------------------------------------------------------ 15.9 access
def test_revoked_grant_waits_for_access_then_try_again_confirms(world, live_server, page, context, sign_in):
    sign_in(context, world.field)
    open_run(page, run_url(live_server, world))
    admin = make_user("d3_admin", "admin")
    for g in RoleGrant.objects.filter(user=world.field, revoked_at__isnull=True):
        account_services.revoke_role(admin, grant_id=g.pk, reason="SYNTHETIC revoked")
    record_verify(page)
    row = page.locator("[data-field-row][data-state='blocked_access']")
    expect(row).to_be_visible(timeout=30000)
    expect(row).to_contain_text("Waiting for access: ask an ops lead")
    RoleGrant.objects.create(user=world.field, role="field_operator", hub_id=world.run.visit.hub_id)
    row.locator("text=Try again").click()
    page.wait_for_selector("[data-field-notice='confirmed']", timeout=30000)
    assert field_commands(world)[-1] == (1, "custody.verify", "confirmed")


# ------------------------------------------------------------------ 15.9 abandonment
def test_skip_a_missing_step_then_reconcile_and_record_in_the_new_generation(
    world, live_server, page, context, sign_in
):
    """Another phone took steps 1 and 2 and never sent them (simulated by a first snapshot that says the server
    expects step 3 from this device's allocation). This phone's step 3 is held; the crew skips step 1."""
    sign_in(context, world.field)
    first = {"done": False}

    def ahead(route: Route) -> None:
        resp = route.fetch()
        data = resp.json()
        if not first["done"]:
            first["done"] = True
            data["ledger"]["next_expected_seq"] = 3
        route.fulfill(response=resp, json=data)

    page.route("**/api/field/runs/*/snapshot*", ahead)
    open_run(page, run_url(live_server, world), pill="online")
    record_verify(page)
    gap = page.locator("[data-field-gap]")
    expect(gap).to_contain_text("waiting for step 1", timeout=30000)
    gap.locator("button").click()
    dlg = page.locator("dialog.f-dialog")
    expect(dlg).to_contain_text("Skip step 1?")
    expect(dlg).to_contain_text("This cannot be undone.")
    dlg.locator("input").fill("SYNTHETIC: the other phone was lost")
    dlg.locator("button:has-text('Skip step 1')").click()
    rc = page.locator("[data-field-reconcile]")
    expect(rc).to_contain_text("Reconcile: step 1 was skipped", timeout=30000)
    expect(rc).to_contain_text("Not sent: replaced after a skipped step")
    expect(rc).to_contain_text("Server's current state")
    rc.locator("text=Record again as a new step").click()
    page.wait_for_selector("[data-field-notice='confirmed']", timeout=30000)
    outcomes = (
        FieldCommand.objects.filter(run=world.run).order_by("id").values_list("generation", "seq", "outcome")
    )
    assert (1, 1, "abandoned") in list(outcomes)
    assert (2, 1, "confirmed") in list(outcomes)
    assert CustodyVerification.objects.filter(custody_event__run=world.run).count() == 1


# ------------------------------------------------------------------ 15.9 / 15.10 item 4: service worker update
def test_service_worker_update_with_unsent_steps_loses_nothing(
    world, live_server, page, context, sign_in, monkeypatch
):
    sign_in(context, world.field)
    url = run_url(live_server, world)
    open_run(page, url)
    go_offline(context, page)
    record_verify(page)
    record_arrival(page)
    page.route("**/api/field/sync", lambda r: r.abort())  # keep them unsent while the update happens
    context.set_offline(False)
    before = page.evaluate("caches.keys()")
    shim_urls.publish_new_service_worker(monkeypatch)
    page.evaluate("navigator.serviceWorker.getRegistration('/execution/').then(r => r.update())")
    expect(page.locator("[data-field-notice='update-ready']")).to_contain_text(
        "reload when it suits you", timeout=30000
    )
    for _ in range(60):  # the new worker's activate step removes the older shell cache
        after = page.evaluate("caches.keys()")
        if any("next" in k for k in after) and not any(k in after for k in before):
            break
        page.wait_for_timeout(500)
    assert after != before and not any(k in after for k in before)  # only the old shell cache went away
    assert [r[1] for r in rows(page)] == ["local", "local"]
    page.unroute("**/api/field/sync")
    page.reload()
    page.wait_for_selector("[data-field-notice='confirmed']", timeout=30000)
    assert [r[2] for r in field_commands(world)] == ["confirmed", "confirmed"]


# ------------------------------------------------------------------ 15.10 item 2: two tabs (real queue.js only)
@needs_merge
def test_two_tabs_record_at_the_same_moment_get_distinct_consecutive_seq(
    world, live_server, page, context, sign_in
):
    sign_in(context, world.field)
    url = run_url(live_server, world)
    open_run(page, url)
    other = context.new_page()
    open_run(other, url)
    go_offline(context, page)
    other.wait_for_selector("[data-field-pill='offline']")
    open_fold(page, "trip")
    open_fold(other, "trip")
    page.evaluate(
        """() => { window.__go = () => document.querySelector("details[data-other='trip'] form").requestSubmit(); }"""
    )
    other.evaluate(
        """() => { window.__go = () => document.querySelector("details[data-other='trip'] form").requestSubmit(); }"""
    )
    page.evaluate("window.__go()")
    other.evaluate("window.__go()")
    page.wait_for_timeout(1500)
    keys_before = page.evaluate("async run => (await import('/field/queue.js')).list(run)", world.run.code)
    seqs = sorted(r["seq"] for r in keys_before)
    assert seqs == [1, 2]
    page.reload()
    keys_after = page.evaluate("async run => (await import('/field/queue.js')).list(run)", world.run.code)
    assert {r["key"]: r["seq"] for r in keys_after} == {r["key"]: r["seq"] for r in keys_before}


# ------------------------------------------------------------------ 15.8: keyboard path and visible focus
def test_keyboard_reaches_the_field_actions_with_a_visible_focus_ring(
    world, live_server, page, context, sign_in
):
    sign_in(context, world.field)
    page.set_viewport_size({"width": 375, "height": 812})
    url = run_url(live_server, world)
    open_run(page, url)
    go_offline(context, page)
    page.reload()  # the offline shell: every action is a button reached by Tab
    page.wait_for_selector("[data-field-app][data-mode='shell'] .f-app")
    reached = []
    for _ in range(40):
        page.keyboard.press("Tab")
        info = page.evaluate(
            "() => { const e = document.activeElement; const s = getComputedStyle(e);"
            " return [e.textContent.trim(), s.outlineStyle, s.outlineWidth, e.getBoundingClientRect().height]; }"
        )
        reached.append(info)
    names = [r[0] for r in reached]
    assert "Stop the session" in names and "Open an interruption" in names
    for text, style, width, height in reached:
        if text in ("Stop the session", "Open an interruption"):
            assert style == "solid" and width == "3px", (text, style, width)  # the existing focus ring
            assert height >= 44, (text, height)  # large touch targets
    page.focus("[data-fid='prot-interruption.open']")
    page.keyboard.press("Enter")
    expect(page.locator("dialog.f-dialog input[name='reason']")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("dialog.f-dialog")).to_have_count(0)
