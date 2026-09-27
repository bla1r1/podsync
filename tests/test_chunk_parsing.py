"""Chunk-level parsing, forensic byte walks, and device-delta bookkeeping.

The cases here exercise the binary engine where it meets raw bytes:

  * raw-chunk preservation during forensic parses (headers, bodies, trailers)
  * the byte-walk JSON export plus its lazy index/outline/cache readers
  * MHSD dataset decoding, including the opaque Genius payload
  * the simple list-container builders
  * artwork reference hydration from ArtworkDB song links
  * Play Counts merging and device-zone timestamp translation

Everything builds its fixtures in code and writes only under tmp_path.
"""

from __future__ import annotations

import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, tzinfo
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

from podsync.itdb.reader import read_itdb
from podsync.itdb.reader import byte_index
from podsync.itdb.reader import cover_links
from podsync.itdb.reader.primitives import preserve_raw_chunks
from podsync.itdb.reader.walker import parse_chunk
from podsync.itdb.reader.diagnose import (
    export_forensic_json,
    forensic_json_document,
    reconstruct_byte_walk,
)
from podsync.itdb.reader.chunks.records import parse_dataset
from podsync.itdb.reader.play_stats import PlayStatsEntry, apply_play_stats
from podsync.itdb.spec.clock import DeviceClock
from podsync.itdb.spec.flatten import (
    split_datasets,
    collect_strings,
)
from podsync.itdb.spec.fields import (
    CHUNK_HEADER_SIZE,
    MHLT_HEADER_SIZE,
    list_chunk_bytes,
    list_header_bytes,
)
from podsync.itdb.spec.layouts.dataset import MHSD_HEADER_SIZE
from podsync.itdb.writer.database import write_mhbd
from podsync.itdb.writer.playlist import PlaylistRecord

# Sample playlist titles and raw marker values used as fixture data below.
FORENSIC_TITLE = "Rhythm Atlas"
FORENSIC_MARKER = 44  # raw u16 parked at MHYP +0x52 on every matching row
WALK_TITLE = "Neon Harbor"
WALK_MARKER = 42
SPANNING_TITLE = "Field Guide"


def _stamped_database(title: str, marker: int) -> bytes:
    """Build a database whose title-bearing playlist rows carry a raw marker.

    Every mirrored copy of *title* lives inside its own ``mhyp`` chunk, so the
    nearest preceding ``mhyp`` signature identifies the header to stamp at
    offset +0x52.
    """
    blob = bytearray(write_mhbd([], playlists_type2=[PlaylistRecord(name=title)]))
    needle = title.encode("utf-16-le")
    cursor = 0
    while (hit := blob.find(needle, cursor)) >= 0:
        header_start = blob.rfind(b"mhyp", 0, hit)
        assert header_start >= 0
        struct.pack_into("<H", blob, header_start + 0x52, marker)
        cursor = hit + len(needle)
    return bytes(blob)


def _playlists_titled(tree: dict, title: str) -> list[dict]:
    """Collect ``mhyp`` wrapper nodes whose first MHOD string equals *title*."""
    matches: list[dict] = []

    def walk(wrapper: dict) -> None:
        payload = wrapper.get("data")
        if wrapper.get("chunk_type") == "mhyp" and isinstance(payload, dict):
            strings = collect_strings(payload.get("mhod_children", []))
            if strings.get("title") == title:
                matches.append(wrapper)
        if isinstance(payload, list):
            descendants = list(payload)
        elif isinstance(payload, dict):
            descendants = [
                *(payload.get("children") or []),
                *(payload.get("mhod_children") or []),
                *(payload.get("mhip_children") or []),
            ]
        else:
            descendants = []
        for child in descendants:
            walk(child)

    for top_level in tree.get("children", []):
        walk(top_level)
    return matches


