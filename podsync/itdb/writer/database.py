"""Assemble a whole database (:func:`write_mhbd`) and install it on a device (:func:`write_itdb`).

Installing is careful by design: the existing database is validated before it
is replaced, the new image is signed the way the model requires, free space is
checked (including the backup copy), the old file is backed up, and the new
one is staged in a sibling temp file and swapped in atomically.  Any failure
before the swap leaves the device untouched.
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import stat
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from podsync.hardware.catalog.checksum import SignatureKind
from podsync.hardware.safety.durable import flush_written_file, open_unique_sibling_temp, safe_replace, safe_unlink
from podsync.hardware.safety.fstype import detect_volume_format, resolve_itunesdb_platform
from podsync.hardware.safety.guard import UnsafeWriteError
from podsync.hardware.safety.limits import FileTooLargeError, allocated_size, ensure_file_fits, max_file_bytes
from podsync.hardware.safety.paths import safe_device_path
from podsync.hardware.safety.readiness import check_write_ready
from podsync.itdb.spec.albums import album_identity_from_track
from podsync.itdb.spec.clock import active_device_time_context, device_clock_scope, load_device_clock
from podsync.itdb.spec.fields import decode_fields
from podsync.itdb.spec.layouts.header import MHBD_HEADER_SIZE, MHBD_OFFSET_HASHING_SCHEME
from podsync.itdb.writer._build import record_bytes
from podsync.itdb.writer.lists import (
    write_mhla,
    write_mhli,
    write_mhlp_smart,
    write_mhlp_with_playlists,
    write_mhlp_with_playlists_type3,
    write_mhlt,
    write_mhsd,
    write_mhsd_empty_stub,
)
from podsync.itdb.writer.playlist import PlaylistRecord
from podsync.itdb.writer.signing.aes72 import (
    HashInfo,
    signature_digest,
    hash72_signature,
    extract_hash_info_to_dict,
    read_hash_info,
)
from podsync.itdb.writer.signing.ab import write_hashab
from podsync.itdb.writer.signing.hmac58 import write_hash58
from podsync.itdb.writer.track import TrackRecord, generate_db_track_id

__all__ = ["DATABASE_VERSION_DEFAULT", "extract_db_info", "extract_preserved_mhsd_blobs", "write_itdb", "write_mhbd"]

logger = logging.getLogger(__name__)

DATABASE_VERSION_DEFAULT = 0x4F
_GENERATED_DATASETS = (1, 2, 3, 4, 5, 6, 8, 10)
_DEFAULT_DATASET_ORDER = (1, 3, 2, 4, 8, 6, 10, 5)
_NOT_ON_LEGACY_FIRMWARE = frozenset({6, 8, 10})
_LEGACY_DB_VERSION = 0x19
_PLATFORM_NAMES = {1: "Mac", 2: "Windows"}
_SIGNATURE_OFFSET, _SIGNATURE_LENGTH = 0x72, 46
_HASH_TYPE_FOR = {SignatureKind.HASHAB: 4, SignatureKind.HASH72: 2}


def generate_database_id() -> int:
    return random.getrandbits(64)


# ── assembling the image ────────────────────────────────────────────


def _db_version(reference_info: dict, capabilities) -> int:
    """The newer of the reference file's version and the model's; otherwise the default."""
    from_reference = int(reference_info.get("version") or 0)
    from_model = int(getattr(capabilities, "db_version", 0) or 0) if capabilities is not None else 0
    if from_model:
        return max(from_reference, from_model)
    return from_reference or DATABASE_VERSION_DEFAULT


def _remap_playlists(playlists: list[PlaylistRecord] | None, id_map: dict[int, int]) -> list[PlaylistRecord]:
    """Database ids → the track ids just assigned; unknown tracks are dropped.

    Entry details survive only if every kept entry still has its own.
    """
    remapped: list[PlaylistRecord] = []
    for playlist in playlists or []:
        details = playlist.item_metadata
        kept_ids: list[int] = []
        kept_details: list[Any] = []
        for index, db_track_id in enumerate(playlist.track_ids):
            track_id = id_map.get(db_track_id)
            if track_id is None:
                continue
            kept_ids.append(track_id)
            if details is not None and index < len(details):
                kept_details.append(details[index])
        complete = details is not None and len(kept_details) == len(kept_ids)
        remapped.append(replace(playlist, track_ids=kept_ids, item_metadata=kept_details if complete else None))
    return remapped


def _host_utc_offset() -> int:
    return -time.altzone if time.localtime().tm_isdst > 0 else -time.timezone


def _timezone_offset(reference_info: dict) -> int:
    """Device clock (if one is scoped) → reference header → this computer's offset."""
    clock = active_device_time_context()
    if clock is not None:
        return clock.offset_at_unix(int(time.time()))
    if "timezone_offset" in reference_info:
        return int(reference_info["timezone_offset"] or 0)
    return _host_utc_offset()


