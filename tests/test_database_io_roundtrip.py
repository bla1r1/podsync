"""Smoke tests for database installation safety, read-back, and cleanup.

The install path must surface serializer failures on demand, forward the
resolved device capability snapshot, and refuse writes whose media is
missing or duplicated. A committed database is reparsed and every media
reference is checked against the device root. Play-count commits run under
the shared writer queue after a readiness probe, cleanup removes only known
state files while revalidating between each, and playlist datasets parsed
back from disk stay in their own buckets. The final section exercises the
pure playlist builders, track conversion, and binary playlist writers that
feed the serializer. Fixtures are synthesized under ``tmp_path``; nothing
touches a device, a network, or a GUI.
"""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from podsync.hardware import traits_for_model, make_virtual_ipod
from podsync.itdb.reader import play_stats
from podsync.itdb.spec.codes import MEDIA_TYPE_PODCAST
from podsync.itdb.writer import TrackRecord, write_mhyp, write_playlist
from podsync.library import database as library_db
from podsync.library.playlists import assemble_playlists
from podsync.library.tracks import record_from_row


# ────────────────────────────────────────────────────────────────
# Write path: strict failures and capability snapshot
# ────────────────────────────────────────────────────────────────


def test_writer_failures_surface_when_strict_mode_is_requested(
    monkeypatch,
    tmp_path,
) -> None:
    """A serializer fault must escape unchanged when the caller asks for it."""

    class SerializerFault(RuntimeError):
        pass

    def explode(*_args, **_kwargs):
        raise SerializerFault(
            "Cover conversion aborted. Offending file: /music/EP/sleeve.tif"
        )

    monkeypatch.setattr("podsync.itdb.writer.write_itdb", explode)

    with pytest.raises(
        SerializerFault, match="Offending file: /music/EP/sleeve.tif"
    ):
        library_db.save_device_library(tmp_path, [], raise_on_error=True)


def test_write_forwards_the_resolved_capability_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The install path must retain the selected device's 64 MiB profile."""
    high_memory_caps = traits_for_model(
        "iPod",
        "5th Gen",
        capacity="60GB",
    )
    assert high_memory_caps is not None
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "podsync.hardware.selected_device_at",
        lambda _path: SimpleNamespace(
            model_family="iPod",
            generation="5th Gen",
            capacity="60GB",
            model_number="MA455",
            capabilities=high_memory_caps,
        ),
    )
    monkeypatch.setattr(
        "podsync.itdb.writer.write_itdb",
        lambda *_args, **kwargs: captured.update(kwargs) or True,
    )
    monkeypatch.setattr(library_db, "verify_saved_library", lambda *_a, **_k: None)

    assert library_db.save_device_library(tmp_path, []) is True
    assert captured["capabilities"] is high_memory_caps
    assert high_memory_caps.max_database_bytes == 64 * 1024 * 1024


# ────────────────────────────────────────────────────────────────
# Write path: media preconditions on a virtual device
# ────────────────────────────────────────────────────────────────


def test_install_is_refused_when_committed_media_is_absent(
    tmp_path: Path,
) -> None:
    """A track whose media file never landed on the device blocks the write."""
    make_virtual_ipod(tmp_path, "MA005")
    track = TrackRecord(
        title="Ghost Take",
        location=":iPod_Control:Music:F04:ABSENT.mp3",
    )

    assert library_db.save_device_library(tmp_path, [track]) is False


def test_install_is_refused_when_two_tracks_share_one_media_path(
    tmp_path: Path,
) -> None:
    """Two rows pointing at one file are a safety violation, not a merge."""
    make_virtual_ipod(tmp_path, "MA477")
    media_path = tmp_path / "iPod_Control" / "Music" / "F04" / "CLIP.mp3"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"audio")
    location = ":iPod_Control:Music:F04:CLIP.mp3"

    assert library_db.save_device_library(
        tmp_path,
        [
            TrackRecord(title="First Pass", location=location),
            TrackRecord(title="Second Pass", location=location),
        ],
    ) is False


