"""Build ArtworkDB bytes, and read an existing ArtworkDB strictly before replacing it.

The writer emits three datasets: images (``mhli``), an empty photo-album list
(``mhla``) and the file list (``mhlf``) naming each format's ``.ithmb`` size.
The reader refuses anything malformed with :class:`UnsafeWriteError` — a
half-understood index must never be overwritten.
"""

from __future__ import annotations

import logging
import os
import struct
from collections.abc import Iterable, Mapping
from typing import Union

from podsync.artwork.spec.files import ithmb_filename, ithmb_path_for_filename, normalize_ithmb_filename
from podsync.artwork.spec.format import (
    MHFD_HEADER_SIZE,
    MHIF_HEADER_SIZE,
    MHII_HEADER_SIZE,
    MHLA_HEADER_SIZE,
    MHLF_HEADER_SIZE,
    MHLI_HEADER_SIZE,
    MHNI_HEADER_SIZE,
    MHOD_HEADER_SIZE,
    MHSD_HEADER_SIZE,
    ArtworkDatasetType,
    ArtworkMhodType,
    decode_mhod_string_chunk,
    encode_mhod_string_body,
    read_chunk_header,
    read_u16,
    read_u32,
    read_u64,
    total_length_is_valid,
)
from podsync.artwork.spec.renditions import expected_size_bytes, read_mhni_fields
from podsync.artwork.writer.records import ArtworkEntry, ExistingFormatRef, IthmbLocation
from podsync.hardware.safety.guard import UnsafeWriteError

__all__ = ["build_artworkdb", "read_existing_artwork"]

logger = logging.getLogger(__name__)

IthmbLocationInput = Union[IthmbLocation, tuple, int, None]
_MAX_PADDING = 0x7FFF

# ── building ────────────────────────────────────────────────────────


def _frame(tag: bytes, header_size: int, body: bytes = b"", *, third: int | None = None,
           words: Iterable[tuple[str, int, tuple]] = ()) -> bytes:
    """A chunk: generic header (third word = total length unless given), extra fields, body."""
    header = bytearray(header_size)
    struct.pack_into("<4sII", header, 0, tag, header_size, header_size + len(body) if third is None else third)
    for fmt, offset, values in words:
        struct.pack_into(fmt, header, offset, *values)
    return bytes(header) + body


def _location(fmt_id: int, where: IthmbLocationInput) -> IthmbLocation:
    """Accept an :class:`IthmbLocation`, a ``(filename, offset)`` pair or a bare offset."""
    if isinstance(where, IthmbLocation):
        return IthmbLocation(normalize_ithmb_filename(fmt_id, where.filename), int(where.offset))
    if isinstance(where, tuple):
        filename, offset = where
        return IthmbLocation(normalize_ithmb_filename(fmt_id, filename), int(offset))
    return IthmbLocation(ithmb_filename(fmt_id, 1), int(where or 0))


def _string_object(kind: int, text: str) -> bytes:
    return _frame(b"mhod", MHOD_HEADER_SIZE, encode_mhod_string_body(kind, text), words=[("<H", 12, (kind,))])


def _rendition(format_id: int, where: IthmbLocation, payload) -> bytes:
    """``mhni``: which ``.ithmb`` holds the pixels, at what offset, with what geometry."""
    vpad = max(0, int(getattr(payload, "vpad", 0) or 0))
    hpad = max(0, int(getattr(payload, "hpad", 0) or 0))
    if vpad > _MAX_PADDING or hpad > _MAX_PADDING:
        raise ValueError(f"MHNI padding too large for format {format_id}: vpad={vpad} hpad={hpad}")
    width, height, size = int(payload.width), int(payload.height), int(payload.size)
    if not (vpad or hpad):
        stride = max(width, int(getattr(payload, "stride_pixels", width) or width))
        expected = expected_size_bytes(format_id, width, height, stride_pixels=stride)
        if expected > 0 and expected != size:
            logger.debug("ART: MHNI size mismatch for fmt %s: payload %d bytes, expected %d", format_id, size, expected)
    name = _string_object(ArtworkMhodType.FILE_NAME, ":" + where.filename)
    return _frame(b"mhni", MHNI_HEADER_SIZE, name, words=[
        ("<IIII", 12, (1, format_id, where.offset, size)),
        ("<hhHH", 28, (vpad, hpad, height & 0xFFFF, width & 0xFFFF)),
        ("<II", 36, (0, size)),
    ])


