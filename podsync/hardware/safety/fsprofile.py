"""What kind of volume a path lives on, and whether that is still the same volume later.

:func:`profile_volume` gathers the facts that decide write safety — filesystem,
mount options, read-only state, a stable volume identity (so a swapped iPod is
noticed), per-file size limit, name length and cluster size, and optionally
case sensitivity.  :func:`recheck_volume` repeats the inspection just before a
write and reports what changed.
"""

from __future__ import annotations

import ctypes
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from podsync.hardware.safety.fstype import detect_volume_format

__all__ = ["VolumeIdentity", "VolumeProfile", "VolumeRecheck", "profile_volume", "recheck_volume", "unescape_mount_field"]

_LINUX_MOUNTINFO = Path("/proc/self/mountinfo")
_LINUX_UDEV_DATA = Path("/run/udev/data")

_FOUR_GIB, _TWO_GIB = 4 * 1024**3 - 1, 2 * 1024**3 - 1
_MAX_FILE_SIZE_BYTES: dict[str, int] = {
    "fat": _TWO_GIB, "fat16": _TWO_GIB,
    **dict.fromkeys(("fat32", "msdos", "msdosfs", "vfat"), _FOUR_GIB),
}
_HFS_FILESYSTEMS = frozenset({"hfs", "hfs+", "hfsplus", "hfsx"})
_CASE_PROBE_PREFIX = ".Podsync_CaseProbe_Aa_"
_WINDOWS_READ_ONLY_VOLUME = 0x00080000


@dataclass(frozen=True, slots=True)
class VolumeIdentity:
    """What identifies a mounted volume across calls (device, volume id, mount instance)."""

    operating_system: str
    device_id: str
    volume_id: str
    mount_instance: str

    @property
    def is_complete(self) -> bool:
        return all((self.operating_system, self.device_id, self.volume_id, self.mount_instance))


@dataclass(frozen=True, slots=True)
class VolumeProfile:
    """Filesystem facts about a mounted volume that decide whether and how to write."""

    mount_path: str
    filesystem_type: str
    reported_volume_format: str
    mount_source: str
    mount_options: tuple[str, ...]
    read_only: bool
    unsafe_write_reasons: tuple[str, ...]
    case_sensitive: bool | None
    max_file_size_bytes: int | None
    max_component_length: int | None
    allocation_unit_size: int | None
    identity: VolumeIdentity
    detection_errors: tuple[str, ...]
    inspection_path: str = ""

    @property
    def safe_for_writes(self) -> bool:
        return not self.read_only and not self.unsafe_write_reasons and bool(self.filesystem_type) and self.identity.is_complete


@dataclass(frozen=True, slots=True)
class VolumeRecheck:
    """Result of re-profiling a volume just before a write."""

    safe_to_continue: bool
    failure_code: str
    reason: str
    current_profile: VolumeProfile

    @property
    def current_identity(self) -> VolumeIdentity:
        return self.current_profile.identity


@dataclass(slots=True)
class _Facts:
    """What the OS-specific inspector could learn about the mount."""

    mount_path: str
    filesystem_type: str = ""
    mount_source: str = ""
    mount_options: tuple[str, ...] = ()
    read_only: bool = False
    identity: VolumeIdentity = VolumeIdentity("", "", "", "")
    max_component_length: int | None = None
    allocation_unit_size: int | None = None
    errors: tuple[str, ...] = ()


def _nothing_known(requested: str, operating_system: str, *errors: str) -> _Facts:
    return _Facts(requested, identity=VolumeIdentity(operating_system, "", "", ""), errors=tuple(errors))


# ── Linux ───────────────────────────────────────────────────────────

_MOUNTINFO_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}


def unescape_mount_field(value: str) -> str:
    """Undo the octal escapes `/proc/self/mountinfo` uses for spaces, tabs, newlines and backslashes."""
    return re.sub(r"\\(040|011|012|134)", lambda m: _MOUNTINFO_ESCAPES[m.group(1)], value)


def _merged_options(*groups: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(opt for group in groups for opt in group.split(",") if opt))


def _udev_filesystem_uuid(device_id: str) -> str:
    if not re.fullmatch(r"\d+:\d+", device_id):
        return ""
    try:
        record = (_LINUX_UDEV_DATA / f"b{device_id}").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return next((line.split("=", 1)[1].strip() for line in record.splitlines() if line.startswith("E:ID_FS_UUID=")), "")


