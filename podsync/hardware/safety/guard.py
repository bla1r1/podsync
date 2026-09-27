"""One writer per iPod: a host-wide lock plus a check that nobody else changed the database.

:class:`WriteLock` serializes writers to the same volume on two levels:

* inside this process, threads queue fairly (FIFO) — or fail fast when asked;
* across processes, an exclusive byte lock on a private file in the temp dir.

On entry it also fingerprints the iTunesDB (size, mtime, inode, SHA-256) so a
session can refuse to overwrite changes made by another app since the
library was loaded.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DatabaseChangedElsewhereError", "DatabaseGeneration", "IpodBusyError", "UnsafeWriteError", "WriteLock",
    "snapshot_database_state",
]

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_ATTR_DIRECTORY = 0x10
_ATTR_REPARSE_POINT = 0x400


class UnsafeWriteError(RuntimeError):
    """A device write was refused because safety could not be established."""


class IpodBusyError(UnsafeWriteError):
    """Another writer already holds the iPod."""


class DatabaseChangedElsewhereError(UnsafeWriteError):
    """The iPod database was changed by something outside this session."""


# ── database fingerprint ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DatabaseGeneration:
    filename: str
    exists: bool
    size: int = 0
    modified_ns: int = 0
    device: int = 0
    inode: int = 0
    digest: str = ""


def _database_path(ipod_path: str | Path) -> Path:
    from podsync.hardware import current as info

    located = info.locate_database(str(ipod_path))
    if located is not None:
        return Path(located)
    return Path(ipod_path) / "iPod_Control" / "iTunes" / info.database_filename_for_write(str(ipod_path))


def _stat_key(details: os.stat_result) -> tuple[int, int, int, int]:
    return details.st_size, details.st_mtime_ns, details.st_dev, details.st_ino


def snapshot_database_state(ipod_path: str | Path) -> DatabaseGeneration:
    """Fingerprint the device database; refuses if it changes while being read."""
    database = _database_path(ipod_path)
    try:
        before = database.stat()
    except FileNotFoundError:
        return DatabaseGeneration(filename=database.name, exists=False)
    except OSError as exc:
        raise UnsafeWriteError(f"Could not inspect the iPod database before writing: {exc}") from exc
    digest = hashlib.sha256()
    try:
        with open(database, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
        after = database.stat()
    except OSError as exc:
        raise UnsafeWriteError(f"Could not read the iPod database generation safely: {exc}") from exc
    if _stat_key(before) != _stat_key(after):
        raise DatabaseChangedElsewhereError(
            "The iPod database changed while podsync was inspecting it. Close other device-management apps, "
            "reload the library, and try again."
        )
    size, modified, device, inode = _stat_key(after)
    return DatabaseGeneration(database.name, True, size, modified, device, inode, digest.hexdigest())


def _same_generation(left: DatabaseGeneration, right: DatabaseGeneration) -> bool:
    return (not left.exists and not right.exists) or left == right


# ── in-process fair queue ───────────────────────────────────────────


class _ProcessQueue:
    """Per-key FIFO of writer threads inside this process."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.active: set[str] = set()
        self.owner: dict[str, int] = {}
        self.waiting: dict[str, deque[object]] = {}

    def acquire(self, key: str, *, wait: bool) -> None:
        me = threading.get_ident()
        with self.condition:
            if self.owner.get(key) == me:
                raise IpodBusyError(
                    "This podsync operation is already writing to this iPod. Nested device write sessions are "
                    "not supported."
                )
            if not wait:
                if key in self.active:
                    raise IpodBusyError(
                        "podsync is already writing to this location. Wait for that operation to finish, then try again."
                    )
            else:
                ticket = object()
                line = self.waiting.setdefault(key, deque())
                line.append(ticket)
                try:
                    while key in self.active or self.waiting[key][0] is not ticket:
                        self.condition.wait()
                except BaseException:
                    self._leave_line(key, ticket)
                    raise
                self._leave_line(key, ticket, notify=False)
            self.active.add(key)
            self.owner[key] = me

    def _leave_line(self, key: str, ticket: object, *, notify: bool = True) -> None:
        line = self.waiting.get(key)
        if line is not None:
            try:
                line.remove(ticket)
            except ValueError:
                pass
            if not line:
                self.waiting.pop(key, None)
        if notify:
            self.condition.notify_all()

    def release(self, key: str) -> None:
        with self.condition:
            self.active.discard(key)
            self.owner.pop(key, None)
            self.condition.notify_all()