def _select_datasets(blobs: dict[int, bytes], reference_info: dict, capabilities) -> list[int]:
    """Which datasets to emit, and in what order.

    Without a reference file the default order is used.  With one, its order is
    followed and only kinds it had are kept — except that tracks and the visible
    playlists (and podcasts when the reference has them) are always present.
    """
    podcasts_ok = capabilities is None or bool(getattr(capabilities, "supports_podcast", True))
    legacy = capabilities is not None and int(getattr(capabilities, "db_version", 0x30) or 0) <= _LEGACY_DB_VERSION

    def emittable(kind: int) -> bool:
        if kind not in _GENERATED_DATASETS or (kind == 3 and not podcasts_ok):
            return False
        if legacy and kind in _NOT_ON_LEGACY_FIRMWARE:
            return False
        return bool(blobs.get(kind))

    known = set(reference_info.get("mhsd_types") or ())
    if 1 not in known:
        return [kind for kind in _DEFAULT_DATASET_ORDER if emittable(kind)]
    order = list(reference_info.get("mhsd_order") or [])
    if not order:
        return [kind for kind in _DEFAULT_DATASET_ORDER if emittable(kind) and (kind in (1, 4) or kind in known)]

    required = {1, 2} | ({3} if 3 in known and podcasts_ok else set())
    chosen: list[int] = []
    for kind in order:
        if kind in chosen or not emittable(kind) or (kind not in known and kind not in required):
            continue
        chosen.append(kind)
        # A reference with podcasts but no visible list still needs dataset 2 right after it.
        if kind == 3 and 2 not in known and 2 not in chosen and emittable(2):
            chosen.append(2)
    chosen += [kind for kind in (1, 3, 2) if kind in required and kind not in chosen and emittable(kind)]
    return chosen


def _assign_library_ids(tracks: list[TrackRecord]) -> tuple[bytes, bytes, dict[tuple[str, str], int], int]:
    """Build album and artist lists and give every track its album/artist/composer ids."""
    albums, album_ids, next_id = write_mhla(tracks, 1)
    artists, artist_ids, next_id = write_mhli(tracks, next_id)
    composer_ids: dict[str, int] = {}
    for track in tracks:
        composer = getattr(track, "composer", None)
        if composer and composer.lower() not in composer_ids:
            composer_ids[composer.lower()] = next_id
            next_id += 1
    for track in tracks:
        if not track.album_id:
            identity = album_identity_from_track(track)
            track.album_id = album_ids.get((identity.album or "", identity.album_artist or identity.artist or ""), 0)
        if track.artist:
            track.artist_id = artist_ids.get(track.artist.lower(), 0)
        if track.composer:
            track.composer_id = composer_ids.get(track.composer.lower(), 0)
    return albums, artists, album_ids, next_id


def _bytes_of(ref: dict, key: str, size: int) -> bytes | None:
    value = ref.get(key)
    return bytes(value) if isinstance(value, (bytes, bytearray)) and len(value) == size else None


