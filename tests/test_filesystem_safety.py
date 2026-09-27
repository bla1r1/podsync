"""Filesystem profiling, database install guards, and durability primitives.

Grouped by subsystem: mounted-volume profile inspection, filesystem
detection and platform selection, guarded database installation,
database filename resolution, and flush/commit primitives.
"""

from __future__ import annotations

import os
import plistlib
import struct
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from podsync.hardware import (
    SignatureKind,
    ModelTraits,
    make_virtual_ipod,
    locate_database,
)
from podsync.hardware.safety import durable as durability
from podsync.hardware.safety import fstype
from podsync.hardware.safety import fsprofile
from podsync.hardware import current as info
from podsync.hardware.discovery import scan as scanner
from podsync.hardware.safety.limits import FileTooLargeError
from podsync.hardware.safety.guard import (
    UnsafeWriteError,
    snapshot_database_state,
)
from podsync.itdb.writer import database as db_writer

# ---------------------------------------------------------------------------
# Mounted-volume profile inspection
# ---------------------------------------------------------------------------


def _mount_line(
    mount_path: Path,
    *,
    mount_id: int = 511,
    parent_id: int = 44,
    device: str = "8:41",
    filesystem_kind: str = "vfat",
    source: str = "/dev/sdd5",
    options: str = "rw,nosuid,noexec,relatime",
    super_options: str = "rw,fmask=0077,dmask=0077",
) -> str:
    encoded_mount = str(mount_path).replace(" ", r"\040")
    return (
        f"{mount_id} {parent_id} {device} / {encoded_mount} {options} - "
        f"{filesystem_kind} {source} {super_options}\n"
    )


def _stub_path_limits(monkeypatch) -> None:
    monkeypatch.setattr(
        fsprofile.os,
        "pathconf",
        lambda *_args: 255,
        raising=False,
    )
    monkeypatch.setattr(
        fsprofile.os,
        "statvfs",
        lambda *_args: type("Stats", (), {"f_frsize": 4096, "f_bsize": 4096})(),
        raising=False,
    )


def _use_linux_mountinfo(monkeypatch, mountinfo: Path) -> None:
    monkeypatch.setattr(fsprofile.sys, "platform", "linux")
    monkeypatch.setattr(fsprofile, "_LINUX_MOUNTINFO", mountinfo)
    _stub_path_limits(monkeypatch)


def test_linux_profile_reports_mount_limits_options_and_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A parsed mount table yields complete, writable vfat facts."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mount_line(tmp_path), encoding="utf-8")
    _use_linux_mountinfo(monkeypatch, mountinfo)

    profile = fsprofile.profile_volume(
        tmp_path,
        reported_volume_format="FAT32",
    )

    assert profile.mount_path == os.path.realpath(tmp_path)
    assert profile.filesystem_type == "vfat"
    assert profile.reported_volume_format == "FAT32"
    assert profile.mount_source == "/dev/sdd5"
    assert profile.mount_options == (
        "rw",
        "nosuid",
        "noexec",
        "relatime",
        "fmask=0077",
        "dmask=0077",
    )
    assert profile.read_only is False
    assert profile.safe_for_writes is True
    assert profile.case_sensitive is None
    assert profile.max_file_size_bytes == 4 * 1024**3 - 1
    assert profile.max_component_length is not None
    assert profile.max_component_length > 0
    assert profile.allocation_unit_size is not None
    assert profile.allocation_unit_size > 0
    assert profile.identity == fsprofile.VolumeIdentity(
        operating_system="linux",
        device_id="8:41",
        volume_id="/dev/sdd5",
        mount_instance="511",
    )
    assert profile.detection_errors == ()


def test_linux_identity_prefers_udev_filesystem_uuid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """When udev knows the UUID, it replaces the device node as volume id."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mount_line(tmp_path), encoding="utf-8")
    udev_data = tmp_path / "udev-data"
    udev_data.mkdir()
    (udev_data / "b8:41").write_text(
        "E:ID_FS_TYPE=vfat\nE:ID_FS_UUID=7F2E-90CD\n",
        encoding="utf-8",
    )
    _use_linux_mountinfo(monkeypatch, mountinfo)
    monkeypatch.setattr(fsprofile, "_LINUX_UDEV_DATA", udev_data)

    profile = fsprofile.profile_volume(tmp_path)

    assert profile.identity.volume_id == "uuid:7F2E-90CD"


def test_revalidation_rejects_a_remounted_mount_instance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A changed mount id means the volume was remounted since inspection."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mount_line(tmp_path), encoding="utf-8")
    _use_linux_mountinfo(monkeypatch, mountinfo)
    original = fsprofile.profile_volume(tmp_path)
    mountinfo.write_text(
        _mount_line(tmp_path, mount_id=512),
        encoding="utf-8",
    )

    result = fsprofile.recheck_volume(original)

    assert result.safe_to_continue is False
    assert result.current_identity.mount_instance == "512"
    assert "mount instance changed" in result.reason.casefold()


def test_case_probe_observes_behavior_and_cleans_up_after_itself(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The probe file is removed and its observation matches the host FS."""
    reference = tmp_path / "ProbeSample"
    reference.write_bytes(b"")
    expected_case_sensitive = not (tmp_path / "probesample").exists()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mount_line(tmp_path), encoding="utf-8")
    _use_linux_mountinfo(monkeypatch, mountinfo)

    profile = fsprofile.profile_volume(
        tmp_path,
        probe_case_sensitivity=True,
    )

    assert profile.case_sensitive is expected_case_sensitive
    assert not any("CaseProbe" in child.name for child in tmp_path.iterdir())

    probes: list[str] = []
    monkeypatch.setattr(
        fsprofile,
        "_probe_case_sensitivity",
        lambda path: (probes.append(path) or expected_case_sensitive, ""),
    )
    revalidated = fsprofile.recheck_volume(profile)

    assert revalidated.safe_to_continue
    assert revalidated.current_profile.case_sensitive is expected_case_sensitive
    assert probes == []


