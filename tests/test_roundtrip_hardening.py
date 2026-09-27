"""Round-trip coverage: playlist property plists, playlist edit lifecycle,
device-clock contexts, and iPod video filetype mapping."""

from __future__ import annotations

import plistlib
import struct
import zoneinfo
from datetime import UTC, datetime
from io import BytesIO

import pytest

from podsync.itdb.reader import read_itdb
from podsync.itdb.reader.chunks.strings import parse_data_object
from podsync.itdb.reader.play_stats import PlayStatsEntry, apply_play_stats
from podsync.itdb.spec.codes import MHOD_TYPE_PLAYLIST_PROPERTY_PLIST
from podsync.itdb.spec.clock import (
    DeviceClock,
    MacTimeRangeError,
    load_device_clock,
    zone_changed_since_write,
    device_clock_scope,
)
from podsync.itdb.spec.flatten import (
    split_datasets,
    collect_playlist_extras,
)
from podsync.itdb.spec.layouts.strings import MHOD_HEADER_SIZE, write_mhod_header
from podsync.itdb.spec.playlists.lifecycle import playlist_edit_payload
from podsync.itdb.spec.playlists.properties import (
    normalize_playlist_description,
    playlist_description_from_row,
    playlist_description_update_fields,
    playlist_property_raw_body_for_write,
)
from podsync.itdb.writer.database import write_mhbd
from podsync.itdb.writer.track import TrackRecord
from podsync.itdb.writer.playlist import PlaylistRecord, write_playlist
from podsync.library.tracks import (
    format_for_extension,
    record_from_row,
)

# ---------------------------------------------------------------------------
# playlist property plist (mhod type 55)
# ---------------------------------------------------------------------------


def _binary_plist(payload: dict) -> bytes:
    return plistlib.dumps(payload, fmt=plistlib.FMT_BINARY)


def test_property_plist_mhod_parses_body_and_keeps_raw_copy() -> None:
    body = _binary_plist({"description": "kept in the folder note"})
    blob = (
        write_mhod_header(
            MHOD_TYPE_PLAYLIST_PROPERTY_PLIST, MHOD_HEADER_SIZE + len(body),
        )
        + body
    )
    entry = parse_data_object(blob, 0, MHOD_HEADER_SIZE, len(blob))["data"]

    assert entry["mhod_type"] == MHOD_TYPE_PLAYLIST_PROPERTY_PLIST
    assert entry["data"]["description"] == "kept in the folder note"
    assert entry["data"]["raw_body"] == body

    extras = collect_playlist_extras([{"data": entry}])
    assert extras["playlist_description"] == "kept in the folder note"
    assert extras["playlist_property_plist"]["raw_body"] == body


def test_playlist_serialisation_emits_description_and_property_plist() -> None:
    body = _binary_plist({"description": "radiant glow"})
    playlist = PlaylistRecord(
        name="Deep Cuts",
        playlist_description="radiant glow",
        raw_mhod55=body,
    )

    blob = write_playlist(playlist, db_id_2=909)

    assert blob.count(b"mhod") == 4
    assert (
        write_mhod_header(
            MHOD_TYPE_PLAYLIST_PROPERTY_PLIST, MHOD_HEADER_SIZE + len(body),
        )
        + body
    ) in blob
    assert "radiant glow".encode("utf-16-le") in blob


def test_stored_property_plist_seeds_description_fields() -> None:
    row = {"playlist_property_plist": {
        "raw_body": _binary_plist({"description": "summer sessions"}),
    }}

    normalize_playlist_description(row)

    assert row["playlist_description"] == "summer sessions"
    assert row["album"] == "summer sessions"
    assert playlist_description_from_row(row) == "summer sessions"
    assert (
        playlist_property_raw_body_for_write(row)
        == row["playlist_property_plist"]["raw_body"]
    )


def test_description_edit_updates_plist_without_dropping_extra_keys() -> None:
    raw = _binary_plist({"description": "draft one", "future": {"rev": 7}})

    fields = playlist_description_update_fields(
        "draft two", {"playlist_property_plist": {"raw_body": raw}},
    )
    updated = playlist_property_raw_body_for_write(fields)

    assert fields["playlist_description"] == "draft two"
    assert fields["album"] == "draft two"
    assert updated is not None
    assert plistlib.loads(updated) == {
        "description": "draft two",
        "future": {"rev": 7},
    }


# ---------------------------------------------------------------------------
# playlist edit lifecycle
# ---------------------------------------------------------------------------


def test_edit_payload_keeps_playlist_origin_and_membership_intact() -> None:
    original = {
        "playlist_id": 0x9A31,
        "_mhsd_dataset_type": 2,
        "_mhsd_result_key": "mhlp",
        "_source": "regular",
        "podcast_flag": 0,
        "mhsd5_type": 0,
        "items": [{"track_id": 14}],
        "playlist_settings": {"columns": 3},
        "playlist_property_plist": {
            "raw_body": b"legacy",
            "plist": {"description": "before edit", "unknown": 91},
        },
    }

    edited = playlist_edit_payload(
        original,
        {
            "title": "Road Trip Mix",
            "_isNew": False,
            "playlist_description": "after edit",
            "album": "after edit",
        },
    )

    assert edited["title"] == "Road Trip Mix"
    assert edited["_mhsd_dataset_type"] == 2
    assert edited["_mhsd_result_key"] == "mhlp"
    assert edited["_source"] == "regular"
    assert edited["items"] == [{"track_id": 14}]
    assert edited["playlist_settings"] == {"columns": 3}
    assert edited["playlist_description"] == "after edit"
    assert edited["album"] == "after edit"


