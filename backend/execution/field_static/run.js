// v2 03D-3: the progressive run page (docs/13_v2_phase03_contract.md section 15, 15.10 wins).
//
// Loaded on every page by base.html as an external module (CSP: script-src 'self'; no inline code, no eval).
//   * Every page: keeps the phone's saved steps bound to the signed-in account (Q.setIdentity) and asks before
//     signing out while steps are unsent (15.5).
//   * The run page (data-mode="page"): adds the connection pill, the "Robot must not operate" banner and the
//     steps saved on this phone; class B and C forms are saved on this phone first and then sent (15.4); class A
//     and other online-only forms are disabled while offline ("Needs a connection. Do not start.").
//   * The offline shell (data-mode="shell", served by sw.js when there is no connection): the same field screen
//     built from this account's saved snapshot, always labelled "Last known at HH:MM".
// Without JavaScript none of this runs and the server page works exactly as before.
// All queue state lives in IndexedDB through queue.js (03D-1); this file never writes next_seq or a command's
// identity. Text is plain and calm; status is words plus shape, never colour alone. SYNTHETIC data only.

import * as Q from "/field/queue.js";

// ------------------------------------------------------------------ context
const boot = document.querySelector("script[data-field-boot]");
const app = document.querySelector("[data-field-app]");
const MODE = app ? app.dataset.mode || "page" : null; // "page" | "shell" | null (any other page)
const RUN = app ? app.dataset.run || runFromPath() : null;
const USER = boot && boot.dataset.user ? Number(boot.dataset.user) : null;
const USER_NAME = (boot && boot.dataset.userName) || "";
const SIGNIN_URL = "/login/?next=" + encodeURIComponent(location.pathname);
const KNOWN_RUNS = "ops-field-runs"; // localStorage: run codes this phone has opened (codes only, no run data)
const FLASH = "ops-field-flash"; // sessionStorage: keys confirmed just before a refresh
const RECONCILE = "ops-field-reconcile"; // sessionStorage: the open Reconcile view after a skipped step
const SYNC_EVERY_MS = 30000;
const FRESH_MS = 120000; // a snapshot fetched in this page within 2 minutes counts as fresh
const REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// What each field command is called on the screen (names first; the command name never leads a line).
const LABELS = {
  "trip.arrived_site": "Arrived at the home",
  "trip.departed_site": "Left the home",
  "trip.arrived_hub": "Back at the hub",
  "custody.back_to_vehicle": "Robot back in the vehicle",
  "custody.verify": "Departure handover verified",
  "site_check.record": "Site check",
  "session.observation": "Observation note",
  "session.pause": "Pause the session",
  "session.stop": "Stop the session",
  "incident.report": "Incident report",
  "interruption.open": "Interruption",
  "departure.record": "Departure",
  "session.start": "Start the session",
  "session.resume": "Resume the session",
  "custody.handover_to_site": "Hand the robot over to the site",
  "robot.recover": "Recover the robot",
};
const CLASS_OF = {
  "departure.authorize": "A", "departure.record": "A", "session.start": "A", "session.resume": "A",
  "custody.handover_to_site": "A", "robot.recover": "A",
  "session.pause": "B", "session.stop": "B", "incident.report": "B", "interruption.open": "B",
  "trip.arrived_site": "C", "trip.departed_site": "C", "trip.arrived_hub": "C", "custody.back_to_vehicle": "C",
  "custody.verify": "C", "site_check.record": "C", "session.observation": "C",
};
const NEEDS_A = "Needs a connection. Do not start.";
const NEEDS_ONLINE = "Needs a connection to record this.";
const NEEDS_ONCE = "Needs a connection once to start recording.";
const UNSENT = new Set(["local", "sending", "blocked_access"]);

const S = {
  snapshot: null, snapshotAt: 0, rows: [], banner: null, reachable: true, syncing: false, syncPromise: null,
  lastOkAt: null, auth: null, updateRequired: false, update: null, holdSync: false, queueReady: false,
  dirty: false, identity: null, flash: [], reconcile: readSession(RECONCILE), knownConfirmed: null,
};

// ------------------------------------------------------------------ small helpers
function runFromPath() {
  const m = location.pathname.match(/^\/execution\/runs\/([^/]+)\/?$/);
  return m ? decodeURIComponent(m[1]) : null;
}
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function zone() {
  return (app && app.dataset.tz) || (S.snapshot && S.snapshot.page && S.snapshot.page.tz) || undefined;
}
function hhmm(iso) {
  if (!iso) return "";
  const d = iso instanceof Date ? iso : new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  try {
    return new Intl.DateTimeFormat("en-US", {
      hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: zone(), timeZoneName: "short",
    }).format(d);
  } catch (_) {
    return d.toTimeString().slice(0, 5);
  }
}
function timeEl(iso) {
  const d = new Date(iso);
  return h("time", { datetime: Number.isNaN(d.getTime()) ? null : d.toISOString(), text: hhmm(iso) });
}
function readSession(key) {
  try { return JSON.parse(sessionStorage.getItem(key) || "null"); } catch (_) { return null; }
}
function writeSession(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, JSON.stringify(value));
  } catch (_) { /* private mode: the view simply does not survive a refresh */ }
}
function rememberRun(userId, run) {
  if (!userId || !run) return;
  try {
    const all = JSON.parse(localStorage.getItem(KNOWN_RUNS) || "{}");
    const mine = new Set(all[userId] || []);
    mine.add(run);
    all[userId] = Array.from(mine).slice(-50);
    localStorage.setItem(KNOWN_RUNS, JSON.stringify(all));
  } catch (_) { /* nothing private is lost: the queue itself is in IndexedDB */ }
}
function knownRuns(userId) {
  try { return (JSON.parse(localStorage.getItem(KNOWN_RUNS) || "{}")[userId] || []).slice(); } catch (_) { return []; }
}
function plural(n, one, many) { return n === 1 ? one : many; }
function online() { return navigator.onLine && S.reachable; }
function label(command) { return LABELS[command] || "Field step"; }
// A saved row says what was recorded ("Arrived at the home"); the checklist uses the server's step names.
function stepLabel(row) {
  if (LABELS[row.command]) return LABELS[row.command];
  const snap = S.snapshot && (S.snapshot.steps || []).find((s) => s.command === row.command);
  return (snap && snap.label) || "Field step";
}
function isUnsent(row) { return UNSENT.has(row.state); }
function scrollTo(el) {
  if (el && el.scrollIntoView) el.scrollIntoView({ block: "start", behavior: REDUCED ? "auto" : "smooth" });
}

