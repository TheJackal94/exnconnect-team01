# Trade Hour: Funnel Copilot

Exness Connect 26 hackathon build. *Exness partners create content blind. Copilot turns the Partnership API into a coach: it shows what converts, where people drop off, and brings stalled sign-ups back, compliantly.*

```
engine/exness_client.py   Ed25519-signed Exness REST client (python exness_client.py = offline signing self-test)
engine/dataset.py         one data shape for dashboard + Copilot; mock generator + live adapter (FIELDS map)
engine/funnel_copilot.py  pull -> analyse -> Claude brief -> Telegram; nudge rewrite + compliance check
engine/rewards.py         EXD sign-up + lot-milestone rewards; Partner Area EXD transfer links
engine/faq.py             landing chatbot knowledge base, guardrails, switch-partner steps
api/server.py             stdlib HTTP server: signs calls server-side, caches, serves JSON + all pages
partners/index.html       partner dashboard (/)  (works standalone on sample data, or via the server)
landing/index.html        client landing page (/landing/): chatbot, New vs Already-at-Exness sign-up
client/index.html         client dashboard (/client/): login gate, auto-filled journal, lot milestones
data/snapshot.json        real partner data captured read-only via the Exness MCP connector (local only)
```

## Run it (2 minutes)

```bash
cd trade-hour
pip install -r requirements.txt          # cryptography, requests, anthropic
cp .env.example .env                     # leave Exness keys blank for sample data
python api/server.py                     # http://127.0.0.1:8787  ·  landing at /landing/
python engine/funnel_copilot.py --mock   # brief in the terminal, writes data/funnel.json
```

Going live: put `EXN_API_KEY` in `.env` and the PKCS#8 Ed25519 key at `keys/exness_ed25519.pem`. Then run
`python engine/funnel_copilot.py --inspect`. It writes the first row of every endpoint to `data/live_samples.json`.
Compare that file with `FIELDS` in `engine/dataset.py` and fix any key that's wrong. The dashboard shows a warning bar for any endpoint that fails.

Optional: `ANTHROPIC_API_KEY` turns on Claude briefs and rewrites (otherwise a template brief is used). `TG_BOT_TOKEN` plus `TG_CHAT_ID` sends the brief to Telegram, and `TG_CLIENT_CHAT_ID` is where approved lessons go. Set `DASH_TOKEN` before exposing the server; then open `/?t=<token>` once.

## Data modes
`live` (own `EXN_API_KEY` + PEM) → `snapshot` (data/snapshot.json, real numbers from 26 Nov) → `mock`. The status pill shows which.
The partner account has no tracked campaign links yet, so the Copilot funnel shows **labelled sample campaigns**; everything else is real.

## EXD rewards (how it really works)
Exness has no API that moves EXD. The partner's EXD wallet pays out in the Partner Area. The dashboard builds the same
prefilled link the Exness connector builds (`my.exnessaffiliates.com/payments/exd-transfer?...defaultRecipients=a,b&defaultAmount=n`),
the partner confirms there, then clicks **Mark paid** so the client dashboard updates. One amount per transfer.
Real snapshot: 47 milestone rewards = 650 EXD due vs a 300 EXD wallet. Tune `MILESTONES` before launch.

## Client dashboard
Login = email + Exness account number. Live: `POST /affiliation` must return the account under that email, and the client
must have `ftd_received`. Snapshot/mock: account number only (demo; shown on the login page). Trades fill the journal
from the partner orders report (`GET /reports/orders`: symbol, open/close time, lots). No P/L or direction is shared, so
the client logs result, setup, risk and rules. **We don't ask clients for Exness API keys**: the partner connection
already has their trades and lots, and collecting clients' private keys would be a security and compliance risk.
Hackathon-grade auth: add an email one-time code before real launch.

## What's confirmed vs guessed (checked against exness-api.com docs, 26 Nov)

| Thing | Status |
|---|---|
| Signing: compact JSON in key order `api_key, idempotency_key, timestamp, sign_version, method, path, body_hash`; `timestamp` and `sign_version` are numbers; base64url without padding | **Confirmed** (matches the official JS example) |
| Envelope `{data, totals}`; clients report `reg_date`, `trade_fn`, `reward_usd` (string), `volume_lots` | **Confirmed** |
| `POST /campaigns {title, reward_schema}` → `campaign_code, base_link, link_code, links[]` | **Confirmed** |
| Conversion report needs `ordering`; `group_by=campaign`; repeat `tags` | **Confirmed** |
| Referral agents: `alias, link_code, share_perc, clients, reward_usd`. **Reward share can't be changed through the API**, so the dashboard shows it read-only | **Confirmed** |
| `PATCH /autorebates/clients/{client_account}` exists | Confirmed. Body field `rebate_percent` is a guess |
| Clients report: `client_uid, reg_date, client_country, volume_lots, reward_usd, kyc_passed, ftd_received, ftt_made, client_status` (`trade_fn` = last reward date) | **Confirmed live** (26 Nov) |
| Accounts report: `client_account, client_uid, partner_code, client_account_type, volume_lots, reward_usd` | **Confirmed live** |
| IB performance: `group_fields`, `click_count, registration_count, first_deposit_count, trade_starting_real_count, volume_lots, reward_usd` | **Confirmed live** |
| Orders: `order_id, symbol, open_date, close_date, volume_lots, client_account` (32-day window) | **Confirmed live** |
| EXD wallet + Partner Area transfer link format | **Confirmed live** |
| `POST /affiliation` REST response field names | Guess (connector returns affiliated / account numbers / client UID) |
| Conversion row field names (`clicks`, `registrations`, `ftd`, `traded`), client deposit flag, tier fields | **Guesses**: run `--inspect` |

## Compliance built in
- Briefs and nudges are education only. The prompts forbid signals, market views and profit promises.
- Every nudge goes through `compliance_check()`. It blocks banned words and requires the risk line. The server returns 422 rather than send a failing message.
- The partner approves every client message. Approved lessons go to the partner's own opted-in Telegram channel; the Exness docs say report data is not permission to contact clients.
- EXD payouts need a click per person, are logged with their idempotency key, and are retried with the same key on unknown outcomes.
- No FSP number on partner materials. **Open question:** the EXD sign-up reward may count as an inducement. Confirm the wording with the Exness contact.

## Open items for the team
0. **Partner change process**: the chatbot's 4 steps are general; confirm the exact Exness process with the Exness contact (`engine/faq.py` SWITCH_STEPS). Also confirm what EXD can be used for.
1. Get a partner API key, run `--inspect`, and fix `FIELDS`. *(3 people)*
2. Set `EXD_REWARD_AMOUNT`, `EXD_TERMS`, `REWARD_WALLET_ACCOUNT` and the real `PARTNER_LINK`. The landing page keeps the `[EXD AMOUNT]` placeholders visible until these are set. *(2)*
3. Create the Telegram bot and channels, add the Claude key, and run a live send. *(2)*
4. Landing: confirm whether the tools list grows past the journal and the indicator.
5. Deck (problem → solution → live demo → architecture → impact) and demo rehearsal. The pitch line "converts 7.7× better than our most-viewed post" comes straight from the sample data.
