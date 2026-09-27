"""Point tracks at their cover art using the song links stored in the ArtworkDB.

The ArtworkDB is the authority on which image belongs to which track
(``songId`` → ``img_id``).  Track rows whose artwork reference is missing or
stale are corrected; a reference that already points at an image of the same
song is left alone.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

__all__ = ["attach_cover_refs"]

logger = logging.getLogger(__name__)


def _artworkdb_beside(itunesdb_path: str | Path) -> Path:
    # iPod_Control/iTunes/iTunesDB -> iPod_Control/Artwork/ArtworkDB
    return Path(itunesdb_path).parent.parent / "Artwork" / "ArtworkDB"


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _song_links(artworkdb: Path) -> list[tuple[int, int]]:
    """``(song_id, image_id)`` pairs of every image entry, in file order."""
    if not artworkdb.exists():
        return []
    try:
        from podsync.artwork.reader import parse_artworkdb

        parsed = parse_artworkdb(str(artworkdb))
    except Exception as exc:
        logger.debug("Could not parse ArtworkDB for artwork links: %s", exc)
        return []
    links = []
    for image in parsed.get("mhli", []) if isinstance(parsed, dict) else []:
        if isinstance(image, dict):
            song = _as_int(image.get("songId") or image.get("song_id"))
            picture = _as_int(image.get("img_id"))
            if song and picture:
                links.append((song, picture))
    return links


def _images_by_song(artworkdb: str | Path) -> dict[int, int]:
    """Song id → its first image id."""
    found: dict[int, int] = {}
    for song, picture in _song_links(Path(artworkdb)):
        found.setdefault(song, picture)
    return found


def _songs_by_image(artworkdb: str | Path) -> dict[int, int]:
    """Image id → the (last) song that references it."""
    return {picture: song for song, picture in _song_links(Path(artworkdb))}


def attach_cover_refs(tracks: list[dict], itunesdb_path: str | Path) -> int:
    """Correct ``artwork_link``/``mhii_link`` on *tracks* in place; returns how many changed."""
    if not tracks:
        return 0
    artworkdb = _artworkdb_beside(itunesdb_path)
    image_for_song = _images_by_song(artworkdb)
    if not image_for_song:
        return 0
    song_for_image: dict[int, int] | None = None  # only needed for tracks with an existing ref

    changed = 0
    for track in tracks:
        if not isinstance(track, dict):
            continue
        song = _as_int(track.get("db_track_id") or track.get("db_id"))
        picture = image_for_song.get(song) if song else None
        if not picture:
            continue
        current = _as_int(track.get("artwork_link"))
        if current == picture:
            continue
        if current:
            if song_for_image is None:
                song_for_image = _songs_by_image(artworkdb)
            if song_for_image.get(current) == song:
                continue  # another image of the same song: still valid
            logger.info("Corrected stale track artwork ref for db_track_id=%d: %d -> %d", song, current, picture)
        track["artwork_link"] = track["mhii_link"] = picture
        if not track.get("artwork_count"):
            track["artwork_count"] = 1
        changed += 1
    if changed:
        logger.info("Normalized %d track artwork refs from ArtworkDB song links", changed)
    return changed
