"""HASHAB: the white-box AES signature at MHBD +0xAB (iPod nano 6G/7G).

Apple's HASHAB algorithm is an obfuscated white-box AES-128 encryption of a
key expanded from a SHA-1 digest of the file and the device's FireWire GUID.
No public specification of the algorithm exists, and there is no way to
derive it from cryptographic first principles — the only known working
implementation is a clean-room WebAssembly module built by reverse-engineering
Apple's own binary (see ``wasm/THIRD_PARTY_NOTICE.md``), which this module
runs through ``wasmtime``.

The signature covers the whole file with ``db_id``, the 20 bytes at +0x32,
and every signature field (HASH58, HASH72, HASHAB) zeroed — mirroring HASH58
so nano 6G/7G devices, which keep a HASH58-shaped header, stay consistent.
"""

from __future__ import annotations

import hashlib
import struct
import threading
from pathlib import Path

from podsync.itdb.spec.layouts.header import (
    MHBD_OFFSET_DB_ID,
    MHBD_OFFSET_HASH58,
    MHBD_OFFSET_HASH72,
    MHBD_OFFSET_HASHAB,
    MHBD_OFFSET_HASHING_SCHEME,
    MHBD_OFFSET_UNK_0x32,
)

__all__ = ["HASHAB_SIZE", "ITDB_CHECKSUM_HASHAB", "compute_hashab", "write_hashab"]

HASHAB_SIZE = 57
ITDB_CHECKSUM_HASHAB = 4
_HASH58_SIZE = 20
_HASH72_SIZE = 46
_UNK_0x32_SIZE = 20
_SHA1_SIZE = 20
_UUID_SIZE = 8

_WASM_PATH = Path(__file__).parent / "wasm" / "calcHashAB.wasm"

_lock = threading.Lock()
_engine_state: dict | None = None


def _wasm_instance():
    """The (store, exports) pair for the vendored module, created once and reused."""
    global _engine_state
    with _lock:
        if _engine_state is None:
            try:
                import wasmtime
            except ImportError as exc:
                raise ImportError(
                    "HASHAB signing (iPod nano 6G/7G) needs the 'wasmtime' package"
                ) from exc
            if not _WASM_PATH.exists():
                raise FileNotFoundError(f"HASHAB WASM module missing: {_WASM_PATH}")
            engine = wasmtime.Engine()
            store = wasmtime.Store(engine)
            module = wasmtime.Module.from_file(engine, str(_WASM_PATH))
            instance = wasmtime.Instance(store, module, [])
            _engine_state = {"store": store, "exports": instance.exports(store)}
        return _engine_state["store"], _engine_state["exports"]


def compute_hashab(sha1_digest: bytes, uuid: bytes) -> bytes:
    """The 57-byte HASHAB signature for a 20-byte SHA-1 digest and an 8-byte UUID."""
    if len(sha1_digest) != _SHA1_SIZE:
        raise ValueError(f"HASHAB SHA-1 input must be {_SHA1_SIZE} bytes, got {len(sha1_digest)}")
    if len(uuid) < _UUID_SIZE:
        raise ValueError(f"HASHAB UUID input must be at least {_UUID_SIZE} bytes, got {len(uuid)}")
    store, exports = _wasm_instance()
    memory = exports["memory"].data_ptr(store)
    sha1_ptr = exports["getInputSha1"](store)
    uuid_ptr = exports["getInputUuid"](store)
    output_ptr = exports["getOutput"](store)
    for i in range(_SHA1_SIZE):
        memory[sha1_ptr + i] = sha1_digest[i]
    for i in range(_UUID_SIZE):
        memory[uuid_ptr + i] = uuid[i]
    exports["calculateHash"](store)
    return bytes(memory[output_ptr + i] for i in range(HASHAB_SIZE))


def _digest_for_signing(itdb_data: bytearray) -> bytes:
    """SHA-1 of the whole file with every signature-related field zeroed."""
    scratch = bytearray(itdb_data)
    for offset, size in (
        (MHBD_OFFSET_DB_ID, 8), (MHBD_OFFSET_UNK_0x32, _UNK_0x32_SIZE),
        (MHBD_OFFSET_HASH58, _HASH58_SIZE), (MHBD_OFFSET_HASH72, _HASH72_SIZE),
        (MHBD_OFFSET_HASHAB, HASHAB_SIZE),
    ):
        scratch[offset:offset + size] = bytes(size)
    return hashlib.sha1(bytes(scratch)).digest()


def write_hashab(itdb_data: bytearray, firewire_id: bytes) -> None:
    """Sign a database image in place (scheme word set to HASHAB)."""
    min_size = MHBD_OFFSET_HASHAB + HASHAB_SIZE
    if len(itdb_data) < min_size:
        raise ValueError(f"iTunesDB file too small ({len(itdb_data)} bytes), need at least {min_size}")
    if bytes(itdb_data[:4]) != b"mhbd":
        raise ValueError("Invalid iTunesDB: expected 'mhbd' header")
    if len(firewire_id) < _UUID_SIZE:
        raise ValueError(f"FireWire ID must be at least {_UUID_SIZE} bytes, got {len(firewire_id)}")

    masked = ((MHBD_OFFSET_DB_ID, 8), (MHBD_OFFSET_UNK_0x32, _UNK_0x32_SIZE))
    saved = [bytes(itdb_data[offset:offset + size]) for offset, size in masked]
    struct.pack_into("<H", itdb_data, MHBD_OFFSET_HASHING_SCHEME, ITDB_CHECKSUM_HASHAB)
    digest = compute_hashab(_digest_for_signing(itdb_data), firewire_id)
    if len(digest) != HASHAB_SIZE:
        raise RuntimeError("HASHAB computation produced an unexpected signature size")
    itdb_data[MHBD_OFFSET_HASHAB:MHBD_OFFSET_HASHAB + HASHAB_SIZE] = digest
    for (offset, size), original in zip(masked, saved):
        itdb_data[offset:offset + size] = original
