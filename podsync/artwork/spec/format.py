"""The ArtworkDB container format: header sizes, data-object kinds and binary helpers.

ArtworkDB (``iPod_Control/Artwork/ArtworkDB``) indexes cover-art renditions.
Its chunks share the iTunesDB ``tag / header length / length`` framing:
``mhfd`` → ``mhsd`` datasets → image list (``mhli``/``mhii``/``mhod``/``mhni``),
album list and file list (``mhlf``/``mhif``).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "ArtworkDatasetType", "ArtworkMhodType", "CHUNK_TYPE_MAP", "ChunkHeader", "GENERIC_CHUNK_HEADER_SIZE",
    "IDENTIFIER_READABLE_MAP", "IMAGE_CONTAINER_MHOD_TYPES", "IMAGE_CONTAINER_NAMES", "MHFD_HEADER_SIZE",
    "MHIF_HEADER_SIZE", "MHII_HEADER_SIZE", "MHLA_HEADER_SIZE", "MHLF_HEADER_SIZE", "MHLI_HEADER_SIZE",
    "MHNI_HEADER_SIZE", "MHOD_HEADER_SIZE", "MHOD_TYPE_MAP", "MHSD_HEADER_SIZE", "MIN_TYPED_CHUNK_HEADER_SIZE",
    "chunk_fits", "decode_mhod_string_body", "decode_mhod_string_chunk", "encode_mhod_string_body",
    "is_mhod_container", "mhod_string_encoding", "mhod_type_info", "mhod_type_name", "read_chunk_header",
    "read_i16", "read_u16", "read_u32", "read_u64", "total_length_is_valid",
]

# ── header sizes ────────────────────────────────────────────────────

MHFD_HEADER_SIZE = 132
MHSD_HEADER_SIZE = 96
MHLI_HEADER_SIZE = MHLA_HEADER_SIZE = MHLF_HEADER_SIZE = 92
MHII_HEADER_SIZE = 152
MHOD_HEADER_SIZE = 24
MHNI_HEADER_SIZE = 76
MHIF_HEADER_SIZE = 124
GENERIC_CHUNK_HEADER_SIZE = 12
MIN_TYPED_CHUNK_HEADER_SIZE = 14  # generic header + the u16 type word


class ArtworkDatasetType(IntEnum):
    """``mhsd`` dataset kinds in an ArtworkDB."""

    IMAGE_LIST = 1
    PHOTO_ALBUM_LIST = 2
    FILE_LIST = 3


class ArtworkMhodType(IntEnum):
    """``mhod`` kinds used inside ArtworkDB records."""

    ALBUM_NAME = 1
    THUMBNAIL_IMAGE = 2
    FILE_NAME = 3
    FULL_RES_IMAGE = 5
    UNKNOWN_CONTAINER_6 = 6


CHUNK_TYPE_MAP: dict[int, str] = {
    ArtworkDatasetType.IMAGE_LIST: "mhli",
    ArtworkDatasetType.PHOTO_ALBUM_LIST: "mhla",
    ArtworkDatasetType.FILE_LIST: "mhlf",
}

IDENTIFIER_READABLE_MAP: dict[str, str] = {
    "mhfd": "Data File", "mhsd": "Data Set", "mhli": "Image List", "mhii": "Image Item",
    "mhni": "Image Name", "mhla": "Photo Album List", "mhba": "Photo Album",
    "mhia": "Photo Album Item", "mhlf": "File List", "mhif": "File List Item", "mhod": "Data Object",
}


def _kind(shape: str, name: str) -> dict[str, str]:
    return {"type": shape, "name": name}


MHOD_TYPE_MAP: dict[int, dict[str, str]] = {
    ArtworkMhodType.ALBUM_NAME: _kind("String", "Album Name"),
    ArtworkMhodType.THUMBNAIL_IMAGE: _kind("Container", "Thumbnail Image"),
    ArtworkMhodType.FILE_NAME: _kind("String", "File Name"),
    ArtworkMhodType.FULL_RES_IMAGE: _kind("Container", "Full Res Image"),
    ArtworkMhodType.UNKNOWN_CONTAINER_6: _kind("Container", "UNK MHOD 6"),
}

IMAGE_CONTAINER_MHOD_TYPES = frozenset(k for k, v in MHOD_TYPE_MAP.items() if v["type"] == "Container")
IMAGE_CONTAINER_NAMES = ("Full Res Image", "Thumbnail Image", "UNK MHOD 6")


# ── binary helpers ──────────────────────────────────────────────────


def _reader(fmt: str):
    unpacker = struct.Struct(fmt)
    return lambda data, offset: unpacker.unpack_from(data, offset)[0]


read_u16 = _reader("<H")
read_i16 = _reader("<h")
read_u32 = _reader("<I")
read_u64 = _reader("<Q")


@dataclass(frozen=True)
class ChunkHeader:
    """The common ``tag, header length, total length`` prefix of a chunk."""

    tag: str
    header_size: int
    length_or_count: int


def read_chunk_header(data: bytes, offset: int) -> ChunkHeader:
    """The chunk header at *offset*, or ``None`` when it does not fit."""
    if offset < 0 or offset + GENERIC_CHUNK_HEADER_SIZE > len(data):
        raise ValueError(f"ArtworkDB chunk header outside buffer at offset {offset}")
    header_size, third = struct.unpack_from("<II", data, offset + 4)
    return ChunkHeader(bytes(data[offset:offset + 4]).decode("utf-8", errors="replace"), header_size, third)


def chunk_fits(data: bytes, offset: int, total_size: int, min_header_size: int = 12) -> bool:
    """Whether a chunk of *total_size* starting at *offset* lies inside *data*."""
    return offset >= 0 and total_size >= min_header_size and offset + total_size <= len(data)


def total_length_is_valid(
    data: bytes, offset: int, header_size: int, total_size: int, min_header_size: int = 12, end: int | None = None,
) -> bool:
    """Header and total length are sane and the chunk ends inside the buffer (or *end*)."""
    limit = len(data) if end is None else min(len(data), end)
    return offset >= 0 and header_size >= min_header_size and total_size >= header_size and offset + total_size <= limit


# ── data objects ────────────────────────────────────────────────────


def mhod_type_info(mhod_type: int) -> dict[str, str] | None:
    """Registry entry (name, kind) for an ArtworkDB ``mhod`` type, or ``None``."""
    try:
        return MHOD_TYPE_MAP.get(ArtworkMhodType(mhod_type))
    except ValueError:
        return None


def is_mhod_container(mhod_type: int) -> bool:
    """Whether the ``mhod`` type wraps child chunks instead of a string."""
    info = mhod_type_info(mhod_type)
    return bool(info) and info["type"] == "Container"


def mhod_type_name(mhod_type: int) -> str | None:
    """Human-readable name of an ArtworkDB ``mhod`` type."""
    info = mhod_type_info(mhod_type)
    return info["name"] if info else None


def mhod_string_encoding(mhod_type: int) -> tuple[str, int]:
    """File names are UTF-16LE (flag 2); every other string is UTF-8 (flag 1)."""
    return ("utf-16-le", 2) if mhod_type == ArtworkMhodType.FILE_NAME else ("utf-8", 1)


def encode_mhod_string_body(mhod_type: int, value: str) -> bytes:
    """``length | encoding flag | 3 pad | reserved u32 | text | pad to 4``."""
    codec, flag = mhod_string_encoding(mhod_type)
    text = str(value).encode(codec)
    return struct.pack("<IB3xI", len(text), flag, 0) + text + bytes(-len(text) % 4)


def decode_mhod_string_body(data: bytes, body_offset: int, body_end: int) -> str | None:
    """The string of a string ``mhod`` (UTF-16LE when flagged 2, else UTF-8)."""
    if body_offset + 12 > body_end:
        return None
    try:
        length, flag = read_u32(data, body_offset), data[body_offset + 4]
        raw = bytes(data[body_offset + 12:min(body_end, body_offset + 12 + length)])
        return raw.decode("utf-16-le" if flag == 2 else "utf-8", errors="replace").rstrip("\x00")
    except (UnicodeError, struct.error, IndexError):
        return None


def decode_mhod_string_chunk(data: bytes, offset: int, total_size: int) -> str | None:
    """Decode a whole string MHOD chunk after validating its header length."""
    if total_size < 36:
        return None
    try:
        header_size = read_u32(data, offset + 4)
    except struct.error:
        return None
    if not 24 <= header_size <= total_size:
        return None
    return decode_mhod_string_body(data, offset + header_size, offset + total_size)
