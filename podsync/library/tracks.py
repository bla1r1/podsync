"""Translate parsed track rows into :class:`TrackRecord` objects and back into rule views.

A *row* is the flattened dict produced by :func:`podsync.library.database.load_device_library`.
A *rule view* is the small dict the smart-playlist engine reads (see
:mod:`podsync.library.smart`).  Both directions are pure: no I/O, no logging.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from podsync.itdb.writer.track import TrackRecord

__all__ = ["format_for_extension", "record_from_row", "rule_view_of"]

# Container codes the writer understands directly (see podsync.itdb.spec.fields).
_KNOWN_FORMATS = frozenset({"mp3", "m4a", "m4p", "m4b", "m4v", "mp4", "wav", "aif", "aiff", "aac"})

# Description-style labels ("Protected AAC audio file", "MOV", ...) are matched by
# case-sensitive substring, first hit wins.  Order matters: "AAC" must beat "Protected".
_LABEL_HINTS: tuple[tuple[str, str], ...] = (
    ("AAC", "m4a"), ("M4A", "m4a"), ("Lossless", "m4a"), ("Protected", "m4p"),
    ("Audiobook", "m4b"), ("WAV", "wav"), ("AIFF", "aiff"), ("M4V", "m4v"),
    ("MP4", "mp4"), ("MOV", "m4v"),
)

_EXTENSION_ALIASES = {"aac": "m4a", "alac": "m4a", "mov": "m4v"}


def format_for_extension(extension: str) -> str:
    """Map a file extension (with or without dots) to the container code the iPod expects."""
    ext = str(extension or "").casefold().lstrip(".")
    if not ext:
        return "mp3"
    return _EXTENSION_ALIASES.get(ext, ext)


def _container_code(raw: object) -> str:
    """Resolve the stored four-character format (or a free-form label) to a container code."""
    label = raw if isinstance(raw, str) else ""
    code = label.strip().casefold()
    if code in _KNOWN_FORMATS:
        return code
    return next((target for hint, target in _LABEL_HINTS if hint in label), "mp3")


_MISSING = object()


def _lookup(row: Mapping[str, Any], keys: tuple[str, ...], default: Any) -> Any:
    """Return the first present key's value (presence, not truthiness), else *default*."""
    for key in keys:
        value = row.get(key, _MISSING)
        if value is not _MISSING:
            return value
    return default


def _first_truthy(row: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    return next((row[key] for key in keys if row.get(key)), None)


# (record attribute, row keys in priority order, default, optional converter)
_Spec = tuple[str, tuple[str, ...], Any, Callable[[Any], Any] | None]
_ROW_TO_RECORD: tuple[_Spec, ...] = (
    # identity and file
    ("title", ("title",), "Unknown", None),
    ("location", ("location",), "", None),
    ("size", ("size",), 0, None),
    ("length", ("length",), 0, None),
    ("filetype", ("filetype",), "MP3", _container_code),
    ("filetype_desc", ("kind",), None, None),
    ("db_track_id", ("db_track_id", "db_id"), 0, None),
    ("media_type", ("media_type",), 1, None),
    ("movie_file_flag", ("is_movie",), 0, None),
    # audio format
    ("bitrate", ("bitrate",), 0, None),
    ("sample_rate", ("sample_rate",), 44100, None),
    ("vbr", ("is_vbr",), 0, bool),
    ("mpeg_audio_type", ("mpeg_audio_type", "unk144"), 0, None),
    ("encoder_flag", ("encoder",), 0, None),
    ("volume", ("volume",), 0, None),
    ("sound_check", ("sound_check",), 0, None),
    ("start_time", ("start_time",), 0, None),
    ("stop_time", ("stop_time",), 0, None),
    ("gapless_data", ("gapless_payload_bytes",), 0, None),
    ("gapless_track_flag", ("gapless_track_flag",), 0, None),
    ("gapless_album_flag", ("gapless_album_flag",), 0, None),
    ("pregap", ("pregap",), 0, None),
    ("postgap", ("postgap",), 0, None),
    ("sample_count", ("sample_count",), 0, None),
    # descriptive text (absent keys stay None, never "")
    ("artist", ("artist",), None, None),
    ("album", ("album",), None, None),
    ("album_artist", ("album_artist",), None, None),
    ("genre", ("genre",), None, None),
    ("composer", ("composer",), None, None),
    ("comment", ("comment",), None, None),
    ("grouping", ("grouping",), None, None),
    ("lyrics", ("lyrics",), None, None),
    ("eq_setting", ("eq_setting",), None, None),
    ("sort_artist", ("sort_artist",), None, None),
    ("sort_album", ("sort_album",), None, None),
    ("sort_album_artist", ("sort_album_artist",), None, None),
    ("sort_composer", ("sort_composer",), None, None),
    ("sort_show", ("sort_show",), None, None),
    ("show_name", ("show",), None, None),
    ("episode_id", ("episode",), None, None),
    ("description", ("description",), None, None),
    ("subtitle", ("subtitle",), None, None),
    ("network_name", ("network",), None, None),
    ("show_locale", ("show_locale",), None, None),
    ("keywords", ("keywords",), None, None),
    ("podcast_enclosure_url", ("enclosure_url",), None, None),
    ("podcast_rss_url", ("feed_url",), None, None),
    ("category", ("category",), None, None),
    ("chapter_data", ("chapter_data",), None, None),
    # numbering
    ("year", ("year",), 0, None),
    ("track_number", ("track_number",), 0, None),
    ("total_tracks", ("total_tracks",), 0, None),
    ("disc_number", ("disc_number",), 1, None),
    ("total_discs", ("total_discs",), 1, None),
    ("bpm", ("bpm",), 0, None),
    ("season_number", ("season_number",), 0, None),
    ("episode_number", ("episode_number",), 0, None),
    # flags
    ("compilation_flag", ("compilation_flag", "compilation"), 0, bool),
    ("skip_when_shuffling", ("skip_when_shuffling",), 0, bool),
    ("remember_position", ("remember_position",), 0, bool),
    ("checked_flag", ("checked_flag", "checked"), 0, None),
    ("explicit_flag", ("explicit_flag",), 0, None),
    ("purchased_aac_flag", ("purchased_aac_flag",), 0, None),
    ("has_lyrics", ("has_lyrics_flag",), 0, bool),
    ("played_mark", ("unplayed_mark",), -1, None),
    ("podcast_flag", ("podcast_now_playing",), 0, None),
    # play statistics (already merged with the Play Counts file by the loader)
    ("rating", ("rating",), 0, None),
    ("app_rating", ("app_rating",), 0, None),
    ("play_count", ("play_count",), 0, None),
    ("play_count_2", ("pending_play_count",), 0, None),
    ("skip_count", ("skip_count",), 0, None),
    ("bookmark_time", ("bookmark_time",), 0, None),
    ("user_id", ("user_id",), 0, None),
    # timestamps (Unix seconds)
    ("date_added", ("date_added",), 0, None),
    ("date_released", ("date_released",), 0, None),
    ("last_played", ("last_played",), 0, None),
    ("last_skipped", ("last_skipped",), 0, None),
    ("last_modified", ("last_modified",), 0, None),
    ("date_added_to_itunes", ("date_added_to_itunes",), 0, None),
    # artwork and store identity
    ("artwork_count", ("artwork_count",), 0, None),
    ("artwork_size", ("artwork_size",), 0, None),
    ("mhii_link", ("artwork_link",), 0, None),
    ("store_track_id", ("store_track_id",), 0, None),
    ("store_encoder_version", ("store_encoder_version",), 0, None),
    ("store_artist_id", ("store_artist_id",), 0, None),
    ("store_album_id", ("store_album_id",), 0, None),
    ("store_content_flag", ("store_content_flag",), 0, None),
    # library cross-references (re-assigned by the writer anyway)
    ("album_id", ("album_id",), 0, None),
    ("artist_id", ("artist_link", "artist_id"), 0, None),
    ("composer_id", ("composer_id",), 0, None),
)

# Host-side paths: an empty value falls through to the next spelling.
_ROW_TO_RECORD_TRUTHY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("source_path", ("source_path",)),
    ("source_relative_path", ("source_relative_path",)),
    ("sort_name", ("sort_title",)),
)