# ────────────────────────────────────────────────────────────────
# Read-back verification
# ────────────────────────────────────────────────────────────────


def test_verification_requires_a_reparseable_database(
    tmp_path: Path,
) -> None:
    """An empty root has nothing to reparse, so verification must fail."""
    with pytest.raises(library_db.ReadbackError, match="could not be reparsed"):
        library_db.verify_saved_library(tmp_path, expected_track_count=0)


def test_verification_rejects_media_pointing_off_the_device(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A location that resolves outside the device root is a hard failure."""
    stray_media = tmp_path.parent / "stray.mp3"
    stray_media.write_bytes(b"audio")
    monkeypatch.setattr(
        library_db,
        "load_device_library",
        lambda *_args, **_kwargs: {
            "tracks": [{"title": "Detour", "location": str(stray_media)}],
        },
    )

    with pytest.raises(library_db.ReadbackError, match="outside the iPod"):
        library_db.verify_saved_library(tmp_path, expected_track_count=1)


def test_duplicate_key_respects_mount_case_sensitivity() -> None:
    """Case-sensitive mounts must not fold names that the firmware keeps apart."""
    upper = library_db._media_identity_key(
        Path("/iPod_Control/Music/Chime.mp3"),
        case_sensitive=True,
    )
    lower = library_db._media_identity_key(
        Path("/iPod_Control/Music/chime.mp3"),
        case_sensitive=True,
    )

    assert upper != lower
    assert library_db._media_identity_key(
        Path("/iPod_Control/Music/Chime.mp3"),
        case_sensitive=False,
    ) == library_db._media_identity_key(
        Path("/iPod_Control/Music/chime.mp3"),
        case_sensitive=False,
    )


# ────────────────────────────────────────────────────────────────
# Standalone play-count commit
# ────────────────────────────────────────────────────────────────


def test_commit_playcounts_rebuilds_and_clears_state_when_deltas_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Play Counts file with real activity triggers a full rebuild and cleanup."""
    make_virtual_ipod(tmp_path, "MA005")
    media_path = tmp_path / "iPod_Control" / "Music" / "F04" / "SPUN.mp3"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"audio")
    track = TrackRecord(title="Spun Up", location=":iPod_Control:Music:F04:SPUN.mp3")
    assert library_db.save_device_library(tmp_path, [track], raise_on_error=True) is True

    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    pc_path = itunes_dir / "Play Counts"
    pc_path.write_bytes(b"placeholder")  # content unused: read_play_stats is stubbed below
    monkeypatch.setattr(
        play_stats, "read_play_stats", lambda _path: [play_stats.PlayStatsEntry(play_count=3)],
    )

    assert library_db.commit_playcounts_if_needed(tmp_path) is True
    assert not pc_path.exists()
    reloaded = library_db.load_device_library(tmp_path)
    assert [row["title"] for row in reloaded["tracks"]] == ["Spun Up"]


