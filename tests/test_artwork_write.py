"""ArtworkDB/ithmb writer checks regrouped by feature.

Covers the per-track decision pipeline (preserve, clear, re-encode), the
rules that decide which numbered .ithmb shard receives new pixel payloads,
salvage behaviour when only part of an entry's formats survive, and
read-back validation of the rewritten index.  Every device tree fixture is
built under tmp_path; nothing here talks to a real player, the network, or
any GUI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from podsync.artwork.writer import pipeline as aw
from podsync.artwork.writer.covers import extract_art
from podsync.hardware.safety.guard import UnsafeWriteError

# CORE/AUX are globally registered cover-art ids; FOREIGN deliberately sits
# outside every registry table so it must be treated as passthrough-only.
CORE_FMT = 1061
AUX_FMT = 1068
FOREIGN_FMT = 4242
VIDEO_MIN_FMT = 1028
VIDEO_MAX_FMT = 1029

# Stubbed frames written by the mocked encoder are this many bytes long.
FRAME_BYTES = 4

# Arbitrary source-image sizes reported by fixture entries.
SRC_SIZE = 77
SECOND_SRC_SIZE = 66

# Extracted artwork bytes for the "everything gets re-encoded" case.
COVER_BYTES = b"fresh-cover"


def _artwork_root(tmp_path: Path) -> tuple[Path, Path]:
    """Build `<root>/iPod_Control/Artwork` and return (root, artwork dir)."""
    root = tmp_path / "device"
    artwork_dir = root / "iPod_Control" / "Artwork"
    artwork_dir.mkdir(parents=True)
    return root, artwork_dir


def _track(db_track_id: int, *, hint: str = "") -> SimpleNamespace:
    """Minimal track record; `hint` injects the per-track artwork directive."""
    return SimpleNamespace(
        db_track_id=db_track_id,
        title=f"Tune {db_track_id}",
        album="Platters",
        artist="Makers",
        album_artist="Makers",
        mhii_link=0,
        artwork_count=0,
        artwork_size=0,
        _iop_artwork_sync_hint=hint,
    )


def _frame_ref(
    ithmb_path: Path,
    *,
    offset: int = 0,
    size: int = FRAME_BYTES,
) -> aw.ExistingFormatRef:
    return aw.ExistingFormatRef(
        path=str(ithmb_path),
        ithmb_offset=offset,
        size=size,
        width=1,
        height=1,
        hpad=0,
        vpad=0,
    )


def _solo_entry(
    ithmb_path: Path,
    *,
    song_id: int = 1,
    format_id: int = CORE_FMT,
    img_id: int = 33,
    src_size: int = SRC_SIZE,
    offset: int = 0,
) -> dict[int, dict]:
    """One on-device entry whose only frame lives in `ithmb_path`."""
    return {
        img_id: {
            "song_id": song_id,
            "src_img_size": src_size,
            "formats": {format_id: _frame_ref(ithmb_path, offset=offset)},
        },
    }


def _first_read_replaces(
    fixture: dict[int, dict],
    real_read,
):
    """Serve `fixture` for the first read, then defer to the real parser."""
    state = {"fixture_pending": True}

    def _reader(artworkdb_path: str, artwork_path: str):
        if state["fixture_pending"]:
            state["fixture_pending"] = False
            return fixture
        return real_read(artworkdb_path, artwork_path)

    return _reader


def _stub_image(*_args, **_kwargs) -> Image.Image:
    return Image.new("RGB", (1, 1), (11, 22, 33))


def _stub_encode(_img, _fmt_id, *_args, **_kwargs) -> aw.EncodedFormatPayload:
    return aw.EncodedFormatPayload(
        data=b"NEXT",
        width=1,
        height=1,
        size=FRAME_BYTES,
        stride_pixels=1,
    )


# ─────────────────────────────────────────────────────────────────────────
# Artwork source bytes
# ─────────────────────────────────────────────────────────────────────────


def test_extractor_returns_png_bytes_verbatim(tmp_path: Path) -> None:
    """A plain image file is its own payload — bytes must come back untouched."""
    picture = tmp_path / "sleeve.png"
    Image.new("RGB", (6, 5), (91, 17, 203)).save(picture)

    assert extract_art(str(picture)) == picture.read_bytes()


# ─────────────────────────────────────────────────────────────────────────
# Deferred commit protocol
# ─────────────────────────────────────────────────────────────────────────


def test_pending_write_revalidates_before_every_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Each staged rename is bracketed by a fresh device revalidation."""
    events: list[str] = []
    staged: list[tuple[str, str]] = []
    for index in range(3):
        temp = tmp_path / f"stage-{index}.tmp"
        final = tmp_path / f"blob-{index}.ithmb"
        temp.write_bytes(bytes([index + 10]))
        staged.append((str(temp), str(final)))

    real_swap = aw.safe_replace

    def _swap(source, target) -> None:
        events.append("swap")
        real_swap(source, target)

    monkeypatch.setattr(aw, "safe_replace", _swap)

    pending = aw.PendingArtworkWrite(
        db_track_id_to_art_info={},
        _pending_renames=staged,
    )
    pending.commit(before_replace=lambda: events.append("check"))

    assert events == ["check", "swap"] * 3


# ─────────────────────────────────────────────────────────────────────────
# Classifying what the device already has
# ─────────────────────────────────────────────────────────────────────────


