"""What is mounted where on Linux, and how the user could check a damaged iPod filesystem.

Nothing here runs a command or writes anything: :func:`linux_repair_plan`
only *describes* the unmount / identify / read-only check commands so the UI
can show them, e.g. after the kernel remounted a damaged FAT volume read-only.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from podsync.hardware.safety.fsprofile import unescape_mount_field
from podsync.hardware.safety.fstype import filesystem_itunesdb_platform

__all__ = ["LinuxFilesystemRecoveryPlan", "LinuxMountFacts", "linux_mount_details", "linux_repair_plan"]

_FAT_FILESYSTEMS = frozenset({"fat", "fat16", "fat32", "msdos", "msdosfs", "vfat"})
_READ_ONLY_CHECKERS = {"fat": "fsck.fat", "exfat": "fsck.exfat"}  # both take -n: report only, change nothing


@dataclass(frozen=True, slots=True)
class LinuxMountFacts:
    """One ``/proc/self/mountinfo`` entry."""

    mount_point: str
    source: str
    filesystem: str
    options: tuple[str, ...]
    super_options: tuple[str, ...]

    @property
    def is_read_only(self) -> bool:
        return "ro" in self.options or "ro" in self.super_options

    @property
    def summary(self) -> str:
        return (f"{self.source or 'unknown device'} on {self.mount_point} "
                f"({self.filesystem or 'unknown filesystem'}, {','.join(self.options)})")


@dataclass(frozen=True, slots=True)
class LinuxFilesystemRecoveryPlan:
    """Commands the user can run to inspect a damaged iPod filesystem."""

    mount_path: str
    source: str
    filesystem: str
    kind: Literal["fat", "exfat", "mac", "ntfs", "unknown"]
    unmount_command: str
    identify_command: str
    checker_command: str


def _mountinfo_entries() -> list[LinuxMountFacts]:
    """Parsed ``/proc/self/mountinfo``; malformed lines are skipped."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        fs_fields = fields[separator + 1:]
        if separator < 6 or len(fs_fields) < 3:
            continue
        entries.append(LinuxMountFacts(
            mount_point=os.path.realpath(unescape_mount_field(fields[4])),
            source=unescape_mount_field(fs_fields[1]),
            filesystem=fs_fields[0],
            options=tuple(fields[5].split(",")),
            super_options=tuple(fs_fields[2].split(",")),
        ))
    return entries


def linux_mount_details(path: str | Path) -> LinuxMountFacts | None:
    """The deepest mount containing *path* (never ``/``); ``None`` off Linux or when there is none."""
    if not sys.platform.startswith("linux"):
        return None
    target = os.path.realpath(path)
    best: LinuxMountFacts | None = None
    for entry in _mountinfo_entries():
        if entry.mount_point == "/":
            continue
        root = entry.mount_point.rstrip(os.sep) or os.sep
        if (target == root or target.startswith(root + os.sep)) and (
                best is None or len(entry.mount_point) > len(best.mount_point)):
            best = entry
    return best


def _filesystem_kind(filesystem: str) -> str:
    if filesystem in _FAT_FILESYSTEMS:
        return "fat"
    if filesystem in {"exfat", "ntfs"}:
        return filesystem
    return "mac" if filesystem_itunesdb_platform(filesystem) == 1 else "unknown"


def linux_repair_plan(mount_path: str | Path, *, filesystem: str = "", source: str = "") -> LinuxFilesystemRecoveryPlan:
    """Commands for inspecting the filesystem at *mount_path*; a checker only for FAT and exFAT."""
    details = linux_mount_details(str(mount_path))
    mount = details.mount_point if details else str(mount_path)
    fs_name = (filesystem or (details.filesystem if details else "") or "").strip().casefold()
    device = (source or (details.source if details else "") or "").strip()
    kind = _filesystem_kind(fs_name)
    checker = _READ_ONLY_CHECKERS.get(kind)
    quoted_mount = shlex.quote(mount)
    return LinuxFilesystemRecoveryPlan(
        mount_path=mount,
        source=device,
        filesystem=fs_name,
        kind=kind,
        unmount_command=f"sudo umount {quoted_mount}",
        identify_command=f"findmnt -no SOURCE,FSTYPE,OPTIONS --target {quoted_mount}",
        checker_command=f"sudo {checker} -n {shlex.quote(device)}" if checker and device else "",
    )