def _raw_byte_ranges(database: dict) -> list[tuple[int, int]]:
    """Flatten every raw header span plus each unparsed body/trailer span."""
    spans: list[tuple[int, int]] = []

    def record(raw: dict) -> None:
        start = raw["offset"]
        spans.append((start, start + len(raw["raw_header"])))
        if raw["unparsed_bytes"]:
            end = raw["end_offset"]
            spans.append((end - len(raw["unparsed_bytes"]), end))

    def walk(wrapper: dict) -> None:
        record(wrapper["_raw_chunk"])
        payload = wrapper.get("data")
        if isinstance(payload, list):
            descendants = list(payload)
        elif isinstance(payload, dict):
            descendants = [
                *(payload.get("children") or []),
                *(payload.get("mhod_children") or []),
                *(payload.get("mhip_children") or []),
            ]
        else:
            descendants = []
        for child in descendants:
            walk(child)

    record(database["_raw_chunk"])
    for top_level in database.get("children", []):
        walk(top_level)
    return spans


def _descendant_chunks(node: dict):
    """Yield *node* and every chunk nested beneath its byte entries."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for entry in current["bytes"]:
            nested = entry.get("chunk")
            if isinstance(nested, dict):
                stack.append(nested)


def _byte_entry(chunk: dict, offset: str) -> dict:
    return next(entry for entry in chunk["bytes"] if entry["at"] == offset)


# ────────────────────────────────────────────────────────────────────
# Forensic raw-chunk preservation
# ────────────────────────────────────────────────────────────────────


class TestForensicRawChunkPreservation:
    def test_plain_parse_stays_light_while_raw_parse_keeps_stamp_and_title(self) -> None:
        """Raw capture is opt-in; when on it retains the stamped header word
        and the undecoded string-MHOD body that ends with the title text."""
        raw = _stamped_database(FORENSIC_TITLE, FORENSIC_MARKER)

        ordinary = read_itdb(BytesIO(raw))
        assert "_raw_chunk" not in ordinary

        captured = read_itdb(BytesIO(raw), preserve_raw=True)
        playlists = _playlists_titled(captured, FORENSIC_TITLE)
        assert playlists
        expected_word = struct.pack("<H", FORENSIC_MARKER)
        title_bytes = FORENSIC_TITLE.encode("utf-16-le")
        for wrapper in playlists:
            assert wrapper["_raw_chunk"]["raw_header"][0x52:0x54] == expected_word
            title_mhod = wrapper["data"]["mhod_children"][0]
            assert title_mhod["_raw_chunk"]["unparsed_bytes"].endswith(title_bytes)

    def test_exported_document_rebuilds_source_and_labels_marker_bytes(self, tmp_path) -> None:
        """The byte-walk export round-trips to the exact input and annotates
        the stamped marker and the unmapped gap beside it."""
        raw = _stamped_database(FORENSIC_TITLE, FORENSIC_MARKER)
        source = tmp_path / "iTunesDB"
        source.write_bytes(raw)

        document = forensic_json_document(source)

        # The format identifier is stamped with the engine's own package name.
        engine_name = read_itdb.__module__.split(".")[0]
        marker_bytes = struct.pack("<H", FORENSIC_MARKER)

        playlist_chunk = next(
            chunk
            for chunk in _descendant_chunks(document["file"])
            if chunk["chunk"] == "mhyp"
            and chunk["caption"] == f"Playlist: {FORENSIC_TITLE}"
        )
        marker = _byte_entry(playlist_chunk, "0x0052")
        gap = _byte_entry(playlist_chunk, "0x004C")
        title_mhod = next(
            chunk
            for chunk in _descendant_chunks(playlist_chunk)
            if chunk["chunk"] == "mhod"
            and any(entry.get("value") == FORENSIC_TITLE for entry in chunk["bytes"])
        )
        title_text = next(entry for entry in title_mhod["bytes"] if entry.get("field") == "text")

        assert document["format"] == f"{engine_name}-byte-walk/v1"
        assert document["source"]["byte_length"] == len(raw)
        assert reconstruct_byte_walk(document) == raw
        assert marker["hex"] == marker_bytes.hex(" ")
        assert marker["field"] == "phase_game_flag"
        assert marker["value"] == FORENSIC_MARKER
        assert marker["status"] == "observed"
        assert gap == {
            "at": "0x004C",
            "byte_length": 4,
            "field": "unk0x4C",
            "value": 0,
            "hex": "00 00 00 00",
            "status": "observed",
        }
        assert title_text["hex"] == FORENSIC_TITLE.encode("utf-16-le").hex(" ")
        assert title_text["value"] == FORENSIC_TITLE

    def test_writer_flag_round_trips_into_every_mirrored_playlist_row(self) -> None:
        """A phase_game_flag supplied to the writer must surface in the parsed
        rows and in the preserved raw header of each mirrored copy."""
        raw = write_mhbd(
            [],
            playlists_type2=[
                PlaylistRecord(name=FORENSIC_TITLE, phase_game_flag=FORENSIC_MARKER)
            ],
        )

        captured = read_itdb(BytesIO(raw), preserve_raw=True)
        playlists = _playlists_titled(captured, FORENSIC_TITLE)

        assert len(playlists) == 2
        expected_word = struct.pack("<H", FORENSIC_MARKER)
        for wrapper in playlists:
            assert wrapper["data"]["phase_game_flag"] == FORENSIC_MARKER
            assert wrapper["_raw_chunk"]["raw_header"][0x52:0x54] == expected_word

    def test_unrecognized_mhod_body_is_stored_verbatim_as_unparsed_bytes(self) -> None:
        """An MHOD whose type number the engine does not know still keeps its
        header and its whole payload without interpretation."""
        body = b"\x7E\x01snipe\xC3"
        chunk = bytearray(24 + len(body))
        struct.pack_into("<4sII", chunk, 0, b"mhod", 24, len(chunk))
        struct.pack_into("<I", chunk, 12, 0xD00D)
        chunk[24:] = body

        with preserve_raw_chunks(True):
            parsed, chunk_type = parse_chunk(chunk, 0)

        assert chunk_type == "mhod"
        assert parsed["data"]["mhod_type"] == 0xD00D
        assert parsed["_raw_chunk"]["raw_header"] == bytes(chunk[:24])
        assert parsed["_raw_chunk"]["unparsed_bytes"] == body

    def test_extended_album_header_decodes_every_declared_field(self) -> None:
        """All fields declared for an 88-byte ``mhia`` header decode at their
        documented offsets; unset ones stay at their defaults."""
        album = bytearray(0x58)
        struct.pack_into("<4sII", album, 0, b"mhia", 0x58, 0x58)
        struct.pack_into("<I", album, 0x0C, 0)  # child count
        struct.pack_into("<I", album, 0x10, 77)  # album_id
        struct.pack_into("<Q", album, 0x14, 0x2468)  # sql_id
        struct.pack_into("<H", album, 0x1C, 3)  # platform_flag
        struct.pack_into("<Q", album, 0x20, 0x6A3C)  # representative track db id
        album[0x28] = 65  # album rating
        album[0x29] = 0x20  # rating companion flag observed in real samples
        struct.pack_into("<I", album, 0x2C, 5)  # season number

        with preserve_raw_chunks(True):
            parsed, chunk_type = parse_chunk(album, 0)

        assert chunk_type == "mhia"
        assert parsed["data"] == {
            "child_count": 0,
            "album_id": 77,
            "sql_id": 0x2468,
            "platform_flag": 3,
            "album_compilation_flag": 0,
            "album_track_db_id": 0x6A3C,
            "album_rating": 65,
            "unk0x29_rating_flag": 0x20,
            "season_number": 5,
            "children": [],
        }

    def test_type9_dataset_trailer_is_captured_as_payload_not_child(self) -> None:
        """A type-9 (Genius) dataset treats everything after its header as one
        opaque payload: no children are parsed from it."""
        payload = b"c41d7b90e2af3856170b9d3e5ca64f21"
        chunk = bytearray(96 + len(payload))
        struct.pack_into("<4sII", chunk, 0, b"mhsd", 96, len(chunk))
        struct.pack_into("<I", chunk, 0x0C, 9)
        chunk[96:] = payload

        with preserve_raw_chunks(True):
            parsed, chunk_type = parse_chunk(chunk, 0)

        assert chunk_type == "mhsd"
        assert parsed["data"]["raw_payload"] == payload
        assert parsed["_raw_chunk"]["unparsed_bytes"] == payload

    def test_raw_header_and_trailer_spans_tile_the_entire_file(self) -> None:
        """Raw headers plus unparsed bodies cover every byte of a written
        database exactly once — no hole, no double-counted child region."""
        raw = write_mhbd([], playlists_type2=[PlaylistRecord(name=SPANNING_TITLE)])
        captured = read_itdb(BytesIO(raw), preserve_raw=True)

        merged: list[tuple[int, int]] = []
        for start, end in sorted(_raw_byte_ranges(captured)):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        assert merged == [(0, len(raw))]

    def test_bytes_after_a_containers_declared_children_are_retained(self) -> None:
        """Leftover trailer bytes past an ``mhyp`` header survive untouched."""
        trailer = b"\x11\x22\x33\x44\x55"
        playlist = bytearray(MHYP_HEADER_BYTES + len(trailer))
        struct.pack_into("<4sII", playlist, 0, b"mhyp", MHYP_HEADER_BYTES, len(playlist))
        playlist[MHYP_HEADER_BYTES:] = trailer

        with preserve_raw_chunks(True):
            parsed, chunk_type = parse_chunk(playlist, 0)

        assert chunk_type == "mhyp"
        assert parsed["_raw_chunk"]["unparsed_bytes"] == trailer


MHYP_HEADER_BYTES = 184


# ────────────────────────────────────────────────────────────────────
# Byte-walk JSON: lazy indexing, outlines, and caching
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def walk_export(tmp_path: Path) -> Path:
    """Export a stamped sample database as an on-disk byte-walk document."""
    raw = _stamped_database(WALK_TITLE, WALK_MARKER)
    source = tmp_path / "iTunesDB"
    source.write_bytes(raw)
    output = tmp_path / "harbor.byte-walk.json"
    export_forensic_json(source, output)
    return output


def _walk_playlist_entry(output: Path) -> byte_index.ByteIndexEntry:
    entries = byte_index.index_byte_walk_json(output)
    return next(entry for entry in entries if entry.caption == f"Playlist: {WALK_TITLE}")


class TestByteWalkLazyReaders:
    def test_index_then_load_fetches_only_the_selected_playlist(self, walk_export: Path) -> None:
        """The index reads just enough metadata to open one chunk on demand,
        including the stamped marker word inside it."""
        entries = byte_index.index_byte_walk_json(walk_export)
        playlist = next(entry for entry in entries if entry.caption == f"Playlist: {WALK_TITLE}")
        chunk = byte_index.load_indexed_chunk(walk_export, playlist)
        marker = next(entry for entry in chunk["bytes"] if entry.get("field") == "phase_game_flag")

        assert len(entries) > 1
        assert chunk["caption"] == f"Playlist: {WALK_TITLE}"
        assert marker["hex"] == struct.pack("<H", WALK_MARKER).hex(" ")
        assert marker["value"] == WALK_MARKER

    def test_root_outline_substitutes_child_refs_without_full_deserialization(
        self,
        walk_export: Path,
        monkeypatch,
    ) -> None:
        """Opening the root reads only its leading spans plus pre-built child
        references; no nested chunk object is ever handed to the JSON decoder."""
        entries = byte_index.index_byte_walk_json(walk_export)
        hierarchy = byte_index.build_chunk_hierarchy(entries)
        root = next(entry for entry in entries if entry.chunk_type == "mhbd")
        decoded_sizes: list[int] = []
        original_loads = byte_index.json.loads

        def spy_loads(payload, *args, **kwargs):
            decoded_sizes.append(len(payload))
            return original_loads(payload, *args, **kwargs)

        monkeypatch.setattr(byte_index.json, "loads", spy_loads)

        outline = byte_index.load_indexed_chunk_outline(
            walk_export, root, hierarchy[root.json_offset],
        )

        nested = [entry for entry in outline["bytes"] if isinstance(entry.get("chunk"), dict)]
        assert len(nested) == len(hierarchy[root.json_offset])
        assert all("bytes" not in entry["chunk"] for entry in nested)
        assert max(decoded_sizes) < 2_048

    def test_outline_cache_rehands_the_same_root_object_on_repeat_calls(
        self,
        walk_export: Path,
    ) -> None:
        entries = byte_index.index_byte_walk_json(walk_export)
        hierarchy = byte_index.build_chunk_hierarchy(entries)
        root = next(entry for entry in entries if entry.chunk_type == "mhbd")
        cache = byte_index.ByteIndexCache()

        first = cache.load_outline(walk_export, root, hierarchy[root.json_offset])
        second = cache.load_outline(walk_export, root, hierarchy[root.json_offset])

        assert first.was_cached is False
        assert second.was_cached is True
        assert first.chunk is second.chunk
        assert all(
            "bytes" not in entry["chunk"]
            for entry in first.chunk["bytes"]
            if isinstance(entry.get("chunk"), dict)
        )

    def test_full_chunk_cache_touches_disk_only_once(self, walk_export: Path, monkeypatch) -> None:
        playlist = _walk_playlist_entry(walk_export)
        cache = byte_index.ByteIndexCache()
        loads = 0
        underlying_loader = byte_index.load_indexed_chunk

        def counted_load(path, entry):
            nonlocal loads
            loads += 1
            return underlying_loader(path, entry)

        monkeypatch.setattr(byte_index, "load_indexed_chunk", counted_load)

        first = cache.load(walk_export, playlist)
        second = cache.load(walk_export, playlist)

        assert first.chunk == second.chunk
        assert first.was_cached is False
        assert second.was_cached is True
        assert loads == 1

    def test_racing_misses_collapse_into_one_disk_read(self, walk_export: Path, monkeypatch) -> None:
        playlist = _walk_playlist_entry(walk_export)
        cache = byte_index.ByteIndexCache()
        loader_started = Event()
        loader_may_finish = Event()
        go_together = Barrier(2)
        count_lock = Lock()
        loads = 0
        underlying_loader = byte_index.load_indexed_chunk

        def paused_load(path, entry):
            nonlocal loads
            with count_lock:
                loads += 1
            loader_started.set()
            assert loader_may_finish.wait(timeout=5)
            return underlying_loader(path, entry)

        def racing_load():
            go_together.wait(timeout=5)
            return cache.load(walk_export, playlist)

        monkeypatch.setattr(byte_index, "load_indexed_chunk", paused_load)
        with ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(racing_load)
            right = pool.submit(racing_load)
            assert loader_started.wait(timeout=5)
            loader_may_finish.set()
            first = left.result(timeout=5)
            second = right.result(timeout=5)

        assert first.chunk == second.chunk
        assert {first.was_cached, second.was_cached} == {False, True}
        assert loads == 1

    def test_clearing_mid_load_keeps_the_stale_result_out_of_cache(
        self,
        walk_export: Path,
        monkeypatch,
    ) -> None:
        playlist = _walk_playlist_entry(walk_export)
        cache = byte_index.ByteIndexCache()
        loader_started = Event()
        loader_may_finish = Event()
        loads = 0
        underlying_loader = byte_index.load_indexed_chunk

        def paused_load(path, entry):
            nonlocal loads
            loads += 1
            loader_started.set()
            assert loader_may_finish.wait(timeout=5)
            return underlying_loader(path, entry)

        monkeypatch.setattr(byte_index, "load_indexed_chunk", paused_load)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(cache.load, walk_export, playlist)
            assert loader_started.wait(timeout=5)
            cache.clear()
            loader_may_finish.set()
            assert pending.result(timeout=5).was_cached is False

        after_clear = cache.load(walk_export, playlist)

        assert after_clear.was_cached is False
        assert loads == 2

    def test_hexdump_interpretation_tolerates_punctuation_and_reports_widths(self) -> None:
        readings = byte_index.hex_interpretations("0x2A:00")

        assert readings["Byte count"] == "2"
        assert readings["Unsigned 16-bit LE"] == str(WALK_MARKER)
        assert readings["Unsigned 16-bit BE"] == str(int.from_bytes(bytes([WALK_MARKER, 0]), "big"))
        assert "UTF-16 LE" in readings

    def test_hexdump_interpretation_refuses_an_odd_digit_string(self) -> None:
        with pytest.raises(ValueError, match="odd number"):
            byte_index.hex_interpretations("1A2")


# ────────────────────────────────────────────────────────────────────
# MHSD datasets and simple list containers
# ────────────────────────────────────────────────────────────────────

GENIUS_CUID = "5d9f1c0a7e3b8426fa1c9d5e0b374826"
GENIUS_CUID_HEX = "3564396631633061376533623834323666613163396435653062333734383236"


class TestDatasetContainers:
    def test_type9_dataset_keeps_cuid_in_payload_with_no_children(self) -> None:
        cuid = GENIUS_CUID.encode("ascii")
        chunk = bytearray(MHSD_HEADER_SIZE + len(cuid))
        struct.pack_into("<4sII", chunk, 0, b"mhsd", MHSD_HEADER_SIZE, len(chunk))
        struct.pack_into("<I", chunk, 0x0C, 9)
        chunk[MHSD_HEADER_SIZE:] = cuid

        parsed = parse_dataset(chunk, 0, MHSD_HEADER_SIZE, len(chunk))["data"]

        assert parsed["children"] == []
        assert parsed["raw_payload"] == cuid
        assert parsed["genius_cuid"] == cuid.decode("ascii")

    def test_dataset_extraction_surfaces_type9_as_hex_and_text(self) -> None:
        raw = {
            "children": [
                {
                    "data": {
                        "dataset_type": 9,
                        "raw_payload": GENIUS_CUID.encode("ascii"),
                        "genius_cuid": GENIUS_CUID,
                        "children": [],
                    }
                }
            ]
        }

        assert split_datasets(raw)["mhsd_type_9"] == {
            "raw_payload_hex": GENIUS_CUID_HEX,
            "genius_cuid": GENIUS_CUID,
        }

    def test_list_header_carries_child_count_and_zero_padding(self) -> None:
        header = list_header_bytes(b"mhlt", MHLT_HEADER_BYTES, 3)

        assert len(header) == MHLT_HEADER_BYTES
        assert struct.unpack_from("<4sII", header, 0) == (
            b"mhlt",
            MHLT_HEADER_BYTES,
            3,
        )
        assert header[CHUNK_HEADER_SIZE:] == b"\x00" * (
            MHLT_HEADER_BYTES - CHUNK_HEADER_SIZE
        )

    def test_list_chunk_counts_children_and_concatenates_bodies(self) -> None:
        chunk = list_chunk_bytes(
            b"mhlp",
            MHLT_HEADER_BYTES,
            [b"alpha", b"beta", b"gamma"],
        )

        assert struct.unpack_from("<4sII", chunk, 0) == (
            b"mhlp",
            MHLT_HEADER_BYTES,
            3,
        )
        assert chunk[MHLT_HEADER_BYTES:] == b"alphabetagamma"


MHLT_HEADER_BYTES = MHLT_HEADER_SIZE


# ────────────────────────────────────────────────────────────────────
# Artwork reference hydration
# ────────────────────────────────────────────────────────────────────


class TestArtworkReferenceHydration:
    def test_absent_and_zero_refs_are_backfilled_from_song_links(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            cover_links,
            "_images_by_song",
            lambda _path: {707: 455, 808: 456},
        )
        tracks = [
            {"db_track_id": 707, "artwork_count": 1, "artwork_link": 0},
            {"db_track_id": 808, "artwork_count": 0},
            {"db_track_id": 909, "artwork_count": 0},
        ]

        updated = cover_links.attach_cover_refs(
            tracks,
            tmp_path / "iPod_Control" / "iTunes" / "iTunesDB",
        )

        assert updated == 2
        assert tracks[0]["artwork_link"] == 455
        assert tracks[0]["artwork_count"] == 1
        assert tracks[1]["artwork_link"] == 456
        assert tracks[1]["mhii_link"] == 456
        assert tracks[1]["artwork_count"] == 1
        assert "artwork_link" not in tracks[2]

    def test_ref_already_equal_to_the_song_link_is_left_alone(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            cover_links,
            "_images_by_song",
            lambda _path: {707: 455},
        )
        tracks = [{"db_track_id": 707, "artwork_count": 1, "artwork_link": 455}]

        updated = cover_links.attach_cover_refs(
            tracks,
            tmp_path / "iPod_Control" / "iTunes" / "iTunesDB",
        )

        assert updated == 0
        assert tracks[0]["artwork_link"] == 455

    def test_stale_ref_pointing_elsewhere_is_corrected_from_song_link(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            cover_links,
            "_images_by_song",
            lambda _path: {707: 455},
        )
        tracks = [{"db_track_id": 707, "artwork_count": 1, "artwork_link": 700}]

        updated = cover_links.attach_cover_refs(
            tracks,
            tmp_path / "iPod_Control" / "iTunes" / "iTunesDB",
        )

        assert updated == 1
        assert tracks[0]["artwork_link"] == 455
        assert tracks[0]["mhii_link"] == 455


# ────────────────────────────────────────────────────────────────────
# Play Counts deltas and device-clock translation
# ────────────────────────────────────────────────────────────────────


class _SummerDeviceZone(tzinfo):
    """Stand-in device zone: UTC+1 in winter, +2 across northern summer.

    A hand-rolled tzinfo keeps this test hermetic — the portable interpreter
    ships without an IANA time-zone database, so named zones are unavailable.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1) + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return timedelta(hours=1) if dt.month in {6, 7, 8} else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "TEST-Summer"