_QUEUE = _ProcessQueue()


def _lock_identity(identity: str) -> str:
    """Drop the mount-instance part of a volume key so a remount still maps to the same lock."""
    parts = identity.split("|")
    return "|".join(parts[:3]) if len(parts) == 4 and all(parts[:3]) else identity


def _default_lock_dir() -> Path:
    name = "podsync-device-locks"
    getuid = getattr(os, "getuid", None)
    return Path(tempfile.gettempdir()) / (f"{name}-{getuid()}" if callable(getuid) else name)


# ── the host lock file ──────────────────────────────────────────────


def _is_link_or_special(details: os.stat_result, *, directory_ok: bool) -> bool:
    attributes = getattr(details, "st_file_attributes", 0) or 0
    forbidden = _ATTR_REPARSE_POINT | (0 if directory_ok else _ATTR_DIRECTORY)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & forbidden)


def _secure_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = directory.lstat()
    except OSError as exc:
        raise UnsafeWriteError(f"Could not create the host-side iPod lock directory safely: {exc}") from exc
    if _is_link_or_special(details, directory_ok=True) or not stat.S_ISDIR(details.st_mode):
        raise UnsafeWriteError(
            "The host-side iPod lock directory is a link, reparse point, or non-directory. podsync stopped before writing."
        )
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        if details.st_uid != geteuid():
            raise UnsafeWriteError("The host-side iPod lock directory is owned by another user. podsync stopped before writing.")
        try:
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise UnsafeWriteError(f"Could not secure the host-side iPod lock directory: {exc}") from exc


def _open_posix(path: Path):
    flags = os.O_RDWR | os.O_CREAT
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise UnsafeWriteError(f"Could not open the host-side iPod lock file safely: {exc}") from exc
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode):
            raise UnsafeWriteError("The host-side iPod lock path is not a regular file. podsync stopped before writing.")
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and details.st_uid != geteuid():
            raise UnsafeWriteError("The host-side iPod lock file is owned by another user. podsync stopped before writing.")
        if callable(fchmod := getattr(os, "fchmod", None)):
            fchmod(fd, 0o600)
        return os.fdopen(fd, "r+b")
    except BaseException:
        os.close(fd)
        raise


_NOT_A_FILE = "The host-side iPod lock path is a link, reparse point, or non-file. podsync stopped before writing."


def _open_windows(path: Path):
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise UnsafeWriteError(f"Could not verify the host-side iPod lock file safely: {exc}") from exc
    if existing is not None and (_is_link_or_special(existing, directory_ok=False) or not stat.S_ISREG(existing.st_mode)):
        raise UnsafeWriteError(_NOT_A_FILE)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0), 0o600)
    except OSError as exc:
        raise UnsafeWriteError(f"Could not open the host-side iPod lock file safely: {exc}") from exc
    try:
        if _is_link_or_special(os.lstat(path), directory_ok=False):  # re-check: it may have been swapped
            raise UnsafeWriteError(_NOT_A_FILE)
        return os.fdopen(fd, "r+b")
    except UnsafeWriteError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise UnsafeWriteError(f"Could not verify the host-side iPod lock file safely: {exc}") from exc


def _lock_byte(file, *, lock: bool) -> None:
    """Exclusive, non-blocking lock (or unlock) of the file's first byte."""
    if sys.platform == "win32":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)


# ── the lock ────────────────────────────────────────────────────────