def write_mhbd(
    tracks,
    db_id=None,
    language="en",
    reference_info=None,
    playlists_type2=None,
    playlists_type3=None,
    playlists_type5=None,
    preserved_mhsd_blobs=None,
    capabilities=None,
    master_playlist_name="iPod",
    master_playlist_id=None,
    podcast_master_playlist_name=None,
    podcast_master_playlist_id=None,
    *,
    platform=None,
) -> bytes:
    """The complete, unsigned, uncompressed database image.

    *reference_info* (from the previous database) keeps header words, dataset
    order and ids stable across rewrites.
    """
    ref = dict(reference_info or {})
    tracks = list(tracks or [])
    db_version = _db_version(ref, capabilities)
    albums, artists, _album_ids, first_track_id = _assign_library_ids(tracks)

    db_id_2 = int(ref.get("db_id_2") or 0) or random.getrandbits(64)
    track_list, next_track_id = write_mhlt(tracks, first_track_id, db_id_2, capabilities=capabilities, db_version=db_version)
    track_ids = list(range(first_track_id, next_track_id))
    id_map = {int(t.db_track_id): tid for tid, t in zip(track_ids, tracks) if t.db_track_id}
    album_of = {tid: t.album or "" for tid, t in zip(track_ids, tracks)}

    visible = _remap_playlists(playlists_type2, id_map)
    podcast = _remap_playlists(playlists_type2 if playlists_type3 is None else playlists_type3, id_map)
    categories = _remap_playlists(playlists_type5, id_map)
    podcasts_ok = capabilities is None or bool(getattr(capabilities, "supports_podcast", True))
    blobs: dict[int, bytes] = {
        1: write_mhsd(1, track_list),
        2: write_mhsd(2, write_mhlp_with_playlists(
            track_ids, visible, db_id_2, tracks=tracks, capabilities=capabilities,
            master_playlist_name=master_playlist_name, master_playlist_id=master_playlist_id,
        )),
        3: write_mhsd(3, write_mhlp_with_playlists_type3(
            track_ids, podcast, db_id_2, album_of, tracks=tracks, capabilities=capabilities,
            master_playlist_name=podcast_master_playlist_name or master_playlist_name,
            next_mhip_id_start=next_track_id, master_playlist_id=podcast_master_playlist_id,
        )) if podcasts_ok else b"",
        4: write_mhsd(4, albums),
        5: write_mhsd(5, write_mhlp_smart(categories, db_id_2)),
        6: write_mhsd_empty_stub(6),
        8: write_mhsd(8, artists),
        10: write_mhsd_empty_stub(10),
    }
    datasets = [blobs[kind] for kind in _select_datasets(blobs, ref, capabilities)]
    datasets += [bytes(blob) for blob in preserved_mhsd_blobs or [] if blob]

    if db_id is None:
        db_id = int(ref.get("db_id") or 0) or generate_database_id()
    language_bytes = _bytes_of(ref, "language", 2) or str(language or "").encode("ascii", errors="ignore")[:2].ljust(2, b"\x00")
    if platform is None:
        platform = ref.get("platform") if ref.get("platform") in (1, 2) else 2
    if "hash_type_indicator" in ref:
        hash_type = int(ref.get("hash_type_indicator") or 0)
    else:
        hash_type = _HASH_TYPE_FOR.get(getattr(capabilities, "checksum", None) if capabilities is not None else None, 0)

    values: dict[str, Any] = {
        "compressed": 2 if capabilities is not None and getattr(capabilities, "supports_compressed_db", False) else 1,
        "version": db_version,
        "child_count": len(datasets),
        "db_id": db_id,
        "platform": platform,
        "unk0x22": ref.get("unk0x22", 611),
        "db_id_2": db_id_2,
        "unk0x2c": 0,
        "hashing_scheme": 0,
        "unk0x32": _bytes_of(ref, "unk0x32", 20) or bytes(20),
        "language": language_bytes,
        "db_persistent_id": int(ref.get("db_persistent_id") or 0) or db_id,
        "unk0x50": ref.get("unk0x50", 1),
        "unk0x54": ref.get("unk0x54", 15),
        "hash58": bytes(20),
        "timezone_offset": _timezone_offset(ref),
        "hash_type_indicator": hash_type,
        "hash72": bytes(46),
        "hashab": bytes(57),
    }
    values.update({k: ref[k] for k in ("audio_language", "subtitle_language", "unk0xa4", "unk0xa6", "cdb_flag") if k in ref})
    return record_bytes(b"mhbd", MHBD_HEADER_SIZE, values, b"".join(datasets))


# ── reading the previous database ───────────────────────────────────


def _inflated(data: bytes) -> bytes:
    from podsync.itdb.reader.entry import decompress_itunescdb

    return bytes(decompress_itunescdb(data))


def _datasets_of(data: bytes):
    """``(offset, header length, total length, type)`` of each top-level MHSD."""
    offset = struct.unpack_from("<I", data, 4)[0]
    for _ in range(struct.unpack_from("<I", data, 0x14)[0]):
        if offset + 16 > len(data) or data[offset:offset + 4] != b"mhsd":
            return
        header_length, total, kind = struct.unpack_from("<III", data, offset + 4)
        yield offset, header_length, total, kind
        if total <= 0:
            return
        offset += total


def extract_preserved_mhsd_blobs(itdb_data: bytes | bytearray | None) -> list[bytes]:
    """Datasets of kinds this writer does not generate, copied verbatim for the rewrite."""
    if not itdb_data or bytes(itdb_data[:4]) != b"mhbd" or len(itdb_data) < 0x18:
        return []
    data = _inflated(bytes(itdb_data))
    return [data[offset:offset + total] for offset, _h, total, kind in _datasets_of(data) if kind not in _GENERATED_DATASETS]


def extract_db_info(itdb_path: str) -> dict:
    """Decoded MHBD header fields of a database file."""
    with open(itdb_path, "rb") as handle:
        header = handle.read(MHBD_HEADER_SIZE)
    if len(header) < 12 or header[:4] != b"mhbd":
        raise ValueError(f"Not an iTunesDB file: {itdb_path}")
    return decode_fields(header, 0, "mhbd", min(struct.unpack_from("<I", header, 4)[0], len(header)))


