"""Giving a freshly formatted (or virtual) iPod an empty iTunes database.

This is the only place that creates device files outside a sync, so it runs
the full safety sequence: exact model known, the path is the identified
device's root, ``iPod_Control`` really lives inside it, the volume passes the
write-readiness checks, and everything happens under the device write lock
with the volume re-checked before every mutation and flushed at the end.
Nothing is created when the model's signature material is missing — a
database the iPod cannot verify would look like an empty library to it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from podsync.hardware.safety.guard import UnsafeWriteError, WriteLock

__all__ = ["ensure_device_itunes_database"]

logger = logging.getLogger(__name__)

_CONTROL_SUBDIRS = ("Device", "iTunes", "Music", "Artwork")


def _checksum_material_available(root: str, device_info) -> bool:
    """Whether the signature the model needs can be computed right now."""
    from podsync.hardware.catalog.checksum import SignatureKind
    from podsync.hardware.current import get_firewire_id

    kind = SignatureKind(int(getattr(device_info, "checksum_type", SignatureKind.UNKNOWN)))
    if kind == SignatureKind.NONE:
        return True
    if kind in (SignatureKind.HASH58, SignatureKind.HASHAB):
        try:
            guid = get_firewire_id(root, known_guid=getattr(device_info, "firewire_guid", None))
        except RuntimeError:
            return False
        return 8 <= len(guid) <= 20
    if kind == SignatureKind.HASH72:
        iv = getattr(device_info, "hash_info_iv", b"") or b""
        rndpart = getattr(device_info, "hash_info_rndpart", b"") or b""
        if len(iv) == 16 and len(rndpart) == 12:
            return True
        try:
            blob = (Path(root) / "iPod_Control" / "Device" / "HashInfo").read_bytes()
        except OSError:
            return False
        return len(blob) == 54 and blob.startswith(b"HASHv0")
    return False


def _verify_root(root: str, device_info) -> None:
    """Raise :class:`UnsafeWriteError` unless *root* is the identified iPod's own root."""
    from podsync.hardware.safety.paths import PathEscapeError, safe_device_path

    if not os.path.isdir(root):
        raise UnsafeWriteError(f"The selected iPod root is not an accessible directory: {root}")
    identified = str(getattr(device_info, "path", "") or "")
    if not identified:
        raise UnsafeWriteError("The identified iPod does not include a verified mount path. podsync stopped "
                               "before creating device files.")
    if os.path.normcase(os.path.realpath(identified)) != os.path.normcase(os.path.realpath(root)):
        raise UnsafeWriteError("The selected iPod path does not match the device that was identified. "
                               f"Selected path: {root}; identified path: {identified}.")
    if not (Path(root) / "iPod_Control").is_dir():
        raise UnsafeWriteError("The selected volume is not a verified iPod root: iPod_Control is missing. "
                               "podsync stopped before creating device files.")
    try:
        safe_device_path(root, "iPod_Control", allowed_subtree="iPod_Control")
    except PathEscapeError as exc:
        raise UnsafeWriteError("The selected iPod_Control directory resolves outside the iPod root. podsync "
                               "stopped before creating device files.") from exc


def ensure_device_itunes_database(ipod_path: str, device_info) -> str | None:
    """Path of the device's database, creating an empty one if there is none.

    ``None`` when the model has no capability profile or its signature
    material is unavailable; raises when the path or volume is unsafe or the
    write / flush fails.
    """
    from podsync.hardware import current
    from podsync.hardware.catalog.capabilities import traits_for_model
    from podsync.hardware.safety.durable import flush_volume
    from podsync.hardware.safety.readiness import check_write_ready, lock_key_for, recheck_write_ready

    current.require_exact_model_number(device_info)
    root = str(ipod_path)
    _verify_root(root, device_info)
    if existing := current.locate_database(root):
        return existing
    traits = traits_for_model(device_info.model_family, device_info.generation,
                              capacity=getattr(device_info, "capacity", None) or None,
                              model_number=device_info.model_number or None)
    if traits is None:
        logger.info("No capability profile for %s; not creating a database", device_info.model_number)
        return None

    profile = check_write_ready(root, reported_volume_format=getattr(device_info, "reported_volume_format", "") or "")
    with WriteLock(root, volume_key=lock_key_for(profile)) as lock:
        latest = [recheck_write_ready(profile, probe_case_sensitivity=True)]

        def revalidate_volume() -> None:
            latest[0] = recheck_write_ready(latest[0])

        if existing := current.locate_database(root):  # another process may have won the race
            return existing
        if not _checksum_material_available(root, device_info):
            logger.warning("Signature material for %s is unavailable; not creating a database", root)
            return None

        previous = current.selected_device()
        try:
            current.select_device(device_info)
            control = Path(root) / "iPod_Control"
            for name in _CONTROL_SUBDIRS:
                revalidate_volume()
                (control / name).mkdir(parents=True, exist_ok=True)
            if traits.uses_sqlite_db:
                revalidate_volume()
                (control / "iTunes" / "iTunes Library.itlp").mkdir(parents=True, exist_ok=True)
            from podsync.itdb.writer.database import write_itdb

            ok = write_itdb(
                root, [], backup=False, capabilities=traits,
                master_playlist_name=getattr(device_info, "ipod_name", "") or getattr(device_info, "mount_name", "")
                or "iPod",
                before_database_replace=lock.assert_database_unchanged,
                before_device_mutation=revalidate_volume,
            )
        finally:
            with current._CURRENT_LOCK:  # restore exactly, even a "no device" state
                current._CURRENT_DEVICE = previous
        if not ok:
            raise RuntimeError("Failed to create an empty iTunesDB for the selected iPod")
        lock.refresh_database_generation()
        revalidate_volume()
        flushed, message = flush_volume(root)
        if not flushed:
            raise RuntimeError(f"Created the empty iTunesDB, but its durability barrier failed: {message}")
    logger.info("Created an empty iTunesDB on %s", root)
    return current.locate_database(root)
