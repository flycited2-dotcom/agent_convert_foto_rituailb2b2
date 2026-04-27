import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0") or 0)
API_TOKEN = os.getenv("API_TOKEN", "").strip()
API_PORT = int(os.getenv("API_PORT", "8765"))

INPUT_DIR = _path("INPUT_DIR", "input")
OUTPUT_DIR = _path("OUTPUT_DIR", "output")
PROCESSED_DIR = _path("PROCESSED_DIR", "processed")
FAILED_DIR = _path("FAILED_DIR", "failed")
LOGS_DIR = _path("LOGS_DIR", "logs")

DB_PATH = ROOT / "queue.db"