def test_case_probe_removal_failure_marks_profile_unsafe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Leaving a probe file behind means the state is no longer certain."""
    monkeypatch.setattr(
        fsprofile.Path,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("media removal refused")
        ),
    )

    case_sensitive, error = fsprofile._probe_case_sensitivity(
        str(tmp_path)
    )

    assert case_sensitive is None
    assert "Could not remove filesystem case probe" in error


def test_case_probe_runs_inside_requested_device_database_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Probing targets the iTunes folder of the selected device directory."""
    mount_root = tmp_path / "mount"
    requested_device = mount_root / "aux-device"
    database_directory = requested_device / "iPod_Control" / "iTunes"
    database_directory.mkdir(parents=True)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mount_line(mount_root), encoding="utf-8")
    probed: list[str] = []
    _use_linux_mountinfo(monkeypatch, mountinfo)
    monkeypatch.setattr(
        fsprofile,
        "_probe_case_sensitivity",
        lambda path: (probed.append(path) or False, ""),
    )

    profile = fsprofile.profile_volume(
        requested_device,
        probe_case_sensitivity=True,
    )

    assert probed == [str(database_directory)]
    assert profile.inspection_path == os.path.realpath(requested_device)
    assert profile.mount_path == os.path.realpath(mount_root)


def test_macos_profile_derives_identity_and_format_from_diskutil(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """diskutil's plist supplies device id, UUID, options, and block size."""
    disk_info = {
        "DeviceIdentifier": "disk9s2",
        "VolumeUUID": "2D4E8A19-6F37-4B90-9C52-E7A31D6B0F84",
        "FilesystemType": "hfs",
        "MountPoint": str(tmp_path),
        "Writable": True,
        "AllocationBlockSize": 8192,
        "MountOptions": ["local", "noowners", "nosuid"],
    }
    commands: list[list[str]] = []

    def fake_run(args, **_kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout=plistlib.dumps(disk_info), stderr=b"")

    monkeypatch.setattr(fsprofile.sys, "platform", "darwin")
    monkeypatch.setattr(fsprofile.subprocess, "run", fake_run)
    _stub_path_limits(monkeypatch)

    profile = fsprofile.profile_volume(
        tmp_path,
        reported_volume_format="HFS+",
    )

    assert commands == [["diskutil", "info", "-plist", os.path.realpath(tmp_path)]]
    assert profile.filesystem_type == "hfs"
    assert profile.reported_volume_format == "HFS+"
    assert profile.mount_source == "/dev/disk9s2"
    assert profile.mount_options == ("local", "noowners", "nosuid")
    assert profile.read_only is False
    assert profile.allocation_unit_size == 8192
    assert profile.identity == fsprofile.VolumeIdentity(
        operating_system="macos",
        device_id="disk9s2",
        volume_id="2D4E8A19-6F37-4B90-9C52-E7A31D6B0F84",
        mount_instance="disk9s2",
    )
    assert profile.safe_for_writes is True


def test_windows_profile_derives_serial_guid_and_geometry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Win32 volume queries supply NTFS type, serial, and cluster size."""

    class FakeKernel32:
        @staticmethod
        def GetVolumeInformationW(
            _root,
            _volume_name,
            _volume_name_size,
            serial,
            max_component,
            flags,
            filesystem_name,
            _filesystem_name_size,
        ):
            serial._obj.value = 0x51CEB007
            max_component._obj.value = 255
            flags._obj.value = 0x00000003
            filesystem_name.value = "NTFS"
            return 1

        @staticmethod
        def GetVolumeNameForVolumeMountPointW(_root, volume_name, _size):
            volume_name.value = "\\\\?\\Volume{4F6E2C81-3A9D-4E57-B0C6-8A29D1F35B70}\\"
            return 1

        @staticmethod
        def GetDiskFreeSpaceW(
            _root,
            sectors_per_cluster,
            bytes_per_sector,
            free_clusters,
            total_clusters,
        ):
            sectors_per_cluster._obj.value = 8
            bytes_per_sector._obj.value = 512
            free_clusters._obj.value = 100
            total_clusters._obj.value = 200
            return 1

    monkeypatch.setattr(fsprofile.sys, "platform", "win32")
    monkeypatch.setattr(
        fsprofile.ctypes,
        "windll",
        SimpleNamespace(kernel32=FakeKernel32()),
        raising=False,
    )

    profile = fsprofile.profile_volume(tmp_path)

    assert profile.filesystem_type == "ntfs"
    assert profile.mount_source == "\\\\?\\Volume{4F6E2C81-3A9D-4E57-B0C6-8A29D1F35B70}\\"
    assert profile.mount_options == ()
    assert profile.read_only is False
    assert profile.max_component_length == 255
    assert profile.allocation_unit_size == 4096
    assert profile.identity == fsprofile.VolumeIdentity(
        operating_system="windows",
        device_id="\\\\?\\Volume{4F6E2C81-3A9D-4E57-B0C6-8A29D1F35B70}\\",
        volume_id="51CEB007",
        mount_instance="\\\\?\\Volume{4F6E2C81-3A9D-4E57-B0C6-8A29D1F35B70}\\",
    )
    assert profile.safe_for_writes is True


def test_linux_hfs_force_option_is_always_marked_unsafe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """HFS mounted with 'force' on Linux can never authorize writes."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        _mount_line(
            tmp_path,
            filesystem_kind="hfsplus",
            options="rw,nosuid,nodev,force",
            super_options="rw,force",
        ),
        encoding="utf-8",
    )
    _use_linux_mountinfo(monkeypatch, mountinfo)

    profile = fsprofile.profile_volume(tmp_path)

    assert profile.safe_for_writes is False
    assert profile.unsafe_write_reasons == (
        "Linux HFS volume is mounted with the unsafe 'force' option",
    )


