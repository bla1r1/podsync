"""Host writer-lock, database-generation, and write-readiness checks.

Grouped by subsystem:

* database generation bookkeeping across a guarded session,
* fail-closed readiness probing of the selected mount before any write,
* exclusive host lock serialization (one writer per underlying volume).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

from podsync.hardware.safety import readiness as write_readiness
from podsync.hardware.safety import guard as guard_mod
from podsync.hardware.safety.fsprofile import (
    VolumeProfile,
    VolumeRecheck,
    VolumeIdentity,
)
from podsync.hardware.safety.guard import (
    IpodBusyError,
    WriteLock,
    UnsafeWriteError,
    DatabaseChangedElsewhereError,
    snapshot_database_state,
)


# ---------------------------------------------------------------------------
# Shared construction helpers
# ---------------------------------------------------------------------------


def _seed_db(root: Path, contents: bytes = b"mhbd-baseline-copy") -> Path:
    target = root / "iPod_Control" / "iTunes" / "iTunesDB"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)
    return target


def _probe(root: Path, **overrides) -> VolumeProfile:
    """Start from one healthy host profile, then bend only what a case needs."""
    (root / "iPod_Control").mkdir(exist_ok=True)
    baseline = VolumeProfile(
        mount_path=str(root),
        filesystem_type="vfat",
        reported_volume_format="FAT16",
        mount_source="/dev/sdg1",
        mount_options=("rw",),
        read_only=False,
        unsafe_write_reasons=(),
        case_sensitive=False,
        max_file_size_bytes=4 * 1024**3 - 1,
        max_component_length=255,
        allocation_unit_size=4096,
        identity=VolumeIdentity("linux", "8:51", "/dev/sdg1", "326"),
        detection_errors=(),
    )
    return replace(baseline, **overrides)


def _held_guard(root: Path, seal: Path, key: str = "volume-884"):
    return WriteLock(
        root,
        volume_key=key,
        track_database_generation=False,
        lock_dir=seal,
    )


# ---------------------------------------------------------------------------
# Database generation bookkeeping during a session
# ---------------------------------------------------------------------------


class TestGenerationBookkeeping:
    def test_entry_refuses_a_stale_cached_generation(self, tmp_path: Path) -> None:
        """A cached generation no longer on disk blocks guard entry entirely."""
        root = tmp_path / "rig"
        db_file = _seed_db(root)
        snapshot = snapshot_database_state(root)
        db_file.write_bytes(b"mhbd-written-by-other-tool")

        with pytest.raises(
            DatabaseChangedElsewhereError,
            match="changed since the iPod library was loaded",
        ):
            with WriteLock(
                root,
                expected_database_generation=snapshot,
                lock_dir=tmp_path / "seal",
            ):
                pass

    def test_refresh_after_own_commit_keeps_the_session_writable(
        self,
        tmp_path: Path,
    ) -> None:
        """Refreshing after this session's write makes the new bytes expected."""
        root = tmp_path / "rig"
        db_file = _seed_db(root)

        with WriteLock(root, lock_dir=tmp_path / "seal") as session:
            db_file.write_bytes(b"mhbd-own-commit")
            session.refresh_database_generation()

            session.assert_database_unchanged()

    def test_external_database_rewrites_are_rejected_mid_session(
        self,
        tmp_path: Path,
    ) -> None:
        """Edits and late arrivals of iTunesDB are both caught before commit."""
        root = tmp_path / "rig"
        db_file = _seed_db(root)

        with WriteLock(root, lock_dir=tmp_path / "seal") as session:
            db_file.write_bytes(b"mhbd-edited-elsewhere")

            with pytest.raises(
                DatabaseChangedElsewhereError,
                match="changed after this write session started",
            ):
                session.assert_database_unchanged()

        late_root = tmp_path / "rig-late"
        late_root.mkdir()

        with WriteLock(late_root, lock_dir=tmp_path / "seal") as session:
            _seed_db(late_root)

            with pytest.raises(DatabaseChangedElsewhereError):
                session.assert_database_unchanged()


# ---------------------------------------------------------------------------
# Fail-closed write readiness probing
# ---------------------------------------------------------------------------