def test_entry_formats_split_into_core_aux_and_foreign_buckets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    core_ithmb = tmp_path / f"F{CORE_FMT}_1.ithmb"
    aux_ithmb = tmp_path / f"F{AUX_FMT}_1.ithmb"
    foreign_ithmb = tmp_path / f"F{FOREIGN_FMT}_1.ithmb"
    core_ithmb.write_bytes(b"OWND")
    aux_ithmb.write_bytes(b"AUXD")
    foreign_ithmb.write_bytes(b"ODDD")

    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    grouped = aw._classify_existing_entry_formats(
        {
            "formats": {
                CORE_FMT: _frame_ref(core_ithmb),
                AUX_FMT: _frame_ref(aux_ithmb),
                FOREIGN_FMT: _frame_ref(foreign_ithmb),
            },
        },
        [CORE_FMT],
        {},
    )

    assert set(grouped.required_known) == {CORE_FMT}
    assert set(grouped.extra_known) == {AUX_FMT}
    assert set(grouped.unknown_passthrough) == {FOREIGN_FMT}
    assert grouped.known_present == {CORE_FMT, AUX_FMT}
    assert isinstance(
        grouped.unknown_passthrough[FOREIGN_FMT],
        aw.PassthroughFormatRef,
    )


def test_registry_padded_stride_keeps_entry_classified_as_known(
    tmp_path: Path,
) -> None:
    fmt_id = 1016
    visible = 57
    stride = 58
    payload_size = stride * visible * 2
    ithmb_path = tmp_path / f"F{fmt_id}_1.ithmb"
    ithmb_path.write_bytes(b"\0" * payload_size)
    override = aw.ArtworkFormat(
        fmt_id,
        visible,
        visible,
        stride * 2,
        "RGB565_LE",
        "cover_small_alt",
        "Padded cover sample",
    )

    grouped = aw._classify_existing_entry_formats(
        {
            "formats": {
                fmt_id: aw.ExistingFormatRef(
                    path=str(ithmb_path),
                    ithmb_offset=0,
                    size=payload_size,
                    width=visible,
                    height=visible,
                    hpad=0,
                    vpad=0,
                ),
            },
        },
        [fmt_id],
        {fmt_id: override},
    )

    assert set(grouped.required_known) == {fmt_id}
    assert grouped.known_present == {fmt_id}


def test_known_format_with_odd_payload_size_is_carried_forward(
    tmp_path: Path,
    caplog,
) -> None:
    aux_ithmb = tmp_path / f"F{AUX_FMT}_1.ithmb"
    aux_ithmb.write_bytes(b"ODDB")

    with caplog.at_level(logging.DEBUG):
        grouped = aw._classify_existing_entry_formats(
            {"formats": {AUX_FMT: _frame_ref(aux_ithmb)}},
            [CORE_FMT],
            {},
        )

    assert set(grouped.extra_known) == {AUX_FMT}
    assert grouped.known_present == {AUX_FMT}
    assert "carrying forward on-device bytes" in caplog.text


def test_preserved_payload_loading_tolerates_shared_frames(
    tmp_path: Path,
) -> None:
    ithmb_path = tmp_path / f"F{CORE_FMT}_1.ithmb"
    ithmb_path.write_bytes(b"BYTE")
    entry = {
        "src_img_size": SRC_SIZE,
        "formats": {CORE_FMT: _frame_ref(ithmb_path)},
    }
    first_ref = aw.ArtworkAssetRef("preserve", 11)
    second_ref = aw.ArtworkAssetRef("preserve", 22)
    decisions = {
        1: aw.TrackArtworkDecision(
            db_track_id=1,
            kind=aw.ArtworkDecisionKind.PRESERVE_FALLBACK,
            asset_ref=first_ref,
            existing_entry=entry,
        ),
        2: aw.TrackArtworkDecision(
            db_track_id=2,
            kind=aw.ArtworkDecisionKind.PRESERVE_FALLBACK,
            asset_ref=second_ref,
            existing_entry=entry,
        ),
    }

    payloads, _salvaged, dropped = aw._load_preserved_art_payloads(
        decisions,
        [CORE_FMT],
        {
            first_ref: [CORE_FMT],
            second_ref: [CORE_FMT],
        },
        {},
        {CORE_FMT: (1, 1)},
        {},
    )

    assert dropped == 0
    assert set(payloads) == {first_ref, second_ref}
    assert payloads[first_ref].formats[CORE_FMT].size == FRAME_BYTES
    assert payloads[second_ref].formats[CORE_FMT].size == FRAME_BYTES


# ─────────────────────────────────────────────────────────────────────────
# Picking rewrite targets
# ─────────────────────────────────────────────────────────────────────────