def _inspect_linux(requested: str) -> _Facts:
    """The deepest ``/proc/self/mountinfo`` entry containing *requested*."""
    try:
        table = _LINUX_MOUNTINFO.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _nothing_known(requested, "linux", f"Could not read Linux mount table: {exc}")
    best: _Facts | None = None
    depth = -1
    for line in table.splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        dash = fields.index("-")
        if dash < 6 or len(fields) <= dash + 3:
            continue
        mount_point = os.path.realpath(unescape_mount_field(fields[4]))
        prefix = mount_point.rstrip(os.sep) or os.sep
        inside = requested == prefix or requested.startswith(prefix if prefix.endswith(os.sep) else prefix + os.sep)
        if not inside or len(prefix) <= depth:
            continue
        depth = len(prefix)
        options = _merged_options(fields[5], fields[dash + 3])
        source = unescape_mount_field(fields[dash + 2])
        best = _Facts(
            mount_path=mount_point, filesystem_type=fields[dash + 1].strip().casefold(), mount_source=source,
            mount_options=options, read_only="ro" in options,
            identity=VolumeIdentity("linux", fields[2], source, fields[0]),
        )
    if best is None:
        return _nothing_known(requested, "linux", f"No Linux mount contains {requested}")
    uuid = _udev_filesystem_uuid(best.identity.device_id)
    if uuid:
        best.identity = replace(best.identity, volume_id=f"uuid:{uuid}")
    return best


# ── macOS ───────────────────────────────────────────────────────────


def _inspect_macos(requested: str) -> _Facts:
    try:
        done = subprocess.run(["diskutil", "info", "-plist", requested], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return _nothing_known(requested, "macos", f"Could not inspect macOS volume: {exc}")
    if done.returncode != 0:
        return _nothing_known(requested, "macos", f"diskutil info failed with exit code {done.returncode}")
    try:
        info = plistlib.loads(done.stdout)
    except Exception as exc:
        return _nothing_known(requested, "macos", f"Could not parse diskutil output: {exc}")
    if not isinstance(info, dict):
        return _nothing_known(requested, "macos", "diskutil returned an unexpected response")

    device = str(info.get("DeviceIdentifier") or "").strip()
    raw = info.get("MountOptions")
    parts = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else ()
    options = tuple(text for part in parts if (text := str(part).strip()))
    block = info.get("AllocationBlockSize")
    volume = next((str(info[k]).strip() for k in ("VolumeUUID", "DiskUUID", "MediaUUID") if str(info.get(k) or "").strip()), "")
    fs = str(info.get("FilesystemType") or info.get("FilesystemName") or info.get("FilesystemPersonality") or "")
    return _Facts(
        mount_path=os.path.realpath(info.get("MountPoint") or requested),
        filesystem_type=fs.strip().casefold(),
        mount_source=f"/dev/{device}" if device else "",
        mount_options=options,
        read_only=info.get("Writable") is False or info.get("VolumeReadOnly") is True or info.get("ReadOnlyVolume") is True,
        identity=VolumeIdentity("macos", device, volume, device),
        allocation_unit_size=block if isinstance(block, int) and block > 0 else None,
    )


# ── Windows ─────────────────────────────────────────────────────────


def _windows_cluster_size(kernel32, root: str) -> int | None:
    try:
        per_cluster, per_sector, free, total = (ctypes.c_uint32(0) for _ in range(4))
        if kernel32.GetDiskFreeSpaceW(root, ctypes.byref(per_cluster), ctypes.byref(per_sector),
                                      ctypes.byref(free), ctypes.byref(total)):
            size = int(per_cluster.value) * int(per_sector.value)
            return size or None
    except (AttributeError, OSError):
        pass
    return None


def _inspect_windows(requested: str) -> _Facts:
    drive = os.path.splitdrive(os.path.abspath(requested))[0]
    root = f"{drive}\\" if drive else Path(requested).anchor
    try:
        kernel32 = ctypes.windll.kernel32
        serial, name_max, flags = ctypes.c_uint32(0), ctypes.c_uint32(0), ctypes.c_uint32(0)
        fs_name = ctypes.create_unicode_buffer(256)
        ok = kernel32.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), ctypes.byref(name_max),
                                            ctypes.byref(flags), fs_name, 256)
    except (AttributeError, OSError) as exc:
        return _nothing_known(requested, "windows", f"Could not inspect Windows volume: {exc}")
    if not ok:
        return _nothing_known(requested, "windows", "GetVolumeInformationW failed")
    device = root
    try:
        guid = ctypes.create_unicode_buffer(1024)
        if kernel32.GetVolumeNameForVolumeMountPointW(root, guid, 1024):
            device = guid.value or root
    except (AttributeError, OSError):
        pass
    return _Facts(
        mount_path=root,
        filesystem_type=str(fs_name.value).strip().casefold(),
        mount_source=device,
        read_only=bool(int(flags.value) & _WINDOWS_READ_ONLY_VOLUME),
        identity=VolumeIdentity("windows", device, f"{int(serial.value):08X}", device),
        max_component_length=int(name_max.value) or None,
        allocation_unit_size=_windows_cluster_size(kernel32, root),
    )


# ── case sensitivity ────────────────────────────────────────────────


def _case_probe_directory(requested: str) -> str:
    inside = os.path.join(requested, "iPod_Control", "iTunes")
    return inside if os.path.isdir(inside) else requested


