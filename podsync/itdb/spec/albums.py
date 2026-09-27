"""When do two tracks belong to the same album?

Album name and show must match (case-insensitively).  If both tracks name an
album artist, those must match; otherwise the track artists decide.  Grouping
preserves first-seen order of albums and of tracks within them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")

__all__ = [
    "AlbumGroup",
    "AlbumIdentity",
    "album_identity_from_mapping",
    "album_identity_from_track",
    "albums_match",
    "group_tracks_by_album_identity",
]


@dataclass(frozen=True)
class AlbumIdentity:
    """The fields that decide which tracks belong to one album."""

    album: str | None
    album_artist: str | None
    artist: str | None
    show_name: str | None


def _text(value: object) -> str | None:
    """Stripped text, with empty/blank treated as missing."""
    return (str(value).strip() or None) if value is not None else None


def album_identity_from_track(track: object) -> AlbumIdentity:
    """Identity of a record-like object (``TrackRecord`` or anything with the same attributes)."""
    return AlbumIdentity(
        *(_text(getattr(track, name, None)) for name in ("album", "album_artist", "artist", "show_name"))
    )


def _first(track: Mapping, *keys: str) -> object:
    return next((track[key] for key in keys if track.get(key)), None)


def album_identity_from_mapping(track: Mapping) -> AlbumIdentity:
    """Identity of a parsed row; the show may appear under several historical keys."""
    return AlbumIdentity(
        album=_text(_first(track, "album")),
        album_artist=_text(_first(track, "album_artist")),
        artist=_text(_first(track, "artist")),
        show_name=_text(_first(track, "show", "Show Name", "TV Show", "show_name")),
    )


def _fold(value: str | None) -> str | None:
    return value.casefold() if value is not None else None


def albums_match(left: AlbumIdentity, right: AlbumIdentity) -> bool:
    """Same show and album (case-insensitive); album artist decides when both have one."""
    same = lambda attr: _fold(getattr(left, attr)) == _fold(getattr(right, attr))  # noqa: E731
    if not (same("show_name") and same("album")):
        return False
    if left.album_artist and right.album_artist:
        return same("album_artist")
    return same("artist")


@dataclass
class AlbumGroup(Generic[T]):
    """One album of the album list and the tracks in it."""

    identity: AlbumIdentity
    tracks: list[T] = field(default_factory=list)


def group_tracks_by_album_identity(
    tracks: Iterable[T], identity_fn: Callable[[T], AlbumIdentity],
) -> list[AlbumGroup[T]]:
    """Group *tracks* into albums, in first-seen order."""
    groups: list[AlbumGroup[T]] = []
    # Candidates are bucketed by (album, show) so matching stays cheap for big libraries.
    candidates: dict[tuple[str, str], list[AlbumGroup[T]]] = {}
    for track in tracks:
        identity = identity_fn(track)
        bucket = candidates.setdefault(
            ((identity.album or "").casefold(), (identity.show_name or "").casefold()), [],
        )
        home = next((group for group in bucket if albums_match(group.identity, identity)), None)
        if home is None:
            home = AlbumGroup(identity=identity)
            bucket.append(home)
            groups.append(home)
        home.tracks.append(track)
    return groups
