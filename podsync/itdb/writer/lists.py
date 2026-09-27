"""Datasets and the list chunks inside them: tracks, albums, artists and playlists.

Album and artist lists are derived from the track records; building them also
assigns ``album_id`` on each record, which the track writer then stores.
"""

from __future__ import annotations

import random
import struct
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from podsync.itdb.spec.albums import AlbumGroup, album_identity_from_track, group_tracks_by_album_identity
from podsync.itdb.spec.codes import MHOD_TYPE_ARTIST_NAME
from podsync.itdb.spec.fields import (
    MHLA_HEADER_SIZE,
    MHLI_HEADER_SIZE,
    MHLP_HEADER_SIZE,
    MHLT_HEADER_SIZE,
    list_chunk_bytes,
    list_header_bytes,
    pack_chunk_header,
)
from podsync.itdb.spec.layouts.album import MHIA_HEADER_SIZE
from podsync.itdb.spec.layouts.artist import MHII_HEADER_SIZE
from podsync.itdb.spec.layouts.dataset import MHSD_HEADER_SIZE
from podsync.itdb.spec.playlists.tree import normalize_playlist_tree
from podsync.itdb.writer._build import nonzero_random_u64, record_bytes
from podsync.itdb.writer.playlist import PlaylistRecord, write_master_playlist, write_playlist
from podsync.itdb.writer.smart_rules import prefs_from_row, rules_from_row
from podsync.itdb.writer.strings import write_mhod_string
from podsync.itdb.writer.track import TrackRecord, write_mhit

# ── datasets ────────────────────────────────────────────────────────


def write_mhsd(dataset_type: int, child_data: bytes) -> bytes:
    """Wrap one list chunk in an MHSD of the given type."""
    header = bytearray(MHSD_HEADER_SIZE)
    pack_chunk_header(header, 0, b"mhsd", MHSD_HEADER_SIZE, MHSD_HEADER_SIZE + len(child_data))
    struct.pack_into("<I", header, 0x0C, dataset_type)
    return bytes(header) + bytes(child_data)


def write_mhsd_type1(track_list_data) -> bytes:
    """Dataset 1: the track list (``mhlt`` with every ``mhit``)."""
    return write_mhsd(1, track_list_data)


def write_mhsd_type2(playlist_list_data) -> bytes:
    """Dataset 2: the playlist list as older firmware reads it (podcasts flat)."""
    return write_mhsd(2, playlist_list_data)


def write_mhsd_type3(podcast_list_data) -> bytes:
    """Dataset 3: the playlist list with podcast episodes grouped under their shows."""
    return write_mhsd(3, podcast_list_data)


def write_mhsd_type4(album_list_data) -> bytes:
    """Dataset 4: the album list (``mhla``)."""
    return write_mhsd(4, album_list_data)


def write_mhsd_smart_type5(smart_playlist_data) -> bytes:
    """Dataset 5: the built-in smart playlists (``mhlp`` of smart ``mhyp``)."""
    return write_mhsd(5, smart_playlist_data)


def write_mhsd_type8(artist_list_data) -> bytes:
    """Dataset 8: the artist list (``mhli``)."""
    return write_mhsd(8, artist_list_data)


def write_mhsd_empty_stub(dataset_type) -> bytes:
    """A dataset holding an empty list (used for kinds the writer does not generate)."""
    return write_mhsd(dataset_type, list_header_bytes(b"mhlt", MHLT_HEADER_SIZE, 0))


# ── tracks ──────────────────────────────────────────────────────────


def write_mhlt(
    tracks: list[TrackRecord], start_track_id: int, db_id_2: int, capabilities=None, db_version: int = 0,
) -> tuple[bytes, int]:
    """The track list with sequential track ids from *start_track_id*; returns the next free id.

    A failing track re-raises the same exception type with the track named in
    the message (falling back to the original exception if that type cannot be
    rebuilt from a message).
    """
    chunks: list[bytes] = []
    for track_id, track in enumerate(tracks, start=start_track_id):
        try:
            chunks.append(write_mhit(track, track_id, db_id_2, capabilities=capabilities, db_version=db_version))
        except Exception as exc:
            described = f"{exc} (track #{track_id}: {getattr(track, 'artist', None)!r} – {getattr(track, 'title', None)!r})"
            try:
                annotated = type(exc)(described)
            except Exception:
                raise exc from None
            raise annotated from exc
    header = list_header_bytes(b"mhlt", MHLT_HEADER_SIZE, len(chunks))
    return header + b"".join(chunks), start_track_id + len(chunks)


