"""Deployment settings. Never commit .env or a Telegram session."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in your environment or .env")
    return value

API_ID = int(required("TG_API_ID"))
API_HASH = required("TG_API_HASH")
NTFY_URL = required("NTFY_URL").rstrip("/")
NTFY_TOPIC = required("NTFY_TOPIC")
NTFY_COMMAND_TOPIC = os.getenv("NTFY_COMMAND_TOPIC", NTFY_TOPIC)
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "")
TOTP_SECRET = required("TOTP_SECRET")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "contacts.db")
SESSION_PATH = str(DATA_DIR / "telegram")
NOTIFICATION_DELAY = max(0, float(os.getenv("NOTIFICATION_DELAY", "20")))
