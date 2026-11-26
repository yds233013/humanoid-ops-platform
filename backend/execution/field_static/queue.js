// Offline field queue (v2 03D-1 + 03D-2 logic). Contract: docs/13_v2_phase03_contract.md sections 14 (with
// 14.8, 14.9) and 15 (15.10 wins). A plain ES module: no framework, no build step, no eval.
//
// IndexedDB database "ops-field", version 1:
//   queues    key `${user_id}:${run}:${device_id}` (plus lease rows `lease:${queue_id}` for the Web Locks fallback)
//   commands  key `key` (uuid made once, never reused), index `queue_id`
//   device    one row `device`: device_id, current_user_id (set at sign-in, cleared at sign-out)
//   snapshots key `${user_id}:${run}` (15.10 item 1: bound to the account)
//
// Command states (15.3): local | sending | confirmed | rejected | blocked_access.
// Every field the server hashes into the command identity (14.8 item 2) is frozen in `body` when the command is
// made; a retry resends exactly that body. A queued command is never renumbered.

export const QUEUE_SCHEMA = 1;

const DB_NAME = "ops-field";
const DB_VERSION = 1;
const BATCH_MAX = 20; // 14.1
const PAYLOAD_MAX_BYTES = 4096; // 14.1
const LEASE_MS = 30000; // 15.10 item 2
const FETCH_TIMEOUT_MS = 15000;
const SNAPSHOT_FRESH_MS = 2 * 60 * 1000; // resolveProtective needs a snapshot at most 2 minutes old (EM: same limit as run.js FRESH_MS)
const SYNC_URL = "/api/field/sync";
const ABANDON_URL = "/api/field/abandon";
const CHANNEL_NAME = "ops-field";
const UNSENT_STATES = new Set(["local", "sending", "blocked_access"]);
// Final rejections that "Record again as a new step" may replace (15.10 item 5, 15.5).
const RECORD_AGAIN_CODES = new Set(["SEQ_TAKEN", "AFTER_ABANDONED", "STALE_GENERATION", "ABANDONED"]);

const MSG = {
  saved: "Robot must not operate: report saved on this phone, not yet confirmed.",
  sending: "Robot must not operate: report saved on this phone, sending it now.",
  waiting: "Robot must not operate: report saved on this phone. Waiting for access: ask an ops lead.",
  accepted:
    "Robot must not operate: the server has the report. Wait for your ops lead to clear it before the robot works again.",
  notAccepted: "Not accepted by the server: the robot must still not operate. Contact your ops lead.",
  afterAbandoned: "Not sent: replaced after a skipped step.",
};

export class NeedsConnectionOnce extends Error {
  constructor() {
    super("Needs a connection once to start recording.");
    this.name = "NeedsConnectionOnce";
  }
}

export class UpdateRequired extends Error {
  constructor() {
    super("Update required: saved steps are kept on this phone.");
    this.name = "UpdateRequired";
  }
}

export class NoIdentity extends Error {
  constructor() {
    super("Sign in again to see this run.");
    this.name = "NoIdentity";
  }
}

export class IdentityMismatch extends Error {
  constructor() {
    super("This belongs to another account on this phone.");
    this.name = "IdentityMismatch";
  }
}

// ------------------------------------------------------------------ small helpers
const TAB_ID = uuid();
let dbPromise = null;
let updateRequired = false; // the database on this phone is newer than this client can read

function uuid() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") return globalThis.crypto.randomUUID();
  const b = globalThis.crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

// ISO 8601 with the phone's UTC offset, e.g. 2026-11-27T09:41:05-08:00 (14.1 device_observed_at).
function localIso(d) {
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const a = Math.abs(off);
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
    `${sign}${pad(Math.floor(a / 60))}:${pad(a % 60)}`
  );
}

function schemaOk(s) {
  return s === undefined || s === QUEUE_SCHEMA; // undefined: a row with no stamp is read as schema 1
}

function queueId(user, run, device) {
  return `${user}:${run}:${device}`;
}

function lockName(qid) {
  return `ops-field-sync:${qid}`;
}

function sameUser(a, b) {
  return a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b);
}

function frozen(value) {
  return JSON.parse(JSON.stringify(value));
}

