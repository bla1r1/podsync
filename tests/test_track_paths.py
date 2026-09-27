"""Track-location resolution and safety filtering for on-device paths.

iTunesDB stores device locations such as ``:iPod_Control:Music:Fxx:Song.mp3``,
but Windows drive paths, file URIs, and absolute host paths also appear in
imported or legacy databases. These tests pin down how each form maps onto
(or is refused by) the expected on-device path: accepted spellings, refused
spellings, lookup by the existing file, filename fallback, and the colon
string a device location must format back into. Everything runs in-process
under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from podsync.library.media_paths import (
    PathEscapeError,
    find_media_file,
    expected_media_path,
    location_for_media_path,
)


class TestColonLocationFormatting:
    def test_music_files_format_to_colon_locations_and_reject_host_paths(
        self,
        tmp_path: Path,
    ) -> None:
        device_root = tmp_path / "player"
        stored = device_root / "iPod_Control" / "Music" / "F07" / "WISP.mp3"

        assert (
            location_for_media_path(device_root, stored)
            == ":iPod_Control:Music:F07:WISP.mp3"
        )

        with pytest.raises(PathEscapeError, match="outside the iPod music"):
            location_for_media_path(device_root, tmp_path / "stray-host.mp3")


class TestExistingFileLookup:
    @pytest.mark.parametrize(
        "template",
        [
            ":iPod_Control:Music:F07:WISP.mp3",
            r"K:\iPod_Control\Music\F07\WISP.mp3",
            "{abs}",
            "file:///Volumes/DECK/iPod_Control/Music/F07/{name}",
        ],
    )
    def test_every_common_spelling_resolves_to_the_stored_file(
        self,
        tmp_path: Path,
        template: str,
    ) -> None:
        device_root = tmp_path / "player"
        stored = device_root / "iPod_Control" / "Music" / "F07" / "WISP.mp3"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"audio")

        location = template.format(abs=str(stored), name=stored.name)
        record = location if location.startswith("file:") else {"location": location}

        assert find_media_file(device_root, record) == stored

    def test_fallback_finds_a_stored_file_whose_folder_moved(
        self,
        tmp_path: Path,
    ) -> None:
        device_root = tmp_path / "player"
        stored = device_root / "iPod_Control" / "Music" / "F47" / "GEMS.m4a"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"audio")

        assert find_media_file(
            device_root,
            {"location": ":iPod_Control:Music:F00:GEMS.mp3"},
            allow_music_filename_fallback=True,
        ) == stored


class TestExpectedPathMapping:
    @pytest.mark.parametrize(
        "spelling, wanted",
        [
            (":iPod_Control:Music:F07:WISP.mp3", "iPod_Control/Music/F07/WISP.mp3"),
            (
                r"K:\iPod_Control\Music\F09\WISP.flac",
                "iPod_Control/Music/F09/WISP.flac",
            ),
        ],
    )
    def test_accepted_spellings_map_onto_the_device_folders(
        self,
        tmp_path: Path,
        spelling: str,
        wanted: str,
    ) -> None:
        device_root = tmp_path / "player"

        assert expected_media_path(device_root, spelling) == (
            device_root.joinpath(*wanted.split("/"))
        )

    def test_host_style_spellings_resolve_to_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        device_root = tmp_path / "player"
        stray = tmp_path / "host-copies" / "oddball.mp3"
        stray.parent.mkdir()
        stray.write_bytes(b"audio")

        assert expected_media_path(device_root, r"Q:\Media\Tunes\Flux.mp3") is None
        assert expected_media_path(
            device_root,
            ":iPod_Control:Music:F07:..:..:escape.mp3",
        ) is None
        assert expected_media_path(
            device_root,
            ":iPod_Control:Music:F07:Clair.mp3\x00../../escape.mp3",
        ) is None
        assert expected_media_path(device_root, stray) is None

    def test_a_music_folder_linked_off_the_device_resolves_to_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        device_root = tmp_path / "player"
        music_root = device_root / "iPod_Control" / "Music"
        elsewhere = tmp_path / "elsewhere"
        music_root.mkdir(parents=True)
        elsewhere.mkdir()
        try:
            (music_root / "F07").symlink_to(elsewhere, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable here: {exc}")

        assert expected_media_path(
            device_root,
            ":iPod_Control:Music:F07:WISP.mp3",
        ) is None


# ────────────────────────────────────────────────────────────────
# Reference accounting
# ────────────────────────────────────────────────────────────────