def test_unsafe_hfs_mount_short_circuits_before_any_probe_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Unsafe mounts must not even get a case-sensitivity probe file."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        _mount_line(
            tmp_path,
            filesystem_kind="hfsplus",
            options="rw,nosuid,nodev,force",
            super_options="rw,force",
        ),
        encoding="utf-8",
    )
    probes: list[str] = []
    monkeypatch.setattr(fsprofile.sys, "platform", "linux")
    monkeypatch.setattr(fsprofile, "_LINUX_MOUNTINFO", mountinfo)
    monkeypatch.setattr(
        fsprofile,
        "_probe_case_sensitivity",
        lambda path: (probes.append(path) or False, ""),
    )

    profile = fsprofile.profile_volume(
        tmp_path,
        probe_case_sensitivity=True,
    )

    assert profile.safe_for_writes is False
    assert probes == []


def test_profile_fails_closed_when_mount_table_is_unreadable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Without a readable mount table the identity simply cannot exist."""
    monkeypatch.setattr(fsprofile.sys, "platform", "linux")
    monkeypatch.setattr(
        fsprofile,
        "_LINUX_MOUNTINFO",
        tmp_path / "missing-mount-table",
    )
    _stub_path_limits(monkeypatch)

    profile = fsprofile.profile_volume(tmp_path)

    assert profile.safe_for_writes is False
    assert profile.identity.is_complete is False
    assert any("mount table" in error.casefold() for error in profile.detection_errors)


# ---------------------------------------------------------------------------
# Filesystem detection and platform selection
# ---------------------------------------------------------------------------


def test_device_without_sysinfo_reports_unknown_checksum(tmp_path: Path) -> None:
    """An unidentifiable device must not be assigned a guessed checksum."""
    assert info.detect_signature_kind(str(tmp_path)) == SignatureKind.UNKNOWN


def test_blank_sysinfo_still_reports_unknown_checksum(tmp_path: Path) -> None:
    """An empty SysInfo file carries no usable checksum evidence either."""
    sysinfo = tmp_path / "iPod_Control" / "Device" / "SysInfo"
    sysinfo.parent.mkdir(parents=True)
    sysinfo.write_text("", encoding="utf-8")

    assert info.detect_signature_kind(str(tmp_path)) == SignatureKind.UNKNOWN


def test_linux_detection_asks_findmnt_for_the_mounted_fstype(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Filesystem detection on Linux is delegated to findmnt verbatim."""
    commands: list[list[str]] = []
    monkeypatch.setattr(fstype.sys, "platform", "linux")

    def fake_run(args, **_kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout="vfat\n", stderr="")

    monkeypatch.setattr(fstype.subprocess, "run", fake_run)

    assert fstype.detect_volume_format(tmp_path) == "vfat"
    assert commands == [
        ["findmnt", "-n", "-o", "FSTYPE", "--target", str(tmp_path)]
    ]


