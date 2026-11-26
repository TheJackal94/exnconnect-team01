"""Funnel Copilot: pull -> analyse -> Claude brief -> Telegram.

    python funnel_copilot.py --mock            # sample data, prints the brief, writes data/funnel.json
    python funnel_copilot.py                   # live if EXN_API_KEY + key file are set, else mock
    python funnel_copilot.py --send            # also send the brief to Telegram
    python funnel_copilot.py --inspect         # live: dump first row of every endpoint to data/live_samples.json

Compliance (FAIS-sensitive): education only. No signals, no market bias, no profit promises.
Every client-facing draft carries a risk line, and nothing goes to clients without partner approval.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import BRAND, DATA_DIR, SNAPSHOT, data_mode, env, live_configured, private_key_material  # noqa: E402
import dataset  # noqa: E402

STALLED_DAYS = 3
IDLE_DAYS = 5
RISK_LINE = "Education only, not financial advice. CFDs carry a high risk of losing money."


# ---------- pull ----------
def make_client():
    """Live REST client, or the snapshot replay when there is no key of our own."""
    if data_mode() == "snapshot":
        return dataset.SnapshotClient(SNAPSHOT)
    from exness_client import ExnessClient
    return ExnessClient(env("EXN_API_KEY"), private_key_material(), env("EXN_HOST", "https://api.exness.com"))


def mock_reports() -> dict:
    return dataset.mock_dataset()


def live_reports(samples: dict | None = None) -> dict:
    return dataset.live_dataset(make_client(), samples)


def load(force_mock: bool = False) -> dict:
    m = "mock" if force_mock else data_mode()
    if m == "mock":
        return mock_reports()
    ds = live_reports()
    ds["mode"] = m
    if m == "snapshot":
        ds["synced"] = make_client().captured
    return ds


# ---------- analyse ----------
def rate(a: float, b: float) -> float:
    return round(a / b, 4) if b else 0.0


def analyse(ds: dict) -> dict:
    camps = []
    for c in ds["campaigns"]:
        camps.append({**c, "reg_rate": rate(c["reg"], c["clicks"]), "fund_rate": rate(c["fund"], c["reg"]),
                      "trade_rate": rate(c["act"], c["fund"])})
    clients = ds["clients"]
    has_clicks = any(c["clicks"] for c in camps)
    # One source for the funnel: the per-campaign conversion numbers when present (same report as clicks),
    # otherwise count from the clients report. Mixing them would compare a full report with one page of clients.
    if any(c["reg"] for c in camps):
        totals = {k: sum(c[k] for c in camps) for k in ("clicks", "reg", "fund", "act")}
    else:
        totals = {"clicks": sum(c["clicks"] for c in camps), "reg": len(clients),
                  "fund": sum(c["stage"] != "registered" for c in clients), "act": sum(c["stage"] == "trading" for c in clients)}
    totals["reward"] = round(sum(c["reward"] for c in camps), 2)
    platforms: dict[str, dict] = {}
    for c in camps:
        p = platforms.setdefault(c["platform"], {"platform": c["platform"], "clicks": 0, "reg": 0, "fund": 0, "act": 0, "reward": 0.0})
        for k in ("clicks", "reg", "fund", "act", "reward"):
            p[k] += c[k]
    for p in platforms.values():
        p["reg_rate"] = rate(p["reg"], p["clicks"])

    steps = [("click → sign-up", totals["clicks"], totals["reg"]), ("sign-up → funded", totals["reg"], totals["fund"]),
             ("funded → trading", totals["fund"], totals["act"])]
    if not has_clicks:
        steps = steps[1:]
    # Biggest leak = the step losing the largest share of people (relative to how normal that step is)
    leak = min(steps, key=lambda s: rate(s[2], s[1]) if s[1] else 1)
    ranked = sorted([c for c in camps if c["clicks"] >= 50], key=lambda c: c["reg_rate"], reverse=True) if has_clicks \
        else sorted(camps, key=lambda c: c["reg"], reverse=True)
    most_viewed = max(camps, key=lambda c: c["clicks"]) if camps else None
    best = ranked[0] if ranked else None
    multiple = round(best["reg_rate"] / most_viewed["reg_rate"], 1) if best and most_viewed and most_viewed["reg_rate"] else None
    return {
        "brand": BRAND, "mode": ds.get("mode"), "generated": datetime.now().isoformat(timespec="minutes"),
        "partner": ds["partner"], "totals": totals, "campaigns": ranked or camps, "platforms": sorted(platforms.values(), key=lambda p: -p["reg"]),
        "stalled": [c for c in clients if c["stage"] == "registered" and c["days"] >= STALLED_DAYS],
        "funded_idle": [c for c in clients if c["stage"] == "funded" and c["days"] >= IDLE_DAYS],
        "biggest_leak": {"step": leak[0], "from": leak[1], "to": leak[2], "kept": rate(leak[2], leak[1])},
        "best": best, "most_viewed": most_viewed, "best_vs_most_viewed": multiple,
        "dead": [c for c in camps if c["clicks"] >= 100 and c["reg"] <= 2],
        "byday": ds.get("byday", []), "warnings": ds.get("warnings", []), "funnel_sample": ds.get("funnel_sample", False),
    }


# ---------- Claude ----------
SYSTEM = f"""You are Funnel Copilot for {BRAND}, an independent Exness partner that publishes trading education and tools.
You write a short morning brief for the partner (not for clients) from their funnel numbers.

