"""Database-writing coverage: dataset layout, artwork/size failures, string caps,
and writer hardening for hostile track metadata."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from podsync.hardware import ModelTraits, traits_for_model
from podsync.hardware.safety.limits import FileTooLargeError
from podsync.itdb.reader import read_itdb
from podsync.itdb.spec.codes import (
    MHOD_TYPE_LYRICS,
    MHOD_TYPE_PODCAST_RSS_URL,
    MHOD_TYPE_TITLE,
)
from podsync.itdb.spec.flatten import split_datasets, collect_strings
from podsync.itdb.writer import database as db_writer
from podsync.itdb.writer.database import write_mhbd
from podsync.itdb.writer.track import TrackRecord
from podsync.itdb.writer.strings import (
    MHOD_LONG_TEXT_MAX_UTF16_BYTES,
    MHOD_STRING_MAX_UTF16_BYTES,
    MHOD_URL_MAX_UTF8_BYTES,
    write_mhod_podcast_url,
    write_mhod_string,
)
from podsync.itdb.writer.playlist import PlaylistRecord, EntryMeta

# ---------------------------------------------------------------------------
# dataset (mhsd) layout
# ---------------------------------------------------------------------------


def _dataset_codes(database: bytes) -> list[int]:
    """Collect top-level mhsd type codes by walking declared block lengths."""
    head_len = int.from_bytes(database[4:8], "little")
    dataset_total = int.from_bytes(database[0x14:0x18], "little")
    cursor = head_len
    codes: list[int] = []
    for _ in range(dataset_total):
        assert database[cursor:cursor + 4] == b"mhsd"
        block_len = int.from_bytes(database[cursor + 8:cursor + 12], "little")
        codes.append(int.from_bytes(database[cursor + 12:cursor + 16], "little"))
        cursor += block_len
    return codes


def _only_track_row(database: bytes) -> dict:
    parsed = read_itdb(BytesIO(database))
    return split_datasets(parsed)["mhlt"][0]


def test_reference_layout_honours_requested_dataset_sequence() -> None:
    blob = write_mhbd(
        [],
        reference_info={
            "version": 0x73,
            "mhsd_types": {1, 2, 3, 4, 5},
            "mhsd_order": [4, 5, 1, 3, 2],
        },
        capabilities=traits_for_model("iPod Mini", "2nd Gen"),
    )

    assert _dataset_codes(blob) == [4, 5, 1, 3, 2]


def test_legacy_mini_layout_discards_artist_dataset_block() -> None:
    blob = write_mhbd(
        [],
        reference_info={
            "version": 0x73,
            "mhsd_types": {1, 2, 3, 4, 5, 8},
            "mhsd_order": [4, 1, 8, 3, 2, 5],
        },
        capabilities=traits_for_model("iPod Mini", "2nd Gen"),
    )

    assert _dataset_codes(blob) == [4, 1, 3, 2, 5]


def test_current_reference_layout_keeps_artist_dataset_block() -> None:
    blob = write_mhbd(
        [],
        reference_info={
            "version": 0x30,
            "mhsd_types": {1, 2, 3, 4, 5, 8},
            "mhsd_order": [8, 4, 1, 3, 2, 5],
        },
    )

    assert _dataset_codes(blob) == [8, 4, 1, 3, 2, 5]


def test_type3_playlist_reference_gains_type2_companion() -> None:
    blob = write_mhbd(
        [],
        reference_info={
            "version": 0x30,
            "mhsd_types": {1, 3, 4, 5},
            "mhsd_order": [1, 5, 3, 4],
        },
    )

    assert _dataset_codes(blob) == [1, 5, 3, 2, 4]


def test_reference_platform_bit_survives_into_db_header() -> None:
    blob = write_mhbd([], reference_info={"platform": 1})

    assert int.from_bytes(blob[0x20:0x22], "little") == 1


# ---------------------------------------------------------------------------
# write_itunesdb failure surfaces
# ---------------------------------------------------------------------------


def test_artwork_stage_failure_propagates_to_db_writer(monkeypatch, tmp_path) -> None:
    ipod = tmp_path / "ipod"
    (ipod / "iPod_Control" / "iTunes").mkdir(parents=True)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("cover art recolor step exploded")

    monkeypatch.setattr(
        "podsync.artwork.writer.pipeline.write_artworkdb", _explode,
    )
    monkeypatch.setattr("podsync.hardware.database_filename_for_write", lambda _p: "iTunesDB")
    monkeypatch.setattr("podsync.hardware.locate_database", lambda _p: None)

    with pytest.raises(RuntimeError, match="cover art recolor step exploded"):
        db_writer.write_itdb(
            str(ipod),
            [TrackRecord(title="Blue Monday",
                       location=":iPod_Control:Music:F03:blue.mp3")],
            pc_file_paths={7: "/library/media/blue.mp3"},
        )


def test_rejected_database_is_still_offered_for_inspection(monkeypatch, tmp_path) -> None:
    ipod = tmp_path / "ipod"
    (ipod / "iPod_Control" / "iTunes").mkdir(parents=True)
    monkeypatch.setattr(
        db_writer,
        "check_write_ready",
        lambda _path: SimpleNamespace(
            max_file_size_bytes=None, allocation_unit_size=1,
        ),
    )

    with pytest.raises(FileTooLargeError) as caught:
        db_writer.write_itdb(
            str(ipod),
            [TrackRecord(
                title="Big Lyrics",
                location=":iPod_Control:Music:F00:big.mp3",
                lyrics="verse line " * 120,
            )],
            backup=False,
            capabilities=ModelTraits(max_database_bytes=1),
        )

    assert caught.value.proposed_database_bytes.startswith(b"mhbd")
    assert caught.value.proposed_database_filename == "iTunesDB"


# ---------------------------------------------------------------------------
# mhod payload size caps
# ---------------------------------------------------------------------------


def test_plain_title_mhod_length_field_hits_standard_cap() -> None:
    chunk = write_mhod_string(MHOD_TYPE_TITLE, "x" * 10_000)
    declared = int.from_bytes(chunk[0x1C:0x20], "little")

    assert declared == MHOD_STRING_MAX_UTF16_BYTES
    assert len(chunk) == 40 + MHOD_STRING_MAX_UTF16_BYTES


def test_lyrics_mhod_length_field_hits_extended_cap() -> None:
    chunk = write_mhod_string(MHOD_TYPE_LYRICS, "y" * 100_000)
    declared = int.from_bytes(chunk[0x1C:0x20], "little")

    assert declared == MHOD_LONG_TEXT_MAX_UTF16_BYTES
    assert len(chunk) == 40 + MHOD_LONG_TEXT_MAX_UTF16_BYTES


def test_half_million_char_lyrics_stay_within_legacy_size() -> None:
    blob = write_mhbd(
        [TrackRecord(
            title="Oversized Lyric Sheet",
            location=":iPod_Control:Music:F07:BULK.m4a",
            lyrics="q" * 500_000,
        )],
        capabilities=traits_for_model("iPod Mini", "2nd Gen"),
    )
    row = _only_track_row(blob)
    lyric = next(
        child["data"]["string"] for child in row["children"]
        if child["data"]["mhod_type"] == MHOD_TYPE_LYRICS
    )

    assert len(lyric.encode("utf-16-le")) == MHOD_LONG_TEXT_MAX_UTF16_BYTES
    assert len(blob) < 30_000


def test_track_without_lyrics_carries_no_lyrics_declaration() -> None:
    row = _only_track_row(write_mhbd(
        [TrackRecord(title="Plain Song",
                   location=":iPod_Control:Music:F01:PLAIN.m4a")],
    ))

    assert row["has_lyrics_flag"] == 0
    assert all(child["data"]["mhod_type"] != MHOD_TYPE_LYRICS
               for child in row["children"])


# ---------------------------------------------------------------------------
# writer hardening
# ---------------------------------------------------------------------------


def test_writer_normalizes_impossible_track_scalar_values() -> None:
    row = _only_track_row(write_mhbd([TrackRecord(
        title="",
        location=":iPod_Control:Music:F00:BAD.mp3",
        size=-42,
        length=150_000,
        sample_rate=192_000,
        rating=175,
        volume=600,
        start_time=220_000,
        stop_time=2_500,
        bookmark_time=999_999,
        play_count=-7,
        skip_count=-9,
        bpm="fast",
        db_track_id=-8,
    )]))
    strings = collect_strings(row["children"])

    assert strings["title"] == "Unknown Title"
    assert row["size"] == 0
    assert row["sample_rate"] == 48_000
    assert row["rating"] == 100
    assert row["volume"] == 255
    assert row["start_time"] == 0
    assert row["stop_time"] == 0
    assert row["bookmark_time"] == 150_000
    assert row["play_count"] == 0
    assert row["skip_count"] == 0
    assert row["bpm"] == 0
    assert row["db_track_id"] > 0
    assert row["child_count"] == 2
    assert len(row["children"]) == row["child_count"]


def test_writer_rejects_track_without_ipod_relative_path() -> None:
    with pytest.raises(ValueError, match="iPod location"):
        write_mhbd([TrackRecord(title="No Path", location="")])


def test_podcast_feed_url_mhod_is_bounded_to_utf8_cap() -> None:
    chunk = write_mhod_podcast_url(
        MHOD_TYPE_PODCAST_RSS_URL,
        "https://feed.example.org/show/" + "q" * 20_000,
    )

    assert len(chunk) == 24 + MHOD_URL_MAX_UTF8_BYTES
    assert len(chunk[24:]) == MHOD_URL_MAX_UTF8_BYTES


def test_playlist_item_metadata_follows_reassigned_track_ids() -> None:
    tracks = [
        TrackRecord("First", ":iPod_Control:Music:F00:ONE.mp3", db_track_id=9001),
        TrackRecord("Second", ":iPod_Control:Music:F00:TWO.mp3", db_track_id=9002),
    ]
    playlist = PlaylistRecord(
        name="",
        track_ids=[9001, 9002],
        playlist_description="",
        item_metadata=[EntryMeta(group_id=777)],
    )

    db = read_itdb(BytesIO(write_mhbd(tracks, playlists_type2=[playlist])))
    entries = split_datasets(db)["mhlp"]
    assert len(entries) == 2

    user_playlist = entries[1]
    strings = collect_strings(user_playlist["mhod_children"])
    assert strings["title"] == "Playlist"
    assert len(user_playlist["mhip_children"]) == 2


# ────────────────────────────────────────────────────────────────
# HASHAB signing (iPod nano 6G/7G)
# ────────────────────────────────────────────────────────────────


class TestHashabSigning:
    def test_signature_is_deterministic_and_input_sensitive(self) -> None:
        from podsync.itdb.writer.signing.ab import HASHAB_SIZE, compute_hashab

        baseline = compute_hashab(bytes(20), bytes(8))
        assert len(baseline) == HASHAB_SIZE
        assert compute_hashab(bytes(20), bytes(8)) == baseline
        assert compute_hashab(b"\x01" * 20, bytes(8)) != baseline
        assert compute_hashab(bytes(20), b"\x02" * 8) != baseline

    def test_write_hashab_sets_scheme_and_preserves_masked_fields(self) -> None:
        from podsync.itdb.spec.layouts.header import MHBD_OFFSET_DB_ID, MHBD_OFFSET_HASHAB, MHBD_OFFSET_HASHING_SCHEME
        from podsync.itdb.writer.signing.ab import HASHAB_SIZE, ITDB_CHECKSUM_HASHAB, write_hashab

        image = bytearray(300)
        image[0:4] = b"mhbd"
        image[MHBD_OFFSET_DB_ID:MHBD_OFFSET_DB_ID + 8] = b"deadbeef"

        write_hashab(image, bytes(range(8)))

        scheme = int.from_bytes(image[MHBD_OFFSET_HASHING_SCHEME:MHBD_OFFSET_HASHING_SCHEME + 2], "little")
        assert scheme == ITDB_CHECKSUM_HASHAB
        assert any(image[MHBD_OFFSET_HASHAB:MHBD_OFFSET_HASHAB + HASHAB_SIZE])
        assert image[MHBD_OFFSET_DB_ID:MHBD_OFFSET_DB_ID + 8] == b"deadbeef"

    def test_write_hashab_rejects_a_short_firewire_id(self) -> None:
        from podsync.itdb.writer.signing.ab import write_hashab

        image = bytearray(300)
        image[0:4] = b"mhbd"
        with pytest.raises(ValueError, match="FireWire ID"):
            write_hashab(image, b"\x00\x01")

    def test_hashab_device_signs_through_the_installer(self, tmp_path) -> None:
        """A device whose capabilities call for HASHAB gets a real signature, not a refusal."""
        from podsync.hardware.catalog.checksum import SignatureKind
        from podsync.itdb.writer.database import write_itdb

        from podsync.hardware import make_virtual_ipod

        caps = traits_for_model("iPod Nano", "6th Gen")
        assert caps is not None and caps.checksum == SignatureKind.HASHAB

        make_virtual_ipod(tmp_path, "MC525")
        assert write_itdb(
            str(tmp_path), [], capabilities=caps, firewire_id=bytes(range(8)),
        ) is True

        # nano 6G/7G capabilities are flagged SQLite-era, so the classic-compatible
        # companion database the installer signs is the compressed "iTunesCDB".
        db_bytes = (tmp_path / "iPod_Control" / "iTunes" / "iTunesCDB").read_bytes()
        from podsync.itdb.spec.layouts.header import MHBD_OFFSET_HASHAB, MHBD_OFFSET_HASHING_SCHEME
        from podsync.itdb.writer.signing.ab import HASHAB_SIZE, ITDB_CHECKSUM_HASHAB

        scheme = int.from_bytes(db_bytes[MHBD_OFFSET_HASHING_SCHEME:MHBD_OFFSET_HASHING_SCHEME + 2], "little")
        assert scheme == ITDB_CHECKSUM_HASHAB
        assert any(db_bytes[MHBD_OFFSET_HASHAB:MHBD_OFFSET_HASHAB + HASHAB_SIZE])