def test_commit_playcounts_is_a_noop_without_activity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No Play Counts file, or one with nothing but zeros, commits nothing."""
    make_virtual_ipod(tmp_path, "MA477")

    assert library_db.commit_playcounts_if_needed(tmp_path) is False

    monkeypatch.setattr(play_stats, "read_play_stats", lambda _path: [play_stats.PlayStatsEntry()])
    assert library_db.commit_playcounts_if_needed(tmp_path) is False


# ────────────────────────────────────────────────────────────────
# Device-generated sync state cleanup
# ────────────────────────────────────────────────────────────────


def test_cleanup_touches_only_known_sync_state_files(
    tmp_path: Path,
) -> None:
    """Only the four known state files go; numbered firmware files stay."""
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    cleanup_names = (
        "Play Counts",
        "iTunesStats",
        "PlayCounts.plist",
        "OTGPlaylistInfo",
    )
    for name in cleanup_names:
        (itunes_dir / name).write_bytes(b"state")
    numbered_otg = itunes_dir / "OTGPlaylistInfo_7"
    numbered_otg.write_bytes(b"firmware-owned")

    revalidations: list[str] = []
    library_db.clear_device_play_state(
        tmp_path,
        before_device_mutation=lambda: revalidations.append("checked"),
    )

    assert all(not (itunes_dir / name).exists() for name in cleanup_names)
    assert numbered_otg.read_bytes() == b"firmware-owned"
    assert revalidations == ["checked"] * len(cleanup_names)


@pytest.mark.parametrize("filename", ["iTunesStats", "PlayCounts.plist"])
def test_cleanup_failure_is_reported_as_a_device_safety_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
) -> None:
    """A state file the device refuses to drop becomes a safety error."""
    itunes_dir = tmp_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    state_file = itunes_dir / filename
    state_file.write_bytes(b"state")
    original_unlink = library_db.safe_unlink

    def fail_selected_file(path: Path, *, missing_ok: bool = False) -> None:
        if path.name == filename:
            raise OSError("the volume refused the delete")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(library_db, "safe_unlink", fail_selected_file)

    with pytest.raises(
        library_db.UnsafeWriteError,
        match=rf"could not be cleared \({filename}\): the volume refused the delete",
    ):
        library_db.clear_device_play_state(tmp_path)

    assert state_file.read_bytes() == b"state"


# ────────────────────────────────────────────────────────────────
# Playlist dataset buckets
# ────────────────────────────────────────────────────────────────


def test_read_back_keeps_playlist_datasets_in_separate_buckets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Visible, podcast, and category rows must not leak into each other."""
    ipod_path = tmp_path / "iPod"
    itunes_dir = ipod_path / "iPod_Control" / "iTunes"
    itunes_dir.mkdir(parents=True)
    itdb_path = itunes_dir / "iTunesDB"
    itdb_path.write_bytes(b"mhbd")

    monkeypatch.setattr("podsync.hardware.locate_database", lambda _path: str(itdb_path))
    monkeypatch.setattr(
        "podsync.itdb.reader.read_itdb",
        lambda *_args, **_kwargs: {"raw": True},
    )
    monkeypatch.setattr(
        "podsync.itdb.spec.flatten.split_datasets",
        lambda raw_bytes: {
            "mhlt": [],
            "mhlp": [
                {"playlist_id": 41, "title": "Main Library", "master_flag": 1}
            ],
            "mhlp_podcast": [
                {"playlist_id": 45, "title": "Episode Stack", "master_flag": 1}
            ],
            "mhlp_smart": [
                {
                    "playlist_id": 46,
                    "title": "Tones Bin",
                    "master_flag": 1,
                    "mhsd5_type": 6,
                }
            ],
        },
    )
    monkeypatch.setattr(
        "podsync.itdb.spec.flatten.collect_strings",
        lambda _children: {},
    )
    monkeypatch.setattr(
        "podsync.itdb.spec.flatten.collect_playlist_extras",
        lambda _children: {},
    )
    monkeypatch.setattr(
        "podsync.itdb.reader.cover_links.attach_cover_refs",
        lambda _tracks, _itdb_path: None,
    )
    monkeypatch.setattr(
        "podsync.itdb.reader.play_stats.read_play_stats",
        lambda _path: None,
    )
    monkeypatch.setattr(
        "podsync.itdb.reader.onthego.load_onthego_playlists",
        lambda _itunes_dir, _tracks: [],
    )

    parsed = library_db.load_device_library(ipod_path)

    assert "playlists" not in parsed
    assert "smart_playlists" not in parsed
    assert [row["title"] for row in parsed["dataset2_standard_playlists"]] == [
        "Main Library"
    ]
    assert [row["title"] for row in parsed["dataset3_podcast_playlists"]] == [
        "Episode Stack"
    ]
    assert [row["title"] for row in parsed["dataset5_smart_playlists"]] == [
        "Tones Bin"
    ]


# ────────────────────────────────────────────────────────────────
# Playlist construction from cached rows
# ────────────────────────────────────────────────────────────────


