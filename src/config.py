import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROSPER_API_KEY = os.getenv("PLATFORM_API_KEY") or os.getenv("PROSPER_API_KEY", "")
PROSPER_BASE_URL = (
    os.getenv("PLATFORM_API_BASE_URL")
    or os.getenv("PROSPER_BASE_URL")
    or "https://voice.getprosperapp.com/api/v1"
)
PORT = int(os.getenv("PORT", "8000"))
