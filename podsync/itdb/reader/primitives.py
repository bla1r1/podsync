"""Building blocks shared by every chunk parser: the common header and raw-span capture."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from podsync.itdb.reader.errors import CorruptHeaderError, InsufficientDataError

__all__ = [
    "CHUNK_HEADER",
    "ParseResult",
    "preserve_raw_chunks",
    "raw_capture_enabled",
    "raw_span_info",
    "read_chunk_header",
]

CHUNK_HEADER = struct.Struct("<4sII")  # tag, header length, total length or child count

# What a chunk parser returns: {"next_offset": int, "data": ..., "_body_end": int (optional)}
ParseResult = dict[str, Any]

_capture_raw: ContextVar[bool] = ContextVar("podsync_preserve_raw_chunks", default=False)


@contextmanager
def preserve_raw_chunks(enabled: bool) -> Iterator[None]:
    """Within the block, every parsed chunk also records its raw header and trailing bytes."""
    token = _capture_raw.set(bool(enabled))
    try:
        yield
    finally:
        _capture_raw.reset(token)


def raw_capture_enabled() -> bool:
    return _capture_raw.get()


def read_chunk_header(data: bytes | bytearray, offset: int) -> tuple[str, int, int]:
    """``(tag, header length, total length or child count)`` of the chunk at *offset*."""
    available = len(data) - offset
    if available < CHUNK_HEADER.size:
        raise InsufficientDataError(offset, CHUNK_HEADER.size, available)
    tag_bytes, header_length, third_word = CHUNK_HEADER.unpack_from(data, offset)
    try:
        tag = tag_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise CorruptHeaderError(offset, f"chunk type bytes are not valid ASCII: {tag_bytes!r}") from None
    return tag, header_length, third_word


def raw_span_info(
    data: bytes | bytearray,
    *,
    offset: int,
    header_length: int,
    declared_length_or_child_count: int,
    end_offset: int,
    parsed_body_end: int,
) -> dict[str, Any]:
    """Raw header bytes plus whatever lies between the parsed body and the chunk end.

    All boundaries are clamped to the buffer so a truncated file still yields a
    consistent description.
    """
    end = min(max(offset, end_offset), len(data))
    header_end = min(max(offset, offset + header_length), end)
    body_end = min(max(header_end, parsed_body_end), end)
    return {
        "offset": offset,
        "header_length": header_length,
        "declared_length_or_child_count": declared_length_or_child_count,
        "end_offset": end,
        "raw_header": bytes(data[offset:header_end]),
        "unparsed_bytes": bytes(data[body_end:end]),
    }
