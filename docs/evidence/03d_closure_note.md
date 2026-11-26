# 03D closure note: offline field screen

Branch `v2/phase03`. `main`, `v2-phase02-release` and both public demos are unchanged. All data is fictional.
Contract: `docs/13_v2_phase03_contract.md` section 15, with 15.10 (EM amendments) and the EM protective clarification
binding. Usability with real field crews is UNVERIFIED: every check here was done by the development team.

## Candidate

| Item | Value |
|---|---|
| SHA | `cdb0154` |
| Diff | `d97a0b2...cdb0154` |
| Built as | 03D-1 queue module (`0747f08`), 03D-2 interruption code + snapshot API + script serving (`29f4415`, `0bf349a`), 03D-3 run page + service worker + design review (`bcd95f7`, `50acd84`); merged with 05 (`4453c50`, `7539c84`) |
| Server changes | 15.7 code only; snapshot API is read-only; no migration in 03D |
| Full CI | `3fa3a18` (03D + 05 + 04 merged, plus the browser-test route fix): run 45903935449 success, 2534 passed, 0 failed. Earlier `81f9d9c`: 2533 passed, 1 setup error (Playwright route outliving its test; fixed in `3fa3a18`) |
| Perf | `cdb0154`: 45895674016, 45895683113, 45895692044, all success; sync-20 p95 533 / 397 / 479 ms (500-robot fixture) |

## What is built

- **`execution/field_static/queue.js`** (plain ES module, no build): account-bound IndexedDB `ops-field`
  (queues, commands, device, snapshots); atomic sequence allocation in one readwrite transaction; one sync coordinator
  per user:run:device via Web Locks with a 30 s lease-row fallback; the 15.3 state machine; batches of at most 20,
  protective first; frozen identical bodies on retry; results matched by key (+ seq, + channel when present); an HTTP
  200 without a per-command `confirmed` never confirms; `queue_schema` stamp with "Update required"; bootstrap from the
  snapshot ledger, never lowering `next_seq`; `SEQ_TAKEN` final with "Record again" (new key, new seq, old row kept);
  abandon + reconcile; `resolveProtective` per the EM clarification.
- **`GET /api/field/runs/<code>/snapshot`**: 401 before lookup, 403 `NOT_YOUR_RUN` reading nothing, `no-store`,
  14 statements (15 closed) independent of run size, minimal data only.
- **15.7 `ROBOT_INTERRUPTION_UNRESOLVED`** in readiness, the authorization refusal, Today, `/api/ops/exceptions` and
  the run page; the DB gate is unchanged (detected from its refusal text, pinned by a test).
- **Run page** (progressive enhancement; works as before without JS): connection pill, "Saved on this phone" vs
  "Confirmed", class A disabled offline ("Needs a connection. Do not start."), the persistent "Robot must not operate"
  banner with saved / sending / accepted / not accepted states and ops lead names, waiting for access + Try again,
  already recorded by + Record again, skip confirmation, Reconcile, "Last known at", locked offline view after
  sign-out, update ready / update required, sign-out confirmation with the unsent count.
- **Service worker** `/execution/sw.js` (scope `/execution/`): caches only the signed-out shell and scripts; private
  run data lives only in the account-bound IndexedDB store; activates after the current batch; never deletes IndexedDB.

## What is proven (real Chromium, Playwright)

- `tests/browser/test_v2_03d_queue.py` (21): two tabs allocate distinct consecutive seq, keys stable after reload;
  503 / network error resend the same keys, confirmed once; 200 with held / retry / no result is not confirmed;
  protective first and with no queue; final protective rejection stays "not accepted"; unsent and rejected reports are
  never cleared by an empty snapshot; NOT_PERMITTED -> waiting for access -> Try again; 401 keeps the queue; identity
  switch hides queue and snapshots; sending rows back to local after reload; SEQ_TAKEN -> Record again; abandon ->
  AFTER_ABANDONED; unknown schema -> update required, nothing deleted; lock held -> locked; batching 25+1 -> 20+6.
  Mutation checks: each of six deliberate bugs made at least one test fail.
- `tests/browser/test_v2_03d_run_page.py` (16) against the real server: network cut -> records -> reconnect ->
  confirmed in order; refresh and restart offline; class A disabled offline; protective banner first and sent first;
  final protective rejection keeps the banner; conflicting devices; sign-out then another user sees nothing; waiting
  for access; skip -> reconcile -> new generation; service worker update with unsent steps loses nothing; keyboard
  path; two tabs.
- Server: snapshot (13), script serving incl. traversal (8), 15.7 (9), run-page server (new) tests.
- Design review: `docs/evidence/03d_design_review.md`, 21 states x 1024 / 768 / 375 = 63 screenshots.

## Deviations and readings (for EM review)

1. The 03C server does not send `channel` per result; the phone matches by key and seq, and checks channel only if
   present. A missing, duplicate or mismatched key sends the row back to `local`.
2. A generation switch resets `next_seq` from the server (one more writer than 15.10 item 2, only on that switch).
3. Sign-out deletes that user's cached snapshots; their queue and protective banner stay for their next sign-in.
4. With JS, class B and C forms always save on the phone and then sync, even online (one path). After a confirmation
   the page reloads so the next-step card moves on, unless a form is in progress ("Refresh records").
5. `base.html` loads `/field/run.js` on every page (sign-out warning anywhere; sign-in page clears the identity). Still
   one inline script; hash unchanged.
6. New `/execution/field-shell/` (always signed out) is the cached offline page; the SW URL is `/execution/sw.js`.
7. Offline forms take their option data (crew codes, checklist items) from an inert template stored inside the
   account-bound snapshot under `page`; no household data.
8. `ops_lead` grants are regional only, so "ops leads at the hub" are all active ops leads. The ledger sequence is
   per run, shared by devices.
9. "Confirm with ops lead: robot is safe" needs online + a snapshot after the rejection (UI: under 2 minutes; module:
   under 5 minutes) showing no open protective state.

## Known limitations

- The server run page under the field layer is still dense at 375 px.
- Without Web Locks a crashed tab can block other tabs for up to 30 s (lease length).
- A sync batch of 20 still runs 631 statements (constant).
- CI installs Chromium with system dependencies (about a minute per run).
