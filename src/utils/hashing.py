"""Stable hashes for dedupe / cache keys."""

from __future__ import annotations

import hashlib


def sha1_short(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]