def test_rewrite_targets_keep_known_ids_and_pass_foreign_ids_through(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    core_ithmb = tmp_path / f"F{CORE_FMT}_1.ithmb"
    aux_ithmb = tmp_path / f"F{AUX_FMT}_1.ithmb"
    foreign_ithmb = tmp_path / f"F{FOREIGN_FMT}_1.ithmb"
    core_ithmb.write_bytes(b"OWND")
    aux_ithmb.write_bytes(b"AUXD")
    foreign_ithmb.write_bytes(b"ODDD")
    asset_ref = aw.ArtworkAssetRef("preserve", 44)

    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    with caplog.at_level(logging.WARNING):
        targets, passthrough = aw._collect_rewrite_targets(
            {
                1: aw.TrackArtworkDecision(
                    db_track_id=1,
                    kind=aw.ArtworkDecisionKind.PRESERVE_FALLBACK,
                    asset_ref=asset_ref,
                    existing_entry={
                        "formats": {
                            CORE_FMT: _frame_ref(core_ithmb),
                            # A stale size on one ref must not demote a
                            # recognized id out of the rewrite set.
                            AUX_FMT: _frame_ref(aux_ithmb, size=2),
                            FOREIGN_FMT: _frame_ref(foreign_ithmb),
                        },
                    },
                ),
            },
            [CORE_FMT],
            {},
        )

    assert targets[asset_ref] == [CORE_FMT, AUX_FMT]
    assert set(passthrough[asset_ref]) == {FOREIGN_FMT}
    assert passthrough[asset_ref][FOREIGN_FMT].path == str(foreign_ithmb)
    assert f"unknown artwork format {FOREIGN_FMT} at {foreign_ithmb}" in caplog.text
    assert f"extra known artwork format {AUX_FMT} at {aux_ithmb}" in caplog.text


def test_clear_decision_never_targets_rewrites_but_still_warns(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    foreign_ithmb = tmp_path / f"F{FOREIGN_FMT}_1.ithmb"
    foreign_ithmb.write_bytes(b"ODDD")

    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    with caplog.at_level(logging.WARNING):
        targets, passthrough = aw._collect_rewrite_targets(
            {
                1: aw.TrackArtworkDecision(
                    db_track_id=1,
                    kind=aw.ArtworkDecisionKind.CLEAR_ART,
                    existing_entry={
                        "formats": {FOREIGN_FMT: _frame_ref(foreign_ithmb)},
                    },
                ),
            },
            [CORE_FMT],
            {},
        )

    assert targets == {}
    assert passthrough == {}
    assert f"unknown artwork format {FOREIGN_FMT} at {foreign_ithmb}" in caplog.text


# ─────────────────────────────────────────────────────────────────────────
# Preserve-only passes
# ─────────────────────────────────────────────────────────────────────────


def test_preserve_fast_path_skips_reencoding_and_keeps_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    pc_file = tmp_path / "clip.mp3"
    pc_file.write_bytes(b"AUDIO")
    existing_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    existing_ithmb.write_bytes(b"KEEP")

    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args, **_kwargs: _solo_entry(existing_ithmb))
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    def _forbidden_extract(_path: str) -> bytes | None:
        raise AssertionError("the preserve fast path must not decode anything")

    monkeypatch.setattr(aw, "extract_art_with_folder", _forbidden_extract)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1, hint="preserve_existing")],
        pc_file_paths={1: str(pc_file)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert result[1] == (100, SRC_SIZE)
    assert existing_ithmb.read_bytes() == b"KEEP"


def test_preserve_only_pass_swaps_index_but_not_image_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    existing_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    existing_ithmb.write_bytes(b"KEEP")
    progress: list[str] = []
    replaced: list[str] = []
    real_replace = aw.os.replace

    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args, **_kwargs: _solo_entry(existing_ithmb))
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    def _record_replace(src: str, dst: str) -> None:
        replaced.append(Path(dst).name)
        real_replace(src, dst)

    monkeypatch.setattr(aw.os, "replace", _record_replace)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1, hint="preserve_existing")],
        pc_file_paths={},
        artwork_formats={CORE_FMT: (1, 1)},
        progress_callback=progress.append,
    )

    assert result[1] == (100, SRC_SIZE)
    assert existing_ithmb.read_bytes() == b"KEEP"
    assert f"F{CORE_FMT}_1.ithmb" not in replaced
    assert replaced == ["ArtworkDB"]
    assert any("no image data rewritten" in message for message in progress)
    assert not any("writing 1 image" in message for message in progress)


def test_preserved_entries_relinked_to_fresh_image_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    existing_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    existing_ithmb.write_bytes(b"FRSTSCND")
    real_read = aw.read_existing_artwork
    fixture = {
        44: {
            "song_id": 1,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(existing_ithmb, offset=0)},
        },
        45: {
            "song_id": 2,
            "src_img_size": SECOND_SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(existing_ithmb, offset=4)},
        },
    }

    monkeypatch.setattr(aw, "read_existing_artwork", _first_read_replaces(fixture, real_read))
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    result = aw.write_artworkdb(
        str(ipod_root),
        [
            _track(1, hint="preserve_existing"),
            _track(2, hint="preserve_existing"),
        ],
        pc_file_paths={},
        artwork_formats={CORE_FMT: (1, 1)},
        start_img_id=900,
    )

    assert result == {
        1: (900, SRC_SIZE),
        2: (901, SECOND_SRC_SIZE),
    }
    assert existing_ithmb.read_bytes() == b"FRSTSCND"

    rewritten = real_read(str(artwork_dir / "ArtworkDB"), str(artwork_dir))
    assert set(rewritten) == {900, 901}
    assert {entry["song_id"]: img_id for img_id, entry in rewritten.items()} == {
        1: 900,
        2: 901,
    }
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in rewritten.values()
    }
    assert refs_by_song_id[1].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[1].ithmb_offset == 0
    assert refs_by_song_id[2].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[2].ithmb_offset == 4


