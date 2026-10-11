"""Configuration loaded from environment variables (see .env.example)."""
import os
from pathlib import Path


def _load_dotenv() -> None:
    """Tiny .env loader so no extra dependency is needed."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Meta / Messenger ---
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN", "")
FB_APP_SECRET = os.getenv("FB_APP_SECRET", "")
FB_VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN", "change-me-verify-token")
FB_GRAPH_VERSION = os.getenv("FB_GRAPH_VERSION", "v23.0")

# --- AI (Claude) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-5-5")

# --- App ---
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
SESSION_SECRET = os.getenv("SESSION_SECRET", "please-change-this-secret")
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Phnom_Penh")

# Optional: Telegram alerts to the owner for new bookings / payment proofs / handoffs
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "hotel.db"