def _reference_info_from_bytes(source: bytes) -> dict:
    """Header fields plus dataset kinds/order and the MHIT header size of the old file."""
    info = decode_fields(source, 0, "mhbd", struct.unpack_from("<I", source, 4)[0])
    data = _inflated(source)
    order: list[int] = []
    mhit_header_size = 0
    for offset, header_length, _total, kind in _datasets_of(data):
        if kind not in order:
            order.append(kind)
        if kind == 1 and not mhit_header_size:
            list_at = offset + header_length
            if data[list_at:list_at + 4] == b"mhlt":
                first = list_at + struct.unpack_from("<I", data, list_at + 4)[0]
                if data[first:first + 4] == b"mhit":
                    mhit_header_size = struct.unpack_from("<I", data, first + 4)[0]
    info.update(mhsd_types=set(order), mhsd_order=order, mhit_header_size=mhit_header_size)
    return info


# ── install helpers (module-level so they can be substituted) ───────


def _database_filename_for_capabilities(capabilities) -> str | None:
    if capabilities is None:
        return None
    return "iTunesCDB" if getattr(capabilities, "supports_compressed_db", False) else "iTunesDB"


def _resolve_existing_itdb_for_write(ipod_path: str, preferred: str | None) -> Path | None:
    """The non-empty database file already on the device (preferred name first)."""
    names = ["iTunesCDB", "iTunesDB"]
    if preferred in names:
        names.sort(key=lambda name: name != preferred)
    for name in names:
        candidate = Path(ipod_path) / "iPod_Control" / "iTunes" / name
        try:
            info = candidate.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"iPod database path is not a regular file: {candidate}")
        if info.st_size > 0:
            return candidate
    return None


def _validate_existing_itunesdb(data: bytes, path: Path) -> None:
    """Refuse to replace a database that is already damaged (it may be the only copy)."""
    if len(data) < MHBD_HEADER_SIZE or data[:4] != b"mhbd":
        raise RuntimeError(
            f"The existing iPod database is truncated or malformed: {path}. podsync stopped before replacing it."
        )
    header_length, total_length = struct.unpack_from("<II", data, 4)
    if header_length < MHBD_HEADER_SIZE or header_length > total_length or total_length > len(data):
        raise RuntimeError(
            f"The existing iPod database has invalid size fields: {path}. podsync stopped before replacing it."
        )
    if struct.unpack_from("<H", data, 0xA8)[0] == 1:
        try:
            zlib.decompress(data[header_length:])
        except zlib.error:
            raise RuntimeError(
                f"The existing compressed iPod database is corrupt: {path}. podsync stopped before replacing it."
            ) from None


def _preflight_database_install(
    ipod_path: str, itdb_path: str, size: int, capabilities=None, *, backup_sources: tuple = (),
) -> None:
    """Size limit and free-space checks for the staged file plus any backup copies."""
    profile = check_write_ready(ipod_path)
    firmware_limit = int(getattr(capabilities, "max_database_bytes", 0) or 0) if capabilities is not None else None
    ensure_file_fits(
        size,
        max_file_size_bytes=max_file_bytes(profile.max_file_size_bytes, firmware_limit or None),
        display_name=os.path.basename(str(itdb_path)) or "iTunes database",
    )
    unit = profile.allocation_unit_size
    needed = allocated_size(size, unit)
    counted: set[str] = set()
    for source in backup_sources:
        if not source:
            continue
        key = os.path.normcase(os.path.realpath(str(source)))
        if key in counted:
            continue
        counted.add(key)
        try:
            if os.path.exists(source):
                needed += allocated_size(os.stat(source).st_size, unit)
        except OSError as exc:
            raise UnsafeWriteError(f"Could not verify space needed to back up the existing iPod database: {exc}") from exc
    try:
        free = shutil.disk_usage(ipod_path).free
    except OSError as exc:
        raise UnsafeWriteError(f"Could not verify iPod free space before writing the database: {exc}") from exc
    if free < needed:
        raise UnsafeWriteError(
            "The iPod does not have enough free space to stage and safely commit its database. "
            f"At least {needed:,} bytes are required, but only {free:,} bytes are available. "
            "podsync stopped before replacing the database."
        )


def _copy_device_file_durably(source: Path, target: Path) -> None:
    """Copy via a synced temp file; refuse if the source changed during the copy."""
    before = source.stat()
    temp_path, temp_file = open_unique_sibling_temp(target, mode="wb")
    try:
        with temp_file as out, open(source, "rb") as src:
            shutil.copyfileobj(src, out)
            flush_written_file(out)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"Source changed while backing up {source}")
        safe_replace(temp_path, target)
    except BaseException:
        try:
            safe_unlink(temp_path, missing_ok=True)
        except OSError:
            pass
        raise