def test_missing_source_file_falls_back_to_existing_art(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    missing_pc_file = tmp_path / "absent.mp3"
    existing_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    existing_ithmb.write_bytes(b"KEEP")

    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args, **_kwargs: _solo_entry(existing_ithmb))
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1)],
        pc_file_paths={1: str(missing_pc_file)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert result[1] == (100, SRC_SIZE)
    assert existing_ithmb.exists()
    assert existing_ithmb.read_bytes() == b"KEEP"


def test_empty_track_list_leaves_stale_files_untouched(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    stale_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    stale_ithmb.write_bytes(b"KEEP")
    # A predictable sibling of the target must never be truncated either.
    predictable_temp = artwork_dir / "ArtworkDB.tmp"
    predictable_temp.write_bytes(b"left alone")

    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})

    result = aw.write_artworkdb(
        str(ipod_root),
        [],
        pc_file_paths={},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert result == {}
    assert stale_ithmb.exists()
    assert stale_ithmb.read_bytes() == b"KEEP"
    assert predictable_temp.read_bytes() == b"left alone"
    assert (artwork_dir / "ArtworkDB").exists()


# ─────────────────────────────────────────────────────────────────────────
# Shard placement for new payloads
# ─────────────────────────────────────────────────────────────────────────


def test_new_and_changed_art_append_to_lowest_numbered_shard(
    monkeypatch,
    tmp_path: Path,
) -> None:
    kept_count = 64
    fresh_count = 3
    later_count = 2

    ipod_root, artwork_dir = _artwork_root(tmp_path)
    existing_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    kept_payload = b"KEEP" * kept_count
    existing_ithmb.write_bytes(kept_payload)

    existing_art = {
        6000 + index: {
            "song_id": index + 1,
            "src_img_size": SRC_SIZE,
            "formats": {
                CORE_FMT: aw.ExistingFormatRef(
                    path=str(existing_ithmb),
                    ithmb_offset=index * FRAME_BYTES,
                    size=FRAME_BYTES,
                    width=1,
                    height=1,
                ),
            },
        }
        for index in range(kept_count)
    }
    kept_tracks = [_track(index + 1) for index in range(kept_count)]
    fresh_tracks = [
        _track(kept_count + index + 1)
        for index in range(fresh_count)
    ]
    later_tracks = [
        _track(kept_count + fresh_count + index + 1)
        for index in range(later_count)
    ]
    first_source = tmp_path / "cover-a.png"
    first_source.write_bytes(b"placeholder alpha")
    second_source = tmp_path / "cover-b.png"
    second_source.write_bytes(b"placeholder beta")

    real_read = aw.read_existing_artwork
    monkeypatch.setattr(
        aw,
        "read_existing_artwork",
        _first_read_replaces(existing_art, real_read),
    )
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    extract_calls = 0

    def _extract(path: str) -> bytes:
        nonlocal extract_calls
        extract_calls += 1
        return Path(path).name.encode("ascii")

    monkeypatch.setattr(aw, "extract_art_with_folder", _extract)
    monkeypatch.setattr(aw, "image_from_bytes", _stub_image)
    encode_calls = 0

    def _encode(_img, _fmt_id, *_args, **_kwargs):
        nonlocal encode_calls
        encode_calls += 1
        return aw.EncodedFormatPayload(
            data=b"NEXT",
            width=1,
            height=1,
            size=FRAME_BYTES,
            stride_pixels=1,
        )

    monkeypatch.setattr(aw, "encode_image_for_format", _encode)

    result = aw.write_artworkdb(
        str(ipod_root),
        [*kept_tracks, *fresh_tracks, *later_tracks],
        pc_file_paths={
            track.db_track_id: str(first_source)
            for track in fresh_tracks
        },
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert len(result) == kept_count + fresh_count
    assert extract_calls == 1
    assert encode_calls == 1
    assert existing_ithmb.read_bytes() == kept_payload + b"NEXT"
    assert not (artwork_dir / f"F{CORE_FMT}_2.ithmb").exists()

    rewritten = real_read(
        str(artwork_dir / "ArtworkDB"),
        str(artwork_dir),
    )
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in rewritten.values()
    }
    assert len(refs_by_song_id) == kept_count + fresh_count
    assert refs_by_song_id[1].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[fresh_tracks[0].db_track_id].ithmb_filename == (
        f"F{CORE_FMT}_1.ithmb"
    )

    second_result = aw.write_artworkdb(
        str(ipod_root),
        [*kept_tracks, *fresh_tracks, *later_tracks],
        pc_file_paths={
            track.db_track_id: str(second_source)
            for track in later_tracks
        },
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert len(second_result) == (
        kept_count + fresh_count + later_count
    )
    assert extract_calls == 2
    assert encode_calls == 2
    assert existing_ithmb.read_bytes() == kept_payload + b"NEXT" + b"NEXT"
    assert not (artwork_dir / f"F{CORE_FMT}_2.ithmb").exists()

    rewritten = real_read(
        str(artwork_dir / "ArtworkDB"),
        str(artwork_dir),
    )
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in rewritten.values()
    }
    assert refs_by_song_id[fresh_tracks[0].db_track_id].ithmb_filename == (
        f"F{CORE_FMT}_1.ithmb"
    )
    assert refs_by_song_id[later_tracks[0].db_track_id].ithmb_filename == (
        f"F{CORE_FMT}_1.ithmb"
    )


def test_appended_art_lands_in_lowest_shard_that_fits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    first_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    second_ithmb = artwork_dir / f"F{CORE_FMT}_2.ithmb"
    first_ithmb.write_bytes(b"FRST")
    second_ithmb.write_bytes(b"SCND")
    source = tmp_path / "brand-new.mp3"
    source.write_bytes(b"AUDIO")
    existing_art = {
        901: {
            "song_id": 1,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(first_ithmb)},
        },
        902: {
            "song_id": 2,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(second_ithmb)},
        },
    }

    parse_written = aw.read_existing_artwork
    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args: existing_art)
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda _path: b"feed")
    monkeypatch.setattr(aw, "image_from_bytes", _stub_image)
    monkeypatch.setattr(aw, "encode_image_for_format", _stub_encode)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1), _track(2), _track(3)],
        pc_file_paths={3: str(source)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert set(result) == {1, 2, 3}
    assert first_ithmb.read_bytes() == b"FRSTNEXT"
    assert second_ithmb.read_bytes() == b"SCND"

    rewritten = parse_written(
        str(artwork_dir / "ArtworkDB"),
        str(artwork_dir),
    )
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in rewritten.values()
    }
    assert refs_by_song_id[1].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[1].ithmb_offset == 0
    assert refs_by_song_id[2].ithmb_filename == f"F{CORE_FMT}_2.ithmb"
    assert refs_by_song_id[2].ithmb_offset == 0
    assert refs_by_song_id[3].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[3].ithmb_offset == FRAME_BYTES


