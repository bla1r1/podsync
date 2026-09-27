"""Write text data objects: UTF-16 strings, UTF-8 podcast URLs and chapter lists.

Firmware reads strings into fixed buffers, so over-long values are cut at a
character boundary instead of being written in full.
"""

from __future__ import annotations

import logging
import struct
import unicodedata
from typing import Any

from podsync.itdb.spec.codes import (
    MHOD_TYPE_ALBUM,
    MHOD_TYPE_ALBUM_ARTIST,
    MHOD_TYPE_ARTIST,
    MHOD_TYPE_CATEGORY,
    MHOD_TYPE_CHAPTER_DATA,
    MHOD_TYPE_COMMENT,
    MHOD_TYPE_COMPOSER,
    MHOD_TYPE_DESCRIPTION,
    MHOD_TYPE_EPISODE_ID,
    MHOD_TYPE_EQ_SETTING,
    MHOD_TYPE_FILETYPE,
    MHOD_TYPE_GENRE,
    MHOD_TYPE_GROUPING,
    MHOD_TYPE_KEYWORDS,
    MHOD_TYPE_LOCATION,
    MHOD_TYPE_LYRICS,
    MHOD_TYPE_NETWORK_NAME,
    MHOD_TYPE_PODCAST_ENCLOSURE_URL,
    MHOD_TYPE_PODCAST_RSS_URL,
    MHOD_TYPE_SHOW_LOCALE,
    MHOD_TYPE_SHOW_NAME,
    MHOD_TYPE_SORT_ALBUM,
    MHOD_TYPE_SORT_ALBUM_ARTIST,
    MHOD_TYPE_SORT_ARTIST,
    MHOD_TYPE_SORT_COMPOSER,
    MHOD_TYPE_SORT_NAME,
    MHOD_TYPE_SORT_SHOW,
    MHOD_TYPE_SUBTITLE,
    MHOD_TYPE_TITLE,
)
from podsync.itdb.spec.layouts.strings import (
    CHAP_ATOM,
    HEDR_ATOM,
    HEDR_SIZE,
    MHOD_HEADER_SIZE,
    NAME_ATOM,
    SEAN_ATOM,
    write_mhod_header,
)
from podsync.itdb.writer._build import clamp_u32

__all__ = [
    "MHOD_LONG_TEXT_MAX_UTF16_BYTES",
    "MHOD_STRING_MAX_UTF16_BYTES",
    "MHOD_URL_MAX_UTF8_BYTES",
    "build_chapter_blob",
    "track_string_objects",
    "write_mhod_chapter_data",
    "write_mhod_podcast_url",
    "write_mhod_string",
]

logger = logging.getLogger(__name__)

MHOD_STRING_MAX_UTF16_BYTES = 4096
MHOD_LONG_TEXT_MAX_UTF16_BYTES = 8192  # comments, descriptions and lyrics get more room
MHOD_URL_MAX_UTF8_BYTES = 4096
_LONG_TEXT_KINDS = frozenset({MHOD_TYPE_COMMENT, MHOD_TYPE_DESCRIPTION, MHOD_TYPE_LYRICS})
_URL_KINDS = (MHOD_TYPE_PODCAST_ENCLOSURE_URL, MHOD_TYPE_PODCAST_RSS_URL)
_ENCODING_UTF16 = 1
_MAX_CHAPTERS = 500


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _fit(encoded: bytes, limit: int, codec: str) -> bytes:
    """Cut *encoded* to at most *limit* bytes without splitting a character."""
    if len(encoded) <= limit:
        return encoded
    return encoded[:limit].decode(codec, errors="ignore").encode(codec)


def write_mhod_string(mhod_type: int, value: Any, unk_0x20: int = 1, unk_0x24: int = 0) -> bytes:
    """A UTF-16LE string MHOD, or ``b""`` for an empty value."""
    text = _text(value)
    if not text:
        return b""
    limit = MHOD_LONG_TEXT_MAX_UTF16_BYTES if mhod_type in _LONG_TEXT_KINDS else MHOD_STRING_MAX_UTF16_BYTES
    raw = text.encode("utf-16-le", errors="replace")
    data = _fit(raw, limit - limit % 2, "utf-16-le")
    if len(data) < len(raw):
        logger.debug("Truncated MHOD type %d string to %d bytes", mhod_type, len(data))
    if not data:
        return b""
    subheader = struct.pack("<IIII", _ENCODING_UTF16, len(data), unk_0x20, unk_0x24)
    return write_mhod_header(mhod_type, MHOD_HEADER_SIZE + len(subheader) + len(data)) + subheader + data


def write_mhod_podcast_url(mhod_type: int, url: Any) -> bytes:
    """A podcast enclosure/feed URL MHOD: bare UTF-8, no string sub-header."""
    if mhod_type not in _URL_KINDS:
        raise ValueError(f"write_mhod_podcast_url only supports types 15 and 16, got {mhod_type}")
    raw = _text(url).encode("utf-8", errors="replace")
    data = _fit(raw, MHOD_URL_MAX_UTF8_BYTES, "utf-8")
    if len(data) < len(raw):
        logger.debug("Truncated podcast URL MHOD to %d bytes", len(data))
    return write_mhod_header(mhod_type, MHOD_HEADER_SIZE + len(data)) + data if data else b""


# ── chapters (MHOD 17) ──────────────────────────────────────────────