def record_from_row(row: dict) -> TrackRecord:
    """Build a :class:`TrackRecord` from a parsed track row.

    ``track_id`` is left for the writer to assign.  The MHOD-6 description
    (``kind``) is kept separate from the container code so localized labels
    survive a read → write cycle unchanged.
    """
    values: dict[str, Any] = {}
    for attribute, keys, default, convert in _ROW_TO_RECORD:
        value = _lookup(row, keys, default)
        values[attribute] = convert(value) if convert is not None else value
    for attribute, keys in _ROW_TO_RECORD_TRUTHY:
        values[attribute] = _first_truthy(row, keys)
    return TrackRecord(**values)


# Rule-view keys that mirror a record attribute one to one.
_TEXT_VIEW = {
    "title": "title", "album": "album", "artist": "artist", "genre": "genre",
    "comment": "comment", "composer": "composer", "album_artist": "album_artist",
    "sort_title": "sort_name", "sort_album": "sort_album", "sort_artist": "sort_artist",
    "sort_album_artist": "sort_album_artist", "sort_composer": "sort_composer",
    "sort_show": "sort_show", "grouping": "grouping", "show": "show_name",
    "description": "description", "category": "category",
}
_NUMBER_VIEW = {
    "bitrate": "bitrate", "sample_rate": "sample_rate", "year": "year",
    "track_number": "track_number", "size": "size", "length": "length",
    "play_count": "play_count", "disc_number": "disc_number", "rating": "rating",
    "bpm": "bpm", "skip_count": "skip_count", "date_added": "date_added",
    "last_modified": "last_modified", "last_played": "last_played",
    "last_skipped": "last_skipped", "artwork_count": "artwork_count",
    "artwork_link": "mhii_link", "purchased_flag": "purchased_aac_flag",
    "media_type": "media_type", "checked_flag": "checked_flag",
    "season_number": "season_number", "podcast_flag": "podcast_flag",
}


def rule_view_of(record: TrackRecord) -> dict:
    """Project a record onto the keys the smart-playlist engine evaluates.

    Text values default to ``""`` so string rules never see ``None``; ``track_id``
    carries the database id, which is what evaluated playlists store.
    """
    view: dict[str, Any] = {"track_id": record.db_track_id}
    view.update({key: getattr(record, attr) or "" for key, attr in _TEXT_VIEW.items()})
    view.update({key: getattr(record, attr) for key, attr in _NUMBER_VIEW.items()})
    view["filetype"] = record.filetype_desc or record.filetype or ""
    view["compilation_flag"] = int(bool(record.compilation_flag))
    view["has_artwork"] = bool(record.artwork_count or record.mhii_link)
    # Everything being evaluated already lives on this iPod ("on this computer" bit).
    view["location_kind"] = 1
    return view
