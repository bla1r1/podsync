"""What kind of playlist a row is, from the MHYP kind word (+0x2A).

Bit 0 marks a podcast playlist, bit 8 a folder.  Rows may also carry the
older ``podcast_flag`` spelling or explicit ``is_podcast``/``is_folder`` booleans.
"""

from __future__ import annotations

from collections.abc import Mapping

PLAYLIST_KIND_PODCAST = 1 << 0
PLAYLIST_KIND_FOLDER = 1 << 8

__all__ = [
    "PLAYLIST_KIND_FOLDER",
    "PLAYLIST_KIND_PODCAST",
    "is_playlist_folder",
    "is_podcast_playlist",
    "playlist_kind_flags",
]


def _word(value: object) -> int:
    try:
        return int(value or 0) & 0xFFFF
    except (TypeError, ValueError, OverflowError):
        return 0


def playlist_kind_flags(value: object) -> int:
    """The 16-bit kind word of a row, a record-like mapping, or a raw number."""
    if not isinstance(value, Mapping):
        return _word(value)
    flags = _word(value.get("playlist_kind_flags", value.get("podcast_flag", 0)))
    if value.get("is_podcast") is True:
        flags |= PLAYLIST_KIND_PODCAST
    if value.get("is_folder") is True:
        flags |= PLAYLIST_KIND_FOLDER
    return flags


def is_podcast_playlist(value: object) -> bool:
    return bool(playlist_kind_flags(value) & PLAYLIST_KIND_PODCAST)


def is_playlist_folder(value: object) -> bool:
    return bool(playlist_kind_flags(value) & PLAYLIST_KIND_FOLDER)
