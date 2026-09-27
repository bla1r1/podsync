"""``Genius.itdb``: Genius mix/similarity data. podsync never computes any, so this
database always has its (real, firmware-expected) empty tables and nothing else.
"""

from __future__ import annotations

from podsync.itdb.sqlite._shared import open_fresh_db

__all__ = ["write_genius_itdb"]

_SCHEMA = """
CREATE TABLE genius_config (
    id INTEGER NOT NULL, version INTEGER, default_num_results INTEGER DEFAULT 0,
    min_num_results INTEGER DEFAULT 0, data BLOB, PRIMARY KEY (id), UNIQUE (version)
);
CREATE TABLE genius_metadata (genius_id INTEGER NOT NULL, version INTEGER, data BLOB, PRIMARY KEY (genius_id));
CREATE TABLE genius_similarities (genius_id INTEGER NOT NULL, version INTEGER, data BLOB, PRIMARY KEY (genius_id));
"""


def write_genius_itdb(path: str) -> None:
    """Write an empty ``Genius.itdb`` — present because the firmware expects the file to exist."""
    conn = open_fresh_db(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
