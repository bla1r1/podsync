"""Contain hostile device strings and guarded metadata mutations.

Groups: untrusted device-relative paths, storage size ceilings, guarded
metadata write sessions, and Linux filesystem recovery guidance.
"""

import logging
import os
import subprocess
from pathlib import Path

import pytest

from podsync.hardware.safety import sysinfo_write
from podsync.hardware.linux import recovery
from podsync.hardware.safety import limits
from podsync.hardware.safety.fsprofile import VolumeProfile, VolumeIdentity
from podsync.hardware.safety.paths import PathEscapeError, safe_device_path
from podsync.hardware.safety.limits import (
    FileTooLargeError,
    allocated_size,
    max_file_bytes,
    ensure_file_fits,
)
from podsync.hardware.safety.guard import UnsafeWriteError

_APPROVED_SUBTREE = "iPod_Control/Audio"


def _directory_link(link: Path, target: Path) -> None:
    """Create a directory link, or skip when the host forbids them."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory links are unavailable: {symlink_error}")

    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"directory links are unavailable: {completed.stderr.strip()}")


# ── Untrusted device-relative paths ─────────────────────────────────────


def test_relative_path_inside_approved_subtree_resolves(tmp_path: Path) -> None:
    device_root = tmp_path / "device"

    resolved = safe_device_path(
        device_root,
        f"{_APPROVED_SUBTREE}/F07/Track07.m4a",
        allowed_subtree=_APPROVED_SUBTREE,
    )

    assert resolved == (
        device_root / "iPod_Control" / "Audio" / "F07" / "Track07.m4a"
    )


@pytest.mark.parametrize(
    "hostile_path",
    [
        "/opt/media/track.m4a",
        r"D:\Library\Rips\track.m4a",
        r"E:Media\track.m4a",
        r"\\nas\vault\track.m4a",
        "iPod_Control/Audio/../../../escape.m4a",
        "iPod_Control/Audio/track.m4a\x00.png",
    ],
)
def test_hostile_path_inputs_are_refused(tmp_path: Path, hostile_path: str) -> None:
    with pytest.raises(PathEscapeError):
        safe_device_path(
            tmp_path / "device",
            hostile_path,
            allowed_subtree=_APPROVED_SUBTREE,
        )


def test_path_outside_the_approved_subtree_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        safe_device_path(
            tmp_path / "device",
            "Photos/Library/photo.heic",
            allowed_subtree=_APPROVED_SUBTREE,
        )


def test_directory_link_escaping_the_subtree_is_refused(tmp_path: Path) -> None:
    device_root = tmp_path / "device"
    audio_root = device_root / "iPod_Control" / "Audio"
    elsewhere = tmp_path / "elsewhere"
    audio_root.mkdir(parents=True)
    elsewhere.mkdir()
    _directory_link(audio_root / "F11", elsewhere)

    with pytest.raises(PathEscapeError):
        safe_device_path(
            device_root,
            f"{_APPROVED_SUBTREE}/F11/Track07.m4a",
            allowed_subtree=_APPROVED_SUBTREE,
        )


def test_link_anywhere_in_the_relative_path_is_refused(tmp_path: Path) -> None:
    device_root = tmp_path / "device"
    audio_root = device_root / "iPod_Control" / "Audio"
    destination = audio_root / "F13"
    destination.mkdir(parents=True)
    _directory_link(audio_root / "F12", destination)

    with pytest.raises(PathEscapeError, match="link|reparse"):
        safe_device_path(
            device_root,
            f"{_APPROVED_SUBTREE}/F12/Track07.m4a",
            allowed_subtree=_APPROVED_SUBTREE,
        )


def test_final_file_link_is_refused_before_any_write(tmp_path: Path) -> None:
    device_root = tmp_path / "device"
    audio_root = device_root / "iPod_Control" / "Audio" / "F14"
    audio_root.mkdir(parents=True)
    genuine = audio_root / "Current.m4a"
    genuine.write_bytes(b"current-bytes")
    alias = audio_root / "Legacy.m4a"
    try:
        alias.symlink_to(genuine)
    except OSError as exc:
        pytest.skip(f"file links are unavailable: {exc}")

    with pytest.raises(PathEscapeError, match="link|reparse"):
        safe_device_path(
            device_root,
            f"{_APPROVED_SUBTREE}/F14/Legacy.m4a",
            allowed_subtree=_APPROVED_SUBTREE,
        )


# ── Storage size ceilings ───────────────────────────────────────────────


def test_allocation_rounds_sizes_up_to_whole_units() -> None:
    assert allocated_size(0, 8192) == 0
    assert allocated_size(1, 8192) == 8192
    assert allocated_size(8193, 8192) == 16384


def test_stricter_of_two_size_limits_is_enforced() -> None:
    assert max_file_bytes(9000, 7000) == 7000
    assert max_file_bytes(9000, None) == 9000
    assert max_file_bytes(None, None) is None


def test_gigabyte_ceiling_error_names_file_and_limit() -> None:
    with pytest.raises(FileTooLargeError, match=r"concert\.flac.*4\.0 GB"):
        ensure_file_fits(
            5 * 1024**3,
            max_file_size_bytes=4 * 1024**3 - 1,
            display_name="concert.flac",
        )


def test_megabyte_ceiling_error_logs_raw_byte_counters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    oversized = 33 * 1024**2
    ceiling = 32 * 1024**2

    caplog.set_level(logging.DEBUG, logger=limits.__name__)

    with pytest.raises(
        FileTooLargeError,
        match=r"ArtworkDB is 33\.0 MB, exceeding the 32\.0 MB maximum",
    ):
        ensure_file_fits(
            oversized,
            max_file_size_bytes=ceiling,
            display_name="ArtworkDB",
        )

    assert (
        "file_size_bytes=34603008 max_file_size_bytes=33554432" in caplog.text
    )


# ── Guarded metadata write sessions ─────────────────────────────────────


def _device_profile(root: Path) -> VolumeProfile:
    return VolumeProfile(
        mount_path=str(root),
        filesystem_type="vfat",
        reported_volume_format="HFS+",
        mount_source="/dev/sdg8",
        mount_options=("rw",),
        read_only=False,
        unsafe_write_reasons=(),
        case_sensitive=False,
        max_file_size_bytes=4 * 1024**3 - 1,
        max_component_length=255,
        allocation_unit_size=4096,
        identity=VolumeIdentity("linux", "8:64", "/dev/sdg8", "811"),
        detection_errors=(),
        inspection_path=str(root),
    )


def test_metadata_install_orders_flush_revalidate_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "iPod_Control" / "Device").mkdir(parents=True)
    session = sysinfo_write.SysInfoWriteSession(
        tmp_path,
        _device_profile(tmp_path),
    )
    events: list[str] = []

    monkeypatch.setattr(
        sysinfo_write,
        "recheck_write_ready",
        lambda profile, **_kwargs: (events.append("revalidate") or profile),
    )
    monkeypatch.setattr(
        sysinfo_write,
        "flush_written_file",
        lambda _file: events.append("flush"),
    )
    real_replace = sysinfo_write.safe_replace

    def noted_replace(source, target) -> None:
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(sysinfo_write, "safe_replace", noted_replace)

    target = session.write_text_atomic(
        "iPod_Control/Device/SysInfo",
        "BoardHwName: T82A\n",
        allowed_subtree="iPod_Control/Device",
    )

    assert target.read_text(encoding="utf-8") == "BoardHwName: T82A\n"
    assert events[-3:] == ["flush", "revalidate", "replace"]


def test_metadata_session_rejects_swapped_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = _device_profile(tmp_path)
    monkeypatch.setattr(
        sysinfo_write,
        "check_write_ready",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        sysinfo_write,
        "lock_key_for",
        lambda _profile: "fresh-volume",
    )

    with pytest.raises(UnsafeWriteError, match="different volume"):
        with sysinfo_write.sysinfo_write_session(
            tmp_path,
            expected_volume_identity_key="stale-volume",
        ):
            pytest.fail("session opened although the volume was swapped")


def test_metadata_writer_refuses_parent_traversal(tmp_path: Path) -> None:
    (tmp_path / "iPod_Control" / "Device").mkdir(parents=True)
    session = sysinfo_write.SysInfoWriteSession(
        tmp_path,
        _device_profile(tmp_path),
    )

    with pytest.raises(ValueError):
        session.write_bytes_atomic(
            "../neighbour.cfg",
            b"unsafe-payload",
            allowed_subtree="iPod_Control/Device",
        )


# ── Linux filesystem recovery guidance ──────────────────────────────────


def test_fat_plan_inherits_detected_source_and_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount = recovery.LinuxMountFacts(
        mount_point="/media/kai/JUKEBOX",
        source="/dev/sde1",
        filesystem="vfat",
        options=("ro",),
        super_options=("ro",),
    )
    monkeypatch.setattr(recovery, "linux_mount_details", lambda _path: mount)

    plan = recovery.linux_repair_plan("/media/kai/JUKEBOX")

    assert plan.mount_path == "/media/kai/JUKEBOX"
    assert plan.source == "/dev/sde1"
    assert plan.filesystem == "vfat"
    assert plan.unmount_command == "sudo umount /media/kai/JUKEBOX"
    assert plan.checker_command == "sudo fsck.fat -n /dev/sde1"


def test_hfsplus_plan_routes_to_macos_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount = recovery.LinuxMountFacts(
        mount_point="/media/kai/JUKEBOX",
        source="/dev/sde2",
        filesystem="hfsplus",
        options=("ro", "nosuid"),
        super_options=("ro",),
    )
    monkeypatch.setattr(recovery, "linux_mount_details", lambda _path: mount)

    plan = recovery.linux_repair_plan("/media/kai/JUKEBOX")

    assert plan.kind == "mac"
    assert not plan.checker_command
