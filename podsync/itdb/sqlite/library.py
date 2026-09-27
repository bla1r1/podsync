"""``Library.itdb``: the main SQLite music library — tracks, albums, artists,
composers, genres and playlists (``container``). Schema facts match what
iTunes itself writes on nano 5G-7G.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from podsync.itdb.spec.albums import album_identity_from_track
from podsync.itdb.spec.layouts.strings import MHOD_HEADER_SIZE
from podsync.itdb.sqlite._shared import clean_sort_key, open_fresh_db, signed_id, strip_leading_article, unix_to_coredata
from podsync.itdb.writer.playlist import PlaylistRecord, write_mhod51
from podsync.itdb.writer.track import TrackRecord

__all__ = ["write_library_itdb"]

# item.media_kind — distinct from the binary iTunesDB's media_type codes.
_MEDIA_KIND_SONG = 1
_MEDIA_KIND_MOVIE = 2
_MEDIA_KIND_PODCAST = 4
_MEDIA_KIND_AUDIOBOOK = 8
_MEDIA_KIND_MUSIC_VIDEO = 32
_MEDIA_KIND_TV_SHOW = 64
_MEDIA_KIND_RINGTONE = 0x4000
# The item table's is_song/is_audio_book/is_music_video/is_movie/is_tv_show/is_ringtone/is_podcast flags, in order.
_MEDIA_KIND_FLAG_ORDER = (
    _MEDIA_KIND_SONG, _MEDIA_KIND_AUDIOBOOK, _MEDIA_KIND_MUSIC_VIDEO,
    _MEDIA_KIND_MOVIE, _MEDIA_KIND_TV_SHOW, _MEDIA_KIND_RINGTONE, _MEDIA_KIND_PODCAST,
)
_MEDIA_TYPE_TO_KIND = {
    0x01: _MEDIA_KIND_SONG, 0x02: _MEDIA_KIND_MOVIE, 0x04: _MEDIA_KIND_PODCAST, 0x06: _MEDIA_KIND_PODCAST,
    0x08: _MEDIA_KIND_AUDIOBOOK, 0x20: _MEDIA_KIND_MUSIC_VIDEO, 0x40: _MEDIA_KIND_TV_SHOW, 0x4000: _MEDIA_KIND_RINGTONE,
}

_AUDIO_FORMAT_MP3, _AUDIO_FORMAT_AAC, _AUDIO_FORMAT_ALAC = 0x012D, 0x01F6, 0x01F7
_AUDIO_FORMAT_BY_FILETYPE = {
    "mp3": _AUDIO_FORMAT_MP3, "aac": _AUDIO_FORMAT_AAC, "m4a": _AUDIO_FORMAT_AAC,
    "m4p": _AUDIO_FORMAT_AAC, "m4b": _AUDIO_FORMAT_AAC, "wav": 0x006E, "aif": 0x006F, "aiff": 0x006F,
}
_ALAC_BITRATE_FLOOR = 500  # AAC tops out well below this; above it, an M4A/M4B is almost certainly ALAC.

_SCHEMA = """
CREATE TABLE version_info (
    id INTEGER PRIMARY KEY, major INTEGER, minor INTEGER, compatibility INTEGER DEFAULT 0,
    update_level INTEGER DEFAULT 0, device_update_level INTEGER DEFAULT 0, platform INTEGER DEFAULT 0
);
CREATE TABLE db_info (
    pid INTEGER NOT NULL, primary_container_pid INTEGER, media_folder_url TEXT, audio_language INTEGER,
    subtitle_language INTEGER, genius_cuid TEXT, bib BLOB, rib BLOB, PRIMARY KEY (pid)
);
CREATE TABLE genre_map (
    id INTEGER NOT NULL, genre TEXT NOT NULL, genre_order INTEGER DEFAULT 0, is_unknown INTEGER DEFAULT 0,
    has_music INTEGER DEFAULT 0, artist_count_calc INTEGER DEFAULT 0 NOT NULL,
    album_count_calc INTEGER DEFAULT 0 NOT NULL, compilation_count_calc INTEGER DEFAULT 0 NOT NULL,
    PRIMARY KEY (id), UNIQUE (genre)
);
CREATE TABLE location_kind_map (id INTEGER NOT NULL, kind TEXT NOT NULL, PRIMARY KEY (id), UNIQUE (kind));
CREATE TABLE category_map (id INTEGER NOT NULL, category TEXT NOT NULL, PRIMARY KEY (id), UNIQUE (category));
CREATE TABLE item (
    pid INTEGER NOT NULL, revision_level INTEGER, media_kind INTEGER DEFAULT 0,
    is_song INTEGER DEFAULT 0, is_audio_book INTEGER DEFAULT 0, is_music_video INTEGER DEFAULT 0,
    is_movie INTEGER DEFAULT 0, is_tv_show INTEGER DEFAULT 0, is_home_video INTEGER DEFAULT 0,
    is_ringtone INTEGER DEFAULT 0, is_tone INTEGER DEFAULT 0, is_voice_memo INTEGER DEFAULT 0,
    is_book INTEGER DEFAULT 0, is_rental INTEGER DEFAULT 0, is_itunes_u INTEGER DEFAULT 0,
    is_digital_booklet INTEGER DEFAULT 0, is_podcast INTEGER DEFAULT 0, date_modified INTEGER DEFAULT 0,
    year INTEGER DEFAULT 0, content_rating INTEGER DEFAULT 0, content_rating_level INTEGER DEFAULT 0,
    is_compilation INTEGER, is_user_disabled INTEGER DEFAULT 0, remember_bookmark INTEGER DEFAULT 0,
    exclude_from_shuffle INTEGER DEFAULT 0, part_of_gapless_album INTEGER DEFAULT 0,
    chosen_by_auto_fill INTEGER DEFAULT 0, artwork_status INTEGER, artwork_cache_id INTEGER DEFAULT 0,
    start_time_ms REAL DEFAULT 0, stop_time_ms REAL DEFAULT 0, total_time_ms REAL DEFAULT 0,
    total_burn_time_ms REAL, track_number INTEGER DEFAULT 0, track_count INTEGER DEFAULT 0,
    disc_number INTEGER DEFAULT 0, disc_count INTEGER DEFAULT 0, bpm INTEGER DEFAULT 0,
    relative_volume INTEGER, eq_preset TEXT, radio_stream_status TEXT, genius_id INTEGER DEFAULT 0,
    genre_id INTEGER DEFAULT 0, category_id INTEGER DEFAULT 0, album_pid INTEGER DEFAULT 0,
    artist_pid INTEGER DEFAULT 0, composer_pid INTEGER DEFAULT 0, title TEXT, artist TEXT, album TEXT,
    album_artist TEXT, composer TEXT, sort_title TEXT, sort_artist TEXT, sort_album TEXT,
    sort_album_artist TEXT, sort_composer TEXT, title_order INTEGER, artist_order INTEGER,
    album_order INTEGER, genre_order INTEGER, composer_order INTEGER, album_artist_order INTEGER,
    album_by_artist_order INTEGER, series_name_order INTEGER, comment TEXT, grouping TEXT,
    description TEXT, description_long TEXT, collection_description TEXT, copyright TEXT,
    track_artist_pid INTEGER DEFAULT 0, physical_order INTEGER, has_lyrics INTEGER DEFAULT 0,
    date_released INTEGER DEFAULT 0, PRIMARY KEY (pid)
);
CREATE TABLE album (
    pid INTEGER NOT NULL, kind INTEGER, artwork_status INTEGER, artwork_item_pid INTEGER,
    artist_pid INTEGER, user_rating INTEGER, name TEXT, name_order INTEGER, all_compilations INTEGER,
    feed_url TEXT, season_number INTEGER, is_unknown INTEGER DEFAULT 0, has_songs INTEGER DEFAULT 0,
    has_music_videos INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0, artist_order INTEGER DEFAULT 0,
    has_any_compilations INTEGER DEFAULT 0, sort_name TEXT, artist_count_calc INTEGER DEFAULT 0 NOT NULL,
    has_movies INTEGER DEFAULT 0, item_count INTEGER DEFAULT 0, PRIMARY KEY (pid)
);
CREATE TABLE artist (
    pid INTEGER NOT NULL, kind INTEGER, artwork_status INTEGER, artwork_album_pid INTEGER, name TEXT,
    name_order INTEGER, sort_name TEXT, is_unknown INTEGER DEFAULT 0, has_songs INTEGER DEFAULT 0,
    has_music_videos INTEGER DEFAULT 0, PRIMARY KEY (pid)
);
CREATE TABLE track_artist (
    pid INTEGER NOT NULL, name TEXT, name_order INTEGER, sort_name TEXT, has_songs INTEGER DEFAULT 0,
    has_music_videos INTEGER DEFAULT 0, has_non_compilation_tracks INTEGER DEFAULT 0,
    is_unknown INTEGER DEFAULT 0, album_count INTEGER DEFAULT 0, PRIMARY KEY (pid)
);
CREATE TABLE composer (
    pid INTEGER NOT NULL, name TEXT, name_order INTEGER, sort_name TEXT, is_unknown INTEGER DEFAULT 0,
    has_music INTEGER DEFAULT 0, PRIMARY KEY (pid)
);
CREATE TABLE avformat_info (
    item_pid INTEGER NOT NULL, sub_id INTEGER NOT NULL DEFAULT 0, audio_format INTEGER,
    bit_rate INTEGER DEFAULT 0, channels INTEGER DEFAULT 0, sample_rate REAL DEFAULT 0, duration INTEGER,
    gapless_heuristic_info INTEGER, gapless_encoding_delay INTEGER, gapless_encoding_drain INTEGER,
    gapless_last_frame_resynch INTEGER, analysis_inhibit_flags INTEGER, audio_fingerprint INTEGER,
    volume_normalization_energy INTEGER, PRIMARY KEY (item_pid, sub_id)
);
CREATE TABLE container (
    pid INTEGER NOT NULL, distinguished_kind INTEGER, date_created INTEGER, date_modified INTEGER,
    name TEXT, name_order INTEGER, parent_pid INTEGER, media_kinds INTEGER, workout_template_id INTEGER,
    is_hidden INTEGER, smart_is_folder INTEGER, smart_is_dynamic INTEGER, smart_is_filtered INTEGER,
    smart_is_genius INTEGER, smart_enabled_only INTEGER, smart_is_limited INTEGER, smart_limit_kind INTEGER,
    smart_limit_order INTEGER, smart_evaluation_order INTEGER, smart_limit_value INTEGER,
    smart_reverse_limit_order INTEGER, smart_criteria BLOB, description TEXT, PRIMARY KEY (pid)
);
CREATE TABLE item_to_container (item_pid INTEGER, container_pid INTEGER, physical_order INTEGER, shuffle_order INTEGER);
CREATE TABLE container_seed (
    container_pid INTEGER NOT NULL, item_pid INTEGER NOT NULL, seed_order INTEGER DEFAULT 0,
    UNIQUE (container_pid, item_pid)
);
CREATE TABLE podcast_info (
    item_pid INTEGER NOT NULL, date_released INTEGER DEFAULT 0, external_guid TEXT, feed_url TEXT,
    feed_keywords TEXT, PRIMARY KEY (item_pid)
);
CREATE TABLE track_size_calc (pid INTEGER NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY (pid), UNIQUE (kind));
"""

_INDEXES = """
CREATE INDEX idx_item_album_pid ON item (album_pid);
CREATE INDEX idx_item_track_artist_pid ON item (track_artist_pid);
CREATE INDEX item_album_order_idx ON item (album_order, disc_number, track_number, artist_order, sort_title, physical_order);
CREATE INDEX item_artist_sort_order_idx ON item (artist_order, album_order, disc_number, track_number, sort_title, physical_order);
CREATE INDEX item_composer_order_idx ON item (composer_pid, composer_order, media_kind);
CREATE INDEX item_genre_id_idx ON item (genre_id);
CREATE INDEX item_title_order_idx ON item (title_order, media_kind);
CREATE INDEX item_to_container_container_pid_idx ON item_to_container (container_pid, physical_order, item_pid);
CREATE INDEX item_to_container_physical_order_idx ON item_to_container (physical_order);
"""


def _media_kind(track: TrackRecord) -> int:
    return _MEDIA_TYPE_TO_KIND.get(track.media_type, _MEDIA_KIND_SONG)


def _media_kind_flags(kind: int) -> tuple[int, ...]:
    return tuple(int(kind == candidate) for candidate in _MEDIA_KIND_FLAG_ORDER)


def _audio_format(track: TrackRecord) -> int:
    filetype = (track.filetype or "").lower()
    if filetype in ("m4a", "m4b") and track.bitrate > _ALAC_BITRATE_FLOOR:
        return _AUDIO_FORMAT_ALAC
    return _AUDIO_FORMAT_BY_FILETYPE.get(filetype, _AUDIO_FORMAT_MP3)


@dataclass
class _NameRegistry:
    """First-seen names get a stable pid; ``ranks`` gives their alphabetical order rank.

    A rank is ``(position + 1) * 100``, matching iTunes' own spacing (room to
    re-sort without renumbering everything); an unset/unknown name ranks 100,
    ahead of anything real.
    """

    next_pid: int
    pids: dict[str, int] = field(default_factory=dict)

    def pid_for(self, name: str) -> int:
        if name not in self.pids:
            self.pids[name] = self.next_pid
            self.next_pid += 1
        return self.pids[name]

    def ranks(self) -> dict[str, int]:
        keys = sorted({clean_sort_key(name) for name in self.pids if name})
        return {key: (index + 1) * 100 for index, key in enumerate(keys)}


def _rank_of(ranks: dict[str, int], name: str | None) -> int:
    return ranks.get(clean_sort_key(name), 100) if name else 100


def _insert_container(conn, *, pid: int, name: str, name_order: int, now: int, **smart_fields) -> None:
    fields = {
        "distinguished_kind": 0, "media_kinds": 1, "is_hidden": 0, "smart_is_folder": 0,
        "smart_is_dynamic": None, "smart_is_filtered": None, "smart_is_limited": None,
        "smart_limit_kind": None, "smart_limit_order": None, "smart_evaluation_order": None,
        "smart_limit_value": None, "smart_reverse_limit_order": None, "smart_criteria": None,
        **smart_fields,
    }
    conn.execute(
        "INSERT INTO container (pid, distinguished_kind, date_created, date_modified, name, name_order, "
        "parent_pid, media_kinds, workout_template_id, is_hidden, smart_is_folder, smart_is_dynamic, "
        "smart_is_filtered, smart_is_genius, smart_enabled_only, smart_is_limited, smart_limit_kind, "
        "smart_limit_order, smart_evaluation_order, smart_limit_value, smart_reverse_limit_order, "
        "smart_criteria, description) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            signed_id(pid), fields["distinguished_kind"], now, now, name, name_order, fields["media_kinds"],
            fields["is_hidden"], fields["smart_is_folder"], fields["smart_is_dynamic"], fields["smart_is_filtered"],
            fields["smart_is_limited"], fields["smart_limit_kind"], fields["smart_limit_order"],
            fields["smart_evaluation_order"], fields["smart_limit_value"], fields["smart_reverse_limit_order"],
            fields["smart_criteria"],
        ),
    )


def _smart_criteria_blob(playlist: PlaylistRecord) -> bytes | None:
    if playlist.smart_rules is None:
        return None
    encoded = write_mhod51(playlist.smart_rules)
    return encoded[MHOD_HEADER_SIZE:] or None


def write_library_itdb(
    path: str, tracks: list[TrackRecord], *, playlists: list[PlaylistRecord] | None = None,
    smart_playlists: list[PlaylistRecord] | None = None, master_playlist_name: str = "iPod", db_pid: int = 0,
) -> list[int]:
    """Write ``Library.itdb``. Returns every playlist's pid, master first, in write order."""
    conn = open_fresh_db(path)
    try:
        conn.executescript(_SCHEMA)
        master_pid = db_pid or 1

        conn.execute(
            "INSERT INTO version_info (id, major, minor, compatibility, update_level, "
            "device_update_level, platform) VALUES (1, 1, 111, 0, 0, 1104, 2)"
        )
        conn.execute(
            "INSERT INTO db_info (pid, primary_container_pid, media_folder_url, audio_language, "
            "subtitle_language, genius_cuid, bib, rib) VALUES (?, ?, NULL, -1, -1, NULL, NULL, NULL)",
            (signed_id(db_pid), signed_id(master_pid)),
        )
        for kind_id, kind_name in ((1, "MPEG audio file"), (2, "Purchased AAC audio file"), (3, "AAC audio file")):
            conn.execute("INSERT INTO location_kind_map (id, kind) VALUES (?, ?)", (kind_id, kind_name))

        def field_rank_source(field_name: str):
            return {
                "title": lambda t: t.sort_name or t.title,
                "artist": lambda t: t.sort_artist or t.artist,
                "album": lambda t: t.sort_album or t.album,
                "genre": lambda t: t.genre,
                "composer": lambda t: t.sort_composer or t.composer,
                "album_artist": lambda t: t.sort_artist or t.artist,
                "album_by_artist": lambda t: (
                    t.sort_album_artist or t.album_artist or t.sort_artist or t.artist
                ),
            }[field_name]

        order_ranks: dict[str, dict[str, int]] = {}
        for field_name in ("title", "artist", "album", "genre", "composer", "album_artist", "album_by_artist"):
            getter = field_rank_source(field_name)
            keys = sorted({clean_sort_key(getter(track)) for track in tracks if getter(track)})
            order_ranks[field_name] = {key: (index + 1) * 100 for index, key in enumerate(keys)}

        categories = _NameRegistry(1)
        for track in tracks:
            if track.category:
                categories.pid_for(track.category)
        for name, cid in categories.pids.items():
            conn.execute("INSERT INTO category_map (id, category) VALUES (?, ?)", (cid, name))

        genres = _NameRegistry(1)
        for track in tracks:
            if track.genre:
                genres.pid_for(track.genre)
        genre_ranks = {name: rank for rank, name in enumerate(sorted(genres.pids, key=str.lower), start=1)}
        genre_artists: dict[str, set[str]] = {}
        genre_albums: dict[str, set[tuple]] = {}
        genre_compilations: dict[str, set[tuple]] = {}
        for track in tracks:
            if not track.genre:
                continue
            identity = album_identity_from_track(track)
            album_key = (identity.album, identity.album_artist or identity.artist, identity.show_name)
            genre_artists.setdefault(track.genre, set()).add(identity.album_artist or identity.artist or "")
            genre_albums.setdefault(track.genre, set()).add(album_key)
            if track.compilation_flag:
                genre_compilations.setdefault(track.genre, set()).add(album_key)
        for name, gid in genres.pids.items():
            conn.execute(
                "INSERT INTO genre_map (id, genre, genre_order, is_unknown, has_music, artist_count_calc, "
                "album_count_calc, compilation_count_calc) VALUES (?, ?, ?, 0, 1, ?, ?, ?)",
                (
                    gid, name, genre_ranks.get(name, 0), len(genre_artists.get(name, ())),
                    len(genre_albums.get(name, ())), len(genre_compilations.get(name, ())),
                ),
            )

        # ── albums / artists / track artists / composers: stable pids in first-seen order ──
        pid_pool = _NameRegistry(101)  # 1..100 reserved for master playlist + small fixed ids
        albums = _NameRegistry(0)
        artists, track_artists, composers = _NameRegistry(0), _NameRegistry(0), _NameRegistry(0)
        album_identities: dict[int, tuple] = {}
        db_track_id_is_known: set[int] = set()
        for track in tracks:
            db_track_id_is_known.add(track.db_track_id)
            identity = album_identity_from_track(track)
            album_artist_name = identity.album_artist or identity.artist or ""
            album_key = (identity.album or "", album_artist_name, identity.show_name or "")
            if album_key not in albums.pids:
                albums.pids[album_key] = pid_pool.next_pid
                pid_pool.next_pid += 1
                album_identities[albums.pids[album_key]] = album_key
            if album_artist_name:
                artists.pid_for(album_artist_name)
            if track.artist:
                track_artists.pid_for(track.artist)
            if track.composer:
                composers.pid_for(track.composer)
        # Give artists/track_artists/composers real pids from the shared pool, in first-seen order.
        for registry in (artists, track_artists, composers):
            for name in list(registry.pids):
                registry.pids[name] = pid_pool.next_pid
                pid_pool.next_pid += 1

        album_item_counts: dict[int, int] = {}
        album_has_compilation: dict[int, bool] = {}
        album_artist_pid: dict[int, int] = {}
        album_artwork_pid: dict[int, int] = {}
        album_feed_url: dict[int, str] = {}
        artist_artwork_album: dict[str, int] = {}
        for track in tracks:
            identity = album_identity_from_track(track)
            album_artist_name = identity.album_artist or identity.artist or ""
            album_pid = albums.pids[(identity.album or "", album_artist_name, identity.show_name or "")]
            album_item_counts[album_pid] = album_item_counts.get(album_pid, 0) + 1
            if track.compilation_flag:
                album_has_compilation[album_pid] = True
            if album_artist_name in artists.pids:
                album_artist_pid[album_pid] = artists.pids[album_artist_name]
            if track.mhii_link and album_pid not in album_artwork_pid:
                album_artwork_pid[album_pid] = track.db_track_id
                artist_artwork_album.setdefault(album_artist_name, album_pid)
            if track.podcast_rss_url and album_pid not in album_feed_url:
                album_feed_url[album_pid] = track.podcast_rss_url

        album_name_ranks = {
            pid: (rank + 1) * 100
            for rank, pid in enumerate(sorted(album_identities, key=lambda p: clean_sort_key(album_identities[p][0])))
        }
        for album_pid, (album_name, album_artist_name, _show) in album_identities.items():
            is_compilation = int(album_has_compilation.get(album_pid, False))
            artwork_pid = album_artwork_pid.get(album_pid, 0)
            conn.execute(
                "INSERT INTO album (pid, kind, artwork_status, artwork_item_pid, artist_pid, user_rating, "
                "name, name_order, all_compilations, feed_url, season_number, is_unknown, has_songs, "
                "has_music_videos, sort_order, artist_order, has_any_compilations, sort_name, "
                "artist_count_calc, has_movies, item_count) "
                "VALUES (?, 2, ?, ?, ?, 0, ?, ?, ?, ?, 0, ?, 1, 0, ?, ?, ?, ?, 0, 0, ?)",
                (
                    album_pid, int(bool(artwork_pid)), signed_id(artwork_pid), album_artist_pid.get(album_pid, 0),
                    album_name or None, album_name_ranks.get(album_pid, 0), is_compilation,
                    album_feed_url.get(album_pid), int(not album_name), album_name_ranks.get(album_pid, 0),
                    _rank_of(order_ranks["album_artist"], album_artist_name), is_compilation,
                    strip_leading_article(album_name) or None, album_item_counts.get(album_pid, 0),
                ),
            )

        for registry, table, extra_sql, extra_defaults in (
            (artists, "artist",
             "artwork_album_pid, name, name_order, sort_name, is_unknown, has_songs, has_music_videos",
             "?, ?, ?, ?, ?, 1, 0"),
            (track_artists, "track_artist",
             "name, name_order, sort_name, has_songs, has_music_videos, has_non_compilation_tracks, is_unknown, "
             "album_count", "?, ?, ?, 1, 0, 1, ?, 0"),
            (composers, "composer", "name, name_order, sort_name, is_unknown, has_music", "?, ?, ?, ?, 1"),
        ):
            names_sorted_ranks = {
                name: (rank + 1) * 100 for rank, name in enumerate(sorted(registry.pids, key=clean_sort_key))
            }
            for name, pid in registry.pids.items():
                sort_name = strip_leading_article(name) or None
                is_unknown = int(not name)
                name_order = names_sorted_ranks.get(name, 0)
                if table == "artist":
                    art_album = artist_artwork_album.get(name, 0)
                    conn.execute(
                        f"INSERT INTO {table} (pid, kind, artwork_status, {extra_sql}) "
                        f"VALUES (?, 2, ?, {extra_defaults})",
                        (pid, int(bool(art_album)), signed_id(art_album), name or None, name_order, sort_name,
                         is_unknown),
                    )
                else:
                    conn.execute(
                        f"INSERT INTO {table} (pid, {extra_sql}) VALUES (?, {extra_defaults})",
                        (pid, name or None, name_order, sort_name, is_unknown),
                    )

        # ── items (tracks) ──────────────────────────────────────────────────
        now_cd = unix_to_coredata(int(time.time()))
        size_by_bucket = {"audio": 0, "video": 0, "music_video": 0}
        for physical_order, track in enumerate(tracks):
            identity = album_identity_from_track(track)
            album_artist_name = identity.album_artist or identity.artist or ""
            album_pid = albums.pids[(identity.album or "", album_artist_name, identity.show_name or "")]
            kind = _media_kind(track)
            bucket = "music_video" if kind == _MEDIA_KIND_MUSIC_VIDEO else (
                "video" if kind in (_MEDIA_KIND_MOVIE, _MEDIA_KIND_TV_SHOW) else "audio")
            size_by_bucket[bucket] += track.size

            sort_title = track.sort_name or (strip_leading_article(track.title) if track.title else None)
            sort_artist = track.sort_artist or (strip_leading_article(track.artist) if track.artist else None)
            sort_album = track.sort_album or (strip_leading_article(track.album) if track.album else None)
            sort_album_artist = (
                track.sort_album_artist or (strip_leading_article(track.album_artist) if track.album_artist else None)
                or sort_artist
            )
            sort_composer = track.sort_composer or (strip_leading_article(track.composer) if track.composer else None)

            conn.execute(
                """INSERT INTO item (
                    pid, media_kind, is_song, is_audio_book, is_music_video, is_movie, is_tv_show, is_ringtone,
                    is_podcast, date_modified, year, content_rating, is_compilation, is_user_disabled,
                    remember_bookmark, exclude_from_shuffle, part_of_gapless_album, artwork_status,
                    artwork_cache_id, start_time_ms, stop_time_ms, total_time_ms, track_number, track_count,
                    disc_number, disc_count, bpm, relative_volume, eq_preset, genre_id, category_id, album_pid,
                    artist_pid, composer_pid, title, artist, album, album_artist, composer, sort_title,
                    sort_artist, sort_album, sort_album_artist, sort_composer, title_order, artist_order,
                    album_order, genre_order, composer_order, album_artist_order, album_by_artist_order,
                    series_name_order, comment, grouping, description, track_artist_pid, physical_order,
                    has_lyrics, date_released
                ) VALUES (
                    :pid, :media_kind, :is_song, :is_audio_book, :is_music_video, :is_movie, :is_tv_show,
                    :is_ringtone, :is_podcast, :date_modified, :year, :content_rating, :is_compilation,
                    :is_user_disabled, :remember_bookmark, :exclude_from_shuffle, :part_of_gapless_album,
                    :artwork_status, :artwork_cache_id, :start_time_ms, :stop_time_ms, :total_time_ms,
                    :track_number, :track_count, :disc_number, :disc_count, :bpm, :relative_volume, :eq_preset,
                    :genre_id, :category_id, :album_pid, :artist_pid, :composer_pid, :title, :artist, :album,
                    :album_artist, :composer, :sort_title, :sort_artist, :sort_album, :sort_album_artist,
                    :sort_composer, :title_order, :artist_order, :album_order, :genre_order, :composer_order,
                    :album_artist_order, :album_by_artist_order, 100, :comment, :grouping, :description,
                    :track_artist_pid, :physical_order, :has_lyrics, :date_released
                )""",
                {
                    "pid": signed_id(track.db_track_id), "media_kind": kind,
                    "is_song": _media_kind_flags(kind)[0], "is_audio_book": _media_kind_flags(kind)[1],
                    "is_music_video": _media_kind_flags(kind)[2], "is_movie": _media_kind_flags(kind)[3],
                    "is_tv_show": _media_kind_flags(kind)[4], "is_ringtone": _media_kind_flags(kind)[5],
                    "is_podcast": _media_kind_flags(kind)[6],
                    "date_modified": now_cd if not (track.last_modified or track.date_added) else
                    unix_to_coredata(track.last_modified or track.date_added),
                    "year": track.year, "content_rating": track.explicit_flag,
                    "is_compilation": int(track.compilation_flag), "is_user_disabled": int(bool(track.checked_flag)),
                    "remember_bookmark": int(track.remember_position),
                    "exclude_from_shuffle": int(track.skip_when_shuffling),
                    "part_of_gapless_album": int(bool(track.gapless_album_flag)),
                    "artwork_status": int(bool(track.mhii_link)), "artwork_cache_id": track.mhii_link or 0,
                    "start_time_ms": float(track.start_time), "stop_time_ms": float(track.stop_time),
                    "total_time_ms": float(track.length), "track_number": track.track_number,
                    "track_count": track.total_tracks, "disc_number": track.disc_number,
                    "disc_count": track.total_discs, "bpm": track.bpm, "relative_volume": track.volume,
                    "eq_preset": track.eq_setting, "genre_id": genres.pids.get(track.genre or "", 0),
                    "category_id": categories.pids.get(track.category or "", 0), "album_pid": album_pid,
                    "artist_pid": artists.pids.get(album_artist_name, 0),
                    "composer_pid": composers.pids.get(track.composer or "", 0),
                    "title": track.title, "artist": track.artist, "album": track.album,
                    "album_artist": track.album_artist, "composer": track.composer, "sort_title": sort_title,
                    "sort_artist": sort_artist, "sort_album": sort_album, "sort_album_artist": sort_album_artist,
                    "sort_composer": sort_composer,
                    "title_order": _rank_of(order_ranks["title"], track.sort_name or track.title),
                    "artist_order": _rank_of(order_ranks["artist"], track.sort_artist or track.artist),
                    "album_order": _rank_of(order_ranks["album"], track.sort_album or track.album),
                    "genre_order": _rank_of(order_ranks["genre"], track.genre),
                    "composer_order": _rank_of(order_ranks["composer"], track.sort_composer or track.composer),
                    "album_artist_order": _rank_of(order_ranks["album_artist"], track.sort_artist or track.artist),
                    "album_by_artist_order": _rank_of(
                        order_ranks["album_by_artist"],
                        track.sort_album_artist or track.album_artist or track.sort_artist or track.artist,
                    ),
                    "comment": track.comment, "grouping": track.grouping, "description": track.description,
                    "track_artist_pid": track_artists.pids.get(track.artist or "", 0),
                    "physical_order": physical_order,
                    "has_lyrics": int(bool(track.has_lyrics or track.lyrics)),
                    "date_released": unix_to_coredata(track.date_released) if track.date_released else 0,
                },
            )

            sample_rate = track.sample_rate or 0
            conn.execute(
                "INSERT INTO avformat_info (item_pid, sub_id, audio_format, bit_rate, channels, sample_rate, "
                "duration, gapless_heuristic_info, gapless_encoding_delay, gapless_encoding_drain, "
                "gapless_last_frame_resynch, analysis_inhibit_flags, audio_fingerprint, "
                "volume_normalization_energy) VALUES (?, 0, ?, ?, 0, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
                (
                    signed_id(track.db_track_id), _audio_format(track), track.bitrate, float(sample_rate),
                    int(track.length * sample_rate / 1000) if sample_rate else 0,
                    track.gapless_track_flag, track.pregap, track.postgap, track.gapless_data, track.sound_check,
                ),
            )

            if kind == _MEDIA_KIND_PODCAST:
                conn.execute(
                    "INSERT INTO podcast_info (item_pid, date_released, external_guid, feed_url, feed_keywords) "
                    "VALUES (?, ?, NULL, ?, NULL)",
                    (
                        signed_id(track.db_track_id),
                        unix_to_coredata(track.date_released) if track.date_released else 0,
                        track.podcast_rss_url,
                    ),
                )

        for pid, kind, size in ((1, "audio", size_by_bucket["audio"]), (2, "video", size_by_bucket["video"]),
                                 (3, "music_video", size_by_bucket["music_video"])):
            conn.execute("INSERT INTO track_size_calc (pid, kind, size) VALUES (?, ?, ?)", (pid, kind, size))

        # ── containers (playlists) ──────────────────────────────────────────
        position = 0
        _insert_container(conn, pid=master_pid, name=master_playlist_name, name_order=(position + 1) * 100,
                          now=now_cd, is_hidden=1)
        position += 1
        for order, track in enumerate(tracks):
            conn.execute(
                "INSERT INTO item_to_container (item_pid, container_pid, physical_order, shuffle_order) "
                "VALUES (?, ?, ?, NULL)",
                (signed_id(track.db_track_id), signed_id(master_pid), order),
            )

        all_playlist_pids = [master_pid]
        next_playlist_pid = master_pid + 1
        for playlist in (playlists or []):
            playlist_pid, next_playlist_pid = next_playlist_pid, next_playlist_pid + 1
            all_playlist_pids.append(playlist_pid)
            _insert_container(conn, pid=playlist_pid, name=playlist.name, name_order=(position + 1) * 100, now=now_cd)
            position += 1
            for order, db_track_id in enumerate(playlist.track_ids):
                if db_track_id in db_track_id_is_known:
                    conn.execute(
                        "INSERT INTO item_to_container (item_pid, container_pid, physical_order, shuffle_order) "
                        "VALUES (?, ?, ?, NULL)",
                        (signed_id(db_track_id), signed_id(playlist_pid), order),
                    )

        # Apple's real databases hide categories 4 (Music) and 5 (Audiobooks); media_kinds
        # follows the same split (1 = music-shaped, 0 = audiobook-shaped).
        _DISTINGUISHED_KIND = {4: 4, 5: 5}
        for smart in (smart_playlists or []):
            playlist_pid, next_playlist_pid = next_playlist_pid, next_playlist_pid + 1
            all_playlist_pids.append(playlist_pid)
            prefs = smart.smart_prefs
            distinguished_kind = _DISTINGUISHED_KIND.get(smart.mhsd5_type, 0)
            _insert_container(
                conn, pid=playlist_pid, name=smart.name, name_order=(position + 1) * 100, now=now_cd,
                distinguished_kind=distinguished_kind, media_kinds=0 if distinguished_kind == 5 else 1,
                is_hidden=int(smart.master), smart_is_dynamic=1, smart_is_filtered=1,
                smart_is_limited=int(prefs.check_limits) if prefs else 0,
                smart_limit_kind=prefs.limit_type if prefs else 2, smart_limit_order=prefs.limit_sort if prefs else 2,
                smart_evaluation_order=1, smart_limit_value=prefs.limit_value if prefs else 25,
                smart_reverse_limit_order=0, smart_criteria=_smart_criteria_blob(smart),
            )
            position += 1
            for order, db_track_id in enumerate(smart.track_ids):
                if db_track_id in db_track_id_is_known:
                    conn.execute(
                        "INSERT INTO item_to_container (item_pid, container_pid, physical_order, shuffle_order) "
                        "VALUES (?, ?, ?, NULL)",
                        (signed_id(db_track_id), signed_id(playlist_pid), order),
                    )

        conn.executescript(_INDEXES)
        conn.commit()
        return all_playlist_pids
    finally:
        conn.close()