def _image(entry: ArtworkEntry, locations: Mapping[int, IthmbLocationInput]) -> bytes:
    """``mhii``: one track's artwork with one thumbnail container per format."""
    containers = [
        _frame(b"mhod", MHOD_HEADER_SIZE,
               _rendition(fmt_id, _location(fmt_id, locations.get(fmt_id, 0)), entry.formats[fmt_id]),
               words=[("<H", 12, (ArtworkMhodType.THUMBNAIL_IMAGE,))])
        for fmt_id in sorted(entry.formats)
    ]
    return _frame(b"mhii", MHII_HEADER_SIZE, b"".join(containers), words=[
        ("<IIQ", 12, (len(containers), entry.img_id, entry.db_track_id)),
        ("<I", 48, (entry.src_img_size,)),
    ])


def _list(tag: bytes, header_size: int, items: list[bytes]) -> bytes:
    return _frame(tag, header_size, b"".join(items), third=len(items))


def _dataset(kind: int, child: bytes) -> bytes:
    return _frame(b"mhsd", MHSD_HEADER_SIZE, child, words=[("<H", 12, (kind,))])


def _file_entry(format_id: int, image_size: int) -> bytes:
    return _frame(b"mhif", MHIF_HEADER_SIZE, third=MHIF_HEADER_SIZE, words=[("<II", 16, (format_id, image_size))])


def build_artworkdb(
    entries: list[ArtworkEntry],
    format_locations_map: Mapping[int, Mapping[int, IthmbLocationInput]],
    format_ids: list[int],
    image_sizes: dict[int, int],
    next_mhii_id: int,
    reference_mhfd: bytes | None = None,
) -> bytes:
    """The complete ArtworkDB; opaque header words are carried over from *reference_mhfd*."""
    datasets = [
        _dataset(ArtworkDatasetType.IMAGE_LIST, _list(b"mhli", MHLI_HEADER_SIZE, [
            _image(entry, format_locations_map.get(entry.img_id, {})) for entry in entries
        ])),
        _dataset(ArtworkDatasetType.PHOTO_ALBUM_LIST, _list(b"mhla", MHLA_HEADER_SIZE, [])),
        _dataset(ArtworkDatasetType.FILE_LIST, _list(b"mhlf", MHLF_HEADER_SIZE, [
            _file_entry(fmt_id, int(image_sizes.get(fmt_id, 0))) for fmt_id in format_ids
        ])),
    ]
    root = bytearray(_frame(b"mhfd", MHFD_HEADER_SIZE, b"".join(datasets), words=[
        ("<I", 16, (2,)), ("<I", 20, (len(datasets),)), ("<I", 28, (next_mhii_id,)), ("<I", 48, (2,)),
    ]))
    reference = reference_mhfd or b""
    if len(reference) >= 48:
        root[32:48] = reference[32:48]
    if len(reference) >= 68:
        root[60:68] = reference[60:68]
    return bytes(root)


# ── strict reading ──────────────────────────────────────────────────


def _malformed(detail: str) -> UnsafeWriteError:
    return UnsafeWriteError(
        "The existing ArtworkDB is malformed or truncated. podsync stopped before replacing "
        f"artwork metadata ({detail})."
    )


def _rendition_filename(data: bytes, mhni_at: int, container_end: int) -> str | None:
    """The file-name string inside an ``mhni``, if a well-formed one is present."""
    try:
        header_size, total = read_u32(data, mhni_at + 4), read_u32(data, mhni_at + 8)
    except struct.error:
        return None
    end = min(container_end, mhni_at + total)
    cursor = mhni_at + max(header_size, MHNI_HEADER_SIZE)
    while cursor + 24 <= end:
        if data[cursor:cursor + 4] != b"mhod":
            return None
        child_header, child_total = read_u32(data, cursor + 4), read_u32(data, cursor + 8)
        if child_header < 24 or child_total < child_header or cursor + child_total > end:
            return None
        if read_u16(data, cursor + 12) == ArtworkMhodType.FILE_NAME:
            return decode_mhod_string_chunk(data, cursor, child_total)
        cursor += child_total
    return None


