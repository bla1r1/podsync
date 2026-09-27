"""``mhod`` — the data object attached to tracks, playlists, entries and albums.

The 24-byte header only says which kind of object follows; the body layout
depends on the kind (UTF-16/UTF-8 strings, chapter atoms, smart-playlist data,
library indices, ...).  Smart-playlist bodies are described in
:mod:`podsync.itdb.spec.smart_fields`.
"""

from __future__ import annotations

import struct

from podsync.itdb.spec.layouts._table import record

MHOD_HEADER_SIZE = 24
MHOD_STRING_SUBHEADER_OFFSET = 0x18  # encoding, byte length, two unknown words
MHOD_STRING_SUBHEADER_SIZE = 16
MHOD_STRING_DATA_OFFSET = MHOD_STRING_SUBHEADER_OFFSET + MHOD_STRING_SUBHEADER_SIZE

MHOD52_BODY_HEADER_SIZE = 48  # library index: sort kind, count, padding
MHOD53_BODY_HEADER_SIZE = 16  # jump table header
MHOD53_ENTRY_SIZE = 12
MHOD100_POSITION_BODY_SIZE = 20

# Chapter data (MHOD 17) is a QuickTime-style atom tree.
CHAPTER_PREAMBLE_SIZE = 12
SEAN_ATOM, CHAP_ATOM, NAME_ATOM, HEDR_ATOM = b"sean", b"chap", b"name", b"hedr"
HEDR_SIZE = 28

MHOD_FIELDS = record("mhod", """
    mhod_type   u32  0x0C  required
    unk0x10     u32  0x10
    unk0x14     u32  0x14
""")

# ── which body a kind carries ───────────────────────────────────────

STRING_MHOD_TYPES = frozenset(
    [*range(1, 15), *range(18, 32), *range(33, 45), *range(200, 205), 300]
)
PODCAST_URL_MHOD_TYPES = frozenset({15, 16})  # UTF-8, no string sub-header
CHAPTER_DATA_MHOD_TYPES = frozenset({17})
BINARY_BLOB_MHOD_TYPES = frozenset({32})
NON_STRING_MHOD_TYPES = frozenset({50, 51, 52, 53, 55, 100, 102})


def write_mhod_header(mhod_type: int, total_length: int, unk0x10: int = 0, unk0x14: int = 0) -> bytes:
    """The fixed 24-byte MHOD header for a body of ``total_length - 24`` bytes."""
    return struct.pack("<4sIIIII", b"mhod", MHOD_HEADER_SIZE, total_length, mhod_type, unk0x10, unk0x14)


def mhod_string_encoding(data, offset: int) -> int:
    """Encoding word of a string MHOD starting at *offset* (1 = UTF-16LE, 2 = UTF-8)."""
    (encoding,) = struct.unpack_from("<I", bytes(data[offset + 0x18:offset + 0x1C]))
    return encoding


# ── library index sort kinds (MHOD 52/53) ───────────────────────────

SORT_TITLE = 0x03
SORT_ALBUM = 0x04
SORT_ARTIST = 0x05
SORT_GENRE = 0x07
SORT_COMPOSER = 0x12
SORT_SHOW = 0x1D
SORT_SEASON = 0x1E
SORT_EPISODE = 0x1F
SORT_ALBUM_ARTIST = 0x23

SORT_TYPE_MAP: dict[int, str] = {
    SORT_TITLE: "title",
    SORT_ALBUM: "album",
    SORT_ARTIST: "artist",
    SORT_GENRE: "genre",
    SORT_COMPOSER: "composer",
    SORT_SHOW: "show",
    SORT_SEASON: "season_number",
    SORT_EPISODE: "episode_number",
    SORT_ALBUM_ARTIST: "album_artist",
    0x24: "artist_nosort",
}
