"""
Centralized configuration.

All environment-dependent values are read once, here, so the rest of the
codebase never touches os.environ directly. This makes it obvious what the
app depends on to run, and makes it trivial to swap in a different config
source (e.g. Streamlit secrets when deployed to Streamlit Community Cloud)
without hunting through every module.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # no-op in production if no .env file is present

BASE_DIR = Path(__file__).resolve().parent.parent

CATEGORIES: list[str] = [
    "Food", "Transport", "Shopping", "Bills", "Entertainment", "Health", "Other"
]


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", "").strip())
    gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"))
    db_path: str = field(default_factory=lambda: os.environ.get(
        "SMARTSPEND_DB_PATH", str(BASE_DIR / "data" / "smartspend.db")
    ))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))

    @property
    def ai_enabled(self) -> bool:
        return bool(self.gemini_api_key)


settings = Settings()

# Ensure the data directory exists before anything tries to open the DB file.
Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger configured once, consistently."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(settings.log_level)
    return logger
