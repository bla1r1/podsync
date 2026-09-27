"""Which cover-art (and photo) formats a device uses.

The model catalog gives the expected formats; formats actually observed on the
device (from SysInfoExtended) take precedence, and an observed format the
catalog does not know is assumed to be plain RGB565.
"""

from __future__ import annotations

from typing import Any

from podsync.hardware.catalog.artwork_presets import ARTWORK_FORMATS_BY_ID, ArtworkFormat
from podsync.hardware.catalog.capabilities import cover_art_formats_for_family_gen, traits_for_model

__all__ = [
    "ITHMB_FORMAT_MAP", "ITHMB_SIZE_MAP", "cover_art_format_definitions_for_device", "ithmb_formats_for_device",
    "photo_formats_for_device", "resolve_cover_art_format_definitions",
    "resolve_cover_art_format_definitions_for_device",
]

ITHMB_FORMAT_MAP: dict[int, ArtworkFormat] = ARTWORK_FORMATS_BY_ID

# Rendition byte size → the first format with that size (used to recognise stray payloads).
ITHMB_SIZE_MAP: dict[int, ArtworkFormat] = {}
for _fmt in ITHMB_FORMAT_MAP.values():
    if _fmt.row_bytes * _fmt.height > 0:
        ITHMB_SIZE_MAP.setdefault(_fmt.row_bytes * _fmt.height, _fmt)
del _fmt


def _by_id(formats) -> dict[int, ArtworkFormat]:
    return {fmt.format_id: fmt for fmt in formats}


def cover_art_format_definitions_for_device(family: str, generation: str, *, capacity: str | None = None,
                                            model_number: str | None = None) -> dict[int, ArtworkFormat]:
    """``{format id: ArtworkFormat}`` the catalog lists for a model."""
    traits = traits_for_model(family, generation, capacity=capacity, model_number=model_number)
    if traits is not None:
        return _by_id(traits.cover_art_formats) if traits.supports_artwork else {}
    return _by_id(cover_art_formats_for_family_gen(family, generation, capacity=capacity, model_number=model_number))


def ithmb_formats_for_device(family: str, generation: str, *, capacity: str | None = None,
                             model_number: str | None = None) -> dict[int, tuple[int, int]]:
    """``{format id: (width, height)}`` of a model's cover-art formats."""
    definitions = cover_art_format_definitions_for_device(family, generation, capacity=capacity, model_number=model_number)
    return {fid: (fmt.width, fmt.height) for fid, fmt in definitions.items()}


def _observed_definition(fid: int, width: int, height: int, expected: dict[int, ArtworkFormat]) -> ArtworkFormat:
    for candidate in (expected.get(fid), ITHMB_FORMAT_MAP.get(fid)):
        if candidate is not None and (candidate.width, candidate.height) == (width, height):
            return candidate
    return ArtworkFormat(fid, width, height, width * 2, "RGB565_LE", "cover", f"Device artwork format {fid}")


def resolve_cover_art_format_definitions(family: str = "", generation: str = "", *, capacity: str | None = None,
                                         model_number: str | None = None,
                                         observed_formats: dict[int, tuple[int, int]] | None = None) -> dict[int, ArtworkFormat]:
    """Catalog formats, or — when the device reported its own — definitions for exactly those."""
    expected = cover_art_format_definitions_for_device(family, generation, capacity=capacity, model_number=model_number)
    if not observed_formats:
        return expected
    resolved: dict[int, ArtworkFormat] = {}
    for raw_id, size in observed_formats.items():
        try:
            fid, width, height = int(raw_id), int(size[0]), int(size[1])
        except (TypeError, ValueError, IndexError):
            continue
        resolved[fid] = _observed_definition(fid, width, height, expected)
    return resolved


def resolve_cover_art_format_definitions_for_device(device: Any) -> dict[int, ArtworkFormat]:
    """Cover-art formats for an :class:`IpodDevice`."""
    if device is None:
        return {}
    return resolve_cover_art_format_definitions(
        getattr(device, "model_family", "") or "", getattr(device, "generation", "") or "",
        capacity=getattr(device, "capacity", None) or None,
        model_number=getattr(device, "model_number", None) or None,
        observed_formats=getattr(device, "artwork_formats", None) or None,
    )


def photo_formats_for_device(family: str, generation: str, *, capacity: str | None = None,
                             model_number: str | None = None) -> dict[int, ArtworkFormat]:
    """``{format id: ArtworkFormat}`` of a model's photo formats."""
    traits = traits_for_model(family, generation, capacity=capacity, model_number=model_number)
    return _by_id(traits.photo_formats) if traits is not None else {}