def test_full_lowest_shard_rolls_new_art_into_next_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    first_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    first_ithmb.write_bytes(b"FRST")
    source = tmp_path / "brand-new.mp3"
    source.write_bytes(b"AUDIO")
    existing_art = {
        901: {
            "song_id": 1,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(first_ithmb)},
        },
    }

    monkeypatch.setattr(aw, "ITHMB_MAX_SIZE_BYTES", 6)
    parse_written = aw.read_existing_artwork
    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args: existing_art)
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda _path: b"feed")
    monkeypatch.setattr(aw, "image_from_bytes", _stub_image)
    monkeypatch.setattr(aw, "encode_image_for_format", _stub_encode)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1), _track(2)],
        pc_file_paths={2: str(source)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert set(result) == {1, 2}
    assert first_ithmb.read_bytes() == b"FRST"
    assert (artwork_dir / f"F{CORE_FMT}_2.ithmb").read_bytes() == b"NEXT"

    rewritten = parse_written(
        str(artwork_dir / "ArtworkDB"),
        str(artwork_dir),
    )
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in rewritten.values()
    }
    assert refs_by_song_id[1].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[1].ithmb_offset == 0
    assert refs_by_song_id[2].ithmb_filename == f"F{CORE_FMT}_2.ithmb"
    assert refs_by_song_id[2].ithmb_offset == 0


def test_payload_budget_rolls_writes_across_numbered_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    tracks = [_track(1), _track(2), _track(3)]
    pc_paths = {}
    for track in tracks:
        pc_file = tmp_path / f"clip-{track.db_track_id}.mp3"
        pc_file.write_bytes(b"AUDIO")
        pc_paths[track.db_track_id] = str(pc_file)

    monkeypatch.setattr(aw, "ITHMB_MAX_SIZE_BYTES", 8)
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(
        aw,
        "extract_art_with_folder",
        lambda path: Path(path).name.encode("ascii"),
    )
    monkeypatch.setattr(
        aw,
        "image_from_bytes",
        lambda data: Image.new("RGB", (1, 1), (data[0], 0, 0)),
    )
    monkeypatch.setattr(
        aw,
        "encode_image_for_format",
        lambda _img, fmt_id, *_args, **_kwargs: aw.EncodedFormatPayload(
            data=bytes([fmt_id % 256]) * FRAME_BYTES,
            width=1,
            height=1,
            size=FRAME_BYTES,
            stride_pixels=1,
        ),
    )

    result = aw.write_artworkdb(
        str(ipod_root),
        tracks,
        pc_file_paths=pc_paths,
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert set(result) == {1, 2, 3}
    assert (artwork_dir / f"F{CORE_FMT}_1.ithmb").read_bytes() == bytes(
        [CORE_FMT % 256]
    ) * 8
    assert (artwork_dir / f"F{CORE_FMT}_2.ithmb").read_bytes() == bytes(
        [CORE_FMT % 256]
    ) * FRAME_BYTES

    parsed = aw.read_existing_artwork(str(artwork_dir / "ArtworkDB"), str(artwork_dir))
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in parsed.values()
    }
    assert refs_by_song_id[1].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[1].ithmb_offset == 0
    assert refs_by_song_id[2].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[2].ithmb_offset == FRAME_BYTES
    assert refs_by_song_id[3].ithmb_filename == f"F{CORE_FMT}_2.ithmb"
    assert refs_by_song_id[3].ithmb_offset == 0


def test_writer_respects_documented_shard_size_budget() -> None:
    assert aw.ITHMB_MAX_SIZE_BYTES == 32 * 1000 * 1000


def test_cleared_slot_in_lowest_shard_is_reused_for_new_art(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    low_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    tail_ithmb = artwork_dir / f"F{CORE_FMT}_2.ithmb"
    low_ithmb.write_bytes(b"ROOT")
    tail_ithmb.write_bytes(b"LAST")
    source = tmp_path / "brand-new.mp3"
    source.write_bytes(b"AUDIO")
    existing_art = {
        901: {
            "song_id": 1,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(low_ithmb)},
        },
        902: {
            "song_id": 2,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(tail_ithmb)},
        },
    }

    parse_written = aw.read_existing_artwork
    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args: existing_art)
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda _path: b"feed")
    monkeypatch.setattr(aw, "image_from_bytes", _stub_image)
    monkeypatch.setattr(aw, "encode_image_for_format", _stub_encode)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1, hint="clear_art"), _track(2), _track(3)],
        pc_file_paths={3: str(source)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert set(result) == {2, 3}
    assert low_ithmb.read_bytes() == b"NEXT"
    assert tail_ithmb.read_bytes() == b"LAST"

    rewritten = parse_written(
        str(artwork_dir / "ArtworkDB"),
        str(artwork_dir),
    )
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in rewritten.values()
    }
    assert set(refs_by_song_id) == {2, 3}
    assert refs_by_song_id[2].ithmb_filename == f"F{CORE_FMT}_2.ithmb"
    assert refs_by_song_id[2].ithmb_offset == 0
    assert refs_by_song_id[3].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[3].ithmb_offset == 0


def test_clearing_only_owner_frees_lowest_shard_for_new_art(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    low_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    low_ithmb.write_bytes(b"ROOT")
    source = tmp_path / "brand-new.mp3"
    source.write_bytes(b"AUDIO")
    existing_art = {
        901: {
            "song_id": 1,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(low_ithmb)},
        },
    }

    parse_written = aw.read_existing_artwork
    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args: existing_art)
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda _path: b"feed")
    monkeypatch.setattr(aw, "image_from_bytes", _stub_image)
    monkeypatch.setattr(aw, "encode_image_for_format", _stub_encode)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1, hint="clear_art"), _track(2)],
        pc_file_paths={2: str(source)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert set(result) == {2}
    assert low_ithmb.read_bytes() == b"NEXT"
    assert not (artwork_dir / f"F{CORE_FMT}_2.ithmb").exists()

    rewritten = parse_written(
        str(artwork_dir / "ArtworkDB"),
        str(artwork_dir),
    )
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in rewritten.values()
    }
    assert set(refs_by_song_id) == {2}
    assert refs_by_song_id[2].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[2].ithmb_offset == 0


