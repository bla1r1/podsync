"""``Locations.itdb``: maps each track's on-device file to its database row.

Two tables: one ``base_location`` row naming the shared root
(``iPod_Control/Music``), and one ``location`` row per track holding its
path under that root.
"""

from __future__ import annotations

import time

from podsync.itdb.sqlite._shared import open_fresh_db, signed_id, unix_to_coredata
from podsync.itdb.spec.codes import FILETYPE_CODES
from podsync.itdb.writer.track import TrackRecord

__all__ = ["write_locations_itdb"]

_SCHEMA = """
CREATE TABLE base_location (id INTEGER NOT NULL, path TEXT, PRIMARY KEY (id));

CREATE TABLE location (
    item_pid INTEGER NOT NULL,
    sub_id INTEGER NOT NULL DEFAULT 0,
    base_location_id INTEGER DEFAULT 0,
    location_type INTEGER,
    location TEXT,
    extension INTEGER,
    kind_id INTEGER DEFAULT 0,
    date_created INTEGER DEFAULT 0,
    file_size INTEGER DEFAULT 0,
    file_creator INTEGER,
    file_type INTEGER,
    num_dir_levels_file INTEGER,
    num_dir_levels_lib INTEGER,
    PRIMARY KEY (item_pid, sub_id)
);
"""

_LOCATION_TYPE_FILE = 0x46494C45  # "FILE", big-endian
_DEFAULT_EXTENSION = FILETYPE_CODES.get("mp3", 0x4D503320)
# location_kind_map ids, as written into Library.itdb by podsync.itdb.sqlite.library.
_KIND_ID_FOR_FILETYPE = {"mp3": 1, "m4p": 2, "aac": 3, "m4a": 3, "m4b": 3, "alac": 3}


def _music_relative_path(location: str) -> str:
    """*location* (podsync's ``:iPod_Control:Music:Fxx:NAME.ext``) as ``Fxx/NAME.ext``."""
    from podsync.library.media_paths import MUSIC_ROOT, _relative_for

    relative = _relative_for(location) or ""
    prefix = MUSIC_ROOT + "/"
    return relative[len(prefix):] if relative.startswith(prefix) else relative


def write_locations_itdb(path: str, tracks: list[TrackRecord]) -> None:
    """Write ``Locations.itdb``: one file-path row per track."""
    conn = open_fresh_db(path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO base_location (id, path) VALUES (1, 'iPod_Control/Music')")

        now = int(time.time())
        for track in tracks:
            filetype = (track.filetype or "").lower()
            conn.execute(
                "INSERT INTO location (item_pid, sub_id, base_location_id, location_type, location, "
                "extension, kind_id, date_created, file_size, file_creator, file_type, "
                "num_dir_levels_file, num_dir_levels_lib) VALUES (?, 0, 1, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                (
                    signed_id(track.db_track_id), _LOCATION_TYPE_FILE, _music_relative_path(track.location),
                    FILETYPE_CODES.get(filetype, _DEFAULT_EXTENSION), _KIND_ID_FOR_FILETYPE.get(filetype, 0),
                    unix_to_coredata(track.date_added or now), track.size,
                ),
            )
        conn.commit()
    finally:
        conn.close()
