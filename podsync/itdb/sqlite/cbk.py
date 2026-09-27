"""``Locations.itdb.cbk``: a signed checksum book proving ``Locations.itdb`` is genuine.

SHA-1 every 1024-byte block of ``Locations.itdb``, SHA-1 the concatenation of
those digests, then sign *that* digest the same way the database itself
would be signed — HASH58/HASH72/HASHAB all sign a digest directly, so this
reuses the exact same signing primitives as the binary iTunesDB.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from podsync.hardware.catalog.checksum import SignatureKind

__all__ = ["write_locations_cbk"]

_BLOCK_SIZE = 1024


def _block_digests(data: bytes) -> list[bytes]:
    return [hashlib.sha1(data[offset:offset + _BLOCK_SIZE]).digest() for offset in range(0, len(data), _BLOCK_SIZE)]


def _signed_header(final_digest: bytes, *, checksum_kind: SignatureKind, firewire_id: bytes | None,
                   ipod_path: str) -> bytes:
    if checksum_kind == SignatureKind.HASHAB:
        from podsync.itdb.writer.signing.ab import compute_hashab

        if not firewire_id or len(firewire_id) < 8:
            raise ValueError("A FireWire ID is required to sign Locations.itdb.cbk with HASHAB")
        return compute_hashab(final_digest, firewire_id[:8])

    if checksum_kind == SignatureKind.HASH58:
        from podsync.itdb.writer.signing.hmac58 import compute_hash58

        if not firewire_id or len(firewire_id) < 8:
            raise ValueError("A FireWire ID is required to sign Locations.itdb.cbk with HASH58")
        return compute_hash58(firewire_id, final_digest)

    if checksum_kind == SignatureKind.HASH72:
        from podsync.itdb.writer.signing.aes72 import hash72_signature, read_hash_info

        material = read_hash_info(ipod_path)
        if material is None:
            raise ValueError("No HashInfo material is available to sign Locations.itdb.cbk with HASH72")
        return hash72_signature(final_digest, material.iv, material.rndpart)

    if checksum_kind == SignatureKind.NONE:
        return final_digest

    raise ValueError(f"Cannot sign Locations.itdb.cbk for checksum scheme {checksum_kind.name}")


def write_locations_cbk(
    cbk_path: str, locations_itdb_path: str, *, checksum_kind: SignatureKind,
    firewire_id: bytes | None, ipod_path: str,
) -> None:
    """Write the checksum book: signed header, then the final digest, then every block digest."""
    data = Path(locations_itdb_path).read_bytes()
    block_digests = _block_digests(data)
    final_digest = hashlib.sha1(b"".join(block_digests)).digest()
    header = _signed_header(final_digest, checksum_kind=checksum_kind, firewire_id=firewire_id, ipod_path=ipod_path)
    with open(cbk_path, "wb") as handle:
        handle.write(header)
        handle.write(final_digest)
        handle.write(b"".join(block_digests))
