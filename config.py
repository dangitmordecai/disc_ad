from dataclasses import dataclass
from typing import Optional
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    token: str
    database_url: str

def get_settings() -> "Settings":
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing in .env")
    db_url = os.getenv("DATABASE_URL", "sqlite:///./app.db").strip()
    return Settings(token=token, database_url=db_url)