function csrfToken() {
  const m = (globalThis.document ? document.cookie : "").match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

function req(r) {
  return new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

// One transaction; fn(t) may await requests of this transaction only. Resolves after the commit.
function inTx(db, stores, mode, fn) {
  return new Promise((resolve, reject) => {
    const t = db.transaction(stores, mode);
    let out;
    let failed = null;
    t.oncomplete = () => resolve(out);
    t.onabort = () => reject(failed || t.error || new Error("IndexedDB transaction aborted"));
    let p;
    try {
      p = Promise.resolve(fn(t));
    } catch (e) {
      p = Promise.reject(e);
    }
    p.then(
      (v) => {
        out = v;
      },
      (e) => {
        failed = e;
        try {
          t.abort();
        } catch (_) {
          /* already finished */
        }
      },
    );
  });
}

// ------------------------------------------------------------------ change notification (all tabs)
const listeners = new Set();
let channel = null;

function bus() {
  if (!channel && typeof BroadcastChannel !== "undefined") {
    channel = new BroadcastChannel(CHANNEL_NAME);
    channel.onmessage = () => fire();
  }
  return channel;
}

function fire() {
  for (const fn of Array.from(listeners)) {
    queueMicrotask(() => {
      try {
        fn();
      } catch (_) {
        /* a listener's error never breaks the queue */
      }
    });
  }
}

function notify() {
  fire();
  try {
    const b = bus();
    if (b) b.postMessage({ at: Date.now() });
  } catch (_) {
    /* closed channel */
  }
}

export function subscribe(fn) {
  listeners.add(fn);
  bus();
  return () => listeners.delete(fn);
}

// ------------------------------------------------------------------ open + restart
function openDb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB_NAME, DB_VERSION);
    r.onupgradeneeded = () => {
      const db = r.result;
      if (!db.objectStoreNames.contains("queues")) db.createObjectStore("queues", { keyPath: "id" });
      if (!db.objectStoreNames.contains("commands")) {
        db.createObjectStore("commands", { keyPath: "key" }).createIndex("queue_id", "queue_id");
      }
      if (!db.objectStoreNames.contains("device")) db.createObjectStore("device", { keyPath: "id" });
      if (!db.objectStoreNames.contains("snapshots")) db.createObjectStore("snapshots", { keyPath: "id" });
    };
    r.onsuccess = () => {
      const db = r.result;
      db.onversionchange = () => {
        // A newer client is upgrading: let it. This tab reopens at version 1 and then reports update_required.
        db.close();
        dbPromise = null;
      };
      resolve(db);
    };
    r.onerror = (ev) => {
      if (r.error && r.error.name === "VersionError") {
        // 15.10 item 4: a newer schema on this phone. Never delete it; open it as it is and stop syncing.
        if (ev && ev.preventDefault) ev.preventDefault();
        updateRequired = true;
        const r2 = indexedDB.open(DB_NAME);
        r2.onsuccess = () => resolve(r2.result);
        r2.onerror = () => reject(r2.error);
        return;
      }
      reject(r.error);
    };
  });
}

// 15.6: commands left in `sending` by a closed tab or a restart go back to `local` (same keys). Only for queues
// whose coordinator is free, so a tab that is still sending keeps its batch.
async function restoreSending(db) {
  if (updateRequired) return;
  const rows = await inTx(db, ["commands"], "readonly", (t) => req(t.objectStore("commands").getAll()));
  const qids = new Set(rows.filter((r) => r.state === "sending").map((r) => r.queue_id));
  for (const qid of qids) {
    await withCoordinator(db, qid, () => resetSending(db, qid));
  }
}

export function open() {
  if (!dbPromise) {
    const p = openDb().then(async (db) => {
      await restoreSending(db);
      return db;
    });
    dbPromise = p;
    p.catch(() => {
      if (dbPromise === p) dbPromise = null;
    });
  }
  return dbPromise.then(() => undefined);
}

async function getDb() {
  await open();
  return dbPromise;
}

// ------------------------------------------------------------------ device + identity
async function deviceRowTx(t) {
  const store = t.objectStore("device");
  let d = await req(store.get("device"));
  if (!d) {
    d = { id: "device", device_id: uuid(), current_user_id: null };
    await req(store.put(d));
  }
  return d;
}

export async function deviceId() {
  const db = await getDb();
  const d = await inTx(db, ["device"], "readwrite", deviceRowTx);
  return d.device_id;
}

export async function identity() {
  const db = await getDb();
  const d = await inTx(db, ["device"], "readwrite", deviceRowTx);
  return d.current_user_id ?? null;
}

// Sign-in sets it (from the server), sign-out clears it. Switching away from an account deletes its cached
// snapshots (they hold names and run state); its queue and commands are kept, keyed by user_id, and become
// invisible to anyone else (15.5, 15.10 item 1).
export async function setIdentity(userId) {
  const next = userId === undefined || userId === "" ? null : userId;
  const db = await getDb();
  await inTx(db, ["device", "snapshots"], "readwrite", async (t) => {
    const d = await deviceRowTx(t);
    const prev = d.current_user_id ?? null;
    if (prev !== null && !sameUser(prev, next)) {
      const snaps = t.objectStore("snapshots");
      const all = await req(snaps.getAll());
      for (const s of all) if (sameUser(s.user_id, prev)) await req(snaps.delete(s.id));
    }
    d.current_user_id = next;
    await req(t.objectStore("device").put(d));
  });
  notify();
}

// The current identity's queue id for this run, or throws NoIdentity.
async function currentTx(t, run) {
  const d = await deviceRowTx(t);
  if (d.current_user_id === null || d.current_user_id === undefined) throw new NoIdentity();
  return { user: d.current_user_id, device: d.device_id, qid: queueId(d.current_user_id, run, d.device_id) };
}