def test_playlist_items_can_reference_library_track_ids() -> None:
    """Items stored with a library id bind straight to that id."""
    track = TrackRecord(
        title="Imported Cut",
        location=":iPod_Control:Music:F04:IMPT.mp3",
        track_id=20,
        db_track_id=555,
    )

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_name,
        _podcast_id,
        _podcast_rows,
        _smart_rows,
    ) = assemble_playlists(
        [{"track_id": 20, "db_track_id": 555}],
        [
            {"playlist_id": 41, "title": "Main", "master_flag": 1},
            {"playlist_id": 42, "title": "Vault", "items": [{"db_track_id": 555}]},
        ],
        [],
        [],
        [track],
    )

    vault = next(row for row in playlists if row.playlist_id == 42)
    assert vault.track_ids == [555]


def test_playlist_items_can_reference_host_source_paths(tmp_path) -> None:
    """A freshly copied file binds by its host path before it has a library id."""
    source = tmp_path / "Lifted.wav"
    source.write_bytes(b"audio")
    track = TrackRecord(
        title="Lifted Cut",
        location=":iPod_Control:Music:F04:LIFT.wav",
        db_track_id=556,
        source_path=str(source),
    )

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_name,
        _podcast_id,
        _podcast_rows,
        _smart_rows,
    ) = assemble_playlists(
        [],
        [
            {"playlist_id": 41, "title": "Main", "master_flag": 1},
            {
                "playlist_id": 42,
                "title": "Loose Singles",
                "items": [{"source_path": str(source)}],
            },
        ],
        [],
        [],
        [track],
        {str(source): 556},
    )

    loose = next(row for row in playlists if row.playlist_id == 42)
    assert loose.track_ids == [556]


def test_live_user_smart_playlist_in_the_visible_bucket_is_evaluated() -> None:
    """A live rule set in the visible bucket is rescored against the library."""
    track = TrackRecord(
        title="Drift",
        location=":iPod_Control:Music:F02:DRFT.mp3",
        track_id=21,
        db_track_id=557,
    )

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_name,
        _podcast_id,
        _podcast_rows,
        smart_rows,
    ) = assemble_playlists(
        [{"track_id": 21, "db_track_id": 557, "title": "Drift"}],
        [
            {"playlist_id": 41, "title": "Main", "master_flag": 1},
            {
                "playlist_id": 43,
                "title": "Auto Lens",
                "_source": "smart",
                "smart_playlist_data": {
                    "live_update": True,
                    "check_rules": True,
                    "check_limits": False,
                },
                "smart_playlist_rules": {"conjunction": "AND", "rules": []},
            },
        ],
        [],
        [],
        [track],
    )

    auto_lens = next(row for row in playlists if row.playlist_id == 43)
    assert auto_lens.is_smart
    assert auto_lens.track_ids == [557]
    assert smart_rows == []


def test_static_user_smart_playlist_keeps_its_stored_membership() -> None:
    """A paused rule set keeps the membership the user pinned on the device."""
    tracks = [
        TrackRecord(
            title="Kept Take",
            location=":iPod_Control:Music:F04:KEPT.mp3",
            track_id=20,
            db_track_id=555,
        ),
        TrackRecord(
            title="Fresh Take",
            location=":iPod_Control:Music:F04:FRESH.mp3",
            track_id=21,
            db_track_id=558,
        ),
    ]

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_name,
        _podcast_id,
        _podcast_rows,
        _smart_rows,
    ) = assemble_playlists(
        [
            {"track_id": 20, "db_track_id": 555, "title": "Kept Take"},
            {"track_id": 21, "db_track_id": 558, "title": "Fresh Take"},
        ],
        [
            {"playlist_id": 41, "title": "Main", "master_flag": 1},
            {
                "playlist_id": 43,
                "title": "Frozen Lens",
                "items": [{"track_id": 20}],
                "smart_playlist_data": {
                    "live_update": False,
                    "check_rules": True,
                    "check_limits": False,
                },
                "smart_playlist_rules": {
                    "conjunction": "AND",
                    "rules": [{
                        "field_id": 0x02,
                        "action_id": 0x01000001,
                        "string_value": "Fresh Take",
                    }],
                },
            },
        ],
        [],
        [],
        tracks,
    )

    frozen = next(row for row in playlists if row.playlist_id == 43)
    assert frozen.is_smart
    assert frozen.track_ids == [555]


