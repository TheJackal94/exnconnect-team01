"""One data shape for the dashboard and Funnel Copilot.

    partner    {name, id, tier, next, link, permissions}
    campaigns  [{id, name, platform, clicks, reg, fund, act, reward, link, created}]
    clients    [{uid, country, campaign, stage: registered|funded|trading, days, lots, reward}]
    accounts   [{acc, uid, type, lots, reward, country}]
    subpartners[{id, name, clients, share, lots, reward, link}]
    rebates    [{acc, uid, type, rebate}]
    payments   [{date, acc, amount, status}]
    tiers      [{n, clients, vol}]
    perf       [{m, reward, lots, active}]
    byday      [{d, n}]

mock_dataset() is deterministic sample data. live_dataset(client) pulls the Partnership API and
adapts it. Field names the docs confirm are marked (doc). The rest are best guesses, collected in
FIELDS so they can be fixed in one place. Run `python funnel_copilot.py --inspect` against a live
key to dump the first row of every endpoint to data/live_samples.json and check them.
"""
from __future__ import annotations

import json
import random
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config import BRAND, DATA_DIR, SNAPSHOT as SNAPSHOT_FILE, env

PLATFORMS = ["TikTok", "YouTube", "Instagram", "Website", "Telegram", "WhatsApp", "X", "Facebook"]
META_FILE = DATA_DIR / "campaign_meta.json"  # platform per campaign_code, set when created in our UI

# ---------- field map: first key that exists wins ----------
FIELDS = {
    # envelope: {data: [...], totals: {...}} (doc)
    "rows": ["data", "results", "items"],
    # clients report (doc: reg_date, trade_fn, reward_usd, volume_lots)
    "client.uid": ["client_uid", "uid", "client_id", "id"],
    "client.country": ["country", "client_country", "country_code"],
    "client.campaign": ["campaign_code", "campaign", "campaign_title"],
    "client.link": ["link_code", "partner_code"],
    "client.reg_date": ["reg_date", "registration_date"],
    "client.trade_date": ["trade_fn", "first_trade_date"],
    "client.deposit": ["ftd_received", "ftd_date", "first_deposit_date", "is_deposited"],   # ftd_received: real (MCP 26 Nov)
    "client.traded": ["ftt_made"],                                                            # real; trade_fn = last-reward date
    "client.lots": ["volume_lots", "lots"],
    "client.reward": ["reward_usd", "reward"],
    # campaigns (doc: campaign_code, title, base_link, link_code, created, links[])
    "camp.id": ["campaign_code", "id"],
    "camp.name": ["title", "name"],
    "camp.link": ["base_link", "link"],
    "camp.created": ["created", "created_at"],
    # conversion report grouped by campaign (guesses)
    # IB performance grouped by campaign_title (real field names, MCP 26 Nov)
    "conv.key": ["campaign_title", "campaign_code", "campaign"],
    "conv.clicks": ["click_count", "clicks"],
    "conv.reg": ["registration_count", "registrations"],
    "conv.fund": ["first_deposit_count", "ftd"],
    "conv.act": ["trade_starting_real_count", "traded"],
    "conv.reward": ["reward_usd", "reward"],
    # client accounts (doc: client_account, client_account_type, partner_code)
    "acc.acc": ["client_account", "account"],
    "acc.uid": ["client_uid", "uid"],
    "acc.type": ["client_account_type", "account_type"],
    "acc.lots": ["volume_lots", "lots"],
    "acc.reward": ["reward_usd", "reward"],
    "acc.country": ["country", "client_country"],
    # referral agents (doc: id, alias, link_code, share_perc, clients, reward_usd)
    "ra.id": ["id", "uid"],
    "ra.name": ["alias", "name"],
    "ra.clients": ["clients"],
    "ra.share": ["share_perc"],
    "ra.reward": ["reward_usd", "agent_commission_usd"],
    "ra.link": ["link", "link_code"],
    # rebates
    "reb.acc": ["client_account", "account"],
    "reb.uid": ["client_uid", "uid"],
    "reb.type": ["client_account_type", "account_type"],
    "reb.pct": ["rebate_percent", "percent", "rebate"],
    "pay.date": ["created", "date", "payment_date"],
    "pay.acc": ["client_account", "account", "destination_account"],
    "pay.amount": ["amount_usd", "amount"],
    "pay.status": ["status"],
    # registrations by day
    "day.date": ["reg_date", "date", "day"],
    "day.n": ["amount", "count", "registrations"],
    # ib performance (doc: group_fields like [{"month": "..."}])
    "perf.reward": ["reward_usd", "reward"],
    "perf.lots": ["volume_lots", "lots"],
    "perf.active": ["trade_starting_real_count", "active_clients", "active"],
    # summary / tiers
    "sum.name": ["name", "partner_name", "alias"],
    "sum.id": ["uid", "partner_id", "id"],
    "sum.permissions": ["permissions", "scopes"],
    "tier.current": ["tier_name", "current_tier", "tier"],
    "tier.name": ["name", "tier_name", "tier"],
    "tier.clients": ["active_clients", "clients", "min_active_clients"],
    "tier.vol": ["volume_lots", "lots", "min_volume_lots", "volume"],
}


