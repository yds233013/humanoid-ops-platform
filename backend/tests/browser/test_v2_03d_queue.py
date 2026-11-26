"""v2 03D-1: the offline field queue (execution/field_static/queue.js) in a real browser (Chromium, Playwright).

Contract: docs/13_v2_phase03_contract.md 14.1, 14.8, 14.9, 15.2, 15.3, 15.5, 15.6 and 15.10 (15.10 wins), plus the
EM clarification of 2026-11-26 on clearing protective reports (Q.resolveProtective).

The module is served straight from disk by a tiny local http.server (no Django view: 03D-2 owns that). The page is
http://127.0.0.1 (a secure context, so IndexedDB, Web Locks and crypto.randomUUID behave as on the phone).
/api/field/sync and /api/field/abandon are faked with Playwright routing by FakeField, which answers exactly in the
14.1 / 14.8 / 14.9 shape (top-level generation, last_decided_seq, next_expected_seq; per-command key, outcome,
reason_code, message, seq, recorded_at, result, final; plus channel per 15.10 item 5 unless switched off).
No database is used. Synthetic data only.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.browser

BACKEND = Path(__file__).resolve().parents[2]
QUEUE_JS = BACKEND / "execution" / "field_static" / "queue.js"
HARNESS = Path(__file__).with_name("harness.html")
RUN = "EX-0007"
NOW = "2026-11-27T09:41:07-08:00"
CSRF = "test-csrf-token-0123456789abcdef0123456789"


# ------------------------------------------------------------------ local harness server
class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(HARNESS.read_bytes(), "text/html; charset=utf-8")
        elif path == "/field/queue.js":
            self._send(QUEUE_JS.read_bytes(), "text/javascript; charset=utf-8")
        else:
            self.send_error(404)

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture(scope="session")
def harness_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(BACKEND)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ------------------------------------------------------------------ fake server (14.1 / 14.8 / 14.9)
Step = str | Callable[[dict[str, Any], "FakeField"], tuple[int, dict[str, Any]]]


class FakeField:
    """A small model of the 03C ledger for one run. `script` overrides the next requests in order."""

    def __init__(self) -> None:
        self.generation = 1
        self.next_expected = 1
        self.last_decided = 0
        self.stored: dict[str, dict[str, Any]] = {}
        self.applied: list[str] = []  # keys whose domain command ran (once each, at most)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.abandons: list[dict[str, Any]] = []
        self.script: list[Step] = []
        self.hung: list[Any] = []
        self.include_channel = True
        self.deny = False  # revoked grant: NOT_PERMITTED, final=false, not stored
        self.protective_rejects: str | None = None  # a final rejection code for protective commands

    def result(self, cmd: dict[str, Any], outcome: str, code: str = "", message: str = "", **kw: Any) -> dict:
        final = outcome == "confirmed" or (outcome == "rejected" and code != "NOT_PERMITTED")
        r = {
            "key": cmd["key"],
            "outcome": outcome,
            "reason_code": code,
            "message": message,
            "seq": cmd["seq"],
            "recorded_at": NOW if outcome in ("confirmed", "rejected") else None,
            "result": kw.get("result", {}),
            "final": final,
        }
        if self.include_channel:
            r["channel"] = cmd["channel"]
        return r

    def state(self, run: str) -> dict[str, Any]:
        return {
            "server_time": NOW,
            "run": run,
            "generation": self.generation,
            "last_decided_seq": self.last_decided,
            "next_expected_seq": self.next_expected,
        }

    def decide(self, body: dict[str, Any]) -> dict[str, Any]:
        results = []
        for cmd in body["commands"]:
            key = cmd["key"]
            if key in self.stored:
                results.append(self.stored[key])  # replay: the stored outcome, nothing runs again
                continue
            if self.deny:
                results.append(self.result(cmd, "rejected", "NOT_PERMITTED", "Waiting for access."))
                continue
            if cmd["channel"] == "protective":
                if self.protective_rejects:
                    r = self.result(
                        cmd, "rejected", self.protective_rejects, "This report could not be recorded."
                    )
                else:
                    r = self.result(cmd, "confirmed", message="Recorded.", result={"effect": "applied"})
                    self.applied.append(key)
                self.stored[key] = r
            elif body["generation"] < self.generation:
                r = self.result(cmd, "rejected", "STALE_GENERATION", "Replaced after a skipped step.")
                self.stored[key] = r
            elif cmd["seq"] == self.next_expected:
                r = self.result(cmd, "confirmed", message="Recorded.")
                self.applied.append(key)
                self.stored[key] = r
                self.next_expected += 1
                self.last_decided = cmd["seq"]
            elif cmd["seq"] < self.next_expected:
                r = self.result(cmd, "rejected", "SEQ_TAKEN", "Already recorded by Dana Ortiz at 09:40.")
                self.stored[key] = r
            else:
                r = self.result(cmd, "held", message="Waiting for an earlier step.")
            results.append(r)
        return {**self.state(body["run"]), "results": results}

    def on_sync(self, route: Any) -> None:
        body = route.request.post_data_json
        self.requests.append(body)
        self.headers.append(route.request.headers)
        step = self.script.pop(0) if self.script else None
        if step == "abort":
            route.abort("internetdisconnected")
            return
        if step == "hang":
            self.hung.append(route)
            return
        if step == "commit_then_503":
            self.decide(body)  # committed on the server, but the answer is lost
            status, out = 503, {"error": "OUTCOME_UNKNOWN", "message": "The server could not confirm."}
        elif callable(step):
            status, out = step(body, self)
        else:
            status, out = 200, self.decide(body)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(out))

    def release_hung(self) -> None:
        while self.hung:
            route = self.hung.pop(0)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(self.decide(route.request.post_data_json)),
            )

    def on_abandon(self, route: Any) -> None:
        body = route.request.post_data_json
        self.abandons.append(body)
        old = self.generation
        self.generation += 1
        self.next_expected, self.last_decided = 1, 0
        out = {**self.state(body["run"]), "abandoned": {"generation": old, "seq": body["seq"]}}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(out))


@pytest.fixture
def fake(context: Any, harness_url: str) -> FakeField:
    f = FakeField()
    context.add_cookies([{"name": "csrftoken", "value": CSRF, "url": harness_url}])
    context.route("**/api/field/sync", f.on_sync)
    context.route("**/api/field/abandon", f.on_abandon)
    return f


def tab(context: Any, url: str) -> Any:
    page = context.new_page()
    page.goto(url + "/")
    page.evaluate("window.ready")
    return page


def reload(page: Any) -> None:
    page.reload()
    page.evaluate("window.ready")


def view(page: Any) -> list[dict[str, Any]]:
    return page.evaluate("(run) => window.view(run)", RUN)


def boot(page: Any, user: int = 12, **ledger: Any) -> dict[str, Any]:
    return page.evaluate("([u, r, l]) => window.boot(u, r, l)", [user, RUN, ledger])


def record(page: Any, n: int = 1, command: str = "trip.observation_note") -> list[dict[str, Any]]:
    return page.evaluate(
        """async ([run, n, cmd]) => {
            const out = [];
            for (let i = 0; i < n; i++) out.push(await Q.recordOrdinary(run, cmd, {note: `step ${i}`}));
            return out;
        }""",
        [RUN, n, command],
    )


def protective(page: Any, command: str = "session.stop") -> dict[str, Any]:
    return page.evaluate(
        "([run, cmd]) => Q.recordProtective(run, cmd, {reason: 'test stop'})", [RUN, command]
    )


def sync(page: Any) -> dict[str, Any]:
    return page.evaluate("(run) => Q.syncOnce(run)", RUN)


def banner(page: Any) -> dict[str, Any] | None:
    return page.evaluate("(run) => Q.protectiveBanner(run)", RUN)


def error_name(page: Any, js: str, arg: Any = None) -> str:
    return page.evaluate(
        f"async (arg) => {{ try {{ await ({js})(arg); return 'no error'; }} catch (e) {{ return e.name; }} }}",
        arg,
    )


def wait_states(page: Any, state: str, n: int) -> None:
    page.wait_for_function(
        "([run, s, n]) => Q.list(run).then(rows => rows.filter(r => r.state === s).length === n)",
        arg=[RUN, state, n],
        timeout=5000,
    )


# ------------------------------------------------------------------ 15.10 item 2: atomic seq, two tabs
def test_two_tabs_recording_at_the_same_moment_get_distinct_consecutive_seq_and_stable_keys(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    a = tab(context, harness_url)
    boot(a)
    b = tab(context, harness_url)
    at = a.evaluate("Date.now()") + 400
    start = """([run, at]) => { window.res = (async () => {
        while (Date.now() < at) await new Promise(r => setTimeout(r, 1));
        const rows = await Promise.all([1, 2, 3, 4, 5].map(i => Q.recordOrdinary(run, 'trip.observation_note', {i})));
        return rows.map(r => ({key: r.key, seq: r.seq}));
    })(); }"""
    a.evaluate(start, [RUN, at])
    b.evaluate(start, [RUN, at])
    got = a.evaluate("window.res") + b.evaluate("window.res")
    assert sorted(r["seq"] for r in got) == list(range(1, 11))
    assert len({r["key"] for r in got}) == 10
    before = {r["key"]: r["seq"] for r in view(a)}
    assert before == {r["key"]: r["seq"] for r in got}
    reload(a)
    reload(b)
    assert {r["key"]: r["seq"] for r in view(a)} == before == {r["key"]: r["seq"] for r in view(b)}
    assert sync(a) == {"status": "ok", "decided": 10}
    assert [c["seq"] for c in fake.requests[0]["commands"]] == list(range(1, 11))


def test_an_older_snapshot_never_resets_next_seq(context: Any, harness_url: str, fake: FakeField) -> None:
    p = tab(context, harness_url)
    boot(p, next_expected_seq=5, last_decided_seq=4, last_decided_outcome="confirmed")
    assert [r["seq"] for r in record(p, 2)] == [5, 6]
    p.evaluate("(run) => Q.ensureQueue(run, {generation: 1, next_expected_seq: 5, last_decided_seq: 4})", RUN)
    assert record(p)[0]["seq"] == 7
    p.evaluate(
        "(run) => Q.ensureQueue(run, {generation: 1, next_expected_seq: 10, last_decided_seq: 9})", RUN
    )
    assert record(p)[0]["seq"] == 10  # never claims a seq the server already decided
    # the frozen body carries 14.8/14.9 causal fields as known at creation
    body = p.evaluate("(run) => Q.list(run).then(rows => rows[0].body)", RUN)
    assert set(body) == {
        "key",
        "channel",
        "seq",
        "last_decided_seq",
        "last_decided_outcome",
        "command",
        "payload",
        "device_observed_at",
    }
    assert body["last_decided_seq"] == 4 and body["last_decided_outcome"] == "confirmed"


# ------------------------------------------------------------------ 15.3: uncertain answers keep keys
def test_lost_503_and_network_error_resend_the_same_keys_and_confirm_once(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    keys = [r["key"] for r in record(p, 2)]
    fake.script = ["commit_then_503", "abort"]
    first = sync(p)
    assert first["status"] == "offline" and first["http"] == 503 and first["error"] == "OUTCOME_UNKNOWN"
    assert [r["state"] for r in view(p)] == ["local", "local"]
    assert sync(p)["status"] == "offline"
    assert [r["state"] for r in view(p)] == ["local", "local"]
    assert sync(p) == {"status": "ok", "decided": 2}
    assert [r["state"] for r in view(p)] == ["confirmed", "confirmed"]
    assert len(fake.requests) == 3
    assert fake.requests[0]["commands"] == fake.requests[1]["commands"] == fake.requests[2]["commands"]
    assert [c["key"] for c in fake.requests[2]["commands"]] == keys
    assert fake.applied == keys  # each applied once on the server
    assert all(h.get("x-csrftoken") == CSRF for h in fake.headers)
    assert fake.requests[0]["unsent_count"] == 2 and fake.requests[0]["generation"] == 1
    assert sync(p) == {"status": "ok", "decided": 0} and len(fake.requests) == 3  # nothing left to send


def test_http_200_with_held_retry_or_no_result_never_confirms(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    rows = record(p, 3)

    def partial_answer(body: dict[str, Any], f: FakeField) -> tuple[int, dict[str, Any]]:
        c = body["commands"]
        stranger = {**c[0], "key": "00000000-0000-4000-8000-000000000000"}
        results = [f.result(c[0], "held"), f.result(c[1], "retry"), f.result(stranger, "confirmed")]
        return 200, {**f.state(body["run"]), "results": results}  # no result at all for the third

    def no_results(body: dict[str, Any], f: FakeField) -> tuple[int, dict[str, Any]]:
        return 200, f.state(body["run"])

    def duplicate(body: dict[str, Any], f: FakeField) -> tuple[int, dict[str, Any]]:
        c = body["commands"][0]  # two answers for one key are ambiguous: not a confirmation
        return 200, {
            **f.state(body["run"]),
            "results": [f.result(c, "confirmed"), f.result(c, "rejected", "X")],
        }

    fake.script = [partial_answer, no_results, duplicate]
    assert sync(p) == {"status": "ok", "decided": 0}
    assert [r["state"] for r in view(p)] == ["local"] * 3
    assert sync(p)["status"] == "offline"
    assert [r["state"] for r in view(p)] == ["local"] * 3
    sync(p)
    assert [r["state"] for r in view(p)] == ["local"] * 3
    assert sync(p) == {"status": "ok", "decided": 3}
    assert [c["key"] for c in fake.requests[-1]["commands"]] == [r["key"] for r in rows]


def test_results_are_matched_by_key_and_channel_never_by_position(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    o = record(p)[0]
    pr = protective(p)

    def swapped(body: dict[str, Any], f: FakeField) -> tuple[int, dict[str, Any]]:
        prot, ordi = body["commands"]
        assert prot["channel"] == "protective" and ordi["channel"] == "ordinary"
        wrong = {**f.result(prot, "confirmed"), "channel": "ordinary"}  # right key, wrong channel
        f.next_expected, f.last_decided = 2, 1
        return 200, {**f.state(body["run"]), "results": [f.result(ordi, "confirmed"), wrong]}

    fake.script = [swapped]
    assert sync(p) == {"status": "ok", "decided": 1}
    states = {r["key"]: r["state"] for r in view(p)}
    assert states == {o["key"]: "confirmed", pr["key"]: "local"}
    fake.include_channel = False  # the 03C server's own shape: matched by key alone
    assert sync(p) == {"status": "ok", "decided": 1}
    assert {r["key"]: r["state"] for r in view(p)} == {o["key"]: "confirmed", pr["key"]: "confirmed"}


# ------------------------------------------------------------------ class B: protective
def test_protective_works_with_no_queue_and_ordinary_needs_a_connection_once(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    p.evaluate("() => Q.setIdentity(12)")
    assert (
        error_name(p, "(run) => Q.recordOrdinary(run, 'trip.arrived_site', {})", RUN) == "NeedsConnectionOnce"
    )
    pr = protective(p, "incident.report")
    assert banner(p)["state"] == "saved"
    assert "Robot must not operate" in banner(p)["message"]
    assert sync(p) == {"status": "ok", "decided": 1}
    sent = fake.requests[0]
    assert sent["generation"] == 1 and len(sent["commands"]) == 1
    assert sent["commands"][0]["channel"] == "protective" and sent["commands"][0]["seq"] is None
    assert sent["commands"][0]["key"] == pr["key"] and sent["commands"][0]["last_decided_outcome"] is None
    b = banner(p)
    assert b["state"] == "accepted" and "must not operate" in b["message"]  # never permission to operate
    # (c) a fresh snapshot after the confirmation with nothing open clears it
    p.evaluate("([run]) => window.saveProtective(12, run, false)", [RUN])
    assert banner(p) is None


def test_protective_is_sent_first_in_batches_of_at_most_20(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    record(p, 25)
    pr = protective(p)
    assert sync(p) == {"status": "ok", "decided": 26}
    assert [len(r["commands"]) for r in fake.requests] == [20, 6]
    first = fake.requests[0]["commands"]
    assert first[0]["key"] == pr["key"] and first[0]["channel"] == "protective"
    seqs = [c["seq"] for r in fake.requests for c in r["commands"] if c["channel"] == "ordinary"]
    assert seqs == list(range(1, 26))
    assert view(p)[0]["channel"] == "protective"  # list(): protective first, then seq


def test_a_confirmed_protective_report_clears_only_on_a_snapshot_fetched_after_the_confirmation(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    protective(p)
    p.evaluate("([run]) => window.saveProtective(12, run, false)", [RUN])  # older than the confirmation
    sync(p)
    assert banner(p)["state"] == "accepted"
    p.evaluate("([run]) => window.saveProtective(12, run, true)", [RUN])
    assert banner(p)["state"] == "accepted"
    p.evaluate("([run]) => window.saveProtective(12, run, false)", [RUN])
    assert banner(p) is None


def test_an_unsent_protective_report_is_never_cleared_by_an_empty_snapshot(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    pr = protective(p)
    fake.script = ["abort"]
    assert sync(p)["status"] == "offline"
    p.evaluate("([run]) => window.saveProtective(12, run, false)", [RUN])
    assert banner(p)["state"] == "saved"
    assert error_name(p, "(k) => Q.resolveProtective(k, 'looked fine')", pr["key"]) == "Error"
    assert banner(p)["state"] == "saved"
    assert sync(p) == {"status": "ok", "decided": 1}
    assert banner(p)["state"] == "accepted"  # the empty snapshot predates the confirmation
    p.evaluate("([run]) => window.saveProtective(12, run, false)", [RUN])
    assert banner(p) is None


def test_a_final_protective_rejection_stays_not_accepted_until_resolved_explicitly_online(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    fake.protective_rejects = "UNKNOWN_COMMAND"
    pr = protective(p, "session.pause")
    assert sync(p) == {"status": "ok", "decided": 1}
    b = banner(p)
    assert b["state"] == "not_accepted" and b["reason"] == "UNKNOWN_COMMAND"
    assert b["message"].startswith("Not accepted by the server: the robot must still not operate.")
    p.evaluate("([run]) => window.saveProtective(12, run, false)", [RUN])
    assert banner(p)["state"] == "not_accepted"  # an empty snapshot alone never clears it
    p.evaluate("() => Q.setIdentity(null)")
    assert banner(p) is None  # nobody signed in sees nothing
    p.evaluate("() => Q.setIdentity(12)")
    assert banner(p)["state"] == "not_accepted"  # kept for the user across sign-out
    resolve = "(k) => Q.resolveProtective(k, 'Ops lead Dana checked the robot')"
    assert error_name(p, resolve, pr["key"]) == "Error"  # no fresh snapshot (cleared at sign-out)
    p.evaluate("([run]) => window.saveProtective(12, run, false)", [RUN])
    context.set_offline(True)
    assert error_name(p, resolve, pr["key"]) == "Error"  # offline
    context.set_offline(False)
    p.evaluate("([run]) => window.saveProtective(12, run, true)", [RUN])
    assert error_name(p, resolve, pr["key"]) == "Error"  # the server still shows it open
    p.evaluate("([run]) => window.saveProtective(12, run, false)", [RUN])
    # older than the 2-minute limit (EM: one limit in the UI and the module): refused
    p.evaluate("() => { const real = Date.now; window.__realNow = real; Date.now = () => real() + 121000; }")
    assert error_name(p, resolve, pr["key"]) == "Error"
    p.evaluate("() => { Date.now = window.__realNow; }")
    assert error_name(p, resolve, pr["key"]) == "no error"
    assert banner(p) is None
    row = view(p)[0]
    assert row["state"] == "rejected" and row["resolved_locally"] is True and row["final"] is True
    assert sync(p) == {"status": "ok", "decided": 0} and len(fake.requests) == 1  # never resent


# ------------------------------------------------------------------ access, sign-in, wrong run
def test_not_permitted_is_blocked_access_until_try_again(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    key = record(p)[0]["key"]
    fake.deny = True
    assert sync(p) == {"status": "ok", "decided": 0}
    row = view(p)[0]
    assert (
        row["state"] == "blocked_access" and row["reason_code"] == "NOT_PERMITTED" and row["final"] is False
    )
    assert sync(p) == {"status": "ok", "decided": 0} and len(fake.requests) == 1  # not resent on its own
    fake.deny = False
    assert p.evaluate("(run) => Q.retryBlocked(run)", RUN) == 1
    assert sync(p) == {"status": "ok", "decided": 1}
    assert view(p)[0]["state"] == "confirmed"
    assert fake.requests[1]["commands"] == fake.requests[0]["commands"] and fake.applied == [key]


def test_401_asks_to_sign_in_and_403_not_your_run_stops_and_the_queue_is_kept(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    record(p, 2)
    fake.script = [
        lambda body, f: (
            401,
            {"error": "NOT_AUTHENTICATED", "message": "Sign in again. Nothing was recorded."},
        ),
        lambda body, f: (403, {"error": "NOT_YOUR_RUN", "message": "This is not one of your runs."}),
    ]
    assert sync(p) == {"status": "signin", "decided": 0}
    assert [r["state"] for r in view(p)] == ["local", "local"]
    assert sync(p) == {"status": "not_your_run", "decided": 0}
    assert [r["state"] for r in view(p)] == ["local", "local"]
    assert sync(p) == {"status": "ok", "decided": 2}


def test_an_identity_switch_hides_the_other_users_queue_and_snapshots(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p, 12)
    record(p, 2)
    protective(p)
    assert p.evaluate("(run) => Q.snapshot(run).then(s => s.user.id)", RUN) == 12
    p.evaluate("() => Q.setIdentity(13)")
    assert p.evaluate("() => Q.identity()") == 13
    assert view(p) == []
    assert p.evaluate("(run) => Q.snapshot(run)", RUN) is None
    assert banner(p) is None
    assert (
        error_name(p, "(run) => Q.recordOrdinary(run, 'trip.arrived_site', {})", RUN) == "NeedsConnectionOnce"
    )
    snap12 = "async (run) => Q.saveSnapshot(window.snapshotFor(12, run, await Q.deviceId()))"
    assert error_name(p, snap12, RUN) == "IdentityMismatch"
    assert (
        sync(p) == {"status": "ok", "decided": 0} and fake.requests == []
    )  # 12's steps are never sent by 13
    p.evaluate("() => Q.setIdentity(null)")
    assert sync(p) == {"status": "signin", "decided": 0}
    assert error_name(p, "(run) => Q.recordProtective(run, 'session.stop', {})", RUN) == "NoIdentity"
    p.evaluate("() => Q.setIdentity(12)")
    assert len(view(p)) == 3  # the queue is kept for its own user
    assert p.evaluate("(run) => Q.snapshot(run)", RUN) is None  # the snapshot was deleted at the switch
    assert banner(p)["state"] == "saved"


# ------------------------------------------------------------------ 15.6 restart
def test_a_reload_while_sending_puts_the_rows_back_to_local_with_the_same_keys(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    keys = [r["key"] for r in record(p, 3)]
    fake.script = ["hang"]
    p.evaluate("(run) => { window.pending = Q.syncOnce(run); }", RUN)
    wait_states(p, "sending", 3)
    reload(p)
    rows = view(p)
    assert [r["state"] for r in rows] == ["local"] * 3 and [r["key"] for r in rows] == keys
    assert sync(p) == {"status": "ok", "decided": 3}
    assert fake.requests[-1]["commands"] == fake.requests[0]["commands"]


# ------------------------------------------------------------------ SEQ_TAKEN and abandonment (15.10 item 5)
def test_seq_taken_is_final_and_record_again_makes_a_new_key_and_seq(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)  # the phone's snapshot says seq 1 is next...
    fake.next_expected, fake.last_decided = 2, 1  # ...but a second phone already recorded seq 1
    old = record(p, 1, "trip.arrived_site")[0]
    assert sync(p) == {"status": "ok", "decided": 1}
    row = view(p)[0]
    assert row["state"] == "rejected" and row["reason_code"] == "SEQ_TAKEN" and row["final"] is True
    msg = p.evaluate("(run) => Q.list(run).then(r => r[0].message)", RUN)
    assert msg.startswith("Already recorded by Dana Ortiz")
    new = p.evaluate("(k) => Q.recordAgain(k)", old["key"])
    assert new["key"] != old["key"] and new["seq"] == 2 and new["command"] == "trip.arrived_site"
    assert error_name(p, "(k) => Q.recordAgain(k)", old["key"]) == "Error"  # only once
    rows = {r["key"]: r for r in view(p)}
    assert rows[old["key"]]["state"] == "rejected" and rows[old["key"]]["replaced_by"] == new["key"]
    assert rows[new["key"]]["replaces"] == old["key"]
    assert sync(p) == {"status": "ok", "decided": 1}
    assert [c["key"] for c in fake.requests[-1]["commands"]] == [new["key"]]
    assert {r["key"]: r["state"] for r in view(p)} == {old["key"]: "rejected", new["key"]: "confirmed"}


def test_abandon_marks_after_abandoned_rows_and_the_new_generation_starts_at_seq_1(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    rows = record(p, 3)

    def gap_at_2(body: dict[str, Any], f: FakeField) -> tuple[int, dict[str, Any]]:
        c = body["commands"]
        f.stored[c[0]["key"]] = f.result(c[0], "confirmed")
        f.next_expected, f.last_decided = 2, 1
        out = [f.stored[c[0]["key"]], f.result(c[1], "retry"), f.result(c[2], "held")]
        return 200, {**f.state(body["run"]), "results": out}

    fake.script = [gap_at_2]
    sync(p)
    assert [r["state"] for r in view(p)] == ["confirmed", "local", "local"]
    out = p.evaluate("(run) => Q.abandon(run, 2, 'Step could not be done (test)')", RUN)
    assert fake.abandons == [
        {"run": RUN, "generation": 1, "seq": 2, "reason": "Step could not be done (test)"}
    ]
    assert out["status"] == "ok" and out["newGeneration"] == 2
    assert [(r["seq"], r["reason_code"], r["state"]) for r in out["invalid"]] == [
        (2, "AFTER_ABANDONED", "rejected"),
        (3, "AFTER_ABANDONED", "rejected"),
    ]
    assert view(p)[0]["state"] == "confirmed"
    again = p.evaluate("(k) => Q.recordAgain(k)", rows[1]["key"])
    assert again["generation"] == 2 and again["seq"] == 1
    assert sync(p) == {"status": "ok", "decided": 1}
    last = fake.requests[-1]
    assert last["generation"] == 2 and [c["key"] for c in last["commands"]] == [again["key"]]
    assert len(view(p)) == 4  # the old steps are kept for the record


# ------------------------------------------------------------------ 15.10 item 4: schema
def test_an_unknown_queue_schema_gives_update_required_and_deletes_nothing(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    record(p, 2)
    p.evaluate(
        """() => new Promise((resolve) => {
            const r = indexedDB.open('ops-field');
            r.onsuccess = () => {
                const db = r.result, t = db.transaction('queues', 'readwrite'), s = t.objectStore('queues');
                s.getAll().onsuccess = (ev) => { for (const q of ev.target.result) if (!q.id.startsWith('lease:')) { q.queue_schema = 99; s.put(q); } };
                t.oncomplete = () => { db.close(); resolve(); };
            };
        })"""
    )
    assert sync(p) == {"status": "update_required", "decided": 0} and fake.requests == []
    assert error_name(p, "(run) => Q.recordOrdinary(run, 'trip.arrived_site', {})", RUN) == "UpdateRequired"
    assert [r["state"] for r in view(p)] == ["local", "local"]


def test_a_newer_database_version_gives_update_required_and_keeps_the_rows(
    context: Any, harness_url: str, fake: FakeField
) -> None:
    p = tab(context, harness_url)
    boot(p)
    record(p, 2)
    p.evaluate(
        """() => new Promise((resolve) => {
            const r = indexedDB.open('ops-field', 2);
            r.onupgradeneeded = () => r.result.createObjectStore('future', {keyPath: 'id'});
            r.onsuccess = () => { r.result.close(); resolve(); };
        })"""
    )
    reload(p)
    assert sync(p) == {"status": "update_required", "decided": 0} and fake.requests == []
    assert [r["state"] for r in view(p)] == ["local", "local"]


# ------------------------------------------------------------------ coordinator lock + cross-tab events
@pytest.mark.parametrize("mode", ["web_locks", "lease_fallback"])
def test_a_queue_being_sent_by_another_tab_is_locked(
    context: Any, harness_url: str, fake: FakeField, mode: str
) -> None:
    if mode == "lease_fallback":
        context.add_init_script(
            "Object.defineProperty(Navigator.prototype, 'locks', {get() { return undefined; }, configurable: true});"
        )
    a = tab(context, harness_url)
    assert a.evaluate("() => typeof navigator.locks") == (
        "undefined" if mode == "lease_fallback" else "object"
    )
    boot(a)
    record(a, 2)
    b = tab(context, harness_url)
    fake.script = ["hang"]
    b.evaluate("(run) => { window.pending = Q.syncOnce(run); }", RUN)
    wait_states(b, "sending", 2)
    assert sync(a) == {"status": "locked", "decided": 0}
    assert [r["state"] for r in view(a)] == ["sending", "sending"]  # a's open() left b's batch alone
    fake.release_hung()
    assert b.evaluate("window.pending") == {"status": "ok", "decided": 2}
    assert len(fake.requests) == 1
    record(a, 1)
    assert sync(a) == {"status": "ok", "decided": 1}  # released once b finished


def test_subscribe_hears_changes_from_another_tab(context: Any, harness_url: str, fake: FakeField) -> None:
    a = tab(context, harness_url)
    boot(a)
    b = tab(context, harness_url)
    before = b.evaluate("window.events")
    record(a)
    b.wait_for_function("(n) => window.events > n", arg=before, timeout=5000)
