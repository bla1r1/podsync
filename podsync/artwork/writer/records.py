"""What the artwork writer moves around: stored renditions, encoded payloads and entries."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Union

__all__ = [
    "ArtworkEntry",
    "ArtworkFormatPayload",
    "ArtworkPayload",
    "EncodedFormatPayload",
    "ExistingFormatRef",
    "IthmbLocation",
    "PassthroughFormatRef",
]


@dataclass(frozen=True)
class IthmbLocation:
    """Where a rendition sits: ``.ithmb`` file name and byte offset."""

    filename: str
    offset: int


@dataclass(frozen=True)
class _StoredRendition:
    """A rendition that already exists in an ``.ithmb`` file on the device."""

    path: str
    ithmb_offset: int
    size: int
    width: int
    height: int
    hpad: int = 0
    vpad: int = 0
    ithmb_filename: str = ""

    @property
    def stride_pixels(self) -> int:
        return max(1, self.width + self.hpad)


@dataclass(frozen=True)
class ExistingFormatRef(_StoredRendition):
    """A stored rendition as read from the current ArtworkDB."""

    @property
    def stored_height(self) -> int:
        return max(1, self.height + self.vpad)


@dataclass(frozen=True)
class PassthroughFormatRef(_StoredRendition):
    """A stored rendition that is referenced again in place, without copying its bytes."""

    @classmethod
    def from_existing_ref(cls, ref: ExistingFormatRef) -> PassthroughFormatRef:
        return cls(**{f.name: getattr(ref, f.name) for f in fields(_StoredRendition)})


@dataclass(frozen=True)
class EncodedFormatPayload:
    """Pixel bytes ready to be appended to an ``.ithmb`` file."""

    data: bytes
    width: int
    height: int
    size: int
    stride_pixels: int
    hpad: int = 0
    vpad: int = 0
    pixel_format: str | None = None

    @classmethod
    def from_existing_ref(cls, ref: ExistingFormatRef, data: bytes) -> EncodedFormatPayload:
        """Bytes read back from a stored rendition, to be rewritten into a new file."""
        return cls(
            data=bytes(data), width=ref.width, height=ref.height, size=ref.size,
            stride_pixels=ref.stride_pixels, hpad=ref.hpad, vpad=ref.vpad,
        )


ArtworkFormatPayload = Union[EncodedFormatPayload, PassthroughFormatRef]


@dataclass
class ArtworkPayload:
    """All renditions of one picture, keyed by format id."""

    formats: dict[int, ArtworkFormatPayload] = field(default_factory=dict)
    src_img_size: int = 0


@dataclass
class ArtworkEntry:
    """One ``mhii``: a track's picture and where each rendition lives."""

    img_id: int
    db_track_id: int
    art_hash: str | None
    src_img_size: int
    formats: dict[int, ArtworkFormatPayload] = field(default_factory=dict)
    db_track_ids: list[int] = field(default_factory=list)