def pick(row: Any, field: str, default: Any = None) -> Any:
    if not isinstance(row, dict):
        return default
    for k in FIELDS.get(field, [field]):
        if k in row and row[k] is not None:
            return row[k]
    return default


def num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def rows(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    r = pick(payload, "rows", [])
    return r if isinstance(r, list) else []


def guess_platform(title: str) -> str:
    t = title.lower()
    for p in PLATFORMS:
        if t.startswith(p.lower()) or f"{p.lower()}:" in t or f"[{p.lower()}]" in t:
            return p
    for kw, p in (("reel", "Instagram"), ("tiktok", "TikTok"), ("youtube", "YouTube"), ("yt", "YouTube"),
                  ("landing", "Website"), ("site", "Website"), ("ig", "Instagram"), ("tg", "Telegram")):
        if re.search(rf"\b{kw}\b", t):
            return p
    return "Other"


def load_meta() -> dict:
    try:
        return json.loads(META_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_meta(code: str, platform: str) -> None:
    m = load_meta()
    m[code] = {"platform": platform}
    META_FILE.write_text(json.dumps(m, indent=2))


# ---------- mock ----------
SAMPLE_CAMPAIGNS = [
    # name, platform, clicks, click->reg, reg->fund, fund->trade
    ("Lot size calculator reel", "TikTok", 2400, .060, .45, .70),
    ("Order blocks explained", "TikTok", 5200, .009, .30, .55),
    ("Gold sessions cheat sheet", "TikTok", 1800, .040, .40, .65),
    ("Trading journal walkthrough", "YouTube", 600, .053, .55, .80),
    ("Free risk plan template", "YouTube", 450, .069, .50, .75),
    ("Strategy carousel", "Instagram", 120, .008, .20, .50),
    (f"{BRAND} landing page", "Website", 900, .035, .38, .62),
]


def mock_dataset() -> dict:
    r = random.Random(11)
    link = env("PARTNER_LINK", "https://one.exnesstrack.org/a/th208114")
    partner = {"name": f"{BRAND} Partners", "id": "PA-208114", "tier": "Gold", "next": "Platinum", "link": link,
               "permissions": ["campaigns", "autorebates", "reports", "referral_agents"]}
    camps = []
    for i, (name, plat, clicks, rr, fr, tr) in enumerate(SAMPLE_CAMPAIGNS):
        reg = round(clicks * rr); fund = round(reg * fr); act = round(fund * tr)
        cid = 4101 + i
        camps.append({"id": f"cmp_{cid}", "name": name, "platform": plat, "clicks": clicks, "reg": reg, "fund": fund,
                      "act": act, "reward": round(act * (8 + r.random() * 6), 2),
                      "link": f"{link}?campaign={cid}", "created": f"2026-{9 + i % 3:02d}-{10 + i:02d}"})
    countries = ["ZA", "ZA", "ZA", "ZA", "NG", "KE", "BW", "GH", "MY"]
    types = ["Standard", "Standard", "Pro", "Raw Spread", "Zero"]
    clients = []
    for c in camps:
        for i in range(c["reg"]):
            st = "trading" if i < c["act"] else "funded" if i < c["fund"] else "registered"
            lots = round(r.random() * 40, 2) if st == "trading" else 0.0
            clients.append({"uid": f"c{73000 + len(clients) * 7}", "country": r.choice(countries), "campaign": c["name"],
                            "stage": st, "days": 1 + int(r.random() * 40), "lots": lots,
                            "reward": round(lots * (1.4 + r.random()), 2)})
    accounts = [{"acc": str(51180000 + i * 13), "uid": c["uid"], "type": r.choice(types), "lots": c["lots"],
                 "reward": c["reward"], "country": c["country"]}
                for i, c in enumerate(x for x in clients if x["stage"] != "registered")]
    subs = [{"id": f"ral_{900 + i}", "name": n, "clients": cl, "share": sh, "lots": round(cl * (6 + r.random() * 9), 1),
             "reward": round(cl * (5 + r.random() * 8), 2), "link": f"{link}?ra={900 + i}"}
            for i, (n, cl, sh) in enumerate([("Kagiso M. (IB)", 42, 30), ("Tumi Trades", 18, 25),
                                              ("Durban FX Club", 27, 30), ("Lerato K.", 9, 20)])]  # share in percent, like share_perc
    rebates = [{"acc": a["acc"], "uid": a["uid"], "type": a["type"], "rebate": r.choice([0, 0, 10, 15, 20, 25])}
               for a in accounts[:14]]
    payments = [{"date": f"2026-11-{24 - i * 3:02d}", "acc": r.choice(rebates)["acc"],
                 "amount": round(4 + r.random() * 30, 2), "status": "pending" if i == 0 else "paid"} for i in range(8)]
    tiers = [{"n": "Silver", "clients": 10, "vol": 100}, {"n": "Gold", "clients": 50, "vol": 500},
             {"n": "Platinum", "clients": 150, "vol": 2000}, {"n": "Diamond", "clients": 400, "vol": 6000}]
    perf = [{"m": m, "reward": round(v + r.random() * 80, 2), "lots": round(v * .9 + r.random() * 60),
             "active": round(v / 11)} for m, v in zip(["Jun", "Jul", "Aug", "Sep", "Oct", "Nov"], [310, 420, 515, 690, 880, 1040])]
    byday = [{"d": f"11-{13 + i:02d}", "n": round(10 + i * 1.3 + r.random() * 12)} for i in range(14)]
    return {"mode": "mock", "synced": datetime.now().isoformat(timespec="minutes"), "partner": partner,
            "campaigns": camps, "clients": clients, "accounts": accounts, "subpartners": subs, "rebates": rebates,
            "payments": payments, "tiers": tiers, "perf": perf, "byday": byday, "warnings": []}


# ---------- live ----------
def _safe(fn, warnings: list, label: str, samples: dict | None = None):
    try:
        out = fn()
        if samples is not None:
            rs = rows(out)
            samples[label] = rs[0] if rs else out
        return out
    except Exception as e:  # one failing endpoint should not sink the whole sync
        warnings.append(f"{label}: {e}")
        return {}


def _days_since(d: Any) -> int:
    try:
        return max(0, (date.today() - date.fromisoformat(str(d)[:10])).days)
    except ValueError:
        return 0


def _stage(row: dict) -> str:
    traded = pick(row, "client.traded")
    if traded is True or (traded is None and num(pick(row, "client.lots")) > 0):
        return "trading"
    dep = pick(row, "client.deposit")
    if dep not in (None, False, 0, "0", "0.00", ""):
        return "funded"
    return "registered"


LINK_DOMAIN = "https://one.exnessonelink.com"


def _abs_link(l: str) -> str:
    l = str(l or "")
    return l if l.startswith("http") else (LINK_DOMAIN + l if l.startswith("/") else (f"{LINK_DOMAIN}/a/{l}" if l else ""))


def live_dataset(client, samples: dict | None = None) -> dict:
    W: list = []
    today = date.today()
    d14, d180 = today - timedelta(days=13), today - timedelta(days=180)
    s = lambda fn, label: _safe(fn, W, label, samples)

    summary = s(client.partner_summary, "partner/summary")
    links = rows(s(client.partner_links, "partner/links"))
    camps_raw = rows(s(client.campaigns, "campaigns"))
    perf_camp = rows(s(lambda: client.ib_performance("campaign_title", d180.isoformat(), today.isoformat()), "reports/ib/performance?group_by=campaign_title"))
    cl_raw = rows(s(lambda: client.clients_report(limit=1000), "reports/clients"))
    byday_raw = rows(s(lambda: client.registrations_byday(d14.isoformat(), today.isoformat()), "reports/clients/registrations/byday"))
    acc_raw = rows(s(lambda: client.client_accounts(limit=1000), "reports/clients/accounts"))
    ra_raw = rows(s(client.referral_agent_links, "referral-agent-links"))
    reb_raw = rows(s(client.rebate_clients, "autorebates/clients"))
    pay_raw = rows(s(client.rebate_payments, "autorebates/payments"))
    tiers_raw = s(client.tier_conditions, "partner-tier/tier-conditions")
    state = s(client.tier_state, "partner-tier/partner-state")
    perf_raw = rows(s(lambda: client.ib_performance("month", d180.isoformat(), today.isoformat()), "reports/ib/performance?group_by=month"))
    wallet = getattr(client, "exd_wallet", lambda: None)()

    default = next((l for l in links if l.get("is_default")), links[0] if links else {})
    link = env("PARTNER_LINK", "") or _abs_link(default.get("url") or (summary.get("default_link") if isinstance(summary, dict) else ""))
    tier_list = rows(tiers_raw) or (tiers_raw.get("tiers", []) if isinstance(tiers_raw, dict) else [])
    tiers = [{"n": str(pick(t, "tier.name", "?")), "clients": int(num(pick(t, "tier.clients"))), "vol": num(pick(t, "tier.vol"))}
             for t in tier_list if isinstance(t, dict)]
    tc = (summary.get("tier_current") if isinstance(summary, dict) else None) or (state.get("tier_current") if isinstance(state, dict) else None) or state
    cur = str(pick(tc, "tier.current", "") or "")
    names = [t["n"] for t in tiers]
    nxt = names[names.index(cur) + 1] if cur in names and names.index(cur) + 1 < len(names) else ""
    perms = pick(summary, "sum.permissions", {})
    perms = [k for k, v in perms.items() if v] if isinstance(perms, dict) else perms if isinstance(perms, list) else []
    partner = {"name": str(pick(summary, "sum.name") or f"{BRAND} Partners"), "id": str(pick(summary, "sum.id", ""))[:8],
               "tier": cur or "—", "next": nxt, "link": link, "permissions": perms,
               "wallet_account": next((a.get("account") for a in (summary.get("accounts") or []) if a.get("account_type") == "partner"), "") if isinstance(summary, dict) else ""}

    # EXD wallet: the REST API has no EXD wallet endpoint (only the Exness connector reads it), so live mode
    # falls back to the last captured value in the snapshot, dated.
    if wallet is None and SNAPSHOT_FILE.exists():
        try:
            snap = json.loads(SNAPSHOT_FILE.read_text())
            if snap.get("exd_wallet"):
                wallet = {**snap["exd_wallet"], "as_of": str(snap.get("captured", ""))[:10]}
        except (OSError, ValueError):
            pass
    # USD partner wallet = the partner account that receives rewards. Live balance via the Payments API
    # (needs a key with payments read scope); the summary's reward total comes with partnership read.
    sr = summary.get("summary_reward") if isinstance(summary, dict) else None
    pw = {"account": partner["wallet_account"], "balance": None, "currency": "USD",
          "reward_total": num(sr.get("amount")) if isinstance(sr, dict) else None,
          "reward_currency": (sr.get("currency") if isinstance(sr, dict) else None) or "USD", "live": False}
    if pw["account"] and hasattr(client, "balance"):
        b = s(lambda: client.balance(pw["account"]), "accounts/{partner account}/balance")
        if isinstance(b, dict) and b.get("balance") is not None:
            pw.update(balance=num(b["balance"]), currency=b.get("currency") or "USD", live=True)

    meta = load_meta()
    perf_by = {}
    for r in perf_camp:
        k = next((g.get("campaign_title") for g in r.get("group_fields", []) if isinstance(g, dict) and "campaign_title" in g), pick(r, "conv.key", ""))
        perf_by[str(k)] = r
    link_to_camp, campaigns = {}, []
    for c in camps_raw:
        code = str(pick(c, "camp.id", "")); name = str(pick(c, "camp.name", code))
        for l in c.get("links", []) or []:
            if isinstance(l, dict) and l.get("link_code"):
                link_to_camp[l["link_code"]] = name
        if c.get("link_code"):
            link_to_camp[c["link_code"]] = name
        cv = perf_by.get(name) or perf_by.get(code) or {}
        campaigns.append({"id": code, "name": name, "platform": meta.get(code, {}).get("platform") or guess_platform(name),
                          "clicks": int(num(pick(cv, "conv.clicks"))), "reg": int(num(pick(cv, "conv.reg"))),
                          "fund": int(num(pick(cv, "conv.fund"))), "act": int(num(pick(cv, "conv.act"))),
                          "reward": num(pick(cv, "conv.reward")), "link": _abs_link(pick(c, "camp.link", "")),
                          "created": str(pick(c, "camp.created", ""))[:10]})

    uid_code = {str(a.get("client_uid")): a.get("partner_code") for a in acc_raw if a.get("partner_code")}
    clients = []
    for r in cl_raw:
        uid = str(pick(r, "client.uid", "?"))
        camp = link_to_camp.get(str(uid_code.get(uid, "")), "Default link")
        clients.append({"uid": uid, "country": str(pick(r, "client.country", "—")), "campaign": camp, "stage": _stage(r),
                        "days": _days_since(pick(r, "client.reg_date", "")), "lots": num(pick(r, "client.lots")),
                        "reward": num(pick(r, "client.reward")), "status": r.get("client_status", "")})

    accounts = [{"acc": str(pick(a, "acc.acc", "—")), "uid": str(pick(a, "acc.uid", "")), "type": str(pick(a, "acc.type") or "Unknown"),
                 "lots": num(pick(a, "acc.lots")), "reward": num(pick(a, "acc.reward")), "country": str(pick(a, "acc.country") or "—"),
                 "last_trade": a.get("client_account_last_trade")} for a in acc_raw]
    subs = [{"id": str(pick(a, "ra.id", "")), "name": str(pick(a, "ra.name", "Agent")), "clients": int(num(pick(a, "ra.clients"))),
             "share": num(pick(a, "ra.share")), "lots": 0.0, "reward": num(pick(a, "ra.reward")), "link": _abs_link(pick(a, "ra.link", ""))}
            for a in ra_raw]
    rebates = [{"acc": str(pick(x, "reb.acc", "")), "uid": str(pick(x, "reb.uid", "")), "type": str(pick(x, "reb.type") or ""),
                "rebate": num(pick(x, "reb.pct"))} for x in reb_raw]
    payments = [{"date": str(pick(p, "pay.date", ""))[:10], "acc": str(pick(p, "pay.acc", "")), "amount": num(pick(p, "pay.amount")),
                 "status": str(pick(p, "pay.status", "")).lower()} for p in pay_raw]

    perf = []
    for p in perf_raw:
        m = next((g.get("month") for g in p.get("group_fields", []) or [] if isinstance(g, dict) and "month" in g), p.get("month", ""))
        try:
            m = datetime.fromisoformat(str(m)[:10] if len(str(m)) >= 10 else f"{m}-01").strftime("%b")
        except ValueError:
            pass
        perf.append({"m": str(m), "reward": num(pick(p, "perf.reward")), "lots": num(pick(p, "perf.lots")), "active": int(num(pick(p, "perf.active")))})
    byday = [{"d": str(pick(b, "day.date", ""))[5:10], "n": int(num(pick(b, "day.n")))} for b in byday_raw]

    ds = {"mode": "live", "synced": datetime.now().isoformat(timespec="minutes"), "partner": partner,
          "campaigns": campaigns, "clients": clients, "accounts": accounts, "subpartners": subs, "rebates": rebates,
          "payments": payments, "tiers": tiers, "perf": perf, "byday": byday, "warnings": W,
          "exd_wallet": wallet, "partner_wallet": pw, "funnel_sample": False}
    # A partner with no tracked campaign links has no content funnel yet. For the demo, show clearly-labelled
    # sample campaigns in the Copilot funnel only (DEMO_FUNNEL=0 turns this off). Real clients stay real.
    if not any(c["clicks"] or c["reg"] for c in campaigns) and env("DEMO_FUNNEL", "1") != "0":
        ds["campaigns"] = [dict(c, sample=True) for c in mock_dataset()["campaigns"]] + campaigns
        ds["funnel_sample"] = True
    return ds


class SnapshotClient:
    """Replays data/snapshot.json (captured read-only through the Exness MCP connector) with the client's interface."""

    def __init__(self, path):
        self.snap = json.loads(Path(path).read_text())
        self.captured = self.snap.get("captured", "")

    def _g(self, key):
        if key not in self.snap:
            raise KeyError(f"not in snapshot")
        return self.snap[key]

    def partner_summary(self): return {**self._g("partner/summary"), "tier_current": self.snap.get("tier_current")}
    def partner_links(self): return self._g("partner/links")
    def campaigns(self): return self._g("campaigns")
    def ib_performance(self, group_by="month", *a, **k): return self._g(f"reports/ib/performance?group_by={group_by}") if f"reports/ib/performance?group_by={group_by}" in self.snap else {"data": []}
    def clients_report(self, **k): return self._g("reports/clients")
    def registrations_byday(self, *a): return self._g("reports/clients/registrations/byday")
    def client_accounts(self, **k): return self._g("reports/clients/accounts")
    def referral_agent_links(self): return self._g("referral-agent-links")
    def rebate_clients(self): return self._g("autorebates/clients")
    def rebate_payments(self): return self._g("autorebates/payments")
    def tier_conditions(self): return {"data": []}
    def tier_state(self): return {}
    def exd_wallet(self): return self.snap.get("exd_wallet")
    def orders(self, accounts, date_from=None, date_to=None, limit=200):
        return {"data": [o for o in self._g("reports/orders")["data"] if o.get("client_account") in accounts][:limit]}
