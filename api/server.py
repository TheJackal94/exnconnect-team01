"""Trade Hour API: signs Exness calls server-side, caches reports, serves JSON and all three pages.

    python api/server.py     # http://127.0.0.1:8787   partner dashboard /   landing /landing/   client dashboard /client/

Data source (engine/config.py data_mode): live (own EXN_API_KEY) > snapshot (data/snapshot.json, real data captured
through the Exness connector) > mock. Standard library only, plus the engine's cryptography/requests/anthropic.

Auth: partner endpoints need X-Dash-Token when DASH_TOKEN is set. /api/public/* is open (landing page, rate-limited
chat). /api/client/* needs a client session from /api/client/login.
"""
from __future__ import annotations

import json
import mimetypes
import random
import secrets
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from config import BRAND, DATA_DIR, data_mode, env  # noqa: E402
import dataset  # noqa: E402
import faq  # noqa: E402
import funnel_copilot as fc  # noqa: E402
import rewards  # noqa: E402

CACHE_TTL = int(env("CACHE_TTL", "600"))
LOCK = threading.Lock()
STATE: dict = {"ds": None, "at": 0.0, "analysis": None, "brief": None, "brief_source": None}
CLAIMS_FILE = DATA_DIR / "exd_claims.json"
APPROVALS_FILE = DATA_DIR / "approvals.jsonl"
SETTINGS_FILE = DATA_DIR / "settings.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
JOURNAL_DIR = DATA_DIR / "journal"
JOURNAL_DIR.mkdir(exist_ok=True)
SESSION_DAYS = 7
PAGES = {"/": "partners/index.html", "/partners/": "partners/index.html", "/landing/": "landing/index.html",
         "/landing": "landing/index.html", "/client/": "client/index.html", "/client": "client/index.html"}
RATE: dict[str, deque] = defaultdict(deque)


# ---------- helpers ----------
def mode() -> str:
    return data_mode()


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def write_json(p: Path, data) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)


def settings() -> dict:
    return read_json(SETTINGS_FILE, {})


def partner_link() -> str:
    ds = STATE["ds"]
    return (ds and ds["partner"].get("link")) or env("PARTNER_LINK", "https://one.exnessonelink.com/a/ijfv29rud7")


def get_ds(force: bool = False) -> dict:
    with LOCK:
        if force or STATE["ds"] is None or time.time() - STATE["at"] > CACHE_TTL:
            ds = fc.load()
            if mode() == "mock":
                ds["exd_wallet"] = {"current_balance": "300.00", "currency": "EXD", "service_enabled": True, "sample": True}
            rewards.sync_milestones(ds)
            STATE.update(ds=ds, at=time.time(), analysis=fc.analyse(ds))
            if force or STATE["brief"] is None:
                STATE["brief"], STATE["brief_source"] = fc.ai_brief(STATE["analysis"])
        return STATE["ds"]


def client():
    return fc.make_client()


def chat_id(kind: str = "brief") -> str:
    s = settings()
    return s.get("tg_client_chat" if kind == "client" else "tg_chat") or env("TG_CLIENT_CHAT_ID" if kind == "client" else "TG_CHAT_ID")


def rate_ok(key: str, limit: int, per: int = 60) -> bool:
    q, t = RATE[key], time.time()
    while q and q[0] < t - per:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(t)
    return True


class ApiError(Exception):
    def __init__(self, status: int, msg: str, **extra):
        super().__init__(msg)
        self.status, self.extra = status, extra


# ---------- partner: data + Copilot ----------
def h_status(_):
    ds = get_ds()
    return {"mode": mode(), "brand": BRAND, "synced": ds["synced"], "warnings": ds.get("warnings", []),
            "claude": bool(env("ANTHROPIC_API_KEY")), "model": env("CLAUDE_MODEL", "claude-sonnet-5-5"),
            "telegram": bool(env("TG_BOT_TOKEN") and chat_id()), "telegram_clients": bool(env("TG_BOT_TOKEN") and chat_id("client")),
            "exd": {"signup_amount": env("EXD_REWARD_AMOUNT"), "wallet": ds.get("exd_wallet"), "milestones": rewards.milestones()},
            "api_key_hint": (env("EXN_API_KEY")[:10] + "…") if env("EXN_API_KEY") else "", "settings": settings()}