def _name_atom(title: str) -> bytes:
    encoded = title.encode("utf-16-be", errors="replace")
    units = min(len(encoded) // 2, 0xFFFF)
    encoded = encoded[:units * 2]
    return struct.pack(">I4sIIIH", 22 + len(encoded), NAME_ATOM, 1, 0, 0, units) + encoded


def build_chapter_blob(chapters, unk024: int = 0, unk028: int = 0, unk032: int = 0) -> bytes:
    """The MHOD 17 body: preamble, then ``sean`` holding one ``chap`` per chapter and a ``hedr``."""
    atoms = []
    for chapter in chapters or []:
        if isinstance(chapter, dict):
            name = _name_atom(_text(chapter.get("title")))
            atoms.append(struct.pack(">I4sIII", 20 + len(name), CHAP_ATOM, clamp_u32(chapter.get("startpos")), 1, 0) + name)
    if not atoms:
        return b""
    children = b"".join(atoms) + struct.pack(">I4sIIIIII", HEDR_SIZE, HEDR_ATOM, 1, 0, 0, 0, 0, 1)
    sean = struct.pack(">I4sIII", 20 + len(children), SEAN_ATOM, 1, len(atoms) + 1, 0)
    preamble = struct.pack("<III", clamp_u32(unk024), clamp_u32(unk028), clamp_u32(unk032))
    return preamble + sean + children


def write_mhod_chapter_data(chapters: list[dict], unk024: int = 0, unk028: int = 0, unk032: int = 0) -> bytes:
    body = build_chapter_blob(chapters, unk024, unk028, unk032)
    return write_mhod_header(MHOD_TYPE_CHAPTER_DATA, MHOD_HEADER_SIZE + len(body)) + body if body else b""


def _looks_corrupt(title: str) -> bool:
    if "\x00" in title or title.count("�") > max(1, len(title) // 10):
        return True
    return any(ch not in "\t\r\n" and unicodedata.category(ch) == "Cc" for ch in title)


def _clean_chapters(chapters: Any) -> list[dict] | None:
    """Chapters safe to write, or ``None`` if any of them looks implausible.

    Start positions must be ints strictly increasing within u32; titles must not
    look like garbage; missing titles become "Chapter N".
    """
    if not isinstance(chapters, list) or not chapters or len(chapters) > _MAX_CHAPTERS:
        return None
    cleaned: list[dict] = []
    previous = -1
    for number, chapter in enumerate(chapters, 1):
        start = chapter.get("startpos") if isinstance(chapter, dict) else None
        plausible = (
            isinstance(start, int) and not isinstance(start, bool)
            and 0 <= start < 0xFFFF_FFFF and start > previous
        )
        title = chapter.get("title") if plausible else None
        title = title.strip() if isinstance(title, str) else ""
        if not plausible or (title and _looks_corrupt(title)):
            logger.debug("Skipping implausible chapter data MHOD")
            return None
        cleaned.append({**chapter, "startpos": start, "title": title or f"Chapter {number}"})
        previous = start
    return cleaned


# ── the per-track set ───────────────────────────────────────────────

# (record attribute, MHOD kind) in the order iTunes writes them.
_TRACK_STRINGS = (
    ("artist", MHOD_TYPE_ARTIST), ("album", MHOD_TYPE_ALBUM), ("genre", MHOD_TYPE_GENRE),
    ("album_artist", MHOD_TYPE_ALBUM_ARTIST), ("composer", MHOD_TYPE_COMPOSER), ("comment", MHOD_TYPE_COMMENT),
    ("filetype_desc", MHOD_TYPE_FILETYPE), ("category", MHOD_TYPE_CATEGORY),
    ("description", MHOD_TYPE_DESCRIPTION), ("subtitle", MHOD_TYPE_SUBTITLE),
    ("show_name", MHOD_TYPE_SHOW_NAME), ("episode_id", MHOD_TYPE_EPISODE_ID),
    ("network_name", MHOD_TYPE_NETWORK_NAME), ("keywords", MHOD_TYPE_KEYWORDS),
    ("sort_artist", MHOD_TYPE_SORT_ARTIST), ("sort_name", MHOD_TYPE_SORT_NAME),
    ("sort_album", MHOD_TYPE_SORT_ALBUM), ("sort_album_artist", MHOD_TYPE_SORT_ALBUM_ARTIST),
    ("sort_composer", MHOD_TYPE_SORT_COMPOSER), ("sort_show", MHOD_TYPE_SORT_SHOW),
    ("show_locale", MHOD_TYPE_SHOW_LOCALE), ("grouping", MHOD_TYPE_GROUPING),
)
_TRACK_URLS = (("podcast_enclosure_url", MHOD_TYPE_PODCAST_ENCLOSURE_URL), ("podcast_rss_url", MHOD_TYPE_PODCAST_RSS_URL))
_TRACK_TRAILING = (("eq_setting", MHOD_TYPE_EQ_SETTING), ("lyrics", MHOD_TYPE_LYRICS))


def track_string_objects(record: Any, *, title: str, location: str) -> tuple[bytes, int]:
    """All data objects of one track and their count; empty values produce nothing."""
    objects = [write_mhod_string(MHOD_TYPE_TITLE, title), write_mhod_string(MHOD_TYPE_LOCATION, location)]
    objects += [write_mhod_string(kind, value) for attr, kind in _TRACK_STRINGS if (value := getattr(record, attr, None))]
    objects += [write_mhod_podcast_url(kind, value) for attr, kind in _TRACK_URLS if (value := getattr(record, attr, None))]
    objects += [write_mhod_string(kind, value) for attr, kind in _TRACK_TRAILING if (value := getattr(record, attr, None))]
    chapter_data = getattr(record, "chapter_data", None)
    if isinstance(chapter_data, dict) and chapter_data.get("chapters"):
        chapters = _clean_chapters(chapter_data["chapters"])
        if chapters:
            objects.append(write_mhod_chapter_data(
                chapters, chapter_data.get("unk024", 0), chapter_data.get("unk028", 0), chapter_data.get("unk032", 0),
            ))
    present = [chunk for chunk in objects if chunk]
    return b"".join(present), len(present)
