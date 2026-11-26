"""Tiny .env loader (no python-dotenv dependency) and shared settings."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ('"', "'") and v[0] in v[1:]:  # quoted: keep everything inside the quotes
            v = v[1:v.index(v[0], 1)]
        else:  # unquoted: drop an inline " # comment"
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip() if not v.startswith("#") else ""
        os.environ.setdefault(k.strip(), v)


load_env()


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


BRAND = env("BRAND", "Trade Hour")
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def private_key_path() -> Path:
    kp = Path(env("EXN_PRIVATE_KEY_PATH", "./keys/exness_ed25519.pem"))
    return kp if kp.is_absolute() else (ROOT / kp).resolve()


def _unwrap(v: str) -> str:
    """Tolerate a value pasted as <value> or with stray quotes."""
    v = v.strip()
    while len(v) > 1 and v[0] + v[-1] in ("<>", '""', "''"):
        v = v[1:-1].strip()
    return v


def api_key() -> str:
    return _unwrap(env("EXN_API_KEY"))


def private_key_material() -> str:
    """EXN_PRIVATE_KEY (PEM text or Exness hex; handy as a Codespaces secret) wins over the key file."""
    if env("EXN_PRIVATE_KEY").strip():
        return _unwrap(env("EXN_PRIVATE_KEY"))
    p = private_key_path()
    return p.read_text() if p.exists() else ""


def live_configured() -> bool:
    """Live mode needs an API key and a private key, and MOCK must not be forced."""
    if env("MOCK", "").lower() in ("1", "true", "yes"):
        return False
    return api_key().startswith("exnsk") and bool(private_key_material().strip())


SNAPSHOT = DATA_DIR / "snapshot.json"


def data_mode() -> str:
    """live (own API key) > snapshot (real data captured via the Exness MCP connector) > mock."""
    if live_configured():
        return "live"
    if env("MOCK", "").lower() in ("1", "true", "yes") or env("USE_SNAPSHOT", "1") == "0":
        return "mock"
    return "snapshot" if SNAPSHOT.exists() else "mock"
