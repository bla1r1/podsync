"""Crash-safe file operations on removable media.

Every change to the device goes through a sibling temp file, is synced to the
medium (``fsync`` plus the platform's stronger barrier), and is then renamed
into place with the directory entry synced too.  :func:`flush_volume` asks the
OS to push everything for the whole volume before an eject.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import IO, Any

__all__ = [
    "flush_parent_directory", "flush_volume", "flush_written_file", "open_unique_sibling_temp",
    "safe_publish", "safe_replace", "safe_unlink",
]

logger = logging.getLogger(__name__)

_TEMP_PREFIX, _TEMP_SUFFIX = ".iop-", ".tmp"


def open_unique_sibling_temp(target: str | Path, *, mode: str = "w+b", encoding: str | None = None) -> tuple[Path, IO[Any]]:
    """A new, exclusively created temp file in *target*'s directory (same filesystem → atomic rename)."""
    fd, name = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX, dir=str(Path(target).parent))
    try:
        handle = os.fdopen(fd, mode) if "b" in mode else os.fdopen(fd, mode, encoding=encoding or "utf-8")
    except BaseException:
        for cleanup in (lambda: os.close(fd), lambda: os.unlink(name)):
            try:
                cleanup()
            except OSError:
                pass
        raise
    return Path(name), handle


# ── per-file barriers ───────────────────────────────────────────────


def _windows_flush_file_buffers(file_descriptor: int) -> None:
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.FlushFileBuffers(ctypes.c_void_p(msvcrt.get_osfhandle(file_descriptor))):
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code).strip() or "FlushFileBuffers failed")


def _macos_full_fsync(file_descriptor: int) -> None:
    import fcntl

    if not callable(getattr(fcntl, "fcntl", None)):
        raise OSError("macOS fcntl() is unavailable")
    fcntl.fcntl(file_descriptor, getattr(fcntl, "F_FULLFSYNC", 51))


def flush_written_file(file: IO[Any], *, full: bool = False) -> None:
    """Push a file's data to the medium; *full* adds macOS's drive-cache flush."""
    file.flush()
    descriptor = file.fileno()
    os.fsync(descriptor)
    if sys.platform == "win32":
        _windows_flush_file_buffers(descriptor)
    elif sys.platform == "darwin" and full:
        _macos_full_fsync(descriptor)