def h_dashboard(q):
    ds = get_ds(force=q.get("refresh") == "1")
    a = STATE["analysis"]
    return {**ds, "mode": mode(), "brief": STATE["brief"], "brief_source": STATE["brief_source"], "nudge": fc.NUDGES[0],
            "leak": a["biggest_leak"], "stalled_count": len(a["stalled"]), "idle_count": len(a["funded_idle"]),
            "claims": read_json(CLAIMS_FILE, []), "rewards": rewards.load(), "milestones": rewards.milestones(),
            "signup_amount": env("EXD_REWARD_AMOUNT")}


def h_sync(_):
    get_ds(force=True)
    return h_dashboard({})


def h_create_campaign(body):
    title = (body.get("title") or "").strip()[:120]
    platform = body.get("platform") or "Other"
    if not title:
        raise ApiError(400, "title is required")
    ds = get_ds()
    if mode() == "live":
        res = client().create_campaign(title if title.lower().startswith(platform.lower()) else f"{platform}: {title}")
        code = str(res.get("campaign_code", ""))
        dataset.save_meta(code, platform)
        camp = {"id": code, "name": res.get("title", title), "platform": platform, "clicks": 0, "reg": 0, "fund": 0, "act": 0, "reward": 0,
                "link": res.get("base_link") or (res.get("links") or [{}])[0].get("link", ""), "created": str(res.get("created", ""))[:10]}
    else:
        n = 4101 + len(ds["campaigns"])
        camp = {"id": f"cmp_{n}", "name": title, "platform": platform, "clicks": 0, "reg": 0, "fund": 0, "act": 0, "reward": 0,
                "link": f"{partner_link()}?campaign={n}", "created": date.today().isoformat(), "sample": mode() != "live"}
    ds["campaigns"].insert(0, camp)
    STATE["analysis"] = fc.analyse(ds)
    return {"campaign": camp, "mode": mode()}


def h_rebate(body):
    acc, pct = str(body.get("account", "")), body.get("percent")
    if not acc or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
        raise ApiError(400, "account and percent (0-100) are required")
    if mode() == "live":
        client().set_client_rebate(acc, pct)
    for r in get_ds()["rebates"]:
        if r["acc"] == acc:
            r["rebate"] = pct
    return {"ok": True, "account": acc, "percent": pct, "mode": mode()}


def h_send_brief(body):
    return {"telegram": fc.send_telegram(body.get("text") or STATE["brief"] or "", chat_id())}


def h_rewrite(body):
    text, src = fc.rewrite_nudge(body.get("previous", ""), body.get("audience", "stalled"))
    return {"text": text, "source": src, "issues": fc.compliance_check(text)}


def h_approve(body):
    text = (body.get("text") or "").strip()
    issues = fc.compliance_check(text)
    if issues:
        raise ApiError(422, "Not sent: " + "; ".join(issues))
    a = STATE["analysis"] or fc.analyse(get_ds())
    audience = body.get("audience", "stalled")
    record = {"id": str(uuid.uuid4()), "at": datetime.now().isoformat(timespec="seconds"), "audience": audience,
              "recipients": len(a["stalled"] if audience == "stalled" else a["funded_idle"]), "text": text}
    # The Exness API is not permission to contact clients: approved lessons go to the partner's own opted-in channel.
    record["telegram"] = fc.send_telegram(text, chat_id("client")) if chat_id("client") else {"ok": False, "skipped": True, "reason": "no client channel set"}
    with APPROVALS_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def h_settings(body):
    s = settings()
    for k in ("tg_chat", "tg_client_chat"):
        if k in body:
            s[k] = str(body[k]).strip()[:100]
    write_json(SETTINGS_FILE, s)
    return {"ok": True, "settings": s}