def test_visible_bucket_rows_win_over_the_podcast_mirror() -> None:
    """When both mirrors hold a row, the visible bucket supplies the answer."""
    track = TrackRecord(
        title="Ballad",
        location=":iPod_Control:Music:F02:BALL.mp3",
        track_id=20,
        db_track_id=559,
    )

    (
        master_name,
        master_id,
        playlists,
        podcast_master_name,
        podcast_master_id,
        podcast_playlists,
        smart_rows,
    ) = assemble_playlists(
        [{"track_id": 20, "db_track_id": 559, "title": "Ballad"}],
        [
            {"playlist_id": 41, "title": "Main Library", "master_flag": 1},
            {
                "playlist_id": 42,
                "title": "Road Trip",
                "items": [{"db_track_id": 559}],
            },
        ],
        [
            {"playlist_id": 45, "title": "Episode Stack", "master_flag": 1},
            {
                "playlist_id": 46,
                "title": "Latest Shows",
                "items": [{"db_track_id": 559}],
            },
        ],
        [],
        [track],
    )

    assert master_name == "Main Library"
    assert master_id == 41
    assert [row.name for row in playlists] == ["Road Trip"]
    assert podcast_master_name == "Episode Stack"
    assert podcast_master_id == 45
    assert [row.name for row in podcast_playlists] == ["Latest Shows"]
    assert smart_rows == []


def test_podcast_bucket_stays_independent_when_the_visible_bucket_is_empty() -> None:
    """An empty visible bucket still yields its default master identity."""
    track = TrackRecord(
        title="Feature Ep",
        location=":iPod_Control:Music:F06:FEAT.mp3",
        track_id=20,
        db_track_id=560,
    )

    (
        master_name,
        master_id,
        playlists,
        podcast_master_name,
        podcast_master_id,
        podcast_playlists,
        smart_rows,
    ) = assemble_playlists(
        [{"track_id": 20, "db_track_id": 560, "title": "Feature Ep"}],
        [],
        [
            {"playlist_id": 45, "title": "Episode Stack", "master_flag": 1},
            {
                "playlist_id": 46,
                "title": "Latest Shows",
                "items": [{"db_track_id": 560}],
            },
        ],
        [],
        [track],
    )

    assert master_name == "iPod"
    assert master_id is None
    assert playlists == []
    assert podcast_master_name == "Episode Stack"
    assert podcast_master_id == 45
    assert [row.name for row in podcast_playlists] == ["Latest Shows"]
    assert smart_rows == []


def test_podcast_playlist_membership_covers_every_podcast_track() -> None:
    """Stored membership is replaced by the full set of podcast episodes."""
    existing_podcast = TrackRecord(
        title="Old Episode",
        location=":iPod_Control:Music:F06:OLDE.mp3",
        track_id=22,
        db_track_id=561,
        media_type=MEDIA_TYPE_PODCAST,
        podcast_flag=1,
    )
    new_podcast = TrackRecord(
        title="New Episode",
        location=":iPod_Control:Music:F06:NEWP.mp3",
        track_id=23,
        db_track_id=562,
        media_type=MEDIA_TYPE_PODCAST,
        podcast_flag=1,
    )
    song = TrackRecord(
        title="Interlude",
        location=":iPod_Control:Music:F02:INTL.mp3",
        track_id=24,
        db_track_id=563,
    )

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_master,
        _podcast_master_id,
        podcast_playlists,
        _smart_rows,
    ) = assemble_playlists(
        [
            {"track_id": 22, "db_track_id": 561, "title": "Old Episode"},
            {"track_id": 24, "db_track_id": 563, "title": "Interlude"},
        ],
        [
            {"playlist_id": 41, "title": "Main", "master_flag": 1},
            {
                "playlist_id": 47,
                "title": "Shows",
                "podcast_flag": 1,
                "items": [{"db_track_id": 561}],
            },
        ],
        [
            {"playlist_id": 45, "title": "Episode Stack", "master_flag": 1},
            {
                "playlist_id": 47,
                "title": "Shows",
                "podcast_flag": 1,
                "items": [{"db_track_id": 561}],
            },
        ],
        [],
        [existing_podcast, new_podcast, song],
    )

    assert [
        row.track_ids for row in playlists if row.podcast_flag
    ] == [[561, 562]]
    assert [
        row.track_ids for row in podcast_playlists if row.podcast_flag
    ] == [[561, 562]]


