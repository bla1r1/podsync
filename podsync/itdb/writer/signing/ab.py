"""HASHAB (iPod nano 6G/7G) — deliberately unsupported.

The entry points exist so imports stay stable; both refuse with a clear error
and never touch the buffer.
"""

from __future__ import annotations

HASHAB_SIZE = 57
ITDB_CHECKSUM_HASHAB = 4

_REFUSAL = "HASHAB signing (iPod nano 6G/7G) is not supported by podsync"


def compute_hashab(sha1_digest: bytes, uuid: bytes) -> bytes:
    """HASHAB (nano 6G/7G) — deliberately not implemented; always raises."""
    raise NotImplementedError(_REFUSAL)


def write_hashab(itdb_data: bytearray, firewire_id: bytes) -> None:
    """HASHAB (nano 6G/7G) — deliberately not implemented; always raises."""
    raise NotImplementedError(_REFUSAL)
