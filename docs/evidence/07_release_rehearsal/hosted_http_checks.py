"""v2rc hosted HTTP checks (items: health, sign-in + roles, pages render, field snapshot). Prints no secrets.

usage: python hosted_http_checks.py BASE PWFILE OUTJSON
"""
import json
import re
import sys
import time
import uuid

import requests

BASE, PWFILE, OUT = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
PW = next(line.split(":", 1)[1].strip() for line in open(PWFILE) if line.startswith("password:"))
results: list[dict] = []


def rec(item, ok, detail):
    results.append({"item": item, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + item + ": " + detail, flush=True)


def login(user):
    s = requests.Session()
    r = s.get(BASE + "/login/", timeout=60)
    tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text).group(1)
    r = s.post(BASE + "/login/", data={"username": user, "password": PW, "csrfmiddlewaretoken": tok},
               headers={"Referer": BASE + "/login/"}, timeout=60)
    return s, r.url.replace(BASE, ""), ("sessionid" in s.cookies.get_dict())


def page(s, path):
    t0 = time.perf_counter()
    r = s.get(BASE + path, timeout=90)
    dt = time.perf_counter() - t0
    body = r.text
    title = re.search(r"<title>(.*?)</title>", body, re.S)
    main = re.search(r"<main[^>]*>(.*)</main>", body, re.S)
    text = re.sub(r"<[^>]+>", " ", main.group(1) if main else body)
    text = re.sub(r"\s+", " ", text).strip()
    err = ('class="msg error"' in body) or ("Server error" in body) or r.status_code >= 500
    return {"status": r.status_code, "s": round(dt, 2), "title": title.group(1).strip() if title else "",
            "chars": len(text), "error_notice": err, "text": text, "html": body, "url": r.url.replace(BASE, "")}


# 1 health
r = requests.get(BASE + "/healthz", timeout=30)
rec("1 health", r.status_code == 200 and r.json().get("status") == "ok", f"{r.status_code} {r.text.strip()}")

# 6 sign-in + role switching
ROLES = [("dana.ortiz", "ops_lead"), ("priya.raman", "admin"), ("kenji.watanabe", "engineering_lead"),
         ("elena.vasquez", "safety_reviewer"), ("sam.keller", "engineer"), ("marcus.bell", "founder"),
         ("lily.tran", "hub_technician OAK"), ("luis.romero", "field_operator SJC crew D lead"),
         ("ivan.petrov", "hub_technician + safety_pause SRF")]
sessions = {}
lines = []
okall = True
for u, role in ROLES:
    s, landed, has = login(u)
    me = s.get(BASE + "/api/auth/me", timeout=30)
    try:
        mj = me.json()
    except Exception:
        mj = {}
    got = mj.get("username") or (mj.get("user") or {}).get("username")
    roles = mj.get("roles") or (mj.get("user") or {}).get("roles")
    ok = has and got == u
    okall &= ok
    sessions[u] = s
    lines.append(f"{u}({role}) -> {landed} me={got} roles={roles}")
rec("6a sign-in, 9 accounts / 8 roles", okall, "; ".join(lines))

# role boundaries (switching shows different access)
dana, luis, sam, lily = sessions["dana.ortiz"], sessions["luis.romero"], sessions["sam.keller"], sessions["lily.tran"]
a1 = dana.get(BASE + "/api/audit-events", timeout=60).status_code
a2 = luis.get(BASE + "/api/audit-events", timeout=60).status_code
b1 = luis.get(BASE + "/planning/board/", timeout=60).status_code
h1 = lily.get(BASE + "/hub/", timeout=60).status_code
st_lily = page(lily, "/hub/stock/") if True else None
st_sam = page(sam, "/hub/stock/")
lily_hub = page(lily, "/hub/")
rec("6b role boundaries", a1 == 200 and a2 == 403,
    f"audit-events ops_lead {a1}, field_operator {a2}; board as field_operator {b1}; /hub/ as hub tech {h1}; "
    f"/hub/stock/ hub tech {st_lily['status']} (receive form {'receive' in st_lily['html'].lower()}), "
    f"engineer {st_sam['status']}")

