"""HASH72: the AES-CBC signature at MHBD +0x72 (iPod nano 5G), and the ``HashInfo`` file.

The signature is ``01 00 | rndpart(12) | AES-CBC(key, iv, sha1(db) + rndpart)``.
The per-device ``iv``/``rndpart`` live in ``iPod_Control/Device/HashInfo``
(written by iTunes) — or can be recovered from any database iTunes signed,
because the first CBC block reveals the IV once the SHA-1 is known.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from dataclasses import dataclass
from pathlib import Path

from podsync.itdb.spec.layouts.header import (
    MHBD_OFFSET_DB_ID,
    MHBD_OFFSET_HASH58,
    MHBD_OFFSET_HASH72,
    MHBD_OFFSET_HASHING_SCHEME,
)

__all__ = [
    "HashInfo",
    "ITDB_CHECKSUM_HASH72",
    "compute_hash72",
    "extract_hash_info",
    "extract_hash_info_to_dict",
    "hash72_signature",
    "read_hash_info",
    "signature_digest",
    "write_hash72",
    "write_hash_info",
]

logger = logging.getLogger(__name__)

AES_KEY = bytes.fromhex("618ca10dc7f57fd3b4723e08157463d7")
ITDB_CHECKSUM_HASH72 = 2
HASHINFO_HEADER = b"HASHv0"
_HASHINFO_LAYOUT = struct.Struct("<6s20s12s16s")  # magic, uuid, rndpart, iv
_SIGNATURE_LENGTH = 46
_SIGNATURE_MARKER = b"\x01\x00"


@dataclass(frozen=True)
class HashInfo:
    """Per-device material (UUID, IV, random part) needed to sign with HASH72."""

    uuid: bytes
    rndpart: bytes
    iv: bytes


def _aes():
    try:
        from Crypto.Cipher import AES  # type: ignore[import-not-found]
    except ImportError:
        try:
            from Cryptodome.Cipher import AES  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError("PyCryptodome is required for HASH72. Install with: pip install pycryptodome") from exc
    return AES


def signature_digest(itdb_data: bytes | bytearray) -> bytes:
    """SHA-1 of the image with the database id and both signature fields zeroed."""
    image = bytearray(itdb_data)
    for offset, size in ((MHBD_OFFSET_DB_ID, 8), (MHBD_OFFSET_HASH58, 20), (MHBD_OFFSET_HASH72, _SIGNATURE_LENGTH)):
        image[offset:offset + size] = bytes(size)
    return hashlib.sha1(bytes(image)).digest()


def hash72_signature(sha1: bytes, iv: bytes, rndpart: bytes) -> bytes:
    """The 46-byte signature for a digest under a device's ``iv``/``rndpart``."""
    aes = _aes()
    sealed = aes.new(AES_KEY, aes.MODE_CBC, iv=bytes(iv)).encrypt(bytes(sha1) + bytes(rndpart))
    return _SIGNATURE_MARKER + bytes(rndpart) + sealed


def _recover_material(signature: bytes, sha1: bytes) -> tuple[bytes, bytes] | None:
    """``(iv, rndpart)`` behind an existing signature, if it really signs *sha1*."""
    if len(signature) < _SIGNATURE_LENGTH or signature[:2] != _SIGNATURE_MARKER:
        return None
    rndpart, sealed = bytes(signature[2:14]), bytes(signature[14:_SIGNATURE_LENGTH])
    aes = _aes()
    # CBC: first plaintext block = D(C1) xor IV, and we know the plaintext (the digest).
    iv = bytes(a ^ b for a, b in zip(aes.new(AES_KEY, aes.MODE_ECB).decrypt(sealed[:16]), sha1[:16]))
    opened = aes.new(AES_KEY, aes.MODE_CBC, iv=iv).decrypt(sealed)
    if opened[:20] != bytes(sha1) or opened[20:32] != rndpart:
        return None
    return iv, rndpart


def _hash_info_path(ipod_path: str) -> Path:
    return Path(ipod_path) / "iPod_Control" / "Device" / "HashInfo"


def _selected_device_material(ipod_path: str) -> HashInfo | None:
    try:
        import podsync.hardware as hardware

        device = hardware.selected_device_at(ipod_path)
    except Exception:
        return None
    if device is None:
        return None
    iv = bytes(getattr(device, "hash_info_iv", b"") or b"")
    rndpart = bytes(getattr(device, "hash_info_rndpart", b"") or b"")
    return HashInfo(bytes(20), rndpart, iv) if len(iv) == 16 and len(rndpart) == 12 else None