def test_converted_track_dict_builds_a_podcast_playlist_row() -> None:
    """Conversion promotes cached flags before the playlist rows are built."""
    converted = {
        "track_id": 25,
        "db_track_id": 564,
        "title": "Rebuilt Episode",
        "album": "Night Signal",
        "location": ":iPod_Control:Music:F06:CNVP.mp3",
        "media_type": MEDIA_TYPE_PODCAST,
        "podcast_now_playing": 1,
        "skip_when_shuffling": 1,
        "remember_position": 1,
    }
    track = record_from_row(converted)

    assert track.media_type == MEDIA_TYPE_PODCAST
    assert track.podcast_flag == 1
    assert track.skip_when_shuffling is True
    assert track.remember_position is True

    (
        _master_name,
        _master_id,
        _playlists,
        _podcast_master,
        _podcast_master_id,
        podcast_playlists,
        _smart_rows,
    ) = assemble_playlists(
        [converted],
        [{"playlist_id": 41, "title": "Main", "master_flag": 1}],
        [],
        [],
        [track],
    )

    assert [
        (row.name, row.track_ids, row.podcast_flag) for row in podcast_playlists
    ] == [("Podcasts", [564], 1)]


def test_podcast_bucket_gains_a_playlist_when_the_device_has_none() -> None:
    """Podcast episodes force a dataset-3 playlist even with no stored row."""
    podcast = TrackRecord(
        title="Episode",
        location=":iPod_Control:Music:F06:EPIS.mp3",
        track_id=26,
        db_track_id=565,
        media_type=MEDIA_TYPE_PODCAST,
        podcast_flag=1,
    )

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_master,
        _podcast_master_id,
        podcast_playlists,
        _smart_rows,
    ) = assemble_playlists(
        [{"track_id": 26, "db_track_id": 565, "title": "Episode"}],
        [{"playlist_id": 41, "title": "Main", "master_flag": 1}],
        [],
        [],
        [podcast],
    )

    assert playlists == []
    assert [
        (row.name, row.track_ids, row.podcast_flag) for row in podcast_playlists
    ] == [("Podcasts", [565], 1)]


