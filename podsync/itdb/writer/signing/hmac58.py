"""HASH58: the HMAC-SHA1 signature at MHBD +0x58 (iPod Classic, nano 3G/4G).

The HMAC key is derived from the device's FireWire GUID: each pair of GUID
bytes gives an LCM whose two bytes are looked up in the AES S-box and its
inverse; SHA-1 of a fixed salt plus those 16 bytes is the key.  The signature
covers the whole file with the database id, the 20 bytes at +0x32 and the
signature field itself zeroed.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import struct

from podsync.itdb.spec.layouts.header import (
    MHBD_OFFSET_DB_ID,
    MHBD_OFFSET_HASH58,
    MHBD_OFFSET_HASHING_SCHEME,
    MHBD_OFFSET_UNK_0x32,
)

__all__ = ["ITDB_CHECKSUM_HASH58", "compute_hash58", "read_firewire_id", "write_hash58"]

ITDB_CHECKSUM_HASH58 = 1
_SALT = bytes.fromhex("6723fe304533f890992107c1d012b2a10781")
_HASH58_LENGTH = 20


def _gf_multiply(a: int, b: int) -> int:
    """Multiplication in GF(2^8) with the AES polynomial x^8 + x^4 + x^3 + x + 1."""
    product = 0
    while b:
        if b & 1:
            product ^= a
        a = ((a << 1) ^ 0x11B) if a & 0x80 else a << 1
        b >>= 1
    return product


def _aes_sbox() -> tuple[bytes, bytes]:
    """The AES S-box and its inverse, computed rather than tabulated."""
    inverse = [0] * 256
    for x in range(1, 256):
        inverse[x] = next(y for y in range(1, 256) if _gf_multiply(x, y) == 1)
    forward = bytearray(256)
    for x in range(256):
        b = inverse[x]
        rotated = b
        for shift in range(1, 5):
            rotated ^= ((b << shift) | (b >> (8 - shift))) & 0xFF
        forward[x] = rotated ^ 0x63
    backward = bytearray(256)
    for x, y in enumerate(forward):
        backward[y] = x
    return bytes(forward), bytes(backward)


SBOX, INVERSE_SBOX = _aes_sbox()


def _lcm(a: int, b: int) -> int:
    return a * b // math.gcd(a, b) if a and b else 1


def _generate_key(firewire_id: bytes) -> bytes:
    """The 64-byte HMAC key for a FireWire GUID (first 8 bytes used)."""
    if len(firewire_id) < 8:
        raise ValueError(f"FireWire ID must be at least 8 bytes, got {len(firewire_id)}")
    mixed = bytearray()
    for i in range(4):
        high, low = divmod(_lcm(firewire_id[2 * i], firewire_id[2 * i + 1]) & 0xFFFF, 0x100)
        mixed += bytes((SBOX[high], INVERSE_SBOX[high], SBOX[low], INVERSE_SBOX[low]))
    return hashlib.sha1(_SALT + bytes(mixed)).digest().ljust(64, b"\x00")


def compute_hash58(firewire_id: bytes, itdb_data: bytes) -> bytes:
    """HMAC-SHA1 of *itdb_data* under the key derived from *firewire_id*."""
    return hmac.new(_generate_key(bytes(firewire_id)), bytes(itdb_data), hashlib.sha1).digest()


def write_hash58(itdb_data: bytearray, firewire_id: bytes) -> None:
    """Sign a database image in place (scheme word set to 1)."""
    if len(itdb_data) < 0x6C:
        raise ValueError(f"iTunesDB file too small ({len(itdb_data)} bytes), need at least 0x6C")
    if bytes(itdb_data[:4]) != b"mhbd":
        raise ValueError("Invalid iTunesDB: expected 'mhbd' header")
    masked = ((MHBD_OFFSET_DB_ID, 8), (MHBD_OFFSET_UNK_0x32, 20), (MHBD_OFFSET_HASH58, _HASH58_LENGTH))
    saved = [bytes(itdb_data[offset:offset + size]) for offset, size in masked]
    for offset, size in masked:
        itdb_data[offset:offset + size] = bytes(size)
    struct.pack_into("<H", itdb_data, MHBD_OFFSET_HASHING_SCHEME, ITDB_CHECKSUM_HASH58)
    digest = compute_hash58(firewire_id, bytes(itdb_data))
    if len(digest) != _HASH58_LENGTH:
        raise RuntimeError("HASH58 computation produced an unexpected digest size")
    itdb_data[MHBD_OFFSET_HASH58:MHBD_OFFSET_HASH58 + _HASH58_LENGTH] = digest
    # Restore the id and the +0x32 bytes; only the signature changes.
    for (offset, size), original in zip(masked[:2], saved[:2]):
        itdb_data[offset:offset + size] = original


def read_firewire_id(ipod_path: str) -> bytes:
    from podsync.hardware import get_firewire_id

    return get_firewire_id(ipod_path)
