"""EXD rewards: sign-up rewards and lot milestones, paid by the partner through the Exness Partner Area.

How payment works (confirmed via the Exness connector, 26 Nov): EXD is sent from the partner's EXD wallet in the
Partner Area. There is no API call that moves EXD; we build a prefilled Partner Area link (recipients + amount)
and the partner completes the transfer there, then marks the reward paid here.

Milestones: MILESTONES="1:2,10:5,50:10,100:20,250:40"  (lots traded : EXD). Lots = lifetime first-level volume
of the client's accounts with this partner (Client accounts report, volume_lots).
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from config import DATA_DIR, env

REWARDS_FILE = DATA_DIR / "rewards.json"
EXD_TRANSFER_URL = "https://my.exnessaffiliates.com/payments/exd-transfer"


def milestones() -> list[dict]:
    out = []
    for part in env("MILESTONES", "1:2,10:5,50:10,100:20,250:40").split(","):
        try:
            lots, exd = part.split(":")
            out.append({"lots": float(lots), "exd": float(exd)})
        except ValueError:
            continue
    return sorted(out, key=lambda m: m["lots"])


def exd_transfer_link(accounts: list[str], amount: float | str) -> str:
    """Same format the Exness connector's partner_build_exd_transfer_link returns."""
    amt = f"{float(amount):g}"
    recipients = ",".join(a for a in accounts if a.isdigit())  # literal commas, as the connector builds it
    return f"{EXD_TRANSFER_URL}?" + urlencode({"tab": "myClients", "defaultMode": "byClientAccount"}) + \
        f"&defaultRecipients={recipients}&defaultAmount={amt}"


def load() -> list[dict]:
    try:
        return json.loads(REWARDS_FILE.read_text())
    except (OSError, ValueError):
        return []


def save(rows: list[dict]) -> None:
    tmp = REWARDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    tmp.replace(REWARDS_FILE)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def client_lots(ds: dict, uid: str) -> tuple[float, list[str]]:
    accs = [a for a in ds["accounts"] if a["uid"] == uid]
    if accs:
        return round(sum(a["lots"] for a in accs), 2), [a["acc"] for a in accs]
    c = next((c for c in ds["clients"] if c["uid"] == uid), None)
    return (c["lots"] if c else 0.0), []


def sync_milestones(ds: dict) -> list[dict]:
    """Create a 'due' reward for every milestone each funded client has reached. Idempotent."""
    rows = load()
    have = {r["id"] for r in rows}
    ms = milestones()
    for c in ds["clients"]:
        if c["stage"] == "registered":
            continue
        lots, accs = client_lots(ds, c["uid"])
        for m in ms:
            rid = f"ms-{c['uid']}-{m['lots']:g}"
            if lots >= m["lots"] and rid not in have:
                rows.append({"id": rid, "type": "milestone", "uid": c["uid"], "accounts": accs, "country": c["country"],
                             "milestone": m["lots"], "lots_at": lots, "amount": m["exd"], "status": "due", "created": now()})
    save(rows)
    return rows


def add_signup_reward(claim: dict) -> dict | None:
    amount = env("EXD_REWARD_AMOUNT")
    if not amount or not claim.get("accounts"):
        return None
    rows = load()
    rid = f"su-{claim['id']}"
    r = next((x for x in rows if x["id"] == rid), None)
    if not r:
        r = {"id": rid, "type": "signup", "uid": claim.get("uid", ""), "email": claim["email"], "name": claim.get("name", ""),
             "accounts": claim["accounts"][:1], "country": claim.get("country", ""), "amount": float(amount), "status": "due", "created": now()}
        rows.append(r)
        save(rows)
    return r


def progress(lots: float, uid: str) -> list[dict]:
    """Milestone ladder for one client, with the status of each reward."""
    by = {r["milestone"]: r for r in load() if r["type"] == "milestone" and r["uid"] == uid}
    out, prev = [], 0.0
    for m in milestones():
        r = by.get(m["lots"])
        status = r["status"] if r else ("due" if lots >= m["lots"] else "locked")
        span = m["lots"] - prev
        out.append({**m, "status": status, "progress": 1.0 if lots >= m["lots"] else max(0.0, (lots - prev) / span if span else 0)})
        prev = m["lots"]
    return out


def mark(ids: list[str], status: str) -> list[dict]:
    rows = load()
    changed = []
    for r in rows:
        if r["id"] in ids and r["status"] != "paid":
            r["status"] = status
            r[f"{status}_at"] = now()
            changed.append(r)
    save(rows)
    return changed


# ---------- journal trades ----------
def mock_orders(accounts: list[str], n: int = 30, seed: str = "") -> list[dict]:
    r = random.Random(seed or ",".join(accounts))
    t = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for i in range(n):
        t -= timedelta(hours=r.choice([2, 3, 5, 8, 20]))
        dur = r.choice([6, 12, 25, 40, 75])
        out.append({"order_id": f"demo:{6200000 + i * 37}", "symbol": r.choice(["XAUUSD", "XAUUSD", "EURUSD", "GBPUSD", "USOIL", "US30"]),
                    "volume_lots": str(r.choice([0.01, 0.02, 0.05, 0.1, 0.1, 0.2])), "open_date": (t - timedelta(minutes=dur)).isoformat().replace("+00:00", "Z"),
                    "close_date": t.isoformat().replace("+00:00", "Z"), "client_account": accounts[0] if accounts else "demo"})
    return out
