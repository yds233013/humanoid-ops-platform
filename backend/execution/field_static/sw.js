// v2 03D-3: the field service worker (docs/13_v2_phase03_contract.md 15.1, 15.6 and 15.10 items 1 and 4).
//
// Served at /execution/sw.js with scope /execution/.
//   * Cache Storage holds ONLY the page shell (/execution/field-shell/, rendered signed out, no run or account
//     data) and the static field modules. No run page, no API response and no snapshot is ever cached here:
//     snapshots live in IndexedDB through queue.js, bound to the account (15.10 item 1).
//   * A run page that cannot be loaded is answered with the shell; run.js then shows this account's saved
//     snapshot as "Last known at HH:MM". Every other request goes to the network untouched.
//   * Updates (15.10 item 4): a new version installs and waits; the page tells it to activate after the
//     current sync batch ("SKIP_WAITING"). It never waits for an empty queue. Activation removes only older
//     caches of this worker. It never opens, changes or deletes IndexedDB: unsent steps are not its business.

const VERSION = "03d-3.1";
const CACHE = `ops-field-shell-${VERSION}`;
const SHELL = "/execution/field-shell/";
const ASSETS = ["/field/run.js", "/field/queue.js"];
const RUN_PAGE = /^\/execution\/runs\/[^/]+\/?$/;
const NAV_TIMEOUT_MS = 8000;
const ASSET_TIMEOUT_MS = 5000;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // The shell is fetched without cookies, so it is always the signed-out rendering.
    const shell = await fetch(SHELL, { credentials: "omit", cache: "no-store" });
    if (!shell.ok) throw new Error(`shell ${shell.status}`);
    await cache.put(SHELL, shell);
    await Promise.all(ASSETS.map(async (url) => {
      const resp = await fetch(url, { credentials: "same-origin", cache: "no-store" });
      if (resp.ok) await cache.put(url, resp);
    }));
  })());
  // No skipWaiting here: the page asks for it after the current batch (15.10 item 4).
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n.startsWith("ops-field-shell-") && n !== CACHE).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then((v) => { clearTimeout(timer); resolve(v); }, (e) => { clearTimeout(timer); reject(e); });
  });
}

async function runPage(request) {
  try {
    // Network first. The real page is never stored: it holds the household's records.
    return await withTimeout(fetch(request), NAV_TIMEOUT_MS);
  } catch (_) {
    const cache = await caches.open(CACHE);
    const shell = await cache.match(SHELL);
    if (shell) return shell;
    return new Response(
      "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" +
        "<title>No connection</title><p style='font:16px/1.5 system-ui;margin:24px'>No connection. Steps saved " +
        "on this phone stay saved. Open this page again when you have a connection.</p>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } },
    );
  }
}

async function asset(request, url) {
  const cache = await caches.open(CACHE);
  try {
    const resp = await withTimeout(fetch(request), ASSET_TIMEOUT_MS);
    if (resp.ok) await cache.put(url, resp.clone());
    return resp;
  } catch (err) {
    const hit = await cache.match(url);
    if (hit) return hit;
    throw err;
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // syncs, sign-out and every form post go straight to the network
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // never cached, never answered from here
  if (req.mode === "navigate" && RUN_PAGE.test(url.pathname)) {
    event.respondWith(runPage(req));
    return;
  }
  if (ASSETS.includes(url.pathname)) {
    event.respondWith(asset(req, url.pathname));
  }
});
