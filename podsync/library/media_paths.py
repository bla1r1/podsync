"""Map iTunesDB ``location`` strings to files under ``iPod_Control/Music`` and back.

The database normally stores ``:iPod_Control:Music:F07:SONG.mp3``.  Imported or
legacy databases also carry slash paths, Windows drive paths and ``file://``
URIs; those are accepted only when they point inside ``iPod_Control/Music``.
Every resolved path goes through :func:`safe_device_path`, so traversal and
symlink escapes are refused.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from podsync.hardware.safety.paths import PathEscapeError, safe_device_path

__all__ = [
    "MusicFolderCache",
    "PathEscapeError",
    "expected_media_path",
    "find_media_file",
    "location_for_media_path",
]

logger = logging.getLogger(__name__)

MUSIC_ROOT = "iPod_Control/Music"
_MUSIC_PARTS = ("ipod_control", "music")


def _location_text(track_or_location: Any) -> str:
    if isinstance(track_or_location, Mapping):
        raw = track_or_location.get("location")
    else:
        raw = track_or_location
    return str(raw or "").strip()


def _drop_file_scheme(location: str) -> str:
    if location[:7].lower() == "file://":
        return unquote(urlparse(location).path).strip()
    return location


def _is_drive_path(text: str) -> bool:
    return len(text) >= 2 and text[0].isalpha() and text[1] == ":"


def _music_relative(location: str) -> str | None:
    """``iPod_Control/Music/...`` for any accepted spelling, else ``None``."""
    if not location or "\x00" in location:
        return None
    text = location.replace("\\", "/")
    if text.startswith("//"):  # UNC share
        return None
    drive = _is_drive_path(text)
    if ":" in text and not drive:  # classic colon-separated iPod location
        text = text.replace(":", "/")
    parts = text.split("/")
    anchor = next((i for i, part in enumerate(parts) if part.lower() == _MUSIC_PARTS[0]), None)
    if anchor is None:
        if text.startswith("/") or drive:  # host path with no iPod_Control inside
            return None
        tail = parts
    else:
        tail = parts[anchor:]
    if len(tail) < 2 or (tail[0].lower(), tail[1].lower()) != _MUSIC_PARTS:
        return None
    return "/".join([MUSIC_ROOT, *tail[2:]])


def _relative_for(track_or_location: Any) -> str | None:
    location = _drop_file_scheme(_location_text(track_or_location))
    return _music_relative(location) if location else None


def expected_media_path(ipod_root, track_or_location) -> Path | None:
    """Where the database says a track's file is, or ``None`` when that is unsafe/invalid."""
    if not ipod_root:
        return None
    relative = _relative_for(track_or_location)
    if relative is None:
        return None
    try:
        return safe_device_path(ipod_root, relative, allowed_subtree=MUSIC_ROOT)
    except PathEscapeError as exc:
        logger.debug("Refused media location %r: %s", relative, exc)
        return None


def _search_music_tree(ipod_root, filename: str) -> Path | None:
    """Exact filename match anywhere under Music, else the first stem match."""
    try:
        music = safe_device_path(ipod_root, MUSIC_ROOT, allowed_subtree=MUSIC_ROOT)
    except PathEscapeError:
        return None
    if not music.is_dir():
        return None
    root = Path(ipod_root).resolve(strict=False)
    wanted_name, wanted_stem = filename.lower(), Path(filename).stem.lower()
    stem_hit: Path | None = None
    for candidate in music.rglob("*"):
        try:
            checked = safe_device_path(
                root, candidate.relative_to(root).as_posix(), allowed_subtree=MUSIC_ROOT,
            )
        except (PathEscapeError, ValueError):
            continue
        if not checked.is_file():
            continue
        if checked.name.lower() == wanted_name:
            return checked
        if stem_hit is None and checked.stem.lower() == wanted_stem:
            stem_hit = checked
    return stem_hit


def find_media_file(
    ipod_root, track_or_location, *, allow_music_filename_fallback: bool = False,
) -> Path | None:
    """The track's file if it exists; optionally search Music by filename when it moved."""
    if not ipod_root:
        return None
    expected = expected_media_path(ipod_root, track_or_location)
    if expected is not None and expected.is_file():
        return expected
    if not allow_music_filename_fallback:
        return None
    if expected is not None:
        filename = expected.name
    else:
        relative = _relative_for(track_or_location)
        filename = Path(relative).name if relative else ""
    found = _search_music_tree(ipod_root, filename) if filename else None
    if found is not None:
        logger.debug("Located %s by filename fallback", found)
    return found


def location_for_media_path(ipod_root, file_path) -> str:
    """The colon location iTunesDB stores for a file under ``iPod_Control/Music``."""
    root = Path(ipod_root).resolve(strict=False)
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.resolve(strict=False).relative_to(root)
    except ValueError:
        raise PathEscapeError(
            f"Track path is outside the iPod music directory: {file_path!s}"
        ) from None
    checked = safe_device_path(root, relative.as_posix(), allowed_subtree=MUSIC_ROOT)
    return ":" + ":".join(checked.relative_to(root).parts)


class MusicFolderCache:
    """Resolve many ``Fxx/file`` locations while validating each Music folder only once."""

    def __init__(self, ipod_root) -> None:
        self._root = ipod_root
        self._folders: dict[str, Path | None] = {}

    def _folder(self, name: str) -> Path | None:
        if name not in self._folders:
            try:
                self._folders[name] = safe_device_path(
                    self._root, f"{MUSIC_ROOT}/{name}", allowed_subtree=MUSIC_ROOT,
                )
            except PathEscapeError:
                self._folders[name] = None
        return self._folders[name]

    def existing_regular_file(self, track_or_location) -> tuple[Path, os.stat_result] | None:
        """``(path, lstat)`` for a regular file at ``Music/<folder>/<file>``, else ``None``."""
        relative = _relative_for(track_or_location)
        if relative is None:
            return None
        parts = relative.split("/")
        if len(parts) != 4 or any(p in ("", ".", "..") or ":" in p for p in parts):
            return None
        folder = self._folder(parts[2])
        if folder is None:
            return None
        candidate = folder / parts[3]
        try:
            info = candidate.lstat()
        except OSError:
            return None
        return (candidate, info) if stat.S_ISREG(info.st_mode) else None