// ------------------------------------------------------------------ snapshots (15.10 item 1)
export async function saveSnapshot(snapshot) {
  if (!snapshot || !snapshot.user || !snapshot.run || !snapshot.run.code) throw new Error("Not a run snapshot.");
  const db = await getDb();
  await inTx(db, ["device", "snapshots"], "readwrite", async (t) => {
    const d = await deviceRowTx(t);
    if (!sameUser(snapshot.user.id, d.current_user_id)) throw new IdentityMismatch();
    await req(
      t.objectStore("snapshots").put({
        id: `${d.current_user_id}:${snapshot.run.code}`,
        user_id: d.current_user_id,
        run: snapshot.run.code,
        snapshot_version: snapshot.snapshot_version ?? null,
        fetched_at: snapshot.fetched_at ?? null,
        saved_at: Date.now(),
        body: frozen(snapshot),
      }),
    );
  });
  notify();
}

async function snapshotRowTx(t, run) {
  const d = await deviceRowTx(t);
  if (d.current_user_id === null || d.current_user_id === undefined) return null;
  const row = await req(t.objectStore("snapshots").get(`${d.current_user_id}:${run}`));
  return row && sameUser(row.user_id, d.current_user_id) ? row : null;
}

export async function snapshot(run) {
  const db = await getDb();
  const row = await inTx(db, ["device", "snapshots"], "readwrite", (t) => snapshotRowTx(t, run));
  return row ? row.body : null;
}

// ------------------------------------------------------------------ queues (bootstrap, 15.10 item 5)
function adoptGeneration(q, generation, nextExpected, lastDecided, lastOutcome) {
  // A new server generation (an abandoned step, 14.8 item 4) restarts the ordinary stream at the server's seq.
  q.generation = generation;
  q.next_seq = Number.isInteger(nextExpected) && nextExpected >= 1 ? nextExpected : 1;
  q.last_decided_seq = Number.isInteger(lastDecided) ? lastDecided : 0;
  q.last_decided_outcome = q.last_decided_seq >= 1 ? lastOutcome ?? null : null;
  q.server_generation = generation;
  q.server_next_expected_seq = q.next_seq;
}

function noteServerState(q, generation, nextExpected, lastDecided) {
  // Same generation: remember the server's position (used when giving out seq) but never move next_seq back.
  if (!Number.isInteger(generation) || generation !== q.generation) return;
  q.server_generation = generation;
  if (Number.isInteger(nextExpected)) {
    q.server_next_expected_seq = Math.max(q.server_next_expected_seq || 0, nextExpected);
  }
  if (Number.isInteger(lastDecided) && lastDecided > (q.last_decided_seq || 0)) {
    q.last_decided_seq = lastDecided;
    q.last_decided_outcome = null; // decided by someone else; this phone does not know the outcome
  }
}

export async function ensureQueue(run, ledger) {
  if (!ledger || !Number.isInteger(ledger.generation) || !Number.isInteger(ledger.next_expected_seq)) {
    throw new Error("The snapshot has no ledger state for this phone.");
  }
  const db = await getDb();
  const q = await inTx(db, ["device", "queues"], "readwrite", async (t) => {
    const me = await currentTx(t, run);
    if (ledger.device_id && ledger.device_id !== me.device) throw new Error("The ledger is for another phone.");
    const store = t.objectStore("queues");
    let row = await req(store.get(me.qid));
    if (!row) {
      row = {
        id: me.qid,
        user_id: me.user,
        run,
        device_id: me.device,
        generation: ledger.generation,
        next_seq: ledger.next_expected_seq,
        last_decided_seq: ledger.last_decided_seq ?? 0,
        last_decided_outcome: (ledger.last_decided_seq ?? 0) >= 1 ? ledger.last_decided_outcome ?? null : null,
        last_sync_at: null,
        queue_schema: QUEUE_SCHEMA,
        server_generation: ledger.generation,
        server_next_expected_seq: ledger.next_expected_seq,
        created_at: Date.now(),
      };
    } else if (!schemaOk(row.queue_schema)) {
      return row; // written by a newer client: never touched here
    } else if (ledger.generation > row.generation) {
      adoptGeneration(
        row,
        ledger.generation,
        ledger.next_expected_seq,
        ledger.last_decided_seq,
        ledger.last_decided_outcome,
      );
    } else {
      // Same or older generation: the phone keeps its own allocation (never reset next_seq from a snapshot).
      noteServerState(row, ledger.generation, ledger.next_expected_seq, ledger.last_decided_seq);
    }
    await req(store.put(row));
    return row;
  });
  notify();
  return q;
}

// ------------------------------------------------------------------ recording
function checkCommand(command, payload) {
  if (typeof command !== "string" || !/^[a-z_]{1,40}\.[a-z_]{1,40}$/.test(command)) {
    throw new Error("A command is a name such as trip.arrived_site.");
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("A payload is an object.");
  }
  if (new TextEncoder().encode(JSON.stringify(payload)).length > PAYLOAD_MAX_BYTES) {
    throw new Error("The payload is over 4 KB.");
  }
}