def test_new_folder_playlist_edit_gets_canonical_kind_fields() -> None:
    payload = playlist_edit_payload(
        None,
        {
            "title": "Genre Vaults",
            "is_folder": True,
            "parent_folder_playlist_id": 0x4C21,
        },
    )

    assert payload["playlist_kind_flags"] == 0x0100
    assert payload["podcast_flag"] == 0x0100
    assert payload["is_folder"] is True
    assert payload["is_podcast"] is False
    assert payload["parent_folder_playlist_id"] == 0x4C21
    assert payload["unk0x30_playlist_ref"] == 0x4C21


# ---------------------------------------------------------------------------
# device clock contexts
# ---------------------------------------------------------------------------


def _minimal_tzif(offset_seconds: int) -> bytes:
    """Build a tiny single-transition TZif file with one fixed UTC offset."""
    header = b"TZif" + b"\x00" + b"\x00" * 15 + struct.pack(
        ">6I", 0, 0, 0, 0, 1, 0,
    )
    body = struct.pack(">iBB", offset_seconds, 0, 0)
    return header + body


def test_captured_mac_timestamps_interpret_by_context_offset() -> None:
    ahead = DeviceClock.fixed_offset(3_600)
    behind = DeviceClock.fixed_offset(-18_000)
    instant = int(datetime(2026, 9, 10, 14, 5, 7, tzinfo=UTC).timestamp())

    assert ahead.unix_to_mac(instant) == 3_871_897_507
    assert behind.unix_to_mac(instant) == 3_871_875_907
    assert ahead.mac_to_unix(3_871_897_507) == instant
    assert behind.mac_to_unix(3_871_875_907) == instant


def test_mac_unix_round_trip_is_stable_under_fixed_offset() -> None:
    context = DeviceClock.fixed_offset(-14_400)
    instant = int(datetime(2026, 7, 15, 12, 0, tzinfo=UTC).timestamp())

    assert context.mac_to_unix(context.unix_to_mac(instant)) == instant


def test_mac_overflow_boundary_raises_instead_of_wrapping() -> None:
    context = DeviceClock.utc()

    assert context.unix_to_mac(2_212_122_495) == 4_294_967_295
    with pytest.raises(MacTimeRangeError):
        context.unix_to_mac(2_212_122_496)


def test_device_preferences_resolve_city_zone_from_available_data(tmp_path) -> None:
    preferences = tmp_path / "iPod_Control" / "Device" / "Preferences"
    preferences.parent.mkdir(parents=True)
    raw = bytearray(2956)
    raw[0xB70:0xB72] = (0x5D).to_bytes(2, "little")
    preferences.write_bytes(raw)

    zone_root = tmp_path / "zoneinfo"
    paris = zone_root / "Europe" / "Paris"
    paris.parent.mkdir(parents=True)
    paris.write_bytes(_minimal_tzif(7_200))
    london = zone_root / "Europe" / "London"
    london.write_bytes(_minimal_tzif(3_600))

    original_tzpath = zoneinfo.TZPATH
    zoneinfo.reset_tzpath((str(zone_root),))
    try:
        context = load_device_clock(tmp_path, database_offset=-14_400)
        assert context.name == "Europe/Paris"
        assert context.city_id == 0x5D
        assert context.source == "device_preferences"
        assert zone_changed_since_write(
            context,
            -14_400,
            now=int(datetime(2026, 8, 1, 10, tzinfo=UTC).timestamp()),
        )

        raw[0xB70:0xB72] = (0x26).to_bytes(2, "little")
        preferences.write_bytes(raw)
        second = load_device_clock(tmp_path)
        assert second.name == "Europe/London"
        assert second.city_id == 0x26
    finally:
        zoneinfo.reset_tzpath(original_tzpath)


def test_playcount_merge_uses_capture_context_not_host_clock() -> None:
    tracks = [{"play_count": 0, "last_played": 0}]
    entries = [PlayStatsEntry(play_count=2, last_played_mac=3_871_901_107)]

    apply_play_stats(
        tracks,
        entries,
        time_context=DeviceClock.fixed_offset(7_200),
    )

    assert tracks[0]["play_count"] == 2
    assert tracks[0]["recent_playcount"] == 2
    assert tracks[0]["last_played"] == int(
        datetime(2026, 9, 10, 14, 5, 7, tzinfo=UTC).timestamp(),
    )


def test_written_db_timestamps_round_trip_via_active_context() -> None:
    context = DeviceClock.fixed_offset(-18_000)
    instant = int(datetime(2026, 7, 20, 9, 45, 30, tzinfo=UTC).timestamp())
    track = TrackRecord(
        title="Clock Trace",
        location=":iPod_Control:Music:F02:TIME.m4a",
        date_added=instant,
        last_modified=instant,
        last_played=instant,
        last_skipped=instant,
    )

    with device_clock_scope(context):
        blob = write_mhbd([track])

    parsed = read_itdb(BytesIO(blob), time_context=context)
    row = split_datasets(parsed)["mhlt"][0]

    assert parsed["timezone_offset"] == -18_000
    assert row["date_added"] == instant
    assert row["last_modified"] == instant
    assert row["last_played"] == instant
    assert row["last_skipped"] == instant


# ---------------------------------------------------------------------------
# track filetype mapping
# ---------------------------------------------------------------------------


def test_mov_extension_selects_ipod_video_filetype() -> None:
    assert format_for_extension(".mov") == "m4v"


def test_uppercase_mov_dict_filetype_reads_as_video() -> None:
    current = record_from_row({
        "title": "Reel Test",
        "location": ":iPod_Control:Music:F04:REEL.mov",
        "filetype": "MOV",
    })

    assert current.filetype == "m4v"
