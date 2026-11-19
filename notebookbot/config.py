import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = _int("GUILD_ID", 0) or None
MANAGER_ROLE_ID = _int("MANAGER_ROLE_ID", 0) or None

NOTEBOOKLM_PROFILE = os.getenv("NOTEBOOKLM_PROFILE", "").strip() or None

ASK_COOLDOWN = max(0, _int("ASK_COOLDOWN", 10))
DATA_DIR = Path(os.getenv("DATA_DIR", "").strip() or "data")

EMBED_COLOR = 0x4285F4
NOTEBOOK_URL = "https://notebooklm.google.com/notebook/{}"
