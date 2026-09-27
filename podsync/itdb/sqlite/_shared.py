"""Small pieces every ``*.itdb`` SQLite writer in this package needs.

These databases (nano 5G's ``iTunes Library.itlp``) are a second, parallel
format to the classic binary iTunesDB — same track/playlist data, Apple's
own SQLite schema instead of the ``mhbd``/chunk layout. The schema facts
below (table and column names) are the real on-device format, independent
of any particular implementation of it.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from pathlib import Path

__all__ = ["clean_sort_key", "open_fresh_db", "signed_id", "strip_leading_article", "unix_to_coredata"]

# CoreData/Cocoa reference date: 2001-01-01 00:00:00 UTC, as a Unix timestamp.
_CORE_DATA_EPOCH = 978307200
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def unix_to_coredata(unix_seconds: int) -> int:
    """Unix seconds to CoreData seconds; ``0`` maps to ``0`` (podsync's "unset" convention)."""
    return 0 if not unix_seconds else unix_seconds - _CORE_DATA_EPOCH


def signed_id(value: int) -> int:
    """An unsigned 64-bit id (db_track_id, playlist_id, ...) as SQLite's signed 64-bit INTEGER."""
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= (1 << 63) else value


def strip_leading_article(name: str | None) -> str | None:
    """*name* with a leading "The"/"A"/"An" removed, for sort-name display."""
    return _LEADING_ARTICLE.sub("", name).strip() if name else name


def clean_sort_key(name: str | None) -> str:
    """A case/accent-insensitive key for grouping and ranking names.

    Empty for ``None``/blank, so unset fields sort together, first.
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", strip_leading_article(name) or "")
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold()


def open_fresh_db(path: str | Path) -> sqlite3.Connection:
    """A new, empty SQLite database at *path* (an existing file there is replaced)."""
    path = Path(path)
    if path.exists():
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA encoding='UTF-8'")
    return conn
