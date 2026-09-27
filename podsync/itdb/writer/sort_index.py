"""The master playlist's library indices: MHOD 52 (sorted order) and MHOD 53 (letter jumps).

For each browse view (songs, albums, artists, …) the firmware wants the track
positions pre-sorted, plus a table saying where each first letter starts so the
click wheel can jump.  Sorting ignores a leading English article and compares
Unicode-normalized, case-folded text.
"""

from __future__ import annotations

import itertools
import struct
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any

from podsync.itdb.spec.fields import without_article
from podsync.itdb.spec.layouts.strings import (
    MHOD_HEADER_SIZE,
    SORT_ALBUM,
    SORT_ALBUM_ARTIST,
    SORT_ARTIST,
    SORT_COMPOSER,
    SORT_EPISODE,
    SORT_GENRE,
    SORT_SEASON,
    SORT_SHOW,
    SORT_TITLE,
    write_mhod_header,
)

__all__ = ["BASE_SORT_TYPES", "VIDEO_SORT_TYPES", "write_library_indices", "write_mhod_type52", "write_mhod_type53"]

BASE_SORT_TYPES = [SORT_TITLE, SORT_ALBUM, SORT_ARTIST, SORT_GENRE, SORT_COMPOSER]
VIDEO_SORT_TYPES = [SORT_SHOW, SORT_SEASON, SORT_EPISODE]
_DIGIT_BUCKET = ord("0")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _collate(value: Any) -> str:
    return unicodedata.normalize("NFKD", without_article(_text(value))).casefold()


def _number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


class _View:
    """The sortable facets of one track (sort overrides win over the displayed values)."""

    def __init__(self, track: Any) -> None:
        get = lambda name: getattr(track, name, None)  # noqa: E731
        self.raw = {
            "title": get("sort_name") or get("title"),
            "album": get("sort_album") or get("album"),
            "artist": get("sort_artist") or get("artist"),
            "composer": get("sort_composer") or get("composer"),
            "show": get("sort_show") or get("show_name"),
            "genre": get("genre"),
            "album_artist": get("sort_album_artist") or get("album_artist") or get("sort_artist") or get("artist"),
        }
        self.text = {name: _collate(value) for name, value in self.raw.items()}
        self.disc, self.number = _number(get("disc_number")), _number(get("track_number"))
        self.season, self.episode = _number(get("season_number")), _number(get("episode_number"))

    def in_album(self, *leading: str) -> tuple:
        """Leading text facets, then album order: album, disc, track, title."""
        return (*(self.text[f] for f in leading), self.text["album"], self.disc, self.number, self.text["title"])


_SORT_KEYS: dict[int, Callable[[_View], tuple]] = {
    SORT_TITLE: lambda v: (v.text["title"],),
    SORT_ALBUM: lambda v: v.in_album(),
    SORT_ARTIST: lambda v: v.in_album("artist"),
    SORT_GENRE: lambda v: v.in_album("genre", "artist"),
    SORT_COMPOSER: lambda v: v.in_album("composer"),
    SORT_ALBUM_ARTIST: lambda v: v.in_album("album_artist"),
    SORT_SHOW: lambda v: (v.text["show"], v.season, v.episode, v.text["title"]),
    SORT_SEASON: lambda v: (v.season, v.episode, v.text["show"], v.text["title"]),
    SORT_EPISODE: lambda v: (v.episode, v.season, v.text["show"], v.text["title"]),
}
_LETTER_FACET = {
    SORT_TITLE: "title", SORT_ALBUM: "album", SORT_ARTIST: "artist", SORT_GENRE: "genre",
    SORT_COMPOSER: "composer", SORT_SHOW: "show", SORT_ALBUM_ARTIST: "album_artist",
}


def _jump_letter(view: _View, sort_type: int) -> int:
    """The wheel letter a track files under: first BMP letter/digit, uppercased; digits → '0'."""
    if sort_type in (SORT_SEASON, SORT_EPISODE):
        return _DIGIT_BUCKET
    for char in _text(view.raw[_LETTER_FACET.get(sort_type, "title")]):
        if not char.isalnum() or ord(char) > 0xFFFF:
            continue
        if char.isdigit():
            return _DIGIT_BUCKET
        upper = char.upper()
        return ord(upper) if len(upper) == 1 and ord(upper) <= 0xFFFF else ord(char)
    return _DIGIT_BUCKET


def write_mhod_type52(tracks: Sequence[Any], sort_type) -> tuple[bytes, list[tuple[int, int, int]]]:
    """The sorted-index MHOD and the ``(letter, first position, count)`` runs for MHOD 53."""
    views = [_View(track) for track in tracks]
    key = _SORT_KEYS.get(sort_type, _SORT_KEYS[SORT_TITLE])
    order = sorted(range(len(views)), key=lambda index: key(views[index]))

    letters = (_jump_letter(views[index], sort_type) for index in order)
    jumps: list[tuple[int, int, int]] = []
    position = 0
    for letter, run in itertools.groupby(letters):
        count = sum(1 for _ in run)
        jumps.append((letter, position, count))
        position += count

    body = struct.pack("<II", sort_type, len(order)) + bytes(40) + struct.pack(f"<{len(order)}I", *order)
    return write_mhod_header(52, MHOD_HEADER_SIZE + len(body)) + body, jumps


def write_mhod_type53(sort_type, jump_entries) -> bytes:
    body = struct.pack("<II", sort_type, len(jump_entries)) + bytes(8)
    body += b"".join(struct.pack("<HHII", letter & 0xFFFF, 0, start, count) for letter, start, count in jump_entries)
    return write_mhod_header(53, MHOD_HEADER_SIZE + len(body)) + body


def write_library_indices(tracks, capabilities=None) -> tuple[bytes, int]:
    """Every index pair the device uses; video views only for video-capable models."""
    if not tracks:
        return b"", 0
    kinds = list(BASE_SORT_TYPES)
    if capabilities is not None:
        if getattr(capabilities, "supports_video", False):
            kinds += VIDEO_SORT_TYPES
        kinds.append(SORT_ALBUM_ARTIST)
    chunks: list[bytes] = []
    for kind in kinds:
        index_mhod, jumps = write_mhod_type52(tracks, kind)
        chunks += [index_mhod, write_mhod_type53(kind, jumps)]
    return b"".join(chunks), len(chunks)