// The atomic allocation (15.10 item 2): called inside ONE read-write transaction over queues and commands that
// reads next_seq, writes the frozen command and updates next_seq.
async function allocateTx(t, q, command, payload, extra) {
  let seq = q.next_seq;
  if (q.server_generation === q.generation && Number.isInteger(q.server_next_expected_seq)) {
    seq = Math.max(seq, q.server_next_expected_seq); // never claim a seq the server already decided
  }
  const lastSeq = q.last_decided_seq || 0;
  const lastOutcome = lastSeq >= 1 ? q.last_decided_outcome ?? null : null;
  const now = new Date();
  const key = uuid();
  const body = {
    key,
    channel: "ordinary",
    seq,
    last_decided_seq: lastSeq,
    last_decided_outcome: lastOutcome,
    command,
    payload: frozen(payload),
    device_observed_at: localIso(now),
  };
  const row = {
    key,
    queue_id: q.id,
    user_id: q.user_id,
    run: q.run,
    device_id: q.device_id,
    channel: "ordinary",
    seq,
    generation: q.generation,
    command,
    payload: body.payload,
    device_observed_at: body.device_observed_at,
    last_decided_seq: lastSeq,
    last_decided_outcome: lastOutcome,
    body,
    state: "local",
    reason_code: "",
    message: "",
    final: false,
    result: null,
    recorded_at: null,
    created_at: now.getTime(),
    sent_at: null,
    decided_at: null,
    queue_schema: QUEUE_SCHEMA,
    ...extra,
  };
  await req(t.objectStore("commands").add(row));
  q.next_seq = seq + 1;
  await req(t.objectStore("queues").put(q));
  return row;
}

export async function recordOrdinary(run, command, payload = {}) {
  checkCommand(command, payload);
  if (updateRequired) throw new UpdateRequired();
  const db = await getDb();
  const row = await inTx(db, ["device", "queues", "commands"], "readwrite", async (t) => {
    const me = await currentTx(t, run);
    const q = await req(t.objectStore("queues").get(me.qid));
    if (!q) throw new NeedsConnectionOnce();
    if (!schemaOk(q.queue_schema)) throw new UpdateRequired();
    return allocateTx(t, q, command, payload, {});
  });
  notify();
  return row;
}

// Class B (15.4): no seq, no dependency on a queue or a snapshot; recorded at once.
export async function recordProtective(run, command, payload = {}) {
  checkCommand(command, payload);
  const db = await getDb();
  const row = await inTx(db, ["device", "queues", "commands", "snapshots"], "readwrite", async (t) => {
    const me = await currentTx(t, run);
    const q = await req(t.objectStore("queues").get(me.qid));
    let generation = null;
    let lastSeq = null;
    let lastOutcome = null;
    if (q && schemaOk(q.queue_schema)) {
      generation = q.generation;
      lastSeq = q.last_decided_seq || 0;
      lastOutcome = q.last_decided_outcome ?? null;
    } else {
      const snap = await snapshotRowTx(t, run);
      const led = snap && snap.body && snap.body.ledger;
      if (led) {
        generation = led.generation ?? null;
        lastSeq = led.last_decided_seq ?? 0;
        lastOutcome = led.last_decided_outcome ?? null;
      }
    }
    if (!lastSeq) lastOutcome = null; // 14.9: an outcome needs the seq it describes
    const now = new Date();
    const key = uuid();
    const body = {
      key,
      channel: "protective",
      seq: null,
      last_decided_seq: lastSeq,
      last_decided_outcome: lastOutcome,
      command,
      payload: frozen(payload),
      device_observed_at: localIso(now),
    };
    const r = {
      key,
      queue_id: me.qid,
      user_id: me.user,
      run,
      device_id: me.device,
      channel: "protective",
      seq: null,
      generation,
      command,
      payload: body.payload,
      device_observed_at: body.device_observed_at,
      last_decided_seq: lastSeq,
      last_decided_outcome: lastOutcome,
      body,
      state: "local",
      reason_code: "",
      message: "",
      final: false,
      result: null,
      recorded_at: null,
      created_at: now.getTime(),
      sent_at: null,
      decided_at: null,
      queue_schema: QUEUE_SCHEMA,
    };
    await req(t.objectStore("commands").add(r));
    return r;
  });
  notify();
  return row;
}

