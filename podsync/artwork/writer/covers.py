"""Find cover art for a source file: embedded tags, then a cover image in the same folder.

Embedded art is read with the optional ``mutagen`` package.  Videos only yield
the embedded ``covr`` of MP4-family containers — no frames are grabbed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "MUTAGEN_AVAILABLE", "art_hash", "extract_art", "extract_art_with_folder", "extract_art_with_source",
    "find_folder_art",
]

logger = logging.getLogger(__name__)

try:
    import mutagen  # type: ignore[import-not-found]  # noqa: F401

    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False
    logger.warning("mutagen not installed - art extraction disabled")

_FOLDER_ART_NAMES = ("cover", "folder", "album", "front", "artwork", "thumb")
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp")
_VIDEO_EXTENSIONS = frozenset({
    ".m4v", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".mpg", ".mpeg",
    ".3gp", ".3g2", ".flv", ".mts", ".m2ts", ".ts", ".ogv",
})
_MP4_VIDEO = frozenset({".m4v", ".mp4", ".mov"})


def _apic(tags) -> bytes | None:
    """Data of the first ID3 ``APIC`` frame."""
    for key in list(tags.keys()) if tags else ():
        if str(key).startswith("APIC"):
            data = getattr(tags[key], "data", None)
            if data:
                return bytes(data)
    return None


def _from_id3(path: str) -> bytes | None:
    from mutagen.id3 import ID3  # type: ignore[import-not-found]

    try:
        return _apic(ID3(path))
    except Exception:
        return None


def _from_mp4(path: str) -> bytes | None:
    from mutagen.mp4 import MP4  # type: ignore[import-not-found]

    tags = MP4(path).tags
    covers = (tags or {}).get("covr") if tags is not None else None
    return bytes(covers[0]) if covers else None


def _from_flac(path: str) -> bytes | None:
    from mutagen.flac import FLAC  # type: ignore[import-not-found]

    pictures = FLAC(path).pictures
    return bytes(pictures[0].data) if pictures else None


def _from_vorbis_comment(audio) -> bytes | None:
    from mutagen.flac import Picture  # type: ignore[import-not-found]

    for encoded in (audio.tags or {}).get("metadata_block_picture", []) or []:
        try:
            return bytes(Picture(base64.b64decode(encoded)).data)
        except Exception:
            continue
    return None


def _from_ogg(path: str) -> bytes | None:
    from mutagen.oggvorbis import OggVorbis  # type: ignore[import-not-found]

    return _from_vorbis_comment(OggVorbis(path))


def _from_opus(path: str) -> bytes | None:
    from mutagen.oggopus import OggOpus  # type: ignore[import-not-found]

    return _from_vorbis_comment(OggOpus(path))


def _from_aiff(path: str) -> bytes | None:
    from mutagen.aiff import AIFF  # type: ignore[import-not-found]

    return _apic(AIFF(path).tags)


def _from_anything(path: str) -> bytes | None:
    import mutagen as _mutagen  # type: ignore[import-not-found]

    audio = _mutagen.File(path)
    if audio is None or audio.tags is None:
        return None
    found = _apic(audio.tags)
    if found:
        return found
    covers = audio.tags.get("covr") if hasattr(audio.tags, "get") else None
    return bytes(covers[0]) if covers else None


_BY_EXTENSION: dict[str, Callable[[str], bytes | None]] = {
    ".mp3": _from_id3,
    **dict.fromkeys((".m4a", ".m4p", ".m4b", ".aac", ".alac"), _from_mp4),
    ".flac": _from_flac,
    ".ogg": _from_ogg,
    ".opus": _from_opus,
    ".aif": _from_aiff,
    ".aiff": _from_aiff,
}


def extract_art(file_path: str) -> bytes | None:
    """Cover bytes embedded in (or equal to, for images) *file_path*; ``None`` if absent."""
    try:
        ext = Path(file_path).suffix.lower()
        if ext in _IMAGE_EXTENSIONS:
            return Path(file_path).read_bytes()
        if ext in _VIDEO_EXTENSIONS:
            return _from_mp4(file_path) if ext in _MP4_VIDEO and MUTAGEN_AVAILABLE else None
        if not MUTAGEN_AVAILABLE:
            return None
        return _BY_EXTENSION.get(ext, _from_anything)(file_path)
    except UnicodeError as exc:
        logger.debug("ART: Could not parse embedded art from %s: %s", file_path, exc)
        return None
    except Exception as exc:
        logger.warning("ART: Failed to extract art from %s: %s", file_path, exc)
        return None


def find_folder_art(file_path: str) -> str | None:
    """A conventionally named cover image next to *file_path* (case-insensitive)."""
    folder = os.path.dirname(os.path.abspath(file_path))
    try:
        present = {name.lower(): name for name in os.listdir(folder)}
    except OSError:
        return None
    for stem in _FOLDER_ART_NAMES:
        for ext in _IMAGE_EXTENSIONS:
            if stem + ext in present:
                return os.path.join(folder, present[stem + ext])
    return None


def extract_art_with_source(file_path: str) -> tuple[bytes | None, str | None]:
    """``(cover bytes, where they came from)``; embedded art wins over folder art."""
    embedded = extract_art(file_path)
    if embedded:
        return embedded, file_path
    folder_image = find_folder_art(file_path)
    if folder_image:
        try:
            return Path(folder_image).read_bytes(), folder_image
        except OSError:
            pass
    return None, None


def extract_art_with_folder(file_path: str) -> bytes | None:
    return extract_art_with_source(file_path)[0]


def art_hash(art_bytes: bytes) -> str:
    """Content identity of cover bytes (not a security hash)."""
    return hashlib.md5(art_bytes).hexdigest()