def _probe_case_sensitivity(directory: str) -> tuple[bool | None, str]:
    """Create a mixed-case temp file and look for it under the swapped-case name."""
    verdict: bool | None = None
    error = ""
    handle: int | None = None
    probe: Path | None = None
    try:
        handle, name = tempfile.mkstemp(prefix=_CASE_PROBE_PREFIX, dir=directory)
        probe = Path(name)
        os.close(handle)
        handle = None
        verdict = not probe.with_name(probe.name.swapcase()).exists()
    except OSError as exc:
        error = f"Could not probe filesystem case sensitivity: {exc}"
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError as exc:
                verdict, error = None, f"Could not remove filesystem case probe: {exc}"
    return verdict, error


# ── public API ──────────────────────────────────────────────────────


def _unsafe_reasons(facts: _Facts, filesystem_type: str) -> tuple[str, ...]:
    if facts.identity.operating_system == "linux" and filesystem_type in _HFS_FILESYSTEMS and "force" in facts.mount_options:
        return ("Linux HFS volume is mounted with the unsafe 'force' option",)
    return ()


def _fill_gaps(facts: _Facts, requested: str, errors: list[str]) -> tuple[int | None, int | None]:
    """Name-length and cluster-size limits from POSIX calls when the inspector had none."""
    name_max = facts.max_component_length
    if name_max is None:
        try:
            name_max = int(os.pathconf(requested, "PC_NAME_MAX"))
        except (AttributeError, OSError, ValueError) as exc:
            errors.append(f"Could not determine maximum filename length: {exc}")
    cluster = facts.allocation_unit_size
    if cluster is None:
        try:
            stats = os.statvfs(requested)
            cluster = int(stats.f_frsize or stats.f_bsize)
        except (AttributeError, OSError, ValueError) as exc:
            errors.append(f"Could not determine allocation unit size: {exc}")
    return name_max, cluster


def profile_volume(mount_path: str | Path, *, reported_volume_format: str = "",
                   probe_case_sensitivity: bool = False) -> VolumeProfile:
    """Inspect the volume that holds *mount_path*.

    The case probe writes (and removes) a temp file, so it only runs on a volume
    that already looks safe to write.
    """
    requested = os.path.realpath(mount_path)
    if sys.platform.startswith("linux"):
        facts = _inspect_linux(requested)
    elif sys.platform == "darwin":
        facts = _inspect_macos(requested)
    elif sys.platform == "win32":
        facts = _inspect_windows(requested)
    else:
        facts = _nothing_known(requested, "unknown")

    errors = list(facts.errors)
    fs = facts.filesystem_type or detect_volume_format(requested)
    name_max, cluster = _fill_gaps(facts, requested, errors)
    unsafe = _unsafe_reasons(facts, fs)
    case_sensitive: bool | None = None
    if probe_case_sensitivity and not facts.read_only and not unsafe and fs and facts.identity.is_complete:
        case_sensitive, error = _probe_case_sensitivity(_case_probe_directory(requested))
        if error:
            errors.append(error)
            unsafe += ("Filesystem case sensitivity could not be verified",)

    return VolumeProfile(
        mount_path=facts.mount_path, filesystem_type=fs,
        reported_volume_format=str(reported_volume_format or "").strip(),
        mount_source=facts.mount_source, mount_options=facts.mount_options, read_only=facts.read_only,
        unsafe_write_reasons=unsafe, case_sensitive=case_sensitive, max_file_size_bytes=_MAX_FILE_SIZE_BYTES.get(fs),
        max_component_length=name_max, allocation_unit_size=cluster, identity=facts.identity,
        detection_errors=tuple(errors), inspection_path=requested,
    )


def recheck_volume(retained: VolumeProfile, *, probe_case_sensitivity: bool | None = None) -> VolumeRecheck:
    """Re-inspect and compare with *retained*; the first difference found is reported."""
    probe = probe_case_sensitivity is True
    current = profile_volume(
        retained.inspection_path or retained.mount_path,
        reported_volume_format=retained.reported_volume_format, probe_case_sensitivity=probe,
    )
    if not probe and retained.case_sensitive is not None:
        current = replace(current, case_sensitive=retained.case_sensitive)

    checks = (
        (not retained.identity.is_complete, "identity_unavailable",
         "The original volume identity was incomplete and cannot be revalidated."),
        (not current.identity.is_complete, "identity_unavailable", "The mounted volume identity could not be read."),
        (current.identity.mount_instance != retained.identity.mount_instance, "mount_changed",
         "The volume mount instance changed after inspection."),
        (current.identity != retained.identity, "volume_changed", "A different volume is mounted at the inspected path."),
        (current.filesystem_type != retained.filesystem_type, "filesystem_changed",
         "The mounted filesystem type changed after inspection."),
        (current.read_only, "read_only", "The mounted volume is now read-only."),
    )
    for failed, code, reason in checks:
        if failed:
            return VolumeRecheck(False, code, reason, current)
    if current.unsafe_write_reasons:
        return VolumeRecheck(False, "unsafe_mount", current.unsafe_write_reasons[0], current)
    return VolumeRecheck(True, "", "", current)
