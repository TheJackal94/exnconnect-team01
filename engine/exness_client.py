"""Ed25519-signed REST client for the Exness API.

Signing (https://www.exness-api.com/authentication/request-signing):
  EXN-DATA = base64url_nopad(JSON{api_key, idempotency_key, timestamp, sign_version, method, path, body_hash})
  EXN-SIGN = base64url_nopad(Ed25519(decoded EXN-DATA bytes))
  path includes the query string exactly as sent; body_hash = base64url_nopad(sha256(body bytes)).
The official example builds the JSON with JSON.stringify (compact, insertion order) and sends
timestamp and sign_version as numbers. We do the same.

Run `python exness_client.py` for an offline signing self-test.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

EMPTY_BODY_HASH = "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"
AUTH_ERRORS = {1000: "invalid API key", 1001: "invalid signature", 1002: "permission denied", 1003: "auth rate limit"}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class ExnessError(Exception):
    def __init__(self, status: int, payload: Any, path: str):
        self.status, self.payload, self.path = status, payload, path
        code = payload.get("code") if isinstance(payload, dict) else None
        hint = AUTH_ERRORS.get(code, "") if isinstance(code, int) else ""
        super().__init__(f"{status} on {path}: {payload}{' (' + hint + ')' if hint else ''}")


class ExnessClient:
    def __init__(self, api_key: str, private_key_pem: bytes | str, host: str = "https://api.exness.com", timeout: int = 20):
        self.api_key = api_key
        self.host = host.rstrip("/")
        self.timeout = timeout
        pem = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Exness requires an Ed25519 private key (PKCS#8 PEM)")
        self.key = key
        self.session = requests.Session()

    @classmethod
    def from_file(cls, api_key: str, key_path, host: str = "https://api.exness.com") -> "ExnessClient":
        with open(key_path, "rb") as f:
            return cls(api_key, f.read(), host)

    # ---------- signing ----------
    def _headers(self, method: str, path: str, body: bytes, idempotency_key: str = "") -> dict:
        ts = int(time.time() * 1000)
        payload = {
            "api_key": self.api_key,
            "idempotency_key": idempotency_key,
            "timestamp": ts,
            "sign_version": 1,
            "method": method.upper(),
            "path": path,
            "body_hash": b64url(hashlib.sha256(body).digest()),
        }
        data = json.dumps(payload, separators=(",", ":")).encode()
        h = {
            "EXN-API-KEY": self.api_key,
            "EXN-IDEMPOTENCY-KEY": idempotency_key,
            "EXN-TIMESTAMP": str(ts),
            "EXN-SIGN-VERSION": "1",
            "EXN-DATA": b64url(data),
            "EXN-SIGN": b64url(self.key.sign(data)),
            "Accept": "application/json",
        }
        if body:
            h["Content-Type"] = "application/json"
        return h

    # ---------- transport ----------
    def request(self, method: str, path: str, params: dict | list | None = None, body: Any = None,
                idempotency_key: str | None = None, retries: int = 3) -> Any:
        """params may be a dict or a list of (key, value) tuples (repeat keys, e.g. tags=a&tags=b)."""
        if params:
            items = params.items() if isinstance(params, dict) else params
            qs = urlencode([(k, v) for k, v in items if v is not None and v != ""])
            if qs:
                path = f"{path}?{qs}"
        method = method.upper()
        raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        if method != "GET" and idempotency_key is None:
            idempotency_key = str(uuid.uuid4())
        idem = idempotency_key or ""
        for attempt in range(retries + 1):
            # Fresh timestamp + signature every attempt; same idempotency key so retries are safe.
            r = self.session.request(method, self.host + path, data=raw or None,
                                     headers=self._headers(method, path, raw, idem), timeout=self.timeout)
            if r.status_code == 429 and attempt < retries:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code in (502, 503, 504) and attempt < retries and method == "GET":
                time.sleep(2 ** attempt)
                continue
            try:
                payload = r.json() if r.content else {}
            except ValueError:
                payload = {"raw": r.text[:500]}
            if r.status_code >= 400:
                raise ExnessError(r.status_code, payload, path)
            return payload
        raise ExnessError(429, {"error": "rate limited"}, path)

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body, idempotency_key=None):
        return self.request("POST", path, body=body, idempotency_key=idempotency_key)

    def patch(self, path, body):
        return self.request("PATCH", path, body=body)

    # ---------- partnership ----------
    P = "/v1/partnership"

    def partner_summary(self): return self.get(f"{self.P}/partner/summary")
    def default_link(self): return self.get(f"{self.P}/partner/default-link")
    def campaigns(self, limit=100): return self.get(f"{self.P}/campaigns", {"limit": limit})
    def create_campaign(self, title, reward_schema="revenue_share"):
        return self.post(f"{self.P}/campaigns", {"title": title, "reward_schema": reward_schema})
    def referral_agent_links(self): return self.get(f"{self.P}/referral-agent-links")
    def rebate_clients(self, limit=200): return self.get(f"{self.P}/autorebates/clients", {"limit": limit})
    def rebate_payments(self, limit=50): return self.get(f"{self.P}/autorebates/payments", {"limit": limit})
    def set_client_rebate(self, client_account, percent):
        return self.patch(f"{self.P}/autorebates/clients/{client_account}", {"rebate_percent": percent})
    def tier_conditions(self): return self.get(f"{self.P}/partner-tier/tier-conditions")
    def tier_state(self): return self.get(f"{self.P}/partner-tier/partner-state")
    def progress_reward(self): return self.get(f"{self.P}/reports/progress-reward")
    def clients_report(self, limit=500, offset=0, **f): return self.get(f"{self.P}/reports/clients", {"limit": limit, "offset": offset, **f})
    def registrations_byday(self, date_from, date_to):
        return self.get(f"{self.P}/reports/clients/registrations/byday", {"reg_date_from": date_from, "reg_date_to": date_to})
    def client_accounts(self, limit=500, offset=0): return self.get(f"{self.P}/reports/clients/accounts", {"limit": limit, "offset": offset})
    def client_accounts_second_level(self, limit=500): return self.get(f"{self.P}/reports/clients/accounts/second-level", {"limit": limit})
    def conversion(self, group_by="campaign", ordering=None, date_from=None, date_to=None, tags=()):
        q = [("group_by", group_by), ("ordering", ordering or group_by), ("date_from", date_from), ("date_to", date_to)]
        q += [("tags", t) for t in tags]
        return self.get(f"{self.P}/reports/conversion", q)
    def ib_performance(self, group_by="month", date_from=None, date_to=None):
        return self.get(f"{self.P}/reports/ib/performance", {"group_by": group_by, "date_from": date_from, "date_to": date_to})
    def check_affiliation(self, email): return self.post(f"{self.P}/affiliation", {"email": email})
    def partner_links(self): return self.get(f"{self.P}/partner/links")
    def orders(self, accounts, date_from, date_to, limit=500):
        """Closed client orders behind first-level rewards. date range is the reward-credit date, max 32 days."""
        return self.get(f"{self.P}/reports/orders", {"date_from": date_from, "date_to": date_to, "limit": limit,
                                                     "client_account": ",".join(accounts), "ordering": "-close_date"})

    # ---------- payments / trading ----------
    def balance(self, account_id): return self.get(f"/v1/accounts/{account_id}/balance")
    def transfer_to_client(self, source_account, destination_account, amount: str, idempotency_key: str):
        """Irreversible. Caller MUST persist idempotency_key before calling and reuse it on unknown outcomes."""
        return self.post("/v1/transfers", {"destination_type": "another_client", "source_account": str(source_account),
                                           "destination_account": str(destination_account), "amount": amount},
                         idempotency_key=idempotency_key)
    def transfer_status(self, transfer_id): return self.get(f"/v1/transfers/{transfer_id}")
    def account(self, account_id): return self.get(f"/v1/configuration/accounts/{account_id}/account")
    def limits(self, account_id): return self.get(f"/v1/configuration/accounts/{account_id}/limits")
    def access_point(self): return self.get("/v1/trading/access-point")


def _self_test() -> None:
    """Offline: sign a GET and a POST with a throwaway key and verify both signatures."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    c = ExnessClient("exnsk_selftest", pem, host="https://example.invalid")
    pub = key.public_key()

    def decode(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    h = c._headers("GET", "/v1/partnership/reports/conversion?group_by=country&ordering=country&tags=a&tags=b", b"")
    data = decode(h["EXN-DATA"])
    pub.verify(decode(h["EXN-SIGN"]), data)
    d = json.loads(data)
    assert d["body_hash"] == EMPTY_BODY_HASH, d["body_hash"]
    assert d["idempotency_key"] == "" and h["EXN-IDEMPOTENCY-KEY"] == ""
    assert d["path"].endswith("tags=a&tags=b") and d["sign_version"] == 1 and isinstance(d["timestamp"], int)
    assert str(d["timestamp"]) == h["EXN-TIMESTAMP"]
    assert list(d) == ["api_key", "idempotency_key", "timestamp", "sign_version", "method", "path", "body_hash"]
    assert "=" not in h["EXN-DATA"] and "=" not in h["EXN-SIGN"]

    body = json.dumps({"title": "Test"}, separators=(",", ":")).encode()
    idem = str(uuid.uuid4())
    h2 = c._headers("POST", "/v1/partnership/campaigns", body, idem)
    d2 = decode(h2["EXN-DATA"])
    pub.verify(decode(h2["EXN-SIGN"]), d2)
    assert json.loads(d2)["body_hash"] == b64url(hashlib.sha256(body).digest())
    assert json.loads(d2)["idempotency_key"] == idem == h2["EXN-IDEMPOTENCY-KEY"]
    print("signing self-test OK")
    print("  EXN-DATA (GET) decoded:", data.decode())


if __name__ == "__main__":
    _self_test()