def _compress_for_cdb(data: bytes) -> bytes:
    """iTunesCDB: header kept, body zlib-compressed, total length and the CDB flag updated."""
    header = bytearray(data[:MHBD_HEADER_SIZE])
    body = zlib.compress(data[MHBD_HEADER_SIZE:], 1)
    struct.pack_into("<I", header, 8, len(header) + len(body))
    struct.pack_into("<H", header, 0xA8, 1)
    return bytes(header) + body


def _infer_checksum_from_source(source: bytes) -> SignatureKind:
    """Guess the signature scheme from a previous database's header."""
    (scheme,) = struct.unpack_from("<H", source, MHBD_OFFSET_HASHING_SCHEME)
    hash72_marker = source[_SIGNATURE_OFFSET:_SIGNATURE_OFFSET + 2] == b"\x01\x00"
    if scheme == 1 and any(source[0x58:0x6C]) and hash72_marker:
        return SignatureKind.HASH58
    if hash72_marker:
        return SignatureKind.HASH72
    return {1: SignatureKind.HASH58, 2: SignatureKind.HASH72}.get(scheme, SignatureKind.NONE)


# ── the install ─────────────────────────────────────────────────────


class _DatabaseInstall:
    """One run of :func:`write_itdb`; each step is a method, in order."""

    def __init__(self, ipod_path: str, tracks: list[TrackRecord], options: dict[str, Any]) -> None:
        import podsync.hardware as hardware

        self.hardware = hardware
        self.ipod_path = ipod_path
        self.tracks = tracks
        self.o = options
        self.capabilities = options["capabilities"]
        self.pending_artwork = None

    # hooks
    def progress(self, message: str) -> None:
        if self.o["progress_callback"] is not None:
            self.o["progress_callback"](message)

    def mutation(self) -> None:
        if self.o["before_device_mutation"] is not None:
            self.o["before_device_mutation"]()

    def abort_artwork(self) -> None:
        pending, self.pending_artwork = self.pending_artwork, None
        if pending is not None and hasattr(pending, "abort"):
            try:
                pending.abort(before_remove=self.o["before_device_mutation"])
            except Exception as exc:
                logger.warning("Could not abort pending artwork cleanly: %s", exc)

    # 1–3: which file, and what is there now
    def resolve_capabilities(self) -> None:
        if self.capabilities is not None:
            return
        try:
            device = self.hardware.selected_device_at(self.ipod_path)
            if device is not None:
                self.capabilities = self.hardware.traits_for_model(
                    getattr(device, "model_family", "") or "",
                    getattr(device, "generation", "") or "",
                    capacity=getattr(device, "capacity", None) or None,
                    model_number=getattr(device, "model_number", None) or None,
                )
        except Exception as exc:
            logger.debug("Could not derive device capabilities: %s", exc)
            self.capabilities = None

    def choose_file(self) -> None:
        preferred = _database_filename_for_capabilities(self.capabilities)
        self.existing_path = _resolve_existing_itdb_for_write(self.ipod_path, preferred)
        if preferred is not None:
            name = preferred
        elif self.existing_path is not None:
            name = self.existing_path.name
        else:
            try:
                name = self.hardware.database_filename_for_write(self.ipod_path) or "iTunesDB"
            except Exception:
                name = "iTunesDB"
        self.db_filename = name
        self.itdb_path = safe_device_path(self.ipod_path, f"iPod_Control/iTunes/{name}", allowed_subtree="iPod_Control/iTunes")
        logger.debug("Target database file: %s", self.itdb_path)

    def read_existing(self) -> None:
        self.existing = b""
        if self.existing_path is None:
            return
        try:
            with open(self.existing_path, "rb") as handle:  # plain open(): callers may intercept it
                self.existing = handle.read()
        except OSError as exc:
            raise RuntimeError(
                f"The existing iPod database could not be read safely: {self.existing_path}. "
                f"podsync stopped before replacing it: {exc}"
            ) from exc
        if not self.existing:
            raise RuntimeError(
                "The existing iPod database became empty while it was being read. podsync stopped before replacing it."
            )
        _validate_existing_itunesdb(self.existing, self.existing_path)

    # 4: reference material and platform flag
    def read_reference(self) -> None:
        reference = b""
        if self.o["reference_itdb_path"]:
            try:
                with open(self.o["reference_itdb_path"], "rb") as handle:
                    reference = handle.read()
            except OSError as exc:
                logger.warning("Could not read reference iTunesDB %s: %s", self.o["reference_itdb_path"], exc)
        self.reference_bytes = reference
        if self.o["db_id"] is None and len(self.existing) >= 32:
            self.o["db_id"] = struct.unpack_from("<Q", self.existing, 24)[0] or None
        self.source = reference if reference[:4] == b"mhbd" else self.existing
        self.reference_info = None
        if self.source[:4] == b"mhbd" and len(self.source) >= MHBD_HEADER_SIZE:
            try:
                self.reference_info = _reference_info_from_bytes(self.source)
            except Exception as exc:
                logger.warning("Could not extract reference database information: %s", exc)

    def choose_platform(self) -> None:
        stored, origin = None, ""
        for blob, label in ((self.existing, "existing_database"), (self.reference_bytes, "reference_database")):
            if len(blob) >= 0x22 and struct.unpack_from("<H", blob, 0x20)[0] in (1, 2):
                stored, origin = struct.unpack_from("<H", blob, 0x20)[0], label
                break
        resolution = resolve_itunesdb_platform(filesystem_type=detect_volume_format(self.ipod_path), reference_platform=stored)
        logger.info(
            "iTunesDB platform selection: flag=%d (%s) source=%s filesystem=%s reference=%s",
            resolution.flag, _PLATFORM_NAMES.get(resolution.flag, "?"), origin or resolution.source,
            resolution.filesystem_type or "unknown", stored if stored is not None else "none",
        )
        if resolution.mismatch:
            evidence = "the existing on-device database" if origin == "existing_database" else "the supplied reference database"
            logger.warning(
                "iTunesDB platform/filesystem mismatch: preserving flag=%d (%s) from %s although filesystem=%s suggests %s",
                resolution.flag, _PLATFORM_NAMES.get(resolution.flag, "?"), evidence,
                resolution.filesystem_type, _PLATFORM_NAMES.get(resolution.inferred_flag, "?"),
            )
        self.platform = resolution.flag

    # 5–6: ids and artwork
    def stage_artwork(self) -> None:
        for track in self.tracks:
            if not track.db_track_id:
                track.db_track_id = generate_db_track_id()
        if self.o["pc_file_paths"] is None:
            self.progress("Skipping artwork (no sources)")
            return
        from podsync.artwork.writer import pipeline as artwork_writer

        formats = None
        if self.capabilities is not None and not getattr(self.capabilities, "supports_artwork", True):
            try:
                fallback = self.hardware.ITHMB_FORMAT_MAP[1060]
                formats = {1060: (fallback.width, fallback.height)}
                self.progress("Artwork — generating podsync-only artwork")
                logger.info("ART: device reports no artwork support; writing fallback format 1060 for podsync view")
            except Exception as exc:
                logger.warning("ART: could not resolve fallback artwork format: %s", exc)
        artwork_db = Path(self.ipod_path) / "iPod_Control" / "Artwork" / "ArtworkDB"

        def artwork_mutation() -> None:
            self.mutation()
            if self.o["before_database_replace"] is not None:
                self.o["before_database_replace"]()

        try:
            result = artwork_writer.write_artworkdb(
                self.ipod_path, self.tracks, self.o["pc_file_paths"],
                reference_artdb_path=str(artwork_db) if artwork_db.exists() else None,
                artwork_formats=formats, defer_commit=True,
                progress_callback=self.o["progress_callback"], before_device_mutation=artwork_mutation,
            )
            if hasattr(result, "commit") and hasattr(result, "abort"):
                self.pending_artwork = result
                links = result.db_track_id_to_art_info
            else:
                links = result or {}
            for track in self.tracks:
                link = links.get(track.db_track_id)
                if link:
                    track.mhii_link, track.artwork_size, track.artwork_count = int(link[0]), int(link[1]), 1
                else:
                    track.mhii_link = track.artwork_count = track.artwork_size = 0
        except Exception:
            self.abort_artwork()
            logger.exception("Artwork stage failed")
            raise

    # 7–8: build (and compress)
    def build(self) -> bytearray:
        self.progress("Building database structure")
        preserved = extract_preserved_mhsd_blobs(self.existing)
        clock = None
        if self.reference_info is not None:
            clock = load_device_clock(self.ipod_path, database_offset=self.reference_info.get("timezone_offset"))

        def assemble() -> bytes:
            return write_mhbd(
                self.tracks, db_id=self.o["db_id"], reference_info=self.reference_info,
                playlists_type2=self.o["playlists"], playlists_type3=self.o["podcast_playlists"],
                playlists_type5=self.o["smart_playlists"], preserved_mhsd_blobs=preserved,
                capabilities=self.capabilities, master_playlist_name=self.o["master_playlist_name"],
                master_playlist_id=self.o["master_playlist_id"],
                podcast_master_playlist_name=self.o["podcast_master_playlist_name"],
                podcast_master_playlist_id=self.o["podcast_master_playlist_id"], platform=self.platform,
            )

        if clock is not None:
            with device_clock_scope(clock):
                data = assemble()
        else:
            data = assemble()
        if self.db_filename == "iTunesCDB":
            data = _compress_for_cdb(data)
        return bytearray(data)

    # 9: sign
    def signature_kind(self) -> SignatureKind:
        if self.o["force_checksum"] is not None:
            kind = SignatureKind(self.o["force_checksum"])
        elif self.capabilities is not None and SignatureKind(self.capabilities.checksum) != SignatureKind.UNKNOWN:
            kind = SignatureKind(self.capabilities.checksum)
        else:
            kind = SignatureKind(self.hardware.detect_signature_kind(self.ipod_path))
        if kind == SignatureKind.NONE and len(self.source) >= 0xA0 and self.source[:4] == b"mhbd":
            kind = _infer_checksum_from_source(self.source)
        return kind

    def _sign_hash58(self, image: bytearray) -> str:
        struct.pack_into("<H", image, MHBD_OFFSET_HASHING_SCHEME, 1)
        source = self.source
        if len(source) >= 0xA0 and source[_SIGNATURE_OFFSET:_SIGNATURE_OFFSET + 2] == b"\x01\x00":
            material = extract_hash_info_to_dict(source)
            if material is not None:
                image[_SIGNATURE_OFFSET:_SIGNATURE_OFFSET + _SIGNATURE_LENGTH] = hash72_signature(
                    signature_digest(image), material["iv"], material["rndpart"],
                )
        firewire = self.o["firewire_id"]
        if not firewire:
            try:
                firewire = self.hardware.get_firewire_id(self.ipod_path)
            except RuntimeError:
                firewire = None
        if not firewire:
            return (
                "No FireWire ID is available to compute the required HASH58 signature. "
                "podsync stopped before writing a database the iPod firmware would reject."
            )
        write_hash58(image, firewire)
        return ""

    def _hash72_material(self) -> HashInfo | None:
        try:
            device = self.hardware.selected_device_at(self.ipod_path)
        except Exception:
            device = None
        iv = bytes(getattr(device, "hash_info_iv", b"") or b"") if device is not None else b""
        rnd = bytes(getattr(device, "hash_info_rndpart", b"") or b"") if device is not None else b""
        if len(iv) == 16 and len(rnd) == 12:
            return HashInfo(bytes(20), rnd, iv)
        material = read_hash_info(self.ipod_path)
        if material is None and self.source:
            extracted = extract_hash_info_to_dict(self.source)
            if extracted is not None:
                material = HashInfo(bytes(20), extracted["rndpart"], extracted["iv"])
        return material

    def _sign_hash72(self, image: bytearray) -> str:
        material = self._hash72_material()
        if material is None:
            return (
                "No valid HashInfo material is available to compute the required HASH72 signature. "
                "podsync stopped before writing a database the iPod firmware would reject."
            )
        struct.pack_into("<H", image, MHBD_OFFSET_HASHING_SCHEME, 2)
        image[_SIGNATURE_OFFSET:_SIGNATURE_OFFSET + _SIGNATURE_LENGTH] = hash72_signature(
            signature_digest(image), material.iv, material.rndpart,
        )
        return ""

    def _sign_hashab(self, image: bytearray) -> str:
        firewire = self.o["firewire_id"]
        if not firewire:
            try:
                firewire = self.hardware.get_firewire_id(self.ipod_path)
            except RuntimeError:
                firewire = None
        if not firewire:
            return (
                "No FireWire ID is available to compute the required HASHAB signature. "
                "podsync stopped before writing a database the iPod firmware would reject."
            )
        try:
            write_hashab(image, firewire)
        except ImportError as exc:
            return str(exc)
        return ""

    def sign(self, image: bytearray) -> str:
        """Sign *image* in place; returns an error message when signing is impossible."""
        self.progress("Signing database")
        kind = self.signature_kind()
        if kind == SignatureKind.HASH58:
            return self._sign_hash58(image)
        if kind == SignatureKind.HASH72:
            return self._sign_hash72(image)
        if kind == SignatureKind.HASHAB:
            return self._sign_hashab(image)
        refusals = {
            SignatureKind.UNSUPPORTED: "Device requires an unsupported hashing scheme",
            SignatureKind.UNKNOWN: (
                "Cannot write iTunesDB: device checksum type is UNKNOWN. The device was not fully identified "
                "— the iPod will reject this database. Please report this as a bug."
            ),
        }
        if kind in refusals:
            return refusals[kind]
        struct.pack_into("<H", image, MHBD_OFFSET_HASHING_SCHEME, 0)
        return ""

    # 10–12: check, back up, commit
    def preflight(self, image: bytearray) -> None:
        sources = (str(self.itdb_path), str(self.existing_path) if self.existing_path else "") if self.o["backup"] else ()
        try:
            self.mutation()
            _preflight_database_install(self.ipod_path, str(self.itdb_path), len(image), self.capabilities, backup_sources=sources)
        except FileTooLargeError as exc:
            self.abort_artwork()
            exc.proposed_database_bytes = bytes(image)  # lets callers offer "save instead"
            exc.proposed_database_filename = self.db_filename
            raise
        except Exception:
            self.abort_artwork()
            raise

    def back_up(self) -> bool:
        if not self.o["backup"]:
            return True
        try:
            done: set[str] = set()
            for candidate in (self.itdb_path, self.existing_path):
                if candidate is None:
                    continue
                candidate = Path(candidate)
                key = os.path.normcase(os.path.realpath(candidate))
                if key in done or not candidate.exists():
                    continue
                done.add(key)
                self.mutation()
                _copy_device_file_durably(candidate, candidate.with_name(candidate.name + ".backup"))
            return True
        except Exception as exc:
            logger.error("Could not back up the existing iPod database: %s", exc)
            self.abort_artwork()
            return False

    def commit(self, image: bytearray) -> bool:
        staged: list[Path] = []
        try:
            self.progress("Writing to iPod")
            self.mutation()
            temp_path, temp_file = open_unique_sibling_temp(self.itdb_path, mode="wb")
            staged.append(temp_path)
            with temp_file as out:
                out.write(bytes(image))
                flush_written_file(out)
            if self.pending_artwork is not None:
                self.pending_artwork.commit(before_replace=self.o["before_device_mutation"])
                self.pending_artwork = None
            self.mutation()
            if self.o["before_database_replace"] is not None:
                self.o["before_database_replace"]()
            safe_replace(temp_path, self.itdb_path)
            staged.remove(temp_path)
            self._empty_stale_variant(staged)
            logger.info("Committed %s (%d bytes)", self.itdb_path, len(image))
            return True
        except Exception as exc:
            logger.error("Could not write the iPod database: %s", exc, exc_info=True)
            for temp in staged:
                try:
                    safe_unlink(temp, missing_ok=True)
                except OSError:
                    pass
            self.abort_artwork()
            return False

    def _empty_stale_variant(self, staged: list[Path]) -> None:
        """When switching iTunesDB ⇄ iTunesCDB, truncate the other file so firmware ignores it."""
        if self.existing_path is None or Path(self.existing_path).name == self.db_filename:
            return
        stale = Path(self.existing_path)
        if not stale.exists():
            return
        self.mutation()
        empty_path, empty_file = open_unique_sibling_temp(stale, mode="wb")
        staged.append(empty_path)
        with empty_file as out:
            flush_written_file(out)
        safe_replace(empty_path, stale)
        staged.remove(empty_path)

    def run(self) -> bool:
        self.progress("Preparing database")
        self.resolve_capabilities()
        self.choose_file()
        self.read_existing()
        self.read_reference()
        self.choose_platform()
        self.stage_artwork()
        try:
            image = self.build()
            problem = self.sign(image)
            if problem:
                logger.error(problem)
                self.abort_artwork()
                return False
        except Exception:
            self.abort_artwork()
            raise
        self.preflight(image)
        return self.back_up() and self.commit(image)