# 7 Today + briefing (several roles), 8 board + review, 9 fleet/hub/readiness
def check_pages(item, sess, user, paths, must):
    out, ok = [], True
    for p in paths:
        pg = page(sess, p)
        good = pg["status"] == 200 and not pg["error_notice"] and pg["chars"] > 400
        for m in must.get(p, []):
            if m not in pg["text"]:
                good = False
                out.append(f"MISSING '{m}' on {p}")
        ok &= good
        out.append(f"{user} {p} {pg['status']} {pg['s']}s '{pg['title']}' {pg['chars']}ch err={pg['error_notice']}")
    rec(item, ok, "; ".join(out))
    return ok


check_pages("7 Today + Operations briefing", dana, "dana.ortiz", ["/ops/", "/ops/briefing/"],
            {"/ops/": ["OAK", "SJC", "SRF", "Needs your attention"], "/ops/briefing/": ["Oakland", "South Bay", "Marin"]})
check_pages("7b Today + briefing other roles", sessions["kenji.watanabe"], "kenji.watanabe",
            ["/ops/", "/ops/briefing/"], {})
check_pages("7c Today hub filter SJC", dana, "dana.ortiz", ["/ops/?hub=SJC", "/ops/briefing/?hub=SRF"], {})
board = page(dana, "/planning/board/")
m = re.search(r'href="(/planning/visits/[^"]*review=1[^"]*)"', board["html"])
rev_path = m.group(1).replace("&amp;", "&").split("#")[0] if m else None
ok8 = board["status"] == 200 and not board["error_notice"] and rev_path is not None
det = f"board {board['status']} {board['s']}s {board['chars']}ch 'not departure' banner={'not departure' in board['text']}"
if rev_path:
    rv = page(dana, rev_path)
    ok8 &= rv["status"] == 200 and not rv["error_notice"] and ("Review options" in rv["text"] and "Why this visit needs attention" in rv["text"])
    det += f"; review {rev_path} {rv['status']} {rv['s']}s 'Why this visit needs attention'={'Why this visit needs attention' in rv['text']} 'Options, best first'={'Options, best first' in rv['text']}"
rec("8 dispatch board + planning review", ok8, det)
check_pages("9 Fleet, Hub, Readiness", dana, "dana.ortiz", ["/fleet/", "/hub/", "/readiness/", "/hub/charging/", "/execution/"], {})
check_pages("9b Hub as hub technicians", lily, "lily.tran", ["/hub/"], {})

# 5 multi-hub demo
fleet = page(dana, "/fleet/")
hubs = {h: (h in fleet["text"]) for h in ("OAK", "SJC", "SRF")}
robots = len(set(re.findall(r"\bNB-\d{3}\b", fleet["text"])))
rec("5 synthetic multi-hub demo", all(hubs.values()) and "fictional" in (fleet["text"] + page(dana, "/ops/")["text"]).lower(),
    f"fleet lists hubs {hubs}, {robots} distinct NB- robot codes on page 1; synthetic/fictional label present")

# 10a field snapshot (crew D lead on the away run)
mine = page(luis, "/execution/mine/")
runs = sorted(set(re.findall(r"/execution/runs/(EX-\d+)/", mine["html"])))
run = runs[0] if runs else "EX-0003"
dev = str(uuid.uuid4())
t0 = time.perf_counter()
snap = luis.get(f"{BASE}/api/field/runs/{run}/snapshot", params={"device": dev}, timeout=60)
dt = time.perf_counter() - t0
sj = snap.json() if snap.headers.get("content-type", "").startswith("application/json") else {}
other = sessions["lily.tran"].get(f"{BASE}/api/field/runs/{run}/snapshot", params={"device": dev}, timeout=60)
rec("10a field snapshot", snap.status_code == 200 and other.status_code == 403,
    f"luis.romero /execution/mine/ runs {runs}; GET snapshot {run} {snap.status_code} {dt:.2f}s keys={sorted(sj)[:12]}; "
    f"another hub's technician (lily.tran OAK) {other.status_code}")

json.dump({"results": results, "run": run}, open(OUT, "w"), indent=1)