def test_new_art_reuses_lowest_shard_after_earlier_ones_clear(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    low_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    middle_ithmb = artwork_dir / f"F{CORE_FMT}_2.ithmb"
    tail_ithmb = artwork_dir / f"F{CORE_FMT}_3.ithmb"
    low_ithmb.write_bytes(b"ROOT")
    middle_ithmb.write_bytes(b"MIDD")
    tail_ithmb.write_bytes(b"LAST")
    source = tmp_path / "brand-new.mp3"
    source.write_bytes(b"AUDIO")
    existing_art = {
        901: {
            "song_id": 1,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(low_ithmb)},
        },
        902: {
            "song_id": 2,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(middle_ithmb)},
        },
        903: {
            "song_id": 3,
            "src_img_size": SRC_SIZE,
            "formats": {CORE_FMT: _frame_ref(tail_ithmb)},
        },
    }

    parse_written = aw.read_existing_artwork
    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args: existing_art)
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda _path: b"feed")
    monkeypatch.setattr(aw, "image_from_bytes", _stub_image)
    monkeypatch.setattr(aw, "encode_image_for_format", _stub_encode)

    result = aw.write_artworkdb(
        str(ipod_root),
        [
            _track(1, hint="clear_art"),
            _track(2, hint="clear_art"),
            _track(3),
            _track(4),
        ],
        pc_file_paths={4: str(source)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert set(result) == {3, 4}
    assert low_ithmb.read_bytes() == b"NEXT"
    assert middle_ithmb.read_bytes() == b"MIDD"
    assert tail_ithmb.read_bytes() == b"LAST"

    rewritten = parse_written(
        str(artwork_dir / "ArtworkDB"),
        str(artwork_dir),
    )
    refs_by_song_id = {
        entry["song_id"]: entry["formats"][CORE_FMT]
        for entry in rewritten.values()
    }
    assert set(refs_by_song_id) == {3, 4}
    assert refs_by_song_id[3].ithmb_filename == f"F{CORE_FMT}_3.ithmb"
    assert refs_by_song_id[3].ithmb_offset == 0
    assert refs_by_song_id[4].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs_by_song_id[4].ithmb_offset == 0


# ─────────────────────────────────────────────────────────────────────────
# Clearing artwork
# ─────────────────────────────────────────────────────────────────────────


def test_unrecoverable_source_clears_link_but_keeps_image_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    pc_file = tmp_path / "clip.mp3"
    pc_file.write_bytes(b"AUDIO")
    existing_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    existing_ithmb.write_bytes(b"KEEP")

    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args, **_kwargs: _solo_entry(existing_ithmb))
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda _path: None)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1)],
        pc_file_paths={1: str(pc_file)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert result == {}
    assert existing_ithmb.exists()
    assert existing_ithmb.read_bytes() == b"KEEP"


def test_clear_hint_with_foreign_format_warns_and_skips_file(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    foreign_ithmb = artwork_dir / f"F{FOREIGN_FMT}_1.ithmb"
    foreign_ithmb.write_bytes(b"ODDD")

    monkeypatch.setattr(
        aw,
        "read_existing_artwork",
        lambda *_args, **_kwargs: _solo_entry(
            foreign_ithmb,
            format_id=FOREIGN_FMT,
        ),
    )
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})

    with caplog.at_level(logging.WARNING):
        result = aw.write_artworkdb(
            str(ipod_root),
            [_track(1, hint="clear_art")],
            pc_file_paths={},
            artwork_formats={CORE_FMT: (1, 1)},
        )

    assert result == {}
    assert foreign_ithmb.read_bytes() == b"ODDD"
    assert f"unknown artwork format {FOREIGN_FMT} at {foreign_ithmb}" in caplog.text


# ─────────────────────────────────────────────────────────────────────────
# Salvage and re-encode
# ─────────────────────────────────────────────────────────────────────────


def test_partial_format_set_is_salvaged_by_reencoding(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    aux_ithmb = artwork_dir / f"F{AUX_FMT}_1.ithmb"
    aux_ithmb.write_bytes(b"KEEP")

    monkeypatch.setattr(
        aw,
        "read_existing_artwork",
        lambda *_args, **_kwargs: _solo_entry(aux_ithmb, format_id=AUX_FMT),
    )
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)
    monkeypatch.setattr(
        aw,
        "_decode_preserved_frame",
        lambda *_args, **_kwargs: Image.new("RGB", (1, 1), (5, 6, 4)),
    )
    monkeypatch.setattr(
        aw,
        "encode_image_for_format",
        lambda _img, fmt_id, *_args, **_kwargs: aw.EncodedFormatPayload(
            data=bytes([fmt_id % 256]) * FRAME_BYTES,
            width=1,
            height=1,
            size=FRAME_BYTES,
            stride_pixels=1,
        ),
    )

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1)],
        pc_file_paths={},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert result[1] == (100, SRC_SIZE)
    assert (artwork_dir / f"F{AUX_FMT}_1.ithmb").exists()
    assert (artwork_dir / f"F{CORE_FMT}_1.ithmb").read_bytes() == bytes(
        [CORE_FMT % 256]
    ) * FRAME_BYTES


def test_unsalvageable_preserved_art_is_dropped(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    aux_ithmb = artwork_dir / f"F{AUX_FMT}_1.ithmb"
    aux_ithmb.write_bytes(b"KEEP")

    monkeypatch.setattr(
        aw,
        "read_existing_artwork",
        lambda *_args, **_kwargs: _solo_entry(aux_ithmb, format_id=AUX_FMT),
    )
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)
    monkeypatch.setattr(aw, "_decode_preserved_frame", lambda *_args, **_kwargs: None)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1)],
        pc_file_paths={},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert result == {}
    assert aux_ithmb.exists()
    assert not (artwork_dir / f"F{CORE_FMT}_1.ithmb").exists()