def _image_entry(data: bytes, offset: int, total: int, artwork_dir: str) -> dict | None:
    """One ``mhii`` as ``{img_id, song_id, src_img_size, formats}``; ``None`` if nothing usable."""
    header_size = read_u32(data, offset + 4)
    if not 52 <= header_size <= total:
        raise _malformed(f"invalid mhii header size {header_size} at offset {offset}")
    child_count, img_id, song_id = read_u32(data, offset + 12), read_u32(data, offset + 16), read_u64(data, offset + 20)
    end = offset + total
    cursor = offset + header_size
    formats: dict[int, ExistingFormatRef] = {}
    for index in range(child_count):
        if cursor + 14 > end or data[cursor:cursor + 4] != b"mhod":
            raise _malformed(f"missing mhod child {index} at offset {cursor}")
        mhod_header, mhod_total = read_u32(data, cursor + 4), read_u32(data, cursor + 8)
        if not total_length_is_valid(data, cursor, mhod_header, mhod_total, min_header_size=14, end=end):
            raise _malformed(f"invalid mhod chunk at offset {cursor}")
        if read_u16(data, cursor + 12) == ArtworkMhodType.THUMBNAIL_IMAGE:
            mhni_at, container_end = cursor + mhod_header, cursor + mhod_total
            if mhni_at + MHNI_HEADER_SIZE > container_end or data[mhni_at:mhni_at + 4] != b"mhni":
                raise _malformed(f"invalid mhni thumbnail at offset {mhni_at}")
            f = read_mhni_fields(data, mhni_at)
            filename = normalize_ithmb_filename(f.format_id, _rendition_filename(data, mhni_at, container_end))
            path = ithmb_path_for_filename(artwork_dir, f.format_id, filename)
            if os.path.exists(path) and f.image_size > 0:
                formats[f.format_id] = ExistingFormatRef(
                    path=path, ithmb_offset=f.ithmb_offset, size=f.image_size,
                    width=max(1, f.image_width), height=max(1, f.image_height),
                    hpad=max(0, f.horizontal_padding), vpad=max(0, f.vertical_padding), ithmb_filename=filename,
                )
        cursor += mhod_total
    if not formats:
        return None
    return {"img_id": img_id, "song_id": song_id, "src_img_size": read_u32(data, offset + 48), "formats": formats}


def _image_list(data: bytes, at: int, end: int, artwork_dir: str, found: dict[int, dict]) -> None:
    if at + 12 > end or data[at:at + 4] != b"mhli":
        raise _malformed(f"missing mhli image list at offset {at}")
    header_size, count = read_u32(data, at + 4), read_u32(data, at + 8)
    if header_size < 12 or at + header_size > end:
        raise _malformed(f"invalid mhli chunk at offset {at}")
    cursor = at + header_size
    for index in range(count):
        if cursor + 52 > end or data[cursor:cursor + 4] != b"mhii":
            raise _malformed(f"missing mhii entry {index} at offset {cursor}")
        total = read_u32(data, cursor + 8)
        if total < 52 or cursor + total > end:
            raise _malformed(f"invalid mhii chunk at offset {cursor}")
        entry = _image_entry(data, cursor, total, artwork_dir)
        if entry is not None:
            found[entry["img_id"]] = entry
        cursor += total


def read_existing_artwork(artworkdb_path: str, artwork_dir: str) -> dict[int, dict]:
    """``{img_id: entry}`` for every image whose ``.ithmb`` file still exists.

    A missing ArtworkDB means "no artwork yet" (``{}``); an unreadable or
    malformed one raises :class:`UnsafeWriteError`.
    """
    try:
        with open(artworkdb_path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise UnsafeWriteError(
            "The existing ArtworkDB could not be read safely. podsync stopped before replacing "
            f"artwork metadata: {exc}"
        ) from exc

    if len(data) < 32 or data[:4] != b"mhfd":
        raise _malformed("missing or truncated mhfd header")
    try:
        root = read_chunk_header(data, 0)
    except (ValueError, struct.error) as exc:
        raise _malformed(str(exc)) from exc
    if not total_length_is_valid(data, 0, root.header_size, root.length_or_count, min_header_size=32):
        raise _malformed(f"invalid mhfd size header={root.header_size} total={root.length_or_count}")
    end = root.length_or_count
    offset = root.header_size
    found: dict[int, dict] = {}
    for index in range(read_u32(data, 20)):
        if offset + 14 > end or data[offset:offset + 4] != b"mhsd":
            raise _malformed(f"missing mhsd child {index} at offset {offset}")
        header_size, total = read_u32(data, offset + 4), read_u32(data, offset + 8)
        if not total_length_is_valid(data, offset, header_size, total, min_header_size=14, end=end):
            raise _malformed(f"invalid mhsd chunk at offset {offset}")
        if read_u16(data, offset + 12) == ArtworkDatasetType.IMAGE_LIST:
            _image_list(data, offset + header_size, offset + total, artwork_dir, found)
        offset += total
    return found
