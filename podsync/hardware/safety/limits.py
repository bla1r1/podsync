"""File-size ceilings and on-disk allocation arithmetic."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from podsync.hardware.safety.guard import UnsafeWriteError

__all__ = ["FileTooLargeError", "allocated_size", "ensure_file_fits", "existing_file_allocated_size", "max_file_bytes"]

logger = logging.getLogger(__name__)


class FileTooLargeError(UnsafeWriteError):
    """A file would exceed what the device or its filesystem accepts.

    The database writer attaches the image it refused to write, so callers can
    offer to save it elsewhere.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.proposed_database_bytes: bytes = b""
        self.proposed_database_filename: str = ""


def _count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def allocated_size(logical_size: int, allocation_unit_size: int | None) -> int:
    """Bytes a file of *logical_size* occupies when rounded up to whole clusters."""
    size, unit = _count(logical_size), _count(allocation_unit_size)
    return size if size == 0 or unit <= 1 else -(-size // unit) * unit


def existing_file_allocated_size(path: str | Path, allocation_unit_size: int | None) -> int:
    """Bytes an existing file occupies on disk (blocks on POSIX, compressed size on Windows)."""
    blocks = getattr(Path(path).stat(), "st_blocks", None)
    if isinstance(blocks, int) and blocks >= 0:
        return blocks * 512
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD
    high = wintypes.DWORD(0)
    low = kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and (code := ctypes.get_last_error()) != 0:
        raise OSError(code, ctypes.FormatError(code))
    return (int(high.value) << 32) | int(low)


def max_file_bytes(filesystem_limit: int | None, device_limit: int | None) -> int | None:
    """The tighter of two positive limits; ``None`` when neither applies."""
    limits = [v for v in (filesystem_limit, device_limit) if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    return min(limits) if limits else None


def _human(size: int) -> str:
    if size >= 0.1 * 1024**3:
        return f"{size / 1024**3:.1f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024:.1f} KB"


def ensure_file_fits(file_size: int, *, max_file_size_bytes: int | None, display_name: str) -> None:
    """Raise :class:`FileTooLargeError` when *file_size* exceeds a positive limit."""
    size = _count(file_size)
    try:
        limit = int(max_file_size_bytes or 0)
    except (TypeError, ValueError, OverflowError):
        limit = 0
    if limit <= 0 or size <= limit:
        return
    logger.debug(
        "File-size safety guard rejected write: display_name=%s file_size_bytes=%d max_file_size_bytes=%d",
        display_name, size, limit,
    )
    raise FileTooLargeError(
        f"{display_name} is {_human(size)}, exceeding the {_human(limit)} maximum supported by this iPod "
        "or its filesystem. podsync stopped before writing the file."
    )
