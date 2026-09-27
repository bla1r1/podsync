"""Pixel codecs for ``.ithmb`` renditions (Pillow only, no numpy).

Each pixel family has a codec object with ``encode`` (Pillow image → payload
bytes) and ``decode`` (bytes → image) methods:

* packed 16-bit RGB — 565 or 555, little- or big-endian, optionally stored
  rotated 90° (``RGB565_BE_90``);
* UYVY 4:2:2 and I420 4:2:0 YUV (TV-out and photo formats);
* JPEG.

Stored geometry is not always what the database says (padding, alignment,
firmware quirks), so decoding first infers the stored width/height from the
payload size, and format 1019 gets a repair pass for its interlaced layout.
"""

from __future__ import annotations

import io
import logging
import sys
from array import array
from typing import Any

from PIL import Image

from podsync.artwork.spec.renditions import default_stride_pixels_for_format
from podsync.artwork.spec.renditions import expected_size_bytes as _renditions_expected_size
from podsync.artwork.writer.records import EncodedFormatPayload
from podsync.hardware import ITHMB_FORMAT_MAP

__all__ = [
    "decode_pixels_for_format",
    "default_stride_pixels",
    "encode_image_for_format",
    "expected_size_bytes",
    "format_dimensions",
    "format_pixel_format",
]

logger = logging.getLogger(__name__)

_LANCZOS = Image.Resampling.LANCZOS
_BILINEAR = Image.Resampling.BILINEAR

# ── format lookup ───────────────────────────────────────────────────


def _format(format_id: int, fmt_override: Any = None):
    return fmt_override if fmt_override is not None else ITHMB_FORMAT_MAP.get(format_id)


def format_pixel_format(format_id: int, fmt_override: Any = None) -> str:
    fmt = _format(format_id, fmt_override)
    return fmt.pixel_format if fmt is not None else "UNKNOWN"


def format_dimensions(format_id: int, fallback_w: int, fallback_h: int, fmt_override: Any = None) -> tuple[int, int]:
    fmt = _format(format_id, fmt_override)
    return (int(fmt.width), int(fmt.height)) if fmt is not None else (int(fallback_w), int(fallback_h))


def default_stride_pixels(format_id: int, width: int, fmt_override: Any = None) -> int:
    return default_stride_pixels_for_format(_format(format_id, fmt_override), width)


def expected_size_bytes(format_id: int, width: int, height: int, stride_pixels: int | None = None,
                        fmt_override: Any = None) -> int:
    return _renditions_expected_size(format_id, width, height, stride_pixels, fmt_override)


def _clip_u8(value: float) -> int:
    return 0 if value < 0 else 255 if value > 255 else int(value)


def _log_excess(label: str, width: int, height: int, excess: int, needed: int, *, debug: bool = False) -> None:
    if excess > needed * 0.1:
        (logger.debug if debug else logger.warning)(
            "%s %dx%d: payload has %d extra bytes (%.1f%% padding), truncating",
            label, width, height, excess, excess / needed * 100 if needed else 0.0,
        )


# ── packed 16-bit RGB ───────────────────────────────────────────────