// Record again (15.10 item 5): a new command with a new key and a new seq; the old row is kept as it was, only
// pointing at its replacement.
export async function recordAgain(key) {
  if (updateRequired) throw new UpdateRequired();
  const db = await getDb();
  const row = await inTx(db, ["device", "queues", "commands"], "readwrite", async (t) => {
    const cmds = t.objectStore("commands");
    const old = await req(cmds.get(key));
    if (!old) throw new Error("No such step on this phone.");
    const me = await currentTx(t, old.run);
    if (old.queue_id !== me.qid) throw new IdentityMismatch();
    if (old.channel !== "ordinary" || old.state !== "rejected" || !RECORD_AGAIN_CODES.has(old.reason_code)) {
      throw new Error("Only a step already recorded by someone else, or replaced after a skipped step, can be recorded again.");
    }
    if (old.replaced_by) throw new Error("This step was already recorded again.");
    const q = await req(t.objectStore("queues").get(me.qid));
    if (!q) throw new NeedsConnectionOnce();
    if (!schemaOk(q.queue_schema)) throw new UpdateRequired();
    const fresh = await allocateTx(t, q, old.command, old.payload, { replaces: old.key });
    old.replaced_by = fresh.key;
    await req(cmds.put(old));
    return fresh;
  });
  notify();
  return row;
}

// ------------------------------------------------------------------ reading
function ordered(rows) {
  const prot = rows.filter((r) => r.channel === "protective").sort((a, b) => a.created_at - b.created_at);
  const ord = rows
    .filter((r) => r.channel !== "protective")
    .sort((a, b) => (a.generation ?? 0) - (b.generation ?? 0) || a.seq - b.seq || a.created_at - b.created_at);
  return prot.concat(ord);
}

async function rowsOf(db, qid) {
  return inTx(db, ["commands"], "readonly", (t) => req(t.objectStore("commands").index("queue_id").getAll(qid)));
}

async function current(db, run) {
  return inTx(db, ["device"], "readwrite", async (t) => {
    try {
      return await currentTx(t, run);
    } catch (e) {
      if (e instanceof NoIdentity) return null;
      throw e;
    }
  });
}

export async function list(run) {
  const db = await getDb();
  const me = await current(db, run);
  if (!me) return [];
  return ordered(await rowsOf(db, me.qid));
}

// 15.10 item 3 with the EM clarification (2026-11-26, binding). An empty protective state in a snapshot alone
// never clears a report that is unsent or was rejected (it may never have reached the server). Per report:
//   (a) local / sending / blocked_access: never settled; only sending it can change that;
//   (b) rejected (final): "not_accepted" until resolveProtective() reconciles it explicitly;
//   (c) confirmed: settled once a snapshot saved after the confirmation shows protective.open === false.
// The banner is non-null while any report of this user+run is not settled.
function snapshotClosedAt(snapRow) {
  return snapRow && snapRow.body && snapRow.body.protective && snapRow.body.protective.open === false
    ? snapRow.saved_at
    : null;
}

function protectiveSettled(r, closedAt) {
  if (r.state === "confirmed") return closedAt !== null && r.decided_at !== null && r.decided_at < closedAt;
  if (r.state === "rejected") return r.resolved_locally === true;
  return false;
}

export async function protectiveBanner(run) {
  const db = await getDb();
  const me = await current(db, run);
  if (!me) return null;
  const rows = (await rowsOf(db, me.qid)).filter((r) => r.channel === "protective");
  if (!rows.length) return null;
  const snapRow = await inTx(db, ["device", "snapshots"], "readwrite", (t) => snapshotRowTx(t, run));
  const closedAt = snapshotClosedAt(snapRow);
  const active = rows.filter((r) => !protectiveSettled(r, closedAt));
  if (!active.length) return null;
  const newest = (xs) => xs.sort((a, b) => b.created_at - a.created_at)[0];
  const rejected = active.filter((r) => r.state === "rejected");
  if (rejected.length) {
    const r = newest(rejected);
    return { state: "not_accepted", reason: r.reason_code || "REJECTED", message: MSG.notAccepted, detail: r.message || "" };
  }
  const waiting = active.filter((r) => r.state === "local" || r.state === "blocked_access");
  if (waiting.length) {
    const blocked = waiting.filter((r) => r.state === "blocked_access");
    if (blocked.length) {
      const r = newest(blocked);
      return { state: "saved", reason: r.reason_code || "NOT_PERMITTED", message: MSG.waiting, detail: r.message || "" };
    }
    return { state: "saved", reason: "", message: MSG.saved, detail: "" };
  }
  if (active.some((r) => r.state === "sending")) return { state: "sending", reason: "", message: MSG.sending, detail: "" };
  const r = newest(active.filter((x) => x.state === "confirmed"));
  const effect = r && r.result && r.result.effect;
  return {
    state: "accepted",
    reason: effect || "",
    // effect=protective_state: the server's own words (what it recorded instead). Never permission to operate.
    message: MSG.accepted,
    detail: (r && r.message) || "",
  };
}