# ---------- EXD rewards (sign-up + milestones) ----------
def _affiliation(email: str) -> dict:
    """{affiliated, accounts, uid}. Live: POST /affiliation. Snapshot/mock: simulated (no email lookup without our own key)."""
    if mode() == "live":
        res = client().check_affiliation(email)
        aff = res.get("affiliated", res.get("is_affiliated", res.get("result")))
        accs = res.get("client_accounts") or res.get("accounts") or []
        return {"affiliated": bool(aff), "accounts": [str(a.get("client_account", a) if isinstance(a, dict) else a) for a in accs],
                "uid": res.get("client_uid") or res.get("uid") or "", "raw": res}
    ds = get_ds()
    rnd = random.Random(email)
    if rnd.random() < .2 or not ds["accounts"]:
        return {"affiliated": False, "accounts": [], "uid": ""}
    a = rnd.choice(ds["accounts"])
    return {"affiliated": True, "accounts": [a["acc"]], "uid": a["uid"], "simulated": True}


def _claim(claims, cid):
    for c in claims:
        if c["id"] == cid:
            return c
    raise ApiError(404, "claim not found")


def h_exd_check(body):
    claims = read_json(CLAIMS_FILE, [])
    c = _claim(claims, body.get("id"))
    res = _affiliation(c["email"])
    c.update(affiliated=res["affiliated"], accounts=res["accounts"], uid=res["uid"], checked=datetime.now().isoformat(timespec="seconds"),
             status="ready" if res["affiliated"] and res["accounts"] else ("affiliated_no_account" if res["affiliated"] else "not_affiliated"))
    write_json(CLAIMS_FILE, claims)
    reward = rewards.add_signup_reward(c) if c["status"] == "ready" else None
    if c["status"] == "ready" and not reward:
        c["note"] = "Set EXD_REWARD_AMOUNT to create the sign-up reward"
    return {"claim": c, "reward": reward, "rewards": rewards.load()}


def h_exd_link(body):
    """Build one prefilled Partner Area EXD transfer link for rewards that share an amount."""
    ids = body.get("ids") or []
    rows = [r for r in rewards.load() if r["id"] in ids and r["status"] != "paid"]
    if not rows:
        raise ApiError(400, "pick at least one unpaid reward")
    amounts = {r["amount"] for r in rows}
    if len(amounts) > 1:
        raise ApiError(400, "the Partner Area sends one amount per transfer: select rewards with the same EXD amount")
    accs = sorted({r["accounts"][0] for r in rows if r.get("accounts")})
    if not accs:
        raise ApiError(400, "these rewards have no client account number yet")
    url = rewards.exd_transfer_link(accs, amounts.pop())
    rewards.mark([r["id"] for r in rows], "in_partner_area")
    return {"url": url, "count": len(accs), "rewards": rewards.load()}


def h_exd_mark_paid(body):
    rewards.mark(body.get("ids") or [], "paid")
    return {"rewards": rewards.load()}


# ---------- public: landing page ----------
def h_public_config(_):
    m = mode()
    demo = None
    if m != "live":
        ds = get_ds()
        funded = [a for a in ds["accounts"] if next((c for c in ds["clients"] if c["uid"] == a["uid"] and c["stage"] != "registered"), None)]
        demo = funded[0]["acc"] if funded else None
    return {"brand": BRAND, "partner_link": partner_link(), "exd_amount": env("EXD_REWARD_AMOUNT"), "exd_terms": env("EXD_TERMS"),
            "milestones": rewards.milestones(), "demo_account": demo, "mode": m}