class TestWriteReadiness:
    def test_virtual_revalidation_reproduces_the_synthesized_identity(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Revalidating a virtual device must reproduce the same identity."""
        chosen = tmp_path / "portable-rig"
        chosen.mkdir()
        (chosen / "iPodInfo.json").write_text("{}", encoding="utf-8")
        sparse_host = _probe(
            tmp_path,
            filesystem_type="",
            identity=VolumeIdentity("windows", "", "", ""),
            detection_errors=("volume query timed out",),
        )
        monkeypatch.setattr(
            write_readiness,
            "profile_volume",
            lambda *_args, **_kwargs: sparse_host,
        )

        seen = write_readiness.check_write_ready(chosen)
        rechecked = write_readiness.recheck_write_ready(seen)

        assert seen.safe_for_writes
        assert seen.identity == rechecked.identity
        assert seen.identity.operating_system == "virtual"

    def test_marker_file_identifies_a_virtual_device_below_a_host_mount(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """A marker file below a larger volume identifies a virtual device."""
        chosen = tmp_path / "portable-rig"
        chosen.mkdir()
        (chosen / "iPodInfo.json").write_text("{}", encoding="utf-8")
        host_view = _probe(tmp_path)
        monkeypatch.setattr(
            write_readiness,
            "profile_volume",
            lambda *_args, **_kwargs: host_view,
        )

        seen = write_readiness.check_write_ready(chosen)

        assert seen.safe_for_writes
        assert seen.mount_path == str(chosen)
        assert seen.identity.operating_system == "virtual"

    def test_foreign_filesystem_fails_the_inspection_closed(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Only FAT and HFS family volumes may ever receive device writes."""
        odd_fs = _probe(tmp_path, filesystem_type="ntfs")
        monkeypatch.setattr(
            write_readiness,
            "profile_volume",
            lambda *_args, **_kwargs: odd_fs,
        )

        with pytest.raises(UnsafeWriteError, match="unsupported filesystem"):
            write_readiness.check_write_ready(tmp_path)

    def test_subdirectory_selection_fails_the_mount_root_check(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Choosing a subdirectory of a mounted volume fails the readiness check."""
        chosen = tmp_path / "mnt" / "JUKEBOX"
        chosen.mkdir(parents=True)
        host_view = _probe(tmp_path)
        monkeypatch.setattr(
            write_readiness,
            "profile_volume",
            lambda *_args, **_kwargs: host_view,
        )

        with pytest.raises(UnsafeWriteError, match="not mounted"):
            write_readiness.check_write_ready(chosen)

    def test_lock_key_encodes_identity_fields_not_labels(self, tmp_path: Path) -> None:
        """The writer key must encode stable identity fields, not a label."""
        seen = _probe(tmp_path)

        assert write_readiness.lock_key_for(seen) == "linux|8:51|/dev/sdg1|326"

    def test_revalidation_refuses_a_swapped_underlying_volume(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """A swapped underlying volume invalidates the retained profile."""
        prior = _probe(tmp_path)
        swapped = _probe(
            tmp_path,
            identity=VolumeIdentity("linux", "8:72", "/dev/sdi3", "344"),
        )
        monkeypatch.setattr(
            write_readiness,
            "recheck_volume",
            lambda _stale, **_kwargs: VolumeRecheck(
                False,
                "volume_swapped",
                "A substitute volume now sits under the inspected path.",
                swapped,
            ),
        )

        with pytest.raises(UnsafeWriteError, match="substitute volume"):
            write_readiness.recheck_write_ready(prior)

    def test_unsafe_mount_warns_then_refuses_the_write(
        self,
        monkeypatch,
        tmp_path: Path,
        caplog,
    ) -> None:
        """Unsafe facts are logged as a warning and turned into a hard error."""
        hazard = _probe(
            tmp_path,
            unsafe_write_reasons=(
                "forced HFS mount bypasses safe write handling",
            ),
        )
        monkeypatch.setattr(
            write_readiness,
            "profile_volume",
            lambda *_args, **_kwargs: hazard,
        )
        caplog.set_level(logging.WARNING, logger=write_readiness.__name__)

        with pytest.raises(UnsafeWriteError, match="forced HFS mount"):
            write_readiness.check_write_ready(tmp_path)

        assert "Unsafe iPod filesystem profile inspected" in caplog.text

        # The refusal does not depend on the log record having been captured.
        with pytest.raises(UnsafeWriteError, match="forced HFS mount"):
            write_readiness.check_write_ready(tmp_path)

    def test_healthy_probing_stays_silent_at_debug_level(
        self,
        monkeypatch,
        tmp_path: Path,
        caplog,
    ) -> None:
        """Healthy probing stays silent even at the most verbose level."""
        sound = _probe(tmp_path)
        monkeypatch.setattr(
            write_readiness,
            "profile_volume",
            lambda *_args, **_kwargs: sound,
        )
        monkeypatch.setattr(
            write_readiness,
            "recheck_volume",
            lambda *_args, **_kwargs: VolumeRecheck(True, "", "", sound),
        )
        caplog.set_level(logging.DEBUG, logger=write_readiness.__name__)

        write_readiness.check_write_ready(tmp_path)
        write_readiness.recheck_write_ready(sound)

        assert caplog.records == []

    def test_readiness_passes_through_the_probed_profile(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Readiness passes the probed profile straight through when safe."""
        expected = _probe(tmp_path)
        monkeypatch.setattr(
            write_readiness,
            "profile_volume",
            lambda *_args, **_kwargs: expected,
        )

        seen = write_readiness.check_write_ready(
            tmp_path,
            reported_volume_format="FAT16",
        )

        assert seen is expected


# ---------------------------------------------------------------------------
# Exclusive host lock serialization
# ---------------------------------------------------------------------------


class TestExclusiveLock:
    def test_bind_aliases_contend_for_one_volume_lock(self, tmp_path: Path) -> None:
        """Two mount points over the same storage contend for the same lock."""
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        first_root.mkdir()
        second_root.mkdir()
        seal = tmp_path / "seal"

        with _held_guard(first_root, seal, key="linux|8:51|/dev/sdg1|326"):
            with pytest.raises(IpodBusyError, match="already writing"):
                with _held_guard(second_root, seal, key="linux|8:51|/dev/sdg1|417"):
                    pass

    def test_second_writer_parks_until_the_holder_leaves(
        self,
        tmp_path: Path,
    ) -> None:
        """In-process writers queue: the second enters only after the first exits."""
        root = tmp_path / "rig"
        root.mkdir()
        seal = tmp_path / "seal"
        holding = Event()
        release = Event()
        queued_start = Event()
        queued_inside = Event()
        order: list[str] = []
        errors: list[BaseException] = []

        def hold() -> None:
            try:
                with _held_guard(root, seal):
                    order.append("held")
                    holding.set()
                    assert release.wait(timeout=2)
            except BaseException as exc:
                errors.append(exc)

        def queue() -> None:
            try:
                assert holding.wait(timeout=2)
                queued_start.set()
                with _held_guard(root, seal):
                    order.append("queued")
                    queued_inside.set()
            except BaseException as exc:
                errors.append(exc)

        holder = Thread(target=hold)
        waiter = Thread(target=queue)
        holder.start()
        assert holding.wait(timeout=2)
        waiter.start()
        assert queued_start.wait(timeout=2)
        assert not queued_inside.wait(timeout=0.1)

        release.set()
        holder.join(timeout=2)
        waiter.join(timeout=2)

        assert not holder.is_alive()
        assert not waiter.is_alive()
        assert errors == []
        assert order == ["held", "queued"]

    def test_second_guard_reports_busy_then_succeeds(self, tmp_path: Path) -> None:
        """A second guard on the same volume reports busy, then works after exit."""
        root = tmp_path / "rig"
        root.mkdir()
        seal = tmp_path / "seal"

        with WriteLock(root, volume_key="volume-884", lock_dir=seal):
            with pytest.raises(IpodBusyError, match="already writing"):
                with WriteLock(root, volume_key="volume-884", lock_dir=seal):
                    pass

        # The lock must be free again once the first writer has left.
        with WriteLock(root, volume_key="volume-884", lock_dir=seal):
            pass

    def test_lock_acquisition_touches_no_database_and_logs_nothing(
        self,
        monkeypatch,
        tmp_path: Path,
        caplog,
    ) -> None:
        """Acquisition can skip the DB entirely and stays quiet at INFO."""
        root = tmp_path / "rig"
        root.mkdir()
        monkeypatch.setattr(
            guard_mod,
            "snapshot_database_state",
            lambda _path: pytest.fail("iTunesDB bytes must stay unread here"),
        )
        caplog.set_level(logging.INFO, logger=guard_mod.__name__)

        with _held_guard(root, tmp_path / "seal"):
            pass

        assert caplog.records == []

    def test_symlinked_lock_leaf_is_refused_untouched(
        self,
        tmp_path: Path,
    ) -> None:
        """Opening the lock must stop at a symlink instead of following it."""
        root = tmp_path / "rig"
        root.mkdir()
        seal = tmp_path / "seal"
        seal.mkdir()
        guard = _held_guard(root, seal)
        bystander = tmp_path / "bystander.txt"
        bystander.write_bytes(b"leave-this-file-alone")
        try:
            guard.lock_path.symlink_to(bystander)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

        with pytest.raises(UnsafeWriteError, match="lock"):
            with guard:
                pass

        assert bystander.read_bytes() == b"leave-this-file-alone"
