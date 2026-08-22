"""Platform-aware locations for Jarvis user data."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def data_dir() -> Path:
    """Return a writable per-user data directory without changing Linux defaults."""
    if os.name == "nt":
        app_data = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA")
        base = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
        return base / "Jarvis"
    return PROJECT_ROOT / "memory"


def history_path() -> Path:
    return data_dir() / "chat_history.db"


def memory_path() -> Path:
    return data_dir() / "user_memory.json"


def user_config_path() -> Path:
    return data_dir() / "user_config.json"