def test_new_art_reencodes_all_known_device_formats(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    pc_file = tmp_path / "clip.mp3"
    pc_file.write_bytes(b"AUDIO")
    core_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    aux_ithmb = artwork_dir / f"F{AUX_FMT}_1.ithmb"
    core_ithmb.write_bytes(b"OWNR")
    aux_ithmb.write_bytes(b"AUXD")

    real_read = aw.read_existing_artwork
    monkeypatch.setattr(
        aw,
        "read_existing_artwork",
        lambda *_args, **_kwargs: {
            44: {
                "song_id": 1,
                "src_img_size": SRC_SIZE,
                "formats": {
                    CORE_FMT: _frame_ref(core_ithmb),
                    AUX_FMT: _frame_ref(aux_ithmb),
                },
            },
        },
    )
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda _path: COVER_BYTES)
    monkeypatch.setattr(aw, "image_from_bytes", _stub_image)

    seen_format_ids: list[int] = []

    def _encode(_img, fmt_id, *_args, **_kwargs):
        seen_format_ids.append(fmt_id)
        return aw.EncodedFormatPayload(
            data=bytes([fmt_id % 256]) * FRAME_BYTES,
            width=1,
            height=1,
            size=FRAME_BYTES,
            stride_pixels=1,
        )

    monkeypatch.setattr(aw, "encode_image_for_format", _encode)

    result = aw.write_artworkdb(
        str(ipod_root),
        [_track(1)],
        pc_file_paths={1: str(pc_file)},
        artwork_formats={CORE_FMT: (1, 1)},
    )

    assert result[1] == (100, len(COVER_BYTES))
    assert seen_format_ids == [CORE_FMT, AUX_FMT]
    assert core_ithmb.read_bytes() == bytes([CORE_FMT % 256]) * FRAME_BYTES
    assert aux_ithmb.read_bytes() == bytes([AUX_FMT % 256]) * FRAME_BYTES
    assert not (artwork_dir / f"F{CORE_FMT}_2.ithmb").exists()
    assert not (artwork_dir / f"F{AUX_FMT}_2.ithmb").exists()

    rewritten = real_read(
        str(artwork_dir / "ArtworkDB"),
        str(artwork_dir),
    )
    refs = next(iter(rewritten.values()))["formats"]
    assert refs[CORE_FMT].ithmb_filename == f"F{CORE_FMT}_1.ithmb"
    assert refs[AUX_FMT].ithmb_filename == f"F{AUX_FMT}_1.ithmb"


def test_incremental_video_sync_preserves_existing_frames(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    video_formats = {
        VIDEO_MIN_FMT: (1, 1),
        VIDEO_MAX_FMT: (2, 2),
    }
    tracks = [_track(1), _track(2)]
    pc_paths: dict[int, str] = {}
    for track in tracks:
        pc_file = tmp_path / f"vid-{track.db_track_id}.mp3"
        pc_file.write_bytes(b"VIDEO")
        pc_paths[track.db_track_id] = str(pc_file)

    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda path: f"img:{path}".encode())
    monkeypatch.setattr(
        aw,
        "image_from_bytes",
        lambda *_args, **_kwargs: Image.new("RGB", (2, 2), (11, 22, 33)),
    )

    def _encode(_img, fmt_id, *_args, **_kwargs):
        width, height = video_formats[fmt_id]
        data = bytes([fmt_id % 256]) * (width * height * 2)
        return aw.EncodedFormatPayload(
            data=data,
            width=width,
            height=height,
            size=len(data),
            stride_pixels=width,
        )

    monkeypatch.setattr(aw, "encode_image_for_format", _encode)

    first_result = aw.write_artworkdb(
        str(ipod_root),
        tracks,
        pc_file_paths=pc_paths,
        artwork_formats=video_formats,
    )
    for track in tracks:
        img_id, src_size = first_result[track.db_track_id]
        track.mhii_link = img_id
        track.artwork_count = 1
        track.artwork_size = src_size
    old_img_links = {track.db_track_id: track.mhii_link for track in tracks}

    new_track = _track(3)
    new_pc_file = tmp_path / "vid-3.mp3"
    new_pc_file.write_bytes(b"VIDEO")

    second_result = aw.write_artworkdb(
        str(ipod_root),
        [*tracks, new_track],
        pc_file_paths={3: str(new_pc_file)},
        artwork_formats=video_formats,
        start_img_id=640,
    )

    assert set(second_result) == {1, 2, 3}
    assert old_img_links == {1: 100, 2: 101}
    assert {db_track_id: current[0] for db_track_id, current in second_result.items()} == {
        1: 640,
        2: 641,
        3: 642,
    }
    assert all(
        second_result[db_track_id][0] != old_img_links[db_track_id]
        for db_track_id in old_img_links
    )

    for track in [*tracks, new_track]:
        img_id, src_size = second_result[track.db_track_id]
        track.mhii_link = img_id
        track.artwork_count = 1
        track.artwork_size = src_size
    assert {track.db_track_id: track.mhii_link for track in [*tracks, new_track]} == {
        1: 640,
        2: 641,
        3: 642,
    }

    rewritten = aw.read_existing_artwork(str(artwork_dir / "ArtworkDB"), str(artwork_dir))
    assert set(rewritten) == {640, 641, 642}
    assert {entry["song_id"]: img_id for img_id, entry in rewritten.items()} == {
        1: 640,
        2: 641,
        3: 642,
    }
    for entry in rewritten.values():
        assert set(entry["formats"]) == {VIDEO_MIN_FMT, VIDEO_MAX_FMT}
    assert (artwork_dir / f"F{VIDEO_MIN_FMT}_1.ithmb").stat().st_size > 0
    assert (artwork_dir / f"F{VIDEO_MAX_FMT}_1.ithmb").stat().st_size > 0


