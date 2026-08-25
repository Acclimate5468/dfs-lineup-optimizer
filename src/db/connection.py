"""SQLite connection helper. Not yet wired into app flow."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.config.settings import DB_PATH


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