class _Packed16:
    """RGB565/RGB555 words, in the byte order the format names."""

    def __init__(self, pixel_format: str) -> None:
        self.pixel_format = pixel_format
        self.five_five_five = pixel_format in ("RGB555_LE", "RGB555_BE") or pixel_format.startswith("REC_RGB555")
        self.big_endian = pixel_format in ("RGB565_BE", "RGB565_BE_90", "RGB555_BE")
        self.rotated = pixel_format == "RGB565_BE_90"

    def _to_host_order(self, words: array) -> None:
        if self.big_endian != (sys.byteorder == "big"):
            words.byteswap()

    def pack(self, image: Image.Image, stride: int) -> bytes:
        """Pixels into a ``stride``-wide word grid (extra columns stay zero)."""
        width, height = image.size
        row = max(max(1, stride), width)
        rgb = image.convert("RGB").tobytes()
        words = array("H", bytes(2 * row * height))
        if self.five_five_five:
            to_word = lambda r, g, b: ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)  # noqa: E731
        else:
            to_word = lambda r, g, b: ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)  # noqa: E731
        source = 0
        for y in range(height):
            base = y * row
            for x in range(width):
                words[base + x] = to_word(rgb[source], rgb[source + 1], rgb[source + 2])
                source += 3
        self._to_host_order(words)
        return words.tobytes()

    def unpack(self, data: bytes) -> bytes:
        """Words back into 8-bit RGB triples (low bits filled by bit replication)."""
        words = array("H")
        words.frombytes(data)
        self._to_host_order(words)
        rgb = bytearray(len(words) * 3)
        for at, word in zip(range(0, len(rgb), 3), words):
            if self.five_five_five:
                r, g, b = (word >> 10) & 0x1F, (word >> 5) & 0x1F, word & 0x1F
                rgb[at:at + 3] = bytes(((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2)))
            else:
                r, g, b = (word >> 11) & 0x1F, (word >> 5) & 0x3F, word & 0x1F
                rgb[at:at + 3] = bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
        return bytes(rgb)

    def encode(self, base: Image.Image, width: int, height: int, stride: int) -> EncodedFormatPayload:
        oriented = base.transpose(Image.Transpose.ROTATE_270) if self.rotated else base
        data = self.pack(oriented, stride)
        return EncodedFormatPayload(data, width, height, len(data), stride, pixel_format=self.pixel_format)

    def decode(self, data: bytes, width: int, height: int, hpad: int, vpad: int, *, format_id, fmt_override):
        stored_w, stored_h = _stored_geometry(data, width, height, hpad, vpad, format_id=format_id,
                                              fmt_override=fmt_override)
        needed = stored_w * stored_h * 2
        if (stored_w, stored_h) == (0, 0) or len(data) < needed:
            return None
        _log_excess("RGB555" if self.five_five_five else "RGB565", stored_w, stored_h, len(data) - needed, needed)
        image = Image.frombytes("RGB", (stored_w, stored_h), self.unpack(data[:needed]))
        if self.rotated:
            image = image.transpose(Image.Transpose.ROTATE_90)
        return _visible_part(image, width, height, hpad, vpad, format_id=format_id, fmt_override=fmt_override)


# ── YUV ─────────────────────────────────────────────────────────────


def _to_yuv(image: Image.Image) -> tuple[list[int], list[float], list[float]]:
    """BT.601 studio-range Y (clipped ints) and U/V (clamped floats) per pixel."""
    rgb = image.convert("RGB").tobytes()
    luma: list[int] = []
    blue_diff: list[float] = []
    red_diff: list[float] = []
    for at in range(0, len(rgb), 3):
        r, g, b = rgb[at], rgb[at + 1], rgb[at + 2]
        luma.append(_clip_u8(0.257 * r + 0.504 * g + 0.098 * b + 16))
        blue_diff.append(min(255.0, max(0.0, -0.148 * r - 0.291 * g + 0.439 * b + 128)))
        red_diff.append(min(255.0, max(0.0, 0.439 * r - 0.368 * g - 0.071 * b + 128)))
    return luma, blue_diff, red_diff


def _to_rgb(y: float, u: float, v: float) -> bytes:
    c, d, e = y - 16, u - 128, v - 128
    return bytes((
        _clip_u8((298.082 * c + 408.583 * e) / 256),
        _clip_u8((298.082 * c - 100.291 * d - 208.120 * e) / 256),
        _clip_u8((298.082 * c + 516.412 * d) / 256),
    ))


