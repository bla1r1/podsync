"""``iTunes Library.itlp``: the SQLite database family nano 5G-7G use instead of
the classic binary iTunesDB.

Five databases plus a signed checksum book, written to a temp directory and
installed one at a time so a failure partway through never leaves a mixed
old/new set on the device:

- ``Library.itdb`` — tracks, albums, artists, composers, playlists.
- ``Locations.itdb`` — each track's on-device file path.
- ``Dynamic.itdb`` — play counts, ratings, bookmarks, playlist UI state.
- ``Extras.itdb`` — lyrics and chapter markers.
- ``Genius.itdb`` — always empty; podsync computes no Genius data.
- ``Locations.itdb.cbk`` — a signed checksum book for ``Locations.itdb``,
  using whatever signing scheme (HASH58/HASH72/HASHAB) the device needs.

This module only writes; there is no SQLite reader, so a caller cannot read
these databases back the way :func:`podsync.library.database.load_device_library`
reads the classic format.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from podsync.hardware.catalog.checksum import SignatureKind
from podsync.hardware.safety.durable import flush_written_file, open_unique_sibling_temp, safe_replace, safe_unlink
from podsync.hardware.safety.paths import safe_device_path
from podsync.itdb.sqlite.cbk import write_locations_cbk
from podsync.itdb.sqlite.dynamic import write_dynamic_itdb
from podsync.itdb.sqlite.extras import write_extras_itdb
from podsync.itdb.sqlite.genius import write_genius_itdb
from podsync.itdb.sqlite.library import write_library_itdb
from podsync.itdb.sqlite.locations import write_locations_itdb
from podsync.itdb.writer.playlist import PlaylistRecord
from podsync.itdb.writer.track import TrackRecord, generate_db_track_id

__all__ = ["write_sqlite_databases"]

_ITLP_RELATIVE = "iPod_Control/iTunes/iTunes Library.itlp"
_DATABASE_NAMES = ("Library.itdb", "Locations.itdb", "Dynamic.itdb", "Extras.itdb", "Genius.itdb")


def _install(src: Path, dst: Path, *, before_device_mutation: Callable[[], None] | None) -> None:
    """Durably replace *dst* with *src*'s contents (temp file + atomic rename)."""
    if before_device_mutation is not None:
        before_device_mutation()
    temp_path, temp_file = open_unique_sibling_temp(dst, mode="wb")
    try:
        with temp_file as handle, open(src, "rb") as source:
            shutil.copyfileobj(source, handle)
            flush_written_file(handle)
        if before_device_mutation is not None:
            before_device_mutation()
        safe_replace(temp_path, dst)
    except BaseException:
        safe_unlink(temp_path, missing_ok=True)
        raise


def write_sqlite_databases(
    ipod_path: str,
    tracks: list[TrackRecord],
    *,
    playlists: list[PlaylistRecord] | None = None,
    smart_playlists: list[PlaylistRecord] | None = None,
    master_playlist_name: str = "iPod",
    db_pid: int | None = None,
    checksum_kind: SignatureKind = SignatureKind.NONE,
    firewire_id: bytes | None = None,
    backup: bool = True,
    before_database_replace: Callable[[], None] | None = None,
    before_device_mutation: Callable[[], None] | None = None,
) -> bool:
    """Write the whole ``iTunes Library.itlp`` database set for one device.

    Every database is built in a temporary directory first; only once all of
    them (and the checksum book, for a signed device) are ready does
    *before_database_replace* fire and the real files get replaced one at a
    time. Returns ``False`` — device untouched — on any failure; a genuine
    device-safety violation still raises.
    """
    itlp_path = safe_device_path(ipod_path, _ITLP_RELATIVE, allowed_subtree=_ITLP_RELATIVE)
    db_pid = db_pid if db_pid is not None else generate_db_track_id()

    with tempfile.TemporaryDirectory(prefix="podsync_sqlite_") as scratch:
        scratch_path = Path(scratch)
        try:
            playlist_pids = write_library_itdb(
                str(scratch_path / "Library.itdb"), tracks, playlists=playlists, smart_playlists=smart_playlists,
                master_playlist_name=master_playlist_name, db_pid=db_pid,
            )
            write_locations_itdb(str(scratch_path / "Locations.itdb"), tracks)
            write_dynamic_itdb(str(scratch_path / "Dynamic.itdb"), tracks, playlist_pids)
            write_extras_itdb(str(scratch_path / "Extras.itdb"), tracks)
            write_genius_itdb(str(scratch_path / "Genius.itdb"))
            if checksum_kind != SignatureKind.NONE:
                write_locations_cbk(
                    str(scratch_path / "Locations.itdb.cbk"), str(scratch_path / "Locations.itdb"),
                    checksum_kind=checksum_kind, firewire_id=firewire_id, ipod_path=str(ipod_path),
                )
        except Exception:
            return False

        if before_database_replace is not None:
            before_database_replace()
        if backup:
            for name in (*_DATABASE_NAMES, "Locations.itdb.cbk"):
                existing = itlp_path / name
                if existing.exists():
                    _install(existing, existing.with_name(existing.name + ".backup"),
                             before_device_mutation=before_device_mutation)
        itlp_path.mkdir(parents=True, exist_ok=True)
        for name in _DATABASE_NAMES:
            _install(scratch_path / name, itlp_path / name, before_device_mutation=before_device_mutation)
        cbk_source = scratch_path / "Locations.itdb.cbk"
        if cbk_source.exists():
            _install(cbk_source, itlp_path / "Locations.itdb.cbk", before_device_mutation=before_device_mutation)
        return True