def test_valid_reference_platform_wins_but_conflict_is_flagged() -> None:
    """A trustworthy stored flag is preserved while a mismatch is reported."""
    resolution = fstype.resolve_itunesdb_platform(
        filesystem_type="hfsplus",
        reference_platform=2,
    )

    assert resolution.flag == 2
    assert resolution.source == "existing_database"
    assert resolution.inferred_flag == 1
    assert resolution.mismatch is True


def test_invalid_reference_platform_defers_to_filesystem_inference() -> None:
    """A zero/unknown stored flag leaves the filesystem as the only evidence."""
    resolution = fstype.resolve_itunesdb_platform(
        filesystem_type="hfsplus",
        reference_platform=0,
    )

    assert resolution.flag == 1
    assert resolution.source == "filesystem"
    assert resolution.mismatch is False


def test_scanner_warns_when_mac_filesystem_appears_on_linux(
    monkeypatch,
    caplog,
) -> None:
    """Scanning an HFS device on Linux is recorded as an odd combination."""
    monkeypatch.setattr(scanner.sys, "platform", "linux")
    monkeypatch.setattr(scanner, "_identify_via_sysinfo", lambda _path: {})
    monkeypatch.setattr(scanner, "_identify_via_hashing_scheme", lambda _path: {})
    monkeypatch.setattr(
        scanner,
        "detect_volume_format",
        lambda _path: "hfsplus",
    )

    result = scanner._probe_filesystem("/media/ren/JUKEBOX")

    assert result["filesystem_type"] == "hfsplus"
    assert "Mac-formatted iPod filesystem detected on Linux" in caplog.text


def test_scanner_records_the_scan_time_volume_identity_key(monkeypatch) -> None:
    """The probe stores a lock key captured while the volume was scanned."""
    monkeypatch.setattr(scanner, "detect_volume_format", lambda _path: "vfat")
    monkeypatch.setattr(scanner, "_identify_via_sysinfo", lambda _path: {})
    monkeypatch.setattr(scanner, "_identify_via_hashing_scheme", lambda _path: {})
    monkeypatch.setattr(
        scanner,
        "profile_volume",
        lambda _path: SimpleNamespace(
            filesystem_type="vfat",
            identity=SimpleNamespace(is_complete=True),
        ),
    )
    monkeypatch.setattr(scanner, "lock_key_for", lambda _profile: "scan-volume-77")

    result = scanner._probe_filesystem("/media/ren/JUKEBOX")

    assert result["volume_identity_key"] == "scan-volume-77"
    assert result["_sources"]["volume_identity_key"] == "mounted_volume_identity"


# ---------------------------------------------------------------------------
# Guarded database installation
# ---------------------------------------------------------------------------


def test_firmware_size_ceiling_aborts_before_replacing_database(
    tmp_path: Path,
) -> None:
    """A too-small firmware limit stops the write with the live file intact."""
    device = make_virtual_ipod(tmp_path, "MA428")
    database = tmp_path / "iPod_Control" / "iTunes" / "iTunesDB"
    original = database.read_bytes()
    limited = replace(device.capabilities, max_database_bytes=4)

    with pytest.raises(FileTooLargeError, match="iTunesDB"):
        db_writer.write_itdb(
            str(tmp_path),
            [],
            backup=False,
            capabilities=limited,
        )

    assert database.read_bytes() == original