def h_public_claim(body):
    email = (body.get("email") or "").strip().lower()
    if "@" not in email or not body.get("consent"):
        raise ApiError(400, "email and risk acknowledgement are required")
    claims = read_json(CLAIMS_FILE, [])
    if not any(c["email"] == email for c in claims):
        claims.append({"id": uuid.uuid4().hex[:12], "name": (body.get("name") or "").strip()[:80], "email": email,
                       "country": (body.get("country") or "")[:40], "campaign": (body.get("campaign") or "")[:60],
                       "path": body.get("path") if body.get("path") in ("new", "switch") else "new",
                       "created": datetime.now().isoformat(timespec="seconds"), "status": "waiting_signup"})
        write_json(CLAIMS_FILE, claims)
    return {"ok": True, "partner_link": partner_link()}


def h_public_faq(_):
    return {"faq": [{"q": x["q"], "a": faq.fallback_answer(x["q"])} for x in faq.kb()], "switch_steps": faq.switch_steps(),
            "partner_link": partner_link()}


def h_public_chat(body, ip=""):
    if not rate_ok(f"chat:{ip}", 20):
        raise ApiError(429, "Too many messages, try again in a minute")
    msgs = [m for m in (body.get("messages") or [])[-12:] if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    msgs = [{"role": m["role"], "content": str(m.get("content", ""))[:1200]} for m in msgs if str(m.get("content", "")).strip()]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs or msgs[-1]["role"] != "user":
        raise ApiError(400, "send a user message")
    reply = fc.claude(faq.system_prompt(), "", max_tokens=800, messages=msgs, effort="low")
    if reply:
        return {"reply": reply, "source": "claude"}
    return {"reply": faq.fallback_answer(msgs[-1]["content"]), "source": "faq"}


# ---------- client portal ----------
def _sessions() -> dict:
    s = read_json(SESSIONS_FILE, {})
    cutoff = (datetime.now() - timedelta(days=SESSION_DAYS)).isoformat()
    return {k: v for k, v in s.items() if v.get("created", "") > cutoff}


def h_client_login(body, ip=""):
    if not rate_ok(f"login:{ip}", 10):
        raise ApiError(429, "Too many attempts, try again in a minute")
    email = (body.get("email") or "").strip().lower()
    account = "".join(ch for ch in str(body.get("account") or "") if ch.isdigit())
    if "@" not in email or not account:
        raise ApiError(400, "Enter your email and an Exness account number")
    ds = get_ds()
    if mode() == "live":
        aff = _affiliation(email)
        if not aff["affiliated"]:
            raise ApiError(403, "not_affiliated", reason="not_affiliated")
        if account not in aff["accounts"]:
            raise ApiError(403, "That account number isn't linked to this email under Trade Hour", reason="account_mismatch")
        uid = aff["uid"] or next((a["uid"] for a in ds["accounts"] if a["acc"] == account), "")
    else:
        # Snapshot/mock: no email lookup without our own API key, so the account number identifies the client (demo only).
        uid = next((a["uid"] for a in ds["accounts"] if a["acc"] == account), "")
        if not uid:
            raise ApiError(403, "not_affiliated", reason="not_affiliated")
    c = next((x for x in ds["clients"] if x["uid"] == uid), None)
    if c is None or c["stage"] == "registered":
        raise ApiError(403, "not_funded", reason="not_funded")
    token = secrets.token_urlsafe(24)
    s = _sessions()
    s[token] = {"uid": uid, "email": email, "accounts": [a["acc"] for a in ds["accounts"] if a["uid"] == uid] or [account],
                "created": datetime.now().isoformat(timespec="seconds"), "demo": mode() != "live"}
    write_json(SESSIONS_FILE, s)
    return {"token": token}


def _session(headers) -> dict:
    tok = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    s = _sessions().get(tok)
    if not s:
        raise ApiError(401, "Please log in again")
    return s


def _journal(uid: str) -> dict:
    return read_json(JOURNAL_DIR / f"{uid}.json", {"notes": {}, "manual": []})


def _trades(accounts: list[str]) -> list[dict]:
    m = mode()
    try:
        if m == "mock":
            rows = rewards.mock_orders(accounts)
        else:
            today = date.today()
            rows = dataset.rows(client().orders(accounts, (today - timedelta(days=31)).isoformat(), today.isoformat(), 300))
    except Exception as e:  # journal still works with manual entries
        sys.stderr.write(f"orders: {e}\n")
        rows = []
    return [{"id": str(o.get("order_id")), "symbol": o.get("symbol", ""), "lots": dataset.num(o.get("volume_lots")),
             "open": o.get("open_date"), "close": o.get("close_date"), "account": o.get("client_account"), "auto": True} for o in rows]


def h_client_me(_, headers):
    s = _session(headers)
    ds = get_ds()
    c = next((x for x in ds["clients"] if x["uid"] == s["uid"]), {})
    lots, accs = rewards.client_lots(ds, s["uid"])
    j = _journal(s["uid"])
    trades = _trades(accs or s["accounts"]) + j["manual"]
    for t in trades:
        t.update(j["notes"].get(t["id"], {}))
    trades.sort(key=lambda t: t.get("close") or "", reverse=True)
    week = (datetime.utcnow() - timedelta(days=7)).isoformat()
    wk = [t for t in trades if (t.get("close") or "") >= week]
    logged = [t for t in trades if "followed" in t]
    mine = [r for r in rewards.load() if r["uid"] == s["uid"]]
    return {"brand": BRAND, "email": s["email"], "demo": s.get("demo"), "mode": mode(), "uid": s["uid"][:8],
            "country": c.get("country"), "stage": c.get("stage"), "accounts": accs or s["accounts"], "lots": lots,
            "milestones": rewards.progress(lots, s["uid"]), "rewards": mine,
            "exd_earned": sum(r["amount"] for r in mine), "exd_paid": sum(r["amount"] for r in mine if r["status"] == "paid"),
            "trades": trades[:150], "stats": {"week_trades": len(wk), "week_lots": round(sum(t["lots"] for t in wk), 2),
                                              "journaled": len(logged), "rules_pct": round(100 * sum(1 for t in logged if t["followed"]) / len(logged)) if logged else None,
                                              "wins": sum(1 for t in logged if t.get("result") == "win"), "losses": sum(1 for t in logged if t.get("result") == "loss")},
            "sync_note": ("Closed trades on your Exness accounts with Trade Hour (last 31 days) are loaded from our partner reports when you open this page."
                          if mode() == "live" else "These are sample trades for the demo.")
                         + " Exness doesn't share profit or loss, so add your P/L to each trade to see your metrics."}


def h_client_journal(body, headers):
    s = _session(headers)
    j = _journal(s["uid"])
    entry = {k: body[k] for k in ("setup", "note", "result") if isinstance(body.get(k), str)}
    for k in ("setup", "note"):
        if k in entry:
            entry[k] = entry[k][:500]
    if entry.get("result") not in (None, "win", "loss", "be"):
        entry.pop("result")
    if isinstance(body.get("risk"), (int, float)) and 0 <= body["risk"] <= 100:
        entry["risk"] = body["risk"]
    if isinstance(body.get("pl"), (int, float)) and not isinstance(body.get("pl"), bool) and abs(body["pl"]) <= 1e7:
        entry["pl"] = round(float(body["pl"]), 2)
    if isinstance(body.get("followed"), bool):
        entry["followed"] = body["followed"]
    tid = str(body.get("id") or "")
    if tid and not tid.startswith("m-"):
        j["notes"][tid] = {**j["notes"].get(tid, {}), **entry}
    else:
        sym = str(body.get("symbol") or "").upper()[:12]
        if not tid and not sym:
            raise ApiError(400, "symbol is required for a manual trade")
        existing = next((m for m in j["manual"] if m["id"] == tid), None)
        if existing:
            existing.update(entry)
        else:
            lots = body.get("lots") if isinstance(body.get("lots"), (int, float)) else 0
            now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            j["manual"].append({"id": f"m-{uuid.uuid4().hex[:8]}", "symbol": sym, "lots": lots, "open": now, "close": now, "auto": False, **entry})
    write_json(JOURNAL_DIR / f"{s['uid']}.json", j)
    return {"ok": True}


ROUTES = {
    ("GET", "/api/status"): h_status,
    ("GET", "/api/dashboard"): h_dashboard,
    ("POST", "/api/sync"): h_sync,
    ("POST", "/api/campaigns"): h_create_campaign,
    ("POST", "/api/rebates"): h_rebate,
    ("POST", "/api/brief/send"): h_send_brief,
    ("POST", "/api/nudge/rewrite"): h_rewrite,
    ("POST", "/api/nudge/approve"): h_approve,
    ("POST", "/api/settings"): h_settings,
    ("POST", "/api/exd/check"): h_exd_check,
    ("POST", "/api/exd/link"): h_exd_link,
    ("POST", "/api/exd/mark-paid"): h_exd_mark_paid,
    ("GET", "/api/public/config"): h_public_config,
    ("GET", "/api/public/faq"): h_public_faq,
    ("POST", "/api/public/claim"): h_public_claim,
    ("POST", "/api/public/chat"): h_public_chat,
    ("POST", "/api/client/login"): h_client_login,
    ("GET", "/api/client/me"): h_client_me,
    ("POST", "/api/client/journal"): h_client_journal,
}
NEEDS_IP = {h_public_chat, h_client_login}
NEEDS_HEADERS = {h_client_me, h_client_journal}


class Handler(BaseHTTPRequestHandler):
    server_version = "TradeHour/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{datetime.now():%H:%M:%S} {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        origin = env("ALLOWED_ORIGIN")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Dash-Token, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def _handle(self, method):
        u = urlparse(self.path)
        path = u.path
        if method == "GET" and not path.startswith("/api/"):
            return self._static(path)
        fn = ROUTES.get((method, path))
        if not fn:
            return self._send(404, {"error": "not found"})
        token = env("DASH_TOKEN")
        open_path = path.startswith("/api/public/") or path.startswith("/api/client/")
        if token and not open_path and self.headers.get("X-Dash-Token") != token:
            return self._send(401, {"error": "dashboard token required"})
        try:
            if method == "GET":
                arg = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
            else:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 64_000:
                    raise ApiError(413, "body too large")
                arg = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(arg, dict):
                    raise ApiError(400, "JSON object expected")
            if fn in NEEDS_IP:
                out = fn(arg, self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip())
            elif fn in NEEDS_HEADERS:
                out = fn(arg, self.headers)
            else:
                out = fn(arg)
            self._send(200, out)
        except ApiError as e:
            self._send(e.status, {"error": str(e), **e.extra})
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
        except Exception as e:  # surface Exness errors (auth 1000/1001/1002 etc.)
            status = getattr(e, "status", 500)
            self._send(status if isinstance(status, int) and 400 <= status < 600 else 500, {"error": str(e)[:500]})

    def _static(self, path):
        rel = PAGES.get(path)
        f = (ROOT / rel) if rel else None
        if f is None:
            cand = (ROOT / path.lstrip("/")).resolve()
            if cand.is_file() and cand.is_relative_to(ROOT) and cand.parent.name in ("partners", "landing", "client", "assets"):
                f = cand
        if not f or not f.exists():
            return self._send(404, b"not found", "text/plain")
        self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "text/html")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    host, port = env("HOST", "127.0.0.1"), int(env("PORT", "8787"))
    print(f"{BRAND} API · {mode()} data · http://{host}:{port}   landing: /landing/   client: /client/")
    get_ds()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
