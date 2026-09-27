"""Safe-eject coverage split by backend: orchestration, Linux, macOS, Windows.

Each group drives ``eject_ipod`` or one per-operating-system worker with the
engine's collaborators replaced by recording stubs, so no real volume is
inspected, flushed, or detached anywhere in this module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from podsync.hardware import eject
from podsync.hardware.safety.fsprofile import VolumeProfile, VolumeIdentity


def _mount_profile(root: Path, *, read_only: bool = False) -> VolumeProfile:
    """Describe a plausible vfat mount rooted at *root* as a plain kwarg table."""
    fields = {
        "mount_path": str(root),
        "filesystem_type": "vfat",
        "reported_volume_format": "FAT32",
        "mount_source": "/dev/mmcblk0p2",
        "mount_options": ("ro",) if read_only else ("rw",),
        "read_only": read_only,
        "unsafe_write_reasons": (),
        "case_sensitive": False,
        "max_file_size_bytes": 4 * 1024**3 - 1,
        "max_component_length": 255,
        "allocation_unit_size": 4096,
        "identity": VolumeIdentity("linux", "8:19", "CRATE", "771"),
        "detection_errors": (),
        "inspection_path": str(root),
    }
    return VolumeProfile(**fields)


class TestWindowsFlushGate:
    def test_privileged_prep_is_skipped_when_the_user_flush_fails(
        self,
        monkeypatch,
    ) -> None:
        stage_calls = 0
        monkeypatch.setattr(eject, "_windows_drive_is_mounted", lambda _letter: True)
        monkeypatch.setattr(
            eject,
            "flush_volume",
            lambda _target: (False, "FlushFileBuffers rejected the handle"),
        )

        def _stage(letter: str) -> tuple[bool, str]:
            nonlocal stage_calls
            stage_calls += 1
            return True, "stage is set"

        monkeypatch.setattr(eject, "_prepare_windows_volume_for_eject", _stage)

        ok, report = eject._eject_windows(Path("R:\\"))

        assert ok is False
        assert "FlushFileBuffers rejected the handle" in report
        assert stage_calls == 0


class TestDarwinDiskutilRoutes:
    def test_a_busy_volume_keeps_both_diskutil_routes_non_forced(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        issued: list[list[str]] = []
        monkeypatch.setattr(eject.shutil, "which", lambda tool: f"/usr/bin/{tool}")
        monkeypatch.setattr(
            eject,
            "_macos_disk_info",
            lambda _target: (
                {
                    "DeviceIdentifier": "disk5s1",
                    "ParentWholeDisk": "disk5",
                    "MountPoint": str(tmp_path),
                },
                "",
            ),
        )
        monkeypatch.setattr(
            eject,
            "flush_volume",
            lambda _target, *, allow_unavailable: (True, "staged writes drained"),
        )
        monkeypatch.setattr(eject, "_wait_for_macos_mount_gone", lambda *_args: False)

        def _busy_runner(argv: list[str], **_kwargs) -> tuple[bool, str]:
            issued.append(argv)
            return False, "diskutil answers that someone is using the volume"

        monkeypatch.setattr(eject, "_run_command", _busy_runner)

        ok, report = eject._eject_macos(tmp_path)

        assert ok is False
        assert issued == [
            ["diskutil", "eject", "disk5"],
            ["diskutil", "unmount", "disk5s1"],
        ]
        assert "still mounted" in report.lower()
        assert "close" in report.lower()
        assert "retry" in report.lower()
        assert "using the volume" in report.lower()

    def test_read_only_eject_never_opens_the_database_for_flushing(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        monkeypatch.setattr(eject.shutil, "which", lambda _tool: "/usr/bin/diskutil")
        monkeypatch.setattr(
            eject,
            "_macos_disk_info",
            lambda _target: (
                {
                    "DeviceIdentifier": "disk5s1",
                    "ParentWholeDisk": "disk5",
                    "MountPoint": str(tmp_path),
                },
                "",
            ),
        )
        monkeypatch.setattr(
            eject,
            "flush_volume",
            lambda *_args, **_kwargs: pytest.fail(
                "a read-only eject must not flush writes"
            ),
        )
        monkeypatch.setattr(eject, "_run_command", lambda *_args, **_kwargs: (True, "ok"))
        monkeypatch.setattr(eject, "_wait_for_macos_mount_gone", lambda *_args: True)

        ok, _report = eject._eject_macos(tmp_path, read_only=True)

        assert ok is True

    def test_eject_halts_after_the_full_file_sync_refuses(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        issued: list[list[str]] = []
        monkeypatch.setattr(eject.shutil, "which", lambda tool: f"/usr/bin/{tool}")
        monkeypatch.setattr(
            eject,
            "_macos_disk_info",
            lambda _target: (
                {
                    "DeviceIdentifier": "disk5s1",
                    "ParentWholeDisk": "disk5",
                    "MountPoint": str(tmp_path),
                },
                "",
            ),
        )
        monkeypatch.setattr(
            eject,
            "flush_volume",
            lambda _target, *, allow_unavailable: (
                False,
                "F_FULLFSYNC refused with EBADF",
            ),
        )
        monkeypatch.setattr(
            eject,
            "_run_command",
            lambda argv, **_kwargs: issued.append(argv) or (True, "ok"),
        )

        ok, report = eject._eject_macos(tmp_path)

        assert ok is False
        assert "F_FULLFSYNC refused with EBADF" in report
        assert issued == []


class TestLinuxUnmountFlow:
    def test_unreadable_mount_table_blocks_every_eject_step(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        sync_calls = 0
        monkeypatch.setattr(eject, "_find_block_device", lambda _path: "/dev/sdc9")
        monkeypatch.setattr(
            eject,
            "_linux_mount_entries",
            lambda: (_ for _ in ()).throw(OSError("reading /proc/mounts gave EACCES")),
        )

        def _flush(_mount=None) -> tuple[bool, str]:
            nonlocal sync_calls
            sync_calls += 1
            return True, "ok"

        monkeypatch.setattr(eject, "_run_sync", _flush)

        ok, report = eject._eject_linux(tmp_path)

        assert ok is False
        assert sync_calls == 0
        assert "mount table" in report.lower()
        assert "did not attempt" in report.lower()

    def test_flush_failure_stops_linux_before_any_unmount_tool_runs(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        tool_calls = 0
        monkeypatch.setattr(eject, "_find_block_device", lambda _path: "/dev/sdc9")
        monkeypatch.setattr(
            eject,
            "_linux_path_is_mounted",
            lambda _mount, _device=None: True,
        )
        monkeypatch.setattr(
            eject,
            "_run_sync",
            lambda _mount=None: (False, "sync ran out of wall-clock time"),
        )
        monkeypatch.setattr(
            eject.shutil,
            "which",
            lambda tool: "/usr/bin/udisksctl" if tool == "udisksctl" else None,
        )
        monkeypatch.setattr(eject, "_wait_for_linux_mount_gone", lambda *_args: True)

        def _udisk_eject(_device: str, _mount: str) -> tuple[bool, str]:
            nonlocal tool_calls
            tool_calls += 1
            return True, "ok"

        monkeypatch.setattr(eject, "_udisks_eject", _udisk_eject)

        ok, report = eject._eject_linux(tmp_path)

        assert ok is False
        assert tool_calls == 0
        assert "flush" in report.lower()
        assert "wall-clock" in report.lower()

    def test_busy_device_report_surfaces_and_never_falls_back_to_forcing(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        board = {"still_here": True, "fat_touched": False, "unmount_runs": 0}

        monkeypatch.setattr(eject, "_find_block_device", lambda _path: "/dev/sdc9")
        monkeypatch.setattr(
            eject,
            "_linux_path_is_mounted",
            lambda _mount, _device=None: board["still_here"],
        )
        monkeypatch.setattr(eject, "_run_sync", lambda _mount=None: (True, "staged"))
        monkeypatch.setattr(
            eject.shutil,
            "which",
            lambda tool: "/usr/bin/udisksctl" if tool == "udisksctl" else None,
        )
        monkeypatch.setattr(eject, "_wait_for_linux_mount_gone", lambda *_args: False)

        def _stubborn_unmount(_device: str) -> tuple[bool, str]:
            board["unmount_runs"] += 1
            return False, "the device is held open by a reader"

        monkeypatch.setattr(eject, "_run_udisks_unmount", _stubborn_unmount)
        monkeypatch.setattr(
            eject,
            "_run_udisks_poweroff",
            lambda _parent: (True, "detached"),
        )

        ok, report = eject._eject_linux(tmp_path)

        assert board["unmount_runs"] == 1
        assert board["fat_touched"] is False
        assert ok is False
        assert "held open" in report.lower()

    def test_the_pre_unmount_sync_reuses_the_shared_flush_entry(
        self,
        monkeypatch,
    ) -> None:
        trail: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            eject,
            "flush_volume",
            lambda target, *, allow_unavailable: (
                trail.append((str(target), allow_unavailable))
                or (True, "write caches drained")
            ),
        )

        ok, report = eject._run_sync("/media/teo/STACK3")

        assert ok is True
        assert report == "write caches drained"
        assert trail == [("/media/teo/STACK3", True)]


class TestGuardedOrchestration:
    def test_a_volume_swapped_since_the_scan_is_refused(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        (tmp_path / "iPod_Control").mkdir()
        probes = 0
        monkeypatch.setattr(
            eject,
            "profile_volume",
            lambda *_args, **_kwargs: _mount_profile(tmp_path),
        )
        monkeypatch.setattr(eject.sys, "platform", "linux")

        def _backend_attempt(_path: Path, **_kwargs) -> tuple[bool, str]:
            nonlocal probes
            probes += 1
            return True, "never runs"

        monkeypatch.setattr(eject, "_eject_linux", _backend_attempt)

        ok, report = eject.eject_device(
            str(tmp_path),
            expected_volume_identity_key="linux|8:40|ELSEWHERE|12",
        )

        assert ok is False
        assert probes == 0
        assert "different volume" in report.lower()

    def test_backend_runs_inside_the_write_lease_even_when_mounted_read_only(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        (tmp_path / "iPod_Control").mkdir()
        profile = _mount_profile(tmp_path, read_only=True)
        timeline: list[str] = []

        class _WriteLease:
            def __init__(self, *_args, **kwargs):
                assert kwargs["volume_key"] == "linux|8:19|CRATE|771"
                assert kwargs["track_database_generation"] is False

            def __enter__(self):
                timeline.append("pin")
                return self

            def __exit__(self, *_args):
                timeline.append("unpin")

        monkeypatch.setattr(
            eject,
            "profile_volume",
            lambda *_args, **_kwargs: profile,
        )
        monkeypatch.setattr(eject, "WriteLock", _WriteLease)
        monkeypatch.setattr(eject.sys, "platform", "linux")
        monkeypatch.setattr(
            eject,
            "_eject_linux",
            lambda _path, **_kwargs: (
                timeline.append("delegate") or True,
                "the drive was let go",
            ),
        )

        ok, report = eject.eject_device(
            str(tmp_path),
            expected_volume_identity_key="linux|8:19|CRATE|771",
        )

        assert ok is True
        assert report == "the drive was let go"
        assert timeline == ["pin", "delegate", "unpin"]

    def test_a_marked_virtual_folder_short_circuits_the_whole_pipeline(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        (tmp_path / "iPodInfo.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            eject,
            "_inspect_eject_volume",
            lambda *_args, **_kwargs: pytest.fail(
                "a virtual folder was inspected as a real volume"
            ),
        )
        monkeypatch.setattr(
            eject,
            "_eject_windows",
            lambda *_args, **_kwargs: pytest.fail(
                "an OS-level eject ran for a virtual folder"
            ),
        )

        ok, report = eject.eject_device(str(tmp_path))

        assert ok is True
        assert "no operating-system eject" in report
