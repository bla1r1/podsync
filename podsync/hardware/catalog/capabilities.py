"""What each iPod model can do: signature scheme, media kinds, artwork formats, database limits.

Models are described by small profiles (``_CLASSIC``, ``_VIDEO_5G`` …) combined
per family and generation.  Lookups accept loose spellings and, when only the
family is known, answer if every generation of that family agrees.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TypeVar

from podsync.hardware.catalog.artwork_presets import ARTWORK_FORMATS_BY_ID, NANO_7G_COVER_ART_OVERRIDES, ArtworkFormat
from podsync.hardware.catalog.checksum import SignatureKind
from podsync.hardware.catalog.models import canonicalize_model_identity

__all__ = ["ModelTraits", "checksum_type_for_family_gen", "cover_art_formats_for_family_gen", "traits_for_model"]

_MIB = 1024 * 1024
_DEFAULT_MAX_DATABASE_BYTES = 32 * _MIB
_LARGE_MAX_DATABASE_BYTES = 64 * _MIB
# 5G/5.5G units with the 64 MiB database allowance despite a small capacity label.
_HIGH_MEMORY_VIDEO_MODELS = frozenset({"MA003", "MA147", "MA448", "MA450"})


@dataclass(frozen=True)
class ModelTraits:
    """What a model supports and how its database must be written."""

    checksum: SignatureKind = SignatureKind.NONE
    is_shuffle: bool = False
    shadow_db_version: int = 0
    supports_compressed_db: bool = False
    supports_video: bool = False
    supports_tx3g_subtitles: bool = False
    supports_cea608_captions: bool = False
    supports_podcast: bool = True
    supports_gapless: bool = False
    supports_artwork: bool = True
    supports_photo: bool = False
    photo_formats: tuple[ArtworkFormat, ...] = ()
    supports_chapter_image: bool = False
    supports_sparse_artwork: bool = False
    supports_alac: bool = True
    cover_art_formats: tuple[ArtworkFormat, ...] = ()
    music_dirs: int = 20
    max_database_bytes: int = _DEFAULT_MAX_DATABASE_BYTES
    uses_sqlite_db: bool = False
    db_version: int = 0x30
    byte_order: str = "le"
    has_screen: bool = True
    max_video_width: int = 0
    max_video_height: int = 0
    max_video_fps: int = 30
    max_video_bitrate: int = 0
    h264_level: str = "3.0"


def _formats(*ids: int) -> tuple[ArtworkFormat, ...]:
    return tuple(ARTWORK_FORMATS_BY_ID[fid] for fid in ids)


def _traits(*profiles: dict, **overrides) -> ModelTraits:
    merged: dict = {}
    for profile in profiles:
        merged.update(profile)
    return ModelTraits(**{**merged, **overrides})


# ── profiles ────────────────────────────────────────────────────────

_EARLY_IPOD = dict(supports_podcast=False, supports_artwork=False, db_version=0x13)
_MINI = dict(supports_artwork=False, music_dirs=6, db_version=0x13)
_PHOTO_4G = dict(supports_photo=True, photo_formats=_formats(1009, 1013, 1015, 1019),
                 cover_art_formats=_formats(1017, 1016), db_version=0x13)
_VIDEO_5G = dict(supports_video=True, supports_photo=True, photo_formats=_formats(1036, 1024, 1015, 1019),
                 cover_art_formats=_formats(1028, 1029), db_version=0x19, max_video_width=640, max_video_height=480)
_NANO_EARLY = dict(supports_photo=True, photo_formats=_formats(1032, 1023), cover_art_formats=_formats(1031, 1027),
                   music_dirs=14, db_version=0x13)
# Everything from the Classic / nano 3G on: signed database, H.264 video with subtitles, gapless, sparse art.
_MODERN = dict(supports_video=True, supports_tx3g_subtitles=True, supports_cea608_captions=True,
               supports_gapless=True, supports_photo=True, supports_sparse_artwork=True)
_CLASSIC = dict(_MODERN, checksum=SignatureKind.HASH58, photo_formats=_formats(1067, 1024, 1066),
                supports_chapter_image=True, cover_art_formats=_formats(1055, 1060, 1061, 1068), music_dirs=50,
                max_database_bytes=_LARGE_MAX_DATABASE_BYTES, max_video_width=640, max_video_height=480,
                max_video_bitrate=2500)
_SQLITE_ERA = dict(supports_compressed_db=True, supports_gapless=True, supports_photo=True,
                   supports_sparse_artwork=True, max_database_bytes=_LARGE_MAX_DATABASE_BYTES, uses_sqlite_db=True)


def _shuffle(shadow: int, db_version: int) -> ModelTraits:
    return ModelTraits(is_shuffle=True, shadow_db_version=shadow, supports_artwork=False, music_dirs=3,
                       db_version=db_version, has_screen=False)


_FAMILY_GEN_CAPABILITIES: dict[tuple[str, str], ModelTraits] = {
    **{("iPod", gen): _traits(_EARLY_IPOD) for gen in ("1st Gen", "2nd Gen", "3rd Gen")},
    ("iPod", "4th Gen (mono)"): _traits(supports_artwork=False, db_version=0x13),
    ("iPod", "4th Gen (photo)"): _traits(_PHOTO_4G),
    ("iPod", "4th Gen (color)"): _traits(_PHOTO_4G),
    ("iPod", "5th Gen"): _traits(_VIDEO_5G),
    ("iPod", "5.5th Gen"): _traits(_VIDEO_5G, supports_gapless=True),
    **{("iPod Classic", gen): _traits(_CLASSIC) for gen in ("6th Gen", "6.5th Gen", "7th Gen")},
    **{("iPod Mini", gen): _traits(_MINI) for gen in ("1st Gen", "2nd Gen")},
    **{("iPod Nano", gen): _traits(_NANO_EARLY) for gen in ("1st Gen", "2nd Gen")},
    ("iPod Nano", "3rd Gen"): _traits(
        _MODERN, checksum=SignatureKind.HASH58, photo_formats=_formats(1067, 1024, 1066),
        cover_art_formats=_formats(1061, 1055, 1068, 1060), max_video_width=320, max_video_height=240,
        max_video_bitrate=768, h264_level="1.3",
    ),
    ("iPod Nano", "4th Gen"): _traits(
        _MODERN, checksum=SignatureKind.HASH58, photo_formats=_formats(1024, 1066, 1079, 1083),
        supports_chapter_image=True, cover_art_formats=_formats(1055, 1068, 1071, 1074, 1078, 1084),
        max_video_width=480, max_video_height=320, max_video_bitrate=768, h264_level="1.3",
    ),
    ("iPod Nano", "5th Gen"): _traits(
        _MODERN, _SQLITE_ERA, checksum=SignatureKind.HASH72, photo_formats=_formats(1087, 1079, 1066),
        cover_art_formats=_formats(1056, 1078, 1073, 1074), music_dirs=14, max_video_width=640, max_video_height=480,
    ),
    ("iPod Nano", "6th Gen"): _traits(
        _SQLITE_ERA, checksum=SignatureKind.HASHAB, photo_formats=_formats(1092, 1093),
        cover_art_formats=_formats(1073, 1085, 1089, 1074),
    ),
    ("iPod Nano", "7th Gen"): _traits(
        _MODERN, _SQLITE_ERA, checksum=SignatureKind.HASHAB, photo_formats=_formats(1007, 1005),
        cover_art_formats=NANO_7G_COVER_ART_OVERRIDES, max_video_width=720, max_video_height=576,
    ),
    ("iPod Shuffle", "1st Gen"): _shuffle(1, 0xC),
    ("iPod Shuffle", "2nd Gen"): _shuffle(1, 0x13),
    ("iPod Shuffle", "3rd Gen"): _shuffle(2, 0x19),
    ("iPod Shuffle", "4th Gen"): _shuffle(2, 0x19),
}

# ── lookups ─────────────────────────────────────────────────────────

T = TypeVar("T")


def _lookup(family: str, generation: str, pick: Callable[[ModelTraits], T], *, capacity: str | None = None,
            model_number: str | None = None) -> tuple[T | None, str, str]:
    """``pick(traits)`` for the exact model, or the value every generation of the family shares."""
    family, generation, _color = canonicalize_model_identity(
        family, generation, capacity=capacity or "", model_number=model_number,
    )
    exact = _FAMILY_GEN_CAPABILITIES.get((family, generation))
    if exact is not None:
        return pick(exact), family, generation
    if not generation:
        answers = [pick(traits) for (fam, _gen), traits in _FAMILY_GEN_CAPABILITIES.items() if fam == family]
        if answers and all(answer == answers[0] for answer in answers):
            return answers[0], family, generation
    return None, family, generation


def _capacity_gb(capacity: str | None) -> int:
    match = re.search(r"\d+", str(capacity or ""))
    return int(match.group(0)) if match else 0


def traits_for_model(family: str, generation: str, *, capacity: str | None = None,
                     model_number: str | None = None) -> ModelTraits | None:
    """Traits of a model; large 5G/5.5G units get the 64 MiB database allowance."""
    traits, family, generation = _lookup(family, generation, lambda t: t, capacity=capacity, model_number=model_number)
    if traits is None:
        return None
    if family == "iPod" and generation in {"5th Gen", "5.5th Gen"}:
        if _capacity_gb(capacity) >= 60 or str(model_number or "").strip().upper() in _HIGH_MEMORY_VIDEO_MODELS:
            return replace(traits, max_database_bytes=_LARGE_MAX_DATABASE_BYTES)
    return traits


def cover_art_formats_for_family_gen(family: str, generation: str, *, capacity: str | None = None,
                                     model_number: str | None = None) -> tuple[ArtworkFormat, ...]:
    """Cover-art formats of a model (``()`` when unknown or without artwork)."""
    formats, _f, _g = _lookup(
        family, generation, lambda t: t.cover_art_formats if t.supports_artwork else (),
        capacity=capacity, model_number=model_number,
    )
    return formats or ()


def checksum_type_for_family_gen(family: str, generation: str) -> SignatureKind | None:
    """Signature kind a model needs, or ``None`` when it cannot be decided."""
    return _lookup(family, generation, lambda t: t.checksum)[0]
