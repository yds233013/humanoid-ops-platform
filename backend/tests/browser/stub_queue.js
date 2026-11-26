// TEST-ONLY stand-in for /field/queue.js (03D-1 owns the real one). Served by tests/browser/shim_urls.py ONLY
// when backend/execution/field_static/queue.js is missing or empty, so the run page can be exercised before
// the three 03D branches are merged. It follows the frozen API in 03d_modules.md, but keeps its rows in
// localStorage (not IndexedDB), has no Web Lock and no schema migration. Never shipped.

const KEY = "ops-field-stub";
const listeners = new Set();
export const QUEUE_SCHEMA = 1;
export class NeedsConnectionOnce extends Error {
  constructor() { super("Needs a connection once to start recording."); this.name = "NeedsConnectionOnce"; }
}

function load() {
  try { return JSON.parse(localStorage.getItem(KEY)) || null; } catch (_) { return null; }
}
function db() {
  return load() || { device: null, identity: null, queues: {}, commands: {}, snapshots: {} };
}
function save(d) {
  localStorage.setItem(KEY, JSON.stringify(d));
  listeners.forEach((fn) => { try { fn(); } catch (_) { /* ignore */ } });
}
function uuid() { return crypto.randomUUID(); }
function nowIso() {
  const d = new Date();
  const off = -d.getTimezoneOffset();
  const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  const local = new Date(d.getTime() + off * 60000).toISOString().slice(0, 19);
  return `${local}${off >= 0 ? "+" : "-"}${pad(off / 60)}:${pad(off % 60)}`;
}
function qid(d, run) { return `${d.identity}:${run}:${d.device}`; }
function csrf() {
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export async function open() {
  const d = db();
  if (!d.device) d.device = uuid();
  for (const r of Object.values(d.commands)) if (r.state === "sending") r.state = "local"; // restart rule 15.6
  save(d);
}
export async function deviceId() { const d = db(); if (!d.device) { d.device = uuid(); save(d); } return d.device; }
export async function setIdentity(userId) {
  const d = db();
  if (d.identity !== (userId ?? null)) { d.identity = userId ?? null; save(d); }
}
export async function identity() { return db().identity; }
export async function saveSnapshot(snap) {
  const d = db();
  if (!snap || !snap.user || snap.user.id !== d.identity) throw new Error("snapshot of another account");
  d.snapshots[`${d.identity}:${snap.run.code}`] = snap;
  save(d);
}
export async function snapshot(run) {
  const d = db();
  return d.identity === null ? null : d.snapshots[`${d.identity}:${run}`] || null;
}
export async function ensureQueue(run, ledger) {
  const d = db();
  if (d.identity === null || !ledger) return;
  const id = qid(d, run);
  if (!d.queues[id]) {
    d.queues[id] = {
      id, user_id: d.identity, run, device_id: d.device, generation: ledger.generation || 1,
      next_seq: ledger.next_expected_seq || 1, last_decided_seq: ledger.last_decided_seq || 0,
      last_decided_outcome: ledger.last_decided_outcome || null, queue_schema: QUEUE_SCHEMA,
    };
    save(d);
  }
}
function row(d, q, run, channel, command, payload) {
  return {
    key: uuid(), queue_id: qid(d, run), channel, seq: null, generation: q ? q.generation : 1, command, payload,
    device_observed_at: nowIso(), last_decided_seq: q ? q.last_decided_seq || null : null,
    last_decided_outcome: q && q.last_decided_seq ? q.last_decided_outcome : null, state: "local",
    reason_code: "", message: "", final: false, created_at: new Date().toISOString(), sent_at: null,
    decided_at: null, queue_schema: QUEUE_SCHEMA,
  };
}
export async function recordOrdinary(run, command, payload) {
  const d = db();
  const q = d.queues[qid(d, run)];
  if (!q) throw new NeedsConnectionOnce();
  const r = row(d, q, run, "ordinary", command, payload);
  r.seq = q.next_seq;
  q.next_seq += 1;
  d.commands[r.key] = r;
  save(d);
  return r;
}
export async function recordProtective(run, command, payload) {
  const d = db();
  const r = row(d, d.queues[qid(d, run)], run, "protective", command, payload);
  d.commands[r.key] = r;
  save(d);
  return r;
}
function rowsOf(d, run) {
  const id = qid(d, run);
  const rows = Object.values(d.commands).filter((r) => r.queue_id === id);
  const prot = rows.filter((r) => r.channel === "protective").sort((a, b) => a.created_at.localeCompare(b.created_at));
  const ord = rows.filter((r) => r.channel !== "protective").sort((a, b) => (a.generation - b.generation) || (a.seq - b.seq));
  return prot.concat(ord);
}
export async function list(run) { const d = db(); return d.identity === null ? [] : rowsOf(d, run); }
export async function protectiveBanner(run) {
  const d = db();
  if (d.identity === null) return null;
  const prot = rowsOf(d, run).filter((r) => r.channel === "protective" && !r.resolved);
  const snap = d.snapshots[`${d.identity}:${run}`];
  const rej = prot.filter((r) => r.state === "rejected").pop();
  if (rej) return { state: "not_accepted", reason: rej.reason_code, message: "", detail: rej.message };
  if (prot.some((r) => r.state === "local" || r.state === "blocked_access")) return { state: "saved", reason: "", message: "" };
  if (prot.some((r) => r.state === "sending")) return { state: "sending", reason: "", message: "" };
  const ok = prot.filter((r) => r.state === "confirmed").pop();
  if (ok && !(snap && snap.protective && snap.protective.open === false && snap.fetched_at > ok.decided_at)) {
    return { state: "accepted", reason: "", message: "", detail: ok.message };
  }
  return null;
}
export async function syncOnce(run, { fetchImpl } = {}) {
  const f = fetchImpl || fetch.bind(window);
  if (!navigator.onLine) return { status: "offline", decided: 0 };
  let d = db();
  const q = d.queues[qid(d, run)];
  const rows = rowsOf(d, run).filter((r) => r.state === "local" && (r.channel === "protective" || (q && r.generation === q.generation)));
  if (!rows.length) return { status: "ok", decided: 0 };
  const batch = rows.slice(0, 20);
  batch.forEach((r) => { d.commands[r.key].state = "sending"; d.commands[r.key].sent_at = new Date().toISOString(); });
  save(d);
  const back = (status) => {
    const x = db();
    batch.forEach((r) => { if (x.commands[r.key].state === "sending") x.commands[r.key].state = "local"; });
    save(x);
    return { status, decided: 0 };
  };
  let resp;
  try {
    resp = await f("/api/field/sync", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify({
        device_id: d.device, run, generation: q ? q.generation : 1,
        unsent_count: rowsOf(d, run).filter((r) => ["local", "sending", "blocked_access"].includes(r.state)).length,
        commands: batch.map((r) => ({
          key: r.key, channel: r.channel, seq: r.seq, last_decided_seq: r.last_decided_seq || null,
          last_decided_outcome: r.last_decided_outcome, command: r.command, payload: r.payload,
          device_observed_at: r.device_observed_at,
        })),
      }),
    });
  } catch (_) {
    return back("offline");
  }
  if (resp.status === 401) return back("signin");
  if (resp.status === 403) return back("not_your_run");
  if (resp.status !== 200) return back("ok");
  const body = await resp.json();
  d = db();
  let decided = 0;
  for (const res of body.results || []) {
    const r = d.commands[res.key];
    if (!r) continue;
    if (res.outcome === "confirmed") {
      Object.assign(r, { state: "confirmed", final: true, message: res.message || "", decided_at: res.recorded_at || body.server_time, result: res.result || {} });
      decided += 1;
    } else if (res.outcome === "rejected") {
      const blocked = res.reason_code === "NOT_PERMITTED";
      Object.assign(r, { state: blocked ? "blocked_access" : "rejected", final: !!res.final, reason_code: res.reason_code, message: res.message || "", decided_at: blocked ? null : body.server_time });
      decided += 1;
    } else {
      Object.assign(r, { state: "local", reason_code: res.outcome === "held" ? "HELD" : "RETRY", message: res.message || "" });
    }
  }
  batch.forEach((b) => { if (d.commands[b.key].state === "sending") d.commands[b.key].state = "local"; }); // no result: resend later
  const qq = d.queues[qid(d, run)];
  if (qq && typeof body.last_decided_seq === "number") qq.last_decided_seq = body.last_decided_seq;
  save(d);
  return { status: "ok", decided };
}
export async function retryBlocked(run) {
  const d = db();
  rowsOf(d, run).forEach((r) => { if (r.state === "blocked_access") d.commands[r.key].state = "local"; });
  save(d);
}
export async function abandon(run, seq, reason, { fetchImpl } = {}) {
  const f = fetchImpl || fetch.bind(window);
  let d = db();
  const q = d.queues[qid(d, run)];
  const resp = await f("/api/field/abandon", {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
    body: JSON.stringify({ run, generation: q.generation, seq, reason }),
  });
  const body = await resp.json();
  if (resp.status !== 200) throw new Error(body.message || "The step could not be skipped.");
  d = db();
  const qq = d.queues[qid(d, run)];
  const invalid = [];
  const newGeneration = body.generation > qq.generation ? body.generation : null;
  for (const r of rowsOf(d, run)) {
    if (r.channel === "ordinary" && r.generation === qq.generation && r.seq >= seq && r.state === "local") {
      Object.assign(d.commands[r.key], { state: "rejected", final: true, reason_code: "AFTER_ABANDONED", message: "Not sent: replaced after a skipped step." });
      invalid.push(d.commands[r.key]);
    }
  }
  if (newGeneration) { qq.generation = newGeneration; qq.next_seq = body.next_expected_seq || 1; qq.last_decided_seq = body.last_decided_seq || 0; }
  save(d);
  return { newGeneration, invalid };
}
export async function recordAgain(key) {
  const d = db();
  const old = d.commands[key];
  const run = old.queue_id.split(":")[1];
  return old.channel === "protective" ? recordProtective(run, old.command, old.payload) : recordOrdinary(run, old.command, old.payload);
}
export async function resolveProtective(key, note) {
  const d = db();
  if (!d.commands[key] || d.commands[key].state !== "rejected") throw new Error("Only a report the server did not accept can be confirmed here.");
  Object.assign(d.commands[key], { resolved: true, resolved_locally: true, resolved_note: note });
  save(d);
}
export function subscribe(fn) {
  listeners.add(fn);
  window.addEventListener("storage", (e) => { if (e.key === KEY) fn(); });
  return () => listeners.delete(fn);
}
