"""``Dynamic.itdb``: play/skip counts, ratings, bookmarks, and per-playlist UI state.

Everything here changes often on the device and never carries device-identity
signatures — unlike ``Locations.itdb``, this file is not checksummed.
"""

from __future__ import annotations

from podsync.itdb.sqlite._shared import open_fresh_db, signed_id, unix_to_coredata
from podsync.itdb.writer.track import TrackRecord

__all__ = ["write_dynamic_itdb"]

_SCHEMA = """
CREATE TABLE item_stats (
    item_pid INTEGER NOT NULL,
    has_been_played INTEGER DEFAULT 0,
    date_played INTEGER DEFAULT 0,
    play_count_user INTEGER DEFAULT 0,
    play_count_recent INTEGER DEFAULT 0,
    date_skipped INTEGER DEFAULT 0,
    skip_count_user INTEGER DEFAULT 0,
    skip_count_recent INTEGER DEFAULT 0,
    bookmark_time_ms REAL,
    bookmark_time_ms_common REAL,
    user_rating INTEGER DEFAULT 0,
    user_rating_common INTEGER DEFAULT 0,
    rental_expired INTEGER DEFAULT 0,
    play_count_user_original INTEGER DEFAULT 0,
    skip_count_user_original INTEGER DEFAULT 0,
    genius_id INTEGER DEFAULT 0,
    PRIMARY KEY (item_pid)
);

CREATE TABLE container_ui (
    container_pid INTEGER NOT NULL,
    play_order INTEGER DEFAULT 0,
    is_reversed INTEGER DEFAULT 0,
    album_field_order INTEGER DEFAULT 0,
    repeat_mode INTEGER DEFAULT 0,
    shuffle_items INTEGER DEFAULT 0,
    has_been_shuffled INTEGER DEFAULT 0,
    PRIMARY KEY (container_pid)
);

CREATE TABLE rental_info (
    item_pid INTEGER NOT NULL,
    rental_date_started INTEGER DEFAULT 0,
    rental_duration INTEGER DEFAULT 0,
    rental_playback_date_started INTEGER DEFAULT 0,
    rental_playback_duration INTEGER DEFAULT 0,
    is_demo INTEGER DEFAULT 0,
    PRIMARY KEY (item_pid)
);
"""


def write_dynamic_itdb(path: str, tracks: list[TrackRecord], playlist_ids: list[int]) -> None:
    """Write ``Dynamic.itdb``: one ``item_stats`` row per track, one ``container_ui`` per playlist."""
    conn = open_fresh_db(path)
    try:
        conn.executescript(_SCHEMA)
        for track in tracks:
            conn.execute(
                "INSERT INTO item_stats (item_pid, has_been_played, date_played, play_count_user, "
                "play_count_recent, date_skipped, skip_count_user, skip_count_recent, bookmark_time_ms, "
                "bookmark_time_ms_common, user_rating, user_rating_common, rental_expired, "
                "play_count_user_original, skip_count_user_original, genius_id) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, 0, ?, ?, 0)",
                (
                    signed_id(track.db_track_id), int(track.play_count > 0),
                    unix_to_coredata(track.last_played), track.play_count,
                    unix_to_coredata(track.last_skipped), track.skip_count,
                    float(track.bookmark_time), float(track.bookmark_time),
                    track.rating, track.app_rating, track.play_count, track.skip_count,
                ),
            )
        for playlist_id in playlist_ids:
            conn.execute(
                "INSERT INTO container_ui (container_pid, play_order, is_reversed, album_field_order, "
                "repeat_mode, shuffle_items, has_been_shuffled) VALUES (?, 0, 0, 1, 0, 0, 0)",
                (signed_id(playlist_id),),
            )
        conn.commit()
    finally:
        conn.close()