// EM clarification (b): explicit reconciliation of a finally rejected protective report. Allowed only while the
// phone is online AND a fresh snapshot (saved after the rejection, at most SNAPSHOT_FRESH_MS old) shows
// protective.open === false. The row is kept; it gains resolved_locally, resolved_note and resolved_at.
export async function resolveProtective(key, note) {
  if (typeof note !== "string" || !note.trim()) throw new Error("Say how this report was resolved.");
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    throw new Error("Needs a connection: fetch the run again, then resolve the report.");
  }
  const db = await getDb();
  const row = await inTx(db, ["device", "commands", "snapshots"], "readwrite", async (t) => {
    const cmds = t.objectStore("commands");
    const r = await req(cmds.get(key));
    if (!r) throw new Error("No such report on this phone.");
    const me = await currentTx(t, r.run);
    if (r.queue_id !== me.qid) throw new IdentityMismatch();
    if (r.channel !== "protective" || r.state !== "rejected" || !r.final) {
      throw new Error("Only a report the server did not accept can be resolved here.");
    }
    if (r.resolved_locally) return r;
    const snapRow = await snapshotRowTx(t, r.run);
    const closedAt = snapshotClosedAt(snapRow);
    const fresh =
      closedAt !== null && r.decided_at !== null && closedAt > r.decided_at && Date.now() - closedAt <= SNAPSHOT_FRESH_MS;
    if (!fresh) {
      throw new Error("Fetch the run again first: it must show no open interruption, pause or stop.");
    }
    Object.assign(r, { resolved_locally: true, resolved_note: note.trim(), resolved_at: Date.now() });
    await req(cmds.put(r));
    return r;
  });
  notify();
  return row;
}

// ------------------------------------------------------------------ coordinator (15.10 item 2)
const LOCKED = Symbol("locked");

function hasWebLocks() {
  return typeof navigator !== "undefined" && navigator.locks && typeof navigator.locks.request === "function";
}

async function leaseTake(db, qid) {
  return inTx(db, ["queues"], "readwrite", async (t) => {
    const store = t.objectStore("queues");
    const id = `lease:${qid}`;
    const row = (await req(store.get(id))) || { id, lease_owner: null, lease_until: 0 };
    const now = Date.now();
    if (row.lease_owner && row.lease_owner !== TAB_ID && row.lease_until > now) return false;
    row.lease_owner = TAB_ID;
    row.lease_until = now + LEASE_MS;
    await req(store.put(row));
    return true;
  });
}

async function leaseRelease(db, qid) {
  await inTx(db, ["queues"], "readwrite", async (t) => {
    const store = t.objectStore("queues");
    const row = await req(store.get(`lease:${qid}`));
    if (row && row.lease_owner === TAB_ID) {
      row.lease_owner = null;
      row.lease_until = 0;
      await req(store.put(row));
    }
  });
}

// Runs fn(renew) as the only coordinator of this queue, or returns LOCKED without waiting.
async function withCoordinator(db, qid, fn) {
  if (hasWebLocks()) {
    return navigator.locks.request(lockName(qid), { ifAvailable: true }, async (lock) => {
      if (!lock) return LOCKED;
      return fn(async () => {});
    });
  }
  if (!(await leaseTake(db, qid))) return LOCKED;
  try {
    return await fn(() => leaseTake(db, qid));
  } finally {
    await leaseRelease(db, qid);
  }
}

// ------------------------------------------------------------------ sync (15.3, 15.5, 15.10 items 2 and 5)
async function resetSending(db, qid, keys) {
  const changed = await inTx(db, ["commands"], "readwrite", async (t) => {
    const store = t.objectStore("commands");
    const rows = keys
      ? await Promise.all(keys.map((k) => req(store.get(k))))
      : await req(store.index("queue_id").getAll(qid));
    let n = 0;
    for (const r of rows) {
      if (r && r.state === "sending") {
        r.state = "local";
        await req(store.put(r));
        n += 1;
      }
    }
    return n;
  });
  if (changed) notify();
}

async function markSending(db, keys) {
  await inTx(db, ["commands"], "readwrite", async (t) => {
    const store = t.objectStore("commands");
    const now = Date.now();
    for (const k of keys) {
      const r = await req(store.get(k));
      if (r && r.state === "local") {
        r.state = "sending";
        r.sent_at = now;
        await req(store.put(r));
      }
    }
  });
  notify();
}