def test_install_preflight_requires_staging_free_space(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Committing needs room for the whole staged database on the volume."""
    database = tmp_path / "iTunesDB"
    profile = SimpleNamespace(
        max_file_size_bytes=4096,
        allocation_unit_size=1,
    )
    monkeypatch.setattr(
        db_writer,
        "check_write_ready",
        lambda _path: profile,
    )
    monkeypatch.setattr(
        db_writer.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=512),
    )

    with pytest.raises(UnsafeWriteError, match="enough free space"):
        db_writer._preflight_database_install(
            str(tmp_path),
            str(database),
            1024,
            capabilities=None,
        )


def test_writer_logs_platform_choice_and_flags_filesystem_conflict(
    monkeypatch,
    caplog,
    tmp_path: Path,
) -> None:
    """The preserved platform flag and the conflicting fs are both logged."""
    make_virtual_ipod(tmp_path, "MA428")
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    decoy_temps = [
        itunes_dir / "iTunesDB.tmp",
        itunes_dir / "iTunesDB.backup.tmp",
    ]
    for decoy in decoy_temps:
        decoy.write_bytes(b"keep-me-intact")
    monkeypatch.setattr(
        db_writer,
        "detect_volume_format",
        lambda _path: "hfsplus",
    )
    caplog.set_level("INFO", logger=db_writer.__name__)

    assert db_writer.write_itdb(str(tmp_path), []) is True

    db_path = tmp_path / "iPod_Control" / "iTunes" / "iTunesDB"
    assert struct.unpack_from("<H", db_path.read_bytes(), 0x20)[0] == 2
    assert all(path.read_bytes() == b"keep-me-intact" for path in decoy_temps)
    assert (
        "iTunesDB platform selection: flag=2 (Windows) "
        "source=existing_database filesystem=hfsplus reference=2"
    ) in caplog.text
    assert "iTunesDB platform/filesystem mismatch" in caplog.text


def test_writer_keeps_on_device_mac_flag_over_foreign_reference(
    monkeypatch,
    caplog,
    tmp_path: Path,
) -> None:
    """A valid on-device flag beats a contradictory supplied reference."""
    make_virtual_ipod(tmp_path, "MA428")
    db_path = tmp_path / "iPod_Control" / "iTunes" / "iTunesDB"
    existing = bytearray(db_path.read_bytes())
    struct.pack_into("<H", existing, 0x20, 1)
    db_path.write_bytes(existing)
    foreign_copy = tmp_path / "foreign-copy.itdb"
    foreign_copy.write_bytes(db_writer.write_mhbd([], platform=2))
    monkeypatch.setattr(db_writer, "detect_volume_format", lambda _path: "hfsplus")
    caplog.set_level("INFO", logger=db_writer.__name__)

    assert db_writer.write_itdb(
        str(tmp_path),
        [],
        backup=False,
        reference_itdb_path=str(foreign_copy),
    ) is True

    assert struct.unpack_from("<H", db_path.read_bytes(), 0x20)[0] == 1
    assert (
        "iTunesDB platform selection: flag=1 (Mac) "
        "source=existing_database filesystem=hfsplus reference=1"
    ) in caplog.text


def test_writer_logs_supplied_reference_as_platform_evidence(
    monkeypatch,
    caplog,
    tmp_path: Path,
) -> None:
    """Without a usable on-device flag the supplied reference is cited."""
    make_virtual_ipod(tmp_path, "MA428")
    db_path = tmp_path / "iPod_Control" / "iTunes" / "iTunesDB"
    existing = bytearray(db_path.read_bytes())
    struct.pack_into("<H", existing, 0x20, 0)
    db_path.write_bytes(existing)
    golden_copy = tmp_path / "golden-master.itdb"
    golden_copy.write_bytes(db_writer.write_mhbd([], platform=1))
    monkeypatch.setattr(db_writer, "detect_volume_format", lambda _path: "hfsplus")
    caplog.set_level("INFO", logger=db_writer.__name__)

    assert db_writer.write_itdb(
        str(tmp_path),
        [],
        backup=False,
        reference_itdb_path=str(golden_copy),
    ) is True

    assert struct.unpack_from("<H", db_path.read_bytes(), 0x20)[0] == 1
    assert "source=reference_database filesystem=hfsplus reference=1" in caplog.text


def test_writer_honors_retained_capabilities_without_device_lookup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Caller-supplied capabilities make the mutable selection irrelevant."""
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    (tmp_path / "iPodInfo.json").write_text("{}", encoding="utf-8")
    (itunes_dir / "iTunesDB").write_bytes(db_writer.write_mhbd([]))
    retained_capabilities = ModelTraits(supports_compressed_db=True)
    monkeypatch.setattr(
        "podsync.hardware.database_filename_for_write",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("mutable selected-device filename must not be consulted")
        ),
    )

    assert db_writer.write_itdb(
        str(tmp_path),
        [],
        backup=False,
        capabilities=retained_capabilities,
        force_checksum=SignatureKind.NONE,
    ) is True

    assert (itunes_dir / "iTunesCDB").read_bytes()[:4] == b"mhbd"
    assert (itunes_dir / "iTunesDB").read_bytes() == b""


def test_truncated_existing_database_blocks_the_rewrite(
    tmp_path: Path,
) -> None:
    """A damaged header is detected before any staging file is produced."""
    make_virtual_ipod(tmp_path, "MA428")
    db_path = tmp_path / "iPod_Control" / "iTunes" / "iTunesDB"
    original = b"mhbd-cut-short"
    db_path.write_bytes(original)

    with pytest.raises(RuntimeError, match="truncated or malformed"):
        db_writer.write_itdb(str(tmp_path), [], backup=False)

    assert db_path.read_bytes() == original
    assert list(db_path.parent.glob(".iop-*.tmp")) == []


def test_unreadable_existing_database_blocks_the_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed read of the live database must abort instead of guessing."""
    make_virtual_ipod(tmp_path, "MA428")
    db_path = tmp_path / "iPod_Control" / "iTunes" / "iTunesDB"
    original_open = open

    def guarded_open(path, mode="r", *args, **kwargs):
        if Path(path) == db_path and "r" in mode:
            raise PermissionError("device read blocked")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    with pytest.raises(RuntimeError, match="could not be read safely"):
        db_writer.write_itdb(str(tmp_path), [], backup=False)

    with original_open(db_path, "rb") as database:
        assert database.read(4) == b"mhbd"
    assert list(db_path.parent.glob(".iop-*.tmp")) == []


def test_generation_check_runs_immediately_before_database_replace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The generation callback fires right before the first swap, not earlier."""
    make_virtual_ipod(tmp_path, "MA428")
    events: list[str] = []

    monkeypatch.setattr(
        db_writer,
        "safe_replace",
        lambda _source, _target: events.append("replace"),
    )

    assert db_writer.write_itdb(
        str(tmp_path),
        [],
        backup=False,
        before_database_replace=lambda: events.append("generation-check"),
    ) is True
    # The first swap commits the live database. Any later swap may retire an
    # obsolete alternate filename.
    assert events[:2] == ["generation-check", "replace"]


