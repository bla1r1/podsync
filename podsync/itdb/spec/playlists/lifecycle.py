"""Apply a user edit to a playlist row and keep its redundant fields consistent."""

from __future__ import annotations

from typing import Any

from podsync.itdb.spec.playlists.kinds import is_playlist_folder, is_podcast_playlist, playlist_kind_flags
from podsync.itdb.spec.playlists.properties import normalize_playlist_description

__all__ = ["playlist_edit_payload"]


def _parent_id(row: dict[str, Any]) -> int:
    raw = row.get("parent_folder_playlist_id", row.get("unk0x30_playlist_ref"))
    try:
        return int(raw or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def playlist_edit_payload(existing_row: dict[str, Any] | None, changes: dict[str, Any]) -> dict[str, Any]:
    """A new row: *existing_row* overlaid with *changes*, with every mirror field re-derived.

    The kind word is written back under both spellings with its decoded booleans,
    the parent folder under both names, and the description in all its places.
    """
    row = {**(existing_row or {}), **changes}
    normalize_playlist_description(row)

    flags = playlist_kind_flags(row)
    parent = _parent_id(row)
    row.update(
        playlist_kind_flags=flags,
        podcast_flag=flags,
        is_folder=is_playlist_folder(flags),
        is_podcast=is_podcast_playlist(flags),
        parent_folder_playlist_id=parent,
        unk0x30_playlist_ref=parent,
    )
    return row