class TestPlayCountBookkeeping:
    def test_device_summer_time_rules_translate_play_and_skip_stamps(self) -> None:
        """Device wall-clock rules — including its DST shift — convert Mac
        timestamps; a plain fixed-offset context gives a different instant."""
        device_zone = _SummerDeviceZone()
        device_time = DeviceClock(
            device_zone, "TEST-Summer", "device_preferences",
        )
        summer_wall = datetime(2026, 7, 9, 16, 30)
        mac_seconds = int((summer_wall - datetime(1904, 1, 1)).total_seconds())
        entry = PlayStatsEntry(
            last_played_mac=mac_seconds,
            last_skipped_mac=mac_seconds,
        )

        expected = int(summer_wall.replace(tzinfo=device_zone).timestamp())
        fixed_expectation = int(
            summer_wall.replace(tzinfo=timezone(timedelta(hours=1))).timestamp(),
        )

        assert entry.last_played_as_unix(device_time) == expected
        assert entry.last_skipped_as_unix(device_time) == expected
        assert (
            entry.last_played_as_unix(DeviceClock.fixed_offset(3600))
            == fixed_expectation
        )
        assert expected != fixed_expectation

    def test_back_to_back_device_sessions_add_into_both_play_queues(self) -> None:
        rows = [{"play_count": 7, "pending_play_count": 5}]

        apply_play_stats(rows, [PlayStatsEntry(play_count=3)])
        apply_play_stats(rows, [PlayStatsEntry(play_count=6)])

        assert rows == [
            {
                "play_count": 16,
                "pending_play_count": 14,
                "recent_playcount": 6,
                "recent_skipcount": 0,
                "skip_count": 0,
            }
        ]

    def test_row_without_a_device_entry_keeps_its_pending_queue_count(self) -> None:
        rows = [
            {"play_count": 9, "pending_play_count": 2},
            {"play_count": 4, "pending_play_count": 6},
        ]

        apply_play_stats(rows, [PlayStatsEntry(play_count=5)])

        assert rows[0]["play_count"] == 14
        assert rows[0]["pending_play_count"] == 7
        assert rows[1]["play_count"] == 4
        assert rows[1]["pending_play_count"] == 6
        assert rows[1]["recent_playcount"] == 0


# ────────────────────────────────────────────────────────────────────
# Reference accounting
# ────────────────────────────────────────────────────────────────────