# ── albums ──────────────────────────────────────────────────────────

_ALBUM_STRING_KINDS = (200, 201, 202, 203, 204)  # name, artist, sort artist, feed URL, show


def write_mhia(
    album_id: int,
    album_name: str,
    album_artist: str,
    sort_album_artist: str = "",
    podcast_url: str = "",
    show_name: str = "",
    is_compilation: bool = False,
    album_track_db_id: int = 0,
) -> bytes:
    """One album entry; empty strings are simply left out."""
    texts = (album_name, album_artist, sort_album_artist, podcast_url, show_name)
    children = [chunk for kind, text in zip(_ALBUM_STRING_KINDS, texts) if (chunk := write_mhod_string(kind, text))]
    return record_bytes(b"mhia", MHIA_HEADER_SIZE, {
        "child_count": len(children),
        "album_id": album_id,
        "sql_id": nonzero_random_u64(),
        "platform_flag": 2,
        "album_compilation_flag": int(bool(is_compilation)),
        "album_track_db_id": album_track_db_id,
    }, b"".join(children))


def write_mhla_empty() -> bytes:
    """An album list with no albums."""
    return list_chunk_bytes(b"mhla", MHLA_HEADER_SIZE, [])


def _first_attr(records: Sequence[Any], *names: str) -> str:
    for name in names:
        value = next((getattr(r, name) for r in records if getattr(r, name, None)), None)
        if value:
            return value
    return ""


def _album_entry(group: AlbumGroup, album_id: int) -> bytes:
    identity, members = group.identity, group.tracks
    return write_mhia(
        album_id,
        identity.album or "",
        identity.album_artist or identity.artist or "",
        sort_album_artist=_first_attr(members, "sort_album_artist") or _first_attr(members, "sort_artist"),
        podcast_url=_first_attr(members, "podcast_rss_url"),
        show_name=identity.show_name or getattr(members[0], "show_name", None) or "",
        is_compilation=any(getattr(t, "compilation_flag", False) for t in members),
        album_track_db_id=int(getattr(members[0], "db_track_id", 0) or 0),
    )


def write_mhla(tracks, starting_index_for_album_id) -> tuple[bytes, dict[tuple[str, str], int], int]:
    """Album list built from the tracks, sorted by (album, artist, show).

    Returns the chunk, ``{(album, album artist): id}`` and the next free id, and
    sets ``album_id`` on every track.
    """
    groups = group_tracks_by_album_identity(list(tracks or []), album_identity_from_track)
    groups.sort(key=lambda g: (
        g.identity.album or "", g.identity.album_artist or g.identity.artist or "", g.identity.show_name or "",
    ))
    ids: dict[tuple[str, str], int] = {}
    chunks: list[bytes] = []
    for album_id, group in enumerate(groups, start=starting_index_for_album_id):
        identity = group.identity
        ids[(identity.album or "", identity.album_artist or identity.artist or "")] = album_id
        for track in group.tracks:
            track.album_id = album_id
        chunks.append(_album_entry(group, album_id))
    return list_chunk_bytes(b"mhla", MHLA_HEADER_SIZE, chunks), ids, starting_index_for_album_id + len(groups)


# ── artists ─────────────────────────────────────────────────────────


def write_mhii_artist(artist_id: int, artist_name: str) -> bytes:
    """One artist-list item carrying the artist name."""
    name = write_mhod_string(MHOD_TYPE_ARTIST_NAME, artist_name) if artist_name else b""
    return record_bytes(b"mhii", MHII_HEADER_SIZE, {
        "child_count": 1 if name else 0,
        "artist_id": artist_id,
        "sql_id": random.getrandbits(64),
        "platform_flag": 2,
    }, name)


def write_mhli_empty() -> bytes:
    """An artist list with no artists."""
    return list_chunk_bytes(b"mhli", MHLI_HEADER_SIZE, [])


