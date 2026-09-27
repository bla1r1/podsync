"""Read a whole iTunesDB (or zlib-compressed iTunesCDB) into a nested dict tree."""

from __future__ import annotations

import contextlib
import logging
import os
import struct
import zlib
from typing import Any, BinaryIO

from podsync.itdb.reader.errors import CorruptHeaderError
from podsync.itdb.reader.primitives import preserve_raw_chunks
from podsync.itdb.spec.clock import DeviceClock, device_clock_scope

__all__ = ["decompress_itunescdb", "read_itdb"]

logger = logging.getLogger(__name__)

_COMPRESSED_FLAG = 2  # MHBD +0x0C on iTunesCDB files


def decompress_itunescdb(data: bytes | bytearray) -> bytes | bytearray:
    """Inflate an iTunesCDB body behind its untouched header; anything else passes through."""
    if len(data) < 16 or data[:4] != b"mhbd":
        return data
    header_length, = struct.unpack_from("<I", data, 0x04)
    if struct.unpack_from("<I", data, 0x0C)[0] != _COMPRESSED_FLAG:
        return data
    compressed = data[header_length:]
    try:
        body = zlib.decompress(compressed)
    except zlib.error:
        return data
    logger.debug("iTunesCDB decompressed: %d -> %d payload bytes", len(compressed), len(body))
    return data[:header_length] + body


def _read_all(file: str | os.PathLike[str] | BinaryIO) -> bytes:
    if isinstance(file, (str, os.PathLike)):
        with open(file, "rb") as handle:
            return handle.read()
    if hasattr(file, "read"):
        return file.read()
    raise TypeError(f"file must be a path (str/PathLike) or a file-like object, got {type(file).__name__}")


def read_itdb(
    file: str | os.PathLike[str] | BinaryIO,
    *,
    time_context: DeviceClock | None = None,
    preserve_raw: bool = False,
) -> dict[str, Any]:
    """Parse a database file into the MHBD dict (with nested ``children``).

    *time_context* decides how device-local timestamps become Unix times;
    *preserve_raw* attaches the raw header and unparsed bytes of every chunk.
    """
    from podsync.itdb.reader import walker

    data = _read_all(file)
    if not data:
        raise CorruptHeaderError(0, "empty file")
    data = decompress_itunescdb(data)

    walker.reset_unknown_chunk_summary()
    clock_scope = device_clock_scope(time_context) if time_context is not None else contextlib.nullcontext()
    with preserve_raw_chunks(preserve_raw), clock_scope:
        parsed, _tag = walker.parse_chunk(data, 0)
        walker.log_unknown_chunk_summary()

    tree = parsed["data"]
    if preserve_raw and isinstance(tree, dict) and "_raw_chunk" in parsed:
        tree["_raw_chunk"] = parsed["_raw_chunk"]
    return tree
