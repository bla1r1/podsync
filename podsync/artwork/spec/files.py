"""The ``.ithmb`` files that hold rendition pixels, and the ArtworkDB file list that names them."""

from __future__ import annotations

import os

from podsync.artwork.spec.format import ArtworkDatasetType, read_u16, read_u32, total_length_is_valid

__all__ = [
    "extract_format_ids",
    "ithmb_filename",
    "ithmb_filename_from_path",
    "ithmb_path_for_filename",
    "normalize_ithmb_filename",
]


def ithmb_filename(format_id: int, index: int = 1) -> str:
    """``F<format>_<n>.ithmb`` — the naming iTunes uses."""
    return f"F{int(format_id)}_{int(index)}.ithmb"


def normalize_ithmb_filename(format_id: int, filename: str | None, default_index: int = 1) -> str:
    """Bare file name from a stored ``:``- or ``/``-separated path (default name when empty)."""
    text = str(filename or "").strip().replace("\\", "/")
    base = text.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    return base or ithmb_filename(format_id, default_index)


def ithmb_filename_from_path(path, format_id: int, default_index: int = 1) -> str:
    return normalize_ithmb_filename(format_id, path, default_index)


def ithmb_path_for_filename(artwork_dir: str, format_id: int, filename: str | None) -> str:
    return os.path.join(artwork_dir, normalize_ithmb_filename(format_id, filename))


def extract_format_ids(data: bytes) -> list[int]:
    """Format ids listed in the ArtworkDB file list (dataset 3), in file order.

    Stops quietly at the first malformed structure; this is a best-effort hint.
    """
    if len(data) < 32 or data[:4] != b"mhfd":
        return []
    ids: list[int] = []
    offset = read_u32(data, 4)
    for _ in range(read_u32(data, 20)):
        if offset + 14 > len(data) or data[offset:offset + 4] != b"mhsd":
            break
        header, total = read_u32(data, offset + 4), read_u32(data, offset + 8)
        if not total_length_is_valid(data, offset, header, total, min_header_size=14):
            break
        if read_u16(data, offset + 12) == ArtworkDatasetType.FILE_LIST:
            end = offset + total
            listing = offset + header
            if listing + 12 > end or data[listing:listing + 4] != b"mhlf":
                break
            entry = listing + read_u32(data, listing + 4)
            for _ in range(read_u32(data, listing + 8)):
                if entry + 20 > end or data[entry:entry + 4] != b"mhif":
                    break
                size = read_u32(data, entry + 4)
                if size < 20 or entry + size > end:
                    break
                ids.append(read_u32(data, entry + 16))
                entry += size
        offset += total
    return ids