def write_mhli(tracks, starting_index_for_artist_id) -> tuple[bytes, dict[str, int], int]:
    """Artist list, one entry per case-insensitive artist name (first spelling wins)."""
    spelled: dict[str, str] = {}
    for track in tracks or []:
        artist = getattr(track, "artist", None)
        if artist:
            spelled.setdefault(artist.lower(), artist)
    ids: dict[str, int] = {}
    chunks: list[bytes] = []
    for artist_id, key in enumerate(sorted(spelled), start=starting_index_for_artist_id):
        ids[key] = artist_id
        chunks.append(write_mhii_artist(artist_id, spelled[key]))
    return list_chunk_bytes(b"mhli", MHLI_HEADER_SIZE, chunks), ids, starting_index_for_artist_id + len(chunks)


# ── playlists ───────────────────────────────────────────────────────


def write_mhlp_empty() -> bytes:
    """A playlist list with no playlists."""
    return list_chunk_bytes(b"mhlp", MHLP_HEADER_SIZE, [])


def write_mhlp(playlist_chunks: list[bytes]) -> bytes:
    """A playlist list wrapping already-serialised ``mhyp`` chunks."""
    return list_chunk_bytes(b"mhlp", MHLP_HEADER_SIZE, playlist_chunks)


def _as_tree_row(index: int, playlist: PlaylistRecord) -> dict[str, Any]:
    return {
        "_index": index,
        "playlist_id": playlist.playlist_id or 0,
        "playlist_kind_flags": playlist.kind_flags,
        "podcast_flag": playlist.kind_flags,
        "is_folder": playlist.is_folder,
        "is_podcast": playlist.is_podcast,
        "parent_folder_playlist_id": playlist.parent_folder_playlist_id or 0,
        "items": [{"db_track_id": track_id} for track_id in playlist.track_ids],
    }


def _with_folder_structure(playlists: Iterable[PlaylistRecord]) -> list[PlaylistRecord]:
    """Records re-ordered into folder pre-order, with folder membership and rules rebuilt."""
    originals = list(playlists or [])
    if not originals:
        return []
    fixed: list[PlaylistRecord] = []
    for row in normalize_playlist_tree([_as_tree_row(i, p) for i, p in enumerate(originals)]):
        original = originals[row["_index"]]
        members = [item["db_track_id"] for item in row.get("items") or [] if isinstance(item, dict) and "db_track_id" in item]
        changes: dict[str, Any] = {
            "track_ids": members,
            "playlist_kind_flags": row["playlist_kind_flags"],
            "podcast_flag": row["playlist_kind_flags"],
            "parent_folder_playlist_id": row["parent_folder_playlist_id"],
        }
        if len(members) != len(original.track_ids):
            changes["item_metadata"] = None
        if row.get("is_folder"):
            changes["smart_prefs"] = original.smart_prefs or prefs_from_row(row.get("smart_playlist_data") or {})
            changes["smart_rules"] = rules_from_row(row.get("smart_playlist_rules") or {})
            changes["item_metadata"] = None
        fixed.append(replace(original, **changes))
    return fixed


def write_mhlp_with_playlists(
    track_ids, playlists, db_id_2, tracks=None, capabilities=None,
    master_playlist_name="iPod", master_playlist_id=None,
) -> bytes:
    """Dataset-2 playlist list: the master playlist followed by the user's playlists."""
    master = write_master_playlist(
        track_ids, db_id_2, name=master_playlist_name, tracks=tracks,
        capabilities=capabilities, playlist_id=master_playlist_id,
    )
    return write_mhlp([master, *(write_playlist(p, db_id_2=db_id_2) for p in _with_folder_structure(playlists))])


def write_mhlp_with_playlists_type3(
    track_ids, playlists, db_id_2, track_album_map, tracks=None, capabilities=None,
    master_playlist_name="iPod", next_mhip_id_start=1, master_playlist_id=None,
) -> bytes:
    """Dataset-3 playlist list: like dataset 2, but podcast playlists group episodes by album."""
    master = write_master_playlist(
        track_ids, db_id_2, name=master_playlist_name, tracks=tracks,
        capabilities=capabilities, playlist_id=master_playlist_id,
    )
    others = [
        write_playlist(p, db_id_2=db_id_2, podcast_grouping=True,
                       track_album_map=track_album_map, next_mhip_id_start=next_mhip_id_start)
        for p in _with_folder_structure(playlists)
    ]
    return write_mhlp([master, *others])


def write_mhlp_smart(playlists, db_id_2=0) -> bytes:
    """Dataset-5 list (categories and smart playlists); no master playlist here."""
    fixed = _with_folder_structure(playlists)
    return write_mhlp([write_playlist(p, db_id_2=db_id_2) for p in fixed]) if fixed else write_mhlp_empty()