Rules (South Africa, FAIS-sensitive):
- Education and tools only. Never give trading signals, market direction, instrument picks or profit promises.
- Talk about content, funnels and client education, never about markets.
- The partner approves every client message before it is sent.

Format: plain text for Telegram, at most 220 words, no markdown headings.
1. One line of headline numbers (clicks → sign-ups → funded → trading).
2. What converts: name the best content and its rate, compared with the most-viewed post.
3. The biggest leak, in one sentence.
4. "Today:" then exactly 3 numbered, concrete actions about content or follow-up.
5. "Draft for stalled sign-ups (needs your approval):" then a 2-3 sentence education-only message that ends with
   this exact risk line: "{RISK_LINE}"
Use only the numbers given. Do not invent figures."""


def _facts(a: dict) -> dict:
    slim = lambda c: c and {k: c[k] for k in ("name", "platform", "clicks", "reg", "fund", "act", "reg_rate")}
    return {"partner": a["partner"]["name"], "totals": a["totals"], "biggest_leak": a["biggest_leak"],
            "best": slim(a["best"]), "most_viewed": slim(a["most_viewed"]), "best_vs_most_viewed_x": a["best_vs_most_viewed"],
            "top_campaigns": [slim(c) for c in a["campaigns"][:6]], "platforms": a["platforms"],
            "dead_campaigns": [c["name"] for c in a["dead"]], "stalled_signups": len(a["stalled"]),
            "funded_not_trading": len(a["funded_idle"]),
            "note": "Campaign numbers are SAMPLE data (no tracked links yet). Say so in the brief." if a.get("funnel_sample") else ""}


def claude(system: str, user: str, max_tokens: int = 2000, messages: list | None = None, effort: str = "medium") -> str | None:
    """Returns text, or None if no key / SDK / the call failed (caller falls back)."""
    if not env("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("anthropic SDK not installed (pip install anthropic); using fallback", file=sys.stderr)
        return None
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"), timeout=60.0)
    try:
        r = client.beta.messages.create(
            model=env("CLAUDE_MODEL", "claude-sonnet-5-5"),
            max_tokens=max_tokens,
            system=system,
            messages=messages or [{"role": "user", "content": user}],
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        print("Claude: invalid ANTHROPIC_API_KEY; using fallback", file=sys.stderr)
        return None
    except anthropic.RateLimitError:
        print("Claude: rate limited; using fallback", file=sys.stderr)
        return None
    except anthropic.APIStatusError as e:
        print(f"Claude: API error {e.status_code}: {e.message}; using fallback", file=sys.stderr)
        return None
    except anthropic.APIConnectionError:
        print("Claude: network error; using fallback", file=sys.stderr)
        return None
    if r.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    return text or None


def fallback_brief(a: dict) -> str:
    t, b, v, L = a["totals"], a["best"], a["most_viewed"], a["biggest_leak"]
    lines = [f"Morning brief · {a['partner']['name']}" + (" (content funnel: sample campaigns until you create tracked links)" if a.get("funnel_sample") else ""), "",
             f"{t['clicks']:,} clicks → {t['reg']:,} sign-ups → {t['fund']:,} funded → {t['act']:,} trading", ""]
    if b and v:
        lines.append(f"Works: \"{b['name']}\" converts {b['reg_rate']*100:.1f}% of clicks"
                     + (f", {a['best_vs_most_viewed']}× your most-viewed post." if a["best_vs_most_viewed"] else "."))
        lines.append(f"Most viewed: \"{v['name']}\" ({v['clicks']:,} clicks) converts {v['reg_rate']*100:.1f}%.")
    lines.append(f"Biggest leak: {L['step']}, only {L['kept']*100:.0f}% get through.")
    lines += ["", "Today:",
              f"1. Make two more tool-led posts like \"{b['name']}\"." if b else "1. Give every post its own campaign link.",
              f"2. Add the free-tool call-to-action to \"{v['name']}\"." if v else "2. Pin your best freebie.",
              f"3. Pause or rework: {', '.join(c['name'] for c in a['dead'])}." if a["dead"] else
              f"3. Approve the lesson below for {len(a['stalled'])} stalled sign-ups.", "",
              "Draft for stalled sign-ups (needs your approval):", NUDGES[0]]
    return "\n".join(lines)


def ai_brief(a: dict) -> tuple[str, str]:
    """Returns (brief, source) where source is 'claude' or 'fallback'."""
    txt = claude(SYSTEM, "Funnel numbers (JSON):\n" + json.dumps(_facts(a), default=str))
    return (txt, "claude") if txt else (fallback_brief(a), "fallback")


NUDGES = [
    f"Lesson 1: your first account, the safe way. Most new traders lose money by sizing too big. Here's the free {BRAND} "
    f"lot-size calculator and a 1% risk plan to practise on a demo first. {RISK_LINE}",
    f"Before you fund, answer 3 questions: What's your max loss per trade? Per day? Which session suits your hour? "
    f"The free {BRAND} journal walks you through all three. {RISK_LINE}",
    f"Try a demo first. Run the free risk plan on a demo for two weeks and log every trade in the journal. "
    f"If you go live later, start small. {RISK_LINE}",
]

NUDGE_SYSTEM = f"""You write short re-engagement messages for {BRAND}, an independent Exness partner offering trading education and tools.
The reader signed up but has not funded an account. Rules: education only; no signals, no market views, no urgency or
pressure, no profit promises, no mention of bonuses as a reason to deposit. Suggest a demo, a risk plan or a free tool.
2-3 sentences, plain text, friendly. End with exactly: "{RISK_LINE}"
Reply with the message only."""

BANNED = ("guarantee", "profit", "signal", "buy ", "sell ", "bullish", "bearish", "easy money", "risk-free", "risk free")


def compliance_check(text: str) -> list[str]:
    low = text.lower()
    issues = [f'contains "{w.strip()}"' for w in BANNED if w in low and not (w == "profit" and "no profit" in low)]
    if RISK_LINE.lower() not in low:
        issues.append("missing the risk line")
    return issues


def rewrite_nudge(previous: str = "", audience: str = "stalled") -> tuple[str, str]:
    who = "registered 3+ days ago and never funded" if audience == "stalled" else "funded but have not placed a trade"
    prompt = f"Write a fresh message for people who {who}. Make it different from this one:\n{previous}"
    txt = claude(NUDGE_SYSTEM, prompt, max_tokens=1000)
    if txt and not compliance_check(txt):
        return txt, "claude"
    i = (NUDGES.index(previous) + 1) % len(NUDGES) if previous in NUDGES else 0
    return NUDGES[i], "library"


# ---------- Telegram ----------
def send_telegram(text: str, chat_id: str | None = None) -> dict:
    token, chat = env("TG_BOT_TOKEN"), chat_id or env("TG_CHAT_ID")
    if not token or not chat:
        return {"ok": False, "skipped": True, "reason": "TG_BOT_TOKEN / TG_CHAT_ID not set"}
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=15)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "status": r.status_code}


# ---------- run ----------
def run(force_mock=False, send=False) -> dict:
    a = analyse(load(force_mock))
    brief, src = ai_brief(a)
    out = {**a, "brief": brief, "brief_source": src, "nudge": NUDGES[0]}
    (DATA_DIR / "funnel.json").write_text(json.dumps(out, indent=2, default=str))
    if send:
        out["telegram"] = send_telegram(brief)
    return out


def main():
    ap = argparse.ArgumentParser(description="Funnel Copilot")
    ap.add_argument("--mock", action="store_true", help="use sample data")
    ap.add_argument("--send", action="store_true", help="send the brief to Telegram")
    ap.add_argument("--inspect", action="store_true", help="dump the first row of each live endpoint")
    args = ap.parse_args()
    if args.inspect:
        if not live_configured():
            sys.exit("--inspect needs EXN_API_KEY and EXN_PRIVATE_KEY_PATH in .env")
        samples: dict = {}
        ds = live_reports(samples)
        p = DATA_DIR / "live_samples.json"
        p.write_text(json.dumps(samples, indent=2, default=str))
        print(f"wrote {p}\nwarnings:", *ds["warnings"] or ["none"], sep="\n  ")
        return
    out = run(args.mock, args.send)
    print(f"[{out['mode']} data · brief by {out['brief_source']}]\n")
    print(out["brief"])
    for w in out["warnings"]:
        print("warning:", w, file=sys.stderr)
    if args.send:
        print("\ntelegram:", out["telegram"])


if __name__ == "__main__":
    main()
