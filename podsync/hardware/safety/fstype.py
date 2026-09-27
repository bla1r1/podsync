"""Which filesystem a mount uses, and what that implies for the iTunesDB platform flag.

The MHBD header records whether iTunes on a Mac (1) or on Windows (2) wrote the
database.  An existing database's flag always wins; otherwise HFS-family
volumes imply Mac and FAT/NTFS-family volumes imply Windows.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ITUNESDB_PLATFORM_MAC", "ITUNESDB_PLATFORM_WINDOWS", "ITunesDBPlatformResolution", "detect_volume_format",
    "filesystem_itunesdb_platform", "resolve_itunesdb_platform",
]

logger = logging.getLogger(__name__)

ITUNESDB_PLATFORM_MAC = 1
ITUNESDB_PLATFORM_WINDOWS = 2

_PLATFORM_OF_FILESYSTEM = {
    **dict.fromkeys(("apfs", "hfs", "hfs+", "hfsplus", "hfsx"), ITUNESDB_PLATFORM_MAC),
    **dict.fromkeys(("exfat", "fat", "fat16", "fat32", "msdos", "msdosfs", "ntfs", "vfat"), ITUNESDB_PLATFORM_WINDOWS),
}


@dataclass(frozen=True, slots=True)
class ITunesDBPlatformResolution:
    flag: int
    source: str  # "existing_database", "filesystem" or "default"
    filesystem_type: str
    inferred_flag: int | None
    reference_platform: int | None
    mismatch: bool


def _norm(value) -> str:
    return str(value or "").strip().casefold()


def _unescape_mount_field(text: str) -> str:
    """``/proc/self/mounts`` escapes spaces etc. as ``\\040``."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _linux(mount_path: str | Path) -> str:
    try:
        done = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(mount_path)],
            capture_output=True, text=True, timeout=5, check=False,
        )
        lines = (done.stdout or "").splitlines() if done.returncode == 0 else []
        if lines and lines[0].strip():
            return _norm(lines[0])
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("findmnt filesystem detection failed: %s", exc)
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as mounts:
            for line in mounts:
                fields = line.split()
                if len(fields) >= 3 and _unescape_mount_field(fields[1]) == str(mount_path):
                    return _norm(fields[2])
    except OSError as exc:
        logger.debug("Could not read /proc/self/mounts: %s", exc)
    return ""


def _macos(mount_path: str | Path) -> str:
    try:
        done = subprocess.run(["diskutil", "info", "-plist", str(mount_path)], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("diskutil filesystem detection failed: %s", exc)
        return ""
    if done.returncode != 0:
        return ""
    try:
        info = plistlib.loads(done.stdout)
    except Exception as exc:
        logger.debug("Could not parse diskutil output: %s", exc)
        return ""
    if not isinstance(info, dict):
        return ""
    return next((v for key in ("FilesystemType", "FilesystemName", "FilesystemPersonality") if (v := _norm(info.get(key)))), "")


def _windows(mount_path: str | Path) -> str:
    try:
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(str(mount_path)))[0]
        name = ctypes.create_unicode_buffer(256)
        if not ctypes.windll.kernel32.GetVolumeInformationW(
            f"{drive}\\" if drive else Path(mount_path).anchor, None, 0, None, None, None, name, 256,
        ):
            return ""
        return _norm(name.value)
    except (AttributeError, OSError) as exc:
        logger.debug("Windows filesystem detection failed: %s", exc)
        return ""


def detect_volume_format(mount_path: str | Path) -> str:
    """Lower-case filesystem name of the volume holding *mount_path* (``""`` if unknown)."""
    if sys.platform.startswith("linux"):
        return _linux(mount_path)
    if sys.platform == "darwin":
        return _macos(mount_path)
    if sys.platform == "win32":
        return _windows(mount_path)
    return ""


def filesystem_itunesdb_platform(filesystem_type: str) -> int | None:
    return _PLATFORM_OF_FILESYSTEM.get(_norm(filesystem_type))


def resolve_itunesdb_platform(*, filesystem_type: str, reference_platform: int | None) -> ITunesDBPlatformResolution:
    """Pick the platform flag: existing database → filesystem → Windows."""
    fs = _norm(filesystem_type)
    inferred = filesystem_itunesdb_platform(fs)
    reference = reference_platform if reference_platform in (1, 2) else None
    if reference is not None:
        flag, source = reference, "existing_database"
    elif inferred is not None:
        flag, source = inferred, "filesystem"
    else:
        flag, source = ITUNESDB_PLATFORM_WINDOWS, "default"
    return ITunesDBPlatformResolution(
        flag=flag, source=source, filesystem_type=fs, inferred_flag=inferred, reference_platform=reference,
        mismatch=reference is not None and inferred is not None and reference != inferred,
    )