def test_foreign_format_passthrough_survives_rewritten_index(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    core_ithmb = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    foreign_ithmb = artwork_dir / f"F{FOREIGN_FMT}_1.ithmb"
    core_ithmb.write_bytes(b"KOWN")
    foreign_ithmb.write_bytes(b"ODDD")

    real_read = aw.read_existing_artwork
    monkeypatch.setattr(
        aw,
        "read_existing_artwork",
        _first_read_replaces(
            {
                44: {
                    "song_id": 1,
                    "src_img_size": SRC_SIZE,
                    "formats": {
                        CORE_FMT: _frame_ref(core_ithmb),
                        FOREIGN_FMT: _frame_ref(foreign_ithmb),
                    },
                },
            },
            real_read,
        ),
    )
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "expected_size_bytes", lambda *_args, **_kwargs: FRAME_BYTES)

    with caplog.at_level(logging.WARNING):
        result = aw.write_artworkdb(
            str(ipod_root),
            [_track(1)],
            pc_file_paths={},
            artwork_formats={CORE_FMT: (1, 1)},
        )

    assert result[1] == (100, SRC_SIZE)
    assert foreign_ithmb.read_bytes() == b"ODDD"
    rewritten = real_read(str(artwork_dir / "ArtworkDB"), str(artwork_dir))
    assert rewritten[100]["formats"][FOREIGN_FMT].path == str(foreign_ithmb)
    assert f"unknown artwork format {FOREIGN_FMT}" in caplog.text


def test_flush_failure_removes_scratch_files_and_reraises(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ipod_root, artwork_dir = _artwork_root(tmp_path)
    source = tmp_path / "clip.mp3"
    source.write_bytes(b"AUDIO")
    hooks: list[str] = []

    monkeypatch.setattr(aw, "read_existing_artwork", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(aw, "get_artwork_format_definitions", lambda _ipod_path: {})
    monkeypatch.setattr(aw, "extract_art_with_folder", lambda _path: b"pixels")
    monkeypatch.setattr(aw, "image_from_bytes", _stub_image)
    monkeypatch.setattr(aw, "encode_image_for_format", _stub_encode)

    def _broken_flush(_file) -> None:
        raise OSError("disk sync failed")

    monkeypatch.setattr(aw, "flush_written_file", _broken_flush)

    with pytest.raises(OSError, match="disk sync failed"):
        aw.write_artworkdb(
            str(ipod_root),
            [_track(1)],
            pc_file_paths={1: str(source)},
            artwork_formats={CORE_FMT: (1, 1)},
            before_device_mutation=lambda: hooks.append("verify"),
        )

    assert hooks
    assert list(artwork_dir.glob(".iop-*.tmp")) == []
    assert not (artwork_dir / f"F{CORE_FMT}_1.ithmb").exists()


# ─────────────────────────────────────────────────────────────────────────
# Reading the index back
# ─────────────────────────────────────────────────────────────────────────


def test_read_back_resolves_numbered_shard_from_mhni_name(
    tmp_path: Path,
) -> None:
    _ipod_root, artwork_dir = _artwork_root(tmp_path)
    ithmb_path = artwork_dir / f"F{CORE_FMT}_2.ithmb"
    ithmb_path.write_bytes(b"NOISEDAT")
    entry = aw.ArtworkEntry(
        55,
        7,
        None,
        SRC_SIZE,
        {
            CORE_FMT: aw.EncodedFormatPayload(
                data=b"BYTE",
                width=1,
                height=1,
                size=FRAME_BYTES,
                stride_pixels=1,
            ),
        },
    )
    artdb_data = aw.build_artworkdb(
        [entry],
        {55: {CORE_FMT: aw.IthmbLocation(f"F{CORE_FMT}_2.ithmb", 6)}},
        [CORE_FMT],
        {CORE_FMT: FRAME_BYTES},
        56,
    )
    artdb_path = artwork_dir / "ArtworkDB"
    artdb_path.write_bytes(artdb_data)

    parsed = aw.read_existing_artwork(str(artdb_path), str(artwork_dir))

    ref = parsed[55]["formats"][CORE_FMT]
    assert ref.path == str(ithmb_path)
    assert ref.ithmb_filename == f"F{CORE_FMT}_2.ithmb"
    assert ref.ithmb_offset == 6


def test_truncated_artworkdb_is_rejected_as_unsafe(
    tmp_path: Path,
) -> None:
    _ipod_root, artwork_dir = _artwork_root(tmp_path)
    ithmb_path = artwork_dir / f"F{CORE_FMT}_1.ithmb"
    ithmb_path.write_bytes(b"BYTE")
    entry = aw.ArtworkEntry(
        55,
        7,
        None,
        SRC_SIZE,
        {
            CORE_FMT: aw.EncodedFormatPayload(
                data=b"BYTE",
                width=1,
                height=1,
                size=FRAME_BYTES,
                stride_pixels=1,
            ),
        },
    )
    artdb_data = aw.build_artworkdb(
        [entry],
        {55: {CORE_FMT: 0}},
        [CORE_FMT],
        {CORE_FMT: FRAME_BYTES},
        56,
    )
    artdb_path = artwork_dir / "ArtworkDB"
    artdb_path.write_bytes(artdb_data[:-12])

    with pytest.raises(
        UnsafeWriteError,
        match="ArtworkDB is malformed or truncated",
    ):
        aw.read_existing_artwork(str(artdb_path), str(artwork_dir))