def test_smart_rules_resolving_playlist_ids_use_those_playlists_membership() -> None:
    """A playlist-membership rule scores through the referenced playlists."""
    slow_burn_id = 0x1B6A44C7E2D0935F
    amber_loop_id = 0x98C2E5B14A7703D6
    tracks = [
        TrackRecord(
            title="Ember",
            location=":iPod_Control:Music:F02:EMBR.mp3",
            track_id=20,
            db_track_id=566,
        ),
        TrackRecord(
            title="Copper",
            location=":iPod_Control:Music:F02:COPP.mp3",
            track_id=21,
            db_track_id=567,
        ),
        TrackRecord(
            title="Other Cut",
            location=":iPod_Control:Music:F02:OTHR.mp3",
            track_id=22,
            db_track_id=568,
        ),
    ]

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_master,
        _podcast_master_id,
        _podcast_rows,
        _smart_rows,
    ) = assemble_playlists(
        [
            {"track_id": 20, "db_track_id": 566, "title": "Ember"},
            {"track_id": 21, "db_track_id": 567, "title": "Copper"},
            {"track_id": 22, "db_track_id": 568, "title": "Other Cut"},
        ],
        [
            {"playlist_id": 41, "title": "Main", "master_flag": 1},
            {
                "playlist_id": slow_burn_id,
                "title": "Slow Burn",
                "items": [{"track_id": 20}],
            },
            {
                "playlist_id": amber_loop_id,
                "title": "Amber Loop",
                "items": [{"track_id": 21}],
            },
            {
                "playlist_id": 0x52DF80B39C1E6A47,
                "title": "Rule Shelf",
                "smart_playlist_data": {
                    "live_update": True,
                    "check_rules": True,
                    "check_limits": False,
                },
                "smart_playlist_rules": {
                    "conjunction": "OR",
                    "rules": [
                        {
                            "field_id": 0x28,
                            "action_id": 0x00000001,
                            "from_value": slow_burn_id,
                            "to_value": slow_burn_id,
                            "from_units": 1,
                            "to_units": 1,
                        },
                        {
                            "field_id": 0x28,
                            "action_id": 0x00000001,
                            "from_value": amber_loop_id,
                            "to_value": amber_loop_id,
                            "from_units": 1,
                            "to_units": 1,
                        },
                    ],
                },
            },
        ],
        [],
        [],
        tracks,
    )

    rule_shelf = next(row for row in playlists if row.name == "Rule Shelf")
    assert rule_shelf.track_ids == [566, 567]


def test_existing_dataset5_smart_row_stays_in_its_own_bucket() -> None:
    """A stored dataset-5 row never migrates into the visible bucket."""
    track = TrackRecord(
        title="Signal",
        location=":iPod_Control:Music:F02:SIGN.mp3",
        track_id=20,
        db_track_id=569,
    )

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_master,
        _podcast_master_id,
        _podcast_rows,
        smart_rows,
    ) = assemble_playlists(
        [{"track_id": 20, "db_track_id": 569, "title": "Signal"}],
        [{"playlist_id": 41, "title": "Main", "master_flag": 1}],
        [],
        [
            {
                "playlist_id": 40,
                "title": "Archive Lens",
                "mhsd5_type": 0,
                "smart_playlist_data": {
                    "live_update": True,
                    "check_rules": True,
                    "check_limits": False,
                },
                "smart_playlist_rules": {"conjunction": "AND", "rules": []},
            }
        ],
        [track],
    )

    assert playlists == []
    archive_lens = next(row for row in smart_rows if row.playlist_id == 40)
    assert archive_lens.is_smart
    assert archive_lens.track_ids == [569]


def test_dataset5_category_row_keeps_the_cached_firmware_marker() -> None:
    """The type byte from the cache is preserved exactly, not inferred."""
    track = TrackRecord(
        title="Signal",
        location=":iPod_Control:Music:F02:SIGN.mp3",
        track_id=20,
        db_track_id=570,
    )

    (
        _master_name,
        _master_id,
        _playlists,
        _podcast_master,
        _podcast_master_id,
        _podcast_rows,
        smart_rows,
    ) = assemble_playlists(
        [{"track_id": 20, "db_track_id": 570, "title": "Signal"}],
        [{"playlist_id": 41, "title": "Main", "master_flag": 1}],
        [],
        [
            {
                "playlist_id": 40,
                "title": "Moods",
                "_source": "category",
                "master_flag": 0,
                "mhsd5_type": "4",
                "smart_playlist_data": {
                    "live_update": True,
                    "check_rules": True,
                    "check_limits": False,
                },
                "smart_playlist_rules": {"conjunction": "AND", "rules": []},
            }
        ],
        [track],
    )

    assert len(smart_rows) == 1
    assert smart_rows[0].master is False
    assert smart_rows[0].mhsd5_type == 4


