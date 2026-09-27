"""Writing small device metadata files (SysInfo, HashInfo …) under a volume lock.

A session holds the host-side write lock for one verified volume.  Every write
re-checks that the same volume is still mounted and safe, stays inside its
allowed subtree, respects the filesystem's limits and free space, and lands
atomically via a synced temp file.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from podsync.hardware.safety.durable import flush_parent_directory, flush_written_file, safe_replace, safe_unlink
from podsync.hardware.safety.fsprofile import VolumeProfile
from podsync.hardware.safety.guard import UnsafeWriteError, WriteLock
from podsync.hardware.safety.limits import allocated_size, ensure_file_fits
from podsync.hardware.safety.paths import safe_device_path
from podsync.hardware.safety.readiness import check_write_ready, lock_key_for, recheck_write_ready

__all__ = ["SysInfoWriteSession", "sysinfo_write_session"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SysInfoWriteSession:
    mount_path: Path
    filesystem_profile: VolumeProfile

    def revalidate(self) -> VolumeProfile:
        self.filesystem_profile = recheck_write_ready(self.filesystem_profile)
        return self.filesystem_profile

    def _target(self, relative_path: str | Path, allowed_subtree: str | Path) -> Path:
        return safe_device_path(self.mount_path, relative_path, allowed_subtree=allowed_subtree)

    def _check_name(self, name: str) -> None:
        limit = int(self.filesystem_profile.max_component_length or 0)
        if limit > 0 and len(name) > limit:
            raise UnsafeWriteError(
                f"The metadata filename {name!r} exceeds this iPod filesystem's {limit}-character component limit."
            )

    def _check_space(self, size: int, display_name: str) -> None:
        try:
            free = shutil.disk_usage(self.mount_path).free
        except OSError as exc:
            raise UnsafeWriteError(f"Could not verify iPod free space before writing {display_name}: {exc}") from exc
        if free < allocated_size(size, self.filesystem_profile.allocation_unit_size):
            raise UnsafeWriteError(
                f"The iPod does not have enough free space to safely write {display_name}. "
                "podsync stopped before creating the file."
            )

    def _make_parent(self, relative_path: str | Path, allowed_subtree: str | Path) -> None:
        self.revalidate()
        parent = self._target(relative_path, allowed_subtree).parent
        if not parent.is_dir():
            parent.mkdir(parents=True, exist_ok=True)
            flush_parent_directory(parent)

    def _discard(self, temp: Path) -> None:
        """Remove a leftover temp file — but only if the volume is still the right one."""
        try:
            self.revalidate()
            safe_unlink(temp, missing_ok=True)
        except Exception as exc:
            logger.warning("Could not safely remove temporary iPod metadata file %s: %s", temp, exc)

    def write_bytes_atomic(self, relative_path: str | Path, data: bytes, *, allowed_subtree: str | Path) -> Path:
        payload = bytes(data)
        target = self._target(relative_path, allowed_subtree)
        self._check_name(target.name)
        ensure_file_fits(len(payload), max_file_size_bytes=self.filesystem_profile.max_file_size_bytes,
                         display_name=target.name)
        self._check_space(len(payload), target.name)
        self._make_parent(relative_path, allowed_subtree)
        self.revalidate()
        target = self._target(relative_path, allowed_subtree)
        fd, name = tempfile.mkstemp(dir=str(target.parent), prefix=".iop-", suffix=".tmp")
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as out:
                fd = -1
                out.write(payload)
                flush_written_file(out)
            self.revalidate()
            target = self._target(relative_path, allowed_subtree)  # resolve again right before the swap
            safe_replace(temp, target)
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._discard(temp)
            raise
        logger.debug("Wrote device metadata %s (%d bytes)", target, len(payload))
        return target

    def write_text_atomic(self, relative_path: str | Path, text: str, *, allowed_subtree: str | Path,
                          encoding: str = "utf-8") -> Path:
        return self.write_bytes_atomic(relative_path, text.encode(encoding), allowed_subtree=allowed_subtree)

    def delete(self, relative_path: str | Path, *, allowed_subtree: str | Path, missing_ok: bool = False) -> None:
        self.revalidate()
        safe_unlink(self._target(relative_path, allowed_subtree), missing_ok=missing_ok)


@contextmanager
def sysinfo_write_session(mount_path: str | Path, *, reported_volume_format: str = "",
                          expected_volume_identity_key: str = "") -> Iterator[SysInfoWriteSession]:
    """A locked session for metadata writes; refuses if another volume is now mounted there."""
    profile = check_write_ready(mount_path, reported_volume_format=reported_volume_format)
    key = lock_key_for(profile)
    if expected_volume_identity_key and expected_volume_identity_key != key:
        raise UnsafeWriteError(
            "A different volume is mounted at the selected iPod path. podsync stopped before writing device metadata."
        )
    with WriteLock(mount_path, volume_key=key):
        profile = recheck_write_ready(profile, probe_case_sensitivity=True)
        yield SysInfoWriteSession(mount_path=Path(os.path.realpath(mount_path)), filesystem_profile=profile)
