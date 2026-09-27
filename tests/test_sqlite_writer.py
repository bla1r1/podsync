"""``iTunes Library.itlp`` (nano 5G-7G) SQLite database writer.

Covers each database's own writer (schema shape, one track/playlist round
trip), the checksum book's signature for all three signed schemes, the
orchestrator's atomic all-or-nothing install, and the
``save_device_library`` integration that routes SQLite-era devices here
instead of the classic binary iTunesDB.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from podsync.hardware import make_virtual_ipod, select_device
from podsync.hardware.catalog.checksum import SignatureKind
from podsync.itdb.sqlite.cbk import write_locations_cbk
from podsync.itdb.sqlite.dynamic import write_dynamic_itdb
from podsync.itdb.sqlite.extras import write_extras_itdb
from podsync.itdb.sqlite.genius import write_genius_itdb
from podsync.itdb.sqlite.library import write_library_itdb
from podsync.itdb.sqlite.locations import write_locations_itdb
from podsync.itdb.sqlite import write_sqlite_databases
from podsync.itdb.writer.playlist import PlaylistRecord
from podsync.itdb.writer.track import TrackRecord
from podsync.library import database as library_db


def _track(**overrides) -> TrackRecord:
    fields = {
        "title": "Test Song", "location": ":iPod_Control:Music:F00:SONG.mp3", "artist": "Artist",
        "album": "Album", "genre": "Rock", "db_track_id": 12345, "size": 5,
    }
    fields.update(overrides)
    return TrackRecord(**fields)


class TestLibraryItdb:
    def test_writes_one_row_per_track_and_the_master_playlist(self, tmp_path) -> None:
        path = tmp_path / "Library.itdb"
        pids = write_library_itdb(str(path), [_track()], master_playlist_name="Blair's iPod")

        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT pid, title, artist, album, genre_id FROM item").fetchall()
        assert rows == [(12345, "Test Song", "Artist", "Album", 1)]
        containers = conn.execute("SELECT pid, name, is_hidden FROM container").fetchall()
        assert containers == [(pids[0], "Blair's iPod", 1)]
        assert conn.execute("SELECT item_pid FROM item_to_container").fetchall() == [(12345,)]

    def test_playlist_gets_its_own_container_and_membership(self, tmp_path) -> None:
        path = tmp_path / "Library.itdb"
        pids = write_library_itdb(
            str(path), [_track()], playlists=[PlaylistRecord(name="My Mix", track_ids=[12345])],
        )

        assert len(pids) == 2
        conn = sqlite3.connect(path)
        names = conn.execute("SELECT name FROM container ORDER BY pid").fetchall()
        assert names == [("iPod",), ("My Mix",)]
        membership = conn.execute(
            "SELECT container_pid, item_pid FROM item_to_container WHERE container_pid = ?", (pids[1],),
        ).fetchall()
        assert membership == [(pids[1], 12345)]

    def test_unknown_album_and_artist_still_get_a_row(self, tmp_path) -> None:
        """A track with no album/artist must not vanish — it becomes the "unknown" bucket."""
        path = tmp_path / "Library.itdb"
        write_library_itdb(str(path), [_track(artist=None, album=None, album_artist=None)])

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT is_unknown, name FROM album").fetchall() == [(1, None)]


class TestLocationsItdb:
    def test_location_is_relative_to_the_music_root(self, tmp_path) -> None:
        path = tmp_path / "Locations.itdb"
        write_locations_itdb(str(path), [_track(location=":iPod_Control:Music:F07:SONG.mp3")])

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT item_pid, location FROM location").fetchall() == [(12345, "F07/SONG.mp3")]
        assert conn.execute("SELECT path FROM base_location").fetchall() == [("iPod_Control/Music",)]


class TestDynamicItdb:
    def test_play_and_skip_counts_carry_through(self, tmp_path) -> None:
        path = tmp_path / "Dynamic.itdb"
        write_dynamic_itdb(str(path), [_track(play_count=4, skip_count=1, rating=80)], [1])

        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT has_been_played, play_count_user, skip_count_user, user_rating FROM item_stats",
        ).fetchone()
        assert row == (1, 4, 1, 80)
        assert conn.execute("SELECT container_pid FROM container_ui").fetchall() == [(1,)]


class TestExtrasItdb:
    def test_lyrics_are_stored_only_when_present(self, tmp_path) -> None:
        path = tmp_path / "Extras.itdb"
        write_extras_itdb(str(path), [_track(lyrics="La la la"), _track(db_track_id=999, lyrics=None)])

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT item_pid, lyrics FROM lyrics").fetchall() == [(12345, "La la la")]


class TestGeniusItdb:
    def test_tables_exist_and_are_empty(self, tmp_path) -> None:
        path = tmp_path / "Genius.itdb"
        write_genius_itdb(str(path))

        conn = sqlite3.connect(path)
        for table in ("genius_config", "genius_metadata", "genius_similarities"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


class TestLocationsCbk:
    def test_hashab_header_matches_the_signing_primitive(self, tmp_path) -> None:
        from podsync.itdb.writer.signing.ab import compute_hashab

        locations = tmp_path / "Locations.itdb"
        locations.write_bytes(b"x" * 2500)  # 3 blocks: 1024 + 1024 + 452
        cbk_path = tmp_path / "Locations.itdb.cbk"

        write_locations_cbk(
            str(cbk_path), str(locations), checksum_kind=SignatureKind.HASHAB,
            firewire_id=bytes(range(8)), ipod_path=str(tmp_path),
        )

        data = locations.read_bytes()
        block_digests = [hashlib.sha1(data[i:i + 1024]).digest() for i in range(0, len(data), 1024)]
        final_digest = hashlib.sha1(b"".join(block_digests)).digest()
        expected_header = compute_hashab(final_digest, bytes(range(8)))

        cbk = cbk_path.read_bytes()
        assert cbk[:57] == expected_header
        assert cbk[57:77] == final_digest
        assert cbk[77:] == b"".join(block_digests)

    def test_hashab_without_a_firewire_id_refuses(self, tmp_path) -> None:
        locations = tmp_path / "Locations.itdb"
        locations.write_bytes(b"x" * 10)

        with pytest.raises(ValueError, match="FireWire ID"):
            write_locations_cbk(
                str(tmp_path / "out.cbk"), str(locations), checksum_kind=SignatureKind.HASHAB,
                firewire_id=None, ipod_path=str(tmp_path),
            )

    def test_none_checksum_kind_uses_the_bare_digest_as_the_header(self, tmp_path) -> None:
        locations = tmp_path / "Locations.itdb"
        locations.write_bytes(b"abc")
        cbk_path = tmp_path / "out.cbk"

        write_locations_cbk(
            str(cbk_path), str(locations), checksum_kind=SignatureKind.NONE, firewire_id=None, ipod_path=str(tmp_path),
        )

        final_digest = hashlib.sha1(hashlib.sha1(b"abc").digest()).digest()
        cbk = cbk_path.read_bytes()
        assert cbk[:20] == final_digest
        assert cbk[20:40] == final_digest


class TestWriteSqliteDatabasesOrchestrator:
    def test_writes_all_five_databases_and_a_signed_cbk(self, tmp_path) -> None:
        ok = write_sqlite_databases(
            str(tmp_path), [_track()], playlists=[PlaylistRecord(name="Mix", track_ids=[12345])],
            checksum_kind=SignatureKind.HASHAB, firewire_id=bytes(range(8)),
        )

        assert ok is True
        itlp = tmp_path / "iPod_Control" / "iTunes" / "iTunes Library.itlp"
        assert sorted(p.name for p in itlp.iterdir()) == [
            "Dynamic.itdb", "Extras.itdb", "Genius.itdb", "Library.itdb", "Locations.itdb", "Locations.itdb.cbk",
        ]

    def test_unsigned_device_gets_no_cbk_file(self, tmp_path) -> None:
        ok = write_sqlite_databases(str(tmp_path), [_track()], checksum_kind=SignatureKind.NONE)

        assert ok is True
        itlp = tmp_path / "iPod_Control" / "iTunes" / "iTunes Library.itlp"
        assert "Locations.itdb.cbk" not in {p.name for p in itlp.iterdir()}

    def test_a_failing_database_write_leaves_nothing_installed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "podsync.itdb.sqlite.write_dynamic_itdb", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        ok = write_sqlite_databases(str(tmp_path), [_track()])

        assert ok is False
        itlp = tmp_path / "iPod_Control" / "iTunes" / "iTunes Library.itlp"
        assert not itlp.exists()

    def test_second_write_backs_up_the_first(self, tmp_path) -> None:
        write_sqlite_databases(str(tmp_path), [_track()])
        write_sqlite_databases(str(tmp_path), [_track(title="Second Cut")])

        itlp = tmp_path / "iPod_Control" / "iTunes" / "iTunes Library.itlp"
        assert (itlp / "Library.itdb.backup").exists()
        conn = sqlite3.connect(itlp / "Library.itdb")
        assert conn.execute("SELECT title FROM item").fetchone() == ("Second Cut",)


class TestSaveDeviceLibraryRoutesToSqlite:
    def test_sqlite_era_device_gets_an_itlp_not_an_itunesdb(self, tmp_path) -> None:
        select_device(make_virtual_ipod(tmp_path, "MC525"))  # nano 6G: HASHAB, SQLite-era
        media = tmp_path / "iPod_Control" / "Music" / "F00" / "SONG.mp3"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"audio")

        ok = library_db.save_device_library(
            tmp_path, [_track(location=":iPod_Control:Music:F00:SONG.mp3")], raise_on_error=True,
        )

        assert ok is True
        itlp = tmp_path / "iPod_Control" / "iTunes" / "iTunes Library.itlp"
        assert (itlp / "Library.itdb").exists()
        assert (itlp / "Locations.itdb.cbk").exists()  # HASHAB device: signed
        assert not (tmp_path / "iPod_Control" / "iTunes" / "iTunesDB").exists()
