"""``Extras.itdb``: lyrics text and chapter marker data, one row per track that has either."""

from __future__ import annotations

from podsync.itdb.sqlite._shared import open_fresh_db, signed_id
from podsync.itdb.writer.strings import build_chapter_blob
from podsync.itdb.writer.track import TrackRecord

__all__ = ["write_extras_itdb"]

_SCHEMA = """
CREATE TABLE chapter (item_pid INTEGER NOT NULL, data BLOB, PRIMARY KEY (item_pid));
CREATE TABLE lyrics (item_pid INTEGER NOT NULL, checksum INTEGER, lyrics TEXT, PRIMARY KEY (item_pid));
"""


def write_extras_itdb(path: str, tracks: list[TrackRecord]) -> None:
    """Write ``Extras.itdb``: lyrics and chapter markers for tracks that carry them."""
    conn = open_fresh_db(path)
    try:
        conn.executescript(_SCHEMA)
        for track in tracks:
            if track.lyrics:
                checksum = sum(track.lyrics.encode("utf-8")) & 0xFFFFFFFF
                conn.execute(
                    "INSERT INTO lyrics (item_pid, checksum, lyrics) VALUES (?, ?, ?)",
                    (signed_id(track.db_track_id), checksum, track.lyrics),
                )
            chapters = (track.chapter_data or {}).get("chapters")
            if chapters:
                blob = build_chapter_blob(
                    chapters,
                    unk024=track.chapter_data.get("unk024", 0),
                    unk028=track.chapter_data.get("unk028", 0),
                    unk032=track.chapter_data.get("unk032", 0),
                )
                if blob:
                    conn.execute(
                        "INSERT INTO chapter (item_pid, data) VALUES (?, ?)",
                        (signed_id(track.db_track_id), blob),
                    )
        conn.commit()
    finally:
        conn.close()
