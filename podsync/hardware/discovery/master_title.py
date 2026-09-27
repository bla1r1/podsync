"""The iPod's user-visible name: the title of the database's master playlist.

Reading the whole database just for one string would be wasteful on large
libraries, so this walks the chunk headers directly (mhbd → mhsd → mhlp →
mhyp → mhod type 1) with small bounds on every loop.  Any surprise yields
``""`` rather than an error.
"""

from __future__ import annotations

import struct

from podsync.hardware.current import locate_database

__all__ = ["master_title_from_bytes", "read_master_playlist_title"]

_PLAYLIST_DATASETS = (2, 3)  # playlists, then podcasts-style playlists
_MAX_PLAYLISTS_SCANNED = 16
_MAX_STRINGS_SCANNED = 64
_MAX_TITLE_BYTES = 1024
_COMPRESSED_DATABASE = 2


def read_master_playlist_title(ipod_path: str) -> str:
    """The master playlist's title in the iPod's database; empty when unreadable."""
    try:
        path = locate_database(ipod_path)
        if not path:
            return ""
        with open(path, "rb") as handle:
            data = handle.read()
    except Exception:
        return ""
    if len(data) < 16 or data[:4] != b"mhbd":
        return ""
    try:
        if struct.unpack_from("<I", data, 0x0C)[0] == _COMPRESSED_DATABASE:
            from podsync.itdb.reader.entry import decompress_itunescdb

            data = bytes(decompress_itunescdb(data))
        return master_title_from_bytes(data)
    except Exception:
        return ""


def _dataset_offsets(data: bytes) -> dict[int, int] | None:
    header_len = struct.unpack_from("<I", data, 4)[0]
    count = struct.unpack_from("<I", data, 0x14)[0]
    offsets: dict[int, int] = {}
    at = header_len
    for _ in range(count):
        if data[at:at + 4] != b"mhsd":
            return None
        _header, total, kind = struct.unpack_from("<III", data, at + 4)
        offsets.setdefault(kind, at)
        at += total
    return offsets


def _title_of_playlist(data: bytes, at: int, header_len: int, string_count: int) -> str:
    child = at + header_len
    for _ in range(min(string_count, _MAX_STRINGS_SCANNED)):
        if data[child:child + 4] != b"mhod":
            return ""
        _header, total, kind = struct.unpack_from("<III", data, child + 4)
        if kind == 1:
            encoding, length = struct.unpack_from("<II", data, child + 0x18)
            if length > _MAX_TITLE_BYTES:
                return ""
            raw = data[child + 0x28:child + 0x28 + length]
            return raw.decode("utf-8" if encoding == 2 else "utf-16-le", errors="replace")
        child += total
    return ""


def master_title_from_bytes(data: bytes) -> str:
    """Title of the first master playlist (``mhyp`` byte 0x14 == 1) in an uncompressed database."""
    offsets = _dataset_offsets(data)
    if offsets is None:
        return ""
    kind = next((k for k in _PLAYLIST_DATASETS if k in offsets), None)
    if kind is None:
        return ""
    mhsd = offsets[kind]
    list_at = mhsd + struct.unpack_from("<I", data, mhsd + 4)[0]
    if data[list_at:list_at + 4] != b"mhlp":
        return ""
    list_header, count = struct.unpack_from("<II", data, list_at + 4)
    at = list_at + list_header
    for _ in range(min(count, _MAX_PLAYLISTS_SCANNED)):
        if data[at:at + 4] != b"mhyp":
            return ""
        header_len, total, string_count = struct.unpack_from("<III", data, at + 4)
        if data[at + 0x14] == 1:
            return _title_of_playlist(data, at, header_len, string_count)
        at += total
    return ""