def write_itdb(
    ipod_path: str,
    tracks: list[TrackRecord],
    db_id: int | None = None,
    backup: bool = True,
    force_checksum: SignatureKind | None = None,
    firewire_id: bytes | None = None,
    reference_itdb_path: str | None = None,
    pc_file_paths: dict | None = None,
    playlists: list[PlaylistRecord] | None = None,
    podcast_playlists: list[PlaylistRecord] | None = None,
    smart_playlists: list[PlaylistRecord] | None = None,
    capabilities=None,
    master_playlist_name: str = "iPod",
    master_playlist_id: int | None = None,
    podcast_master_playlist_name: str | None = None,
    podcast_master_playlist_id: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    before_database_replace: Callable[[], None] | None = None,
    before_device_mutation: Callable[[], None] | None = None,
) -> bool:
    """Build, sign and atomically install the database; ``False`` when it was not written.

    Raises for unreadable/damaged existing databases, size-limit and free-space
    refusals, and artwork failures; returns ``False`` for signing problems and
    I/O failures during backup or commit.
    """
    options = {
        "db_id": db_id, "backup": backup, "force_checksum": force_checksum, "firewire_id": firewire_id,
        "reference_itdb_path": reference_itdb_path, "pc_file_paths": pc_file_paths, "playlists": playlists,
        "podcast_playlists": podcast_playlists, "smart_playlists": smart_playlists, "capabilities": capabilities,
        "master_playlist_name": master_playlist_name, "master_playlist_id": master_playlist_id,
        "podcast_master_playlist_name": podcast_master_playlist_name,
        "podcast_master_playlist_id": podcast_master_playlist_id, "progress_callback": progress_callback,
        "before_database_replace": before_database_replace, "before_device_mutation": before_device_mutation,
    }
    return _DatabaseInstall(str(ipod_path), list(tracks or []), options).run()
