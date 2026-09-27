"""Renditions: the ``mhni`` record describing one stored image, its byte size and its format.

Old or foreign databases do not always use the format ids the current model
table expects, so :func:`infer_image_format` works out the pixel format from
the stored size and geometry when the id alone is not conclusive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from podsync.artwork.spec.format import read_i16, read_u16, read_u32

__all__ = [
    "MhniFields",
    "default_stride_pixels_for_format",
    "expected_size_bytes",
    "expected_size_for_format",
    "infer_image_format",
    "read_mhni_fields",
]

_TWO_BYTE_FORMATS = frozenset({"RGB565_LE", "RGB565_BE", "RGB565_BE_90", "RGB555_LE", "RGB555_BE", "UYVY"})
_GEOMETRY_TOLERANCE = 2


@dataclass(frozen=True)
class MhniFields:
    child_count: int
    format_id: int
    ithmb_offset: int
    image_size: int
    vertical_padding: int
    horizontal_padding: int
    image_height: int
    image_width: int
    unk1: int
    image_size_2: int

    @property
    def estimated_pixmap_height(self) -> int:
        return self.vertical_padding + self.image_height

    @property
    def estimated_pixmap_width(self) -> int:
        return self.horizontal_padding + self.image_width


# (attribute, reader, offset from the chunk start)
_MHNI_LAYOUT = (
    ("child_count", read_u32, 12), ("format_id", read_u32, 16), ("ithmb_offset", read_u32, 20),
    ("image_size", read_u32, 24), ("vertical_padding", read_i16, 28), ("horizontal_padding", read_i16, 30),
    ("image_height", read_u16, 32), ("image_width", read_u16, 34), ("unk1", read_u32, 36),
    ("image_size_2", read_u32, 40),
)


def read_mhni_fields(data: bytes, offset: int) -> MhniFields:
    return MhniFields(**{name: read(data, offset + at) for name, read, at in _MHNI_LAYOUT})


def _two_bytes_per_pixel(pixel_format: str) -> bool:
    return pixel_format in _TWO_BYTE_FORMATS or pixel_format.startswith("REC_RGB555")


def default_stride_pixels_for_format(fmt: Any, width: int) -> int:
    """Row length in pixels: the format's row bytes for 16-bit formats, else the width."""
    if fmt is None or not _two_bytes_per_pixel(fmt.pixel_format):
        return width
    row_bytes = int(fmt.row_bytes or 0)
    return max(width, row_bytes // 2) if row_bytes else width


def expected_size_for_format(fmt: Any, width: int | None = None, height: int | None = None,
                             stride_pixels: int | None = None) -> int:
    """Bytes one rendition occupies in its ``.ithmb`` (0 for JPEG, which is variable)."""
    if fmt is None:
        return 0
    w = int(fmt.width if width is None else width)
    h = int(fmt.height if height is None else height)
    stride = stride_pixels if stride_pixels is not None else default_stride_pixels_for_format(fmt, w)
    if fmt.pixel_format == "I420_LE" and not _two_bytes_per_pixel(fmt.pixel_format):
        return (w & ~1) * (h & ~1) * 3 // 2
    if fmt.pixel_format == "JPEG":
        return 0
    return int(stride) * h * 2


def expected_size_bytes(format_id: int, width: int, height: int, stride_pixels: int | None = None,
                        fmt_override: Any = None) -> int:
    from podsync.hardware import ITHMB_FORMAT_MAP

    fmt = fmt_override if fmt_override is not None else ITHMB_FORMAT_MAP.get(format_id)
    return expected_size_for_format(fmt, width, height, stride_pixels)


def _near(a: int, b: int) -> bool:
    return abs(a - b) <= _GEOMETRY_TOLERANCE


def _fit(fmt: Any, fields: MhniFields) -> tuple[bool, int, int]:
    """``(compatible, size difference, geometry difference)`` of a candidate format."""
    expected = expected_size_for_format(fmt)
    w, h = fields.estimated_pixmap_width, fields.estimated_pixmap_height
    geometry_ok = w > 0 and h > 0 and (
        (_near(w, fmt.width) and _near(h, fmt.height)) or (_near(w, fmt.height) and _near(h, fmt.width))
    )
    size_ok = (expected > 0 and expected == fields.image_size) or (expected == 0 and geometry_ok)
    size_delta = abs(fields.image_size - expected) if expected > 0 else 0
    return size_ok or geometry_ok, size_delta, abs(w - fmt.width) + abs(h - fmt.height)


def _describe(fmt: Any, format_id: int) -> dict[str, Any]:
    return {
        "height": fmt.height, "width": fmt.width, "format": fmt.pixel_format,
        "description": fmt.description, "format_id": format_id,
    }


def infer_image_format(fields: MhniFields) -> dict[str, Any] | None:
    """Best matching format description for a stored rendition.

    1. candidates with the same id that fit the size/geometry (closest wins);
    2. the id's entry in the global table, if it fits;
    3. otherwise the closest candidate of any id, with its ``score``.
    """
    from podsync.hardware import ITHMB_FORMAT_MAP
    from podsync.hardware.catalog.artwork_presets import artwork_format_candidates

    candidates = artwork_format_candidates()
    same_id = []
    for fmt in candidates:
        if fmt.format_id != fields.format_id:
            continue
        ok, size_delta, geometry_delta = _fit(fmt, fields)
        if ok:
            same_id.append((size_delta + geometry_delta, fmt))
    if same_id:
        return _describe(min(same_id, key=lambda pair: pair[0])[1], fields.format_id)

    known = ITHMB_FORMAT_MAP.get(fields.format_id)
    if known is not None and _fit(known, fields)[0]:
        return _describe(known, fields.format_id)

    w, h = fields.estimated_pixmap_width, fields.estimated_pixmap_height
    best, best_score = None, None
    for fmt in candidates:
        score = abs(h - fmt.height) + abs(w - fmt.width)
        expected = expected_size_for_format(fmt)
        if expected > 0:
            score += abs(fields.image_size - expected) / max(1, fmt.row_bytes, fmt.width)
        if best_score is None or score < best_score:
            best, best_score = fmt, score
    if best is None:
        return None
    return {**_describe(best, best.format_id), "score": best_score}
