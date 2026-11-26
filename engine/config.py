"""Tiny .env loader (no python-dotenv dependency) and shared settings."""
import os
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
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


BRAND = env("BRAND", "Trade Hour")
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def private_key_path() -> Path:
    kp = Path(env("EXN_PRIVATE_KEY_PATH", "./keys/exness_ed25519.pem"))
    return kp if kp.is_absolute() else (ROOT / kp).resolve()


def live_configured() -> bool:
    """Live mode needs a public key and a readable private key, and MOCK must not be forced."""
    if env("MOCK", "").lower() in ("1", "true", "yes"):
        return False
    return env("EXN_API_KEY").startswith("exnsk_") and private_key_path().exists()


SNAPSHOT = DATA_DIR / "snapshot.json"


def data_mode() -> str:
    """live (own API key) > snapshot (real data captured via the Exness MCP connector) > mock."""
    if live_configured():
        return "live"
    if env("MOCK", "").lower() in ("1", "true", "yes") or env("USE_SNAPSHOT", "1") == "0":
        return "mock"
    return "snapshot" if SNAPSHOT.exists() else "mock"