class WriteLock:
    """Context manager holding exclusive write access to one iPod.

    *expected_database_generation*, when given, must match the database found on
    entry (the library the caller loaded is still current).
    """

    def __init__(
        self,
        ipod_path: str | Path,
        *,
        volume_key: str = "",
        expected_database_generation: DatabaseGeneration | None = None,
        track_database_generation: bool = True,
        lock_dir: str | Path | None = None,
        queue_in_process: bool = True,
    ) -> None:
        self.ipod_path = Path(os.path.realpath(ipod_path))
        identity = _lock_identity(volume_key.strip() if volume_key else str(self.ipod_path).strip())
        self._writer_key = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
        self.lock_path = (Path(lock_dir) if lock_dir is not None else _default_lock_dir()) / f"{self._writer_key}.lock"
        self._expected_generation = expected_database_generation
        self._track_database_generation = bool(track_database_generation)
        self._queue_in_process = bool(queue_in_process)
        self._file = None
        self._locked = False
        self._entered = False
        self._queued = False
        self._database_generation: DatabaseGeneration | None = None

    def _open_and_lock(self) -> None:
        _secure_directory(self.lock_path.parent)
        self._file = _open_windows(self.lock_path) if sys.platform == "win32" else _open_posix(self.lock_path)
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:  # need a byte to lock
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)
        try:
            _lock_byte(self._file, lock=True)
        except OSError as exc:
            raise IpodBusyError(
                "Another podsync process is already writing to this iPod. Wait for that operation to finish, then try again."
            ) from exc
        self._locked = True
        self._file.seek(1)
        self._file.truncate()
        self._file.write(f"pid={os.getpid()} mount={self.ipod_path}\n".encode("utf-8", errors="replace"))
        self._file.flush()

    def _check_generation(self) -> None:
        current = snapshot_database_state(self.ipod_path) if self._track_database_generation else None
        if self._expected_generation is not None:
            if current is None:
                raise UnsafeWriteError(
                    "The expected iPod database generation cannot be verified while database tracking is disabled. "
                    "podsync stopped before writing."
                )
            if not _same_generation(current, self._expected_generation):
                raise DatabaseChangedElsewhereError(
                    "The iPod database changed since the iPod library was loaded. podsync stopped before overwriting "
                    "those newer changes. Reload the iPod library and try again."
                )
        self._database_generation = current

    def __enter__(self) -> WriteLock:
        if self._entered or self._file is not None or self._queued:
            raise RuntimeError("DeviceWriteGuard instances cannot be entered twice")
        try:
            _QUEUE.acquire(self._writer_key, wait=self._queue_in_process)
            self._queued = True
            self._open_and_lock()
            self._check_generation()
        except BaseException:
            self._release()
            raise
        self._entered = True
        logger.debug(
            "Acquired exclusive iPod writer guard: mount=%s lock=%s database=%s",
            self.ipod_path, self.lock_path, self._database_generation,
        )
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._release()

    def _release(self) -> None:
        file, self._file = self._file, None
        was_locked, self._locked = self._locked, False
        if file is not None:
            try:
                if was_locked:
                    try:
                        _lock_byte(file, lock=False)
                    except OSError as exc:
                        logger.warning("Could not release iPod writer lock cleanly: %s", exc)
            finally:
                file.close()
        if self._queued:
            _QUEUE.release(self._writer_key)
            self._queued = False
        if self._entered:
            logger.debug("Released exclusive iPod writer guard: mount=%s", self.ipod_path)
        self._entered = False

    @property
    def starting_database_generation(self) -> DatabaseGeneration:
        if self._database_generation is None:
            raise RuntimeError("Device write guard is not active")
        return self._database_generation

    def assert_database_unchanged(self) -> None:
        """Raise if the database differs from the one seen when the lock was taken."""
        if _same_generation(snapshot_database_state(self.ipod_path), self.starting_database_generation):
            return
        raise DatabaseChangedElsewhereError(
            "The iPod database changed after this write session started. podsync stopped before overwriting the newer "
            "database. Reload the iPod library and try again after closing other device-management apps."
        )

    def refresh_database_generation(self) -> None:
        """Adopt the current database as the baseline (after this session wrote it)."""
        if not self._entered:
            raise RuntimeError("Device write guard is not active")
        self._database_generation = snapshot_database_state(self.ipod_path)
