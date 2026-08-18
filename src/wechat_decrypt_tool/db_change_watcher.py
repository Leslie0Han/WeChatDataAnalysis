"""Shared, low-cost change detection for live WeChat database storage."""

from __future__ import annotations

import os
from pathlib import Path


def scan_db_storage_mtime_ns(db_storage_dir: Path) -> int:
    """Return the newest relevant database mtime, or zero on a best-effort failure."""

    try:
        base = str(db_storage_dir)
    except Exception:
        return 0

    max_ns = 0
    try:
        for root, dirs, files in os.walk(base):
            if root == base:
                allowed = {
                    "message",
                    "session",
                    "contact",
                    "head_image",
                    "bizchat",
                    "sns",
                    "general",
                    "favorite",
                }
                dirs[:] = [name for name in dirs if str(name or "").lower() in allowed]

            for filename in files:
                name = str(filename or "").lower()
                if not name.endswith((".db", ".db-wal", ".db-shm")):
                    continue
                if not any(token in name for token in ("message", "session", "contact", "name2id", "head_image")):
                    continue
                try:
                    stat = os.stat(os.path.join(root, filename))
                    mtime_ns = int(getattr(stat, "st_mtime_ns", 0) or 0)
                    if mtime_ns <= 0:
                        mtime_ns = int(float(getattr(stat, "st_mtime", 0.0) or 0.0) * 1_000_000_000)
                    max_ns = max(max_ns, mtime_ns)
                except Exception:
                    continue
    except Exception:
        return 0
    return max_ns
