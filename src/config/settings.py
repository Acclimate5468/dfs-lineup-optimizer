"""App-level settings loaded from environment with sane local defaults."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]

DB_PATH = Path(os.getenv("DK_LAB_DB_PATH", REPO_ROOT / "data" / "database" / "dk_lab.sqlite3"))
UPLOADS_DIR = REPO_ROOT / "data" / "uploads"
EXPORTS_DIR = REPO_ROOT / "exports"
