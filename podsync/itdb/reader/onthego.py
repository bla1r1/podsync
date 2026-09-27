"""On-The-Go playlists the user built on the iPod (``OTGPlaylistInfo``, ``_1``, ``_2`` ...).

Each file (``mhpo``, or byte-swapped ``ohpm``) lists indexes into the database
track list.  Playlists get a stable id derived from the file contents so a
re-read does not create duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from typing import Any

__all__ = ["delete_otg_files", "load_onthego_playlists"]

logger = logging.getLogger(__name__)

_BASE_NAME = "OTGPlaylistInfo"
_MAX_NUMBERED = 19
_BYTE_ORDER = {b"mhpo": "<", b"ohpm": ">"}
_MIN_HEADER = 0x14


def _otg_files(itunes_dir: str) -> list[str]:
    """The base file followed by consecutive numbered siblings (stops at the first gap)."""
    first = os.path.join(itunes_dir, _BASE_NAME)
    if not os.path.exists(first):
        return []
    found = [first]
    for number in range(1, _MAX_NUMBERED + 1):
        candidate = os.path.join(itunes_dir, f"{_BASE_NAME}_{number}")
        if not os.path.exists(candidate):
            break
        found.append(candidate)
    return found


def _read_playlist(path: str, ordinal: int, tracks: list) -> dict[str, Any] | None:
    name = os.path.basename(path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        logger.warning("OTG: could not read %s: %s", path, exc)
        return None
    if len(raw) < _MIN_HEADER:
        logger.warning("OTG: %s too short (%d B)", name, len(raw))
        return None
    order = _BYTE_ORDER.get(raw[:4])
    if order is None:
        logger.warning("OTG: %s has unrecognised magic %r — skipping", name, raw[:4])
        return None
    header_length, entry_length, entry_count = struct.unpack_from(f"{order}III", raw, 4)
    if header_length < _MIN_HEADER:
        logger.warning("OTG: %s header_len %d < 20 — skipping", name, header_length)
        return None
    if entry_length < 4:
        logger.warning("OTG: %s entry_len %d < 4 — skipping", name, entry_length)
        return None

    items: list[dict[str, int]] = []
    for index in range(entry_count):
        at = header_length + index * entry_length
        if at + 4 > len(raw):
            logger.warning("OTG: %s entry %d extends past EOF — truncating", name, index)
            break
        (position,) = struct.unpack_from(f"{order}I", raw, at)
        if position >= len(tracks):
            logger.warning(
                "OTG: %s entry %d references track index %d but track list has only %d entries — skipping entry",
                name, index, position, len(tracks),
            )
            continue
        track = tracks[position]
        track_id = track.get("track_id") if isinstance(track, dict) else None
        if track_id:
            items.append({"track_id": int(track_id)})
    if not items:
        return None

    title = f"On-The-Go {ordinal}"
    logger.info("OTG: imported '%s' (%d tracks) from %s", title, len(items), name)
    return {
        "title": title,
        "items": items,
        "playlist_id": int.from_bytes(hashlib.md5(raw).digest()[:8], "little"),
    }


def load_onthego_playlists(itunes_dir: str, track_list: list) -> list[dict[str, Any]]:
    """Playlist rows (``title``, ``items``, ``playlist_id``) for every readable OTG file."""
    rows = (_read_playlist(path, n, track_list) for n, path in enumerate(_otg_files(itunes_dir), 1))
    return [row for row in rows if row is not None]


def delete_otg_files(itunes_dir: str) -> None:
    """Remove the base OTG file (numbered siblings belong to the firmware)."""
    target = os.path.join(itunes_dir, _BASE_NAME)
    if not os.path.exists(target):
        return
    try:
        os.remove(target)
    except OSError as exc:
        logger.warning("Could not delete OTGPlaylistInfo: %s", exc)
        return
    logger.info("Deleted OTGPlaylistInfo")
