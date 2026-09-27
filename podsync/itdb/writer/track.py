"""Tracks: the :class:`TrackRecord` model and the ``mhit`` chunk it becomes.

Values are sanitized on the way out — clamped to their field widths, playback
windows made consistent with the track length, media kinds downgraded for
devices without video/podcast support — so a bad record never produces a
database the firmware chokes on.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Any

from podsync.itdb.spec.codes import (
    AUDIO_FORMAT_FLAG_DEFAULT,
    AUDIO_FORMAT_FLAG_MAP,
    FILETYPE_CODES,
    MEDIA_TYPE_AUDIO,
    MEDIA_TYPE_MUSIC_VIDEO,
    MEDIA_TYPE_PODCAST,
    MEDIA_TYPE_TV_SHOW,
    MEDIA_TYPE_VIDEO,
    MEDIA_TYPE_VIDEO_PODCAST,
)
from podsync.itdb.spec.layouts.track import MHIT_HEADER_SIZE, mhit_header_size_for_version
from podsync.itdb.writer._build import record_bytes
from podsync.itdb.writer.strings import track_string_objects

__all__ = ["TrackRecord", "generate_db_id", "generate_db_track_id", "normalize_filetype", "write_mhit"]

U32_MAX = 0xFFFF_FFFF
_VIDEO_KINDS = frozenset({MEDIA_TYPE_VIDEO, MEDIA_TYPE_MUSIC_VIDEO, MEDIA_TYPE_TV_SHOW, MEDIA_TYPE_VIDEO_PODCAST})
_PLAIN_VIDEO_KINDS = (MEDIA_TYPE_VIDEO, MEDIA_TYPE_MUSIC_VIDEO, MEDIA_TYPE_TV_SHOW)
_SAMPLE_RATE_FLOOR, _SAMPLE_RATE_CEILING, _SAMPLE_RATE_FALLBACK = 8000, 48000, 44100
# Order of the per-string "has a sort override" bytes at MHIT +0x134.
_SORT_OVERRIDES = ("sort_name", "sort_album", "sort_artist", "sort_album_artist", "sort_composer", "sort_show")


@dataclass
class TrackRecord:
    """Everything written for one track: ``mhit`` fields plus its string ``mhod`` values."""

    title: str
    location: str  # ":iPod_Control:Music:Fxx:NAME.ext"
    # file
    size: int = 0
    length: int = 0  # milliseconds
    filetype: str = "mp3"
    bitrate: int = 0
    sample_rate: int = 44100
    vbr: bool = False
    # descriptive
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None
    composer: str | None = None
    comment: str | None = None
    year: int = 0
    track_number: int = 0
    total_tracks: int = 0
    disc_number: int = 1
    total_discs: int = 1
    bpm: int = 0
    compilation_flag: bool = False
    # playback
    rating: int = 0
    play_count: int = 0
    play_count_2: int = 0
    skip_count: int = 0
    volume: int = 0
    start_time: int = 0
    stop_time: int = 0
    sound_check: int = 0
    bookmark_time: int = 0
    checked_flag: int = 0
    # gapless
    gapless_data: int = 0
    gapless_track_flag: int = 0
    gapless_album_flag: int = 0
    pregap: int = 0
    postgap: int = 0
    sample_count: int = 0
    encoder_flag: int = 0
    # flags
    skip_when_shuffling: bool = False
    remember_position: bool = False
    podcast_flag: int = 0
    movie_file_flag: int = 0
    played_mark: int = -1  # -1: derive from play_count
    explicit_flag: int = 0
    purchased_aac_flag: int = 0
    has_lyrics: bool = False
    lyrics: str | None = None
    eq_setting: str | None = None
    # timestamps (Unix seconds)
    date_added: int = 0
    date_released: int = 0
    last_modified: int = 0
    last_played: int = 0
    last_skipped: int = 0
    # identity on the device
    track_id: int = 0
    db_track_id: int = 0
    media_type: int = MEDIA_TYPE_AUDIO
    season_number: int = 0
    episode_number: int = 0
    artwork_count: int = 0
    artwork_size: int = 0
    mhii_link: int = 0
    album_id: int = 0
    source_path: str | None = None
    source_relative_path: str | None = None
    # sort overrides
    sort_artist: str | None = None
    sort_name: str | None = None
    sort_album: str | None = None
    sort_album_artist: str | None = None
    sort_composer: str | None = None
    grouping: str | None = None
    keywords: str | None = None
    # podcast
    podcast_enclosure_url: str | None = None
    podcast_rss_url: str | None = None
    category: str | None = None
    # video
    description: str | None = None
    subtitle: str | None = None
    show_name: str | None = None
    episode_id: str | None = None
    network_name: str | None = None
    sort_show: str | None = None
    show_locale: str | None = None
    filetype_desc: str | None = None  # the "Kind" label, e.g. "MPEG audio file"
    # carried through unchanged
    user_id: int = 0
    app_rating: int = 0
    mpeg_audio_type: int = 0
    date_added_to_itunes: int = 0
    store_track_id: int = 0
    store_encoder_version: int = 0
    store_artist_id: int = 0
    store_album_id: int = 0
    store_content_flag: int = 0
    # assigned by the writer
    artist_id: int = 0
    composer_id: int = 0
    chapter_data: dict | None = None
    _iop_artwork_sync_hint: str = ""

    @property
    def db_id(self) -> int:
        return self.db_track_id

    @db_id.setter
    def db_id(self, value: int) -> None:
        self.db_track_id = value


def generate_db_track_id() -> int:
    """A random 64-bit database id for a new track."""
    return random.getrandbits(64)


generate_db_id = generate_db_track_id


def _int(value: Any) -> int:
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded(value: Any, bits: int, *, signed_floor: int | None = None) -> int:
    low = signed_floor if signed_floor is not None else 0
    return min(max(_int(value), low), (1 << bits) - 1)


def u8(value: Any) -> int:
    """*value* bounded to an unsigned 8-bit field."""
    return _bounded(value, 8)


def u16(value: Any) -> int:
    """*value* bounded to an unsigned 16-bit field."""
    return _bounded(value, 16)


def u32(value: Any) -> int:
    """*value* bounded to an unsigned 32-bit field."""
    return _bounded(value, 32)


def u64(value: Any) -> int:
    """*value* bounded to an unsigned 64-bit field."""
    return _bounded(value, 64)


def _percent(value: Any) -> int:
    return min(max(_int(value), 0), 100)


def normalize_filetype(filetype: Any) -> str:
    """Lower-case extension without the dot; ``mp3`` when unknown."""
    return str(filetype or "").strip().lower().lstrip(".") or "mp3"


def _device_media_type(media_type: int, capabilities: Any) -> int:
    """Downgrade kinds the device cannot play: video → audio, podcast → audio."""
    if capabilities is None:
        return media_type
    if not getattr(capabilities, "supports_video", True):
        if media_type in _PLAIN_VIDEO_KINDS:
            media_type = MEDIA_TYPE_AUDIO
        elif media_type == MEDIA_TYPE_VIDEO_PODCAST:
            media_type = MEDIA_TYPE_PODCAST
    if not getattr(capabilities, "supports_podcast", True) and media_type in (MEDIA_TYPE_PODCAST, MEDIA_TYPE_VIDEO_PODCAST):
        media_type = MEDIA_TYPE_AUDIO
    return media_type


def _playback_window(length: int, start: int, stop: int, bookmark: int) -> tuple[int, int, int]:
    """Start/stop/bookmark that make sense for a track of *length* ms (0 = unknown length)."""
    if length <= 0:
        return start, stop, bookmark
    start_bad = start >= length
    if start_bad:
        start = 0
    stop = min(stop, length)
    if stop and (stop <= start or start_bad):
        stop = 0
    return start, stop, min(bookmark, length)


def _checked_location(location: Any) -> str:
    text = str(location or "")
    if not text:
        raise ValueError("track iPod location is empty")
    if not text.startswith(":iPod_Control:"):
        raise ValueError(f"track iPod location must be an iPod path, got {text!r}")
    return text


def _sample_rate(value: Any) -> int:
    hz = _int(value)
    return _SAMPLE_RATE_FALLBACK if hz < _SAMPLE_RATE_FLOOR else min(hz, _SAMPLE_RATE_CEILING)


def _unplayed_mark(played_mark: Any, play_count: int) -> int:
    mark = _int(played_mark)
    if mark >= 0:
        return u8(mark)
    return 0x01 if play_count > 0 else 0x02


def write_mhit(track: TrackRecord, track_id: int, db_id_2: int = 0, capabilities=None, db_version: int = 0) -> bytes:
    """Serialize one track.

    Missing ``db_track_id`` and ``date_added`` are generated and written back to
    the record so playlists and the album list refer to the same values.
    """
    header_size = mhit_header_size_for_version(db_version) if db_version else MHIT_HEADER_SIZE

    if not u64(track.db_track_id):
        track.db_track_id = generate_db_track_id()
    db_track_id = u64(track.db_track_id)
    if not u32(track.date_added):
        track.date_added = int(time.time())
    date_added = u32(track.date_added)

    filetype = normalize_filetype(track.filetype)
    title = str(track.title or "").strip() or "Unknown Title"
    location = _checked_location(track.location)
    sample_rate = _sample_rate(track.sample_rate)
    length = u32(track.length)
    start, stop, bookmark = _playback_window(length, u32(track.start_time), u32(track.stop_time), u32(track.bookmark_time))
    media_type = _device_media_type(u32(track.media_type), capabilities)
    play_count = u32(track.play_count)
    artwork_count = u16(track.artwork_count)
    gapless = capabilities is None or getattr(capabilities, "supports_gapless", True)
    size = u32(track.size)

    body, body_count = track_string_objects(track, title=title, location=location)
    values: dict[str, Any] = {
        # identity and file
        "child_count": body_count,
        "track_id": u32(track_id),
        "visible": 1,
        "db_track_id": db_track_id,
        "db_track_id_2": db_track_id,
        "library_db_link": u64(db_id_2),
        "filetype": FILETYPE_CODES.get(filetype, FILETYPE_CODES["mp3"]),
        "is_mp3": int(filetype == "mp3"),
        "audio_format_flag": AUDIO_FORMAT_FLAG_MAP.get(filetype, AUDIO_FORMAT_FLAG_DEFAULT),
        "size": size,
        "size_2": size,
        "length": length,
        "media_type": media_type,
        "is_movie": u8(track.movie_file_flag) if track.movie_file_flag else int(media_type in _VIDEO_KINDS),
        # audio
        "is_vbr": int(bool(track.vbr)),
        "bitrate": u32(track.bitrate),
        "sample_rate": sample_rate,
        "sample_rate_float": float(sample_rate),
        "volume": min(max(_int(track.volume), -255), 255),
        "sound_check": u32(track.sound_check),
        "start_time": start,
        "stop_time": stop,
        "mpeg_audio_type": u16(track.mpeg_audio_type),
        "encoder": u32(track.encoder_flag),
        # gapless playback (omitted for devices that cannot use it)
        "pregap": u32(track.pregap) if gapless else 0,
        "postgap": u32(track.postgap) if gapless else 0,
        "sample_count": u64(track.sample_count) if gapless else 0,
        "gapless_payload_bytes": u32(track.gapless_data) if gapless else 0,
        "gapless_track_flag": u16(track.gapless_track_flag) if gapless else 0,
        "gapless_album_flag": u16(track.gapless_album_flag) if gapless else 0,
        # numbering
        "track_number": u32(track.track_number),
        "total_tracks": u32(track.total_tracks),
        "disc_number": u32(track.disc_number),
        "total_discs": u32(track.total_discs),
        "year": u32(track.year),
        "bpm": u16(track.bpm),
        "season_number": u32(track.season_number),
        "episode_number": u32(track.episode_number),
        # flags
        "compilation_flag": int(bool(track.compilation_flag)),
        "checked_flag": u8(track.checked_flag),
        "explicit_flag": u8(track.explicit_flag),
        "purchased_aac_flag": u8(track.purchased_aac_flag),
        "skip_when_shuffling": int(bool(track.skip_when_shuffling)),
        "remember_position": int(bool(track.remember_position)),
        "podcast_now_playing": u8(track.podcast_flag),
        "has_lyrics_flag": int(bool(track.has_lyrics or track.lyrics)),
        "unplayed_mark": _unplayed_mark(track.played_mark, play_count),
        "sort_mhod_indicators": bytes(0x81 if getattr(track, name, None) else 0x80 for name in _SORT_OVERRIDES) + b"\x00\x00",
        # statistics
        "rating": _percent(track.rating),
        "app_rating": _percent(track.app_rating),
        "play_count": play_count,
        "pending_play_count": u32(track.play_count_2),
        "skip_count": u32(track.skip_count),
        "bookmark_time": bookmark,
        "user_id": u32(track.user_id),
        # timestamps
        "date_added": date_added,
        "last_modified": u32(track.last_modified) or date_added,
        "last_played": u32(track.last_played),
        "last_skipped": u32(track.last_skipped),
        "date_released": u32(track.date_released),
        "date_added_to_itunes": u32(track.date_added_to_itunes),
        # artwork
        "artwork_count": artwork_count,
        "artwork_size": u32(track.artwork_size),
        "has_artwork": 1 if artwork_count > 0 else 2,
        "artwork_link": u32(track.mhii_link),
        # store and library cross-references
        "store_track_id": u32(track.store_track_id),
        "store_encoder_version": u32(track.store_encoder_version),
        "store_artist_id": u32(track.store_artist_id),
        "store_album_id": u32(track.store_album_id),
        "store_content_flag": u32(track.store_content_flag),
        "album_id": u32(track.album_id),
        "artist_link": u32(track.artist_id),
        "composer_id": u32(track.composer_id),
    }
    return record_bytes(b"mhit", header_size, values, body)