// The workspace-zone offset of a datetime-local value ("2026-11-27T09:41"), as ISO 8601 with its offset.
function zonedIso(local, tz) {
  if (!local) return null;
  const guess = new Date(local + ":00Z");
  if (Number.isNaN(guess.getTime())) return null;
  let offsetMin = -new Date(local).getTimezoneOffset();
  if (tz) {
    try {
      const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
        timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
      }).formatToParts(guess).map((p) => [p.type, p.value]));
      const asZone = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour % 24, +parts.minute);
      offsetMin = Math.round((asZone - guess.getTime()) / 60000);
    } catch (_) { /* keep the phone's own offset */ }
  }
  const sign = offsetMin >= 0 ? "+" : "-";
  const abs = Math.abs(offsetMin);
  return `${local}:00${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

// ------------------------------------------------------------------ dialogs (confirmations name the thing)
function dialog({ title, body, confirm, cancel = "Cancel", danger = false, field = null, extra = null }) {
  return new Promise((resolve) => {
    const id = "f-dlg-" + Math.random().toString(36).slice(2);
    let input = null;
    let err = null;
    if (field) {
      const ctl = field.multiline
        ? h("textarea", { id: id + "-in", rows: 3, required: field.required ? true : null, maxlength: field.max || 500 })
        : h("input", { id: id + "-in", required: field.required ? true : null, maxlength: field.max || 500 });
      input = ctl;
      err = h("p", { class: "f-err", id: id + "-err", hidden: true });
      ctl.setAttribute("aria-describedby", id + "-err");
    }
    const ok = h("button", { type: "submit", class: danger ? "f-danger" : "primary", value: "ok", text: confirm });
    const no = cancel ? h("button", { type: "button", "data-cancel": true, text: cancel }) : null; // null: a notice
    const form = h("form", { method: "dialog" },
      h("h2", { id: id + "-h", text: title }),
      ...(Array.isArray(body) ? body : [body]).map((b) => (typeof b === "string" ? h("p", { text: b }) : b)),
      field ? h("div", { class: "field" }, h("label", { for: id + "-in", text: field.label }), input, err) : null,
      extra,
      h("div", { class: "f-actions" }, no, ok));
    const dlg = h("dialog", { class: "f-dialog", "aria-labelledby": id + "-h" }, form);
    const done = (value) => { dlg.close(); dlg.remove(); resolve(value); };
    if (no) no.addEventListener("click", () => done({ ok: false }));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done({ ok: false }); });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (input && field.required && !input.value.trim()) {
        err.textContent = field.missing || "Write a short note first.";
        err.hidden = false;
        input.focus();
        return;
      }
      done({ ok: true, value: input ? input.value.trim() : null, form });
    });
    document.body.append(dlg);
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
    const first = extra && extra.querySelector("input:not([type='hidden']):not(.sr), select, textarea");
    (input || first || no || ok).focus();
  });
}

// ------------------------------------------------------------------ sign-out (every page, 15.5)
async function unsentFor(userId) {
  const runs = new Set(knownRuns(userId));
  if (RUN) runs.add(RUN);
  let n = 0;
  for (const run of runs) {
    try { n += (await Q.list(run)).filter(isUnsent).length; } catch (_) { /* a broken row never blocks sign-out */ }
  }
  return n;
}
function hookSignOut() {
  document.addEventListener("submit", async (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || form.getAttribute("action") !== "/logout/") return;
    if (form.dataset.fieldConfirmed === "1") return;
    e.preventDefault();
    let n = 0;
    try { n = await unsentFor(USER); } catch (_) { n = 0; }
    if (n > 0) {
      const who = USER_NAME || "you";
      const answer = await dialog({
        title: "Sign out with unsent steps?",
        body: [
          `${n} ${plural(n, "step is", "steps are")} not yet confirmed. They stay on this phone for ${who}; another person cannot send them.`,
          "They are sent the next time you sign in here with a connection.",
        ],
        confirm: "Sign out",
        cancel: "Stay signed in",
      });
      if (!answer.ok) return;
    }
    try { await Q.setIdentity(null); } catch (_) { /* the server sign-out still happens */ }
    form.dataset.fieldConfirmed = "1";
    form.submit();
  }, true);
}

// ------------------------------------------------------------------ snapshot and sync
function extractPage() {
  // The minimum the offline forms need, read from the server page: crew (code + name), site-check templates,
  // unverified custody numbers, incident lists, the zone. Nothing about the household is copied.
  const page = { tz: zone() || null, crew: [], templates: [], unverified: [], event_types: [], knowledge_sources: [] };
  const seen = new Set();
  document.querySelectorAll('[data-site-check-form] select[name="verifier"] option, [data-field-command="custody.verify"] select[name="verifier"] option')
    .forEach((o) => { if (o.value && !seen.has(o.value)) { seen.add(o.value); page.crew.push({ code: o.value, label: o.textContent.trim() }); } });
  const data = document.querySelector("template[data-field-offline]");
  if (data) {
    // The page's inert offline data (all checklists, the crew, unverified custody), present at every step.
    const c = data.content;
    c.querySelectorAll("[data-person]").forEach((u) => { if (!seen.has(u.dataset.person)) { seen.add(u.dataset.person); page.crew.push({ code: u.dataset.person, label: u.textContent.trim() }); } });
    c.querySelectorAll("[data-template]").forEach((t) => page.templates.push({
      version: t.dataset.template, demo: t.dataset.demo === "1",
      items: Array.from(t.querySelectorAll("[data-item]")).map((b) => ({ id: b.dataset.item, label: b.textContent.trim() })),
    }));
    c.querySelectorAll("[data-custody-seq]").forEach((x) => { const n = Number(x.dataset.custodySeq); if (n && !page.unverified.includes(n)) page.unverified.push(n); });
  }
  const form = page.templates.length ? null : document.querySelector("[data-site-check-form]");
  if (form) {
    form.querySelectorAll("fieldset").forEach((fs) => {
      const radio = fs.querySelector('input[name="template"]');
      if (!radio) return;
      const items = [];
      fs.querySelectorAll("select[name^='cond_']").forEach((sel) => {
        const lab = sel.closest("label");
        const text = lab ? Array.from(lab.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim() : sel.name;
        items.push({ id: sel.name.slice(5), label: text });
      });
      page.templates.push({ version: radio.value, demo: /demo checklist/i.test(fs.querySelector("legend").textContent), items });
    });
  }
  document.querySelectorAll('[data-field-command="custody.verify"] input[name="custody_seq"]').forEach((i) => {
    const n = Number(i.value);
    if (n && !page.unverified.includes(n)) page.unverified.push(n);
  });
  const inc = document.querySelector('[data-field-command="incident.report"]');
  if (inc) {
    inc.querySelectorAll('select[name="event_type"] option').forEach((o) => page.event_types.push([o.value, o.textContent.trim()]));
    inc.querySelectorAll('select[name="knowledge_source"] option').forEach((o) => page.knowledge_sources.push([o.value, o.textContent.trim()]));
  }
  return page;
}

async function loadSnapshot() {
  try { S.snapshot = await Q.snapshot(RUN); } catch (_) { S.snapshot = null; }
}

async function refreshSnapshot() {
  if (!navigator.onLine) return;
  let resp;
  try {
    const device = await Q.deviceId();
    resp = await fetch(`/api/field/runs/${encodeURIComponent(RUN)}/snapshot?device=${encodeURIComponent(device)}`, {
      credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
    });
  } catch (_) {
    S.reachable = false;
    return;
  }
  S.reachable = true;
  if (resp.status === 401) { S.auth = "signin"; return; }
  if (resp.status === 403) { S.auth = "not_your_run"; return; }
  if (!resp.ok) return;
  let snap;
  try { snap = await resp.json(); } catch (_) { return; }
  const before = S.snapshot;
  snap.page = MODE === "page" ? extractPage() : (before && before.page) || null;
  try {
    await Q.saveSnapshot(snap);
    S.snapshot = snap;
    S.snapshotAt = Date.now();
    S.auth = null;
  } catch (_) {
    // A snapshot for another account than this phone's current identity is never kept or shown.
    return;
  }
  try {
    await Q.ensureQueue(RUN, snap.ledger);
    S.queueReady = true;
  } catch (_) { /* recording shows "Needs a connection once to start recording" */ }
}

function syncNow() {
  if (S.syncPromise) return S.syncPromise;
  if (S.holdSync || S.auth === "not_your_run" || S.updateRequired) return Promise.resolve();
  if (!navigator.onLine) { render(); return Promise.resolve(); }
  S.syncing = true;
  render();
  S.syncPromise = (async () => {
    let decided = 0;
    try {
      for (let round = 0; round < 10; round += 1) {
        const r = await Q.syncOnce(RUN, {});
        const status = r && r.status;
        if (status === "offline") { S.reachable = false; break; }
        S.reachable = true;
        if (status === "signin") { S.auth = "signin"; break; }
        if (status === "not_your_run") { S.auth = "not_your_run"; break; }
        if (status === "update_required") { S.updateRequired = true; break; }
        if (status === "locked") break; // another tab of this phone is sending this queue
        if (status !== "ok") break;
        S.auth = null;
        S.lastOkAt = new Date();
        decided += r.decided || 0;
        const rows = await Q.list(RUN);
        if (!r.decided || !rows.some((x) => x.state === "local")) break;
      }
    } catch (_) {
      S.reachable = false;
    } finally {
      S.syncing = false;
      S.syncPromise = null;
    }
    const held = S.rows.some((x) => x.channel === "ordinary" && x.state === "local");
    if (decided > 0 || (held && online())) {
      // A step still waiting after a good sync may be held behind a missing one: re-read the server's ledger.
      await refreshSnapshot();
      await refresh();
      afterConfirmed();
    } else {
      await refresh();
    }
  })();
  return S.syncPromise;
}

// The server page shows the records as of its load. When new steps are confirmed and the crew is not in the
// middle of a form, refresh it so the Next step card moves on; otherwise offer the refresh.
function afterConfirmed() {
  if (MODE !== "page") return;
  const fresh = S.rows.filter((r) => r.state === "confirmed" && !S.knownConfirmed.has(r.key));
  if (!fresh.length) return;
  fresh.forEach((r) => S.knownConfirmed.add(r.key));
  if (S.dirty || document.querySelector("dialog[open]") || S.rows.some((r) => r.state === "local" || r.state === "sending")) {
    S.flash = fresh.map((r) => r.key);
    S.offerRefresh = true;
    render();
    return;
  }
  writeSession(FLASH, fresh.map((r) => ({ key: r.key, label: stepLabel(r), at: r.decided_at || null })));
  location.reload();
}

async function refresh() {
  try { S.rows = await Q.list(RUN); } catch (_) { S.rows = []; }
  if (S.knownConfirmed === null) S.knownConfirmed = new Set(S.rows.filter((r) => r.state === "confirmed").map((r) => r.key));
  try { S.banner = await Q.protectiveBanner(RUN); } catch (_) { S.banner = null; }
  try { S.identity = await Q.identity(); } catch (_) { S.identity = null; }
  render();
}

// ------------------------------------------------------------------ recording (class B and C, 15.4)
function formCommand(form) {
  if (form.dataset.fieldCommand) return form.dataset.fieldCommand;
  if (form.dataset.fieldForm === "session") {
    const kind = (form.querySelector('[name="kind"]') || {}).value;
    return kind ? "session." + kind : null;
  }
  if (form.dataset.fieldForm === "custody") {
    const to = (form.querySelector('[name="to_kind"]') || {}).value;
    if (to === "site") return "custody.handover_to_site";
    if (to === "vehicle" && form.dataset.from === "site") return "custody.back_to_vehicle";
    return null; // to the hub, or a recovery: online only, the normal form post
  }
  return null;
}
function formClass(form) {
  if (form.dataset.fieldOnline) return "online";
  const cmd = formCommand(form);
  if (!cmd) return form.dataset.fieldForm ? "online" : null;
  return CLASS_OF[cmd] || "online";
}

function payloadFrom(command, form) {
  const v = (name) => {
    const el = form.querySelector(`[name="${name}"]`);
    return el ? String(el.value || "").trim() : "";
  };
  const fail = (msg) => { throw Object.assign(new Error(msg), { fieldMessage: msg }); };
  switch (command) {
    case "trip.arrived_site": case "trip.departed_site": case "trip.arrived_hub":
      return {};
    case "custody.verify": {
      const seq = Number(v("custody_seq"));
      if (!seq || !v("verifier")) fail("Choose who verifies the handover first.");
      return { custody_seq: seq, verifier: v("verifier") };
    }
    case "custody.back_to_vehicle": {
      if (!v("custodian") || !v("received_by")) fail("Choose who hands over and who receives the robot first.");
      const p = { custodian: v("custodian"), received_by: v("received_by") };
      if (v("note")) p.note = v("note");
      return p;
    }
    case "site_check.record": {
      const radio = form.querySelector('input[name="template"]:checked');
      if (!radio) fail("Choose the checklist first.");
      const scope = radio.closest("fieldset") || form;
      const conditions = {};
      scope.querySelectorAll("select[name^='cond_']").forEach((s) => { if (s.value) conditions[s.name.slice(5)] = s.value; });
      const adults = v("adults_present");
      if (adults === "" || !/^\d+$/.test(adults)) fail("Enter how many adults are present.");
      if (!v("verifier")) fail("Choose the crew member who verifies the check.");
      return { template: radio.value, adults_present: Number(adults), conditions, verifier: v("verifier") };
    }
    case "session.observation":
      if (!v("note")) fail("Write the observation in the note first.");
      return { note: v("note") };
    case "session.pause": case "session.stop":
      return v("note") ? { note: v("note") } : {};
    case "interruption.open":
      if (!v("reason")) fail("Write the reason for the interruption first.");
      return { reason: v("reason") };
    case "incident.report": {
      if (!v("description")) fail("Describe what happened first.");
      const p = { event_type: v("event_type") || "other", description: v("description"), knowledge_source: v("knowledge_source") || "witnessed" };
      const at = zonedIso(v("occurred_at"), zone());
      if (at) p.occurred_at = at;
      return p;
    }
    default:
      return fail("This step cannot be saved on the phone.");
  }
}

function recordError(err) {
  const is = (name) => Q[name] && err instanceof Q[name];
  if (is("NeedsConnectionOnce")) return NEEDS_ONCE + " Protective reports still work.";
  if (is("UpdateRequired")) { S.updateRequired = true; render(); return "Update required: saved steps are kept on this phone."; }
  if (is("NoIdentity") || is("IdentityMismatch")) return "Sign in again to record on this phone. Nothing was saved.";
  return "Not saved: " + (err && err.message ? err.message : "the phone could not save this step.");
}

async function record(command, payload) {
  if (CLASS_OF[command] === "B") return Q.recordProtective(RUN, command, payload);
  return Q.recordOrdinary(RUN, command, payload);
}

// A note about a button sits right under it (not after the form's long help text).
function placeAfterSubmit(form, note) {
  const submit = form.querySelector("button:not([type='button'])");
  if (submit) submit.after(note);
  else form.append(note);
}

function formNote(form, text, cls = "f-saved") {
  let note = form.querySelector("[data-field-note]");
  if (note) note.remove();
  note = h("p", { class: cls, role: "status", "data-field-note": true, text });
  placeAfterSubmit(form, note);
  return note;
}

async function onFieldSubmit(e) {
  const form = e.target;
  if (MODE !== "page" || !(form instanceof HTMLFormElement) || !form.closest("main")) return;
  const cls = formClass(form);
  if (!cls) return;
  if (cls === "A" || cls === "online") {
    if (!online()) { e.preventDefault(); applyConnectivity(); }
    return; // online: the normal server form post, exactly as without JavaScript
  }
  e.preventDefault();
  if (form.dataset.fieldBusy === "1") return; // one tap, one step
  const command = formCommand(form);
  let payload;
  try {
    payload = payloadFrom(command, form);
  } catch (err) {
    formNote(form, err.fieldMessage || "Check the form.", "f-err");
    return;
  }
  form.dataset.fieldBusy = "1";
  try {
    const row = await record(command, payload);
    form.dataset.fieldKey = row && row.key ? row.key : "";
    form.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    formNote(form, cls === "B"
      ? "Saved on this phone. The robot must not operate. The report is sent first."
      : "Saved on this phone, not yet confirmed. It is sent when there is a connection.");
    await refresh();
    if (cls === "B") scrollTo(document.querySelector("[data-field-protect]"));
    syncNow();
  } catch (err) {
    form.dataset.fieldBusy = "";
    formNote(form, recordError(err), "f-err");
  }
}

// Class A and other online-only forms are disabled offline (15.4); ordinary records need the queue once.
function applyConnectivity() {
  if (MODE !== "page") return;
  const off = !online();
  document.querySelectorAll("main form").forEach((form) => {
    const cls = formClass(form);
    if (!cls) return;
    let need = null;
    if (off && cls === "A") need = NEEDS_A;
    else if (off && cls === "online") need = NEEDS_ONLINE;
    else if (off && cls === "C" && !S.queueReady) need = NEEDS_ONCE;
    const submit = form.querySelector("button:not([type='button'])");
    let note = form.querySelector(".f-needs");
    if (need) {
      if (submit && !submit.disabled) { submit.disabled = true; submit.dataset.fieldOff = "1"; }
      if (submit) submit.setAttribute("aria-describedby", (form.id || (form.id = "ff-" + Math.random().toString(36).slice(2))) + "-needs");
      if (!note) { note = h("p", { class: "f-needs", id: form.id + "-needs" }); placeAfterSubmit(form, note); }
      note.replaceChildren(h("span", { "aria-hidden": "true", text: "■" }), h("span", { text: need }));
    } else {
      if (submit && submit.dataset.fieldOff === "1") { submit.disabled = false; delete submit.dataset.fieldOff; submit.removeAttribute("aria-describedby"); }
      if (note) note.remove();
    }
  });
}

function enhancePage() {
  document.addEventListener("submit", onFieldSubmit);
  document.querySelectorAll("main form").forEach((form) => {
    if (form.dataset.fieldForm) form.addEventListener("change", applyConnectivity);
    form.addEventListener("input", () => { S.dirty = true; });
  });
  const flash = readSession(FLASH);
  if (flash && flash.length) { S.flash = flash; writeSession(FLASH, null); }
}

// ------------------------------------------------------------------ rendering
function mount(name, where) {
  let el = document.querySelector(`[data-field-${name}]`);
  if (!el) {
    el = h("div", { [`data-field-${name}`]: true });
    where(el);
  }
  return el;
}
function swap(el, node, sig) {
  // Re-render only when the content changed, and keep the keyboard focus where it was.
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  const focus = document.activeElement && el.contains(document.activeElement) ? document.activeElement.dataset.fid : null;
  el.replaceChildren(...(node ? [node] : []));
  if (focus) { const again = el.querySelector(`[data-fid="${focus}"]`); if (again) again.focus(); }
}

function unsentRows() { return S.rows.filter(isUnsent); }

function pill() {
  const pending = S.rows.filter((r) => r.state === "local" || r.state === "sending").length;
  const blocked = S.rows.filter((r) => r.state === "blocked_access").length;
  let cls = "p-online"; let shape = "●"; let text = "Online";
  if (!online()) { cls = "p-offline"; shape = "○"; text = "Offline"; }
  else if (S.syncing && pending) { cls = "p-syncing"; shape = "↻"; text = `Syncing ${pending}`; }
  else if (!pending && !blocked && S.lastOkAt && !S.auth) { cls = "p-uptodate"; shape = "✓"; text = `Up to date at ${hhmm(S.lastOkAt)}`; }
  return h("span", { class: `f-pill ${cls}`, "data-field-pill": cls.slice(2) },
    h("span", { class: "shape", "aria-hidden": "true", text: shape }), h("span", { text }));
}

function barNode(locked = false) {
  const pending = unsentRows().length;
  const bits = [pill()];
  if (locked) {
    // Nothing about this account's steps is shown until the person signs in again.
  } else if (MODE === "shell" && S.snapshot) {
    bits.push(h("span", { class: "f-when" }, h("b", {}, "Last known at ", timeEl(S.snapshot.fetched_at)),
      ". Saved on this phone; this is not current authorization."));
  } else if (!online() && pending) {
    bits.push(h("span", { class: "f-when", text: `${pending} ${plural(pending, "step is", "steps are")} saved on this phone and will be sent when the connection is back.` }));
  } else if (!online()) {
    bits.push(h("span", { class: "f-when", text: "Steps you record are saved on this phone and sent when the connection is back." }));
  }
  return h("div", { class: "f-bar", role: "status", "aria-live": "polite" }, ...bits);
}

function leadsNode() {
  const leads = (S.snapshot && S.snapshot.ops_leads) || [];
  return h("div", { class: "f-leads", id: "f-leads", hidden: true },
    leads.length
      ? [h("b", { text: "Ops leads for this hub" }), h("ul", {}, leads.map((l) => h("li", { text: l.name })))]
      : h("span", { text: "No ops lead is listed on this phone for this hub. Call your hub." }));
}

function latestProtective(state) {
  return S.rows.filter((r) => r.channel === "protective" && (!state || r.state === state))
    .sort((a, b) => Number(b.created_at) - Number(a.created_at))[0] || null;
}

function bannerNode() {
  const b = S.banner;
  if (!b) return null;
  const kids = [h("h2", {}, h("span", { class: "shape", "aria-hidden": "true", text: "■" }), "Robot must not operate")];
  const row = b.state === "not_accepted" ? latestProtective("rejected") : latestProtective();
  const what = row ? stepLabel(row) : "Protective report";
  const server = b.detail || "";
  if (b.state === "saved" && b.reason === "NOT_PERMITTED") {
    kids.push(h("p", { class: "f-state" }, `${what}: report saved on this phone. Waiting for access: ask an ops lead.`));
    if (server) kids.push(h("p", { class: "f-server", text: server }));
    kids.push(h("p", { text: "Keep the robot stopped. Once your access is back, use Try again below." }));
  } else if (b.state === "saved") {
    kids.push(h("p", { class: "f-state" }, `${what}: report saved on this phone, not yet confirmed.`),
      h("p", { text: "It is sent first, as soon as there is a connection. Keep the robot stopped." }));
  } else if (b.state === "sending") {
    kids.push(h("p", { class: "f-state" }, `${what}: sending the report to the server.`),
      h("p", { text: "Keep the robot stopped." }));
  } else if (b.state === "accepted") {
    kids.push(h("p", { class: "f-state" }, `${what}: report confirmed by the server.`));
    if (server) kids.push(h("p", { class: "f-server", text: server }));
    kids.push(h("p", { text: "This only means the report is recorded. The robot stays stopped until the ops lead resolves it." }));
  } else {
    kids.push(h("p", { class: "f-state", text: "Not accepted by the server: the robot must still not operate. Contact your ops lead." }));
    if (server) kids.push(h("p", { class: "f-server", text: server }));
    const leads = leadsNode();
    const contact = h("button", { type: "button", class: "primary", "aria-expanded": "false", "aria-controls": "f-leads", "data-fid": "contact", text: "Contact your ops lead" });
    contact.addEventListener("click", () => {
      const open = contact.getAttribute("aria-expanded") !== "true";
      contact.setAttribute("aria-expanded", open ? "true" : "false");
      leads.hidden = !open;
    });
    const actions = [contact];
    // EM ruling: only online, with a fresh snapshot showing no open protective state, for a rejected report.
    // queue.js enforces the same rule (snapshot saved after the rejection, at most 5 minutes old).
    const fresh = online() && S.snapshot && Date.now() - S.snapshotAt < FRESH_MS && S.snapshot.protective && S.snapshot.protective.open === false;
    const after = row && S.snapshotAt > Number(row.decided_at || 0);
    if (fresh && after && row.final !== false && typeof Q.resolveProtective === "function") {
      actions.push(h("button", { type: "button", class: "f-quiet", "data-fid": "resolve", text: "Confirm with ops lead: robot is safe", onclick: () => resolveProtective(row) }));
    }
    kids.push(h("div", { class: "f-actions" }, actions), leads);
  }
  return h("section", { class: "f-protect", role: "alert", "aria-label": "Robot must not operate", "data-field-banner": b.state }, kids);
}

async function resolveProtective(row) {
  const answer = await dialog({
    title: "Confirm with your ops lead: robot is safe",
    body: [
      `Report: ${stepLabel(row)}, saved on this phone at ${hhmm(row.created_at)}, not accepted by the server.`,
      "Do this only after your ops lead has confirmed the robot is safe. The report stays on this phone for the record.",
    ],
    field: { label: "Note (who confirmed, and how)", required: true, multiline: true, max: 300, missing: "Write who confirmed it first." },
    confirm: "Robot is safe: remove the banner",
    cancel: "Keep the banner",
  });
  if (!answer.ok) return;
  try {
    await Q.resolveProtective(row.key, answer.value);
  } catch (err) {
    await dialog({ title: "Banner kept", body: [err && err.message ? err.message : "The phone could not confirm this. The banner stays."], confirm: "OK", cancel: null });
  }
  await refresh();
}

function noticeNodes() {
  const out = [];
  const n = unsentRows().length;
  const note = (tone, shape, text, action, attr) => h("div", { class: `notice f-note ${tone}`, "data-field-notice": attr },
    h("p", {}, h("span", { class: "shape", "aria-hidden": "true", text: shape }), text), action || null);
  if (S.auth === "signin") {
    out.push(note("warn", "▲", `Sign in again to keep sending. ${n ? `${n} ${plural(n, "step stays", "steps stay")} saved on this phone.` : "Nothing is lost."}`,
      h("a", { class: "btn primary", href: SIGNIN_URL, text: "Sign in again" }), "signin"));
  }
  if (S.auth === "not_your_run") out.push(note("bad", "■", "This run is not yours; ask your ops lead. Nothing more is sent for it from this phone.", null, "not-your-run"));
  if (S.updateRequired) {
    out.push(note("warn", "▲", "Update required: saved steps are kept on this phone.",
      h("button", { type: "button", class: "primary", text: "Update now", onclick: applyUpdateNow }), "update-required"));
  }
  if (S.update === "waiting") out.push(note("neutral", "◆", "Update ready. It starts after the current send finishes; saved steps are kept on this phone.", null, "update-ready"));
  if (S.update === "activated") {
    out.push(note("neutral", "◆", "Update ready: reload when it suits you. Saved steps are kept on this phone.",
      h("button", { type: "button", text: "Reload", onclick: () => location.reload() }), "update-ready"));
  }
  if (S.flash && S.flash.length && !S.offerRefresh) {
    const items = S.flash.map((f) => (typeof f === "string" ? { label: stepLabel(S.rows.find((r) => r.key === f) || {}) } : f));
    out.push(h("p", { class: "f-saved ok", role: "status", "data-field-notice": "confirmed" },
      h("span", { "aria-hidden": "true", text: "✓ " }), `Confirmed by the server: ${items.map((i) => i.label).join(", ")}.`));
  }
  if (S.offerRefresh) {
    out.push(MODE === "shell"
      ? note("neutral", "●", "The connection is back.", h("button", { type: "button", text: "Open the full page", onclick: () => location.reload() }), "refresh")
      : note("neutral", "✓", "Steps were confirmed. The records below are from before.",
        h("button", { type: "button", text: "Refresh records", onclick: () => location.reload() }), "refresh"));
  }
  return out;
}

function statusOf(row) {
  const code = row.reason_code || "";
  if (row.state === "confirmed") {
    return { tone: "t-ok", shape: "✓", text: "Confirmed", when: row.recorded_at || row.decided_at };
  }
  if (row.state === "sending") return { tone: "t-info", shape: "↻", text: "Sending" };
  if (row.state === "blocked_access") {
    return { tone: "t-warn", shape: "▲", text: "Waiting for access: ask an ops lead", why: row.message, action: "retry" };
  }
  if (row.state === "rejected") {
    if (code === "SEQ_TAKEN") {
      const m = String(row.message || "").match(/Already recorded by (.+?)(?: at |\.|$)/);
      const at = String(row.message || "").match(/ at ([^.]+)/);
      return { tone: "", shape: "◆", text: `Already recorded by ${m ? m[1] : "another phone"}`, why: m ? (at ? `On another phone, at ${at[1]}.` : null) : row.message, action: "again", hint: "If the other phone recorded the same thing, there is nothing more to do." };
    }
    if (code === "AFTER_ABANDONED" || code === "STALE_GENERATION" || code === "ABANDONED") {
      return { tone: "", shape: "—", text: "Not sent: replaced after a skipped step", why: /replaced after a skipped step/i.test(row.message || "") ? null : row.message, action: "again" };
    }
    if (code === "ONLINE_ONLY") return { tone: "t-bad", shape: "■", text: NEEDS_A, why: row.message };
    return { tone: "t-bad", shape: "✕", text: "Not accepted", why: row.message || "The server did not accept this step.", bad: true };
  }
  const gap = gapSeq();
  if (row.channel === "ordinary" && gap && row.seq > gap) {
    return { tone: "t-warn", shape: "◐", text: "Saved on this phone", why: `Waiting for step ${gap} to reach the server first.` };
  }
  return { tone: "t-warn", shape: "◐", text: "Saved on this phone" };
}

// The server waits for an ordinary step this phone does not hold (another phone's, or a lost one): 15.5.
function gapSeq() {
  const led = S.snapshot && S.snapshot.ledger;
  if (!led || !online() || !S.lastOkAt || !S.snapshotAt) return null;
  const mine = S.rows.filter((r) => r.channel === "ordinary" && r.state === "local" && r.generation === led.generation);
  if (!mine.length) return null;
  const lowest = Math.min(...mine.map((r) => r.seq));
  return lowest > led.next_expected_seq ? led.next_expected_seq : null;
}

function rowNode(row) {
  const st = statusOf(row);
  const meta = [];
  if (row.channel === "protective") meta.push("Protective report");
  else if (row.seq) meta.push(`Step ${row.seq}`);
  if (row.created_at) meta.push(`saved ${hhmm(row.created_at)}`);
  if (st.when) meta.push(`confirmed ${hhmm(st.when)}`);
  const actions = [];
  if (st.action === "retry") {
    actions.push(h("button", { type: "button", "data-fid": "retry-" + row.key, text: "Try again", onclick: async () => { await Q.retryBlocked(RUN); await refresh(); syncNow(); } }));
  }
  if (st.action === "again" && row.replaced_by) {
    actions.push(h("p", { class: "f-why muted", text: "Recorded again as a new step." }));
  } else if (st.action === "again") {
    actions.push(h("button", { type: "button", "data-fid": "again-" + row.key, text: "Record again as a new step", onclick: async () => { await Q.recordAgain(row.key); await refresh(); syncNow(); } }));
  }
  return h("li", { class: "s-" + row.state, "data-field-row": row.key, "data-state": row.state, "data-reason": row.reason_code || null },
    h("div", { class: "f-what" }, h("b", { text: stepLabel(row) }), h("span", { class: "f-meta", text: meta.join(" · ") })),
    h("span", { class: `status ${st.tone}` }, h("span", { class: "shape", "aria-hidden": "true", text: st.shape }), st.text),
    st.why ? h("p", { class: "f-why" + (st.bad ? " bad" : ""), text: st.why }) : null,
    st.hint ? h("p", { class: "f-why muted", text: st.hint }) : null,
    actions.length ? h("div", { class: "f-actions" }, actions) : null);
}

function gapNode() {
  const gap = gapSeq();
  if (!gap) return null;
  return h("div", { class: "notice warn f-note f-gap", "data-field-gap": gap },
    h("p", {}, h("span", { class: "shape", "aria-hidden": "true", text: "▲" }),
      `The server is waiting for step ${gap} before it can take the steps after it. Step ${gap} was given out on another phone of this crew, or was lost. If no phone will send it, skip it.`),
    h("button", { type: "button", "data-fid": "skip", text: `Skip step ${gap}…`, onclick: () => skipStep(gap) }));
}

async function skipStep(seq) {
  const mine = S.rows.find((r) => r.channel === "ordinary" && r.seq === seq);
  const name = mine ? `“${stepLabel(mine)}” (step ${seq})` : `step ${seq}`;
  const answer = await dialog({
    title: `Skip ${name}?`,
    body: [
      `The server records ${name} as skipped. Steps recorded after it, on any phone of this crew, are not sent; you record again only what is still needed.`,
      "This cannot be undone.",
    ],
    field: { label: "Why is it skipped?", required: true, max: 500, missing: "Write why the step is skipped first." },
    confirm: `Skip step ${seq}`,
    cancel: "Keep waiting",
    danger: true,
  });
  if (!answer.ok) return;
  let out;
  try {
    out = await Q.abandon(RUN, seq, answer.value, {});
    if (out && out.status && out.status !== "ok") {
      const why = { offline: "There is no connection. Try again when you are online.", signin: "Sign in again first.", not_your_run: "This run is not yours; ask your ops lead.", refused: "The server did not skip it. Ask your ops lead." };
      throw new Error(out.message || why[out.status] || "The step was not skipped.");
    }
  } catch (err) {
    await dialog({ title: "Not skipped", body: [err && err.message ? err.message : "The step could not be skipped. Try again with a connection."], confirm: "OK", cancel: null });
    return;
  }
  await refreshSnapshot();
  if (out && out.newGeneration) {
    S.reconcile = { seq, keys: (out.invalid || []).map((r) => r.key), at: new Date().toISOString() };
    writeSession(RECONCILE, S.reconcile);
  }
  await refresh();
  scrollTo(document.querySelector("[data-field-reconcile]"));
}

function reconcileNode() {
  const rc = S.reconcile;
  if (!rc) return null;
  const invalid = S.rows.filter((r) => rc.keys.includes(r.key) || (r.reason_code === "AFTER_ABANDONED" && r.state === "rejected"));
  const steps = (S.snapshot && S.snapshot.steps) || [];
  const close = h("button", { type: "button", class: "primary", "data-fid": "rc-done", text: "Done reconciling", onclick: () => { S.reconcile = null; writeSession(RECONCILE, null); render(); } });
  return h("section", { class: "card", "data-field-reconcile": rc.seq, "aria-labelledby": "f-rc-h" },
    h("h2", { id: "f-rc-h", text: `Reconcile: step ${rc.seq} was skipped` }),
    h("p", { text: "The steps on the left were recorded on this phone after the skipped step, so they were not sent. Compare them with the server's current state, then record again only what is still needed." }),
    h("div", { class: "f-cols" },
      h("div", {}, h("h3", { text: "Not sent from this phone" }),
        invalid.length ? h("ul", { class: "f-list" }, invalid.map(rowNode)) : h("p", { class: "muted", text: "Nothing on this phone was recorded after the skipped step." })),
      h("div", {}, h("h3", {}, "Server's current state · last known at ", timeEl(S.snapshot && S.snapshot.fetched_at)),
        steps.length ? h("ul", { class: "f-list" }, steps.map((s) => h("li", { class: s.done ? "done" : "" },
          h("div", { class: "f-what" }, h("b", { text: s.label || label(s.command) }), s.done && s.recorded_at ? h("span", { class: "f-meta" }, "Recorded ", timeEl(s.recorded_at)) : null),
          h("span", { class: "status " + (s.done ? "t-ok" : "") }, h("span", { class: "shape", "aria-hidden": "true", text: s.done ? "✓" : "○" }), s.done ? "Recorded" : "Not recorded yet"))))
          : h("p", { class: "muted", text: "The server's steps are not on this phone yet. Open this run with a connection." }))),
    h("div", { class: "f-actions" }, close));
}

function rowsCard() {
  const open = S.rows.filter((r) => r.state !== "confirmed");
  const done = S.rows.filter((r) => r.state === "confirmed");
  if (MODE === "page" && !S.rows.length) return null;
  return h("section", { class: "card", "data-field-steps": true, "aria-labelledby": "f-steps-h" },
    h("h2", { id: "f-steps-h", text: "Saved on this phone" }),
    gapNode(),
    open.length ? h("ul", { class: "f-list" }, open.map(rowNode))
      : h("p", { class: "muted", text: S.rows.length ? "Every step saved on this phone is confirmed by the server." : "Nothing saved on this phone for this run yet. Steps you record show here until the server confirms them." }),
    done.length ? h("details", { class: "fold" }, h("summary", { text: `Confirmed (${done.length})` }), h("ul", { class: "f-list" }, done.map(rowNode))) : null);
}

function render() {
  if (!MODE) return;
  const main = document.querySelector("main");
  const banner = mount("protect", (el) => main.prepend(el));
  const top = app;
  const vm = JSON.stringify([S.banner, S.banner && S.snapshot && S.snapshot.protective, online(), S.snapshotAt > 0 && Date.now() - S.snapshotAt < FRESH_MS, (S.snapshot && S.snapshot.ops_leads) || null, S.rows.filter((r) => r.channel === "protective").map((r) => [r.key, r.state])]);
  swap(banner, bannerNode(), vm);
  if (MODE === "shell") renderShell(top);
  else {
    const sig = JSON.stringify([online(), S.syncing, S.lastOkAt && hhmm(S.lastOkAt), S.auth, S.updateRequired, S.update, S.flash, S.offerRefresh, S.reconcile, S.rows.map((r) => [r.key, r.state, r.reason_code, r.message]), gapSeq(), S.snapshotAt]);
    swap(top, h("div", { class: "f-app" }, barNode(), ...noticeNodes(), reconcileNode(), rowsCard()), sig);
  }
  applyConnectivity();
}

// ------------------------------------------------------------------ the offline shell (15.10 items 1 and 5)
function renderShell(top) {
  const fallback = document.querySelector("[data-field-shell-fallback]");
  if (fallback) fallback.remove();
  const locked = S.identity === null || S.identity === undefined;
  const snap = S.snapshot;
  const sig = JSON.stringify([locked, !!snap, snap && snap.fetched_at, online(), S.syncing, S.lastOkAt && hhmm(S.lastOkAt), S.auth, S.updateRequired, S.update, S.reconcile, S.rows.map((r) => [r.key, r.state, r.reason_code, r.message]), gapSeq()]);
  if (locked) {
    swap(top, h("div", { class: "f-app" },
      h("div", { class: "f-shell-head" }, h("h1", { text: RUN || "Field run" })), barNode(true),
      h("section", { class: "card", "data-field-locked": true },
        h("h2", { text: "Sign in again to see this run" }),
        h("p", { text: "This phone shows saved runs only to the person who saved them. Sign in again with a connection to see this run and send its saved steps." }),
        h("div", { class: "f-actions" }, h("a", { class: "btn primary", href: SIGNIN_URL, text: "Sign in again" })))), sig);
    return;
  }
  if (!snap) {
    swap(top, h("div", { class: "f-app" },
      h("div", { class: "f-shell-head" }, h("h1", { text: RUN || "Field run" })), barNode(), ...noticeNodes(),
      h("section", { class: "card", "data-field-nosnapshot": true },
        h("h2", { text: "This run is not saved on this phone" }),
        h("p", { text: `Open ${RUN || "the run"} once with a connection to keep a copy here for offline use. A protective report can still be saved.` }),
        protectiveActions()),
      rowsCard()), sig);
    return;
  }
  const r = snap.run || {};
  swap(top, h("div", { class: "f-app" },
    h("div", { class: "f-shell-head" }, h("a", { class: "back muted", href: "/execution/", text: "Field runs" }), h("h1", { text: r.code || RUN }),
      h("div", { class: "sub", text: [r.home_label, r.robot ? `Robot ${r.robot}` : null].filter(Boolean).join(" · ") })),
    barNode(), ...noticeNodes(), reconcileNode(),
    checklistCard(snap),
    h("section", { class: "card", "aria-labelledby": "f-prot-h" }, h("h2", { id: "f-prot-h", text: "Something is wrong?" }),
      h("p", { text: "These are saved on this phone at once and sent first. They work without a connection." }), protectiveActions()),
    rowsCard()), sig);
}

function checklistCard(snap) {
  const steps = (snap.steps || []).filter((s) => s.class !== "B");
  let primaryGiven = false; // one primary action: the first step still to record
  const queued = new Map();
  for (const row of S.rows) if (row.channel === "ordinary" && row.state !== "rejected") queued.set(row.command, row);
  const items = steps.map((s) => {
    const row = queued.get(s.command);
    let status;
    if (s.done) status = h("span", { class: "status t-ok" }, h("span", { class: "shape", "aria-hidden": "true", text: "✓" }), "Confirmed ", s.recorded_at ? timeEl(s.recorded_at) : "");
    else if (row && row.state === "confirmed") status = h("span", { class: "status t-ok" }, h("span", { class: "shape", "aria-hidden": "true", text: "✓" }), "Confirmed");
    else if (row) status = h("span", { class: "status t-warn" }, h("span", { class: "shape", "aria-hidden": "true", text: "◐" }), row.state === "sending" ? "Sending" : "Saved on this phone");
    else status = h("span", { class: "status" }, h("span", { class: "shape", "aria-hidden": "true", text: "○" }), "Not recorded");
    let action = null;
    if (!s.done && !row) {
      if (s.class === "A") {
        action = h("div", { class: "f-actions" },
          h("button", { type: "button", disabled: true, "aria-describedby": "need-" + s.command, text: s.label || label(s.command) }),
          h("p", { class: "f-needs", id: "need-" + s.command }, h("span", { "aria-hidden": "true", text: "■" }), h("span", { text: NEEDS_A })));
      } else {
        const primary = !primaryGiven;
        primaryGiven = true;
        action = h("div", { class: "f-actions" }, h("button", { type: "button", class: primary ? "primary" : null, "data-fid": "rec-" + s.command, text: s.label || `Record: ${label(s.command)}`, onclick: () => shellRecord(s.command) }));
      }
    }
    return h("li", { class: s.done ? "done" : "", "data-field-step": s.command },
      h("div", { class: "f-what" }, h("b", { text: s.label || label(s.command) }), s.class === "A" ? h("span", { class: "f-meta", text: "Online only" }) : null), status, action);
  });
  return h("section", { class: "card", "aria-labelledby": "f-check-h" },
    h("h2", { id: "f-check-h" }, "Steps · last known at ", timeEl(snap.fetched_at)),
    items.length ? h("ul", { class: "f-list" }, items) : h("p", { class: "muted", text: "No steps were saved with this run. Open it with a connection to refresh them." }));
}

function protectiveActions() {
  const b = (command, text) => h("button", { type: "button", class: command === "session.stop" ? "f-danger" : "", "data-fid": "prot-" + command, text, onclick: () => shellRecord(command) });
  return h("div", { class: "f-actions" }, b("session.stop", "Stop the session"), b("session.pause", "Pause the session"), b("incident.report", "Report an incident"), b("interruption.open", "Open an interruption"));
}

function select(name, options, labelText, id) {
  return h("div", { class: "field" }, h("label", { for: id, text: labelText }),
    h("select", { id, name }, options.map(([v, t]) => h("option", { value: v, text: t }))));
}

// One dialog per step in the shell; the same names and payloads as the server forms (payloadFrom).
async function shellRecord(command) {
  const page = (S.snapshot && S.snapshot.page) || {};
  const crew = (page.crew || []).map((c) => [c.code, c.label]);
  const fields = [];
  const id = (n) => "sf-" + n;
  const needPage = () => dialog({ title: label(command), body: ["This form needs a connection once: open the run with a connection to load it on this phone."], confirm: "OK", cancel: null });
  switch (command) {
    case "custody.back_to_vehicle":
      if (!crew.length) return needPage();
      fields.push(select("custodian", crew, "Hands over", id("cust")), select("received_by", crew.slice().reverse(), "Receives the robot", id("recv")),
        h("div", { class: "field" }, h("label", { for: id("note"), text: "Note (optional)" }), h("input", { id: id("note"), name: "note" })));
      break;
    case "custody.verify":
      if (!crew.length || !(page.unverified || []).length) return needPage();
      fields.push(h("input", { type: "hidden", name: "custody_seq", value: page.unverified[0] }), select("verifier", crew.slice(1).concat(crew.slice(0, 1)), "Verified by (crew)", id("ver")));
      break;
    case "site_check.record": {
      const t = (page.templates || [])[0];
      if (!t || !crew.length) return needPage();
      fields.push(h("fieldset", {}, h("legend", {}, h("label", {}, h("input", { type: "radio", name: "template", value: t.version, checked: true, class: "sr" }), ` Checklist ${t.version}${t.demo ? " (demo checklist, not approved for production)" : ""}`)),
        ...t.items.map((i) => select("cond_" + i.id, [["", "not checked"], ["ok", "ok"], ["not_ok", "not ok"]], i.label, id("c-" + i.id)))),
        h("div", { class: "field" }, h("label", { for: id("adults"), text: "Adults present" }), h("input", { id: id("adults"), name: "adults_present", type: "number", min: 0, inputmode: "numeric", required: true })),
        select("verifier", crew, "Verifier (crew)", id("ver")));
      break;
    }
    case "session.observation":
      fields.push(h("div", { class: "field" }, h("label", { for: id("note"), text: "Observation" }), h("textarea", { id: id("note"), name: "note", rows: 3, required: true })));
      break;
    case "session.pause": case "session.stop":
      fields.push(h("div", { class: "field" }, h("label", { for: id("note"), text: "Note (optional)" }), h("input", { id: id("note"), name: "note" })));
      break;
    case "interruption.open":
      fields.push(h("div", { class: "field" }, h("label", { for: id("reason"), text: "Reason" }), h("input", { id: id("reason"), name: "reason", required: true })));
      break;
    case "incident.report": {
      const types = page.event_types && page.event_types.length ? page.event_types : [["other", "Other"]];
      const sources = page.knowledge_sources && page.knowledge_sources.length ? page.knowledge_sources : [["witnessed", "Witnessed"]];
      fields.push(select("event_type", types, "Type", id("type")), select("knowledge_source", sources, "How you know", id("src")),
        h("div", { class: "field" }, h("label", { for: id("desc"), text: "What happened" }), h("textarea", { id: id("desc"), name: "description", rows: 3, required: true })));
      break;
    }
    default:
      break;
  }
  const cls = CLASS_OF[command];
  const box = h("div", { class: "f-form-fields" }, fields);
  const body = cls === "B"
    ? [`${label(command)}: saved on this phone at once and sent first. The robot must not operate.`]
    : [`${label(command)}: saved on this phone and confirmed when the server answers.`];
  for (;;) {
    const answer = await dialog({ title: label(command), body, extra: fields.length ? box : null, confirm: "Save on this phone", cancel: "Cancel", danger: command === "session.stop" });
    if (!answer.ok) return;
    let payload;
    try {
      payload = payloadFrom(command, answer.form);
    } catch (err) {
      body.splice(1, body.length - 1, h("p", { class: "f-err", text: err.fieldMessage || "Check the form." }));
      continue;
    }
    try {
      await record(command, payload);
    } catch (err) {
      await dialog({ title: "Not saved", body: [recordError(err)], confirm: "OK", cancel: null });
      return;
    }
    await refresh();
    if (cls === "B") scrollTo(document.querySelector("[data-field-protect]"));
    syncNow();
    return;
  }
}

// ------------------------------------------------------------------ service worker (15.10 item 4)
async function registerWorker() {
  if (!("serviceWorker" in navigator)) return;
  let reg;
  try {
    reg = await navigator.serviceWorker.register("/execution/sw.js", { scope: "/execution/" });
  } catch (_) {
    return; // the page works without it, online
  }
  const hadController = !!navigator.serviceWorker.controller;
  const waiting = () => { if (reg.waiting && navigator.serviceWorker.controller) onWaiting(reg); };
  waiting();
  reg.addEventListener("updatefound", () => {
    const w = reg.installing;
    if (w) w.addEventListener("statechange", () => { if (w.state === "installed") waiting(); });
  });
  let controlled = hadController;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!controlled) { controlled = true; return; } // the first install taking this page: nothing to announce
    S.update = "activated";
    S.holdSync = false;
    render();
  });
}
async function onWaiting(reg) {
  if (S.update) return;
  S.update = "waiting";
  render();
  await (S.syncPromise || Promise.resolve()); // finish the current batch; never start a new one meanwhile
  S.holdSync = true;
  if (reg.waiting) reg.waiting.postMessage({ type: "SKIP_WAITING" });
  setTimeout(() => { S.holdSync = false; }, 10000); // never leave sending stopped if activation stalls
}
async function applyUpdateNow() {
  try {
    const reg = await navigator.serviceWorker.getRegistration("/execution/");
    if (reg) await reg.update();
  } catch (_) { /* reload anyway */ }
  location.reload();
}

// ------------------------------------------------------------------ start
async function start() {
  try { await Q.open(); } catch (_) { return; } // no IndexedDB: the server page still works online
  if (boot && MODE !== "shell") {
    // The server page says who is signed in; the offline shell never changes the identity.
    try { await Q.setIdentity(USER); } catch (_) { /* keep the server page working */ }
  }
  hookSignOut();
  if (!MODE || !RUN) return;
  try { S.identity = await Q.identity(); } catch (_) { S.identity = null; }
  rememberRun(S.identity, RUN);
  if (MODE === "page") enhancePage();
  registerWorker();
  await loadSnapshot();
  if (MODE === "shell" && S.snapshot && S.identity !== null) {
    try { await Q.ensureQueue(RUN, S.snapshot.ledger); S.queueReady = true; } catch (_) { /* recording says so */ }
  }
  await refresh();
  if (typeof Q.subscribe === "function") Q.subscribe(() => { refresh(); });
  window.addEventListener("online", async () => {
    S.reachable = true;
    render();
    await refreshSnapshot();
    await syncNow();
    // The shell stands in for the run page only while offline: with the connection back, show the real page
    // (unless a form is open; then the notice offers it).
    if (MODE === "shell" && S.identity !== null) {
      if (!document.querySelector("dialog[open]")) location.reload();
      else { S.offerRefresh = true; render(); }
    }
  });
  window.addEventListener("offline", () => render());
  setInterval(() => { if (navigator.onLine) syncNow(); }, SYNC_EVERY_MS);
  setInterval(() => render(), 60000); // keeps "fresh" and times honest
  if (navigator.onLine) {
    await refreshSnapshot();
    await refresh();
    syncNow();
  }
}

start();
