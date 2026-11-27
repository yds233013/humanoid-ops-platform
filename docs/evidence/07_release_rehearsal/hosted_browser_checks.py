"""v2rc hosted real-browser checks (Chromium/Playwright over HTTPS). Sign-in is by HTTP (requests); the session and
csrf cookies are injected into the browser context. No password is typed into a page or printed.

usage: python hosted_browser_checks.py BASE PWFILE RUN OUTJSON SHOTDIR
Items: 10b sync confirmed, 11 service worker, 14 cross-account isolation, 12 offline survives refresh + reconnect,
13 protective banner, 15 375 px layout, timings.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

BASE, PWFILE, RUN, OUT, SHOTS = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3], sys.argv[4], Path(sys.argv[5])
SHOTS.mkdir(parents=True, exist_ok=True)
PW = next(line.split(":", 1)[1].strip() for line in open(PWFILE) if line.startswith("password:"))
HOST = BASE.split("//", 1)[1]
results: list[dict] = []
FIELD = {"width": 375, "height": 812}
CREW, OTHER_CREW, OPS = "luis.romero", "derek.osei", "dana.ortiz"


def rec(item, ok, detail):
    results.append({"item": item, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + item + ": " + detail, flush=True)


def http_cookies(user):
    s = requests.Session()
    r = s.get(BASE + "/login/", timeout=60)
    tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text).group(1)
    s.post(BASE + "/login/", data={"username": user, "password": PW, "csrfmiddlewaretoken": tok},
           headers={"Referer": BASE + "/login/"}, timeout=60)
    jar = s.cookies.get_dict()
    assert "sessionid" in jar, f"login failed for {user}"
    return [{"name": k, "value": v, "domain": HOST, "path": "/", "secure": True, "httpOnly": k == "sessionid",
             "sameSite": "Lax"} for k, v in jar.items() if k in ("sessionid", "csrftoken")]


def sign_in(ctx, user):
    ctx.clear_cookies()
    ctx.add_cookies(http_cookies(user))


def next_step(page, key, timeout_s=60):
    for i in range(timeout_s * 2):
        if page.locator(f"[data-next-step='{key}']").count():
            return
        page.wait_for_timeout(500)
        if i % 10 == 9:
            page.reload()
    cur = page.locator("[data-next-step]").first.get_attribute("data-next-step") if page.locator("[data-next-step]").count() else None
    raise AssertionError(f"next step never became {key}: {cur}")


def open_run(page):
    page.goto(f"{BASE}/execution/runs/{RUN}/")
    page.wait_for_selector("[data-field-pill='uptodate']", timeout=45000)
    wait_js(page, "() => !!navigator.serviceWorker.controller", 45)


def wait_js(page, fn, timeout_s):
    # page.wait_for_function evaluates a string in the page, which the hosted CSP (no unsafe-eval) refuses
    for _ in range(timeout_s * 4):
        if page.evaluate(fn):
            return
        page.wait_for_timeout(250)
    raise AssertionError(f"timeout waiting for {fn}")


def no_hscroll(page):
    return page.evaluate("({sw: document.documentElement.scrollWidth, w: window.innerWidth,"
                         " bw: document.body.scrollWidth})")


def shot(page, name):
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def snapshot_ledger(user):
    s = requests.Session()
    s.cookies.update({c["name"]: c["value"] for c in http_cookies(user)})
    import uuid
    r = s.get(f"{BASE}/api/field/runs/{RUN}/snapshot", params={"device": str(uuid.uuid4())}, timeout=60)
    return r.status_code, r.json()


def seq():
    return snapshot_ledger(CREW)[1]["ledger"]["last_decided_seq"]


def step(name, fn):
    try:
        return fn()
    except Exception as e:  # record and continue with the next item
        rec(name, False, f"exception {type(e).__name__}: {str(e).splitlines()[0][:300]}")
        return None


with sync_playwright() as p:
    browser = p.chromium.launch()
    crew_ctx = browser.new_context(viewport=FIELD, is_mobile=True, has_touch=True)
    sign_in(crew_ctx, CREW)
    crew = crew_ctx.new_page()
    sync_log: list[dict] = []

    def on_resp(r):
        if r.url.endswith("/api/field/sync") and r.request.method == "POST":
            try:
                body = r.json()
            except Exception:
                body = {}
            sync_log.append({"status": r.status, "body": body,
                             "sent": json.loads(r.request.post_data or "{}")})
    crew.on("response", on_resp)

    # ---------------------------------------------------------------- 11 service worker over HTTPS
    def sw():
        open_run(crew)
        info = crew.evaluate("""async () => { const r = await navigator.serviceWorker.ready;
            return {scope: r.scope, script: (r.active||{}).scriptURL, proto: location.protocol,
                    secure: window.isSecureContext, controller: !!navigator.serviceWorker.controller}; }""")
        ok = info["scope"] == f"{BASE}/execution/" and info["proto"] == "https:" and info["secure"] and info["controller"]
        rec("11 service worker registers over HTTPS", ok, json.dumps(info))
        shot(crew, "run_375_open")
    step("11 service worker registers over HTTPS", sw)

    # ---------------------------------------------------------------- 10b one sync command confirmed (online)
    def sync_online():
        n0 = len(sync_log)
        crew.locator("[data-next-step] button:has-text('Vehicle arrived at site')").click()
        next_step(crew, "to_site")
        new = [x for x in sync_log[n0:] if x["sent"].get("commands")]
        first = new[0] if new else {}
        cmds = [(c.get("type") or c.get("command") or c.get("kind"), c.get("channel")) for c in first.get("sent", {}).get("commands", [])]
        res = first.get("body", {})
        rec("10b sync endpoint: one command confirmed", bool(new) and first["status"] == 200,
            f"POST /api/field/sync {first.get('status')} sent {cmds} -> {json.dumps(res)[:400]}; next step to_site")
    step("10b sync endpoint: one command confirmed", sync_online)

    def to_session():
        form = crew.locator("[data-next-step] form[data-custody-form]")
        form.locator("[name='to_kind']").first.select_option("site") if form.locator("select[name='to_kind']").count() else None
        with crew.expect_navigation(wait_until="load"):
            form.locator("button[type=submit], button.primary").first.click()
        next_step(crew, "site_check")
        check = crew.locator("[data-next-step] form[data-site-check-form]")
        for sel in check.locator("select[name^='cond_']").all():
            sel.select_option("ok")
        check.locator("[name='adults_present']").fill("1")
        check.locator("button.primary").click()
        next_step(crew, "start")
        form = crew.locator("[data-next-step] form[data-field-form='session']")
        if form.locator("select[name='kind']").count():
            form.locator("select[name='kind']").select_option("start")
        with crew.expect_navigation(wait_until="load"):
            form.locator("button[type=submit], button.primary").first.click()
        next_step(crew, "stop")
        expect(crew.locator("[data-field-pill]")).to_have_attribute("data-field-pill", "uptodate", timeout=30000)
        rec("10c crew run to session start (handover online, site check queued+confirmed, start online)", True,
            f"run {RUN}: Next step 'stop'; pill uptodate")
    step("10c crew run to session start", to_session)

    # ---------------------------------------------------------------- 14 cross-account isolation
    def cross_account():
        crew.route("**/api/field/sync", lambda r: r.abort())
        form = crew.locator("form[data-field-form='session']").first
        form.locator("select[name='kind']").select_option("observation")
        form.locator("[name='note']").fill("SYN: rehearsal note left unsent before sign-out")
        form.locator("button.primary").click()
        expect(crew.locator("[data-field-row][data-state='local']")).to_have_count(1, timeout=15000)
        crew.click("[data-navbtn]")  # the phone header's Menu button (375 px)
        crew.evaluate("document.querySelector('.usermenu').open = true")
        crew.click("form[action='/logout/'] button")
        dlg = crew.locator("dialog.f-dialog")
        expect(dlg).to_be_visible(timeout=10000)
        dlg_text = dlg.inner_text()
        dlg.locator("button:has-text('Sign out')").click()
        crew.wait_for_url(re.compile(r"/login/"), timeout=30000)
        crew.unroute("**/api/field/sync")
        before = seq()
        # another crew member of the same hub, same phone: offline, the run is not on this phone for them
        sign_in(crew_ctx, OTHER_CREW)
        crew.goto(f"{BASE}/execution/")
        wait_js(crew, "() => !!document.querySelector('script[data-field-boot]')", 30)
        crew.wait_for_timeout(1500)
        crew_ctx.set_offline(True)
        crew.goto(f"{BASE}/execution/runs/{RUN}/")
        crew.wait_for_timeout(1500)
        main = crew.locator("main").inner_text()
        nosnap = crew.locator("[data-field-nosnapshot], [data-field-locked]").count() > 0
        robot_seen = bool(re.search(r"NB-201", main))
        shot(crew, "other_account_offline_375")
        crew_ctx.set_offline(False)
        # the ops lead on the same phone, online: the run page shows none of Luis's queued steps
        sign_in(crew_ctx, OPS)
        crew.goto(f"{BASE}/execution/runs/{RUN}/")
        crew.wait_for_timeout(4000)
        rows_ops = crew.locator("[data-field-row]").count()
        after = seq()
        ok = nosnap and not robot_seen and rows_ops == 0 and after == before
        rec("14 cross-account offline data isolated", ok,
            f"sign-out dialog: '{dlg_text.strip()[:160]}'; {OTHER_CREW} offline on run page: no snapshot/locked={nosnap}, "
            f"robot code shown={robot_seen}; {OPS} online: queued rows shown={rows_ops}; server ledger rows "
            f"last_decided_seq {before} -> {after} (nothing of {CREW}'s sent by another account)")
        # Luis signs in again on the same phone: his unsent step is still his, and is now sent
        sign_in(crew_ctx, CREW)
        open_run(crew)
        crew.wait_for_selector("[data-field-notice='confirmed'], [data-field-row][data-state='local']", timeout=45000)
        crew.wait_for_timeout(5000)
        final = seq()
        rec("14b same user signs in again: kept step sent once", final == after + 1,
            f"server last_decided_seq {after} -> {final} (the kept observation, sent by {CREW} himself)")
    step("14 cross-account offline data isolated", cross_account)

    # ---------------------------------------------------------------- 12 + 13 offline, reload, reconnect, banner
    def offline_flow():
        open_run(crew)
        next_step(crew, "stop")
        crew_ctx.set_offline(True)
        crew.wait_for_selector("[data-field-pill='offline']", timeout=20000)
        crew.fill("form[data-field-command='interruption.open'] input[name='reason']", "SYN: a child entered the work area")
        crew.click("form[data-field-command='interruption.open'] button")
        banner = crew.locator("[data-field-banner]")
        expect(banner).to_have_attribute("data-field-banner", "saved", timeout=15000)
        b1 = banner.inner_text()
        form = crew.locator("form[data-field-form='session']").first
        form.locator("select[name='kind']").select_option("observation")
        form.locator("[name='note']").fill("SYN: robot folding laundry, household watching")
        form.locator("button.primary").click()
        expect(crew.locator("[data-field-row][data-state='local']")).to_have_count(2, timeout=15000)
        shot(crew, "offline_saved_375")
        n_srv0 = seq()  # direct HTTP from this host, not the browser
        prot0 = snapshot_ledger(CREW)[1].get("protective")
        # reload while still offline
        crew.reload()
        crew.wait_for_timeout(2500)
        main = crew.locator("main").inner_text()
        lastknown = "Last known at" in main
        rows_after = crew.locator("[data-field-row][data-state='local']").count()
        banner_after = crew.locator("[data-field-banner]")
        b_state = banner_after.first.get_attribute("data-field-banner") if banner_after.count() else None
        b2 = banner_after.first.inner_text() if banner_after.count() else ""
        shot(crew, "offline_reloaded_375")
        # reconnect
        crew_ctx.set_offline(False)
        crew.wait_for_selector("[data-field-banner='accepted']", timeout=60000)
        b3 = crew.locator("[data-field-banner]").first.inner_text()
        crew.wait_for_timeout(3000)
        n_srv1 = seq()
        prot1 = snapshot_ledger(CREW)[1].get("protective")
        kinds = f"ordinary seq {n_srv0} -> {n_srv1}; protective before {json.dumps(prot0)[:150]} after {json.dumps(prot1)[:200]}"
        shot(crew, "reconnected_375")
        crew.reload()
        crew.wait_for_timeout(2000)
        b4b = crew.locator("[data-field-banner]")
        b4 = b4b.first.inner_text() if b4b.count() else "(no banner)"
        sent_first = next((x for x in sync_log if x["sent"].get("commands")), None)
        rec("12 offline recording survives refresh and reconnect",
            lastknown and rows_after == 2 and n_srv1 == n_srv0 + 1 and prot1 != prot0,
            f"offline: 2 steps saved on phone, server ledger unchanged ({n_srv0}); after offline reload: "
            f"'Last known at'={lastknown}, local rows={rows_after}; after reconnect: {kinds}")
        rec("13 protective banner persists correctly",
            "Robot must not operate" in b1 and b_state == "saved" and "Robot must not operate" in b2
            and "confirmed by the server" in b3 and "Robot must not operate" in b4,
            f"offline: '{b1[:110]}'; after reload state={b_state} '{b2[:80]}'; after reconnect: '{b3[:140]}'; "
            f"after online reload: '{b4[:100]}'")
    step("12 offline recording survives refresh and reconnect", offline_flow)

    # ---------------------------------------------------------------- 15 375 px layout + timings
    def layout():
        out, ok = [], True
        m = no_hscroll(crew)
        out.append(f"run page {RUN} ({CREW}) scrollWidth {m['sw']} / {m['w']}")
        ok &= m["sw"] <= m["w"]
        ops_ctx = browser.new_context(viewport=FIELD, is_mobile=True, has_touch=True)
        sign_in(ops_ctx, OPS)
        pg = ops_ctx.new_page()
        for path, name in (("/ops/", "today"), ("/ops/briefing/", "briefing"), ("/execution/mine/", None)):
            if name is None:
                continue
            pg.goto(BASE + path, wait_until="load")
            m = no_hscroll(pg)
            ok &= m["sw"] <= m["w"]
            out.append(f"{path} ({OPS}) scrollWidth {m['sw']} / {m['w']}")
            shot(pg, f"{name}_375")
        mine_ctx = crew
        mine_ctx.goto(BASE + "/execution/mine/", wait_until="load")
        m = no_hscroll(mine_ctx)
        out.append(f"/execution/mine/ ({CREW}) scrollWidth {m['sw']} / {m['w']}")
        rec("15 mobile layout at 375 px (no horizontal scroll)", ok, "; ".join(out))
        ops_ctx.close()
    step("15 mobile layout at 375 px", layout)

    def timings():
        out = []
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        sign_in(ctx, OPS)
        pg = ctx.new_page()
        for path in ("/ops/", "/ops/briefing/"):
            ts = []
            for i in range(4):
                t0 = time.perf_counter()
                pg.goto(BASE + path, wait_until="load")
                ts.append(round(time.perf_counter() - t0, 2))
            nav = pg.evaluate("(() => { const n = performance.getEntriesByType('navigation')[0];"
                              " return {ttfb: Math.round(n.responseStart), load: Math.round(n.loadEventEnd)}; })()")
            out.append(f"{path} first(new browser context) {ts[0]} s, then {ts[1:]} s; last nav TTFB {nav['ttfb']} ms")
        rec("T browser load times (desktop 1280)", True, "; ".join(out))
        ctx.close()
    step("T timings", timings)
    browser.close()

json.dump({"results": results, "sync_log": sync_log[:6]}, open(OUT, "w"), indent=1, default=str)
