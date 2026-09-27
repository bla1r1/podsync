"""The one fail-closed answer to "may I write to this iPod right now?".

A physical iPod must be its own mounted volume, contain ``iPod_Control``, use a
filesystem stock firmware reads (FAT or HFS), and pass the volume profile's
safety checks.  Virtual iPods (plain directories) only need the latter.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from podsync.hardware.safety.fsprofile import VolumeProfile, profile_volume, recheck_volume
from podsync.hardware.safety.fstype import filesystem_itunesdb_platform
from podsync.hardware.safety.guard import UnsafeWriteError
from podsync.hardware.virtual.identity import virtual_ipod_profile

__all__ = ["check_write_ready", "lock_key_for", "recheck_write_ready"]

logger = logging.getLogger(__name__)

_FIRMWARE_FILESYSTEMS = frozenset({"fat", "fat16", "fat32", "hfs", "hfs+", "hfsplus", "hfsx", "msdos", "msdosfs", "vfat"})


def lock_key_for(profile: VolumeProfile) -> str:
    """A string that names this exact mounted volume (for the host-side write lock)."""
    who = profile.identity
    return "|".join((who.operating_system, who.device_id, who.volume_id, who.mount_instance))


def _why_unsafe(profile: VolumeProfile) -> str:
    reasons = ["the volume is mounted read-only"] if profile.read_only else []
    reasons += profile.unsafe_write_reasons
    if not profile.filesystem_type:
        reasons.append("the actual filesystem type could not be detected")
    if not profile.identity.is_complete:
        reasons.append("the mounted volume identity could not be verified")
    if not reasons:
        reasons = list(profile.detection_errors)
    return (
        f"The iPod filesystem is not safe for writing: {'; '.join(reasons) or 'filesystem safety could not be verified'}. "
        f"Actual filesystem: {profile.filesystem_type or 'unknown'}; "
        f"device-reported format: {profile.reported_volume_format or 'unknown'}."
    )


def _report_if_unsafe(stage: str, profile: VolumeProfile, selected: str = "") -> None:
    if profile.safe_for_writes:
        return
    who = profile.identity
    logger.warning(
        "Unsafe iPod filesystem profile %s: selected=%s mount=%s actual=%s reported=%s source=%s options=%s "
        "read_only=%s case_sensitive=%s max_file_bytes=%s max_name=%s allocation_unit=%s identity=%s/%s/%s/%s "
        "safe_for_writes=%s errors=%s",
        stage, selected or profile.inspection_path, profile.mount_path, profile.filesystem_type,
        profile.reported_volume_format, profile.mount_source, ",".join(profile.mount_options), profile.read_only,
        profile.case_sensitive, profile.max_file_size_bytes, profile.max_component_length,
        profile.allocation_unit_size, who.operating_system, who.device_id, who.volume_id, who.mount_instance,
        profile.safe_for_writes, "; ".join(profile.detection_errors),
    )


def _report_format_disagreement(profile: VolumeProfile) -> None:
    actual = filesystem_itunesdb_platform(profile.filesystem_type)
    reported = filesystem_itunesdb_platform(profile.reported_volume_format)
    if actual is not None and reported is not None and actual != reported:
        logger.warning(
            "iPod filesystem/report mismatch: actual=%s reported=%s", profile.filesystem_type, profile.reported_volume_format,
        )


def _check_physical(requested: str, profile: VolumeProfile) -> None:
    mounted_at = os.path.normcase(os.path.realpath(profile.mount_path))
    if requested != mounted_at:
        raise UnsafeWriteError(
            f"The selected iPod path is not mounted as its own volume. Selected path: {requested}; containing mount: "
            f"{mounted_at}. podsync stopped to avoid writing into an empty host directory."
        )
    if not (Path(requested) / "iPod_Control").is_dir():
        raise UnsafeWriteError(
            "The selected volume does not contain an iPod_Control directory. podsync stopped before writing to an "
            "unrecognized volume."
        )
    if profile.filesystem_type not in _FIRMWARE_FILESYSTEMS:
        raise UnsafeWriteError(
            f"The selected physical iPod uses an unsupported filesystem ({profile.filesystem_type or 'unknown'}). "
            "Stock iPods require a FAT-formatted Windows volume or an HFS-formatted Mac volume."
        )


def check_write_ready(mount_path: str | Path, *, reported_volume_format: str = "") -> VolumeProfile:
    """Inspect *mount_path* and raise :class:`UnsafeWriteError` unless writing is safe."""
    requested = os.path.normcase(os.path.realpath(mount_path))
    profile = virtual_ipod_profile(
        profile_volume(mount_path, reported_volume_format=reported_volume_format, probe_case_sensitivity=False),
        requested,
    )
    _report_if_unsafe("inspected", profile, requested)
    _report_format_disagreement(profile)
    if profile.identity.operating_system != "virtual":
        _check_physical(requested, profile)
    if not profile.safe_for_writes:
        raise UnsafeWriteError(_why_unsafe(profile))
    return profile  # deliberately silent on success: this runs before every write


def _recheck_virtual(retained: VolumeProfile, probe_case_sensitivity: bool | None) -> VolumeProfile:
    target = retained.inspection_path or retained.mount_path
    current = virtual_ipod_profile(
        profile_volume(target, reported_volume_format=retained.reported_volume_format,
                       probe_case_sensitivity=probe_case_sensitivity is True),
        os.path.normcase(os.path.realpath(target)),
    )
    _report_if_unsafe("revalidated", current)
    if current.identity.operating_system != "virtual":
        raise UnsafeWriteError(
            "The virtual iPod metadata is no longer present at the selected path. podsync stopped before the next write."
        )
    if current.identity != retained.identity:
        raise UnsafeWriteError("The virtual iPod root changed after inspection. podsync stopped before the next write.")
    if not current.safe_for_writes:
        raise UnsafeWriteError(f"The virtual iPod is no longer safe to write: {_why_unsafe(current)}")
    return current


def recheck_write_ready(retained_profile: VolumeProfile, *, probe_case_sensitivity: bool | None = None) -> VolumeProfile:
    """Confirm the same volume is still mounted and safe; returns the fresh profile."""
    if retained_profile.identity.operating_system == "virtual":
        return _recheck_virtual(retained_profile, probe_case_sensitivity)
    result = recheck_volume(retained_profile, probe_case_sensitivity=probe_case_sensitivity)
    _report_if_unsafe("revalidated", result.current_profile)
    if not result.safe_to_continue:
        raise UnsafeWriteError(
            f"The iPod volume is no longer safe to write: {result.reason} podsync stopped before the next write."
        )
    return result.current_profile