# ---------------------------------------------------------------------------
# Database filename resolution
# ---------------------------------------------------------------------------


def test_zero_byte_companion_database_does_not_take_resolution(
    tmp_path: Path,
) -> None:
    """An empty compressed sibling is ignored for both read and generation."""
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    (itunes_dir / "iTunesCDB").write_bytes(b"")
    (itunes_dir / "iTunesDB").write_bytes(b"mhbd-latest-copy")

    assert locate_database(str(tmp_path)) == str(itunes_dir / "iTunesDB")
    assert snapshot_database_state(tmp_path).filename == "iTunesDB"


def test_foreign_selection_and_empty_marker_keep_default_write_name(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A stale marker plus a mismatched current device cannot rename writes."""
    target = tmp_path / "fullsize"
    other = tmp_path / "shuffled"
    itunes_dir = target / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    (itunes_dir / "iTunesCDB").write_bytes(b"")
    (itunes_dir / "iTunesDB").write_bytes(b"mhbd-latest-copy")
    monkeypatch.setattr(
        info,
        "selected_device",
        lambda: SimpleNamespace(
            path=str(other),
            model_family="iPod Nano",
            generation="5th Gen",
        ),
    )

    assert info.database_filename_for_write(str(target)) == "iTunesDB"


def test_known_classic_overrides_nonempty_companion_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """An identified Classic still writes iTunesDB despite a filled sibling."""
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    (itunes_dir / "iTunesCDB").write_bytes(b"mhbd-not-ours")
    (itunes_dir / "iTunesDB").write_bytes(b"mhbd-ours")
    monkeypatch.setattr(
        info,
        "selected_device",
        lambda: SimpleNamespace(
            path=str(tmp_path),
            model_family="iPod Classic",
            generation="6th Gen",
        ),
    )

    assert info.database_filename_for_write(str(tmp_path)) == "iTunesDB"
    assert info.locate_database(str(tmp_path)) == str(itunes_dir / "iTunesDB")
    assert snapshot_database_state(tmp_path).filename == "iTunesDB"


def test_known_compressed_device_overrides_nonempty_plain_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """An identified compressed-DB device ignores a foreign iTunesDB file."""
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    (itunes_dir / "iTunesCDB").write_bytes(b"mhbd-ours")
    (itunes_dir / "iTunesDB").write_bytes(b"mhbd-not-ours")
    monkeypatch.setattr(
        info,
        "selected_device",
        lambda: SimpleNamespace(
            path=str(tmp_path),
            model_family="iPod Nano",
            generation="5th Gen",
        ),
    )

    assert info.database_filename_for_write(str(tmp_path)) == "iTunesCDB"
    assert info.locate_database(str(tmp_path)) == str(itunes_dir / "iTunesCDB")
    assert snapshot_database_state(tmp_path).filename == "iTunesCDB"


def test_classic_recovers_when_only_companion_database_is_nonempty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A Classic reads the only surviving database but still writes plain."""
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    (itunes_dir / "iTunesDB").write_bytes(b"")
    (itunes_dir / "iTunesCDB").write_bytes(b"mhbd-backup-only")
    monkeypatch.setattr(
        info,
        "selected_device",
        lambda: SimpleNamespace(
            path=str(tmp_path),
            model_family="iPod Classic",
            generation="6th Gen",
        ),
    )

    assert info.locate_database(str(tmp_path)) == str(itunes_dir / "iTunesCDB")
    assert info.database_filename_for_write(str(tmp_path)) == "iTunesDB"
    assert snapshot_database_state(tmp_path).filename == "iTunesCDB"


# ---------------------------------------------------------------------------
# Flush and durable commit primitives
# ---------------------------------------------------------------------------


def _seed_database(ipod_root: Path) -> Path:
    path = ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"mhbd-primed")
    return path