def read_hash_info(ipod_path: str) -> HashInfo | None:
    """Signing material: from the selected device if known, else the HashInfo file."""
    material = _selected_device_material(ipod_path)
    if material is not None:
        return material
    try:
        blob = _hash_info_path(ipod_path).read_bytes()
    except OSError:
        return None
    if len(blob) < _HASHINFO_LAYOUT.size or not blob.startswith(HASHINFO_HEADER):
        return None
    _magic, uuid, rndpart, iv = _HASHINFO_LAYOUT.unpack_from(blob)
    return HashInfo(uuid, rndpart, iv)


def write_hash_info(
    ipod_path: str,
    uuid: bytes,
    iv: bytes,
    rndpart: bytes,
    *,
    reported_volume_format: str = "",
    expected_volume_identity_key: str = "",
) -> bool:
    """Store signing material on the device through a guarded metadata write."""
    if (len(uuid), len(iv), len(rndpart)) != (20, 16, 12):
        return False
    from podsync.hardware.safety.sysinfo_write import sysinfo_write_session

    payload = _HASHINFO_LAYOUT.pack(HASHINFO_HEADER, bytes(uuid), bytes(rndpart), bytes(iv))
    with sysinfo_write_session(
        ipod_path, reported_volume_format=reported_volume_format,
        expected_volume_identity_key=expected_volume_identity_key,
    ) as session:
        session.write_bytes_atomic("iPod_Control/Device/HashInfo", payload, allowed_subtree="iPod_Control/Device")
    logger.info("Wrote HashInfo for %s", ipod_path)
    return True


def extract_hash_info_to_dict(valid_itdb_data: bytes | bytearray) -> dict[str, bytes] | None:
    """``{"iv", "rndpart"}`` recovered from a database iTunes signed with HASH72."""
    data = bytes(valid_itdb_data)
    if len(data) < 0xA0 or data[:4] != b"mhbd":
        return None
    signature = data[MHBD_OFFSET_HASH72:MHBD_OFFSET_HASH72 + _SIGNATURE_LENGTH]
    if signature[:2] != _SIGNATURE_MARKER:
        return None
    recovered = _recover_material(signature, signature_digest(data))
    return {"iv": recovered[0], "rndpart": recovered[1]} if recovered else None


def extract_hash_info(ipod_path: str, valid_itdb_data: bytes | bytearray) -> bool:
    """Recover material from *valid_itdb_data* and save it as the device's HashInfo."""
    material = extract_hash_info_to_dict(valid_itdb_data)
    if material is None:
        return False
    uuid = bytearray(20)
    try:
        import podsync.hardware as hardware

        guid = hardware.get_firewire_id(ipod_path)
        uuid[:min(20, len(guid))] = guid[:20]
    except Exception:
        uuid = bytearray(20)
    return write_hash_info(ipod_path, bytes(uuid), material["iv"], material["rndpart"])


def compute_hash72(ipod_path: str, itdb_data: bytes | bytearray) -> bytes:
    """The 46-byte HASH72 signature for *itdb_data* using the device's HashInfo."""
    info = read_hash_info(ipod_path)
    if info is None:
        raise FileNotFoundError(
            f"HashInfo file not found at {_hash_info_path(ipod_path)}. Sync once with iTunes to create it, "
            "or use extract_hash_info() with a valid iTunes-generated iTunesDB."
        )
    return hash72_signature(signature_digest(itdb_data), info.iv, info.rndpart)


def write_hash72(itdb_data: bytearray, ipod_path: str) -> None:
    """Sign a database image in place (scheme word set to 2)."""
    if len(itdb_data) < 0x6C:
        raise ValueError(f"iTunesDB file too small ({len(itdb_data)} bytes)")
    if bytes(itdb_data[:4]) != b"mhbd":
        raise ValueError("Invalid iTunesDB: expected 'mhbd' header")
    struct.pack_into("<H", itdb_data, MHBD_OFFSET_HASHING_SCHEME, ITDB_CHECKSUM_HASH72)
    itdb_data[MHBD_OFFSET_HASH72:MHBD_OFFSET_HASH72 + _SIGNATURE_LENGTH] = compute_hash72(ipod_path, itdb_data)