class _Uyvy:
    """4:2:2 — each pixel pair shares U and V: ``U Y0 V Y1``."""

    pixel_format = "UYVY"

    def encode(self, base: Image.Image, width: int, height: int, stride: int) -> EncodedFormatPayload:
        if width % 2:
            base = base.resize((max(width - 1, 2), height), _LANCZOS)
            width = base.size[0]
        luma, u, v = _to_yuv(base)
        out = bytearray(width * height * 2)
        at = 0
        for y in range(height):
            row = y * width
            for pair in range(width // 2):
                left, right = row + 2 * pair, row + 2 * pair + 1
                out[at:at + 4] = bytes((int((u[left] + u[right]) / 2), luma[left], int((v[left] + v[right]) / 2), luma[right]))
                at += 4
        return EncodedFormatPayload(bytes(out), width, height, len(out), stride, pixel_format="UYVY")

    @staticmethod
    def _expand(data: bytes, width: int, height: int) -> Image.Image:
        rgb = bytearray(width * height * 3)
        at = 0
        for y in range(height):
            row = data[y * width * 2:(y + 1) * width * 2]
            for pair in range(width // 2):
                u, y0, v, y1 = row[4 * pair:4 * pair + 4]
                rgb[at:at + 6] = _to_rgb(y0, u, v) + _to_rgb(y1, u, v)
                at += 6
        return Image.frombytes("RGB", (width, height), bytes(rgb))

    def decode(self, data: bytes, width: int, height: int, hpad: int, vpad: int, *, format_id, fmt_override):
        stored_w, stored_h = _stored_geometry(data, width, height, hpad, vpad, format_id=format_id,
                                              fmt_override=fmt_override, even_width=True)
        needed = stored_w * stored_h * 2
        if (stored_w, stored_h) == (0, 0) or len(data) < needed:
            return None
        _log_excess("UYVY", stored_w, stored_h, len(data) - needed, needed)
        image = _visible_part(self._expand(data[:needed], stored_w, stored_h), width, height, hpad, vpad,
                              format_id=format_id, fmt_override=fmt_override)
        return _repair_1019(image) if format_id == 1019 else image


class _I420:
    """4:2:0 planar — full Y plane, then quarter-size U and V planes."""

    pixel_format = "I420_LE"

    def encode(self, base: Image.Image, width: int, height: int, stride: int) -> EncodedFormatPayload:
        even = (max(2, width & ~1), max(2, height & ~1))
        if even != (width, height):
            base = base.resize(even, _LANCZOS)
            width, height = even
        luma, u, v = _to_yuv(base)
        half_w = width // 2
        u_plane = bytearray(half_w * (height // 2))
        v_plane = bytearray(len(u_plane))
        for block_y in range(height // 2):
            for block_x in range(half_w):
                top = 2 * block_y * width + 2 * block_x
                quad = (top, top + 1, top + width, top + width + 1)
                u_plane[block_y * half_w + block_x] = int(sum(u[i] for i in quad) / 4)
                v_plane[block_y * half_w + block_x] = int(sum(v[i] for i in quad) / 4)
        data = bytes(luma) + bytes(u_plane) + bytes(v_plane)
        return EncodedFormatPayload(data, width, height, len(data), stride, pixel_format="I420_LE")

    def decode(self, data: bytes, width: int, height: int, hpad: int, vpad: int, *, format_id, fmt_override):
        width, height = width & ~1, height & ~1
        if width <= 0 or height <= 0:
            return None
        chroma = (width // 2) * (height // 2)
        needed = width * height + 2 * chroma
        if len(data) < needed:
            return None
        _log_excess("I420_LE", width, height, len(data) - needed, needed, debug=True)
        u_plane = data[width * height:width * height + chroma]
        v_plane = data[width * height + chroma:needed]
        rgb = bytearray(width * height * 3)
        at = 0
        for y in range(height):
            for x in range(width):
                c = (y // 2) * (width // 2) + (x // 2)
                rgb[at:at + 3] = _to_rgb(data[y * width + x], u_plane[c], v_plane[c])
                at += 3
        return Image.frombytes("RGB", (width, height), bytes(rgb))


class _Jpeg:
    pixel_format = "JPEG"

    def encode(self, base: Image.Image, width: int, height: int, stride: int) -> EncodedFormatPayload:
        buffer = io.BytesIO()
        base.save(buffer, format="JPEG", quality=92, optimize=False)
        data = buffer.getvalue()
        return EncodedFormatPayload(data, width, height, len(data), stride, pixel_format="JPEG")

    def decode(self, data: bytes, *_args, **_kwargs):
        try:
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None


def _codec_for(pixel_format: str):
    if pixel_format == "JPEG":
        return _Jpeg()
    if pixel_format == "UYVY":
        return _Uyvy()
    if pixel_format == "I420_LE":
        return _I420()
    if pixel_format in ("RGB565_BE", "RGB565_BE_90", "RGB555_BE", "RGB555_LE") or pixel_format.startswith("REC_RGB555"):
        return _Packed16(pixel_format)
    return _Packed16("RGB565_LE")  # the default for anything else that is known


# ── stored geometry ─────────────────────────────────────────────────


def _stored_geometry(
    data: bytes, width: int, height: int, hpad: int = 0, vpad: int = 0, *, format_id: int | None = None,
    fmt_override: Any = None, even_width: bool = False, use_format: bool = True,
) -> tuple[int, int]:
    """The ``(stored width, stored height)`` in 16-bit pixels that exactly explains ``len(data)``.

    Candidates (visible size, padded size, near misses, format table, alignment)
    are ranked; ``(0, 0)`` when nothing fits.
    """
    vis_w, vis_h = max(1, int(width)), max(1, int(height))
    pad_w, pad_h = max(0, int(hpad)), max(0, int(vpad))
    pixels = len(data) // 2
    if pixels <= 0:
        return 0, 0
    ranked: dict[tuple[int, int], int] = {}

    def consider(w: int, h: int, rank: int) -> None:
        if w > 0 and h > 0 and w * h == pixels and not (even_width and w % 2):
            ranked.setdefault((w, h), rank)

    consider(vis_w + pad_w, vis_h + pad_h, 0)
    consider(vis_w, vis_h, 1)
    for base_w, base_h, first_rank in ((vis_w + pad_w, vis_h + pad_h, 2), (vis_w, vis_h, 4)):
        for dw in range(-2, 3):
            for dh in range(-2, 3):
                consider(base_w + dw, base_h + dh, first_rank + abs(dw) + abs(dh))
    for known_h, rank in ((vis_h + pad_h, 2), (vis_h, 3)):
        if pixels % known_h == 0:
            consider(pixels // known_h, known_h, rank)
    for known_w, rank in ((vis_w + pad_w, 4), (vis_w, 5)):
        if pixels % known_w == 0:
            consider(known_w, pixels // known_w, rank)

    if use_format:
        fmt = _format(format_id, fmt_override) if format_id is not None else fmt_override
    else:
        fmt = None
    if fmt is not None:
        fmt_w, fmt_h = max(1, int(fmt.width)), max(1, int(fmt.height))
        stride = max(1, int(fmt.row_bytes) // 2 if int(fmt.row_bytes or 0) > 0 else fmt_w)
        consider(stride, fmt_h, 6)
        consider(fmt_w, fmt_h, 6)
        if pixels % stride == 0:
            consider(stride, pixels // stride, 7)
        if pixels % fmt_w == 0:
            consider(fmt_w, pixels // fmt_w, 8)
        if pixels % fmt_h == 0:
            consider(pixels // fmt_h, fmt_h, 9)
        for alignment in (2, 4, 8, 16):
            aligned = -(-vis_w // alignment) * alignment
            for bytes_per_pixel in (2, 4):
                if aligned * vis_h * bytes_per_pixel == len(data):
                    consider(aligned, vis_h, 1)
                if aligned * fmt_h * bytes_per_pixel == len(data):
                    consider(aligned, fmt_h, 1)

    if ranked:
        def order(item):
            (w, h), rank = item
            clipped = max(0, vis_w - w) + max(0, vis_h - h)
            pad_mismatch = abs((w - vis_w) - pad_w) + abs((h - vis_h) - pad_h)
            return (rank, clipped, pad_mismatch, w, h)

        return min(ranked.items(), key=order)[0]

    # A few trailing bytes past the format's full frame: retry without them.
    if fmt is not None and int(fmt.row_bytes or 0) > 0:
        frame = int(fmt.row_bytes) * int(fmt.height)
        if 0 < len(data) - frame <= 256:
            trimmed = _stored_geometry(data[:frame], width, height, hpad, vpad, format_id=format_id,
                                       fmt_override=fmt_override, even_width=even_width, use_format=False)
            if trimmed != (0, 0):
                return trimmed
    return 0, 0


def _visible_part(image: Image.Image, width: int, height: int, hpad: int, vpad: int, *,
                  format_id: int | None = None, fmt_override: Any = None) -> Image.Image:
    """Crop the stored frame to what should be shown (photo formats pad at the top/left)."""
    stored_w, stored_h = image.size
    left = top = 0
    shown_w, shown_h = min(max(1, width), stored_w), min(max(1, height), stored_h)
    fmt = _format(format_id, fmt_override) if format_id is not None else fmt_override
    padded_photo = fmt is not None and str(fmt.role).startswith("photo") and (hpad > 0 or vpad > 0)
    if padded_photo and (abs(stored_w - (width + hpad)) <= 2 or abs(stored_h - (height + vpad)) <= 2):
        left, top = min(max(hpad, 0), stored_w - 1), min(max(vpad, 0), stored_h - 1)
        inner_w, inner_h = width - hpad, height - vpad
        shown_w = min(stored_w - left, inner_w) if inner_w > 0 else min(stored_w - left, shown_w)
        shown_h = min(stored_h - top, inner_h) if inner_h > 0 else min(stored_h - top, shown_h)
    return image.crop((left, top, left + max(1, shown_w), top + max(1, shown_h)))


# ── format 1019 (TV-out) repair ─────────────────────────────────────


def _rows(image: Image.Image) -> list[bytes]:
    width, height = image.size
    raw = image.tobytes()
    return [raw[y * width * 3:(y + 1) * width * 3] for y in range(height)]


def _halves_alike(image: Image.Image) -> tuple[float, float]:
    """Mean and 95th-percentile byte difference between the top and bottom halves."""
    height = image.size[1]
    if height < 4 or height % 2:
        return 999.0, 999.0
    rows = _rows(image)
    diffs = [abs(a - b) for upper, lower in zip(rows[:height // 2], rows[height // 2:]) for a, b in zip(upper, lower)]
    if not diffs:
        return 999.0, 999.0
    diffs.sort()
    rank = 0.95 * (len(diffs) - 1)
    low = int(rank)
    high = min(low + 1, len(diffs) - 1)
    return sum(diffs) / len(diffs), float(diffs[low] + (diffs[high] - diffs[low]) * (rank - low))


def _spread(values: list[int]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


def _detail(image: Image.Image) -> float:
    width, height = image.size
    if width < 2 or height < 2:
        return 0.0
    rows = _rows(image)
    across = [row[i + 3] - row[i] for row in rows for i in range(len(row) - 3)]
    down = [b - a for upper, lower in zip(rows, rows[1:]) for a, b in zip(upper, lower)]
    return _spread(across) + _spread(down)


def _seam_ratio(image: Image.Image) -> float:
    """How much the middle row boundary jumps compared with an average row boundary."""
    height = image.size[1]
    if height < 3:
        return 1.0
    rows = _rows(image)
    adjacent = [abs(a - b) for upper, lower in zip(rows, rows[1:]) for a, b in zip(upper, lower)]
    average = sum(adjacent) / len(adjacent) if adjacent else 0.0
    if average <= 0:
        return 1.0
    seam = [abs(a - b) for a, b in zip(rows[height // 2 - 1], rows[height // 2])]
    return (sum(seam) / len(seam) if seam else 0.0) / average


def _weave_fields(top: Image.Image, bottom: Image.Image, swap: bool = False) -> Image.Image:
    """Interleave two half-height fields line by line."""
    width, half = top.size
    first, second = (bottom, top) if swap else (top, bottom)
    lines = [line for pair in zip(_rows(first), _rows(second)) for line in pair]
    return Image.frombytes("RGB", (width, half * 2), b"".join(lines))


def _looks_stacked(image: Image.Image) -> bool:
    mean, p95 = _halves_alike(image)
    return mean < 8.0 and p95 < 30.0


def _repair_1019(image: Image.Image) -> Image.Image:
    """Undo the field layouts seen in format 1019 dumps (stacked or interlaced halves)."""
    width, height = image.size
    if height < 120 or height % 2:
        return image
    top, bottom = image.crop((0, 0, width, height // 2)), image.crop((0, height // 2, width, height))
    if _looks_stacked(image):
        sharper = top if _detail(top) >= _detail(bottom) else bottom
        return sharper.resize((width, height), _BILINEAR)
    candidates = [
        image,
        _weave_fields(top, bottom),
        _weave_fields(top, bottom, swap=True),
        top.resize((width, height), _BILINEAR),
        bottom.resize((width, height), _BILINEAR),
    ]
    return min(candidates, key=lambda c: (1.0 if _looks_stacked(c) else 0.0, _seam_ratio(c), -_detail(c)))


_fix_1019_layout = _repair_1019  # name kept for diagnostics tooling


# ── public entry points ─────────────────────────────────────────────


def encode_image_for_format(
    source_img: Image.Image, format_id: int, target_width: int | None = None, target_height: int | None = None,
    fmt_override: Any = None,
) -> EncodedFormatPayload:
    """Resize *source_img* to the format's size and encode it in the format's pixel layout."""
    pixel_format = format_pixel_format(format_id, fmt_override)
    width, height = format_dimensions(
        format_id, target_width or source_img.width, target_height or source_img.height, fmt_override,
    )
    stride = default_stride_pixels(format_id, width, fmt_override)
    base = source_img.convert("RGB").resize((width, height), _LANCZOS)
    if pixel_format == "UNKNOWN":
        raise ValueError(f"Unsupported unknown pixel format for format_id={format_id}")
    return _codec_for(pixel_format).encode(base, width, height, stride)


def decode_pixels_for_format(
    format_id: int, pixel_bytes: bytes, width: int, height: int, hpad: int = 0, vpad: int = 0,
    fmt_override: Any = None,
) -> Image.Image | None:
    """Stored payload → visible image, or ``None`` if the payload cannot be explained."""
    pixel_format = format_pixel_format(format_id, fmt_override)
    decodable = (
        pixel_format in ("JPEG", "I420_LE", "UYVY", "RGB565_LE", "RGB565_BE", "RGB565_BE_90", "RGB555_LE", "RGB555_BE")
        or pixel_format.startswith("REC_RGB555")
    )
    if not decodable:
        return None
    return _codec_for(pixel_format).decode(
        bytes(pixel_bytes or b""), max(1, int(width)), max(1, int(height)), max(0, int(hpad)), max(0, int(vpad)),
        format_id=format_id, fmt_override=fmt_override,
    )