def flush_parent_directory(path: str | Path) -> None:
    """Sync the directory entry of *path* (no-op on Windows, where renames are journaled)."""
    if sys.platform == "win32":
        return
    descriptor = os.open(str(Path(path).parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_replace(source: str | Path, target: str | Path) -> None:
    """Atomically rename *source* over *target* and sync the directory."""
    os.replace(source, target)
    flush_parent_directory(target)


def safe_unlink(path: str | Path, *, missing_ok: bool = False) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    flush_parent_directory(path)


def _copy_exclusively(source: str | Path, target: str | Path) -> None:
    """Fallback when hard links are unavailable: create *target* exclusively and copy."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    out = None
    try:
        out = os.fdopen(fd, "wb")
        fd = -1
        with open(source, "rb") as src:
            shutil.copyfileobj(src, out)
        flush_written_file(out)
        out.close()
        out = None
    except BaseException:
        try:
            if out is not None:
                out.close()
            elif fd >= 0:
                os.close(fd)
        except OSError:
            pass
        try:
            safe_unlink(target, missing_ok=True)
        except OSError:
            pass
        raise


def safe_publish(source: str | Path, target: str | Path) -> bool:
    """Create *target* from *source* without ever overwriting an existing file.

    Returns ``False`` when the target was published but the temp source could
    not be removed afterwards.  Raises ``FileExistsError`` if *target* exists.
    """
    try:
        os.link(source, target)
    except FileExistsError:
        raise
    except OSError:
        _copy_exclusively(source, target)
    try:
        flush_parent_directory(target)
    except OSError:
        try:
            safe_unlink(target, missing_ok=True)
        except OSError:
            logger.exception("Could not remove unconfirmed publication target %s", target)
        raise
    try:
        safe_unlink(source)
    except OSError as exc:
        logger.warning("Published %s, but its temporary source %s could not be removed: %s", target, source, exc)
        return False
    return True


# ── whole-volume flush ──────────────────────────────────────────────


def _committed_database_path(mount_path: str | Path) -> Path | None:
    """The non-empty database file on the device, used as a flush anchor."""
    from podsync.hardware import current as info

    try:
        located = info.locate_database(str(mount_path))
        if located is None:
            return None
        candidate = Path(located)
        details = candidate.stat()
        return candidate if stat.S_ISREG(details.st_mode) and details.st_size > 0 else None
    except Exception:
        return None


def _flush_anchor(anchor: Path, *, full: bool) -> tuple[bool, str]:
    try:
        with open(anchor, "rb+") as handle:
            flush_written_file(handle, full=full)
    except OSError as exc:
        return False, f"filesystem flush failed for {anchor}: {exc}"
    return True, ""


def _windows_volume_name(kernel32, mount_path: str | Path) -> str:
    import ctypes

    try:
        root = ctypes.create_unicode_buffer(1024)
        if kernel32.GetVolumePathNameW(str(mount_path), root, 1024):
            guid = ctypes.create_unicode_buffer(1024)
            if kernel32.GetVolumeNameForVolumeMountPointW(root.value, guid, 1024):
                return guid.value.rstrip("\\")
    except (AttributeError, OSError):
        pass
    drive = os.path.splitdrive(os.path.abspath(str(mount_path)))[0]
    return f"\\\\.\\{drive}" if drive else ""


def _windows_flush_volume_anchor(mount_path: str | Path, *, allow_unavailable: bool = False) -> tuple[bool, str]:
    """``FlushFileBuffers`` on the volume handle itself (flushes metadata too)."""
    import ctypes
    from ctypes import wintypes

    def unavailable(message: str) -> tuple[bool, str]:
        return (True, message + "; relying on the required safe-eject flush") if allow_unavailable else (False, message)

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return unavailable("could not resolve the iPod volume handle for a durability barrier")
    volume = _windows_volume_name(kernel32, mount_path)
    if not volume:
        return unavailable("could not resolve the iPod volume handle for a durability barrier")
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    generic_rw, share_rw, open_existing = 0x80000000 | 0x40000000, 0x1 | 0x2, 3
    handle = kernel32.CreateFileW(volume, generic_rw, share_rw, None, open_existing, 0, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        code = ctypes.get_last_error()
        return unavailable(
            f"could not open the iPod volume for a durability barrier: {ctypes.FormatError(code).strip() or code}"
        )
    try:
        if not kernel32.FlushFileBuffers(wintypes.HANDLE(handle)):
            code = ctypes.get_last_error()
            return unavailable(f"Windows directory durability barrier failed: {ctypes.FormatError(code).strip() or code}")
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    return True, f"Windows volume buffers flushed for {volume}"


def _flush_linux(mount_path, allow_unavailable: bool, _require_volume_barrier: bool) -> tuple[bool, str]:
    missing = (True, "sync utility unavailable; relying on the unmount flush") if allow_unavailable else (
        False, "sync utility unavailable")
    if not shutil.which("sync"):
        return missing
    try:
        done = subprocess.run(["sync", "-f", str(mount_path)], capture_output=True, text=True, timeout=15, check=False)
    except FileNotFoundError:
        return missing
    except subprocess.TimeoutExpired:
        return False, "filesystem flush timed out"
    output = (done.stderr or "").strip() or (done.stdout or "").strip()
    if done.returncode != 0:
        return False, output or f"filesystem flush failed with code {done.returncode}"
    return True, output or "pending writes flushed"


def _flush_macos(mount_path, allow_unavailable: bool, _require_volume_barrier: bool) -> tuple[bool, str]:
    try:
        os.sync()
    except (AttributeError, OSError) as exc:
        if allow_unavailable:
            return True, f"macOS sync unavailable ({exc}); relying on the unmount flush"
        return False, f"macOS sync failed: {exc}"
    anchor = _committed_database_path(mount_path)
    if anchor is None:
        return True, "macOS filesystem sync completed; no regular full-fsync anchor remains on the restored device"
    ok, message = _flush_anchor(anchor, full=True)
    return (True, f"macOS full filesystem flush completed via {anchor}") if ok else (False, message)


def _flush_windows(mount_path, allow_unavailable: bool, require_volume_barrier: bool) -> tuple[bool, str]:
    anchor = _committed_database_path(mount_path)
    if anchor is None:
        return _windows_flush_volume_anchor(mount_path, allow_unavailable=allow_unavailable)
    ok, message = _flush_anchor(anchor, full=False)
    if not ok:
        return False, message
    if not require_volume_barrier:
        return True, f"Windows file buffers flushed for {anchor}"
    volume_ok, volume_message = _windows_flush_volume_anchor(mount_path, allow_unavailable=allow_unavailable)
    if not volume_ok:
        return False, f"Windows file buffers flushed for {anchor}, but the full volume barrier failed: {volume_message}"
    return True, f"Windows file buffers flushed for {anchor}; {volume_message}"


def flush_volume(mount_path: str | Path, *, allow_unavailable: bool = False,
                 require_volume_barrier: bool = False) -> tuple[bool, str]:
    """Flush pending writes for the whole volume; ``(ok, human-readable detail)``.

    With *allow_unavailable*, a missing mechanism counts as success because a
    later unmount/eject will flush anyway.
    """
    if sys.platform.startswith("linux"):
        flusher = _flush_linux
    elif sys.platform == "darwin":
        flusher = _flush_macos
    elif sys.platform == "win32":
        flusher = _flush_windows
    else:
        message = f"filesystem flush is unsupported on {sys.platform}"
        return (True, f"{message}; relying on the unmount flush") if allow_unavailable else (False, message)
    return flusher(mount_path, allow_unavailable, require_volume_barrier)