async function post(fetchImpl, url, body, timeoutMs) {
  const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  try {
    const resp = await fetchImpl(url, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify(body),
      signal: ctrl ? ctrl.signal : undefined,
    });
    let data = null;
    try {
      data = await resp.json();
    } catch (_) {
      data = null; // a body that is not JSON is treated like no answer
    }
    return { status: resp.status, data };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// Decides the sent rows from the per-command results, matched by key (+ channel when the result carries it, and
// seq for ordinary rows), never by position. Anything unmatched or not final goes back to `local`.
async function applyResults(db, qid, sentKeys, data) {
  const byKey = new Map();
  const dup = new Set();
  for (const res of data.results) {
    if (!res || typeof res.key !== "string") continue;
    if (byKey.has(res.key)) dup.add(res.key);
    byKey.set(res.key, res);
  }
  const decided = await inTx(db, ["queues", "commands"], "readwrite", async (t) => {
    const cmds = t.objectStore("commands");
    const queues = t.objectStore("queues");
    const q = await req(queues.get(qid));
    const now = Date.now();
    let n = 0;
    for (const key of sentKeys) {
      const row = await req(cmds.get(key));
      if (!row || row.state !== "sending") continue;
      const res = dup.has(key) ? null : byKey.get(key);
      const matches =
        res &&
        (res.channel === undefined || res.channel === row.channel) &&
        (row.channel !== "ordinary" || res.seq === undefined || res.seq === null || res.seq === row.seq);
      if (matches && res.outcome === "confirmed") {
        Object.assign(row, {
          state: "confirmed",
          final: true,
          reason_code: "",
          message: res.message || "",
          result: res.result || {},
          recorded_at: res.recorded_at || null,
          decided_at: now,
        });
        n += 1;
        if (q && row.channel === "ordinary" && row.generation === q.generation && row.seq >= (q.last_decided_seq || 0)) {
          q.last_decided_seq = row.seq;
          q.last_decided_outcome = "confirmed";
        }
      } else if (matches && res.outcome === "rejected" && (res.reason_code === "NOT_PERMITTED" || res.final === false)) {
        Object.assign(row, {
          state: "blocked_access",
          final: false,
          reason_code: res.reason_code || "NOT_PERMITTED",
          message: res.message || "",
        });
      } else if (matches && res.outcome === "rejected") {
        Object.assign(row, {
          state: "rejected",
          final: true,
          reason_code: res.reason_code || "REJECTED",
          message: res.message || "",
          result: res.result || {},
          recorded_at: res.recorded_at || null,
          decided_at: now,
        });
        n += 1;
        const ownSeqDecision = !RECORD_AGAIN_CODES.has(row.reason_code) && row.reason_code !== "KEY_REUSED";
        if (q && ownSeqDecision && row.channel === "ordinary" && row.generation === q.generation && row.seq >= (q.last_decided_seq || 0)) {
          q.last_decided_seq = row.seq;
          q.last_decided_outcome = "rejected";
        }
      } else {
        row.state = "local"; // held, retry, missing, ambiguous: resent later with the same key
      }
      await req(cmds.put(row));
    }
    if (q && schemaOk(q.queue_schema)) {
      if (Number.isInteger(data.generation) && data.generation > q.generation) {
        adoptGeneration(q, data.generation, data.next_expected_seq, data.last_decided_seq, null);
      } else {
        noteServerState(q, data.generation, data.next_expected_seq, data.last_decided_seq);
      }
      q.last_sync_at = new Date().toISOString();
      await req(queues.put(q));
    }
    return n;
  });
  notify();
  return decided;
}

async function syncState(db, run) {
  const me = await current(db, run);
  if (!me) return null;
  const q = await inTx(db, ["queues"], "readonly", (t) => req(t.objectStore("queues").get(me.qid)));
  return { me, q };
}

export async function syncOnce(run, { fetchImpl, timeoutMs = FETCH_TIMEOUT_MS } = {}) {
  const doFetch = fetchImpl || ((...a) => globalThis.fetch(...a));
  if (updateRequired) return { status: "update_required", decided: 0 };
  const db = await getDb();
  const st = await syncState(db, run);
  if (!st) return { status: "signin", decided: 0 };
  const { me } = st;
  if (st.q && !schemaOk(st.q.queue_schema)) return { status: "update_required", decided: 0 };
  if ((await rowsOf(db, me.qid)).some((r) => !schemaOk(r.queue_schema))) {
    return { status: "update_required", decided: 0 };
  }
  const out = await withCoordinator(db, me.qid, async (renew) => {
    await resetSending(db, me.qid); // we are the only coordinator: nothing else is sending this queue
    const attempted = new Set();
    let decided = 0;
    for (;;) {
      const q = await inTx(db, ["queues"], "readonly", (t) => req(t.objectStore("queues").get(me.qid)));
      const rows = await rowsOf(db, me.qid);
      const sendable = rows.filter((r) => r.state === "local" && !attempted.has(r.key));
      if (!sendable.length) break;
      const prot = sendable.filter((r) => r.channel === "protective").sort((a, b) => a.created_at - b.created_at);
      const ord = sendable
        .filter((r) => r.channel === "ordinary")
        .sort((a, b) => (a.generation ?? 0) - (b.generation ?? 0) || a.seq - b.seq);
      let batch = prot.slice(0, BATCH_MAX);
      let generation = q ? q.generation : null;
      if (ord.length && batch.length < BATCH_MAX) {
        // One request carries one generation: rows of an older generation go in their own request, so the
        // server answers them (replay or AFTER_ABANDONED / STALE_GENERATION) instead of claiming new seqs.
        const g = ord[0].generation;
        batch = batch.concat(ord.filter((r) => r.generation === g).slice(0, BATCH_MAX - batch.length));
        generation = g;
      }
      if (!Number.isInteger(generation)) {
        const snap = await snapshot(run);
        generation = (snap && snap.ledger && snap.ledger.generation) || prot[0].generation || 1;
      }
      const keys = batch.map((r) => r.key);
      const body = {
        device_id: me.device,
        run,
        generation,
        unsent_count: rows.filter((r) => UNSENT_STATES.has(r.state)).length,
        commands: batch.map((r) => r.body), // the frozen bodies, byte for byte the same on every retry
      };
      await markSending(db, keys);
      await renew();
      let resp;
      try {
        resp = await post(doFetch, SYNC_URL, body, timeoutMs);
      } catch (_) {
        await resetSending(db, me.qid, keys);
        return { status: "offline", decided };
      }
      if (resp.status === 401) {
        await resetSending(db, me.qid, keys);
        return { status: "signin", decided };
      }
      if (resp.status === 403 && resp.data && resp.data.error === "NOT_YOUR_RUN") {
        await resetSending(db, me.qid, keys);
        return { status: "not_your_run", decided };
      }
      if (resp.status !== 200 || !resp.data || !Array.isArray(resp.data.results)) {
        // 5xx, 503 OUTCOME_UNKNOWN, or any other answer without per-command results: nothing is decided.
        await resetSending(db, me.qid, keys);
        return {
          status: "offline",
          decided,
          http: resp.status,
          error: (resp.data && resp.data.error) || null,
        };
      }
      decided += await applyResults(db, me.qid, keys, resp.data);
      for (const k of keys) attempted.add(k);
    }
    return { status: "ok", decided };
  });
  return out === LOCKED ? { status: "locked", decided: 0 } : out;
}

export async function retryBlocked(run) {
  const db = await getDb();
  const me = await current(db, run);
  if (!me) return 0;
  const n = await inTx(db, ["commands"], "readwrite", async (t) => {
    const store = t.objectStore("commands");
    const rows = await req(store.index("queue_id").getAll(me.qid));
    let c = 0;
    for (const r of rows) {
      if (r.state === "blocked_access") {
        r.state = "local";
        await req(store.put(r));
        c += 1;
      }
    }
    return c;
  });
  if (n) notify();
  return n;
}

// ------------------------------------------------------------------ abandonment (14.3 rule 6, 14.8 item 4, 15.5)
export async function abandon(run, seq, reason, { fetchImpl, timeoutMs = FETCH_TIMEOUT_MS } = {}) {
  const doFetch = fetchImpl || ((...a) => globalThis.fetch(...a));
  const none = (status, extra = {}) => ({ status, newGeneration: null, invalid: [], ...extra });
  if (updateRequired) return none("update_required");
  const db = await getDb();
  const st = await syncState(db, run);
  if (!st) return none("signin");
  if (!st.q) throw new NeedsConnectionOnce();
  if (!schemaOk(st.q.queue_schema)) return none("update_required");
  const { me } = st;
  const out = await withCoordinator(db, me.qid, async (renew) => {
    await renew();
    const q = await inTx(db, ["queues"], "readonly", (t) => req(t.objectStore("queues").get(me.qid)));
    let resp;
    try {
      resp = await post(doFetch, ABANDON_URL, { run, generation: q.generation, seq, reason }, timeoutMs);
    } catch (_) {
      return none("offline");
    }
    if (resp.status === 401) return none("signin");
    if (resp.status === 403 && resp.data && resp.data.error === "NOT_YOUR_RUN") return none("not_your_run");
    if (resp.status !== 200 || !resp.data) {
      return none(resp.status >= 500 ? "offline" : "refused", {
        http: resp.status,
        error: (resp.data && resp.data.error) || null,
        message: (resp.data && resp.data.message) || "",
      });
    }
    const data = resp.data;
    const barrier = (data.abandoned && Number.isInteger(data.abandoned.seq) && data.abandoned.seq) || seq;
    const oldGen = (data.abandoned && data.abandoned.generation) || q.generation;
    if (!Number.isInteger(data.generation) || data.generation <= q.generation) {
      return none("ok");
    }
    const invalid = await inTx(db, ["queues", "commands"], "readwrite", async (t) => {
      const cmds = t.objectStore("commands");
      const queues = t.objectStore("queues");
      const rows = await req(cmds.index("queue_id").getAll(me.qid));
      const now = Date.now();
      const marked = [];
      for (const r of rows) {
        if (r.channel === "ordinary" && r.generation === oldGen && r.seq >= barrier && UNSENT_STATES.has(r.state)) {
          Object.assign(r, {
            state: "rejected",
            final: true,
            reason_code: "AFTER_ABANDONED",
            message: MSG.afterAbandoned,
            decided_at: now,
          });
          await req(cmds.put(r));
          marked.push(r);
        }
      }
      const fresh = await req(queues.get(me.qid));
      adoptGeneration(fresh, data.generation, data.next_expected_seq, data.last_decided_seq, null);
      fresh.last_sync_at = new Date().toISOString();
      await req(queues.put(fresh));
      return ordered(marked);
    });
    notify();
    return { status: "ok", newGeneration: data.generation, invalid };
  });
  return out === LOCKED ? none("locked") : out;
}
