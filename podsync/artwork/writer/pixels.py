"""Image loading, device format tables and the plain RGB565 helper.

RGB565 is 16 bits per pixel — 5 red, 6 green, 5 blue — little-endian, the
layout most Classic/nano cover-art formats use.
"""

from __future__ import annotations

import io
import logging
import sys
from array import array
from typing import Any

from PIL import Image

from podsync.artwork.spec.files import extract_format_ids
from podsync.hardware import ITHMB_FORMAT_MAP, ithmb_formats_for_device

__all__ = [
    "ALL_KNOWN_FORMATS", "IPOD_4G_PHOTO_FORMATS", "IPOD_5G_FORMATS", "IPOD_CLASSIC_FORMATS",
    "IPOD_NANO_1G2G_FORMATS", "IPOD_NANO_4G_FORMATS", "IPOD_NANO_5G_FORMATS", "IPOD_STRIDE_OVERRIDE",
    "convert_art_for_ipod", "get_artwork_format_definitions", "get_artwork_formats", "image_from_bytes",
    "resize_for_format", "rgb888_to_rgb565",
]

logger = logging.getLogger(__name__)

ALL_KNOWN_FORMATS: dict[int, tuple[int, int]] = {fid: (fmt.width, fmt.height) for fid, fmt in ITHMB_FORMAT_MAP.items()}
IPOD_CLASSIC_FORMATS = ithmb_formats_for_device("iPod Classic", "6th Gen")
IPOD_NANO_1G2G_FORMATS = ithmb_formats_for_device("iPod Nano", "1st Gen")
IPOD_4G_PHOTO_FORMATS = ithmb_formats_for_device("iPod", "4th Gen (photo)")
IPOD_5G_FORMATS = ithmb_formats_for_device("iPod", "5th Gen")
IPOD_NANO_4G_FORMATS = ithmb_formats_for_device("iPod Nano", "4th Gen")
IPOD_NANO_5G_FORMATS = ithmb_formats_for_device("iPod Nano", "5th Gen")
IPOD_STRIDE_OVERRIDE: dict[int, int] = {}


def get_artwork_format_definitions(ipod_path: str) -> dict[int, Any]:
    """Cover-art formats of the device currently selected for *ipod_path*."""
    import podsync.hardware as hardware

    return hardware.resolve_cover_art_format_definitions_for_device(hardware.selected_device_at(ipod_path))


def get_artwork_formats(ipod_path: str) -> dict[int, tuple[int, int]]:
    """``{format id: (width, height)}`` for the device, or ``{}`` — never a guess."""
    definitions = get_artwork_format_definitions(ipod_path)
    if definitions:
        sizes = {fid: (fmt.width, fmt.height) for fid, fmt in definitions.items()}
        logger.info("ART: using resolved format definitions: %s", sizes)
        return sizes
    import podsync.hardware as hardware

    device = hardware.selected_device_at(ipod_path)
    logger.warning(
        "ART: no artwork definitions available for device %s %s at %s; refusing to guess an unrelated device format",
        getattr(device, "model_family", "unknown") if device else "unknown",
        getattr(device, "generation", "") if device else "", ipod_path,
    )
    return {}


def image_from_bytes(art_bytes: bytes, *, source_path: str = "") -> Image.Image | None:
    """Decode image bytes to RGB; ``None`` if unreadable.  Decompression bombs raise ``ValueError``."""
    try:
        picture = Image.open(io.BytesIO(art_bytes))
        return picture if picture.mode == "RGB" else picture.convert("RGBA").convert("RGB")
    except Image.DecompressionBombError as exc:
        detail = f"Artwork image exceeds Pillow safety limit: {exc}"
        raise ValueError(detail + (f" Offending image: {source_path}" if source_path else "")) from exc
    except Exception:
        return None


def resize_for_format(img: Image.Image, format_id: int) -> Image.Image:
    if format_id not in ALL_KNOWN_FORMATS:
        raise ValueError(f"Unknown format ID: {format_id}")
    return img.resize(ALL_KNOWN_FORMATS[format_id], Image.Resampling.LANCZOS)


def rgb888_to_rgb565(img: Image.Image, format_width: int, format_height: int, stride: int | None = None) -> bytes:
    """Little-endian RGB565 words, rows padded to *stride* pixels."""
    assert img.size == (format_width, format_height), f"image is {img.size}, expected {(format_width, format_height)}"
    row = max(stride or format_width, format_width)
    rgb = img.convert("RGB").tobytes()
    words = array("H", bytes(row * format_height * 2))
    source = 0
    for y in range(format_height):
        for x in range(format_width):
            r, g, b = rgb[source:source + 3]
            words[y * row + x] = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            source += 3
    if sys.byteorder == "big":
        words.byteswap()
    return words.tobytes()


def convert_art_for_ipod(art_bytes: bytes, format_id: int) -> dict | None:
    """Resize and pack cover art for one known RGB565 format."""
    width, height = ALL_KNOWN_FORMATS[format_id]
    picture = image_from_bytes(art_bytes)
    if picture is None:
        return None
    data = rgb888_to_rgb565(resize_for_format(picture, format_id), width, height, IPOD_STRIDE_OVERRIDE.get(format_id, width))
    return {"data": data, "width": width, "height": height, "size": len(data), "format_width": width, "format_height": height}


def _extract_format_ids(data: bytes) -> list[int]:
    return extract_format_ids(data)
