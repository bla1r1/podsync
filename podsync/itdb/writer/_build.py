"""Small helpers every chunk writer uses."""

from __future__ import annotations

import random
from typing import Any

from podsync.itdb.spec.fields import U32_MAX, encode_fields, pack_chunk_header

__all__ = ["clamp_u32", "nonzero_random_u64", "record_bytes"]


def record_bytes(tag: bytes, header_size: int, values: dict[str, Any], body: bytes = b"") -> bytes:
    """A chunk whose header fields come from the registered layout of *tag*, followed by *body*."""
    header = bytearray(header_size)
    pack_chunk_header(header, 0, tag, header_size, header_size + len(body))
    encode_fields(header, 0, tag.decode("ascii"), values, header_size)
    return bytes(header) + body


def clamp_u32(value: Any) -> int:
    """Best-effort unsigned 32-bit value; junk becomes 0, overflow saturates."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(number, 0), U32_MAX)


def nonzero_random_u64() -> int:
    """A random 64-bit id; zero is reserved as "unset" by the firmware."""
    value = 0
    while value == 0:
        value = random.getrandbits(64)
    return value