def test_dataset5_category_row_preserves_membership_and_item_metadata() -> None:
    """A categorized row keeps both its stored members and their MHIP detail."""
    tracks = [
        TrackRecord(
            title="Included",
            location=":iPod_Control:Music:F02:INCL.mp3",
            track_id=20,
            db_track_id=571,
        ),
        TrackRecord(
            title="Rule Match Later",
            location=":iPod_Control:Music:F02:RULE.mp3",
            track_id=30,
            db_track_id=700,
        ),
    ]

    (
        _master_name,
        _master_id,
        _playlists,
        _podcast_master,
        _podcast_master_id,
        _podcast_rows,
        smart_rows,
    ) = assemble_playlists(
        [
            {"track_id": 20, "db_track_id": 571, "title": "Included"},
            {"track_id": 30, "db_track_id": 700, "title": "Rule Match Later"},
        ],
        [{"playlist_id": 41, "title": "Main", "master_flag": 1}],
        [],
        [
            {
                "playlist_id": 40,
                "title": "Moods",
                "_source": "category",
                "master_flag": 1,
                "mhsd5_type": 4,
                "items": [
                    {
                        "track_id": 20,
                        "podcast_group_flag": 0,
                        "group_id": 61,
                        "group_link": 0,
                        "track_persistent_id": 571,
                        "mhip_persistent_id": 987,
                    }
                ],
                "smart_playlist_data": {
                    "live_update": True,
                    "check_rules": True,
                    "check_limits": False,
                },
                "smart_playlist_rules": {"conjunction": "AND", "rules": []},
            }
        ],
        tracks,
    )

    assert smart_rows[0].track_ids == [571]
    assert smart_rows[0].item_metadata is not None
    assert smart_rows[0].item_metadata[0].group_id == 61
    assert smart_rows[0].item_metadata[0].mhip_persistent_id == 987


def test_ringtone_category_carries_the_special_header_flag() -> None:
    """The ringtones category sets both mirror words and the special flag."""
    data = write_mhyp(
        "Ringtones",
        [],
        playlist_id=0x51A7,
        master=True,
        mhsd5_type=6,
    )

    assert struct.unpack_from("<H", data, 0x50)[0] == 6
    assert struct.unpack_from("<H", data, 0x52)[0] == 6
    assert struct.unpack_from("<I", data, 0x54)[0] == 1


def test_phase_marker_survives_the_builder_into_the_writer() -> None:
    """The opaque +0x52 word rides from the cached row into the bytes."""
    (
        _master_name,
        _master_id,
        playlists,
        _podcast_master,
        _podcast_master_id,
        _podcast_rows,
        _smart_rows,
    ) = assemble_playlists(
        [],
        [
            {"playlist_id": 41, "title": "Main", "master_flag": 1},
            {"playlist_id": 44, "title": "Nightphase", "phase_game_flag": 37},
        ],
        [],
        [],
        [],
    )

    assert len(playlists) == 1
    assert playlists[0].phase_game_flag == 37
    assert struct.unpack_from("<H", write_playlist(playlists[0]), 0x52)[0] == 37


def test_dataset5_marker_inside_the_visible_bucket_is_left_alone() -> None:
    """A category row parked in the visible bucket keeps its marker as parsed."""
    track = TrackRecord(
        title="Reel",
        location=":iPod_Control:Music:F02:REEL.m4b",
        track_id=20,
        db_track_id=573,
    )

    (
        _master_name,
        _master_id,
        playlists,
        _podcast_master,
        _podcast_master_id,
        _podcast_rows,
        _smart_rows,
    ) = assemble_playlists(
        [{"track_id": 20, "db_track_id": 573, "title": "Reel"}],
        [
            {"playlist_id": 41, "title": "Main", "master_flag": 1},
            {
                "playlist_id": 47,
                "title": "Spoken Shelf",
                "_source": "category",
                "mhsd5_type": 5,
                "smart_playlist_data": {
                    "live_update": True,
                    "check_rules": True,
                    "check_limits": False,
                },
                "smart_playlist_rules": {"conjunction": "AND", "rules": []},
            },
        ],
        [],
        [],
        [track],
    )

    assert len(playlists) == 1
    assert playlists[0].name == "Spoken Shelf"
    assert playlists[0].mhsd5_type == 5


# ────────────────────────────────────────────────────────────────
# Reference accounting
# ────────────────────────────────────────────────────────────────