def test_linux_flush_targets_mount_with_sync_flag(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """GNU sync -f is invoked against exactly the selected mount."""
    commands: list[list[str]] = []
    monkeypatch.setattr(durability.sys, "platform", "linux")
    monkeypatch.setattr(
        durability.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )

    def fake_run(args, **_kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(durability.subprocess, "run", fake_run)

    success, message = durability.flush_volume(tmp_path)

    assert success is True
    assert message == "pending writes flushed"
    assert commands == [["sync", "-f", str(tmp_path)]]


@pytest.mark.skipif(
    durability.sys.platform != "linux" or not durability.shutil.which("sync"),
    reason="needs the native Linux sync binary",
)
def test_native_linux_sync_flushes_temp_directory(tmp_path: Path) -> None:
    """The real sync tool must accept an arbitrary directory path."""
    success, message = durability.flush_volume(tmp_path)

    assert success is True, message


def test_linux_flush_fails_closed_when_sync_is_absent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A missing sync utility cannot be reported as a successful flush."""
    monkeypatch.setattr(durability.sys, "platform", "linux")
    monkeypatch.setattr(durability.shutil, "which", lambda _command: None)

    success, message = durability.flush_volume(tmp_path)

    assert success is False
    assert "unavailable" in message


def test_windows_flush_commits_the_committed_database_handle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Exactly one FlushFileBuffers call lands on the committed database."""
    database_path = _seed_database(tmp_path)
    flushed_handles: list[int] = []
    monkeypatch.setattr(durability.sys, "platform", "win32")
    monkeypatch.setattr(
        durability,
        "_windows_flush_file_buffers",
        lambda file_descriptor: flushed_handles.append(file_descriptor),
        raising=False,
    )
    success, message = durability.flush_volume(tmp_path)

    assert success is True
    assert str(database_path) in message
    assert len(flushed_handles) == 1


def test_windows_flush_picks_the_classic_active_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Device-aware resolution flushes iTunesDB, not the stale sibling."""
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    stale_database = itunes_dir / "iTunesCDB"
    active_database = itunes_dir / "iTunesDB"
    stale_database.write_bytes(b"mhbd-obsolete")
    active_database.write_bytes(b"mhbd-live")
    monkeypatch.setattr(durability.sys, "platform", "win32")
    monkeypatch.setattr(
        info,
        "selected_device",
        lambda: SimpleNamespace(
            path=str(tmp_path),
            model_family="iPod Classic",
            generation="6th Gen",
        ),
    )
    monkeypatch.setattr(
        durability,
        "_windows_flush_file_buffers",
        lambda _file_descriptor: None,
        raising=False,
    )
    monkeypatch.setattr(
        durability,
        "_windows_flush_volume_anchor",
        lambda _path, **_kwargs: (True, "Windows volume buffers flushed"),
    )

    success, message = durability.flush_volume(tmp_path)

    assert success is True
    assert str(active_database) in message
    assert str(stale_database) not in message


def test_file_flush_without_volume_barrier_reports_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """When a volume barrier is required, its failure fails the flush."""
    database_path = _seed_database(tmp_path)
    monkeypatch.setattr(durability.sys, "platform", "win32")
    monkeypatch.setattr(
        durability,
        "_windows_flush_file_buffers",
        lambda _file_descriptor: None,
        raising=False,
    )
    monkeypatch.setattr(
        durability,
        "_windows_flush_volume_anchor",
        lambda _path, **_kwargs: (False, "volume barrier denied"),
    )

    success, message = durability.flush_volume(
        tmp_path,
        require_volume_barrier=True,
    )

    assert success is False
    assert str(database_path) in message
    assert "volume barrier denied" in message


def test_windows_flush_falls_back_to_volume_without_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """With no committed database, the volume handle itself is flushed."""
    monkeypatch.setattr(durability.sys, "platform", "win32")
    calls: list[Path] = []
    monkeypatch.setattr(
        durability,
        "_windows_flush_volume_anchor",
        lambda path, **_kwargs: (
            calls.append(path) or True,
            "Windows volume handle flushed",
        ),
    )

    success, message = durability.flush_volume(tmp_path)

    assert success is True
    assert message == "Windows volume handle flushed"
    assert calls == [tmp_path]


@pytest.mark.skipif(
    durability.sys.platform != "win32",
    reason="exercises native Windows volume-handle resolution",
)
def test_native_windows_flush_degrades_to_safe_message(
    tmp_path: Path,
) -> None:
    """The unprivileged fallback still reports a volume-based outcome."""
    success, message = durability.flush_volume(
        tmp_path,
        allow_unavailable=True,
    )

    assert success is True
    assert "volume" in message.casefold()


def test_macos_flush_issues_sync_then_full_fsync(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """macOS orders a drive-wide sync before the per-file full fsync."""
    _seed_database(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(durability.sys, "platform", "darwin")
    monkeypatch.setattr(durability.os, "sync", lambda: calls.append("sync"), raising=False)
    monkeypatch.setattr(
        durability,
        "_macos_full_fsync",
        lambda _file_descriptor: calls.append("fullfsync"),
        raising=False,
    )

    success, message = durability.flush_volume(tmp_path)

    assert success is True
    assert "full filesystem flush" in message
    assert calls == ["sync", "fullfsync"]


def test_macos_flush_without_anchor_reports_sync_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A database-free macOS snapshot still receives the global sync."""
    calls: list[str] = []
    monkeypatch.setattr(durability.sys, "platform", "darwin")
    monkeypatch.setattr(
        durability.os,
        "sync",
        lambda: calls.append("sync"),
        raising=False,
    )

    success, message = durability.flush_volume(tmp_path)

    assert success is True
    assert "no regular full-fsync anchor" in message
    assert calls == ["sync"]


def test_windows_flush_surfaces_failed_file_buffer_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A raised FlushFileBuffers error is reported, not swallowed."""
    _seed_database(tmp_path)
    monkeypatch.setattr(durability.sys, "platform", "win32")

    def fail_flush(_file_descriptor: int) -> None:
        raise OSError("FlushFileBuffers refused")

    monkeypatch.setattr(
        durability,
        "_windows_flush_file_buffers",
        fail_flush,
        raising=False,
    )

    success, message = durability.flush_volume(tmp_path)

    assert success is False
    assert "FlushFileBuffers refused" in message


def test_directory_barrier_runs_open_fsync_close(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The POSIX directory barrier strictly orders its three syscalls."""
    target = tmp_path / "iPod_Control" / "iTunes" / "iTunesDB"
    target.parent.mkdir(parents=True)
    calls: list[tuple[str, int | str]] = []
    monkeypatch.setattr(durability.sys, "platform", "linux")
    monkeypatch.setattr(
        durability.os,
        "open",
        lambda path, flags: calls.append(("open", path)) or 91,
    )
    monkeypatch.setattr(
        durability.os,
        "fsync",
        lambda descriptor: calls.append(("fsync", descriptor)),
    )
    monkeypatch.setattr(
        durability.os,
        "close",
        lambda descriptor: calls.append(("close", descriptor)),
    )

    durability.flush_parent_directory(target)

    assert calls == [
        ("open", str(target.parent)),
        ("fsync", 91),
        ("close", 91),
    ]


def test_durable_replace_flushes_the_parent_directory_entry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The atomic swap is followed by a directory-entry flush of its folder."""
    source = tmp_path / "iTunesDB.tmp"
    target = tmp_path / "iTunesDB"
    events: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        durability.os,
        "replace",
        lambda old, new: events.append(("replace", Path(new))),
    )
    monkeypatch.setattr(
        durability,
        "flush_parent_directory",
        lambda path: events.append(("directory", Path(path).parent)),
        raising=False,
    )

    durability.safe_replace(source, target)

    assert events == [("replace", target), ("directory", target.parent)]


def test_publish_new_never_clobbers_existing_target(
    tmp_path: Path,
) -> None:
    """Publishing must refuse to overwrite an already-present target."""
    source = tmp_path / "playlist.tmp"
    target = tmp_path / "playlist.json"
    source.write_bytes(b"fresh")
    target.write_bytes(b"kept-intact")

    with pytest.raises(FileExistsError):
        durability.safe_publish(source, target)

    assert target.read_bytes() == b"kept-intact"
    assert source.read_bytes() == b"fresh"


def test_publish_new_commits_and_cleans_staging_file(
    tmp_path: Path,
) -> None:
    """A successful publication removes its staging file and reports so."""
    source = tmp_path / "playlist.tmp"
    target = tmp_path / "playlist.json"
    source.write_bytes(b"checked-copy")

    cleanup_complete = durability.safe_publish(source, target)

    assert cleanup_complete is True
    assert target.read_bytes() == b"checked-copy"
    assert not source.exists()


def test_publish_new_copies_when_hard_links_are_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Filesystems without hard links still get an exclusive creation."""
    source = tmp_path / "playlist.tmp"
    target = tmp_path / "playlist.json"
    source.write_bytes(b"checked-copy")
    monkeypatch.setattr(
        durability.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("link creation denied")
        ),
    )

    cleanup_complete = durability.safe_publish(source, target)

    assert cleanup_complete is True
    assert target.read_bytes() == b"checked-copy"
    assert not source.exists()


def test_publish_new_reports_only_staging_cleanup_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A leftover temp degrades the result but never the committed target."""
    source = tmp_path / "playlist.tmp"
    target = tmp_path / "playlist.json"
    source.write_bytes(b"checked-copy")
    original_unlink = durability.safe_unlink

    def fail_source_cleanup(
        path: str | Path,
        *,
        missing_ok: bool = False,
    ) -> None:
        if Path(path) == source:
            raise OSError("staging cleanup denied")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(durability, "safe_unlink", fail_source_cleanup)

    cleanup_complete = durability.safe_publish(source, target)

    assert cleanup_complete is False
    assert target.read_bytes() == b"checked-copy"
    assert source.read_bytes() == b"checked-copy"


def test_unique_sibling_temp_bypasses_predictable_symlink(
    tmp_path: Path,
) -> None:
    """A planted predictable temp symlink is created past, never followed."""
    target = tmp_path / "iTunesDB"
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep-outside")
    predictable = tmp_path / "iTunesDB.tmp"
    try:
        predictable.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    temp_path, temp_file = durability.open_unique_sibling_temp(target, mode="wb")
    with temp_file as file:
        file.write(b"fresh-database")
        durability.flush_written_file(file)
    durability.safe_replace(temp_path, target)

    assert temp_path != predictable
    assert target.read_bytes() == b"fresh-database"
    assert outside.read_bytes() == b"keep-outside"
    assert predictable.is_symlink()
